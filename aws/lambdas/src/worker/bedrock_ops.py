"""AWS calls for Bedrock runs: transcription (Amazon Transcribe, then Bedrock Voxtral), image analysis and video
frame-sheet analysis, with model fallbacks.

Ported from scripts/transcribe_videos.py and scripts/analyze_images.py. Auth is the Lambda execution role (no API key).
The step entry points (S3 in/out, progress markers) are in media.py.
"""
from __future__ import annotations

import os
import re
import time

from .errors import TransientError
from .images import TooLarge, analyse_many_with_shrink, analyse_with_shrink, is_size_error
from .prompts import IMAGE_MAX_TOKENS, IMAGE_PROMPT, TRANSCRIBE_PROMPT

# Since Oct 2026 Bedrock's Voxtral (now in its Legacy period) rejects every audio request with "Model does not support
# audio modality", on Converse, InvokeModel and the OpenAI-compatible endpoints alike. Bedrock runs therefore
# transcribe with Amazon Transcribe (a batch job reading the chunk from S3); Voxtral stays as the fallback in case AWS
# restores audio input.
AMAZON_TRANSCRIBE = "amazon.transcribe"
TRANSCRIBE_MODELS = [AMAZON_TRANSCRIBE, "mistral.voxtral-small-24b-2507", "mistral.voxtral-mini-3b-2507"]
# How long one step waits for its job (Lambda timeout 900 s). A step that runs out of time raises TransientError; the
# state machine's retry then finds the same job by name and keeps waiting instead of paying for a second one.
TRANSCRIBE_WAIT_S = 720.0
VISION_MODELS = [
    "moonshotai.kimi-k2.5",
    "qwen.qwen3-vl-235b-a22b",
    "us.meta.llama4-maverick-17b-instruct-v1:0",
]
# Video contact sheets (several images per request). Nova 2 Lite bills a flat ~230 tokens per image, so a 3x3 sheet
# costs about as much as one small image; its extended thinking is off unless requested. Qwen3-VL is the fallback.
VIDEO_MODELS = ["us.amazon.nova-2-lite-v1:0", "qwen.qwen3-vl-235b-a22b"]


def bedrock_client(region: str | None = None):
    import boto3
    from botocore.config import Config

    return boto3.client("bedrock-runtime", region_name=region or os.environ.get("AWS_REGION", "us-east-1"),
                        config=Config(read_timeout=300, retries={"max_attempts": 6, "mode": "adaptive"}))


# the interface media.py dispatches on (openrouter_ops has the same functions)
default_client = bedrock_client


def transcribe_models() -> list[str]:
    return list(TRANSCRIBE_MODELS)


def vision_models() -> list[str]:
    return list(VISION_MODELS)


def video_models() -> list[str]:
    return list(VIDEO_MODELS)


def _is_client_error(e: Exception) -> bool:
    try:
        from botocore.exceptions import BotoCoreError, ClientError

        return isinstance(e, (ClientError, BotoCoreError))
    except ImportError:  # pragma: no cover
        return False


def _text_of(resp: dict) -> str:
    return "".join(b.get("text", "") for b in resp["output"]["message"]["content"]).strip()


# --------------------------------------------------------------------------- transcription
def transcribe_client(region: str | None = None):
    import boto3
    from botocore.config import Config

    return boto3.client("transcribe", region_name=region or os.environ.get("AWS_REGION", "us-east-1"),
                        config=Config(retries={"max_attempts": 6, "mode": "adaptive"}))


def safe_job_name(name: str) -> str:
    """Transcribe job names allow [0-9a-zA-Z._-], up to 200 characters. media.py names one job per chunk
    ("qs-eval-{eid}-{sid}-c{n}"), so a retried step or evaluation finds the job it already started; the qs-eval-
    prefix is also what the Lambda role may read and delete."""
    return re.sub(r"[^0-9A-Za-z._-]", "-", name)[:200]


def _error_code(e: Exception) -> str:
    return ((getattr(e, "response", None) or {}).get("Error") or {}).get("Code", "")


def _start_job(t, s3_uri: str, job_name: str) -> None:
    try:
        t.start_transcription_job(TranscriptionJobName=job_name, Media={"MediaFileUri": s3_uri}, MediaFormat="mp3",
                                  IdentifyLanguage=True)
    except Exception as e:
        if _error_code(e) != "ConflictException":
            raise
        # the job already exists (this step or evaluation is a retry): wait for it below


class Transcript(str):
    """A transcript's text that also carries its timed segments ([{"start","end","text"}], seconds from the start of
    the chunk). A plain str works wherever a Transcript is expected (it just has no segments)."""

    segments: list = []

    def __new__(cls, text: str, segments=()):
        obj = super().__new__(cls, text)
        obj.segments = list(segments)
        return obj


def _float(value) -> float | None:
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def transcript_segments(results: dict) -> list[dict]:
    """Timed segments of a Transcribe output: its `audio_segments`, else sentences rebuilt from the word `items`."""
    out: list[dict] = []
    for seg in results.get("audio_segments") or []:
        start, end = _float(seg.get("start_time")), _float(seg.get("end_time"))
        text = " ".join(str(seg.get("transcript") or "").split())
        if start is not None and text:
            out.append({"start": start, "end": end if end is not None else start, "text": text})
    if out:
        return out
    words: list[str] = []
    start = end = None
    for item in results.get("items") or []:
        content = str(((item.get("alternatives") or [{}])[0] or {}).get("content") or "")
        if item.get("type") == "pronunciation":
            t0, t1 = _float(item.get("start_time")), _float(item.get("end_time"))
            start = t0 if start is None else start
            end = t1 if t1 is not None else end
            words.append(content)
        elif item.get("type") == "punctuation" and words:
            words[-1] += content
            if content in (".", "?", "!") and start is not None:
                out.append({"start": start, "end": end if end is not None else start, "text": " ".join(words)})
                words, start, end = [], None, None
    if words and start is not None:
        out.append({"start": start, "end": end if end is not None else start, "text": " ".join(words)})
    return out


