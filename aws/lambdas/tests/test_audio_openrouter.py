import io

import pytest

import base64

import requests

from fakes import FakeHttpResponse, FakeOrSession, MemoryStore, chat_err, chat_ok
from worker import audio, openrouter_ops as bo
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


# ----------------------------------------------------------------------------- OpenRouter client
@pytest.fixture(autouse=True)
def _env(monkeypatch):
    monkeypatch.setenv("OPENROUTER_API_KEY", "sk-or-v1-SECRETKEY123")
    monkeypatch.delenv("OPENROUTER_SECRET_PARAM", raising=False)
    monkeypatch.delenv("OPENROUTER_TRANSCRIBE_MODELS", raising=False)
    monkeypatch.delenv("OPENROUTER_VISION_MODELS", raising=False)
    bo.reset_key_cache()
    yield
    bo.reset_key_cache()


def make_client(plan, **kw):
    sess = FakeOrSession(plan)
    sleeps = []
    kw.setdefault("sleep", sleeps.append)
    kw.setdefault("rand", lambda: 0.5)
    c = bo.OpenRouterClient(session=sess, base_url="https://or.test/api/v1", **kw)
    c.sleeps = sleeps
    return c, sess


TM = bo.DEFAULT_TRANSCRIBE_MODELS
VM = bo.DEFAULT_VISION_MODELS


def test_default_models_and_env_override(monkeypatch):
    assert bo.transcribe_models() == ["mistralai/voxtral-small-24b-2507", "google/gemini-2.5-flash"]
    assert bo.vision_models() == ["openai/gpt-6-luna", "deepseek/deepseek-v4.1-flash", "google/gemini-2.5-flash"]
    monkeypatch.setenv("OPENROUTER_VISION_MODELS", " a/b , c/d ,")
    assert bo.vision_models() == ["a/b", "c/d"]


def test_transcribe_request_shape_and_fallback():
    c, s = make_client({TM[0]: chat_err(400, "audio unsupported"), TM[1]: "hello world"})
    text, model, usage = bo.transcribe(c, TM, b"mp3-bytes")
    assert (text, model) == ("hello world", TM[1])
    assert usage == {"tokens_in": 1, "tokens_out": 2}
    assert [x["json"]["model"] for x in s.calls] == TM
    call = s.calls[0]
    assert call["url"] == "https://or.test/api/v1/chat/completions" and call["timeout"] == (10, 300)
    h = call["headers"]
    assert h["Authorization"] == "Bearer sk-or-v1-SECRETKEY123"
    assert h["HTTP-Referer"] and h["X-OpenRouter-Title"] and h["X-Title"]
    body = call["json"]
    assert body["temperature"] == 0 and body["max_tokens"] == 8000
    parts = body["messages"][0]["content"]
    audio_part = next(p for p in parts if p["type"] == "input_audio")
    assert audio_part["input_audio"] == {"data": base64.b64encode(b"mp3-bytes").decode(), "format": "mp3"}
    assert next(p for p in parts if p["type"] == "text")["text"] == "Transcribe this audio verbatim. Output only the transcript."


def test_transcribe_all_models_fail_is_transient():
    c, _ = make_client({m: chat_err(400, "nope") for m in TM})
    with pytest.raises(TransientError):
        bo.transcribe(c, TM, b"x")


def test_retry_then_success_with_jittered_backoff():
    c, s = make_client({"m": [chat_err(429, "slow down"), chat_err(503), requests.ConnectionError("reset"), "fine"]})
    r = c.chat({"model": "m"})
    assert bo._text_of(r) == "fine" and len(s.calls) == 4
    assert len(c.sleeps) == 3 and c.sleeps[0] < c.sleeps[1] < c.sleeps[2]  # exponential, jittered (rand fixed)


def test_retries_exhausted_raise_transient():
    c, s = make_client({"m": chat_err(500, "down")}, max_attempts=3)
    with pytest.raises(TransientError):
        c.chat({"model": "m"})
    assert len(s.calls) == 3 and len(c.sleeps) == 2


def test_timeout_and_connection_errors_are_transient():
    c, _ = make_client({"m": requests.ReadTimeout("slow")}, max_attempts=2)
    with pytest.raises(TransientError):
        c.chat({"model": "m"})


def test_retry_budget_stops_early():
    t = iter([0, 1000, 2000, 3000])
    c, s = make_client({"m": chat_err(502)}, clock=lambda: next(t), budget_s=10)
    with pytest.raises(TransientError):
        c.chat({"model": "m"})
    assert len(s.calls) == 1


@pytest.mark.parametrize("status", [401, 402, 403])
def test_auth_billing_are_permanent_and_not_retried(status):
    c, s = make_client({"m": chat_err(status, "bad key sk-or-v1-SECRETKEY123 Bearer abc.def")})
    with pytest.raises(PipelineError) as ei:
        c.chat({"model": "m"})
    assert len(s.calls) == 1 and ei.value.code == "internal"
    assert "SECRETKEY123" not in str(ei.value) and "abc.def" not in str(ei.value)


