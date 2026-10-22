import assert from "node:assert/strict";
import test from "node:test";

import type { CodexReader, CodexTurn } from "../src/app-server.ts";
import type { AdapterConfig, ThreadTarget } from "../src/config.ts";
import { ChatSyncError } from "../src/errors.ts";
import type {
  PlaneBinding,
  PlaneClient,
  PlaneObservation,
  PlaneObservationWriteResult,
  PlaneWriteResult,
} from "../src/plane-client.ts";
import type { PlanePayload, SyncState } from "../src/state.ts";
import { runCycle, runDryRunCycle } from "../src/sync.ts";

const THREAD_ID = "01900000-0000-7000-8000-000000000000";
const TARGET: ThreadTarget = {
  provider: "codex",
  threadId: THREAD_ID,
  workspaceSlug: "workspace",
  projectId: "00000000-0000-4000-8000-000000000000",
  issueId: "00000000-0000-4000-8000-000000000001",
  url: "https://chatgpt.com/codex/tasks/example",
  title: "Codex task",
};

const CONFIG: AdapterConfig = {
  enabled: true,
  planeBaseUrl: "https://plane.example.com",
  stateFile: "unused",
  pollIntervalSeconds: 30,
  codexExecutable: "codex",
  progressMode: "status_block",
  threads: [TARGET],
};
const STATE_KEY = `codex:${THREAD_ID}:workspace:00000000-0000-4000-8000-000000000000:00000000-0000-4000-8000-000000000001`;

const FINAL_MESSAGE = [
  "Arbitrary final prose that must not be forwarded.",
  "```plane-status",
  "Done:",
  "- API endpoint",
  "Now:",
  "- Mock tests",
  "Waiting:",
  "- Review",
  "```",
].join("\n");

function completedTurn(id = "turn-1", text = FINAL_MESSAGE): CodexTurn {
  return {
    id,
    status: "completed",
    completedAt: 1_800_000_000,
    items: [
      { type: "agentMessage", phase: "commentary", text: "internal commentary" },
      { type: "agentMessage", phase: "final_answer", text },
    ],
  };
}

class FakeReader implements CodexReader {
  threads = new Set([THREAD_ID]);
  turns: CodexTurn[] = [completedTurn()];
  listCalls = 0;
  readCalls = 0;

  async initialize(): Promise<void> {}

  async readThread(id: string) {
    this.readCalls += 1;
    return this.threads.has(id) ? { id, modelProvider: "openai", status: { type: "notLoaded" as const } } : null;
  }

  async listTurns(): Promise<CodexTurn[]> {
    this.listCalls += 1;
    return this.turns;
  }

  async close(): Promise<void> {}
}

function asBinding(payload: PlanePayload): PlaneBinding {
  return {
    id: "binding-1",
    provider: payload.provider,
    thread_id: payload.thread_id,
    sync_cursor: payload.cursor,
    progress: payload.progress,
    observation_state: "unknown",
    observation_source: null,
    observation_observed_at: null,
    observation_turn_id: "",
    link: { url: payload.url, title: payload.title ?? "Codex task" },
  };
}

function manualBinding(): PlaneBinding {
  return {
    id: "binding-manual",
    provider: "codex",
    thread_id: TARGET.threadId,
    sync_cursor: 0,
    progress: { done: [], now: [], waiting: [] },
    observation_state: "unknown",
    observation_source: null,
    observation_observed_at: null,
    observation_turn_id: "",
    link: { url: TARGET.url, title: TARGET.title ?? "Codex task" },
  };
}

class FakePlane {
  remote: PlaneBinding | null = null;
  posts: PlanePayload[] = [];
  observations: Array<{ target: ThreadTarget; bindingId: string; observation: PlaneObservation }> = [];
  gets = 0;
  mode: "success" | "uncertain" | "rejected" = "success";
  observationMode: "success" | "uncertain" | "rejected" = "success";
  markUncertainAsApplied = false;
  markObservationUncertainAsApplied = false;

  async listBindings(): Promise<PlaneBinding[]> {
    this.gets += 1;
    return this.remote ? [structuredClone(this.remote)] : [];
  }

