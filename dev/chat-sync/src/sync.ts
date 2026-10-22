import type { AdapterConfig, ThreadTarget } from "./config.ts";
import { assertSupportedProviders, normalizeUuid } from "./config.ts";
import { ChatSyncError, codeForError } from "./errors.ts";
import { HookSpoolReader, timestampMicros, type HookSpoolEvent } from "./hook-spool.ts";
import { syncHookObservations } from "./hook-sync.ts";
import type { CodexReader, CodexTurn } from "./app-server.ts";
import type { PlaneBinding, PlaneClient, PlaneObservation, PlaneObservationWriteResult } from "./plane-client.ts";
import {
  getBindingState,
  nextCursor,
  payloadContentHash,
  payloadHash,
  type BindingState,
  type PendingWrite,
  type PlanePayload,
  type SyncState,
} from "./state.ts";
import { getFinalAgentText, parseProgressFromFinalText } from "./status-block.ts";

export interface SyncEvent {
  event: string;
  threadId?: string;
  issueId?: string;
  code?: string;
}

export type EventLogger = (event: SyncEvent) => void;

export interface CycleResult {
  synced: number;
  skipped: number;
  errors: number;
}

type ReconcileResult = { kind: "applied"; binding: PlaneBinding } | { kind: "not_applied" } | { kind: "conflict" };

function findRemoteBinding(bindings: PlaneBinding[], target: ThreadTarget): PlaneBinding | null {
  const matches = bindings.filter(
    (binding) =>
      binding.provider === target.provider && normalizeUuid(binding.thread_id) === normalizeUuid(target.threadId)
  );
  if (matches.length > 1) throw new ChatSyncError("reconciliation_conflict");
  return matches[0] ?? null;
}

function equalLists(left: string[], right: string[]): boolean {
  return left.length === right.length && left.every((value, index) => value === right[index]);
}

function bindingMatchesPayload(binding: PlaneBinding, payload: PlanePayload): boolean {
  return (
    binding.provider === payload.provider &&
    binding.thread_id === payload.thread_id &&
    binding.sync_cursor === payload.cursor &&
    equalLists(binding.progress.done, payload.progress.done) &&
    equalLists(binding.progress.now, payload.progress.now) &&
    equalLists(binding.progress.waiting, payload.progress.waiting) &&
    binding.link?.url === payload.url &&
    (payload.title === undefined || binding.link?.title === payload.title)
  );
}

function bindingMatchesContent(binding: PlaneBinding, payload: PlanePayload): boolean {
  return (
    binding.provider === payload.provider &&
    binding.thread_id === payload.thread_id &&
    equalLists(binding.progress.done, payload.progress.done) &&
    equalLists(binding.progress.now, payload.progress.now) &&
    equalLists(binding.progress.waiting, payload.progress.waiting) &&
    binding.link?.url === payload.url &&
    (payload.title === undefined || binding.link?.title === payload.title)
  );
}

function reconcile(binding: PlaneBinding | null, pending: PendingWrite): ReconcileResult {
  if (!binding) return { kind: "not_applied" };
  if (binding.sync_cursor === pending.cursor) {
    return bindingMatchesPayload(binding, pending.payload) ? { kind: "applied", binding } : { kind: "conflict" };
  }
  if (binding.sync_cursor > pending.cursor) return { kind: "conflict" };
  return { kind: "not_applied" };
}

function commit(state: BindingState, turn: CodexTurn, payload: PlanePayload): void {
  state.cursor = Math.max(state.cursor, payload.cursor);
  state.lastTurnId = turn.id;
  state.lastObservedTurnId = turn.id;
  state.lastPayloadHash = payloadContentHash(payload);
  delete state.pending;
  delete state.blocked;
}

function block(state: BindingState, code: string): void {
  state.blocked = { code, at: new Date().toISOString() };
}

function logSkipped(log: EventLogger, target: ThreadTarget, event: string, code?: string): CycleResult {
  log({ event, threadId: target.threadId, issueId: target.issueId, ...(code ? { code } : {}) });
  return { synced: 0, skipped: 1, errors: code ? 1 : 0 };
}

