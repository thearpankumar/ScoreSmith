"""Transcription, image-analysis and video frame-sheet steps, for either provider.

The provider of the run comes from run_config (saved by ingest). bedrock_ops and openrouter_ops expose the same
interface (default_client, transcribe_models, vision_models, video_models, transcribe, analyse_image, analyse_sheets),
and the outputs do not depend on the provider: chunk c{n}.txt / c{n}.json, the image's .analysis.md with its
source/model header and its .analysis.json usage sidecar, a video's frames/batch_NN.md + batch_NN.json, and the
progress markers.
"""
from __future__ import annotations

import os
from datetime import datetime, timezone

from . import run_config, scorecard_context, video_settings
from .errors import PipelineError, TransientError
from .frames import frames_prefix
from .images import analysis_key, usage_key
from .progress import Progress
from .prompts import image_prompt, video_frames_prompt
from .store import derived
from .timeline import fmt_ts

MAX_SHEETS_PER_REQUEST = 8
HOLD_NOTE_S = 10.0  # a frame whose picture stayed this long is listed as "unchanged until ..."


def provider_ops(provider: str):
    """The module that calls `provider`, imported lazily (a Bedrock run never loads the OpenRouter client)."""
    if provider == "openrouter":
        from . import openrouter_ops

        return openrouter_ops
    from . import bedrock_ops

    return bedrock_ops


def _start_step(client) -> None:
    """Start the per-invocation time budget on clients that keep one (openrouter_ops; boto3 has its own timeouts)."""
    start = getattr(client, "start_step", None)
    if callable(start):
        start()


def _tokens(usage: dict) -> tuple[int, int]:
    """Bedrock reports {inputTokens, outputTokens}; openrouter_ops already returns {tokens_in, tokens_out}."""
    return (int(usage.get("tokens_in", usage.get("inputTokens", 0)) or 0),
            int(usage.get("tokens_out", usage.get("outputTokens", 0)) or 0))


def usage_doc(model: str, usage: dict) -> dict:
    """What one model call used: tokens, and the cost when the provider reports it (OpenRouter; None on Bedrock)."""
    tokens_in, tokens_out = _tokens(usage)
    cost = usage.get("cost_usd")
    return {"model": model, "tokens_in": tokens_in, "tokens_out": tokens_out,
            "cost_usd": float(cost) if isinstance(cost, (int, float)) and not isinstance(cost, bool) else None}


def _absolute_segments(segments: object, start: float) -> list[dict]:
    """The transcriber's timed segments (seconds from the chunk start) moved to seconds from the video start."""
    out = []
    for s in segments if isinstance(segments, list) else []:
        try:
            a, b, text = float(s["start"]), float(s.get("end", s["start"])), str(s["text"])
        except (KeyError, TypeError, ValueError):
            continue
        out.append({"start": round(start + a, 2), "end": round(start + b, 2), "text": text})
    return out


def run_transcribe_chunk(event: dict, store, client=None, models: list[str] | None = None) -> dict:
    eid, sid, chunk = event["evaluation_id"], event["source_id"], event["chunk"]
    n = int(chunk["index"])
    ops = provider_ops(run_config.load(store, eid))
    client = client or ops.default_client()
    _start_step(client)
    audio = store.get_bytes(chunk["key"])
    # The S3 location and a job name per chunk are for Amazon Transcribe (Bedrock runs), which reads S3, not bytes.
    text, model, usage = ops.transcribe(client, models or ops.transcribe_models(), audio,
                                        s3_uri=f"s3://{event['bucket']}/{chunk['key']}",
                                        job_name=f"qs-eval-{eid}-{sid}-c{n}")
    tokens_in, tokens_out = _tokens(usage)
    prefix = derived(eid, "video", sid, "chunks")
    store.put_text(f"{prefix}/c{n}.txt", text)
    meta = {"chunk": n, "start": chunk.get("start"), "end": chunk.get("end"), "model": model, "chars": len(text),
            "tokens_in": tokens_in, "tokens_out": tokens_out}
    # Amazon Transcribe's sentence times (Bedrock runs), so the corpus can say when each sentence was spoken
    segments = _absolute_segments(usage.get("segments"), float(chunk.get("start") or 0.0))
    if segments:
        meta["segments"] = segments
    store.put_json(f"{prefix}/c{n}.json", meta)
    Progress(store, eid).mark("chunk", sid, n, True)
    return {"source_id": sid, "index": n, "chars": len(text), "model": model}


