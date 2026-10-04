import io

import pytest

from fakes import FakeConverse, MemoryStore
from worker import audio, bedrock_ops as bo
from worker.errors import PipelineError, TransientError

EID = "e1"


# ----------------------------------------------------------------------------- chunk planning
def gaps_factory(gaps_by_level):
    calls = []

    def f(level):
        calls.append(level)
        return gaps_by_level.get(level, [])

    f.calls = calls
    return f


def test_short_audio_single_chunk():
    assert audio.plan_chunks(380.0, gaps_factory({})) == [(0.0, 380.0)]


def test_cut_in_longest_pause_near_goal():
    gaps = {-40: [(290.0, 290.5), (305.0, 307.0), (500.0, 505.0)]}
    chunks = audio.plan_chunks(600.0, gaps_factory(gaps))
    assert len(chunks) == 2
    assert chunks[0][1] == pytest.approx(306.0)  # centre of the 2 s pause, not the 0.5 s one or the far 5 s one
    assert chunks[-1][1] == 600.0 and chunks[1][0] == chunks[0][1]


def test_threshold_loosens_until_pause_found():
    f = gaps_factory({-30: [(298.0, 299.0)]})
    chunks = audio.plan_chunks(600.0, f)
    assert chunks[0][1] == pytest.approx(298.5)
    assert f.calls[:3] == [-40, -35, -30]


def test_hard_cut_when_no_pauses():
    chunks = audio.plan_chunks(1000.0, gaps_factory({}))
    assert chunks[0] == (0.0, 300.0) and chunks[-1][1] == 1000.0
    assert all(b > a for a, b in chunks)


def test_parse_probe():
    err = "Input #0\n  Duration: 00:12:34.50, start: 0\n  Stream #0:0: Video: h264\n  Stream #0:1: Audio: aac\n"
    assert audio.parse_probe(err) == (754.5, True, True)
    assert audio.parse_probe("x.bin: Invalid data found when processing input") == (0.0, False, False)
    assert audio.parse_probe("  Duration: 00:00:10.00\n  Stream #0:0: Video: h264\n") == (10.0, False, True)


def test_run_plan_audio_with_fakes(tmp_path):
    s = MemoryStore()
    s.put_bytes("raw/e1/v1.mp4", b"video-bytes")

    def extract(video, mp3):
        open(mp3, "wb").write(b"mp3")

    def cut(mp3, piece, a, b):
        open(piece, "wb").write(f"{a}-{b}".encode())

    ev = {"evaluation_id": EID, "bucket": "b", "file": {"source_id": "v1", "kind": "video", "raw_key": "raw/e1/v1.mp4", "original_name": "demo.mp4"}}
    out = audio.run_plan_audio(ev, s, probe_fn=lambda p: (600.0, True, True), extract_fn=extract,
                               gaps_fn=lambda mp3, lv: [(298.0, 299.0)] if lv == -40 else [], cut_fn=cut, tmp_root=str(tmp_path))
    assert out["has_audio"] and [c["index"] for c in out["chunks"]] == [1, 2]
    assert s.exists("derived/e1/video/v1/audio.mp3") and s.exists("derived/e1/video/v1/chunks/c2.mp3")
    assert s.get_json("derived/e1/video/v1/plan.json")["chunks"][0]["key"].endswith("chunks/c1.mp3")


def test_run_plan_audio_no_audio_and_invalid(tmp_path):
    s = MemoryStore()
    s.put_bytes("raw/e1/v1.mp4", b"v")
    ev = {"evaluation_id": EID, "bucket": "b", "file": {"source_id": "v1", "kind": "video", "raw_key": "raw/e1/v1.mp4", "original_name": "demo.mp4"}}
    out = audio.run_plan_audio(ev, s, probe_fn=lambda p: (10.0, False, True), tmp_root=str(tmp_path))
    assert out["chunks"] == [] and not s.get_json("derived/e1/video/v1/plan.json")["has_audio"]
    out = audio.run_plan_audio(ev, s, probe_fn=lambda p: (0.0, False, False), tmp_root=str(tmp_path))
    assert out["chunks"] == [] and "readable" in s.get_json("derived/e1/video/v1/plan.json")["error"]


# ----------------------------------------------------------------------------- transcription fallback
def test_voxtral_small_then_mini_fallback():
    fc = FakeConverse({"mistral.voxtral-small-24b-2507": RuntimeError("throttled"), "mistral.voxtral-mini-3b-2507": "hello world"})
    text, model, usage = bo.transcribe(fc, bo.TRANSCRIBE_MODELS, b"mp3")
    assert (text, model) == ("hello world", "mistral.voxtral-mini-3b-2507")
    assert [c["modelId"] for c in fc.calls] == bo.TRANSCRIBE_MODELS
    call = fc.calls[0]["messages"][0]["content"]
    assert call[0]["audio"]["format"] == "mp3" and call[1]["text"] == "Transcribe this audio verbatim. Output only the transcript."
    assert fc.calls[0]["inferenceConfig"] == {"maxTokens": 8000, "temperature": 0}


