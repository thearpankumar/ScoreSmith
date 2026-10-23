"""OpenRouter calls: Voxtral transcription and vision analysis with model fallbacks.

Plain HTTPS with `requests` to {OPENROUTER_API_BASE}/chat/completions, and to /audio/transcriptions for the
speech-to-text models that chat does not serve. The API key is not in the Lambda environment: it is read once per
container from SSM Parameter Store (SecureString named by OPENROUTER_SECRET_PARAM). OPENROUTER_API_KEY in the
environment overrides it (local runs and tests only). The step entry points (S3 in/out, progress markers) are in
media.py.
"""
from __future__ import annotations

import base64
import os
import random
import re
import time

from .errors import PipelineError, TransientError
from .images import TooLarge, analyse_with_shrink, is_size_error
from .prompts import IMAGE_MAX_TOKENS, IMAGE_PROMPT, TRANSCRIBE_PROMPT

DEFAULT_BASE_URL = "https://openrouter.ai/api/v1"
DEFAULT_REFERER = "https://scoresmith.app"
APP_TITLE = "ScoreSmith"
TRANSCRIBE_MODELS = ["mistralai/voxtral-small-24b-2507", "mistralai/voxtral-mini-3b-2507"]
VISION_MODELS = ["qwen/qwen3.7-flash", "qwen/qwen3-vl-235b-a22b-instruct"]
# Speech-to-text models are called on /audio/transcriptions (Voxtral Mini is not offered on chat). An id from the
# OPENROUTER_TRANSCRIBE_MODELS override that contains "transcribe" or "whisper" is sent there too.
STT_MODELS = frozenset({"mistralai/voxtral-mini-3b-2507"})
TIMEOUT = (10, 180)  # (connect, read) seconds per request
# Retries of one call stop after this long, so a step that also tries a fallback model stays inside the 900 s
# Lambda timeout. What is still failing then becomes a TransientError and the state machine retries the step.
RETRY_BUDGET_S = 120.0
# Wall-clock budget of one Lambda invocation (timeout 900 s) for all its OpenRouter requests, fallbacks and shrink
# rounds included; media.py starts it per step. Past it the step ends as a TransientError (retried by the state
# machine) instead of being killed by the Lambda timeout, which is not retried.
STEP_BUDGET_S = 780.0
MIN_REQUEST_S = 15.0
MAX_ATTEMPTS = 5
MAX_RETRY_AFTER_S = 30.0
_FIX = ("add OPENROUTER_API_KEY to infra/.env and re-run aws/bootstrap or aws/deploy-lambdas, "
        "or switch the AI provider to Bedrock")


class ModelError(RuntimeError):
    """This model could not answer (a 4xx other than auth or size, no usable response): try the next model."""


def _env_models(name: str, default: list[str]) -> list[str]:
    models = [m.strip() for m in os.environ.get(name, "").split(",") if m.strip()]
    return models or list(default)


def transcribe_models() -> list[str]:
    return _env_models("OPENROUTER_TRANSCRIBE_MODELS", TRANSCRIBE_MODELS)


def vision_models() -> list[str]:
    return _env_models("OPENROUTER_VISION_MODELS", VISION_MODELS)


def is_stt_model(model: str) -> bool:
    name = model.strip().lower()
    return name in STT_MODELS or "transcribe" in name or "whisper" in name


def _vision_reasoning_on() -> bool:
    return os.environ.get("OPENROUTER_VISION_REASONING", "").strip().lower() in ("on", "1", "true", "yes")


# --------------------------------------------------------------------------- secrets
_SECRET_RE = re.compile(r"(sk-or-[A-Za-z0-9_\-]+|Bearer\s+[A-Za-z0-9._\-]+)", re.I)
_KEY_CACHE: dict[str, str] = {}
_SSM_PERMANENT = {"ParameterNotFound", "ParameterVersionNotFound", "AccessDeniedException", "AccessDenied",
                  "InvalidKeyId"}


def scrub(text: object, limit: int = 300) -> str:
    """Never let the API key (or any bearer header) reach a log line or an exception message."""
    out = str(text)
    for key in (_KEY_CACHE.get("key"), os.environ.get("OPENROUTER_API_KEY", "").strip()):
        if key and len(key) >= 8:
            out = out.replace(key, "[redacted]")
    return _SECRET_RE.sub("[redacted]", out)[:limit]


