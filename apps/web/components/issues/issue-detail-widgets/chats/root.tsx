/**
 * Copyright (c) 2023-present Plane Software, Inc. and contributors
 * SPDX-License-Identifier: AGPL-3.0-only
 * See the LICENSE file for details.
 */

import { useCallback, useEffect, useState } from "react";
import { observer } from "mobx-react";
import { AddOutline, RefreshOutline } from "@makeplane/propel/icons";
import { Collapsible } from "@makeplane/propel/components/collapsible";
import type { TIssueChatBinding, TIssueChatBindingCreate, TIssueServiceType } from "@plane/types";
// hooks
import { useIssueDetail } from "@/hooks/store/use-issue-detail";
// services
import { IssueChatBindingService } from "@/services/issue";
// local imports
import { IssueChatBindingAddForm } from "./add-form";
import { IssueChatBindingItem } from "./binding-item";

const issueChatBindingService = new IssueChatBindingService();

type Props = {
  workspaceSlug: string;
  projectId: string;
  issueId: string;
  disabled?: boolean;
  issueServiceType: TIssueServiceType;
};

const getErrorMessage = (error: unknown): string => {
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
  return "Could not load chat bindings. The app route may not be available in this deployment yet.";
};

export const ChatsCollapsible = observer(function ChatsCollapsible(props: Props) {
  const { workspaceSlug, projectId, issueId, disabled = false, issueServiceType } = props;
  const { openWidgets, toggleOpenWidget } = useIssueDetail(issueServiceType);
  const [bindings, setBindings] = useState<TIssueChatBinding[]>([]);
  const [isLoading, setIsLoading] = useState(true);
  const [isSubmitting, setIsSubmitting] = useState(false);
  const [formError, setFormError] = useState<string>();
  const [loadError, setLoadError] = useState<string>();
  const [isFormOpen, setIsFormOpen] = useState(false);
  const isCollapsibleOpen = openWidgets.includes("chats");

  const loadBindings = useCallback(async () => {
    setIsLoading(true);
    setLoadError(undefined);
    try {
      const response = await issueChatBindingService.listIssueChatBindings(workspaceSlug, projectId, issueId);
      setBindings(response);
    } catch (error) {
      setLoadError(getErrorMessage(error));
    } finally {
      setIsLoading(false);
    }
  }, [issueId, projectId, workspaceSlug]);

  useEffect(() => {
    void loadBindings();
  }, [loadBindings]);

  const handleCreate = async (data: TIssueChatBindingCreate) => {
    setIsSubmitting(true);
    setFormError(undefined);
    try {
      const binding = await issueChatBindingService.createIssueChatBinding(workspaceSlug, projectId, issueId, data);
      setBindings((current) => [binding, ...current.filter((item) => item.id !== binding.id)]);
      setIsFormOpen(false);
    } catch (error) {
      setFormError(getErrorMessage(error));
    } finally {
      setIsSubmitting(false);
    }
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
        isCollapsibleOpen && !disabled ? (
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

        {isLoading ? (
          <p className="text-11 text-tertiary">Loading chat bindings…</p>
        ) : loadError ? (
          <div className="flex flex-wrap items-center justify-between gap-2 rounded-md bg-surface-2 p-3">
            <p className="text-11 text-danger-primary">{loadError}</p>
            <button
              type="button"
              className="flex items-center gap-1 text-11 font-medium text-secondary hover:text-primary"
              onClick={() => void loadBindings()}
            >
              <RefreshOutline className="size-3" />
              Retry
            </button>
          </div>
        ) : bindings.length > 0 ? (
          bindings.map((binding) => <IssueChatBindingItem key={binding.id} binding={binding} />)
        ) : (
          <p className="text-11 text-tertiary">
            No chat bindings yet. Add a web link, or use a Codex thread UUID for a local deep link.
          </p>
        )}
      </div>
    </Collapsible>
  );
});
