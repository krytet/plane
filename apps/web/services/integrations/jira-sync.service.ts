/**
 * Copyright (c) 2023-present Plane Software, Inc. and contributors
 * SPDX-License-Identifier: AGPL-3.0-only
 * See the LICENSE file for details.
 */

import { API_BASE_URL } from "@plane/constants";
import { APIService } from "@/services/api.service";

export type TJiraDirection = "off" | "plane_to_jira" | "jira_to_plane" | "bidirectional";
export type TJiraOverride = "inherit" | TJiraDirection;

export type TJiraGrant = {
  id: string;
  site_id: string;
  site_url: string;
  account_id: string;
  display_name: string;
  email: string;
  auth_type: string;
  health: string;
  credential_version: number;
  last_verified_at: string | null;
};

export type TJiraReadiness = {
  allowed_sites: string[];
  grant: TJiraGrant | null;
  site: { id: string; site_url: string; enabled: boolean } | null;
  policy: TJiraPolicy | null;
  blockers: { code: string; responsible_role: string; action: string }[];
  ready: boolean;
};

export type TJiraPolicy = {
  id: string;
  site_id: string;
  jira_project_key: string;
  jira_project_id: string;
  jira_issue_type_id: string;
  jira_issue_type_name: string;
  board_id: string;
  direction: TJiraDirection;
  close_enabled: boolean;
  reopen_enabled: boolean;
  state_mappings: Record<string, { jira_status_id?: string; jira_status_name?: string; plane_state_id?: string }>;
  template_version: string;
  repository_base_ref: string;
  repository_target_ref: string;
  branch_template: string;
  pr_title_template: string;
  enabled: boolean;
  version: number;
  effective_at: string | null;
};

export type TJiraBinding = {
  linked: boolean;
  access_required?: boolean;
  binding?: {
    id: string;
    jira_issue_key: string;
    jira_project_key: string;
    jira_status_id: string;
    jira_status_name: string;
    jira_url: string;
    sync_override: TJiraOverride;
    consent_version: number;
    last_verified_at: string | null;
    last_error: Record<string, unknown>;
  };
};

export type TJiraDraft = {
  id: string;
  summary: string;
  description_markdown: string;
  fields: Record<string, unknown>;
  source_revision: string;
  template_version: string;
  approval_hash: string;
  approved_at: string | null;
  expires_at: string | null;
  validation: { valid?: boolean; items?: { code: string; field?: string; message?: string }[] };
};

export type TJiraOperation = {
  id: string;
  kind: string;
  state: string;
  error_code: string;
  error_message: string;
  correlation_id: string;
  result: Record<string, unknown>;
  created_at: string;
};

export type TJiraDevelopmentPlan = {
  ready: boolean;
  reason: string | null;
  jira_issue_key: string;
  summary: string;
  repository: { id: number; owner: string; name: string; url: string | null } | null;
  branch_name: string;
  branch_source: string;
  pr_target: string;
  pr_title: string;
  pr_body: string;
  plan_hash: string;
};

export type TJiraConsent = {
  binding_id: string;
  automation_owner: string | null;
  consent: { id: string; scope_hash: string; version: number; granted_at: string; revoked_at: string | null } | null;
};

export class JiraSyncService extends APIService {
  constructor() {
    super(API_BASE_URL);
  }

  async getPersonalConnection(workspaceSlug: string): Promise<{ connected: boolean; grant?: TJiraGrant }> {
    return this.get(`/api/workspaces/${workspaceSlug}/integrations/jira/me/`)
      .then((response) => response?.data)
      .catch((error) => {
        throw error?.response?.data;
      });
  }

  async connectPersonal(
    workspaceSlug: string,
    data: { site_url: string; email: string; api_token: string }
  ): Promise<{ connected: boolean; grant: TJiraGrant }> {
    return this.post(`/api/workspaces/${workspaceSlug}/integrations/jira/me/`, data)
      .then((response) => response?.data)
      .catch((error) => {
        throw error?.response?.data;
      });
  }

  async disconnectPersonal(workspaceSlug: string): Promise<void> {
    return this.delete(`/api/workspaces/${workspaceSlug}/integrations/jira/me/`)
      .then(() => undefined)
      .catch((error) => {
        throw error?.response?.data;
      });
  }

  async getReadiness(workspaceSlug: string, projectId: string): Promise<TJiraReadiness> {
    return this.get(`/api/workspaces/${workspaceSlug}/integrations/jira/readiness/`, {
      params: { project_id: projectId },
    })
      .then((response) => response?.data)
      .catch((error) => {
        throw error?.response?.data;
      });
  }

