/**
 * Copyright (c) 2023-present Plane Software, Inc. and contributors
 * SPDX-License-Identifier: AGPL-3.0-only
 * See the LICENSE file for details.
 */

import { action, makeObservable, runInAction } from "mobx";
// types
import type {
  TIssue,
  TLoader,
  ViewFlags,
  IssuePaginationOptions,
  TIssuesResponse,
  TBulkOperationsPayload,
  TIssueParams,
} from "@plane/types";
// helpers
import {
  MAX_PROJECT_BOARD_REFRESH_PAGES,
  applyProjectBoardRefreshPlan,
  buildProjectBoardRefreshPlan,
  collectProjectIssueIds,
  createProjectBoardRefreshSnapshot,
  loadProjectBoardRefreshTransaction,
  projectBoardRefreshSnapshotMatches,
  resolveProjectBoardPaginationCursor,
  resolveProjectBoardRefreshWatermark,
  type TProjectBoardRefreshResult,
} from "@/hooks/project-board-auto-refresh";
// base class
import type { IBaseIssuesStore } from "../helpers/base-issues.store";
import { BaseIssuesStore, EIssueGroupedAction } from "../helpers/base-issues.store";
// services
import type { IIssueRootStore } from "../root.store";
import type { IProjectIssuesFilter } from "./filter.store";

export interface IProjectIssues extends IBaseIssuesStore {
  viewFlags: ViewFlags;
  paginationOptions: IssuePaginationOptions | undefined;
  // action
  fetchIssues: (
    workspaceSlug: string,
    projectId: string,
    loadType: TLoader,
    option: IssuePaginationOptions
  ) => Promise<TIssuesResponse | undefined>;
  fetchIssuesWithExistingPagination: (
    workspaceSlug: string,
    projectId: string,
    loadType: TLoader
  ) => Promise<TIssuesResponse | undefined>;
  refreshIssuesPreservingLoadedPages: (
    workspaceSlug: string,
    projectId: string,
    updatedAfter: string,
    shouldApply?: () => boolean
  ) => Promise<TProjectBoardRefreshResult>;
  fetchNextIssues: (
    workspaceSlug: string,
    projectId: string,
    groupId?: string,
    subGroupId?: string
  ) => Promise<TIssuesResponse | undefined>;

  createIssue: (workspaceSlug: string, projectId: string, data: Partial<TIssue>) => Promise<TIssue>;
  updateIssue: (workspaceSlug: string, projectId: string, issueId: string, data: Partial<TIssue>) => Promise<void>;
  archiveIssue: (workspaceSlug: string, projectId: string, issueId: string) => Promise<void>;
  quickAddIssue: (workspaceSlug: string, projectId: string, data: TIssue) => Promise<TIssue | undefined>;
  removeBulkIssues: (workspaceSlug: string, projectId: string, issueIds: string[]) => Promise<void>;
  archiveBulkIssues: (workspaceSlug: string, projectId: string, issueIds: string[]) => Promise<void>;
  bulkUpdateProperties: (workspaceSlug: string, projectId: string, data: TBulkOperationsPayload) => Promise<void>;
}

export class ProjectIssues extends BaseIssuesStore implements IProjectIssues {
  viewFlags = {
    enableQuickAdd: true,
    enableIssueCreation: true,
    enableInlineEditing: true,
  };
  router;

  // filter store
  issueFilterStore: IProjectIssuesFilter;

  constructor(_rootStore: IIssueRootStore, issueFilterStore: IProjectIssuesFilter) {
    super(_rootStore, issueFilterStore);
    makeObservable(this, {
      fetchIssues: action,
      fetchNextIssues: action,
      fetchIssuesWithExistingPagination: action,

      quickAddIssue: action,
    });
    // filter store
    this.issueFilterStore = issueFilterStore;
    this.router = _rootStore.rootStore.router;
  }

  /**
   * Fetches the project details
   * @param workspaceSlug
   * @param projectId
   */
  fetchParentStats = async (workspaceSlug: string, projectId?: string) => {
    if (projectId) {
      this.rootIssueStore.rootStore.projectRoot.project.fetchProjectDetails(workspaceSlug, projectId);
    }
  };

  /** */
  updateParentStats = () => {};

