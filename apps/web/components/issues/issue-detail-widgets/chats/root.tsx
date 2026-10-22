/**
 * Copyright (c) 2023-present Plane Software, Inc. and contributors
 * SPDX-License-Identifier: AGPL-3.0-only
 * See the LICENSE file for details.
 */

import { useCallback, useEffect, useRef, useState } from "react";
import { observer } from "mobx-react";
import { AddOutline, RefreshOutline } from "@makeplane/propel/icons";
import { Collapsible } from "@makeplane/propel/components/collapsible";
import type {
  TIssueChatBinding,
  TIssueChatBindingCreate,
  TIssueChatManualAttention,
  TIssueServiceType,
} from "@plane/types";
// hooks
import { useIssueDetail } from "@/hooks/store/use-issue-detail";
// services
import { IssueChatBindingService } from "@/services/issue";
// local imports
import { IssueChatBindingAddForm } from "./add-form";
import { IssueChatBindingItem } from "./binding-item";
import {
  createIssueChatRefreshScheduler,
  finishIssueChatMutation,
  isCurrentIssueChatRefreshLifecycle,
  shouldApplyIssueChatRefresh,
  shouldPollIssueChatBindings,
} from "./helpers";

const issueChatBindingService = new IssueChatBindingService();

type Props = {
  workspaceSlug: string;
  projectId: string;
  issueId: string;
  disabled?: boolean;
  issueServiceType: TIssueServiceType;
};

const getErrorMessage = (
  error: unknown,
  fallback = "Could not load chat bindings. The app route may not be available in this deployment yet."
): string => {
  if (typeof error === "string") return error;
  if (error && typeof error === "object") {
    const data = error as { error?: string; detail?: string; code?: string; [key: string]: unknown };
    if (data.code === "thread_already_bound") return "This provider thread is already bound to another work item.";
    if (data.error) return data.error;
    if (data.detail) return data.detail;
    const fieldError = Object.values(data).find(
      (value) => Array.isArray(value) && value.some((item) => typeof item === "string")
    );
    if (Array.isArray(fieldError)) return fieldError.find((item): item is string => typeof item === "string") ?? "";
  }
  return fallback;
};