def test_transcribe_all_models_fail_is_transient():
    fc = FakeConverse({m: RuntimeError("boom") for m in bo.TRANSCRIBE_MODELS})
    with pytest.raises(TransientError):
        bo.transcribe(fc, bo.TRANSCRIBE_MODELS, b"x")


def test_run_transcribe_chunk_writes_outputs():
    s = MemoryStore()
    s.put_bytes("derived/e1/video/v1/chunks/c1.mp3", b"audio")
    fc = FakeConverse({"mistral.voxtral-small-24b-2507": " spoken words "})
    out = bo.run_transcribe_chunk({"evaluation_id": EID, "bucket": "b", "source_id": "v1",
                                   "chunk": {"index": 1, "key": "derived/e1/video/v1/chunks/c1.mp3", "start": 0, "end": 60}}, s, client=fc)
    assert out["chars"] == 12
    assert s.get_text("derived/e1/video/v1/chunks/c1.txt") == "spoken words"
    assert s.exists("derived/e1/progress/chunk/v1/1.done")


# ----------------------------------------------------------------------------- vision
def jpeg(size=(200, 200), noise=False):
    from PIL import Image

    img = Image.effect_noise(size, 80).convert("RGB") if noise else Image.new("RGB", size, "white")
    buf = io.BytesIO()
    img.save(buf, "JPEG", quality=95)
    return buf.getvalue()


def test_vision_fallback_order_kimi_qwen_maverick():
    plan = {bo.VISION_MODELS[0]: RuntimeError("ValidationException"), bo.VISION_MODELS[1]: RuntimeError("throttle"),
            bo.VISION_MODELS[2]: "## Text (OCR)\n(none)"}
    fc = FakeConverse(plan)
    text, model, _ = bo.analyse(fc, bo.VISION_MODELS, jpeg(), "jpeg")
    assert model == "us.meta.llama4-maverick-17b-instruct-v1:0" and [c["modelId"] for c in fc.calls] == bo.VISION_MODELS
    content = fc.calls[0]["messages"][0]["content"]
    assert content[1]["text"].startswith("You are analysing one image taken from a hackathon solution document")
    assert fc.calls[0]["inferenceConfig"] == {"maxTokens": 3000, "temperature": 0}


def test_vision_empty_response_falls_through():
    fc = FakeConverse({bo.VISION_MODELS[0]: "   ", bo.VISION_MODELS[1]: "ok", bo.VISION_MODELS[2]: "x"})
    assert bo.analyse(fc, bo.VISION_MODELS, jpeg(), "jpeg")[1] == bo.VISION_MODELS[1]


def test_prepare_keeps_small_original_and_shrinks_large():
    raw = jpeg((300, 300))
    data, fmt = bo.prepare(raw, "jpg")
    assert data == raw and fmt == "jpeg"
    big = jpeg((1500, 1500), noise=True)
    data, fmt = bo.prepare(big, "jpg", budget=60_000)
    assert len(data) <= 60_000 and fmt == "jpeg"


def test_analyse_image_shrinks_on_size_error():
    sizes = []

    def picky(kw):
        n = len(kw["messages"][0]["content"][0]["image"]["source"]["bytes"])
        sizes.append(n)
        if len(sizes) < 3:
            raise RuntimeError("Input is too long: request body exceeds the length limit")
        return "ok"

    fc = FakeConverse({m: picky for m in bo.VISION_MODELS})
    big = jpeg((1500, 1500), noise=True)
    text, model, _ = bo.analyse_image(fc, bo.VISION_MODELS, big, "jpg")
    assert text == "ok" and sizes[0] > sizes[1] > sizes[2]


def test_run_analyze_image_writes_analysis_and_rejects_foreign_keys():
    s = MemoryStore()
    key = "derived/e1/docs/d1/images/0002_p01_image.jpg"
    s.put_bytes(key, jpeg())
    fc = FakeConverse({bo.VISION_MODELS[0]: "## Summary\nA chart."})
    out = bo.run_analyze_image({"evaluation_id": EID, "bucket": "b", "source_id": "d1", "image_key": key}, s, client=fc)
    assert out["ok"]
    body = s.get_text("derived/e1/docs/d1/images/0002_p01_image.analysis.md")
    assert body.startswith("<!-- source: 0002_p01_image.jpg | model: moonshotai.kimi-k2.5") and "A chart." in body
    assert s.exists("derived/e1/progress/img/d1/0002.done")
    with pytest.raises(PipelineError):
        bo.run_analyze_image({"evaluation_id": EID, "bucket": "b", "source_id": "d1", "image_key": "derived/other/docs/d1/images/1_x.jpg"}, s, client=fc)


def test_undecodable_image_is_soft_failure():
    s = MemoryStore()
    key = "derived/e1/docs/d1/images/0003_image.jpg"
    s.put_bytes(key, b"not an image")
    out = bo.run_analyze_image({"evaluation_id": EID, "bucket": "b", "source_id": "d1", "image_key": key}, s, client=FakeConverse({}))
    assert out["ok"] is False and s.exists("derived/e1/progress/img/d1/0003.failed")
