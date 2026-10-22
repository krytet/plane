/**
 * Copyright (c) 2023-present Plane Software, Inc. and contributors
 * SPDX-License-Identifier: AGPL-3.0-only
 * See the LICENSE file for details.
 */

import type {
  TIssueChatBinding,
  TIssueChatObservationSource,
  TIssueChatObservationState,
  TIssueChatProvider,
} from "@plane/types";

const PROVIDER_LABELS: Record<TIssueChatProvider, string> = {
  codex: "Codex",
  chatgpt: "ChatGPT",
};

const OBSERVATION_STATE_LABELS: Record<TIssueChatObservationState, string> = {
  unknown: "No reported state",
  running: "Running",
  answer_available: "Answer available",
  needs_action: "Needs action",
  failed: "Failed",
};

const OBSERVATION_SOURCE_LABELS: Record<TIssueChatObservationSource, string> = {
  manual: "Marked manually in Plane",
  codex: "Codex observation",
};

const PROVIDER_HOSTS: Record<TIssueChatProvider, Set<string>> = {
  codex: new Set(["chatgpt.com", "www.chatgpt.com", "chat.openai.com", "www.chat.openai.com"]),
  chatgpt: new Set(["chatgpt.com", "www.chatgpt.com", "chat.openai.com", "www.chat.openai.com"]),
};

const LOCAL_DEBUG_HOSTS = new Set(["localhost", "127.0.0.1", "::1"]);
const UUID_RE = /^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$/i;
const CODEX_NATIVE_URL_PREFIX = "codex://threads/";

export const getIssueChatProviderLabel = (provider: TIssueChatProvider): string =>
  PROVIDER_LABELS[provider] ?? provider;

export const getIssueChatObservationStateLabel = (state: TIssueChatObservationState): string =>
  OBSERVATION_STATE_LABELS[state] ?? state;

export const getIssueChatObservationSourceLabel = (source: TIssueChatObservationSource | null): string =>
  source ? OBSERVATION_SOURCE_LABELS[source] : "Source not reported";

export const formatIssueChatObservationFreshness = (
  observedAt: string | null | undefined,
  now = Date.now()
): string => {
  if (!observedAt) return "No observation yet";

  const timestamp = Date.parse(observedAt);
  if (Number.isNaN(timestamp)) return "Observation time unavailable";

  const elapsedSeconds = Math.max(0, Math.floor((now - timestamp) / 1000));
  if (elapsedSeconds < 60) return "Observed just now";

  const units = [
    { seconds: 86_400, label: "day" },
    { seconds: 3_600, label: "hour" },
    { seconds: 60, label: "minute" },
  ] as const;

  for (const unit of units) {
    if (elapsedSeconds >= unit.seconds) {
      const count = Math.floor(elapsedSeconds / unit.seconds);
      return `Observed ${count} ${unit.label}${count === 1 ? "" : "s"} ago`;
    }
  }

  return "Observed just now";
};

export const formatIssueChatProgressSyncFreshness = (
  syncedAt: string | null | undefined,
  hasProgress: boolean,
  now = Date.now()
): string => {
  if (!hasProgress) return "No progress synced yet";
  if (!syncedAt) return "Progress sync time unavailable";

  const timestamp = Date.parse(syncedAt);
  if (Number.isNaN(timestamp)) return "Progress sync time unavailable";

  const elapsedSeconds = Math.max(0, Math.floor((now - timestamp) / 1000));
  if (elapsedSeconds < 60) return "Progress synced just now";

  const units = [
    { seconds: 86_400, label: "day" },
    { seconds: 3_600, label: "hour" },
    { seconds: 60, label: "minute" },
  ] as const;

  for (const unit of units) {
    if (elapsedSeconds >= unit.seconds) {
      const count = Math.floor(elapsedSeconds / unit.seconds);
      return `Progress synced ${count} ${unit.label}${count === 1 ? "" : "s"} ago`;
    }
  }

  return "Progress synced just now";
};

export const getIssueChatPlaneReadLabel = (
  binding: Pick<TIssueChatBinding, "attention" | "unread" | "seen_at">
): string | undefined => {
  if (!binding.attention) return undefined;
  if (binding.unread) return "Unread in Plane";
  return binding.seen_at ? "Seen in Plane" : undefined;
};

export const getIssueChatManualAttentionAction = (
  binding: Pick<TIssueChatBinding, "provider" | "manual_attention">
): { label: string; value: "needs_action" | "clear" } | null => {
  if (binding.provider !== "chatgpt") return null;
  return binding.manual_attention
    ? { label: "Clear manual attention", value: "clear" }
    : { label: "Mark needs action", value: "needs_action" };
};

export const getCodexThreadDeepLink = (threadId: string): string | undefined => {
  const normalizedThreadId = threadId.trim();
  if (!UUID_RE.test(normalizedThreadId)) return undefined;
  return `${CODEX_NATIVE_URL_PREFIX}${normalizedThreadId.toLowerCase()}`;
};

export const getIssueChatUrlValidationError = (value: string, provider: TIssueChatProvider): string | undefined => {
  try {
    const url = new URL(value.trim());
    if (url.username || url.password) return "Chat URLs cannot contain credentials.";
    if (LOCAL_DEBUG_HOSTS.has(url.hostname)) {
      return url.protocol === "http:" || url.protocol === "https:"
        ? undefined
        : "Enter a valid http:// or https:// chat URL.";
    }
    if (url.protocol !== "https:") return "Provider chat URLs must use HTTPS.";
    if (url.port && url.port !== "443") return "Provider chat URLs must use the default HTTPS port.";
    if (!PROVIDER_HOSTS[provider].has(url.hostname)) {
      return `The URL must belong to ${provider === "codex" ? "Codex" : "ChatGPT"} on chatgpt.com.`;
    }
    return undefined;
  } catch {
    return "Enter a valid http:// or https:// chat URL.";
  }
};

