"""Video visual analysis after PlanFrames: the knobs, the frames prompt, multi-image requests for both providers, the
AnalyzeSheets step, Transcribe's timed segments, and MergeVideo's SAID / SHOWN timeline in the corpus."""
import base64
import io

import pytest

from fakes import FakeConverse, FakeOrSession, MemoryStore, chat_err, chat_ok
from worker import bedrock_ops, corpus, media, openrouter_ops, prompts, run_config, scorecard_context, timeline
from worker import video_settings
from worker.errors import PipelineError, TransientError
from worker.ingest import run_ingest
from worker.progress import merge_progress

EID = "e1"
FR = "derived/e1/video/v1/frames"
KEY = "sk-or-v1-SECRETKEY123"


def jpeg(size=(300, 200), color="white", noise=False):
    from PIL import Image

    img = Image.effect_noise(size, 80).convert("RGB") if noise else Image.new("RGB", size, color)
    buf = io.BytesIO()
    img.save(buf, "JPEG", quality=95)
    return buf.getvalue()


# ----------------------------------------------------------------------------- knobs
def test_settings_come_from_the_environment_then_the_run():
    env = {"FRAME_MAX_GAP_S": "30", "GRID": "2x2", "FRAME_CHANGE_THRESHOLD": "nonsense", "MAX_FRAMES_PER_VIDEO": "99999"}
    s = video_settings.resolve(None, env)
    assert (s.max_gap_s, s.cols, s.rows, s.change_threshold, s.max_frames) == (30.0, 2, 2, 0.001, 540)
    s = video_settings.resolve({"video_visual_mode": "OFF", "frame_min_gap_s": 50, "sheets_per_request": 0,
                                "grid": "9x9", "unknown": 1}, env)
    assert s.mode == "off" and s.min_gap_s == 50 and s.max_gap_s == 50  # max gap never below the min gap
    assert s.sheets_per_request == 1 and s.grid == "2x2"  # the bad grid is ignored, the env one stays
    defaults = video_settings.resolve(None, {})
    d = defaults
    assert (d.mode, d.min_gap_s, d.max_gap_s, d.change_threshold, d.max_frames, d.grid,
            d.sheets_per_request, d.max_output_tokens) == ("frames", 2.0, 20.0, 0.001, 108, "3x3", 4, 1500)
    # The old scene-score knobs no longer exist: a run that still sends them simply has them dropped.
    assert video_settings.clean({"scene_threshold": 0.3, "dhash_max_distance": 6}) == {}


def test_ingest_saves_the_runs_video_overrides_and_kpi_names():
    s = MemoryStore()
    s.put_bytes("uploads/u/g/1.pdf", b"%PDF-1.4\n" + b"x" * 100)
    run_ingest({"evaluation_id": EID, "bucket": "b", "limits": {"max_file_bytes": 10_000, "max_files": 20},
                "sources": [{"source_id": "a", "kind": "upload", "s3_key": "uploads/u/g/1.pdf",
                             "original_name": "a.pdf"}],
                "video": {"grid": "2x2", "frame_max_gap_s": "15", "bogus": True, "frame_change_threshold": "x"},
                "context": {"scorecard": "Lab safety", "kpis": ["Wears gloves", " Wears  gloves ", 7, "x" * 500]}}, s)
    assert s.get_json("derived/e1/run_config.json") == {"provider": "bedrock",
                                                         "video": {"frame_max_gap_s": 15.0, "grid": "2x2"}}
    loaded = video_settings.load(s, EID)
    assert loaded.grid == "2x2" and loaded.max_gap_s == 15.0 and loaded.min_gap_s == 2.0
    ctx = s.get_json("derived/e1/context.json")
    assert ctx["kpis"] == ["Wears gloves", "x" * 120]
    assert run_config.load(s, EID) == "bedrock"


def test_kpi_names_are_capped():
    ctx = scorecard_context.clean({"scorecard": "S", "kpis": [f"KPI number {i}" for i in range(200)]})
    assert len(ctx["kpis"]) == scorecard_context.MAX_KPIS


