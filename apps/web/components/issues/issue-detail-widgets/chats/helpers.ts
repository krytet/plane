/**
 * Copyright (c) 2023-present Plane Software, Inc. and contributors
 * SPDX-License-Identifier: AGPL-3.0-only
 * See the LICENSE file for details.
 */

import type { TIssueChatProgress, TIssueChatProvider } from "@plane/types";

const PROVIDER_LABELS: Record<TIssueChatProvider, string> = {
  codex: "Codex",
  chatgpt: "ChatGPT",
};

const EMPTY_PROGRESS: TIssueChatProgress = {
  done: [],
  now: [],
  waiting: [],
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

export const normalizeIssueChatProgress = (
  progress: Partial<TIssueChatProgress> | null | undefined
): TIssueChatProgress => ({
  done: Array.isArray(progress?.done) ? progress.done.filter((item) => item.trim().length > 0) : [],
  now: Array.isArray(progress?.now) ? progress.now.filter((item) => item.trim().length > 0) : [],
  waiting: Array.isArray(progress?.waiting) ? progress.waiting.filter((item) => item.trim().length > 0) : [],
});

export const hasIssueChatProgress = (progress: Partial<TIssueChatProgress> | null | undefined): boolean => {
  const normalized = normalizeIssueChatProgress(progress);
  return normalized.done.length > 0 || normalized.now.length > 0 || normalized.waiting.length > 0;
};

export const formatIssueChatFreshness = (lastSyncedAt: string | null | undefined, now = Date.now()): string => {
  if (!lastSyncedAt) return "Not synced yet";

  const timestamp = Date.parse(lastSyncedAt);
  if (Number.isNaN(timestamp)) return "Sync time unavailable";

  const elapsedSeconds = Math.max(0, Math.floor((now - timestamp) / 1000));
  if (elapsedSeconds < 60) return "Synced just now";

  const units = [
    { seconds: 86_400, label: "day" },
    { seconds: 3_600, label: "hour" },
    { seconds: 60, label: "minute" },
  ] as const;

  for (const unit of units) {
    if (elapsedSeconds >= unit.seconds) {
      const count = Math.floor(elapsedSeconds / unit.seconds);
      return `Synced ${count} ${unit.label}${count === 1 ? "" : "s"} ago`;
    }
  }

  return "Synced just now";
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

export const getEmptyIssueChatProgress = (): TIssueChatProgress => ({ ...EMPTY_PROGRESS });
