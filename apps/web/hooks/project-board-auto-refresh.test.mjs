import assert from "node:assert/strict";
import test from "node:test";

import {
  DEFAULT_PROJECT_BOARD_REFRESH_INTERVAL_MS,
  MAX_PROJECT_BOARD_REFRESH_INTERVAL_MS,
  MIN_PROJECT_BOARD_REFRESH_INTERVAL_MS,
  ProjectBoardRefreshScheduler,
  applyProjectBoardRefreshPlan,
  buildProjectBoardRefreshPlan,
  collectProjectIssueIds,
  createProjectBoardRefreshSnapshot,
  hasActiveProjectBoardLoader,
  isProjectBoardRefreshWindowActive,
  loadProjectBoardRefreshTransaction,
  projectBoardRefreshSnapshotMatches,
  resolveProjectBoardPaginationCursor,
  resolveProjectBoardRefreshWatermark,
  resolveProjectBoardRefreshInterval,
} from "./project-board-auto-refresh.ts";

function issue(id, updatedAt, extra = {}) {
  return {
    id,
    updated_at: updatedAt,
    project_id: "project-1",
    state_id: "todo",
    priority: "none",
    sort_order: 1,
    ...extra,
  };
}

const flushMicrotasks = async () => {
  await Promise.resolve();
  await Promise.resolve();
};

function createFakeTimers() {
  let currentTime = 0;
  let sequence = 0;
  const timers = new Map();

  return {
    timerApi: {
      setTimeout(handler, timeoutMs) {
        sequence += 1;
        timers.set(sequence, { at: currentTime + timeoutMs, handler });
        return sequence;
      },
      clearTimeout(handle) {
        timers.delete(handle);
      },
    },
    async advance(milliseconds) {
      const target = currentTime + milliseconds;
      await flushMicrotasks();
      while (true) {
        const next = [...timers.entries()]
          .filter(([, timer]) => timer.at <= target)
          .toSorted((left, right) => left[1].at - right[1].at || left[0] - right[0])[0];
        if (!next) break;
        const [id, timer] = next;
        currentTime = timer.at;
        timers.delete(id);
        timer.handler();
        // oxlint-disable-next-line no-await-in-loop
        await flushMicrotasks();
      }
      currentTime = target;
      await flushMicrotasks();
    },
  };
}

test("resolves the documented refresh interval with clamps", () => {
  assert.equal(resolveProjectBoardRefreshInterval(undefined), DEFAULT_PROJECT_BOARD_REFRESH_INTERVAL_MS);
  assert.equal(resolveProjectBoardRefreshInterval("not-a-number"), DEFAULT_PROJECT_BOARD_REFRESH_INTERVAL_MS);
  assert.equal(resolveProjectBoardRefreshInterval("1000"), MIN_PROJECT_BOARD_REFRESH_INTERVAL_MS);
  assert.equal(resolveProjectBoardRefreshInterval("900000"), MAX_PROJECT_BOARD_REFRESH_INTERVAL_MS);
  assert.equal(resolveProjectBoardRefreshInterval("30000"), 30_000);
});

test("scheduler never overlaps an in-flight refresh", async () => {
  const clock = createFakeTimers();
  let calls = 0;
  let settle;
  const scheduler = new ProjectBoardRefreshScheduler({
    intervalMs: 15_000,
    shouldRun: () => true,
    timerApi: clock.timerApi,
    task: () => {
      calls += 1;
      if (calls === 1) return new Promise((resolve) => (settle = resolve));
      return undefined;
    },
  });

  scheduler.start();
  await clock.advance(15_000);
  assert.equal(calls, 1);

  await clock.advance(30_000);
  assert.equal(calls, 1);

  settle();
  await clock.advance(15_000);
  assert.equal(calls, 2);
  scheduler.stop();
});

test("scheduler pauses while hidden and resumes immediately when eligible", async () => {
  const clock = createFakeTimers();
  let visible = false;
  let calls = 0;
  const scheduler = new ProjectBoardRefreshScheduler({
    intervalMs: 15_000,
    shouldRun: () => visible,
    timerApi: clock.timerApi,
    task: () => {
      calls += 1;
    },
  });

  scheduler.start();
  await clock.advance(15_000);
  assert.equal(calls, 0);

  visible = true;
  scheduler.requestRun(0);
  await clock.advance(0);
  assert.equal(calls, 1);
  scheduler.stop();
});

test("a visible blurred window remains eligible for refresh", async () => {
  const clock = createFakeTimers();
  let calls = 0;
  const focused = false;
  const scheduler = new ProjectBoardRefreshScheduler({
    intervalMs: 15_000,
    shouldRun: () => isProjectBoardRefreshWindowActive(true, true),
    timerApi: clock.timerApi,
    task: () => {
      calls += 1;
    },
  });

  assert.equal(focused, false);
  scheduler.start();
  await clock.advance(15_000);
  assert.equal(calls, 1);
  scheduler.stop();
});

