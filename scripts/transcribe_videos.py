#!/usr/bin/env -S uv run --script
# /// script
# requires-python = ">=3.10"
# dependencies = ["boto3>=1.40", "imageio-ffmpeg>=0.5"]
# ///
"""Turn ONE student's demo videos into audio + transcript with a Bedrock speech model.

Usage (one email id per run - required):
    uv run scripts/transcribe_videos.py am8131
    uv run scripts/transcribe_videos.py am8131 --model mistral.voxtral-mini-3b-2507
    uv run scripts/transcribe_videos.py am8131 --clean      # delete the video folders for that email

Input  : GdriveDownload/<id>/*.mp4   (also .mov .mkv .webm .m4v, top level only)
Output : GdriveDownload/<id>/extracted_assets/<video name>/
             <video name>.mp3      audio track (mono, 16 kHz)
             transcript.txt        full transcript
             transcript.json       model, chunk timings, usage
         If a document has the same name, the folder becomes "<video name>-video".
Re-runs skip videos that already have a transcript.txt (use --force to redo).

How
  * ffmpeg comes from the pip package imageio-ffmpeg (nothing to install).
  * Audio is cut into ~5 minute chunks, each cut placed in a pause (silence threshold adapts to the
    recording's noise level) so words are not split; every chunk is sent to the model through the Bedrock Converse API (audio block).
  * Model: mistral.voxtral-small-24b-2507 (accurate), falling back to voxtral-mini-3b-2507.
    Chosen after testing on a real student demo: no S3 bucket or Amazon Transcribe job needed.
Auth   : same as analyze_images.py - AWS_BEARER_TOKEN_BEDROCK, else scripts/.env, else
         ~/.aws/bedrock_api_key.json (2-day Bedrock API key), else the AWS profile.
"""
from __future__ import annotations

import argparse
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import time
from datetime import datetime, timezone
from pathlib import Path

import boto3
import imageio_ffmpeg
from botocore.config import Config
from botocore.exceptions import BotoCoreError, ClientError

HERE = Path(__file__).resolve().parent
OUT_DIR = "extracted_assets"
KEY_FILE = Path.home() / ".aws" / "bedrock_api_key.json"
VIDEO_EXT = {".mp4", ".mov", ".mkv", ".webm", ".m4v"}
MODELS = ["mistral.voxtral-small-24b-2507", "mistral.voxtral-mini-3b-2507"]
FFMPEG = imageio_ffmpeg.get_ffmpeg_exe()
PROMPT = "Transcribe this audio verbatim. Output only the transcript."  # tested wording; longer prompts made the model translate


def log(msg: str) -> None:
    print(msg, flush=True)


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


# --------------------------------------------------------------------------- audio
def ffmpeg(*args: str) -> subprocess.CompletedProcess:
    return subprocess.run([FFMPEG, "-hide_banner", "-y", *args], capture_output=True, text=True, errors="replace")


def probe(video: Path) -> tuple[float, bool]:
    """(duration seconds, has audio stream)."""
    err = ffmpeg("-i", str(video)).stderr
    m = re.search(r"Duration: (\d+):(\d+):([\d.]+)", err)
    dur = int(m.group(1)) * 3600 + int(m.group(2)) * 60 + float(m.group(3)) if m else 0.0
    return dur, bool(re.search(r"Stream #.*Audio:", err))


def extract_audio(video: Path, mp3: Path) -> None:
    r = ffmpeg("-i", str(video), "-vn", "-ac", "1", "-ar", "16000", "-b:a", "48k", str(mp3))
    if r.returncode != 0 or not mp3.exists() or mp3.stat().st_size == 0:
        raise RuntimeError("ffmpeg could not extract audio: " + r.stderr.strip().splitlines()[-1][:200])


# strictest first: clean studio audio has deep silences, a noisy recording only has shallow pauses
SILENCE_LEVELS_DB = (-40, -35, -30, -25, -20)


