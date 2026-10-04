"""Bedrock calls: Voxtral transcription and vision analysis with model fallbacks.

Ported from scripts/transcribe_videos.py and scripts/analyze_images.py. Auth is the Lambda execution role (no API key).
"""
from __future__ import annotations

import io
import os
from datetime import datetime, timezone

from .errors import PipelineError, TransientError
from .progress import Progress
from .store import derived

TRANSCRIBE_MODELS = ["mistral.voxtral-small-24b-2507", "mistral.voxtral-mini-3b-2507"]
VISION_MODELS = [
    "moonshotai.kimi-k2.5",
    "qwen.qwen3-vl-235b-a22b",
    "us.meta.llama4-maverick-17b-instruct-v1:0",
]
# tested wording; longer prompts made the model translate
TRANSCRIBE_PROMPT = "Transcribe this audio verbatim. Output only the transcript."
IMAGE_PROMPT = """You are analysing one image taken from a hackathon solution document or slide deck.

Reply in Markdown with exactly these sections:

## Text (OCR)
Transcribe ALL text visible in the image exactly as written, in reading order. Keep table rows on one line each,
separated by " | ". If there is no text, write "(none)".

## Description
Say what kind of image this is (screenshot, architecture diagram, flow chart, chart/graph, table, photo, logo, other).
Describe the key elements, how they connect or flow, and any numbers/values shown. Be factual and concise.

## Summary
One sentence."""

# Converse documents 3.75 MB / 8000 px per image, but that limit is really on the request BODY, where the image is
# base64-encoded (+33%). Measured: Kimi K2.5 and Qwen3-VL accept raw images up to ~2.61 MiB; one budget safe for all:
MAX_BYTES = 2_700_000
MAX_PX = 8000
MAX_IMAGE_PIXELS = 100_000_000  # decompression-bomb guard for Pillow


def bedrock_client(region: str | None = None):
    import boto3
    from botocore.config import Config

    return boto3.client("bedrock-runtime", region_name=region or os.environ.get("AWS_REGION", "us-east-1"),
                        config=Config(read_timeout=300, retries={"max_attempts": 6, "mode": "adaptive"}))


def _is_client_error(e: Exception) -> bool:
    try:
        from botocore.exceptions import BotoCoreError, ClientError

        return isinstance(e, (ClientError, BotoCoreError))
    except ImportError:  # pragma: no cover
        return False


def _text_of(resp: dict) -> str:
    return "".join(b.get("text", "") for b in resp["output"]["message"]["content"]).strip()


# --------------------------------------------------------------------------- transcription
def transcribe(client, models: list[str], audio: bytes, prompt: str = TRANSCRIBE_PROMPT) -> tuple[str, str, dict]:
    last: Exception | None = None
    for model in models:
        try:
            r = client.converse(
                modelId=model,
                messages=[{"role": "user", "content": [{"audio": {"format": "mp3", "source": {"bytes": audio}}},
                                                       {"text": prompt}]}],
                inferenceConfig={"maxTokens": 8000, "temperature": 0},
            )
            return _text_of(r), model, r.get("usage", {})
        except Exception as e:
            if not _is_client_error(e) and not isinstance(e, RuntimeError):
                raise
            last = e
            print(f"    {model}: {type(e).__name__}: {str(e)[:120]}; trying next model")
    raise TransientError(f"all transcription models failed (last: {last})")


def run_transcribe_chunk(event: dict, store, client=None, models: list[str] | None = None) -> dict:
    eid, sid, chunk = event["evaluation_id"], event["source_id"], event["chunk"]
    n = int(chunk["index"])
    client = client or bedrock_client()
    audio = store.get_bytes(chunk["key"])
    text, model, usage = transcribe(client, models or TRANSCRIBE_MODELS, audio)
    prefix = derived(eid, "video", sid, "chunks")
    store.put_text(f"{prefix}/c{n}.txt", text)
    store.put_json(f"{prefix}/c{n}.json", {"chunk": n, "start": chunk.get("start"), "end": chunk.get("end"),
                                           "model": model, "chars": len(text),
                                           "tokens_in": usage.get("inputTokens", 0), "tokens_out": usage.get("outputTokens", 0)})
    Progress(store, eid).mark("chunk", sid, n, True)
    return {"source_id": sid, "index": n, "chars": len(text), "model": model}


# --------------------------------------------------------------------------- images
class TooLarge(Exception):
    """The service rejected the request body as too big (shrink the image and retry)."""


def is_size_error(e: Exception) -> bool:
    m = str(e).lower()
    return "length limit" in m or "too large" in m or "exceeds the maximum" in m or "request body" in m


def _pil():
    from PIL import Image

    Image.MAX_IMAGE_PIXELS = MAX_IMAGE_PIXELS
    return Image


