#!/usr/bin/env -S uv run --script
# /// script
# requires-python = ">=3.10"
# dependencies = ["boto3>=1.40", "pillow>=10"]
# ///
"""OCR + describe every extracted image of ONE student with a non-Anthropic vision model on Bedrock.

Usage (one email id per run - required):
    uv run scripts/analyze_images.py am8131
    uv run scripts/analyze_images.py am8131 --model qwen.qwen3-vl-235b-a22b
    uv run scripts/analyze_images.py am8131 --clean      # delete the analysis folders for that email

Input  : GdriveDownload/<id>/extracted_assets/<document>/NNNN_*.jpg   (made by extract_assets.py)
Output : GdriveDownload/<id>/extracted_assets/<document>/<image name>/analysis.md
         i.e. the image's file name (without .jpg) becomes a folder next to it, holding the text.
Re-runs skip images that already have an analysis.md (use --force to redo them).

Model  : default moonshotai.kimi-k2.5, then qwen.qwen3-vl-235b-a22b, then Llama 4 Maverick.
         Picked by testing 9 Bedrock vision models on a real screenshot and on a dense page
         rendered at 110 and 60 dpi: Kimi K2.5 and Qwen3-VL scored 100% word recall at both.
Auth   : AWS_BEARER_TOKEN_BEDROCK env var, else scripts/.env, else the 2-day Bedrock key in
         ~/.aws/bedrock_api_key.json, else your AWS profile/credentials. Create a 2-day key with:
         aws iam create-service-specific-credential --user-name <you> --service-name bedrock.amazonaws.com
             --credential-age-days 2 --profile arpan-aws > ~/.aws/bedrock_api_key.json
"""
from __future__ import annotations

import argparse
import io
import json
import os
import shutil
import sys
import threading
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timezone
from pathlib import Path

import boto3
from botocore.config import Config
from botocore.exceptions import BotoCoreError, ClientError
from PIL import Image

HERE = Path(__file__).resolve().parent
OUT_DIR = "extracted_assets"
KEY_FILE = Path.home() / ".aws" / "bedrock_api_key.json"
MODELS = [
    "moonshotai.kimi-k2.5",
    "qwen.qwen3-vl-235b-a22b",
    "us.meta.llama4-maverick-17b-instruct-v1:0",
]
PROMPT = """You are analysing one image taken from a hackathon solution document or slide deck.

Reply in Markdown with exactly these sections:

## Text (OCR)
Transcribe ALL text visible in the image exactly as written, in reading order. Keep table rows on one line each,
separated by " | ". If there is no text, write "(none)".

## Description
Say what kind of image this is (screenshot, architecture diagram, flow chart, chart/graph, table, photo, logo, other).
Describe the key elements, how they connect or flow, and any numbers/values shown. Be factual and concise.

## Summary
One sentence."""
STOP = threading.Event()
_lock = threading.Lock()


def log(msg: str) -> None:
    with _lock:
        print(msg, flush=True)


# --------------------------------------------------------------------------- auth
def read_dotenv(path: Path) -> dict[str, str]:
    out: dict[str, str] = {}
    try:
        for line in path.read_text("utf-8").splitlines():
            line = line.strip()
            if line and not line.startswith("#") and "=" in line:
                k, v = line.split("=", 1)
                out[k.strip()] = v.strip().strip("\"'")
    except OSError:
        pass
    return out


