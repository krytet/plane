import { constants } from "node:fs";
import { mkdir, open, opendir, rename, unlink } from "node:fs/promises";
import { basename, join, resolve } from "node:path";

const UUID_RE = /^[0-9a-f]{8}-[0-9a-f]{4}-[1-8][0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$/;
const SPOOL_FILE_RE = /^([0-9a-f]{8}-[0-9a-f]{4}-[1-8][0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12})\.json$/;
const TIMESTAMP_RE = /^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}\.\d{6}Z$/;
const TIMESTAMP_PARTS_RE = /^(\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2})(?:\.(\d{1,9}))?(Z|[+-]\d{2}:\d{2})$/;
const DEFAULT_MAX_BATCH = 100;
const DEFAULT_MAX_SCAN = 10_000;
const DEFAULT_MAX_INVALID = 100;
const DEFAULT_MAX_UNKNOWN = 100;
const DEFAULT_MAX_BYTES = 16 * 1024;
const DEFAULT_FUTURE_SKEW_MS = 5 * 60 * 1_000;
const MAX_TERMINAL_TURNS = 16;
export type HookEventState = "running" | "needs_action" | "answer_available";
export type HookEventName = "UserPromptSubmit" | "PermissionRequest" | "PostToolUse" | "Stop" | "Interrupt";

const EVENT_STATES: Record<HookEventName, HookEventState> = {
  UserPromptSubmit: "running",
  PermissionRequest: "needs_action",
  PostToolUse: "running",
  Stop: "answer_available",
  Interrupt: "needs_action",
};
const TERMINAL_EVENTS = new Set<HookEventName>(["Interrupt"]);
const EVENT_KEYS = new Set([
  "version",
  "id",
  "source",
  "event",
  "state",
  "terminal",
  "thread_id",
  "turn_id",
  "observed_at",
]);

export interface HookSpoolEvent {
  version: 1;
  id: string;
  source: "codex_hook";
  event: HookEventName;
  state: HookEventState;
  terminal: boolean;
  thread_id: string;
  turn_id: string;
  observed_at: string;
}

export interface InvalidHookSpoolFile {
  fileName: string;
  code: string;
}

export interface HookSpoolBatch {
  events: HookSpoolEvent[];
  invalid: InvalidHookSpoolFile[];
}

interface HookSpoolReaderOptions {
  maxBatch?: number;
  maxScan?: number;
  maxInvalid?: number;
  maxUnknown?: number;
  maxBytes?: number;
  futureSkewMs?: number;
}

function isRecord(value: unknown): value is Record<string, unknown> {
  return typeof value === "object" && value !== null && !Array.isArray(value);
}

function timestamp(value: unknown): number | null {
  if (typeof value !== "string" || !TIMESTAMP_RE.test(value)) return null;
  return timestampMicros(value) === null ? null : Date.parse(value);
}

export function timestampMicros(value: string | null | undefined): bigint | null {
  if (!value) return null;
  const match = TIMESTAMP_PARTS_RE.exec(value);
  if (!match) return null;
  const wholeMilliseconds = Date.parse(`${match[1]}${match[3]}`);
  if (!Number.isFinite(wholeMilliseconds)) return null;
  const fraction = (match[2] ?? "").padEnd(6, "0").slice(0, 6);
  return BigInt(wholeMilliseconds) * 1_000n + BigInt(fraction || "0");
}