test("scheduler cleanup prevents later runs", async () => {
  const clock = createFakeTimers();
  let calls = 0;
  const scheduler = new ProjectBoardRefreshScheduler({
    intervalMs: 15_000,
    shouldRun: () => true,
    timerApi: clock.timerApi,
    task: () => {
      calls += 1;
    },
  });

  scheduler.start();
  scheduler.stop();
  await clock.advance(30_000);
  assert.equal(calls, 0);
});

test("scheduler reports errors and keeps the recursive timeout alive", async () => {
  const clock = createFakeTimers();
  let calls = 0;
  const errors = [];
  const scheduler = new ProjectBoardRefreshScheduler({
    intervalMs: 15_000,
    shouldRun: () => true,
    timerApi: clock.timerApi,
    onError: (error) => errors.push(error),
    task: () => {
      calls += 1;
      if (calls === 1) throw new Error("temporary failure");
    },
  });

  scheduler.start();
  await clock.advance(15_000);
  assert.equal(calls, 1);
  assert.equal(errors.length, 1);

  await clock.advance(15_000);
  assert.equal(calls, 2);
  assert.equal(errors.length, 1);
  scheduler.stop();
});

test("drag quiet time defers the next refresh", async () => {
  const clock = createFakeTimers();
  let dragging = true;
  let calls = 0;
  const scheduler = new ProjectBoardRefreshScheduler({
    intervalMs: 15_000,
    shouldRun: () => !dragging,
    timerApi: clock.timerApi,
    task: () => {
      calls += 1;
    },
  });

  scheduler.start();
  await clock.advance(15_000);
  assert.equal(calls, 0);

  dragging = false;
  scheduler.requestRun(1_000);
  await clock.advance(999);
  assert.equal(calls, 0);
  await clock.advance(1);
  assert.equal(calls, 1);
  scheduler.stop();
});

test("collects nested group and subgroup issue ids once", () => {
  assert.deepEqual(
    collectProjectIssueIds({
      all: ["a", "b"],
      state: { todo: ["b", "c"], done: ["d"] },
    }).toSorted(),
    ["a", "b", "c", "d"]
  );
});

test("detects active loaders for grouped issue lists", () => {
  assert.equal(hasActiveProjectBoardLoader({ all: undefined }), false);
  assert.equal(hasActiveProjectBoardLoader({ all: undefined, grouped_state: "pagination" }), true);
});

test("prefers server cursors over the mutable loaded-list length", () => {
  assert.equal(resolveProjectBoardPaginationCursor("50:2:0", "50:1:0", "50:1:0"), "50:2:0");
  assert.equal(resolveProjectBoardPaginationCursor(undefined, "50:1:0", "50:0:0"), "50:1:0");
  assert.equal(resolveProjectBoardPaginationCursor(undefined, undefined, "50:0:0"), "50:0:0");
});

test("keeps the previous watermark while bounded additions drain", () => {
  assert.equal(
    resolveProjectBoardRefreshWatermark("2026-10-22T12:00:00.000000Z", "2026-10-22T12:00:15.000000Z", 2),
    "2026-10-22T12:00:00.000000Z"
  );
  assert.equal(
    resolveProjectBoardRefreshWatermark("2026-10-22T12:00:00.000000Z", "2026-10-22T12:00:15.000000Z", 0),
    "2026-10-22T12:00:15.000000Z"
  );
});

