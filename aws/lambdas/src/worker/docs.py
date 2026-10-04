"""PDF / DOCX extraction in reading order (ported from scripts/extract_assets.py) and the ExtractDoc step.

Output of one document (local dir, then uploaded):
    content.md                      text + image references, page headings ("## Page N")
    NNNN_pNN_image.jpg / _page.jpg / _figure.jpg
"""
from __future__ import annotations

import hashlib
import io
import os
import re
import shutil
import tempfile
from dataclasses import dataclass, field
from pathlib import Path

from .errors import PipelineError
from .progress import Progress
from .security import check_docx, check_pdf_head
from .store import derived

MC_FALLBACK = "{http://schemas.openxmlformats.org/markup-compatibility/2006}Fallback"
A_BLIP = "{http://schemas.openxmlformats.org/drawingml/2006/main}blip"
V_IMAGEDATA = "{urn:schemas-microsoft-com:vml}imagedata"

MAX_PAGES = 600
MAX_OUTPUT_BYTES = 400_000_000  # total bytes of images written for one document (zip-bomb style inputs)
MAX_RENDER_PIXELS = 60_000_000
MAX_IMAGES_ANALYSED = 150  # per document; the rest are listed in a warning


@dataclass
class ExtractConfig:
    dpi: int = 150
    min_px: int = 40
    jpg_quality: int = 92
    flatten_threshold: float = 0.8
    vector_figures: bool = True
    max_output_bytes: int = MAX_OUTPUT_BYTES
    max_pages: int = MAX_PAGES


@dataclass
class Stats:
    text: int = 0
    images: int = 0
    pages_rendered: int = 0
    figures: int = 0
    dup_skipped: int = 0
    small_skipped: int = 0
    undecodable: int = 0
    page_errors: list = field(default_factory=list)


def to_jpg(img, quality: int) -> bytes:
    from PIL import Image

    if img.mode in ("RGBA", "LA") or (img.mode == "P" and "transparency" in img.info):
        rgba = img.convert("RGBA")
        bg = Image.new("RGB", rgba.size, "white")
        bg.paste(rgba, mask=rgba.split()[3])
        img = bg
    elif img.mode != "RGB":  # CMYK, grayscale, 16-bit, palette, 1-bit
        img = img.convert("RGB")
    buf = io.BytesIO()
    img.save(buf, "JPEG", quality=quality)
    return buf.getvalue()


