import assert from "node:assert/strict";
import { mkdtemp, readdir, rm, writeFile } from "node:fs/promises";
import { tmpdir } from "node:os";
import { join } from "node:path";
import test from "node:test";

import type { ThreadTarget } from "../src/config.ts";
import { HookSpoolReader, type HookSpoolEvent } from "../src/hook-spool.ts";
import { syncHookObservations } from "../src/hook-sync.ts";
import type { PlaneBinding, PlaneClient, PlaneObservation, PlaneObservationWriteResult } from "../src/plane-client.ts";
import type { BindingState } from "../src/state.ts";

const THREAD_ID = "01900000-0000-7000-8000-000000000000";
const TURN_ID = "01900000-0000-7000-8000-000000000001";
const TARGET: ThreadTarget = {
  provider: "codex",
  threadId: THREAD_ID,
  workspaceSlug: "workspace",
  projectId: "00000000-0000-4000-8000-000000000000",
  issueId: "00000000-0000-4000-8000-000000000001",
  url: "codex://threads/01900000-0000-7000-8000-000000000000",
};

function hookEvent(id: string, event: HookSpoolEvent["event"], observedAt: string, turnId = TURN_ID): HookSpoolEvent {
  return {
    version: 1,
    id,
    source: "codex_hook",
    event,
    state:
      event === "Stop"
        ? "answer_available"
        : event === "PermissionRequest" || event === "Interrupt"
          ? "needs_action"
          : "running",
    terminal: event === "Interrupt",
    thread_id: THREAD_ID,
    turn_id: turnId,
    observed_at: observedAt,
  };
}

function binding(observation: PlaneObservation | null = null): PlaneBinding {
  return {
    id: "binding-1",
    provider: "codex",
    thread_id: THREAD_ID,
    sync_cursor: 0,
    progress: { done: [], now: [], waiting: [] },
    observation_state: observation?.state ?? "unknown",
    observation_source: observation ? "codex" : null,
    observation_observed_at: observation?.observed_at ?? null,
    observation_turn_id: observation?.turn_id ?? "",
    link: { url: TARGET.url, title: "Codex task" },
  };
}

class FakePlane {
  remote: PlaneBinding | null = binding();
  observations: PlaneObservation[] = [];
  mode: "success" | "uncertain" | "rejected" = "success";
  uncertainStatus = 503;
  rejectCode = "stale_observation";
  rejectStatus = 409;
  gets = 0;

  async listBindings(): Promise<PlaneBinding[]> {
    this.gets += 1;
    return this.remote ? [structuredClone(this.remote)] : [];
  }

  async syncObservation(
    _target: ThreadTarget,
    _bindingId: string,
    observation: PlaneObservation
  ): Promise<PlaneObservationWriteResult> {
    this.observations.push(structuredClone(observation));
    if (this.mode === "uncertain") return { kind: "uncertain", status: this.uncertainStatus };
    if (this.mode === "rejected") {
      return { kind: "rejected", status: this.rejectStatus, code: this.rejectCode };
    }
    this.remote = binding(observation);
    return { kind: "success", binding: structuredClone(this.remote), applied: true };
  }
}

async function writeEvent(directory: string, value: HookSpoolEvent): Promise<void> {
  await writeFile(join(directory, `${value.id}.json`), `${JSON.stringify(value)}\n`, "utf8");
}

test("latest permission-resumed event is written and older state is acknowledged", async (t) => {
  const directory = await mkdtemp(join(tmpdir(), "plane-hook-sync-"));
  t.after(() => rm(directory, { recursive: true, force: true }));
  const spool = new HookSpoolReader(directory);
  const plane = new FakePlane();
  const events = [
    hookEvent("01900000-0000-7000-8000-000000000010", "PermissionRequest", "2026-10-22T12:00:00.000000Z"),
    hookEvent("01900000-0000-7000-8000-000000000011", "PostToolUse", "2026-10-22T12:00:10.000000Z"),
  ];
  await Promise.all(events.map((value) => writeEvent(directory, value)));

  const state: BindingState = { cursor: 0 };
  const result = await syncHookObservations(
    plane as unknown as PlaneClient,
    TARGET,
    state,
    events,
    spool,
    async () => undefined,
    () => undefined
  );

  assert.equal(result.synced, 1);
  assert.deepEqual(
    plane.observations.map((value) => value.state),
    ["running"]
  );
  assert.equal(state.pendingObservation, undefined);
  assert.equal(state.hook?.lastEventId, events[1]?.id);
  assert.deepEqual(await readdir(directory), []);
});