def setup_auth(profile: str | None) -> str:
    """Prefer a Bedrock API key (env, then key file); otherwise fall back to normal AWS credentials."""
    if os.environ.get("AWS_BEARER_TOKEN_BEDROCK"):
        return "AWS_BEARER_TOKEN_BEDROCK env var"
    env = read_dotenv(HERE / ".env")  # scripts/.env (git-ignored)
    if env.get("AWS_BEARER_TOKEN_BEDROCK"):
        try:
            exp = datetime.fromisoformat(env["AWS_BEARER_TOKEN_BEDROCK_EXPIRES"])
        except (KeyError, ValueError):
            exp = None
        if exp is None or exp > datetime.now(timezone.utc):
            os.environ["AWS_BEARER_TOKEN_BEDROCK"] = env["AWS_BEARER_TOKEN_BEDROCK"]
            return f"Bedrock API key from scripts/.env" + (f" (expires {exp:%Y-%m-%d %H:%M} UTC)" if exp else "")
        log(f"[auth] key in scripts/.env expired {exp:%Y-%m-%d %H:%M} UTC; trying other sources")
    try:
        cred = json.loads(KEY_FILE.read_text("utf-8"))["ServiceSpecificCredential"]
        expires = datetime.fromisoformat(cred["ExpirationDate"])
        if expires > datetime.now(timezone.utc):
            os.environ["AWS_BEARER_TOKEN_BEDROCK"] = cred["ServiceCredentialSecret"]
            return f"Bedrock API key from {KEY_FILE} (expires {expires:%Y-%m-%d %H:%M} UTC)"
        log(f"[auth] key in {KEY_FILE} expired {expires:%Y-%m-%d %H:%M} UTC; using AWS credentials instead")
    except (OSError, KeyError, ValueError):
        pass
    if profile:
        os.environ["AWS_PROFILE"] = profile
    return f"AWS credentials (profile {os.environ.get('AWS_PROFILE', 'default')})"


# --------------------------------------------------------------------------- image
# Converse documents 3.75 MB / 8000 px per image, but that limit is really on the request BODY, where
# the image is base64-encoded (+33%). Measured on this account: Kimi K2.5 and Qwen3-VL accept raw images
# up to ~2.61 MiB and reject from ~2.66 MiB; Llama 4 took 4.1 MiB. One budget safe for all of them:
MAX_BYTES = 2_700_000
MAX_PX = 8000


class TooLarge(Exception):
    """The service rejected the request body as too big (shrink the image and retry)."""


def is_size_error(e: Exception) -> bool:
    m = str(e).lower()
    return "length limit" in m or "too large" in m or "exceeds the maximum" in m or "request body" in m


def encode_best(img: Image.Image, budget: int) -> bytes:
    """Highest JPEG quality (bisection, 30..95) whose size fits the budget; shrink pixels only if even q=30 doesn't."""
    for _ in range(8):
        def enc(q: int) -> bytes:
            buf = io.BytesIO()
            img.save(buf, "JPEG", quality=q, optimize=True)
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


def prepare(path: Path, budget: int = MAX_BYTES) -> tuple[bytes, str]:
    """Bytes + Converse format. Original bytes are sent untouched when they fit; otherwise re-encoded at the
    highest quality that fits `budget`."""
    raw = path.read_bytes()
    fmt = {".jpg": "jpeg", ".jpeg": "jpeg", ".png": "png", ".webp": "webp", ".gif": "gif"}.get(path.suffix.lower())
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
                messages=[{"role": "user", "content": [{"image": {"format": fmt, "source": {"bytes": data}}}, {"text": PROMPT}]}],
                inferenceConfig={"maxTokens": 3000, "temperature": 0},
            )
            text = "".join(b.get("text", "") for b in r["output"]["message"]["content"]).strip()
            if not text:
                raise RuntimeError("empty response")
            return text, model, r.get("usage", {})
        except (ClientError, BotoCoreError, RuntimeError) as e:
            if is_size_error(e):
                raise TooLarge(str(e)[:120]) from e
            last = e
            log(f"    {model}: {type(e).__name__}: {str(e)[:120]}; trying next model")
    raise RuntimeError(f"all models failed (last: {last})")


def analyse_image(client, models: list[str], img: Path) -> tuple[str, str, dict]:
    """prepare + analyse, shrinking the budget by 15% and retrying if the service still says the body is too big."""
    budget = MAX_BYTES
    for attempt in range(4):
        data, fmt = prepare(img, budget)
        try:
            return analyse(client, models, data, fmt)
        except TooLarge as e:
            budget = int(min(budget, len(data)) * 0.85)
            log(f"    {img.name}: body too large ({len(data) / 1e6:.2f} MB); retrying at <= {budget / 1e6:.2f} MB")
    raise RuntimeError("image still too large after 3 shrink attempts")


