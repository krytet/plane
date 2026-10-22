import { setTimeout as delay } from "node:timers/promises";
import { pathToFileURL } from "node:url";
import { parseArgs } from "node:util";

import { assertSupportedCodexVersion, CodexAppServerClient } from "./app-server.ts";
import { assertSupportedProviders, loadConfig } from "./config.ts";
import { ChatSyncError, codeForError } from "./errors.ts";
import { PlaneClient } from "./plane-client.ts";
import { StateStore } from "./state.ts";
import { runCycle, runDryRunCycle, type SyncEvent } from "./sync.ts";

function writeEvent(event: SyncEvent): void {
  process.stdout.write(`${JSON.stringify({ time: new Date().toISOString(), ...event })}\n`);
}

function usage(): string {
  return [
    "Usage: node src/cli.ts --config <path> [--once] [--dry-run]",
    "",
    "  --config <path>  JSON config file; defaults to CHAT_SYNC_CONFIG",
    "  --once           Run one polling cycle",
    "  --dry-run        Read and summarize Codex only; no Plane requests or state writes",
    "  --help           Show this help",
  ].join("\n");
}

async function main(): Promise<void> {
  const args = parseArgs({
    options: {
      config: { type: "string" },
      once: { type: "boolean", default: false },
      "dry-run": { type: "boolean", default: false },
      help: { type: "boolean", default: false },
    },
    strict: true,
    allowPositionals: false,
  });

  if (args.values.help) {
    process.stdout.write(`${usage()}\n`);
    return;
  }

  const configPath = args.values.config ?? process.env.CHAT_SYNC_CONFIG;
  if (!configPath) {
    throw new ChatSyncError("config_invalid", "Provide --config or set CHAT_SYNC_CONFIG.");
  }

  const config = await loadConfig(configPath);
  if (!config.enabled) {
    writeEvent({ event: "disabled" });
    return;
  }

  assertSupportedProviders(config);
  await assertSupportedCodexVersion(config.codexExecutable);

  if (args.values["dry-run"]) {
    const reader = new CodexAppServerClient({ executable: config.codexExecutable });
    const stopController = new AbortController();
    const stop = () => stopController.abort();
    process.once("SIGINT", stop);
    process.once("SIGTERM", stop);

    try {
      await reader.initialize();
      let firstCycle = true;
      while (!stopController.signal.aborted) {
        if (!firstCycle) {
          try {
            // oxlint-disable-next-line no-await-in-loop
            await delay(config.pollIntervalSeconds * 1_000, undefined, {
              signal: stopController.signal,
            });
          } catch {
            break;
          }
        }
        firstCycle = false;

        // oxlint-disable-next-line no-await-in-loop
        const result = await runDryRunCycle(reader, config, writeEvent);
        writeEvent({
          event: "cycle_complete",
          code: result.errors > 0 ? "completed_with_errors" : undefined,
        });
        process.exitCode = result.errors > 0 ? 1 : 0;
        if (args.values.once) break;
      }
    } finally {
      process.removeListener("SIGINT", stop);
      process.removeListener("SIGTERM", stop);
      await reader.close();
    }
    return;
  }

  const apiKey = process.env.PLANE_API_KEY;
  if (!apiKey) {
    throw new ChatSyncError("config_invalid", "PLANE_API_KEY must be supplied through the environment.");
  }

  const reader = new CodexAppServerClient({ executable: config.codexExecutable });
  const store = new StateStore(config.stateFile);
  const state = await store.load();
  const plane = new PlaneClient({
    baseUrl: config.planeBaseUrl,
    apiKey,
  });
  const stopController = new AbortController();
  const stop = () => stopController.abort();
  process.once("SIGINT", stop);
  process.once("SIGTERM", stop);

  try {
    await reader.initialize();
    let firstCycle = true;
    while (!stopController.signal.aborted) {
      if (!firstCycle) {
        try {
          // Poll cycles are deliberately serial.
          // oxlint-disable-next-line no-await-in-loop
          await delay(config.pollIntervalSeconds * 1_000, undefined, {
            signal: stopController.signal,
          });
        } catch {
          break;
        }
      }
      firstCycle = false;

      // oxlint-disable-next-line no-await-in-loop
      const result = await runCycle(reader, plane, config, state, () => store.save(state), writeEvent);
      writeEvent({
        event: "cycle_complete",
        code: result.errors > 0 ? "completed_with_errors" : undefined,
      });
      process.exitCode = result.errors > 0 ? 1 : 0;
      if (args.values.once) break;
    }
  } finally {
    process.removeListener("SIGINT", stop);
    process.removeListener("SIGTERM", stop);
    await reader.close();
  }
}

const invokedPath = process.argv[1] ? pathToFileURL(process.argv[1]).href : "";
if (import.meta.url === invokedPath) {
  main().catch((error: unknown) => {
    const code = codeForError(error);
    const message = error instanceof ChatSyncError ? error.message : "Chat sync stopped fail-closed.";
    process.stderr.write(`${JSON.stringify({ event: "fatal", code, message })}\n`);
    process.exitCode = 1;
  });
}
