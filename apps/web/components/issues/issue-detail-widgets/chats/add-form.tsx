/**
 * Copyright (c) 2023-present Plane Software, Inc. and contributors
 * SPDX-License-Identifier: AGPL-3.0-only
 * See the LICENSE file for details.
 */

import { useState } from "react";
import { Button } from "@makeplane/propel/components/button";
import { InputField } from "@makeplane/propel/components/input-field";
import { Select, SelectContent, SelectItem, SelectList, SelectTrigger } from "@makeplane/propel/components/select";
import type { TIssueChatBindingCreate, TIssueChatProvider } from "@plane/types";
// local imports
import { getCodexThreadDeepLink, getIssueChatBindingUrlValidationError } from "./helpers";

const PROVIDER_OPTIONS: { label: string; value: TIssueChatProvider }[] = [
  { label: "Codex", value: "codex" },
  { label: "ChatGPT", value: "chatgpt" },
];

type Props = {
  isSubmitting: boolean;
  error?: string;
  onCancel: () => void;
  onSubmit: (data: TIssueChatBindingCreate) => Promise<void>;
};

export function IssueChatBindingAddForm(props: Props) {
  const { isSubmitting, error, onCancel, onSubmit } = props;
  const [provider, setProvider] = useState<TIssueChatProvider>("codex");
  const [url, setUrl] = useState("");
  const [threadId, setThreadId] = useState("");
  const [title, setTitle] = useState("");
  const [validationError, setValidationError] = useState<string>();
  const nativeCodexUrl = provider === "codex" ? getCodexThreadDeepLink(threadId) : undefined;

  const handleSubmit = async (event: React.FormEvent<HTMLFormElement>) => {
    event.preventDefault();
    setValidationError(undefined);

    const normalizedUrl = url.trim();
    const normalizedThreadId = threadId.trim();
    const normalizedTitle = title.trim();

    if (!normalizedThreadId) {
      setValidationError("Thread ID is required.");
      return;
    }
    const urlError = getIssueChatBindingUrlValidationError(normalizedUrl, provider, normalizedThreadId);
    if (urlError) {
      setValidationError(urlError);
      return;
    }

    await onSubmit({
      provider,
      thread_id: normalizedThreadId,
      ...(normalizedUrl ? { url: normalizedUrl } : {}),
      ...(normalizedTitle ? { title: normalizedTitle } : {}),
    });
  };

  return (
    <form onSubmit={handleSubmit} className="space-y-3 rounded-md border border-subtle bg-surface-2 p-3">
      <div className="space-y-1">
        <p className="text-body-sm-medium">Add a chat binding</p>
        <p className="text-11 text-tertiary">
          Binding stores a chat link and identifiers. It does not sync or start a chat automatically.
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
            disabled={isSubmitting}
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
          disabled={isSubmitting}
          maxLength={255}
          required
        />

        <InputField
          id="chat-binding-url"
          type="url"
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
        <Button type="submit" variant="primary" size="sm" stretch="auto" label="Add binding" loading={isSubmitting} />
      </div>
    </form>
  );
}