test("start-stop-posttool-stop resumes the same turn only with the stop flag", async (t) => {
  const directory = await mkdtemp(join(tmpdir(), "plane-hook-sync-"));
  t.after(() => rm(directory, { recursive: true, force: true }));
  const spool = new HookSpoolReader(directory);
  const plane = new FakePlane();
  const state: BindingState = { cursor: 0 };
  const events = [
    hookEvent("01900000-0000-7000-8000-000000000080", "UserPromptSubmit", "2026-10-22T12:00:00.000000Z"),
    hookEvent("01900000-0000-7000-8000-000000000081", "Stop", "2026-10-22T12:00:01.000000Z"),
    hookEvent("01900000-0000-7000-8000-000000000082", "PostToolUse", "2026-10-22T12:00:02.000000Z"),
    hookEvent("01900000-0000-7000-8000-000000000083", "Stop", "2026-10-22T12:00:03.000000Z"),
  ];

  await Promise.all(events.map((event) => writeEvent(directory, event)));
  for (const event of events) {
    // oxlint-disable-next-line no-await-in-loop
    await syncHookObservations(
      plane as unknown as PlaneClient,
      TARGET,
      state,
      [event],
      spool,
      async () => undefined,
      () => undefined
    );
  }

  assert.deepEqual(
    plane.observations.map((observation) => ({
      state: observation.state,
      resume: observation.resume_after_stop ?? false,
    })),
    [
      { state: "running", resume: false },
      { state: "answer_available", resume: false },
      { state: "running", resume: true },
      { state: "answer_available", resume: false },
    ]
  );
  assert.deepEqual(state.hook?.terminalTurnIds, []);
  assert.deepEqual(state.hook?.softStoppedTurnIds, [TURN_ID]);
});

test("stop-same-turn-user-prompt resumes before the following post-tool", async (t) => {
  const directory = await mkdtemp(join(tmpdir(), "plane-hook-sync-"));
  t.after(() => rm(directory, { recursive: true, force: true }));
  const spool = new HookSpoolReader(directory);
  const plane = new FakePlane();
  const state: BindingState = { cursor: 0 };
  const events = [
    hookEvent("01900000-0000-7000-8000-000000000084", "Stop", "2026-10-22T12:01:00.000000Z"),
    hookEvent("01900000-0000-7000-8000-000000000085", "UserPromptSubmit", "2026-10-22T12:01:01.000000Z"),
    hookEvent("01900000-0000-7000-8000-000000000086", "PostToolUse", "2026-10-22T12:01:02.000000Z"),
  ];
  await Promise.all(events.map((event) => writeEvent(directory, event)));
  for (const event of events) {
    // oxlint-disable-next-line no-await-in-loop
    await syncHookObservations(
      plane as unknown as PlaneClient,
      TARGET,
      state,
      [event],
      spool,
      async () => undefined,
      () => undefined
    );
  }

  assert.deepEqual(
    plane.observations.map((observation) => ({
      state: observation.state,
      resume: observation.resume_after_stop ?? false,
    })),
    [
      { state: "answer_available", resume: false },
      { state: "running", resume: true },
      { state: "running", resume: false },
    ]
  );
  assert.deepEqual(state.hook?.softStoppedTurnIds, []);
});

