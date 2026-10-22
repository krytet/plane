# Plane Chat Sync (Windows, Codex app-server)

This local adapter reads explicitly configured Codex threads through the Codex
app-server protocol and posts progress to Plane's API-key `chat-bindings`
endpoint. The only verified protocol target is exactly:

| Item                                     | Verified value                                                       |
| ---------------------------------------- | -------------------------------------------------------------------- |
| CLI version                              | `codex-cli 0.163.0-alpha.11`                                         |
| Bundled executable used for verification | `%LOCALAPPDATA%\OpenAI\Codex\bin\6257d9e8f63c0f2b\codex.exe`         |
| Stale PATH binary                        | `0.156.1` is rejected                                                |
| Read scope                               | One explicitly configured thread at a time                           |
| Read verification                        | `thread/read` and newest-only `thread/turns/list` succeeded          |
| End-to-end Plane write                   | Not claimed by this document; local rollout verification is separate |

The adapter uses only these protocol calls:

| Method                       | Parameters                                                  | Purpose                                                                                 |
| ---------------------------- | ----------------------------------------------------------- | --------------------------------------------------------------------------------------- |
| `initialize` + `initialized` | Standard handshake                                          | Establish the read-only app-server session                                              |
| `thread/read`                | `{ "threadId": "<allowlisted UUIDv7>" }`                    | Validate the exact configured thread's `id`, `modelProvider`, `status`, and `readState` |
| `thread/turns/list`          | `limit: 1`, `sortDirection: "desc"`, `itemsView: "summary"` | Read only the newest turn summary                                                       |

There is no broad `thread/list` discovery. A verified absent-thread response
(JSON-RPC `-32600`) is isolated to that target; malformed protocol responses
are fatal and stop the run. The newest-summary response avoids the observed
4.79 MB response from the previous full-20-item read, which exceeded the
adapter's 2 MB protocol line limit.

The adapter does not read Codex SQLite or JSONL storage, use a browser, inspect
auth tokens, manage Codex/ChatGPT Desktop, call auth or write methods, or
scrape ChatGPT threads. ChatGPT web/Desktop synchronization is unsupported.

## Progress modes

`progressMode` defaults to `status_block`, preserving the strict original
behavior. The local management command explicitly selects
`structured_or_summary`.

### Structured status block

When an explicit block exists, it is always preferred. It must be the only
block, complete, and use all three headings in order:

````text
```plane-status
Done:
- API endpoint
- Mock tests
Now:
- Local review
Waiting:
- Deployment approval
```
````

Each item is at most 1,000 characters and each section is limited to 50 items.
Only the block items are posted. Malformed or multiple `plane-status` blocks
fail closed.

### Conservative answer summary

In `structured_or_summary` mode, an ordinary final answer without a status
block becomes a short extractive summary:

- The item starts with `Последний ответ Codex (кратко):`.
- The summary is capped near 300 characters and uses at most the first two
  meaningful prose sentences.
- Fenced code, URLs, Markdown link destinations, and path-like link decoration
  are removed.
- The `Done` and `Waiting` sections remain empty, so the adapter does not claim
  that work or the overall task is complete.
- The full final text is scanned for likely secrets, including `plane_api_`
  tokens, before any extraction. A detected secret fails closed and the value
  is never logged or persisted.

The final message is selected from a `final_answer` agent item; an unphased
agent message is accepted for older history. Commentary and partial answers
are ignored. If the newest turn is not `completed`, the adapter waits.

## Launcher-managed service

Routine local use is configured through an external service file:

```text
%LOCALAPPDATA%\plane-chat-sync\service.json
```

```json
{
  "enabled": true,
  "workspace": "personal",
  "codexExecutable": "C:\\Users\\you\\AppData\\Local\\OpenAI\\Codex\\bin\\6257d9e8f63c0f2b\\codex.exe",
  "pollIntervalSeconds": 30,
  "stateDir": "C:\\Users\\you\\AppData\\Local\\plane-chat-sync"
}
```

