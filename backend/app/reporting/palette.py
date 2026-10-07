"""Colours for the Excel export.

Score cells are coloured RELATIVE TO THE SCORECARD'S TARGET (see `target_band`): "meets target" is always the
same green whether the target is 4 or 9. The band maths here is mirrored by `frontend/lib/rag.ts`
(`getTargetBand`); `tests/data/target_band_cases.json` is the shared parity fixture for both.

The absolute 0-10 RAG bands (`BANDS`, `band_for_score`) and the red -> amber -> green `score_fill` ramp are kept for
the places where a score is an absolute rubric level (the Guidelines sheet headers) and for the frontend parity test.
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from decimal import ROUND_HALF_UP, Decimal

from app.models.enums import RagBand, rag_band_for_score

# ---------------------------------------------------------------------------------------------------------------
# Absolute 0-10 bands (legacy; still mirrored by frontend/lib/rag.ts for the rubric score picker)
# ---------------------------------------------------------------------------------------------------------------


@dataclass(frozen=True)
class Band:
    key: str
    label: str
    min_score: float
    fill: str
    font: str
    range_text: str


# Highest first, matched with `score >= min_score` (same rule as the backend and the UI).
BANDS: tuple[Band, ...] = (
    Band("excellent", "Excellent", 9, "#1B5E20", "#FFFFFF", "9.0 – 10"),
    Band("good", "Good", 8, "#66BB6A", "#111827", "8.0 – 8.99"),
    Band("acceptable", "Acceptable", 7, "#9CCC65", "#111827", "7.0 – 7.99"),
    Band("needs-improvement", "Needs Improvement", 6, "#F9A825", "#111827", "6.0 – 6.99"),
    Band("weak", "Weak", 5, "#E65100", "#FFFFFF", "5.0 – 5.99"),
    Band("poor", "Poor", 4, "#D32F2F", "#FFFFFF", "4.0 – 4.99"),
    Band("critical", "Critical", 0, "#7F0000", "#FFFFFF", "0 – 3.99"),
)

_BY_RAG = {
    RagBand.BAND_10_9: "excellent",
    RagBand.BAND_8: "good",
    RagBand.BAND_7: "acceptable",
    RagBand.BAND_6: "needs-improvement",
    RagBand.BAND_5: "weak",
    RagBand.BAND_4: "poor",
    RagBand.BAND_3_0: "critical",
}
_BAND_BY_KEY = {b.key: b for b in BANDS}


def band_for_score(score: float) -> Band:
    """The ABSOLUTE band for a 0-10 score, decided by the backend's own `rag_band_for_score`."""
    return _BAND_BY_KEY[_BY_RAG[rag_band_for_score(round(float(score), 2))]]


# --- fixed 0-10 ramp (red -> amber -> green): rubric level headers on the Guidelines sheet ----------------------
SCALE_MIN, SCALE_MID, SCALE_MAX = 0, 5, 10
SCALE_MIN_COLOR, SCALE_MID_COLOR, SCALE_MAX_COLOR = "#E8736B", "#FFE08A", "#4FB286"
SCORE_FONT = "#111827"
BLANK_FILL = "#EEEEEE"


def _rgb(hex_color: str) -> tuple[int, int, int]:
    h = hex_color.lstrip("#")
    return int(h[0:2], 16), int(h[2:4], 16), int(h[4:6], 16)


def score_fill(score: float) -> str:
    """The colour Excel's 3-colour scale paints for `score` (linear RGB interpolation), for static swatches."""
    s = min(float(SCALE_MAX), max(float(SCALE_MIN), float(score)))
    if s <= SCALE_MID:
        lo, hi, t = _rgb(SCALE_MIN_COLOR), _rgb(SCALE_MID_COLOR), (s - SCALE_MIN) / (SCALE_MID - SCALE_MIN)
    else:
        lo, hi, t = _rgb(SCALE_MID_COLOR), _rgb(SCALE_MAX_COLOR), (s - SCALE_MID) / (SCALE_MAX - SCALE_MID)
    return "#{:02X}{:02X}{:02X}".format(*(round(a + (b - a) * t) for a, b in zip(lo, hi, strict=True)))


# ---------------------------------------------------------------------------------------------------------------
# Target-relative bands
# ---------------------------------------------------------------------------------------------------------------
DEFAULT_TARGET = 7.0


@dataclass(frozen=True)
class TargetBand:
    key: str
    label: str
    fill: str  # solid colour: band label cells, swatches, chart columns, tab colours
    font: str  # readable text colour on `fill`
    tint: str  # pale colour: score cells (keeps the number legible)


