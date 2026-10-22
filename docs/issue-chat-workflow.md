# Opt-in chat workflow

This server-side integration moves a work item between configured workflow
states based on typed Codex chat observations. It is disabled by default and is
scoped to one explicitly configured project at a time.

## State contract

| Mapping             | Required group      | Purpose                                              |
| ------------------- | ------------------- | ---------------------------------------------------- |
| `source_state`      | STARTED             | Human-started state, normally `In Progress`          |
| `running_state`     | STARTED             | Automatic running state, normally `LLM Work`         |
| `attention_state`   | STARTED             | Attention state, normally `Requires attention`       |
| `manual_hold_state` | UNSTARTED           | Manually protected state, normally `Come back later` |
| `completion_state`  | STARTED             | Exact project state named `LLM Complete`             |
| `todo_state`        | UNSTARTED, optional | Optional initial source, normally `Todo`             |

All configured states must be active in the same exact project. The five
required mappings and the optional Todo mapping must be distinct. Configuration
rejects any other state shape.

`Todo` is not a general automatic source. It may initially leave Todo only when
the observable aggregate contains a fresh `running` observation and resolves
to LLM Work. An answer, failure, attention request, or historical event from
Todo does not initially move the work item.

## Transition rules

- Any active running binding wins, regardless of attention on other bindings.
- With no active running binding, any `answer_available`, `needs_action`, or
  `failed` observation moves the item to Requires attention.
- A `needs_action` observation is nonterminal. A later `running` observation
  may resume the same turn. `answer_available` and `failed` cannot return to
  running within the same turn.
- A trusted local hook may explicitly resume an `answer_available` turn after a
  blocking Stop by sending `resume_after_stop: true` with a `running`
  observation. The flag is invalid for other states, defaults to false, and
  never allows a `failed` turn to resume. Completion cutoffs and snapshots still
  apply to the resumed running event.
- A running observation older than `--stale-running-ttl-seconds` is stale. It
  does not keep the item in LLM Work and never causes completion.
- `manual_hold_state`, completed, cancelled, backlog, and unstarted states are
  protected unless the exact optional Todo rule above applies.
- Raw binding snapshots are authoritative per binding. The endpoint rejects an
  older timestamp for the same binding, but independent binding timestamps do
  not order the whole issue.
- The worker preserves work items whose state no longer matches the state it
  last expected. Moving an item back to `source_state` or the configured
  `todo_state` initializes a manual-resume cutoff: the first raw event is
  retained but causes no board change, and only a later event can act.
- Description HTML/JSON and the verified PDF attachment are never changed by
  this workflow.

## Completion boundary

The only completion transition remains the verified PDF finalizer in
`plane.utils.llm_complete`. This workflow never selects `LLM Complete` as an
automatic target. An answer is evidence that a turn stopped, not proof that the
task is complete.

When the finalizer commits LLM Complete, it records a `verified_report` cutoff
and appends a snapshot of every current binding ID, turn ID, observation
state, and observation time. Historical snapshots survive later finalizations
and policy revisions.

A same-snapshot Stop or answer remains LLM Complete. A genuinely new turn may
move the item to Requires attention when it yields `needs_action`,
`answer_available`, or `failed` even if its start/running event was never
delivered. A new running turn moves it to LLM Work. Selection is based on the
newest actionable binding, not necessarily the event that triggered
evaluation.

An item already in LLM Complete when the policy is enabled is adopted as
`existing_state`, not `verified_report`. Configuration snapshots its current
bindings before updating the policy, and does not claim PDF verification. The
adoption cutoff protects same-turn late observations while allowing a new turn
to reopen attention or work.

## Configuration and diagnostics

Diagnostics are read-only by default:

```powershell
.venv\Scripts\python.exe manage.py issue_chat_workflow diagnostics `
  --workspace personal `
  --project-id 493d1fd1-ec7b-475d-b835-a0ecd2c903af
```

Configuration requires an explicit `configure` action, `--enable`, an active
project admin/member operator, and all required state IDs:

```powershell
.venv\Scripts\python.exe manage.py issue_chat_workflow configure `
  --workspace personal `
  --project-id 493d1fd1-ec7b-475d-b835-a0ecd2c903af `
  --operator-id <active-project-member-uuid> `
  --enable `
  --source-state-id <in-progress-uuid> `
  --running-state-id <llm-work-uuid> `
  --attention-state-id <requires-attention-uuid> `
  --manual-hold-state-id <come-back-later-uuid> `
  --completion-state-id <llm-complete-uuid> `
  --todo-state-id <todo-uuid> `
  --stale-running-ttl-seconds 3600
```

`--todo-state-id` is optional. Use `--disable` with `configure` to stop runtime
mutation while retaining the last valid mapping. Every configuration revision
increments the policy version, sets a new effective timestamp, and preserves
manual/completion cutoffs. Enabling may append `existing_state` completion
snapshots to cursors; it does not rewrite issue states, descriptions, bindings,
or audit history.

Diagnostics include the validated mapping, per-binding reasons such as
`active_running`, `stale_running`, `answer_ready_missing_report`,
`needs_action`, `manual_resume_requires_newer_event`, and
`completion_preserved`, plus cutoff sources, completion snapshot counts, recent
durable decisions, and each binding's last decision.

## Persistence and locking

The integration uses three server-side records:

- a per-project policy with effective timestamp, version, state mappings, and
  stale-running TTL;
- a per-issue cursor with expected state, diagnostic event baseline,
  manual-resume cutoff/source, completion cutoff/source, append-only completion
  snapshots, last turn, and last reason;
- append-only decision audits with a deterministic idempotency key, previous
  and target state, aggregate binding details, and the applied/skipped reason.

Observation evaluation locks the issue, policy, and bindings in that stable
order. Completion adoption locks the project, relevant completion issues in
stable ID order, policy, cursors, and bindings. Policy is never locked before an
issue that configuration will snapshot, preventing a configuration/evaluation
deadlock.

The backend accepts same-turn `needs_action` to `running`. The local hook
producer is maintained separately; backend configuration does not depend on
its transport details.
