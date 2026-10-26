"""Progress tracking.

Many Lambdas run in parallel for one evaluation, so none of them edits progress.json in place. Each writes
its own tiny object under derived/{eid}/progress/ and `refresh()` merges them into progress.json:

    progress/stage.json                  {"stage","message"}              (ingest / assemble)
    progress/file-{source_id}.json       {"source_id","name","state","detail","images_total","chunks_total"}
    progress/visual-{source_id}.json     {"source_id","requests_total"}   (PlanFrames: vision requests of a video)
    progress/img/{source_id}/{seq}.done|.failed      (empty marker per analysed image)
    progress/chunk/{source_id}/{n}.done|.failed      (empty marker per transcribed chunk)
    progress/vis/{source_id}/{n}.done|.failed        (empty marker per vision request on a video's frame sheets)

A video's audio (PlanAudio) and frames (PlanFrames) are planned in parallel, so the frame side keeps its own
visual-{source_id}.json instead of sharing the file document. Its requests count as images in the counters (the
UI shows them as "Images analysed").

merge_progress() is pure; the last writer of progress.json wins, which is fine because every refresh rebuilds
the whole document from the complete set of per-file keys.
"""
from __future__ import annotations

import re
import time
from datetime import datetime, timezone

from .store import derived

STAGES = ("ingest", "extract", "transcribe", "analyze", "assemble", "done", "failed")
_MARKER_RE = re.compile(r"progress/(img|chunk|vis)/([^/]+)/([^/]+)\.(done|failed)$")
REFRESH_MIN_AGE_S = 2.0


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def merge_progress(stage_doc: dict | None, file_docs: list[dict], marker_keys: list[str], now: str | None = None,
                   visual_docs: list[dict] | tuple = ()) -> dict:
    """Pure merge of per-file keys into the contract's progress.json document."""
    done = {"img": {}, "chunk": {}, "vis": {}}
    failed = {"img": {}, "chunk": {}, "vis": {}}
    visual_total = {str(d.get("source_id")): int(d.get("requests_total") or 0) for d in visual_docs}
    for k in marker_keys:
        m = _MARKER_RE.search(k)
        if not m:
            continue
        kind, sid, _n, state = m.groups()
        (done if state == "done" else failed)[kind][sid] = (done if state == "done" else failed)[kind].get(sid, 0) + 1

    files, imgs_total, chunks_total, imgs_done, chunks_done, files_done = [], 0, 0, 0, 0, 0
    for fd in sorted(file_docs, key=lambda d: d.get("source_id", "")):
        sid = fd["source_id"]
        it, ct = int(fd.get("images_total") or 0) + visual_total.get(sid, 0), int(fd.get("chunks_total") or 0)
        idn = sum(done[k].get(sid, 0) + failed[k].get(sid, 0) for k in ("img", "vis"))
        cdn = done["chunk"].get(sid, 0) + failed["chunk"].get(sid, 0)
        state, detail = fd.get("state", "pending"), fd.get("detail", "")
        if state == "running" and (it or ct) and idn >= it and cdn >= ct:
            state = "done"
        if state == "running" and (it or ct):
            detail = detail or f"{idn}/{it} images, {cdn}/{ct} chunks"
        files.append({"source_id": sid, "name": fd.get("name", ""), "state": state, "detail": detail})
        imgs_total, chunks_total = imgs_total + it, chunks_total + ct
        imgs_done, chunks_done = imgs_done + min(idn, it) if it else imgs_done, chunks_done + min(cdn, ct) if ct else chunks_done
        files_done += state in ("done", "skipped", "failed")

    base = (stage_doc or {}).get("stage", "ingest")
    message = (stage_doc or {}).get("message", "")
    stage = base
    if base == "extract":
        if imgs_total and imgs_done < imgs_total:
            stage = "analyze"
        elif chunks_total and chunks_done < chunks_total:
            stage = "transcribe"
    return {
        "stage": stage,
        "updated_at": now or now_iso(),
        "message": message,
        "files": files,
        "counters": {
            "files_total": len(files), "files_done": files_done,
            "images_total": imgs_total, "images_done": imgs_done,
            "chunks_total": chunks_total, "chunks_done": chunks_done,
        },
    }


class Progress:
    def __init__(self, store, evaluation_id: str):
        self.store, self.eid = store, evaluation_id

    def _k(self, *parts: str) -> str:
        return derived(self.eid, "progress", *parts)

    def set_stage(self, stage: str, message: str = "") -> None:
        self.store.put_json(self._k("stage.json"), {"stage": stage, "message": message})
        self.refresh(force=True)

    def file(self, source_id: str, name: str, state: str, detail: str = "", images_total: int = 0,
             chunks_total: int = 0, refresh: bool = True) -> None:
        self.store.put_json(self._k(f"file-{source_id}.json"), {
            "source_id": source_id, "name": name, "state": state, "detail": detail,
            "images_total": images_total, "chunks_total": chunks_total})
        if refresh:
            self.refresh(force=True)

    def visual(self, source_id: str, requests_total: int) -> None:
        """PlanFrames: how many vision requests this video's frame sheets need (0 when there are none)."""
        self.store.put_json(self._k(f"visual-{source_id}.json"),
                            {"source_id": source_id, "requests_total": requests_total})
        self.refresh(force=True)

    def mark(self, kind: str, source_id: str, n: int | str, ok: bool) -> None:
        self.store.put_bytes(self._k(kind, source_id, f"{n}.{'done' if ok else 'failed'}"), b"")
        self.refresh()

    def load(self) -> dict:
        prefix = self._k("")
        keys, files, visual = [], [], []
        stage_doc = None
        for o in self.store.list(prefix):
            k = o["key"]
            rel = k[len(prefix):]
            if rel == "stage.json":
                stage_doc = self.store.get_json(k)
            elif rel.startswith("file-") and rel.endswith(".json"):
                files.append(self.store.get_json(k))
            elif rel.startswith("visual-") and rel.endswith(".json"):
                visual.append(self.store.get_json(k))
            else:
                keys.append(k)
        return merge_progress(stage_doc, files, keys, visual_docs=visual)

    def refresh(self, force: bool = False) -> dict | None:
        """Best-effort rebuild of progress.json; never raises."""
        try:
            key = derived(self.eid, "progress.json")
            if not force:
                h = self.store.head(key)
                if h and time.time() - h["last_modified"].timestamp() < REFRESH_MIN_AGE_S:
                    return None
            doc = self.load()
            self.store.put_json(key, doc)
            return doc
        except Exception:
            return None
