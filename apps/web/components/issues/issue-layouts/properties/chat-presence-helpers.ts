/**
 * Copyright (c) 2023-present Plane Software, Inc. and contributors
 * SPDX-License-Identifier: AGPL-3.0-only
 * See the LICENSE file for details.
 */

import type { TIssueChatPresence } from "@plane/types";

export type TIssueChatPresenceBadge = {
  label: string;
  title: string;
  tone: "attention" | "unread" | "count";
};

const normalizeCount = (value: number): number => (Number.isFinite(value) ? Math.max(0, Math.floor(value)) : 0);

export const getIssueChatPresenceBadge = (
  presence: Partial<TIssueChatPresence> | null | undefined
): TIssueChatPresenceBadge | null => {
  if (!presence || typeof presence.count !== "number") return null;

  const count = normalizeCount(presence.count);
  if (count === 0) return null;

  const attention = normalizeCount(presence.attention ?? 0);
  const unread = normalizeCount(presence.unread ?? 0);
  const manualAttention = normalizeCount(presence.manual_attention ?? 0);
  const titleParts = [
    `Chats: ${count}`,
    attention ? `Needs attention: ${attention}` : null,
    unread ? `Unread in Plane: ${unread}` : null,
    manualAttention ? `Manually marked: ${manualAttention}` : null,
  ].filter((part): part is string => Boolean(part));
  const title = `${titleParts.join(". ")}.`;

  if (attention > 0) {
    return {
      label: `${attention} ${attention === 1 ? "needs" : "need"} attention`,
      title,
      tone: "attention",
    };
  }

  if (unread > 0) {
    return {
      label: `${unread} unread in Plane`,
      title,
      tone: "unread",
    };
  }

  return {
    label: `${count} ${count === 1 ? "chat" : "chats"}`,
    title,
    tone: "count",
  };
};
