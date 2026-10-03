#!/usr/bin/env -S uv run --script
# /// script
# requires-python = ">=3.10"
# dependencies = ["pymupdf>=1.24.2", "python-docx>=1.1", "pillow>=10"]
# ///
"""Extract text + images, in reading order, from one student's PDF / DOCX submissions.

Usage (one email id per run - it is required):
    uv run scripts/extract_assets.py am8131
    uv run scripts/extract_assets.py am8131@srmist.edu.in --dpi 200
    uv run scripts/extract_assets.py am8131 --clean       # delete that student's extracted_assets

Reads every top-level .pdf / .docx in GdriveDownload/<id>/ and writes

    GdriveDownload/<id>/extracted_assets/<document name>/
        0001_p01_text.txt        <- numbering is the reading order across text AND images
        0002_p01_image.jpg
        0003_p02_page.jpg        <- page that is a scan / flattened image, rendered whole
        0004_p03_figure.jpg      <- vector diagram (drawn with lines/shapes, not a bitmap)
        content.md               <- all of it stitched together in order

If a pdf and a docx share the same name the folders become "<name>-pdf" and "<name>-docx".
Re-running regenerates each document's folder from scratch.

How it works
  PDF   PyMuPDF. page.get_text("dict", sort=True) yields text blocks and image blocks in
        position order, so text and images interleave correctly. A page is rendered whole to
        JPG when images cover >= 80% of it (scanned / flattened pages; any OCR text layer is
        still saved). Tables (page.find_tables) become a picture plus row-per-line text. Vector diagrams are found with
        page.cluster_drawings() and rendered as figures. Images are converted to RGB JPG (CMYK, transparency, JPX handled);
        tiny images and exact duplicates (logos on every page) are skipped.
  DOCX  python-docx + a document-order walk of the XML: text, tabs, tables, text boxes, and
        both inline and floating (anchored) pictures. Floating pictures land at their anchor
        paragraph, which can be a step or two off the visual position. Charts / SmartArt /
        header-footer content are not extracted.
"""
from __future__ import annotations

import argparse
import hashlib
import io
import re
import shutil
import sys
from pathlib import Path

import pymupdf
from docx import Document
from docx.oxml.ns import qn
from PIL import Image

HERE = Path(__file__).resolve().parent
OUT_DIR = "extracted_assets"
MC_FALLBACK = "{http://schemas.openxmlformats.org/markup-compatibility/2006}Fallback"
A_BLIP = "{http://schemas.openxmlformats.org/drawingml/2006/main}blip"
V_IMAGEDATA = "{urn:schemas-microsoft-com:vml}imagedata"


def safe_name(name: str) -> str:
    name = re.sub(r'[<>:"/\\|?*\x00-\x1f]', "_", name).strip(" .")
    return name or "unnamed"


# --------------------------------------------------------------------------- output
class Emitter:
    """Writes numbered text/image files and a content.md, preserving the order of calls."""

    def __init__(self, outdir: Path, args):
        outdir.mkdir(parents=True, exist_ok=True)
        self.out, self.args = outdir, args
        self.seq = 0
        self.page: int | None = None
        self.buf: list[str] = []
        self.md: list[str] = []
        self.seen: set[str] = set()
        self.stats = dict(text=0, images=0, pages_rendered=0, figures=0, dup_skipped=0, small_skipped=0)

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
        if not text:
            return
        name = self._name("text", "txt")
        (self.out / name).write_text(text + "\n", "utf-8")
        self.md.append(text + "\n")
        self.stats["text"] += 1

    def _save(self, data: bytes, kind: str, ext: str) -> str:
        self.flush()
        name = self._name(kind, ext)
        (self.out / name).write_bytes(data)
        self.md.append(f"![{name}]({name})\n")
        return name

    def image(self, blob: bytes, ext_hint: str = "bin", kind: str = "image") -> bool:
        """Convert an embedded picture to JPG and save it. Returns True if saved."""
        jpg = None
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
        if img is None:  # EMF/WMF etc. that nothing here can decode: keep the raw file
            self._save(blob, kind, ext_hint.lstrip(".") or "bin")
            self.stats["images"] += 1
            return True
        if min(img.size) < self.args.min_px:
            self.stats["small_skipped"] += 1
            return False
        jpg = to_jpg(img, self.args.jpg_quality)
        digest = hashlib.md5(jpg).hexdigest()
        if digest in self.seen:
            self.stats["dup_skipped"] += 1
            return False
        self.seen.add(digest)
        self._save(jpg, kind, "jpg")
        self.stats["images"] += 1
        return True

    def render(self, page: "pymupdf.Page", kind: str, clip=None) -> None:
        pix = page.get_pixmap(dpi=self.args.dpi, clip=clip, alpha=False)
        self._save(pix.tobytes("jpeg", jpg_quality=self.args.jpg_quality), kind, "jpg")
        self.stats["pages_rendered" if kind == "page" else "figures"] += 1

    def finish(self) -> None:
        self.flush()
        (self.out / "content.md").write_text("\n".join(self.md).strip() + "\n", "utf-8")


def to_jpg(img: Image.Image, quality: int) -> bytes:
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


# --------------------------------------------------------------------------- PDF
def block_text(block: dict) -> str:
    lines = ("".join(span["text"] for span in line["spans"]) for line in block.get("lines", []))
    return "\n".join(lines).strip()


def table_rows_text(rows: list[list[str | None]]) -> str:
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


