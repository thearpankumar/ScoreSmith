"""Ingest step: validate uploaded files, fetch Drive sources into S3 raw/, write manifest.json.

Input is the Step Functions input (contract). Output is small ({"files": [...], "manifest_key"}) because Step
Functions payloads are limited to 256 KB; the full manifest lives in S3.
"""
from __future__ import annotations

import os
import shutil
import tempfile

from . import audio
from .drive import DriveClient, DriveError, classify
from .errors import PipelineError
from .progress import Progress
from .security import (DOC_EXT, TEXT_EXT, UNSUPPORTED_EXT, VIDEO_EXT, check_docx, check_pdf_head, check_text_head,
                       check_video_head, ext_of, kind_of, safe_name, sniff_kind)
from .store import derived

SUPPORTED = DOC_EXT | VIDEO_EXT | TEXT_EXT
KINDS = ("pdf", "docx", "text", "video")
DEFAULT_LIMITS = {"max_file_bytes": 2 * 1024 ** 3, "max_files": 20}


def _guess_kind(name: str, head: bytes) -> str:
    k = kind_of(name)
    if k != "other":
        return k
    s = sniff_kind(head)
    # A zip is a DOCX candidate; `check_docx` later rejects any zip without word/document.xml.
    return {"pdf": "pdf", "video": "video", "zip": "docx"}.get(s, "other")


def _entry(source_id, name, kind, raw_key, size, status, warnings, parent=None):
    e = {"source_id": source_id, "original_name": name, "kind": kind, "raw_key": raw_key, "size": size,
         "status": status, "warnings": warnings}
    if parent:
        e["parent_source_id"] = parent
    return e


def choose_error(files: list[dict], drive: list[dict], codes: list[str]) -> PipelineError:
    """Pick the contract error code when nothing processable was found."""
    states = {d["state"] for d in drive}
    if "quota" in codes or "quota" in states:
        return PipelineError("drive_quota", "Google Drive rate limit/quota hit. Try again later or upload the files directly.")
    if "drive_invalid" in codes:
        return PipelineError("drive_invalid", next((w for d in drive for w in d["warnings"]), "Invalid Google Drive link."))
    if "inaccessible" in states:
        return PipelineError("drive_inaccessible", "The Google Drive link is not public ('Anyone with the link') or not found. "
                                                   "Make it public or upload the files directly.")
    if "empty" in states:
        return PipelineError("drive_empty", "The Google Drive link contains no files.")
    if "too_large" in codes:
        return PipelineError("file_too_large", "All files exceed the size limit.")
    if any(f["status"] == "failed" for f in files) or "unsupported" in codes:
        return PipelineError("unsupported_type", "No supported files (PDF, DOCX, Markdown/text, MP4/MOV/MKV/WEBM/M4V) were found.")
    return PipelineError("no_content", "No files were provided.")


