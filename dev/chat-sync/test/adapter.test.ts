import assert from "node:assert/strict";
import test from "node:test";

import type { CodexReader, CodexTurn } from "../src/app-server.ts";
import type { AdapterConfig, ThreadTarget } from "../src/config.ts";
import { ChatSyncError } from "../src/errors.ts";
import type { PlaneBinding, PlaneClient, PlaneWriteResult } from "../src/plane-client.ts";
import type { PlanePayload, SyncState } from "../src/state.ts";
import { runCycle } from "../src/sync.ts";

const TARGET: ThreadTarget = {
  provider: "codex",
  threadId: "thread-1",
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
  threads: [TARGET],
};

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
  threads = new Set(["thread-1"]);
  turns: CodexTurn[] = [completedTurn()];
  listCalls = 0;
  findCalls = 0;

  async initialize(): Promise<void> {}

  async findThreads(ids: string[]) {
    this.findCalls += 1;
    return new Map(ids.filter((id) => this.threads.has(id)).map((id) => [id, { id, modelProvider: "openai" }]));
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
    link: { url: payload.url, title: payload.title ?? "Codex task" },
  };
}

class FakePlane {
  remote: PlaneBinding | null = null;
  posts: PlanePayload[] = [];
  gets = 0;
  mode: "success" | "uncertain" | "rejected" = "success";
  markUncertainAsApplied = false;

  async listBindings(): Promise<PlaneBinding[]> {
    this.gets += 1;
    return this.remote ? [structuredClone(this.remote)] : [];
  }

  async syncBinding(_target: ThreadTarget, payload: PlanePayload): Promise<PlaneWriteResult> {
    this.posts.push(structuredClone(payload));
    if (this.mode === "success") {
      this.remote = asBinding(payload);
      return { kind: "success", binding: structuredClone(this.remote) };
    }
    if (this.mode === "rejected") {
      return { kind: "rejected", status: 409, code: "cursor_reused" };
    }
    if (this.markUncertainAsApplied) this.remote = asBinding(payload);
    return { kind: "uncertain", status: 503 };
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
  assert.equal(result.skipped, 1);
});

test("does not post while the latest turn is still in progress", async () => {
  const reader = new FakeReader();
  reader.turns = [{ ...completedTurn(), status: "inProgress", completedAt: null }];
  const plane = new FakePlane();

  const result = await runCycle(reader, plane as unknown as PlaneClient, CONFIG, initialState(), async () => undefined);

  assert.equal(plane.posts.length, 0);
  assert.equal(plane.gets, 0);
  assert.equal(result.skipped, 1);
});

test("does not post a completed turn without a status block", async () => {
  const reader = new FakeReader();
  reader.turns = [completedTurn("turn-2", "Only arbitrary prose.")];
  const plane = new FakePlane();
  const state = initialState();

  await runCycle(reader, plane as unknown as PlaneClient, CONFIG, state, async () => undefined);

  assert.equal(plane.posts.length, 0);
  assert.equal(
    state.bindings["codex:thread-1:workspace:00000000-0000-4000-8000-000000000000:00000000-0000-4000-8000-000000000001"]
      ?.lastObservedTurnId,
    "turn-2"
  );
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
  assert.equal(result.synced, 1);
  assert.equal(
    state.bindings["codex:thread-1:workspace:00000000-0000-4000-8000-000000000000:00000000-0000-4000-8000-000000000001"]
      ?.pending,
    undefined
  );
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
  assert.equal(reader.findCalls, 0);
  assert.equal(plane.gets, 0);
});