function parseEvent(fileName: string, value: unknown, now: number, futureSkewMs: number): HookSpoolEvent | string {
  const match = SPOOL_FILE_RE.exec(fileName);
  if (!match) return "invalid_file_name";
  if (!isRecord(value) || Object.keys(value).length !== EVENT_KEYS.size) return "invalid_shape";
  for (const key of Object.keys(value)) {
    if (!EVENT_KEYS.has(key)) return "invalid_shape";
  }

  const id = value.id;
  const threadId = value.thread_id;
  const turnId = value.turn_id;
  const event = value.event;
  const state = value.state;
  const observedAt = timestamp(value.observed_at);
  if (
    value.version !== 1 ||
    value.source !== "codex_hook" ||
    typeof id !== "string" ||
    !UUID_RE.test(id) ||
    id !== match[1] ||
    typeof threadId !== "string" ||
    !UUID_RE.test(threadId) ||
    typeof turnId !== "string" ||
    !UUID_RE.test(turnId) ||
    typeof event !== "string" ||
    !Object.hasOwn(EVENT_STATES, event) ||
    typeof state !== "string" ||
    !Object.hasOwn(EVENT_STATES, event) ||
    state !== EVENT_STATES[event as HookEventName] ||
    value.terminal !== TERMINAL_EVENTS.has(event as HookEventName) ||
    observedAt === null
  ) {
    return "invalid_event";
  }
  if (observedAt > now + futureSkewMs) return "future_timestamp";

  return {
    version: 1,
    id,
    source: "codex_hook",
    event: event as HookEventName,
    state: state as HookEventState,
    terminal: value.terminal as boolean,
    thread_id: threadId,
    turn_id: turnId,
    observed_at: value.observed_at as string,
  };
}

function safeFileName(value: string): boolean {
  return SPOOL_FILE_RE.test(value) && basename(value) === value;
}

function compareHookEvents(left: HookSpoolEvent, right: HookSpoolEvent): number {
  const leftTime = timestampMicros(left.observed_at) ?? 0n;
  const rightTime = timestampMicros(right.observed_at) ?? 0n;
  if (leftTime === rightTime) return left.id.localeCompare(right.id);
  return leftTime < rightTime ? -1 : 1;
}

export class HookSpoolReader {
  readonly directory: string;
  private readonly maxBatch: number;
  private readonly maxScan: number;
  private readonly maxInvalid: number;
  private readonly maxUnknown: number;
  private readonly maxBytes: number;
  private readonly futureSkewMs: number;

  constructor(directory: string, options: HookSpoolReaderOptions = {}) {
    this.directory = resolve(directory);
    this.maxBatch = options.maxBatch ?? DEFAULT_MAX_BATCH;
    this.maxScan = options.maxScan ?? DEFAULT_MAX_SCAN;
    this.maxInvalid = options.maxInvalid ?? DEFAULT_MAX_INVALID;
    this.maxUnknown = options.maxUnknown ?? DEFAULT_MAX_UNKNOWN;
    this.maxBytes = options.maxBytes ?? DEFAULT_MAX_BYTES;
    this.futureSkewMs = options.futureSkewMs ?? DEFAULT_FUTURE_SKEW_MS;
  }