function observationForTurn(turn: CodexTurn): PlaneObservation | null {
  if (turn.completedAt === null) return null;
  const state =
    turn.status === "completed"
      ? "answer_available"
      : turn.status === "interrupted"
        ? "needs_action"
        : turn.status === "failed"
          ? "failed"
          : null;
  if (!state) return null;
  return {
    state,
    observed_at: new Date(turn.completedAt * 1_000).toISOString(),
    turn_id: turn.id,
  };
}

function bindingMatchesObservation(binding: PlaneBinding, observation: PlaneObservation): boolean {
  if (
    binding.observation_state !== observation.state ||
    binding.observation_source !== "codex" ||
    binding.observation_turn_id !== observation.turn_id ||
    binding.observation_observed_at === null
  ) {
    return false;
  }
  const bindingObservedAt = timestampMicros(binding.observation_observed_at);
  const observationObservedAt = timestampMicros(observation.observed_at);
  return bindingObservedAt !== null && observationObservedAt !== null && bindingObservedAt === observationObservedAt;
}

async function reconcileObservation(
  plane: PlaneClient,
  target: ThreadTarget,
  bindingId: string,
  observation: PlaneObservation,
  log: EventLogger
): Promise<number> {
  let bindings: PlaneBinding[];
  try {
    bindings = await plane.listBindings(target);
  } catch (error) {
    if (error instanceof ChatSyncError && error.code === "plane_auth_failed") throw error;
    log({
      event: "observation_reconcile_failed",
      threadId: target.threadId,
      issueId: target.issueId,
      code: codeForError(error),
    });
    return 1;
  }

  let remote: PlaneBinding | null;
  try {
    remote = findRemoteBinding(bindings, target);
  } catch (error) {
    log({
      event: "observation_reconcile_failed",
      threadId: target.threadId,
      issueId: target.issueId,
      code: codeForError(error),
    });
    return 1;
  }

  if (remote?.id === bindingId && bindingMatchesObservation(remote, observation)) {
    log({ event: "observation_reconciled", threadId: target.threadId, issueId: target.issueId });
    return 0;
  }

  log({
    event: "observation_not_applied",
    threadId: target.threadId,
    issueId: target.issueId,
    code: "plane_write_not_applied",
  });
  return 1;
}

async function observeTurn(
  plane: PlaneClient,
  target: ThreadTarget,
  binding: PlaneBinding,
  turn: CodexTurn,
  log: EventLogger
): Promise<number> {
  const observation = observationForTurn(turn);
  if (!observation) return 0;
  const observedAt = timestampMicros(observation.observed_at);
  const remoteObservedAt = timestampMicros(binding.observation_observed_at);
  if (observedAt !== null && remoteObservedAt !== null && remoteObservedAt >= observedAt) {
    log({ event: "observation_already_applied", threadId: target.threadId, issueId: target.issueId });
    return 0;
  }

  let result: PlaneObservationWriteResult;
  try {
    result = await plane.syncObservation(target, binding.id, observation);
  } catch (error) {
    if (error instanceof ChatSyncError && error.code === "plane_auth_failed") throw error;
    log({
      event: "observation_failed",
      threadId: target.threadId,
      issueId: target.issueId,
      code: codeForError(error),
    });
    return 1;
  }

  if (result.kind === "success") {
    log({
      event: result.applied ? "observation_synced" : "observation_already_applied",
      threadId: target.threadId,
      issueId: target.issueId,
    });
    return 0;
  }

  if (result.kind === "uncertain") {
    return await reconcileObservation(plane, target, binding.id, observation, log);
  }

  log({
    event: "observation_failed",
    threadId: target.threadId,
    issueId: target.issueId,
    code: result.code ?? "observation_rejected",
  });
  return 1;
}

async function observeExistingTurn(
  plane: PlaneClient,
  target: ThreadTarget,
  turn: CodexTurn,
  log: EventLogger
): Promise<number> {
  let bindings: PlaneBinding[];
  try {
    bindings = await plane.listBindings(target);
  } catch (error) {
    if (error instanceof ChatSyncError && error.code === "plane_auth_failed") throw error;
    log({
      event: "observation_reconcile_failed",
      threadId: target.threadId,
      issueId: target.issueId,
      code: codeForError(error),
    });
    return 1;
  }
  const remote = findRemoteBinding(bindings, target);
  if (!remote) return 0;
  return await observeTurn(plane, target, remote, turn, log);
}

