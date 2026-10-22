# Plane chat-sync lifecycle hooks

`hook_handler.py` records only lifecycle metadata emitted by Codex hooks:

| Codex event         | Plane observation                        | Terminal |
| ------------------- | ---------------------------------------- | -------- |
| `UserPromptSubmit`  | `running`                                | no       |
| `PermissionRequest` | `needs_action`                           | no       |
| `PostToolUse`       | `running` (`30` second heartbeat bucket) | no       |
| `Stop`              | `answer_available`                       | yes      |
| `Interrupt`         | `needs_action`                           | yes      |

The handler deliberately ignores prompts, tool input/output, assistant text, transcript paths, and all other payload fields. It validates canonical UUID thread/turn identifiers, stores each event with its own receipt timestamp and a stable per-invocation event ID, and writes through a bounded fsynced temporary file followed by an atomic rename. Repeated `PermissionRequest` events are distinguished by a hashed bounded tool/request identifier plus receipt time. `PostToolUse` uses a separate locked marker for its 30-second rate limit, so accepted heartbeat files never overwrite each other. Invalid input, I/O errors, and invocation errors are fail-open and produce no stdout or stderr.

`Stop` is a soft stop for that Codex turn, not task completion. Codex can emit another `Stop` if another Stop hook continues the turn, and a same-turn `UserPromptSubmit` or explicit `PostToolUse` can resume it. The daemon marks only that resume as `resume_after_stop`; `Interrupt` remains terminal. The existing Plane finalizer alone decides whether the work is complete.

## Installer

The installer is a dry run unless `--apply` is supplied. It copies the stable handler into the Codex hooks directory and adds one handler for each event above to the global `hooks.json`. Existing handlers, matcher groups, other events, top-level metadata, `config.toml`, and hook trust state are left untouched.

Prefer deriving the spool from the canonical service configuration:

```powershell
python dev/chat-sync/hooks/install_hooks.py `
  --service-config "$env:USERPROFILE\AppData\Local\plane-chat-sync\service.json" `
  --interpreter "C:\Path\To\Python\python.exe"

python dev/chat-sync/hooks/install_hooks.py `
  --service-config "$env:USERPROFILE\AppData\Local\plane-chat-sync\service.json" `
  --interpreter "C:\Path\To\Python\python.exe" `
  --apply
```

The installer derives `stateDir\hook-spool` from `service.json`. If `--spool-dir` is also supplied, it must exactly match. This avoids using an MSIX-affected `$env:LOCALAPPDATA` path. The first command only prints the plan. The second creates a timestamped backup before changing `hooks.json` and is idempotent on subsequent runs. It never edits trust hashes or the service configuration.

After applying, open `/hooks` in Codex once and trust the newly added definitions. Until that review occurs, Codex skips them.
