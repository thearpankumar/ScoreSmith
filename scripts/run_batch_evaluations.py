#!/usr/bin/env python3
"""Upload every student's folder from scripts/GdriveDownload/ and trigger AI evaluations through the
backend API, five students at a time.

Per student (folder `<id>/`): every usable file in the folder top level (PDF, DOCX, markdown/text, MP4/MOV/
WebM/MKV video — type is detected from the file's magic bytes, so extensionless downloads work) is uploaded to
S3 via the backend's multipart presigned-URL API, then ONE evaluation is created per student. A folder with
only a PDF, or only a video, is evaluated on just that. `extracted_assets/`, zips/rars, audio and link-only
notes are never uploaded.

Flow: canary (one student, must complete) -> waves of `--batch-size` students (upload -> one /ai/jobs call ->
wait until all of the wave are terminal) -> retry rounds for failed folders (POST /retry for pipeline failures,
re-upload for upload failures). State is persisted after every change, so a re-run resumes where it stopped.

    python scripts/run_batch_evaluations.py --dry-run                      # plan only, zero network calls
    python scripts/run_batch_evaluations.py --check                        # preflight, uploads nothing
    python scripts/run_batch_evaluations.py                                # canary, then everything
    python scripts/run_batch_evaluations.py --resume                       # after Ctrl+C / a crash
    python scripts/run_batch_evaluations.py --list-scorecards

Authentication: the script signs in as the evaluator with --email / EVAL_EMAIL and the password from the
EVAL_PASSWORD environment variable (or a hidden prompt) - never on the command line. It uses the short-lived
bearer token that POST /api/v1/auth/login returns and signs in again when it expires. The scorecard must belong
to that account (set a password for an existing account with `python -m app.scripts.set_password <email>`).

The scorecard is found by --scorecard-name (default: "Connected Vehicle Intelligence Hackathon Solution
Document Evaluation"; --scorecard-id overrides).

Stdlib only. Nothing is read from any .env file; config comes from flags or the environment variables
BACKEND_URL, EVAL_EMAIL, EVAL_PASSWORD and SCORECARD_ID.
"""

from __future__ import annotations

import argparse
import csv
import getpass
import json
import math
import os
import re
import sys
import threading
import time
import urllib.error
import urllib.request
import zipfile
from collections import Counter
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path

API = "/api/v1/evaluations"
SUBMISSION_EXTS = frozenset({"pdf", "docx", "md", "markdown", "txt", "mp4", "mov", "mkv", "webm", "m4v"})
VIDEO_EXTS = frozenset({"mp4", "mov", "mkv", "webm", "m4v"})
DOC_EXTS = frozenset({"pdf", "docx", "md", "markdown"})
CONTENT_TYPES = {
    "pdf": "application/pdf",
    "docx": "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
    "md": "text/markdown", "markdown": "text/markdown", "txt": "text/plain",
    "mp4": "video/mp4", "m4v": "video/x-m4v", "mov": "video/quicktime", "webm": "video/webm",
    "mkv": "video/x-matroska",
}
MAX_FILES = 10  # backend: upload_max_files per submission
MAX_FILE_BYTES = 2 * 1024**3  # backend: upload_max_bytes
DEFAULT_SCORECARD_NAME = "Connected Vehicle Intelligence Hackathon Solution Document Evaluation"
TERMINAL = frozenset({"completed", "failed"})
MP4_BOXES = (b"moov", b"mdat", b"wide", b"free", b"skip")

SLEEP = time.sleep  # tests replace this
STOP = threading.Event()
_LOG_LOCK = threading.Lock()


class Fatal(Exception):
    """Misconfiguration that makes every further request pointless (bad user/scorecard, no storage...)."""


class Interrupted(Exception):
    pass


class WaveTimeout(Exception):
    def __init__(self, folders: list[str]):
        super().__init__(f"timed out waiting for {', '.join(folders)}")
        self.folders = folders


class ApiError(Exception):
    def __init__(self, status: int, detail: str, method: str = "", path: str = ""):
        super().__init__(f"{method} {path} -> HTTP {status}: {detail}" if status else f"{method} {path}: {detail}")
        self.status, self.detail = status, detail


def log(msg: str) -> None:
    line = f"[{datetime.now().strftime('%H:%M:%S')}] {msg}"
    with _LOG_LOCK:
        try:
            print(line, flush=True)
        except UnicodeEncodeError:  # Windows consoles (cp1252) vs. file names with exotic spaces/emoji
            enc = getattr(sys.stdout, "encoding", None) or "ascii"
            print(line.encode(enc, "replace").decode(enc), flush=True)


def now_iso() -> str:
    return datetime.now(UTC).isoformat(timespec="seconds")


def human(n: float) -> str:
    for unit in ("B", "KB", "MB", "GB"):
        if abs(n) < 1024 or unit == "GB":
            return f"{n:.0f} {unit}" if unit == "B" else f"{n:.1f} {unit}"
        n /= 1024
    return f"{n:.1f} GB"


# ------------------------------------------------------------------------------------------------ scanning


@dataclass
class PlanFile:
    path: Path
    name: str  # the name sent to the backend (its extension decides the stored type)
    ext: str
    size: int

    @property
    def kind(self) -> str:
        return "video" if self.ext in VIDEO_EXTS else ("text" if self.ext == "txt" else "document")


@dataclass
class Student:
    folder: str
    email: str | None
    name: str
    files: list[PlanFile] = field(default_factory=list)
    skipped: list[tuple[str, str]] = field(default_factory=list)  # (file name, reason)
    note: str = ""  # why the whole folder is skipped

    @property
    def total_bytes(self) -> int:
        return sum(f.size for f in self.files)

    @property
    def has_document(self) -> bool:
        return any(f.kind == "document" for f in self.files)


def file_extension(name: str) -> str:
    base = name.replace("\\", "/").rsplit("/", 1)[-1]
    return base.rsplit(".", 1)[-1].lower() if "." in base else ""


def magic_ok(ext: str, head: bytes) -> bool:
    """Mirror of backend/app/pipeline/uploads.py::magic_ok, so what we send is accepted at /complete."""
    if ext == "pdf":
        return head.startswith(b"%PDF")
    if ext == "docx":
        return head.startswith(b"PK\x03\x04")
    if ext in {"mp4", "mov", "m4v"}:
        return len(head) >= 8 and head[4:8] in (b"ftyp", *MP4_BOXES)
    if ext in {"mkv", "webm"}:
        return head.startswith(b"\x1a\x45\xdf\xa3")
    if ext in {"md", "markdown", "txt"}:
        return b"\x00" not in head and len(head) > 0
    return False


