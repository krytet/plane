# Plane Chat Sync (Windows, Codex app-server)

This local adapter reads explicitly configured Codex threads through the Codex
app-server protocol and posts a structured progress update to Plane's API-key
`chat-bindings` endpoint.

It fails closed by default because no matching Desktop/CLI app-server protocol
has been live-verified. Once verified, it uses only the stable read surface:

| Method                       | Purpose                                                        |
| ---------------------------- | -------------------------------------------------------------- |
| `initialize` + `initialized` | Protocol handshake                                             |
| `thread/list`                | Resolve configured thread IDs; does not add discovered threads |
| `thread/turns/list`          | Read full items for the configured thread                      |

The implementation is based on the [official Codex app-server
documentation](https://developers.openai.com/codex/app-server) and the JSON
Schema generated locally with:

```powershell
codex app-server generate-json-schema --out <temporary-directory>
```

It does not read SQLite or JSONL storage, use a browser, inspect auth tokens,
manage the Codex/ChatGPT desktop app, call auth or write methods, or scrape
ChatGPT web/Desktop threads.

## Required thread status block

Only the final agent message of the newest completed turn is inspected. A
`final_answer` item is preferred; an unphased agent message is accepted for
older history. Commentary and partial answers are ignored.

The final message must contain exactly one block in this format:

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

Rules:

- All three headings are required, in `Done`, `Now`, `Waiting` order.
- A section contains zero or more `- item` lines.
- Each item is at most 1,000 characters; each section is limited to 50 items.
- Only the block's items are sent to Plane. Surrounding prose and reasoning are
  never persisted in state or included in the Plane request.
- Secret-like values are rejected. The adapter does not log, return, or persist
  the rejected message or matched value.
- If the newest turn is not `completed`, the adapter waits. Failed or
  interrupted newest turns are not replaced with an older completed turn.

## Configuration and secret placement

Copy `config.example.json` to a location outside this repository, for example
`%LOCALAPPDATA%\plane-chat-sync\config.json`, and set:

```json
{
  "enabled": true,
  "planeBaseUrl": "https://your-plane.example",
  "stateFile": "C:\\Users\\you\\AppData\\Local\\plane-chat-sync\\cursors.json",
  "pollIntervalSeconds": 30,
  "codexExecutable": "codex",
  "threads": [
    {
      "provider": "codex",
      "threadId": "01900000-0000-7000-8000-000000000000",
      "workspaceSlug": "workspace",
      "projectId": "00000000-0000-4000-8000-000000000000",
      "issueId": "00000000-0000-4000-8000-000000000001",
      "url": "https://chatgpt.com/codex/tasks/example",
      "title": "Codex task"
    }
  ]
}
```

`enabled` must be explicitly `true`; there is no discovery and no implicit
opt-in. The Plane API key must be supplied only through the
`PLANE_API_KEY` environment variable, managed outside the repository. Config
files containing key-like field names such as `apiKey`, `token`, or `secret`
are rejected.

The adapter uses this existing endpoint:

```text
POST /api/v1/workspaces/{workspaceSlug}/projects/{projectId}/work-items/{issueId}/chat-bindings/
```

The request header is `X-Api-Key`, and the body contains only `provider`,
`thread_id`, `url`, optional `title`, `cursor`, and the parsed `progress` object.
After a completed progress update is confirmed, the adapter also sends a
typed `answer_available` observation to the existing binding's
`/observations/` endpoint. An uncertain observation is reconciled with GET
before any later attempt. A manually created binding with cursor `0` and
empty progress is accepted. Running turns do not generate observations yet;
no workflow state is changed.

## Run

Node.js 22.22 or newer is required. The adapter has no package dependencies.

```powershell
cd D:\projects\plane-chat-links-progress\dev\chat-sync
$env:CHAT_SYNC_CONFIG = "$env:LOCALAPPDATA\plane-chat-sync\config.json"

# One fail-closed cycle
node src/cli.ts --once

# Continuous polling
node src/cli.ts
```

Do not run more than one process against the same state file. The adapter
persists the cursor and pending request durably, but it does not provide
multi-process coordination.

## Cursor and write safety

Before a write, the adapter reads the existing binding with the same API key.
The next cursor is greater than both the local cursor and the remote
`sync_cursor`.

The write protocol is:

1. Persist a pending request atomically in the local state file.
2. Send one POST, with no automatic HTTP retries.
3. On success, persist the committed cursor and observed turn ID.
4. On timeout, connection failure, malformed success response, or 5xx, perform
   a GET reconciliation before any later retry.
5. If GET proves the exact payload was applied, commit it.
6. If the write was not applied, leave the turn unobserved and require the next
   cycle's GET before retrying.
7. If remote state is ahead or conflicts, block that binding for manual review.

Replaying an already observed turn is skipped. The Plane backend remains the
final idempotency guard for an identical cursor and payload.

To recover a deliberately blocked binding, inspect the Plane work item and the
state entry, back up the state file, then remove only that binding's `blocked`
and, if necessary, stale `pending` fields. Do not lower or invent a cursor.

## Tests

All tests use mocks and do not start Codex app-server, contact Plane, or read
live thread data.

```powershell
npm test
```

Coverage includes the exact status-block grammar, final-message selection,
secret rejection, protocol method/parameter shape, the API-key binding URL and
header, no-retry network behavior, completed-turn gating, duplicate
suppression, ChatGPT rejection, and uncertain-write GET reconciliation.

## Compatibility and unresolved gaps

| Area               | Current status                                                                                                                                                                      |
| ------------------ | ----------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| Installed CLI      | `codex-cli 0.156.1` is known older than the desktop `0.163.0-alpha` line and is **not** treated as compatible; every version currently fails with `cli_incompatible`                |
| Desktop protocol   | Not verified. The adapter has no approved matching protocol version, so it cannot start sync. It fails closed rather than falling back to private files or scraping                 |
| ChatGPT provider   | Unsupported; any configured `chatgpt` target stops the run before external reads or writes                                                                                          |
| Thread visibility  | `thread/list` is bounded to 20 pages of 100 `cli`/`vscode`/`subAgent` threads and uses the state DB without JSONL repair scans; a configured ID outside that snapshot is not synced |
| Live validation    | None performed in this change. The mock suite verifies behavior, not current Desktop-to-CLI thread visibility or a live Plane endpoint                                              |
| Manual Plane edits | A manually edited generated block or a remote cursor conflict requires operator review; the adapter does not overwrite it                                                           |
| Concurrency        | One process per state file is assumed                                                                                                                                               |

There is currently **no enabled runtime path**. The protocol implementation and
mock tests are present, but `VERIFIED_CODEX_PROTOCOL_VERSION` remains `null`
until a matching Codex CLI/app-server protocol for the installed Desktop build
is verified. `0.156.1` must not be used as a compatibility claim.