def reset_key_cache() -> None:
    _KEY_CACHE.clear()


def _key_from_ssm() -> bool:
    return not os.environ.get("OPENROUTER_API_KEY", "").strip()


def _ssm_client():
    import boto3

    return boto3.client("ssm", region_name=os.environ.get("AWS_REGION", "us-east-1"))


def get_api_key() -> str:
    """OPENROUTER_API_KEY from the environment (local runs), else the SSM parameter named by OPENROUTER_SECRET_PARAM,
    read once per container. Missing, empty or denied -> PipelineError; an SSM hiccup -> TransientError."""
    key = os.environ.get("OPENROUTER_API_KEY", "").strip()
    if key:
        return key
    if "key" in _KEY_CACHE:
        return _KEY_CACHE["key"]
    param = os.environ.get("OPENROUTER_SECRET_PARAM", "").strip()
    if not param:
        raise PipelineError("internal", "OpenRouter key not configured in AWS (OPENROUTER_SECRET_PARAM is not set on "
                                        f"the Lambda function); {_FIX}")
    try:
        key = str(_ssm_client().get_parameter(Name=param, WithDecryption=True)["Parameter"]["Value"]).strip()
    except Exception as e:  # botocore ClientError / BotoCoreError
        code = ((getattr(e, "response", None) or {}).get("Error") or {}).get("Code", "")
        if code in _SSM_PERMANENT:
            raise PipelineError("internal", f"OpenRouter key not configured in AWS (SSM parameter {param}: {code}); "
                                            f"{_FIX}") from None
        raise TransientError(f"SSM unavailable reading {param}: {type(e).__name__} {code}".strip()) from None
    if not key:
        raise PipelineError("internal", f"OpenRouter key not configured in AWS (SSM parameter {param} is empty); "
                                        f"{_FIX}")
    _KEY_CACHE["key"] = key
    return key


# --------------------------------------------------------------------------- HTTP client
def _int(value: object) -> int:
    try:
        return int(value or 0)
    except (TypeError, ValueError):
        return 0


def _error_message(body: object) -> str:
    err = body.get("error") if isinstance(body, dict) else None
    if isinstance(err, str):
        return err
    if not isinstance(err, dict):
        return ""
    msg = str(err.get("message") or "")
    meta = err.get("metadata")
    raw = meta.get("raw") if isinstance(meta, dict) else None
    if raw:  # the upstream provider's own words, e.g. its size limit
        msg = f"{msg} ({str(raw)[:200]})"
    return msg


def _read(resp) -> tuple[int, object, str]:
    """(status, JSON body or None, error message). A 200 that carries an `error` (top level or on the first choice)
    takes that error's code as its status, 502 when it has none."""
    try:
        body = resp.json()
    except Exception:
        body = None
    status = int(resp.status_code)
    message = _error_message(body) or str(getattr(resp, "text", "") or f"HTTP {status}")
    if status == 200 and isinstance(body, dict):
        choices = body.get("choices")
        err = body.get("error") or (choices[0].get("error") if isinstance(choices, list) and choices
                                    and isinstance(choices[0], dict) else None)
        if err:
            code = _int(err.get("code")) if isinstance(err, dict) else 0
            status = code if 400 <= code <= 599 else 502
            message = _error_message({"error": err}) or "error in a 200 response"
    return status, body, message


def _classify(status: int, message: str, body: object) -> str:
    """-> 'ok' | 'retry' | 'auth' | 'size' | 'model'"""
    m = message.lower()
    if status == 200:
        return "ok"
    if status == 402 and "openrouter_in_flight_budget" in str(body):
        return "retry"  # too many requests in flight for the key's budget, not an empty account
    if status == 403 and ("moderation" in m or "flagged" in m):
        return "model"  # this input was flagged by one model's moderation, the key is fine
    if status in (401, 402, 403):
        return "auth"
    if status == 413 or (status == 400 and (is_size_error(Exception(message)) or ("payload" in m and "large" in m))):
        return "size"
    if status in (408, 429) or status >= 500:
        return "retry"
    return "model"  # e.g. 404 "No endpoints found that can handle the requested parameters"


