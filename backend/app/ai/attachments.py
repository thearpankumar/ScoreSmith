"""Chat attachments: which files the chat accepts, how each is turned into text ONCE (when its upload completes), and
how that text reaches the models within a token budget.

Extraction (`extract_*`) runs per file type:
- **PDF / DOCX**: in the extraction Lambda (`chat_extract` mode, app/pipeline/aws_jobs.py `invoke_function`), never in
  this process: uploaded PDF / DOCX are only ever parsed inside Lambda (docs/ai-features.md, security). A PDF whose
  pages are mostly images (a scan) is flagged `needs_native`: the chat model is then also sent the file itself.
- **XLSX / CSV**: here (openpyxl read-only, as batch sheets already are). Every visible sheet becomes its size, a
  column profile (type, filled, distinct, range or top values) and its rows as `- a | b | c` lines; past the budget
  only the first and last rows are kept, with an explicit note of the rows left out.
- **Images**: described once by the builder's (vision-capable) model, so the text-only builder can use them; the chat
  model also gets the image itself.
- **TXT / MD**: as they are.

Use: the message that carries files reaches the graph with an `<attached_files>` block appended
(`compose_message`, PIPELINE_BUDGET_TOKENS), so the builder can e.g. build a scorecard from an SOP or pick a KPI list
out of a spreadsheet. The chat assistant additionally gets an `AttachmentContext`: every file of the chat with
`read()` for the parts a block left out, and the native image / scanned-PDF blocks of the current message. Blocks
older than the last two user messages are collapsed to a one-line stub (`collapse_old_blocks`), so a long chat does
not keep paying for old files.
"""

from __future__ import annotations

import csv
import io
import logging
import re
from collections import Counter
from dataclasses import dataclass, field
from datetime import date, datetime, time
from typing import Any

logger = logging.getLogger(__name__)

# extension -> kind (chat_attachments.kind)
ATTACHMENT_EXTENSIONS: dict[str, str] = {
    "pdf": "pdf", "docx": "docx", "xlsx": "xlsx", "csv": "csv", "txt": "text", "md": "text",
    "png": "image", "jpg": "image", "jpeg": "image", "webp": "image",
}
IMAGE_FORMATS = {"png": "png", "jpg": "jpeg", "jpeg": "jpeg", "webp": "webp"}
CONTENT_TYPES = {
    "pdf": "application/pdf",
    "docx": "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
    "xlsx": "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
    "csv": "text/csv", "txt": "text/plain", "md": "text/markdown",
    "png": "image/png", "jpg": "image/jpeg", "jpeg": "image/jpeg", "webp": "image/webp",
}

CHARS_PER_TOKEN = 4  # the usual rough estimate; only used for budgets
MAX_EXTRACTED_CHARS = 400_000  # what is stored per file (the Lambda is asked for the same)
SPREADSHEET_BUDGET_CHARS = 160_000  # rows kept per workbook, split across its sheets
MAX_SHEET_ROWS_SCANNED = 50_000
MAX_CELL_CHARS = 300
MAX_PROFILE_COLUMNS = 40
PIPELINE_BUDGET_TOKENS = 15_000  # the block appended to the message (every builder call re-reads it)
READ_CHUNK_CHARS = 30_000  # one read_attachment call
NATIVE_PDF_MAX_PAGES = 30  # a scanned PDF longer than this goes as text only
NATIVE_MAX_BYTES = 15 * 1024 * 1024

IMAGE_PROMPT = (
    "Describe this image for an assistant that designs quality scorecards and evaluates evidence. First transcribe "
    "all visible text exactly (headings, labels, table cells, numbers). Then describe any chart, table or diagram "
    "with its values, and finally summarise in one or two sentences what the image shows. Only state what is "
    "visible."
)


def kind_of(filename: str) -> str | None:
    base = filename.replace("\\", "/").rsplit("/", 1)[-1]
    ext = base.rsplit(".", 1)[-1].lower() if "." in base else ""
    return ATTACHMENT_EXTENSIONS.get(ext)


def extension_of(filename: str) -> str:
    base = filename.replace("\\", "/").rsplit("/", 1)[-1]
    return base.rsplit(".", 1)[-1].lower() if "." in base else ""


