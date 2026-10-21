import { createHash, randomUUID } from "node:crypto";
import { mkdir, readFile, rename, writeFile } from "node:fs/promises";
import { dirname } from "node:path";

import { normalizeUuid, type ThreadTarget } from "./config.ts";
import { ChatSyncError } from "./errors.ts";
import type { Progress } from "./status-block.ts";

export interface PlanePayload {
  provider: "codex";
  thread_id: string;
  url: string;
  title?: string;
  cursor: number;
  progress: Progress;
}

export interface PendingWrite {
  cursor: number;
  turnId: string;
  payloadHash: string;
  payload: PlanePayload;
}

export interface BindingState {
  cursor: number;
  lastTurnId?: string;
  lastObservedTurnId?: string;
  lastPayloadHash?: string;
  pending?: PendingWrite;
  blocked?: {
    code: string;
    at: string;
  };
}

export interface SyncState {
  version: 1;
  bindings: Record<string, BindingState>;
}

export const MAX_CURSOR = Number.MAX_SAFE_INTEGER;

function isRecord(value: unknown): value is Record<string, unknown> {
  return typeof value === "object" && value !== null && !Array.isArray(value);
}

function assertBindingState(value: unknown): asserts value is BindingState {
  if (!isRecord(value)) throw new ChatSyncError("state_invalid");
  if (!Number.isSafeInteger(value.cursor) || (value.cursor as number) < 0) {
    throw new ChatSyncError("state_invalid");
  }
  if (value.pending !== undefined) {
    if (!isRecord(value.pending) || !isRecord(value.pending.payload)) {
      throw new ChatSyncError("state_invalid");
    }
    if (
      !Number.isSafeInteger(value.pending.cursor) ||
      typeof value.pending.turnId !== "string" ||
      typeof value.pending.payloadHash !== "string"
    ) {
      throw new ChatSyncError("state_invalid");
    }
  }
}

export function bindingKey(target: ThreadTarget): string {
  return [
    target.provider,
    normalizeUuid(target.threadId),
    target.workspaceSlug,
    normalizeUuid(target.projectId),
    normalizeUuid(target.issueId),
  ].join(":");
}

export function getBindingState(state: SyncState, target: ThreadTarget): BindingState {
  return (state.bindings[bindingKey(target)] ??= { cursor: 0 });
}

export function nextCursor(floor: number, completedAt: number): number {
  const timestampCursor = Math.trunc(completedAt);
  if (
    !Number.isSafeInteger(floor) ||
    floor < 0 ||
    floor >= MAX_CURSOR ||
    !Number.isSafeInteger(timestampCursor) ||
    timestampCursor < 1
  ) {
    throw new ChatSyncError("protocol_incompatible");
  }
  const cursor = Math.max(floor + 1, timestampCursor);
  if (!Number.isSafeInteger(cursor) || cursor > MAX_CURSOR) {
    throw new ChatSyncError("protocol_incompatible");
  }
  return cursor;
}

function canonicalPayload(payload: PlanePayload): string {
  return JSON.stringify({
    provider: payload.provider,
    thread_id: payload.thread_id,
    url: payload.url,
    title: payload.title ?? null,
    cursor: payload.cursor,
    progress: {
      done: payload.progress.done,
      now: payload.progress.now,
      waiting: payload.progress.waiting,
    },
  });
}

function canonicalContent(payload: PlanePayload): string {
  return JSON.stringify({
    provider: payload.provider,
    thread_id: payload.thread_id,
    url: payload.url,
    title: payload.title ?? null,
    progress: {
      done: payload.progress.done,
      now: payload.progress.now,
      waiting: payload.progress.waiting,
    },
  });
}

export function payloadHash(payload: PlanePayload): string {
  return createHash("sha256").update(canonicalPayload(payload)).digest("hex");
}

export function payloadContentHash(payload: PlanePayload): string {
  return createHash("sha256").update(canonicalContent(payload)).digest("hex");
}

export class StateStore {
  readonly filePath: string;

  constructor(filePath: string) {
    this.filePath = filePath;
  }

  async load(): Promise<SyncState> {
    let raw: unknown;
    try {
      raw = JSON.parse(await readFile(this.filePath, "utf8"));
    } catch (error) {
      if ((error as NodeJS.ErrnoException).code === "ENOENT") {
        return { version: 1, bindings: {} };
      }
      throw new ChatSyncError("state_invalid");
    }
    if (!isRecord(raw) || raw.version !== 1 || !isRecord(raw.bindings)) {
      throw new ChatSyncError("state_invalid");
    }
    for (const value of Object.values(raw.bindings)) assertBindingState(value);
    return raw as unknown as SyncState;
  }

  async save(state: SyncState): Promise<void> {
    await mkdir(dirname(this.filePath), { recursive: true });
    const temporaryPath = `${this.filePath}.${process.pid}.${randomUUID()}.tmp`;
    await writeFile(temporaryPath, `${JSON.stringify(state, null, 2)}\n`, {
      encoding: "utf8",
      mode: 0o600,
    });
    await rename(temporaryPath, this.filePath);
  }
}