Only those five fields are accepted. Secret-like or unknown fields are
rejected. The Plane API URL is not configurable here: the native launcher always
supplies `http://127.0.0.1:8059`, and `stateDir` must be outside this repository.

Enable or restore the service with a normal launcher run:

```powershell
start-plane.bat --no-pause
```

`dev/start-native.ps1` starts the command after the native API is ready, hidden
and owned by the launcher. Re-running `start-plane.bat` reuses a healthy owned
process with the same configuration hash. A live process with a stale heartbeat
or changed configuration is not terminated; the launcher fails clearly and
waits for the daemon's own graceful exit. The command owns `stateDir\sync.lock`,
so a second launcher/manual process fails closed rather than running
concurrently.

The native launcher passes `--service-config <service.json>` to the command. The
daemon rechecks that path during its loop and sleep interval: setting
`"enabled": false`, removing the file, or changing the service configuration
asks the running daemon to exit gracefully. The launcher itself never terminates
the chat-sync root or its descendants, including the bundled Codex app-server.
A missing or disabled service file is a launcher no-op. An external lock owner
is also never terminated.

The launcher also pins `UV_ENV_FILE` to the absolute main `apps/api/.env` path.
This keeps the uv child deterministic when the helper is invoked from either
Windows PowerShell 5.1 or PowerShell 7.

Inspect the launcher result with:

```powershell
powershell -NoProfile -ExecutionPolicy Bypass -File .\dev\check-native.ps1
```

The chat-sync checks require the launcher-owned process and a fresh successful
status cycle. They are readiness evidence only, not proof of per-thread
throughput or end-to-end delivery.

## Direct management-command runs

The same Django command can be run explicitly from the main `apps/api` for a
single cycle, after stopping any launcher-owned `chat-sync` process. It refreshes
existing active Codex `IssueChatBinding` rows on every cycle; it never creates
bindings.

```powershell
cd D:\projects\plane\apps\api

uv run --env-file .env manage.py sync_codex_chats `
  --workspace personal `
  --codex-executable "C:\Users\you\AppData\Local\OpenAI\Codex\bin\6257d9e8f63c0f2b\codex.exe" `
  --plane-base-url http://127.0.0.1:8059 `
  --state-dir "$env:LOCALAPPDATA\plane-chat-sync" `
  --once
```

`--plane-base-url` is required and has no default. Both the app URL and the
database host must be loopback, and `--state-dir` must be outside this
repository. The command holds an OS advisory file lock for its full lifetime;
a second worker against the same state directory fails closed.

After every completed cycle, including a zero-binding cycle, the command
atomically writes `<state-dir>/status.json` with `pid`, `updated_at`,
`workspace`, `intervalSeconds`, `exitCode`, and `threadCount`. This heartbeat
contains no chat text or token material.

Omit `--once` to refresh bindings and invoke the Node adapter serially every
`--interval` seconds (default `30`).

### Token and access scope

The command first verifies the exact Codex executable before any credential or
membership write. Unless `--dry-run` is used, it provisions or reuses:

- a dedicated local bot account with an unusable password;
- workspace membership at role `15`, not workspace admin or owner;
- project membership only for projects that currently have active Codex
  memberships and bindings;
- an API token labelled `Plane local Codex progress sync`, with `user_type=1`
  and a 90-day expiry.

The token is never written to config, state, repository files, or logs. It is
injected into the child Node process environment as `PLANE_API_KEY` only.
Existing binding link URLs, titles, and `IssueLink.metadata` are read, not
rewritten.

`--dry-run` invokes only the adapter's read/summary pass. It makes no Plane
requests, writes no state, and provisions no bot, membership, or token.

## Standalone configuration

The management command generates the config. A standalone operator config may
use this shape, stored outside the repository:

