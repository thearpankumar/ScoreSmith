#!/usr/bin/env python3
"""Check what an OpenRouter account can actually use.

1. Lists every model the account can access (GET /models/user, falling back to /models).
2. Probes OpenAI GPT, DeepSeek and GLM models: plain chat + forced tool call (the app's
   structured-output path).
3. Probes audio transcription, image OCR/vision and web search.

Stdlib only. The key is read from $OPENROUTER_API_KEY or infra/.env and is never printed.

    python scripts/check_openrouter_access.py            # default probes
    python scripts/check_openrouter_access.py --all      # probe every listed gpt/deepseek/glm model
    python scripts/check_openrouter_access.py --json out.json
"""
from __future__ import annotations

import argparse
import base64
import io
import json
import math
import os
import struct
import sys
import time
import urllib.error
import urllib.request
import wave
import zlib
from pathlib import Path

BASE = os.environ.get("OPENROUTER_BASE_URL", "https://openrouter.ai/api/v1").rstrip("/")
ROOT = Path(__file__).resolve().parent.parent

# Probed by default (and always, if present in the account's list).
DEFAULT_TEXT = [
    "openai/gpt-6-luna",
    "deepseek/deepseek-v4.1-flash",
]
DEFAULT_AUDIO = ["mistralai/voxtral-small-24b-2507", "google/gemini-2.5-flash", "openai/gpt-audio-mini"]
DEFAULT_VISION = ["openai/gpt-6-luna", "deepseek/deepseek-v4.1-flash", "google/gemini-2.5-flash"]
DEFAULT_SEARCH = ["openai/gpt-6-luna", "deepseek/deepseek-v4.1-flash"]
FAMILIES = (("openai/gpt", "GPT"), ("deepseek/", "DeepSeek"), ("z-ai/glm", "GLM"))


def load_key() -> str:
    key = os.environ.get("OPENROUTER_API_KEY", "").strip()
    if key:
        return key
    env = ROOT / "infra" / ".env"
    if env.exists():
        for line in env.read_text(encoding="utf-8", errors="ignore").splitlines():
            line = line.strip()
            if line.startswith("export "):
                line = line[7:]
            if line.startswith("OPENROUTER_API_KEY="):
                return line.split("=", 1)[1].strip().strip("'\"")
    return ""


KEY = load_key()