def magic_ok(ext: str, head: bytes) -> bool:
    """Cheap content sniffing on the first bytes of the stored object (same idea as pipeline/uploads.magic_ok)."""
    if ext == "png":
        return head.startswith(b"\x89PNG\r\n\x1a\n")
    if ext in ("jpg", "jpeg"):
        return head.startswith(b"\xff\xd8\xff")
    if ext == "webp":
        return len(head) >= 12 and head[:4] == b"RIFF" and head[8:12] == b"WEBP"
    from app.pipeline.uploads import magic_ok as upload_magic_ok

    return upload_magic_ok(ext, head)


def estimate_tokens(text: str | None) -> int:
    return (len(text or "") + CHARS_PER_TOKEN - 1) // CHARS_PER_TOKEN


@dataclass
class Extracted:
    text: str
    outline: dict[str, Any] = field(default_factory=dict)
    needs_native: bool = False


# --- spreadsheets -------------------------------------------------------------------------------------------------


def _cell(value: Any) -> str:
    if value is None:
        return ""
    if isinstance(value, datetime):
        return value.isoformat(sep=" ", timespec="minutes") if value.time() != time(0) else value.date().isoformat()
    if isinstance(value, date | time):
        return value.isoformat()
    if isinstance(value, float):
        return f"{value:.6g}"
    text = re.sub(r"\s+", " ", str(value)).strip()
    return text[:MAX_CELL_CHARS] + ("…" if len(text) > MAX_CELL_CHARS else "")


def _number(text: str) -> float | None:
    try:
        return float(text.replace(",", ""))
    except ValueError:
        return None


def _profile(header: list[str], body: list[list[str]]) -> list[str]:
    """One line per column: type, how many cells are filled, distinct values, and a range or the top values."""
    width = max([len(header), *(len(r) for r in body)] or [0])
    lines: list[str] = []
    for c in range(min(width, MAX_PROFILE_COLUMNS)):
        name = header[c] if c < len(header) and header[c] else f"column {c + 1}"
        values = [r[c] for r in body if c < len(r) and r[c] != ""]
        if not values:
            lines.append(f"- {name}: empty")
            continue
        numbers = [n for n in (_number(v) for v in values) if n is not None]
        distinct = Counter(values)
        if len(numbers) == len(values):
            detail = f"number, min {min(numbers):g}, max {max(numbers):g}"
        elif all(re.fullmatch(r"\d{4}-\d{2}-\d{2}.*", v) for v in values):
            detail = f"date, {min(values)[:10]} to {max(values)[:10]}"
        elif len(distinct) <= 12:
            detail = "text, values: " + ", ".join(f"{v} ({n})" for v, n in distinct.most_common(6))
        else:
            detail = "text"
        lines.append(f"- {name}: {detail}; {len(values)} filled, {len(distinct)} distinct")
    if width > MAX_PROFILE_COLUMNS:
        lines.append(f"- ... and {width - MAX_PROFILE_COLUMNS} more columns")
    return lines


def _render_rows(title: str, rows: list[list[str]], budget_chars: int) -> tuple[str, dict[str, Any]]:
    header, body = (rows[0], rows[1:]) if rows else ([], [])
    width = max([len(header), *(len(r) for r in body)] or [0])
    lines = [f"## {title} ({len(body)} data rows x {width} columns)"]
    if header:
        lines.append("Columns: " + " | ".join(h or f"column {i + 1}" for i, h in enumerate(header)))
    lines.append("Column profile:")
    lines.extend(_profile(header, body))
    lines.append("Rows:")
    rendered = ["- " + " | ".join(r) for r in body]
    used = sum(len(line) + 1 for line in lines)
    omitted: list[int] | None = None
    if used + sum(len(r) + 1 for r in rendered) <= budget_chars:
        lines.extend(rendered)
    else:
        room = max(budget_chars - used, 0)
        head: list[str] = []
        size = 0
        for r in rendered:  # the first rows get two thirds of the room ...
            if size + len(r) + 1 > room * 2 // 3:
                break
            head.append(r)
            size += len(r) + 1
        tail: list[str] = []
        for r in reversed(rendered[len(head):]):  # ... the last rows the rest
            if size + len(r) + 1 > room:
                break
            tail.insert(0, r)
            size += len(r) + 1
        first_omitted, last_omitted = len(head) + 1, len(rendered) - len(tail)
        omitted = [first_omitted, last_omitted]
        lines.extend(head)
        lines.append(f"[rows {first_omitted}-{last_omitted} of {len(rendered)} omitted to fit; ask to read them]")
        lines.extend(tail)
    return "\n".join(lines), {"name": title, "rows": len(body), "columns": width, "omitted_rows": omitted}


