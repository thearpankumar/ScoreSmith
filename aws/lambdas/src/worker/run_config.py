"""Per-run settings chosen by the backend: which LLM provider the media steps call.

The backend sends `provider` ("bedrock" | "openrouter") in the execution input; ingest saves it to
derived/{eid}/run_config.json because the transcription and image steps only receive S3 keys. A missing or unknown
value means "bedrock" (older backends never send it), so old payloads behave exactly as before.

load() reads the file on every call (one small S3 GET next to an LLM call): a retried evaluation keeps its id but may
run on the other provider, so nothing may be cached across steps.
"""
from __future__ import annotations

from .errors import TransientError
from .store import derived

PROVIDERS = ("bedrock", "openrouter")
DEFAULT = "bedrock"


def normalise(raw: object) -> str:
    p = raw.strip().lower() if isinstance(raw, str) else ""
    return p if p in PROVIDERS else DEFAULT


def config_key(eid: str) -> str:
    return derived(eid, "run_config.json")


def save(store, eid: str, provider: object) -> str:
    """Write the normalised provider for this run and return it."""
    p = normalise(provider)
    if provider is not None and not (isinstance(provider, str) and provider.strip().lower() in PROVIDERS):
        print(f"    unknown provider {str(provider)[:40]!r}; using {p}")
    store.put_json(config_key(eid), {"provider": p})
    return p


def _missing(e: Exception) -> bool:
    if isinstance(e, KeyError):  # tests' MemoryStore
        return True
    code = ((getattr(e, "response", None) or {}).get("Error") or {}).get("Code", "")
    return code in ("404", "NoSuchKey", "NotFound")


def load(store, eid: str) -> str:
    """The run's provider. No file (an older backend) means Bedrock, as before; any other read failure is transient,
    so the step is retried rather than silently run on the wrong provider."""
    try:
        raw = store.get_json(config_key(eid))
    except ValueError:  # unreadable JSON: not something a retry fixes
        print("    run_config.json is not valid JSON; using bedrock")
        return DEFAULT
    except Exception as e:  # botocore ClientError / BotoCoreError
        if _missing(e):
            return DEFAULT
        raise TransientError(f"could not read run_config.json: {type(e).__name__}") from None
    return normalise(raw.get("provider") if isinstance(raw, dict) else None)