def run_ingest(event: dict, store, drive: DriveClient | None = None, probe_fn=audio.probe,
               tmp_root: str | None = None) -> dict:
    eid, bucket = event["evaluation_id"], event["bucket"]
    limits = {**DEFAULT_LIMITS, **(event.get("limits") or {})}
    max_bytes, max_files = int(limits["max_file_bytes"]), int(limits["max_files"])
    prog = Progress(store, eid)
    prog.set_stage("ingest", "Fetching and validating files")

    files: list[dict] = []
    drive_entries: list[dict] = []
    codes: list[str] = []  # internal reasons used by choose_error
    drive = drive or DriveClient()

    def accepted() -> int:
        return sum(1 for f in files if f["status"] == "ok")

    for src in event.get("sources", []):
        sid = src["source_id"]
        name = src.get("original_name") or sid
        if src.get("kind") == "upload":
            _ingest_upload(src, eid, store, files, codes, max_bytes, max_files, accepted, prog)
        elif src.get("kind") == "drive":
            _ingest_drive(src, eid, store, drive, files, drive_entries, codes, max_bytes, max_files, accepted, prog,
                          probe_fn, tmp_root)
        else:
            files.append(_entry(sid, name, "other", "", 0, "failed", ["Unknown source kind"]))
            codes.append("unsupported")

    manifest = {"files": files, "drive": drive_entries}
    manifest_key = derived(eid, "manifest.json")
    store.put_json(manifest_key, manifest)

    ok = [f for f in files if f["status"] == "ok" and f["kind"] in KINDS]
    if not ok:
        raise choose_error(files, drive_entries, codes)
    for f in files:
        if f["status"] == "ok":
            prog.file(f["source_id"], f["original_name"], "pending", refresh=False)
        else:
            prog.file(f["source_id"], f["original_name"], "skipped" if f["status"] == "skipped" else "failed",
                      "; ".join(f["warnings"])[:200], refresh=False)
    prog.set_stage("extract", "Extracting documents and transcribing audio")
    return {"manifest_key": manifest_key,
            "files": [{"source_id": f["source_id"], "kind": f["kind"], "raw_key": f["raw_key"],
                       "original_name": f["original_name"], "size": f["size"]} for f in ok]}


# --------------------------------------------------------------------------- uploads
def _ingest_upload(src, eid, store, files, codes, max_bytes, max_files, accepted, prog):
    sid, name, key = src["source_id"], src.get("original_name") or src["source_id"], src.get("s3_key", "")
    if not key.startswith("uploads/") or ".." in key:
        files.append(_entry(sid, name, "other", "", 0, "failed", ["Upload key is outside the uploads/ prefix"]))
        codes.append("unsupported")
        return
    h = store.head(key)
    if h is None:
        files.append(_entry(sid, name, "other", "", 0, "failed", ["Uploaded file is missing from storage"]))
        codes.append("unsupported")
        return
    size = h["size"]
    head = store.get_range(key, 0, 4095) if size else b""
    kind = _guess_kind(name, head)
    if kind not in KINDS:
        files.append(_entry(sid, name, "other", "", size, "skipped", [f"Unsupported file type: {name}"]))
        codes.append("unsupported")
        return
    if size > max_bytes:
        files.append(_entry(sid, name, kind, "", size, "skipped", [f"{name} is larger than the limit ({max_bytes} bytes)"]))
        codes.append("too_large")
        return
    if accepted() >= max_files:
        files.append(_entry(sid, name, kind, "", size, "skipped", [f"{name} skipped: file limit ({max_files}) reached"]))
        return
    bad = None
    if kind == "pdf" and not check_pdf_head(head):
        bad = "not a valid PDF"
    elif kind == "video" and not check_video_head(head):
        bad = "not a recognised video container"
    elif kind == "docx" and not head.startswith(b"PK\x03\x04"):
        bad = "not a valid DOCX"
    elif kind == "text" and not check_text_head(head):
        bad = "not a text file"
    if bad:
        files.append(_entry(sid, name, kind, "", size, "failed", [f"{name}: {bad}"]))
        codes.append("unsupported")
        return
    ext = ext_of(name) or kind
    raw_key = f"raw/{eid}/{sid}.{ext}"
    if kind == "docx":  # needs the zip central directory: check the whole file
        tmp = tempfile.mkdtemp(prefix="docx_")
        try:
            p = os.path.join(tmp, "f.docx")
            store.download(key, p)
            check_docx(p)
        except PipelineError as e:
            files.append(_entry(sid, name, kind, "", size, "failed", [f"{name}: {e.message}"]))
            codes.append("unsupported")
            return
        finally:
            shutil.rmtree(tmp, ignore_errors=True)
    store.copy(key, raw_key)
    files.append(_entry(sid, name, kind, raw_key, size, "ok", []))