```json
{
  "enabled": true,
  "planeBaseUrl": "http://127.0.0.1:8059",
  "stateFile": "C:\\Users\\you\\AppData\\Local\\plane-chat-sync\\cursors.json",
  "pollIntervalSeconds": 30,
  "codexExecutable": "C:\\Users\\you\\AppData\\Local\\OpenAI\\Codex\\bin\\6257d9e8f63c0f2b\\codex.exe",
  "progressMode": "structured_or_summary",
  "threads": [
    {
      "provider": "codex",
      "threadId": "01900000-0000-7000-8000-000000000000",
      "workspaceSlug": "personal",
      "projectId": "00000000-0000-4000-8000-000000000000",
      "issueId": "00000000-0000-4000-8000-000000000001",
      "url": "codex://threads/01900000-0000-7000-8000-000000000000",
      "title": "Codex task"
    }
  ]
}
```

Codex targets require a UUIDv7 `threadId`. A native URL must exactly match
`codex://threads/<threadId>` with no trailing slash, query, fragment, or
alternate scheme. Existing HTTPS provider URLs are also accepted. Config files
containing key-like field names such as `apiKey`, `token`, or `secret` are
rejected.

```powershell
cd D:\projects\plane-chat-links-progress\dev\chat-sync
$env:PLANE_API_KEY = "<supplied by the local runner>"
node src/cli.ts --config "$env:LOCALAPPDATA\plane-chat-sync\config.json" --once
```

Node.js 22.22 or newer is required. The adapter has no package dependencies.
Do not run multiple standalone processes against the same state file; the
management command is the supported lock-protected runner.

## Cursor and write safety

Before a write, the adapter reads the existing binding with the same scoped API
key. The next cursor is greater than both the local cursor and the remote
`sync_cursor`.

1. Persist a pending request atomically in the local state file.
2. Send one POST, with no automatic HTTP retries.
3. On success, persist the committed cursor and observed turn ID.
4. On timeout, connection failure, malformed success response, or 5xx, perform
   a GET reconciliation before any later retry.
5. If GET proves the exact payload was applied, commit it.
6. If the write was not applied, leave the turn unobserved and require the next
   cycle's GET before retrying.
7. If remote state is ahead or conflicts, block only that binding for review.

After a confirmed progress write, the adapter sends an `answer_available`
observation. It does not change Plane workflow state.

To recover a deliberately blocked binding, inspect the work item and state
entry, back up the state file, then remove only that binding's `blocked` and,
if necessary, stale `pending` fields. Do not lower or invent a cursor.

## Tests

All adapter tests use mocks and do not start Codex app-server, contact Plane,
or read live thread data.

```powershell
cd D:\projects\plane-chat-links-progress\dev\chat-sync
npm test
```

Coverage includes exact version pinning, direct per-thread reads, newest-only
summary parameters, missing-thread isolation, malformed-protocol failure,
UUIDv7/native URL validation, strict and fallback progress modes, whole-answer
secret detection, bounded summary extraction, no Plane access in dry-run, and
uncertain-write GET reconciliation.

The Django command tests cover command discovery, binding membership and URL
generation, loopback/state-directory guards, bot/project scoping, token
idempotency and expiry, executable preflight ordering, child-only token
injection, output redaction, and atomic heartbeat state.

The native launcher helper has a dependency-free PowerShell suite:

```powershell
powershell -NoProfile -ExecutionPolicy Bypass -File D:\projects\plane\dev\tests\run-native-chat-sync-tests.ps1
```

## Current operational status

The app-server read path, structured summary, and local management command are
live-verified for the exact version above. When `service.json` is enabled, a
normal `start-plane.bat` launch owns and restores the long-running command; no
scheduled task is used. A missing or disabled service file remains a no-op.

A fresh successful status cycle proves that the owned runner completed a recent
cycle. It does not prove Plane update throughput, every configured thread, or
frontend delivery.

### 2026-10-21 local validation

- All 4 bound Codex thread IDs resolved through the verified read path.
- 2 completed replies were safely summarized and durably saved with progress
  and `answer_available` observations.
- 2 nonterminal turns were skipped; no streaming or completion claim was made.
- Manual description HTML, description JSON, and workflow state were preserved.
- No raw chat text or secret values were included in validation output.
- The background service had not been launched at the time of this validation.

Later Desktop/CLI versions remain fail-closed until the exact protocol pin is
reverified and updated.
