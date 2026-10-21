import assert from "node:assert/strict";
import test from "node:test";

import { ChatSyncError } from "../src/errors.ts";
import { MAX_CURSOR, nextCursor } from "../src/state.ts";

test("advances cursors without exceeding the safe integer bound", () => {
  assert.equal(nextCursor(0, 1_800_000_000), 1_800_000_000);
  assert.equal(nextCursor(1_800_000_000, 1), 1_800_000_001);
  assert.equal(nextCursor(MAX_CURSOR - 1, 1), MAX_CURSOR);
  assert.throws(
    () => nextCursor(MAX_CURSOR, 1),
    (error: unknown) => error instanceof ChatSyncError && error.code === "protocol_incompatible"
  );
});
