"""Provider per run: run_config, ingest's fail-fast key check, media dispatch and the shared image prompt."""
import io
import json

import pytest

from fakes import FakeConverse, FakeHttpResponse, FakeOrSession, FakeSsm, FakeTranscribe, MemoryStore, chat_ok
from worker import bedrock_ops, media, openrouter_ops, prompts, run_config
from worker.errors import PipelineError, TransientError, code_from_error
from worker.ingest import run_ingest

EID = "e1"
PDF = b"%PDF-1.4\n" + b"x" * 100
CHUNK_EVENT = {"evaluation_id": EID, "bucket": "b", "source_id": "v1",
               "chunk": {"index": 1, "key": "derived/e1/video/v1/chunks/c1.mp3", "start": 0, "end": 60}}


def jpeg():
    from PIL import Image

    buf = io.BytesIO()
    Image.new("RGB", (64, 64), "white").save(buf, "JPEG")
    return buf.getvalue()


def upload_event(**extra):
    return {"evaluation_id": EID, "bucket": "b", "limits": {"max_file_bytes": 10_000, "max_files": 20},
            "sources": [{"source_id": "a", "kind": "upload", "s3_key": "uploads/u/g/1.pdf", "original_name": "a.pdf"}],
            **extra}


def store_with_upload():
    s = MemoryStore()
    s.put_bytes("uploads/u/g/1.pdf", PDF)
    return s


# ----------------------------------------------------------------------------- run_config
@pytest.mark.parametrize("raw,expected", [("openrouter", "openrouter"), (" OpenRouter ", "openrouter"),
                                          ("bedrock", "bedrock"), (None, "bedrock"), ("gpt", "bedrock"),
                                          (7, "bedrock")])
def test_save_normalises_and_writes_the_provider(raw, expected):
    s = MemoryStore()
    assert run_config.save(s, EID, raw) == expected
    assert s.get_json("derived/e1/run_config.json") == {"provider": expected}


def test_load_defaults_to_bedrock_without_caching_the_default():
    s = MemoryStore()
    assert run_config.load(s, EID) == "bedrock"  # older backend: no run_config.json
    s.put_json("derived/e1/run_config.json", {"provider": "openrouter"})
    assert run_config.load(s, EID) == "openrouter"
    s.put_bytes("derived/e2/run_config.json", b"{not json")
    assert run_config.load(s, "e2") == "bedrock"
    s.put_json("derived/e3/run_config.json", {"provider": "mystery"})
    assert run_config.load(s, "e3") == "bedrock"


def test_load_sees_a_retry_switching_provider_at_once():
    s = MemoryStore()
    s.put_json("derived/e1/run_config.json", {"provider": "openrouter"})
    assert run_config.load(s, EID) == "openrouter"
    s.put_json("derived/e1/run_config.json", {"provider": "bedrock"})  # a retry of the same evaluation
    assert run_config.load(s, EID) == "bedrock"


def test_load_raises_transient_on_s3_errors_other_than_a_missing_file():
    from botocore.exceptions import ClientError

    class FlakyStore(MemoryStore):
        def __init__(self, code):
            super().__init__()
            self.code = code

        def get_bytes(self, key):
            raise ClientError({"Error": {"Code": self.code, "Message": "x"}}, "GetObject")

    assert run_config.load(FlakyStore("NoSuchKey"), EID) == "bedrock"
    with pytest.raises(TransientError):
        run_config.load(FlakyStore("SlowDown"), EID)


# ----------------------------------------------------------------------------- ingest
def no_ssm():
    raise AssertionError("SSM must not be called for a Bedrock run")


def test_ingest_without_provider_saves_bedrock_and_never_reads_the_key(monkeypatch):
    monkeypatch.setattr(openrouter_ops, "_ssm_client", no_ssm)
    s = store_with_upload()
    run_ingest(upload_event(), s)
    assert s.get_json("derived/e1/run_config.json") == {"provider": "bedrock"}
    run_ingest(upload_event(provider="bedrock"), s)
    assert s.get_json("derived/e1/run_config.json") == {"provider": "bedrock"}


def test_ingest_saves_openrouter_when_the_key_is_readable(monkeypatch):
    monkeypatch.setenv("OPENROUTER_SECRET_PARAM", "/qs-eval/openrouter-api-key")
    ssm = FakeSsm()
    monkeypatch.setattr(openrouter_ops, "_ssm_client", lambda: ssm)
    session = FakeOrSession({})
    monkeypatch.setattr(openrouter_ops, "_client", openrouter_ops.OpenRouterClient(session=session))
    s = store_with_upload()
    out = run_ingest(upload_event(provider="openrouter"), s)
    assert [f["source_id"] for f in out["files"]] == ["a"]
    assert s.get_json("derived/e1/run_config.json") == {"provider": "openrouter"}
    assert ssm.calls == [{"Name": "/qs-eval/openrouter-api-key", "WithDecryption": True}]
    assert [c["url"] for c in session.calls] == ["https://openrouter.ai/api/v1/key"]
    assert session.calls[0]["headers"]["Authorization"] == "Bearer ssm-secret-key"


