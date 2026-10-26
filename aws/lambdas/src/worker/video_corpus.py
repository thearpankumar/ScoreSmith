"""MergeVideo and a video's corpus sections: the transcript (PlanAudio -> TranscribeChunks) and the frame notes
(PlanFrames -> AnalyzeSheets) merged into one timeline.

merge_video writes video/{sid}/transcript.txt + transcript.json (as before) and visual.json:
    {"status": "ok|failed|off", "frames", "sheets", "requests", "requests_ok", "mean_gap_s", "duration_sec", "grid",
     "decode", "models": [...], "tokens_in", "tokens_out", "cost_usd" (null unless the provider reported costs),
     "error", "created"}
The backend reads visual.json to tell the scorer whether (and how densely) the video's pictures were analysed.

video_sections (assemble) emits, per video:
  - one `VIDEO {name} mm:ss-mm:ss` section per audio-chunk window (per sheet batch for a video without audio), its
    lines `[mm:ss] SAID: ...` and `[mm:ss] SHOWN: ...` in time order;
  - `VIDEO {name} steps`: the steps the frames show, in order, and what the frames do not show;
  - `VIDEO {name} quality`: container facts and frame statistics (quality.json) plus the model's quality notes.
Either side may be missing (silent video, failed transcription, failed or disabled frame analysis); the other still
makes it into the corpus.
"""
from __future__ import annotations

import re

from .progress import now_iso
from .store import derived
from .timeline import Event, assign, fmt_ts, parse_notes, said_events, split_window

_HDR_RE = re.compile(r"^<!--.*?-->\s*", re.S)
_EMPTY_RE = re.compile(r"^\(?\s*(none|nothing notable|n/?a)\s*\)?\.?$", re.I)


def _json(store, key: str):
    try:
        return store.get_json(key)
    except Exception:
        return None


def _text(store, key: str) -> str | None:
    try:
        return store.get_text(key)
    except Exception:
        return None


# --------------------------------------------------------------------------- merge (MergeVideo step)
def _merge_transcript(store, prefix: str, plan: dict | None, name: str) -> tuple[dict | None, dict[int, tuple]]:
    """transcript.txt / transcript.json as before; also returns {chunk index: (text, chunk json)} for the timeline."""
    if not plan or plan.get("error") or not plan.get("chunks"):
        return None, {}
    parts, meta, missing, texts = [], [], [], {}
    for c in plan.get("chunks", []):
        i = c["index"]
        text = _text(store, f"{prefix}/chunks/c{i}.txt")
        if text is None:
            missing.append(i)
            continue
        text = text.strip()
        parts.append(text)
        info = _json(store, f"{prefix}/chunks/c{i}.json") or {}
        texts[i] = (text, info)
        meta.append({"chunk": i, "start": c.get("start"), "end": c.get("end"), "model": info.get("model"),
                     "chars": len(text)})
    transcript = "\n\n".join(p for p in parts if p).strip()
    store.put_text(f"{prefix}/transcript.txt", transcript + "\n")
    doc = {"source": name or plan.get("source", ""), "duration_sec": plan.get("duration_sec"), "chunks": meta,
           "missing_chunks": missing, "created": now_iso()}
    store.put_json(f"{prefix}/transcript.json", doc)
    return doc, texts


def _merge_visual(store, prefix: str) -> tuple[dict, list[tuple[dict, str]]]:
    """visual.json and the usable batches [(batch info + its sidecar, notes text)], in time order."""
    sheets = _json(store, f"{prefix}/frames/sheets.json")
    doc: dict = {"status": "failed", "frames": 0, "sheets": 0, "requests": 0, "requests_ok": 0, "mean_gap_s": None,
                 "duration_sec": None, "grid": None, "decode": None, "models": [], "tokens_in": 0, "tokens_out": 0,
                 "cost_usd": None, "error": None, "created": now_iso()}
    if sheets is None:
        doc["error"] = "the frame sampling step did not finish"
        return doc, []
    if sheets.get("mode") == "off":
        doc.update(status="off", error=None)
        return doc, []
    if sheets.get("error"):
        doc["error"] = str(sheets["error"])[:300]
        return doc, []
    batches = sheets.get("batches", [])
    doc.update(frames=int(sheets.get("frames") or 0), sheets=len(sheets.get("sheets", [])), requests=len(batches),
               mean_gap_s=sheets.get("mean_gap_s"), duration_sec=sheets.get("duration_sec"), grid=sheets.get("grid"),
               decode=sheets.get("decode"))
    cells = {sh.get("index"): [float(c["t"]) for c in sh.get("cells", [])] for sh in sheets.get("sheets", [])}
    usable, models, costs = [], set(), []
    for b in batches:
        n = int(b["index"])
        meta = _json(store, f"{prefix}/frames/batch_{n:02d}.json")
        text = _text(store, f"{prefix}/frames/batch_{n:02d}.md")
        if not meta or meta.get("run") != sheets.get("run") or text is None:
            continue  # failed, given up, or left over from an earlier run of this evaluation
        times = [t for i in b.get("sheets", []) for t in cells.get(i, [])]
        usable.append(({**b, **meta, "times": times}, _HDR_RE.sub("", text).strip()))
        models.add(str(meta.get("model") or ""))
        doc["tokens_in"] += int(meta.get("tokens_in") or 0)
        doc["tokens_out"] += int(meta.get("tokens_out") or 0)
        if isinstance(meta.get("cost_usd"), (int, float)):
            costs.append(float(meta["cost_usd"]))
    doc.update(requests_ok=len(usable), models=sorted(m for m in models if m),
               cost_usd=round(sum(costs), 6) if costs else None)
    if usable:
        doc["status"] = "ok"
    else:
        doc["error"] = "no frame sheet could be analysed"
    return doc, usable