def _retry_after(resp) -> float:
    headers = getattr(resp, "headers", None) or {}
    try:
        seconds = float(headers.get("Retry-After") or headers.get("retry-after") or 0)
    except (TypeError, ValueError):  # an HTTP date: use the normal backoff
        return 0.0
    return max(0.0, min(seconds, MAX_RETRY_AFTER_S))


class OpenRouterClient:
    """POSTs with bounded, jittered retries. Transient trouble (408/429/5xx, connection errors) ends as TransientError
    so the state machine retries the step; auth and billing problems are PipelineErrors; anything this model cannot
    do is a ModelError (the caller tries the next model); a body that is too big is TooLarge."""

    def __init__(self, session=None, base_url: str | None = None, sleep=time.sleep, rand=random.random,
                 clock=time.monotonic, max_attempts: int = MAX_ATTEMPTS, budget_s: float = RETRY_BUDGET_S):
        self._session = session
        self.base_url = (base_url or os.environ.get("OPENROUTER_API_BASE") or DEFAULT_BASE_URL).rstrip("/")
        self.sleep, self.rand, self.clock = sleep, rand, clock
        self.max_attempts, self.budget_s = max_attempts, budget_s
        self.deadline: float | None = None

    def start_step(self, seconds: float = STEP_BUDGET_S) -> None:
        """Every request until the next start_step must finish within `seconds` from now."""
        self.deadline = self.clock() + seconds

    def _time_left(self) -> float:
        return float("inf") if self.deadline is None else self.deadline - self.clock()

    @property
    def session(self):
        if self._session is None:
            import requests

            self._session = requests.Session()
        return self._session

    def _headers(self) -> dict:
        return {"Authorization": f"Bearer {get_api_key()}", "Content-Type": "application/json",
                "HTTP-Referer": os.environ.get("OPENROUTER_HTTP_REFERER") or DEFAULT_REFERER,
                "X-OpenRouter-Title": APP_TITLE, "X-OpenRouter-App-Visibility": "hidden"}

    def chat(self, payload: dict) -> dict:
        body = self._post("/chat/completions", payload)
        if not isinstance(body, dict) or not body.get("choices"):
            raise ModelError("response had no choices")
        return body

    def check_key(self) -> None:
        """GET /key (free): a rejected key or a used-up credit limit is a PipelineError. Network trouble or any other
        status passes, since every real call retries on its own."""
        for refresh in (True, False):
            headers = self._headers()  # PipelineError / TransientError from the key lookup propagate untouched
            try:
                resp = self.session.get(self.base_url + "/key", headers=headers, timeout=(10, 20))
            except Exception:
                return
            status, body, message = _read(resp)
            if not (status == 401 and refresh and _key_from_ssm()):
                break
            reset_key_cache()  # the key may have been rotated in SSM since this container read it
        if status in (401, 403):
            reset_key_cache()
            raise PipelineError("internal", f"OpenRouter rejected the API key (HTTP {status}: {scrub(message, 120)}); "
                                            f"{_FIX}")
        data = body.get("data") if status == 200 and isinstance(body, dict) else None
        remaining = data.get("limit_remaining") if isinstance(data, dict) else None
        if isinstance(remaining, (int, float)) and remaining <= 0:
            raise PipelineError("internal", "The OpenRouter key has no credit left (its limit is used up); raise the "
                                            "key's limit or add credits, or switch the AI provider to Bedrock")

    def transcription(self, payload: dict) -> dict:
        body = self._post("/audio/transcriptions", payload)
        if not isinstance(body, dict) or not isinstance(body.get("text"), str):
            raise ModelError("response had no text")
        return body

    def _post(self, path: str, payload: dict) -> object:
        start = self.clock()
        headers = self._headers()  # PipelineError / TransientError from the key lookup propagate untouched
        last = "no attempt"
        refreshed = False
        for attempt in range(1, self.max_attempts + 1):
            wait = 0.0
            left = self._time_left()
            if left < MIN_REQUEST_S:
                raise TransientError(f"step time budget used up before the OpenRouter call finished ({last})")
            timeout = (TIMEOUT[0], min(TIMEOUT[1], left - 5.0))
            try:
                resp = self.session.post(self.base_url + path, headers=headers, json=payload, timeout=timeout)
            except Exception as e:  # connection reset, DNS, read timeout, ...
                last = f"{type(e).__name__}: {scrub(e, 120)}"
            else:
                status, body, message = _read(resp)
                kind = _classify(status, message, body)
                last = f"HTTP {status}: {scrub(message, 200)}"
                if kind == "ok":
                    return body
                if kind == "auth":
                    if status == 401:
                        reset_key_cache()
                        if not refreshed and _key_from_ssm():  # the key may have been rotated in SSM: re-read once
                            refreshed = True
                            headers = self._headers()
                            continue
                    raise PipelineError("internal", f"OpenRouter rejected the request ({last}); check the API key "
                                                    "and the account's credits")
                if kind == "size":
                    raise TooLarge(last)
                if kind == "model":
                    raise ModelError(last)
                wait = _retry_after(resp)
            elapsed = self.clock() - start
            delay = max(wait, min(20.0, 1.5 * 2 ** (attempt - 1)) * (0.5 + self.rand()))
            if (attempt == self.max_attempts or elapsed + delay > self.budget_s
                    or self._time_left() - delay < MIN_REQUEST_S):
                break
            self.sleep(delay)
        raise TransientError(f"OpenRouter unavailable after retries ({last})")