@pytest.mark.parametrize("response, match", [
    (FakeHttpResponse(401, {"error": {"code": 401, "message": "User not found."}}), "OpenRouter rejected the API key"),
    (FakeHttpResponse(200, {"data": {"limit": 5, "limit_remaining": 0}}), "no credit left"),
])
def test_ingest_fails_fast_when_openrouter_rejects_the_key(monkeypatch, response, match):
    monkeypatch.setenv("OPENROUTER_API_KEY", "local-test-key")
    monkeypatch.setattr(openrouter_ops, "_client",
                        openrouter_ops.OpenRouterClient(session=FakeOrSession({}, key_response=response)))
    s = store_with_upload()
    with pytest.raises(PipelineError, match=match) as ei:
        run_ingest(upload_event(provider="openrouter"), s)
    assert ei.value.code == "internal" and "local-test-key" not in str(ei.value)
    assert not any(k.startswith("raw/") for k in s.objects)


@pytest.mark.parametrize("response", [
    ConnectionError("network down"),
    FakeHttpResponse(503, {"error": {"code": 503, "message": "busy"}}),
    FakeHttpResponse(200, {"data": {"limit": 5, "limit_remaining": 4.2}}),
])
def test_key_check_lets_the_run_continue_unless_the_key_is_bad(monkeypatch, response):
    monkeypatch.setenv("OPENROUTER_API_KEY", "local-test-key")
    openrouter_ops.OpenRouterClient(session=FakeOrSession({}, key_response=response)).check_key()


def test_ingest_fails_fast_when_the_openrouter_key_is_missing(monkeypatch):
    from botocore.exceptions import ClientError

    monkeypatch.setenv("OPENROUTER_SECRET_PARAM", "/qs-eval/openrouter-api-key")
    missing = ClientError({"Error": {"Code": "ParameterNotFound", "Message": "x"}}, "GetParameter")
    monkeypatch.setattr(openrouter_ops, "_ssm_client", lambda: FakeSsm(exc=missing))
    s = store_with_upload()
    with pytest.raises(PipelineError) as ei:
        run_ingest(upload_event(provider="openrouter"), s)
    assert ei.value.code == "internal"
    assert "OpenRouter key not configured in AWS (SSM parameter /qs-eval/openrouter-api-key" in str(ei.value)
    assert not any(k.startswith("raw/") for k in s.objects)  # nothing fetched
    # what HandleFailure makes of it (status.json error_code / message)
    cause = json.dumps({"errorMessage": str(ei.value), "errorType": "PipelineError"})
    code, message = code_from_error({"Error": "PipelineError", "Cause": cause})
    assert code == "internal" and message.startswith("OpenRouter key not configured in AWS")


def test_ingest_fails_fast_when_the_function_has_no_parameter_name():
    with pytest.raises(PipelineError, match="OpenRouter key not configured in AWS"):
        run_ingest(upload_event(provider="openrouter"), store_with_upload())


# ----------------------------------------------------------------------------- dispatch
def test_provider_ops():
    assert media.provider_ops("openrouter") is openrouter_ops
    assert media.provider_ops("bedrock") is bedrock_ops


def test_missing_run_config_dispatches_to_bedrock(monkeypatch):
    fc = FakeConverse({})
    ft = FakeTranscribe()
    monkeypatch.setattr(bedrock_ops, "default_client", lambda: fc)
    monkeypatch.setattr(bedrock_ops, "transcribe_client", lambda: ft)
    monkeypatch.setattr(bedrock_ops, "_fetch_transcript", lambda uri, session=None: "from amazon transcribe")
    monkeypatch.setattr(openrouter_ops, "default_client", lambda: pytest.fail("OpenRouter used for a Bedrock run"))
    s = MemoryStore()
    s.put_bytes("derived/e1/video/v1/chunks/c1.mp3", b"audio")
    out = media.run_transcribe_chunk(CHUNK_EVENT, s)
    assert out["model"] == "amazon.transcribe"
    assert s.get_text("derived/e1/video/v1/chunks/c1.txt") == "from amazon transcribe"
    meta = s.get_json("derived/e1/video/v1/chunks/c1.json")
    assert meta["model"] == "amazon.transcribe" and meta["tokens_in"] == meta["tokens_out"] == 0
    start = ft.calls[0]
    assert start == ("start", {"TranscriptionJobName": "qs-eval-e1-v1-c1",
                               "Media": {"MediaFileUri": "s3://b/derived/e1/video/v1/chunks/c1.mp3"},
                               "MediaFormat": "mp3", "IdentifyLanguage": True})
    assert fc.calls == []  # Voxtral was not needed


