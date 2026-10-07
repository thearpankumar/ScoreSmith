"""OpenRouter calls: audio transcription and vision analysis with model fallbacks.

Replaces the former Bedrock client. Plain HTTPS to `{OPENROUTER_BASE_URL}/chat/completions` with `requests`.
The API key is NOT in the Lambda environment: it is read once per container from SSM Parameter Store
(SecureString, name in OPENROUTER_SECRET_PARAM). OPENROUTER_API_KEY in the environment overrides it (local tests only).
"""
from __future__ import annotations

import base64
import io
import os
import random
import re
import time
from datetime import datetime, timezone

from .errors import PipelineError, TransientError
from .progress import Progress
from .store import derived

DEFAULT_BASE_URL = "https://openrouter.ai/api/v1"
DEFAULT_TRANSCRIBE_MODELS = ["mistralai/voxtral-small-24b-2507", "google/gemini-2.5-flash"]
DEFAULT_VISION_MODELS = ["openai/gpt-6-luna", "deepseek/deepseek-v4.1-flash", "google/gemini-2.5-flash"]
APP_URL = os.environ.get("OPENROUTER_APP_URL", "https://github.com/thearpankumar/QualityAnalysisTool")
APP_TITLE = "Quality Analysis Tool (AWS pipeline)"
TIMEOUT = (10, 300)  # (connect, read) seconds


def _env_models(name: str, default: list[str]) -> list[str]:
    raw = os.environ.get(name, "")
    models = [m.strip() for m in raw.split(",") if m.strip()]
    return models or list(default)


def transcribe_models() -> list[str]:
    return _env_models("OPENROUTER_TRANSCRIBE_MODELS", DEFAULT_TRANSCRIBE_MODELS)


def vision_models() -> list[str]:
    return _env_models("OPENROUTER_VISION_MODELS", DEFAULT_VISION_MODELS)


# module-level aliases (evaluated at import; handlers use the functions above so env changes are honoured)
TRANSCRIBE_MODELS = transcribe_models()
VISION_MODELS = vision_models()
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

# Request-size budget. The image travels base64-encoded (+33%) inside the JSON body; one raw-size budget that is
# safe for every vision model in the chain:
MAX_BYTES = 2_700_000
MAX_PX = 8000
MAX_IMAGE_PIXELS = 100_000_000  # decompression-bomb guard for Pillow


class ModelError(RuntimeError):
    """This model could not answer (4xx other than auth, empty/odd response, ...): try the next model."""


class TooLarge(Exception):
    """The service rejected the request body as too big (shrink the image and retry)."""


def is_size_error(e: Exception) -> bool:
    m = str(e).lower()
    return ("length limit" in m or "too large" in m or "exceeds the maximum" in m or "request body" in m
            or ("payload" in m and "large" in m))


_SECRET_RE = re.compile(r"(sk-or-[A-Za-z0-9_\-]+|Bearer\s+[A-Za-z0-9._\-]+)", re.I)
_KEY_CACHE: dict[str, str] = {}


def scrub(text: object, limit: int = 300) -> str:
    """Never let an API key (or a bearer header) reach a log line or an exception message."""
    out = _SECRET_RE.sub("[redacted]", str(text))
    key = _KEY_CACHE.get("key")
    if key:
        out = out.replace(key, "[redacted]")
    return out[:limit]


# --------------------------------------------------------------------------- API key (SSM, cached per container)
def reset_key_cache() -> None:
    _KEY_CACHE.clear()


def _ssm_client():
    import boto3

    return boto3.client("ssm", region_name=os.environ.get("AWS_REGION", "us-east-1"))


def get_api_key() -> str:
    if "key" in _KEY_CACHE:
        return _KEY_CACHE["key"]
    key = os.environ.get("OPENROUTER_API_KEY", "").strip()  # local-test override
    if not key:
        param = os.environ.get("OPENROUTER_SECRET_PARAM", "").strip()
        if not param:
            raise PipelineError("internal", "OpenRouter key is not configured (OPENROUTER_SECRET_PARAM unset)")
        try:
            key = _ssm_client().get_parameter(Name=param, WithDecryption=True)["Parameter"]["Value"].strip()
        except Exception as e:  # botocore ClientError / BotoCoreError
            code = getattr(e, "response", {}).get("Error", {}).get("Code", "")
            if code in ("ParameterNotFound", "AccessDeniedException", "InvalidKeyId"):
                raise PipelineError("internal", f"cannot read the OpenRouter key from SSM ({code}); "
                                                "re-run aws/bootstrap with OPENROUTER_API_KEY set") from None
            raise TransientError(f"SSM unavailable: {type(e).__name__}") from None
        if not key:
            raise PipelineError("internal", "OpenRouter key parameter is empty; re-run aws/bootstrap")
    _KEY_CACHE["key"] = key
    return key