# ----------------------------------------------------------------------------- the prompt
def test_frames_prompt_puts_the_scorecard_and_its_kpis_before_the_frame_list():
    ctx = {"scorecard": "Lab safety", "domain": "Labs", "kpis": ["Wears gloves", "Labels samples"]}
    p = prompts.video_frames_prompt(ctx, "Sheet 1: 00:00, 00:04")
    assert p.startswith('Context: the video is being evaluated with the scorecard "Lab safety" (domain: Labs).')
    assert "The scorecard grades: Wears gloves; Labels samples." in p
    assert p.index("Sheet 1: 00:00") < p.index("[mm:ss] SHOWN:") and p.endswith(prompts.VIDEO_FRAMES_PROMPT)
    for line in ("[mm:ss] SHOWN:", "Steps observed, in order:", "Visual quality:", "Not visible in sampled frames:"):
        assert line in prompts.VIDEO_FRAMES_PROMPT
    assert prompts.video_frames_prompt(None, "LIST") == "LIST\n\n" + prompts.VIDEO_FRAMES_PROMPT
    # the image prompt is unchanged by the KPI names
    assert "grades" not in prompts.image_prompt({"scorecard": "Lab safety", "kpis": ["Wears gloves"]})


# ----------------------------------------------------------------------------- multi-image requests
def test_openrouter_sends_every_sheet_in_one_request_and_falls_back(monkeypatch):
    monkeypatch.setenv("OPENROUTER_API_KEY", KEY)
    vm = openrouter_ops.video_models()
    assert vm == ["qwen/qwen3.7-flash", "google/gemini-3.1-flash-lite"]
    ok = chat_ok("[00:00] SHOWN: a form", 900, 120)
    ok._body["usage"]["cost"] = 0.00042
    sess = FakeOrSession({vm[0]: chat_err(404, "No endpoints found"), vm[1]: ok})
    client = openrouter_ops.OpenRouterClient(session=sess, sleep=lambda s: None)
    text, model, usage = openrouter_ops.analyse_sheets(client, vm, [jpeg(), jpeg(color="red")], "PROMPT", 1500)
    assert model == vm[1] and usage == {"tokens_in": 900, "tokens_out": 120, "cost_usd": 0.00042}
    body = sess.calls[0]["json"]
    parts = body["messages"][0]["content"]
    assert parts[0] == {"type": "text", "text": "PROMPT"} and [p["type"] for p in parts[1:]] == ["image_url"] * 2
    assert base64.b64decode(parts[2]["image_url"]["url"].split(",", 1)[1]) == jpeg(color="red")
    assert body["max_tokens"] == 1500 and body["temperature"] == 0 and body["reasoning"] == {"enabled": False}
    monkeypatch.setenv("OPENROUTER_VIDEO_MODELS", "a/b, c/d")
    assert openrouter_ops.video_models() == ["a/b", "c/d"]


def test_bedrock_sends_the_sheets_then_the_prompt_nova_first():
    fc = FakeConverse({bedrock_ops.VIDEO_MODELS[0]: RuntimeError("throttled"), bedrock_ops.VIDEO_MODELS[1]: "notes"})
    text, model, usage = bedrock_ops.analyse_sheets(fc, bedrock_ops.video_models(), [jpeg()] * 3, "PROMPT", 1200)
    assert (text, model) == ("notes", "qwen.qwen3-vl-235b-a22b")
    assert [c["modelId"] for c in fc.calls] == ["us.amazon.nova-2-lite-v1:0", "qwen.qwen3-vl-235b-a22b"]
    content = fc.calls[0]["messages"][0]["content"]
    assert [list(b) for b in content] == [["image"]] * 3 + [["text"]] and content[-1]["text"] == "PROMPT"
    assert fc.calls[0]["inferenceConfig"] == {"maxTokens": 1200, "temperature": 0}
    with pytest.raises(TransientError, match="all video models failed"):
        bedrock_ops.analyse_sheets(FakeConverse({m: RuntimeError("x") for m in bedrock_ops.VIDEO_MODELS}),
                                   bedrock_ops.VIDEO_MODELS, [jpeg()], "P", 100)


def test_a_too_large_body_shrinks_every_sheet():
    sizes = []

    def picky(kw):
        blocks = [b for b in kw["messages"][0]["content"] if "image" in b]
        sizes.append(sum(len(b["image"]["source"]["bytes"]) for b in blocks))
        if len(sizes) < 3:
            raise RuntimeError("Input is too long: request body exceeds the maximum")
        return "ok"

    fc = FakeConverse({m: picky for m in bedrock_ops.VIDEO_MODELS})
    big = jpeg((1800, 1800), noise=True)
    assert bedrock_ops.analyse_sheets(fc, bedrock_ops.VIDEO_MODELS, [big, big], "P", 100)[0] == "ok"
    assert sizes[0] > sizes[1] > sizes[2]