# --------------------------------------------------------------------------- main
def find_images(out_root: Path) -> list[Path]:
    imgs = []
    for doc in sorted(p for p in out_root.iterdir() if p.is_dir()):
        imgs += sorted(p for p in doc.glob("*.jpg") if p.is_file())  # *_image / *_figure / *_page
        imgs += sorted(p for p in doc.glob("*_image.png") if p.is_file())
    return imgs


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("email", help="student email or id, e.g. am8131 (required, one per run)")
    ap.add_argument("--clean", action="store_true", help="delete the per-image analysis folders for this email and exit")
    ap.add_argument("--force", action="store_true", help="re-analyse images that already have an analysis.md")
    ap.add_argument("--model", help="use this Bedrock model id first (fallbacks still apply)")
    ap.add_argument("--root", type=Path, default=HERE / "GdriveDownload")
    ap.add_argument("--region", default="us-east-1")
    ap.add_argument("--profile", default="arpan-aws", help="AWS profile used only if no Bedrock API key is available")
    ap.add_argument("--workers", type=int, default=4, help="images analysed concurrently (default 4)")
    ap.add_argument("--only", nargs="+", metavar="KIND", choices=["image", "figure", "page"], help="only these image kinds")
    args = ap.parse_args()
    for s in (sys.stdout, sys.stderr):
        s.reconfigure(errors="replace")

    sid = args.email.strip().lower().split("@")[0]
    out_root = args.root / sid / OUT_DIR
    if not out_root.is_dir():
        log(f"No {OUT_DIR}/ for '{sid}' at {out_root}. Run extract_assets.py {sid} first.")
        return 2

    if args.clean:
        n = 0
        for f in out_root.glob("*/*/analysis.md"):
            shutil.rmtree(f.parent)
            n += 1
        log(f"Removed {n} analysis folder(s) under {out_root}")
        return 0

    images = find_images(out_root)
    if args.only:
        images = [p for p in images if any(p.stem.endswith("_" + k) for k in args.only)]
    if not images:
        log(f"No images under {out_root}")
        return 1

    log(f"auth: {setup_auth(args.profile)}")
    client = boto3.client(
        "bedrock-runtime", region_name=args.region,
        config=Config(read_timeout=180, retries={"max_attempts": 6, "mode": "adaptive"}),
    )
    models = ([args.model] if args.model else []) + [m for m in MODELS if m != args.model]
    log(f"{len(images)} image(s) for {sid}; model order: {', '.join(models)}")

    stats = dict(done=0, skipped=0, failed=0, tin=0, tout=0)

    def work(img: Path) -> None:
        dest = img.parent / img.stem
        label = f"{img.parent.name}/{img.name}"
        if STOP.is_set():
            return
        if (dest / "analysis.md").exists() and not args.force:
            stats["skipped"] += 1
            return
        t0 = time.time()
        try:
            text, model, usage = analyse_image(client, models, img)
            dest.mkdir(exist_ok=True)
            (dest / "analysis.md").write_text(
                f"<!-- source: {img.name} | model: {model} | analysed: {datetime.now():%Y-%m-%d %H:%M} -->\n\n{text}\n", "utf-8")
            with _lock:
                stats["done"] += 1
                stats["tin"] += usage.get("inputTokens", 0)
                stats["tout"] += usage.get("outputTokens", 0)
            log(f"  + {label}  ({model},{time.time() - t0:.1f}s)")
        except Exception as e:
            with _lock:
                stats["failed"] += 1
            log(f"  ! {label}: {type(e).__name__}: {str(e)[:160]}")

    ex = ThreadPoolExecutor(max_workers=max(1, args.workers))
    futs = [ex.submit(work, p) for p in images]
    try:
        for f in as_completed(futs):
            f.result()
    except KeyboardInterrupt:
        STOP.set()
        log("\nCtrl+C: finishing requests in flight, starting no new ones... (Ctrl+C again to quit now)")
        ex.shutdown(wait=True, cancel_futures=True)
    else:
        ex.shutdown()
    log(f"\nDone: {stats['done']} analysed, {stats['skipped']} skipped (already done), {stats['failed']} failed; "
        f"tokens in/out {stats['tin']}/{stats['tout']}")
    if STOP.is_set():
        log("Interrupted - re-run to continue; finished images are skipped.")
        return 130
    return 1 if stats["failed"] else 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except KeyboardInterrupt:
        print("\nAborted.")
        sys.exit(130)
