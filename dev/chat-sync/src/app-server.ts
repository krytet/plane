import { spawn, type ChildProcessWithoutNullStreams } from "node:child_process";
import { createInterface } from "node:readline";

import { ChatSyncError } from "./errors.ts";

// Keep this null until a live, matching Desktop/app-server protocol has been
// verified and this adapter's tests are updated for that exact schema.
export const VERIFIED_CODEX_PROTOCOL_VERSION: string | null = null;

type SpawnLike = typeof spawn;

export interface CodexThreadSummary {
  id: string;
  modelProvider: string;
}

export interface CodexTurnItem {
  type?: unknown;
  text?: unknown;
  phase?: unknown;
}

export interface CodexTurn {
  id: string;
  status: "completed" | "interrupted" | "failed" | "inProgress";
  completedAt: number | null;
  items: CodexTurnItem[];
}

export interface CodexReader {
  initialize(): Promise<void>;
  findThreads(threadIds: string[]): Promise<Map<string, CodexThreadSummary>>;
  listTurns(threadId: string): Promise<CodexTurn[]>;
  close(): Promise<void>;
}

interface PendingRequest {
  resolve: (value: unknown) => void;
  reject: (error: ChatSyncError) => void;
  timer: NodeJS.Timeout;
}

interface AppServerOptions {
  executable?: string;
  spawnImpl?: SpawnLike;
  requestTimeoutMs?: number;
  threadPageLimit?: number;
  threadPageMax?: number;
  turnLimit?: number;
}

const MAX_PROTOCOL_LINE_BYTES = 2_097_152;

function isRecord(value: unknown): value is Record<string, unknown> {
  return typeof value === "object" && value !== null && !Array.isArray(value);
}

export function parseCodexVersion(output: string): string {
  const match = /^codex-cli\s+(\S+)\s*$/.exec(output.trim());
  if (!match || VERIFIED_CODEX_PROTOCOL_VERSION === null || match[1] !== VERIFIED_CODEX_PROTOCOL_VERSION) {
    throw new ChatSyncError("cli_incompatible");
  }
  return match[1];
}

export async function assertSupportedCodexVersion(
  executable = "codex",
  spawnImpl: SpawnLike = spawn,
  timeoutMs = 10_000
): Promise<string> {
  return await new Promise((resolve, reject) => {
    const child = spawnImpl(executable, ["--version"], {
      shell: false,
      windowsHide: true,
      stdio: ["ignore", "pipe", "ignore"],
    });
    let output = "";
    const timer = setTimeout(() => {
      child.kill();
      reject(new ChatSyncError("cli_incompatible"));
    }, timeoutMs);

    child.stdout.on("data", (chunk: Buffer) => {
      if (output.length < 4_096) output += chunk.toString("utf8");
    });
    child.on("error", () => {
      clearTimeout(timer);
      reject(new ChatSyncError("cli_incompatible"));
    });
    child.on("close", (code) => {
      clearTimeout(timer);
      if (code !== 0) {
        reject(new ChatSyncError("cli_incompatible"));
        return;
      }
      try {
        resolve(parseCodexVersion(output));
      } catch (error) {
        reject(error);
      }
    });
  });
}

export class CodexAppServerClient implements CodexReader {
  private readonly executable: string;
  private readonly spawnImpl: SpawnLike;
  private readonly requestTimeoutMs: number;
  private readonly threadPageLimit: number;
  private readonly threadPageMax: number;
  private readonly turnLimit: number;
  private child: ChildProcessWithoutNullStreams | null = null;
  private nextRequestId = 1;
  private pending = new Map<number, PendingRequest>();
  private closed = false;

  constructor(options: AppServerOptions = {}) {
    this.executable = options.executable ?? "codex";
    this.spawnImpl = options.spawnImpl ?? spawn;
    this.requestTimeoutMs = options.requestTimeoutMs ?? 15_000;
    this.threadPageLimit = options.threadPageLimit ?? 100;
    this.threadPageMax = options.threadPageMax ?? 20;
    this.turnLimit = options.turnLimit ?? 20;
  }

  private failProtocol(): never {
    throw new ChatSyncError("protocol_incompatible");
  }

  private failAll(error: ChatSyncError): void {
    for (const pending of this.pending.values()) {
      clearTimeout(pending.timer);
      pending.reject(error);
    }
    this.pending.clear();
  }

  private send(message: unknown): void {
    if (!this.child || this.closed) throw new ChatSyncError("app_server_failed");
    try {
      this.child.stdin.write(`${JSON.stringify(message)}\n`);
    } catch {
      throw new ChatSyncError("app_server_failed");
    }
  }

  private handleLine(line: string): void {
    if (Buffer.byteLength(line, "utf8") > MAX_PROTOCOL_LINE_BYTES) {
      this.failAll(new ChatSyncError("protocol_incompatible"));
      return;
    }

    let message: unknown;
    try {
      message = JSON.parse(line);
    } catch {
      this.failAll(new ChatSyncError("protocol_incompatible"));
      return;
    }
    if (!isRecord(message)) {
      this.failAll(new ChatSyncError("protocol_incompatible"));
      return;
    }

    if (message.method !== undefined && message.id === undefined) return;

    if (message.method !== undefined && message.id !== undefined) {
      this.send({
        id: message.id,
        error: { code: -32601, message: "chat-sync does not accept server requests" },
      });
      return;
    }

    if (typeof message.id !== "number" || !this.pending.has(message.id)) {
      this.failAll(new ChatSyncError("protocol_incompatible"));
      return;
    }

    const pending = this.pending.get(message.id);
    if (!pending) return;
    this.pending.delete(message.id);
    clearTimeout(pending.timer);

    if (message.error !== undefined) {
      pending.reject(new ChatSyncError("app_server_failed"));
      return;
    }
    if (!("result" in message)) {
      pending.reject(new ChatSyncError("protocol_incompatible"));
      return;
    }
    pending.resolve(message.result);
  }

