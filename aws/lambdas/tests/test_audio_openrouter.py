import base64
import io

import pytest
import requests

from fakes import FakeHttpResponse, FakeOrSession, FakeSsm, MemoryStore, chat_err, chat_ok, stt_ok
from worker import media, prompts, run_config
from worker import openrouter_ops as orp
from worker.errors import PipelineError, TransientError

EID = "e1"
KEY = "sk-or-v1-SECRETKEY123"  # short on purpose: the repo's secrets guard flags long sk-... strings
TM = orp.TRANSCRIBE_MODELS
VM = orp.VISION_MODELS


@pytest.fixture(autouse=True)
def _key(monkeypatch):
    monkeypatch.setenv("OPENROUTER_API_KEY", KEY)


def make_client(plan, **kw):
    sess = FakeOrSession(plan)
    sleeps = []
    kw.setdefault("sleep", sleeps.append)
    kw.setdefault("rand", lambda: 0.5)
    c = orp.OpenRouterClient(session=sess, base_url="https://or.test/api/v1", **kw)
    c.sleeps = sleeps
    return c, sess


def openrouter_run(store):
    run_config.save(store, EID, "openrouter")
    return store


def client_error(code):
    from botocore.exceptions import ClientError

    return ClientError({"Error": {"Code": code, "Message": "x"}}, "GetParameter")


# ----------------------------------------------------------------------------- models
def test_default_models_and_env_override(monkeypatch):
    assert orp.transcribe_models() == ["mistralai/voxtral-small-24b-2507", "mistralai/voxtral-mini-3b-2507"]
    assert orp.vision_models() == ["qwen/qwen3.7-flash", "qwen/qwen3-vl-235b-a22b-instruct"]
    monkeypatch.setenv("OPENROUTER_VISION_MODELS", " a/b , c/d ,")
    monkeypatch.setenv("OPENROUTER_TRANSCRIBE_MODELS", "x/y")
    assert orp.vision_models() == ["a/b", "c/d"] and orp.transcribe_models() == ["x/y"]


def test_stt_only_models_are_recognised():
    assert orp.is_stt_model("mistralai/voxtral-mini-3b-2507")
    assert orp.is_stt_model("openai/gpt-4o-mini-transcribe") and orp.is_stt_model("openai/whisper-1")
    assert not orp.is_stt_model("mistralai/voxtral-small-24b-2507")


# ----------------------------------------------------------------------------- transcription
def test_transcribe_request_shape_and_headers():
    c, s = make_client({TM[0]: chat_ok(" hello world ", 5, 6)})
    text, model, usage = orp.transcribe(c, TM, b"mp3-bytes")
    assert (text, model, usage) == ("hello world", TM[0], {"tokens_in": 5, "tokens_out": 6})
    call = s.calls[0]
    assert call["url"] == "https://or.test/api/v1/chat/completions" and call["timeout"] == orp.TIMEOUT
    assert call["headers"] == {"Authorization": f"Bearer {KEY}", "Content-Type": "application/json",
                               "HTTP-Referer": "https://scoresmith.app", "X-OpenRouter-Title": "ScoreSmith",
                               "X-OpenRouter-App-Visibility": "hidden"}
    body = call["json"]
    assert body["model"] == TM[0] and body["temperature"] == 0 and body["max_tokens"] == 8000
    audio_part, text_part = body["messages"][0]["content"]  # audio first, then the instruction (probed order)
    assert audio_part == {"type": "input_audio",
                          "input_audio": {"data": base64.b64encode(b"mp3-bytes").decode(), "format": "mp3"}}
    assert text_part == {"type": "text", "text": "Transcribe this audio verbatim. Output only the transcript."}


def test_base_url_and_referer_come_from_the_environment(monkeypatch):
    monkeypatch.setenv("OPENROUTER_API_BASE", "https://proxy.test/v1/")
    monkeypatch.setenv("OPENROUTER_HTTP_REFERER", "https://example.test")
    sess = FakeOrSession({"m": "ok"})
    orp.OpenRouterClient(session=sess).chat({"model": "m"})
    assert sess.calls[0]["url"] == "https://proxy.test/v1/chat/completions"
    assert sess.calls[0]["headers"]["HTTP-Referer"] == "https://example.test"


