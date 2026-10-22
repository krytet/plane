import assert from "node:assert/strict";
import { execFile } from "node:child_process";
import { mkdtemp, rm, writeFile } from "node:fs/promises";
import { tmpdir } from "node:os";
import { join } from "node:path";
import test from "node:test";
import { promisify } from "node:util";
import { fileURLToPath } from "node:url";

const execFileAsync = promisify(execFile);
const cliPath = fileURLToPath(new URL("../src/cli.ts", import.meta.url));

test("accepts the documented --dry-run spelling", async (t) => {
  const directory = await mkdtemp(join(tmpdir(), "plane-chat-sync-cli-"));
  t.after(() => rm(directory, { force: true, recursive: true }));
  const configPath = join(directory, "config.json");
  await writeFile(
    configPath,
    JSON.stringify({
      enabled: false,
      planeBaseUrl: "http://localhost:8000",
      pollIntervalSeconds: 30,
      threads: [
        {
          provider: "codex",
          threadId: "01900000-0000-7000-8000-000000000000",
          workspaceSlug: "workspace",
          projectId: "00000000-0000-4000-8000-000000000000",
          issueId: "00000000-0000-4000-8000-000000000001",
          url: "codex://threads/01900000-0000-7000-8000-000000000000",
        },
      ],
    }),
    "utf8"
  );

  const help = await execFileAsync(process.execPath, [cliPath, "--help"], { encoding: "utf8" });
  assert.match(help.stdout, /--dry-run/);
  assert.equal(help.stderr, "");

  const dryRun = await execFileAsync(process.execPath, [cliPath, "--config", configPath, "--dry-run"], {
    encoding: "utf8",
  });
  const event = JSON.parse(dryRun.stdout.trim()) as { event?: string; time?: string };
  assert.equal(event.event, "disabled");
  assert.match(event.time ?? "", /^\d{4}-\d{2}-\d{2}T/);
  assert.equal(dryRun.stderr, "");
});
