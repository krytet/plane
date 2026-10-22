/**
 * Copyright (c) 2023-present Plane Software, Inc. and contributors
 * SPDX-License-Identifier: AGPL-3.0-only
 * See the LICENSE file for details.
 */

import { useCallback, useEffect, useRef } from "react";
import { monitorForElements } from "@atlaskit/pragmatic-drag-and-drop/element/adapter";
import { EIssuesStoreType } from "@plane/types";
import { useKanbanView } from "@/hooks/store/use-kanban-view";
import { useIssues } from "@/hooks/store/use-issues";
import useOnlineStatus from "@/hooks/use-online-status";
import {
  PROJECT_BOARD_DRAG_QUIET_MS,
  PROJECT_BOARD_REFRESH_ERROR_LOG_INTERVAL_MS,
  ProjectBoardRefreshScheduler,
  hasActiveProjectBoardLoader,
  isProjectBoardRefreshWindowActive,
  resolveProjectBoardRefreshInterval,
} from "./project-board-auto-refresh";

type TUseProjectBoardAutoRefreshOptions = {
  workspaceSlug: string | undefined;
  projectId: string | undefined;
  enabled: boolean;
};

function isEditableTarget(target: EventTarget | null): boolean {
  if (typeof HTMLElement === "undefined" || !(target instanceof HTMLElement)) return false;
  return (
    target.isContentEditable || target.matches("input, textarea, select, [contenteditable='true'], [role='textbox']")
  );
}

function diagnosticMessage(error: unknown): string {
  const message = error instanceof Error ? error.message : String(error);
  return message.replace(/\s+/g, " ").slice(0, 200);
}

export const useProjectBoardAutoRefresh = (options: TUseProjectBoardAutoRefreshOptions) => {
  const { workspaceSlug, projectId, enabled } = options;
  const { issues } = useIssues(EIssuesStoreType.PROJECT);
  const kanbanView = useKanbanView();
  const { isOnline } = useOnlineStatus();

  const onlineRef = useRef(isOnline);
  const visibleRef = useRef(typeof document === "undefined" ? false : document.visibilityState === "visible");
  const draggingRef = useRef(false);
  const dragQuietUntilRef = useRef(0);
  const mountedRef = useRef(false);
  const schedulerRef = useRef<ProjectBoardRefreshScheduler | null>(null);
  const lastSuccessfulUpdatedAfterRef = useRef(new Date().toISOString());
  const lastErrorLogAtRef = useRef(0);
  const intervalMs = resolveProjectBoardRefreshInterval(import.meta.env.VITE_PROJECT_ISSUES_REFRESH_INTERVAL_MS);

  useEffect(() => {
    lastSuccessfulUpdatedAfterRef.current = new Date().toISOString();
  }, [workspaceSlug, projectId]);

  const canRefresh = useCallback(() => {
    if (!mountedRef.current) return false;
    if (!isProjectBoardRefreshWindowActive(onlineRef.current, visibleRef.current)) return false;
    if (draggingRef.current || kanbanView.isDragging || Date.now() < dragQuietUntilRef.current) return false;
    if (hasActiveProjectBoardLoader(issues.loader)) return false;
    if (!issues.groupedIssueIds || !issues.paginationOptions) return false;
    if (typeof document !== "undefined" && isEditableTarget(document.activeElement)) return false;
    return true;
  }, [issues, kanbanView]);

  useEffect(() => {
    onlineRef.current = isOnline;
    if (isOnline) schedulerRef.current?.requestRun(0);
  }, [isOnline]);

  useEffect(() => {
    const updateVisibility = () => {
      const wasVisible = visibleRef.current;
      visibleRef.current = document.visibilityState === "visible";
      if (!wasVisible && visibleRef.current && onlineRef.current) {
        schedulerRef.current?.requestRun(0);
      }
    };
    const handleFocus = () => {
      if (visibleRef.current && onlineRef.current) schedulerRef.current?.requestRun(0);
    };

    document.addEventListener("visibilitychange", updateVisibility);
    window.addEventListener("focus", handleFocus);
    return () => {
      document.removeEventListener("visibilitychange", updateVisibility);
      window.removeEventListener("focus", handleFocus);
    };
  }, []);

  useEffect(() => {
    if (!enabled) return;
    return monitorForElements({
      onDragStart: () => {
        draggingRef.current = true;
      },
      onDrop: () => {
        draggingRef.current = false;
        dragQuietUntilRef.current = Date.now() + PROJECT_BOARD_DRAG_QUIET_MS;
        schedulerRef.current?.requestRun(PROJECT_BOARD_DRAG_QUIET_MS);
      },
    });
  }, [enabled]);

  useEffect(() => {
    if (!enabled || !workspaceSlug || !projectId) return;

    mountedRef.current = true;
    const logDiagnostic = (message: string) => {
      const now = Date.now();
      if (now - lastErrorLogAtRef.current < PROJECT_BOARD_REFRESH_ERROR_LOG_INTERVAL_MS) return;
      lastErrorLogAtRef.current = now;
      console.warn(`[project-board-auto-refresh] ${message}`);
    };
    const scheduler = new ProjectBoardRefreshScheduler({
      intervalMs,
      shouldRun: canRefresh,
      task: async () => {
        const previousUpdatedAfter = lastSuccessfulUpdatedAfterRef.current;
        const result = await issues.refreshIssuesPreservingLoadedPages(
          workspaceSlug,
          projectId,
          previousUpdatedAfter,
          canRefresh
        );
        if (result.applied && mountedRef.current) {
          lastSuccessfulUpdatedAfterRef.current = result.nextUpdatedAfter;
          if (result.deferredNewIssueCount > 0) {
            logDiagnostic(
              `deferred ${result.deferredNewIssueCount} new matching issues; reload the board to include them`
            );
          }
        }
      },
      onError: (error) => logDiagnostic(diagnosticMessage(error)),
    });
    schedulerRef.current = scheduler;
    scheduler.start();

    return () => {
      mountedRef.current = false;
      scheduler.stop();
      if (schedulerRef.current === scheduler) schedulerRef.current = null;
    };
  }, [canRefresh, enabled, intervalMs, issues, projectId, workspaceSlug]);
};