  async readBatch(now = Date.now(), preferredThreadIds?: ReadonlySet<string>): Promise<HookSpoolBatch> {
    let directory;
    try {
      directory = await opendir(this.directory);
    } catch (error) {
      if ((error as NodeJS.ErrnoException).code === "ENOENT") {
        return { events: [], invalid: [] };
      }
      throw error;
    }

    const preferredEvents: HookSpoolEvent[] = [];
    const otherEvents: HookSpoolEvent[] = [];
    const invalid: InvalidHookSpoolFile[] = [];
    let scanned = 0;
    for await (const entry of directory) {
      if (scanned >= this.maxScan) break;
      scanned += 1;
      if (!entry.isFile() && !entry.isSymbolicLink()) continue;
      if (!safeFileName(entry.name)) continue;
      if (entry.isSymbolicLink()) {
        if (invalid.length >= this.maxInvalid) continue;
        invalid.push({ fileName: entry.name, code: "not_regular_file" });
        continue;
      }

      const path = join(this.directory, entry.name);
      let handle;
      try {
        const noFollow = typeof constants.O_NOFOLLOW === "number" ? constants.O_NOFOLLOW : 0;
        handle = await open(path, constants.O_RDONLY | noFollow);
        const stat = await handle.stat();
        if (!stat.isFile() || stat.size > this.maxBytes) {
          if (invalid.length < this.maxInvalid) {
            invalid.push({
              fileName: entry.name,
              code: stat.isFile() ? "oversize" : "not_regular_file",
            });
          }
          continue;
        }
        const parsed: unknown = JSON.parse((await handle.readFile()).toString("utf8"));
        const event = parseEvent(entry.name, parsed, now, this.futureSkewMs);
        if (typeof event === "string") {
          if (invalid.length < this.maxInvalid) invalid.push({ fileName: entry.name, code: event });
        } else if (!preferredThreadIds || preferredThreadIds.has(event.thread_id)) {
          preferredEvents.push(event);
        } else {
          otherEvents.push(event);
        }
      } catch {
        if (invalid.length < this.maxInvalid) invalid.push({ fileName: entry.name, code: "unreadable" });
      } finally {
        await handle?.close();
      }
    }

    preferredEvents.sort(compareHookEvents);
    otherEvents.sort(compareHookEvents);
    const selected = preferredEvents.slice(0, this.maxBatch);
    if (!preferredThreadIds || selected.length < this.maxBatch) {
      const unknownLimit = preferredThreadIds ? this.maxUnknown : this.maxBatch;
      selected.push(...otherEvents.slice(0, Math.min(unknownLimit, this.maxBatch - selected.length)));
    }
    return { events: selected, invalid };
  }

  private async removeFile(fileName: string): Promise<void> {
    if (!safeFileName(fileName)) return;
    try {
      await unlink(join(this.directory, fileName));
    } catch (error) {
      if ((error as NodeJS.ErrnoException).code !== "ENOENT") throw error;
    }
  }

  async acknowledge(eventId: string): Promise<void> {
    await this.removeFile(`${eventId}.json`);
  }

  async reject(fileName: string): Promise<void> {
    await this.removeFile(fileName);
  }

  async quarantine(eventId: string, code: string): Promise<void> {
    if (!safeFileName(`${eventId}.json`)) return;
    const source = join(this.directory, `${eventId}.json`);
    const quarantineDirectory = join(this.directory, ".quarantine");
    const safeCode = code.replace(/[^a-z0-9_-]/gi, "_").slice(0, 64) || "rejected";
    await mkdir(quarantineDirectory, { recursive: true, mode: 0o700 });
    try {
      await rename(source, join(quarantineDirectory, `${eventId}.${safeCode}.${Date.now()}.json`));
    } catch (error) {
      if ((error as NodeJS.ErrnoException).code !== "ENOENT") throw error;
    }
  }
}

export interface HookOrderingState {
  lastObservedAt?: string;
  lastEventId?: string;
  activeTurnId?: string;
  activeTurnStartedAt?: string;
  terminalTurnIds?: string[];
  softStoppedTurnIds?: string[];
  // Legacy state from the first hook-spool draft.
  terminalTurnId?: string;
}

export interface HookOrderingResult {
  selected: HookSpoolEvent | null;
  activeTurnId?: string;
  activeTurnStartedAt?: string;
  terminalTurnIds: string[];
  softStoppedTurnIds: string[];
  resumeAfterStop: boolean;
  superseded: HookSpoolEvent[];
}