  /**
   * This method is called to fetch the first issues of pagination
   * @param workspaceSlug
   * @param projectId
   * @param loadType
   * @param options
   * @returns
   */
  fetchIssues = async (
    workspaceSlug: string,
    projectId: string,
    loadType: TLoader = "init-loader",
    options: IssuePaginationOptions,
    isExistingPaginationOptions: boolean = false
  ) => {
    try {
      // set loader and clear store
      runInAction(() => {
        this.setLoader(loadType);
        this.clear(!isExistingPaginationOptions); // clear while fetching from server.
      });

      // get params from pagination options
      const params = this.issueFilterStore?.getFilterParams(options, projectId, undefined, undefined, undefined);
      // call the fetch issues API with the params
      const response = await this.issueService.getIssues(workspaceSlug, projectId, params, {
        signal: this.controller.signal,
      });

      // after fetching issues, call the base method to process the response further
      this.onfetchIssues(response, options, workspaceSlug, projectId, undefined, !isExistingPaginationOptions);
      return response;
    } catch (error) {
      // set loader to undefined if errored out
      this.setLoader(undefined);
      throw error;
    }
  };

  /**
   * This method is called subsequent pages of pagination
   * if groupId/subgroupId is provided, only that specific group's next page is fetched
   * else all the groups' next page is fetched
   * @param workspaceSlug
   * @param projectId
   * @param groupId
   * @param subGroupId
   * @returns
   */
  fetchNextIssues = async (workspaceSlug: string, projectId: string, groupId?: string, subGroupId?: string) => {
    const cursorObject = this.getPaginationData(groupId, subGroupId);
    const rootCursorObject = this.getPaginationData(undefined, undefined);
    const paginationCursorObject = cursorObject ?? rootCursorObject;
    // if there are no pagination options and the next page results do not exist the return
    if (!this.paginationOptions || (paginationCursorObject && !paginationCursorObject.nextPageResults)) return;
    try {
      // set Loader
      this.setLoader("pagination", groupId, subGroupId);

      // get params from stored pagination options
      const nextCursor = resolveProjectBoardPaginationCursor(
        cursorObject?.nextCursor,
        rootCursorObject?.nextCursor,
        this.getNextCursor(groupId, subGroupId)
      );
      const params = this.issueFilterStore?.getFilterParams(
        this.paginationOptions,
        projectId,
        nextCursor,
        groupId,
        subGroupId
      );
      // call the fetch issues API with the params for next page in issues
      const response = await this.issueService.getIssues(workspaceSlug, projectId, params);

      // after the next page of issues are fetched, call the base method to process the response
      this.onfetchNexIssues(response, groupId, subGroupId);
      return response;
    } catch (error) {
      // set Loader as undefined if errored out
      this.setLoader(undefined, groupId, subGroupId);
      throw error;
    }
  };

  /**
   * This Method exists to fetch the first page of the issues with the existing stored pagination
   * This is useful for refetching when filters, groupBy, orderBy etc changes
   * @param workspaceSlug
   * @param projectId
   * @param loadType
   * @returns
   */
  fetchIssuesWithExistingPagination = async (
    workspaceSlug: string,
    projectId: string,
    loadType: TLoader = "mutation"
  ) => {
    if (!this.paginationOptions) return;
    return await this.fetchIssues(workspaceSlug, projectId, loadType, this.paginationOptions, true);
  };