# ----------------------------------------------------------------------------- the AnalyzeSheets step
def seed_sheets(s, run="r1", provider="bedrock"):
    run_config.save(s, EID, provider)
    s.put_json(f"{FR}/sheets.json", {
        "source": "demo.mp4", "run": run, "mode": "frames", "duration_sec": 70.0, "grid": "3x3", "frames": 4,
        "mean_gap_s": 17.5, "decode": "keyframes",
        "sheets": [{"index": 1, "key": f"{FR}/sheet_01.jpg", "cells": [
            {"cell": 1, "t": 0.0, "until": 20.0}, {"cell": 2, "t": 24.0, "until": 24.0}]},
                   {"index": 2, "key": f"{FR}/sheet_02.jpg", "cells": [
                       {"cell": 1, "t": 40.0, "until": 40.0}, {"cell": 2, "t": 60.0, "until": 60.0}]}],
        "batches": [{"index": 1, "sheets": [1], "start": 0.0, "end": 24.0},
                    {"index": 2, "sheets": [2], "start": 40.0, "end": 60.0}]})
    s.put_bytes(f"{FR}/sheet_01.jpg", jpeg())
    s.put_bytes(f"{FR}/sheet_02.jpg", jpeg(color="blue"))


def sheet_event(n=1, keys=None):
    return {"mode": "video_sheets", "evaluation_id": EID, "bucket": "b", "source_id": "v1",
            "batch": {"index": n, "sheets": keys or [f"{FR}/sheet_{n:02d}.jpg"]}}


NOTES_1 = """[00:00] SHOWN: The login page of the admin console.
**[00:24] SHOWN:** The orders table, row 42 selected.
Steps observed, in order:
1. [00:00] Signed in.
2. [00:24] Opened order 42.
Visual quality: text is readable; slight glare.
Not visible in sampled frames: the refund confirmation."""


def test_analyze_sheets_writes_the_notes_and_their_usage():
    s = MemoryStore()
    seed_sheets(s)
    s.put_json("derived/e1/context.json", {"scorecard": "Refund QA", "kpis": ["Verifies the order"]})
    fc = FakeConverse({bedrock_ops.VIDEO_MODELS[0]: NOTES_1})
    out = media.run_analyze_image(sheet_event(1), s, client=fc)
    assert out == {"source_id": "v1", "index": 1, "ok": True, "model": "us.amazon.nova-2-lite-v1:0"}
    prompt = fc.calls[0]["messages"][0]["content"][-1]["text"]
    assert "The scorecard grades: Verifies the order." in prompt
    assert "Sheet 1: 00:00 (unchanged until 00:20), 00:24" in prompt
    md = s.get_text(f"{FR}/batch_01.md")
    assert md.startswith("<!-- source: demo.mp4 | sheets: sheet_01.jpg | model: us.amazon.nova-2-lite-v1:0")
    assert NOTES_1 in md
    assert s.get_json(f"{FR}/batch_01.json") == {"batch": 1, "run": "r1", "sheets": [1], "frames": 2, "start": 0.0,
                                                  "end": 24.0, "model": "us.amazon.nova-2-lite-v1:0", "tokens_in": 1,
                                                  "tokens_out": 2, "cost_usd": None}
    assert s.exists("derived/e1/progress/vis/v1/1.done")


@pytest.mark.parametrize("keys", [
    ["derived/e2/video/v1/frames/sheet_01.jpg"],             # another evaluation
    ["derived/e1/video/v1/frames/../../v2/frames/x.jpg"],    # traversal
    ["derived/e1/docs/v1/images/0001_image.jpg"],             # not a frame sheet
    [f"{FR}/sheet_01.jpg"] * 9,                               # too many for one request
])
def test_analyze_sheets_rejects_keys_outside_the_videos_frames(keys):
    s = MemoryStore()
    seed_sheets(s)
    with pytest.raises(PipelineError):
        media.run_analyze_image(sheet_event(1, keys), s, client=FakeConverse({}))


