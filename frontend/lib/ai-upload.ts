// Pure, dependency-free logic for the AI-judge inputs: client-side file validation, the chunked
// multipart uploader (part math, bounded concurrency, per-part retry/backoff, ETag collection) and
// Google Drive link classification. Kept free of runtime imports so node:test can load it directly.
import type { AiCompletedPart, AiUploadPart, AiUploadPurpose } from "./types";

export const SUBMISSION_EXTENSIONS = ["pdf", "docx", "md", "markdown", "txt", "mp4", "mov", "mkv", "webm", "m4v"] as const;
export const SHEET_EXTENSIONS = ["xlsx", "csv"] as const;
export const MAX_FILE_BYTES = 2 * 1024 ** 3; // 2 GiB
export const MAX_FILES = 10;
export const UPLOAD_CONCURRENCY = 3;
export const PART_RETRIES = 3;

const CONTENT_TYPES: Record<string, string> = {
  pdf: "application/pdf",
  docx: "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
  md: "text/markdown",
  markdown: "text/markdown",
  txt: "text/plain",
  mp4: "video/mp4",
  mov: "video/quicktime",
  mkv: "video/x-matroska",
  webm: "video/webm",
  m4v: "video/x-m4v",
  xlsx: "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
  csv: "text/csv",
};

export function fileExtension(name: string): string {
  const i = name.lastIndexOf(".");
  return i < 0 ? "" : name.slice(i + 1).toLowerCase();
}

export function contentTypeFor(name: string, fallback = ""): string {
  return CONTENT_TYPES[fileExtension(name)] ?? (fallback || "application/octet-stream");
}

export function formatBytes(n: number): string {
  if (n < 1024) return `${n} B`;
  const units = ["KB", "MB", "GB", "TB"];
  let v = n / 1024;
  let u = 0;
  while (v >= 1024 && u < units.length - 1) {
    v /= 1024;
    u++;
  }
  return `${v >= 100 ? v.toFixed(0) : v.toFixed(1)} ${units[u]}`;
}

/** Returns a human-readable reason the file is not acceptable, or null when it is. */
export function validateUploadFile(
  file: { name: string; size: number },
  purpose: AiUploadPurpose,
  existingCount = 0,
  maxFiles = MAX_FILES,
): string | null {
  const allowed: readonly string[] = purpose === "submission" ? SUBMISSION_EXTENSIONS : SHEET_EXTENSIONS;
  const ext = fileExtension(file.name);
  if (!allowed.includes(ext)) {
    return `Unsupported file type${ext ? ` ".${ext}"` : ""}. Allowed: ${allowed.join(", ")}.`;
  }
  if (file.size <= 0) return "This file is empty.";
  if (file.size > MAX_FILE_BYTES) return `Too large (${formatBytes(file.size)}). The limit is 2 GiB per file.`;
  if (existingCount >= maxFiles) return `At most ${maxFiles} file${maxFiles === 1 ? "" : "s"} per evaluation.`;
  return null;
}

// ---------------------------------------------------------------------------
// Multipart upload
// ---------------------------------------------------------------------------

export interface PartSlice {
  partNumber: number;
  url: string;
  start: number;
  /** Exclusive end offset. */
  end: number;
}

/** Maps the server's presigned parts onto byte ranges of the file (part n = [(n-1)*partSize, n*partSize)). */
export function partSlices(size: number, partSize: number, parts: AiUploadPart[]): PartSlice[] {
  if (!(partSize > 0)) throw new Error("Invalid part size from the server.");
  const sorted = [...parts].sort((a, b) => a.partNumber - b.partNumber);
  const needed = Math.max(1, Math.ceil(size / partSize));
  if (sorted.length < needed) {
    throw new Error(`The server issued ${sorted.length} upload parts but ${needed} are needed.`);
  }
  return sorted.slice(0, needed).map((p) => ({
    partNumber: p.partNumber,
    url: p.url,
    start: (p.partNumber - 1) * partSize,
    end: Math.min(p.partNumber * partSize, size),
  }));
}

export class PartUploadError extends Error {
  readonly retryable: boolean;
  readonly status: number;
  constructor(message: string, opts: { retryable: boolean; status?: number }) {
    super(message);
    this.name = "PartUploadError";
    this.retryable = opts.retryable;
    this.status = opts.status ?? 0;
  }
}

const abortError = () => new DOMException("Upload cancelled.", "AbortError");

/** Capped exponential backoff with +-20% jitter. `attempt` is 0 for the delay before the first retry. */
export function backoffMs(attempt: number, rand: () => number = Math.random): number {
  const base = Math.min(500 * 2 ** attempt, 8000);
  return Math.round(base * (0.8 + 0.4 * rand()));
}