def test_transcribe_falls_back_to_voxtral_mini_on_the_stt_endpoint():
    c, s = make_client({TM[0]: chat_err(400, "audio unsupported"), TM[1]: stt_ok(" hello there ")})
    text, model, usage = orp.transcribe(c, TM, b"mp3-bytes")
    assert (text, model) == ("hello there", "mistralai/voxtral-mini-3b-2507")
    assert usage == {"tokens_in": 0, "tokens_out": 0}
    stt = s.calls[1]
    assert stt["url"] == "https://or.test/api/v1/audio/transcriptions"
    assert stt["json"] == {"model": TM[1], "input_audio": {"data": base64.b64encode(b"mp3-bytes").decode(),
                                                           "format": "mp3"}}


def test_stt_answer_without_text_is_a_model_error():
    c, _ = make_client({TM[1]: FakeHttpResponse(200, {"usage": {"seconds": 3}})})
    with pytest.raises(TransientError, match="all transcription models failed"):
        orp.transcribe(c, [TM[1]], b"x")


def test_transcribe_all_models_fail_is_transient():
    c, _ = make_client({m: chat_err(400, "nope") for m in TM})
    with pytest.raises(TransientError):
        orp.transcribe(c, TM, b"x")


def test_transcribe_falls_back_after_transient_exhaustion():
    c, s = make_client({TM[0]: chat_err(503), TM[1]: stt_ok("rescued")}, max_attempts=2)
    assert orp.transcribe(c, TM, b"x")[:2] == ("rescued", TM[1])
    assert [x["json"]["model"] for x in s.calls] == [TM[0], TM[0], TM[1]]


# ----------------------------------------------------------------------------- client: retries, errors
def test_retry_then_success_with_jittered_backoff():
    c, s = make_client({"m": [chat_err(429, "slow down"), chat_err(503), requests.ConnectionError("reset"), "fine"]})
    r = c.chat({"model": "m"})
    assert orp._text_of(r) == "fine" and len(s.calls) == 4
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


def test_waiting_never_exceeds_the_per_call_budget():
    sleeps = []
    c, s = make_client({"m": chat_err(502)}, sleep=sleeps.append, clock=lambda: sum(sleeps), budget_s=10)
    with pytest.raises(TransientError):
        c.chat({"model": "m"})
    assert sleeps == [1.5, 3.0] and len(s.calls) == 3  # a third wait (6 s) would end past the 10 s budget
    assert orp.RETRY_BUDGET_S == 120.0


def test_retry_after_is_honoured_and_capped():
    c, s = make_client({"m": [chat_err(429), "ok"]})
    c.session.plan["m"][0].headers = {"Retry-After": "7"}
    assert orp._text_of(c.chat({"model": "m"})) == "ok" and c.sleeps == [7.0]
    c, _ = make_client({"m": [chat_err(503), "ok"]})
    c.session.plan["m"][0].headers = {"retry-after": "999"}
    c.chat({"model": "m"})
    assert c.sleeps == [orp.MAX_RETRY_AFTER_S]


def test_in_flight_budget_402_is_retried():
    c, s = make_client({"m": [chat_err(402, "too many requests in flight",
                                       metadata={"limit_source": "openrouter_in_flight_budget"}), "ok"]})
    assert orp._text_of(c.chat({"model": "m"})) == "ok" and len(s.calls) == 2


@pytest.mark.parametrize("status", [401, 402, 403])
def test_auth_billing_are_permanent_and_not_retried(status):
    c, s = make_client({"m": chat_err(status, f"bad key {KEY} Bearer abc.def")})
    with pytest.raises(PipelineError) as ei:
        c.chat({"model": "m"})
    assert len(s.calls) == 1 and ei.value.code == "internal"
    assert "SECRETKEY123" not in str(ei.value) and "abc.def" not in str(ei.value)


