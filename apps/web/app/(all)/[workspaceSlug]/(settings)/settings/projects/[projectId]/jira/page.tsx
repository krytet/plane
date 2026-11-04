/**
 * Copyright (c) 2023-present Plane Software, Inc. and contributors
 * SPDX-License-Identifier: AGPL-3.0-only
 * See the LICENSE file for details.
 */

"use client";

import { useEffect, useMemo, useState } from "react";
import useSWR from "swr";
import { observer } from "mobx-react";
import { EUserPermissions, EUserPermissionsLevel } from "@plane/constants";
import { setToast } from "@plane/blocks/toast";
import { NotAuthorizedView } from "@/components/auth-screens/not-authorized-view";
import { PageHead } from "@/components/core/page-title";
import { SettingsContentWrapper } from "@/components/settings/content-wrapper";
import { SettingsHeading } from "@/components/settings/heading";
import { useProject } from "@/hooks/store/use-project";
import { useProjectState } from "@/hooks/store/use-project-state";
import { useUserPermissions } from "@/hooks/store/user";
import { JiraSyncService, type TJiraDirection, type TJiraPolicy } from "@/services/integrations/jira-sync.service";
import type { Route } from "./+types/page";
import { JiraProjectSettingsHeader } from "./header";

const jiraSyncService = new JiraSyncService();

type PolicyForm = {
  jira_project_key: string;
  jira_issue_type_id: string;
  jira_issue_type_name: string;
  board_id: string;
  direction: TJiraDirection;
  close_enabled: boolean;
  reopen_enabled: boolean;
  enabled: boolean;
  repository_base_ref: string;
  repository_target_ref: string;
  branch_template: string;
  pr_title_template: string;
  state_mappings: Record<string, { jira_status_id: string; jira_status_name: string; plane_state_id: string }>;
};

const emptyPolicy: PolicyForm = {
  jira_project_key: "",
  jira_issue_type_id: "",
  jira_issue_type_name: "",
  board_id: "",
  direction: "off",
  close_enabled: false,
  reopen_enabled: false,
  enabled: false,
  repository_base_ref: "master",
  repository_target_ref: "staging",
  branch_template: "{jira_key}-{slug}",
  pr_title_template: "{jira_key}: {summary}",
  state_mappings: {},
};

function policyToForm(policy: TJiraPolicy | null | undefined, stateIds: string[]): PolicyForm {
  if (!policy) {
    return {
      ...emptyPolicy,
      state_mappings: Object.fromEntries(
        stateIds.map((stateId) => [stateId, { jira_status_id: "", jira_status_name: "", plane_state_id: "" }])
      ),
    };
  }
  const mappings = { ...emptyPolicy.state_mappings };
  stateIds.forEach((stateId) => {
    const existing = policy.state_mappings[stateId] ?? {};
    mappings[stateId] = {
      jira_status_id: existing.jira_status_id ?? "",
      jira_status_name: existing.jira_status_name ?? "",
      plane_state_id: existing.plane_state_id ?? "",
    };
  });
  return {
    jira_project_key: policy.jira_project_key,
    jira_issue_type_id: policy.jira_issue_type_id,
    jira_issue_type_name: policy.jira_issue_type_name,
    board_id: policy.board_id,
    direction: policy.direction,
    close_enabled: policy.close_enabled,
    reopen_enabled: policy.reopen_enabled,
    enabled: policy.enabled,
    repository_base_ref: policy.repository_base_ref,
    repository_target_ref: policy.repository_target_ref,
    branch_template: policy.branch_template,
    pr_title_template: policy.pr_title_template,
    state_mappings: mappings,
  };
}