  async getPolicy(workspaceSlug: string, projectId: string): Promise<{ policy: TJiraPolicy | null }> {
    return this.get(`/api/workspaces/${workspaceSlug}/projects/${projectId}/integrations/jira/policy/`)
      .then((response) => response?.data)
      .catch((error) => {
        throw error?.response?.data;
      });
  }

  async savePolicy(workspaceSlug: string, projectId: string, data: Record<string, unknown>) {
    return this.put(`/api/workspaces/${workspaceSlug}/projects/${projectId}/integrations/jira/policy/`, data)
      .then((response) => response?.data)
      .catch((error) => {
        throw error?.response?.data;
      });
  }

  async getIssueBinding(workspaceSlug: string, projectId: string, issueId: string): Promise<TJiraBinding> {
    return this.get(`/api/workspaces/${workspaceSlug}/projects/${projectId}/issues/${issueId}/jira/`)
      .then((response) => response?.data)
      .catch((error) => {
        throw error?.response?.data;
      });
  }

  async linkIssue(
    workspaceSlug: string,
    projectId: string,
    issueId: string,
    data: { issue_key: string; sync_override?: TJiraOverride }
  ): Promise<TJiraBinding> {
    return this.post(`/api/workspaces/${workspaceSlug}/projects/${projectId}/issues/${issueId}/jira/`, data)
      .then((response) => response?.data)
      .catch((error) => {
        throw error?.response?.data;
      });
  }

  async unlinkIssue(workspaceSlug: string, projectId: string, issueId: string): Promise<void> {
    return this.delete(`/api/workspaces/${workspaceSlug}/projects/${projectId}/issues/${issueId}/jira/`)
      .then(() => undefined)
      .catch((error) => {
        throw error?.response?.data;
      });
  }

  async getConsent(workspaceSlug: string, projectId: string, issueId: string): Promise<TJiraConsent> {
    return this.get(`/api/workspaces/${workspaceSlug}/projects/${projectId}/issues/${issueId}/jira/consent/`)
      .then((response) => response?.data)
      .catch((error) => {
        throw error?.response?.data;
      });
  }

  async grantConsent(
    workspaceSlug: string,
    projectId: string,
    issueId: string,
    allowedEvents: string[] = []
  ): Promise<{ id: string; version: number }> {
    return this.post(`/api/workspaces/${workspaceSlug}/projects/${projectId}/issues/${issueId}/jira/consent/`, {
      allowed_events: allowedEvents,
    })
      .then((response) => response?.data)
      .catch((error) => {
        throw error?.response?.data;
      });
  }

  async revokeConsent(workspaceSlug: string, projectId: string, issueId: string): Promise<void> {
    return this.delete(`/api/workspaces/${workspaceSlug}/projects/${projectId}/issues/${issueId}/jira/consent/`)
      .then(() => undefined)
      .catch((error) => {
        throw error?.response?.data;
      });
  }

  async createDraft(
    workspaceSlug: string,
    projectId: string,
    issueId: string,
    data: Record<string, unknown>
  ): Promise<TJiraDraft> {
    return this.post(`/api/workspaces/${workspaceSlug}/projects/${projectId}/issues/${issueId}/jira/drafts/`, data)
      .then((response) => response?.data)
      .catch((error) => {
        throw error?.response?.data;
      });
  }

  async approveDraft(
    workspaceSlug: string,
    projectId: string,
    draftId: string,
    approvalHash: string
  ): Promise<TJiraDraft> {
    return this.post(`/api/workspaces/${workspaceSlug}/projects/${projectId}/jira/drafts/${draftId}/approve/`, {
      approval_hash: approvalHash,
    })
      .then((response) => response?.data)
      .catch((error) => {
        throw error?.response?.data;
      });
  }

  async submitDraft(
    workspaceSlug: string,
    projectId: string,
    draftId: string,
    data: { approval_hash: string; idempotency_key: string; fields?: Record<string, unknown> }
  ): Promise<TJiraOperation> {
    return this.post(`/api/workspaces/${workspaceSlug}/projects/${projectId}/jira/drafts/${draftId}/submit/`, data)
      .then((response) => response?.data)
      .catch((error) => {
        throw error?.response?.data;
      });
  }

  async getDevelopmentPlan(workspaceSlug: string, projectId: string, issueId: string): Promise<TJiraDevelopmentPlan> {
    return this.get(`/api/workspaces/${workspaceSlug}/projects/${projectId}/issues/${issueId}/jira/development/`)
      .then((response) => response?.data)
      .catch((error) => {
        throw error?.response?.data;
      });
  }
}
