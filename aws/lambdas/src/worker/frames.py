"""PlanFrames: sample a video's frames, drop near-duplicates, tile them into timestamped contact sheets, and measure
picture quality from the samples. Runs on the plan-audio function (10 GB /tmp), in parallel with PlanAudio.

1. ffmpeg samples a frame every FRAME_MIN_GAP_S seconds (2 s), stretched for long videos so about TARGET_SAMPLES
   frames are read but never sparser than FRAME_MAX_GAP_S; showinfo prints each sampled frame's time. It decodes
   keyframes only, which keeps an hour-long video well inside the Lambda timeout; a video up to FULL_DECODE_MAX_S
   whose keyframes are too sparse for the interval is decoded in full instead. (Sampling on ffmpeg's scene score
   missed screen recordings: when only text changes on a fixed background the score barely moves.)
2. A sample is a new picture when more than FRAME_CHANGE_THRESHOLD of its grayscale pixels (at the sampled size,
   448 px wide for a 3x3 grid, so small text still shows) differ from the last kept one: a changed word or line of
   text counts, a moved mouse pointer or compression noise does not. Repeats are dropped and the kept frame records how long the picture stayed. Above MAX_FRAMES_PER_VIDEO, half
   the slots go to the strongest changes and the rest are spread evenly over time.
3. Frames are tiled COLSxROWS into JPEG sheets, each cell with its [mm:ss] printed by Pillow (no ffmpeg drawtext).

Outputs under derived/{eid}/video/{sid}/frames/: sheet_NN.jpg, sheets.json (cell -> time map and the request batches
the AnalyzeSheets Map runs over) and quality.json (container facts from `ffmpeg -i`, brightness / detail / black-frame
/ static-picture stats from the sampled frames).
"""
from __future__ import annotations

import glob
import io
import math
import os
import re
import shutil
import statistics
import subprocess
import tempfile
import uuid
from dataclasses import dataclass
from typing import Any

from . import video_settings
from .audio import MAX_DURATION_S, ffmpeg_exe
from .errors import PipelineError
from .progress import Progress
from .store import derived
from .timeline import fmt_ts
from .video_settings import VideoSettings

SHEET_WIDTH = 1344        # px; a cell's box is SHEET_WIDTH // cols square (448 for 3x3, 672 for 2x2)
SHEET_QUALITY = 80
MAX_SHEET_BYTES = 900_000
CELL_GAP = 4
FULL_DECODE_MAX_S = 1200.0  # sparse keyframes: decode every frame, but only for videos up to this long
TARGET_SAMPLES = 900       # the sampling interval grows for long videos so about this many frames are read
MAX_SAMPLED = 2000         # hard cap on frames ffmpeg writes
PIXEL_CHANGE = 20          # a pixel changed when its brightness moved by more than this (0-255)
FFMPEG_TIMEOUT_S = 600
BLACK_LUMA, DARK_LUMA, BRIGHT_LUMA = 16.0, 50.0, 225.0
FLAT_STDDEV = 10.0         # a frame this uniform (and dark) is a black frame, not a dark scene
BLUR_MIN_STDDEV = 12.0     # plain frames (one colour) are never called blurred
BLUR_EDGE_VAR = 40.0       # edge variance of a 448 px frame: sharp ~150-6000, Gaussian blur r=2 ~15-20
STATIC_MIN_S = 30.0
MAX_LISTED = 10            # timestamps listed per quality fact

_SHOWINFO_RE = re.compile(r"\bn:\s*\d+\s+pts:\s*-?\d+\s+pts_time:\s*(-?[\d.]+)")


@dataclass
class Frame:
    path: str
    t: float
    width: int = 0
    height: int = 0
    thumb: Any = None       # the grayscale sample (PIL), what samples are compared on
    luma: float = 0.0       # mean brightness 0-255
    contrast: float = 0.0   # brightness standard deviation
    edges: float = 0.0      # variance of the edge image (low = little detail / blurred)
    until: float = 0.0      # the picture stayed (near-)identical until this time
    change: float = 1.0     # share of pixels changed since the previous kept frame (1.0 for the first)


