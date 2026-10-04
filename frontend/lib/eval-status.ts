import type { Evaluation, EvaluationStatus } from "./types";

/** Statuses during which the backend is still working on the evaluation (worth polling). */
export function isActiveStatus(status: EvaluationStatus): boolean {
  return status === "queued" || status === "ingesting" || status === "processing" || status === "scoring";
}

/**
 * True when the result page should show the live progress view instead of the result: any
 * in-flight AI run, or a failed AI run (it carries an error_code; legacy manual/sync failures do not).
 */
export function showsProgressView(e: Pick<Evaluation, "status" | "errorCode">): boolean {
  return isActiveStatus(e.status) || (e.status === "failed" && !!e.errorCode);
}

export interface StatusLabel {
  label: string;
  tone: "muted" | "lemon" | "soft" | "danger" | "ok";
}

/** Badge text for a non-completed evaluation. `queuePosition` is 1-based when known. */
export function statusLabel(
  e: Pick<Evaluation, "status" | "stage" | "errorCode">,
  queuePosition?: number | null,
): StatusLabel {
  switch (e.status) {
    case "queued":
      return { label: queuePosition ? `Queued #${queuePosition}` : "Queued", tone: "muted" };
    case "ingesting":
    case "processing": {
      const stage = (e.stage ?? "").toLowerCase();
      if (stage.startsWith("ingest")) return { label: "Fetching", tone: "soft" };
      if (stage.startsWith("transcribe")) return { label: "Transcribing", tone: "soft" };
      return { label: "Extracting", tone: "soft" };
    }
    case "scoring":
      return { label: "Scoring", tone: "lemon" };
    case "completed":
      return { label: "Done", tone: "ok" };
    case "failed":
      return { label: e.errorCode === "cancelled" ? "Cancelled" : e.errorCode ? "Failed" : "Not scored", tone: "danger" };
    case "pending":
      return { label: "Pending", tone: "muted" };
    default:
      return { label: "In progress", tone: "muted" };
  }
}

export const PIPELINE_STEPS = ["Fetch", "Extract", "Transcribe", "Score"] as const;

/**
 * Index of the pipeline step currently running (0-3), 4 when everything is done, -1 when nothing
 * has started (queued). `stage` is progress.json's stage or `scoring:<substep>`.
 */
export function stepIndex(status: EvaluationStatus, stage: string | null | undefined): number {
  if (status === "completed") return 4;
  if (status === "queued" || status === "pending") return -1;
  const s = (stage ?? "").toLowerCase();
  if (status === "scoring" || s.startsWith("scoring") || s === "done") return 3;
  if (s.startsWith("ingest")) return 0;
  if (s.startsWith("extract") || s.startsWith("analyze")) return 1;
  if (s.startsWith("transcribe") || s.startsWith("assemble")) return 2;
  if (status === "ingesting" || status === "processing") return 0;
  return -1;
}

export interface ErrorInfo {
  title: string;
  hint: string;
}

const ERRORS: Record<string, ErrorInfo> = {
  drive_invalid: {
    title: "That doesn't look like a valid Google Drive link",
    hint: "Check the link points to a Drive file, folder or Google Doc, then submit again.",
  },
  drive_inaccessible: {
    title: "Couldn't open the Google Drive link",
    hint: "Make the Drive link public (anyone with the link can view) or upload the files instead.",
  },
  drive_quota: {
    title: "Google Drive is temporarily refusing downloads for this link",
    hint: "Drive's download quota was exceeded. Retry in a while, or upload the files instead.",
  },
  drive_empty: {
    title: "The Drive link has nothing we can evaluate",
    hint: "The folder or file was empty or only held unsupported files. Check the link, or upload the files instead.",
  },
  file_too_large: {
    title: "A file is larger than the 2 GiB limit",
    hint: "Compress or trim the file (for example export a shorter video) and submit again.",
  },
  unsupported_type: {
    title: "None of the files are a supported type",
    hint: "Supported: PDF, DOCX, Markdown/text and video (mp4, mov, mkv, webm, m4v).",
  },
  extract_failed: {
    title: "Couldn't read the submitted files",
    hint: "A file may be corrupt or password-protected. Re-export it and retry, or upload a different copy.",
  },
  no_content: {
    title: "No usable content was found",
    hint: "The files had no extractable text, images or speech. Check them and try again.",
  },
  timeout: {
    title: "Processing took too long",
    hint: "Retry the evaluation; if it keeps happening, try smaller or fewer files.",
  },
  cancelled: {
    title: "This evaluation was cancelled",
    hint: "You can retry it to run it again from the start.",
  },
  scoring_failed: {
    title: "The AI judge couldn't finish scoring",
    hint: "The AI service had a problem. Retry the evaluation in a moment.",
  },
  internal: {
    title: "Something went wrong on our side",
    hint: "Retry the evaluation. If it keeps failing, contact support with this evaluation's id.",
  },
};

export function errorInfo(code: string | null | undefined): ErrorInfo {
  return (code && ERRORS[code]) || ERRORS.internal;
}
