"""Pure security helpers: Drive URL allowlist, SSRF checks, magic bytes, zip-bomb limits."""
from __future__ import annotations

import ipaddress
import re
import socket
import zipfile
from urllib.parse import urlparse

from .errors import PipelineError

ALLOWED_HOSTS = frozenset({"drive.google.com", "docs.google.com", "drive.usercontent.google.com"})
# Google serves some export/download responses from per-file *.googleusercontent.com hosts. Redirects (never user
# input) may land there; every hop is still https-only and IP-checked.
REDIRECT_SUFFIXES = (".googleusercontent.com",)

VIDEO_EXT = frozenset({"mp4", "mov", "mkv", "webm", "m4v"})
DOC_EXT = frozenset({"pdf", "docx"})
TEXT_EXT = frozenset({"md", "markdown", "txt"})  # plain-text solution documents
# Extensions that are certainly not a submission document or video. Anything else (including no extension,
# e.g. a Drive file named "report (final)") is downloaded and identified by its content instead.
UNSUPPORTED_EXT = frozenset({
    "zip", "rar", "7z", "tar", "gz", "tgz", "rtf", "csv", "json", "xml", "html", "htm", "log",
    "png", "jpg", "jpeg", "gif", "webp", "bmp", "svg", "ico", "pptx", "ppt", "xlsx", "xls", "doc", "odt",
    "exe", "dll", "msi", "apk", "iso", "py", "js", "ts", "java", "ipynb", "mp3", "wav", "m4a", "gdoc", "gsheet",
    "gslides", "url", "lnk",
})

MAX_DOCX_UNCOMPRESSED = 1_000_000_000  # 1 GB total inflated size
MAX_DOCX_RATIO = 200  # per-member inflate ratio (only enforced for members > 10 MB)


def default_resolver(host: str) -> list[str]:
    return sorted({ai[4][0] for ai in socket.getaddrinfo(host, 443, proto=socket.IPPROTO_TCP)})


def is_public_ip(ip: str) -> bool:
    a = ipaddress.ip_address(ip)
    if isinstance(a, ipaddress.IPv6Address) and a.ipv4_mapped:
        a = a.ipv4_mapped
    return not (a.is_private or a.is_loopback or a.is_link_local or a.is_reserved
                or a.is_multicast or a.is_unspecified)


def host_allowed(host: str, *, redirect: bool = False) -> bool:
    host = (host or "").lower().rstrip(".")
    if host in ALLOWED_HOSTS:
        return True
    return redirect and any(host.endswith(s) for s in REDIRECT_SUFFIXES)


def check_url(url: str, *, redirect: bool = False, resolver=default_resolver) -> str:
    """Validate an https URL against the host allowlist and block non-public resolved addresses.
    Returns the host. Raises PipelineError(drive_invalid)."""
    u = urlparse((url or "").strip())
    if u.scheme != "https":
        raise PipelineError("drive_invalid", "Only https Google Drive links are accepted")
    if u.username or u.password:
        raise PipelineError("drive_invalid", "Credentials in URLs are not allowed")
    try:
        port = u.port
    except ValueError:
        port = -1
    if port not in (None, 443):
        raise PipelineError("drive_invalid", "Custom ports are not allowed")
    host = (u.hostname or "").lower().rstrip(".")
    if not host_allowed(host, redirect=redirect):
        raise PipelineError("drive_invalid", f"Host not allowed: {host or '(none)'}")
    try:
        ips = resolver(host)
    except OSError as e:
        raise PipelineError("drive_invalid", f"Could not resolve {host}: {e}") from e
    if not ips:
        raise PipelineError("drive_invalid", f"Could not resolve {host}")
    for ip in ips:
        if not is_public_ip(ip):
            raise PipelineError("drive_invalid", f"{host} resolves to a non-public address")
    return host


# --------------------------------------------------------------------------- types / magic bytes
def ext_of(name: str) -> str:
    return name.rsplit(".", 1)[-1].lower() if "." in (name or "") else ""


def kind_of(name: str) -> str:
    e = ext_of(name)
    if e in DOC_EXT:
        return e
    if e in VIDEO_EXT:
        return "video"
    if e in TEXT_EXT:
        return "text"
    return "other"


def sniff_kind(head: bytes) -> str:
    """Kind from content alone: pdf | zip | video | other."""
    if b"%PDF" in head[:1024]:
        return "pdf"
    if head[:4] == b"PK\x03\x04":
        return "zip"
    if head[4:8] == b"ftyp" or head[:4] == b"\x1a\x45\xdf\xa3":
        return "video"
    return "other"


def check_text_head(head: bytes) -> bool:
    """A text file has no NUL bytes (those mean binary: an image, an archive, an office file...)."""
    return b"\x00" not in head


def check_pdf_head(head: bytes) -> bool:
    return b"%PDF" in head[:1024]


def check_video_head(head: bytes) -> bool:
    return sniff_kind(head) == "video"


def check_docx(path: str) -> None:
    """Raises PipelineError(unsupported_type|extract_failed) unless `path` is a sane DOCX zip."""
    try:
        zf = zipfile.ZipFile(path)
    except zipfile.BadZipFile as e:
        raise PipelineError("unsupported_type", "File is not a valid DOCX (not a zip archive)") from e
    with zf:
        if "word/document.xml" not in zf.namelist():
            raise PipelineError("unsupported_type", "File is not a valid DOCX (no word/document.xml)")
        total = 0
        for info in zf.infolist():
            total += info.file_size
            if info.file_size > 10_000_000 and info.file_size / max(info.compress_size, 1) > MAX_DOCX_RATIO:
                raise PipelineError("extract_failed", "DOCX rejected: suspicious compression ratio")
        if total > MAX_DOCX_UNCOMPRESSED:
            raise PipelineError("extract_failed", "DOCX rejected: uncompressed size too large")


def safe_name(name: str) -> str:
    name = re.sub(r'[<>:"/\\|?*\x00-\x1f]', "_", name or "").strip(" .")
    return name or "unnamed"