# --------------------------------------------------------------------------- ffmpeg
def _run(args: list[str], timeout: float = FFMPEG_TIMEOUT_S) -> subprocess.CompletedProcess:
    try:
        return subprocess.run([ffmpeg_exe(), "-hide_banner", "-y", *args], capture_output=True, text=True,
                              errors="replace", timeout=timeout)
    except subprocess.TimeoutExpired:
        raise PipelineError("extract_failed", f"frame extraction took longer than {timeout:.0f}s") from None


def parse_info(stderr: str) -> dict:
    """Container facts from `ffmpeg -i` stderr (None where ffmpeg did not say)."""
    def num(rx: str, cast=float):
        m = re.search(rx, stderr)
        return cast(m.group(1)) if m else None

    m = re.search(r"Duration: (\d+):(\d+):([\d.]+)", stderr)
    duration = int(m.group(1)) * 3600 + int(m.group(2)) * 60 + float(m.group(3)) if m else 0.0
    video = re.search(r"Stream #[^\n]*?Video: ([^\n]*)", stderr)
    width = height = fps = codec = None
    if video:
        line = video.group(1)
        codec = line.split(",")[0].split()[0] if line.strip() else None
        size = re.search(r"\b(\d{2,5})x(\d{2,5})\b", line)
        if size:
            width, height = int(size.group(1)), int(size.group(2))
        rate = re.search(r"([\d.]+) fps", line) or re.search(r"([\d.]+) tbr", line)
        fps = float(rate.group(1)) if rate else None
    if width and height and re.search(r"rotation of -?(90|270)\.", stderr):
        width, height = height, width  # phone video stored sideways; ffmpeg rotates the decoded frames
    return {
        "duration_sec": round(duration, 1), "start_sec": num(r"Duration: [^\n]*?start: (-?[\d.]+)") or 0.0,
        "has_video": video is not None, "has_audio": bool(re.search(r"Stream #[^\n]*Audio:", stderr)),
        "width": width, "height": height, "fps": round(fps, 2) if fps else None,
        "bitrate_kbps": num(r"Duration: [^\n]*?bitrate: (\d+) kb/s", int), "video_codec": codec,
    }


def probe_info(path: str) -> dict:
    return parse_info(_run(["-i", path], timeout=120).stderr)


def parse_showinfo(stderr: str) -> list[float]:
    return [float(x) for x in _SHOWINFO_RE.findall(stderr)]


def sample_interval(s: VideoSettings, duration: float) -> float:
    """Seconds between samples: FRAME_MIN_GAP_S, stretched for a long video to about TARGET_SAMPLES samples, but never
    above FRAME_MAX_GAP_S."""
    return min(s.max_gap_s, max(s.min_gap_s, (duration or 0.0) / TARGET_SAMPLES))


def select_filter(interval: float, box: int) -> str:
    """The frame selector: the first frame, then one every `interval` seconds; scaled into a box x box square and
    timestamped by showinfo."""
    sel = f"select='isnan(prev_selected_t)+gte(t-prev_selected_t,{interval:g})'"
    return f"{sel},scale={box}:{box}:force_original_aspect_ratio=decrease:force_divisible_by=2,showinfo"


def extract_frames(video: str, outdir: str, s: VideoSettings, start: float = 0.0, keyframes_only: bool = True,
                   runner=_run, interval: float | None = None) -> list[Frame]:
    """Run the selector and pair the written JPEGs with their times (seconds from the start of the video)."""
    shutil.rmtree(outdir, ignore_errors=True)
    os.makedirs(outdir)
    box = SHEET_WIDTH // s.cols
    vf = select_filter(interval or s.min_gap_s, box)
    args = (["-skip_frame", "nokey"] if keyframes_only else []) + [
        "-i", video, "-an", "-sn", "-dn", "-vf", vf, "-fps_mode", "vfr",
        "-frames:v", str(MAX_SAMPLED), "-q:v", "3", os.path.join(outdir, "f_%05d.jpg")]
    r = runner(args)
    files = sorted(glob.glob(os.path.join(outdir, "f_*.jpg")))
    times = parse_showinfo(r.stderr)
    if r.returncode != 0 and not files:
        tail = (r.stderr.strip().splitlines() or ["unknown error"])[-1][:200]
        raise PipelineError("extract_failed", "ffmpeg could not read frames: " + tail)
    if len(times) != len(files):
        print(f"    {len(files)} frame files but {len(times)} showinfo times; pairing the first ones")
    return [Frame(path=p, t=max(0.0, t - start)) for p, t in zip(files, times)]


