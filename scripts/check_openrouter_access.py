#!/usr/bin/env python3
"""Check that an OpenRouter key can run everything the app uses when the AI provider is OpenRouter.

Probes, with the same request shapes as the app:
  agent      openai/gpt-5-mini: plain chat and a forced named-tool call (the structured-output path), with
             reasoning effort, no temperature, provider.require_parameters and the configured sort
  vision     qwen/qwen3.7-flash (+ fallback): OCR of a generated image, with and without reasoning
  audio      mistralai/voxtral-small-24b-2507 via chat input_audio
  stt        mistralai/voxtral-mini-3b-2507 via /audio/transcriptions
  embedding  openai/text-embedding-3-small at 1024 dimensions
  search     the openrouter:web_search server tool on the agent model

Stdlib only. The key is read from $OPENROUTER_API_KEY or infra/.env and is never printed.

    python scripts/check_openrouter_access.py
    python scripts/check_openrouter_access.py --audio speech.wav   # a real speech clip for the audio probes
    python scripts/check_openrouter_access.py --json out.json
"""
from __future__ import annotations

import argparse
import base64
import io
import json
import math
import os
import re
import struct
import sys
import time
import urllib.error
import urllib.request
import wave
import zlib
from pathlib import Path

BASE = os.environ.get("OPENROUTER_API_BASE", "https://openrouter.ai/api/v1").rstrip("/")
ROOT = Path(__file__).resolve().parent.parent

AGENT_MODEL = "openai/gpt-5-mini"
VISION_MODELS = ["qwen/qwen3.7-flash", "qwen/qwen3-vl-235b-a22b-instruct"]
AUDIO_MODEL = "mistralai/voxtral-small-24b-2507"
STT_MODEL = "mistralai/voxtral-mini-3b-2507"
EMBED_MODEL = "openai/text-embedding-3-small"
SORT = os.environ.get("OPENROUTER_PROVIDER_SORT", "latency")


def load_key() -> str:
    key = os.environ.get("OPENROUTER_API_KEY", "").strip()
    if key:
        return key
    env = ROOT / "infra" / ".env"
    if env.exists():
        # Same reading as aws/bootstrap's env_value: optional `export`, spaces around `=`, the last occurrence wins
        # (like docker compose), quotes and a trailing " # comment" dropped.
        found = ""
        for line in env.read_text(encoding="utf-8", errors="ignore").splitlines():
            m = re.match(r"\s*(?:export\s+)?OPENROUTER_API_KEY\s*=\s*(.*)$", line)
            if m:
                v = m.group(1)
                q = re.match(r"""^"([^"]*)"|^'([^']*)'|^(\S*)""", v)
                found = next((g for g in q.groups() if g is not None), "") if q else ""
        return found
    return ""


KEY = load_key()


def call(method: str, path: str, body: dict | None = None, timeout: int = 120):
    """Returns (http_status, parsed_json_or_text, seconds)."""
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(
        f"{BASE}{path}",
        data=data,
        method=method,
        headers={
            "Authorization": f"Bearer {KEY}",
            "Content-Type": "application/json",
            "HTTP-Referer": "https://scoresmith.local",
            "X-OpenRouter-Title": "ScoreSmith access check",
            "X-OpenRouter-App-Visibility": "hidden",
        },
    )
    t0 = time.time()
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            raw, status = r.read().decode("utf-8", "replace"), r.status
    except urllib.error.HTTPError as e:
        raw, status = e.read().decode("utf-8", "replace"), e.code
    except Exception as e:  # network / timeout
        return 0, f"{type(e).__name__}: {e}", time.time() - t0
    try:
        return status, json.loads(raw), time.time() - t0
    except ValueError:
        return status, raw, time.time() - t0


def err_text(payload) -> str:
    if isinstance(payload, dict):
        e = payload.get("error")
        if isinstance(e, dict):
            raw = (e.get("metadata") or {}).get("raw")
            return (f"{e.get('code')}: {e.get('message', '')}" + (f" | {str(raw)[:160]}" if raw else ""))[:300]
        if e:
            return str(e)[:300]
    return str(payload)[:300]