async function syncTarget(
  reader: CodexReader,
  plane: PlaneClient,
  target: ThreadTarget,
  progressMode: AdapterConfig["progressMode"],
  state: BindingState,
  saveState: () => Promise<void>,
  log: EventLogger
): Promise<CycleResult> {
  if (state.blocked) {
    return logSkipped(log, target, "blocked", state.blocked.code);
  }

  if (state.pending) {
    let bindings: PlaneBinding[];
    try {
      bindings = await plane.listBindings(target);
    } catch (error) {
      return logSkipped(log, target, "reconcile_failed", codeForError(error));
    }

    let result: ReconcileResult;
    try {
      result = reconcile(findRemoteBinding(bindings, target), state.pending);
    } catch (error) {
      block(state, codeForError(error));
      await saveState();
      return logSkipped(log, target, "blocked", codeForError(error));
    }

    if (result.kind === "applied") {
      commit(
        state,
        { id: state.pending.turnId, status: "completed", completedAt: null, items: [] },
        {
          ...state.pending.payload,
          cursor: result.binding.sync_cursor,
        }
      );
      await saveState();
      log({ event: "reconciled", threadId: target.threadId, issueId: target.issueId });
      return { synced: 1, skipped: 0, errors: 0 };
    }

    if (result.kind === "not_applied") {
      delete state.pending;
      state.cursor = Math.max(state.cursor, findRemoteBinding(bindings, target)?.sync_cursor ?? 0);
      await saveState();
      return logSkipped(log, target, "write_not_applied", "plane_write_not_applied");
    }

    block(state, "reconciliation_conflict");
    await saveState();
    return logSkipped(log, target, "blocked", "reconciliation_conflict");
  }

  const thread = await reader.readThread(target.threadId);
  if (!thread) return logSkipped(log, target, "thread_not_found", "thread_not_found");

  const turns = await reader.listTurns(target.threadId);
  const newest = turns[0];
  if (!newest) return logSkipped(log, target, "no_turns");
  if (newest.status !== "completed" || newest.completedAt === null) {
    if (observationForTurn(newest)) {
      const errors = await observeExistingTurn(plane, target, newest, log);
      return { synced: errors ? 0 : 1, skipped: errors ? 1 : 0, errors };
    }
    return logSkipped(log, target, "latest_turn_not_completed");
  }
  if (state.lastObservedTurnId === newest.id) {
    const observation = observationForTurn(newest);
    if (!observation) return logSkipped(log, target, "turn_already_observed");
    let bindings: PlaneBinding[];
    try {
      bindings = await plane.listBindings(target);
    } catch (error) {
      return logSkipped(log, target, "observation_reconcile_failed", codeForError(error));
    }
    const remote = findRemoteBinding(bindings, target);
    if (!remote || remote.sync_cursor === 0 || bindingMatchesObservation(remote, observation)) {
      return logSkipped(log, target, "turn_already_observed");
    }
    const errors = await observeTurn(plane, target, remote, newest, log);
    return { synced: errors ? 0 : 1, skipped: errors ? 1 : 0, errors };
  }

  const finalText = getFinalAgentText(newest.items);
  if (!finalText) {
    state.lastObservedTurnId = newest.id;
    await saveState();
    const errors = await observeExistingTurn(plane, target, newest, log);
    const result = logSkipped(log, target, "no_final_agent_message", "status_block_missing");
    return { ...result, errors: result.errors + errors };
  }

  let progress;
  try {
    progress = parseProgressFromFinalText(finalText, progressMode).progress;
  } catch (error) {
    state.lastObservedTurnId = newest.id;
    await saveState();
    const errors = await observeExistingTurn(plane, target, newest, log);
    const result = logSkipped(log, target, "invalid_status_block", codeForError(error));
    return { ...result, errors: result.errors + errors };
  }

  let bindings: PlaneBinding[];
  try {
    bindings = await plane.listBindings(target);
  } catch (error) {
    return logSkipped(log, target, "remote_read_failed", codeForError(error));
  }

  const remote = findRemoteBinding(bindings, target);
  const floor = Math.max(state.cursor, remote?.sync_cursor ?? 0);
  state.cursor = floor;
  const cursor = nextCursor(floor, newest.completedAt);
  const remoteLink = remote?.link;
  const payloadTitle = remoteLink ? remoteLink.title?.trim() : target.title?.trim();
  const payload: PlanePayload = {
    provider: "codex",
    thread_id: normalizeUuid(target.threadId),
    url: remoteLink?.url.trim() || target.url,
    ...(payloadTitle ? { title: payloadTitle } : {}),
    cursor,
    progress,
  };

  if (remote && bindingMatchesContent(remote, payload)) {
    commit(state, newest, { ...payload, cursor: remote.sync_cursor });
    await saveState();
    log({ event: "already_synced", threadId: target.threadId, issueId: target.issueId });
    const errors = await observeTurn(plane, target, remote, newest, log);
    return { synced: 1, skipped: 0, errors };
  }

  state.pending = {
    cursor,
    turnId: newest.id,
    payloadHash: payloadHash(payload),
    payload,
  };
  await saveState();

  const writeResult = await plane.syncBinding(target, payload);

  if (writeResult.kind === "success") {
    commit(state, newest, payload);
    await saveState();
    log({ event: "synced", threadId: target.threadId, issueId: target.issueId });
    const errors = await observeTurn(plane, target, writeResult.binding, newest, log);
    return { synced: 1, skipped: 0, errors };
  }

  if (writeResult.kind === "rejected") {
    try {
      bindings = await plane.listBindings(target);
      const result = reconcile(findRemoteBinding(bindings, target), {
        cursor,
        turnId: newest.id,
        payloadHash: payloadHash(payload),
        payload,
      });
      if (result.kind === "applied") {
        commit(state, newest, payload);
        await saveState();
        log({ event: "reconciled", threadId: target.threadId, issueId: target.issueId });
        const errors = await observeTurn(plane, target, result.binding, newest, log);
        return { synced: 1, skipped: 0, errors };
      }
    } catch (error) {
      if (error instanceof ChatSyncError && error.code === "plane_auth_failed") throw error;
      // Keep the pending write for reconciliation on the next cycle.
    }
    block(state, writeResult.code ?? "plane_write_rejected");
    await saveState();
    return logSkipped(log, target, "write_rejected", writeResult.code ?? "plane_write_rejected");
  }

  try {
    bindings = await plane.listBindings(target);
    const result = reconcile(findRemoteBinding(bindings, target), {
      cursor,
      turnId: newest.id,
      payloadHash: payloadHash(payload),
      payload,
    });
    if (result.kind === "applied") {
      commit(state, newest, payload);
      await saveState();
      log({ event: "reconciled", threadId: target.threadId, issueId: target.issueId });
      const errors = await observeTurn(plane, target, result.binding, newest, log);
      return { synced: 1, skipped: 0, errors };
    }
    if (result.kind === "not_applied") delete state.pending;
    else block(state, "reconciliation_conflict");
    await saveState();
    return logSkipped(
      log,
      target,
      "write_uncertain",
      result.kind === "not_applied" ? "plane_write_not_applied" : "reconciliation_conflict"
    );
  } catch {
    await saveState();
    return logSkipped(log, target, "write_uncertain", "plane_write_uncertain");
  }
}

