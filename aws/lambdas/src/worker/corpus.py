"""Assemble: stitch extracted text, image analyses and transcripts into corpus.json / corpus.md.
Also hosts merge_video (per-video transcript merge) and the failure handler that writes status.json."""
from __future__ import annotations

import re

from .errors import PipelineError, code_from_error
from .progress import Progress, merge_progress, now_iso
from .store import derived

MAX_SECTION_CHARS = 6000
MAX_CORPUS_CHARS = 12_000_000
MIN_CORPUS_WORDS = 30  # below this there is nothing meaningful to score
_PAGE_RE = re.compile(r"^## Page (\d+)\s*$")
_IMG_RE = re.compile(r"^!\[([^\]]*)\]\(([^)]*)\)\s*$")
_HDR_RE = re.compile(r"^<!--.*?-->\s*", re.S)


def fmt_ts(sec: float) -> str:
    sec = int(round(sec))
    h, rem = divmod(sec, 3600)
    m, s = divmod(rem, 60)
    return f"{h}:{m:02d}:{s:02d}" if h else f"{m:02d}:{s:02d}"


def words(text: str) -> int:
    return len(text.split())


# --------------------------------------------------------------------------- documents
def parse_content(md: str):
    """Yield ('page', n) | ('text', str) | ('image', filename) in reading order from content.md."""
    buf: list[str] = []

    def flush():
        t = "\n".join(buf).strip()
        buf.clear()
        return t

    for line in md.splitlines():
        pm, im = _PAGE_RE.match(line), _IMG_RE.match(line)
        if pm or im:
            t = flush()
            if t:
                yield "text", t
            yield ("page", int(pm.group(1))) if pm else ("image", im.group(2))
        else:
            buf.append(line)
    t = flush()
    if t:
        yield "text", t


def _split_text(text: str, limit: int = MAX_SECTION_CHARS) -> list[str]:
    if len(text) <= limit:
        return [text]
    out, cur = [], ""
    for para in re.split(r"\n{2,}", text):
        while len(para) > limit:  # a single huge paragraph
            if cur:
                out.append(cur)
                cur = ""
            out.append(para[:limit])
            para = para[limit:]
        if len(cur) + len(para) + 2 > limit and cur:
            out.append(cur)
            cur = para
        else:
            cur = f"{cur}\n\n{para}" if cur else para
    if cur:
        out.append(cur)
    return out


def doc_sections(store, eid: str, f: dict) -> tuple[list[dict], list[str]]:
    sid, name, kind = f["source_id"], f["original_name"], f["kind"]
    prefix = derived(eid, "docs", sid)
    warnings: list[str] = []
    try:
        md = store.get_text(f"{prefix}/content.md")
    except Exception:
        err = ""
        try:
            err = store.get_json(f"{prefix}/extract.json").get("error", "")
        except Exception:
            pass
        return [], [f"{name}: could not be extracted" + (f" ({err})" if err else "")]
    try:
        warnings.extend(store.get_json(f"{prefix}/extract.json").get("warnings", []))
    except Exception:
        pass
    secs: list[dict] = []
    page, part, missing = None, 0, 0

    def add_text(t: str):
        nonlocal part
        for piece in _split_text(t):
            part += 1
            label = f"DOC {name} p{page}" if page else f"DOC {name} part {part}"
            secs.append({"source_id": sid, "source_name": name, "kind": "doc", "label": label, "text": piece})

    for tok, val in parse_content(md):
        if tok == "page":
            page = val
        elif tok == "text":
            add_text(val)
        else:
            m = re.match(r"^(\d+)", val)
            seq = int(m.group(1)) if m else 0
            try:
                text = store.get_text(f"{prefix}/images/{val.rsplit('.', 1)[0]}.analysis.md")
            except Exception:
                missing += 1
                continue
            text = _HDR_RE.sub("", text).strip()
            if text:
                secs.append({"source_id": sid, "source_name": name, "kind": "image",
                             "label": f"IMAGE {name} fig {seq}", "text": text})
    if missing:
        warnings.append(f"{name}: {missing} image(s) were not analysed.")
    return secs, warnings


# --------------------------------------------------------------------------- video
def merge_video(store, eid: str, sid: str, name: str = "") -> dict:
    """Merge chunk transcripts into video/{sid}/transcript.txt + transcript.json. Returns the transcript.json doc."""
    prefix = derived(eid, "video", sid)
    plan = store.get_json(f"{prefix}/plan.json")
    parts, meta, missing = [], [], []
    for c in plan.get("chunks", []):
        i = c["index"]
        try:
            text = store.get_text(f"{prefix}/chunks/c{i}.txt").strip()
        except Exception:
            missing.append(i)
            continue
        parts.append(text)
        info = {}
        try:
            info = store.get_json(f"{prefix}/chunks/c{i}.json")
        except Exception:
            pass
        meta.append({"chunk": i, "start": c.get("start"), "end": c.get("end"), "model": info.get("model"),
                     "chars": len(text)})
    transcript = "\n\n".join(p for p in parts if p).strip()
    store.put_text(f"{prefix}/transcript.txt", transcript + "\n")
    doc = {"source": name or plan.get("source", ""), "duration_sec": plan.get("duration_sec"), "chunks": meta,
           "missing_chunks": missing, "created": now_iso()}
    store.put_json(f"{prefix}/transcript.json", doc)
    return doc


