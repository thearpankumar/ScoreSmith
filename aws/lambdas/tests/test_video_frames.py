"""PlanFrames: frame selection, near-duplicate removal, the frame cap, contact sheets and quality facts.

The end-to-end tests build short synthetic clips with the bundled ffmpeg (lavfi colour / testsrc2 sources, mpeg4) and
are skipped when no ffmpeg is available."""
import io
import subprocess

import pytest

from fakes import MemoryStore
from worker import frames
from worker.errors import PipelineError
from worker.frames import Frame
from worker.video_settings import VideoSettings

EID = "e1"
PREFIX = "derived/e1/video/v1/frames"


def _ffmpeg() -> str | None:
    try:
        exe = frames.ffmpeg_exe()
        subprocess.run([exe, "-version"], capture_output=True, check=True, timeout=30)
        return exe
    except Exception:
        return None


FFMPEG = _ffmpeg()
needs_ffmpeg = pytest.mark.skipif(FFMPEG is None, reason="no ffmpeg available")


def make_clip(path, segments, audio=True, gop=10):
    """segments: [(lavfi source, seconds)], concatenated at 320x240, 10 fps; a keyframe every `gop` frames."""
    args = [FFMPEG, "-hide_banner", "-y"]
    for src, seconds in segments:
        args += ["-f", "lavfi", "-i", f"{src}{':' if '=' in src else '='}size=320x240:rate=10:duration={seconds}"]
    total = sum(s for _src, s in segments)
    if audio:
        args += ["-f", "lavfi", "-i", f"sine=frequency=440:duration={total}"]
    n = len(segments)
    args += ["-filter_complex", "".join(f"[{i}:v]" for i in range(n)) + f"concat=n={n}:v=1:a=0[v]", "-map", "[v]"]
    if audio:
        args += ["-map", f"{n}:a", "-c:a", "aac"]
    args += ["-c:v", "mpeg4", "-q:v", "5", "-g", str(gop), str(path)]
    subprocess.run(args, capture_output=True, check=True, timeout=120)
    return path


def plan(tmp_path, clip, settings=None, name="demo.mp4"):
    s = MemoryStore()
    s.put_bytes("raw/e1/v1.mp4", clip.read_bytes())
    out = frames.run_plan_frames({"evaluation_id": EID, "bucket": "b", "file": {
        "source_id": "v1", "kind": "video", "raw_key": "raw/e1/v1.mp4", "original_name": name}}, s,
        tmp_root=str(tmp_path), settings=settings or VideoSettings())
    return s, out


THREE_SCENES = [("color=c=red", 7), ("testsrc2", 10), ("color=c=blue", 7)]


def make_slides_clip(path, texts, seconds=6, gop=250):
    """A screen-recording-like clip: one slide per text, same background and layout, only the words change."""
    from PIL import Image, ImageDraw

    for i, text in enumerate(texts, 1):
        im = Image.new("RGB", (640, 360), (30, 60, 120))
        d = ImageDraw.Draw(im)
        d.text((40, 90), f"STEP {i}", fill=(255, 255, 255))
        d.text((40, 160), text, fill=(255, 255, 255))
        im.save(path.parent / f"slide_{i}.png")
    subprocess.run(
        [FFMPEG, "-hide_banner", "-y", "-framerate", f"1/{seconds}", "-i", str(path.parent / "slide_%d.png"),
         "-vf", "fps=10,format=yuv420p", "-c:v", "mpeg4", "-q:v", "4", "-g", str(gop), str(path)],
        capture_output=True, check=True, timeout=120)
    return path


