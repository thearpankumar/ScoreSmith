"""Google Drive URL parsing, classification and SSRF guards.

Only https URLs on an allowlist of Google hosts are accepted; the actual download happens in the
`ingest` Lambda on AWS, but the backend rejects anything else up front (and exposes the resolved-IP
check the Lambda-side contract also requires)."""

from __future__ import annotations

import ipaddress
import socket
from dataclasses import dataclass
from urllib.parse import parse_qs, urlsplit

ALLOWED_DRIVE_HOSTS = frozenset({"drive.google.com", "docs.google.com", "drive.usercontent.google.com"})


class DriveUrlError(ValueError):
    """The URL is not an acceptable public Google Drive link (message is user-presentable)."""


@dataclass(frozen=True)
class DriveLink:
    url: str
    kind: str  # folder | file | doc
    resource_id: str


def _segments(path: str) -> list[str]:
    return [p for p in path.split("/") if p]


def classify_drive_url(url: str) -> DriveLink:
    """Validates and classifies a Drive URL. Raises `DriveUrlError` (with a reason) otherwise."""
    raw = (url or "").strip()
    if not raw:
        raise DriveUrlError("Empty link.")
    try:
        parts = urlsplit(raw)
    except ValueError as exc:
        raise DriveUrlError("Malformed link.") from exc
    if parts.scheme.lower() != "https":
        raise DriveUrlError("Only https links are allowed.")
    if parts.username or parts.password:
        raise DriveUrlError("Links with credentials are not allowed.")
    try:
        port = parts.port
    except ValueError as exc:
        raise DriveUrlError("Malformed link.") from exc
    if port not in (None, 443):
        raise DriveUrlError("Custom ports are not allowed.")
    host = (parts.hostname or "").lower().rstrip(".")
    if host not in ALLOWED_DRIVE_HOSTS:
        raise DriveUrlError(f"Host {host or '?'} is not a Google Drive host.")

    segs = _segments(parts.path)
    query = parse_qs(parts.query)
    qid = (query.get("id") or [""])[0]

    if host == "docs.google.com":
        # /document/d/ID, /spreadsheets/d/ID, /presentation/d/ID, /forms/d/ID
        if len(segs) >= 3 and segs[1] == "d" and segs[0] in {"document", "spreadsheets", "presentation"}:
            return DriveLink(raw, "doc", segs[2])
        raise DriveUrlError("Unsupported Google Docs link.")

    if host == "drive.usercontent.google.com":
        if qid:
            return DriveLink(raw, "file", qid)
        raise DriveUrlError("Unsupported Drive download link.")

    # drive.google.com
    if "folders" in segs:
        idx = segs.index("folders")
        if idx + 1 < len(segs):
            return DriveLink(raw, "folder", segs[idx + 1])
        raise DriveUrlError("Folder link has no folder id.")
    if "file" in segs and "d" in segs:
        idx = segs.index("d")
        if idx + 1 < len(segs):
            return DriveLink(raw, "file", segs[idx + 1])
    if segs and segs[0] in {"open", "uc", "drive"} and qid:
        return DriveLink(raw, "file", qid)
    if segs and segs[0] == "folderview" and qid:
        return DriveLink(raw, "folder", qid)
    raise DriveUrlError("Unrecognised Google Drive link format.")


def is_valid_drive_url(url: str) -> bool:
    try:
        classify_drive_url(url)
    except DriveUrlError:
        return False
    return True


def ip_is_public(ip: str) -> bool:
    try:
        addr = ipaddress.ip_address(ip)
    except ValueError:
        return False
    return not (
        addr.is_private or addr.is_loopback or addr.is_link_local or addr.is_multicast
        or addr.is_reserved or addr.is_unspecified
    )


def assert_host_resolves_public(host: str) -> None:
    """Resolved-IP SSRF guard: every address `host` resolves to must be a public one."""
    try:
        infos = socket.getaddrinfo(host, 443, proto=socket.IPPROTO_TCP)
    except OSError as exc:
        raise DriveUrlError(f"Could not resolve {host}.") from exc
    for info in infos:
        if not ip_is_public(info[4][0]):
            raise DriveUrlError(f"{host} resolves to a non-public address.")