def sniff(path: Path, name_ext: str) -> tuple[str | None, str]:
    """-> (detected extension | None, reason when None). Decides by content; the name only breaks ties."""
    with path.open("rb") as fh:
        head = fh.read(4096)
    if not head:
        return None, "empty file"
    if head.startswith(b"%PDF"):
        return "pdf", ""
    if head.startswith(b"PK\x03\x04"):
        if name_ext == "zip":
            return None, "zip archive"
        try:
            with zipfile.ZipFile(path) as zf:
                names = zf.namelist()
        except (zipfile.BadZipFile, OSError):
            return None, "unreadable zip/docx"
        if "[Content_Types].xml" in names and any(n.startswith("word/") for n in names):
            return "docx", ""
        return None, "zip archive"
    if head[4:8] == b"ftyp" or head[4:8] in MP4_BOXES:
        brand = head[8:12]
        if brand == b"qt  " or name_ext == "mov":
            return "mov", ""
        return ("m4v" if brand.startswith(b"M4V") else "mp4"), ""
    if head.startswith(b"\x1a\x45\xdf\xa3"):
        return ("webm" if b"webm" in head[:64] else "mkv"), ""
    if head.startswith(b"Rar!"):
        return None, "rar archive"
    if head.startswith(b"ID3") or head[:2] in (b"\xff\xfb", b"\xff\xf3", b"\xff\xf2"):
        return None, "audio file (unsupported)"
    if head.startswith(b"\xd0\xcf\x11\xe0"):
        return None, "legacy .doc (unsupported)"
    if name_ext in {"md", "markdown", "txt"} and magic_ok("txt", head):
        return name_ext, ""
    return None, "unrecognised content"


def parse_label(label: str | None, folder: str) -> tuple[str | None, str]:
    """'<email> - <Name>' -> (email, name); tolerant of missing/odd labels."""
    label = (label or "").strip()
    m = re.match(r"^(\S+@\S+)\s+[-–—]\s+(.+)$", label)
    if m:
        return m.group(1).lower(), m.group(2).strip()
    if re.fullmatch(r"\S+@\S+", label):
        return label.lower(), folder
    return None, label or folder


_NS = "{http://schemas.openxmlformats.org/spreadsheetml/2006/main}"


def _col_index(ref: str) -> int:
    n = 0
    for ch in re.match(r"[A-Z]+", ref).group(0):  # type: ignore[union-attr]
        n = n * 26 + ord(ch) - 64
    return n - 1


def read_sheet_identities(xlsx: Path) -> dict[str, tuple[str, str]]:
    """folder id (email local part) -> (email, name) from the submissions spreadsheet, latest row wins.
    Stdlib-only .xlsx reader, mirroring scripts/gdrive_download.py::read_submissions."""
    import xml.etree.ElementTree as ET

    with zipfile.ZipFile(xlsx) as zf:
        shared: list[str] = []
        if "xl/sharedStrings.xml" in zf.namelist():
            for si in ET.fromstring(zf.read("xl/sharedStrings.xml")).iter(f"{_NS}si"):
                shared.append("".join(t.text or "" for t in si.iter(f"{_NS}t")))
        sheet = "xl/worksheets/sheet1.xml"
        if sheet not in zf.namelist():
            sheet = sorted(n for n in zf.namelist() if n.startswith("xl/worksheets/sheet"))[0]
        rows: list[dict[int, str]] = []
        for row in ET.fromstring(zf.read(sheet)).iter(f"{_NS}row"):
            cells: dict[int, str] = {}
            for c in row.iter(f"{_NS}c"):
                v = c.find(f"{_NS}v")
                if c.get("t") == "inlineStr":
                    val = "".join(t.text or "" for t in c.iter(f"{_NS}t"))
                elif v is None or v.text is None:
                    continue
                else:
                    val = shared[int(v.text)] if c.get("t") == "s" else v.text
                cells[_col_index(c.get("r", "A1"))] = val
            rows.append(cells)
    if not rows:
        return {}
    header = {i: str(v).strip().lower() for i, v in rows[0].items()}
    c_email = next((i for i, h in header.items() if "email" in h), None)
    c_name = next((i for i, h in header.items() if h == "name"), None)
    c_ts = next((i for i, h in header.items() if "timestamp" in h), None)
    if c_email is None:
        return {}
    latest: dict[str, tuple[float, str, str]] = {}
    for n, cells in enumerate(rows[1:], start=2):
        email = (cells.get(c_email) or "").strip()
        if "@" not in email:
            continue
        try:
            ts = float(cells[c_ts]) if c_ts is not None and c_ts in cells else float(n)
        except ValueError:
            ts = float(n)
        sid = email.lower().split("@")[0]
        if sid not in latest or ts >= latest[sid][0]:
            latest[sid] = (ts, email.lower(), (cells.get(c_name) or "").strip() if c_name is not None else "")
    return {sid: (e, nm) for sid, (_, e, nm) in latest.items()}


def scan_student(root: Path, folder: str, identity: tuple[str | None, str]) -> Student:
    email, name = identity
    st = Student(folder=folder, email=email, name=name)
    cands: list[PlanFile] = []
    for p in sorted((root / folder).iterdir(), key=lambda x: x.name.lower()):
        if not p.is_file() or p.name.startswith("."):
            continue  # extracted_assets/ and other subdirectories, .manifest.json
        size = p.stat().st_size
        name_ext = file_extension(p.name)
        if size == 0:
            st.skipped.append((p.name, "empty file"))
            continue
        if size > MAX_FILE_BYTES:
            st.skipped.append((p.name, "larger than the 2 GiB upload limit"))
            continue
        ext, reason = sniff(p, name_ext)
        if ext is None:
            st.skipped.append((p.name, reason))
            continue
        if name_ext in SUBMISSION_EXTS:
            with p.open("rb") as fh:
                if magic_ok(name_ext, fh.read(64)):
                    ext = name_ext  # keep the student's own extension (.markdown, .m4v, ...)
        send = p.name if file_extension(p.name) == ext else f"{p.name}.{ext}"
        if len(send) > 255:
            send = send[:255 - len(ext) - 1] + "." + ext
        cands.append(PlanFile(p, send, ext, size))
    notes = [f for f in cands if f.ext == "txt"]  # usually link-only notes (github_link.txt)
    if notes and len(notes) < len(cands):  # a .txt is used only when the folder has nothing else
        for f in notes:
            st.skipped.append((f.path.name, ".txt note (used only if the folder has no other usable file)"))
        cands = [f for f in cands if f not in notes]
    if len(cands) > MAX_FILES:  # documents first, then videos smallest-first
        order = sorted(cands, key=lambda f: (f.kind == "video", f.size if f.kind == "video" else 0, f.name))
        for f in order[MAX_FILES:]:
            st.skipped.append((f.path.name, f"over the {MAX_FILES}-files-per-submission limit"))
        cands = order[:MAX_FILES]
    st.files = cands
    if not st.files:
        st.note = "no usable files"
    return st


