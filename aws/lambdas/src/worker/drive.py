"""Google Drive access (ported from scripts/gdrive_download.py).

Folder listing uses gdown (it scrapes the public folder page); file downloads use a small streaming client that
(a) only ever talks to allowlisted Google hosts, re-validating every redirect hop, (b) enforces a byte cap while
streaming, and (c) detects quota / not-public / empty responses. Only the Drive *id* is taken from user input; every
URL is built here, so the user-supplied host is never contacted.
"""
from __future__ import annotations

import html
import re
import time
from dataclasses import dataclass
from urllib.parse import parse_qs, unquote, urljoin, urlparse

from .errors import PipelineError
from .security import check_url, default_resolver, safe_name

EXPORTS = {  # Google-native doc type -> export format (the pipeline only ingests pdf/docx/video)
    "document": "docx",
    "spreadsheets": "pdf",
    "presentation": "pdf",
}
FOLDER_LISTING_CAP = 50  # Google caps a folder listing at 50 files
MAX_REDIRECTS = 5
HTML_PEEK_BYTES = 256 * 1024
QUOTA_MARKERS = ("quota exceeded", "too many users have viewed or downloaded", "download quota", "too many requests",
                 "unusual traffic")


class DriveError(Exception):
    """state: inaccessible | quota | too_large | empty ; retryable only for transient network errors."""

    def __init__(self, state: str, message: str):
        super().__init__(message)
        self.state = state
        self.message = message


class RetryableDriveError(Exception):
    pass


def parse_link(url: str) -> tuple[str, str] | None:
    """(kind, id) where kind is folder | file | document | spreadsheets | presentation | open."""
    u = urlparse((url or "").strip())
    m = re.search(r"/folders/([\w-]+)", u.path)
    if m:
        return "folder", m.group(1)
    m = re.search(r"/(document|spreadsheets|presentation)/d/([\w-]+)", u.path)
    if m:
        return m.group(1), m.group(2)
    m = re.search(r"/file/d/([\w-]+)", u.path)
    if m:
        return "file", m.group(1)
    q = parse_qs(u.query).get("id")
    if q and re.fullmatch(r"[\w-]+", q[0]):
        return "open", q[0]
    return None


def classify(url: str, resolver=default_resolver) -> tuple[str, str]:
    """Validate (https, allowlisted host, public IP) and parse. Raises PipelineError(drive_invalid)."""
    check_url(url, resolver=resolver)
    parsed = parse_link(url)
    if not parsed:
        raise PipelineError("drive_invalid", "Unrecognised Google Drive link format")
    return parsed


def filename_from_disposition(cd: str) -> str:
    m = re.search(r"filename\*=(?:UTF-8|utf-8)''([^;]+)", cd or "")
    if m:
        return safe_name(unquote(m.group(1)))
    m = re.search(r'filename="([^"]+)"', cd or "")
    if m:
        return safe_name(m.group(1))
    return ""


def classify_error_text(text: str) -> str:
    low = (text or "").lower()
    if any(k in low for k in QUOTA_MARKERS):
        return "quota"
    return "inaccessible"


def default_list_folder(folder_id: str) -> list[tuple[str, str]]:
    import gdown

    try:
        files = gdown.download_folder(id=folder_id, skip_download=True, quiet=True, use_cookies=False, remaining_ok=True)
    except TypeError:  # old gdown without remaining_ok
        files = gdown.download_folder(id=folder_id, skip_download=True, quiet=True, use_cookies=False)
    return [(f.id, f.path) for f in (files or [])]


@dataclass
class Downloaded:
    name: str
    size: int