test("co-batched prompt and post-tool retain resume proof on the final POST", async (t) => {
  const directory = await mkdtemp(join(tmpdir(), "plane-hook-sync-"));
  t.after(() => rm(directory, { recursive: true, force: true }));
  const spool = new HookSpoolReader(directory);
  const plane = new FakePlane();
  const events = [
    hookEvent("01900000-0000-7000-8000-000000000087", "UserPromptSubmit", "2026-10-22T12:01:10.000000Z"),
    hookEvent("01900000-0000-7000-8000-000000000088", "PostToolUse", "2026-10-22T12:01:11.000000Z"),
  ];
  await Promise.all(events.map((event) => writeEvent(directory, event)));
  const state: BindingState = {
    cursor: 0,
    hook: {
      activeTurnId: TURN_ID,
      activeTurnStartedAt: "2026-10-22T12:01:00.000000Z",
      softStoppedTurnIds: [TURN_ID],
    },
  };

  await syncHookObservations(
    plane as unknown as PlaneClient,
    TARGET,
    state,
    events,
    spool,
    async () => undefined,
    () => undefined
  );

  assert.deepEqual(
    plane.observations.map((observation) => ({
      state: observation.state,
      resume: observation.resume_after_stop ?? false,
    })),
    [{ state: "running", resume: true }]
  );
});

test("co-batched prompt, post-tool, and final stop omit the resume flag from the final POST", async (t) => {
  const directory = await mkdtemp(join(tmpdir(), "plane-hook-sync-"));
  t.after(() => rm(directory, { recursive: true, force: true }));
  const spool = new HookSpoolReader(directory);
  const plane = new FakePlane();
  const events = [
    hookEvent("01900000-0000-7000-8000-000000000089", "UserPromptSubmit", "2026-10-22T12:01:20.000000Z"),
    hookEvent("01900000-0000-7000-8000-000000000101", "PostToolUse", "2026-10-22T12:01:21.000000Z"),
    hookEvent("01900000-0000-7000-8000-000000000102", "Stop", "2026-10-22T12:01:22.000000Z"),
  ];
  await Promise.all(events.map((event) => writeEvent(directory, event)));
  const state: BindingState = {
    cursor: 0,
    hook: {
      activeTurnId: TURN_ID,
      activeTurnStartedAt: "2026-10-22T12:01:00.000000Z",
      softStoppedTurnIds: [TURN_ID],
    },
  };

  await syncHookObservations(
    plane as unknown as PlaneClient,
    TARGET,
    state,
    events,
    spool,
    async () => undefined,
    () => undefined
  );

  assert.deepEqual(
    plane.observations.map((observation) => ({
      state: observation.state,
      resume: observation.resume_after_stop ?? false,
    })),
    [{ state: "answer_available", resume: false }]
  );
});

test("restart reconciles an uncertain pending observation before retrying", async (t) => {
  const directory = await mkdtemp(join(tmpdir(), "plane-hook-sync-"));
  t.after(() => rm(directory, { recursive: true, force: true }));
  const spool = new HookSpoolReader(directory);
  const plane = new FakePlane();
  plane.mode = "uncertain";
  const event = hookEvent("01900000-0000-7000-8000-000000000020", "Stop", "2026-10-22T12:01:00.000000Z");
  await writeEvent(directory, event);
  const state: BindingState = { cursor: 0 };

  await syncHookObservations(
    plane as unknown as PlaneClient,
    TARGET,
    state,
    [event],
    spool,
    async () => undefined,
    () => undefined
  );

  assert.equal(plane.observations.length, 1);
  assert.equal(state.pendingObservation?.eventId, event.id);
  assert.equal((await readdir(directory)).length, 1);
  assert.ok(plane.gets > plane.observations.length);

  plane.mode = "success";
  await syncHookObservations(
    plane as unknown as PlaneClient,
    TARGET,
    state,
    [],
    spool,
    async () => undefined,
    () => undefined
  );

  assert.equal(plane.observations.length, 2);
  assert.ok(plane.gets >= 4);
  assert.equal(state.pendingObservation, undefined);
  assert.deepEqual(await readdir(directory), []);
});

