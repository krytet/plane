import { spawn, type ChildProcessWithoutNullStreams } from "node:child_process";
import { createInterface } from "node:readline";

import { ChatSyncError } from "./errors.ts";

export const VERIFIED_CODEX_PROTOCOL_VERSION = "0.163.0-alpha.11";

type SpawnLike = typeof spawn;

export interface CodexThreadSummary {
  id: string;
  modelProvider: string;
  status: {
    type: "notLoaded" | "idle" | "systemError" | "active";
    activeFlags?: string[];
  };
}

export interface CodexTurnItem {
  type?: unknown;
  text?: unknown;
  phase?: unknown;
}

export interface CodexTurn {
  id: string;
  status: "completed" | "interrupted" | "failed" | "inProgress";
  startedAt?: number | null;
  completedAt: number | null;
  items: CodexTurnItem[];
}

export interface CodexReader {
  initialize(): Promise<void>;
  readThread(threadId: string): Promise<CodexThreadSummary | null>;
  listTurns(threadId: string): Promise<CodexTurn[]>;
  close(): Promise<void>;
}

interface PendingRequest {
  resolve: (value: unknown) => void;
  reject: (error: Error) => void;
  timer: NodeJS.Timeout;
}

class AppServerRpcError extends Error {
  readonly rpcCode: number;

  constructor(rpcCode: number) {
    super("app_server_rpc_error");
    this.name = "AppServerRpcError";
    this.rpcCode = rpcCode;
  }
}

interface AppServerOptions {
  executable?: string;
  spawnImpl?: SpawnLike;
  requestTimeoutMs?: number;
  closeTimeoutMs?: number;
  turnLimit?: number;
}

const MAX_PROTOCOL_LINE_BYTES = 2_097_152;

function isRecord(value: unknown): value is Record<string, unknown> {
  return typeof value === "object" && value !== null && !Array.isArray(value);
}

function codexChildEnvironment(): NodeJS.ProcessEnv {
  return Object.fromEntries(Object.entries(process.env).filter(([key]) => key.toUpperCase() !== "PLANE_API_KEY"));
}

function detachOwnedProcess(child: ChildProcessWithoutNullStreams): void {
  for (const stream of [child.stdin, child.stdout, child.stderr]) {
    try {
      stream.destroy();
    } catch {
      // Only this adapter's pipe handles are being released.
    }
  }
  try {
    child.unref();
  } catch {
    // The child may already be detached or exited.
  }
}

export function parseCodexVersion(output: string): string {
  const match = /^codex-cli\s+(\S+)\s*$/.exec(output.trim());
  if (!match || match[1] !== VERIFIED_CODEX_PROTOCOL_VERSION) {
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
      env: codexChildEnvironment(),
    });
    let output = "";
    const timer = setTimeout(() => {
      try {
        child.stdout?.destroy();
      } catch {
        // Release only this version-check child's pipe.
      }
      try {
        child.unref();
      } catch {
        // The child may already have exited.
      }
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
  private readonly closeTimeoutMs: number;
  private readonly turnLimit: number;
  private child: ChildProcessWithoutNullStreams | null = null;
  private nextRequestId = 1;
  private pending = new Map<number, PendingRequest>();
  private closed = false;

  constructor(options: AppServerOptions = {}) {
    this.executable = options.executable ?? "codex";
    this.spawnImpl = options.spawnImpl ?? spawn;
    this.requestTimeoutMs = options.requestTimeoutMs ?? 15_000;
    this.closeTimeoutMs = options.closeTimeoutMs ?? 500;
    this.turnLimit = options.turnLimit ?? 1;
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
      if (!isRecord(message.error) || typeof message.error.code !== "number") {
        pending.reject(new ChatSyncError("protocol_incompatible"));
        return;
      }
      pending.reject(new AppServerRpcError(message.error.code));
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
      env: codexChildEnvironment(),
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

  async readThread(threadId: string): Promise<CodexThreadSummary | null> {
    let result: unknown;
    try {
      result = await this.request("thread/read", { threadId });
    } catch (error) {
      // -32600 is generic, so map it to absence only for this exact validated
      // thread/read method and parameter shape.
      if (error instanceof AppServerRpcError && error.rpcCode === -32600) return null;
      if (error instanceof ChatSyncError) throw error;
      throw new ChatSyncError("app_server_failed");
    }

    if (!isRecord(result) || !isRecord(result.thread) || !isRecord(result.readState)) {
      this.failProtocol();
    }
    const thread = result.thread;
    if (
      typeof thread.id !== "string" ||
      thread.id.toLowerCase() !== threadId.toLowerCase() ||
      typeof thread.modelProvider !== "string" ||
      !thread.modelProvider ||
      !isRecord(thread.status) ||
      !["notLoaded", "idle", "systemError", "active"].includes(String(thread.status.type))
    ) {
      this.failProtocol();
    }
    if (
      thread.status.type === "active" &&
      (!Array.isArray(thread.status.activeFlags) ||
        !thread.status.activeFlags.every((flag) => typeof flag === "string"))
    ) {
      this.failProtocol();
    }

    return {
      id: thread.id.toLowerCase(),
      modelProvider: thread.modelProvider,
      status: {
        type: thread.status.type as CodexThreadSummary["status"]["type"],
        ...(thread.status.type === "active" ? { activeFlags: thread.status.activeFlags as string[] } : {}),
      },
    };
  }

  async listTurns(threadId: string): Promise<CodexTurn[]> {
    const result = await this.request("thread/turns/list", {
      threadId,
      limit: this.turnLimit,
      sortDirection: "desc",
      itemsView: "summary",
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
        value.startedAt !== undefined &&
        value.startedAt !== null &&
        (!Number.isInteger(value.startedAt) || (value.startedAt as number) < 0)
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
        startedAt: (value.startedAt as number | null | undefined) ?? null,
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
        detachOwnedProcess(child);
        finish();
      }, this.closeTimeoutMs);
      child.once("close", () => {
        clearTimeout(timer);
        finish();
      });
    });
  }
}