# --------------------------------------------------------------------------- HTTP client
class OpenRouterClient:
    """POST /chat/completions with bounded, jittered retries. Everything transient ends as TransientError so the
    state machine retries; auth/billing problems are permanent PipelineErrors."""

    def __init__(self, session=None, base_url: str | None = None, sleep=time.sleep, rand=random.random,
                 clock=time.monotonic, max_attempts: int = 4, budget_s: float = 420.0):
        self._session = session
        self.base_url = (base_url or os.environ.get("OPENROUTER_BASE_URL") or DEFAULT_BASE_URL).rstrip("/")
        self.sleep, self.rand, self.clock = sleep, rand, clock
        self.max_attempts, self.budget_s = max_attempts, budget_s

    @property
    def session(self):
        if self._session is None:
            import requests

            self._session = requests.Session()
        return self._session

    def _headers(self) -> dict:
        return {"Authorization": f"Bearer {get_api_key()}", "Content-Type": "application/json",
                "HTTP-Referer": APP_URL, "X-OpenRouter-Title": APP_TITLE, "X-Title": APP_TITLE}

    @staticmethod
    def _message(body: object, fallback: str) -> str:
        if isinstance(body, dict):
            err = body.get("error")
            if isinstance(err, dict) and err.get("message"):
                return str(err["message"])
            if isinstance(err, str):
                return err
        return fallback

    @staticmethod
    def _classify(status: int, message: str) -> str:
        """-> 'ok' | 'retry' | 'auth' | 'size' | 'model'"""
        if status == 200:
            return "ok"
        if status in (401, 402, 403):
            return "auth"
        if status == 413 or (status == 400 and is_size_error(Exception(message))):
            return "size"
        if status in (408, 429) or status >= 500:
            return "retry"
        return "model"

    def chat(self, payload: dict) -> dict:
        start = self.clock()
        last = "no attempt"
        headers = self._headers()  # PipelineError / TransientError from the key lookup propagate untouched
        for attempt in range(1, self.max_attempts + 1):
            try:
                resp = self.session.post(f"{self.base_url}/chat/completions", headers=headers, json=payload,
                                         timeout=TIMEOUT)
            except Exception as e:  # requests connection / timeout errors
                last = f"{type(e).__name__}: {scrub(e, 120)}"
            else:
                try:
                    body = resp.json()
                except Exception:
                    body = None
                status = int(resp.status_code)
                message = self._message(body, scrub(getattr(resp, "text", "") or f"HTTP {status}", 200))
                if status == 200 and isinstance(body, dict):
                    # OpenRouter can answer 200 with {"error": {...}} (or an error on the choice)
                    err = body.get("error") or ((body.get("choices") or [{}])[0] or {}).get("error")
                    if err:
                        code = err.get("code") if isinstance(err, dict) else None
                        status = int(code) if isinstance(code, int) else 502
                        message = self._message({"error": err}, "error in 200 response")
                kind = self._classify(status, message)
                last = f"HTTP {status}: {scrub(message, 200)}"
                if kind == "ok":
                    if not isinstance(body, dict) or not body.get("choices"):
                        raise ModelError("response had no choices")
                    return body
                if kind == "auth":
                    raise PipelineError("internal", f"OpenRouter rejected the request ({last}); check the API key and "
                                                    "credits, then re-run aws/bootstrap")
                if kind == "size":
                    raise TooLarge(last)
                if kind == "model":
                    raise ModelError(last)
            # transient: back off (jittered) unless out of attempts / time
            if attempt == self.max_attempts or self.clock() - start > self.budget_s:
                break
            self.sleep(min(20.0, 1.5 * 2 ** (attempt - 1)) * (0.5 + self.rand()))
        raise TransientError(f"OpenRouter unavailable after retries ({last})")


_client: OpenRouterClient | None = None


def openrouter_client() -> OpenRouterClient:
    global _client
    if _client is None:
        _client = OpenRouterClient()
    return _client