export async function runCycle(
  reader: CodexReader,
  plane: PlaneClient,
  config: AdapterConfig,
  state: SyncState,
  saveState: () => Promise<void>,
  log: EventLogger = () => undefined
): Promise<CycleResult> {
  assertSupportedProviders(config);
  const summary: CycleResult = { synced: 0, skipped: 0, errors: 0 };
  const targets = config.threads;
  const spool = config.hookSpoolDir ? new HookSpoolReader(config.hookSpoolDir) : null;
  const configuredThreads = new Set(targets.map((target) => target.threadId.toLowerCase()));
  let hookEvents: HookSpoolEvent[] = [];
  if (spool) {
    try {
      const batch = await spool.readBatch(Date.now(), configuredThreads);
      hookEvents = batch.events;
      summary.skipped += batch.invalid.length;
      await Promise.all(
        batch.invalid.map(async (invalid) => {
          log({ event: "hook_spool_rejected", code: invalid.code });
          try {
            await spool.reject(invalid.fileName);
          } catch {
            log({ event: "hook_spool_reject_failed", code: invalid.code });
          }
        })
      );
    } catch (error) {
      log({ event: "hook_spool_read_failed", code: codeForError(error) });
      summary.errors += 1;
    }
  }

  const unknownEvents = hookEvents.filter((event) => !configuredThreads.has(event.thread_id));
  summary.skipped += unknownEvents.length;
  await Promise.all(
    unknownEvents.map(async (event) => {
      log({ event: "hook_unknown_binding", threadId: event.thread_id });
      if (!spool) return;
      try {
        await spool.acknowledge(event.id);
      } catch {
        log({ event: "hook_ack_failed", threadId: event.thread_id });
      }
    })
  );

  for (const target of targets) {
    try {
      if (spool) {
        const targetEvents = hookEvents.filter((event) => event.thread_id === target.threadId.toLowerCase());
        const bindingState = getBindingState(state, target);
        // oxlint-disable-next-line no-await-in-loop
        const hookResult = await syncHookObservations(plane, target, bindingState, targetEvents, spool, saveState, log);
        summary.synced += hookResult.synced;
        summary.skipped += hookResult.skipped;
        summary.errors += hookResult.errors;
      }

      // Targets are processed serially so one shared state file is updated in
      // a deterministic order.
      // oxlint-disable-next-line no-await-in-loop
      const result = await syncTarget(
        reader,
        plane,
        target,
        config.progressMode,
        getBindingState(state, target),
        saveState,
        log
      );
      summary.synced += result.synced;
      summary.skipped += result.skipped;
      summary.errors += result.errors;
    } catch (error) {
      log({
        event: "target_failed",
        threadId: target.threadId,
        issueId: target.issueId,
        code: codeForError(error),
      });
      summary.errors += 1;
      if (
        error instanceof ChatSyncError &&
        ["plane_auth_failed", "protocol_incompatible", "app_server_failed", "cli_incompatible"].includes(error.code)
      ) {
        throw error;
      }
    }
  }

  return summary;
}

