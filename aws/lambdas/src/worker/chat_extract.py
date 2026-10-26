"""chat_extract: the text of a chat attachment (PDF / DOCX), for the backend's chat.

The backend invokes the extract-doc function synchronously with
    {"mode": "chat_extract", "bucket", "key": "chat/{user}/{uuid}.pdf|docx", "kind": "pdf"|"docx",
     "out_key": "chat/{user}/{uuid}.json", "max_chars": 400000}
and gets back (the same JSON is also written to out_key)
    {"ok": true, "kind", "text": "<markdown>", "pages": int|null, "chars", "truncated", "scanned_pages": [int],
     "needs_native": bool, "warnings": [str]}
or {"ok": false, "error": "<short reason>"}. It never raises: a bad file is an answer, not a Lambda error.

The text comes from the same extractors as evaluation documents (docs.extract_pdf / extract_docx: reading order,
"## Page N" headings for PDFs), with tables as Markdown and no image analysis: images are counted, never decoded. A PDF
whose pages are mostly images with little text (< 100 characters: scans) is flagged `needs_native`, so the backend can
send the PDF itself to a model that reads it.
"""
from __future__ import annotations

import json
import os
import re
import shutil
import tempfile
from pathlib import Path

from . import docs
from .errors import PipelineError
from .store import S3Store

KINDS = ("pdf", "docx")
MAX_CHARS = 400_000
MAX_BYTES = 50 * 1024 * 1024
SCANNED_PAGE_CHARS = 100        # a page with images and fewer non-space characters than this looks scanned
MAX_RESPONSE_BYTES = 5_500_000  # Lambda's synchronous response limit is 6 MB (JSON, non-ASCII escaped)
_KEY_RE = re.compile(r"^chat/[A-Za-z0-9._-]+(/[A-Za-z0-9._-]+)*$")
_PAGE_HEADING_RE = re.compile(r"^## Page \d+\s*$", re.M)


class TextEmitter(docs.Emitter):
    """An Emitter that keeps only text: tables as Markdown, images and page renders counted per page, never decoded
    or written."""

    markdown_tables = True

    def __init__(self, outdir: Path, cfg: docs.ExtractConfig):
        super().__init__(outdir, cfg)
        self.images = 0
        self.pages = 0
        self.page_chars: dict[int, int] = {}
        self.page_images: set[int] = set()

    def set_page(self, n: int) -> None:
        super().set_page(n)
        self.pages = max(self.pages, n)

    def text(self, s: str) -> None:
        super().text(s)
        if self.page:
            self.page_chars[self.page] = self.page_chars.get(self.page, 0) + len("".join(s.split()))

    def image(self, blob: bytes, kind: str = "image") -> bool:
        self.images += 1
        if self.page:
            self.page_images.add(self.page)
        return False

    def render(self, page, kind: str, clip=None) -> None:
        if self.page:  # a scanned page, a figure or a table's picture: nothing to keep in text-only output
            self.page_images.add(self.page)

    def markdown(self) -> str:
        self.flush()
        return re.sub(r"\n{3,}", "\n\n", "\n".join(self.md)).strip()


def _key_ok(key: object, suffix: tuple[str, ...]) -> bool:
    return (isinstance(key, str) and len(key) <= 512 and bool(_KEY_RE.match(key)) and ".." not in key
            and key.lower().endswith(suffix))


def check_event(event: dict) -> str | None:
    """Why the event cannot be served, or None."""
    kind = event.get("kind")
    if kind not in KINDS:
        return "kind must be pdf or docx"
    if not isinstance(event.get("bucket"), str) or not event["bucket"]:
        return "bucket is missing"
    if not _key_ok(event.get("key"), (f".{kind}",)):
        return f"key must be a chat/... .{kind} object"
    if not _key_ok(event.get("out_key"), (".json",)):
        return "out_key must be a chat/... .json object"
    return None


def _max_chars(raw: object) -> int:
    try:
        n = int(raw) if raw is not None and not isinstance(raw, bool) else MAX_CHARS
    except (TypeError, ValueError):
        n = MAX_CHARS
    return max(1000, min(MAX_CHARS, n))