# ----------------------------------------------------------------------------- end to end (real ffmpeg)
@needs_ffmpeg
def test_scene_changes_become_frames_tiled_on_a_timestamped_sheet(tmp_path):
    s, out = plan(tmp_path, make_clip(tmp_path / "c.mp4", THREE_SCENES))
    doc = s.get_json(f"{PREFIX}/sheets.json")
    times = [c["t"] for sh in doc["sheets"] for c in sh["cells"]]
    # samples every 2 s: the red still is one frame, the moving test pattern several, the blue still one more
    assert times[0] == 0.0 and any(7 <= t <= 9 for t in times) and any(17 <= t <= 19 for t in times)
    assert doc["sheets"][0]["cells"][0]["until"] == 6.0  # the red picture lasted until the last red sample
    assert doc["decode"] == "keyframes" and doc["mode"] == "frames" and doc["grid"] == "3x3"
    assert doc["interval_s"] == 2.0
    assert doc["frames"] == len(times) and doc["mean_gap_s"] == round(24 / len(times), 1)
    assert doc["batches"] == [{"index": 1, "sheets": [1], "start": 0.0, "end": doc["sheets"][0]["cells"][-1]["until"]}]
    assert out == {"source_id": "v1", "batches": [{"index": 1, "sheets": [f"{PREFIX}/sheet_01.jpg"]}]}
    from PIL import Image

    sheet = Image.open(io.BytesIO(s.get_bytes(f"{PREFIX}/sheet_01.jpg")))
    assert sheet.format == "JPEG" and sheet.width <= frames.SHEET_WIDTH + 4 * frames.CELL_GAP
    q = s.get_json(f"{PREFIX}/quality.json")
    assert (q["width"], q["height"], q["fps"], q["has_audio"], q["video_codec"]) == (320, 240, 10.0, True, "mpeg4")
    assert q["duration_sec"] == 24.0 and q["sampled_frames"] >= q["kept_frames"] == len(times)
    assert s.get_json("derived/e1/progress/visual-v1.json") == {"source_id": "v1", "requests_total": 1}


@needs_ffmpeg
def test_text_only_changes_on_the_same_background_each_become_a_frame(tmp_path):
    # Live regression: five procedure screens 6 s apart on one background were reduced to 2 frames (ffmpeg's scene
    # score barely moves when only text changes, and a 64-bit dHash saw the slides as identical).
    texts = ["Agent greets the caller", "Identity verified", "Ticket 4521 resolved", "Customer confirms",
             "Anything else? Call ends"]
    s, _ = plan(tmp_path, make_slides_clip(tmp_path / "slides.mp4", texts))
    doc = s.get_json(f"{PREFIX}/sheets.json")
    times = [c["t"] for sh in doc["sheets"] for c in sh["cells"]]
    assert len(times) == 5, times
    for expected, t in zip((0, 6, 12, 18, 24), times):
        assert expected <= t <= expected + 2.5, times
    assert doc["sheets"][0]["cells"][0]["until"] >= 4  # each slide's frames were merged into one


@needs_ffmpeg
def test_a_static_picture_is_sampled_every_interval_then_deduplicated(tmp_path):
    s, _ = plan(tmp_path, make_clip(tmp_path / "still.mp4", [("color=c=red", 50)]))
    doc = s.get_json(f"{PREFIX}/sheets.json")
    assert doc["frames_sampled"] == 25 and doc["frames"] == 1  # 0, 2, ... 48 s: one picture
    assert doc["sheets"][0]["cells"] == [{"cell": 1, "t": 0.0, "until": 48.0}]
    q = s.get_json(f"{PREFIX}/quality.json")
    assert q["static_segments"] == [{"start": "00:00", "end": "00:48", "seconds": 48.0}] and q["static_seconds"] == 48
    assert q["dark_frames"]["count"] == 0 and q["black_frames"]["count"] == 0


@needs_ffmpeg
def test_sparse_keyframes_fall_back_to_a_full_decode_for_short_videos(tmp_path):
    # one keyframe in 50 s: decoding keyframes only would give a single frame, far below one every 2 s
    s, _ = plan(tmp_path, make_clip(tmp_path / "sparse.mp4", [("color=c=red", 50)], gop=1000))
    doc = s.get_json(f"{PREFIX}/sheets.json")
    assert doc["decode"] == "full" and doc["frames_sampled"] == 25  # 0, 2, ... 48 s
    assert s.get_json(f"{PREFIX}/quality.json")["decode"] == "full"


