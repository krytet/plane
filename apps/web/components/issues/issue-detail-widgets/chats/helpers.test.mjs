import assert from "node:assert/strict";
import test from "node:test";
// local imports
import {
  formatIssueChatFreshness,
  getCodexThreadDeepLink,
  getIssueChatBindingUrlValidationError,
  getIssueChatUrlValidationError,
  getIssueChatProviderLabel,
  getSafeIssueChatWebLink,
  hasIssueChatProgress,
  isValidIssueChatUrl,
  normalizeIssueChatProgress,
} from "./helpers.ts";

test("normalizes and detects chat progress", () => {
  const progress = normalizeIssueChatProgress({
    done: ["Shipped", "  "],
    now: ["Review"],
    waiting: [],
  });

  assert.deepEqual(progress, {
    done: ["Shipped"],
    now: ["Review"],
    waiting: [],
  });
  assert.equal(hasIssueChatProgress(progress), true);
  assert.equal(hasIssueChatProgress({ done: [], now: ["  "], waiting: [] }), false);
});

test("formats freshness without claiming a sync for null timestamps", () => {
  const now = Date.parse("2026-10-21T12:00:00Z");

  assert.equal(formatIssueChatFreshness(null, now), "Not synced yet");
  assert.equal(formatIssueChatFreshness("invalid", now), "Sync time unavailable");
  assert.equal(formatIssueChatFreshness("2026-10-21T11:59:45Z", now), "Synced just now");
  assert.equal(formatIssueChatFreshness("2026-10-21T10:00:00Z", now), "Synced 2 hours ago");
  assert.equal(formatIssueChatFreshness("2026-10-19T12:00:00Z", now), "Synced 2 days ago");
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
