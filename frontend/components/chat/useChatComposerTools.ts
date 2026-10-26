"use client";

import { useCallback, useEffect, useRef, useState } from "react";

import { contentTypeFor, uploadFileMultipart } from "@/lib/ai-upload";
import { completeChatAttachment, deleteChatAttachment, initChatAttachment } from "@/lib/api-client";
import { getChatConfig } from "@/lib/chat-config-client";
import { validateChatFile } from "@/lib/chat-attachments";
import type { ComposerAttachment } from "@/lib/chat-attachments";
import { EMPTY_CHAT_CONFIG, LAST_MODEL_STORAGE_KEY, initialChatModel } from "@/lib/chat-models";
import type { ChatConfig } from "@/lib/chat-models";

function readLastPick(): string | null {
  try {
    return window.localStorage.getItem(LAST_MODEL_STORAGE_KEY);
  } catch {
    return null;
  }
}

function writeLastPick(model: string): void {
  try {
    window.localStorage.setItem(LAST_MODEL_STORAGE_KEY, model);
  } catch {
    // private mode / blocked storage: the server still remembers the session's pick
  }
}

/** The chat config (`GET /chat/config`, loaded once per mount) and the picked model: the session's own pick,
 * else the user's last pick, else the admin's default. */
export function useChatModel(sessionModel: string | null | undefined) {
  const [config, setConfig] = useState<ChatConfig>(EMPTY_CHAT_CONFIG);
  const [model, setModelState] = useState<string | null>(null);
  const sessionModelRef = useRef(sessionModel);

  useEffect(() => {
    const ac = new AbortController();
    void getChatConfig(ac.signal).then((loaded) => {
      if (ac.signal.aborted) return;
      setConfig(loaded);
      setModelState(initialChatModel(loaded, sessionModelRef.current, readLastPick()));
    });
    return () => ac.abort();
  }, []);

  const setModel = useCallback((next: string) => {
    setModelState(next);
    writeLastPick(next);
  }, []);

  return { config, model, setModel };
}

let counter = 0;
const nextLocalId = () => `att-${Date.now().toString(36)}-${counter++}`;

/** Files attached in the composer: validated against the server's limits, uploaded straight to S3, then read by
 * the server (`complete`). `clear()` after a send; `remove()` deletes a not-yet-sent file on the server too. */
export function useComposerAttachments(config: ChatConfig) {
  const [items, setItems] = useState<ComposerAttachment[]>([]);
  const [rejections, setRejections] = useState<string[]>([]);
  const itemsRef = useRef<ComposerAttachment[]>([]);
  const controllers = useRef(new Map<string, AbortController>());

  const commit = useCallback((updater: (prev: ComposerAttachment[]) => ComposerAttachment[]) => {
    itemsRef.current = updater(itemsRef.current);
    setItems(itemsRef.current);
  }, []);
  const patch = useCallback(
    (localId: string, p: Partial<ComposerAttachment>) =>
      commit((prev) => prev.map((it) => (it.localId === localId ? { ...it, ...p } : it))),
    [commit],
  );

  const upload = useCallback(
    async (localId: string, file: File) => {
      const ac = new AbortController();
      controllers.current.set(localId, ac);
      try {
        const plan = await initChatAttachment(
          { name: file.name, size: file.size, contentType: contentTypeFor(file.name, file.type) },
          ac.signal,
        );
        patch(localId, { serverId: plan.attachment.id });
        const parts = await uploadFileMultipart(file, {
          partSize: plan.partSize,
          parts: plan.parts,
          signal: ac.signal,
          onProgress: (loaded, total) => patch(localId, { progress: total ? loaded / total : 0 }),
        });
        patch(localId, { status: "reading", progress: 1 });
        const done = await completeChatAttachment(plan.attachment.id, plan.uploadId, parts);
        patch(localId, {
          status: done.status === "ready" ? "ready" : "failed",
          error: done.status === "ready" ? null : done.error || "The file could not be read.",
          outline: done.outline,
        });
      } catch (err) {
        if (ac.signal.aborted) return; // removed by the user
        patch(localId, { status: "failed", error: err instanceof Error ? err.message : "Upload failed." });
      } finally {
        controllers.current.delete(localId);
      }
    },
    [patch],
  );

  const addFiles = useCallback(
    (files: File[]) => {
      const bad: string[] = [];
      let count = itemsRef.current.length;
      const accepted: { item: ComposerAttachment; file: File }[] = [];
      for (const file of files) {
        const problem = validateChatFile(file, config.attachments, count);
        if (problem) {
          bad.push(problem);
          continue;
        }
        count += 1;
        accepted.push({
          file,
          item: {
            localId: nextLocalId(), name: file.name, size: file.size, status: "uploading", progress: 0,
            error: null, serverId: null, outline: null,
          },
        });
      }
      setRejections(bad);
      if (accepted.length === 0) return;
      commit((prev) => [...prev, ...accepted.map((a) => a.item)]);
      accepted.forEach((a) => void upload(a.item.localId, a.file));
    },
    [config.attachments, commit, upload],
  );

  const remove = useCallback(
    (localId: string) => {
      const item = itemsRef.current.find((i) => i.localId === localId);
      controllers.current.get(localId)?.abort();
      commit((prev) => prev.filter((i) => i.localId !== localId));
      if (item?.serverId) void deleteChatAttachment(item.serverId).catch(() => undefined);
    },
    [commit],
  );

  /** After a send: the sent files now belong to the message. */
  const clear = useCallback(() => {
    commit(() => []);
    setRejections([]);
  }, [commit]);

  useEffect(() => {
    const map = controllers.current;
    return () => map.forEach((ac) => ac.abort());
  }, []);

  return { items, rejections, addFiles, remove, clear, dismissRejections: () => setRejections([]) };
}
