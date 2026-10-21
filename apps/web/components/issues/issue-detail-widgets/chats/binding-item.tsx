/**
 * Copyright (c) 2023-present Plane Software, Inc. and contributors
 * SPDX-License-Identifier: AGPL-3.0-only
 * See the LICENSE file for details.
 */

import { NewTabOutline } from "@makeplane/propel/icons";
import type { TIssueChatBinding, TIssueChatProgress } from "@plane/types";
// local imports
import {
  formatIssueChatFreshness,
  getCodexThreadDeepLink,
  getIssueChatProviderLabel,
  getSafeIssueChatWebLink,
  hasIssueChatProgress,
  normalizeIssueChatProgress,
} from "./helpers";

const PROGRESS_GROUPS: { key: keyof TIssueChatProgress; label: string }[] = [
  { key: "done", label: "Done" },
  { key: "now", label: "Now" },
  { key: "waiting", label: "Waiting" },
];

type Props = {
  binding: TIssueChatBinding;
};

export function IssueChatBindingItem(props: Props) {
  const { binding } = props;
  const progress = normalizeIssueChatProgress(binding.progress);
  const hasProgress = hasIssueChatProgress(progress);
  const nativeCodexUrl = binding.provider === "codex" ? getCodexThreadDeepLink(binding.thread_id) : undefined;
  const webLink = getSafeIssueChatWebLink(binding.link?.url);
  const webLinkLabel = binding.link?.title?.trim() || webLink || "Chat link unavailable";

  return (
    <article className="space-y-3 rounded-md border border-subtle bg-surface-2 p-3">
      <div className="flex flex-wrap items-start justify-between gap-2">
        <div className="min-w-0 space-y-1">
          <div className="flex flex-wrap items-center gap-2">
            <span className="rounded-sm bg-accent-primary/10 px-1.5 py-0.5 text-10 font-medium text-accent-primary">
              {getIssueChatProviderLabel(binding.provider)}
            </span>
            <span className="truncate text-11 text-tertiary" title={binding.thread_id}>
              Thread {binding.thread_id}
            </span>
          </div>
          <div className="flex min-w-0 flex-wrap items-center gap-x-3 gap-y-1">
            {nativeCodexUrl && (
              <a
                href={nativeCodexUrl}
                className="group flex min-w-0 items-center gap-1.5 text-body-sm-medium text-primary hover:text-accent-primary"
                aria-label={`Open Codex thread ${binding.thread_id}`}
              >
                <span className="truncate">Open in Codex</span>
                <NewTabOutline className="size-3 shrink-0" />
              </a>
            )}
            {webLink && (
              <a
                href={webLink}
                target="_blank"
                rel="noopener noreferrer"
                className="group flex min-w-0 items-center gap-1.5 text-body-sm-medium text-secondary hover:text-accent-primary"
              >
                <span className="truncate">{webLinkLabel}</span>
                <NewTabOutline className="size-3 shrink-0" />
              </a>
            )}
            {!nativeCodexUrl && !webLink && <p className="text-body-sm-medium text-secondary">Chat link unavailable</p>}
          </div>
        </div>
        <div className="text-right">
          <p className="text-11 font-medium text-secondary">{formatIssueChatFreshness(binding.last_synced_at)}</p>
          <p className="text-10 text-tertiary">Cursor {binding.sync_cursor}</p>
        </div>
      </div>

      <div className="rounded-sm bg-surface-1 px-2.5 py-2 text-11 text-secondary">
        Live sync is unavailable in the web app. This view is read-only and updates after the connected provider session
        syncs.
      </div>

      {hasProgress ? (
        <div className="grid grid-cols-1 gap-3 sm:grid-cols-3">
          {PROGRESS_GROUPS.map((group) => (
            <div key={group.key} className="min-w-0 space-y-1.5">
              <p className="text-11 font-medium text-tertiary">{group.label}</p>
              {progress[group.key].length > 0 ? (
                <ul className="space-y-1">
                  {progress[group.key].map((item) => (
                    <li key={`${group.key}-${item}`} className="text-12 break-words text-primary">
                      {item}
                    </li>
                  ))}
                </ul>
              ) : (
                <p className="text-11 text-tertiary">—</p>
              )}
            </div>
          ))}
        </div>
      ) : (
        <p className="text-11 text-tertiary">No progress has been synced for this chat yet.</p>
      )}
    </article>
  );
}
