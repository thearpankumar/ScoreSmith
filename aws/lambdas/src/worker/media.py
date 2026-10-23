"""Transcription and image-analysis steps, for either provider.

The provider of the run comes from run_config (saved by ingest). bedrock_ops and openrouter_ops expose the same
interface (default_client, transcribe_models, vision_models, transcribe, analyse_image), and the outputs do not depend
on the provider: chunk c{n}.txt / c{n}.json, the image's .analysis.md with its source/model header, and the progress
markers.
"""
from __future__ import annotations

import os
from datetime import datetime, timezone

from . import run_config, scorecard_context
from .errors import PipelineError, TransientError
from .images import analysis_key
from .progress import Progress
from .prompts import image_prompt
from .store import derived


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
    store.put_json(f"{prefix}/c{n}.json", {"chunk": n, "start": chunk.get("start"), "end": chunk.get("end"),
                                           "model": model, "chars": len(text),
                                           "tokens_in": tokens_in, "tokens_out": tokens_out})
    Progress(store, eid).mark("chunk", sid, n, True)
    return {"source_id": sid, "index": n, "chars": len(text), "model": model}


def run_analyze_image(event: dict, store, client=None, models: list[str] | None = None, now=None) -> dict:
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
        text, model, _usage = ops.analyse_image(client, models or ops.vision_models(), raw, ext, prompt)
    except (TransientError, PipelineError):
        raise  # retried by the state machine (transient) or a key/billing problem: never this image's fault
    except Exception as e:  # undecodable image etc.: soft failure, the evaluation continues
        prog.mark("img", sid, seq, False)
        return {"source_id": sid, "image_key": key, "ok": False, "error": f"{type(e).__name__}: {str(e)[:120]}"}
    stamp = (now or datetime.now(timezone.utc)).strftime("%Y-%m-%d %H:%M")
    store.put_text(analysis_key(key),
                   f"<!-- source: {os.path.basename(key)} | model: {model} | analysed: {stamp} -->\n\n{text}\n",
                   "text/markdown; charset=utf-8")
    prog.mark("img", sid, seq, True)
    return {"source_id": sid, "image_key": key, "ok": True, "model": model}
