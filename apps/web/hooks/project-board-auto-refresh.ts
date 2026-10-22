/**
 * Copyright (c) 2023-present Plane Software, Inc. and contributors
 * SPDX-License-Identifier: AGPL-3.0-only
 * See the LICENSE file for details.
 */

import { cloneDeep } from "lodash-es";
import type { IssuePaginationOptions, TIssue, TIssues } from "@plane/types";

export const DEFAULT_PROJECT_BOARD_REFRESH_INTERVAL_MS = 15_000;
export const MIN_PROJECT_BOARD_REFRESH_INTERVAL_MS = 5_000;
export const MAX_PROJECT_BOARD_REFRESH_INTERVAL_MS = 300_000;
export const PROJECT_BOARD_DRAG_QUIET_MS = 1_000;
export const PROJECT_BOARD_REFRESH_ERROR_LOG_INTERVAL_MS = 60_000;
export const MAX_PROJECT_BOARD_REFRESH_PAGES = 25;
export const MAX_PROJECT_BOARD_NEW_MATCHES_PER_REFRESH = 50;

export type TProjectBoardRefreshResult =
  | { applied: true; nextUpdatedAfter: string; deferredNewIssueCount: number }
  | { applied: false; reason: "blocked" | "stale" | "unavailable" };

export interface TProjectBoardRefreshSnapshot {
  workspaceSlug: string | undefined;
  projectId: string | undefined;
  filterFingerprint: string;
  paginationFingerprint: string;
  loadedIssueIdsFingerprint: string;
  issueRevisionsFingerprint: string;
  loaderFingerprint: string;
}

export interface TProjectBoardRefreshUpdate {
  action: "add" | "update" | "remove";
  issueId: string;
  next?: TIssue;
  previous?: TIssue;
}

export interface TProjectBoardRefreshPlan {
  issues: TIssue[];
  updates: TProjectBoardRefreshUpdate[];
  deferredNewIssueCount: number;
}

export interface TProjectBoardRefreshApplyHandlers {
  addIssues: (issues: TIssue[]) => void;
  addIssue: (issueId: string) => void;
  updateIssue: (issueId: string, next: TIssue, previous: TIssue) => void;
  removeIssue: (issueId: string, previous: TIssue) => void;
}

export interface TProjectBoardRefreshTimerApi {
  setTimeout: (handler: () => void, timeoutMs: number) => unknown;
  clearTimeout: (handle: unknown) => void;
}

export interface TProjectBoardRefreshSchedulerOptions {
  intervalMs: number;
  task: () => Promise<void> | void;
  shouldRun: () => boolean;
  onError?: (error: unknown) => void;
  timerApi?: TProjectBoardRefreshTimerApi;
}

const defaultTimerApi: TProjectBoardRefreshTimerApi = {
  setTimeout: (handler, timeoutMs) => setTimeout(handler, timeoutMs),
  clearTimeout: (handle) => clearTimeout(handle as ReturnType<typeof setTimeout>),
};
const TIMESTAMP_PARTS_RE = /^(\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2})(?:\.(\d{1,9}))?(Z|[+-]\d{2}:\d{2})$/;

export function resolveProjectBoardRefreshInterval(rawValue: string | undefined): number {
  if (rawValue === undefined || rawValue.trim() === "") return DEFAULT_PROJECT_BOARD_REFRESH_INTERVAL_MS;

  const parsed = Number(rawValue);
  if (!Number.isFinite(parsed)) return DEFAULT_PROJECT_BOARD_REFRESH_INTERVAL_MS;

  return Math.min(
    MAX_PROJECT_BOARD_REFRESH_INTERVAL_MS,
    Math.max(MIN_PROJECT_BOARD_REFRESH_INTERVAL_MS, Math.round(parsed))
  );
}

export function isProjectBoardRefreshWindowActive(isOnline: boolean, isVisible: boolean): boolean {
  return isOnline && isVisible;
}

export function hasActiveProjectBoardLoader(loader: Record<string, unknown>): boolean {
  return Object.values(loader).some(Boolean);
}

export function resolveProjectBoardPaginationCursor(
  groupCursor: string | undefined,
  rootCursor: string | undefined,
  computedCursor: string | undefined
): string | undefined {
  return groupCursor || rootCursor || computedCursor;
}

export function resolveProjectBoardRefreshWatermark(
  previousWatermark: string,
  requestStartedAt: string,
  deferredNewIssueCount: number
): string {
  return deferredNewIssueCount > 0 ? previousWatermark : requestStartedAt;
}

function stableSerialize(value: unknown): string {
  if (Array.isArray(value)) return `[${value.map(stableSerialize).join(",")}]`;
  if (value && typeof value === "object") {
    const record = value as Record<string, unknown>;
    // oxlint-disable-next-line unicorn/no-array-sort
    const keys = Object.keys(record).sort();
    return `{${keys.map((key) => `${JSON.stringify(key)}:${stableSerialize(record[key])}`).join(",")}}`;
  }
  return JSON.stringify(value) ?? "null";
}