def test_moderation_403_only_skips_the_model():
    c, _ = make_client({"m": chat_err(403, "Your input was flagged by moderation")})
    with pytest.raises(orp.ModelError):
        c.chat({"model": "m"})


def test_auth_error_not_swallowed_by_fallback_chain():
    c, s = make_client({TM[0]: chat_err(401), TM[1]: stt_ok("never")})
    with pytest.raises(PipelineError):
        orp.transcribe(c, TM, b"x")
    assert len(s.calls) == 1
    c, s = make_client({VM[0]: chat_err(402, "insufficient credits"), VM[1]: "never"})
    with pytest.raises(PipelineError):
        orp.analyse(c, VM, jpeg(), "jpeg")
    assert len(s.calls) == 1


def test_no_endpoints_404_tries_the_next_model():
    c, s = make_client({VM[0]: chat_err(404, "No endpoints found that can handle the requested parameters."),
                        VM[1]: "## Summary\nok"})
    text, model, _ = orp.analyse(c, VM, jpeg(), "jpeg")
    assert model == VM[1] and len(s.calls) == 2


def test_http_200_with_error_object_is_handled():
    c, s = make_client({"m": [FakeHttpResponse(200, {"error": {"code": 429, "message": "rate limited"}}), "ok"]})
    assert orp._text_of(c.chat({"model": "m"})) == "ok" and len(s.calls) == 2
    c, _ = make_client({"m": FakeHttpResponse(200, {"error": {"code": 401, "message": "no auth"}})})
    with pytest.raises(PipelineError):
        c.chat({"model": "m"})
    c, _ = make_client({"m": FakeHttpResponse(200, {"error": {"message": "provider hiccup"}})}, max_attempts=2)
    with pytest.raises(TransientError):
        c.chat({"model": "m"})
    c, _ = make_client({"m": FakeHttpResponse(200, {"choices": [{"error": {"code": 400, "message": "bad"}}]})})
    with pytest.raises(orp.ModelError):
        c.chat({"model": "m"})
    c, _ = make_client({"m": FakeHttpResponse(200, None, text="<html>not json</html>")})
    with pytest.raises(orp.ModelError):
        c.chat({"model": "m"})


def test_non_json_5xx_body_is_transient():
    c, _ = make_client({"m": FakeHttpResponse(502, None, text="<html>Bad gateway</html>")}, max_attempts=2)
    with pytest.raises(TransientError):
        c.chat({"model": "m"})


def test_error_messages_never_contain_the_key(monkeypatch, capsys):
    c, _ = make_client({"m": chat_err(500, f"echo {KEY}")}, max_attempts=2)
    with pytest.raises(TransientError) as ei:
        c.chat({"model": "m"})
    assert "SECRETKEY123" not in str(ei.value)
    assert "SECRETKEY123" not in orp.scrub(f"x Bearer {KEY} y")
    # a key from SSM that does not look like sk-or-... is redacted too
    monkeypatch.delenv("OPENROUTER_API_KEY")
    monkeypatch.setenv("OPENROUTER_SECRET_PARAM", "/p")
    monkeypatch.setattr(orp, "_ssm_client", lambda: FakeSsm(value="plain-key-0123456789"))
    c, _ = make_client({TM[0]: chat_err(400, "bad plain-key-0123456789"), TM[1]: chat_err(400, "no")})
    with pytest.raises(TransientError) as ei:
        orp.transcribe(c, TM, b"x")
    assert "plain-key-0123456789" not in str(ei.value) + capsys.readouterr().out


def test_413_and_400_too_large_map_to_toolarge():
    for resp in (chat_err(413, "Payload Too Large"), chat_err(400, "request body is too large"),
                 chat_err(400, "Provider returned error", metadata={"raw": "payload size too large"})):
        c, _ = make_client({"m": resp})
        with pytest.raises(orp.TooLarge):
            c.chat({"model": "m"})


