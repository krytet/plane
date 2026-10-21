import assert from "node:assert/strict";
import test from "node:test";

import { ChatSyncError } from "../src/errors.ts";
import { getFinalAgentText, parseStatusBlock } from "../src/status-block.ts";

const VALID = [
  "Summary text that must not be forwarded.",
  "```plane-status",
  "Done:",
  "- API endpoint",
  "- Mock tests",
  "Now:",
  "- Local review",
  "Waiting:",
  "- Deployment approval",
  "```",
  "Trailing text that must also not be forwarded.",
].join("\n");

function errorCode(fn: () => unknown): string {
  try {
    fn();
  } catch (error) {
    assert.ok(error instanceof ChatSyncError);
    return error.code;
  }
  assert.fail("expected ChatSyncError");
}

test("parses one explicit block and returns only structured status", () => {
  assert.deepEqual(parseStatusBlock(VALID), {
    done: ["API endpoint", "Mock tests"],
    now: ["Local review"],
    waiting: ["Deployment approval"],
  });
});

test("requires exactly one complete Done/Now/Waiting block", () => {
  assert.equal(
    errorCode(() => parseStatusBlock("No block here")),
    "status_block_missing"
  );
  assert.equal(
    errorCode(() => parseStatusBlock("```plane-status\nDone:\n- one\n```")),
    "status_block_invalid"
  );
  assert.equal(
    errorCode(() => parseStatusBlock(`${VALID}\n${VALID}`)),
    "status_block_invalid"
  );
  assert.equal(
    errorCode(() => parseStatusBlock("```plane-status\nDone:\n- one\nNow:\n- two\nWaiting:\n- three\nanything\n```")),
    "status_block_invalid"
  );
});

test("rejects secret-like status content without echoing it", () => {
  const code = errorCode(() =>
    parseStatusBlock("```plane-status\nDone:\n- API_KEY=not-a-real-secret-value\nNow:\nWaiting:\n```")
  );
  assert.equal(code, "status_secret_detected");
});

test("uses the final answer item and ignores commentary and partial answers", () => {
  const text = getFinalAgentText([
    { type: "agentMessage", phase: "commentary", text: "commentary" },
    { type: "agentMessage", phase: "partial_answer", text: "partial" },
    { type: "agentMessage", phase: "final_answer", text: VALID },
  ]);
  assert.equal(text, VALID);

  assert.equal(getFinalAgentText([{ type: "agentMessage", phase: "commentary", text: VALID }]), null);
});
