"""Video -> audio -> silence-aware chunks (ported from scripts/transcribe_videos.py) and the PlanAudio step."""
from __future__ import annotations

import os
import re
import shutil
import subprocess
import tempfile

from .errors import PipelineError
from .progress import Progress
from .store import derived

CHUNK_SECONDS = 300.0  # ~5 minute target
MAX_DURATION_S = 6 * 3600
MAX_CHUNKS = 80
# strictest first: clean studio audio has deep silences, a noisy recording only has shallow pauses
SILENCE_LEVELS_DB = (-40, -35, -30, -25, -20)


def ffmpeg_exe() -> str:
    env = os.environ.get("FFMPEG_BIN")
    if env:
        return env
    found = shutil.which("ffmpeg")
    if found:
        return found
    import imageio_ffmpeg

    return imageio_ffmpeg.get_ffmpeg_exe()


def ffmpeg(*args: str) -> subprocess.CompletedProcess:
    return subprocess.run([ffmpeg_exe(), "-hide_banner", "-y", *args], capture_output=True, text=True, errors="replace")


def parse_probe(stderr: str) -> tuple[float, bool, bool]:
    """(duration seconds, has audio stream, looks like a media file) from `ffmpeg -i` stderr."""
    m = re.search(r"Duration: (\d+):(\d+):([\d.]+)", stderr)
    dur = int(m.group(1)) * 3600 + int(m.group(2)) * 60 + float(m.group(3)) if m else 0.0
    has_audio = bool(re.search(r"Stream #.*Audio:", stderr))
    has_video = bool(re.search(r"Stream #.*Video:", stderr))
    return dur, has_audio, bool(m) and (has_audio or has_video)


def probe(path: str) -> tuple[float, bool, bool]:
    return parse_probe(ffmpeg("-i", path).stderr)


def extract_audio(video: str, mp3: str) -> None:
    r = ffmpeg("-i", video, "-vn", "-ac", "1", "-ar", "16000", "-b:a", "48k", mp3)
    if r.returncode != 0 or not os.path.exists(mp3) or os.path.getsize(mp3) == 0:
        tail = (r.stderr.strip().splitlines() or ["unknown error"])[-1][:200]
        raise PipelineError("extract_failed", "ffmpeg could not extract audio: " + tail)


def silence_gaps(mp3: str, level_db: int) -> list[tuple[float, float]]:
    """(start, end) of every pause of >= 0.3 s quieter than level_db."""
    err = ffmpeg("-i", mp3, "-af", f"silencedetect=noise={level_db}dB:d=0.3", "-f", "null", "-").stderr
    starts = [float(x) for x in re.findall(r"silence_start: ([\d.]+)", err)]
    ends = [float(x) for x in re.findall(r"silence_end: ([\d.]+)", err)]
    return list(zip(starts, ends))


def best_cut(gaps_for, goal: float, floor: float, cache: dict, window: float = 30.0) -> float | None:
    """Cut point within +-window s of `goal`: loosen the silence threshold until a pause exists there,
    then take the longest pause (most likely a sentence boundary). None if the audio has no pauses at all.
    `gaps_for(level_db)` returns the gap list for a threshold."""
    for level in SILENCE_LEVELS_DB:
        if level not in cache:
            cache[level] = gaps_for(level)
        near = [(s, e) for s, e in cache[level] if abs((s + e) / 2 - goal) <= window and (s + e) / 2 > floor + 30]
        if near:
            s, e = max(near, key=lambda g: (g[1] - g[0], -abs((g[0] + g[1]) / 2 - goal)))
            return (s + e) / 2
    return None


def plan_chunks(duration: float, gaps_for, target: float = CHUNK_SECONDS) -> list[tuple[float, float]]:
    if duration <= target * 1.3:
        return [(0.0, duration)]
    cache: dict = {}
    cuts, pos = [], 0.0
    while duration - pos > target * 1.3:
        cut = best_cut(gaps_for, pos + target, pos, cache)
        pos = cut if cut is not None else pos + target  # hard cut only if no pause anywhere near
        cuts.append(pos)
    bounds = [0.0, *cuts, duration]
    return list(zip(bounds[:-1], bounds[1:]))


def run_plan_audio(event: dict, store, probe_fn=probe, extract_fn=extract_audio, gaps_fn=silence_gaps,
                   cut_fn=None, tmp_root: str | None = None) -> dict:
    """PlanAudio: download video, extract mono 16 kHz mp3, pick silence-aware chunk bounds, upload chunks."""
    eid, f = event["evaluation_id"], event["file"]
    sid, name = f["source_id"], f.get("original_name", f["source_id"])
    prog = Progress(store, eid)
    out = {"source_id": sid, "chunks": [], "has_audio": False}
    prefix = derived(eid, "video", sid)
    prog.file(sid, name, "running", "extracting audio")
    tmp = tempfile.mkdtemp(prefix="plan_", dir=tmp_root)
    try:
        video = os.path.join(tmp, "video." + (f["raw_key"].rsplit(".", 1)[-1] if "." in f["raw_key"] else "bin"))
        store.download(f["raw_key"], video)
        duration, has_audio, valid = probe_fn(video)
        if not valid:
            raise PipelineError("extract_failed", "File is not a readable video")
        if not has_audio:
            store.put_json(f"{prefix}/plan.json", {"source": name, "duration_sec": round(duration, 1),
                                                   "has_audio": False, "chunks": []})
            prog.file(sid, name, "skipped", "no audio track in this video")
            return out
        if duration > MAX_DURATION_S:
            raise PipelineError("file_too_large", f"Video is {duration / 3600:.1f} h long (limit {MAX_DURATION_S // 3600} h)")
        mp3 = os.path.join(tmp, "audio.mp3")
        extract_fn(video, mp3)
        os.remove(video)  # free /tmp before cutting
        store.upload(mp3, f"{prefix}/audio.mp3", "audio/mpeg")
        bounds = plan_chunks(duration, lambda lv: gaps_fn(mp3, lv))
        if len(bounds) > MAX_CHUNKS:
            raise PipelineError("file_too_large", f"Video needs {len(bounds)} audio chunks (limit {MAX_CHUNKS})")
        chunks = []
        for i, (a, b) in enumerate(bounds, 1):
            key = f"{prefix}/chunks/c{i}.mp3"
            piece = os.path.join(tmp, f"c{i}.mp3")
            if len(bounds) > 1:
                if cut_fn:
                    cut_fn(mp3, piece, a, b)
                else:
                    r = ffmpeg("-i", mp3, "-ss", f"{a:.2f}", "-to", f"{b:.2f}", "-ac", "1", "-ar", "16000", "-b:a", "48k", piece)
                    if r.returncode != 0:
                        raise PipelineError("extract_failed", "ffmpeg chunking failed")
            else:
                piece = mp3
            store.upload(piece, key, "audio/mpeg")
            chunks.append({"index": i, "key": key, "start": round(a, 1), "end": round(b, 1)})
        store.put_json(f"{prefix}/plan.json", {"source": name, "duration_sec": round(duration, 1),
                                               "has_audio": True, "chunks": chunks})
        out.update(chunks=chunks, has_audio=True)
        prog.file(sid, name, "running", "", chunks_total=len(chunks))
        return out
    except PipelineError as e:
        store.put_json(f"{prefix}/plan.json", {"source": name, "error": e.message, "chunks": [], "has_audio": False})
        prog.file(sid, name, "failed", e.message[:200])
        return out  # per-file failure: the evaluation continues with whatever else has content
    finally:
        shutil.rmtree(tmp, ignore_errors=True)