class Emitter:
    """Writes numbered text/image files and a content.md, preserving the order of calls."""

    def __init__(self, outdir: Path, cfg: ExtractConfig):
        outdir.mkdir(parents=True, exist_ok=True)
        self.out, self.cfg = outdir, cfg
        self.seq = 0
        self.page: int | None = None
        self.buf: list[str] = []
        self.md: list[str] = []
        self.seen: set[str] = set()
        self.bytes_written = 0
        self.stats = Stats()

    def _name(self, kind: str, ext: str) -> str:
        self.seq += 1
        pg = f"_p{self.page:02d}" if self.page else ""
        return f"{self.seq:04d}{pg}_{kind}.{ext}"

    def set_page(self, n: int) -> None:
        self.flush()
        self.page = n
        self.md.append(f"\n## Page {n}\n")

    def text(self, s: str) -> None:
        self.buf.append(s)

    def flush(self) -> None:
        text = re.sub(r"[ \t]+\n", "\n", "".join(self.buf))
        text = re.sub(r"\n{3,}", "\n\n", text).strip()
        self.buf.clear()
        if text:
            self.md.append(text + "\n")
            self.stats.text += 1

    def _save(self, data: bytes, kind: str, ext: str) -> str:
        self.flush()
        self.bytes_written += len(data)
        if self.bytes_written > self.cfg.max_output_bytes:
            raise PipelineError("extract_failed", "Document expands to too much image data (limit reached)")
        name = self._name(kind, ext)
        (self.out / name).write_bytes(data)
        self.md.append(f"![{name}]({name})\n")
        return name

    def image(self, blob: bytes, kind: str = "image") -> bool:
        """Convert an embedded picture to JPG and save it. Returns True if saved."""
        from PIL import Image

        import pymupdf

        img = None
        try:
            img = Image.open(io.BytesIO(blob))
            img.load()
        except Exception:
            img = None
        if img is None:  # JPX and other formats Pillow lacks: let MuPDF decode
            try:
                pix = pymupdf.Pixmap(blob)
                img = Image.open(io.BytesIO(pix.tobytes("png")))
            except Exception:
                img = None
        if img is None:  # EMF/WMF etc. that nothing here can decode
            self.stats.undecodable += 1
            return False
        if min(img.size) < self.cfg.min_px:
            self.stats.small_skipped += 1
            return False
        jpg = to_jpg(img, self.cfg.jpg_quality)
        digest = hashlib.md5(jpg).hexdigest()
        if digest in self.seen:
            self.stats.dup_skipped += 1
            return False
        self.seen.add(digest)
        self._save(jpg, kind, "jpg")
        self.stats.images += 1
        return True

    def render(self, page, kind: str, clip=None) -> None:
        dpi = self.cfg.dpi
        rect = clip if clip is not None else page.rect
        px = rect.width * rect.height * (dpi / 72) ** 2
        if px > MAX_RENDER_PIXELS:  # absurd page size: lower the dpi instead of allocating gigabytes
            dpi = max(20, int(dpi * (MAX_RENDER_PIXELS / px) ** 0.5))
        pix = page.get_pixmap(dpi=dpi, clip=clip, alpha=False)
        self._save(pix.tobytes("jpeg", jpg_quality=self.cfg.jpg_quality), kind, "jpg")
        if kind == "page":
            self.stats.pages_rendered += 1
        else:
            self.stats.figures += 1

    def finish(self) -> None:
        self.flush()
        (self.out / "content.md").write_text("\n".join(self.md).strip() + "\n", "utf-8")


# --------------------------------------------------------------------------- PDF
def block_text(block: dict) -> str:
    lines = ("".join(span["text"] for span in line["spans"]) for line in block.get("lines", []))
    return "\n".join(lines).strip()


def table_rows_text(rows: list) -> str:
    """Table.extract() grid -> one line per row, non-empty cells joined by ' | '.
    Word-style merged cells make column positions unreliable, so no markdown grid; the
    table's picture (saved next to this text) is the faithful copy."""
    lines = []
    for r in rows:
        cells = [" ".join((c or "").split()) for c in r]
        line = " | ".join(c for c in cells if c)
        if line:
            lines.append(line)
    return "\n".join(lines) if len(lines) >= 2 else ""  # one row is just a box, not a table


def extract_pdf(path: str, em: Emitter, cfg: ExtractConfig) -> None:
    import pymupdf

    doc = pymupdf.open(path)
    if doc.needs_pass and not doc.authenticate(""):
        raise PipelineError("extract_failed", "PDF is password protected")
    if doc.page_count > cfg.max_pages:
        raise PipelineError("extract_failed", f"PDF has {doc.page_count} pages (limit {cfg.max_pages})")
    for pno, page in enumerate(doc, start=1):
        em.set_page(pno)
        try:
            pdf_page(page, em, cfg)
        except PipelineError:
            raise
        except Exception as e:  # never lose a page: fall back to a picture of it
            em.stats.page_errors.append(f"page {pno}: {type(e).__name__}: {str(e)[:80]}")
            em.buf.clear()
            em.render(page, "page")
    doc.close()


