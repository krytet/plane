import assert from "node:assert/strict";
import { EventEmitter } from "node:events";
import { PassThrough, Writable } from "node:stream";
import test from "node:test";

import { CodexAppServerClient, parseCodexVersion } from "../src/app-server.ts";
import { ChatSyncError } from "../src/errors.ts";

function fakeSpawn(): {
  spawnImpl: typeof import("node:child_process").spawn;
  messages: Array<Record<string, unknown>>;
} {
  const messages: Array<Record<string, unknown>> = [];
  const spawnImpl = (() => {
    const child = new EventEmitter() as EventEmitter & {
      stdin: Writable;
      stdout: PassThrough;
      stderr: PassThrough;
      exitCode: number | null;
      kill: () => boolean;
    };
    child.stdout = new PassThrough();
    child.stderr = new PassThrough();
    child.exitCode = null;
    child.kill = () => true;
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
        if (message.method === "thread/list" && typeof message.id === "number") {
          child.stdout.write(
            `${JSON.stringify({
              id: message.id,
              result: {
                data: [
                  {
                    id: "thread-1",
                    modelProvider: "openai",
                    status: { type: "notLoaded" },
                  },
                ],
                nextCursor: null,
              },
            })}\n`
          );
        }
        if (message.method === "thread/turns/list" && typeof message.id === "number") {
          child.stdout.write(
            `${JSON.stringify({
              id: message.id,
              result: { data: [] },
            })}\n`
          );
        }
        callback();
      },
    });
    child.exitCode = 0;
    return child;
  }) as unknown as typeof import("node:child_process").spawn;
  return { spawnImpl, messages };
}

test("fails closed until an exact app-server protocol version is verified", () => {
  assert.throws(
    () => parseCodexVersion("codex-cli 0.156.1"),
    (error: unknown) => error instanceof ChatSyncError && error.code === "cli_incompatible"
  );
  assert.throws(
    () => parseCodexVersion("codex-cli 0.163.0-alpha"),
    (error: unknown) => error instanceof ChatSyncError && error.code === "cli_incompatible"
  );
});

test("uses only initialize, thread/list, and thread/turns/list", async () => {
  const { spawnImpl, messages } = fakeSpawn();
  const client = new CodexAppServerClient({
    executable: "mock",
    spawnImpl,
  });

  await client.initialize();
  const found = await client.findThreads(["thread-1", "missing"]);
  await client.listTurns("thread-1");
  await client.close();

  assert.deepEqual([...found.keys()], ["thread-1"]);
  assert.deepEqual(
    messages.map((message) => message.method),
    ["initialize", "initialized", "thread/list", "thread/turns/list"]
  );
  assert.deepEqual((messages[2].params as Record<string, unknown>).sourceKinds, ["cli", "vscode", "subAgent"]);
  assert.equal((messages[2].params as Record<string, unknown>).useStateDbOnly, true);
  assert.equal(messages[3]?.params && (messages[3].params as Record<string, unknown>).itemsView, "full");
});