def chat(model: str, messages: list, **extra):
    status, p, secs = call("POST", "/chat/completions", {"model": model, "messages": messages, **extra})
    if status == 200 and isinstance(p, dict) and not p.get("error"):
        return True, p, secs, ""
    return False, p, secs, f"HTTP {status} {err_text(p)}"


def first(p) -> dict:
    return (p.get("choices") or [{}])[0]


def meta(p) -> str:
    u = p.get("usage") or {}
    det = u.get("completion_tokens_details") or {}
    return (f"via={p.get('provider', '?')} finish={first(p).get('finish_reason')} "
            f"in={u.get('prompt_tokens')} out={u.get('completion_tokens')} reasoning={det.get('reasoning_tokens')} "
            f"cost=${u.get('cost')}")


AGENT_EXTRA = {"reasoning": {"effort": "low", "exclude": True}, "max_tokens": 4000,
               "provider": {"require_parameters": True, **({"sort": SORT} if SORT else {})}}


# ---------------------------------------------------------------- probes
def probe_chat():
    ok, p, secs, why = chat(AGENT_MODEL, [{"role": "system", "content": "Be terse."},
                                          {"role": "user", "content": "Reply with the single word: pong"}], **AGENT_EXTRA)
    if not ok:
        return False, secs, why
    out = (first(p).get("message") or {}).get("content") or ""
    return "pong" in out.lower(), secs, f"{out.strip()[:30]!r} {meta(p)}"


RECORD_TOOL = {"type": "function", "function": {
    "name": "record_kpi", "description": "Record one KPI.",
    "parameters": {"type": "object", "properties": {
        "name": {"type": "string"}, "weight": {"type": "number"},
        "levels": {"type": "array", "items": {"type": "string"}}}, "required": ["name", "weight", "levels"]}}}
OTHER_TOOL = {"type": "function", "function": {
    "name": "ask_clarification", "description": "Ask the user a question.",
    "parameters": {"type": "object", "properties": {"question": {"type": "string"}}, "required": ["question"]}}}


def _tool_result(p, secs, want: str):
    calls = (first(p).get("message") or {}).get("tool_calls") or []
    if not calls:
        return False, secs, f"no tool_calls ({meta(p)})"
    fn = calls[0].get("function") or {}
    try:
        args = json.loads(fn.get("arguments") or "{}")
    except ValueError:
        return False, secs, "arguments are not valid JSON"
    return fn.get("name") == want, secs, f"{fn.get('name')} args_keys={sorted(args)} {meta(p)}"


def probe_named_tool():
    msgs = [{"role": "user", "content": "Record a KPI for customer response time, weight 30, with 3 levels."}]
    ok, p, secs, why = chat(AGENT_MODEL, msgs, tools=[RECORD_TOOL],
                            tool_choice={"type": "function", "function": {"name": "record_kpi"}}, seed=7, **AGENT_EXTRA)
    if not ok:
        return False, secs, why
    return _tool_result(p, secs, "record_kpi")


def probe_required_tool():
    msgs = [{"role": "user", "content": "Record a KPI for first-contact resolution, weight 20, with 3 levels."}]
    ok, p, secs, why = chat(AGENT_MODEL, msgs, tools=[RECORD_TOOL, OTHER_TOOL], tool_choice="required", **AGENT_EXTRA)
    if not ok:
        return False, secs, why
    return _tool_result(p, secs, "record_kpi")


def probe_temperature_rejected():
    """With require_parameters, a temperature on gpt-5-mini should leave no endpoint (so the client must strip it)."""
    ok, p, secs, why = chat(AGENT_MODEL, [{"role": "user", "content": "Say hi"}], temperature=0, **AGENT_EXTRA)
    if ok:
        return "partial", secs, f"temperature accepted ({meta(p)})"
    return True, secs, f"rejected as expected: {why[:120]}"