function JiraSettingsPage({ params }: Route.ComponentProps) {
  const { workspaceSlug, projectId } = params;
  const { currentProjectDetails } = useProject();
  const { workspaceUserInfo, allowPermissions } = useUserPermissions();
  const { projectStates, fetchProjectStates } = useProjectState();
  const [connectionOpen, setConnectionOpen] = useState(false);
  const [siteUrl, setSiteUrl] = useState("");
  const [email, setEmail] = useState("");
  const [apiToken, setApiToken] = useState("");
  const [form, setForm] = useState<PolicyForm>(emptyPolicy);
  const [error, setError] = useState<string | null>(null);
  const [saving, setSaving] = useState(false);

  const canPerformProjectAdminActions = allowPermissions(
    [EUserPermissions.ADMIN],
    EUserPermissionsLevel.PROJECT,
    workspaceSlug,
    projectId
  );
  const { data: readiness, mutate: refreshReadiness } = useSWR(
    workspaceSlug && projectId && canPerformProjectAdminActions ? `jira-readiness-${workspaceSlug}-${projectId}` : null,
    () => jiraSyncService.getReadiness(workspaceSlug, projectId)
  );
  const { data: policyResponse, mutate: refreshPolicy } = useSWR(
    workspaceSlug && projectId && canPerformProjectAdminActions ? `jira-policy-${workspaceSlug}-${projectId}` : null,
    () => jiraSyncService.getPolicy(workspaceSlug, projectId)
  );

  useEffect(() => {
    if (workspaceSlug && projectId && !projectStates) void fetchProjectStates(workspaceSlug, projectId);
  }, [fetchProjectStates, projectId, projectStates, workspaceSlug]);

  const stateIds = useMemo(() => (projectStates ?? []).map((state) => state.id), [projectStates]);

  useEffect(() => {
    setForm(policyToForm(policyResponse?.policy, stateIds));
  }, [policyResponse?.policy, stateIds]);

  const pageTitle = currentProjectDetails?.name ? `${currentProjectDetails.name} - Jira` : undefined;

  if (workspaceUserInfo && !canPerformProjectAdminActions) {
    return <NotAuthorizedView section="settings" isProjectView className="h-auto" />;
  }

  const handleConnect = async (event: React.FormEvent) => {
    event.preventDefault();
    setError(null);
    try {
      await jiraSyncService.connectPersonal(workspaceSlug, { site_url: siteUrl, email, api_token: apiToken });
      setApiToken("");
      setConnectionOpen(false);
      await Promise.all([refreshReadiness(), refreshPolicy()]);
      setToast({ type: "success", title: "Jira connected", message: "Your personal Jira account is connected." });
    } catch (caught) {
      const value = caught as { error?: string; message?: string };
      setError(value?.error ?? value?.message ?? "Could not connect Jira.");
    }
  };

  const handleDisconnect = async () => {
    setError(null);
    try {
      await jiraSyncService.disconnectPersonal(workspaceSlug);
      await refreshReadiness();
      setToast({ type: "success", title: "Jira disconnected", message: "Background Jira work has been paused." });
    } catch (caught) {
      const value = caught as { error?: string };
      setError(value?.error ?? "Could not disconnect Jira.");
    }
  };

  const handleSave = async (event: React.FormEvent) => {
    event.preventDefault();
    setError(null);
    setSaving(true);
    try {
      await jiraSyncService.savePolicy(workspaceSlug, projectId, {
        ...form,
        site_id: readiness?.site?.id,
        version: policyResponse?.policy?.version,
      });
      await Promise.all([refreshPolicy(), refreshReadiness()]);
      setToast({ type: "success", title: "Jira policy saved", message: "The project Jira policy was validated." });
    } catch (caught) {
      const value = caught as { error?: string; field?: string };
      setError(value?.error ?? "Could not save the Jira policy.");
    } finally {
      setSaving(false);
    }
  };

  const updateMapping = (
    stateId: string,
    field: "jira_status_id" | "jira_status_name" | "plane_state_id",
    value: string
  ) => {
    setForm((current) => ({
      ...current,
      state_mappings: {
        ...current.state_mappings,
        [stateId]: { ...current.state_mappings[stateId], [field]: value },
      },
    }));
  };

  return (
    <SettingsContentWrapper header={<JiraProjectSettingsHeader />} hugging>
      <PageHead title={pageTitle} />
      <section className="w-full">
        <SettingsHeading
          title="Jira integration"
          description="Connect your personal Jira account, link existing work items, and configure explicit status sync."
        />

        <div className="mt-7 space-y-6">
          <div className="rounded-md border border-subtle bg-surface-1 p-5">
            <div className="flex flex-wrap items-start justify-between gap-4">
              <div>
                <h4 className="text-body-sm-medium text-primary">Personal Jira connection</h4>
                <p className="mt-1 text-caption-md-regular text-tertiary">
                  Credentials are stored server-side and are never returned to the browser.
                </p>
              </div>
              {readiness?.grant ? (
                <button
                  type="button"
                  className="rounded-sm border border-subtle px-3 py-2 text-caption-sm-medium"
                  onClick={handleDisconnect}
                >
                  Disconnect
                </button>
              ) : (
                <button
                  type="button"
                  className="rounded-sm bg-accent-primary px-3 py-2 text-caption-sm-medium text-white"
                  onClick={() => setConnectionOpen((open) => !open)}
                >
                  Connect Jira
                </button>
              )}
            </div>
            {readiness?.grant ? (
              <div className="mt-4 grid gap-2 text-body-xs-regular text-secondary sm:grid-cols-3">
                <div>
                  <span className="text-tertiary">Account</span>
                  <div>{readiness.grant.display_name || readiness.grant.email}</div>
                </div>
                <div>
                  <span className="text-tertiary">Site</span>
                  <div className="truncate">{readiness.grant.site_url}</div>
                </div>
                <div>
                  <span className="text-tertiary">Health</span>
                  <div>{readiness.grant.health}</div>
                </div>
              </div>
            ) : (
              <p className="mt-4 text-body-xs-regular text-secondary">
                Connect your own Atlassian account to link or create Jira work items.
              </p>
            )}
            {connectionOpen && !readiness?.grant && (
              <form className="mt-5 grid gap-3" onSubmit={handleConnect}>
                <label className="text-caption-sm-medium text-primary">
                  Jira site URL
                  <input
                    className="mt-1 block w-full rounded-sm border border-subtle bg-surface-1 px-3 py-2 text-body-xs-regular"
                    value={siteUrl}
                    onChange={(event) => setSiteUrl(event.target.value)}
                    placeholder="https://example.atlassian.net"
                    required
                  />
                </label>
                <label className="text-caption-sm-medium text-primary">
                  Atlassian email
                  <input
                    type="email"
                    autoComplete="email"
                    className="mt-1 block w-full rounded-sm border border-subtle bg-surface-1 px-3 py-2 text-body-xs-regular"
                    value={email}
                    onChange={(event) => setEmail(event.target.value)}
                    required
                  />
                </label>
                <label className="text-caption-sm-medium text-primary">
                  Personal API token
                  <input
                    type="password"
                    autoComplete="new-password"
                    className="mt-1 block w-full rounded-sm border border-subtle bg-surface-1 px-3 py-2 text-body-xs-regular"
                    value={apiToken}
                    onChange={(event) => setApiToken(event.target.value)}
                    required
                  />
                </label>
                <div>
                  <button
                    type="submit"
                    className="rounded-sm bg-accent-primary px-3 py-2 text-caption-sm-medium text-white"
                  >
                    Verify and connect
                  </button>
                </div>
              </form>
            )}
            {readiness?.blockers?.length ? (
              <ul className="mt-4 space-y-1 text-caption-md-regular text-secondary">
                {readiness.blockers.map((blocker) => (
                  <li key={blocker.code}>
                    • {blocker.code} — {blocker.responsible_role}
                  </li>
                ))}
              </ul>
            ) : null}
          </div>

          <form className="rounded-md border border-subtle bg-surface-1 p-5" onSubmit={handleSave}>
            <div className="flex flex-wrap items-start justify-between gap-4">
              <div>
                <h4 className="text-body-sm-medium text-primary">Project routing</h4>
                <p className="mt-1 text-caption-md-regular text-tertiary">
                  Configure statuses explicitly. Changes are versioned and require review.
                </p>
              </div>
              <label className="flex items-center gap-2 text-caption-md-regular">
                <input
                  type="checkbox"
                  checked={form.enabled}
                  onChange={(event) => setForm((current) => ({ ...current, enabled: event.target.checked }))}
                />{" "}
                Enable sync
              </label>
            </div>
            <div className="mt-5 grid gap-4 sm:grid-cols-2">
              <label className="text-caption-sm-medium text-primary">
                Jira project key
                <input
                  className="mt-1 block w-full rounded-sm border border-subtle bg-surface-1 px-3 py-2 text-body-xs-regular uppercase"
                  value={form.jira_project_key}
                  onChange={(event) => setForm((current) => ({ ...current, jira_project_key: event.target.value }))}
                  placeholder="FP"
                  required
                />
              </label>
              <label className="text-caption-sm-medium text-primary">
                Jira issue type ID
                <input
                  className="mt-1 block w-full rounded-sm border border-subtle bg-surface-1 px-3 py-2 text-body-xs-regular"
                  value={form.jira_issue_type_id}
                  onChange={(event) => setForm((current) => ({ ...current, jira_issue_type_id: event.target.value }))}
                  placeholder="10001"
                  required
                />
              </label>
              <label className="text-caption-sm-medium text-primary">
                Issue type name
                <input
                  className="mt-1 block w-full rounded-sm border border-subtle bg-surface-1 px-3 py-2 text-body-xs-regular"
                  value={form.jira_issue_type_name}
                  onChange={(event) => setForm((current) => ({ ...current, jira_issue_type_name: event.target.value }))}
                  placeholder="Task"
                />
              </label>
              <label className="text-caption-sm-medium text-primary">
                Board ID (optional)
                <input
                  className="mt-1 block w-full rounded-sm border border-subtle bg-surface-1 px-3 py-2 text-body-xs-regular"
                  value={form.board_id}
                  onChange={(event) => setForm((current) => ({ ...current, board_id: event.target.value }))}
                />
              </label>
              <label className="text-caption-sm-medium text-primary">
                Direction
                <select
                  className="mt-1 block w-full rounded-sm border border-subtle bg-surface-1 px-3 py-2 text-body-xs-regular"
                  value={form.direction}
                  onChange={(event) =>
                    setForm((current) => ({ ...current, direction: event.target.value as TJiraDirection }))
                  }
                >
                  <option value="off">Off</option>
                  <option value="plane_to_jira">Plane → Jira</option>
                  <option value="jira_to_plane">Jira → Plane</option>
                  <option value="bidirectional">Bidirectional</option>
                </select>
              </label>
              <div className="flex items-end gap-4 pb-2 text-caption-md-regular">
                <label className="flex items-center gap-2">
                  <input
                    type="checkbox"
                    checked={form.close_enabled}
                    onChange={(event) => setForm((current) => ({ ...current, close_enabled: event.target.checked }))}
                  />{" "}
                  Close
                </label>
                <label className="flex items-center gap-2">
                  <input
                    type="checkbox"
                    checked={form.reopen_enabled}
                    onChange={(event) => setForm((current) => ({ ...current, reopen_enabled: event.target.checked }))}
                  />{" "}
                  Reopen
                </label>
              </div>
            </div>
            <div className="mt-6">
              <h5 className="text-caption-md-medium text-secondary">Repository profile</h5>
              <p className="mt-1 text-caption-sm-regular text-tertiary">
                The plan uses the linked Jira key. It does not push or merge automatically.
              </p>
              <div className="mt-3 grid gap-4 sm:grid-cols-2">
                <label className="text-caption-sm-medium text-primary">
                  Branch source
                  <input
                    className="mt-1 block w-full rounded-sm border border-subtle bg-surface-1 px-3 py-2 text-body-xs-regular"
                    value={form.repository_base_ref}
                    onChange={(event) =>
                      setForm((current) => ({ ...current, repository_base_ref: event.target.value }))
                    }
                  />
                </label>
                <label className="text-caption-sm-medium text-primary">
                  PR target
                  <input
                    className="mt-1 block w-full rounded-sm border border-subtle bg-surface-1 px-3 py-2 text-body-xs-regular"
                    value={form.repository_target_ref}
                    onChange={(event) =>
                      setForm((current) => ({ ...current, repository_target_ref: event.target.value }))
                    }
                  />
                </label>
                <label className="text-caption-sm-medium text-primary">
                  Branch template
                  <input
                    className="mt-1 block w-full rounded-sm border border-subtle bg-surface-1 px-3 py-2 text-body-xs-regular"
                    value={form.branch_template}
                    onChange={(event) => setForm((current) => ({ ...current, branch_template: event.target.value }))}
                  />
                </label>
                <label className="text-caption-sm-medium text-primary">
                  PR title template
                  <input
                    className="mt-1 block w-full rounded-sm border border-subtle bg-surface-1 px-3 py-2 text-body-xs-regular"
                    value={form.pr_title_template}
                    onChange={(event) => setForm((current) => ({ ...current, pr_title_template: event.target.value }))}
                  />
                </label>
              </div>
            </div>
            <div className="mt-6">
              <h5 className="text-caption-md-medium text-secondary">State mappings</h5>
              <p className="mt-1 text-caption-sm-regular text-tertiary">
                Jira status IDs are explicit IDs from the selected Jira workflow. Plane target states are used only for
                inbound sync.
              </p>
              <div className="mt-3 space-y-3">
                {(projectStates ?? []).map((state) => (
                  <div
                    key={state.id}
                    className="grid gap-2 rounded-sm border border-subtle p-3 sm:grid-cols-[minmax(0,1fr)_minmax(0,1fr)_minmax(0,1fr)] sm:items-center"
                  >
                    <div>
                      <div className="text-caption-md-medium text-primary">{state.name}</div>
                      <div className="text-caption-sm-regular text-tertiary">{state.group}</div>
                    </div>
                    <label className="text-caption-sm-regular text-secondary">
                      Jira status ID
                      <input
                        className="mt-1 block w-full rounded-sm border border-subtle bg-surface-1 px-2 py-1.5"
                        value={form.state_mappings[state.id]?.jira_status_id ?? ""}
                        onChange={(event) => updateMapping(state.id, "jira_status_id", event.target.value)}
                        placeholder="10001"
                      />
                    </label>
                    <label className="text-caption-sm-regular text-secondary">
                      Inbound Plane state
                      <select
                        className="mt-1 block w-full rounded-sm border border-subtle bg-surface-1 px-2 py-1.5"
                        value={form.state_mappings[state.id]?.plane_state_id ?? ""}
                        onChange={(event) => updateMapping(state.id, "plane_state_id", event.target.value)}
                      >
                        <option value="">Not mapped</option>
                        {(projectStates ?? []).map((target) => (
                          <option key={target.id} value={target.id}>
                            {target.name}
                          </option>
                        ))}
                      </select>
                    </label>
                  </div>
                ))}
              </div>
            </div>
            {error ? (
              <p
                role="alert"
                className="mt-4 rounded-sm bg-danger-subtle px-3 py-2 text-caption-md-regular text-danger-primary"
              >
                {error}
              </p>
            ) : null}
            <div className="mt-5 flex items-center gap-3">
              <button
                type="submit"
                disabled={saving}
                className="rounded-sm bg-accent-primary px-3 py-2 text-caption-sm-medium text-white disabled:opacity-60"
              >
                {saving ? "Saving…" : "Save and validate"}
              </button>
              {policyResponse?.policy ? (
                <span className="text-caption-sm-regular text-tertiary">Version {policyResponse.policy.version}</span>
              ) : null}
            </div>
          </form>

          <div className="rounded-md border border-subtle bg-surface-1 p-5">
            <h4 className="text-body-sm-medium text-primary">What happens next</h4>
            <p className="mt-2 text-body-xs-regular text-secondary">
              New Jira tickets use the Jackrabbit English template. The description is reviewed before creation. Status
              writes are disabled until the policy is enabled.
            </p>
            <p className="mt-2 text-body-xs-regular text-secondary">
              Linked branches and pull requests use the real Jira key and the configured repository workflow.
            </p>
          </div>
        </div>
      </section>
    </SettingsContentWrapper>
  );
}

export default observer(JiraSettingsPage);