def test_an_undecodable_sheet_is_a_soft_failure_but_billing_is_not(monkeypatch):
    s = MemoryStore()
    seed_sheets(s, provider="openrouter")
    s.put_bytes(f"{FR}/sheet_01.jpg", b"not a jpeg")
    monkeypatch.setenv("OPENROUTER_API_KEY", KEY)
    client = openrouter_ops.OpenRouterClient(session=FakeOrSession({}), sleep=lambda x: None)
    out = media.run_analyze_image(sheet_event(1), s, client=client)
    assert out["ok"] is False and s.exists("derived/e1/progress/vis/v1/1.failed")
    broke = openrouter_ops.OpenRouterClient(session=FakeOrSession({m: chat_err(402, "no credits")
                                                                   for m in openrouter_ops.VIDEO_MODELS}))
    with pytest.raises(PipelineError):
        media.run_analyze_image(sheet_event(2), s, client=broke)


def test_document_images_keep_their_usage_in_a_sidecar(monkeypatch):
    s = MemoryStore()
    run_config.save(s, EID, "openrouter")
    key = "derived/e1/docs/d1/images/0002_p01_image.jpg"
    s.put_bytes(key, jpeg())
    monkeypatch.setenv("OPENROUTER_API_KEY", KEY)
    ok = chat_ok("## Summary\nA chart.", 50, 7)
    ok._body["usage"]["cost"] = 0.0001
    client = openrouter_ops.OpenRouterClient(session=FakeOrSession({openrouter_ops.VISION_MODELS[0]: ok}))
    assert media.run_analyze_image({"evaluation_id": EID, "bucket": "b", "source_id": "d1", "image_key": key}, s,
                                   client=client)["ok"]
    assert s.get_json("derived/e1/docs/d1/images/0002_p01_image.analysis.json") == {
        "model": "qwen/qwen3.7-flash", "tokens_in": 50, "tokens_out": 7, "cost_usd": 0.0001}


# ----------------------------------------------------------------------------- Transcribe's timed segments
def test_transcribe_segments_from_audio_segments_or_items():
    results = {"audio_segments": [{"start_time": "0.5", "end_time": "3.0", "transcript": "Hello there."},
                                  {"start_time": "4.0", "end_time": "6.5", "transcript": " Next  step. "}]}
    assert bedrock_ops.transcript_segments(results) == [{"start": 0.5, "end": 3.0, "text": "Hello there."},
                                                        {"start": 4.0, "end": 6.5, "text": "Next step."}]
    items = [{"type": "pronunciation", "start_time": "1.0", "end_time": "1.4", "alternatives": [{"content": "Open"}]},
             {"type": "pronunciation", "start_time": "1.5", "end_time": "1.9", "alternatives": [{"content": "it"}]},
             {"type": "punctuation", "alternatives": [{"content": "."}]},
             {"type": "pronunciation", "start_time": "3.0", "end_time": "3.2", "alternatives": [{"content": "Done"}]}]
    assert bedrock_ops.transcript_segments({"items": items}) == [{"start": 1.0, "end": 1.9, "text": "Open it."},
                                                                 {"start": 3.0, "end": 3.2, "text": "Done"}]


def test_chunk_json_keeps_segment_times_from_the_start_of_the_video(monkeypatch):
    from fakes import FakeTranscribe

    t = bedrock_ops.Transcript("Hello there. Next step.", [{"start": 0.5, "end": 3.0, "text": "Hello there."},
                                                           {"start": 4.0, "end": 6.5, "text": "Next step."}])
    monkeypatch.setattr(bedrock_ops, "transcribe_client", lambda: FakeTranscribe())
    monkeypatch.setattr(bedrock_ops, "_fetch_transcript", lambda uri, session=None: t)
    s = MemoryStore()
    s.put_bytes("derived/e1/video/v1/chunks/c2.mp3", b"audio")
    media.run_transcribe_chunk({"evaluation_id": EID, "bucket": "b", "source_id": "v1", "chunk": {
        "index": 2, "key": "derived/e1/video/v1/chunks/c2.mp3", "start": 300.0, "end": 600.0}}, s,
        client=FakeConverse({}))
    meta = s.get_json("derived/e1/video/v1/chunks/c2.json")
    assert meta["segments"] == [{"start": 300.5, "end": 303.0, "text": "Hello there."},
                                {"start": 304.0, "end": 306.5, "text": "Next step."}]
    assert s.get_text("derived/e1/video/v1/chunks/c2.txt") == "Hello there. Next step."