# --------------------------------------------------------------------------- measuring and choosing frames
def _gray(path: str):
    from PIL import Image

    with Image.open(path) as im:
        return im.convert("L"), im.size


def changed_share(a, b) -> float:
    """Share of pixels whose brightness differs by more than PIXEL_CHANGE (1.0 when one is missing or the sizes
    differ)."""
    from PIL import ImageChops

    if a is None or b is None or a.size != b.size:
        return 1.0
    diff = ImageChops.difference(a, b).point(lambda v: 255 if v > PIXEL_CHANGE else 0)
    return diff.histogram()[255] / (diff.width * diff.height)


def measure(frame: Frame) -> Frame:
    from PIL import ImageFilter, ImageStat

    gray, (frame.width, frame.height) = _gray(frame.path)
    frame.thumb = gray
    stat = ImageStat.Stat(gray)
    frame.luma, frame.contrast = stat.mean[0], stat.stddev[0]
    edges = gray.filter(ImageFilter.FIND_EDGES)
    w, h = edges.size
    if w > 4 and h > 4:
        edges = edges.crop((2, 2, w - 2, h - 2))  # FIND_EDGES marks the image border itself
    frame.edges = ImageStat.Stat(edges).var[0]
    return frame


def dedup(frames: list[Frame], threshold: float) -> list[Frame]:
    """Frames that differ from the last kept one in more than `threshold` of their pixels; `until` and `change` are
    set."""
    kept: list[Frame] = []
    for f in frames:
        if kept:
            d = changed_share(f.thumb, kept[-1].thumb)
            if d <= threshold:
                kept[-1].until = f.t
                continue
            f.change = d
        f.until = f.t
        kept.append(f)
    return kept


