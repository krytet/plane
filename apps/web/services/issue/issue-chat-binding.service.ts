/**
 * Copyright (c) 2023-present Plane Software, Inc. and contributors
 * SPDX-License-Identifier: AGPL-3.0-only
 * See the LICENSE file for details.
 */

import { API_BASE_URL } from "@plane/constants";
import type {
  TIssueChatBinding,
  TIssueChatBindingCreate,
  TIssueChatBindingResponse,
  TIssueChatManualAttention,
} from "@plane/types";
// services
import { APIService } from "@/services/api.service";

export class IssueChatBindingService extends APIService {
  constructor() {
    super(API_BASE_URL);
  }

  private getPath(workspaceSlug: string, projectId: string, issueId: string) {
    return `/api/workspaces/${workspaceSlug}/projects/${projectId}/issues/${issueId}/chat-bindings/`;
  }

  private getBindingPath(workspaceSlug: string, projectId: string, issueId: string, bindingId: string) {
    return `${this.getPath(workspaceSlug, projectId, issueId)}${bindingId}/`;
  }

  async listIssueChatBindings(workspaceSlug: string, projectId: string, issueId: string): Promise<TIssueChatBinding[]> {
    return this.get(this.getPath(workspaceSlug, projectId, issueId))
      .then((response) => response?.data)
      .catch((error) => {
        throw error?.response?.data;
      });
  }

  async createIssueChatBinding(
    workspaceSlug: string,
    projectId: string,
    issueId: string,
    data: TIssueChatBindingCreate
  ): Promise<TIssueChatBindingResponse> {
    return this.post(this.getPath(workspaceSlug, projectId, issueId), data)
      .then((response) => response?.data)
      .catch((error) => {
        throw error?.response?.data;
      });
  }

  async markIssueChatBindingSeen(
    workspaceSlug: string,
    projectId: string,
    issueId: string,
    bindingId: string
  ): Promise<TIssueChatBindingResponse> {
    return this.post(`${this.getBindingPath(workspaceSlug, projectId, issueId, bindingId)}seen/`, {})
      .then((response) => response?.data)
      .catch((error) => {
        throw error?.response?.data;
      });
  }

  async setIssueChatManualAttention(
    workspaceSlug: string,
    projectId: string,
    issueId: string,
    bindingId: string,
    manualAttention: TIssueChatManualAttention
  ): Promise<TIssueChatBindingResponse> {
    return this.post(`${this.getBindingPath(workspaceSlug, projectId, issueId, bindingId)}manual-attention/`, {
      manual_attention: manualAttention,
    })
      .then((response) => response?.data)
      .catch((error) => {
        throw error?.response?.data;
      });
  }
}
