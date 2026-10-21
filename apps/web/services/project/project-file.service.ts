/**
 * Copyright (c) 2023-present Plane Software, Inc. and contributors
 * SPDX-License-Identifier: AGPL-3.0-only
 * See the LICENSE file for details.
 */

import { API_BASE_URL } from "@plane/constants";
import type { TProjectFile, TPaginatedResponse } from "@plane/types";
// services
import { APIService } from "@/services/api.service";

export class ProjectFileService extends APIService {
  constructor() {
    super(API_BASE_URL);
  }

  async getProjectFiles(
    workspaceSlug: string,
    projectId: string,
    cursor?: string
  ): Promise<TPaginatedResponse<TProjectFile[]>> {
    return this.get(`/api/workspaces/${workspaceSlug}/projects/${projectId}/files/`, {
      params: {
        per_page: 100,
        ...(cursor ? { cursor } : {}),
      },
    })
      .then((response) => response?.data)
      .catch((error) => {
        throw error?.response?.data;
      });
  }
}
