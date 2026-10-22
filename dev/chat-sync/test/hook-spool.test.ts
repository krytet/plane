import assert from "node:assert/strict";
import { mkdir, mkdtemp, open, rm, writeFile } from "node:fs/promises";
import { tmpdir } from "node:os";
import { join } from "node:path";
import test from "node:test";

import { HookSpoolReader, selectHookEvent, type HookSpoolEvent } from "../src/hook-spool.ts";

const THREAD_ID = "01900000-0000-7000-8000-000000000000";
const TURN_ID = "01900000-0000-7000-8000-000000000001";
const NOW = Date.parse("2026-10-22T12:10:00.000000Z");

function event(
  id: string,
  eventName: HookSpoolEvent["event"],
  observedAt: string,
  turnId = TURN_ID,
  threadId = THREAD_ID
): HookSpoolEvent {
  return {
    version: 1,
    id,
    source: "codex_hook",
    event: eventName,
    state:
      eventName === "Stop"
        ? "answer_available"
        : eventName === "PermissionRequest" || eventName === "Interrupt"
          ? "needs_action"
          : "running",
    terminal: eventName === "Interrupt",
    thread_id: threadId,
    turn_id: turnId,
    observed_at: observedAt,
  };
}

async function spoolFile(directory: string, value: object, fileName?: string): Promise<string> {
  const name = fileName ?? `${String((value as { id?: unknown }).id)}.json`;
  await writeFile(join(directory, name), `${JSON.stringify(value)}\n`, "utf8");
  return name;
}

test("reads bounded valid events in source-time order and rejects unsafe files", async (t) => {
  const directory = await mkdtemp(join(tmpdir(), "plane-hook-spool-"));
  t.after(() => rm(directory, { recursive: true, force: true }));
  const laterId = "01900000-0000-7000-8000-000000000010";
  const earlierId = "01900000-0000-7000-8000-000000000011";
  await spoolFile(directory, event(laterId, "PermissionRequest", "2026-10-22T12:05:00.000000Z"));
  await spoolFile(directory, event(earlierId, "Stop", "2026-10-22T12:04:00.000000Z"));
  await spoolFile(
    directory,
    { id: "01900000-0000-7000-8000-000000000014", state: "failed" },
    "01900000-0000-7000-8000-000000000014.json"
  );
  await writeFile(join(directory, "01900000-0000-7000-8000-000000000012.json"), "x".repeat(20_000));
  await writeFile(
    join(directory, "01900000-0000-7000-8000-000000000013.json"),
    JSON.stringify({ ...event("01900000-0000-7000-8000-000000000013", "Stop", "2030-01-01T00:00:00.000000Z") })
  );

  const reader = new HookSpoolReader(directory, { maxBytes: 1_024, maxBatch: 1 });
  const first = await reader.readBatch(NOW);

  assert.deepEqual(
    first.events.map((item) => item.id),
    [earlierId]
  );
  assert.deepEqual(first.invalid.map((item) => item.code).toSorted(), [
    "future_timestamp",
    "invalid_shape",
    "oversize",
  ]);
});

test("rejects malformed JSON and symlinks without following them", async (t) => {
  const directory = await mkdtemp(join(tmpdir(), "plane-hook-spool-"));
  const outside = await mkdtemp(join(tmpdir(), "plane-hook-outside-"));
  t.after(() => rm(directory, { recursive: true, force: true }));
  t.after(() => rm(outside, { recursive: true, force: true }));
  const malformedId = "01900000-0000-7000-8000-000000000020";
  const symlinkId = "01900000-0000-7000-8000-000000000021";
  await writeFile(join(directory, `${malformedId}.json`), "{bad");
  await writeFile(join(outside, "event.json"), JSON.stringify(event(symlinkId, "Stop", "2026-10-22T12:00:00.000000Z")));
  try {
    await (await open(outside, "r")).close();
    const { symlink } = await import("node:fs/promises");
    await symlink(join(outside, "event.json"), join(directory, `${symlinkId}.json`));
  } catch {
    t.skip("symlinks are unavailable in this environment");
    return;
  }

  const result = await new HookSpoolReader(directory).readBatch(NOW);

  assert.equal(result.events.length, 0);
  assert.deepEqual(result.invalid.map((item) => item.code).toSorted(), ["not_regular_file", "unreadable"]);
});

test("selects the newest lifecycle event and records Stop as a soft stop", () => {
  const stopped = event("01900000-0000-7000-8000-000000000030", "Stop", "2026-10-22T12:05:00.000000Z");
  const resumed = event("01900000-0000-7000-8000-000000000031", "PostToolUse", "2026-10-22T12:05:01.000000Z");
  const newTurnRunning = event(
    "01900000-0000-7000-8000-000000000032",
    "UserPromptSubmit",
    "2026-10-22T12:06:00.000000Z",
    "01900000-0000-7000-8000-000000000002"
  );

  const result = selectHookEvent([stopped, resumed, newTurnRunning], null, {});

  assert.equal(result.selected?.id, newTurnRunning.id);
  assert.deepEqual(
    result.superseded.map((item) => item.id),
    [stopped.id, resumed.id]
  );
  assert.equal(result.activeTurnId, "01900000-0000-7000-8000-000000000002");
  assert.deepEqual(result.terminalTurnIds, []);
  assert.deepEqual(result.softStoppedTurnIds, []);
});

