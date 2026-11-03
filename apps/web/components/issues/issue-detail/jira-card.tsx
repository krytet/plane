/**
 * Copyright (c) 2023-present Plane Software, Inc. and contributors
 * SPDX-License-Identifier: AGPL-3.0-only
 * See the LICENSE file for details.
 */

import { useEffect, useState } from "react";
import useSWR from "swr";
import { setToast } from "@plane/blocks/toast";
import {
  JiraSyncService,
  type TJiraConsent,
  type TJiraDevelopmentPlan,
  type TJiraDraft,
} from "@/services/integrations/jira-sync.service";

type Props = {
  workspaceSlug: string;
  projectId: string;
  issueId: string;
  issueName: string;
  disabled?: boolean;
};

const jiraSyncService = new JiraSyncService();

export function JiraIssueCard({ workspaceSlug, projectId, issueId, issueName, disabled = false }: Props) {
  const [linkKey, setLinkKey] = useState("");
  const [showLink, setShowLink] = useState(false);
  const [showCreate, setShowCreate] = useState(false);
  const [summary, setSummary] = useState(issueName);
  const [description, setDescription] = useState("");
  const [draft, setDraft] = useState<TJiraDraft | null>(null);
  const [plan, setPlan] = useState<TJiraDevelopmentPlan | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);
  const { data, mutate } = useSWR(`jira-issue-${workspaceSlug}-${projectId}-${issueId}`, () =>
    jiraSyncService.getIssueBinding(workspaceSlug, projectId, issueId)
  );

  useEffect(() => setSummary(issueName), [issueName]);

  const binding = data?.binding;
  const { data: consent, mutate: mutateConsent } = useSWR<TJiraConsent>(
    binding ? `jira-consent-${workspaceSlug}-${projectId}-${issueId}` : null,
    () => jiraSyncService.getConsent(workspaceSlug, projectId, issueId)
  );

  const handleLink = async (event: React.FormEvent) => {
    event.preventDefault();
    setError(null);
    setBusy(true);
    try {
      await jiraSyncService.linkIssue(workspaceSlug, projectId, issueId, { issue_key: linkKey.trim() });
      setLinkKey("");
      setShowLink(false);
      await mutate();
      setToast({ type: "success", title: "Jira linked", message: "The Jira issue is now linked." });
    } catch (caught) {
      const value = caught as { error?: string };
      setError(value?.error ?? "Could not link the Jira issue.");
    } finally {
      setBusy(false);
    }
  };

  const handleCreateDraft = async (event: React.FormEvent) => {
    event.preventDefault();
    setError(null);
    setBusy(true);
    try {
      const created = await jiraSyncService.createDraft(workspaceSlug, projectId, issueId, {
        summary,
        description_markdown: description,
      });
      setDraft(created);
    } catch (caught) {
      const value = caught as { error?: string };
      setError(value?.error ?? "Could not build the Jira draft.");
    } finally {
      setBusy(false);
    }
  };

  const handleSubmitDraft = async () => {
    if (!draft) return;
    setError(null);
    setBusy(true);
    try {
      await jiraSyncService.approveDraft(workspaceSlug, projectId, draft.id, draft.approval_hash);
      const operation = await jiraSyncService.submitDraft(workspaceSlug, projectId, draft.id, {
        approval_hash: draft.approval_hash,
        idempotency_key: crypto.randomUUID(),
      });
      await mutate();
      setDraft(null);
      setShowCreate(false);
      setToast({
        type: operation.state === "outcome_unknown" ? "warning" : "success",
        title: operation.state === "outcome_unknown" ? "Jira outcome unknown" : "Jira issue created",
        message:
          operation.state === "outcome_unknown"
            ? "The request may have reached Jira. Reconcile it before trying again."
            : "The Jira issue was read back and linked.",
      });
    } catch (caught) {
      const value = caught as { error?: string };
      setError(value?.error ?? "Could not create the Jira issue.");
    } finally {
      setBusy(false);
    }
  };

  const handleUnlink = async () => {
    setBusy(true);
    try {
      await jiraSyncService.unlinkIssue(workspaceSlug, projectId, issueId);
      await mutate();
      setToast({ type: "success", title: "Jira unlinked", message: "The remote Jira issue was left unchanged." });
    } catch (caught) {
      const value = caught as { error?: string };
      setError(value?.error ?? "Could not unlink the Jira issue.");
    } finally {
      setBusy(false);
    }
  };

  const handleConsent = async (checked: boolean) => {
    setError(null);
    setBusy(true);
    try {
      if (checked) await jiraSyncService.grantConsent(workspaceSlug, projectId, issueId);
      else await jiraSyncService.revokeConsent(workspaceSlug, projectId, issueId);
      await mutateConsent();
    } catch (caught) {
      const value = caught as { error?: string };
      setError(value?.error ?? "Could not update the sync consent.");
    } finally {
      setBusy(false);
    }
  };

  const handlePreparePlan = async () => {
    setError(null);
    setBusy(true);
    try {
      const prepared = await jiraSyncService.getDevelopmentPlan(workspaceSlug, projectId, issueId);
      setPlan(prepared);
    } catch (caught) {
      const value = caught as { error?: string };
      setError(value?.error ?? "Could not prepare the development plan.");
    } finally {
      setBusy(false);
    }
  };

  return (
    <div className="mt-5 border-t border-subtle pt-4">
      <div className="flex items-center justify-between gap-2">
        <h5 className="text-body-xs-medium text-primary">Jira</h5>
        {binding ? (
          <span className="rounded-full bg-layer-2 px-2 py-0.5 text-caption-sm-regular text-secondary">
            {binding.jira_status_name || "Linked"}
          </span>
        ) : null}
      </div>
      {data?.access_required ? (
        <p className="mt-2 text-caption-md-regular text-tertiary">
          Jira is connected, but your personal account cannot view this issue.
        </p>
      ) : binding ? (
        <div className="mt-2 space-y-2">
          <a
            className="block truncate text-caption-md-medium text-accent-primary"
            href={binding.jira_url}
            target="_blank"
            rel="noreferrer"
          >
            {binding.jira_issue_key}
          </a>
          <p className="text-caption-sm-regular text-tertiary">Sync: {binding.sync_override}</p>
          {!disabled ? (
            <label className="flex items-start gap-2 text-caption-sm-regular text-secondary">
              <input
                type="checkbox"
                disabled={busy}
                checked={!!consent?.consent}
                onChange={(event) => handleConsent(event.target.checked)}
              />
              <span>Allow background status sync from my Jira account</span>
            </label>
          ) : null}
          {!disabled ? (
            <button
              type="button"
              className="text-caption-sm-regular text-secondary underline"
              disabled={busy}
              onClick={handlePreparePlan}
            >
              Prepare branch and PR plan
            </button>
          ) : null}
          {plan ? (
            <div className="rounded-sm bg-layer-2 p-2 text-caption-sm-regular text-secondary">
              <div>
                <span className="text-tertiary">Branch:</span> {plan.branch_name}
              </div>
              <div>
                <span className="text-tertiary">PR:</span> {plan.pr_title}
              </div>
              <div>
                <span className="text-tertiary">Target:</span> {plan.repository?.owner}/{plan.repository?.name} →{" "}
                {plan.pr_target}
              </div>
              {!plan.ready ? <div className="text-danger-primary">Repository profile is not configured.</div> : null}
            </div>
          ) : null}
          {!disabled && (
            <button
              type="button"
              className="text-caption-sm-regular text-secondary underline"
              disabled={busy}
              onClick={handleUnlink}
            >
              Unlink locally
            </button>
          )}
        </div>
      ) : (
        <div className="mt-2 space-y-2">
          <p className="text-caption-sm-regular text-tertiary">No Jira issue linked.</p>
          {!disabled && (
            <div className="flex flex-wrap gap-2">
              <button
                type="button"
                className="rounded-sm border border-subtle px-2 py-1 text-caption-sm-medium"
                onClick={() => {
                  setShowLink((value) => !value);
                  setShowCreate(false);
                }}
              >
                Link existing
              </button>
              <button
                type="button"
                className="rounded-sm border border-subtle px-2 py-1 text-caption-sm-medium"
                onClick={() => {
                  setShowCreate((value) => !value);
                  setShowLink(false);
                }}
              >
                Create in Jira
              </button>
            </div>
          )}
        </div>
      )}
      {showLink && !binding ? (
        <form className="mt-3 space-y-2" onSubmit={handleLink}>
          <input
            aria-label="Jira issue key"
            className="w-full rounded-sm border border-subtle bg-surface-1 px-2 py-1.5 text-body-xs-regular"
            placeholder="FP-12345"
            value={linkKey}
            onChange={(event) => setLinkKey(event.target.value)}
            required
          />
          <button
            type="submit"
            disabled={busy}
            className="rounded-sm bg-accent-primary px-2 py-1 text-caption-sm-medium text-white disabled:opacity-60"
          >
            {busy ? "Linking…" : "Link issue"}
          </button>
        </form>
      ) : null}
      {showCreate && !binding ? (
        <form className="mt-3 space-y-2" onSubmit={handleCreateDraft}>
          <input
            aria-label="English Jira summary"
            className="w-full rounded-sm border border-subtle bg-surface-1 px-2 py-1.5 text-body-xs-regular"
            value={summary}
            onChange={(event) => setSummary(event.target.value)}
            required
          />
          <textarea
            aria-label="English Jira description"
            className="min-h-20 w-full rounded-sm border border-subtle bg-surface-1 px-2 py-1.5 text-body-xs-regular"
            placeholder="Add confirmed English context for the Jira description."
            value={description}
            onChange={(event) => setDescription(event.target.value)}
          />
          <button
            type="submit"
            disabled={busy}
            className="rounded-sm border border-subtle px-2 py-1 text-caption-sm-medium"
          >
            {busy ? "Building…" : "Build English draft"}
          </button>
        </form>
      ) : null}
      {draft ? (
        <div className="mt-3 rounded-sm border border-subtle bg-layer-2 p-2">
          <p className="text-caption-sm-medium text-primary">Review before creation</p>
          <p className="mt-1 line-clamp-4 text-caption-sm-regular whitespace-pre-wrap text-secondary">
            {draft.description_markdown}
          </p>
          {draft.validation?.items?.length ? (
            <ul className="mt-2 space-y-1 text-caption-sm-regular text-danger-primary">
              {draft.validation.items.map((item) => (
                <li key={item.code}>{item.message ?? item.code}</li>
              ))}
            </ul>
          ) : null}
          <div className="mt-2 flex flex-wrap gap-2">
            <button
              type="button"
              disabled={busy || draft.validation.valid === false}
              className="rounded-sm bg-accent-primary px-2 py-1 text-caption-sm-medium text-white disabled:opacity-60"
              onClick={handleSubmitDraft}
            >
              Approve and create
            </button>
            <button
              type="button"
              disabled={busy}
              className="rounded-sm border border-subtle px-2 py-1 text-caption-sm-medium"
              onClick={() => setDraft(null)}
            >
              Cancel
            </button>
          </div>
        </div>
      ) : null}
      {error ? (
        <p role="alert" className="mt-2 text-caption-sm-regular text-danger-primary">
          {error}
        </p>
      ) : null}
    </div>
  );
}