  async syncBinding(_target: ThreadTarget, payload: PlanePayload): Promise<PlaneWriteResult> {
    this.posts.push(structuredClone(payload));
    if (this.mode === "success") {
      this.remote = { ...asBinding(payload), id: this.remote?.id ?? "binding-1" };
      return { kind: "success", binding: structuredClone(this.remote) };
    }
    if (this.mode === "rejected") {
      return { kind: "rejected", status: 409, code: "cursor_reused" };
    }
    if (this.markUncertainAsApplied) {
      this.remote = { ...asBinding(payload), id: this.remote?.id ?? "binding-1" };
    }
    return { kind: "uncertain", status: 503 };
  }

  async syncObservation(
    target: ThreadTarget,
    bindingId: string,
    observation: PlaneObservation
  ): Promise<PlaneObservationWriteResult> {
    this.observations.push({
      target: structuredClone(target),
      bindingId,
      observation: structuredClone(observation),
    });
    if (this.observationMode === "rejected") {
      return { kind: "rejected", status: 409, code: "stale_observation" };
    }
    if (this.observationMode === "uncertain") {
      if (this.markObservationUncertainAsApplied && this.remote) {
        this.remote = this.withObservation(this.remote, observation);
      }
      return { kind: "uncertain", status: 503 };
    }
    this.remote = this.withObservation(this.remote ?? manualBinding(), observation);
    return {
      kind: "success",
      binding: structuredClone(this.remote),
      applied: true,
    };
  }

  private withObservation(binding: PlaneBinding, observation: PlaneObservation): PlaneBinding {
    return {
      ...binding,
      observation_state: observation.state,
      observation_source: "codex",
      observation_observed_at: observation.observed_at,
      observation_turn_id: observation.turn_id,
    };
  }
}

function initialState(): SyncState {
  return { version: 1, bindings: {} };
}

async function save(): Promise<void> {}

test("posts only the explicit status block after a completed final turn", async () => {
  const reader = new FakeReader();
  const plane = new FakePlane();
  const state = initialState();
  let saves = 0;

  const result = await runCycle(reader, plane as unknown as PlaneClient, CONFIG, state, async () => {
    saves += 1;
  });

  assert.equal(result.synced, 1);
  assert.equal(result.errors, 0);
  assert.equal(plane.posts.length, 1);
  assert.equal(plane.observations.length, 1);
  assert.equal(plane.observations[0]?.observation.state, "answer_available");
  assert.deepEqual(plane.posts[0]?.progress, {
    done: ["API endpoint"],
    now: ["Mock tests"],
    waiting: ["Review"],
  });
  assert.doesNotMatch(JSON.stringify(plane.posts[0]), /Arbitrary final prose/);
  assert.doesNotMatch(JSON.stringify(plane.posts[0]), /internal commentary/);
  assert.ok(saves >= 2);
});

test("a completed turn is not posted twice", async () => {
  const reader = new FakeReader();
  const plane = new FakePlane();
  const state = initialState();
  await runCycle(reader, plane as unknown as PlaneClient, CONFIG, state, save);
  const result = await runCycle(reader, plane as unknown as PlaneClient, CONFIG, state, save);

  assert.equal(plane.posts.length, 1);
  assert.equal(plane.observations.length, 1);
  assert.equal(result.skipped, 1);
});

test("does not observe or post while the latest turn is still in progress", async () => {
  const reader = new FakeReader();
  reader.turns = [{ ...completedTurn(), status: "inProgress", completedAt: null }];
  const plane = new FakePlane();

  const result = await runCycle(reader, plane as unknown as PlaneClient, CONFIG, initialState(), async () => undefined);

  assert.equal(plane.posts.length, 0);
  assert.equal(plane.observations.length, 0);
  assert.equal(plane.gets, 0);
  assert.equal(result.skipped, 1);
});

test("updates an existing cursor-zero binding and posts the completed observation", async () => {
  const reader = new FakeReader();
  const plane = new FakePlane();
  plane.remote = manualBinding();
  const state = initialState();

  const result = await runCycle(reader, plane as unknown as PlaneClient, CONFIG, state, save);

  assert.equal(result.synced, 1);
  assert.equal(result.errors, 0);
  assert.equal(plane.posts.length, 1);
  assert.ok((plane.posts[0]?.cursor ?? 0) > 0);
  assert.equal(plane.observations.length, 1);
  assert.equal(plane.observations[0]?.bindingId, "binding-manual");
  assert.equal(plane.observations[0]?.observation.state, "answer_available");
  assert.equal(plane.observations[0]?.observation.observed_at, new Date(1_800_000_000 * 1_000).toISOString());
});

