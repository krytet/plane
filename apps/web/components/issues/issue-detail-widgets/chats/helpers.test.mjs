import assert from "node:assert/strict";
import test from "node:test";
// local imports
import {
  formatIssueChatObservationFreshness,
  getIssueChatManualAttentionAction,
  getIssueChatObservationSourceLabel,
  getIssueChatObservationStateLabel,
  getIssueChatPlaneReadLabel,
  getCodexThreadDeepLink,
  getIssueChatBindingUrlValidationError,
  getIssueChatUrlValidationError,
  getIssueChatProviderLabel,
  getSafeIssueChatWebLink,
  isValidIssueChatUrl,
} from "./helpers.ts";

test("labels observations and their source without claiming provider read state", () => {
  assert.equal(getIssueChatObservationStateLabel("answer_available"), "Answer available");
  assert.equal(getIssueChatObservationStateLabel("needs_action"), "Needs action");
  assert.equal(getIssueChatObservationStateLabel("failed"), "Failed");
  assert.equal(getIssueChatObservationStateLabel("unknown"), "No reported state");
  assert.equal(getIssueChatObservationSourceLabel("codex"), "Codex observation");
  assert.equal(getIssueChatObservationSourceLabel("manual"), "Marked manually in Plane");
  assert.equal(getIssueChatObservationSourceLabel(null), "Source not reported");
  assert.equal(getIssueChatPlaneReadLabel({ attention: true, unread: true, seen_at: null }), "Unread in Plane");
  assert.equal(
    getIssueChatPlaneReadLabel({ attention: true, unread: false, seen_at: "2026-10-21T12:00:00Z" }),
    "Seen in Plane"
  );
  assert.equal(getIssueChatPlaneReadLabel({ attention: false, unread: false, seen_at: null }), undefined);
});

test("formats observation freshness without implying automatic sync", () => {
  const now = Date.parse("2026-10-21T12:00:00Z");

  assert.equal(formatIssueChatObservationFreshness(null, now), "No observation yet");
  assert.equal(formatIssueChatObservationFreshness("invalid", now), "Observation time unavailable");
  assert.equal(formatIssueChatObservationFreshness("2026-10-21T11:59:45Z", now), "Observed just now");
  assert.equal(formatIssueChatObservationFreshness("2026-10-21T10:00:00Z", now), "Observed 2 hours ago");
  assert.equal(formatIssueChatObservationFreshness("2026-10-19T12:00:00Z", now), "Observed 2 days ago");
});

test("offers manual attention only for ChatGPT bindings", () => {
  assert.deepEqual(getIssueChatManualAttentionAction({ provider: "chatgpt", manual_attention: false }), {
    label: "Mark needs action",
    value: "needs_action",
  });
  assert.deepEqual(getIssueChatManualAttentionAction({ provider: "chatgpt", manual_attention: true }), {
    label: "Clear manual attention",
    value: "clear",
  });
  assert.equal(getIssueChatManualAttentionAction({ provider: "codex", manual_attention: false }), null);
});

test("matches the backend provider URL contract", () => {
  assert.equal(isValidIssueChatUrl("https://chatgpt.com/c/example", "chatgpt"), true);
  assert.equal(isValidIssueChatUrl("https://chat.openai.com/c/example", "codex"), true);
  assert.equal(isValidIssueChatUrl("http://localhost:3000/thread", "codex"), true);
  assert.equal(isValidIssueChatUrl("http://chatgpt.com/c/example", "chatgpt"), false);
  assert.equal(isValidIssueChatUrl("https://chatgpt.com.evil.example/c/example", "chatgpt"), false);
  assert.equal(isValidIssueChatUrl("https://user:token@chatgpt.com/c/example", "chatgpt"), false);
  assert.equal(isValidIssueChatUrl("codex://thread/123", "codex"), false);
  assert.match(getIssueChatUrlValidationError("http://chatgpt.com/c/example", "chatgpt"), /HTTPS/);
  assert.equal(getIssueChatProviderLabel("codex"), "Codex");
  assert.equal(getIssueChatProviderLabel("chatgpt"), "ChatGPT");
});

test("builds and validates only exact native Codex thread deep links", () => {
  const threadId = "01900000-0000-7000-8000-000000000000";
  const otherThreadId = "01900000-0000-7000-8000-000000000001";
  const nativeUrl = `codex://threads/${threadId}`;

  assert.equal(getCodexThreadDeepLink(threadId), nativeUrl);
  assert.equal(getCodexThreadDeepLink(` ${threadId.toUpperCase()} `), nativeUrl);
  assert.equal(getCodexThreadDeepLink("thread-not-a-uuid"), undefined);
  assert.equal(getIssueChatBindingUrlValidationError("", "codex", threadId), undefined);
  assert.equal(getIssueChatBindingUrlValidationError(nativeUrl, "codex", threadId), undefined);
  assert.match(getIssueChatBindingUrlValidationError("", "codex", "thread-1"), /required/i);
  assert.match(getIssueChatBindingUrlValidationError("", "chatgpt", threadId), /required/i);
  assert.match(getIssueChatBindingUrlValidationError(`codex://threads/${otherThreadId}`, "codex", threadId), /match/);
  assert.match(getIssueChatBindingUrlValidationError(nativeUrl, "chatgpt", threadId), /HTTPS|scheme|valid/i);
  assert.equal(
    getIssueChatBindingUrlValidationError("https://chatgpt.com/codex/tasks/example", "codex", threadId),
    undefined
  );
  assert.equal(getSafeIssueChatWebLink(nativeUrl), undefined);
  assert.equal(getSafeIssueChatWebLink("javascript:alert(1)"), undefined);
  assert.equal(
    getSafeIssueChatWebLink("https://chatgpt.com/codex/tasks/example"),
    "https://chatgpt.com/codex/tasks/example"
  );
});