@needs_ffmpeg
def test_silent_video_and_a_small_grid_and_frame_cap(tmp_path):
    clip = make_clip(tmp_path / "silent.mp4", THREE_SCENES, audio=False)
    s, out = plan(tmp_path, clip, VideoSettings(max_frames=2, cols=2, rows=2, sheets_per_request=1))
    doc = s.get_json(f"{PREFIX}/sheets.json")
    assert doc["frames"] == 2 and doc["sheets"][0]["cells"][0]["t"] == 0.0 and doc["grid"] == "2x2"
    assert s.get_json(f"{PREFIX}/quality.json")["has_audio"] is False
    assert len(out["batches"]) == 1


@needs_ffmpeg
def test_unreadable_video_is_a_soft_failure(tmp_path):
    bad = tmp_path / "bad.mp4"
    bad.write_bytes(b"\x00\x00\x00\x18ftypmp42" + b"\0" * 200)
    s, out = plan(tmp_path, bad)
    assert out["batches"] == []
    assert "no video stream" in s.get_json(f"{PREFIX}/sheets.json")["error"]
    assert s.get_json("derived/e1/progress/visual-v1.json")["requests_total"] == 0


def test_visual_mode_off_writes_only_the_marker(tmp_path):
    s = MemoryStore()
    out = frames.run_plan_frames({"evaluation_id": EID, "bucket": "b", "file": {
        "source_id": "v1", "raw_key": "raw/e1/v1.mp4", "original_name": "demo.mp4"}}, s,
        settings=VideoSettings(mode="off"))
    assert out["batches"] == [] and s.get_json(f"{PREFIX}/sheets.json")["mode"] == "off"
    assert "raw/e1/v1.mp4" not in s.objects and not s.exists(f"{PREFIX}/quality.json")  # nothing downloaded


def test_the_handler_routes_plan_frames_mode(monkeypatch):
    from worker import handlers

    seen = {}
    monkeypatch.setattr(handlers, "S3Store", lambda bucket: "store")
    monkeypatch.setattr(frames, "run_plan_frames", lambda event, store: seen.update(event=event, store=store) or {})
    handlers.plan_audio({"mode": "plan_frames", "evaluation_id": EID, "bucket": "b", "file": {"source_id": "v1"}})
    assert seen["event"]["mode"] == "plan_frames" and seen["store"] == "store"


# ----------------------------------------------------------------------------- pure helpers
PROBE = """Input #0, mov,mp4,m4a,3gp,3g2,mj2, from 'video.mp4':
  Duration: 00:12:34.50, start: 0.120000, bitrate: 2400 kb/s
  Stream #0:0[0x1](und): Video: h264 (High) (avc1 / 0x31637661), yuv420p(tv, bt709), 1080x1920, 2200 kb/s, 29.97 fps, 30 tbr
      Side data:
        displaymatrix: rotation of -90.00 degrees
  Stream #0:1[0x2](und): Audio: aac (LC) (mp4a / 0x6134706D), 48000 Hz, stereo, fltp, 128 kb/s
"""


def test_parse_info_reads_the_container_facts():
    info = frames.parse_info(PROBE)
    assert info == {"duration_sec": 754.5, "start_sec": 0.12, "has_video": True, "has_audio": True, "width": 1920,
                    "height": 1080, "fps": 29.97, "bitrate_kbps": 2400, "video_codec": "h264"}
    assert frames.parse_info("x: Invalid data found")["has_video"] is False


def test_showinfo_times_and_the_selector():
    err = ("[Parsed_showinfo_2 @ 0x1] n:   0 pts:      0 pts_time:0       duration:1\n"
           "[Parsed_showinfo_2 @ 0x1] color_range:pc\n"
           "[Parsed_showinfo_2 @ 0x1] n:   1 pts: 204800 pts_time:20.5    duration:1\n")
    assert frames.parse_showinfo(err) == [0.0, 20.5]
    f = frames.select_filter(2.5, 448)
    assert f.startswith("select='isnan(prev_selected_t)+gte(t-prev_selected_t,2.5)'")
    assert "scale=448:448:force_original_aspect_ratio=decrease" in f and f.endswith(",showinfo")