function defaultSleep(ms: number, signal?: AbortSignal): Promise<void> {
  return new Promise<void>((resolve, reject) => {
    if (signal?.aborted) return reject(abortError());
    const onAbort = () => {
      clearTimeout(t);
      reject(abortError());
    };
    const t = setTimeout(() => {
      signal?.removeEventListener("abort", onAbort);
      resolve();
    }, ms);
    signal?.addEventListener("abort", onAbort, { once: true });
  });
}

export type Sleep = (ms: number, signal?: AbortSignal) => Promise<void>;

/** Runs `fn`, retrying retryable failures up to `retries` times with backoff. Aborts are never retried. */
export async function withRetry<T>(
  fn: (attempt: number) => Promise<T>,
  opts: { retries: number; signal?: AbortSignal; sleep?: Sleep; rand?: () => number; onRetry?: (attempt: number, err: unknown) => void },
): Promise<T> {
  const sleep = opts.sleep ?? defaultSleep;
  for (let attempt = 0; ; attempt++) {
    if (opts.signal?.aborted) throw abortError();
    try {
      return await fn(attempt);
    } catch (err) {
      if (opts.signal?.aborted) throw abortError();
      const retryable = !(err instanceof PartUploadError) || err.retryable;
      if (!retryable || attempt >= opts.retries) throw err;
      opts.onRetry?.(attempt, err);
      await sleep(backoffMs(attempt, opts.rand), opts.signal);
    }
  }
}

/** Runs `worker` over `items` with at most `limit` in flight; the first failure stops scheduling and rejects. */
export async function runPool<T>(items: T[], limit: number, worker: (item: T) => Promise<void>): Promise<void> {
  let next = 0;
  let failed: unknown;
  let hasFailed = false;
  const lane = async () => {
    while (!hasFailed && next < items.length) {
      const item = items[next++];
      try {
        await worker(item);
      } catch (err) {
        if (!hasFailed) {
          hasFailed = true;
          failed = err;
        }
      }
    }
  };
  await Promise.all(Array.from({ length: Math.max(1, Math.min(limit, items.length)) }, lane));
  if (hasFailed) throw failed;
}

export type PartPutter = (
  url: string,
  body: Blob,
  opts: { signal?: AbortSignal; onProgress: (loaded: number) => void },
) => Promise<string>;

/** PUTs one part via XHR (fetch has no upload progress) and returns the ETag response header. */
export const xhrPutPart: PartPutter = (url, body, { signal, onProgress }) =>
  new Promise<string>((resolve, reject) => {
    if (signal?.aborted) return reject(abortError());
    const xhr = new XMLHttpRequest();
    const onAbort = () => xhr.abort();
    xhr.open("PUT", url);
    xhr.upload.onprogress = (e) => {
      if (e.lengthComputable) onProgress(e.loaded);
    };
    xhr.onload = () => {
      signal?.removeEventListener("abort", onAbort);
      if (xhr.status >= 200 && xhr.status < 300) {
        const etag = xhr.getResponseHeader("ETag");
        if (!etag) {
          reject(
            new PartUploadError(
              "Upload storage did not return an ETag (check the bucket CORS ExposeHeaders).",
              { retryable: false, status: xhr.status },
            ),
          );
        } else resolve(etag);
        return;
      }
      const status = xhr.status;
      reject(
        new PartUploadError(`Upload failed (HTTP ${status}).`, {
          retryable: status === 408 || status === 429 || status >= 500,
          status,
        }),
      );
    };
    xhr.onerror = () => {
      signal?.removeEventListener("abort", onAbort);
      reject(new PartUploadError("Network error while uploading.", { retryable: true }));
    };
    xhr.onabort = () => {
      signal?.removeEventListener("abort", onAbort);
      reject(abortError());
    };
    signal?.addEventListener("abort", onAbort, { once: true });
    xhr.send(body);
  });

export interface UploadFileOptions {
  partSize: number;
  parts: AiUploadPart[];
  signal?: AbortSignal;
  concurrency?: number;
  retries?: number;
  /** Called with the cumulative bytes sent across all parts (whole file) and the file size. */
  onProgress?: (loaded: number, total: number) => void;
  putPart?: PartPutter;
  sleep?: Sleep;
  rand?: () => number;
}

/**
 * Uploads `file` to its presigned multipart part URLs (3 parts in flight by default), retrying each
 * part with backoff, and returns the collected `{partNumber, etag}` list sorted by part number —
 * exactly what `POST /evaluations/ai/uploads/complete` wants.
 */
