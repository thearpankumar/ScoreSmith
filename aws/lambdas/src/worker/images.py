"""Provider-neutral image handling: re-encoding under a request-size budget and the shrink-and-retry loop.

Ported from scripts/analyze_images.py. Both providers send the image base64-encoded inside the request body.
"""
from __future__ import annotations

import io
from collections.abc import Callable

# Converse documents 3.75 MB / 8000 px per image, but that limit is really on the request BODY, where the image is
# base64-encoded (+33%). Measured: Kimi K2.5 and Qwen3-VL accept raw images up to ~2.61 MiB; one budget safe for all:
MAX_BYTES = 2_700_000
MAX_PX = 8000
MAX_IMAGE_PIXELS = 100_000_000  # decompression-bomb guard for Pillow


class TooLarge(Exception):
    """The service rejected the request body as too big (shrink the image and retry)."""


def is_size_error(e: Exception) -> bool:
    m = str(e).lower()
    return "length limit" in m or "too large" in m or "exceeds the maximum" in m or "request body" in m


def _pil():
    from PIL import Image

    Image.MAX_IMAGE_PIXELS = MAX_IMAGE_PIXELS
    return Image


def encode_best(img, budget: int) -> bytes:
    """Highest JPEG quality (bisection, 30..95) whose size fits the budget; shrink pixels only if even q=30 doesn't."""
    Image = _pil()
    for _ in range(8):
        def enc(q: int, im=img) -> bytes:
            buf = io.BytesIO()
            im.save(buf, "JPEG", quality=q, optimize=True)
            return buf.getvalue()

        best = enc(95)
        if len(best) <= budget:
            return best
        lo, hi, found = 30, 94, None
        while lo <= hi:
            mid = (lo + hi) // 2
            data = enc(mid)
            if len(data) <= budget:
                found, lo = data, mid + 1
            else:
                hi = mid - 1
        if found:
            return found
        scale = max(0.5, min(0.9, (budget / len(enc(30))) ** 0.5 * 0.95))  # size ~ pixels ~ scale^2
        img = img.resize((max(1, int(img.width * scale)), max(1, int(img.height * scale))), Image.LANCZOS)
    raise RuntimeError("could not shrink image under the size budget")


def prepare(raw: bytes, ext: str, budget: int = MAX_BYTES) -> tuple[bytes, str]:
    """Bytes + image format (jpeg/png/webp/gif). Original bytes are sent untouched when they fit; otherwise
    re-encoded at the highest quality that fits `budget`."""
    Image = _pil()
    fmt = {"jpg": "jpeg", "jpeg": "jpeg", "png": "png", "webp": "webp", "gif": "gif"}.get(ext.lower().lstrip("."))
    img = Image.open(io.BytesIO(raw))
    if fmt and len(raw) <= budget and max(img.size) <= MAX_PX:
        return raw, fmt
    if img.mode in ("RGBA", "LA") or (img.mode == "P" and "transparency" in img.info):
        rgba = img.convert("RGBA")
        bg = Image.new("RGB", rgba.size, "white")
        bg.paste(rgba, mask=rgba.split()[3])
        img = bg
    else:
        img = img.convert("RGB")
    if max(img.size) > MAX_PX:
        img.thumbnail((MAX_PX, MAX_PX), Image.LANCZOS)
    return encode_best(img, budget), "jpeg"


def analyse_with_shrink(analyse: Callable[[bytes, str], tuple[str, str, dict]], raw: bytes,
                        ext: str) -> tuple[str, str, dict]:
    """prepare + analyse(data, fmt), shrinking the budget by 15% and retrying if the service still says the body is
    too big (analyse raises TooLarge)."""
    budget = MAX_BYTES
    for _ in range(4):
        data, fmt = prepare(raw, ext, budget)
        try:
            return analyse(data, fmt)
        except TooLarge:
            budget = int(min(budget, len(data)) * 0.85)
            print(f"    body too large ({len(data) / 1e6:.2f} MB); retrying at <= {budget / 1e6:.2f} MB")
    raise RuntimeError("image still too large after 3 shrink attempts")


def analysis_key(image_key: str) -> str:
    stem = image_key.rsplit(".", 1)[0]
    return stem + ".analysis.md"