# ----------------------------------------------------------------------------- key handling
def test_key_comes_from_ssm_once_per_container(monkeypatch):
    monkeypatch.delenv("OPENROUTER_API_KEY")
    monkeypatch.setenv("OPENROUTER_SECRET_PARAM", "/qs-eval/openrouter-api-key")
    ssm = FakeSsm()
    monkeypatch.setattr(orp, "_ssm_client", lambda: ssm)
    c, s = make_client({"m": "a"})
    c.chat({"model": "m"})
    c.chat({"model": "m"})
    assert ssm.calls == [{"Name": "/qs-eval/openrouter-api-key", "WithDecryption": True}]
    assert s.calls[0]["headers"]["Authorization"] == "Bearer ssm-secret-key"


def test_env_key_skips_ssm(monkeypatch):
    def no_ssm():
        raise AssertionError("SSM must not be called")

    monkeypatch.setattr(orp, "_ssm_client", no_ssm)
    assert orp.get_api_key() == KEY


@pytest.mark.parametrize("code", ["ParameterNotFound", "AccessDeniedException", "InvalidKeyId"])
def test_missing_or_denied_parameter_is_a_clear_pipeline_error(monkeypatch, code):
    monkeypatch.delenv("OPENROUTER_API_KEY")
    monkeypatch.setenv("OPENROUTER_SECRET_PARAM", "/qs-eval/openrouter-api-key")
    monkeypatch.setattr(orp, "_ssm_client", lambda: FakeSsm(exc=client_error(code)))
    with pytest.raises(PipelineError) as ei:
        orp.get_api_key()
    assert ei.value.code == "internal"
    assert f"OpenRouter key not configured in AWS (SSM parameter /qs-eval/openrouter-api-key: {code})" in str(ei.value)


def test_unset_param_empty_value_and_ssm_hiccups(monkeypatch):
    monkeypatch.delenv("OPENROUTER_API_KEY")
    with pytest.raises(PipelineError, match="OPENROUTER_SECRET_PARAM is not set"):
        orp.get_api_key()
    monkeypatch.setenv("OPENROUTER_SECRET_PARAM", "/p")
    monkeypatch.setattr(orp, "_ssm_client", lambda: FakeSsm(exc=client_error("ThrottlingException")))
    with pytest.raises(TransientError):
        orp.get_api_key()
    monkeypatch.setattr(orp, "_ssm_client", lambda: FakeSsm(value="  "))
    with pytest.raises(PipelineError, match="is empty"):
        orp.get_api_key()


def test_401_with_an_ssm_key_rereads_the_key_once_and_retries(monkeypatch):
    monkeypatch.delenv("OPENROUTER_API_KEY")
    monkeypatch.setenv("OPENROUTER_SECRET_PARAM", "/p")
    ssm = FakeSsm()
    monkeypatch.setattr(orp, "_ssm_client", lambda: ssm)
    c, sess = make_client({"m": [chat_err(401, "revoked"), "ok"]})  # key rotated in SSM since the container read it
    assert c.chat({"model": "m"})["choices"]
    assert len(ssm.calls) == 2 and len(sess.calls) == 2
    c2, _ = make_client({"m": chat_err(401, "revoked")})  # still rejected with the fresh key: fail, no loop
    with pytest.raises(PipelineError):
        c2.chat({"model": "m"})
    assert len(ssm.calls) == 3  # cached key, then one re-read


def test_401_with_an_env_key_is_not_retried(monkeypatch):
    monkeypatch.setenv("OPENROUTER_API_KEY", "local-key")
    c, sess = make_client({"m": [chat_err(401, "revoked"), "ok"]})
    with pytest.raises(PipelineError):
        c.chat({"model": "m"})
    assert len(sess.calls) == 1


