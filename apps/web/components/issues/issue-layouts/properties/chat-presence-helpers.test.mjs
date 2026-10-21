import assert from "node:assert/strict";
import test from "node:test";
// local imports
import { getIssueChatPresenceBadge } from "./chat-presence-helpers.ts";

test("hides chat presence when guest-facing fields are absent or empty", () => {
  assert.equal(getIssueChatPresenceBadge(undefined), null);
  assert.equal(getIssueChatPresenceBadge({ count: 0, attention: 0, unread: 0, manual_attention: 0 }), null);
});

test("prioritizes attention, then Plane unread, then chat count", () => {
  assert.equal(
    getIssueChatPresenceBadge({ count: 3, attention: 2, unread: 1, manual_attention: 1 })?.label,
    "2 need attention"
  );
  assert.equal(
    getIssueChatPresenceBadge({ count: 2, attention: 0, unread: 1, manual_attention: 0 })?.label,
    "1 unread in Plane"
  );
  assert.equal(getIssueChatPresenceBadge({ count: 1, attention: 0, unread: 0, manual_attention: 0 })?.label, "1 chat");
});

test("uses a Plane-local, non-interactive badge description", () => {
  const badge = getIssueChatPresenceBadge({
    count: 2,
    attention: 1,
    unread: 1,
    manual_attention: 1,
  });

  assert.deepEqual(badge, {
    label: "1 needs attention",
    title: "Chats: 2. Needs attention: 1. Unread in Plane: 1. Manually marked: 1.",
    tone: "attention",
  });
});
