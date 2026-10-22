import assert from "node:assert/strict";
import test from "node:test";

import type { ThreadTarget } from "../src/config.ts";
import { PlaneClient } from "../src/plane-client.ts";
import type { PlanePayload } from "../src/state.ts";

const TARGET: ThreadTarget = {
  provider: "codex",
  threadId: "thread-1",
  workspaceSlug: "workspace",
  projectId: "00000000-0000-4000-8000-000000000000",
  issueId: "00000000-0000-4000-8000-000000000001",
  url: "https://chatgpt.com/codex/tasks/example",
  title: "Codex task",
};

const PAYLOAD: PlanePayload = {
  provider: "codex",
  thread_id: TARGET.threadId,
  url: TARGET.url,
  title: TARGET.title,
  cursor: 1_800_000_000,
  progress: {
    done: ["API endpoint"],
    now: ["Mock tests"],
    waiting: ["Review"],
  },
};

const OBSERVATION = {
  state: "running" as const,
  observed_at: "2026-10-21T12:00:00.000Z",
  turn_id: "turn-1",
};

const BINDING_RESPONSE = {
  id: "binding-1",
  provider: "codex",
  thread_id: TARGET.threadId,
  sync_cursor: PAYLOAD.cursor,
  progress: PAYLOAD.progress,
  link: { url: TARGET.url, title: TARGET.title },
  applied: true,
};

test("uses the API-key work-item endpoint without putting the key in the body", async () => {
  const calls: Array<{ url: string; init: RequestInit }> = [];
  const fetchImpl = async (input: string | URL | Request, init: RequestInit = {}) => {
    calls.push({ url: String(input), init });
    return new Response(JSON.stringify(BINDING_RESPONSE), {
      status: 201,
      headers: { "Content-Type": "application/json" },
    });
  };

  const client = new PlaneClient({
    baseUrl: "https://plane.example.com/prefix",
    apiKey: "test-api-key",
    fetchImpl: fetchImpl as typeof fetch,
  });
  const result = await client.syncBinding(TARGET, PAYLOAD);

  assert.equal(result.kind, "success");
  assert.equal(calls.length, 1);
  assert.equal(
    calls[0]?.url,
    "https://plane.example.com/prefix/api/v1/workspaces/workspace/projects/00000000-0000-4000-8000-000000000000/work-items/00000000-0000-4000-8000-000000000001/chat-bindings/"
  );
  const firstCall = calls[0];
  assert.ok(firstCall);
  assert.equal(firstCall.init.redirect, "error");
  assert.equal((firstCall.init.headers as Record<string, string>)["X-Api-Key"], "test-api-key");
  assert.doesNotMatch(String(firstCall.init.body), /test-api-key/);
  assert.deepEqual(JSON.parse(String(firstCall.init.body)), PAYLOAD);
});

test("returns uncertain once on network failure without retrying", async () => {
  let calls = 0;
  const fetchImpl = async () => {
    calls += 1;
    throw new Error("network down");
  };
  const client = new PlaneClient({
    baseUrl: "https://plane.example.com",
    apiKey: "test-api-key",
    fetchImpl: fetchImpl as typeof fetch,
  });

  const result = await client.syncBinding(TARGET, PAYLOAD);

  assert.deepEqual(result, { kind: "uncertain", status: null });
  assert.equal(calls, 1);
});

test("parses an existing manual binding with cursor zero and empty progress", async () => {
  const fetchImpl = async () =>
    new Response(
      JSON.stringify([
        {
          id: "binding-manual",
          provider: "codex",
          thread_id: TARGET.threadId,
          sync_cursor: 0,
          progress: {},
          link: null,
        },
      ]),
      { status: 200, headers: { "Content-Type": "application/json" } }
    );
  const client = new PlaneClient({
    baseUrl: "https://plane.example.com",
    apiKey: "test-api-key",
    fetchImpl: fetchImpl as typeof fetch,
  });

  const bindings = await client.listBindings(TARGET);

  assert.equal(bindings.length, 1);
  assert.equal(bindings[0]?.sync_cursor, 0);
  assert.deepEqual(bindings[0]?.progress, { done: [], now: [], waiting: [] });
});