# ----------------------------------------------------------------------------- notes parsing
def test_parse_notes_reads_the_contract_lines_and_snaps_invented_times():
    text = ("- [00:00] SHOWN: Login page.\n**[1:30:00] SHOWN:** Way out of range.\n"
            "### Steps observed, in order\n1. [00:00] Signed in.\n\n**Visual quality:** Sharp.\n"
            "Not visible in sampled frames: (nothing notable)\nSomething unrelated")
    n = timeline.parse_notes(text, [0.0, 24.0])
    assert n.shown == [(0.0, "Login page."), (24.0, "Way out of range.")]
    assert n.steps == ["1. [00:00] Signed in."] and n.quality == ["Sharp."]
    assert n.not_visible == ["(nothing notable)", "Something unrelated"]
    assert timeline.parse_ts("1:02:03") == 3723.0 and timeline.parse_ts("00:61") is None


# ----------------------------------------------------------------------------- MergeVideo and the corpus
def seed_video(s, *, audio=True, segments=True, visual="ok"):
    s.put_json("derived/e1/manifest.json", {"drive": [], "files": [
        {"source_id": "v1", "original_name": "demo.mp4", "kind": "video", "raw_key": "raw/e1/v1.mp4", "size": 1,
         "status": "ok", "warnings": []}]})
    v = "derived/e1/video/v1"
    if audio:
        s.put_json(f"{v}/plan.json", {"source": "demo.mp4", "duration_sec": 70.0, "has_audio": True, "chunks": [
            {"index": 1, "key": "k1", "start": 0.0, "end": 30.0},
            {"index": 2, "key": "k2", "start": 30.0, "end": 70.0}]})
        s.put_text(f"{v}/chunks/c1.txt", "Welcome to the refund demo. First I sign in to the console.")
        s.put_text(f"{v}/chunks/c2.txt", "Now I approve the refund and the customer gets an email right away.")
        meta1 = {"chunk": 1, "model": "amazon.transcribe"}
        if segments:
            meta1["segments"] = [{"start": 0.0, "end": 3.0, "text": "Welcome to the refund demo."},
                                 {"start": 22.0, "end": 26.0, "text": "First I sign in to the console."}]
        s.put_json(f"{v}/chunks/c1.json", meta1)
        s.put_json(f"{v}/chunks/c2.json", {"chunk": 2, "model": "mistralai/voxtral-small-24b-2507"})
    else:
        s.put_json(f"{v}/plan.json", {"source": "demo.mp4", "duration_sec": 70.0, "has_audio": False, "chunks": []})
    if visual == "off":
        s.put_json(f"{FR}/sheets.json", {"source": "demo.mp4", "run": "r1", "mode": "off", "sheets": [], "batches": []})
        return
    if visual is None:
        return
    seed_sheets(s)
    s.put_json(f"{FR}/quality.json", {"source": "demo.mp4", "duration_sec": 70.0, "width": 1920, "height": 1080,
                                      "fps": 30.0, "bitrate_kbps": 2400, "video_codec": "h264", "has_audio": audio,
                                      "decode": "keyframes", "sampled_frames": 6, "kept_frames": 4,
                                      "brightness": {"mean": 120.0, "min": 3.0, "max": 200.0},
                                      "black_frames": {"count": 1, "at": ["00:00"]},
                                      "dark_frames": {"count": 0, "at": []}, "bright_frames": {"count": 0, "at": []},
                                      "low_detail_frames": {"count": 0, "at": [], "judged": 5},
                                      "static_segments": [{"start": "00:00", "end": "00:40", "seconds": 40.0}]})
    if visual == "ok":
        s.put_text(f"{FR}/batch_01.md", "<!-- source: demo.mp4 | model: m -->\n\n" + NOTES_1)
        s.put_json(f"{FR}/batch_01.json", {"batch": 1, "run": "r1", "start": 0.0, "end": 24.0,
                                            "model": "qwen/qwen3.7-flash", "tokens_in": 1000, "tokens_out": 300,
                                            "cost_usd": 0.0004})
        s.put_text(f"{FR}/batch_02.md",
                   "[00:40] SHOWN: Refund dialog, amount 25.00 EUR.\n[01:00] SHOWN: Email sent banner.")
        s.put_json(f"{FR}/batch_02.json", {"batch": 2, "run": "OLD-RUN", "start": 40.0, "end": 60.0,
                                            "model": "qwen/qwen3.7-flash", "tokens_in": 9, "tokens_out": 9})


