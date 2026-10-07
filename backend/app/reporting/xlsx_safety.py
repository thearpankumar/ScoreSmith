"""Text safety helpers for the Excel export: formula/CSV injection, XML-illegal characters, Excel limits."""

from __future__ import annotations

import re
import unicodedata

_ILLEGAL_XML = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f]")
_FORBIDDEN_SHEET_CHARS = re.compile(r"[\[\]:*?/\\]")
_EMAIL = re.compile(r"^[^@\s,;<>()\[\]\\\"]+@[^@\s,;<>()\[\]\\\"]+\.[^@\s,;<>()\[\]\\\"]+$")
_CELL_MAX = 32767
_DANGEROUS_START = ("=", "+", "-", "@", "\t", "\r")


def clean_text(value: object, max_len: int = 32000) -> str:
    """Stringify, drop XML-illegal control characters, and truncate (Excel cells hold 32,767 chars)."""
    if value is None:
        return ""
    s = _ILLEGAL_XML.sub("", str(value))
    max_len = min(max_len, _CELL_MAX)
    if len(s) > max_len:
        s = s[: max_len - 15].rstrip() + "… [truncated]"
    return s


def needs_quote_prefix(s: str) -> bool:
    """True when a string would be parsed as a formula/command if it were typed into a cell."""
    return s.startswith(_DANGEROUS_START)


def safe_sheet_name(base: str, used: set[str]) -> str:
    """A valid, unique (case-insensitive) sheet name: <= 31 chars, no square brackets, colon, *, ?, / or backslash, no apostrophes."""
    name = _FORBIDDEN_SHEET_CHARS.sub(" ", _ILLEGAL_XML.sub("", base or "")).replace("'", "’")
    name = re.sub(r"\s+", " ", name).strip() or "Sheet"
    if name.lower() == "history":  # reserved by Excel
        name = "History_"
    name = name[:31].rstrip() or "Sheet"
    candidate, n = name, 1
    while candidate.lower() in used:
        n += 1
        suffix = f" ({n})"
        candidate = name[: 31 - len(suffix)].rstrip() + suffix
    used.add(candidate.lower())
    return candidate


def is_plausible_email(s: str | None) -> bool:
    if not s or len(s) > 254 or _ILLEGAL_XML.search(s):
        return False
    return bool(_EMAIL.match(s))


def slugify_filename(s: str, max_len: int = 40) -> str:
    """ASCII-safe filename fragment."""
    ascii_s = unicodedata.normalize("NFKD", s or "").encode("ascii", "ignore").decode()
    slug = re.sub(r"[^A-Za-z0-9._-]+", "-", ascii_s).strip("-._")
    return slug[:max_len].strip("-._") or "export"
