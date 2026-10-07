#!/usr/bin/env python3
"""Check what a Bedrock long-term API key can actually use (Bedrock twin of check_openrouter_access.py).

1. Lists foundation models + inference profiles visible to the key.
2. Invokes (Converse API) OpenAI gpt-oss, DeepSeek and GLM models: plain chat + forced tool call.
3. Probes audio input (Voxtral), image OCR/vision, and reports web search availability.

Stdlib only. The key is read from $AWS_BEARER_TOKEN_BEDROCK or a `bedrock-long-term-api-key*.csv`
in the repo root (columns: "API key name","API key") and is never printed.

    python scripts/check_bedrock_access.py [--region us-east-1] [--all] [--json out.json]
"""
from __future__ import annotations

import argparse
import base64
import csv
import json
import os
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from check_openrouter_access import make_png, make_wav  # noqa: E402  (shared test fixtures)

ROOT = Path(__file__).resolve().parent.parent

# (label, substrings that identify the family in a model id)
FAMILIES = (("OpenAI gpt-oss", ("openai.gpt",)), ("DeepSeek", ("deepseek",)), ("GLM (Z.ai)", ("zai.glm", "glm")))
AUDIO_HINTS = ("voxtral",)
VISION_HINTS = ("kimi", "qwen3-vl", "qwen.qwen3-vl", "llama4", "nova-lite", "nova-pro", "nova-2", "pixtral", "gemma-3", "mistral-large-3")


def load_key() -> str:
    k = os.environ.get("AWS_BEARER_TOKEN_BEDROCK", "").strip()
    if k:
        return k
    for f in sorted(ROOT.glob("bedrock-long-term-api-key*.csv")):
        rows = list(csv.reader(f.open(encoding="utf-8-sig")))
        if len(rows) > 1 and len(rows[1]) > 1:
            return rows[1][1].strip()
    return ""


KEY = load_key()


def call(method: str, url: str, body: dict | None = None, timeout: int = 120):
    req = urllib.request.Request(
        url, method=method, data=json.dumps(body).encode() if body is not None else None,
        headers={"Authorization": f"Bearer {KEY}", "Content-Type": "application/json", "Accept": "application/json"})
    t0 = time.time()
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            raw, status = r.read().decode("utf-8", "replace"), r.status
    except urllib.error.HTTPError as e:
        raw, status = e.read().decode("utf-8", "replace"), e.code
    except Exception as e:
        return 0, f"{type(e).__name__}: {e}", time.time() - t0
    try:
        return status, json.loads(raw), time.time() - t0
    except ValueError:
        return status, raw, time.time() - t0