def video_sections(store, eid: str, f: dict) -> tuple[list[dict], list[str]]:
    sid, name = f["source_id"], f["original_name"]
    prefix = derived(eid, "video", sid)
    try:
        plan = store.get_json(f"{prefix}/plan.json")
    except Exception:
        return [], [f"{name}: video could not be processed"]
    if plan.get("error"):
        return [], [f"{name}: {plan['error']}"]
    if not plan.get("has_audio", True) or not plan.get("chunks"):
        return [], [f"{name}: no audio track, nothing to transcribe"]
    doc = merge_video(store, eid, sid, name)
    secs: list[dict] = []
    for c in plan["chunks"]:
        i = c["index"]
        if i in doc["missing_chunks"]:
            continue
        text = store.get_text(f"{prefix}/chunks/c{i}.txt").strip()
        if not text:
            continue
        secs.append({"source_id": sid, "source_name": name, "kind": "video",
                     "label": f"VIDEO {name} {fmt_ts(c['start'])}-{fmt_ts(c['end'])}", "text": text})
    warns = []
    if doc["missing_chunks"]:
        warns.append(f"{name}: {len(doc['missing_chunks'])} of {len(plan['chunks'])} audio chunk(s) could not be transcribed.")
    return secs, warns


# --------------------------------------------------------------------------- assemble
def run_assemble(event: dict, store) -> dict:
    eid = event["evaluation_id"]
    prog = Progress(store, eid)
    prog.set_stage("assemble", "Building the evaluation corpus")
    manifest = store.get_json(derived(eid, "manifest.json"))
    warnings: list[str] = []
    for d in manifest.get("drive", []):
        warnings.extend(d.get("warnings", []))
    sections: list[dict] = []
    file_states: dict[str, tuple[str, str]] = {}
    n_docs = n_videos = 0
    for f in manifest.get("files", []):
        sid = f["source_id"]
        if f["status"] != "ok":
            warnings.extend(f.get("warnings", []))
            file_states[sid] = ("skipped" if f["status"] == "skipped" else "failed", "; ".join(f.get("warnings", []))[:200])
            continue
        if f["kind"] == "video":
            secs, warns = video_sections(store, eid, f)
        else:
            secs, warns = doc_sections(store, eid, f)
        warnings.extend(warns)
        sections.extend(secs)
        if any(s["kind"] in ("doc", "video") for s in secs):
            file_states[sid] = ("done", "")
            n_videos += f["kind"] == "video"
            n_docs += f["kind"] != "video"
        elif f["kind"] == "video" and any("no audio track" in w for w in warns):
            # A silent screen recording is not an error: there is simply nothing to transcribe.
            file_states[sid] = ("skipped", "No audio track, nothing to transcribe.")
        else:
            file_states[sid] = ("failed" if warns else "done", "; ".join(warns)[:200])
    total_chars = 0
    kept: list[dict] = []
    for s in sections:
        total_chars += len(s["text"])
        if total_chars > MAX_CORPUS_CHARS:
            warnings.append("Corpus truncated: it exceeded the maximum size.")
            break
        kept.append(s)
    sections = kept
    for i, s in enumerate(sections, 1):
        s["id"] = f"s{i:03d}"
    n_words = sum(words(s["text"]) for s in sections)
    if not sections or n_words == 0:
        raise PipelineError("no_content", "No text could be extracted from the submitted files.")
    if n_words < MIN_CORPUS_WORDS:
        raise PipelineError(
            "no_content", f"Only {n_words} word(s) could be extracted, which is not enough to evaluate a submission."
        )
    # keep contract key order: id first
    sections = [{"id": s["id"], "source_id": s["source_id"], "source_name": s["source_name"], "kind": s["kind"],
                 "label": s["label"], "text": s["text"]} for s in sections]
    corpus = {
        "evaluation_id": eid, "built_at": now_iso(),
        "stats": {"docs": n_docs, "videos": n_videos, "images": sum(s["kind"] == "image" for s in sections), "words": n_words},
        "warnings": list(dict.fromkeys(warnings)), "sections": sections,
    }
    md = "\n\n".join(f"## [{s['label']}]\n\n{s['text']}" for s in sections) + "\n"
    store.put_text(derived(eid, "corpus.md"), md, "text/markdown; charset=utf-8")
    store.put_json(derived(eid, "corpus.json"), corpus)  # written last: its presence means success

    final = prog.load()
    for fe in final["files"]:
        if fe["source_id"] in file_states:
            fe["state"], fe["detail"] = file_states[fe["source_id"]]
    c = final["counters"]
    c["files_done"] = len(final["files"])
    c["images_done"], c["chunks_done"] = c["images_total"], c["chunks_total"]
    final.update(stage="done", message="Corpus ready", updated_at=now_iso())
    store.put_json(derived(eid, "progress.json"), final)
    return {"evaluation_id": eid, "corpus_key": derived(eid, "corpus.json"), "stats": corpus["stats"]}


# --------------------------------------------------------------------------- failure path
def run_fail(event: dict, store) -> dict:
    """Catch handler: write status.json + progress.json (stage failed). Never raises."""
    eid = event["evaluation_id"]
    code, message = code_from_error(event.get("error"))
    try:
        store.put_json(derived(eid, "status.json"), {"error_code": code, "error_message": message})
    except Exception:
        pass
    try:
        prog = Progress(store, eid)
        try:
            doc = prog.load()
        except Exception:
            doc = merge_progress(None, [], [])
        doc.update(stage="failed", message=message, updated_at=now_iso())
        store.put_json(derived(eid, "progress.json"), doc)
    except Exception:
        pass
    return {"evaluation_id": eid, "error_code": code, "error_message": message}