def scan_all(root: Path, sheet: Path | None = None) -> tuple[list[Student], int]:
    sheet = sheet or next(iter(sorted((Path(__file__).resolve().parent / "SampleFiles").glob("*.xlsx"))), None)
    sheet_ids: dict[str, tuple[str, str]] = {}
    if sheet and sheet.exists():
        try:
            sheet_ids = read_sheet_identities(sheet)
            log(f"Student names/emails from {sheet.name}: {len(sheet_ids)} rows")
        except Exception as e:  # noqa: BLE001
            log(f"warning: could not read {sheet}: {e}")
    labels: dict[str, str] = {}
    sp = root / ".summary_state.json"
    if sp.exists():
        try:
            raw = json.loads(sp.read_text(encoding="utf-8"))
            labels = {k: (v or {}).get("label", "") for k, v in raw.items() if isinstance(v, dict)}
        except (OSError, ValueError):
            log("warning: could not read .summary_state.json; falling back to folder ids as names")
    folders = sorted(p.name for p in root.iterdir() if p.is_dir() and not p.name.startswith("."))
    not_downloaded = len([k for k in labels if k not in folders])
    def identity(f: str) -> tuple[str | None, str]:
        if f in sheet_ids:
            email, name = sheet_ids[f]
            return email, name or f
        return parse_label(labels.get(f), f)

    return [scan_student(root, f, identity(f)) for f in folders], not_downloaded


# ------------------------------------------------------------------------------------------------ state


class State:
    def __init__(self, path: Path, scorecard_id: str):
        self.path, self.scorecard_id = path, scorecard_id
        self.lock = threading.Lock()
        self.folders: dict[str, dict] = {}

    @classmethod
    def load(cls, path: Path, scorecard_id: str) -> State:
        st = cls(path, scorecard_id)
        if path.exists():
            data = json.loads(path.read_text(encoding="utf-8"))
            if data.get("scorecard_id") not in (None, scorecard_id) and any(
                r.get("status") != "pending" for r in data.get("folders", {}).values()
            ):
                raise Fatal(
                    f"{path} belongs to scorecard {data.get('scorecard_id')}, not {scorecard_id}. "
                    "Use another --state file or delete this one."
                )
            st.folders = data.get("folders", {})
        return st

    def rec(self, folder: str) -> dict:
        with self.lock:
            if folder not in self.folders:
                self.folders[folder] = {
                    "status": "pending", "evaluation_id": None, "batch_id": None, "attempts": 0, "errors": [],
                    "uploaded": [], "score": None, "rag_band": None, "error_code": None, "error_message": None,
                }
            return self.folders[folder]

    def update(self, folder: str, **kw) -> None:
        self.rec(folder)
        with self.lock:
            self.folders[folder].update(kw, updated_at=now_iso())
            self._save()

    def add_error(self, folder: str, where: str, message: str) -> None:
        self.rec(folder)
        with self.lock:
            self.folders[folder]["errors"].append({"at": now_iso(), "where": where, "message": message[:500]})
            self.folders[folder]["errors"] = self.folders[folder]["errors"][-20:]
            self._save()

    def status(self, folder: str) -> str:
        return self.rec(folder)["status"]

    def _save(self) -> None:
        tmp = self.path.with_suffix(self.path.suffix + ".tmp")
        tmp.write_text(
            json.dumps({"scorecard_id": self.scorecard_id, "folders": self.folders}, indent=2), encoding="utf-8"
        )
        os.replace(tmp, self.path)


# ------------------------------------------------------------------------------------------------ API


def _detail(raw: str) -> str:
    try:
        d = json.loads(raw)
    except ValueError:
        return raw.strip()[:300] or "(empty body)"
    detail = d.get("detail", d) if isinstance(d, dict) else d
    return detail if isinstance(detail, str) else json.dumps(detail)[:800]


class Api:
    """Signs in with e-mail + password (POST /auth/login) and sends the short-lived bearer token; a 401 triggers one
    re-login. Clones made by `clone()` share the credentials and token."""

    def __init__(self, base: str, email: str, password: str, timeout: float = 60.0, attempts: int = 4,
                 ignore_stop: bool = False, _shared: dict | None = None):
        self.base, self.timeout, self.attempts = base.rstrip("/"), timeout, attempts
        self.ignore_stop = ignore_stop  # abort calls must still go out after Ctrl+C
        self._auth = _shared if _shared is not None else {"email": email, "password": password, "token": None}
        self.user_id: str | None = None

    def clone(self, **kw) -> "Api":
        return Api(self.base, "", "", _shared=self._auth, **kw)

    def _login(self) -> None:
        body = json.dumps({"email": self._auth["email"], "password": self._auth["password"]}).encode()
        req = urllib.request.Request(
            self.base + "/api/v1/auth/login", data=body, method="POST",
            headers={"Content-Type": "application/json", "Accept": "application/json"},
        )
        try:
            with urllib.request.urlopen(req, timeout=self.timeout) as r:
                self._auth["token"] = json.loads(r.read())["access_token"]
        except urllib.error.HTTPError as e:
            raise Fatal(f"Sign-in failed ({e.code}): {_detail(e.read().decode('utf-8', 'replace'))}") from e
        except (urllib.error.URLError, OSError) as e:
            raise Fatal(f"cannot reach {self.base} ({e})") from e

    def call(self, method: str, path: str, body: object | None = None):
        data = json.dumps(body).encode() if body is not None else None
        for i in range(self.attempts):
            if STOP.is_set() and not self.ignore_stop:
                raise Interrupted()
            if path != "/health" and not self._auth["token"]:
                self._login()
            headers = {"Accept": "application/json"}
            if self._auth["token"]:
                headers["Authorization"] = f"Bearer {self._auth['token']}"
            if data is not None:
                headers["Content-Type"] = "application/json"
            try:
                req = urllib.request.Request(self.base + path, data=data, method=method, headers=headers)
                with urllib.request.urlopen(req, timeout=self.timeout) as r:
                    raw = r.read()
                    return json.loads(raw) if raw else None
            except urllib.error.HTTPError as e:
                detail = _detail(e.read().decode("utf-8", "replace"))
                if e.code == 401 and self._auth["token"] and i < self.attempts - 1:
                    self._auth["token"] = None  # expired: sign in again and retry
                    continue
                if e.code == 503 and "not configured" in detail.lower():
                    raise Fatal(
                        f"{detail}\nThe backend has no S3/Step Functions configured (OR_S3_BUCKET, "
                        "OR_SFN_STATE_MACHINE_ARN, OR_AWS_APP_*): run the AWS bootstrap, put the values in "
                        "infra/.env and restart the backend."
                    ) from e
                if e.code in (429, 500, 502, 503, 504) and i < self.attempts - 1:
                    retry_after = e.headers.get("Retry-After") if e.headers else None
                    SLEEP(min(30, int(retry_after)) if retry_after and retry_after.isdigit() else min(30, 2**i))
                    continue
                raise ApiError(e.code, detail, method, path) from e
            except (urllib.error.URLError, OSError) as e:
                if i < self.attempts - 1:
                    SLEEP(min(30, 2**i))
                    continue
                raise ApiError(0, f"cannot reach {self.base} ({e})", method, path) from e
        raise AssertionError("unreachable")

    def get(self, path: str):
        return self.call("GET", path)

    def post(self, path: str, body: object | None = None):
        return self.call("POST", path, body if body is not None else {})