def merge_video(store, eid: str, sid: str, name: str = "") -> dict:
    """Merge one video's transcript chunks and frame notes. Writes transcript.txt / transcript.json (when the video
    has transcribed audio) and visual.json; returns {"plan", "transcript", "chunk_texts", "visual", "batches"}."""
    prefix = derived(eid, "video", sid)
    plan = _json(store, f"{prefix}/plan.json")
    transcript, texts = _merge_transcript(store, prefix, plan, name)
    visual, batches = _merge_visual(store, prefix)
    store.put_json(f"{prefix}/visual.json", visual)
    return {"plan": plan, "transcript": transcript, "chunk_texts": texts, "visual": visual, "batches": batches}


# --------------------------------------------------------------------------- corpus sections
def _quality_text(q: dict, visual: dict, notes: list[tuple[dict, list[str]]]) -> str:
    lines = []
    facts = []
    if q.get("width") and q.get("height"):
        facts.append(f"{q['width']}x{q['height']}")
    if q.get("fps"):
        facts.append(f"{q['fps']:g} fps")
    if q.get("bitrate_kbps"):
        facts.append(f"{q['bitrate_kbps']} kb/s")
    if q.get("video_codec"):
        facts.append(str(q["video_codec"]))
    if q.get("duration_sec"):
        facts.append(f"{fmt_ts(q['duration_sec'])} long")
    facts.append("with an audio track" if q.get("has_audio") else "no audio track")
    lines.append("Technical: " + ", ".join(facts) + ".")
    gap = visual.get("mean_gap_s")
    lines.append(f"Sampled frames: {q.get('kept_frames', 0)} distinct of {q.get('sampled_frames', 0)} sampled"
                 + (f" (about one every {gap:g} s)." if gap else "."))
    b = q.get("brightness") or {}
    if b:
        parts = [f"average {b.get('mean')} of 255"]
        for key, what in (("black_frames", "black"), ("dark_frames", "dark"), ("bright_frames", "very bright")):
            item = q.get(key) or {}
            if item.get("count"):
                parts.append(f"{item['count']} {what} frame(s) (at {', '.join(item.get('at', []))})")
        lines.append("Brightness: " + "; ".join(parts) + ".")
    low = q.get("low_detail_frames") or {}
    if low.get("judged"):
        lines.append(f"Detail: {low.get('count', 0)} of {low['judged']} sampled frames with content show little edge "
                     "detail (possibly blurred or out of focus)"
                     + (f" (at {', '.join(low.get('at', []))})." if low.get("count") else "."))
    static = q.get("static_segments") or []
    if static:
        spans = ", ".join(f"{s['start']}-{s['end']}" for s in static)
        lines.append(f"Static picture (no visible change for 30 s or more): {spans}.")
    for meta, quality in notes:
        if quality:
            lines.append(f"Visual quality noted from the frames {fmt_ts(meta['start'])}-{fmt_ts(meta['end'])}: "
                         + " ".join(quality))
    return "\n".join(lines)


def _useful(items: list[str]) -> list[str]:
    return [x for x in items if not _EMPTY_RE.match(x)]