def call(method: str, path: str, body: dict | None = None, timeout: int = 90):
    """Returns (http_status, parsed_json_or_text, seconds)."""
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(
        f"{BASE}{path}",
        data=data,
        method=method,
        headers={
            "Authorization": f"Bearer {KEY}",
            "Content-Type": "application/json",
            "HTTP-Referer": "http://localhost",
            "X-Title": "QA Tool access check",
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
            msg = str(e.get("message", ""))
            raw = (e.get("metadata") or {}).get("raw")
            return (f"{e.get('code')}: {msg}" + (f" | {str(raw)[:120]}" if raw else ""))[:220]
        if e:
            return str(e)[:220]
    return str(payload)[:220]


def chat(model: str, messages: list, **extra):
    body = {"model": model, "messages": messages, **extra}
    status, p, secs = call("POST", "/chat/completions", body)
    if status == 200 and isinstance(p, dict) and not p.get("error"):
        return True, p, secs, ""
    return False, p, secs, f"HTTP {status} {err_text(p)}"


def msg_of(p) -> dict:
    return (p.get("choices") or [{}])[0].get("message") or {}


# ---------------------------------------------------------------- probes
def probe_text(model: str):
    ok, p, secs, why = chat(model, [{"role": "user", "content": "Reply with the single word: pong"}], max_tokens=64)
    if not ok:
        return False, secs, why
    m = msg_of(p)
    return True, secs, (m.get("content") or "").strip()[:40] or "(empty content; reasoning model?)"


def probe_tool(model: str):
    tool = {"type": "function", "function": {
        "name": "record", "description": "Record a number.",
        "parameters": {"type": "object", "properties": {"n": {"type": "integer"}}, "required": ["n"]}}}
    ok, p, secs, why = chat(
        model, [{"role": "user", "content": "Call record with n=7."}], tools=[tool],
        tool_choice="required", max_tokens=300, provider={"require_parameters": True})
    if not ok:
        # retry without strict routing so we can tell "no tool support" from "unsupported combination"
        ok2, p2, _, why2 = chat(model, [{"role": "user", "content": "Call record with n=7."}], tools=[tool], tool_choice="auto", max_tokens=300)
        if ok2 and msg_of(p2).get("tool_calls"):
            return "partial", secs, f"works with tool_choice=auto only ({why[:90]})"
        return False, secs, why
    calls = msg_of(p).get("tool_calls") or []
    if not calls:
        return False, secs, "200 OK but no tool_calls returned"
    try:
        args = json.loads(calls[0]["function"]["arguments"])
    except Exception:
        return False, secs, "tool_calls arguments are not valid JSON"
    return True, secs, f"forced tool ok args={args}"


def make_wav() -> bytes:
    buf = io.BytesIO()
    with wave.open(buf, "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(16000)
        frames = b"".join(struct.pack("<h", int(8000 * math.sin(2 * math.pi * 440 * i / 16000))) for i in range(16000))
        w.writeframes(frames)
    return buf.getvalue()


def probe_audio(model: str):
    b64 = base64.b64encode(make_wav()).decode()
    ok, p, secs, why = chat(model, [{"role": "user", "content": [
        {"type": "text", "text": "Describe this audio in one short sentence (it may just be a tone)."},
        {"type": "input_audio", "input_audio": {"data": b64, "format": "wav"}}]}], max_tokens=80)
    if not ok:
        return False, secs, why
    return True, secs, ((msg_of(p).get("content") or "").strip()[:60] or "(empty)")


# 5x7 bitmap digits so we can build an OCR test image without PIL.
_FONT = {
    "0": ["01110", "10001", "10011", "10101", "11001", "10001", "01110"],
    "1": ["00100", "01100", "00100", "00100", "00100", "00100", "01110"],
    "2": ["01110", "10001", "00001", "00010", "00100", "01000", "11111"],
    "3": ["11110", "00001", "00001", "01110", "00001", "00001", "11110"],
    "4": ["00010", "00110", "01010", "10010", "11111", "00010", "00010"],
    "5": ["11111", "10000", "11110", "00001", "00001", "10001", "01110"],
    "6": ["00110", "01000", "10000", "11110", "10001", "10001", "01110"],
    "7": ["11111", "00001", "00010", "00100", "01000", "01000", "01000"],
    "8": ["01110", "10001", "10001", "01110", "10001", "10001", "01110"],
    "9": ["01110", "10001", "10001", "01111", "00001", "00010", "01100"],
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


def probe_vision(model: str):
    b64 = base64.b64encode(make_png("4721")).decode()
    ok, p, secs, why = chat(model, [{"role": "user", "content": [
        {"type": "text", "text": "Read the digits in this image. Reply with only the digits."},
        {"type": "image_url", "image_url": {"url": f"data:image/png;base64,{b64}"}}]}], max_tokens=200)
    if not ok:
        return False, secs, why
    out = (msg_of(p).get("content") or "").strip()
    return ("4721" in out.replace(" ", "")), secs, f"read: {out[:40]!r} (expected 4721)"


def probe_search(model: str):
    ok, p, secs, why = chat(model, [{"role": "user", "content": "Search the web: what is the latest stable Python 3 release? One sentence."}],
                            plugins=[{"id": "web", "max_results": 3}], max_tokens=400)
    if not ok:
        return False, secs, why
    anns = [a for a in (msg_of(p).get("annotations") or []) if a.get("type") == "url_citation"]
    if not anns:
        return "partial", secs, "200 OK but no url_citation annotations"
    return True, secs, f"{len(anns)} citations, first: {(anns[0].get('url_citation') or {}).get('url', '')[:60]}"


# ---------------------------------------------------------------- main
def fmt(r):
    return {True: "PASS", False: "FAIL", "partial": "WARN"}[r]


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--all", action="store_true", help="probe every listed GPT/DeepSeek/GLM model (costs more)")
    ap.add_argument("--json", metavar="FILE", help="also write results as JSON")
    args = ap.parse_args()

    if not KEY:
        print("No OPENROUTER_API_KEY in the environment or infra/.env")
        return 2

    results: list[dict] = []

    def record(section, model, res):
        r, secs, note = res
        results.append({"section": section, "model": model, "result": fmt(r), "seconds": round(secs, 1), "note": note})
        print(f"  [{fmt(r)}] {model:<42} {secs:5.1f}s  {note}")

    print("== Key ==")
    status, p, _ = call("GET", "/key")
    if status == 200 and isinstance(p, dict):
        d = p.get("data", {})
        print(f"  label={d.get('label', '?')} usage=${d.get('usage', '?')} limit={d.get('limit')} remaining={d.get('limit_remaining')} free_tier={d.get('is_free_tier')}")
    else:
        print(f"  /key -> HTTP {status} {err_text(p)}")
        if status == 401:
            print("  Key rejected; nothing else will work.")
            return 1

    print("\n== Models this account can access ==")
    status, p, _ = call("GET", "/models/user")
    src = "/models/user"
    if status != 200:
        status, p, _ = call("GET", "/models")
        src = "/models (public catalog; /models/user unavailable)"
    models = (p.get("data") if isinstance(p, dict) else None) or []
    ids = {m["id"] for m in models}
    print(f"  source: {src}, {len(ids)} models")
    for prefix, label in FAMILIES:
        fam = sorted(i for i in ids if i.startswith(prefix) and ":" not in i)
        print(f"  {label}: {len(fam)} -> " + (", ".join(fam[:25]) + (" ..." if len(fam) > 25 else "") if fam else "none"))
    by_id = {m["id"]: m for m in models}

    def listed(mid):  # warn when a default isn't in the account's list
        return mid in ids

    text_models = list(DEFAULT_TEXT)
    if args.all:
        text_models += sorted(i for i in ids if any(i.startswith(pf) for pf, _ in FAMILIES) and ":" not in i)
    else:  # newest GLM, since the app may fall back to it
        glm = sorted((i for i in ids if i.startswith("z-ai/glm") and ":" not in i), reverse=True)
        text_models += glm[:1]
    text_models = list(dict.fromkeys(text_models))

    print("\n== Text chat + forced tool call (GPT / DeepSeek / GLM) ==")
    for m in text_models:
        if not listed(m):
            record("text", m, (False, 0, "not in this account's model list"))
            continue
        record("chat", m, probe_text(m))
        record("tool", m, probe_tool(m))

    def section(title, key, models_, fn, needs=None):
        print(f"\n== {title} ==")
        for m in models_:
            if not listed(m):
                record(key, m, (False, 0, "not in this account's model list"))
                continue
            mods = (by_id.get(m, {}).get("architecture") or {}).get("input_modalities") or []
            if needs and mods and needs not in mods:
                record(key, m, (False, 0, f"catalog says input_modalities={mods}, no {needs}"))
                continue
            record(key, m, fn(m))

    section("Audio input (transcription)", "audio", DEFAULT_AUDIO, probe_audio, "audio")
    section("Image OCR / vision", "vision", DEFAULT_VISION, probe_vision, "image")
    section("Web search (web plugin)", "search", DEFAULT_SEARCH, probe_search)

    print("\n== Summary ==")
    for sec in ("chat", "tool", "audio", "vision", "search"):
        rows = [r for r in results if r["section"] == sec]
        if rows:
            good = [r["model"] for r in rows if r["result"] == "PASS"]
            print(f"  {sec:<7} {len(good)}/{len(rows)} pass: {', '.join(good) or '-'}")
    if args.json:
        Path(args.json).write_text(json.dumps(results, indent=2), encoding="utf-8")
        print(f"\nWrote {args.json}")
    return 0 if all(r["result"] != "FAIL" for r in results) else 1


if __name__ == "__main__":
    sys.exit(main())
