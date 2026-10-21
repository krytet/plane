import type { AdapterConfig, ThreadTarget } from "./config.ts";
import { assertSupportedProviders, normalizeUuid } from "./config.ts";
import { ChatSyncError, codeForError } from "./errors.ts";
import type { CodexReader, CodexTurn } from "./app-server.ts";
import type { PlaneBinding, PlaneClient } from "./plane-client.ts";
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
import { getFinalAgentText, parseStatusBlock } from "./status-block.ts";

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

async function syncTarget(
  reader: CodexReader,
  plane: PlaneClient,
  target: ThreadTarget,
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

  const turns = await reader.listTurns(target.threadId);
  const newest = turns[0];
  if (!newest) return logSkipped(log, target, "no_turns");
  if (newest.status !== "completed" || newest.completedAt === null) {
    return logSkipped(log, target, "latest_turn_not_completed");
  }
  if (state.lastObservedTurnId === newest.id) {
    return logSkipped(log, target, "turn_already_observed");
  }

  const finalText = getFinalAgentText(newest.items);
  if (!finalText) {
    state.lastObservedTurnId = newest.id;
    await saveState();
    return logSkipped(log, target, "no_final_agent_message", "status_block_missing");
  }

  let progress;
  try {
    progress = parseStatusBlock(finalText);
  } catch (error) {
    state.lastObservedTurnId = newest.id;
    await saveState();
    return logSkipped(log, target, "invalid_status_block", codeForError(error));
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
  const payload: PlanePayload = {
    provider: "codex",
    thread_id: normalizeUuid(target.threadId),
    url: target.url,
    ...(target.title ? { title: target.title } : {}),
    cursor,
    progress,
  };

  if (remote && bindingMatchesContent(remote, payload)) {
    commit(state, newest, { ...payload, cursor: remote.sync_cursor });
    await saveState();
    log({ event: "already_synced", threadId: target.threadId, issueId: target.issueId });
    return { synced: 1, skipped: 0, errors: 0 };
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
    return { synced: 1, skipped: 0, errors: 0 };
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
        return { synced: 1, skipped: 0, errors: 0 };
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
      return { synced: 1, skipped: 0, errors: 0 };
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
  const found = await reader.findThreads(targets.map((target) => target.threadId));

  for (const target of targets) {
    if (!found.has(target.threadId)) {
      const result = logSkipped(log, target, "thread_not_found", "thread_not_found");
      summary.skipped += result.skipped;
      summary.errors += result.errors;
      continue;
    }

    try {
      // Targets are processed serially so one shared state file is updated in
      // a deterministic order.
      // oxlint-disable-next-line no-await-in-loop
      const result = await syncTarget(reader, plane, target, getBindingState(state, target), saveState, log);
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

export function stateEntryCount(state: SyncState): number {
  return Object.keys(state.bindings).length;
}
