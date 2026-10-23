"""What the scorecard behind an evaluation is about (name, domain, purpose, scope).

The backend sends it as `context` in the execution input; ingest saves it to derived/{eid}/context.json so the image
step (which only receives S3 keys) can describe images with the scorecard in mind. Payload data is untrusted: only the
known string fields are kept, whitespace-collapsed and clipped. A missing or unreadable file means "no context".
"""
from __future__ import annotations

from .store import derived

FIELDS = {"scorecard": 255, "domain": 120, "purpose": 2000, "scope": 2000}


def clean(raw: object) -> dict[str, str]:
    if not isinstance(raw, dict):
        return {}
    out: dict[str, str] = {}
    for name, cap in FIELDS.items():
        value = raw.get(name)
        if isinstance(value, str) and value.strip():
            out[name] = " ".join(value.split())[:cap]
    return out


def context_key(eid: str) -> str:
    return derived(eid, "context.json")


def save(store, eid: str, raw: object) -> None:
    ctx = clean(raw)
    if ctx:
        store.put_json(context_key(eid), ctx)


def load(store, eid: str) -> dict[str, str]:
    try:
        return clean(store.get_json(context_key(eid)))
    except Exception:  # not written (older backend / no context) or unreadable: describe without context
        return {}