function timestampMicros(value: string | undefined): bigint | null {
  if (!value) return null;
  const match = TIMESTAMP_PARTS_RE.exec(value);
  if (!match) return null;
  const wholeMilliseconds = Date.parse(`${match[1]}${match[3]}`);
  if (!Number.isFinite(wholeMilliseconds)) return null;
  const fraction = (match[2] ?? "").padEnd(6, "0").slice(0, 6);
  return BigInt(wholeMilliseconds) * 1_000n + BigInt(fraction || "0");
}

export function collectProjectIssueIds(groupedIssueIds: TIssues | undefined): string[] {
  if (!groupedIssueIds) return [];

  const issueIds = new Set<string>();
  const visit = (value: unknown) => {
    if (Array.isArray(value)) {
      value.forEach((issueId) => {
        if (typeof issueId === "string") issueIds.add(issueId);
      });
      return;
    }
    if (value && typeof value === "object") {
      Object.values(value).forEach(visit);
    }
  };
  visit(groupedIssueIds);
  return [...issueIds];
}

export function createProjectBoardRefreshSnapshot(input: {
  workspaceSlug: string | undefined;
  projectId: string | undefined;
  filterParams: unknown;
  paginationOptions: IssuePaginationOptions | undefined;
  loadedIssueIds: string[];
  issues: Array<TIssue | undefined>;
  loader: unknown;
}): TProjectBoardRefreshSnapshot {
  const issueRevisions: Array<[string, string]> = input.issues
    .filter((issue): issue is TIssue => Boolean(issue?.id))
    .map((issue) => [issue.id, issue.updated_at]);
  // oxlint-disable-next-line unicorn/no-array-sort
  issueRevisions.sort(([left], [right]) => left.localeCompare(right));

  return {
    workspaceSlug: input.workspaceSlug,
    projectId: input.projectId,
    filterFingerprint: stableSerialize(input.filterParams),
    paginationFingerprint: stableSerialize(input.paginationOptions),
    // oxlint-disable-next-line unicorn/no-array-sort
    loadedIssueIdsFingerprint: stableSerialize([...input.loadedIssueIds].sort()),
    issueRevisionsFingerprint: stableSerialize(issueRevisions),
    loaderFingerprint: stableSerialize(input.loader),
  };
}

export function projectBoardRefreshSnapshotMatches(
  expected: TProjectBoardRefreshSnapshot,
  current: TProjectBoardRefreshSnapshot
): boolean {
  return (
    expected.workspaceSlug === current.workspaceSlug &&
    expected.projectId === current.projectId &&
    expected.filterFingerprint === current.filterFingerprint &&
    expected.paginationFingerprint === current.paginationFingerprint &&
    expected.loadedIssueIdsFingerprint === current.loadedIssueIdsFingerprint &&
    expected.issueRevisionsFingerprint === current.issueRevisionsFingerprint &&
    expected.loaderFingerprint === current.loaderFingerprint
  );
}

