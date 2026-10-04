"use client";

import { useCallback, useEffect, useRef, useState } from "react";

import { abortAiUploads, completeAiUploads, initAiUploads } from "@/lib/api-client";
import { MAX_FILES, contentTypeFor, uploadFileMultipart, validateUploadFile } from "@/lib/ai-upload";
import type { AiUploadPurpose } from "@/lib/types";

export type UploadStatus = "queued" | "uploading" | "finalizing" | "done" | "error";

export interface UploadItem {
  id: string;
  file: File;
  status: UploadStatus;
  /** Bytes sent so far (all parts). */
  loaded: number;
  error: string | null;
  uploadId?: string;
  s3Key?: string;
}

const MAX_ACTIVE = 2; // files uploading at once (each uses up to 3 parallel parts)

let counter = 0;
const nextId = () => `up-${Date.now().toString(36)}-${counter++}`;

/**
 * Chunked-upload queue for one input tab: validates dropped files, uploads them straight to S3 via
 * presigned multipart URLs (progress, cancel, retry, remove) and exposes the finished S3 keys.
 */
export function useFileUploads(purpose: AiUploadPurpose, opts: { maxFiles?: number; replace?: boolean } = {}) {
  const maxFiles = opts.maxFiles ?? MAX_FILES;
  const replace = opts.replace ?? false;
  const [items, setItems] = useState<UploadItem[]>([]);
  const [rejections, setRejections] = useState<string[]>([]);
  const itemsRef = useRef<UploadItem[]>([]);
  const controllers = useRef(new Map<string, AbortController>());

  const commit = useCallback((updater: (prev: UploadItem[]) => UploadItem[]) => {
    itemsRef.current = updater(itemsRef.current);
    setItems(itemsRef.current);
  }, []);
  const patch = useCallback(
    (id: string, p: Partial<UploadItem>) => commit((prev) => prev.map((it) => (it.id === id ? { ...it, ...p } : it))),
    [commit],
  );

  const run = useCallback(
    async (item: UploadItem) => {
      const ac = new AbortController();
      controllers.current.set(item.id, ac);
      let handle: { uploadId: string; s3Key: string } | null = null;
      let finished = false;
      try {
        const plan = await initAiUploads(
          purpose,
          [{ name: item.file.name, size: item.file.size, contentType: contentTypeFor(item.file.name, item.file.type) }],
          ac.signal,
        );
        const f = plan.files[0];
        if (!f) throw new Error("The server returned no upload plan.");
        handle = { uploadId: f.uploadId, s3Key: f.s3Key };
        patch(item.id, { uploadId: f.uploadId, s3Key: f.s3Key });
        const parts = await uploadFileMultipart(item.file, {
          partSize: plan.partSize,
          parts: f.parts,
          signal: ac.signal,
          onProgress: (loaded) => patch(item.id, { loaded }),
        });
        patch(item.id, { status: "finalizing", loaded: item.file.size });
        const [res] = await completeAiUploads([{ uploadId: f.uploadId, s3Key: f.s3Key, parts }]);
        if (!res?.ok) throw new Error(res?.error || "The uploaded file could not be verified.");
        finished = true;
        patch(item.id, { status: "done", error: null });
      } catch (err) {
        const cancelled = ac.signal.aborted;
        // A removed item no longer exists; patch is then a harmless no-op.
        patch(item.id, {
          status: "error",
          loaded: 0,
          error: cancelled ? "Cancelled." : err instanceof Error ? err.message : "Upload failed.",
        });
        if (handle && !finished) void abortAiUploads([handle]).catch(() => undefined);
      } finally {
        controllers.current.delete(item.id);
      }
    },
    [purpose, patch],
  );

  // Scheduler: start queued items while fewer than MAX_ACTIVE are in flight.
  useEffect(() => {
    const active = items.filter((i) => i.status === "uploading" || i.status === "finalizing").length;
    const toStart = items.filter((i) => i.status === "queued").slice(0, Math.max(0, MAX_ACTIVE - active));
    if (toStart.length === 0) return;
    const ids = new Set(toStart.map((i) => i.id));
    commit((prev) => prev.map((i) => (ids.has(i.id) ? { ...i, status: "uploading" as const } : i)));
    toStart.forEach((i) => void run(i));
  }, [items, run, commit]);

  useEffect(() => {
    const map = controllers.current;
    return () => map.forEach((ac) => ac.abort());
  }, []);

  const remove = useCallback(
    (id: string) => {
      controllers.current.get(id)?.abort();
      commit((prev) => prev.filter((i) => i.id !== id));
    },
    [commit],
  );

  const addFiles = useCallback(
    (files: File[]) => {
      if (replace) {
        controllers.current.forEach((ac) => ac.abort());
        itemsRef.current = [];
      }
      const bad: string[] = [];
      const accepted: UploadItem[] = [];
      let count = itemsRef.current.length;
      for (const file of files) {
        const reason = validateUploadFile(file, purpose, count, maxFiles);
        if (reason) {
          bad.push(`${file.name}: ${reason}`);
          continue;
        }
        accepted.push({ id: nextId(), file, status: "queued", loaded: 0, error: null });
        count++;
      }
      setRejections(bad);
      if (accepted.length || replace) commit((prev) => [...prev, ...accepted]);
    },
    [purpose, maxFiles, replace, commit],
  );

  const cancel = useCallback((id: string) => controllers.current.get(id)?.abort(), []);
  const retry = useCallback((id: string) => patch(id, { status: "queued", loaded: 0, error: null }), [patch]);
  const clearRejections = useCallback(() => setRejections([]), []);

  const busy = items.some((i) => i.status === "queued" || i.status === "uploading" || i.status === "finalizing");
  return { items, rejections, clearRejections, addFiles, cancel, retry, remove, busy };
}
