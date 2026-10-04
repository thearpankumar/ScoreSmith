"""Error types and the mapping from Step Functions error payloads to contract error codes."""
from __future__ import annotations

import json
import re

ERROR_CODES = frozenset({
    "drive_invalid", "drive_inaccessible", "drive_quota", "drive_empty", "file_too_large",
    "unsupported_type", "extract_failed", "no_content", "timeout", "cancelled", "scoring_failed", "internal",
})


class PipelineError(Exception):
    """A terminal, expected failure. The message always starts with the contract error code
    ("drive_quota: ...") so the Catch path can recover it from the Lambda error Cause."""

    def __init__(self, code: str, message: str):
        if code not in ERROR_CODES:
            code = "internal"
        super().__init__(f"{code}: {message}")
        self.code = code
        self.message = message


class TransientError(Exception):
    """Retryable by the state machine (throttling, all Bedrock models temporarily failing)."""


_CODE_RE = re.compile(r"\b(" + "|".join(sorted(ERROR_CODES)) + r"):\s*(.*)", re.S)


def code_from_error(error: dict | None) -> tuple[str, str]:
    """Step Functions Catch payload {"Error": ..., "Cause": "<json string>"} -> (error_code, message)."""
    error = error or {}
    name = str(error.get("Error") or "")
    cause = error.get("Cause") or ""
    message = str(cause)
    try:
        parsed = json.loads(cause) if isinstance(cause, str) else dict(cause)
        message = str(parsed.get("errorMessage") or parsed.get("message") or message)
    except (ValueError, TypeError):
        pass
    m = _CODE_RE.search(message)
    if m:
        return m.group(1), m.group(2).strip()[:500] or m.group(1)
    if name == "States.Timeout" or "Task timed out" in message or "Sandbox.Timedout" in name:
        return "timeout", "A processing step timed out"
    return "internal", (message or name or "Unknown error")[:500]
