import { ChatSyncError } from "./errors.ts";

export type ProgressKey = "done" | "now" | "waiting";

export type Progress = Record<ProgressKey, string[]>;

type TurnItem = {
  type?: unknown;
  text?: unknown;
  phase?: unknown;
};

const PROGRESS_KEYS: ProgressKey[] = ["done", "now", "waiting"];
const HEADER_LABELS: Record<ProgressKey, string> = {
  done: "Done:",
  now: "Now:",
  waiting: "Waiting:",
};
const MAX_FINAL_TEXT_LENGTH = 200_000;
const MAX_ITEMS_PER_SECTION = 50;
const MAX_ITEM_LENGTH = 1_000;

const SECRET_PATTERNS = [
  /\bsk-[A-Za-z0-9_-]{20,}\b/,
  /\bgh[pousr]_[A-Za-z0-9]{20,}\b/,
  /\bAKIA[0-9A-Z]{16}\b/,
  /\bxox[abprs]-[A-Za-z0-9-]{10,}\b/,
  /\beyJ[A-Za-z0-9_-]{10,}\.[A-Za-z0-9_-]{10,}\.[A-Za-z0-9_-]{10,}\b/,
  /-----BEGIN [A-Z ]*PRIVATE KEY-----/,
  /\bBearer\s+[A-Za-z0-9._~+/-]{16,}=*/i,
  /\b(?:api[_-]?key|secret|password|passwd|token)\b\s*[:=]\s*\S{8,}/i,
];

function emptyProgress(): Progress {
  return { done: [], now: [], waiting: [] };
}

function containsSecret(value: string): boolean {
  return SECRET_PATTERNS.some((pattern) => pattern.test(value));
}

export function getFinalAgentText(items: TurnItem[]): string | null {
  const messages = items.filter((item) => item.type === "agentMessage" && typeof item.text === "string") as Array<
    TurnItem & { text: string }
  >;
  if (messages.length === 0) return null;

  const finalAnswers = messages.filter((item) => item.phase === "final_answer");
  if (finalAnswers.length > 0) {
    return finalAnswers.at(-1)?.text ?? null;
  }

  const unphased = messages.filter((item) => item.phase == null);
  return unphased.at(-1)?.text ?? null;
}

export function parseStatusBlock(finalText: string): Progress {
  if (finalText.length > MAX_FINAL_TEXT_LENGTH) {
    throw new ChatSyncError("status_block_invalid");
  }

  const lines = finalText.replaceAll("\r\n", "\n").split("\n");
  const blocks: string[][] = [];

  for (let index = 0; index < lines.length; index += 1) {
    if (lines[index]?.trim() !== "```plane-status") continue;

    const block: string[] = [];
    let closed = false;
    for (let inner = index + 1; inner < lines.length; inner += 1) {
      if (lines[inner]?.trim() === "```") {
        blocks.push(block);
        index = inner;
        closed = true;
        break;
      }
      block.push(lines[inner] ?? "");
    }
    if (!closed) throw new ChatSyncError("status_block_invalid");
  }

  if (blocks.length === 0) throw new ChatSyncError("status_block_missing");
  if (blocks.length > 1) throw new ChatSyncError("status_block_invalid");

  const progress = emptyProgress();
  let currentKey: ProgressKey | null = null;
  let headerIndex = -1;

  for (const rawLine of blocks[0] ?? []) {
    const line = rawLine.trim();
    if (!line) continue;

    const nextHeaderIndex = headerIndex + 1;
    const expectedKey = PROGRESS_KEYS[nextHeaderIndex];
    if (expectedKey && line === HEADER_LABELS[expectedKey]) {
      currentKey = expectedKey;
      headerIndex = nextHeaderIndex;
      continue;
    }

    if (!currentKey || !line.startsWith("- ")) {
      throw new ChatSyncError("status_block_invalid");
    }

    const item = line.slice(2).trim();
    if (!item || item.length > MAX_ITEM_LENGTH) {
      throw new ChatSyncError("status_block_invalid");
    }
    if (containsSecret(item)) {
      throw new ChatSyncError("status_secret_detected");
    }
    if (progress[currentKey].length >= MAX_ITEMS_PER_SECTION) {
      throw new ChatSyncError("status_block_invalid");
    }
    progress[currentKey].push(item);
  }

  if (headerIndex !== PROGRESS_KEYS.length - 1) {
    throw new ChatSyncError("status_block_invalid");
  }

  return progress;
}