test("a crash after the combined Stop checkpoint never loses the pending event", async (t) => {
  const directory = await mkdtemp(join(tmpdir(), "plane-hook-sync-"));
  t.after(() => rm(directory, { recursive: true, force: true }));
  const spool = new HookSpoolReader(directory);
  const plane = new FakePlane();
  const event = hookEvent("01900000-0000-7000-8000-000000000090", "Stop", "2026-10-22T12:04:00.000000Z");
  await writeEvent(directory, event);
  const state: BindingState = { cursor: 0 };
  let durable: BindingState | undefined;

  await assert.rejects(
    syncHookObservations(
      plane as unknown as PlaneClient,
      TARGET,
      state,
      [event],
      spool,
      async () => {
        durable = structuredClone(state);
        throw new Error("simulated crash after durable checkpoint");
      },
      () => undefined
    )
  );

  assert.ok(durable);
  assert.equal(durable.pendingObservation?.eventId, event.id);
  assert.deepEqual(durable.hook?.softStoppedTurnIds, [TURN_ID]);
  assert.equal(state.pendingObservation, undefined);
  assert.equal((await readdir(directory)).length, 1);

  await syncHookObservations(
    plane as unknown as PlaneClient,
    TARGET,
    durable,
    [event],
    spool,
    async () => undefined,
    () => undefined
  );

  assert.deepEqual(
    plane.observations.map((observation) => observation.state),
    ["answer_available"]
  );
  assert.deepEqual(await readdir(directory), []);
});

test("save failure rolls back ordering and pending state without acknowledging", async (t) => {
  const directory = await mkdtemp(join(tmpdir(), "plane-hook-sync-"));
  t.after(() => rm(directory, { recursive: true, force: true }));
  const spool = new HookSpoolReader(directory);
  const plane = new FakePlane();
  const event = hookEvent("01900000-0000-7000-8000-000000000091", "Stop", "2026-10-22T12:05:00.000000Z");
  await writeEvent(directory, event);
  const state: BindingState = { cursor: 0 };

  await assert.rejects(
    syncHookObservations(
      plane as unknown as PlaneClient,
      TARGET,
      state,
      [event],
      spool,
      async () => {
        throw new Error("checkpoint save failed");
      },
      () => undefined
    )
  );

  assert.equal(state.pendingObservation, undefined);
  assert.equal(state.hook, undefined);
  assert.equal(plane.observations.length, 0);
  assert.equal((await readdir(directory)).length, 1);
});

test("crash after POST before applied save reconciles without a duplicate POST", async (t) => {
  const directory = await mkdtemp(join(tmpdir(), "plane-hook-sync-"));
  t.after(() => rm(directory, { recursive: true, force: true }));
  const spool = new HookSpoolReader(directory);
  const plane = new FakePlane();
  const event = hookEvent("01900000-0000-7000-8000-000000000092", "Stop", "2026-10-22T12:06:00.000000Z");
  await writeEvent(directory, event);
  const state: BindingState = { cursor: 0 };
  let durable: BindingState | undefined;
  let saves = 0;

  await assert.rejects(
    syncHookObservations(
      plane as unknown as PlaneClient,
      TARGET,
      state,
      [event],
      spool,
      async () => {
        saves += 1;
        if (saves === 1) durable = structuredClone(state);
        else throw new Error("simulated crash before applied save");
      },
      () => undefined
    )
  );

  assert.ok(durable);
  assert.equal(plane.observations.length, 1);
  assert.equal((await readdir(directory)).length, 1);

  await syncHookObservations(
    plane as unknown as PlaneClient,
    TARGET,
    durable,
    [event],
    spool,
    async () => undefined,
    () => undefined
  );

  assert.equal(plane.observations.length, 1);
  assert.deepEqual(await readdir(directory), []);
});