export const isValidIssueChatUrl = (value: string, provider: TIssueChatProvider): boolean =>
  !getIssueChatUrlValidationError(value, provider);

export const getIssueChatBindingUrlValidationError = (
  value: string,
  provider: TIssueChatProvider,
  threadId: string
): string | undefined => {
  const normalizedValue = value.trim();
  const nativeCodexUrl = provider === "codex" ? getCodexThreadDeepLink(threadId) : undefined;

  if (!normalizedValue) {
    return provider === "codex" && nativeCodexUrl ? undefined : "A chat URL is required.";
  }

  if (provider === "codex" && normalizedValue.toLowerCase().startsWith("codex:")) {
    if (!nativeCodexUrl) return "Native Codex links require a UUID thread ID.";
    try {
      const url = new URL(normalizedValue);
      const expected = new URL(nativeCodexUrl);
      const matchesExpectedThread =
        url.protocol === expected.protocol &&
        url.hostname === expected.hostname &&
        !url.port &&
        !url.username &&
        !url.password &&
        !url.search &&
        !url.hash &&
        url.pathname.toLowerCase() === expected.pathname;
      return matchesExpectedThread
        ? undefined
        : "Native Codex links must match codex://threads/{thread_id} with the thread UUID.";
    } catch {
      return "Native Codex links must match codex://threads/{thread_id} with the thread UUID.";
    }
  }

  return getIssueChatUrlValidationError(normalizedValue, provider);
};

export const getSafeIssueChatWebLink = (value: string | null | undefined): string | undefined => {
  if (!value) return undefined;
  try {
    const url = new URL(value.trim());
    if ((url.protocol !== "https:" && url.protocol !== "http:") || !url.hostname) return undefined;
    if (url.username || url.password) return undefined;
    return url.toString();
  } catch {
    return undefined;
  }
};

export const ISSUE_CHAT_REFRESH_INTERVAL_MS = 15_000;

export type TIssueChatRefreshTrigger = "interval" | "focus" | "manual";

type TIssueChatRefreshLifecycleGuard = {
  identity: string;
  requestId: number;
};

type TIssueChatRefreshGuard = TIssueChatRefreshLifecycleGuard & {
  mutationRevision: number;
};

export const isCurrentIssueChatRefreshLifecycle = (
  request: TIssueChatRefreshLifecycleGuard,
  current: TIssueChatRefreshLifecycleGuard
): boolean => request.identity === current.identity && request.requestId === current.requestId;

export const shouldApplyIssueChatRefresh = (
  request: TIssueChatRefreshGuard,
  current: TIssueChatRefreshGuard
): boolean =>
  isCurrentIssueChatRefreshLifecycle(request, current) && request.mutationRevision === current.mutationRevision;

export const finishIssueChatMutation = (options: {
  mutationIdentity: string;
  currentIdentity: string;
  isMounted: boolean;
  mutationRevision: number;
}): { isCurrent: boolean; mutationRevision: number } => {
  const isCurrent = options.isMounted && options.mutationIdentity === options.currentIdentity;
  return {
    isCurrent,
    mutationRevision: isCurrent ? options.mutationRevision + 1 : options.mutationRevision,
  };
};

export const shouldPollIssueChatBindings = (options: {
  widgetOpen: boolean;
  disabled: boolean;
  hasCodexBinding: boolean;
  isDocumentVisible: boolean;
}): boolean => options.widgetOpen && !options.disabled && options.hasCodexBinding && options.isDocumentVisible;

type TIssueChatRefreshScheduler = {
  start: () => void;
  stop: () => void;
  request: (trigger?: TIssueChatRefreshTrigger) => Promise<void>;
};

type TIssueChatRefreshSchedulerOptions = {
  refresh: (trigger: TIssueChatRefreshTrigger) => Promise<void> | void;
  intervalMs?: number;
  scheduleInterval?: (callback: () => void, intervalMs: number) => ReturnType<typeof setInterval>;
  cancelInterval?: (handle: ReturnType<typeof setInterval>) => void;
};

export const createIssueChatRefreshScheduler = (
  options: TIssueChatRefreshSchedulerOptions
): TIssueChatRefreshScheduler => {
  const intervalMs = options.intervalMs ?? ISSUE_CHAT_REFRESH_INTERVAL_MS;
  const scheduleInterval = options.scheduleInterval ?? setInterval;
  const cancelInterval = options.cancelInterval ?? clearInterval;
  let intervalHandle: ReturnType<typeof setInterval> | undefined;
  let active = false;
  let inFlight: Promise<void> | undefined;

  const request = (trigger: TIssueChatRefreshTrigger = "interval"): Promise<void> => {
    if (inFlight) return inFlight;

    let requestPromise: Promise<void>;
    requestPromise = Promise.resolve()
      .then(() => options.refresh(trigger))
      .then(() => undefined)
      .catch(() => undefined)
      .finally(() => {
        if (inFlight === requestPromise) inFlight = undefined;
      });
    inFlight = requestPromise;
    return requestPromise;
  };

  const start = () => {
    if (active) return;
    active = true;
    intervalHandle = scheduleInterval(() => {
      if (active) void request("interval");
    }, intervalMs);
  };

  const stop = () => {
    active = false;
    if (intervalHandle === undefined) return;
    cancelInterval(intervalHandle);
    intervalHandle = undefined;
  };

  return { start, stop, request };
};