test("uses exact microseconds and rejects cross-turn delayed stopped/running events", () => {
  const turnB = "01900000-0000-7000-8000-000000000002";
  const stopA = event("01900000-0000-7000-8000-000000000060", "Stop", "2026-10-22T12:00:00.000100Z");
  const startB = event(
    "01900000-0000-7000-8000-000000000061",
    "UserPromptSubmit",
    "2026-10-22T12:00:00.000200Z",
    turnB
  );
  const latePostA = event("01900000-0000-7000-8000-000000000062", "PostToolUse", "2026-10-22T12:00:00.000300Z");
  const lateStopA = event("01900000-0000-7000-8000-000000000063", "Stop", "2026-10-22T12:00:00.000400Z");

  const first = selectHookEvent([stopA, startB, latePostA, lateStopA], null, {});
  assert.equal(first.selected?.id, startB.id);
  assert.equal(first.activeTurnId, turnB);
  assert.deepEqual(
    first.superseded.map((item) => item.id),
    [stopA.id, latePostA.id, lateStopA.id]
  );

  const stopB = event("01900000-0000-7000-8000-000000000064", "Stop", "2026-10-22T12:00:00.000500Z", turnB);
  const second = selectHookEvent([stopB, latePostA], null, {
    activeTurnId: first.activeTurnId,
    activeTurnStartedAt: first.activeTurnStartedAt,
    terminalTurnIds: first.terminalTurnIds,
    softStoppedTurnIds: first.softStoppedTurnIds,
  });
  assert.equal(second.selected?.id, stopB.id);
  assert.deepEqual(second.terminalTurnIds, []);
  assert.deepEqual(second.softStoppedTurnIds, [TURN_ID, turnB]);

  const sameMillisecond = [
    event("01900000-0000-7000-8000-000000000065", "PermissionRequest", "2026-10-22T12:01:00.000900Z"),
    event("01900000-0000-7000-8000-000000000066", "PostToolUse", "2026-10-22T12:01:00.000100Z"),
  ];
  const ordered = selectHookEvent(sameMillisecond, null, {
    activeTurnId: TURN_ID,
    activeTurnStartedAt: "2026-10-22T12:00:00.000000Z",
  });
  assert.equal(ordered.selected?.event, "PermissionRequest");
});

test("keeps terminal turn history bounded", () => {
  const events: HookSpoolEvent[] = [];
  for (let index = 0; index < 20; index += 1) {
    const turnId = `01900000-0000-7000-8000-${String(index + 100).padStart(12, "0")}`;
    const second = String(index * 2).padStart(2, "0");
    events.push(
      event(
        `01900000-0000-7000-8000-${String(200 + index).padStart(12, "0")}`,
        "UserPromptSubmit",
        `2026-10-22T13:00:${second}.000000Z`,
        turnId
      ),
      event(
        `01900000-0000-7000-8000-${String(300 + index).padStart(12, "0")}`,
        "Interrupt",
        `2026-10-22T13:00:${String(index * 2 + 1).padStart(2, "0")}.000000Z`,
        turnId
      )
    );
  }

  const result = selectHookEvent(events, null, {});

  assert.equal(result.terminalTurnIds.length, 16);
  assert.equal(result.activeTurnId, "01900000-0000-7000-8000-000000000119");
});

test("only an explicit PostToolUse after Stop resumes the same turn", () => {
  const stopped = event("01900000-0000-7000-8000-000000000120", "Stop", "2026-10-22T14:00:00.000000Z");
  const heartbeat = event("01900000-0000-7000-8000-000000000121", "PostToolUse", "2026-10-22T14:00:01.000000Z");
  const resumed = selectHookEvent([stopped, heartbeat], null, {});
  assert.equal(resumed.selected?.id, heartbeat.id);
  assert.equal(resumed.resumeAfterStop, true);
  assert.deepEqual(resumed.softStoppedTurnIds, []);

  const arbitrary = selectHookEvent([heartbeat], null, {
    activeTurnId: TURN_ID,
    activeTurnStartedAt: "2026-10-22T14:00:00.000000Z",
  });
  assert.equal(arbitrary.resumeAfterStop, false);

  const interrupt = event("01900000-0000-7000-8000-000000000122", "Interrupt", "2026-10-22T14:00:02.000000Z");
  const terminal = selectHookEvent([interrupt, heartbeat], null, {});
  assert.equal(terminal.selected?.id, interrupt.id);
  assert.deepEqual(terminal.softStoppedTurnIds, []);
  assert.deepEqual(terminal.terminalTurnIds, [TURN_ID]);
});

