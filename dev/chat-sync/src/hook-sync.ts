import type { ThreadTarget } from "./config.ts";
import { ChatSyncError } from "./errors.ts";
import type { HookSpoolEvent, HookSpoolReader } from "./hook-spool.ts";
import { selectHookEvent, timestampMicros } from "./hook-spool.ts";
import type { PlaneBinding, PlaneClient, PlaneObservation } from "./plane-client.ts";
import type { BindingState, PendingObservationWrite } from "./state.ts";

export interface HookSyncResult {
  synced: number;
  skipped: number;
  errors: number;
}

type HookLog = (event: { event: string; threadId?: string; issueId?: string; code?: string }) => void;

function findRemoteBinding(bindings: PlaneBinding[], target: ThreadTarget): PlaneBinding | null {
  const matches = bindings.filter(
    (binding) => binding.provider === "codex" && binding.thread_id === target.threadId.toLowerCase()
  );
  if (matches.length > 1) throw new ChatSyncError("reconciliation_conflict");
  return matches[0] ?? null;
}

function observationMatches(binding: PlaneBinding, observation: PlaneObservation): boolean {
  const remoteObservedAt = timestampMicros(binding.observation_observed_at);
  const observationObservedAt = timestampMicros(observation.observed_at);
  return (
    binding.observation_source === "codex" &&
    binding.observation_state === observation.state &&
    binding.observation_turn_id === observation.turn_id &&
    remoteObservedAt !== null &&
    observationObservedAt !== null &&
    remoteObservedAt === observationObservedAt
  );
}

function remoteSupersedes(binding: PlaneBinding, observation: PlaneObservation): boolean {
  const remoteTime = timestampMicros(binding.observation_observed_at);
  const pendingTime = timestampMicros(observation.observed_at);
  return remoteTime !== null && pendingTime !== null && remoteTime >= pendingTime;
}

function appliedHook(state: BindingState, pending: PendingObservationWrite): NonNullable<BindingState["hook"]> {
  return {
    ...state.hook,
    lastObservedAt: pending.observation.observed_at,
    ...(pending.eventId ? { lastEventId: pending.eventId } : {}),
    ...(pending.event ? { lastEvent: pending.event } : {}),
    lastState: pending.observation.state,
  };
}

function observationFromEvent(event: HookSpoolEvent, resumeAfterStop: boolean): PlaneObservation {
  return {
    state: event.state,
    observed_at: event.observed_at,
    turn_id: event.turn_id,
    ...(resumeAfterStop && event.state === "running" ? { resume_after_stop: true } : {}),
  };
}

async function persistCheckpoint(
  state: BindingState,
  hook: NonNullable<BindingState["hook"]>,
  pending: PendingObservationWrite | undefined,
  saveState: () => Promise<void>
): Promise<void> {
  const previousHook = state.hook;
  const previousPending = state.pendingObservation;
  state.hook = hook;
  if (pending) state.pendingObservation = pending;
  else delete state.pendingObservation;
  try {
    await saveState();
  } catch (error) {
    if (previousHook) state.hook = previousHook;
    else delete state.hook;
    if (previousPending) state.pendingObservation = previousPending;
    else delete state.pendingObservation;
    throw error;
  }
}

type ReconcileDisposition = "applied" | "superseded" | "not_applied";

async function reconcilePending(
  plane: PlaneClient,
  target: ThreadTarget,
  pending: PendingObservationWrite
): Promise<ReconcileDisposition> {
  const bindings = await plane.listBindings(target);
  const remote = findRemoteBinding(bindings, target);
  if (!remote) return "superseded";
  if (observationMatches(remote, pending.observation)) return "applied";
  if (remoteSupersedes(remote, pending.observation)) return "superseded";
  return "not_applied";
}

async function acknowledge(
  spool: HookSpoolReader,
  eventId: string | undefined,
  log: HookLog,
  target: ThreadTarget
): Promise<void> {
  if (!eventId) return;
  try {
    await spool.acknowledge(eventId);
  } catch {
    log({ event: "hook_ack_failed", threadId: target.threadId, issueId: target.issueId });
  }
}

