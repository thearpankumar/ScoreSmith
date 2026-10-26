"""What the scorecard behind an evaluation is about (name, domain, purpose, scope, and its leaf KPI names).

The backend sends it as `context` in the execution input; ingest saves it to derived/{eid}/context.json so the image
and video-frame steps (which only receive S3 keys) can describe what they see with the scorecard in mind. Payload data
is untrusted: only the known fields are kept, whitespace-collapsed and clipped. A missing or unreadable file means
"no context".
"""
from __future__ import annotations

from .store import derived

FIELDS = {"scorecard": 255, "domain": 120, "purpose": 2000, "scope": 2000}
# `kpis`: the names of the scorecard's leaf KPIs, so the video step looks for the steps the scorecard grades
MAX_KPIS = 60
MAX_KPI_CHARS = 120
MAX_KPIS_TOTAL_CHARS = 3000


def _kpi_names(raw: object) -> list[str]:
    if not isinstance(raw, list):
        return []
    out: list[str] = []
    total = 0
    for value in raw:
        if not isinstance(value, str):
            continue
        name = " ".join(value.split())[:MAX_KPI_CHARS]
        if not name or name in out:
            continue
        total += len(name)
        if len(out) >= MAX_KPIS or total > MAX_KPIS_TOTAL_CHARS:
            break
        out.append(name)
    return out


def clean(raw: object) -> dict:
    if not isinstance(raw, dict):
        return {}
    out: dict = {}
    for name, cap in FIELDS.items():
        value = raw.get(name)
        if isinstance(value, str) and value.strip():
            out[name] = " ".join(value.split())[:cap]
    kpis = _kpi_names(raw.get("kpis"))
    if kpis:
        out["kpis"] = kpis
    return out


def context_key(eid: str) -> str:
    return derived(eid, "context.json")


def save(store, eid: str, raw: object) -> None:
    ctx = clean(raw)
    if ctx:
        store.put_json(context_key(eid), ctx)


def load(store, eid: str) -> dict:
    try:
        return clean(store.get_json(context_key(eid)))
    except Exception:  # not written (older backend / no context) or unreadable: describe without context
        return {}