def test_auth_error_not_swallowed_by_fallback_chain():
    c, s = make_client({TM[0]: chat_err(401), TM[1]: "never"})
    with pytest.raises(PipelineError):
        bo.transcribe(c, TM, b"x")
    assert len(s.calls) == 1


def test_http_200_with_error_object_is_handled():
    c, s = make_client({"m": [FakeHttpResponse(200, {"error": {"code": 429, "message": "rate limited"}}), "ok"]})
    assert bo._text_of(c.chat({"model": "m"})) == "ok" and len(s.calls) == 2
    c, _ = make_client({"m": FakeHttpResponse(200, {"error": {"code": 401, "message": "no auth"}})})
    with pytest.raises(PipelineError):
        c.chat({"model": "m"})
    c, _ = make_client({"m": FakeHttpResponse(200, {"error": {"message": "provider hiccup"}})}, max_attempts=2)
    with pytest.raises(TransientError):
        c.chat({"model": "m"})
    c, _ = make_client({"m": FakeHttpResponse(200, {"choices": [{"error": {"code": 400, "message": "bad"}}]})})
    with pytest.raises(bo.ModelError):
        c.chat({"model": "m"})


def test_non_json_5xx_body_is_transient():
    c, _ = make_client({"m": FakeHttpResponse(502, None, text="<html>Bad gateway</html>")}, max_attempts=2)
    with pytest.raises(TransientError):
        c.chat({"model": "m"})


def test_error_messages_never_contain_the_key():
    c, _ = make_client({"m": chat_err(500, "echo sk-or-v1-SECRETKEY123")}, max_attempts=2)
    with pytest.raises(TransientError) as ei:
        c.chat({"model": "m"})
    assert "SECRETKEY123" not in str(ei.value)
    assert "SECRETKEY123" not in bo.scrub("x Bearer sk-or-v1-SECRETKEY123 y")


def test_413_and_400_too_large_map_to_toolarge():
    c, _ = make_client({"m": chat_err(413, "Payload Too Large")})
    with pytest.raises(bo.TooLarge):
        c.chat({"model": "m"})
    c, _ = make_client({"m": chat_err(400, "request body is too large")})
    with pytest.raises(bo.TooLarge):
        c.chat({"model": "m"})


# ----------------------------------------------------------------------------- key handling
class FakeSsm:
    def __init__(self, value="ssm-secret-key", exc=None):
        self.value, self.exc, self.calls = value, exc, []

    def get_parameter(self, **kw):
        self.calls.append(kw)
        if self.exc:
            raise self.exc
        return {"Parameter": {"Value": self.value}}


def test_key_comes_from_ssm_once_per_container(monkeypatch):
    monkeypatch.delenv("OPENROUTER_API_KEY")
    monkeypatch.setenv("OPENROUTER_SECRET_PARAM", "/qs-or/openrouter-api-key")
    ssm = FakeSsm()
    monkeypatch.setattr(bo, "_ssm_client", lambda: ssm)
    c, s = make_client({"m": "a"})
    c.chat({"model": "m"})
    c.chat({"model": "m"})
    assert len(ssm.calls) == 1
    assert ssm.calls[0] == {"Name": "/qs-or/openrouter-api-key", "WithDecryption": True}
    assert s.calls[0]["headers"]["Authorization"] == "Bearer ssm-secret-key"


def test_missing_param_and_ssm_errors(monkeypatch):
    from botocore.exceptions import ClientError

    monkeypatch.delenv("OPENROUTER_API_KEY")
    with pytest.raises(PipelineError):  # no param name configured
        bo.get_api_key()
    monkeypatch.setenv("OPENROUTER_SECRET_PARAM", "/p")
    denied = ClientError({"Error": {"Code": "AccessDeniedException", "Message": "x"}}, "GetParameter")
    monkeypatch.setattr(bo, "_ssm_client", lambda: FakeSsm(exc=denied))
    with pytest.raises(PipelineError):
        bo.get_api_key()
    throttled = ClientError({"Error": {"Code": "ThrottlingException", "Message": "x"}}, "GetParameter")
    monkeypatch.setattr(bo, "_ssm_client", lambda: FakeSsm(exc=throttled))
    with pytest.raises(TransientError):
        bo.get_api_key()
    monkeypatch.setattr(bo, "_ssm_client", lambda: FakeSsm(value="  "))
    with pytest.raises(PipelineError):
        bo.get_api_key()


def test_run_transcribe_chunk_writes_outputs():
    s = MemoryStore()
    s.put_bytes("derived/e1/video/v1/chunks/c1.mp3", b"audio")
    c, _ = make_client({TM[0]: chat_ok(" spoken words ", 7, 9)})
    out = bo.run_transcribe_chunk({"evaluation_id": EID, "bucket": "b", "source_id": "v1",
                                   "chunk": {"index": 1, "key": "derived/e1/video/v1/chunks/c1.mp3", "start": 0, "end": 60}}, s, client=c)
    assert out["chars"] == 12 and out["model"] == TM[0]
    assert s.get_text("derived/e1/video/v1/chunks/c1.txt") == "spoken words"
    meta = s.get_json("derived/e1/video/v1/chunks/c1.json")
    assert (meta["model"], meta["tokens_in"], meta["tokens_out"]) == (TM[0], 7, 9)
    assert s.exists("derived/e1/progress/chunk/v1/1.done")


