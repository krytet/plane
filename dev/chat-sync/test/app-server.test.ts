import assert from "node:assert/strict";
import { EventEmitter } from "node:events";
import { PassThrough, Writable } from "node:stream";
import test from "node:test";

import {
  assertSupportedCodexVersion,
  CodexAppServerClient,
  parseCodexVersion,
  VERIFIED_CODEX_PROTOCOL_VERSION,
} from "../src/app-server.ts";
import { ChatSyncError } from "../src/errors.ts";

function fakeSpawn(
  mode: "valid" | "malformedThread" = "valid",
  closeOnEnd = true
): {
  spawnImpl: typeof import("node:child_process").spawn;
  messages: Array<Record<string, unknown>>;
  killCalls: () => number;
  unrefCalls: () => number;
  spawnOptions: Array<Record<string, unknown>>;
} {
  const messages: Array<Record<string, unknown>> = [];
  const spawnOptions: Array<Record<string, unknown>> = [];
  let killCalls = 0;
  let unrefCalls = 0;
  const spawnImpl = ((_executable: string, _args: readonly string[], options: Record<string, unknown>) => {
    spawnOptions.push(options);
    const child = new EventEmitter() as EventEmitter & {
      stdin: Writable;
      stdout: PassThrough;
      stderr: PassThrough;
      exitCode: number | null;
      kill: () => boolean;
      unref: () => void;
    };
    child.stdout = new PassThrough();
    child.stderr = new PassThrough();
    child.exitCode = null;
    child.kill = () => {
      killCalls += 1;
      return true;
    };
    child.unref = () => {
      unrefCalls += 1;
    };
    child.stdin = new Writable({
      write(chunk, _encoding, callback) {
        const message = JSON.parse(chunk.toString("utf8")) as Record<string, unknown>;
        messages.push(message);
        if (message.method === "initialize" && typeof message.id === "number") {
          child.stdout.write(
            `${JSON.stringify({
              id: message.id,
              result: {
                userAgent: "mock",
                platformFamily: "windows",
                platformOs: "windows",
              },
            })}\n`
          );
        }
        if (message.method === "thread/read" && typeof message.id === "number") {
          const threadId =
            message.params && typeof message.params === "object"
              ? (message.params as Record<string, unknown>).threadId
              : undefined;
          if (threadId === "missing") {
            child.stdout.write(
              `${JSON.stringify({
                id: message.id,
                error: { code: -32600, message: "thread not found" },
              })}\n`
            );
          } else {
            child.stdout.write(
              `${JSON.stringify({
                id: message.id,
                result: {
                  thread: {
                    id: threadId,
                    modelProvider: "openai",
                    status: mode === "malformedThread" ? null : { type: "notLoaded" },
                  },
                  readState: { mode: "summary" },
                },
              })}\n`
            );
          }
        }
        if (message.method === "thread/turns/list" && typeof message.id === "number") {
          child.stdout.write(
            `${JSON.stringify({
              id: message.id,
              result: {
                data: [],
              },
            })}\n`
          );
        }
        callback();
      },
    });
    child.stdin.on("finish", () => {
      if (closeOnEnd) child.emit("close", 0);
    });
    child.exitCode = closeOnEnd ? 0 : null;
    return child;
  }) as unknown as typeof import("node:child_process").spawn;
  return {
    spawnImpl,
    messages,
    killCalls: () => killCalls,
    unrefCalls: () => unrefCalls,
    spawnOptions,
  };
}

function messageParams(messages: Array<Record<string, unknown>>, index: number): Record<string, unknown> {
  const message = messages[index];
  assert.ok(message);
  const params = message.params;
  assert.ok(typeof params === "object" && params !== null && !Array.isArray(params));
  return params as Record<string, unknown>;
}

