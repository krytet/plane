/**
 * Copyright (c) 2023-present Plane Software, Inc. and contributors
 * SPDX-License-Identifier: AGPL-3.0-only
 * See the LICENSE file for details.
 */

import { ChatOutline } from "@makeplane/propel/icons";
import type { TIssueChatPresence } from "@plane/types";
import { cn } from "@plane/utils";
// local imports
import { getIssueChatPresenceBadge } from "./chat-presence-helpers";

const TONE_CLASSES = {
  attention: "border-accent-strong/20 bg-accent-primary/10 text-accent-secondary",
  unread: "border-accent-strong/20 bg-surface-1 text-accent-secondary",
  count: "border-subtle bg-surface-1 text-tertiary",
} as const;

type Props = {
  presence?: TIssueChatPresence;
};

export function IssueChatPresenceBadge(props: Props) {
  const { presence } = props;
  const badge = getIssueChatPresenceBadge(presence);
  if (!badge) return null;

  return (
    <span
      aria-label={badge.title}
      title={badge.title}
      className={cn(
        "flex h-5 flex-shrink-0 items-center gap-1.5 rounded-sm border px-2.5 py-1 text-caption-sm-regular",
        TONE_CLASSES[badge.tone]
      )}
    >
      <ChatOutline className="h-3 w-3 flex-shrink-0" aria-hidden="true" />
      <span>{badge.label}</span>
    </span>
  );
}