def extract_spreadsheet(data: bytes, budget_chars: int = SPREADSHEET_BUDGET_CHARS) -> Extracted:
    import openpyxl

    workbook = openpyxl.load_workbook(io.BytesIO(data), read_only=True, data_only=True)
    try:
        sheets = [ws for ws in workbook.worksheets if getattr(ws, "sheet_state", "visible") == "visible"]
        share = max(8_000, budget_chars // max(len(sheets), 1))
        parts: list[str] = []
        outline: list[dict[str, Any]] = []
        for ws in sheets:
            rows: list[list[str]] = []
            for raw in ws.iter_rows(values_only=True):
                cells = [_cell(v) for v in raw]
                while cells and cells[-1] == "":
                    cells.pop()
                if cells:
                    rows.append(cells)
                if len(rows) >= MAX_SHEET_ROWS_SCANNED:
                    break
            text, info = _render_rows(f"Sheet: {ws.title}", rows, share)
            parts.append(text)
            outline.append(info)
    finally:
        workbook.close()
    truncated = any(s["omitted_rows"] for s in outline)
    return Extracted(text="\n\n".join(parts), outline={"sheets": outline, "truncated": truncated})


def _decode(data: bytes) -> str:
    for encoding in ("utf-8-sig", "cp1252"):
        try:
            return data.decode(encoding)
        except UnicodeDecodeError:
            continue
    return data.decode("utf-8", errors="replace")


def extract_csv(data: bytes, budget_chars: int = SPREADSHEET_BUDGET_CHARS) -> Extracted:
    text = _decode(data)
    try:
        dialect: Any = csv.Sniffer().sniff(text[:4096], delimiters=",;\t|")
    except csv.Error:
        dialect = csv.excel
    rows: list[list[str]] = []
    for raw in csv.reader(io.StringIO(text), dialect):
        cells = [_cell(v) for v in raw]
        while cells and cells[-1] == "":
            cells.pop()
        if cells:
            rows.append(cells)
        if len(rows) >= MAX_SHEET_ROWS_SCANNED:
            break
    rendered, info = _render_rows("Table", rows, budget_chars)
    return Extracted(text=rendered, outline={"sheets": [info], "truncated": bool(info["omitted_rows"])})


def extract_plain(data: bytes) -> Extracted:
    text = _decode(data)
    truncated = len(text) > MAX_EXTRACTED_CHARS
    return Extracted(text=text[:MAX_EXTRACTED_CHARS], outline={"chars": len(text), "truncated": truncated})


def from_lambda_result(result: dict[str, Any]) -> Extracted:
    """The extraction Lambda's `chat_extract` answer -> Extracted. Raises ValueError with its reason when it failed."""
    if not isinstance(result, dict) or not result.get("ok"):
        reason = str((result or {}).get("error") or "the file could not be read") if isinstance(result, dict) else ""
        raise ValueError(reason or "the file could not be read")
    text = str(result.get("text") or "")[:MAX_EXTRACTED_CHARS]
    pages = result.get("pages")
    outline = {
        "pages": pages if isinstance(pages, int) else None,
        "chars": result.get("chars"),
        "truncated": bool(result.get("truncated")),
        "scanned_pages": list(result.get("scanned_pages") or [])[:200],
    }
    needs_native = bool(result.get("needs_native"))
    if not text.strip() and not needs_native:
        raise ValueError("no readable text was found in the file")
    return Extracted(text=text, outline=outline, needs_native=needs_native)


def describe_image(client: Any, model_id: str | None, data: bytes, ext: str) -> Extracted:
    """One vision call on the builder's model; synchronous (callers use asyncio.to_thread)."""
    result = client.converse(
        messages=[
            {
                "role": "user",
                "content": [
                    {"text": IMAGE_PROMPT},
                    {"image": {"format": IMAGE_FORMATS.get(ext, "png"), "source": {"bytes": data}}},
                ],
            }
        ],
        model_id=model_id,
    )
    text = (result.text or "").strip()
    if not text:
        raise ValueError("the image could not be described")
    return Extracted(text=text[:MAX_EXTRACTED_CHARS], outline={"described": True})


# --- use in prompts -----------------------------------------------------------------------------------------------


@dataclass
class AttachmentView:
    """What the models need of one ready attachment (no ORM object crosses into the graph)."""

    id: str
    filename: str
    kind: str
    text: str
    outline: dict[str, Any] = field(default_factory=dict)
    needs_native: bool = False


def _describe(view: AttachmentView) -> str:
    o = view.outline or {}
    if o.get("sheets"):
        return ", ".join(f"sheet {s.get('name')}: {s.get('rows')} rows" for s in o["sheets"][:6])
    if o.get("pages"):
        return f"{o['pages']} pages" + (", scanned" if view.needs_native else "")
    return view.kind


def render_block(views: list[AttachmentView], budget_tokens: int) -> str:
    """The `<attached_files>` block for `views`, the budget shared fairly (a short file leaves its unused share to the
    others); a file cut to fit says how much is missing and the offset to read from."""
    if not views:
        return ""
    budget = budget_tokens * CHARS_PER_TOKEN
    remaining = len(views)
    parts = ["<attached_files>"]
    for view in sorted(views, key=lambda v: len(v.text)):
        share = budget // remaining
        text = view.text or ""
        if len(text) > share:
            cut = text.rfind("\n", 0, share)
            cut = cut if cut > share // 2 else share
            text = f"{text[:cut]}\n[... {len(view.text) - cut} more characters not included (offset {cut})]"
        budget -= min(len(view.text or ""), share)
        remaining -= 1
        name = view.filename.replace('"', "'")
        parts.append(f'<file id="{view.id}" name="{name}" type="{view.kind}">\n{text}\n</file>')
    parts.append("</attached_files>")
    return "\n".join(parts)


def compose_message(message: str, views: list[AttachmentView]) -> str:
    """The user's message as the graph sees it: unchanged without files, else with the files' block appended."""
    if not views:
        return message
    return f"{message}\n\n{render_block(views, PIPELINE_BUDGET_TOKENS)}"


_BLOCK = re.compile(r"\n*<attached_files>.*?</attached_files>", re.DOTALL)
_NAME = re.compile(r'<file id="[^"]*" name="([^"]*)"')


def collapse_old_blocks(messages: list[dict[str, Any]], keep_last_user_turns: int = 2) -> list[dict[str, Any]]:
    """`messages` (ChatTurns) with the attachment blocks of all but the last `keep_last_user_turns` user messages
    replaced by a one-line stub naming the files."""
    user_positions = [i for i, m in enumerate(messages) if m.get("role") == "user"]
    old = set(user_positions[:-keep_last_user_turns]) if keep_last_user_turns else set(user_positions)
    out: list[dict[str, Any]] = []
    for i, m in enumerate(messages):
        content = m.get("content")
        if i in old and isinstance(content, str) and "<attached_files>" in content:
            names = ", ".join(_NAME.findall(content)) or "files"
            content = _BLOCK.sub(f"\n\n[attached earlier: {names}; their text is no longer shown here]", content)
            m = {**m, "content": content}
        out.append(m)
    return out


@dataclass
class AttachmentContext:
    """The chat assistant's view of the chat's files: `files` (every ready attachment, oldest first), the ids sent with
    the current message, and the native image / scanned-PDF blocks of the current message (OpenRouter only)."""

    files: list[AttachmentView]
    current_ids: list[str] = field(default_factory=list)
    native_blocks: list[dict[str, Any]] = field(default_factory=list)

    def summary(self) -> str:
        lines = []
        for v in self.files:
            marker = " (sent with the latest message)" if v.id in self.current_ids else ""
            lines.append(f'- id "{v.id}": {v.filename} ({_describe(v)}, ~{estimate_tokens(v.text)} tokens){marker}')
        return "\n".join(lines)

    def read(self, attachment_id: str, offset: int = 0, limit: int = READ_CHUNK_CHARS) -> str:
        view = next((v for v in self.files if v.id == str(attachment_id).strip()), None)
        if view is None:
            return f"No attachment with id {attachment_id!r} in this chat."
        text = view.text or ""
        offset = max(0, int(offset or 0))
        limit = max(1000, min(int(limit or READ_CHUNK_CHARS), READ_CHUNK_CHARS))
        chunk = text[offset: offset + limit]
        if not chunk:
            return f"{view.filename}: nothing at offset {offset} (the text is {len(text)} characters long)."
        end = offset + len(chunk)
        more = f"\n[{len(text) - end} more characters: read again with offset {end}]" if end < len(text) else ""
        return f"{view.filename}, characters {offset}-{end} of {len(text)}:\n{chunk}{more}"