class DriveClient:
    def __init__(self, session=None, list_folder=default_list_folder, resolver=default_resolver,
                 sleep=time.sleep, tries: int = 3, log=print):
        if session is None:
            import requests

            session = requests.Session()
            session.headers["User-Agent"] = "Mozilla/5.0 (compatible; qs-or-ingest/1.0)"
        self.session, self._list_folder, self.resolver = session, list_folder, resolver
        self.sleep, self.tries, self.log = sleep, tries, log

    # --------------------------------------------------------------- retry
    def _retry(self, fn, what: str):
        for attempt in range(1, self.tries + 1):
            try:
                return fn()
            except (RetryableDriveError, OSError) as e:
                last: Exception = e
                state = "network"
            except DriveError as e:
                if e.state != "quota":
                    raise
                last, state = e, "quota"
            if attempt == self.tries:
                if isinstance(last, DriveError):
                    raise last
                raise DriveError("inaccessible", f"{what}: network error after {self.tries} attempts ({last})") from last
            wait = 2 ** attempt
            self.log(f"    retry {attempt}/{self.tries - 1} for {what} in {wait}s ({state})")
            self.sleep(wait)

    # --------------------------------------------------------------- listing
    def list_remote(self, kind: str, rid: str) -> tuple[list[tuple[str, str]], int, list[str]]:
        """([(drive_id, filename)] of top-level files, #files hidden in subfolders, warnings)."""
        warnings: list[str] = []
        if kind in EXPORTS:
            return [(rid, "")], 0, warnings
        if kind in ("folder", "open"):
            try:
                files = self._retry(lambda: self._listing(rid), "folder listing")
            except DriveError as e:
                if kind == "folder":
                    raise
                files = None  # open?id= pointing at a single file
                _ = e
            if files is not None:
                top = [(i, p) for i, p in files if "/" not in p and "\\" not in p]
                nested = len(files) - len(top)
                if len(files) >= FOLDER_LISTING_CAP:
                    warnings.append(f"Google lists at most {FOLDER_LISTING_CAP} files per folder; "
                                    "files beyond that were not visible (upload them directly instead).")
                if nested:
                    warnings.append(f"Ignored {nested} file(s) inside subfolders (only top-level files are fetched).")
                return top, nested, warnings
        return [(rid, "")], 0, warnings

    def _listing(self, folder_id: str):
        try:
            return self._list_folder(folder_id)
        except DriveError:
            raise
        except Exception as e:  # gdown raises several unrelated exception types
            msg = str(e)
            state = classify_error_text(msg)
            if state == "quota":
                raise DriveError("quota", "Google Drive quota/rate limit hit") from e
            if "public link" in msg or "Anyone with the link" in msg or "not public" in msg or "Cannot retrieve" in msg:
                raise DriveError("inaccessible", "Folder is not public ('Anyone with the link' required)") from e
            if isinstance(e, (OSError, ConnectionError, TimeoutError)):
                raise RetryableDriveError(msg) from e
            raise DriveError("inaccessible", f"Could not list folder: {' '.join(msg.split())[:120]}") from e

    # --------------------------------------------------------------- download
    def _get(self, url: str, params: dict | None = None):
        """GET with manual redirects; every hop re-validated (https, allowlist, public IP)."""
        for _ in range(MAX_REDIRECTS + 1):
            check_url(url, redirect=True, resolver=self.resolver)
            try:
                r = self.session.get(url, params=params, stream=True, allow_redirects=False, timeout=60)
            except OSError as e:
                raise RetryableDriveError(str(e)) from e
            params = None
            if r.status_code in (301, 302, 303, 307, 308):
                loc = r.headers.get("location")
                r.close()
                if not loc:
                    raise DriveError("inaccessible", "Redirect without Location")
                url = urljoin(url, loc)
                continue
            return r
        raise DriveError("inaccessible", "Too many redirects")

    @staticmethod
    def _download_url(kind: str, fid: str) -> str:
        if kind in EXPORTS:
            return f"https://docs.google.com/{kind}/d/{fid}/export?format={EXPORTS[kind]}"
        return f"https://drive.usercontent.google.com/download?id={fid}&export=download&confirm=t"

    def download(self, kind: str, fid: str, dest: str, max_bytes: int, name_hint: str = "") -> Downloaded:
        """Stream one public file/export to `dest`. Raises DriveError (inaccessible|quota|too_large|empty)."""
        url = self._download_url(kind, fid)

        def go() -> Downloaded:
            current, params = url, None
            for _ in range(3):  # at most: direct, virus-scan confirm form, one more
                r = self._get(current, params)
                try:
                    if r.status_code == 429:
                        raise DriveError("quota", "HTTP 429 from Google Drive")
                    if r.status_code in (401, 403, 404):
                        raise DriveError("inaccessible", f"HTTP {r.status_code}: file not public or not found")
                    if r.status_code >= 500:
                        raise RetryableDriveError(f"HTTP {r.status_code}")
                    if r.status_code != 200:
                        raise DriveError("inaccessible", f"HTTP {r.status_code}")
                    ctype = (r.headers.get("content-type") or "").lower()
                    if "text/html" in ctype:
                        body = b""
                        for chunk in r.iter_content(65536):
                            body += chunk
                            if len(body) >= HTML_PEEK_BYTES:
                                break
                        text = body.decode("utf-8", errors="replace")
                        form = _confirm_form(text)
                        if form:
                            current, params = form
                            continue
                        raise DriveError(classify_error_text(text),
                                         "Google returned a web page instead of the file (not public, or quota exceeded)")
                    clen = r.headers.get("content-length")
                    if clen and clen.isdigit() and int(clen) > max_bytes:
                        raise DriveError("too_large", f"File is {int(clen)} bytes (limit {max_bytes})")
                    size = 0
                    with open(dest, "wb") as f:
                        for chunk in r.iter_content(1 << 20):
                            size += len(chunk)
                            if size > max_bytes:
                                raise DriveError("too_large", f"File exceeds the {max_bytes} byte limit")
                            f.write(chunk)
                    if size == 0:
                        raise DriveError("empty", "Empty download (not public / quota exceeded?)")
                    name = filename_from_disposition(r.headers.get("content-disposition", "")) or name_hint
                    if not name:
                        name = f"{kind}_{fid}.{EXPORTS[kind]}" if kind in EXPORTS else f"file_{fid}"
                    return Downloaded(name=name, size=size)
                finally:
                    r.close()
            raise DriveError("inaccessible", "Could not get past the Google confirmation page")

        return self._retry(go, name_hint or fid)


def _confirm_form(page: str) -> tuple[str, dict] | None:
    """Parse Google's 'can't scan for viruses' confirmation form into (url, params)."""
    m = re.search(r'<form[^>]*id="download-form"[^>]*action="([^"]+)"', page) or \
        re.search(r'<form[^>]*action="([^"]*usercontent[^"]*)"', page)
    if not m:
        return None
    action = html.unescape(m.group(1))
    params = {k: html.unescape(v) for k, v in re.findall(r'<input[^>]+type="hidden"[^>]+name="([^"]+)"[^>]+value="([^"]*)"', page)}
    if not params.get("id"):
        return None
    return action, params