  private startProcess(): void {
    if (this.child) return;
    const child = this.spawnImpl(this.executable, ["app-server", "--listen", "stdio://"], {
      shell: false,
      windowsHide: true,
      stdio: ["pipe", "pipe", "pipe"],
    });
    this.child = child;
    this.closed = false;

    const lines = createInterface({ input: child.stdout, crlfDelay: Infinity });
    lines.on("line", (line) => this.handleLine(line));
    child.stderr.resume();
    child.on("error", () => this.failAll(new ChatSyncError("app_server_failed")));
    child.on("close", () => {
      this.closed = true;
      this.failAll(new ChatSyncError("app_server_failed"));
    });
  }

  private request(method: string, params: unknown): Promise<unknown> {
    const id = this.nextRequestId;
    this.nextRequestId += 1;
    return new Promise((resolve, reject) => {
      const timer = setTimeout(() => {
        this.pending.delete(id);
        reject(new ChatSyncError("app_server_failed"));
      }, this.requestTimeoutMs);
      this.pending.set(id, { resolve, reject, timer });
      try {
        this.send({ method, id, params });
      } catch (error) {
        clearTimeout(timer);
        this.pending.delete(id);
        reject(error);
      }
    });
  }

  async initialize(): Promise<void> {
    this.startProcess();
    const result = await this.request("initialize", {
      clientInfo: {
        name: "plane_chat_sync",
        title: "Plane Chat Sync",
        version: "0.1.0",
      },
    });
    if (
      !isRecord(result) ||
      typeof result.userAgent !== "string" ||
      typeof result.platformFamily !== "string" ||
      typeof result.platformOs !== "string"
    ) {
      this.failProtocol();
    }
    this.send({ method: "initialized", params: {} });
  }

  async findThreads(threadIds: string[]): Promise<Map<string, CodexThreadSummary>> {
    const remaining = new Set(threadIds);
    const found = new Map<string, CodexThreadSummary>();
    let cursor: string | null = null;

    for (let page = 0; page < this.threadPageMax && remaining.size > 0; page += 1) {
      // Pagination is intentionally sequential because each cursor depends on
      // the previous response.
      // oxlint-disable-next-line no-await-in-loop
      const result = await this.request("thread/list", {
        cursor,
        limit: this.threadPageLimit,
        sortKey: "updated_at",
        sortDirection: "desc",
        sourceKinds: ["cli", "vscode", "subAgent"],
        useStateDbOnly: true,
      });
      if (!isRecord(result) || !Array.isArray(result.data)) this.failProtocol();

      for (const value of result.data) {
        if (
          !isRecord(value) ||
          typeof value.id !== "string" ||
          typeof value.modelProvider !== "string" ||
          !isRecord(value.status)
        ) {
          this.failProtocol();
        }
        if (remaining.delete(value.id)) {
          found.set(value.id, { id: value.id, modelProvider: value.modelProvider });
        }
      }

      cursor = typeof result.nextCursor === "string" && result.nextCursor ? result.nextCursor : null;
      if (!cursor) break;
    }

    return found;
  }

  async listTurns(threadId: string): Promise<CodexTurn[]> {
    const result = await this.request("thread/turns/list", {
      threadId,
      limit: this.turnLimit,
      sortDirection: "desc",
      itemsView: "full",
    });
    if (!isRecord(result) || !Array.isArray(result.data)) this.failProtocol();

    return result.data.map((value) => {
      if (
        !isRecord(value) ||
        typeof value.id !== "string" ||
        !["completed", "interrupted", "failed", "inProgress"].includes(String(value.status)) ||
        !Array.isArray(value.items)
      ) {
        this.failProtocol();
      }
      if (
        value.completedAt !== undefined &&
        value.completedAt !== null &&
        (!Number.isInteger(value.completedAt) || (value.completedAt as number) < 0)
      ) {
        this.failProtocol();
      }
      return {
        id: value.id,
        status: value.status as CodexTurn["status"],
        completedAt: (value.completedAt as number | null | undefined) ?? null,
        items: value.items as CodexTurnItem[],
      };
    });
  }

  async close(): Promise<void> {
    const child = this.child;
    this.child = null;
    this.closed = true;
    this.failAll(new ChatSyncError("app_server_failed"));
    if (!child) return;

    try {
      child.stdin.end();
    } catch {
      // The child may already have exited.
    }
    if (child.exitCode !== null) return;

    await new Promise<void>((resolve) => {
      let settled = false;
      const finish = () => {
        if (settled) return;
        settled = true;
        resolve();
      };
      const timer = setTimeout(() => {
        child.kill();
        finish();
      }, 500);
      child.once("close", () => {
        clearTimeout(timer);
        finish();
      });
    });
  }
}
