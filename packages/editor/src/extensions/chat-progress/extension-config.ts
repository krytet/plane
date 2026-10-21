/**
 * Copyright (c) 2023-present Plane Software, Inc. and contributors
 * SPDX-License-Identifier: AGPL-3.0-only
 * See the LICENSE file for details.
 */

import { mergeAttributes, Node } from "@tiptap/core";
// constants
import { CORE_EXTENSIONS } from "@/constants/extension";

export const CHAT_PROGRESS_BLOCK_TYPE = "chat-progress";

type TChatProgress = {
  done: string[];
  now: string[];
  waiting: string[];
};

const EMPTY_PROGRESS: TChatProgress = {
  done: [],
  now: [],
  waiting: [],
};

const PROGRESS_LABELS = [
  ["done", "Done"],
  ["now", "Now"],
  ["waiting", "Waiting"],
] as const;

const parseProgressItems = (element: HTMLElement): TChatProgress => {
  const progress: TChatProgress = { ...EMPTY_PROGRESS };

  for (const paragraph of Array.from(element.querySelectorAll("p"))) {
    const label = paragraph.querySelector("strong")?.textContent?.replace(":", "").trim().toLowerCase();
    if (label !== "done" && label !== "now" && label !== "waiting") continue;

    const value = paragraph.textContent?.replace(/^[^:]+:\s*/, "").trim() ?? "";
    progress[label] = value && value !== "—" ? value.split(" · ").filter(Boolean) : [];
  }

  return progress;
};

const renderProgressValue = (items: string[]) => (items.length > 0 ? items.join(" · ") : "—");

const getProgressAttributes = (element: HTMLElement) => ({
  id: element.getAttribute("id"),
  progress: parseProgressItems(element),
});

const renderChatProgressElement = (progress: TChatProgress) => {
  const element = document.createElement("div");
  element.setAttribute("data-node-type", CHAT_PROGRESS_BLOCK_TYPE);
  element.setAttribute("contenteditable", "false");
  element.className = "my-3 space-y-1 rounded-md border border-subtle bg-surface-2 px-3 py-2";

  for (const [key, label] of PROGRESS_LABELS) {
    const row = document.createElement("p");
    row.className = "text-body-xs-regular text-secondary";
    const strong = document.createElement("strong");
    strong.textContent = `${label}:`;
    row.append(strong, document.createTextNode(` ${renderProgressValue(progress[key])}`));
    element.append(row);
  }

  return element;
};

export const ChatProgressExtensionConfig = Node.create({
  name: CORE_EXTENSIONS.CHAT_PROGRESS,
  group: "block",
  atom: true,
  selectable: false,
  draggable: false,
  isolating: true,

  addAttributes() {
    return {
      id: {
        default: null,
        parseHTML: (element) => element.getAttribute("id"),
        renderHTML: (attributes) => (attributes.id ? { id: attributes.id } : {}),
      },
      progress: {
        default: EMPTY_PROGRESS,
        parseHTML: (element) => parseProgressItems(element),
        renderHTML: () => ({}),
      },
    };
  },

  parseHTML() {
    return [
      {
        tag: `div[data-node-type="${CHAT_PROGRESS_BLOCK_TYPE}"]`,
        getAttrs: (element) => getProgressAttributes(element as HTMLElement),
      },
    ];
  },

  renderHTML({ node, HTMLAttributes }) {
    const progress = (node.attrs.progress ?? EMPTY_PROGRESS) as TChatProgress;
    return [
      "div",
      mergeAttributes({ "data-node-type": CHAT_PROGRESS_BLOCK_TYPE }, HTMLAttributes),
      ...PROGRESS_LABELS.map(([key, label]) => [
        "p",
        ["strong", `${label}:`],
        ` ${renderProgressValue(progress[key])}`,
      ]),
    ];
  },

  addNodeView() {
    return ({ node }) => {
      const dom = renderChatProgressElement(node.attrs.progress as TChatProgress);
      if (node.attrs.id) dom.id = node.attrs.id;

      return {
        dom,
        update: (updatedNode) => {
          if (updatedNode.type !== node.type) return false;
          const updatedDom = renderChatProgressElement(updatedNode.attrs.progress as TChatProgress);
          if (updatedNode.attrs.id) dom.id = updatedNode.attrs.id;
          else dom.removeAttribute("id");
          dom.replaceChildren(...Array.from(updatedDom.childNodes));
          return true;
        },
      };
    };
  },
});
