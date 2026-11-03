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
  parseT3CodeThreadLink,
  parseT3ThreadInput,
  type TParsedT3CodeThreadLink,
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
  const [pasteValue, setPasteValue] = useState("");
  const [pasteNotice, setPasteNotice] = useState<string>();
  const [validationError, setValidationError] = useState<string>();
  const nativeCodexUrl = provider === "codex" ? getCodexThreadDeepLink(threadId) : undefined;
  const parsedT3Link = parseT3CodeThreadLink(t3Url);

  const applyT3Reference = (reference: TParsedT3CodeThreadLink) => {
    setProvider("codex");
    if (reference.codexThreadId) setThreadId(reference.codexThreadId);
    setT3Url(reference.url);
    if (reference.title) setTitle(reference.title);
    setValidationError(undefined);
    setPasteNotice(
      reference.codexThreadId
        ? "Codex thread ID and T3 Code link filled."
        : "T3 Code link filled, but it does not include a Codex thread ID."
    );
  };

  const handlePasteValueChange = (nextValue: string) => {
    setPasteValue(nextValue);
    const parsed = parseT3ThreadInput(nextValue);
    if (!parsed) {
      setPasteNotice(undefined);
      return;
    }
    if (parsed.kind === "codex") {
      setProvider("codex");
      setThreadId(parsed.codexThreadId);
      setValidationError(undefined);
      setPasteNotice("Codex thread ID filled.");
      return;
    }
    if (parsed.kind === "t3") {
      applyT3Reference(parsed.reference);
      return;
    }
    if (parsed.codexThreadId) {
      setProvider("codex");
      setThreadId(parsed.codexThreadId);
    }
    setPasteNotice("This is a T3 context link, not a thread link. Use Copy thread in T3, then paste the new link.");
  };

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
    if (normalizedT3Url.includes("t3-context:")) {
      setValidationError(
        "This is a T3 context link, not a thread link. Use Copy thread in T3, then paste the new link."
      );
      return;
    }
    const t3UrlError = getIssueChatT3UrlValidationError(normalizedT3Url);
    if (t3UrlError) {
      setValidationError(t3UrlError);
      return;
    }
    const currentT3Link = parseT3CodeThreadLink(normalizedT3Url);
    if (
      currentT3Link?.codexThreadId &&
      currentT3Link.codexThreadId.toLowerCase() !== normalizedThreadId.toLowerCase()
    ) {
      setValidationError("The T3 Code link belongs to a different Codex thread.");
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

      <InputField
        id="chat-binding-t3-paste"
        type="text"
        size="md"
        orientation="vertical"
        label="Paste from T3"
        value={pasteValue}
        onChange={(event) => handlePasteValueChange(event.target.value)}
        placeholder="Copy thread in T3, then paste the link here"
        hint="Accepts a T3 thread link, a legacy T3 context link, or a Codex UUID."
        disabled={isSubmitting}
      />
      {pasteNotice && <p className="text-11 text-tertiary">{pasteNotice}</p>}

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
          onChange={(event) => {
            const nextValue = event.target.value;
            const parsed = parseT3ThreadInput(nextValue);
            if (parsed?.kind === "t3") {
              setPasteValue(nextValue);
              applyT3Reference(parsed.reference);
              return;
            }
            if (parsed?.kind === "context") {
              setPasteValue(nextValue);
              handlePasteValueChange(nextValue);
              return;
            }
            setThreadId(nextValue);
          }}
          placeholder="Codex UUID, or paste a T3 thread link"
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
          onChange={(event) => {
            const nextValue = event.target.value;
            setT3Url(nextValue);
            const parsed = parseT3CodeThreadLink(nextValue);
            if (parsed) applyT3Reference(parsed);
          }}
          placeholder="t3code://threads/<environment>/<thread>?codexThreadId=<uuid>"
          hint="Optional. Paste a T3 thread link; its Codex ID fills automatically."
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

      {parsedT3Link && (
        <p className="text-11 text-tertiary">
          Codex {parsedT3Link.codexThreadId ?? "not included"} · T3 {parsedT3Link.threadId} · env{" "}
          {parsedT3Link.environmentId}
        </p>
      )}

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