def encode_best(img, budget: int) -> bytes:
    """Highest JPEG quality (bisection, 30..95) whose size fits the budget; shrink pixels only if even q=30 doesn't."""
    Image = _pil()
    for _ in range(8):
        def enc(q: int, im=img) -> bytes:
            buf = io.BytesIO()
            im.save(buf, "JPEG", quality=q, optimize=True)
            return buf.getvalue()

        best = enc(95)
        if len(best) <= budget:
            return best
        lo, hi, found = 30, 94, None
        while lo <= hi:
            mid = (lo + hi) // 2
            data = enc(mid)
            if len(data) <= budget:
                found, lo = data, mid + 1
            else:
                hi = mid - 1
        if found:
            return found
        scale = max(0.5, min(0.9, (budget / len(enc(30))) ** 0.5 * 0.95))  # size ~ pixels ~ scale^2
        img = img.resize((max(1, int(img.width * scale)), max(1, int(img.height * scale))), Image.LANCZOS)
    raise RuntimeError("could not shrink image under the size budget")


def prepare(raw: bytes, ext: str, budget: int = MAX_BYTES) -> tuple[bytes, str]:
    """Bytes + Converse format. Original bytes are sent untouched when they fit; otherwise re-encoded at the
    highest quality that fits `budget`."""
    Image = _pil()
    fmt = {"jpg": "jpeg", "jpeg": "jpeg", "png": "png", "webp": "webp", "gif": "gif"}.get(ext.lower().lstrip("."))
    img = Image.open(io.BytesIO(raw))
    if fmt and len(raw) <= budget and max(img.size) <= MAX_PX:
        return raw, fmt
    if img.mode in ("RGBA", "LA") or (img.mode == "P" and "transparency" in img.info):
        rgba = img.convert("RGBA")
        bg = Image.new("RGB", rgba.size, "white")
        bg.paste(rgba, mask=rgba.split()[3])
        img = bg
    else:
        img = img.convert("RGB")
    if max(img.size) > MAX_PX:
        img.thumbnail((MAX_PX, MAX_PX), Image.LANCZOS)
    return encode_best(img, budget), "jpeg"


def analyse(client, models: list[str], data: bytes, fmt: str) -> tuple[str, str, dict]:
    last: Exception | None = None
    for model in models:
        try:
            r = client.converse(
                modelId=model,
                messages=[{"role": "user", "content": [{"image": {"format": fmt, "source": {"bytes": data}}},
                                                       {"text": IMAGE_PROMPT}]}],
                inferenceConfig={"maxTokens": 3000, "temperature": 0},
            )
            text = _text_of(r)
            if not text:
                raise RuntimeError("empty response")
            return text, model, r.get("usage", {})
        except Exception as e:
            if not _is_client_error(e) and not isinstance(e, RuntimeError):
                raise
            if is_size_error(e):
                raise TooLarge(str(e)[:120]) from e
            last = e
            print(f"    {model}: {type(e).__name__}: {str(e)[:120]}; trying next model")
    raise TransientError(f"all vision models failed (last: {last})")


def analyse_image(client, models: list[str], raw: bytes, ext: str) -> tuple[str, str, dict]:
    """prepare + analyse, shrinking the budget by 15% and retrying if the service still says the body is too big."""
    budget = MAX_BYTES
    for _ in range(4):
        data, fmt = prepare(raw, ext, budget)
        try:
            return analyse(client, models, data, fmt)
        except TooLarge:
            budget = int(min(budget, len(data)) * 0.85)
            print(f"    body too large ({len(data) / 1e6:.2f} MB); retrying at <= {budget / 1e6:.2f} MB")
    raise RuntimeError("image still too large after 3 shrink attempts")


def analysis_key(image_key: str) -> str:
    stem = image_key.rsplit(".", 1)[0]
    return stem + ".analysis.md"


def run_analyze_image(event: dict, store, client=None, models: list[str] | None = None, now=None) -> dict:
    eid, sid, key = event["evaluation_id"], event["source_id"], event["image_key"]
    # the key must live under this evaluation's docs prefix (never trust the payload blindly)
    if not key.startswith(derived(eid, "docs", sid, "images") + "/"):
        raise PipelineError("internal", "image key outside the evaluation prefix")
    seq = os.path.basename(key).split("_", 1)[0]
    client = client or bedrock_client()
    raw = store.get_bytes(key)
    ext = key.rsplit(".", 1)[-1]
    prog = Progress(store, eid)
    try:
        text, model, usage = analyse_image(client, models or VISION_MODELS, raw, ext)
    except TransientError:
        raise
    except Exception as e:  # undecodable image etc.: soft failure, the evaluation continues
        prog.mark("img", sid, seq, False)
        return {"source_id": sid, "image_key": key, "ok": False, "error": f"{type(e).__name__}: {str(e)[:120]}"}
    stamp = (now or datetime.now(timezone.utc)).strftime("%Y-%m-%d %H:%M")
    store.put_text(analysis_key(key),
                   f"<!-- source: {os.path.basename(key)} | model: {model} | analysed: {stamp} -->\n\n{text}\n",
                   "text/markdown; charset=utf-8")
    prog.mark("img", sid, seq, True)
    return {"source_id": sid, "image_key": key, "ok": True, "model": model}
