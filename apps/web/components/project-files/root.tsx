/**
 * Copyright (c) 2023-present Plane Software, Inc. and contributors
 * SPDX-License-Identifier: AGPL-3.0-only
 * See the LICENSE file for details.
 */

import { useCallback, useEffect, useState } from "react";
import Link from "next/link";
import { DownloadOutline, FolderOutline, RefreshOutline } from "@makeplane/propel/icons";
import type { TProjectFile } from "@plane/types";
import { convertBytesToSize, getFileExtension, getFileURL, renderFormattedDate } from "@plane/utils";
// components
import { getFileIcon } from "@/components/icons";
// services
import { ProjectFileService } from "@/services/project";
// local imports
import { getProjectFileIssueHref, mergeProjectFilePages } from "./helpers";

const projectFileService = new ProjectFileService();

type Props = {
  workspaceSlug: string;
  projectId: string;
};

const getErrorMessage = (error: unknown): string => {
  if (typeof error === "string") return error;
  if (error && typeof error === "object") {
    const data = error as { error?: string; detail?: string };
    if (data.error) return data.error;
    if (data.detail) return data.detail;
  }
  return "Could not load project files. Check your connection and try again.";
};

export function ProjectFilesRoot(props: Props) {
  const { workspaceSlug, projectId } = props;
  const [files, setFiles] = useState<TProjectFile[]>([]);
  const [totalCount, setTotalCount] = useState(0);
  const [nextCursor, setNextCursor] = useState<string>();
  const [hasMore, setHasMore] = useState(false);
  const [isLoading, setIsLoading] = useState(true);
  const [isLoadingMore, setIsLoadingMore] = useState(false);
  const [error, setError] = useState<string>();

  const loadFiles = useCallback(async () => {
    setIsLoading(true);
    setError(undefined);
    try {
      const response = await projectFileService.getProjectFiles(workspaceSlug, projectId);
      setFiles(response.results ?? []);
      setTotalCount(response.total_count ?? response.results?.length ?? 0);
      setNextCursor(response.next_cursor);
      setHasMore(!!response.next_page_results);
    } catch (loadError) {
      setError(getErrorMessage(loadError));
    } finally {
      setIsLoading(false);
    }
  }, [projectId, workspaceSlug]);

  useEffect(() => {
    void loadFiles();
  }, [loadFiles]);

  const loadMore = async () => {
    if (!nextCursor || isLoadingMore) return;
    setIsLoadingMore(true);
    setError(undefined);
    try {
      const response = await projectFileService.getProjectFiles(workspaceSlug, projectId, nextCursor);
      setFiles((current) => mergeProjectFilePages(current, response.results ?? []));
      setTotalCount(response.total_count ?? totalCount);
      setNextCursor(response.next_cursor);
      setHasMore(!!response.next_page_results);
    } catch (loadError) {
      setError(getErrorMessage(loadError));
    } finally {
      setIsLoadingMore(false);
    }
  };

  return (
    <div className="mx-auto flex h-full w-full max-w-6xl flex-col gap-5 px-4 py-6 sm:px-6">
      <div className="flex flex-wrap items-end justify-between gap-3">
        <div>
          <div className="flex items-center gap-2">
            <FolderOutline className="size-5 text-secondary" />
            <h1 className="text-h4-medium text-primary">Files</h1>
          </div>
          <p className="mt-1 text-body-sm-regular text-tertiary">
            Uploaded issue attachments from this project, newest first.
          </p>
        </div>
        {!isLoading && !error && <span className="text-body-sm-medium text-tertiary">{totalCount} files</span>}
      </div>

      {isLoading ? (
        <div
          className="overflow-hidden rounded-md border border-subtle bg-surface-1"
          aria-label="Loading project files"
        >
          {["first", "second", "third", "fourth", "fifth"].map((placeholder) => (
            <div key={placeholder} className="h-14 animate-pulse border-b border-subtle bg-surface-2 last:border-b-0" />
          ))}
        </div>
      ) : error && files.length === 0 ? (
        <div className="flex min-h-56 flex-col items-center justify-center gap-3 rounded-md border border-subtle bg-surface-1 p-6 text-center">
          <p className="text-body-sm-regular text-danger-primary">{error}</p>
          <button
            type="button"
            className="flex items-center gap-1.5 rounded-sm border border-subtle px-3 py-1.5 text-body-sm-medium text-secondary hover:bg-surface-2"
            onClick={() => void loadFiles()}
          >
            <RefreshOutline className="size-4" />
            Retry
          </button>
        </div>
      ) : files.length === 0 ? (
        <div className="flex min-h-64 flex-col items-center justify-center gap-2 rounded-md border border-subtle bg-surface-1 p-6 text-center">
          <FolderOutline className="size-8 text-tertiary" />
          <p className="text-body-sm-medium text-primary">No uploaded files yet</p>
          <p className="max-w-sm text-body-sm-regular text-tertiary">
            Files uploaded as issue attachments will appear here with a link to their issue.
          </p>
        </div>
      ) : (
        <div className="overflow-hidden rounded-md border border-subtle bg-surface-1">
          <div className="hidden grid-cols-[minmax(0,2fr)_minmax(0,1.5fr)_120px_140px_88px] gap-3 border-b border-subtle px-4 py-2 text-11 font-medium text-tertiary md:grid">
            <span>File</span>
            <span>Issue</span>
            <span>Size</span>
            <span>Uploaded</span>
            <span className="text-right">Download</span>
          </div>
          {files.map((file) => {
            const issueHref = getProjectFileIssueHref(workspaceSlug, projectId, file.issue_id);
            const downloadHref = getFileURL(file.url);
            const fileIcon = getFileIcon(getFileExtension(file.name), 20);

            return (
              <div
                key={file.id}
                className="grid gap-3 border-b border-subtle px-4 py-3 last:border-b-0 md:grid-cols-[minmax(0,2fr)_minmax(0,1.5fr)_120px_140px_88px] md:items-center"
              >
                <div className="flex min-w-0 items-center gap-3">
                  <span className="shrink-0">{fileIcon}</span>
                  <div className="min-w-0">
                    <p className="truncate text-body-sm-medium text-primary" title={file.name}>
                      {file.name}
                    </p>
                    <p className="truncate text-11 text-tertiary md:hidden">
                      {file.issue_identifier ?? "Project file"} · {convertBytesToSize(file.size)}
                    </p>
                  </div>
                </div>

                <div className="min-w-0 text-body-sm-regular">
                  {issueHref ? (
                    <Link className="block truncate text-secondary hover:text-accent-primary" href={issueHref}>
                      <span className="font-medium">{file.issue_identifier}</span>
                      <span className="text-tertiary"> · {file.issue_name}</span>
                    </Link>
                  ) : (
                    <span className="text-tertiary">Issue unavailable</span>
                  )}
                </div>

                <span className="hidden text-body-sm-regular text-tertiary md:block">
                  {convertBytesToSize(file.size)}
                </span>
                <span className="hidden text-body-sm-regular text-tertiary md:block">
                  {renderFormattedDate(file.created_at)}
                </span>

                {downloadHref ? (
                  <a
                    href={downloadHref}
                    aria-label={`Download ${file.name}`}
                    className="flex w-fit items-center gap-1.5 rounded-sm px-2 py-1 text-body-sm-medium text-secondary hover:bg-surface-2 hover:text-primary md:ml-auto"
                  >
                    <DownloadOutline className="size-4" />
                    Download
                  </a>
                ) : (
                  <span className="text-body-sm-regular text-tertiary md:text-right">Unavailable</span>
                )}
              </div>
            );
          })}
        </div>
      )}

      {!isLoading && files.length > 0 && (
        <div className="flex flex-col items-center gap-2">
          {error && <p className="text-body-sm-regular text-danger-primary">{error}</p>}
          {hasMore && (
            <button
              type="button"
              className="rounded-sm border border-subtle px-3 py-1.5 text-body-sm-medium text-secondary hover:bg-surface-2 disabled:opacity-60"
              disabled={isLoadingMore}
              onClick={() => void loadMore()}
            >
              {isLoadingMore ? "Loading more..." : "Load more files"}
            </button>
          )}
        </div>
      )}
    </div>
  );
}