export async function uploadFileMultipart(
  file: Blob,
  opts: UploadFileOptions,
): Promise<AiCompletedPart[]> {
  const slices = partSlices(file.size, opts.partSize, opts.parts);
  const put = opts.putPart ?? xhrPutPart;
  const loadedByPart = new Map<number, number>();
  const report = () => {
    let sum = 0;
    loadedByPart.forEach((v) => (sum += v));
    opts.onProgress?.(Math.min(sum, file.size), file.size);
  };
  const results = new Map<number, string>();

  await runPool(slices, opts.concurrency ?? UPLOAD_CONCURRENCY, async (slice) => {
    const blob = file.slice(slice.start, slice.end);
    const etag = await withRetry(
      async () => {
        loadedByPart.set(slice.partNumber, 0);
        report();
        return put(slice.url, blob, {
          signal: opts.signal,
          onProgress: (loaded) => {
            loadedByPart.set(slice.partNumber, Math.min(loaded, slice.end - slice.start));
            report();
          },
        });
      },
      { retries: opts.retries ?? PART_RETRIES, signal: opts.signal, sleep: opts.sleep, rand: opts.rand },
    );
    loadedByPart.set(slice.partNumber, slice.end - slice.start);
    report();
    results.set(slice.partNumber, etag);
  });

  return slices.map((s) => ({ partNumber: s.partNumber, etag: results.get(s.partNumber) as string }));
}

// ---------------------------------------------------------------------------
// Google Drive link classification
// ---------------------------------------------------------------------------

export type DriveLinkKind = "folder" | "file" | "doc" | "invalid";

export interface DriveLinkEntry {
  raw: string;
  kind: DriveLinkKind;
  /** Why it is invalid, or a note (e.g. duplicate). */
  reason?: string;
  duplicate: boolean;
  /** True when it counts toward the evaluations to queue. */
  valid: boolean;
}

const DRIVE_HOSTS = new Set(["drive.google.com", "docs.google.com", "drive.usercontent.google.com"]);
const ID_RE = /^[A-Za-z0-9_-]{10,}$/;

/** Classifies one link. `key` is a normalised identity used for de-duplication. */
export function classifyDriveLink(raw: string): { kind: DriveLinkKind; reason?: string; key?: string } {
  let u: URL;
  try {
    u = new URL(raw.trim());
  } catch {
    return { kind: "invalid", reason: "Not a valid URL." };
  }
  if (u.protocol !== "https:") return { kind: "invalid", reason: "Only https links are accepted." };
  const host = u.hostname.toLowerCase();
  if (!DRIVE_HOSTS.has(host)) return { kind: "invalid", reason: "Not a Google Drive or Docs link." };
  const path = u.pathname;
  const idParam = u.searchParams.get("id");

  if (host === "docs.google.com") {
    const m = path.match(/^\/(?:u\/\d+\/)?(document|spreadsheets|presentation)\/d\/([A-Za-z0-9_-]+)/);
    if (m && ID_RE.test(m[2])) return { kind: "doc", key: `doc:${m[2]}` };
    return { kind: "invalid", reason: "Unrecognised Google Docs link." };
  }
  if (host === "drive.usercontent.google.com") {
    if (idParam && ID_RE.test(idParam)) return { kind: "file", key: `file:${idParam}` };
    return { kind: "invalid", reason: "Unrecognised Drive download link." };
  }
  // drive.google.com
  const folder = path.match(/^\/drive\/(?:u\/\d+\/)?folders\/([A-Za-z0-9_-]+)/) ?? path.match(/^\/folderview/);
  if (folder) {
    const id = folder[1] ?? idParam;
    if (id && ID_RE.test(id)) return { kind: "folder", key: `folder:${id}` };
    return { kind: "invalid", reason: "Folder link is missing its id." };
  }
  const file = path.match(/^\/file\/(?:u\/\d+\/)?d\/([A-Za-z0-9_-]+)/);
  if (file && ID_RE.test(file[1])) return { kind: "file", key: `file:${file[1]}` };
  if ((path === "/open" || path === "/uc") && idParam && ID_RE.test(idParam)) {
    return { kind: "file", key: `file:${idParam}` };
  }
  return { kind: "invalid", reason: "Unrecognised Google Drive link." };
}

/** Splits free text into links (whitespace/comma separated, one per line normally) and classifies each. */
export function parseDriveLinks(text: string): DriveLinkEntry[] {
  const seen = new Set<string>();
  const out: DriveLinkEntry[] = [];
  for (const raw of text.split(/[\s,]+/).filter(Boolean)) {
    const c = classifyDriveLink(raw);
    if (c.kind === "invalid") {
      out.push({ raw, kind: "invalid", reason: c.reason, duplicate: false, valid: false });
      continue;
    }
    const dup = seen.has(c.key as string);
    seen.add(c.key as string);
    out.push({
      raw,
      kind: c.kind,
      duplicate: dup,
      reason: dup ? "Duplicate link (ignored)." : undefined,
      valid: !dup,
    });
  }
  return out;
}