test("uses freshly fetched binding URL and title instead of the config snapshot", async () => {
  const reader = new FakeReader();
  const plane = new FakePlane();
  plane.remote = {
    ...manualBinding(),
    link: {
      url: "https://chatgpt.com/codex/tasks/human-metadata",
      title: "Human-maintained title",
    },
  };

  await runCycle(reader, plane as unknown as PlaneClient, CONFIG, initialState(), save);

  assert.equal(plane.posts[0]?.url, "https://chatgpt.com/codex/tasks/human-metadata");
  assert.equal(plane.posts[0]?.title, "Human-maintained title");
});

test("reconciles a pending write before treating a disappeared thread as missing", async () => {
  const reader = new FakeReader();
  reader.threads.clear();
  const plane = new FakePlane();
  const payload: PlanePayload = {
    provider: "codex",
    thread_id: THREAD_ID,
    url: TARGET.url,
    title: TARGET.title,
    cursor: 1_800_000_001,
    progress: {
      done: ["Previously posted"],
      now: [],
      waiting: [],
    },
  };
  plane.remote = asBinding(payload);
  const state = initialState();
  state.bindings[STATE_KEY] = {
    cursor: 0,
    pending: {
      cursor: payload.cursor,
      turnId: "turn-pending",
      payloadHash: "pending-hash",
      payload,
    },
  };

  const result = await runCycle(reader, plane as unknown as PlaneClient, CONFIG, state, save);

  assert.equal(result.synced, 1);
  assert.equal(result.errors, 0);
  assert.equal(reader.readCalls, 0);
  assert.equal(plane.gets, 1);
  assert.equal(state.bindings[STATE_KEY]?.pending, undefined);
});

test("does not post a completed turn without a status block", async () => {
  const reader = new FakeReader();
  reader.turns = [completedTurn("turn-2", "Only arbitrary prose.")];
  const plane = new FakePlane();
  const state = initialState();

  await runCycle(reader, plane as unknown as PlaneClient, CONFIG, state, async () => undefined);

  assert.equal(plane.posts.length, 0);
  assert.equal(plane.observations.length, 0);
  assert.equal(plane.gets, 0);
  assert.equal(state.bindings[STATE_KEY]?.lastObservedTurnId, "turn-2");
});

test("reconciles an uncertain write with GET instead of blindly retrying", async () => {
  const reader = new FakeReader();
  const plane = new FakePlane();
  plane.mode = "uncertain";
  plane.markUncertainAsApplied = true;
  const state = initialState();

  const result = await runCycle(reader, plane as unknown as PlaneClient, CONFIG, state, async () => undefined);

  assert.equal(plane.posts.length, 1);
  assert.equal(plane.gets, 2);
  assert.equal(plane.observations.length, 1);
  assert.equal(result.synced, 1);
  assert.equal(state.bindings[STATE_KEY]?.pending, undefined);
});

test("keeps a committed progress write when the follow-up observation is rejected", async () => {
  const reader = new FakeReader();
  const plane = new FakePlane();
  plane.observationMode = "rejected";
  const state = initialState();

  const result = await runCycle(reader, plane as unknown as PlaneClient, CONFIG, state, save);

  assert.equal(plane.posts.length, 1);
  assert.equal(plane.observations.length, 1);
  assert.equal(result.synced, 1);
  assert.equal(result.errors, 1);
  assert.equal(state.bindings[STATE_KEY]?.pending, undefined);
});

test("reconciles before retrying a completed observation on the next cycle", async () => {
  const reader = new FakeReader();
  const plane = new FakePlane();
  plane.observationMode = "rejected";
  const state = initialState();

  await runCycle(reader, plane as unknown as PlaneClient, CONFIG, state, save);
  plane.observationMode = "success";
  const second = await runCycle(reader, plane as unknown as PlaneClient, CONFIG, state, save);
  const third = await runCycle(reader, plane as unknown as PlaneClient, CONFIG, state, save);

  assert.equal(plane.posts.length, 1);
  assert.equal(plane.observations.length, 2);
  assert.equal(second.errors, 0);
  assert.equal(third.skipped, 1);
});

