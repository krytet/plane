# Project board auto-refresh

Project issue **List** and **Kanban** views refresh already-loaded cards from the server every 15 seconds by default.

Set `VITE_PROJECT_ISSUES_REFRESH_INTERVAL_MS` to change the interval. Values are clamped to 5-300 seconds. The refresh is not a realtime subscription: it is a bounded polling fallback for workflow changes that update issue rows without emitting a board event.

## Behavior

- The board keeps its current filter, grouping, loaded pages, cursors, and manual ordering.
- Filtered changes are fetched with `updated_at__gt`; only currently loaded issue IDs are fetched again for presence-only changes.
- The loaded-ID request is intentionally unfiltered and scoped to IDs already on the board. It detects cards that moved out of the current filter; it never adds unloaded project rows.
- Up to 50 unloaded matching issues are added per refresh. This is a per-poll cap, not a global board-size bound; if more matches exist, the delta watermark stays put so later polls can drain them gradually without adding project-wide rows.
- All requests must finish successfully before any board mutation is applied.
- Before applying, the refresh rechecks the workspace, project, filters, pagination shape, loaded issue IDs, issue revisions, active loaders, and caller safety callback. A stale response is discarded.
- Failed, partial, or discarded refreshes leave the current board unchanged and do not advance the delta watermark.
- Pagination drains are bounded. Exceeding the bound is an explicit refresh failure rather than a silent partial refresh.
- A refresh that needs more than 25 changed pages fails closed without advancing the delta watermark. Reload the board to reset the polling window if that diagnostic repeats.
- Polling pauses while offline or while the document is hidden. A visible window continues refreshing when another window has focus; refocusing the Plane window can trigger an immediate catch-up.
- Polling also waits while dragging, editing, paginating, or performing an issue mutation.
- Drag-and-drop has a short quiet period after drop. Refreshes never start while another refresh is in flight.
- Errors are logged diagnostically at most once per minute; no toast is shown for background failures.

The initial board load and normal pagination remain owned by the existing issue-layout stores and loaders.