def assemble(s):
    corpus.run_assemble({"evaluation_id": EID, "bucket": "b"}, s)
    return s.get_json("derived/e1/corpus.json")


def test_said_and_shown_lines_are_interleaved_per_audio_window(monkeypatch):
    s = MemoryStore()
    seed_video(s)
    s.put_json(f"{FR}/batch_02.json", {"batch": 2, "run": "r1", "start": 40.0, "end": 60.0,
                                        "model": "qwen/qwen3.7-flash", "tokens_in": 800, "tokens_out": 200,
                                        "cost_usd": 0.0003})
    c = assemble(s)
    secs = {x["label"]: x["text"] for x in c["sections"]}
    assert list(secs) == ["VIDEO demo.mp4 00:00-00:30", "VIDEO demo.mp4 00:30-01:10", "VIDEO demo.mp4 steps",
                          "VIDEO demo.mp4 quality"]
    assert secs["VIDEO demo.mp4 00:00-00:30"].splitlines() == [
        "[00:00] SAID: Welcome to the refund demo.",
        "[00:00] SHOWN: The login page of the admin console.",
        "[00:22] SAID: First I sign in to the console.",
        "[00:24] SHOWN: The orders table, row 42 selected."]
    # no Transcribe segments (OpenRouter): one SAID line at the chunk start
    assert secs["VIDEO demo.mp4 00:30-01:10"].splitlines() == [
        "[00:30] SAID: Now I approve the refund and the customer gets an email right away.",
        "[00:40] SHOWN: Refund dialog, amount 25.00 EUR.", "[01:00] SHOWN: Email sent banner."]
    steps = secs["VIDEO demo.mp4 steps"]
    assert "Steps observed in the frames 00:00-00:24, in order:\n1. [00:00] Signed in." in steps
    assert "Not visible in the sampled frames 00:00-00:24: the refund confirmation." in steps
    q = secs["VIDEO demo.mp4 quality"]
    assert q.startswith("Technical: 1920x1080, 30 fps, 2400 kb/s, h264, 01:10 long, with an audio track.")
    assert "1 black frame(s) (at 00:00)" in q and "Static picture" in q and "slight glare" in q
    assert c["stats"]["visual"] == {"frames": 4, "sheets": 2, "requests": 2, "tokens_in": 1800, "tokens_out": 500,
                                    "cost_usd": 0.0007}
    v = s.get_json("derived/e1/video/v1/visual.json")
    assert (v["status"], v["frames"], v["requests"], v["requests_ok"], v["mean_gap_s"]) == ("ok", 4, 2, 2, 17.5)
    assert v["models"] == ["qwen/qwen3.7-flash"] and v["error"] is None
    assert s.get_text("derived/e1/video/v1/transcript.txt").startswith("Welcome to the refund demo.")


def test_notes_from_an_earlier_run_are_ignored_and_reported():
    s = MemoryStore()
    seed_video(s)  # batch 2 belongs to another PlanFrames run
    c = assemble(s)
    text = "\n".join(x["text"] for x in c["sections"])
    assert "Refund dialog" not in text and "The orders table" in text
    assert any("1 of 2 frame-sheet request(s)" in w for w in c["warnings"])
    assert s.get_json("derived/e1/video/v1/visual.json")["requests_ok"] == 1


def test_a_silent_video_gets_sections_from_its_frames(monkeypatch):
    monkeypatch.setattr(corpus, "MIN_CORPUS_WORDS", 1)
    from worker.progress import Progress

    s = MemoryStore()
    seed_video(s, audio=False)
    s.put_json(f"{FR}/batch_02.json", {"batch": 2, "run": "r1", "start": 40.0, "end": 60.0, "model": "m"})
    Progress(s, EID).file("v1", "demo.mp4", "pending")
    c = assemble(s)
    labels = [x["label"] for x in c["sections"]]
    assert labels == ["VIDEO demo.mp4 00:00-00:40", "VIDEO demo.mp4 00:40-01:10", "VIDEO demo.mp4 steps",
                      "VIDEO demo.mp4 quality"]
    assert c["sections"][0]["text"].startswith("[00:00] SHOWN: The login page")
    assert c["stats"]["videos"] == 1 and any("no audio track" in w for w in c["warnings"])
    # analysed from its frames: done, no longer "skipped"
    assert {f["source_id"]: f["state"] for f in s.get_json("derived/e1/progress.json")["files"]} == {"v1": "done"}
    assert "no audio track" in c["sections"][-1]["text"]