_client: OpenRouterClient | None = None


def openrouter_client() -> OpenRouterClient:
    global _client
    if _client is None:
        _client = OpenRouterClient()
    return _client


# the interface media.py dispatches on (bedrock_ops has the same three)
default_client = openrouter_client


def _text_of(resp: dict) -> str:
    message = (resp["choices"][0] or {}).get("message") or {}
    content = message.get("content") or ""
    if isinstance(content, list):
        content = "".join(p.get("text", "") for p in content if isinstance(p, dict))
    return str(content).strip()


def _usage(resp: dict) -> dict:
    u = resp.get("usage")
    u = u if isinstance(u, dict) else {}
    return {"tokens_in": _int(u.get("prompt_tokens") or u.get("input_tokens")),
            "tokens_out": _int(u.get("completion_tokens") or u.get("output_tokens"))}


# --------------------------------------------------------------------------- transcription
def transcribe(client, models: list[str], audio: bytes, prompt: str = TRANSCRIBE_PROMPT,
               **_s3_location: object) -> tuple[str, str, dict]:
    """The S3 location keywords media.py passes are for Bedrock's Amazon Transcribe path; OpenRouter gets the bytes."""
    clip = {"data": base64.b64encode(audio).decode("ascii"), "format": "mp3"}
    last: Exception | None = None
    for model in models:
        try:
            if is_stt_model(model):
                r = client.transcription({"model": model, "input_audio": clip})
                return str(r["text"]).strip(), model, _usage(r)
            r = client.chat({
                "model": model,
                "messages": [{"role": "user", "content": [{"type": "input_audio", "input_audio": clip},
                                                          {"type": "text", "text": prompt}]}],
                "temperature": 0,
                "max_tokens": 8000,
            })
            return _text_of(r), model, _usage(r)
        except (ModelError, TooLarge, TransientError) as e:
            last = e
            print(f"    {model}: {type(e).__name__}: {scrub(e, 120)}; trying next model")
    raise TransientError(f"all transcription models failed (last: {scrub(last)})")


# --------------------------------------------------------------------------- images
def analyse(client, models: list[str], data: bytes, fmt: str, prompt: str = IMAGE_PROMPT) -> tuple[str, str, dict]:
    url = f"data:image/{fmt};base64," + base64.b64encode(data).decode("ascii")
    last: Exception | None = None
    for model in models:
        payload = {
            "model": model,
            "messages": [{"role": "user", "content": [{"type": "text", "text": prompt},
                                                      {"type": "image_url", "image_url": {"url": url}}]}],
            "temperature": 0,
            "max_tokens": IMAGE_MAX_TOKENS,
        }
        if not _vision_reasoning_on():
            # measured on qwen3.7-flash: 11.6 s and ~600 reasoning tokens with its default reasoning, 1.6 s without,
            # same OCR
            payload["reasoning"] = {"enabled": False}
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


def analyse_image(client, models: list[str], raw: bytes, ext: str,
                  prompt: str = IMAGE_PROMPT) -> tuple[str, str, dict]:
    """prepare + analyse, shrinking the image while OpenRouter says the request body is too big."""
    return analyse_with_shrink(lambda data, fmt: analyse(client, models, data, fmt, prompt), raw, ext)