# ------------------------------------------------------------------------------------------------ uploading


class UrlExpired(Exception):
    pass


class UploadFailed(Exception):
    pass


class Registry:
    """Multipart uploads currently in flight, so Ctrl+C / failures can abort them instead of orphaning parts."""

    def __init__(self):
        self.lock = threading.Lock()
        self.items: set[tuple[str, str]] = set()

    def add(self, key: str, upload_id: str) -> None:
        with self.lock:
            self.items.add((key, upload_id))

    def discard(self, key: str, upload_id: str) -> None:
        with self.lock:
            self.items.discard((key, upload_id))

    def abort_all(self, api: Api) -> int:
        with self.lock:
            pending, self.items = list(self.items), set()
        for key, upload_id in pending:
            abort_upload(api, key, upload_id)
        return len(pending)


def abort_upload(api: Api, key: str, upload_id: str) -> None:
    try:
        api.clone(attempts=2, ignore_stop=True).post(
            f"{API}/ai/uploads/abort", {"files": [{"upload_id": upload_id, "s3_key": key}]}
        )
    except Exception as e:  # noqa: BLE001 - best effort
        log(f"  could not abort multipart upload {key}: {e}")


class Progress:
    """Throttled per-file upload progress line."""

    def __init__(self, label: str, total: int):
        self.label, self.total, self.done = label, total, 0
        self.t0, self.last = time.time(), 0.0
        self.lock = threading.Lock()

    def add(self, n: int) -> None:
        with self.lock:
            self.done += n
            t = time.time()
            if t - self.last >= 5 or self.done >= self.total:
                self.last = t
                rate = self.done / max(t - self.t0, 1e-6)
                log(
                    f"  {self.label}: {human(self.done)}/{human(self.total)} "
                    f"({100 * self.done / max(self.total, 1):.0f}%) {human(rate)}/s"
                )

    def rollback(self, n: int) -> None:
        with self.lock:
            self.done = max(0, self.done - n)


class SliceReader:
    """File-like view over [offset, offset+length) of a file; streams in 1 MiB chunks, honours Ctrl+C."""

    def __init__(self, path: Path, offset: int, length: int, progress: Progress):
        self.fh = path.open("rb")
        self.fh.seek(offset)
        self.left, self.length, self.progress, self.sent = length, length, progress, 0

    def read(self, n: int = -1) -> bytes:
        if STOP.is_set():
            raise Interrupted()
        n = self.left if n is None or n < 0 else min(n, self.left)
        data = self.fh.read(min(n, 1 << 20))
        self.left -= len(data)
        self.sent += len(data)
        if data:
            self.progress.add(len(data))
        return data

    def close(self) -> None:
        self.fh.close()


def put_part(url: str, path: Path, offset: int, length: int, progress: Progress, attempts: int = 4) -> str:
    last: Exception | None = None
    for i in range(attempts):
        reader = SliceReader(path, offset, length, progress)
        try:
            req = urllib.request.Request(
                url, data=reader, method="PUT", headers={"Content-Length": str(length)}  # type: ignore[arg-type]
            )
            with urllib.request.urlopen(req, timeout=600) as r:
                etag = r.headers.get("ETag")
            if not etag:
                raise UploadFailed("storage returned no ETag for a part")
            return etag
        except urllib.error.HTTPError as e:
            progress.rollback(reader.sent)
            if e.code == 403:
                raise UrlExpired(f"HTTP 403 on part upload (presigned URL expired?): {e.read()[:200]!r}") from e
            last = e
        except (urllib.error.URLError, OSError, UploadFailed) as e:
            progress.rollback(reader.sent)
            last = e
        finally:
            reader.close()
        if i < attempts - 1:
            SLEEP(min(30, 2**i))
    raise UploadFailed(f"part upload failed after {attempts} attempts: {last}")