# --------------------------------------------------------------------------- drive
def _ingest_drive(src, eid, store, drive, files, drive_entries, codes, max_bytes, max_files, accepted, prog,
                  probe_fn, tmp_root):
    sid, url = src["source_id"], src.get("drive_url", "")
    d = {"source_id": sid, "url": url, "state": "ok", "files_found": 0, "warnings": []}
    drive_entries.append(d)
    try:
        kind, rid = classify(url, resolver=drive.resolver)
    except PipelineError as e:
        d.update(state="inaccessible", warnings=[e.message])
        codes.append("drive_invalid")
        return
    try:
        remote, _nested, warns = drive.list_remote(kind, rid)
    except DriveError as e:
        d.update(state=e.state if e.state in ("quota", "empty") else "inaccessible", warnings=[e.message])
        return
    d["warnings"].extend(warns)
    if not remote:
        d.update(state="empty", warnings=d["warnings"] + ["Folder has no files at the top level."])
        return
    d["files_found"] = len(remote)
    ok_count = 0
    for n, (fid, rname) in enumerate(remote, 1):
        child = f"{sid}-{n:02d}"
        name = safe_name(rname) if rname else ""
        if name and ext_of(name) in UNSUPPORTED_EXT:
            files.append(_entry(child, name, "other", "", 0, "skipped", [f"Unsupported file type skipped: {name}"], sid))
            codes.append("unsupported")
            continue
        if accepted() >= max_files:
            files.append(_entry(child, name or fid, kind_of(name), "", 0, "skipped",
                                [f"{name or fid} skipped: file limit ({max_files}) reached"], sid))
            continue
        tmp = tempfile.mkdtemp(prefix="drive_", dir=tmp_root)
        try:
            dest = os.path.join(tmp, "download")
            try:
                got = drive.download(kind if kind in ("document", "spreadsheets", "presentation") else "file", fid,
                                     dest, max_bytes, name_hint=name)
            except DriveError as e:
                if e.state == "too_large":
                    files.append(_entry(child, name or fid, kind_of(name), "", 0, "skipped", [f"{name or fid}: {e.message}"], sid))
                    codes.append("too_large")
                else:
                    state = e.state if e.state in ("quota", "empty") else "inaccessible"
                    d["state"] = state if d["state"] == "ok" else d["state"]
                    d["warnings"].append(f"{name or fid}: {e.message}")
                    files.append(_entry(child, name or fid, kind_of(name), "", 0, "failed", [e.message], sid))
                continue
            name = got.name
            with open(dest, "rb") as fh:
                head = fh.read(4096)
            fkind = _guess_kind(name, head)
            if fkind not in KINDS:
                files.append(_entry(child, name, "other", "", got.size, "skipped", [f"Unsupported file type skipped: {name}"], sid))
                codes.append("unsupported")
                continue
            if ext_of(name) not in SUPPORTED:  # name had no usable extension: derive one from the content
                name = f"{name}.{ {'pdf': 'pdf', 'docx': 'docx', 'text': 'txt', 'video': 'mp4'}[fkind] }"
            bad = None
            try:
                if fkind == "pdf" and not check_pdf_head(head):
                    bad = "not a valid PDF"
                elif fkind == "docx":
                    check_docx(dest)
                elif fkind == "text" and not check_text_head(head):
                    bad = "not a text file"
                elif fkind == "video":
                    dur, _a, valid = probe_fn(dest)
                    if not valid:
                        bad = "not a readable video"
            except PipelineError as e:
                bad = e.message
            if bad:
                files.append(_entry(child, name, fkind, "", got.size, "failed", [f"{name}: {bad}"], sid))
                codes.append("unsupported")
                continue
            raw_key = f"raw/{eid}/{child}.{ext_of(name) or fkind}"
            store.upload(dest, raw_key)
            files.append(_entry(child, name, fkind, raw_key, got.size, "ok", [], sid))
            ok_count += 1
        finally:
            shutil.rmtree(tmp, ignore_errors=True)
    if ok_count == 0 and d["state"] == "ok":
        d["state"] = "empty" if not any(f.get("parent_source_id") == sid for f in files) else "ok"