def extract_pdf(path: Path, em: Emitter, args) -> None:
    doc = pymupdf.open(path)
    if doc.needs_pass and not doc.authenticate(""):
        raise RuntimeError("PDF is password protected")
    for pno, page in enumerate(doc, start=1):
        em.set_page(pno)
        try:
            pdf_page(page, em, args)
        except Exception as e:  # never lose a page: fall back to a picture of it
            print(f"    page {pno}: {type(e).__name__}: {e}; saving page render instead")
            em.buf.clear()
            em.render(page, "page")
    doc.close()


def pdf_page(page: "pymupdf.Page", em: Emitter, args) -> None:
    prect = page.rect
    parea = max(prect.get_area(), 1.0)
    blocks = page.get_text("dict", sort=True)["blocks"]
    text_blocks = [b for b in blocks if b["type"] == 0 and block_text(b)]
    img_blocks = [b for b in blocks if b["type"] == 1]

    # scanned / flattened page: pictures cover (almost) the whole page -> render it whole
    covered = sum((pymupdf.Rect(b["bbox"]) & prect).get_area() for b in img_blocks)
    if covered / parea >= args.flatten_threshold:
        em.render(page, "page")
        for b in text_blocks:  # keep an OCR text layer if the scan has one
            em.text(block_text(b) + "\n\n")
        return

    # tables -> picture + row text; their cells are dropped from the plain text flow
    tables: list[tuple[pymupdf.Rect, str]] = []
    try:
        for t in page.find_tables().tables:
            md = table_rows_text(t.extract())
            if md:
                tables.append((pymupdf.Rect(t.bbox), md))
    except Exception:
        tables = []

    def in_table(r: "pymupdf.Rect") -> bool:
        return any((r & tr).get_area() >= 0.5 * max(r.get_area(), 1e-6) for tr, _ in tables)

    text_blocks = [b for b in text_blocks if not in_table(pymupdf.Rect(b["bbox"]))]

    figures: list[pymupdf.Rect] = []
    if args.vector_figures:
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

    items: list[tuple[float, float, str, object]] = []
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
            em.image(payload["image"], payload.get("ext", "bin"))
        else:
            em.render(page, "figure", clip=payload + (-4, -4, 4, 4))


# --------------------------------------------------------------------------- DOCX
def extract_docx(path: Path, em: Emitter, args) -> None:
    doc = Document(path)
    related = doc.part.related_parts
    depth = {"cell": 0}

    def picture(rid: str | None) -> None:
        part = related.get(rid) if rid else None
        if part is not None:
            em.image(part.blob, Path(str(part.partname)).suffix)

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


# --------------------------------------------------------------------------- main
def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("email", help="student email or id, e.g. am8131 or am8131@srmist.edu.in (required, one per run)")
    ap.add_argument("--clean", action="store_true", help="delete this student's extracted_assets folder and exit")
    ap.add_argument("--root", type=Path, default=HERE / "GdriveDownload", help="download root (default scripts/GdriveDownload)")
    ap.add_argument("--dpi", type=int, default=150, help="resolution for rendered pages/figures (default 150)")
    ap.add_argument("--min-px", type=int, default=40, help="skip pictures smaller than this on either side (default 40)")
    ap.add_argument("--jpg-quality", type=int, default=92)
    ap.add_argument("--flatten-threshold", type=float, default=0.8, help="page is 'flattened' when pictures cover this fraction (default 0.8)")
    ap.add_argument("--no-vector-figures", dest="vector_figures", action="store_false", help="don't render vector diagrams as images")
    args = ap.parse_args()
    for stream in (sys.stdout, sys.stderr):  # odd characters in file names must not crash printing on cp1252 consoles
        stream.reconfigure(errors="replace")

    sid =args.email.strip().lower().split("@")[0]
    sdir = args.root / sid
    out_root = sdir / OUT_DIR
    if not sdir.is_dir():
        print(f"No folder for '{sid}' at {sdir}. Run gdrive_download.py first.")
        return 2

    if args.clean:
        if out_root.exists():
            shutil.rmtree(out_root)
            print(f"Removed {out_root}")
        else:
            print(f"Nothing to clean: {out_root} does not exist")
        return 0

    docs = sorted(p for p in sdir.iterdir()
                  if p.is_file() and p.suffix.lower() in (".pdf", ".docx") and not p.name.startswith(("~$", ".")))
    others = [p.name for p in sdir.iterdir()
              if p.is_file() and p.suffix.lower() not in (".pdf", ".docx") and not p.name.startswith(".")]
    if not docs:
        print(f"No .pdf or .docx files in {sdir}" + (f" (found: {', '.join(others)})" if others else ""))
        return 1
    if others:
        print(f"Not extracted (unsupported type): {', '.join(others)}")

    stems: dict[str, int] = {}
    for p in docs:
        stems[p.stem.lower()] = stems.get(p.stem.lower(), 0) + 1

    failed = 0
    for p in docs:
        folder = safe_name(p.stem)
        if stems[p.stem.lower()] > 1:  # a pdf and a docx with the same name
            folder += "-" + p.suffix.lower().lstrip(".")
        dest = out_root / folder
        if dest.exists():
            shutil.rmtree(dest)
        print(f"{p.name} -> {OUT_DIR}/{folder}/")
        em = Emitter(dest, args)
        try:
            (extract_pdf if p.suffix.lower() == ".pdf" else extract_docx)(p, em, args)
            em.finish()
            s = em.stats
            print(f"    {s['text']} text file(s), {s['images']} image(s), {s['pages_rendered']} page render(s), "
                  f"{s['figures']} figure(s); skipped {s['small_skipped']} tiny, {s['dup_skipped']} duplicate")
        except Exception as e:
            failed += 1
            print(f"    FAILED: {type(e).__name__}: {e}")
            shutil.rmtree(dest, ignore_errors=True)
    return 1 if failed else 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except KeyboardInterrupt:
        print("\nAborted.")
        sys.exit(130)
