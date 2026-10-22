import assert from "node:assert/strict";
import test from "node:test";

import { ChatSyncError } from "../src/errors.ts";
import { getFinalAgentText, parseProgressFromFinalText, parseStatusBlock } from "../src/status-block.ts";

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

test("uses an explicitly labelled extractive summary when no status block exists", () => {
  const finalText = [
    "# Result",
    "The implementation is ready for a focused local review.",
    "Open [the report](https://example.test/private/report) and run the checks.",
    "```ts",
    "const leakedPath = 'C:\\private\\file.ts';",
    "```",
    "Do not infer that the overall task is complete.",
  ].join("\n");

  const result = parseProgressFromFinalText(finalText, "structured_or_summary");

  assert.equal(result.source, "summary");
  assert.deepEqual(result.progress.done, []);
  assert.deepEqual(result.progress.waiting, []);
  assert.equal(result.progress.now.length, 1);
  const summary = result.progress.now[0] ?? "";
  assert.match(summary, /^Последний ответ Codex \(кратко\):/);
  assert.match(summary, /implementation is ready/);
  assert.doesNotMatch(summary, /https?:\/\//);
  assert.doesNotMatch(summary, /C:\\private/);
  assert.doesNotMatch(summary, /leakedPath/);
  assert.ok(summary.length <= 400);
});

test("keeps the structured block preferred over fallback prose", () => {
  const result = parseProgressFromFinalText(
    `Ordinary prose that would otherwise become a summary.\n${VALID}`,
    "structured_or_summary"
  );

  assert.equal(result.source, "status_block");
  assert.deepEqual(result.progress.done, ["API endpoint", "Mock tests"]);
});

test("bounds a longer ordinary answer to a brief two-line excerpt", () => {
  const finalText = [
    "The local adapter now validates the configured Codex thread before reading turns.",
    "It uses the newest summary view and isolates a verified missing target.",
    "This third sentence must not be copied into the short Plane item.",
    "A fourth sentence is also outside the excerpt.",
  ].join(" ");

  const result = parseProgressFromFinalText(finalText, "structured_or_summary");
  const summary = result.progress.now[0] ?? "";

  assert.match(summary, /validates the configured Codex thread/);
  assert.match(summary, /newest summary view/);
  assert.doesNotMatch(summary, /third sentence/);
  assert.ok(summary.length <= 400);
});

test("rejects malformed explicit blocks even when summaries are enabled", () => {
  const malformed = "Ordinary prose.\n```plane-status\nDone:\n- one\n```";
  assert.equal(
    errorCode(() => parseProgressFromFinalText(malformed, "structured_or_summary")),
    "status_block_invalid"
  );
  assert.equal(
    errorCode(() =>
      parseProgressFromFinalText("```plane-status extra\nDone:\nNow:\nWaiting:\n```", "structured_or_summary")
    ),
    "status_block_invalid"
  );
});

test("scans the whole final answer for secrets before fallback extraction", () => {
  const secret = "plane_api_0123456789abcdef0123456789abcdef";
  const finalText = ["A normal response summary.", "```text", `token=${secret}`, "```"].join("\n");

  try {
    parseProgressFromFinalText(finalText, "structured_or_summary");
    assert.fail("expected ChatSyncError");
  } catch (error) {
    assert.ok(error instanceof ChatSyncError);
    assert.equal(error.code, "status_secret_detected");
    assert.doesNotMatch(error.message, new RegExp(secret));
  }
});

test("rejects env-style AWS credentials and database URI credentials without echoing them", () => {
  const values = [
    "AWS_SECRET_ACCESS_KEY=not-a-real-aws-secret",
    "DATABASE_URL=postgresql://plane:not-a-real-db-password@127.0.0.1:5432/plane",
  ];

  for (const value of values) {
    assert.equal(
      errorCode(() => parseProgressFromFinalText(`A normal answer.\n${value}`, "structured_or_summary")),
      "status_secret_detected"
    );
  }
});