def silence_gaps(mp3: Path, level_db: int) -> list[tuple[float, float]]:
    """(start, end) of every pause of >= 0.3 s quieter than level_db."""
    err = ffmpeg("-i", str(mp3), "-af", f"silencedetect=noise={level_db}dB:d=0.3", "-f", "null", "-").stderr
    starts = [float(x) for x in re.findall(r"silence_start: ([\d.]+)", err)]
    ends = [float(x) for x in re.findall(r"silence_end: ([\d.]+)", err)]
    return list(zip(starts, ends))


def best_cut(mp3: Path, goal: float, floor: float, cache: dict, window: float = 30.0) -> float | None:
    """Cut point within +-window s of `goal`: loosen the silence threshold until a pause exists there,
    then take the longest pause (most likely a sentence boundary). None if the audio has no pauses at all."""
    for level in SILENCE_LEVELS_DB:
        if level not in cache:
            cache[level] = silence_gaps(mp3, level)
        near = [(s, e) for s, e in cache[level] if abs((s + e) / 2 - goal) <= window and (s + e) / 2 > floor + 30]
        if near:
            s, e = max(near, key=lambda g: (g[1] - g[0], -abs((g[0] + g[1]) / 2 - goal)))
            return (s + e) / 2
    return None


def plan_chunks(duration: float, mp3: Path, target: float) -> list[tuple[float, float]]:
    if duration <= target * 1.3:
        return [(0.0, duration)]
    cache: dict = {}
    cuts, pos = [], 0.0
    while duration - pos > target * 1.3:
        cut = best_cut(mp3, pos + target, pos, cache)
        pos = cut if cut is not None else pos + target  # hard cut only if no pause anywhere near
        cuts.append(pos)
    bounds = [0.0, *cuts, duration]
    return list(zip(bounds[:-1], bounds[1:]))


def transcribe(client, models: list[str], audio: bytes, prompt: str = PROMPT) -> tuple[str, str, dict]:
    last: Exception | None = None
    for model in models:
        try:
            r = client.converse(
                modelId=model,
                messages=[{"role": "user", "content": [{"audio": {"format": "mp3", "source": {"bytes": audio}}}, {"text": prompt}]}],
                inferenceConfig={"maxTokens": 8000, "temperature": 0},
            )
            text = "".join(b.get("text", "") for b in r["output"]["message"]["content"]).strip()
            return text, model, r.get("usage", {})
        except (ClientError, BotoCoreError) as e:
            last = e
            log(f"    {model}: {type(e).__name__}: {str(e)[:120]}; trying next model")
    raise RuntimeError(f"all models failed (last: {last})")


