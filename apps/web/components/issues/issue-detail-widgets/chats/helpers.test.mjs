import assert from "node:assert/strict";
import test from "node:test";
// local imports
import {
  createIssueChatRefreshScheduler,
  finishIssueChatMutation,
  formatIssueChatObservationFreshness,
  formatIssueChatProgressSyncFreshness,
  getIssueChatManualAttentionAction,
  getIssueChatObservationSourceLabel,
  getIssueChatObservationStateLabel,
  getIssueChatPlaneReadLabel,
  getCodexThreadDeepLink,
  getIssueChatBindingUrlValidationError,
  getIssueChatUrlValidationError,
  getIssueChatProviderLabel,
  getSafeIssueChatWebLink,
  isCurrentIssueChatRefreshLifecycle,
  isValidIssueChatUrl,
  shouldApplyIssueChatRefresh,
  shouldPollIssueChatBindings,
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

test("formats progress sync separately from provider observation time", () => {
  const now = Date.parse("2026-10-21T12:00:00Z");

  assert.equal(formatIssueChatProgressSyncFreshness(null, false, now), "No progress synced yet");
  assert.equal(formatIssueChatProgressSyncFreshness(null, true, now), "Progress sync time unavailable");
  assert.equal(formatIssueChatProgressSyncFreshness("invalid", true, now), "Progress sync time unavailable");
  assert.equal(formatIssueChatProgressSyncFreshness("2026-10-21T11:59:45Z", true, now), "Progress synced just now");
  assert.equal(formatIssueChatProgressSyncFreshness("2026-10-21T10:00:00Z", true, now), "Progress synced 2 hours ago");
  assert.equal(formatIssueChatProgressSyncFreshness("2026-10-19T12:00:00Z", true, now), "Progress synced 2 days ago");
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

test("rejects stale refresh results after identity, request, or mutation changes", () => {
  const current = { identity: "workspace:project:issue", requestId: 4, mutationRevision: 7 };
  const newerMutation = { ...current, mutationRevision: 8 };

  assert.equal(shouldApplyIssueChatRefresh(current, current), true);
  assert.equal(isCurrentIssueChatRefreshLifecycle(current, newerMutation), true);
  assert.equal(shouldApplyIssueChatRefresh(current, newerMutation), false);
  assert.equal(shouldApplyIssueChatRefresh({ ...current, identity: "other-issue" }, current), false);
  assert.equal(shouldApplyIssueChatRefresh({ ...current, requestId: 3 }, current), false);
  assert.equal(shouldApplyIssueChatRefresh({ ...current, mutationRevision: 6 }, current), false);
});

test("finishes only current-issue mutations before advancing the revision fence", () => {
  const current = {
    mutationIdentity: "workspace:project:issue",
    currentIdentity: "workspace:project:issue",
    isMounted: true,
    mutationRevision: 7,
  };

  assert.deepEqual(finishIssueChatMutation(current), { isCurrent: true, mutationRevision: 8 });
  assert.deepEqual(finishIssueChatMutation({ ...current, currentIdentity: "other-issue" }), {
    isCurrent: false,
    mutationRevision: 7,
  });
  assert.deepEqual(finishIssueChatMutation({ ...current, isMounted: false }), {
    isCurrent: false,
    mutationRevision: 7,
  });
});

test("polls only while an open Codex widget is visible and enabled", () => {
  const enabled = {
    widgetOpen: true,
    disabled: false,
    hasCodexBinding: true,
    isDocumentVisible: true,
  };

  assert.equal(shouldPollIssueChatBindings(enabled), true);
  assert.equal(shouldPollIssueChatBindings({ ...enabled, widgetOpen: false }), false);
  assert.equal(shouldPollIssueChatBindings({ ...enabled, disabled: true }), false);
  assert.equal(shouldPollIssueChatBindings({ ...enabled, hasCodexBinding: false }), false);
  assert.equal(shouldPollIssueChatBindings({ ...enabled, isDocumentVisible: false }), false);
});

test("serializes polling and stops stale interval callbacks on cleanup", async () => {
  const triggers = [];
  let intervalCallback;
  let resolveFirstRefresh;
  let scheduledIntervals = 0;
  let cancelledIntervals = 0;
  let refreshCount = 0;

  const scheduler = createIssueChatRefreshScheduler({
    refresh: (trigger) => {
      triggers.push(trigger);
      refreshCount += 1;
      if (refreshCount === 1) {
        return new Promise((resolve) => {
          resolveFirstRefresh = resolve;
        });
      }
    },
    scheduleInterval: (callback) => {
      scheduledIntervals += 1;
      intervalCallback = callback;
      return scheduledIntervals;
    },
    cancelInterval: () => {
      cancelledIntervals += 1;
    },
  });

  scheduler.start();
  scheduler.start();
  assert.equal(scheduledIntervals, 1);

  intervalCallback?.();
  const coalescedRequest = scheduler.request("manual");
  await Promise.resolve();
  assert.deepEqual(triggers, ["interval"]);

  intervalCallback?.();
  await Promise.resolve();
  assert.deepEqual(triggers, ["interval"]);

  resolveFirstRefresh?.();
  await coalescedRequest;
  scheduler.stop();
  assert.equal(cancelledIntervals, 1);

  intervalCallback?.();
  await Promise.resolve();
  assert.deepEqual(triggers, ["interval"]);

  await scheduler.request("manual");
  assert.deepEqual(triggers, ["interval", "manual"]);
});

test("keeps polling after a rejected quiet refresh", async () => {
  let refreshCount = 0;
  const scheduler = createIssueChatRefreshScheduler({
    refresh: async () => {
      refreshCount += 1;
      if (refreshCount === 1) throw new Error("transient");
    },
  });

  await scheduler.request();
  await scheduler.request();
  assert.equal(refreshCount, 2);
});