test("keeps a pending observation whose microseconds are newer within the same millisecond", async (t) => {
  const directory = await mkdtemp(join(tmpdir(), "plane-hook-sync-"));
  t.after(() => rm(directory, { recursive: true, force: true }));
  const spool = new HookSpoolReader(directory);
  const plane = new FakePlane();
  plane.remote = binding({
    state: "running",
    observed_at: "2026-10-22T12:01:00.000100Z",
    turn_id: "01900000-0000-7000-8000-000000000099",
  });
  const state: BindingState = {
    cursor: 0,
    pendingObservation: {
      observation: {
        state: "answer_available",
        observed_at: "2026-10-22T12:01:00.000900Z",
        turn_id: TURN_ID,
      },
    },
  };

  const result = await syncHookObservations(
    plane as unknown as PlaneClient,
    TARGET,
    state,
    [],
    spool,
    async () => undefined,
    () => undefined
  );

  assert.equal(result.synced, 1);
  assert.deepEqual(
    plane.observations.map((value) => value.state),
    ["answer_available"]
  );
  assert.equal(state.pendingObservation, undefined);
});

test("terminal turn state suppresses a late running event without a POST", async (t) => {
  const directory = await mkdtemp(join(tmpdir(), "plane-hook-sync-"));
  t.after(() => rm(directory, { recursive: true, force: true }));
  const spool = new HookSpoolReader(directory);
  const plane = new FakePlane();
  const late = hookEvent("01900000-0000-7000-8000-000000000030", "PostToolUse", "2026-10-22T12:02:00.000000Z");
  await writeEvent(directory, late);
  const state: BindingState = {
    cursor: 0,
    hook: { terminalTurnId: TURN_ID, lastObservedAt: "2026-10-22T12:01:00.000000Z" },
  };

  const result = await syncHookObservations(
    plane as unknown as PlaneClient,
    TARGET,
    state,
    [late],
    spool,
    async () => undefined,
    () => undefined
  );

  assert.equal(result.skipped, 1);
  assert.equal(plane.observations.length, 0);
  assert.deepEqual(await readdir(directory), []);
});

test("crash after applied save before acknowledgement discards the replay without POST", async (t) => {
  const directory = await mkdtemp(join(tmpdir(), "plane-hook-sync-"));
  t.after(() => rm(directory, { recursive: true, force: true }));
  const spool = new HookSpoolReader(directory);
  const plane = new FakePlane();
  const event = hookEvent("01900000-0000-7000-8000-000000000095", "Stop", "2026-10-22T12:09:00.000000Z");
  await writeEvent(directory, event);
  const state: BindingState = { cursor: 0 };
  let durable: BindingState | undefined;
  let saves = 0;

  await assert.rejects(
    syncHookObservations(
      plane as unknown as PlaneClient,
      TARGET,
      state,
      [event],
      spool,
      async () => {
        saves += 1;
        durable = structuredClone(state);
        if (saves === 2) throw new Error("simulated crash after applied save");
      },
      () => undefined
    )
  );

  assert.ok(durable);
  assert.equal(durable.pendingObservation, undefined);
  assert.equal(durable.hook?.lastEventId, event.id);
  assert.equal(plane.observations.length, 1);
  assert.equal((await readdir(directory)).length, 1);

  await syncHookObservations(
    plane as unknown as PlaneClient,
    TARGET,
    durable,
    [event],
    spool,
    async () => undefined,
    () => undefined
  );
  assert.equal(plane.observations.length, 1);
  assert.deepEqual(await readdir(directory), []);
});