TARGET_BANDS: tuple[TargetBand, ...] = (
    TargetBand("exceeds", "Exceeds target", "#1B5E20", "#FFFFFF", "#A5D6A7"),
    TargetBand("meets", "Meets target", "#66BB6A", "#111827", "#C8E6C9"),
    TargetBand("near", "Near target", "#F9A825", "#111827", "#FFE9A8"),
    TargetBand("below", "Below target", "#E65100", "#FFFFFF", "#FFCC9C"),
    TargetBand("well_below", "Well below target", "#D32F2F", "#FFFFFF", "#F6B5B0"),
    TargetBand("critical", "Critical", "#7F0000", "#FFFFFF", "#D99A9A"),
)
TARGET_BAND_BY_KEY = {b.key: b for b in TARGET_BANDS}
_RATIOS = (("near", Decimal("0.90")), ("below", Decimal("0.75")), ("well_below", Decimal("0.50")))
_CENT = Decimal("0.01")


def _dec(x: float | int | str) -> Decimal:
    return Decimal(str(float(x)))


def _q2(x: Decimal) -> Decimal:
    return x.quantize(_CENT, rounding=ROUND_HALF_UP)


def target_is_default(target: float | None) -> bool:
    """True when the scorecard has no usable target (missing, <= 0 or not finite) and `DEFAULT_TARGET` applies."""
    try:
        t = float(target)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return True
    return (not math.isfinite(t)) or t <= 0


def effective_target(target: float | None) -> float:
    return DEFAULT_TARGET if target_is_default(target) else float(target)  # type: ignore[arg-type]


def _thresholds(target: float | None) -> dict[str, Decimal | None]:
    t = _dec(effective_target(target))
    exceeds = _q2(min(t * Decimal("1.10"), (t + 10) / 2))
    out: dict[str, Decimal | None] = {"exceeds": exceeds if exceeds > _q2(t) else None, "meets": _q2(t)}
    for key, ratio in _RATIOS:
        out[key] = _q2(t * ratio)
    return out


def band_thresholds(target: float | None) -> dict[str, float | None]:
    """Lower bound (inclusive, 2 dp) of every band except `critical`; `exceeds` is None when it collapses into
    `meets` (a target at or near 10)."""
    return {k: (float(v) if v is not None else None) for k, v in _thresholds(target).items()}


def target_band(score: float, target: float | None) -> TargetBand:
    """The band of `score` relative to `target`: the first band whose (2-dp, half-up) lower bound the 2-dp score
    reaches. Cut-offs are inclusive; bands are Exceeds / Meets / Near / Below / Well below / Critical."""
    s = _q2(_dec(score))
    th = _thresholds(target)
    for band in TARGET_BANDS[:-1]:
        lo = th[band.key]
        if lo is not None and s >= lo:
            return band
    return TARGET_BANDS[-1]


def _fmt_t(x: float) -> str:
    s = f"{x:.2f}"
    return s[:-1] if s.endswith("0") else s


def band_ranges(target: float | None) -> list[tuple[TargetBand, float | None, str]]:
    """(band, inclusive lower bound, human range text) for the bands that exist at this target, best first."""
    th = band_thresholds(target)
    present = [(b, th[b.key]) for b in TARGET_BANDS[:-1] if th[b.key] is not None]
    out: list[tuple[TargetBand, float | None, str]] = []
    prev_lo: float | None = None
    for band, lo in present:
        assert lo is not None
        text = f"≥ {_fmt_t(lo)}" if prev_lo is None else f"{_fmt_t(lo)} – {_fmt_t(max(lo, prev_lo - 0.01))}"
        out.append((band, lo, text))
        prev_lo = lo
    out.append((TARGET_BANDS[-1], None, f"< {_fmt_t(prev_lo if prev_lo is not None else 0.0)}"))
    return out


# ---------------------------------------------------------------------------------------------------------------
# Design tokens: every colour used by the workbook chrome lives here
# ---------------------------------------------------------------------------------------------------------------
INK = "#111827"
MUTED = "#6B7280"
HEADER_FILL = "#1F2937"  # table headers, section bars, title band
SUBHEADER_FILL = "#374151"  # sub-table headers, sub-title band
DANGER_FILL = "#7F1D1D"  # "needs attention" bars
HAIRLINE = "#E5E7EB"
BORDER = "#D1D5DB"
PANEL = "#F3F4F6"
ZEBRA = "#F9FAFB"
LINK = "#1D4ED8"
POSITIVE = "#166534"
NEGATIVE = "#B91C1C"
WARN_TEXT = "#92400E"
GOLD, SILVER, BRONZE = "#F4D35E", "#D9D9D9", "#E0B07B"
TAB = {
    "summary": "#1F2937", "leaderboard": "#C9A227", "data": "#6B7280", "matrix": "#4FB286", "notes": "#9CA3AF",
    "reference": "#3B82F6",
}
SEVERITY_FILL = {"high": "#FECACA", "medium": "#FEF3C7", "info": "#E5E7EB"}