def upload_file(api: Api, pf: PlanFile, folder: str, registry: Registry, part_workers: int) -> dict:
    """Init (fresh presigned URLs for THIS file; they only live 15 min), upload parts, complete."""
    for attempt in range(2):
        init = api.post(
            f"{API}/ai/uploads",
            {"purpose": "submission",
             "files": [{"name": pf.name, "size": pf.size, "content_type": CONTENT_TYPES[pf.ext]}]},
        )
        meta, part_size = init["files"][0], init["part_size"]
        key, upload_id = meta["s3_key"], meta["upload_id"]
        registry.add(key, upload_id)
        progress = Progress(f"{folder}/{pf.name}", pf.size)
        try:
            urls = {p["part_number"]: p["url"] for p in meta["parts"]}
            with ThreadPoolExecutor(max_workers=max(1, part_workers)) as pool:
                futs = {}
                for n in sorted(urls):
                    offset = (n - 1) * part_size
                    length = min(part_size, pf.size - offset)
                    futs[n] = pool.submit(put_part, urls[n], pf.path, offset, length, progress)
                etags = {n: f.result() for n, f in futs.items()}
            done = api.post(
                f"{API}/ai/uploads/complete",
                {"files": [{"upload_id": upload_id, "s3_key": key,
                            "parts": [{"part_number": n, "etag": etags[n]} for n in sorted(etags)]}]},
            )
            res = done["files"][0]
            if not res.get("ok"):
                raise UploadFailed(res.get("error") or "storage rejected the file")
            registry.discard(key, upload_id)
            return {"s3_key": key, "original_name": pf.name, "size": res.get("size") or pf.size}
        except UrlExpired as e:
            registry.discard(key, upload_id)
            abort_upload(api, key, upload_id)
            if attempt == 1:
                raise UploadFailed(str(e)) from e
            log(f"  {folder}/{pf.name}: upload URLs expired, requesting fresh ones")
        except BaseException:
            registry.discard(key, upload_id)
            abort_upload(api, key, upload_id)
            raise
    raise AssertionError("unreachable")


def upload_student(ctx: Ctx, st: Student) -> list[dict]:
    rec = ctx.state.rec(st.folder)
    if rec["status"] == "uploaded" and rec["uploaded"]:
        log(f"{st.folder}: reusing {len(rec['uploaded'])} previously uploaded file(s)")
        return rec["uploaded"]
    out = []
    for pf in st.files:
        log(f"{st.folder}: uploading {pf.name} ({human(pf.size)}, {pf.kind})")
        out.append(upload_file(ctx.api, pf, st.folder, ctx.registry, ctx.args.part_workers))
    ctx.state.update(st.folder, status="uploaded", uploaded=out)
    return out


# ------------------------------------------------------------------------------------------------ waves


@dataclass
class Ctx:
    api: Api
    state: State
    args: argparse.Namespace
    students: dict[str, Student]
    registry: Registry = field(default_factory=Registry)


def explain(e: Exception) -> str:
    if isinstance(e, ApiError):
        return e.detail if e.status else str(e)
    return f"{type(e).__name__}: {e}"


def check_fatal(e: ApiError) -> None:
    if e.status in (401, 403):
        raise Fatal(f"The backend rejected the request ({e.detail}). Check --email / EVAL_PASSWORD.") from e


def fetch_diagnostics(ctx: Ctx, folder: str) -> None:
    eid = ctx.state.rec(folder).get("evaluation_id")
    if not eid:
        return
    try:
        p = ctx.api.get(f"{API}/{eid}/progress")
    except Exception as e:  # noqa: BLE001
        log(f"  (could not fetch diagnostics: {explain(e)})")
        return
    log(f"  diagnostics for {folder}: status={p['status']} stage={p.get('stage')} "
        f"error_code={p.get('error_code')} error={p.get('error_message')}")
    for s in p.get("sources", []):
        log(f"    source {s.get('original_name')}: {s.get('status')} {s.get('warnings') or ''}")
    for ev in p.get("events", [])[-12:]:
        log(f"    {ev.get('created_at', '')[:19]} {ev.get('event_type')}: {ev.get('message')}")


def create_jobs(ctx: Ctx, folders: list[str]) -> list[str]:
    """One /ai/jobs call for the folders that uploaded fine. Returns the folders that became evaluations."""
    st_map = ctx.students
    body_items = []
    for f in folders:
        s = st_map[f]
        body_items.append({
            "subject_email": s.email, "subject_name": s.name,
            "sources": [
                {"kind": "upload", "s3_key": u["s3_key"], "original_name": u["original_name"], "size": u["size"]}
                for u in ctx.state.rec(f)["uploaded"]
            ],
        })
    payload = {"scorecard_id": ctx.args.scorecard_id, "items": body_items}
    if ctx.args.direction_prompt:
        payload["direction_prompt"] = ctx.args.direction_prompt
    try:
        resp = ctx.api.post(f"{API}/ai/jobs", payload)
    except ApiError as e:
        check_fatal(e)
        if e.status == 404 and "scorecard" in e.detail.lower():
            raise Fatal(f"Scorecard {ctx.args.scorecard_id} not found (try --list-scorecards).") from e
        bad = set()
        if e.status == 422:
            try:
                bad = {int(x["item"]) for x in json.loads(e.detail) if isinstance(x, dict) and "item" in x}
            except (ValueError, TypeError, KeyError):
                bad = set()
        for i, f in enumerate(folders):
            if not bad or i in bad:
                ctx.state.update(f, status="failed", uploaded=[], error_code="job_rejected",
                                 error_message=e.detail[:500])
                ctx.state.add_error(f, "create_job", e.detail)
        rest = [f for i, f in enumerate(folders) if bad and i not in bad]
        return create_jobs(ctx, rest) if rest else []
    evs = resp.get("evaluations", [])
    if len(evs) != len(folders):
        for f in folders:
            ctx.state.update(f, status="failed", error_code="job_mismatch",
                             error_message=f"expected {len(folders)} evaluations, got {len(evs)}")
        return []
    for f, ev in zip(folders, evs, strict=True):
        ctx.state.update(f, status="evaluating", evaluation_id=ev["id"], batch_id=resp.get("batch_id"),
                         attempts=ctx.state.rec(f)["attempts"] + 1, error_code=None, error_message=None)
        log(f"{f}: evaluation {ev['id']} created ({ev.get('status')})")
    return folders


def retry_evaluation(ctx: Ctx, folder: str) -> bool:
    rec = ctx.state.rec(folder)
    try:
        ctx.api.post(f"{API}/{rec['evaluation_id']}/retry")
    except ApiError as e:
        check_fatal(e)
        if e.status == 409:
            log(f"{folder}: server says it is not retryable ({e.detail}); re-attaching to its current state")
            ctx.state.update(folder, status="evaluating")
            return True
        ctx.state.add_error(folder, "retry", explain(e))
        return False
    ctx.state.update(folder, status="evaluating", attempts=rec["attempts"] + 1, error_code=None, error_message=None)
    log(f"{folder}: re-queued evaluation {rec['evaluation_id']} (attempt {rec['attempts']})")
    return True