def cap(frames: list[Frame], limit: int, duration: float) -> list[Frame]:
    """At most `limit` frames: the first, the strongest changes (half the slots), then the frames nearest to evenly
    spaced times over the rest of the video."""
    if len(frames) <= limit:
        return list(frames)
    first, rest = frames[0], frames[1:]
    chosen = {id(first)}
    for f in sorted(rest, key=lambda f: (-f.change, f.t))[:(limit - 1) // 2]:
        chosen.add(id(f))
    left = [f for f in rest if id(f) not in chosen]
    slots = limit - len(chosen)
    end = max(duration, frames[-1].t)
    for i in range(slots):
        if not left:
            break
        target = first.t + (end - first.t) * (i + 0.5) / slots
        best = min(left, key=lambda f: abs(f.t - target))
        chosen.add(id(best))
        left.remove(best)
    return [f for f in frames if id(f) in chosen]


# --------------------------------------------------------------------------- contact sheets
def _font(size: int):
    from PIL import ImageFont

    try:
        return ImageFont.load_default(size=size)  # Pillow >= 10.1 with FreeType (the wheels bundle it)
    except (TypeError, OSError, ImportError):
        return ImageFont.load_default()


def tile(frames: list[Frame], cols: int, rows: int) -> bytes:
    """One contact sheet: frames left to right, top to bottom, [mm:ss] in each cell's top-left corner. A last, partly
    filled sheet only gets the rows (and, for a single row, the columns) it needs."""
    from PIL import Image, ImageDraw

    n = len(frames)
    cols_used = cols if n >= cols else n
    rows_used = min(rows, math.ceil(n / cols_used))
    cw, ch = max(f.width for f in frames), max(f.height for f in frames)
    sheet = Image.new("RGB", (cols_used * cw + (cols_used + 1) * CELL_GAP, rows_used * ch + (rows_used + 1) * CELL_GAP),
                      (24, 24, 24))
    draw = ImageDraw.Draw(sheet)
    font = _font(max(14, min(28, min(cw, ch) // 14)))
    for i, f in enumerate(frames):
        r, c = divmod(i, cols_used)
        x, y = CELL_GAP + c * (cw + CELL_GAP), CELL_GAP + r * (ch + CELL_GAP)
        with Image.open(f.path) as im:
            sheet.paste(im.convert("RGB"), (x + (cw - im.width) // 2, y + (ch - im.height) // 2))
        label = f"[{fmt_ts(f.t)}]"
        left, top, right, bottom = draw.textbbox((0, 0), label, font=font)
        draw.rectangle((x, y, x + right - left + 8, y + bottom - top + 8), fill=(0, 0, 0))
        draw.text((x + 4 - left, y + 4 - top), label, fill=(255, 255, 255), font=font)
    for quality in (SHEET_QUALITY, 70, 60, 50):
        buf = io.BytesIO()
        sheet.save(buf, "JPEG", quality=quality, optimize=True)
        if buf.tell() <= MAX_SHEET_BYTES:
            break
    return buf.getvalue()


# --------------------------------------------------------------------------- quality facts
def _listed(times: list[float]) -> dict:
    return {"count": len(times), "at": [fmt_ts(t) for t in times[:MAX_LISTED]]}


def quality(info: dict, sampled: list[Frame], kept: list[Frame], decode: str) -> dict:
    """quality.json: container facts plus stats over the sampled frames (keyframes / scene changes, so they describe
    the samples, not every frame of the video)."""
    black = [f.t for f in sampled if f.luma < BLACK_LUMA and f.contrast < FLAT_STDDEV]
    dark = [f.t for f in sampled if f.luma < DARK_LUMA and not (f.luma < BLACK_LUMA and f.contrast < FLAT_STDDEV)]
    bright = [f.t for f in sampled if f.luma > BRIGHT_LUMA]
    judged = [f for f in sampled if f.contrast >= BLUR_MIN_STDDEV]
    static = [(f.t, f.until) for f in kept if f.until - f.t >= STATIC_MIN_S]
    lumas = [f.luma for f in sampled]
    return {
        **{k: info.get(k) for k in ("duration_sec", "width", "height", "fps", "bitrate_kbps", "video_codec",
                                     "has_audio")},
        "decode": decode,
        "sampled_frames": len(sampled),
        "kept_frames": len(kept),
        "brightness": ({"mean": round(statistics.fmean(lumas), 1), "min": round(min(lumas), 1),
                        "max": round(max(lumas), 1)} if lumas else None),
        "black_frames": _listed(black),
        "dark_frames": _listed(dark),
        "bright_frames": _listed(bright),
        "low_detail_frames": {**_listed([f.t for f in judged if f.edges < BLUR_EDGE_VAR]), "judged": len(judged)},
        "edge_variance_median": round(statistics.median([f.edges for f in judged]), 1) if judged else None,
        "static_segments": [{"start": fmt_ts(a), "end": fmt_ts(b), "seconds": round(b - a, 1)}
                            for a, b in static[:MAX_LISTED]],
        "static_seconds": round(sum(b - a for a, b in static), 1),
    }


# --------------------------------------------------------------------------- the PlanFrames step
def frames_prefix(eid: str, sid: str) -> str:
    return derived(eid, "video", sid, "frames")


def run_plan_frames(event: dict, store, tmp_root: str | None = None, settings: VideoSettings | None = None) -> dict:
    """PlanFrames. Returns {"source_id", "batches": [{"index", "sheets": [keys]}]} (the AnalyzeSheets Map items).
    A video that cannot be sampled is a soft failure (sheets.json carries the error, no batches); anything
    unexpected raises and the state machine's Catch skips the visual branch."""
    eid, f = event["evaluation_id"], event["file"]
    sid, name = f["source_id"], f.get("original_name", f["source_id"])
    prefix = frames_prefix(eid, sid)
    prog = Progress(store, eid)
    s = settings or video_settings.load(store, eid)
    run = uuid.uuid4().hex[:12]  # AnalyzeSheets tags its notes with it, so a re-run never mixes in stale notes
    out = {"source_id": sid, "batches": []}
    base = {"source": name, "run": run, "mode": s.mode, "settings": s.summary()}
    if s.mode == "off":
        store.put_json(f"{prefix}/sheets.json", {**base, "sheets": [], "batches": []})
        return out
    tmp = tempfile.mkdtemp(prefix="frames_", dir=tmp_root)
    try:
        video = os.path.join(tmp, "video." + (f["raw_key"].rsplit(".", 1)[-1] if "." in f["raw_key"] else "bin"))
        store.download(f["raw_key"], video)
        info = probe_info(video)
        if not info["has_video"]:
            raise PipelineError("extract_failed", "no video stream in this file")
        duration = float(info["duration_sec"] or 0.0)
        if duration > MAX_DURATION_S:
            raise PipelineError("file_too_large",
                                f"Video is {duration / 3600:.1f} h long (limit {MAX_DURATION_S // 3600} h)")
        raw_dir = os.path.join(tmp, "raw")
        interval = sample_interval(s, duration)
        try:
            sampled = extract_frames(video, raw_dir, s, info["start_sec"], interval=interval)
        except PipelineError:
            if duration > FULL_DECODE_MAX_S:
                raise
            sampled = []  # a short video: try decoding every frame below
        decode = "keyframes"
        # Keyframes much sparser than the interval would skip what happens between them: decode every frame instead.
        if len(sampled) < max(2.0, duration / interval / 3) and duration <= FULL_DECODE_MAX_S:
            sampled = extract_frames(video, raw_dir, s, info["start_sec"], keyframes_only=False, interval=interval)
            decode = "full"
        os.remove(video)
        if not sampled:
            raise PipelineError("extract_failed", "no frame could be decoded from this video")
        for fr in sampled:
            measure(fr)
        kept = cap(dedup(list(sampled), s.change_threshold), s.max_frames, duration)
        sheets, keys = [], []
        for i in range(0, len(kept), s.per_sheet):
            group = kept[i:i + s.per_sheet]
            n = len(sheets) + 1
            key = f"{prefix}/sheet_{n:02d}.jpg"
            store.put_bytes(key, tile(group, s.cols, s.rows), "image/jpeg")
            keys.append(key)
            sheets.append({"index": n, "key": key, "cells": [
                {"cell": j + 1, "t": round(fr.t, 2), "until": round(fr.until, 2)} for j, fr in enumerate(group)]})
        batches = []
        for i in range(0, len(sheets), s.sheets_per_request):
            group = sheets[i:i + s.sheets_per_request]
            batches.append({"index": len(batches) + 1, "sheets": [g["index"] for g in group],
                            "start": group[0]["cells"][0]["t"], "end": group[-1]["cells"][-1]["until"]})
        store.put_json(f"{prefix}/quality.json", {"source": name, **quality(info, sampled, kept, decode)})
        store.put_json(f"{prefix}/sheets.json", {
            **base, "duration_sec": round(duration, 1), "grid": s.grid, "decode": decode, "interval_s": interval,
            "frames_sampled": len(sampled), "frames": len(kept),
            "mean_gap_s": round(duration / len(kept), 1) if duration else None,
            "sheets": sheets, "batches": batches})
        prog.visual(sid, len(batches))
        out["batches"] = [{"index": b["index"], "sheets": [keys[n - 1] for n in b["sheets"]]} for b in batches]
        return out
    except PipelineError as e:
        store.put_json(f"{prefix}/sheets.json", {**base, "error": e.message, "sheets": [], "batches": []})
        prog.visual(sid, 0)
        return out  # the transcript (if any) still makes it into the corpus
    finally:
        shutil.rmtree(tmp, ignore_errors=True)