export async function syncHookObservations(
  plane: PlaneClient,
  target: ThreadTarget,
  state: BindingState,
  events: HookSpoolEvent[],
  spool: HookSpoolReader,
  saveState: () => Promise<void>,
  log: HookLog
): Promise<HookSyncResult> {
  const result: HookSyncResult = { synced: 0, skipped: 0, errors: 0 };
  let bindings: PlaneBinding[];
  try {
    bindings = await plane.listBindings(target);
  } catch (error) {
    if (error instanceof ChatSyncError && error.code === "plane_auth_failed") throw error;
    log({ event: "hook_binding_read_failed", threadId: target.threadId, issueId: target.issueId });
    return { synced: 0, skipped: events.length ? 1 : 0, errors: 1 };
  }

  let remote = findRemoteBinding(bindings, target);
  if (!remote) {
    if (state.pendingObservation) {
      const previousPending = state.pendingObservation;
      await persistCheckpoint(state, state.hook ?? {}, undefined, saveState);
      await acknowledge(spool, previousPending.eventId, log, target);
    }
    await Promise.all(events.map((event) => acknowledge(spool, event.id, log, target)));
    log({ event: "hook_unknown_binding", threadId: target.threadId, issueId: target.issueId });
    return { synced: 0, skipped: 1, errors: 0 };
  }

  let pending = state.pendingObservation;
  const pendingWasBlocked =
    pending?.eventId !== undefined && state.hook?.blockedObservation?.eventId === pending.eventId;
  if (pending && !pendingWasBlocked) {
    let disposition: ReconcileDisposition;
    try {
      disposition = await reconcilePending(plane, target, pending);
    } catch (error) {
      if (error instanceof ChatSyncError && error.code === "plane_auth_failed") throw error;
      log({ event: "hook_reconcile_failed", threadId: target.threadId, issueId: target.issueId });
      return { synced: 0, skipped: 1, errors: 1 };
    }

    if (disposition === "applied") {
      const applied = pending;
      await persistCheckpoint(state, appliedHook(state, applied), undefined, saveState);
      await acknowledge(spool, applied.eventId, log, target);
      events = events.filter((event) => event.id !== applied.eventId);
      log({ event: "hook_reconciled", threadId: target.threadId, issueId: target.issueId });
      result.synced += 1;
      pending = undefined;
    } else if (disposition === "superseded") {
      const superseded = pending;
      await persistCheckpoint(state, state.hook ?? {}, undefined, saveState);
      await acknowledge(spool, superseded.eventId, log, target);
      events = events.filter((event) => event.id !== superseded.eventId);
      log({ event: "hook_superseded", threadId: target.threadId, issueId: target.issueId });
      result.skipped += 1;
      pending = undefined;
    } else {
      log({ event: "hook_write_not_applied", threadId: target.threadId, issueId: target.issueId });
    }
  }

  const blockedEventId = state.hook?.blockedObservation?.eventId;
  const selectableEvents = blockedEventId ? events.filter((event) => event.id !== blockedEventId) : events;
  const selection = selectHookEvent(selectableEvents, remote.observation_observed_at, state.hook ?? {});
  let replacedPending: PendingObservationWrite | undefined;
  let candidate = selection.selected;
  if (candidate && pending) {
    const pendingTime = timestampMicros(pending.observation.observed_at);
    const candidateTime = timestampMicros(candidate.observed_at);
    if (pendingTime === null || candidateTime === null || candidateTime <= pendingTime) candidate = null;
    else {
      replacedPending = pending;
      pending = undefined;
    }
  }
  if (candidate) {
    pending = {
      eventId: candidate.id,
      event: candidate.event,
      eventTerminal: candidate.terminal,
      observation: observationFromEvent(candidate, selection.resumeAfterStop),
    };
  }

  const nextHook = {
    ...state.hook,
    ...(selection.activeTurnId ? { activeTurnId: selection.activeTurnId } : {}),
    ...(selection.activeTurnStartedAt ? { activeTurnStartedAt: selection.activeTurnStartedAt } : {}),
    terminalTurnIds: selection.terminalTurnIds,
    softStoppedTurnIds: selection.softStoppedTurnIds,
    ...(candidate ? {} : { blockedObservation: state.hook?.blockedObservation }),
  };
  delete nextHook.terminalTurnId;
  if (candidate) delete nextHook.blockedObservation;
  const lifecycleChanged = JSON.stringify(nextHook) !== JSON.stringify(state.hook ?? {});

  if (!pending && !lifecycleChanged) {
    await Promise.all(selection.superseded.map((event) => acknowledge(spool, event.id, log, target)));
    result.skipped += selection.superseded.length;
    if (state.hook?.blockedObservation) {
      if (state.hook.blockedObservation.eventId) {
        try {
          await spool.quarantine(state.hook.blockedObservation.eventId, state.hook.blockedObservation.code);
        } catch {
          await acknowledge(spool, state.hook.blockedObservation.eventId, log, target);
        }
      }
      log({
        event: "hook_observation_blocked",
        threadId: target.threadId,
        issueId: target.issueId,
        code: state.hook.blockedObservation.code,
      });
      result.skipped += 1;
    }
    return result;
  }

  const quarantinedPending = pendingWasBlocked && !candidate ? state.pendingObservation : undefined;
  await persistCheckpoint(state, nextHook, candidate ? pending : quarantinedPending ? undefined : pending, saveState);
  await Promise.all(selection.superseded.map((event) => acknowledge(spool, event.id, log, target)));
  result.skipped += selection.superseded.length;
  if (replacedPending) await acknowledge(spool, replacedPending.eventId, log, target);
  if (quarantinedPending) {
    if (quarantinedPending.eventId) {
      try {
        await spool.quarantine(quarantinedPending.eventId, state.hook?.blockedObservation?.code ?? "blocked");
      } catch {
        await acknowledge(spool, quarantinedPending.eventId, log, target);
      }
    }
    log({ event: "hook_observation_blocked", threadId: target.threadId, issueId: target.issueId });
    return { ...result, skipped: result.skipped + 1 };
  }
  if (!pending) return result;

  let writeResult;
  try {
    writeResult = await plane.syncObservation(target, remote.id, pending.observation);
  } catch (error) {
    if (error instanceof ChatSyncError && error.code === "plane_auth_failed") throw error;
    log({ event: "hook_observation_failed", threadId: target.threadId, issueId: target.issueId });
    return { ...result, skipped: result.skipped + 1, errors: result.errors + 1 };
  }

  if (writeResult.kind === "success") {
    const applied = pending;
    await persistCheckpoint(state, appliedHook(state, applied), undefined, saveState);
    await acknowledge(spool, applied.eventId, log, target);
    log({
      event: writeResult.applied ? "hook_observation_synced" : "hook_observation_already_applied",
      threadId: target.threadId,
      issueId: target.issueId,
    });
    result.synced += 1;
    return result;
  }

  let disposition: ReconcileDisposition;
  try {
    disposition = await reconcilePending(plane, target, pending);
  } catch (error) {
    if (error instanceof ChatSyncError && error.code === "plane_auth_failed") throw error;
    log({ event: "hook_reconcile_failed", threadId: target.threadId, issueId: target.issueId });
    return { ...result, skipped: result.skipped + 1, errors: result.errors + 1 };
  }

  if (disposition === "applied") {
    const applied = pending;
    await persistCheckpoint(state, appliedHook(state, applied), undefined, saveState);
    await acknowledge(spool, applied.eventId, log, target);
    log({ event: "hook_reconciled", threadId: target.threadId, issueId: target.issueId });
    result.synced += 1;
    return result;
  }
  if (disposition === "superseded") {
    const superseded = pending;
    await persistCheckpoint(state, state.hook ?? {}, undefined, saveState);
    await acknowledge(spool, superseded.eventId, log, target);
    log({ event: "hook_superseded", threadId: target.threadId, issueId: target.issueId });
    return { ...result, skipped: result.skipped + 1 };
  }

  if (writeResult.kind === "rejected" && writeResult.code === "stale_observation") {
    const stale = pending;
    await persistCheckpoint(state, state.hook ?? {}, undefined, saveState);
    await acknowledge(spool, stale.eventId, log, target);
    log({ event: "hook_superseded", threadId: target.threadId, issueId: target.issueId, code: "stale_observation" });
    return { ...result, skipped: result.skipped + 1 };
  }
  if (writeResult.kind === "rejected") {
    const blockedCode = writeResult.code ?? `http_${writeResult.status}`;
    const blocked = pending;
    const blockedHook = {
      ...state.hook,
      blockedObservation: {
        code: blockedCode,
        status: writeResult.status,
        at: new Date().toISOString(),
        ...(blocked.eventId ? { eventId: blocked.eventId } : {}),
      },
    };
    await persistCheckpoint(state, blockedHook, blocked, saveState);
    if (blocked.eventId) {
      try {
        await spool.quarantine(blocked.eventId, blockedCode);
      } catch {
        await acknowledge(spool, blocked.eventId, log, target);
      }
    }
    log({ event: "hook_observation_blocked", threadId: target.threadId, issueId: target.issueId, code: blockedCode });
    return { ...result, skipped: result.skipped + 1, errors: result.errors + 1 };
  }

  log({
    event: "hook_write_uncertain",
    threadId: target.threadId,
    issueId: target.issueId,
  });
  return { ...result, skipped: result.skipped + 1, errors: result.errors + 1 };
}