# ----------------------------------------------------------------------------- vision
def jpeg(size=(200, 200), noise=False):
    from PIL import Image

    img = Image.effect_noise(size, 80).convert("RGB") if noise else Image.new("RGB", size, "white")
    buf = io.BytesIO()
    img.save(buf, "JPEG", quality=95)
    return buf.getvalue()


def test_vision_request_shape_and_fallback_order():
    plan = {VM[0]: chat_err(400, "no vision"), VM[1]: chat_err(404, "no such model"), VM[2]: "## Text (OCR)\n(none)"}
    c, s = make_client(plan)
    text, model, usage = bo.analyse(c, VM, jpeg(), "jpeg")
    assert model == VM[2] and [x["json"]["model"] for x in s.calls] == VM
    body = s.calls[0]["json"]
    assert body["temperature"] == 0 and body["max_tokens"] == 3000
    parts = body["messages"][0]["content"]
    img = next(p for p in parts if p["type"] == "image_url")
    assert img["image_url"]["url"].startswith("data:image/jpeg;base64,")
    assert next(p for p in parts if p["type"] == "text")["text"].startswith("You are analysing one image taken from a hackathon solution document")


def test_vision_empty_response_falls_through():
    c, _ = make_client({VM[0]: "   ", VM[1]: "ok", VM[2]: "x"})
    assert bo.analyse(c, VM, jpeg(), "jpeg")[1] == VM[1]


def test_prepare_keeps_small_original_and_shrinks_large():
    raw = jpeg((300, 300))
    data, fmt = bo.prepare(raw, "jpg")
    assert data == raw and fmt == "jpeg"
    big = jpeg((1500, 1500), noise=True)
    data, fmt = bo.prepare(big, "jpg", budget=60_000)
    assert len(data) <= 60_000 and fmt == "jpeg"


def test_analyse_image_shrinks_on_size_error():
    sizes = []

    def picky(body):
        url = body["messages"][0]["content"][0]["image_url"]["url"]
        sizes.append(len(url))
        if len(sizes) < 3:
            return chat_err(413, "Request Entity Too Large")
        return chat_ok("ok")

    c, _ = make_client({m: picky for m in VM})
    big = jpeg((1500, 1500), noise=True)
    text, model, _ = bo.analyse_image(c, VM, big, "jpg")
    assert text == "ok" and sizes[0] > sizes[1] > sizes[2]


def test_run_analyze_image_writes_analysis_and_rejects_foreign_keys():
    s = MemoryStore()
    key = "derived/e1/docs/d1/images/0002_p01_image.jpg"
    s.put_bytes(key, jpeg())
    c, _ = make_client({VM[0]: "## Summary\nA chart."})
    out = bo.run_analyze_image({"evaluation_id": EID, "bucket": "b", "source_id": "d1", "image_key": key}, s, client=c)
    assert out["ok"] and out["model"] == VM[0]
    body = s.get_text("derived/e1/docs/d1/images/0002_p01_image.analysis.md")
    assert body.startswith(f"<!-- source: 0002_p01_image.jpg | model: {VM[0]}") and "A chart." in body
    assert s.exists("derived/e1/progress/img/d1/0002.done")
    with pytest.raises(PipelineError):
        bo.run_analyze_image({"evaluation_id": EID, "bucket": "b", "source_id": "d1", "image_key": "derived/other/docs/d1/images/1_x.jpg"}, s, client=c)


def test_undecodable_image_is_soft_failure():
    s = MemoryStore()
    key = "derived/e1/docs/d1/images/0003_image.jpg"
    s.put_bytes(key, b"not an image")
    c, _ = make_client({})
    out = bo.run_analyze_image({"evaluation_id": EID, "bucket": "b", "source_id": "d1", "image_key": key}, s, client=c)
    assert out["ok"] is False and s.exists("derived/e1/progress/img/d1/0003.failed")


def test_auth_failure_is_not_a_soft_image_failure():
    s = MemoryStore()
    key = "derived/e1/docs/d1/images/0004_image.jpg"
    s.put_bytes(key, jpeg())
    c, _ = make_client({m: chat_err(402, "insufficient credits") for m in VM})
    with pytest.raises(PipelineError):
        bo.run_analyze_image({"evaluation_id": EID, "bucket": "b", "source_id": "d1", "image_key": key}, s, client=c)
    assert not s.exists("derived/e1/progress/img/d1/0004.failed")


def test_transient_failure_propagates_for_step_functions_retry():
    s = MemoryStore()
    key = "derived/e1/docs/d1/images/0005_image.jpg"
    s.put_bytes(key, jpeg())
    c, _ = make_client({m: chat_err(503) for m in VM}, max_attempts=2)
    with pytest.raises(TransientError):
        bo.run_analyze_image({"evaluation_id": EID, "bucket": "b", "source_id": "d1", "image_key": key}, s, client=c)