export function selectHookEvent(
  events: HookSpoolEvent[],
  baselineObservedAt: string | null,
  state: HookOrderingState
): HookOrderingResult {
  const baselineValues = [timestampMicros(baselineObservedAt), timestampMicros(state.lastObservedAt)].filter(
    (value): value is bigint => value !== null
  );
  const baseline = baselineValues.reduce((maximum, value) => (value > maximum ? value : maximum), 0n);
  let activeTurnId = state.activeTurnId ?? state.terminalTurnId;
  let activeTurnStartedAt = state.activeTurnStartedAt;
  let activeTurnStart = timestampMicros(activeTurnStartedAt);
  const terminalTurnIds = [...(state.terminalTurnIds ?? [])];
  const softStoppedTurnIds = [...(state.softStoppedTurnIds ?? [])];
  if (state.terminalTurnId && !terminalTurnIds.includes(state.terminalTurnId)) {
    terminalTurnIds.push(state.terminalTurnId);
  }
  if (terminalTurnIds.length > MAX_TERMINAL_TURNS) {
    terminalTurnIds.splice(0, terminalTurnIds.length - MAX_TERMINAL_TURNS);
  }
  let selected: HookSpoolEvent | null = null;
  let resumeProofTurnId: string | null = null;
  const superseded: HookSpoolEvent[] = [];

  for (const event of events.toSorted(compareHookEvents)) {
    let resumeForEvent = false;
    const eventTime = timestampMicros(event.observed_at);
    if (eventTime === null || event.id === state.lastEventId || eventTime <= baseline) {
      superseded.push(event);
      continue;
    }
    if (terminalTurnIds.includes(event.turn_id)) {
      superseded.push(event);
      continue;
    }
    const wasSoftStopped = softStoppedTurnIds.includes(event.turn_id);
    if (event.event === "UserPromptSubmit") {
      if (wasSoftStopped) {
        if (activeTurnId && activeTurnId !== event.turn_id) {
          superseded.push(event);
          continue;
        }
        softStoppedTurnIds.splice(softStoppedTurnIds.indexOf(event.turn_id), 1);
        resumeForEvent = true;
      }
      if (activeTurnId && activeTurnId !== event.turn_id && activeTurnStart !== null && eventTime <= activeTurnStart) {
        superseded.push(event);
        continue;
      }
      if (activeTurnId !== event.turn_id) {
        activeTurnId = event.turn_id;
        activeTurnStartedAt = event.observed_at;
        activeTurnStart = eventTime;
      } else if (activeTurnStart === null) {
        activeTurnStartedAt = event.observed_at;
        activeTurnStart = eventTime;
      }
    } else {
      if (activeTurnId && activeTurnId !== event.turn_id) {
        superseded.push(event);
        continue;
      }
      if (activeTurnStart !== null && eventTime < activeTurnStart) {
        superseded.push(event);
        continue;
      }
      if (!activeTurnId) {
        activeTurnId = event.turn_id;
        activeTurnStartedAt = event.observed_at;
        activeTurnStart = eventTime;
      }
      if (event.event === "PermissionRequest" && wasSoftStopped) {
        superseded.push(event);
        continue;
      }
      if (event.event === "Stop") {
        if (!wasSoftStopped) {
          softStoppedTurnIds.push(event.turn_id);
          if (softStoppedTurnIds.length > MAX_TERMINAL_TURNS) {
            softStoppedTurnIds.splice(0, softStoppedTurnIds.length - MAX_TERMINAL_TURNS);
          }
        }
      } else if (event.event === "PostToolUse" && wasSoftStopped) {
        softStoppedTurnIds.splice(softStoppedTurnIds.indexOf(event.turn_id), 1);
        resumeForEvent = true;
      }
      if (event.terminal) {
        const softIndex = softStoppedTurnIds.indexOf(event.turn_id);
        if (softIndex >= 0) softStoppedTurnIds.splice(softIndex, 1);
        terminalTurnIds.push(event.turn_id);
        if (terminalTurnIds.length > MAX_TERMINAL_TURNS) {
          terminalTurnIds.splice(0, terminalTurnIds.length - MAX_TERMINAL_TURNS);
        }
      }
    }

    if (resumeForEvent) resumeProofTurnId = event.turn_id;
    if (selected) superseded.push(selected);
    selected = event;
  }

  return {
    selected,
    ...(activeTurnId ? { activeTurnId } : {}),
    ...(activeTurnStartedAt ? { activeTurnStartedAt } : {}),
    terminalTurnIds,
    softStoppedTurnIds,
    resumeAfterStop: selected !== null && selected.state === "running" && resumeProofTurnId === selected.turn_id,
    superseded: superseded.toSorted(compareHookEvents),
  };
}