export const ChatsCollapsible = observer(function ChatsCollapsible(props: Props) {
  const { workspaceSlug, projectId, issueId, disabled = false, issueServiceType } = props;
  const { openWidgets, toggleOpenWidget } = useIssueDetail(issueServiceType);
  const identity = `${workspaceSlug}:${projectId}:${issueId}`;
  const identityRef = useRef(identity);
  const isMountedRef = useRef(true);
  const requestSequenceRef = useRef(0);
  const mutationRevisionRef = useRef(0);
  const refreshSchedulerRef = useRef<ReturnType<typeof createIssueChatRefreshScheduler> | null>(null);
  identityRef.current = identity;
  const [bindings, setBindings] = useState<TIssueChatBinding[]>([]);
  const [isLoading, setIsLoading] = useState(true);
  const [isRefreshing, setIsRefreshing] = useState(false);
  const [isSubmitting, setIsSubmitting] = useState(false);
  const [formError, setFormError] = useState<string>();
  const [loadError, setLoadError] = useState<string>();
  const [refreshError, setRefreshError] = useState<string>();
  const [isFormOpen, setIsFormOpen] = useState(false);
  const [pendingAction, setPendingAction] = useState<{
    bindingId: string;
    action: "seen" | "manual_attention";
  }>();
  const [actionErrors, setActionErrors] = useState<Record<string, string>>({});
  const isCollapsibleOpen = openWidgets.includes("chats");
  const hasCodexBinding = bindings.some((binding) => binding.provider === "codex");

  const loadBindings = useCallback(
    async (mode: "initial" | "silent" | "manual" = "silent") => {
      const requestIdentity = identity;
      const requestId = ++requestSequenceRef.current;
      const requestGuard = {
        identity: requestIdentity,
        requestId,
        mutationRevision: mutationRevisionRef.current,
      };
      const isCurrentRequestLifecycle = () =>
        isMountedRef.current &&
        isCurrentIssueChatRefreshLifecycle(
          { identity: requestIdentity, requestId },
          { identity: identityRef.current, requestId: requestSequenceRef.current }
        );
      const isCurrentRequest = () =>
        isCurrentRequestLifecycle() &&
        shouldApplyIssueChatRefresh(requestGuard, {
          identity: identityRef.current,
          requestId: requestSequenceRef.current,
          mutationRevision: mutationRevisionRef.current,
        });

      if (mode === "initial") {
        setIsLoading(true);
        setLoadError(undefined);
      }
      if (mode === "manual") {
        setIsRefreshing(true);
        setRefreshError(undefined);
      }

      try {
        const response = await issueChatBindingService.listIssueChatBindings(workspaceSlug, projectId, issueId);
        if (!isCurrentRequest()) return;
        setBindings(response);
        setIsLoading(false);
        setLoadError(undefined);
        setRefreshError(undefined);
      } catch (error) {
        if (!isCurrentRequest()) return;
        const message = getErrorMessage(error);
        if (mode === "initial") setLoadError(message);
        if (mode === "manual") setRefreshError(message);
      } finally {
        const isCurrent = isCurrentRequestLifecycle();
        if (isCurrent && mode === "initial") setIsLoading(false);
        if (isCurrent && mode === "manual") setIsRefreshing(false);
      }
    },
    [identity, issueId, projectId, workspaceSlug]
  );

  useEffect(() => {
    isMountedRef.current = true;
    return () => {
      isMountedRef.current = false;
    };
  }, []);

  useEffect(() => {
    mutationRevisionRef.current += 1;
    setBindings([]);
    setActionErrors({});
    setPendingAction(undefined);
    setFormError(undefined);
    setRefreshError(undefined);
    setIsFormOpen(false);
    setIsSubmitting(false);
    setIsRefreshing(false);
    void loadBindings("initial");

    return () => {
      requestSequenceRef.current += 1;
    };
  }, [loadBindings]);

  const refreshBindings = useCallback(
    (trigger: "interval" | "focus" | "manual") => loadBindings(trigger === "manual" ? "manual" : "silent"),
    [loadBindings]
  );

  useEffect(() => {
    const scheduler = createIssueChatRefreshScheduler({ refresh: refreshBindings });
    refreshSchedulerRef.current = scheduler;

    const isPollingAllowed = () =>
      typeof document !== "undefined" &&
      shouldPollIssueChatBindings({
        widgetOpen: isCollapsibleOpen,
        disabled,
        hasCodexBinding,
        isDocumentVisible: document.visibilityState === "visible",
      });

    const handleFocus = () => {
      if (!isPollingAllowed()) return;
      scheduler.start();
      void scheduler.request("focus");
    };

    const handleVisibilityChange = () => {
      if (!isPollingAllowed()) {
        scheduler.stop();
        return;
      }
      scheduler.start();
      void scheduler.request("focus");
    };

    if (isPollingAllowed()) scheduler.start();
    window.addEventListener("focus", handleFocus);
    document.addEventListener("visibilitychange", handleVisibilityChange);

    return () => {
      window.removeEventListener("focus", handleFocus);
      document.removeEventListener("visibilitychange", handleVisibilityChange);
      scheduler.stop();
      if (refreshSchedulerRef.current === scheduler) refreshSchedulerRef.current = null;
    };
  }, [disabled, hasCodexBinding, isCollapsibleOpen, refreshBindings]);

  const beginMutation = () => {
    mutationRevisionRef.current += 1;
    return identityRef.current;
  };

  const finishMutation = (mutationIdentity: string) => {
    const completion = finishIssueChatMutation({
      mutationIdentity,
      currentIdentity: identityRef.current,
      isMounted: isMountedRef.current,
      mutationRevision: mutationRevisionRef.current,
    });
    mutationRevisionRef.current = completion.mutationRevision;
    return completion.isCurrent;
  };

  const handleCreate = async (data: TIssueChatBindingCreate) => {
    const mutationIdentity = beginMutation();
    setIsSubmitting(true);
    setFormError(undefined);
    try {
      const binding = await issueChatBindingService.createIssueChatBinding(workspaceSlug, projectId, issueId, data);
      if (!isMountedRef.current || identityRef.current !== mutationIdentity) return;
      setBindings((current) => [binding, ...current.filter((item) => item.id !== binding.id)]);
      setIsFormOpen(false);
    } catch (error) {
      if (!isMountedRef.current || identityRef.current !== mutationIdentity) return;
      setFormError(getErrorMessage(error));
    } finally {
      if (finishMutation(mutationIdentity)) setIsSubmitting(false);
    }
  };

  const replaceBinding = (updatedBinding: TIssueChatBinding) => {
    setBindings((current) => current.map((binding) => (binding.id === updatedBinding.id ? updatedBinding : binding)));
  };

  const clearBindingActionError = (bindingId: string) => {
    setActionErrors((current) => {
      if (!(bindingId in current)) return current;
      const next = { ...current };
      delete next[bindingId];
      return next;
    });
  };

  const handleMarkSeen = async (bindingId: string) => {
    if (disabled || pendingAction) return;
    const mutationIdentity = beginMutation();
    setPendingAction({ bindingId, action: "seen" });
    clearBindingActionError(bindingId);
    try {
      const updatedBinding = await issueChatBindingService.markIssueChatBindingSeen(
        workspaceSlug,
        projectId,
        issueId,
        bindingId
      );
      if (!isMountedRef.current || identityRef.current !== mutationIdentity) return;
      replaceBinding(updatedBinding);
    } catch (error) {
      if (!isMountedRef.current || identityRef.current !== mutationIdentity) return;
      setActionErrors((current) => ({
        ...current,
        [bindingId]: getErrorMessage(error, "Could not mark this chat as seen in Plane."),
      }));
    } finally {
      if (finishMutation(mutationIdentity)) {
        setPendingAction((current) =>
          current?.bindingId === bindingId && current.action === "seen" ? undefined : current
        );
      }
    }
  };

  const handleManualAttention = async (bindingId: string, value: TIssueChatManualAttention) => {
    if (disabled || pendingAction) return;
    const mutationIdentity = beginMutation();
    setPendingAction({ bindingId, action: "manual_attention" });
    clearBindingActionError(bindingId);
    try {
      const updatedBinding = await issueChatBindingService.setIssueChatManualAttention(
        workspaceSlug,
        projectId,
        issueId,
        bindingId,
        value
      );
      if (!isMountedRef.current || identityRef.current !== mutationIdentity) return;
      replaceBinding(updatedBinding);
    } catch (error) {
      if (!isMountedRef.current || identityRef.current !== mutationIdentity) return;
      setActionErrors((current) => ({
        ...current,
        [bindingId]: getErrorMessage(error, "Could not update manual attention."),
      }));
    } finally {
      if (finishMutation(mutationIdentity)) {
        setPendingAction((current) =>
          current?.bindingId === bindingId && current.action === "manual_attention" ? undefined : current
        );
      }
    }
  };

  const handleManualRefresh = () => {
    if (isLoading || isRefreshing) return;
    const scheduler = refreshSchedulerRef.current;
    if (scheduler) void scheduler.request("manual");
    else void loadBindings("manual");
  };

  return (
    <Collapsible
      open={isCollapsibleOpen}
      onOpenChange={() => toggleOpenWidget("chats")}
      trigger={
        <span className="inline-flex items-center gap-2">
          Chats
          <span className="flex items-center justify-center">
            <p className="text-14 leading-3! text-tertiary">{isLoading ? "…" : bindings.length}</p>
          </span>
        </span>
      }
      trailing={
        isCollapsibleOpen ? (
          <span className="flex items-center gap-2">
            <button
              type="button"
              aria-label="Refresh chat bindings"
              title="Refresh chat bindings"
              disabled={isLoading || isRefreshing}
              onClick={(event) => {
                event.preventDefault();
                event.stopPropagation();
                handleManualRefresh();
              }}
            >
              <RefreshOutline className={`h-4 w-4 ${isRefreshing ? "animate-spin" : ""}`} />
            </button>
            {!disabled && (
              <button
                type="button"
                aria-label="Add chat binding"
                onClick={(event) => {
                  event.preventDefault();
                  event.stopPropagation();
                  setFormError(undefined);
                  setIsFormOpen((current) => !current);
                }}
              >
                <AddOutline className="h-4 w-4" />
              </button>
            )}
          </span>
        ) : undefined
      }
    >
      <div className="space-y-3">
        {isFormOpen && (
          <IssueChatBindingAddForm
            isSubmitting={isSubmitting}
            error={formError}
            onCancel={() => {
              setFormError(undefined);
              setIsFormOpen(false);
            }}
            onSubmit={handleCreate}
          />
        )}

        {refreshError && (
          <p role="alert" className="text-11 text-danger-primary">
            {refreshError}
          </p>
        )}

        {isLoading ? (
          <p className="text-11 text-tertiary">Loading chat bindings…</p>
        ) : loadError && bindings.length === 0 ? (
          <div className="flex flex-wrap items-center justify-between gap-2 rounded-md bg-surface-2 p-3">
            <p className="text-11 text-danger-primary">{loadError}</p>
            <button
              type="button"
              className="flex items-center gap-1 text-11 font-medium text-secondary hover:text-primary"
              onClick={() => void loadBindings("initial")}
            >
              <RefreshOutline className="size-3" />
              Retry
            </button>
          </div>
        ) : bindings.length > 0 ? (
          bindings.map((binding) => (
            <IssueChatBindingItem
              key={binding.id}
              binding={binding}
              disabled={disabled}
              actionError={actionErrors[binding.id]}
              pendingAction={pendingAction?.bindingId === binding.id ? pendingAction.action : undefined}
              onMarkSeen={() => handleMarkSeen(binding.id)}
              onManualAttention={(value) => handleManualAttention(binding.id, value)}
            />
          ))
        ) : (
          <p className="text-11 text-tertiary">
            No chat bindings yet. Add a web link, or use a Codex thread UUID for a local deep link.
          </p>
        )}
      </div>
    </Collapsible>
  );
});
