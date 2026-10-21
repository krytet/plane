/**
 * Copyright (c) 2023-present Plane Software, Inc. and contributors
 * SPDX-License-Identifier: AGPL-3.0-only
 * See the LICENSE file for details.
 */

import type { TIssueLink } from "./issue_link";

export type TIssueChatProvider = "codex" | "chatgpt";

export type TIssueChatProgress = {
  done: string[];
  now: string[];
  waiting: string[];
};

export type TIssueChatBinding = {
  id: string;
  issue_id: string;
  link: TIssueLink | null;
  provider: TIssueChatProvider;
  thread_id: string;
  sync_cursor: number;
  progress: TIssueChatProgress;
  last_synced_at: string | null;
  created_at?: string;
  updated_at?: string;
};

export type TIssueChatBindingCreate = {
  provider: TIssueChatProvider;
  url?: string;
  thread_id: string;
  title?: string;
};

export type TIssueChatBindingSyncResponse = TIssueChatBinding & {
  applied: boolean;
};