test("latches Stop resume proof for the selected turn across a co-batched prompt and tool event", () => {
  const stopped = event("01900000-0000-7000-8000-000000000123", "Stop", "2026-10-22T14:01:00.000000Z");
  const prompt = event("01900000-0000-7000-8000-000000000124", "UserPromptSubmit", "2026-10-22T14:01:01.000000Z");
  const resumed = event("01900000-0000-7000-8000-000000000125", "PostToolUse", "2026-10-22T14:01:02.000000Z");

  const result = selectHookEvent([stopped, prompt, resumed], null, {});

  assert.equal(result.selected?.id, resumed.id);
  assert.equal(result.resumeAfterStop, true);
  assert.deepEqual(result.softStoppedTurnIds, []);
});

test("does not flag non-running co-batched events after a resume", () => {
  const stopped = event("01900000-0000-7000-8000-000000000126", "Stop", "2026-10-22T14:02:00.000000Z");
  const prompt = event("01900000-0000-7000-8000-000000000127", "UserPromptSubmit", "2026-10-22T14:02:01.000000Z");
  const resumed = event("01900000-0000-7000-8000-000000000128", "PostToolUse", "2026-10-22T14:02:02.000000Z");
  const finalStop = event("01900000-0000-7000-8000-000000000129", "Stop", "2026-10-22T14:02:03.000000Z");
  const permission = event("01900000-0000-7000-8000-000000000130", "PermissionRequest", "2026-10-22T14:02:04.000000Z");

  const stoppedResult = selectHookEvent([stopped, prompt, resumed, finalStop], null, {});
  assert.equal(stoppedResult.selected?.state, "answer_available");
  assert.equal(stoppedResult.resumeAfterStop, false);

  const permissionResult = selectHookEvent([stopped, prompt, resumed, permission], null, {});
  assert.equal(permissionResult.selected?.state, "needs_action");
  assert.equal(permissionResult.resumeAfterStop, false);
});

test("allows permission to resume but never regresses a newer observation", () => {
  const permission = event("01900000-0000-7000-8000-000000000040", "PermissionRequest", "2026-10-22T12:00:00.000000Z");
  const resumed = event("01900000-0000-7000-8000-000000000041", "PostToolUse", "2026-10-22T12:00:10.000000Z");
  const result = selectHookEvent([permission, resumed], "2026-10-21T12:00:00.000000Z", {});
  assert.equal(result.selected?.id, resumed.id);
  assert.equal(result.selected?.state, "running");

  const duplicate = selectHookEvent([resumed], "2026-10-22T12:00:10.000000Z", {
    lastEventId: resumed.id,
  });
  assert.equal(duplicate.selected, null);
  assert.equal(duplicate.superseded[0]?.id, resumed.id);
});

test("prefers configured thread events beyond a large unknown-event prefix", async (t) => {
  const directory = await mkdtemp(join(tmpdir(), "plane-hook-spool-"));
  t.after(() => rm(directory, { recursive: true, force: true }));
  const otherThread = "01900000-0000-7000-8000-000000000099";
  await Promise.all(
    Array.from({ length: 140 }, (_, index) => {
      const id = `01900000-0000-7000-8000-${String(index).padStart(12, "0")}`;
      return spoolFile(
        directory,
        event(
          id,
          "PermissionRequest",
          `2026-10-22T12:00:${String(index % 60).padStart(2, "0")}.000000Z`,
          TURN_ID,
          otherThread
        )
      );
    })
  );
  const preferred = event("01900000-0000-7000-8000-000000000098", "Stop", "2026-10-22T12:02:00.000000Z");
  await spoolFile(directory, preferred);
  const reader = new HookSpoolReader(directory, {
    maxBatch: 1,
    maxScan: 300,
    maxUnknown: 0,
    maxInvalid: 10,
  });

  const batch = await reader.readBatch(NOW, new Set([THREAD_ID]));

  assert.deepEqual(
    batch.events.map((value) => value.id),
    [preferred.id]
  );
});

test("reader infrastructure creates a missing spool directory only when requested by the writer", async (t) => {
  const directory = await mkdtemp(join(tmpdir(), "plane-hook-spool-"));
  t.after(() => rm(directory, { recursive: true, force: true }));
  const missing = join(directory, "missing");
  assert.deepEqual(await new HookSpoolReader(missing).readBatch(NOW), { events: [], invalid: [] });
  await mkdir(missing);
  const reader = new HookSpoolReader(missing);
  const value = event("01900000-0000-7000-8000-000000000050", "Interrupt", "2026-10-22T12:00:00.000000Z");
  await spoolFile(missing, value);
  await reader.acknowledge(value.id);
  assert.deepEqual(await reader.readBatch(NOW), { events: [], invalid: [] });
});
