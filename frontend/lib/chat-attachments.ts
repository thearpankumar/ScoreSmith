// Pure logic for files attached in the chat composer (backend app/api/v1/chat_attachments.py): validation
// against the server's limits, the per-file chip state, and how a file's extraction outline is summarised.
// No runtime imports, so node:test loads it directly.

import type { ChatAttachmentLimits } from "./chat-models";

export type ComposerAttachmentStatus = "uploading" | "reading" | "ready" | "failed";

/** One file in the composer, from pick to "ready" (uploaded and its text extracted by the server). */
export interface ComposerAttachment {
  localId: string;
  name: string;
  size: number;
  status: ComposerAttachmentStatus;
  /** 0..1 while uploading. */
  progress: number;
  error: string | null;
  /** The server's attachment id once the upload was created. */
  serverId: string | null;
  /** What the server extracted (pages / sheets / rows / truncated), once ready. */
  outline: Record<string, unknown> | null;
}

export const CHAT_FILE_ACCEPT = ".pdf,.docx,.xlsx,.csv,.txt,.md,.png,.jpg,.jpeg,.webp";

export function fileExtension(name: string): string {
  const i = name.lastIndexOf(".");
  return i < 0 ? "" : name.slice(i + 1).toLowerCase();
}

/** Why `file` cannot be attached (null when it can). */
export function validateChatFile(
  file: { name: string; size: number },
  limits: ChatAttachmentLimits,
  existingCount: number,
): string | null {
  if (!limits.enabled) return "File attachments are not available.";
  if (existingCount >= limits.maxFiles) return `At most ${limits.maxFiles} files per message.`;
  const ext = fileExtension(file.name);
  if (!limits.extensions.includes(ext)) {
    return `${file.name}: unsupported type. Allowed: ${limits.extensions.join(", ")}.`;
  }
  if (file.size <= 0) return `${file.name} is empty.`;
  if (limits.maxBytes > 0 && file.size > limits.maxBytes) {
    return `${file.name} is larger than ${Math.round(limits.maxBytes / (1024 * 1024))} MB.`;
  }
  return null;
}

/** Ids to send with the message: only files that are ready. */
export function readyAttachmentIds(items: ComposerAttachment[]): string[] {
  return items.filter((i) => i.status === "ready" && i.serverId).map((i) => i.serverId as string);
}

/** True while any file is still uploading or being read (sending would leave it behind). */
export function attachmentsPending(items: ComposerAttachment[]): boolean {
  return items.some((i) => i.status === "uploading" || i.status === "reading");
}

/** A short description of what was extracted: "12 pages", "2 sheets, 340 rows", "cut to fit"... */
export function outlineSummary(outline: Record<string, unknown> | null | undefined): string | null {
  if (!outline) return null;
  const parts: string[] = [];
  const sheets = Array.isArray(outline.sheets) ? (outline.sheets as { rows?: number }[]) : null;
  if (sheets && sheets.length > 0) {
    const rows = sheets.reduce((sum, s) => sum + (typeof s.rows === "number" ? s.rows : 0), 0);
    parts.push(`${sheets.length} sheet${sheets.length === 1 ? "" : "s"}, ${rows} row${rows === 1 ? "" : "s"}`);
  } else if (typeof outline.pages === "number" && outline.pages > 0) {
    parts.push(`${outline.pages} page${outline.pages === 1 ? "" : "s"}`);
  }
  if (Array.isArray(outline.scanned_pages) && outline.scanned_pages.length > 0) parts.push("scanned");
  if (outline.truncated === true) parts.push("long: the assistant reads it in parts");
  return parts.length ? parts.join(", ") : null;
}
