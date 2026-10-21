/**
 * Copyright (c) 2023-present Plane Software, Inc. and contributors
 * SPDX-License-Identifier: AGPL-3.0-only
 * See the LICENSE file for details.
 */

import { API_BASE_URL } from "@plane/constants";
import type { TIssueChatBinding, TIssueChatBindingCreate, TIssueChatBindingSyncResponse } from "@plane/types";
// services
import { APIService } from "@/services/api.service";

export class IssueChatBindingService extends APIService {
  constructor() {
    super(API_BASE_URL);
  }

  private getPath(workspaceSlug: string, projectId: string, issueId: string) {
    return `/api/workspaces/${workspaceSlug}/projects/${projectId}/issues/${issueId}/chat-bindings/`;
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
  ): Promise<TIssueChatBindingSyncResponse> {
    // The visible form only collects binding metadata. An initial cursor with empty
    // progress keeps the app route compatible with the existing idempotent sync contract.
    const payload = {
      ...data,
      cursor: 1,
      progress: {
        done: [],
        now: [],
        waiting: [],
      },
    };

    return this.post(this.getPath(workspaceSlug, projectId, issueId), payload)
      .then((response) => response?.data)
      .catch((error) => {
        throw error?.response?.data;
      });
  }
}