test("accepts only the live-verified exact app-server protocol version", () => {
  assert.equal(VERIFIED_CODEX_PROTOCOL_VERSION, "0.163.0-alpha.11");
  assert.equal(parseCodexVersion("codex-cli 0.163.0-alpha.11"), "0.163.0-alpha.11");
  assert.throws(
    () => parseCodexVersion("codex-cli 0.156.1"),
    (error: unknown) => error instanceof ChatSyncError && error.code === "cli_incompatible"
  );
  assert.throws(
    () => parseCodexVersion("codex-cli 0.163.0-alpha"),
    (error: unknown) => error instanceof ChatSyncError && error.code === "cli_incompatible"
  );
});

test("reads each allowlisted thread directly and requests only its newest summary turn", async () => {
  const { spawnImpl, messages } = fakeSpawn();
  const client = new CodexAppServerClient({
    executable: "mock",
    spawnImpl,
  });

  await client.initialize();
  const thread = await client.readThread("thread-1");
  const missing = await client.readThread("missing");
  await client.listTurns("thread-1");
  await client.close();

  assert.equal(thread?.id, "thread-1");
  assert.equal(thread?.modelProvider, "openai");
  assert.deepEqual(thread?.status, { type: "notLoaded" });
  assert.equal(missing, null);
  assert.deepEqual(
    messages.map((message) => message.method),
    ["initialize", "initialized", "thread/read", "thread/read", "thread/turns/list"]
  );
  assert.equal(messageParams(messages, 2).threadId, "thread-1");
  assert.equal(messageParams(messages, 3).threadId, "missing");
  assert.equal(messageParams(messages, 4).limit, 1);
  assert.equal(messageParams(messages, 4).sortDirection, "desc");
  assert.equal(messageParams(messages, 4).itemsView, "summary");
});

test("treats malformed thread metadata as a fatal protocol error", async () => {
  const { spawnImpl } = fakeSpawn("malformedThread");
  const client = new CodexAppServerClient({
    executable: "mock",
    spawnImpl,
  });

  await client.initialize();
  await assert.rejects(
    client.readThread("thread-1"),
    (error: unknown) => error instanceof ChatSyncError && error.code === "protocol_incompatible"
  );
  await client.close();
});

test("version-check timeout releases only owned handles and never kills the child", async () => {
  const { spawnImpl, killCalls, unrefCalls } = fakeSpawn("valid", false);

  await assert.rejects(
    assertSupportedCodexVersion("mock", spawnImpl, 10),
    (error: unknown) => error instanceof ChatSyncError && error.code === "cli_incompatible"
  );
  assert.equal(killCalls(), 0);
  assert.ok(unrefCalls() >= 1);
});

test("bounded app-server close detaches owned handles without killing the child", async () => {
  const { spawnImpl, killCalls, unrefCalls } = fakeSpawn("valid", false);
  const client = new CodexAppServerClient({
    executable: "mock",
    spawnImpl,
    closeTimeoutMs: 10,
  });

  await client.initialize();
  await client.close();

  assert.equal(killCalls(), 0);
  assert.ok(unrefCalls() >= 1);
});

test("Codex version and app-server children never receive the Plane API key", async () => {
  const previous = process.env.PLANE_API_KEY;
  process.env.PLANE_API_KEY = "plane_api_test_secret";
  const { spawnImpl, spawnOptions } = fakeSpawn();
  const client = new CodexAppServerClient({
    executable: "mock",
    spawnImpl,
    closeTimeoutMs: 10,
  });

  try {
    await assert.rejects(
      assertSupportedCodexVersion("mock", spawnImpl, 10),
      (error: unknown) => error instanceof ChatSyncError && error.code === "cli_incompatible"
    );
    await client.initialize();
    await client.close();
  } finally {
    if (previous === undefined) delete process.env.PLANE_API_KEY;
    else process.env.PLANE_API_KEY = previous;
  }

  assert.equal(spawnOptions.length, 2);
  for (const options of spawnOptions) {
    const environment = options.env as NodeJS.ProcessEnv | undefined;
    assert.ok(environment);
    assert.equal(environment.PLANE_API_KEY, undefined);
  }
});