# --------------------------------------------------------------------------- main
def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("email", help="student email or id, e.g. am8131 (required, one per run)")
    ap.add_argument("--clean", action="store_true", help="delete this email's video folders (those with a transcript.txt) and exit")
    ap.add_argument("--force", action="store_true", help="redo videos that already have a transcript.txt")
    ap.add_argument("--model", help="use this Bedrock model id first (fallback still applies)")
    ap.add_argument("--root", type=Path, default=HERE / "GdriveDownload")
    ap.add_argument("--region", default="us-east-1")
    ap.add_argument("--profile", default="arpan-aws", help="AWS profile used only if no Bedrock API key is available")
    ap.add_argument("--language", help="spoken language, e.g. English or Hindi (default: let the model detect)")
    ap.add_argument("--chunk-minutes", type=float, default=5.0, help="target chunk length (default 5)")
    args = ap.parse_args()
    for s in (sys.stdout, sys.stderr):
        s.reconfigure(errors="replace")

    sid = args.email.strip().lower().split("@")[0]
    sdir = args.root / sid
    out_root = sdir / OUT_DIR
    if not sdir.is_dir():
        log(f"No folder for '{sid}' at {sdir}.")
        return 2

    if args.clean:
        n = 0
        if out_root.is_dir():
            for f in out_root.glob("*/transcript.txt"):
                shutil.rmtree(f.parent)
                n += 1
        log(f"Removed {n} video folder(s) under {out_root}")
        return 0

    videos = sorted(p for p in sdir.iterdir() if p.is_file() and p.suffix.lower() in VIDEO_EXT and not p.name.startswith("."))
    if not videos:
        log(f"No video files in {sdir}")
        return 1
    doc_stems = {p.stem.lower() for p in sdir.iterdir() if p.is_file() and p.suffix.lower() in (".pdf", ".docx")}

    log(f"auth: {setup_auth(args.profile)}")
    client = boto3.client("bedrock-runtime", region_name=args.region,
                          config=Config(read_timeout=300, retries={"max_attempts": 6, "mode": "adaptive"}))
    models = ([args.model] if args.model else []) + [m for m in MODELS if m != args.model]

    prompt = PROMPT + (f" The audio is in {args.language}." if args.language else "")
    failed = 0
    for video in videos:
        folder = video.stem if video.stem.lower() not in doc_stems else video.stem + "-video"
        dest = out_root / re.sub(r'[<>:"/\\|?*\x00-\x1f]', "_", folder).strip(" .")
        log(f"{video.name} -> {OUT_DIR}/{dest.name}/")
        if (dest / "transcript.txt").exists() and not args.force:
            log("    already transcribed; skipped (use --force to redo)")
            continue
        try:
            duration, has_audio = probe(video)
            if not has_audio:
                log("    no audio track in this video; nothing to transcribe")
                continue
            dest.mkdir(parents=True, exist_ok=True)
            mp3 = dest / (dest.name + ".mp3")
            extract_audio(video, mp3)
            chunks = plan_chunks(duration, mp3, args.chunk_minutes * 60)
            log(f"    audio {mp3.stat().st_size / 1e6:.1f} MB, {duration / 60:.1f} min, {len(chunks)} chunk(s)")

            parts, meta, tin, tout = [], [], 0, 0
            with tempfile.TemporaryDirectory() as tmp:
                for i, (a, b) in enumerate(chunks, 1):
                    piece = mp3
                    if len(chunks) > 1:
                        piece = Path(tmp) / f"c{i}.mp3"
                        r = ffmpeg("-i", str(mp3), "-ss", f"{a:.2f}", "-to", f"{b:.2f}", "-ac", "1", "-ar", "16000", "-b:a", "48k", str(piece))
                        if r.returncode != 0:
                            raise RuntimeError("ffmpeg chunking failed")
                    t0 = time.time()
                    text, model, usage = transcribe(client, models, piece.read_bytes(), prompt)
                    parts.append(text)
                    tin, tout = tin + usage.get("inputTokens", 0), tout + usage.get("outputTokens", 0)
                    meta.append(dict(chunk=i, start=round(a, 1), end=round(b, 1), model=model, chars=len(text)))
                    log(f"    chunk {i}/{len(chunks)} [{a / 60:.1f}-{b / 60:.1f} min] {len(text)} chars, {model}, {time.time() - t0:.1f}s")
            transcript = "\n\n".join(p for p in parts if p).strip()
            (dest / "transcript.txt").write_text(transcript + "\n", "utf-8")
            (dest / "transcript.json").write_text(json.dumps(
                dict(source=video.name, duration_sec=round(duration, 1), chunks=meta, tokens_in=tin, tokens_out=tout,
                     created=datetime.now().isoformat(timespec="seconds")), indent=2), "utf-8")
            log(f"    transcript: {len(transcript.split())} words")
        except KeyboardInterrupt:
            log("\nAborted.")
            return 130
        except Exception as e:
            failed += 1
            log(f"    FAILED: {type(e).__name__}: {e}")
    return 1 if failed else 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except KeyboardInterrupt:
        print("\nAborted.")
        sys.exit(130)
