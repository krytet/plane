/**
 * Copyright (c) 2023-present Plane Software, Inc. and contributors
 * SPDX-License-Identifier: AGPL-3.0-only
 * See the LICENSE file for details.
 */

import { useState } from "react";
import { Button } from "@makeplane/propel/components/button";
import { InputField } from "@makeplane/propel/components/input-field";
import { Select, SelectContent, SelectItem, SelectList, SelectTrigger } from "@makeplane/propel/components/select";
import type { TIssueChatBinding, TIssueChatBindingCreate, TIssueChatProvider } from "@plane/types";
// local imports
import {
  getCodexThreadDeepLink,
  getIssueChatBindingUrlValidationError,
  getIssueChatT3UrlValidationError,
} from "./helpers";

const PROVIDER_OPTIONS: { label: string; value: TIssueChatProvider }[] = [
  { label: "Codex", value: "codex" },
  { label: "ChatGPT", value: "chatgpt" },
];

type Props = {
  isSubmitting: boolean;
  error?: string;
  initialBinding?: TIssueChatBinding;
  onCancel: () => void;
  onSubmit: (data: TIssueChatBindingCreate) => Promise<void>;
};

export function IssueChatBindingAddForm(props: Props) {
  const { isSubmitting, error, initialBinding, onCancel, onSubmit } = props;
  const isEditing = initialBinding !== undefined;
  const [provider, setProvider] = useState<TIssueChatProvider>(initialBinding?.provider ?? "codex");
  const [url, setUrl] = useState(initialBinding?.link?.url ?? "");
  const [threadId, setThreadId] = useState(initialBinding?.thread_id ?? "");
  const [title, setTitle] = useState(initialBinding?.link?.title ?? "");
  const [t3Url, setT3Url] = useState(initialBinding?.t3_url ?? "");
  const [validationError, setValidationError] = useState<string>();
  const nativeCodexUrl = provider === "codex" ? getCodexThreadDeepLink(threadId) : undefined;

  const handleSubmit = async (event: React.FormEvent<HTMLFormElement>) => {
    event.preventDefault();
    setValidationError(undefined);

    const normalizedUrl = url.trim();
    const normalizedThreadId = threadId.trim();
    const normalizedTitle = title.trim();
    const normalizedT3Url = t3Url.trim();

    if (!normalizedThreadId) {
      setValidationError("Thread ID is required.");
      return;
    }
    const urlError = getIssueChatBindingUrlValidationError(normalizedUrl, provider, normalizedThreadId);
    if (urlError) {
      setValidationError(urlError);
      return;
    }
    const t3UrlError = getIssueChatT3UrlValidationError(normalizedT3Url);
    if (t3UrlError) {
      setValidationError(t3UrlError);
      return;
    }

    const data: TIssueChatBindingCreate = {
      provider,
      thread_id: normalizedThreadId,
      ...(normalizedUrl ? { url: normalizedUrl } : {}),
      ...(normalizedTitle ? { title: normalizedTitle } : {}),
      ...(normalizedT3Url ? { t3_url: normalizedT3Url } : isEditing ? { t3_url: "" } : {}),
    };
    await onSubmit(data);
  };

  return (
    <form onSubmit={handleSubmit} className="space-y-3 rounded-md border border-subtle bg-surface-2 p-3">
      <div className="space-y-1">
        <p className="text-body-sm-medium">{isEditing ? "Edit chat links" : "Add a chat binding"}</p>
        <p className="text-11 text-tertiary">
          Binding stores Codex/ChatGPT identifiers and an optional T3 Code link. It does not sync or start a chat
          automatically.
        </p>
      </div>

      <div className="grid grid-cols-1 gap-3 sm:grid-cols-2">
        <div className="space-y-1">
          <label htmlFor="chat-binding-provider" className="text-11 font-medium text-secondary">
            Provider
          </label>
          <Select<TIssueChatProvider>
            items={PROVIDER_OPTIONS}
            value={provider}
            onValueChange={(value) => {
              if (value) setProvider(value);
            }}
            disabled={isSubmitting || isEditing}
          >
            <SelectTrigger id="chat-binding-provider" size="md" />
            <SelectContent side="bottom" align="start">
              <SelectList>
                {PROVIDER_OPTIONS.map((option) => (
                  <SelectItem key={option.value} value={option.value} size="md" label={option.label} />
                ))}
              </SelectList>
            </SelectContent>
          </Select>
        </div>

        <InputField
          id="chat-binding-thread-id"
          type="text"
          size="md"
          orientation="vertical"
          label="Thread ID"
          value={threadId}
          onChange={(event) => setThreadId(event.target.value)}
          placeholder="Provider thread identifier or Codex UUID"
          disabled={isSubmitting || isEditing}
          maxLength={255}
          required
        />

        <InputField
          id="chat-binding-url"
          type="text"
          size="md"
          orientation="vertical"
          label="URL"
          value={url}
          onChange={(event) => setUrl(event.target.value)}
          placeholder={nativeCodexUrl ?? "https://chatgpt.com/c/..."}
          hint={nativeCodexUrl ? "Optional for a local Codex UUID; blank uses the native link." : "Required"}
          disabled={isSubmitting}
          required={!nativeCodexUrl}
        />

        <InputField
          id="chat-binding-t3-url"
          type="text"
          size="md"
          orientation="vertical"
          label="T3 Code link"
          value={t3Url}
          onChange={(event) => setT3Url(event.target.value)}
          placeholder="t3code://threads/<environment>/<thread>"
          hint="Optional. Use the T3 Code thread link for the same chat."
          disabled={isSubmitting}
          maxLength={2048}
        />

        <InputField
          id="chat-binding-title"
          type="text"
          size="md"
          orientation="vertical"
          label="Title"
          hint="Optional"
          value={title}
          onChange={(event) => setTitle(event.target.value)}
          placeholder="Display title"
          disabled={isSubmitting}
          maxLength={255}
        />
      </div>

      {(validationError || error) && (
        <p role="alert" className="text-11 text-danger-primary">
          {validationError ?? error}
        </p>
      )}

      <div className="flex items-center justify-end gap-2">
        <Button
          type="button"
          variant="secondary"
          size="sm"
          stretch="auto"
          label="Cancel"
          disabled={isSubmitting}
          onClick={onCancel}
        />
        <Button
          type="submit"
          variant="primary"
          size="sm"
          stretch="auto"
          label={isEditing ? "Save binding" : "Add binding"}
          loading={isSubmitting}
        />
      </div>
    </form>
  );
}