def test_openrouter_run_dispatches_to_openrouter(monkeypatch):
    monkeypatch.setenv("OPENROUTER_API_KEY", "sk-or-v1-test")
    sess = FakeOrSession({openrouter_ops.TRANSCRIBE_MODELS[0]: chat_ok("from openrouter", 3, 4),
                          openrouter_ops.VISION_MODELS[0]: "## Summary\nA chart."})
    client = openrouter_ops.OpenRouterClient(session=sess)
    monkeypatch.setattr(openrouter_ops, "default_client", lambda: client)
    monkeypatch.setattr(bedrock_ops, "default_client", lambda: pytest.fail("Bedrock used for an OpenRouter run"))
    s = MemoryStore()
    run_config.save(s, EID, "openrouter")
    s.put_bytes("derived/e1/video/v1/chunks/c1.mp3", b"audio")
    assert media.run_transcribe_chunk(CHUNK_EVENT, s)["model"] == "mistralai/voxtral-small-24b-2507"
    meta = s.get_json("derived/e1/video/v1/chunks/c1.json")
    assert (meta["tokens_in"], meta["tokens_out"]) == (3, 4)
    key = "derived/e1/docs/d1/images/0001_image.jpg"
    s.put_bytes(key, jpeg())
    out = media.run_analyze_image({"evaluation_id": EID, "bucket": "b", "source_id": "d1", "image_key": key}, s)
    assert out["ok"] and out["model"] == "qwen/qwen3.7-flash"


# ----------------------------------------------------------------------------- the image prompt (both providers)
SECTIONS = ["## Text (OCR)", "## Description", "## What it shows", "## Summary"]


def test_image_prompt_has_the_what_it_shows_section_in_order():
    p = prompts.IMAGE_PROMPT
    assert [p.index(h) for h in SECTIONS] == sorted(p.index(h) for h in SECTIONS)
    section = p[p.index("## What it shows"):p.index("## Summary")].lower()
    for words in ("what the image is about", "represents", "process", "comparison", "trend", "argument",
                  "take away", "supports the document", "do not invent", "unclear"):
        assert words in section, words
    assert prompts.IMAGE_MAX_TOKENS == 4000


def test_both_providers_send_the_new_prompt_after_the_context_with_4000_tokens(monkeypatch):
    context = {"scorecard": "Billing Support QA", "purpose": "Measure first-contact resolution."}
    prompt = prompts.image_prompt(context)
    assert prompt.startswith("Context: the document is being evaluated") and "## What it shows" in prompt
    assert prompt.index("Measure first-contact resolution.") < prompt.index("## Text (OCR)")

    fc = FakeConverse({bedrock_ops.VISION_MODELS[0]: "ok"})
    bedrock_ops.analyse(fc, bedrock_ops.VISION_MODELS, jpeg(), "jpeg", prompt)
    assert fc.calls[0]["messages"][0]["content"][1]["text"] == prompt
    assert fc.calls[0]["inferenceConfig"]["maxTokens"] == 4000

    monkeypatch.setenv("OPENROUTER_API_KEY", "sk-or-v1-test")
    sess = FakeOrSession({openrouter_ops.VISION_MODELS[0]: "ok"})
    openrouter_ops.analyse(openrouter_ops.OpenRouterClient(session=sess), openrouter_ops.VISION_MODELS, jpeg(),
                           "jpeg", prompt)
    body = sess.calls[0]["json"]
    assert body["messages"][0]["content"][0]["text"] == prompt and body["max_tokens"] == 4000


def test_local_analyze_images_script_uses_the_same_prompt():
    """scripts/analyze_images.py is the local twin of the Lambda image step."""
    import ast
    from pathlib import Path

    script = Path(__file__).resolve().parents[3] / "scripts" / "analyze_images.py"
    if not script.exists():
        pytest.skip("not a full repository checkout")
    tree = ast.parse(script.read_text(encoding="utf-8"))
    local = next(n.value.value for n in tree.body if isinstance(n, ast.Assign)
                 and any(getattr(t, "id", None) == "PROMPT" for t in n.targets))
    assert local == prompts.IMAGE_PROMPT