def wait_terminal(ctx: Ctx, folders: list[str]) -> None:
    pending = {f: ctx.state.rec(f)["evaluation_id"] for f in folders}
    deadline = time.time() + ctx.args.wave_timeout * 60
    last_status: dict[str, str] = {}
    last_beat = time.time()
    poll_errors = 0
    while pending:
        for f, eid in list(pending.items()):
            try:
                p = ctx.api.get(f"{API}/{eid}/progress")
                poll_errors = 0
            except ApiError as e:
                check_fatal(e)
                poll_errors += 1
                if poll_errors > 20:
                    raise Fatal(f"lost contact with the backend while polling: {explain(e)}") from e
                continue
            line = f"{p['status']}/{p.get('stage') or '-'}" + (
                f" (queue #{p['queue_position']})" if p.get("queue_position") else "")
            if last_status.get(f) != line:
                last_status[f] = line
                log(f"{f}: {line}")
            if p["status"] == "completed":
                score = band = None
                try:
                    ev = ctx.api.get(f"{API}/{eid}")
                    score, band = ev.get("final_weighted_score"), ev.get("rag_band")
                except ApiError:
                    pass
                ctx.state.update(f, status="completed", score=score, rag_band=band, error_code=None, error_message=None)
                log(f"{f}: COMPLETED score={score} band={band}")
                del pending[f]
            elif p["status"] == "failed":
                code, msg = p.get("error_code"), p.get("error_message")
                ctx.state.update(f, status="failed", error_code=code, error_message=msg)
                ctx.state.add_error(f, "pipeline", f"{code}: {msg}")
                log(f"{f}: FAILED {code}: {msg}")
                del pending[f]
        if not pending:
            return
        if time.time() > deadline:
            raise WaveTimeout(list(pending))
        if time.time() - last_beat >= 60:
            last_beat = time.time()
            log("  waiting: " + ", ".join(f"{f}={last_status.get(f, '?')}" for f in pending))
        SLEEP(ctx.args.poll_interval)


def process_wave(ctx: Ctx, folders: list[str], label: str) -> None:
    log(f"=== {label}: {', '.join(folders)} ===")
    retry_now = [f for f in folders if ctx.state.rec(f)["evaluation_id"] and ctx.state.rec(f)["uploaded"]
                 and ctx.state.status(f) == "failed"]
    submit = [f for f in folders if f not in retry_now]
    uploaded_ok: list[str] = []
    if submit:
        try:
            with ThreadPoolExecutor(max_workers=len(submit)) as pool:
                futs = {f: pool.submit(upload_student, ctx, ctx.students[f]) for f in submit}
                for f, fut in futs.items():
                    try:
                        fut.result()
                        uploaded_ok.append(f)
                    except (Fatal, Interrupted):
                        raise
                    except Exception as e:  # noqa: BLE001
                        msg = explain(e)
                        if isinstance(e, ApiError):
                            check_fatal(e)
                        ctx.state.update(f, status="failed", error_code="upload_failed", error_message=msg[:500],
                                         uploaded=[], evaluation_id=None)
                        ctx.state.add_error(f, "upload", msg)
                        log(f"{f}: UPLOAD FAILED: {msg}")
        except KeyboardInterrupt:
            STOP.set()
            raise
    live = create_jobs(ctx, uploaded_ok) if uploaded_ok else []
    for f in retry_now:
        if retry_evaluation(ctx, f):
            live.append(f)
    if live:
        wait_terminal(ctx, live)
    done = Counter(ctx.state.status(f) for f in folders)
    log(f"=== {label} finished: {dict(done)} ===")


# ------------------------------------------------------------------------------------------------ plan/report


def chunks(seq: list[str], n: int) -> list[list[str]]:
    return [seq[i:i + n] for i in range(0, len(seq), n)]


def print_plan(students: list[Student], not_downloaded: int, batch_size: int, canary: str | None = None) -> int:
    usable = [s for s in students if s.files]
    total = sum(s.total_bytes for s in usable)
    kinds = Counter(f.ext for s in usable for f in s.files)
    log(f"Plan: {len(students)} folder(s) on disk ({not_downloaded} listed in .summary_state.json but not downloaded)")
    for s in students:
        if s.files:
            desc = ", ".join(f"{f.name} [{f.ext}, {human(f.size)}]" for f in s.files)
            log(f"  {s.folder:<8} {s.name} <{s.email or 'no email'}>: {len(s.files)} file(s), "
                f"{human(s.total_bytes)}: {desc}")
        else:
            log(f"  {s.folder:<8} {s.name} <{s.email or 'no email'}>: SKIPPED ({s.note})")
        for name, reason in s.skipped:
            log(f"             - not uploaded: {name} ({reason})")
    waves = math.ceil(max(len(usable) - (1 if canary else 0), 0) / batch_size)
    log(f"Summary: {len(usable)} student(s) to evaluate, {len(students) - len(usable)} folder(s) with no usable files")
    log(f"         file types: {dict(sorted(kinds.items()))}; total upload {total / 1024**3:.2f} GB")
    log(f"         {('canary ' + canary + ' + ') if canary else ''}{waves} wave(s) of up to {batch_size}")
    return len(usable)


def write_report(ctx: Ctx, order: list[str], report_path: Path) -> list[dict]:
    rows = []
    for f in order:
        s, rec = ctx.students[f], ctx.state.rec(f)
        status = "skipped" if (not s.files) else rec["status"]
        rows.append({
            "folder": f, "email": s.email, "name": s.name, "status": status,
            "reason": s.note if not s.files else (rec["error_message"] or ""),
            "error_code": rec["error_code"], "evaluation_id": rec["evaluation_id"], "batch_id": rec["batch_id"],
            "score": rec["score"], "rag_band": rec["rag_band"], "attempts": rec["attempts"],
            "files": [{"name": x.name, "bytes": x.size} for x in s.files],
            "skipped_files": [{"name": n, "reason": r} for n, r in s.skipped],
            "errors": rec["errors"],
        })
    report_path.write_text(json.dumps({"generated_at": now_iso(), "students": rows}, indent=2), encoding="utf-8")
    with report_path.with_suffix(".csv").open("w", newline="", encoding="utf-8") as fh:
        w = csv.writer(fh)
        w.writerow(["folder", "email", "name", "status", "score", "rag_band", "attempts", "evaluation_id",
                    "error_code", "reason"])
        for r in rows:
            w.writerow([r["folder"], r["email"], r["name"], r["status"], r["score"], r["rag_band"], r["attempts"],
                        r["evaluation_id"], r["error_code"], r["reason"]])
    return rows


