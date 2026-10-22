/**
 * Copyright (c) 2023-present Plane Software, Inc. and contributors
 * SPDX-License-Identifier: AGPL-3.0-only
 * See the LICENSE file for details.
 */

import { Button } from "@makeplane/propel/components/button";
import { NewTabOutline } from "@makeplane/propel/icons";
import type { TIssueChatBinding, TIssueChatManualAttention, TIssueChatProgress } from "@plane/types";
import { cn } from "@plane/utils";
// local imports
import {
  formatIssueChatObservationFreshness,
  formatIssueChatProgressSyncFreshness,
  getCodexThreadDeepLink,
  getIssueChatManualAttentionAction,
  getIssueChatObservationSourceLabel,
  getIssueChatObservationStateLabel,
  getIssueChatPlaneReadLabel,
  getIssueChatProviderLabel,
  getSafeIssueChatWebLink,
} from "./helpers";

type Props = {
  binding: TIssueChatBinding;
  disabled: boolean;
  actionError?: string;
  pendingAction?: "seen" | "manual_attention";
  onMarkSeen: () => Promise<void>;
  onManualAttention: (value: TIssueChatManualAttention) => Promise<void>;
};

const PROGRESS_GROUPS: { key: keyof TIssueChatProgress; label: string }[] = [
  { key: "done", label: "Done" },
  { key: "now", label: "Now" },
  { key: "waiting", label: "Waiting" },
];

export function IssueChatBindingItem(props: Props) {
  const { binding, disabled, actionError, pendingAction, onMarkSeen, onManualAttention } = props;
  const nativeCodexUrl = binding.provider === "codex" ? getCodexThreadDeepLink(binding.thread_id) : undefined;
  const webLink = getSafeIssueChatWebLink(binding.link?.url);
  const webLinkLabel = binding.link?.title?.trim() || webLink || "Chat link unavailable";
  const stateLabel = getIssueChatObservationStateLabel(binding.observation_state);
  const sourceLabel = getIssueChatObservationSourceLabel(binding.observation_source);
  const planeReadLabel = getIssueChatPlaneReadLabel(binding);
  const manualAttentionAction = getIssueChatManualAttentionAction(binding);
  const progress = binding.progress;
  const hasProgress = PROGRESS_GROUPS.some(({ key }) => Array.isArray(progress?.[key]) && progress[key].length > 0);

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
        <div className="space-y-1 text-right text-11 text-tertiary">
          <p>{formatIssueChatObservationFreshness(binding.observation_observed_at)}</p>
          <p>{formatIssueChatProgressSyncFreshness(binding.last_synced_at, hasProgress)}</p>
        </div>
      </div>

      <div className="flex flex-wrap items-center gap-2">
        <span
          className={cn(
            "rounded-sm border px-1.5 py-0.5 text-10 font-medium",
            binding.attention
              ? "border-accent-strong/20 bg-accent-primary/10 text-accent-secondary"
              : "border-subtle bg-surface-1 text-secondary"
          )}
        >
          {stateLabel}
        </span>
        {planeReadLabel && (
          <span className="rounded-sm border border-subtle bg-surface-1 px-1.5 py-0.5 text-10 text-secondary">
            {planeReadLabel}
          </span>
        )}
        <span className="text-11 text-tertiary">{sourceLabel}</span>
      </div>

      <p className="text-11 text-tertiary">
        Seen is tracked only in Plane, not in the chat provider. Opening this chat does not mark it seen here.
      </p>

      {hasProgress && (
        <div className="grid grid-cols-1 gap-3 sm:grid-cols-3">
          {PROGRESS_GROUPS.map(({ key, label }) => (
            <div key={key} className="min-w-0 space-y-1.5">
              <p className="text-11 font-medium text-tertiary">{label}</p>
              {progress[key]?.length ? (
                <ul className="space-y-1">
                  {progress[key].map((item) => (
                    <li key={`${key}-${item}`} className="text-12 break-words text-primary">
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
      )}

      {(binding.unread || manualAttentionAction) && (
        <div className="flex flex-wrap items-center gap-2">
          {binding.unread && (
            <Button
              type="button"
              variant="secondary"
              size="sm"
              stretch="auto"
              label="Mark seen in Plane"
              loading={pendingAction === "seen"}
              disabled={disabled || (!!pendingAction && pendingAction !== "seen")}
              onClick={() => void onMarkSeen()}
            />
          )}
          {manualAttentionAction && (
            <Button
              type="button"
              variant="secondary"
              size="sm"
              stretch="auto"
              label={manualAttentionAction.label}
              loading={pendingAction === "manual_attention"}
              disabled={disabled || (!!pendingAction && pendingAction !== "manual_attention")}
              onClick={() => void onManualAttention(manualAttentionAction.value)}
            />
          )}
        </div>
      )}

      {actionError && (
        <p role="alert" className="text-11 text-danger-primary">
          {actionError}
        </p>
      )}
    </article>
  );
}
