"""Upload validation helpers: allowed extensions, S3 key construction/validation, magic-byte checks."""

from __future__ import annotations

import math
import re
import uuid

SUBMISSION_EXTENSIONS = frozenset({"pdf", "docx", "md", "markdown", "txt", "mp4", "mov", "mkv", "webm", "m4v"})
BATCH_EXTENSIONS = frozenset({"xlsx", "csv"})
MIN_PART_SIZE = 5 * 1024 * 1024
MAX_PARTS = 10_000

_UUID = r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}"
_EXT = r"[a-z0-9]{2,5}"
_UPLOAD_KEY_RE = re.compile(rf"^uploads/(?P<user>{_UUID})/(?P<group>{_UUID})/(?P<file>{_UUID})\.(?P<ext>{_EXT})$")
_BATCH_KEY_RE = re.compile(
    rf"^batches/(?P<user>{_UUID})/(?P<group>{_UUID})/(?P<file>{_UUID})\.(?P<ext>{_EXT})$"
)


def file_extension(name: str) -> str:
    base = name.replace("\\", "/").rsplit("/", 1)[-1]
    return base.rsplit(".", 1)[-1].lower() if "." in base else ""


def allowed_extensions(purpose: str) -> frozenset[str]:
    return BATCH_EXTENSIONS if purpose == "batch_sheet" else SUBMISSION_EXTENSIONS


def build_key(purpose: str, user_id: uuid.UUID, group_id: uuid.UUID, ext: str) -> str:
    if purpose == "batch_sheet":
        return f"batches/{user_id}/{group_id}/{uuid.uuid4()}.{ext}"
    return f"uploads/{user_id}/{group_id}/{uuid.uuid4()}.{ext}"


def key_extension(key: str, user_id: uuid.UUID | None = None, purpose: str | None = None) -> str | None:
    """The extension of a well-formed upload key, or None when the key is not one we could have issued
    (wrong prefix, other user's folder, path tricks). `purpose=None` accepts either layout."""
    if purpose in (None, "submission"):
        m = _UPLOAD_KEY_RE.fullmatch(key)  # fullmatch: `$` alone would accept a trailing newline
        if m and (user_id is None or m.group("user") == str(user_id)) and m.group("ext") in SUBMISSION_EXTENSIONS:
            return m.group("ext")
    if purpose in (None, "batch_sheet"):
        m = _BATCH_KEY_RE.fullmatch(key)
        if m and (user_id is None or m.group("user") == str(user_id)) and m.group("ext") in BATCH_EXTENSIONS:
            return m.group("ext")
    return None


def part_size_for(max_size: int, preferred: int) -> int:
    """One part size for the whole request: at least S3's 5 MiB minimum and large enough that the
    biggest file stays under S3's 10,000-part limit."""
    needed = math.ceil(max(max_size, 1) / (MAX_PARTS - 1))
    return max(preferred, MIN_PART_SIZE, needed)


def part_count(size: int, part_size: int) -> int:
    return max(1, math.ceil(size / part_size))


_MP4_BRANDS = (b"ftyp", b"moov", b"mdat", b"wide", b"free", b"skip")


def magic_ok(ext: str, head: bytes) -> bool:
    """Cheap content sniffing on the first bytes of the stored object."""
    if ext == "pdf":
        return head.startswith(b"%PDF")
    if ext in {"docx", "xlsx"}:
        return head.startswith(b"PK\x03\x04")
    if ext in {"mp4", "mov", "m4v"}:
        return len(head) >= 8 and head[4:8] in _MP4_BRANDS
    if ext in {"mkv", "webm"}:
        return head.startswith(b"\x1a\x45\xdf\xa3")
    if ext in {"csv", "md", "markdown", "txt"}:  # text: no NUL bytes
        return b"\x00" not in head and len(head) > 0
    return False
