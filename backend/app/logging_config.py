"""Logging setup shared by the API and the worker.

`LOG_FORMAT=json` prints one JSON object per line (what CloudWatch / Loki / Datadog parse), `text` (the
default) keeps the readable dev format. In both, every record can carry the `evaluation_id` / `session_id`
of the job being processed: `log_context(...)` sets them in context variables, which asyncio tasks inherit,
so every log line from a driver task or a background chat turn - including those from library code it
calls - is attributable without threading ids through every logger call.
"""

from __future__ import annotations

import contextvars
import json
import logging
import os
import platform
import sys
from collections.abc import Iterator
from contextlib import contextmanager
from datetime import UTC, datetime

from app.config import get_settings

_evaluation_id: contextvars.ContextVar[str | None] = contextvars.ContextVar("evaluation_id", default=None)
_session_id: contextvars.ContextVar[str | None] = contextvars.ContextVar("session_id", default=None)

_TEXT_FORMAT = "%(asctime)s %(levelname)s %(name)s: %(message)s"
_STANDARD = set(vars(logging.makeLogRecord({}))) | {"message", "asctime", "evaluation_id", "session_id"}


@contextmanager
def log_context(*, evaluation_id: object = None, session_id: object = None) -> Iterator[None]:
    tokens = []
    if evaluation_id is not None:
        tokens.append((_evaluation_id, _evaluation_id.set(str(evaluation_id))))
    if session_id is not None:
        tokens.append((_session_id, _session_id.set(str(session_id))))
    try:
        yield
    finally:
        for var, token in reversed(tokens):
            var.reset(token)


def bind_log_context(*, evaluation_id: object = None, session_id: object = None) -> None:
    """Sets the ids for the rest of the CURRENT task (no reset needed: a task owns its context copy)."""
    if evaluation_id is not None:
        _evaluation_id.set(str(evaluation_id))
    if session_id is not None:
        _session_id.set(str(session_id))


class ContextFilter(logging.Filter):
    def filter(self, record: logging.LogRecord) -> bool:
        record.evaluation_id = _evaluation_id.get()
        record.session_id = _session_id.get()
        return True


class JsonFormatter(logging.Formatter):
    def __init__(self, role: str) -> None:
        super().__init__()
        self._role = role
        self._host = platform.node()
        self._pid = os.getpid()

    def format(self, record: logging.LogRecord) -> str:
        payload: dict[str, object] = {
            "ts": datetime.fromtimestamp(record.created, UTC).isoformat(timespec="milliseconds"),
            "level": record.levelname,
            "logger": record.name,
            "msg": record.getMessage(),
            "role": self._role,
            "host": self._host,
            "pid": self._pid,
        }
        for key in ("evaluation_id", "session_id"):
            value = getattr(record, key, None)
            if value:
                payload[key] = value
        # Anything passed through `extra={...}` (e.g. the autoscaling metric) is kept as a field.
        for key, value in record.__dict__.items():
            if key not in _STANDARD and key not in payload and not key.startswith("_"):
                payload[key] = value
        if record.exc_info:
            payload["exc"] = self.formatException(record.exc_info)
        return json.dumps(payload, default=str)


def configure_logging(level: int = logging.INFO) -> None:
    """Replaces `logging.basicConfig` in app/main.py and app/worker.py. Idempotent."""
    settings = get_settings()
    root = logging.getLogger()
    root.setLevel(level)
    for handler in list(root.handlers):
        if getattr(handler, "_qs_handler", False):
            root.removeHandler(handler)
    handler = logging.StreamHandler(sys.stderr)
    handler._qs_handler = True  # type: ignore[attr-defined]
    handler.addFilter(ContextFilter())
    if settings.log_format.lower() == "json":
        handler.setFormatter(JsonFormatter(settings.role))
    else:
        handler.setFormatter(logging.Formatter(_TEXT_FORMAT))
    root.addHandler(handler)