def make_wav() -> bytes:
    buf = io.BytesIO()
    with wave.open(buf, "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(16000)
        w.writeframes(b"".join(struct.pack("<h", int(8000 * math.sin(2 * math.pi * 440 * i / 16000))) for i in range(16000)))
    return buf.getvalue()


def probe_audio(audio: bytes, fmt: str):
    b64 = base64.b64encode(audio).decode()
    ok, p, secs, why = chat(AUDIO_MODEL, [{"role": "user", "content": [
        {"type": "input_audio", "input_audio": {"data": b64, "format": fmt}},
        {"type": "text", "text": "Transcribe this audio verbatim. Output only the transcript."}]}],
        temperature=0, max_tokens=2000)
    if not ok:
        return False, secs, why
    out = ((first(p).get("message") or {}).get("content") or "").strip()
    return bool(out), secs, f"{out[:80]!r} {meta(p)}"


def probe_stt(audio: bytes, fmt: str):
    status, p, secs = call("POST", "/audio/transcriptions", {
        "model": STT_MODEL, "input_audio": {"data": base64.b64encode(audio).decode(), "format": fmt}})
    if status != 200 or not isinstance(p, dict) or p.get("error"):
        return False, secs, f"HTTP {status} {err_text(p)}"
    u = p.get("usage") or {}
    return True, secs, f"{(p.get('text') or '').strip()[:80]!r} seconds={u.get('seconds')} cost=${u.get('cost')}"


_FONT = {
    "0": ["01110", "10001", "10011", "10101", "11001", "10001", "01110"],
    "1": ["00100", "01100", "00100", "00100", "00100", "00100", "01110"],
    "2": ["01110", "10001", "00001", "00010", "00100", "01000", "11111"],
    "4": ["00010", "00110", "01010", "10010", "11111", "00010", "00010"],
    "7": ["11111", "00001", "00010", "00100", "01000", "01000", "01000"],
}


def make_png(text: str = "4721", scale: int = 12) -> bytes:
    rows = [""] * 7
    for ch in text:
        for r in range(7):
            rows[r] += _FONT[ch][r] + "0"
    w, h = len(rows[0]) * scale + 40, 7 * scale + 40
    raw = bytearray()
    for y in range(h):
        raw.append(0)
        for x in range(w):
            gx, gy = (x - 20) // scale, (y - 20) // scale
            on = 0 <= gy < 7 and 0 <= gx < len(rows[0]) and 20 <= x and 20 <= y and rows[gy][gx] == "1"
            raw.append(0 if on else 255)

    def chunk(t: bytes, d: bytes) -> bytes:
        c = struct.pack(">I", len(d)) + t + d
        return c + struct.pack(">I", zlib.crc32(t + d) & 0xFFFFFFFF)

    return (b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", struct.pack(">IIBBBBB", w, h, 8, 0, 0, 0, 0))
            + chunk(b"IDAT", zlib.compress(bytes(raw))) + chunk(b"IEND", b""))


def probe_vision(model: str, reasoning: dict | None):
    b64 = base64.b64encode(make_png("4721")).decode()
    extra = {"max_tokens": 1500}
    if reasoning is not None:
        extra["reasoning"] = reasoning
    ok, p, secs, why = chat(model, [{"role": "user", "content": [
        {"type": "text", "text": "Read the digits in this image. Reply with only the digits."},
        {"type": "image_url", "image_url": {"url": f"data:image/png;base64,{b64}"}}]}], **extra)
    if not ok:
        return False, secs, why
    out = ((first(p).get("message") or {}).get("content") or "").strip()
    return "4721" in out.replace(" ", ""), secs, f"read {out[:30]!r} {meta(p)}"


def probe_embedding():
    status, p, secs = call("POST", "/embeddings", {"model": EMBED_MODEL, "input": "customer support quality scorecard",
                                                   "dimensions": 1024, "encoding_format": "float"})
    if status != 200 or not isinstance(p, dict) or p.get("error"):
        return False, secs, f"HTTP {status} {err_text(p)}"
    vec = ((p.get("data") or [{}])[0]).get("embedding") or []
    norm = math.sqrt(sum(v * v for v in vec)) if vec else 0
    return len(vec) == 1024, secs, f"dims={len(vec)} norm={norm:.3f} cost=${(p.get('usage') or {}).get('cost')}"


def probe_search():
    ok, p, secs, why = chat(AGENT_MODEL, [{"role": "user", "content":
                                           "Search the web: what is the latest stable Python 3 release? One sentence."}],
                            tools=[{"type": "openrouter:web_search", "parameters": {"max_results": 3, "search_context_size": "low"}}],
                            reasoning={"effort": "low", "exclude": True}, max_tokens=3000)
    if not ok:
        return False, secs, why
    anns = [a for a in ((first(p).get("message") or {}).get("annotations") or []) if a.get("type") == "url_citation"]
    st = ((p.get("usage") or {}).get("server_tool_use") or {})
    if not anns:
        return "partial", secs, f"no url_citation annotations; server_tool_use={st} {meta(p)}"
    return True, secs, f"{len(anns)} citations, first {(anns[0].get('url_citation') or {}).get('url', '')[:60]} {meta(p)}"


# ---------------------------------------------------------------- main
def fmt(r):
    return {True: "PASS", False: "FAIL", "partial": "WARN"}[r]


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--audio", metavar="FILE", help="speech clip (wav or mp3) for the audio probes; default: a tone")
    ap.add_argument("--json", metavar="FILE", help="also write results as JSON")
    args = ap.parse_args()
    if not KEY:
        print("No OPENROUTER_API_KEY in the environment or infra/.env")
        return 2

    if args.audio:
        audio, audio_fmt = Path(args.audio).read_bytes(), Path(args.audio).suffix.lstrip(".").lower()
    else:
        audio, audio_fmt = make_wav(), "wav"

    results: list[dict] = []

    def record(section, label, res):
        r, secs, note = res
        results.append({"section": section, "probe": label, "result": fmt(r), "seconds": round(secs, 1), "note": note})
        print(f"  [{fmt(r)}] {label:<44} {secs:5.1f}s  {note}")

    print("== Key ==")
    status, p, _ = call("GET", "/key")
    if status != 200 or not isinstance(p, dict):
        print(f"  /key -> HTTP {status} {err_text(p)}")
        return 1
    d = p.get("data", {})
    print(f"  usage=${d.get('usage')} limit={d.get('limit')} remaining={d.get('limit_remaining')} free_tier={d.get('is_free_tier')}")

    print(f"\n== Agent model {AGENT_MODEL} (sort={SORT or 'default'}) ==")
    record("agent", "plain chat", probe_chat())
    record("agent", "forced named tool (+seed)", probe_named_tool())
    record("agent", "tool_choice=required, 2 tools", probe_required_tool())
    record("agent", "temperature + require_parameters", probe_temperature_rejected())

    print("\n== Vision ==")
    for m in VISION_MODELS:
        record("vision", f"{m} default reasoning", probe_vision(m, None))
        record("vision", f"{m} reasoning disabled", probe_vision(m, {"enabled": False}))

    print(f"\n== Audio ({audio_fmt}, {len(audio)} bytes) ==")
    record("audio", f"{AUDIO_MODEL} chat input_audio", probe_audio(audio, audio_fmt))
    record("audio", f"{STT_MODEL} /audio/transcriptions", probe_stt(audio, audio_fmt))

    print("\n== Embeddings ==")
    record("embedding", f"{EMBED_MODEL} 1024 dims", probe_embedding())

    print("\n== Web search (openrouter:web_search) ==")
    record("search", f"{AGENT_MODEL} server tool", probe_search())

    if args.json:
        Path(args.json).write_text(json.dumps(results, indent=2), encoding="utf-8")
        print(f"\nWrote {args.json}")
    return 0 if all(r["result"] != "FAIL" for r in results) else 1


if __name__ == "__main__":
    sys.exit(main())