def test_step_budget_caps_the_read_timeout_and_ends_as_transient(monkeypatch):
    monkeypatch.setenv("OPENROUTER_API_KEY", "local-key")
    t = [0.0]

    def sleep(s):
        t[0] += s

    c, sess = make_client({"m": chat_err(502, "upstream down")}, sleep=sleep, clock=lambda: t[0], budget_s=10_000)
    c.start_step(60)
    with pytest.raises(TransientError):
        c.chat({"model": "m"})
    assert sess.calls[0]["timeout"][1] <= 55  # never waits past the step budget
    assert t[0] <= 60 - orp.MIN_REQUEST_S  # stopped retrying while there was still time to report


def test_step_budget_used_up_fails_fast_without_a_request(monkeypatch):
    monkeypatch.setenv("OPENROUTER_API_KEY", "local-key")
    t = [0.0]
    c, sess = make_client({"m": "ok"}, clock=lambda: t[0])
    c.start_step(100)
    t[0] = 90
    with pytest.raises(TransientError, match="time budget"):
        c.chat({"model": "m"})
    assert sess.calls == []


def test_media_starts_the_step_budget(monkeypatch):
    s = openrouter_run(MemoryStore())
    s.put_bytes("derived/e1/video/v1/chunks/c1.mp3", b"audio")
    c, _ = make_client({TM[0]: chat_ok("words")})
    media.run_transcribe_chunk({"evaluation_id": EID, "bucket": "b", "source_id": "v1",
                                "chunk": {"index": 1, "key": "derived/e1/video/v1/chunks/c1.mp3"}}, s, client=c)
    assert c.deadline is not None


# ----------------------------------------------------------------------------- transcribe step (media + openrouter)
def test_run_transcribe_chunk_writes_outputs():
    s = openrouter_run(MemoryStore())
    s.put_bytes("derived/e1/video/v1/chunks/c1.mp3", b"audio")
    c, _ = make_client({TM[0]: chat_ok(" spoken words ", 7, 9)})
    out = media.run_transcribe_chunk({"evaluation_id": EID, "bucket": "b", "source_id": "v1",
                                      "chunk": {"index": 1, "key": "derived/e1/video/v1/chunks/c1.mp3", "start": 0,
                                                "end": 60}}, s, client=c)
    assert out == {"source_id": "v1", "index": 1, "chars": 12, "model": TM[0]}
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
    c, s = make_client({VM[0]: chat_err(400, "no vision"), VM[1]: "## Text (OCR)\n(none)"})
    text, model, usage = orp.analyse(c, VM, jpeg(), "jpeg")
    assert model == VM[1] and [x["json"]["model"] for x in s.calls] == VM
    assert usage == {"tokens_in": 1, "tokens_out": 2}
    body = s.calls[0]["json"]
    assert body["temperature"] == 0 and body["max_tokens"] == 4000 and body["reasoning"] == {"enabled": False}
    text_part, image_part = body["messages"][0]["content"]
    assert text_part == {"type": "text", "text": prompts.IMAGE_PROMPT}
    assert image_part["type"] == "image_url" and image_part["image_url"]["url"].startswith("data:image/jpeg;base64,")
    assert base64.b64decode(image_part["image_url"]["url"].split(",", 1)[1]) == jpeg()


def test_vision_reasoning_can_be_left_to_the_model(monkeypatch):
    monkeypatch.setenv("OPENROUTER_VISION_REASONING", "on")
    c, s = make_client({VM[0]: "ok"})
    orp.analyse(c, VM, jpeg(), "jpeg")
    assert "reasoning" not in s.calls[0]["json"]


def test_vision_empty_response_falls_through():
    c, _ = make_client({VM[0]: "   ", VM[1]: "ok"})
    assert orp.analyse(c, VM, jpeg(), "jpeg")[1] == VM[1]


def test_vision_all_models_fail_is_transient():
    c, _ = make_client({m: chat_err(400, "nope") for m in VM})
    with pytest.raises(TransientError, match="all vision models failed"):
        orp.analyse(c, VM, jpeg(), "jpeg")