def _reason(e: Exception) -> str:
    if isinstance(e, PipelineError):
        return e.message[:300]
    code = ((getattr(e, "response", None) or {}).get("Error") or {}).get("Code", "")
    if isinstance(e, KeyError) or code in ("404", "NoSuchKey", "NotFound"):
        return "file not found"
    return f"could not read the file ({type(e).__name__})"


def extract_text(path: str, kind: str, max_chars: int = MAX_CHARS) -> dict:
    """The success document for a local file; raises PipelineError for a file that is not a readable pdf/docx."""
    tmp = tempfile.mkdtemp(prefix="chat_text_")
    try:
        cfg = docs.ExtractConfig(vector_figures=False)
        em = TextEmitter(Path(tmp), cfg)
        docs.extract_into(path, kind, em, cfg)
        text = em.markdown()
    finally:
        shutil.rmtree(tmp, ignore_errors=True)
    pages = em.pages if kind == "pdf" else None
    scanned = [p for p in range(1, (pages or 0) + 1)
               if p in em.page_images and em.page_chars.get(p, 0) < SCANNED_PAGE_CHARS]
    warnings = list(em.stats.page_errors)
    if scanned:
        warnings.append(f"{len(scanned)} of {pages} page(s) are images with little or no text (scanned?).")
    if em.images:
        warnings.append(f"{em.images} embedded image(s) were not read (text only).")
    if not _PAGE_HEADING_RE.sub("", text).strip():
        warnings.append("No text was found in this file.")
    truncated = len(text) > max_chars
    if truncated:
        warnings.append(f"Text truncated to the first {max_chars} of {len(text)} characters.")
        text = text[:max_chars]
    return {"ok": True, "kind": kind, "text": text, "pages": pages, "chars": len(text), "truncated": truncated,
            "scanned_pages": scanned, "needs_native": bool(pages) and len(scanned) * 2 > pages,
            "warnings": warnings}


def fit_response(result: dict, limit: int = MAX_RESPONSE_BYTES) -> dict:
    """Shorten the text until the JSON answer fits the synchronous Lambda response limit."""
    for _ in range(6):
        size = len(json.dumps(result))
        if size <= limit or not result.get("text"):
            break
        keep = int(len(result["text"]) * limit / size * 0.95)
        result["text"] = result["text"][:keep]
        result["chars"], result["truncated"] = keep, True
        note = "Text shortened to fit the response size limit."
        if note not in result["warnings"]:
            result["warnings"].append(note)
    return result


def run_chat_extract(event: dict, store=None, tmp_root: str | None = None) -> dict:
    """The chat_extract op. Never raises."""
    problem = check_event(event)
    if problem:
        return {"ok": False, "error": problem}  # nothing is written: out_key itself may be the bad part
    kind = event["kind"]
    tmp = tempfile.mkdtemp(prefix="chat_", dir=tmp_root)
    try:
        store = store or S3Store(event["bucket"])
        head = store.head(event["key"])
        if head is None:
            result = {"ok": False, "error": "file not found"}
        elif head["size"] > MAX_BYTES:
            result = {"ok": False, "error": f"file is larger than {MAX_BYTES // (1024 * 1024)} MB"}
        else:
            src = os.path.join(tmp, f"input.{kind}")
            store.download(event["key"], src)
            result = fit_response(extract_text(src, kind, _max_chars(event.get("max_chars"))))
    except Exception as e:  # PipelineError (not a pdf, password protected, too many pages, ...) or S3 trouble
        result = {"ok": False, "error": _reason(e)}
    finally:
        shutil.rmtree(tmp, ignore_errors=True)
    try:
        store.put_json(event["out_key"], result)
    except Exception as e:  # the answer still goes back to the caller
        print(f"    could not write {event['out_key']}: {type(e).__name__}")
        if result.get("ok"):
            result["warnings"].append("The result could not be saved to storage.")
    return result