test("a definitive 4xx is blocked and quarantined without an automatic retry", async (t) => {
  const directory = await mkdtemp(join(tmpdir(), "plane-hook-sync-"));
  t.after(() => rm(directory, { recursive: true, force: true }));
  const spool = new HookSpoolReader(directory);
  const plane = new FakePlane();
  plane.mode = "rejected";
  plane.rejectStatus = 422;
  plane.rejectCode = "invalid_transition";
  const event = hookEvent("01900000-0000-7000-8000-000000000093", "PostToolUse", "2026-10-22T12:07:00.000000Z");
  await writeEvent(directory, event);
  const state: BindingState = { cursor: 0 };

  await syncHookObservations(
    plane as unknown as PlaneClient,
    TARGET,
    state,
    [event],
    spool,
    async () => undefined,
    () => undefined
  );
  assert.equal(plane.observations.length, 1);
  assert.equal(state.hook?.blockedObservation?.code, "invalid_transition");
  assert.deepEqual(await readdir(directory), [".quarantine"]);

  await syncHookObservations(
    plane as unknown as PlaneClient,
    TARGET,
    state,
    [],
    spool,
    async () => undefined,
    () => undefined
  );
  assert.equal(plane.observations.length, 1);
  assert.equal(state.pendingObservation, undefined);
  assert.equal(state.hook?.blockedObservation?.code, "invalid_transition");
});

test("an HTTP 429 observation write remains pending for reconciliation", async (t) => {
  const directory = await mkdtemp(join(tmpdir(), "plane-hook-sync-"));
  t.after(() => rm(directory, { recursive: true, force: true }));
  const spool = new HookSpoolReader(directory);
  const plane = new FakePlane();
  plane.mode = "uncertain";
  plane.uncertainStatus = 429;
  const value = hookEvent("01900000-0000-7000-8000-000000000096", "Stop", "2026-10-22T12:09:30.000000Z");
  await writeEvent(directory, value);
  const state: BindingState = { cursor: 0 };

  await syncHookObservations(
    plane as unknown as PlaneClient,
    TARGET,
    state,
    [value],
    spool,
    async () => undefined,
    () => undefined
  );

  assert.equal(state.pendingObservation?.eventId, value.id);
  assert.equal(state.hook?.blockedObservation, undefined);
  assert.equal((await readdir(directory)).length, 1);
});

test("a stale 409 is reconciled as superseded rather than blocked", async (t) => {
  const directory = await mkdtemp(join(tmpdir(), "plane-hook-sync-"));
  t.after(() => rm(directory, { recursive: true, force: true }));
  const spool = new HookSpoolReader(directory);
  const plane = new FakePlane();
  plane.mode = "rejected";
  plane.rejectCode = "stale_observation";
  const event = hookEvent("01900000-0000-7000-8000-000000000094", "Stop", "2026-10-22T12:08:00.000000Z");
  await writeEvent(directory, event);
  const state: BindingState = { cursor: 0 };

  await syncHookObservations(
    plane as unknown as PlaneClient,
    TARGET,
    state,
    [event],
    spool,
    async () => undefined,
    () => undefined
  );

  assert.equal(state.hook?.blockedObservation, undefined);
  assert.equal(state.pendingObservation, undefined);
  assert.deepEqual(await readdir(directory), []);
});

test("a missing binding is reconciled as a nonfatal skip and never creates one", async (t) => {
  const directory = await mkdtemp(join(tmpdir(), "plane-hook-sync-"));
  t.after(() => rm(directory, { recursive: true, force: true }));
  const spool = new HookSpoolReader(directory);
  const plane = new FakePlane();
  plane.remote = null;
  const event = hookEvent("01900000-0000-7000-8000-000000000040", "Interrupt", "2026-10-22T12:03:00.000000Z");
  await writeEvent(directory, event);
  const state: BindingState = {
    cursor: 0,
    pendingObservation: {
      eventId: event.id,
      eventTerminal: true,
      observation: {
        state: "needs_action",
        observed_at: event.observed_at,
        turn_id: event.turn_id,
      },
    },
  };

  const result = await syncHookObservations(
    plane as unknown as PlaneClient,
    TARGET,
    state,
    [event],
    spool,
    async () => undefined,
    () => undefined
  );

  assert.equal(result.errors, 0);
  assert.equal(result.skipped, 1);
  assert.equal(plane.observations.length, 0);
  assert.equal(state.pendingObservation, undefined);
  assert.deepEqual(await readdir(directory), []);
});