def print_summary(rows: list[dict]) -> None:
    log("-" * 100)
    log(f"{'folder':<8} {'status':<10} {'score':<6} {'band':<18} {'tries':<5} name / reason")
    for r in rows:
        why = f"  <- {r['error_code'] or ''} {r['reason']}".rstrip() if r["status"] in ("failed", "skipped") else ""
        extra = r["name"] + why
        log(f"{r['folder']:<8} {r['status']:<10} {str(r['score'] if r['score'] is not None else '-'):<6} "
            f"{str(r['rag_band'] or '-'):<18} {r['attempts']:<5} {extra}")
    c = Counter(r["status"] for r in rows)
    log(f"Totals: {dict(c)}")


# ------------------------------------------------------------------------------------------------ main


def build_parser() -> argparse.ArgumentParser:
    here = Path(__file__).resolve().parent
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--gdrive-dir", type=Path, default=here / "GdriveDownload")
    p.add_argument("--sheet", type=Path, default=None,
                   help="submissions .xlsx with Email/Name columns (default: first .xlsx in scripts/SampleFiles)")
    p.add_argument("--api-base", default=os.environ.get("BACKEND_URL", "http://localhost:8000"))
    p.add_argument("--email", default=os.environ.get("EVAL_EMAIL"), help="evaluator account e-mail (password: EVAL_PASSWORD)")
    p.add_argument("--scorecard-id", default=os.environ.get("SCORECARD_ID"), help="overrides --scorecard-name")
    p.add_argument("--scorecard-name", default=DEFAULT_SCORECARD_NAME,
                   help="resolved via GET /scorecards: exact (case-insensitive) match, else a unique 'contains'")
    p.add_argument("--check", action="store_true",
                   help="preflight only: backend reachable, scorecard + user resolved, storage init works (no uploads)")
    p.add_argument("--direction-prompt", default=None)
    p.add_argument("--batch-size", type=int, default=5)
    p.add_argument("--only", default="", help="comma-separated folder ids")
    p.add_argument("--skip", default="", help="comma-separated folder ids")
    p.add_argument("--limit", type=int, default=0, help="only the first N students (after --only/--skip)")
    p.add_argument("--canary", action=argparse.BooleanOptionalAction, default=True)
    p.add_argument("--canary-folder", default=None)
    p.add_argument("--no-canary-gate", action="store_true", help="continue even if the canary fails")
    p.add_argument("--max-retry-rounds", type=int, default=2)
    p.add_argument("--wave-timeout", type=float, default=90.0, help="minutes to wait for one wave")
    p.add_argument("--poll-interval", type=float, default=10.0)
    p.add_argument("--part-workers", type=int, default=4)
    p.add_argument("--state", type=Path, default=None)
    p.add_argument("--report", type=Path, default=None)
    p.add_argument("--resume", action="store_true", help="re-attach to evaluations the state file says are running")
    p.add_argument("--dry-run", action="store_true", help="print the plan; no network calls at all")
    p.add_argument("--yes", action="store_true", help="do not ask for confirmation")
    p.add_argument("--list-scorecards", action="store_true")
    return p


def pick_canary(args: argparse.Namespace, todo: list[str], students: dict[str, Student]) -> str | None:
    if not args.canary or not todo:
        return None
    if args.canary_folder:
        if args.canary_folder not in todo:
            raise Fatal(f"--canary-folder {args.canary_folder} is not among the folders to process.")
        return args.canary_folder
    docs = [f for f in todo if students[f].has_document] or todo
    return min(docs, key=lambda f: students[f].total_bytes)


def _fmt_cards(cards: list[dict], limit: int = 30) -> str:
    return "\n".join(f"  {c['id']}  {c.get('name')}  [{c.get('domain')}]" for c in cards[:limit])


def resolve_scorecard(api: Api, args: argparse.Namespace) -> dict:
    """--scorecard-id wins; otherwise --scorecard-name: exact case-insensitive, then a unique 'contains'."""
    if args.scorecard_id:
        try:
            return api.get(f"/api/v1/scorecards/{args.scorecard_id}")
        except ApiError as e:
            if e.status == 404:
                raise Fatal(f"Scorecard {args.scorecard_id} not found (try --list-scorecards).") from e
            raise
    cards = api.get("/api/v1/scorecards?limit=500") or []
    want = (args.scorecard_name or "").strip().lower()
    for pool, how in (
        ([c for c in cards if (c.get("name") or "").strip().lower() == want], "exact"),
        ([c for c in cards if want and want in (c.get("name") or "").lower()], "partial"),
    ):
        if len(pool) == 1:
            log(f"Scorecard ({how} match): {pool[0]['name']} [{pool[0].get('domain')}] {pool[0]['id']}")
            return pool[0]
        if len(pool) > 1:
            raise Fatal(f"Scorecard name {args.scorecard_name!r} is ambiguous ({how} match); pass --scorecard-id:\n"
                        + _fmt_cards(pool))
    available = _fmt_cards(cards) or "  (none)"
    raise Fatal(f"No scorecard named {args.scorecard_name!r}. Available scorecards:\n{available}\n"
                "Pass --scorecard-name or --scorecard-id.")


def resolve_user(api: Api, args: argparse.Namespace, scorecard: dict) -> str:
    """The signed-in evaluator's id (GET /me). The scorecard must be one this account owns."""
    me = api.get("/api/v1/me")
    if scorecard.get("owner_id") not in (None, me["id"]):
        raise Fatal(f"Scorecard {scorecard['id']} does not belong to {me['email']}.")
    log(f"Signed in as {me.get('name')} <{me.get('email')}> {me['id']}")
    return me["id"]


def make_api(args: argparse.Namespace) -> Api:
    if not args.email:
        raise Fatal("Pass --email (or set EVAL_EMAIL); the password comes from EVAL_PASSWORD or a hidden prompt.")
    password = os.environ.get("EVAL_PASSWORD") or getpass.getpass(f"Password for {args.email}: ")
    return Api(args.api_base, args.email, password)


def probe_storage(api: Api) -> None:
    """Start and immediately abort a multipart upload: proves S3 + presigning work, stores nothing."""
    init = api.post(f"{API}/ai/uploads", {"purpose": "submission", "files": [
        {"name": "preflight-check.pdf", "size": 1, "content_type": "application/pdf"}]})
    f = init["files"][0]
    abort_upload(api, f["s3_key"], f["upload_id"])


