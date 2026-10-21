export type ChatSyncErrorCode =
  | "config_invalid"
  | "config_disabled"
  | "unsupported_provider"
  | "cli_incompatible"
  | "protocol_incompatible"
  | "app_server_failed"
  | "thread_not_found"
  | "status_block_missing"
  | "status_block_invalid"
  | "status_secret_detected"
  | "plane_auth_failed"
  | "plane_read_failed"
  | "plane_write_uncertain"
  | "plane_write_not_applied"
  | "reconciliation_conflict"
  | "state_invalid";

export class ChatSyncError extends Error {
  readonly code: ChatSyncErrorCode;

  constructor(code: ChatSyncErrorCode, message?: string) {
    super(message ?? code);
    this.name = "ChatSyncError";
    this.code = code;
  }
}

export function codeForError(error: unknown): ChatSyncErrorCode {
  return error instanceof ChatSyncError ? error.code : "app_server_failed";
}