def why(status, p) -> str:
    msg = p.get("message") if isinstance(p, dict) else str(p)
    return f"HTTP {status} {str(msg)[:200]}"


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--region", default=os.environ.get("AWS_REGION", "us-east-1"))
    ap.add_argument("--all", action="store_true", help="probe every listed OpenAI/DeepSeek/GLM model")
    ap.add_argument("--json", metavar="FILE")
    args = ap.parse_args()
    if not KEY:
        print("No key: set AWS_BEARER_TOKEN_BEDROCK or place bedrock-long-term-api-key*.csv in the repo root")
        return 2
    cp = f"https://bedrock.{args.region}.amazonaws.com"
    rt = f"https://bedrock-runtime.{args.region}.amazonaws.com"
    results: list[dict] = []

    def record(section, model, res):
        ok, secs, note = res
        tag = {True: "PASS", False: "FAIL", "partial": "WARN"}[ok]
        results.append({"section": section, "model": model, "result": tag, "seconds": round(secs, 1), "note": note})
        print(f"  [{tag}] {model:<52} {secs:5.1f}s  {note}")

    print(f"== Models visible to this key ({args.region}) ==")
    st, p, _ = call("GET", f"{cp}/foundation-models")
    if st != 200:
        print("  foundation-models:", why(st, p))
        if st in (401, 403):
            print("  Key rejected or lacks bedrock:ListFoundationModels (invoke may still work); continuing with a built-in list.")
    fm = {m["modelId"]: m for m in (p.get("modelSummaries", []) if st == 200 else [])}
    st2, p2, _ = call("GET", f"{cp}/inference-profiles?maxResults=1000")
    profiles = {m["inferenceProfileId"]: m for m in (p2.get("inferenceProfileSummaries", []) if st2 == 200 else [])}
    print(f"  foundation models: {len(fm)}; inference profiles: {len(profiles)}")
    allids = sorted(set(fm) | set(profiles))
    for label, subs in FAMILIES:
        fam = [i for i in allids if any(s in i for s in subs)]
        print(f"  {label}: {len(fam)} -> " + (", ".join(fam[:20]) or "none (not listed; probing built-ins anyway)"))

    def fam_ids(subs):
        return [i for i in allids if any(s in i for s in subs)]

    def converse(model, messages, tools=None, force=False, max_tokens=1500):
        body = {"messages": messages, "inferenceConfig": {"maxTokens": max_tokens, "temperature": 0}}
        if tools:
            body["toolConfig"] = {"tools": tools, **({"toolChoice": {"any": {}}} if force else {})}
        url = f"{rt}/model/{urllib.parse.quote(model, safe='')}/converse"
        st, p, secs = call("POST", url, body)
        if st == 400 and "temperature" in str(p):  # GPT-5/6 reject the temperature field
            body["inferenceConfig"].pop("temperature", None)
            st, p, secs = call("POST", url, body)
        return st, p, secs

    def text_of(p):
        return "".join(b.get("text", "") for b in p.get("output", {}).get("message", {}).get("content", []))

    TOOL = {"toolSpec": {"name": "record", "description": "Record a number.",
                         "inputSchema": {"json": {"type": "object", "properties": {"n": {"type": "integer"}}, "required": ["n"]}}}}

    def probe_chat(m):
        st, p, s = converse(m, [{"role": "user", "content": [{"text": "Reply with the single word: pong"}]}], max_tokens=1500)
        return (True, s, repr(text_of(p).strip()[:40]) or "(empty)") if st == 200 else (False, s, why(st, p))

    def probe_tool(m):
        msg = [{"role": "user", "content": [{"text": "Call the record tool with n=7."}]}]
        st, p, s = converse(m, msg, [TOOL], force=True)
        if st != 200:
            st2, p2, _ = converse(m, msg, [TOOL], force=False)
            if st2 == 200 and any("toolUse" in b for b in p2["output"]["message"]["content"]):
                return "partial", s, f"tool use works only without forced toolChoice ({why(st, p)[:80]})"
            return False, s, why(st, p)
        use = [b["toolUse"] for b in p["output"]["message"]["content"] if "toolUse" in b]
        return (True, s, f"forced tool ok input={use[0]['input']}") if use else (False, s, "200 OK but no toolUse block")

    def probe_audio(m):
        msg = [{"role": "user", "content": [{"audio": {"format": "wav", "source": {"bytes": base64.b64encode(make_wav()).decode()}}},
                                            {"text": "Describe this audio in one short sentence (it may be a plain tone)."}]}]
        st, p, s = converse(m, msg, max_tokens=120)
        return (True, s, repr(text_of(p).strip()[:60])) if st == 200 else (False, s, why(st, p))

    def probe_vision(m):
        msg = [{"role": "user", "content": [{"image": {"format": "png", "source": {"bytes": base64.b64encode(make_png('4721')).decode()}}},
                                            {"text": "Read the digits in this image. Reply with only the digits."}]}]
        st, p, s = converse(m, msg, max_tokens=300)
        if st != 200:
            return False, s, why(st, p)
        out = text_of(p).strip()
        return ("4721" in out.replace(" ", "")), s, f"read {out[:40]!r} (expected 4721)"

    # --- chat + tools
    # Newest OpenAI GPT models (not oss) via the us. profile, plus bare/global variants of the two flagships.
    gpt = [i for i in allids if i.startswith("us.openai.gpt-") and "oss" not in i]
    chat_models = gpt + ["openai.gpt-6-sol", "global.openai.gpt-6.1-sol", "global.openai.gpt-6-luna"]
    chat_models += ["deepseek.v3.2", "us.deepseek.r1-v1:0", "zai.glm-5.3", "us.zai.glm-5.3", "zai.glm-5", "zai.glm-4.7-flash"]
    chat_models += [i for i in fam_ids(("deepseek", "zai.glm", "openai.")) if args.all]
    chat_models = list(dict.fromkeys(chat_models))
    print("\n== Chat + forced tool call (gpt-oss / DeepSeek / GLM) ==")
    for m in chat_models:
        record("chat", m, probe_chat(m))
        record("tool", m, probe_tool(m))

    audio_models = list(dict.fromkeys(["mistral.voxtral-small-24b-2507", "mistral.voxtral-mini-3b-2507"] + [i for i in allids if any(h in i for h in AUDIO_HINTS)]))
    print("\n== Audio input (transcription) ==")
    for m in audio_models:
        record("audio", m, probe_audio(m))

    vision_models = ["moonshotai.kimi-k2.5", "qwen.qwen3-vl-235b-a22b", "us.meta.llama4-maverick-17b-instruct-v1:0", "us.amazon.nova-lite-v1:0"]
    print("\n== Image OCR / vision ==")
    for m in vision_models:
        record("vision", m, probe_vision(m))

    print("\n== Web search ==")
    print("  Only Amazon Nova models have built-in web search on Bedrock (system tool `nova_grounding`).")

    def probe_search(m):
        body = {"messages": [{"role": "user", "content": [{"text": "What is the latest stable Python 3 release? One sentence."}]}],
                "inferenceConfig": {"maxTokens": 400},
                "toolConfig": {"tools": [{"systemTool": {"name": "nova_grounding"}}]}}
        st, p, s = call("POST", f"{rt}/model/{urllib.parse.quote(m, safe='')}/converse", body)
        if st != 200:
            return False, s, why(st, p)
        blocks = p.get("output", {}).get("message", {}).get("content", [])
        cites = sum("citationsContent" in b for b in blocks)
        return (True if cites else "partial"), s, f"{cites} citation blocks; text={text_of(p).strip()[:50]!r}"

    for m in ("amazon.nova-2-lite-v1:0", "global.amazon.nova-2-lite-v1:0", "amazon.nova-pro-v1:0", "us.amazon.nova-premier-v1:0"):
        record("search", m, probe_search(m))

    print("\n== Summary ==")
    for sec in ("chat", "tool", "audio", "vision", "search"):
        rows = [r for r in results if r["section"] == sec]
        print(f"  {sec:<7} {sum(r['result'] == 'PASS' for r in rows)}/{len(rows)} pass: " + (", ".join(r["model"] for r in rows if r["result"] == "PASS") or "-"))
    if args.json:
        Path(args.json).write_text(json.dumps(results, indent=2), encoding="utf-8")
    return 0


if __name__ == "__main__":
    sys.exit(main())