test("reconciliation preserves pagination and applies update, removal, and addition", () => {
  const paginationOptions = { canGroup: true, perPageCount: 20, groupedBy: "state" };
  const issuePaginationData = { state: { nextCursor: "20:1:0", prevCursor: "", nextPageResults: true } };
  const previous = {
    a: issue("a", "2026-10-22T12:00:00.000Z", {
      chat_presence: { count: 2, attention: 0, unread: 1, manual_attention: 0 },
    }),
    b: issue("b", "2026-10-22T12:00:00.123000Z"),
  };
  const board = {
    issueIds: ["a", "b"],
    issues: new Map(Object.entries(previous)),
    paginationOptions,
    issuePaginationData,
  };
  const plan = buildProjectBoardRefreshPlan({
    loadedIssueIds: ["a", "b"],
    loadedIssues: [
      issue("a", "2026-10-22T12:01:00.000Z", { state_id: "done" }),
      issue("b", "2026-10-22T12:00:00.123500Z", { state_id: "done" }),
    ],
    deltaIssues: [issue("a", "2026-10-22T12:01:00.000Z", { state_id: "done" }), issue("d", "2026-10-22T12:01:00.000Z")],
    previousIssues: previous,
    updatedAfter: "2026-10-22T12:00:00.123000Z",
    deltaStartedAt: "2026-10-22T12:00:00.124000Z",
  });

  applyProjectBoardRefreshPlan(plan, {
    addIssues: (issues) =>
      issues.forEach((value) => {
        const liveIssue = board.issues.get(value.id);
        if (liveIssue) Object.assign(liveIssue, value);
        else board.issues.set(value.id, value);
      }),
    addIssue: (issueId) => board.issueIds.push(issueId),
    updateIssue: (issueId, next, before) => {
      assert.equal(before.state_id, "todo");
      board.issues.set(issueId, next);
    },
    removeIssue: (issueId) => {
      board.issueIds = board.issueIds.filter((value) => value !== issueId);
    },
  });

  assert.deepEqual(board.issueIds, ["a", "d"]);
  assert.equal(board.issues.get("a").state_id, "done");
  assert.equal(plan.issues.find((value) => value.id === "a").chat_presence.count, 2);
  assert.equal(board.issues.get("d").id, "d");
  assert.equal(board.paginationOptions, paginationOptions);
  assert.equal(board.issuePaginationData, issuePaginationData);
});

test("bounds unloaded matching additions without truncating loaded-card reconciliation", () => {
  const previous = { a: issue("a", "2026-10-22T12:00:00.000Z") };
  const plan = buildProjectBoardRefreshPlan({
    loadedIssueIds: ["a"],
    loadedIssues: [issue("a", "2026-10-22T12:00:00.000Z")],
    deltaIssues: [
      issue("a", "2026-10-22T12:00:01.000Z"),
      issue("b", "2026-10-22T12:00:01.000Z"),
      issue("c", "2026-10-22T12:00:01.000Z"),
      issue("d", "2026-10-22T12:00:01.000Z"),
    ],
    previousIssues: previous,
    updatedAfter: "2026-10-22T12:00:00.000Z",
    deltaStartedAt: "2026-10-22T12:00:02.000Z",
    maxNewIssues: 2,
  });

  assert.deepEqual(
    plan.updates.map((update) => `${update.action}:${update.issueId}`),
    ["update:a", "add:b", "add:c"]
  );
  assert.equal(plan.deferredNewIssueCount, 1);
  assert.deepEqual(plan.issues.map((value) => value.id).toSorted(), ["a", "b", "c"]);
});

test("removes a filtered-out loaded issue updated later in the same millisecond", () => {
  const previous = issue("same-ms", "2026-10-22T12:00:00.123000Z");
  const plan = buildProjectBoardRefreshPlan({
    loadedIssueIds: [previous.id],
    loadedIssues: [issue("same-ms", "2026-10-22T12:00:00.123500Z", { state_id: "done" })],
    deltaIssues: [],
    previousIssues: { "same-ms": previous },
    updatedAfter: "2026-10-22T12:00:00.123000Z",
    deltaStartedAt: "2026-10-22T12:00:00.124000Z",
  });

  assert.deepEqual(
    plan.updates.map((update) => update.action),
    ["remove"]
  );
});

test("a failed request prevents any board changes", async () => {
  let applied = false;
  await assert.rejects(async () => {
    const transaction = await loadProjectBoardRefreshTransaction({
      loadDelta: async () => [issue("a", "2026-10-22T12:01:00.000Z")],
      loadLoaded: async () => {
        throw new Error("loaded-card refresh failed");
      },
    });
    if (transaction) applied = true;
  });
  assert.equal(applied, false);
});

test("stale filter, project, or issue revisions invalidate an in-flight refresh", () => {
  const build = (overrides = {}) =>
    createProjectBoardRefreshSnapshot({
      workspaceSlug: "workspace",
      projectId: "project-1",
      filterParams: { state_id__in: ["todo"] },
      paginationOptions: { canGroup: true, perPageCount: 20 },
      loadedIssueIds: ["a"],
      issues: [issue("a", "2026-10-22T12:00:00.000Z")],
      loader: {},
      ...overrides,
    });
  const expected = build();

  assert.equal(projectBoardRefreshSnapshotMatches(expected, build()), true);
  assert.equal(
    projectBoardRefreshSnapshotMatches(expected, build({ filterParams: { state_id__in: ["done"] } })),
    false
  );
  assert.equal(projectBoardRefreshSnapshotMatches(expected, build({ projectId: "project-2" })), false);
  assert.equal(
    projectBoardRefreshSnapshotMatches(expected, build({ issues: [issue("a", "2026-10-22T12:00:01.000Z")] })),
    false
  );
});