def run_analyze_image(event: dict, store, client=None, models: list[str] | None = None, now=None) -> dict:
    """AnalyzeImage; with event["mode"] == "video_sheets", AnalyzeSheets (one request over a video's contact sheets)."""
    if event.get("mode") == "video_sheets":
        return run_analyze_sheets(event, store, client, models, now)
    eid, sid, key = event["evaluation_id"], event["source_id"], event["image_key"]
    # the key must live under this evaluation's docs prefix (never trust the payload blindly)
    if not key.startswith(derived(eid, "docs", sid, "images") + "/"):
        raise PipelineError("internal", "image key outside the evaluation prefix")
    seq = os.path.basename(key).split("_", 1)[0]
    ops = provider_ops(run_config.load(store, eid))
    client = client or ops.default_client()
    _start_step(client)
    raw = store.get_bytes(key)
    ext = key.rsplit(".", 1)[-1]
    prog = Progress(store, eid)
    prompt = image_prompt(scorecard_context.load(store, eid))
    try:
        text, model, usage = ops.analyse_image(client, models or ops.vision_models(), raw, ext, prompt)
    except (TransientError, PipelineError):
        raise  # retried by the state machine (transient) or a key/billing problem: never this image's fault
    except Exception as e:  # undecodable image etc.: soft failure, the evaluation continues
        prog.mark("img", sid, seq, False)
        return {"source_id": sid, "image_key": key, "ok": False, "error": f"{type(e).__name__}: {str(e)[:120]}"}
    stamp = (now or datetime.now(timezone.utc)).strftime("%Y-%m-%d %H:%M")
    store.put_text(analysis_key(key),
                   f"<!-- source: {os.path.basename(key)} | model: {model} | analysed: {stamp} -->\n\n{text}\n",
                   "text/markdown; charset=utf-8")
    store.put_json(usage_key(key), usage_doc(model, usage))
    prog.mark("img", sid, seq, True)
    return {"source_id": sid, "image_key": key, "ok": True, "model": model}


# --------------------------------------------------------------------------- AnalyzeSheets (video frames)
def _sheet_keys(eid: str, sid: str, batch: object) -> list[str]:
    """The batch's sheet keys, each under this evaluation's video/{sid}/frames/ prefix (never trust the payload)."""
    prefix = frames_prefix(eid, sid) + "/"
    keys = batch.get("sheets") if isinstance(batch, dict) else None
    if not isinstance(keys, list) or not 1 <= len(keys) <= MAX_SHEETS_PER_REQUEST:
        raise PipelineError("internal", "sheet batch must list 1-8 sheet keys")
    for k in keys:
        name = k[len(prefix):] if isinstance(k, str) and k.startswith(prefix) else ""
        if not name or "/" in name or ".." in name or not name.endswith(".jpg"):
            raise PipelineError("internal", "sheet key outside the evaluation prefix")
    return keys


def frame_list(sheets: list[dict]) -> str:
    """The prompt's list of every frame time, sheet by sheet (the times are also printed on the frames)."""
    lines = [f"This request has {len(sheets)} contact sheet(s). Frame timestamps, in cell order:"]
    for i, sh in enumerate(sheets, 1):
        cells = []
        for c in sh.get("cells", []):
            item = fmt_ts(c["t"])
            if float(c.get("until", c["t"])) - float(c["t"]) >= HOLD_NOTE_S:
                item += f" (unchanged until {fmt_ts(c['until'])})"
            cells.append(item)
        lines.append(f"Sheet {i}: " + ", ".join(cells))
    return "\n".join(lines)


def run_analyze_sheets(event: dict, store, client=None, models: list[str] | None = None, now=None) -> dict:
    """One vision request over a batch of a video's contact sheets -> frames/batch_NN.md (the model's notes with a
    source/model header) and batch_NN.json (frames covered, the PlanFrames run it belongs to, usage)."""
    eid, sid, batch = event["evaluation_id"], event["source_id"], event.get("batch")
    keys = _sheet_keys(eid, sid, batch)
    n = int(batch.get("index") or 0)
    prefix = frames_prefix(eid, sid)
    plan = store.get_json(f"{prefix}/sheets.json")
    by_key = {sh.get("key"): sh for sh in plan.get("sheets", [])}
    if any(k not in by_key for k in keys):
        raise PipelineError("internal", "sheet batch does not match sheets.json")
    sheets = [by_key[k] for k in keys]
    settings = video_settings.load(store, eid)
    ops = provider_ops(run_config.load(store, eid))
    client = client or ops.default_client()
    _start_step(client)
    raws = [store.get_bytes(k) for k in keys]
    prompt = video_frames_prompt(scorecard_context.load(store, eid), frame_list(sheets))
    prog = Progress(store, eid)
    try:
        text, model, usage = ops.analyse_sheets(client, models or ops.video_models(), raws, prompt,
                                                settings.max_output_tokens)
    except (TransientError, PipelineError):
        raise  # retried by the state machine (transient) or a key/billing problem
    except Exception as e:  # undecodable sheet etc.: soft failure, the transcript and other batches still count
        prog.mark("vis", sid, n, False)
        return {"source_id": sid, "index": n, "ok": False, "error": f"{type(e).__name__}: {str(e)[:120]}"}
    times = [float(c["t"]) for sh in sheets for c in sh.get("cells", [])]
    stamp = (now or datetime.now(timezone.utc)).strftime("%Y-%m-%d %H:%M")
    names = ", ".join(os.path.basename(k) for k in keys)
    store.put_text(f"{prefix}/batch_{n:02d}.md",
                   f"<!-- source: {plan.get('source', '')} | sheets: {names} | model: {model} | analysed: {stamp} "
                   f"-->\n\n{text}\n", "text/markdown; charset=utf-8")
    store.put_json(f"{prefix}/batch_{n:02d}.json", {
        "batch": n, "run": plan.get("run"), "sheets": [sh.get("index") for sh in sheets], "frames": len(times),
        "start": min(times, default=0.0), "end": max(times, default=0.0), **usage_doc(model, usage)})
    prog.mark("vis", sid, n, True)
    return {"source_id": sid, "index": n, "ok": True, "model": model}