def _steps_text(parsed: list[tuple[dict, object]]) -> str:
    lines = []
    for meta, notes in parsed:
        steps, missing = _useful(notes.steps), _useful(notes.not_visible)
        if not steps and not missing:
            continue
        span = f"{fmt_ts(meta['start'])}-{fmt_ts(meta['end'])}"
        if steps:
            lines.append(f"Steps observed in the frames {span}, in order:")
            lines.extend(steps)
        if missing:
            lines.append(f"Not visible in the sampled frames {span}: " + " ".join(missing))
    return "\n".join(lines)


def _bounds(plan: dict | None, batches: list[dict], duration: float | None) -> list[tuple[float, float]]:
    """Windows of the VIDEO sections: the audio chunks, or (no audio) the sheet batches."""
    if plan and not plan.get("error") and plan.get("chunks"):
        return [(float(c.get("start") or 0.0), float(c.get("end") or 0.0)) for c in plan["chunks"]]
    starts = sorted(float(b.get("start") or 0.0) for b in batches)
    end = float(duration or 0.0) or max((float(b.get("end") or 0.0) for b in batches), default=0.0)
    return [(a, starts[i + 1] if i + 1 < len(starts) else max(end, a)) for i, a in enumerate(starts)]


def video_sections(store, eid: str, f: dict) -> tuple[list[dict], list[str], dict]:
    """(sections, warnings, visual.json doc) of one video."""
    sid, name = f["source_id"], f["original_name"]
    prefix = derived(eid, "video", sid)
    m = merge_video(store, eid, sid, name)
    plan, transcript, visual, batches = m["plan"], m["transcript"], m["visual"], m["batches"]
    warns: list[str] = []
    if plan is None:
        warns.append(f"{name}: the audio of this video could not be processed")
    elif plan.get("error"):
        warns.append(f"{name}: {plan['error']}")
    elif not plan.get("has_audio", True) or not plan.get("chunks"):
        warns.append(f"{name}: no audio track, nothing to transcribe")
    if transcript and transcript["missing_chunks"]:
        warns.append(f"{name}: {len(transcript['missing_chunks'])} of {len(plan['chunks'])} audio chunk(s) could not "
                     "be transcribed.")
    if visual["status"] == "failed":
        warns.append(f"{name}: the frames could not be analysed ({visual['error']})")
    elif visual["status"] == "ok" and visual["requests_ok"] < visual["requests"]:
        warns.append(f"{name}: {visual['requests'] - visual['requests_ok']} of {visual['requests']} frame-sheet "
                     "request(s) could not be analysed.")

    events: list[Event] = []
    for c in (plan or {}).get("chunks", []) if transcript else []:
        if c["index"] in m["chunk_texts"]:
            text, info = m["chunk_texts"][c["index"]]
            events.extend(said_events(c, text, info.get("segments")))
    parsed = []
    for meta, text in batches:
        notes = parse_notes(text, meta.get("times"))
        parsed.append((meta, notes))
        events.extend(Event(t, "SHOWN", line) for t, line in notes.shown)

    def section(label: str, text: str) -> dict:
        return {"source_id": sid, "source_name": name, "kind": "video", "label": label, "text": text}

    secs: list[dict] = []
    bounds = _bounds(plan, [meta for meta, _t in batches], visual.get("duration_sec"))
    for (a, b), evs in zip(bounds, assign(events, bounds)):
        if evs:
            secs.extend(section(f"VIDEO {name} {fmt_ts(pa)}-{fmt_ts(pb)}", body) for pa, pb, body in
                        split_window(a, b, evs))
    if secs and parsed:
        steps = _steps_text(parsed)
        if steps:
            secs.append(section(f"VIDEO {name} steps", steps))
    quality = _json(store, f"{prefix}/frames/quality.json")
    if secs and quality:
        secs.append(section(f"VIDEO {name} quality",
                            _quality_text(quality, visual, [(meta, notes.quality) for meta, notes in parsed])))
    return secs, warns, visual


def visual_stats(visuals: list[dict]) -> dict | None:
    """corpus.json stats.visual over the videos whose frames were sampled (None when there are none)."""
    sampled = [v for v in visuals if v.get("status") in ("ok", "failed") and v.get("frames")]
    if not sampled:
        return None
    costs = [v["cost_usd"] for v in sampled if isinstance(v.get("cost_usd"), (int, float))]
    return {"frames": sum(int(v.get("frames") or 0) for v in sampled),
            "sheets": sum(int(v.get("sheets") or 0) for v in sampled),
            "requests": sum(int(v.get("requests_ok") or 0) for v in sampled),
            "tokens_in": sum(int(v.get("tokens_in") or 0) for v in sampled),
            "tokens_out": sum(int(v.get("tokens_out") or 0) for v in sampled),
            "cost_usd": round(sum(costs), 6) if costs else None}
