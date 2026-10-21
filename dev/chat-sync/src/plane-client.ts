import { normalizeUuid, type ThreadTarget } from "./config.ts";
import { ChatSyncError } from "./errors.ts";
import type { PlanePayload } from "./state.ts";
import type { Progress } from "./status-block.ts";

type FetchLike = typeof fetch;

export interface PlaneBinding {
  id: string;
  provider: "codex" | "chatgpt";
  thread_id: string;
  sync_cursor: number;
  progress: Progress;
  link: {
    url: string;
    title: string;
  } | null;
}

export type PlaneWriteResult =
  | { kind: "success"; binding: PlaneBinding }
  | { kind: "rejected"; status: number; code: string | null }
  | { kind: "uncertain"; status: number | null };

interface PlaneClientOptions {
  baseUrl: string;
  apiKey: string;
  fetchImpl?: FetchLike;
  timeoutMs?: number;
}

const MAX_RESPONSE_BYTES = 1_048_576;

function isRecord(value: unknown): value is Record<string, unknown> {
  return typeof value === "object" && value !== null && !Array.isArray(value);
}

function parseProgress(value: unknown): Progress {
  if (!isRecord(value)) throw new ChatSyncError("plane_read_failed");
  const result: Progress = { done: [], now: [], waiting: [] };
  for (const key of ["done", "now", "waiting"] as const) {
    const items = value[key];
    if (!Array.isArray(items) || !items.every((item) => typeof item === "string")) {
      throw new ChatSyncError("plane_read_failed");
    }
    result[key] = items;
  }
  return result;
}

function parseBinding(value: unknown): PlaneBinding {
  if (
    !isRecord(value) ||
    typeof value.id !== "string" ||
    (value.provider !== "codex" && value.provider !== "chatgpt") ||
    typeof value.thread_id !== "string" ||
    !Number.isSafeInteger(value.sync_cursor) ||
    (value.sync_cursor as number) < 1
  ) {
    throw new ChatSyncError("plane_read_failed");
  }

  let link: PlaneBinding["link"] = null;
  if (value.link != null) {
    if (!isRecord(value.link) || typeof value.link.url !== "string" || typeof value.link.title !== "string") {
      throw new ChatSyncError("plane_read_failed");
    }
    link = { url: value.link.url, title: value.link.title };
  }

  return {
    id: value.id,
    provider: value.provider,
    thread_id: normalizeUuid(value.thread_id),
    sync_cursor: value.sync_cursor as number,
    progress: parseProgress(value.progress),
    link,
  };
}

function joinPath(basePath: string, path: string): string {
  const prefix = basePath.replace(/\/+$/, "");
  return `${prefix}${path}`;
}

export class PlaneClient {
  private readonly baseUrl: URL;
  private readonly apiKey: string;
  private readonly fetchImpl: FetchLike;
  private readonly timeoutMs: number;

  constructor(options: PlaneClientOptions) {
    this.baseUrl = new URL(options.baseUrl);
    this.apiKey = options.apiKey;
    this.fetchImpl = options.fetchImpl ?? fetch;
    this.timeoutMs = options.timeoutMs ?? 15_000;
  }

  private bindingUrl(target: ThreadTarget): string {
    const path = joinPath(
      this.baseUrl.pathname,
      `/api/v1/workspaces/${encodeURIComponent(target.workspaceSlug)}` +
        `/projects/${encodeURIComponent(normalizeUuid(target.projectId))}` +
        `/work-items/${encodeURIComponent(normalizeUuid(target.issueId))}/chat-bindings/`
    );
    return new URL(path, this.baseUrl.origin).toString();
  }

  private async request(url: string, init: RequestInit): Promise<Response> {
    const controller = new AbortController();
    const timer = setTimeout(() => controller.abort(), this.timeoutMs);
    try {
      return await this.fetchImpl(url, {
        ...init,
        redirect: "error",
        signal: controller.signal,
        headers: {
          Accept: "application/json",
          "Content-Type": "application/json",
          "X-Api-Key": this.apiKey,
          ...init.headers,
        },
      });
    } finally {
      clearTimeout(timer);
    }
  }

  private async responseText(response: Response): Promise<string> {
    const declaredLength = Number(response.headers.get("content-length") ?? "0");
    if (declaredLength > MAX_RESPONSE_BYTES) {
      throw new Error("response too large");
    }
    const text = await response.text();
    if (Buffer.byteLength(text, "utf8") > MAX_RESPONSE_BYTES) {
      throw new Error("response too large");
    }
    return text;
  }

  async listBindings(target: ThreadTarget): Promise<PlaneBinding[]> {
    let response: Response;
    try {
      response = await this.request(this.bindingUrl(target), { method: "GET" });
    } catch {
      throw new ChatSyncError("plane_read_failed");
    }

    if (response.status === 401 || response.status === 403) {
      throw new ChatSyncError("plane_auth_failed");
    }
    if (!response.ok) throw new ChatSyncError("plane_read_failed");

    try {
      const body: unknown = JSON.parse(await this.responseText(response));
      if (!Array.isArray(body)) throw new Error("invalid body");
      return body.map(parseBinding);
    } catch (error) {
      if (error instanceof ChatSyncError) throw error;
      throw new ChatSyncError("plane_read_failed");
    }
  }

  async syncBinding(target: ThreadTarget, payload: PlanePayload): Promise<PlaneWriteResult> {
    let response: Response;
    try {
      response = await this.request(this.bindingUrl(target), {
        method: "POST",
        body: JSON.stringify(payload),
      });
    } catch {
      return { kind: "uncertain", status: null };
    }

    if (response.status === 200 || response.status === 201) {
      try {
        const body: unknown = JSON.parse(await this.responseText(response));
        if (!isRecord(body) || typeof body.applied !== "boolean") {
          return { kind: "uncertain", status: response.status };
        }
        const binding = parseBinding(body);
        if (
          binding.provider !== payload.provider ||
          binding.thread_id !== normalizeUuid(payload.thread_id) ||
          binding.sync_cursor !== payload.cursor
        ) {
          return { kind: "uncertain", status: response.status };
        }
        return { kind: "success", binding };
      } catch {
        return { kind: "uncertain", status: response.status };
      }
    }

    if (response.status >= 500 || response.status === 408 || response.status === 425) {
      return { kind: "uncertain", status: response.status };
    }

    if (response.status === 401 || response.status === 403) {
      throw new ChatSyncError("plane_auth_failed");
    }

    let code: string | null = null;
    try {
      const body: unknown = JSON.parse(await this.responseText(response));
      if (isRecord(body) && typeof body.code === "string" && /^[a-z_]{1,64}$/.test(body.code)) {
        code = body.code;
      }
    } catch {
      code = null;
    }
    return { kind: "rejected", status: response.status, code };
  }
}