def test_the_sampling_interval_stretches_for_long_videos_within_the_gaps():
    s = VideoSettings(min_gap_s=2, max_gap_s=20)
    assert frames.sample_interval(s, 30) == 2  # short: every 2 s
    assert frames.sample_interval(s, 3600) == 4  # an hour: ~900 samples
    assert frames.sample_interval(s, 6 * 3600) == 20  # never sparser than the max gap
    assert frames.sample_interval(s, 0) == 2


def frame_file(tmp_path, name, color, size=(64, 48), noise=False):
    from PIL import Image

    p = tmp_path / name
    img = Image.effect_noise(size, 90).convert("RGB") if noise else Image.new("RGB", size, color)
    img.save(p, "JPEG")
    return str(p)


def test_dedup_merges_near_duplicates_and_records_how_long_they_lasted(tmp_path):
    red, noise = frame_file(tmp_path, "r.jpg", "red"), frame_file(tmp_path, "n.jpg", None, noise=True)
    seq = [frames.measure(Frame(p, t)) for p, t in ((red, 0), (red, 5), (red, 10), (noise, 15), (red, 20))]
    kept = frames.dedup(seq, 0.001)
    assert [(f.t, f.until) for f in kept] == [(0, 10), (15, 15), (20, 20)]
    assert kept[0].change == 1.0 and kept[1].change > 0.1
    assert frames.changed_share(seq[0].thumb, seq[1].thumb) == 0.0
    assert frames.changed_share(seq[0].thumb, None) == 1.0


def test_cap_keeps_the_first_the_strongest_changes_and_spreads_the_rest():
    fs = [Frame(f"f{i}", float(i * 10), change=(0.9 if i in (7, 13) else 0.1)) for i in range(20)]
    kept = frames.cap(fs, 5, 200.0)
    times = [f.t for f in kept]
    assert len(kept) == 5 and times[0] == 0.0 and {70.0, 130.0} <= set(times)
    assert times == sorted(times)
    assert frames.cap(fs[:3], 5, 30.0) == fs[:3]


def test_tile_fills_a_grid_and_shrinks_the_last_sheet(tmp_path):
    from PIL import Image

    fs = [frames.measure(Frame(frame_file(tmp_path, f"{i}.jpg", "green", (100, 60)), i * 4.0)) for i in range(4)]
    full = Image.open(io.BytesIO(frames.tile(fs, 3, 3)))
    g = frames.CELL_GAP
    assert full.size == (3 * 100 + 4 * g, 2 * 60 + 3 * g)  # 4 frames: 3 columns, 2 rows used
    one = Image.open(io.BytesIO(frames.tile(fs[:1], 3, 3)))
    assert one.size == (100 + 2 * g, 60 + 2 * g)
    assert one.getpixel((g + 2, g + 2))[1] < 100  # the [00:00] label box covers the top-left corner


def test_quality_counts_black_dark_and_low_detail_frames(tmp_path):
    black, gray = frame_file(tmp_path, "b.jpg", "black"), frame_file(tmp_path, "g.jpg", (128, 128, 128))
    noise = frame_file(tmp_path, "n.jpg", None, noise=True)
    sampled = [frames.measure(Frame(p, t)) for p, t in ((black, 0), (gray, 10), (noise, 20))]
    q = frames.quality({"duration_sec": 30.0, "has_audio": False}, sampled, sampled, "keyframes")
    assert q["black_frames"] == {"count": 1, "at": ["00:00"]} and q["dark_frames"]["count"] == 0
    assert q["low_detail_frames"]["judged"] == 1 and q["low_detail_frames"]["count"] == 0  # plain frames never judged
    assert q["brightness"]["min"] < 5 and q["decode"] == "keyframes"


def test_extract_frames_raises_when_ffmpeg_reads_nothing(tmp_path):
    class Failed:
        returncode, stderr = 1, "moov atom not found"

    with pytest.raises(PipelineError, match="moov atom not found"):
        frames.extract_frames("v.mp4", str(tmp_path / "o"), VideoSettings(), runner=lambda args: Failed())
