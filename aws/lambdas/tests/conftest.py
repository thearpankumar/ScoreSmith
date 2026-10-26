import pytest

from worker import bedrock_ops, openrouter_ops, video_settings


def _no_real_aws(*_args, **_kwargs):
    pytest.fail("a test reached the real Amazon Transcribe client or transcript download; inject a fake")


@pytest.fixture(autouse=True)
def _isolated_provider_state(monkeypatch):
    """No test sees the developer's OpenRouter or video settings or a key cached by an earlier test, and none reaches
    the real Amazon Transcribe (tests that exercise it pass a FakeTranscribe and patch the transcript download)."""
    for name in ("OPENROUTER_API_KEY", "OPENROUTER_SECRET_PARAM", "OPENROUTER_API_BASE", "OPENROUTER_HTTP_REFERER",
                 "OPENROUTER_TRANSCRIBE_MODELS", "OPENROUTER_VISION_MODELS", "OPENROUTER_VISION_REASONING",
                 "OPENROUTER_VIDEO_MODELS", *video_settings.KNOBS):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setattr(bedrock_ops, "transcribe_client", _no_real_aws)
    monkeypatch.setattr(bedrock_ops, "_fetch_transcript", _no_real_aws)
    openrouter_ops.reset_key_cache()
    yield
    openrouter_ops.reset_key_cache()
