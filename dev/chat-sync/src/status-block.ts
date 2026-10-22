import { ChatSyncError } from "./errors.ts";
import type { ProgressMode } from "./config.ts";

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
const MAX_SUMMARY_TEXT_LENGTH = 300;
const SUMMARY_LABEL = "Последний ответ Codex (кратко): ";
const SUMMARY_ITEM_LIMIT = SUMMARY_LABEL.length + MAX_SUMMARY_TEXT_LENGTH;

const SECRET_PATTERNS = [
  /\bsk-[A-Za-z0-9_-]{20,}\b/,
  /\bgh[pousr]_[A-Za-z0-9]{20,}\b/,
  /\bAKIA[0-9A-Z]{16}\b/,
  /\bxox[abprs]-[A-Za-z0-9-]{10,}\b/,
  /\beyJ[A-Za-z0-9_-]{10,}\.[A-Za-z0-9_-]{10,}\.[A-Za-z0-9_-]{10,}\b/,
  /\bplane_api_[A-Za-z0-9_-]{16,}\b/,
  /-----BEGIN [A-Z ]*PRIVATE KEY-----/,
  /\bBearer\s+[A-Za-z0-9._~+/-]{16,}=*/i,
  /\b(?:api[_-]?key|secret|password|passwd|token)\b\s*[:=]\s*\S{8,}/i,
  /\b(?:[A-Z][A-Z0-9_]*)?(?:SECRET|TOKEN|PASSWORD|PASSWD|PRIVATE_KEY|ACCESS_KEY|API_KEY)(?:[A-Z0-9_]*)?\s*[:=]\s*["']?[^\s"']{8,}/i,
  /\b[a-z][a-z0-9+.-]*:\/\/[^\s/:@]+:[^\s/@]+@[^\s"']+/i,
];

function emptyProgress(): Progress {
  return { done: [], now: [], waiting: [] };
}

export function containsSecret(value: string): boolean {
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
  if (containsSecret(finalText)) {
    throw new ChatSyncError("status_secret_detected");
  }

  const lines = finalText.replaceAll("\r\n", "\n").split("\n");
  const blocks: string[][] = [];

  for (let index = 0; index < lines.length; index += 1) {
    const line = lines[index] ?? "";
    if (!line.includes("```plane-status")) continue;
    if (line.trim() !== "```plane-status") throw new ChatSyncError("status_block_invalid");

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

function stripFencedCode(value: string): string {
  const lines = value.replaceAll("\r\n", "\n").split("\n");
  const kept: string[] = [];
  let inFence = false;

  for (const line of lines) {
    if (line.trim().startsWith("```")) {
      inFence = !inFence;
      continue;
    }
    if (!inFence) kept.push(line);
  }
  return kept.join("\n");
}

function cleanSummaryLine(value: string): string {
  return value
    .replace(/<https?:\/\/[^>\s]+>/gi, " ")
    .replace(/!\[[^\]]*\]\([^)]*\)/g, " ")
    .replace(/\[([^\]]+)\]\([^)]*\)/g, "$1")
    .replace(/`([^`\n]+)`/g, (_match, inline: string) => (/[/\\]/.test(inline) ? " " : inline))
    .replace(/\b(?:https?|file):\/\/\S+/gi, " ")
    .replace(/\bwww\.\S+/gi, " ")
    .replace(/\b[A-Za-z]:\\[^\s]+/g, " ")
    .replace(/(?:^|\s)\/(?:[^\s/]+\/)+[^\s]*/g, " ")
    .replace(/<[^>]+>/g, " ")
    .replace(/^\s{0,3}(?:#{1,6}|>|[-+*]|\d+[.)])\s+/g, "")
    .replace(/[*_~]/g, "")
    .replace(/\|/g, " ")
    .replace(/\s+/g, " ")
    .trim();
}

function truncateSummary(value: string): string {
  if (value.length <= MAX_SUMMARY_TEXT_LENGTH) return value;
  const candidate = value.slice(0, MAX_SUMMARY_TEXT_LENGTH - 1);
  const boundary = Math.max(candidate.lastIndexOf(". "), candidate.lastIndexOf("; "), candidate.lastIndexOf(" "));
  const truncated = candidate.slice(0, boundary > MAX_SUMMARY_TEXT_LENGTH / 2 ? boundary : candidate.length).trimEnd();
  return `${truncated}…`;
}

function extractFallbackSummary(finalText: string): string {
  const cleaned = stripFencedCode(finalText)
    .split("\n")
    .map(cleanSummaryLine)
    .filter((line) => line.length >= 12)
    .join(" ");

  const sentences = cleaned
    .split(/(?<=[.!?])\s+/)
    .map((sentence) => sentence.trim())
    .filter(Boolean);
  const selected: string[] = [];

  for (const sentence of sentences.slice(0, 2)) {
    const next = [...selected, sentence].join(" ");
    if (next.length > MAX_SUMMARY_TEXT_LENGTH && selected.length > 0) break;
    selected.push(sentence);
  }

  const summary = truncateSummary(selected.join(" "));
  return summary || "No safe prose summary was available in the latest answer.";
}

export function parseProgressFromFinalText(
  finalText: string,
  mode: ProgressMode = "status_block"
): { progress: Progress; source: "status_block" | "summary" } {
  if (finalText.length > MAX_FINAL_TEXT_LENGTH) {
    throw new ChatSyncError("status_block_invalid");
  }
  if (containsSecret(finalText)) {
    throw new ChatSyncError("status_secret_detected");
  }

  if (finalText.includes("```plane-status")) {
    return { progress: parseStatusBlock(finalText), source: "status_block" };
  }
  if (mode === "status_block") {
    throw new ChatSyncError("status_block_missing");
  }

  const summary = `${SUMMARY_LABEL}${extractFallbackSummary(finalText)}`;
  if (summary.length > SUMMARY_ITEM_LIMIT) {
    throw new ChatSyncError("status_block_invalid");
  }
  return {
    progress: {
      done: [],
      now: [summary],
      waiting: [],
    },
    source: "summary",
  };
}