def test_analyse_image_shrinks_on_size_error():
    sizes = []

    def picky(body):
        url = body["messages"][0]["content"][1]["image_url"]["url"]
        sizes.append(len(url))
        if len(sizes) < 3:
            return chat_err(413, "Request Entity Too Large")
        return chat_ok("ok")

    c, _ = make_client({m: picky for m in VM})
    big = jpeg((1500, 1500), noise=True)
    text, model, _ = orp.analyse_image(c, VM, big, "jpg")
    assert text == "ok" and sizes[0] > sizes[1] > sizes[2]


def test_run_analyze_image_writes_analysis_and_rejects_foreign_keys():
    s = openrouter_run(MemoryStore())
    key = "derived/e1/docs/d1/images/0002_p01_image.jpg"
    s.put_bytes(key, jpeg())
    c, _ = make_client({VM[0]: "## Summary\nA chart."})
    ev = {"evaluation_id": EID, "bucket": "b", "source_id": "d1", "image_key": key}
    out = media.run_analyze_image(ev, s, client=c)
    assert out["ok"] and out["model"] == VM[0]
    body = s.get_text("derived/e1/docs/d1/images/0002_p01_image.analysis.md")
    assert body.startswith(f"<!-- source: 0002_p01_image.jpg | model: {VM[0]} | analysed: ") and "A chart." in body
    assert s.exists("derived/e1/progress/img/d1/0002.done")
    with pytest.raises(PipelineError):
        media.run_analyze_image({"evaluation_id": EID, "bucket": "b", "source_id": "d1",
                                 "image_key": "derived/other/docs/d1/images/1_x.jpg"}, s, client=c)


def test_run_analyze_image_puts_the_scorecard_context_first():
    s = openrouter_run(MemoryStore())
    key = "derived/e1/docs/d1/images/0006_image.jpg"
    s.put_bytes(key, jpeg())
    s.put_json("derived/e1/context.json", {"scorecard": "Billing Support QA", "domain": "Customer Support"})
    c, sess = make_client({VM[0]: "## Summary\nA flow."})
    assert media.run_analyze_image({"evaluation_id": EID, "bucket": "b", "source_id": "d1", "image_key": key}, s,
                                   client=c)["ok"]
    sent = sess.calls[0]["json"]["messages"][0]["content"][0]["text"]
    assert sent.startswith('Context: the document is being evaluated with the scorecard "Billing Support QA"')
    assert sent.endswith(prompts.IMAGE_PROMPT)


def test_undecodable_image_is_soft_failure():
    s = openrouter_run(MemoryStore())
    key = "derived/e1/docs/d1/images/0003_image.jpg"
    s.put_bytes(key, b"not an image")
    c, _ = make_client({})
    ev = {"evaluation_id": EID, "bucket": "b", "source_id": "d1", "image_key": key}
    out = media.run_analyze_image(ev, s, client=c)
    assert out["ok"] is False and s.exists("derived/e1/progress/img/d1/0003.failed")


def test_auth_failure_is_not_a_soft_image_failure():
    s = openrouter_run(MemoryStore())
    key = "derived/e1/docs/d1/images/0004_image.jpg"
    s.put_bytes(key, jpeg())
    c, _ = make_client({m: chat_err(402, "insufficient credits") for m in VM})
    with pytest.raises(PipelineError):
        media.run_analyze_image({"evaluation_id": EID, "bucket": "b", "source_id": "d1", "image_key": key}, s, client=c)
    assert not s.exists("derived/e1/progress/img/d1/0004.failed")


def test_transient_failure_propagates_for_step_functions_retry():
    s = openrouter_run(MemoryStore())
    key = "derived/e1/docs/d1/images/0005_image.jpg"
    s.put_bytes(key, jpeg())
    c, _ = make_client({m: chat_err(503) for m in VM}, max_attempts=2)
    with pytest.raises(TransientError):
        media.run_analyze_image({"evaluation_id": EID, "bucket": "b", "source_id": "d1", "image_key": key}, s, client=c)
    assert not s.exists("derived/e1/progress/img/d1/0005.failed")