def run_check(ctx_args: argparse.Namespace, api: Api, selected: list[Student], not_downloaded: int) -> int:
    ok = True
    try:
        h = api.get("/health")
        log(f"OK  backend reachable at {api.base} (/health -> {h})")
    except ApiError as e:
        raise Fatal(f"Backend not reachable: {e}") from e
    sc = resolve_scorecard(api, ctx_args)
    log(f"OK  scorecard: {sc['name']} [{sc.get('domain')}] id={sc['id']} status={sc.get('status')}")
    api.user_id = resolve_user(api, ctx_args, sc)
    log(f"OK  evaluator user id: {api.user_id}")
    try:
        probe_storage(api)
        log("OK  upload init + abort works (S3 multipart is configured); nothing was stored")
    except Fatal as e:
        ok = False
        log(f"FAIL storage: {e}")
    except ApiError as e:
        ok = False
        log(f"FAIL storage: {e}")
    usable = [s for s in selected if s.files]
    log(f"OK  local folders: {len(usable)} student(s) with usable files, "
        f"{sum(s.total_bytes for s in usable) / 1024**3:.2f} GB, {len(selected) - len(usable)} with none "
        f"({not_downloaded} more listed but not downloaded)")
    log("Preflight passed: ready to run." if ok else "Preflight FAILED: fix the above before running.")
    return 0 if ok else 2


def run(args: argparse.Namespace) -> int:
    root: Path = args.gdrive_dir
    if args.batch_size < 1:
        raise Fatal("--batch-size must be >= 1")
    if args.list_scorecards:
        api = make_api(args)
        for s in api.get("/api/v1/scorecards?limit=200"):
            log(f"{s['id']}  {s.get('name')}  [{s.get('domain')}]  status={s.get('status')}")
        return 0

    students_all, not_downloaded = scan_all(root, args.sheet)
    only = {x for x in args.only.split(",") if x}
    skip = {x for x in args.skip.split(",") if x}
    selected = [s for s in students_all if (not only or s.folder in only) and s.folder not in skip]
    if args.limit:
        selected = selected[: args.limit]
    if not selected:
        raise Fatal("No folders selected.")
    students = {s.folder: s for s in selected}
    order = [s.folder for s in selected]

    if args.dry_run:
        canary = None
        if args.canary:
            todo = [s.folder for s in selected if s.files]
            canary = pick_canary(args, todo, students)
        print_plan(selected, not_downloaded, args.batch_size, canary)
        log("Dry run: nothing was uploaded or created.")
        return 0

    api = make_api(args)
    if args.check:
        return run_check(args, api, selected, not_downloaded)
    scorecard = resolve_scorecard(api, args)
    args.scorecard_id = scorecard["id"]
    api.user_id = resolve_user(api, args, scorecard)
    log(f"Scorecard: {scorecard['name']} [{scorecard.get('domain')}]  evaluator: {api.user_id}")
    state_path = args.state or root / ".batch_eval_state.json"
    report_path = args.report or root / ".batch_eval_report.json"
    state = State.load(state_path, args.scorecard_id)
    ctx = Ctx(api, state, args, students)

    for f in order:  # persist identity-level skips and normalise states left by a previous run
        s = students[f]
        if not s.files:
            state.update(f, status="skipped", error_message=s.note)
    evaluating = [f for f in order if state.status(f) == "evaluating"]
    if evaluating and not args.resume:
        raise Fatal(f"{state_path} has {len(evaluating)} evaluation(s) still running ({', '.join(evaluating)}). "
                    "Re-run with --resume to re-attach to them (they are never submitted twice).")

    todo = [f for f in order if students[f].files and state.status(f) in ("pending", "uploaded")]
    canary = pick_canary(args, todo, students) if not any(state.status(f) == "completed" for f in order) else None
    print_plan([students[f] for f in order], not_downloaded, args.batch_size, canary)
    to_upload = [f for f in todo]
    log(f"Will process {len(to_upload)} new folder(s), re-attach {len(evaluating)}, retry "
        f"{len([f for f in order if state.status(f) == 'failed'])} previously failed.")
    if not args.yes:
        if not sys.stdin.isatty():
            raise Fatal("Refusing to start without confirmation: pass --yes "
                        "(this uploads files and spends AI credits).")
        if input("Proceed? [y/N] ").strip().lower() not in ("y", "yes"):
            log("Aborted by user.")
            return 2

    rc = 0
    try:
        if evaluating:
            log(f"Re-attaching to {len(evaluating)} running evaluation(s)")
            wait_terminal(ctx, evaluating)
        if canary:
            process_wave(ctx, [canary], "canary")
            if state.status(canary) != "completed":
                fetch_diagnostics(ctx, canary)
                if not args.no_canary_gate:
                    log("Canary did NOT complete: stopping. Fix the problem above, then re-run (state is kept).")
                    rows = write_report(ctx, order, report_path)
                    print_summary(rows)
                    return 2
                log("Canary failed but --no-canary-gate was given: continuing.")
            else:
                log("Canary completed: continuing with the remaining folders.")
            todo = [f for f in todo if f != canary]
        waves = chunks(todo, args.batch_size)
        for i, wave in enumerate(waves, 1):
            process_wave(ctx, wave, f"wave {i}/{len(waves)}")
        for rnd in range(1, args.max_retry_rounds + 1):
            queue = [f for f in order if students[f].files and state.status(f) == "failed"]
            if not queue:
                break
            log(f"Retry round {rnd}/{args.max_retry_rounds}: {len(queue)} folder(s) in the retry queue")
            for i, wave in enumerate(chunks(queue, args.batch_size), 1):
                process_wave(ctx, wave, f"retry round {rnd} wave {i}")
    except WaveTimeout as e:
        log(f"Gave up waiting after {args.wave_timeout:g} min: {e}. They are still running on the server; "
            "re-run with --resume to re-attach.")
        rc = 3
    except (KeyboardInterrupt, Interrupted):
        STOP.set()
        n = ctx.registry.abort_all(api)
        log(f"Interrupted: aborted {n} in-flight upload(s); state saved. Re-run with --resume to continue.")
        rc = 130
    rows = write_report(ctx, order, report_path)
    print_summary(rows)
    log(f"State: {state_path}\nReport: {report_path} (+ .csv)")
    if rc:
        return rc
    return 1 if any(r["status"] == "failed" for r in rows) else 0


def main(argv: list[str] | None = None) -> int:
    STOP.clear()
    args = build_parser().parse_args(argv)
    try:
        return run(args)
    except Fatal as e:
        log(f"ERROR: {e}")
        return 2
    except ApiError as e:
        log(f"ERROR: {e}")
        return 2


if __name__ == "__main__":
    sys.exit(main())