test("normalizes a nullable link title to an empty string", async () => {
  const fetchImpl = async () =>
    new Response(
      JSON.stringify([
        {
          id: "binding-null-title",
          provider: "codex",
          thread_id: TARGET.threadId,
          sync_cursor: 0,
          progress: {},
          link: { url: TARGET.url, title: null },
        },
      ]),
      { status: 200, headers: { "Content-Type": "application/json" } }
    );
  const client = new PlaneClient({
    baseUrl: "https://plane.example.com",
    apiKey: "test-api-key",
    fetchImpl: fetchImpl as typeof fetch,
  });

  const bindings = await client.listBindings(TARGET);

  assert.deepEqual(bindings[0]?.link, { url: TARGET.url, title: "" });
});

test("rejects partially initialized progress", async () => {
  const fetchImpl = async () =>
    new Response(
      JSON.stringify([
        {
          id: "binding-partial",
          provider: "codex",
          thread_id: TARGET.threadId,
          sync_cursor: 0,
          progress: { done: ["Only one bucket"] },
          link: null,
        },
      ]),
      { status: 200, headers: { "Content-Type": "application/json" } }
    );
  const client = new PlaneClient({
    baseUrl: "https://plane.example.com",
    apiKey: "test-api-key",
    fetchImpl: fetchImpl as typeof fetch,
  });

  await assert.rejects(
    client.listBindings(TARGET),
    (error: unknown) =>
      error instanceof Error && "code" in error && (error as { code?: unknown }).code === "plane_read_failed"
  );
});

test("posts a typed observation to the existing binding endpoint", async () => {
  const calls: Array<{ url: string; init: RequestInit }> = [];
  const fetchImpl = async (input: string | URL | Request, init: RequestInit = {}) => {
    calls.push({ url: String(input), init });
    return new Response(
      JSON.stringify({
        id: "binding-manual",
        provider: "codex",
        thread_id: TARGET.threadId,
        sync_cursor: 0,
        progress: {},
        link: null,
        applied: true,
      }),
      { status: 200, headers: { "Content-Type": "application/json" } }
    );
  };
  const client = new PlaneClient({
    baseUrl: "https://plane.example.com/prefix",
    apiKey: "test-api-key",
    fetchImpl: fetchImpl as typeof fetch,
  });

  const result = await client.syncObservation(TARGET, "binding-manual", OBSERVATION);

  assert.equal(result.kind, "success");
  assert.equal(calls.length, 1);
  assert.equal(
    calls[0]?.url,
    "https://plane.example.com/prefix/api/v1/workspaces/workspace/projects/00000000-0000-4000-8000-000000000000/work-items/00000000-0000-4000-8000-000000000001/chat-bindings/binding-manual/observations/"
  );
  const firstCall = calls[0];
  assert.ok(firstCall);
  assert.equal(firstCall.init.method, "POST");
  assert.equal((firstCall.init.headers as Record<string, string>)["X-Api-Key"], "test-api-key");
  assert.deepEqual(JSON.parse(String(firstCall.init.body)), OBSERVATION);
  assert.doesNotMatch(String(firstCall.init.body), /test-api-key/);
});

test("returns uncertain once for an observation network failure", async () => {
  let calls = 0;
  const fetchImpl = async () => {
    calls += 1;
    throw new Error("network down");
  };
  const client = new PlaneClient({
    baseUrl: "https://plane.example.com",
    apiKey: "test-api-key",
    fetchImpl: fetchImpl as typeof fetch,
  });

  const result = await client.syncObservation(TARGET, "binding-manual", OBSERVATION);

  assert.deepEqual(result, { kind: "uncertain", status: null });
  assert.equal(calls, 1);
});