test("reconciles an uncertain observation with GET instead of retrying POST", async () => {
  const reader = new FakeReader();
  const plane = new FakePlane();
  plane.observationMode = "uncertain";
  plane.markObservationUncertainAsApplied = true;
  const state = initialState();

  const result = await runCycle(reader, plane as unknown as PlaneClient, CONFIG, state, save);

  assert.equal(plane.posts.length, 1);
  assert.equal(plane.observations.length, 1);
  assert.equal(plane.gets, 2);
  assert.equal(result.synced, 1);
  assert.equal(result.errors, 0);
});

test("reports an uncertain observation that GET proves was not applied", async () => {
  const reader = new FakeReader();
  const plane = new FakePlane();
  plane.observationMode = "uncertain";
  const state = initialState();

  const result = await runCycle(reader, plane as unknown as PlaneClient, CONFIG, state, save);

  assert.equal(plane.posts.length, 1);
  assert.equal(plane.observations.length, 1);
  assert.equal(plane.gets, 2);
  assert.equal(result.synced, 1);
  assert.equal(result.errors, 1);
});

test("reports ChatGPT before starting any read", async () => {
  const reader = new FakeReader();
  const plane = new FakePlane();
  const config = {
    ...CONFIG,
    threads: [{ ...TARGET, provider: "chatgpt" as const }],
  };

  await assert.rejects(
    runCycle(reader, plane as unknown as PlaneClient, config, initialState(), async () => undefined),
    (error: unknown) => error instanceof ChatSyncError && error.code === "unsupported_provider"
  );
  assert.equal(reader.readCalls, 0);
  assert.equal(plane.gets, 0);
});

test("keeps missing threads isolated so peer targets still sync", async () => {
  const reader = new FakeReader();
  const plane = new FakePlane();
  const missingTarget: ThreadTarget = {
    ...TARGET,
    threadId: "01900000-0000-7000-8000-000000000002",
    issueId: "00000000-0000-4000-8000-000000000002",
  };
  const config: AdapterConfig = { ...CONFIG, threads: [TARGET, missingTarget] };

  const result = await runCycle(reader, plane as unknown as PlaneClient, config, initialState(), save);

  assert.equal(result.synced, 1);
  assert.equal(result.skipped, 1);
  assert.equal(result.errors, 1);
  assert.equal(plane.posts.length, 1);
});

test("structured-or-summary syncs an ordinary answer without claiming task completion", async () => {
  const reader = new FakeReader();
  reader.turns = [
    completedTurn(
      "turn-summary",
      "The endpoint is implemented and focused tests pass. Review [the diff](https://example.test/private) before rollout."
    ),
  ];
  const plane = new FakePlane();
  const config: AdapterConfig = { ...CONFIG, progressMode: "structured_or_summary" };

  const result = await runCycle(reader, plane as unknown as PlaneClient, config, initialState(), save);

  assert.equal(result.synced, 1);
  assert.equal(plane.posts.length, 1);
  assert.deepEqual(plane.posts[0]?.progress.done, []);
  assert.match(plane.posts[0]?.progress.now[0] ?? "", /^Последний ответ Codex \(кратко\):/);
  assert.doesNotMatch(plane.posts[0]?.progress.now.join(" ") ?? "", /https?:\/\//);
});

test("dry-run reads and classifies an answer without any Plane access", async () => {
  const reader = new FakeReader();
  reader.turns = [completedTurn("turn-summary", "A safe ordinary answer for the local probe.")];
  const config: AdapterConfig = { ...CONFIG, progressMode: "structured_or_summary" };
  const events: Array<{ event: string; code?: string }> = [];

  const result = await runDryRunCycle(reader, config, (event) => events.push(event));

  assert.equal(result.synced, 1);
  assert.equal(events[0]?.event, "dry_run_target");
  assert.equal(events[0]?.code, "summary_available");
  assert.doesNotMatch(JSON.stringify(events), /safe ordinary answer/);
});