def _text_of(resp: dict) -> str:
    content = resp["choices"][0].get("message", {}).get("content") or ""
    if isinstance(content, list):
        content = "".join(p.get("text", "") for p in content if isinstance(p, dict))
    return str(content).strip()


def _usage(resp: dict) -> dict:
    u = resp.get("usage") or {}
    return {"tokens_in": int(u.get("prompt_tokens") or 0), "tokens_out": int(u.get("completion_tokens") or 0)}


# --------------------------------------------------------------------------- transcription
def transcribe(client, models: list[str], audio: bytes, prompt: str = TRANSCRIBE_PROMPT) -> tuple[str, str, dict]:
    b64 = base64.b64encode(audio).decode("ascii")
    last: Exception | None = None
    for model in models:
        payload = {
            "model": model,
            "messages": [{"role": "user", "content": [
                {"type": "text", "text": prompt},
                {"type": "input_audio", "input_audio": {"data": b64, "format": "mp3"}},
            ]}],
            "temperature": 0,
            "max_tokens": 8000,
        }
        try:
            r = client.chat(payload)
            return _text_of(r), model, _usage(r)
        except (ModelError, TooLarge, TransientError) as e:
            last = e
            print(f"    {model}: {type(e).__name__}: {scrub(e, 120)}; trying next model")
    raise TransientError(f"all transcription models failed (last: {scrub(last)})")


def run_transcribe_chunk(event: dict, store, client=None, models: list[str] | None = None) -> dict:
    eid, sid, chunk = event["evaluation_id"], event["source_id"], event["chunk"]
    n = int(chunk["index"])
    client = client or openrouter_client()
    audio = store.get_bytes(chunk["key"])
    text, model, usage = transcribe(client, models or transcribe_models(), audio)
    prefix = derived(eid, "video", sid, "chunks")
    store.put_text(f"{prefix}/c{n}.txt", text)
    store.put_json(f"{prefix}/c{n}.json", {"chunk": n, "start": chunk.get("start"), "end": chunk.get("end"),
                                           "model": model, "chars": len(text),
                                           "tokens_in": usage.get("tokens_in", 0), "tokens_out": usage.get("tokens_out", 0)})
    Progress(store, eid).mark("chunk", sid, n, True)
    return {"source_id": sid, "index": n, "chars": len(text), "model": model}


# --------------------------------------------------------------------------- images
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
    """Bytes + image format. Original bytes are sent untouched when they fit; otherwise re-encoded at the
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
    url = f"data:image/{fmt};base64," + base64.b64encode(data).decode("ascii")
    last: Exception | None = None
    for model in models:
        payload = {
            "model": model,
            "messages": [{"role": "user", "content": [
                {"type": "image_url", "image_url": {"url": url}},
                {"type": "text", "text": IMAGE_PROMPT},
            ]}],
            "temperature": 0,
            "max_tokens": 3000,
        }
        try:
            r = client.chat(payload)
            text = _text_of(r)
            if not text:
                raise ModelError("empty response")
            return text, model, _usage(r)
        except TooLarge:
            raise
        except (ModelError, TransientError) as e:
            last = e
            print(f"    {model}: {type(e).__name__}: {scrub(e, 120)}; trying next model")
    raise TransientError(f"all vision models failed (last: {scrub(last)})")


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
    client = client or openrouter_client()
    raw = store.get_bytes(key)
    ext = key.rsplit(".", 1)[-1]
    prog = Progress(store, eid)
    try:
        text, model, usage = analyse_image(client, models or vision_models(), raw, ext)
    except (TransientError, PipelineError):
        raise  # retry (transient) / fail the run (auth, billing); never a soft per-image failure
    except Exception as e:  # undecodable image etc.: soft failure, the evaluation continues
        prog.mark("img", sid, seq, False)
        return {"source_id": sid, "image_key": key, "ok": False, "error": f"{type(e).__name__}: {str(e)[:120]}"}
    stamp = (now or datetime.now(timezone.utc)).strftime("%Y-%m-%d %H:%M")
    store.put_text(analysis_key(key),
                   f"<!-- source: {os.path.basename(key)} | model: {model} | analysed: {stamp} -->\n\n{text}\n",
                   "text/markdown; charset=utf-8")
    prog.mark("img", sid, seq, True)
    return {"source_id": sid, "image_key": key, "ok": True, "model": model}