export function buildProjectBoardRefreshPlan(input: {
  loadedIssueIds: string[];
  loadedIssues: TIssue[];
  deltaIssues: TIssue[];
  previousIssues: Record<string, TIssue | undefined>;
  updatedAfter: string;
  deltaStartedAt: string;
  maxNewIssues?: number;
}): TProjectBoardRefreshPlan {
  const loadedSet = new Set(input.loadedIssueIds);
  const loadedById = new Map(input.loadedIssues.map((issue) => [issue.id, issue]));
  const deltaById = new Map(input.deltaIssues.map((issue) => [issue.id, issue]));
  const nextById = new Map<string, TIssue>();
  const previousIssues = Object.fromEntries(
    Object.entries(input.previousIssues).map(([issueId, issue]) => [issueId, cloneDeep(issue)])
  );

  for (const [issueId, issue] of deltaById) nextById.set(issueId, issue);
  for (const [issueId, issue] of loadedById) {
    nextById.set(issueId, { ...previousIssues[issueId], ...deltaById.get(issueId), ...issue });
  }

  const updates: TProjectBoardRefreshUpdate[] = [];
  const removedIssueIds = new Set<string>();
  const includedIssueIds = new Set<string>();
  const watermarkTime = timestampMicros(input.updatedAfter);
  const deltaStartedTime = timestampMicros(input.deltaStartedAt);
  const maxNewIssues = input.maxNewIssues ?? MAX_PROJECT_BOARD_NEW_MATCHES_PER_REFRESH;
  let addedNewIssueCount = 0;
  let deferredNewIssueCount = 0;

  for (const issueId of input.loadedIssueIds) {
    const previous = previousIssues[issueId];
    const next = nextById.get(issueId);
    if (next) includedIssueIds.add(issueId);

    if (!next) {
      if (previous) {
        updates.push({ action: "remove", issueId, previous });
        removedIssueIds.add(issueId);
      }
      continue;
    }

    if (deltaById.has(issueId)) {
      updates.push(previous ? { action: "update", issueId, next, previous } : { action: "add", issueId, next });
      continue;
    }

    const nextUpdatedTime = timestampMicros(next.updated_at);
    const changedSinceWatermark = nextUpdatedTime !== null && watermarkTime !== null && nextUpdatedTime > watermarkTime;
    const changedBeforeDeltaSnapshot =
      nextUpdatedTime !== null && deltaStartedTime !== null && nextUpdatedTime <= deltaStartedTime;
    if (previous && changedSinceWatermark && changedBeforeDeltaSnapshot) {
      updates.push({ action: "remove", issueId, previous });
      removedIssueIds.add(issueId);
      continue;
    }

    updates.push(previous ? { action: "update", issueId, next, previous } : { action: "add", issueId, next });
  }

  for (const [issueId, next] of deltaById) {
    if (loadedSet.has(issueId) || removedIssueIds.has(issueId)) continue;
    const previous = previousIssues[issueId];
    if (!previous) {
      if (addedNewIssueCount >= maxNewIssues) {
        deferredNewIssueCount += 1;
        continue;
      }
      addedNewIssueCount += 1;
    }
    includedIssueIds.add(issueId);
    updates.push(previous ? { action: "update", issueId, next, previous } : { action: "add", issueId, next });
  }

  return {
    issues: [...nextById.values()].filter((issue) => includedIssueIds.has(issue.id)),
    updates,
    deferredNewIssueCount,
  };
}

export function applyProjectBoardRefreshPlan(
  plan: TProjectBoardRefreshPlan,
  handlers: TProjectBoardRefreshApplyHandlers
): void {
  handlers.addIssues(plan.issues);
  for (const update of plan.updates) {
    if (update.action === "remove" && update.previous) {
      handlers.removeIssue(update.issueId, update.previous);
    } else if (update.action === "update" && update.next && update.previous) {
      handlers.updateIssue(update.issueId, update.next, update.previous);
    } else if (update.action === "add") {
      handlers.addIssue(update.issueId);
    }
  }
}

export async function loadProjectBoardRefreshTransaction<TDelta, TLoaded>(loaders: {
  loadDelta: () => Promise<TDelta>;
  loadLoaded: () => Promise<TLoaded>;
}): Promise<{ delta: TDelta; loaded: TLoaded }> {
  const delta = await loaders.loadDelta();
  const loaded = await loaders.loadLoaded();
  return { delta, loaded };
}

export class ProjectBoardRefreshScheduler {
  private readonly intervalMs: number;
  private readonly task: () => Promise<void> | void;
  private readonly shouldRun: () => boolean;
  private readonly onError?: (error: unknown) => void;
  private readonly timerApi: TProjectBoardRefreshTimerApi;
  private timer: unknown = null;
  private running = false;
  private stopped = true;
  private generation = 0;

  constructor(options: TProjectBoardRefreshSchedulerOptions) {
    this.intervalMs = options.intervalMs;
    this.task = options.task;
    this.shouldRun = options.shouldRun;
    this.onError = options.onError;
    this.timerApi = options.timerApi ?? defaultTimerApi;
  }

  start(): void {
    if (!this.stopped) return;
    this.stopped = false;
    this.generation += 1;
    this.schedule(this.intervalMs);
  }

  stop(): void {
    this.stopped = true;
    this.generation += 1;
    this.clearScheduled();
  }

  requestRun(delayMs = 0): void {
    if (this.stopped || this.running) return;
    this.schedule(Math.max(0, delayMs));
  }

  private clearScheduled(): void {
    if (this.timer === null) return;
    this.timerApi.clearTimeout(this.timer);
    this.timer = null;
  }

  private schedule(delayMs: number): void {
    this.clearScheduled();
    const generation = this.generation;
    this.timer = this.timerApi.setTimeout(() => {
      this.timer = null;
      void this.run(generation);
    }, delayMs);
  }

  private async run(generation: number): Promise<void> {
    if (this.stopped || generation !== this.generation || this.running) return;

    try {
      if (!this.shouldRun()) {
        this.schedule(this.intervalMs);
        return;
      }

      this.running = true;
      try {
        await this.task();
      } catch (error) {
        this.onError?.(error);
      } finally {
        this.running = false;
      }

      if (!this.stopped && generation === this.generation) this.schedule(this.intervalMs);
    } catch (error) {
      this.onError?.(error);
      if (!this.stopped && generation === this.generation) this.schedule(this.intervalMs);
    }
  }
}