export async function runDryRunCycle(
  reader: CodexReader,
  config: AdapterConfig,
  log: EventLogger = () => undefined
): Promise<CycleResult> {
  assertSupportedProviders(config);
  const summary: CycleResult = { synced: 0, skipped: 0, errors: 0 };

  for (const target of config.threads) {
    try {
      // Dry-run reads deliberately avoid Plane entirely.
      // oxlint-disable-next-line no-await-in-loop
      const thread = await reader.readThread(target.threadId);
      if (!thread) {
        const result = logSkipped(log, target, "thread_not_found", "thread_not_found");
        summary.skipped += result.skipped;
        summary.errors += result.errors;
        continue;
      }

      // oxlint-disable-next-line no-await-in-loop
      const turns = await reader.listTurns(target.threadId);
      const newest = turns[0];
      if (!newest) {
        const result = logSkipped(log, target, "no_turns");
        summary.skipped += result.skipped;
        continue;
      }
      if (newest.status !== "completed" || newest.completedAt === null) {
        const result = logSkipped(log, target, "latest_turn_not_completed");
        summary.skipped += result.skipped;
        continue;
      }

      const finalText = getFinalAgentText(newest.items);
      if (!finalText) {
        const result = logSkipped(log, target, "no_final_agent_message", "status_block_missing");
        summary.skipped += result.skipped;
        summary.errors += result.errors;
        continue;
      }

      const parsed = parseProgressFromFinalText(finalText, config.progressMode);
      log({
        event: "dry_run_target",
        threadId: target.threadId,
        issueId: target.issueId,
        code: parsed.source === "summary" ? "summary_available" : "structured_block",
      });
      summary.synced += 1;
    } catch (error) {
      log({
        event: "target_failed",
        threadId: target.threadId,
        issueId: target.issueId,
        code: codeForError(error),
      });
      summary.errors += 1;
      if (
        error instanceof ChatSyncError &&
        ["protocol_incompatible", "app_server_failed", "cli_incompatible"].includes(error.code)
      ) {
        throw error;
      }
    }
  }

  return summary;
}

export function stateEntryCount(state: SyncState): number {
  return Object.keys(state.bindings).length;
}
