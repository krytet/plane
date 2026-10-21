/**
 * Copyright (c) 2023-present Plane Software, Inc. and contributors
 * SPDX-License-Identifier: AGPL-3.0-only
 * See the LICENSE file for details.
 */

import type { TProjectFile } from "@plane/types";

export const mergeProjectFilePages = (current: TProjectFile[], next: TProjectFile[]): TProjectFile[] => {
  const seen = new Set<string>();
  return [...current, ...next].filter((file) => {
    if (seen.has(file.id)) return false;
    seen.add(file.id);
    return true;
  });
};

export const getProjectFileIssueHref = (
  workspaceSlug: string,
  projectId: string,
  issueId: string | null
): string | null => (issueId ? `/${workspaceSlug}/projects/${projectId}/issues/${issueId}` : null);