def _fetch_transcript(uri: str, session=None) -> str:
    """The job's output, from the short-lived pre-signed URL Transcribe hands out (no output bucket to grant), as a
    Transcript (text + timed segments)."""
    import requests

    try:
        r = (session or requests).get(uri, timeout=(10, 60))
        r.raise_for_status()
        body = r.json()
    except Exception as e:  # network trouble or an expired URL: the retried step asks for a fresh one
        raise TransientError(f"could not download the Amazon Transcribe output: {type(e).__name__}") from None
    results = body.get("results") or {}
    transcripts = results.get("transcripts") or [{}]
    return Transcript(str(transcripts[0].get("transcript") or "").strip(), transcript_segments(results))


def transcribe_job(t, s3_uri: str, job_name: str, *, sleep=time.sleep, clock=time.monotonic,
                   wait_s: float = TRANSCRIBE_WAIT_S, session=None) -> str:
    """Run (or pick up) the Amazon Transcribe job for one chunk, language identified automatically. A FAILED job is
    deleted and started once more; a job still running when the wait ends raises TransientError (retried step)."""
    deadline = clock() + wait_s
    _start_job(t, s3_uri, job_name)
    restarted, delay = False, 2.0
    while True:
        job = t.get_transcription_job(TranscriptionJobName=job_name)["TranscriptionJob"]
        status = job.get("TranscriptionJobStatus")
        if status == "COMPLETED":
            return _fetch_transcript(job["Transcript"]["TranscriptFileUri"], session)
        if status == "FAILED":
            reason = str(job.get("FailureReason") or "no reason given")[:200]
            if restarted:
                raise RuntimeError(f"Amazon Transcribe job failed: {reason}")
            print(f"    {job_name}: FAILED ({reason}); starting it again")
            t.delete_transcription_job(TranscriptionJobName=job_name)
            _start_job(t, s3_uri, job_name)
            restarted = True
            continue
        if clock() + delay > deadline:
            raise TransientError(f"Amazon Transcribe job {job_name} still {status} after {wait_s:.0f}s")
        sleep(delay)
        delay = min(10.0, delay * 1.5)


def transcribe(client, models: list[str], audio: bytes, prompt: str = TRANSCRIBE_PROMPT, *,
               s3_uri: str | None = None, job_name: str | None = None, transcriber=None) -> tuple[str, str, dict]:
    """The first model that answers. `amazon.transcribe` needs the chunk's S3 location (s3_uri) and a job name;
    without them it is skipped; its third value is {"segments": [...]}, the timed sentences (seconds from the start
    of the chunk). The Bedrock models get the audio bytes."""
    last: Exception | None = None
    for model in models:
        try:
            if model == AMAZON_TRANSCRIBE:
                if not (s3_uri and job_name):
                    continue
                text = transcribe_job(transcriber or transcribe_client(), s3_uri, safe_job_name(job_name))
                return str(text), model, {"segments": list(getattr(text, "segments", []))}
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


# --------------------------------------------------------------------------- images
def analyse(client, models: list[str], data: bytes, fmt: str, prompt: str = IMAGE_PROMPT) -> tuple[str, str, dict]:
    last: Exception | None = None
    for model in models:
        try:
            r = client.converse(
                modelId=model,
                messages=[{"role": "user", "content": [{"image": {"format": fmt, "source": {"bytes": data}}},
                                                       {"text": prompt}]}],
                inferenceConfig={"maxTokens": IMAGE_MAX_TOKENS, "temperature": 0},
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


def analyse_image(client, models: list[str], raw: bytes, ext: str,
                  prompt: str = IMAGE_PROMPT) -> tuple[str, str, dict]:
    """prepare + analyse, shrinking the image while Bedrock says the request body is too big."""
    return analyse_with_shrink(lambda data, fmt: analyse(client, models, data, fmt, prompt), raw, ext)


# --------------------------------------------------------------------------- video contact sheets
def _sheets_once(client, models: list[str], images: list[tuple[bytes, str]], prompt: str,
                 max_tokens: int) -> tuple[str, str, dict]:
    content: list[dict] = [{"image": {"format": fmt, "source": {"bytes": data}}} for data, fmt in images]
    content.append({"text": prompt})
    last: Exception | None = None
    for model in models:
        try:
            r = client.converse(modelId=model, messages=[{"role": "user", "content": content}],
                                inferenceConfig={"maxTokens": max_tokens, "temperature": 0})
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
    raise TransientError(f"all video models failed (last: {last})")


def analyse_sheets(client, models: list[str], raws: list[bytes], prompt: str,
                   max_tokens: int) -> tuple[str, str, dict]:
    """One Converse request with several contact sheets (JPEG) and the prompt; shrinks the sheets while Bedrock says
    the body is too big. Usage is Bedrock's {inputTokens, outputTokens} (Bedrock does not report a cost)."""
    return analyse_many_with_shrink(lambda images: _sheets_once(client, models, images, prompt, max_tokens), raws)
