import assert from "node:assert/strict";
import { mkdtemp, rm, writeFile } from "node:fs/promises";
import { tmpdir } from "node:os";
import { join } from "node:path";
import test from "node:test";

import { assertSupportedProviders, loadConfig, normalizeProviderUrl } from "../src/config.ts";
import { ChatSyncError } from "../src/errors.ts";

async function configFile(value: unknown): Promise<{ directory: string; path: string }> {
  const directory = await mkdtemp(join(tmpdir(), "plane-chat-sync-config-"));
  const path = join(directory, "config.json");
  await writeFile(path, JSON.stringify(value), "utf8");
  return { directory, path };
}

function baseConfig() {
  return {
    enabled: true,
    planeBaseUrl: "https://plane.example.com/prefix",
    pollIntervalSeconds: 30,
    threads: [
      {
        provider: "codex",
        threadId: "thread-1",
        workspaceSlug: "workspace",
        projectId: "00000000-0000-4000-8000-000000000000",
        issueId: "00000000-0000-4000-8000-000000000001",
        url: "https://chatgpt.com/codex/tasks/example",
      },
    ],
  };
}

test("loads an explicit opt-in config and resolves state next to it", async (t) => {
  const file = await configFile(baseConfig());
  t.after(() => rm(file.directory, { recursive: true, force: true }));

  const config = await loadConfig(file.path);
  assert.equal(config.enabled, true);
  assert.equal(config.threads.length, 1);
  assert.equal(config.stateFile, join(file.directory, ".state", "cursors.json"));
});

test("rejects api key fields instead of reading secrets from config", async (t) => {
  const file = await configFile({ ...baseConfig(), apiKey: "not-allowed" });
  t.after(() => rm(file.directory, { recursive: true, force: true }));

  await assert.rejects(loadConfig(file.path), (error: unknown) => {
    assert.ok(error instanceof ChatSyncError);
    assert.equal(error.code, "config_invalid");
    return true;
  });
});

test("reports ChatGPT as unsupported before any sync work", async (t) => {
  const value = baseConfig();
  value.threads[0]!.provider = "chatgpt";
  const file = await configFile(value);
  t.after(() => rm(file.directory, { recursive: true, force: true }));

  const config = await loadConfig(file.path);
  assert.throws(
    () => assertSupportedProviders(config),
    (error: unknown) => error instanceof ChatSyncError && error.code === "unsupported_provider"
  );
});

test("requires an explicit enabled boolean", async (t) => {
  const value = baseConfig();
  delete (value as { enabled?: boolean }).enabled;
  const file = await configFile(value);
  t.after(() => rm(file.directory, { recursive: true, force: true }));

  await assert.rejects(loadConfig(file.path), (error: unknown) => {
    assert.ok(error instanceof ChatSyncError);
    assert.equal(error.code, "config_invalid");
    return true;
  });
});

test("normalizes provider URLs with uppercase hosts, trailing dots, and the default HTTPS port", async (t) => {
  const value = baseConfig();
  value.threads[0]!.url = "HTTPS://WWW.CHATGPT.COM.:443/codex/tasks/Example?source=test#progress";
  const file = await configFile(value);
  t.after(() => rm(file.directory, { recursive: true, force: true }));

  const config = await loadConfig(file.path);

  assert.equal(config.threads[0]!.url, "https://www.chatgpt.com/codex/tasks/Example?source=test#progress");
  assert.equal(normalizeProviderUrl("HTTPS://CHATGPT.COM.:443"), "https://chatgpt.com/");
});

test("rejects non-provider and non-canonical provider URLs", async (t) => {
  const urls = [
    "http://chatgpt.com/codex/tasks/example",
    "https://chatgpt.com.evil.example/codex/tasks/example",
    "https://chatgpt.com:8443/codex/tasks/example",
    "https://token@chatgpt.com/codex/tasks/example",
  ];

  await Promise.all(
    urls.map(async (url) => {
      const value = baseConfig();
      value.threads[0]!.url = url;
      const file = await configFile(value);
      t.after(() => rm(file.directory, { recursive: true, force: true }));

      await assert.rejects(loadConfig(file.path), (error: unknown) => {
        assert.ok(error instanceof ChatSyncError);
        assert.equal(error.code, "config_invalid");
        return true;
      });
    })
  );
});