def pdf_page(page, em: Emitter, cfg: ExtractConfig) -> None:
    import pymupdf

    prect = page.rect
    parea = max(prect.get_area(), 1.0)
    blocks = page.get_text("dict", sort=True)["blocks"]
    text_blocks = [b for b in blocks if b["type"] == 0 and block_text(b)]
    img_blocks = [b for b in blocks if b["type"] == 1]

    # scanned / flattened page: pictures cover (almost) the whole page -> render it whole
    covered = sum((pymupdf.Rect(b["bbox"]) & prect).get_area() for b in img_blocks)
    if covered / parea >= cfg.flatten_threshold:
        em.render(page, "page")
        for b in text_blocks:  # keep an OCR text layer if the scan has one
            em.text(block_text(b) + "\n\n")
        return

    # tables -> picture + row text; their cells are dropped from the plain text flow
    tables: list[tuple] = []
    try:
        for t in page.find_tables().tables:
            md = table_rows_text(t.extract())
            if md:
                tables.append((pymupdf.Rect(t.bbox), md))
    except Exception:
        tables = []

    def in_table(r) -> bool:
        return any((r & tr).get_area() >= 0.5 * max(r.get_area(), 1e-6) for tr, _ in tables)

    text_blocks = [b for b in text_blocks if not in_table(pymupdf.Rect(b["bbox"]))]

    figures: list = []
    if cfg.vector_figures:
        try:
            for r in page.cluster_drawings():
                r = r & prect
                big_enough = r.get_area() >= 0.03 * parea and r.width >= 60 and r.height >= 60
                if not big_enough or r.get_area() >= 0.85 * parea:  # skip page backgrounds / borders
                    continue
                on_bitmap = sum((r & pymupdf.Rect(b["bbox"])).get_area() for b in img_blocks)
                if on_bitmap > 0.5 * r.get_area():  # already a picture, don't render it twice
                    continue
                if any((r & tr).get_area() >= 0.5 * r.get_area() for tr, _ in tables):  # a table, not a diagram
                    continue
                figures.append(r)
        except Exception:
            figures = []

    items: list[tuple] = []
    for b in text_blocks:
        items.append((b["bbox"][1], b["bbox"][0], "text", block_text(b)))
    for b in img_blocks:
        r = pymupdf.Rect(b["bbox"])
        if any((r & f).get_area() >= 0.9 * r.get_area() for f in figures):
            continue  # part of a rendered figure
        items.append((r.y0, r.x0, "image", b))
    for f in figures:
        items.append((f.y0, f.x0, "figure", f))
    for tr, txt in tables:  # picture of the table, then its rows as text
        items.append((tr.y0, tr.x0, "figure", tr))
        items.append((tr.y0, tr.x0 + 0.001, "text", txt))
    items.sort(key=lambda t: (t[0], t[1]))

    for _, _, kind, payload in items:
        if kind == "text":
            em.text(payload + "\n\n")
        elif kind == "image":
            em.image(payload["image"])
        else:
            em.render(page, "figure", clip=payload + (-4, -4, 4, 4))


# --------------------------------------------------------------------------- DOCX
def extract_docx(path: str, em: Emitter, cfg: ExtractConfig) -> None:
    from docx import Document
    from docx.oxml.ns import qn

    doc = Document(path)
    related = doc.part.related_parts
    depth = {"cell": 0}

    def picture(rid: str | None) -> None:
        part = related.get(rid) if rid else None
        if part is not None:
            em.image(part.blob)

    def walk(el) -> None:
        tag = el.tag
        if tag == MC_FALLBACK:  # duplicate of the mc:Choice branch
            return
        if tag == qn("w:t"):
            em.text(el.text or "")
        elif tag == qn("w:tab"):
            em.text("\t")
        elif tag in (qn("w:br"), qn("w:cr")):
            em.text("\n")
        elif tag == qn("w:noBreakHyphen"):
            em.text("-")
        elif tag == A_BLIP:
            picture(el.get(qn("r:embed")))
        elif tag == V_IMAGEDATA:
            picture(el.get(qn("r:id")))
        elif tag == qn("w:p"):
            for child in el:
                walk(child)
            em.text(" " if depth["cell"] else "\n")
        elif tag == qn("w:tc"):
            depth["cell"] += 1
            for child in el:
                walk(child)
            depth["cell"] -= 1
            em.text("\t")
        elif tag == qn("w:tr"):
            for child in el:
                walk(child)
            em.text("\n")
        else:
            for child in el:
                walk(child)

    for child in doc.element.body:
        walk(child)


MAX_TEXT_BYTES = 3_000_000
TEXT_PAGE_CHARS = 3500