  /**
   * Refreshes already-loaded board cards without resetting pagination or list state.
   * All requests and state guards complete before any issue-list mutation is applied.
   */
  refreshIssuesPreservingLoadedPages = async (
    workspaceSlug: string,
    projectId: string,
    updatedAfter: string,
    shouldApply?: () => boolean
  ): Promise<TProjectBoardRefreshResult> => {
    const canApply = () => shouldApply?.() ?? true;
    if (!canApply()) return { applied: false, reason: "blocked" };
    if (!this.paginationOptions) return { applied: false, reason: "unavailable" };

    const requestStartedAt = new Date().toISOString();
    const loadedIssueIds = collectProjectIssueIds(this.groupedIssueIds);
    const previousIssues = Object.fromEntries(
      loadedIssueIds.map((issueId) => [issueId, this.rootIssueStore.issues.getIssueById(issueId)])
    );
    const refreshPaginationOptions: IssuePaginationOptions = {
      ...this.paginationOptions,
      groupedBy: undefined,
      subGroupedBy: undefined,
    };
    const getFilterSnapshot = () =>
      this.issueFilterStore.getFilterParams(refreshPaginationOptions, projectId, undefined, undefined, undefined);
    const getSnapshot = () =>
      createProjectBoardRefreshSnapshot({
        workspaceSlug: this.rootIssueStore.workspaceSlug,
        projectId: this.rootIssueStore.projectId,
        filterParams: getFilterSnapshot(),
        paginationOptions: this.paginationOptions,
        loadedIssueIds: collectProjectIssueIds(this.groupedIssueIds),
        issues: loadedIssueIds.map((issueId) => this.rootIssueStore.issues.getIssueById(issueId)),
        loader: this.loader,
      });
    const snapshot = getSnapshot();
    const perPageCount = Math.max(1, refreshPaginationOptions.perPageCount);
    const baseParams = {
      ...getFilterSnapshot(),
      per_page: perPageCount.toString(),
      updated_at__gt: updatedAfter,
    } as Partial<Record<TIssueParams, string | boolean>> & { updated_at__gt: string };
    delete baseParams.group_by;
    delete baseParams.sub_group_by;

    let deltaStartedAt = "";
    const transaction = await loadProjectBoardRefreshTransaction({
      loadDelta: async () => {
        deltaStartedAt = new Date().toISOString();
        const deltaIssues = [];
        let cursor = `${perPageCount}:0:0`;
        let previousCursor: string | undefined;

        for (let page = 0; page < MAX_PROJECT_BOARD_REFRESH_PAGES; page += 1) {
          // oxlint-disable-next-line no-await-in-loop
          const response = await this.issueService.getIssues(
            workspaceSlug,
            projectId,
            { ...baseParams, cursor },
            { signal: this.controller.signal }
          );
          if (!Array.isArray(response.results)) {
            throw new Error("Project board refresh expected an ungrouped issue response");
          }
          deltaIssues.push(...response.results);
          if (!response.next_page_results) return deltaIssues;
          if (!response.next_cursor || response.next_cursor === previousCursor || response.next_cursor === cursor) {
            throw new Error("Project board refresh received an invalid pagination cursor");
          }
          previousCursor = cursor;
          cursor = response.next_cursor;
        }

        throw new Error(
          `Project board refresh exceeded ${MAX_PROJECT_BOARD_REFRESH_PAGES} pagination pages; reload the board to reset the delta window`
        );
      },
      loadLoaded: async () => {
        // Filtered delta pages own membership; this bounded ID refresh detects loaded cards that left the filter
        // and refreshes presence, which may change without advancing Issue.updated_at.
        const batchSize = 50;
        const issueBatches: string[][] = [];
        for (let index = 0; index < loadedIssueIds.length; index += batchSize) {
          issueBatches.push(loadedIssueIds.slice(index, index + batchSize));
        }
        const responses = await Promise.all(
          issueBatches.map((issueIds) => this.issueService.retrieveIssues(workspaceSlug, projectId, issueIds))
        );
        return responses.flat();
      },
    });

    if (!canApply()) return { applied: false, reason: "blocked" };
    if (!projectBoardRefreshSnapshotMatches(snapshot, getSnapshot())) {
      return { applied: false, reason: "stale" };
    }

    const plan = buildProjectBoardRefreshPlan({
      loadedIssueIds,
      loadedIssues: transaction.loaded,
      deltaIssues: transaction.delta,
      previousIssues,
      updatedAfter,
      deltaStartedAt,
    });
    runInAction(() => {
      applyProjectBoardRefreshPlan(plan, {
        addIssues: (nextIssues) => this.rootIssueStore.issues.addIssue(nextIssues),
        addIssue: (issueId) => this.addIssueToList(issueId),
        updateIssue: (_, next, previous) => this.updateIssueList(next, previous),
        removeIssue: (_, previous) => this.updateIssueList(undefined, previous, EIssueGroupedAction.DELETE),
      });
    });

    return {
      applied: true,
      nextUpdatedAfter: resolveProjectBoardRefreshWatermark(updatedAfter, requestStartedAt, plan.deferredNewIssueCount),
      deferredNewIssueCount: plan.deferredNewIssueCount,
    };
  };

  /**
   * Override inherited create issue, to update list only if user is on current project
   * @param workspaceSlug
   * @param projectId
   * @param data
   * @returns
   */
  override createIssue = async (workspaceSlug: string, projectId: string, data: Partial<TIssue>) => {
    const response = await super.createIssue(workspaceSlug, projectId, data, "", projectId === this.router.projectId);
    return response;
  };

  // Using aliased names as they cannot be overridden in other stores
  archiveBulkIssues = this.bulkArchiveIssues;
  quickAddIssue = this.issueQuickAdd;
  updateIssue = this.issueUpdate;
  archiveIssue = this.issueArchive;
}
