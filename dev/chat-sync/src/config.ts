import { readFile } from "node:fs/promises";
import { dirname, isAbsolute, resolve } from "node:path";

import { ChatSyncError } from "./errors.ts";

export type ChatProvider = "codex" | "chatgpt";
export type ProgressMode = "status_block" | "structured_or_summary";

export interface ThreadTarget {
  provider: ChatProvider;
  threadId: string;
  workspaceSlug: string;
  projectId: string;
  issueId: string;
  url: string;
  title?: string;
}

export interface AdapterConfig {
  enabled: boolean;
  planeBaseUrl: string;
  stateFile: string;
  hookSpoolDir?: string;
  pollIntervalSeconds: number;
  codexExecutable: string;
  progressMode: ProgressMode;
  threads: ThreadTarget[];
}

const TOP_LEVEL_KEYS = new Set([
  "enabled",
  "planeBaseUrl",
  "stateFile",
  "hookSpoolDir",
  "pollIntervalSeconds",
  "codexExecutable",
  "progressMode",
  "threads",
]);
const THREAD_KEYS = new Set(["provider", "threadId", "workspaceSlug", "projectId", "issueId", "url", "title"]);
const SECRET_KEY = /(?:api[_-]?key|secret|password|passwd|token)/i;
const UUID_RE = /^[0-9a-f]{8}-[0-9a-f]{4}-[1-8][0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$/i;
const UUID_V7_RE = /^[0-9a-f]{8}-[0-9a-f]{4}-7[0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$/i;
const CODEX_NATIVE_URL_RE =
  /^codex:\/\/threads\/([0-9a-f]{8}-[0-9a-f]{4}-7[0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12})$/i;
const WORKSPACE_RE = /^[A-Za-z0-9][A-Za-z0-9_-]*$/;
const PROVIDER_HOSTS = new Set(["chatgpt.com", "www.chatgpt.com", "chat.openai.com", "www.chat.openai.com"]);

function isRecord(value: unknown): value is Record<string, unknown> {
  return typeof value === "object" && value !== null && !Array.isArray(value);
}

export function normalizeUuid(value: string): string {
  return UUID_RE.test(value) ? value.toLowerCase() : value;
}

function invalid(): never {
  throw new ChatSyncError("config_invalid");
}

function assertKnownKeys(record: Record<string, unknown>, allowed: Set<string>): void {
  for (const key of Object.keys(record)) {
    if (SECRET_KEY.test(key)) invalid();
    if (!allowed.has(key)) invalid();
  }
}

function parseBaseUrl(value: string): string {
  let url: URL;
  try {
    url = new URL(value);
  } catch {
    invalid();
  }
  if (url.username || url.password || url.search || url.hash) invalid();
  if (url.protocol !== "https:" && url.protocol !== "http:") invalid();
  const loopback = ["localhost", "127.0.0.1", "::1"].includes(url.hostname);
  if (url.protocol === "http:" && !loopback) invalid();
  return url.toString().replace(/\/+$/, "");
}

export function normalizeProviderUrl(value: string): string {
  let url: URL;
  try {
    url = new URL(value);
  } catch {
    invalid();
  }
  if (url.protocol !== "https:" || url.username || url.password || url.port) invalid();

  const hostname = url.hostname.replace(/\.+$/, "").toLowerCase();
  if (!PROVIDER_HOSTS.has(hostname)) invalid();
  url.hostname = hostname;
  return url.toString();
}

function normalizeCodexNativeUrl(value: string, threadId: string): string {
  const match = CODEX_NATIVE_URL_RE.exec(value.trim());
  if (!match) invalid();
  const normalizedThreadId = normalizeUuid(threadId.trim());
  if (match[1]!.toLowerCase() !== normalizedThreadId) invalid();
  return `codex://threads/${normalizedThreadId}`;
}

function parseThread(value: unknown): ThreadTarget {
  if (!isRecord(value)) invalid();
  assertKnownKeys(value, THREAD_KEYS);

  const provider = value.provider;
  if (provider !== "codex" && provider !== "chatgpt") invalid();

  const threadId = value.threadId;
  if (
    typeof threadId !== "string" ||
    !threadId.trim() ||
    threadId.length > 255 ||
    [...threadId].some((character) => {
      const codePoint = character.codePointAt(0) ?? 0;
      return codePoint < 32 || codePoint === 127;
    })
  ) {
    invalid();
  }
  if (provider === "codex" && !UUID_V7_RE.test(threadId.trim())) invalid();

  const workspaceSlug = value.workspaceSlug;
  if (typeof workspaceSlug !== "string" || !WORKSPACE_RE.test(workspaceSlug)) invalid();

  const projectId = value.projectId;
  if (typeof projectId !== "string" || !UUID_RE.test(projectId)) invalid();

  const issueId = value.issueId;
  if (typeof issueId !== "string" || !UUID_RE.test(issueId)) invalid();

  const rawUrl = value.url;
  if (typeof rawUrl !== "string" || rawUrl.length > 2_048) invalid();
  const url =
    provider === "codex" && rawUrl.trim().toLowerCase().startsWith("codex:")
      ? normalizeCodexNativeUrl(rawUrl, threadId)
      : normalizeProviderUrl(rawUrl);

  const title = value.title;
  if (title !== undefined && (typeof title !== "string" || title.length > 255)) invalid();

  return {
    provider,
    threadId: normalizeUuid(threadId.trim()),
    workspaceSlug,
    projectId: normalizeUuid(projectId),
    issueId: normalizeUuid(issueId),
    url,
    ...(title ? { title } : {}),
  };
}

export async function loadConfig(configPath: string): Promise<AdapterConfig> {
  let raw: unknown;
  try {
    raw = JSON.parse(await readFile(configPath, "utf8"));
  } catch {
    invalid();
  }
  if (!isRecord(raw)) invalid();
  assertKnownKeys(raw, TOP_LEVEL_KEYS);

  if (typeof raw.enabled !== "boolean") invalid();
  if (typeof raw.planeBaseUrl !== "string") invalid();
  const planeBaseUrl = parseBaseUrl(raw.planeBaseUrl);

  const pollIntervalSeconds = raw.pollIntervalSeconds ?? 30;
  if (
    typeof pollIntervalSeconds !== "number" ||
    !Number.isInteger(pollIntervalSeconds) ||
    pollIntervalSeconds < 5 ||
    pollIntervalSeconds > 3_600
  ) {
    invalid();
  }

  const codexExecutable = raw.codexExecutable ?? "codex";
  if (typeof codexExecutable !== "string" || !codexExecutable.trim()) invalid();

  const progressMode = raw.progressMode ?? "status_block";
  if (progressMode !== "status_block" && progressMode !== "structured_or_summary") invalid();

  if (!Array.isArray(raw.threads) || raw.threads.length === 0 || raw.threads.length > 100) {
    invalid();
  }
  const threads = raw.threads.map(parseThread);
  const identities = new Set<string>();
  const threadIdentities = new Set<string>();
  for (const thread of threads) {
    const identity = `${thread.provider}:${thread.threadId}:${thread.workspaceSlug}:${thread.projectId}:${thread.issueId}`;
    if (identities.has(identity)) invalid();
    identities.add(identity);
    const threadIdentity = `${thread.provider}:${thread.workspaceSlug}:${thread.threadId}`;
    if (threadIdentities.has(threadIdentity)) invalid();
    threadIdentities.add(threadIdentity);
  }

  const stateFileValue = raw.stateFile ?? "./.state/cursors.json";
  if (typeof stateFileValue !== "string" || !stateFileValue.trim()) invalid();
  const stateFile = resolve(dirname(configPath), stateFileValue);
  let hookSpoolDir: string | undefined;
  if (raw.hookSpoolDir !== undefined) {
    if (
      typeof raw.hookSpoolDir !== "string" ||
      !raw.hookSpoolDir.trim() ||
      raw.hookSpoolDir.length > 2_048 ||
      !isAbsolute(raw.hookSpoolDir)
    ) {
      invalid();
    }
    hookSpoolDir = resolve(raw.hookSpoolDir);
  }

  return {
    enabled: raw.enabled,
    planeBaseUrl,
    stateFile,
    ...(hookSpoolDir ? { hookSpoolDir } : {}),
    pollIntervalSeconds,
    codexExecutable,
    progressMode,
    threads,
  };
}

export function assertSupportedProviders(config: AdapterConfig): void {
  if (config.threads.some((thread) => thread.provider === "chatgpt")) {
    throw new ChatSyncError(
      "unsupported_provider",
      "ChatGPT web/desktop sync is unsupported; only Codex app-server threads can be read."
    );
  }
}