def extract_text(path: str, em: "Emitter", cfg: ExtractConfig) -> None:
    """A .md / .txt solution document: split into page-sized chunks on paragraph boundaries so the
    corpus has the same shape (one section per 'page') as a PDF or DOCX."""
    with open(path, "rb") as fh:
        raw = fh.read(MAX_TEXT_BYTES + 1)
    if b"\x00" in raw[:4096]:
        raise PipelineError("unsupported_type", "File is not a text document")
    if len(raw) > MAX_TEXT_BYTES:
        raw = raw[:MAX_TEXT_BYTES]
        em.stats.page_errors.append("Text file truncated to the first 3 MB.")
    text = raw.decode("utf-8", errors="replace").replace("\r\n", "\n")
    page, buf = 1, ""
    em.set_page(page)
    for para in re.split(r"\n\s*\n", text):
        if buf and len(buf) + len(para) > TEXT_PAGE_CHARS:
            em.text(buf)
            page += 1
            em.set_page(page)
            buf = ""
        buf += para.strip("\n") + "\n\n"
    em.text(buf)


def extract_file(path: str, kind: str, outdir: Path, cfg: ExtractConfig | None = None) -> Stats:
    """Validate magic bytes, extract one pdf/docx into `outdir`. Raises PipelineError."""
    cfg = cfg or ExtractConfig()
    em = Emitter(outdir, cfg)
    try:
        if kind == "pdf":
            with open(path, "rb") as fh:
                if not check_pdf_head(fh.read(1024)):
                    raise PipelineError("unsupported_type", "File is not a valid PDF")
            extract_pdf(path, em, cfg)
        elif kind == "docx":
            check_docx(path)
            extract_docx(path, em, cfg)
        elif kind == "text":
            extract_text(path, em, cfg)
        else:
            raise PipelineError("unsupported_type", f"Cannot extract {kind}")
    except PipelineError:
        raise
    except Exception as e:
        raise PipelineError("extract_failed", f"{type(e).__name__}: {str(e)[:150]}") from e
    em.finish()
    return em.stats


# --------------------------------------------------------------------------- the ExtractDoc step
def run_extract_doc(event: dict, store, tmp_root: str | None = None) -> dict:
    eid, f = event["evaluation_id"], event["file"]
    sid, name, kind = f["source_id"], f.get("original_name", f["source_id"]), f["kind"]
    prog = Progress(store, eid)
    prefix = derived(eid, "docs", sid)
    prog.file(sid, name, "running", "extracting text and images")
    tmp = tempfile.mkdtemp(prefix="extract_", dir=tmp_root)
    try:
        src = os.path.join(tmp, "input." + kind)
        store.download(f["raw_key"], src)
        outdir = Path(tmp) / "out"
        stats = extract_file(src, kind, outdir)
        warnings: list[str] = list(stats.page_errors)
        jpgs = sorted(p for p in outdir.glob("*.jpg"))
        if len(jpgs) > MAX_IMAGES_ANALYSED:
            warnings.append(f"{name}: {len(jpgs)} images found; only the first {MAX_IMAGES_ANALYSED} were analysed.")
        keys = []
        for p in jpgs:
            key = f"{prefix}/images/{p.name}"
            store.upload(str(p), key, "image/jpeg")
            if len(keys) < MAX_IMAGES_ANALYSED:
                keys.append(key)
        store.upload(str(outdir / "content.md"), f"{prefix}/content.md", "text/markdown; charset=utf-8")
        store.put_json(f"{prefix}/extract.json", {
            "stats": {k: v for k, v in vars(stats).items() if k != "page_errors"}, "warnings": warnings,
            "images_analysed": len(keys)})
        if keys:
            prog.file(sid, name, "running", "", images_total=len(keys))
        else:
            prog.file(sid, name, "done", "no images" if not stats.images else "")
        return {"source_id": sid, "images": keys, "stats": {"images": stats.images, "text_blocks": stats.text}}
    except PipelineError as e:
        store.put_json(f"{prefix}/extract.json", {"error": e.message, "code": e.code, "warnings": []})
        prog.file(sid, name, "failed", e.message[:200])
        return {"source_id": sid, "images": [], "error": e.message}
    finally:
        shutil.rmtree(tmp, ignore_errors=True)