def test_a_failed_visual_analysis_only_adds_a_warning():
    s = MemoryStore()
    seed_video(s, visual=None)  # PlanFrames never finished (its branch was caught)
    c = assemble(s)
    assert [x["label"] for x in c["sections"]] == ["VIDEO demo.mp4 00:00-00:30", "VIDEO demo.mp4 00:30-01:10"]
    assert any("frames could not be analysed" in w for w in c["warnings"]) and "visual" not in c["stats"]
    v = s.get_json("derived/e1/video/v1/visual.json")
    assert v["status"] == "failed" and "did not finish" in v["error"]


def test_visual_mode_off_records_off_without_a_warning():
    s = MemoryStore()
    seed_video(s, visual="off")
    c = assemble(s)
    assert s.get_json("derived/e1/video/v1/visual.json")["status"] == "off"
    assert not any("frames" in w for w in c["warnings"]) and "visual" not in c["stats"]


def test_silent_video_without_any_analysis_is_still_skipped():
    s = MemoryStore()
    seed_video(s, audio=False, visual=None)
    s.put_json("derived/e1/manifest.json", {"drive": [], "files": [
        {"source_id": "d1", "original_name": "a.pdf", "kind": "pdf", "raw_key": "r", "size": 1, "status": "ok",
         "warnings": []},
        {"source_id": "v1", "original_name": "demo.mp4", "kind": "video", "raw_key": "r", "size": 1, "status": "ok",
         "warnings": []}]})
    s.put_text("derived/e1/docs/d1/content.md", "## Page 1\n\n" + "A real solution document sentence. " * 12)
    c = assemble(s)
    assert c["stats"]["videos"] == 0 and [x["kind"] for x in c["sections"]] == ["doc"]


def test_long_windows_are_split_between_lines():
    events = [timeline.Event(float(i), "SAID", "word " * 60) for i in range(100)]
    parts = timeline.split_window(0.0, 100.0, events, limit=2000)
    assert len(parts) > 1 and parts[0][0] == 0.0 and parts[-1][1] == 100.0
    assert all(len(body) <= 2000 for _a, _b, body in parts)
    assert parts[1][0] == parts[0][1]


# ----------------------------------------------------------------------------- progress
def test_vision_requests_count_as_images_until_done():
    files = [{"source_id": "v1", "name": "demo.mp4", "state": "running", "chunks_total": 1}]
    visual = [{"source_id": "v1", "requests_total": 2}]
    keys = ["derived/e1/progress/chunk/v1/1.done", "derived/e1/progress/vis/v1/1.done"]
    doc = merge_progress({"stage": "extract"}, files, keys, visual_docs=visual)
    assert doc["counters"]["images_total"] == 2 and doc["counters"]["images_done"] == 1
    assert doc["stage"] == "analyze" and doc["files"][0]["state"] == "running"
    keys.append("derived/e1/progress/vis/v1/2.failed")
    doc = merge_progress({"stage": "extract"}, files, keys, visual_docs=visual)
    assert doc["files"][0]["state"] == "done"


def test_plan_audio_keeps_a_silent_video_running_while_its_frames_are_analysed(tmp_path):
    from worker import audio

    s = MemoryStore()
    s.put_bytes("raw/e1/v1.mp4", b"v")
    ev = {"evaluation_id": EID, "bucket": "b", "file": {"source_id": "v1", "kind": "video", "raw_key": "raw/e1/v1.mp4",
                                                        "original_name": "demo.mp4"}}
    audio.run_plan_audio(ev, s, probe_fn=lambda p: (10.0, False, True), tmp_root=str(tmp_path))
    f = s.get_json("derived/e1/progress/file-v1.json")
    assert f["state"] == "running" and "analysing the frames" in f["detail"]
    run_config.save(s, EID, "bedrock", {"video_visual_mode": "off"})
    audio.run_plan_audio(ev, s, probe_fn=lambda p: (10.0, False, True), tmp_root=str(tmp_path))
    assert s.get_json("derived/e1/progress/file-v1.json")["state"] == "skipped"
