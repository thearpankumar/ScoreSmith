"""Renders an `ExportBundle` + `Insights` as an .xlsx with XlsxWriter.

Design rules (see the export plan):
- The first sheet ("Summary") carries every insight; a reader should get the whole story from it.
- Colour is TARGET-RELATIVE: every score is compared with its scorecard's target (`palette.target_band`). Score cells
  carry a pale tint of the band colour, Band cells the solid colour; the number and a Band label are always shown too,
  so colour is never the only signal.
- One sheet per exported student ("NN Name"): header card, category roll-up and a KPI card for every KPI with the
  reasoning ("Why") and each piece of evidence on its own row.
- Only completed, scored evaluations are ever exported (failed / queued / unscored ones never reach a sheet).
- No formulas are ever written (user text could otherwise inject them), only computed values; every user string goes
  through `write_string` with XML-illegal characters stripped and `quote_prefix` where needed.
"""

from __future__ import annotations

import math
import re
from dataclasses import dataclass, replace
from datetime import UTC, datetime
from io import BytesIO

import xlsxwriter

from app.reporting import palette as P
from app.reporting.export_data import UNCATEGORISED, ExportBundle, ExportResult
from app.reporting.insights import AT_RISK_TEXT, Insights, KpiStat, ScoredRow, compute_insights, target_phrase
from app.reporting.xlsx_safety import clean_text, is_plausible_email, needs_quote_prefix, safe_sheet_name

MAX_MATRIX_SHEETS = 8
TEXT_CAP = 8000
MAX_ROW_PT = 409.0
LEVEL_COL_W = 34.0
MAX_SUMMARY_STUDENT_LINKS = 60
DELTA_FMT = '"▲" +0.00;"▼" -0.00;"●" 0.00'


@dataclass
class ExportOptions:
    include_reasoning: bool = True
    filter_summary: str | None = None
    generated_by: str = ""
    generated_at: datetime | None = None


def _naive_utc(dt: datetime | None) -> datetime | None:
    if dt is None:
        return None
    return dt.astimezone(UTC).replace(tzinfo=None) if dt.tzinfo else dt


class _Book:
    """Format cache + safe cell writers around one XlsxWriter workbook."""

    def __init__(self, wb: xlsxwriter.Workbook) -> None:
        self.wb = wb
        self._fmts: dict[tuple, object] = {}

    def f(self, **props):
        base = {"font_name": "Calibri", "font_size": 11, "font_color": P.INK, "valign": "vcenter"}
        base.update(props)
        key = tuple(sorted(base.items()))
        if key not in self._fmts:
            self._fmts[key] = self.wb.add_format(base)
        return self._fmts[key]

    # -- safe writers ----------------------------------------------------------------------------------
    def put(self, ws, r: int, c: int, value, **props) -> None:
        """Write any value safely. Text -> write_string (+quote_prefix when it looks like a formula)."""
        if value is None or value == "":
            ws.write_blank(r, c, None, self.f(**props))
        elif isinstance(value, bool):
            ws.write_string(r, c, "Yes" if value else "No", self.f(**props))
        elif isinstance(value, int | float):
            if isinstance(value, float) and not math.isfinite(value):
                ws.write_blank(r, c, None, self.f(**props))
            else:
                ws.write_number(r, c, value, self.f(**props))
        elif isinstance(value, datetime):
            ws.write_datetime(r, c, _naive_utc(value), self.f(num_format="yyyy-mm-dd hh:mm", **props))
        else:
            s = clean_text(value, TEXT_CAP)
            if needs_quote_prefix(s):
                props = {**props, "quote_prefix": True}
            ws.write_string(r, c, s, self.f(**props))

    def merge(self, ws, r1: int, c1: int, r2: int, c2: int, text, **props) -> None:
        s = clean_text(text, TEXT_CAP)
        if needs_quote_prefix(s):
            props = {**props, "quote_prefix": True}
        if (r1, c1) == (r2, c2):
            ws.write_string(r1, c1, s, self.f(**props))
        else:
            ws.merge_range(r1, c1, r2, c2, s, self.f(**props))

    def email(self, ws, r: int, c: int, email: str | None, **props) -> None:
        email = (email or "").strip()
        if is_plausible_email(email) and not needs_quote_prefix(email):
            ws.write_url(r, c, f"mailto:{email}", self.f(font_color=P.LINK, underline=1, **props), string=email)
        else:
            self.put(ws, r, c, email, **props)

    def link(self, ws, r: int, c: int, sheet: str, text: str, **props) -> None:
        target = sheet.replace("'", "''")
        shown = clean_text(text, 200)
        if needs_quote_prefix(shown):
            props = {**props, "quote_prefix": True}
        ws.write_url(r, c, f"internal:'{target}'!A1", self.f(font_color=P.LINK, underline=1, **props), string=shown)

    def band_cell(self, ws, r: int, c: int, score: float | None, target: float | None, **props) -> None:
        """The SOLID band label of `score` relative to `target`."""
        if score is None:
            self.put(ws, r, c, "", **props)
            return
        b = P.target_band(score, target)
        self.put(ws, r, c, b.label, bg_color=b.fill, font_color=b.font, bold=True, align="center", border=1,
                 border_color=P.HAIRLINE, **props)

    def score_cell(self, ws, r: int, c: int, score: float | None, target: float | None, fmt: str = "0.00", **props) -> None:
        """A score with the PALE TINT of its target-relative band (static fill, so it is right on every sheet)."""
        if score is None:
            self.put(ws, r, c, "", bg_color=P.BLANK_FILL, border=1, border_color=P.HAIRLINE, **props)
            return
        self.put(ws, r, c, float(score), num_format=fmt, bg_color=P.target_band(score, target).tint, align="center",
                 bold=True, border=1, border_color=P.HAIRLINE, **props)

    def delta_cell(self, ws, r: int, c: int, delta: float | None, **props) -> None:
        if delta is None:
            self.put(ws, r, c, "", border=1, border_color=P.HAIRLINE, **props)
            return
        color = P.POSITIVE if delta > 0.004 else P.NEGATIVE if delta < -0.004 else P.MUTED
        self.put(ws, r, c, float(delta), num_format=DELTA_FMT, align="center", font_color=color, bold=True, border=1,
                 border_color=P.HAIRLINE, **props)


# ---- sizing helpers -----------------------------------------------------------------------------------------
# Excel never auto-fits merged cells and clips text next to a filled neighbour, so every wrapped/merged block gets an
# explicit height and every text column a content-derived width. The estimates are deliberately conservative
# (wider glyph than Calibri's real average) so text is never cut off; tests/test_export_layout.py checks the result.
_GLYPH_PX = 6.6  # px per character at 11pt


def _cap(width_units: float, size: float = 11, bold: bool = False) -> int:
    """Characters that fit on one line of a column `width_units` wide."""
    glyph = _GLYPH_PX * size / 11 * (1.08 if bold else 1.0)
    return max(1, int((width_units * 7 + 5 - 8) / glyph))


def _lines(text, width_units: float, size: float = 11, bold: bool = False) -> int:
    cap = _cap(width_units, size, bold)
    return sum(max(1, math.ceil(len(p) / cap)) for p in str("" if text is None else text).split("\n"))


def _height(lines: int, size: float = 11, minimum: float = 18.0) -> float:
    return min(MAX_ROW_PT, max(minimum, lines * size * 1.32 + 4))


def _fit(values, lo: float, hi: float, size: float = 11, bold: bool = False) -> float:
    """Column width (units) that shows the longest value on one line, within [lo, hi]."""
    longest = max((len(str(v)) for v in values if v not in (None, "")), default=0)
    glyph = _GLYPH_PX * size / 11 * (1.08 if bold else 1.0)
    return float(min(hi, max(lo, math.ceil(longest * glyph / 7) + 2)))


def _header_height(heads: list[str], widths: list[float], size: float = 11, minimum: float = 34.0) -> float:
    n = max((_lines(h, widths[i], size, True) for i, h in enumerate(heads)), default=1)
    return max(minimum, _height(n, size))


def _max_lines(size: float) -> int:
    return max(1, int((MAX_ROW_PT - 4) / (size * 1.32)))


def split_for_cell(text: str, width_units: float, size: float = 11, max_lines: int | None = None) -> list[str]:
    """Split `text` at word boundaries into chunks that each fit in one Excel row (<= 409pt) at this width, so a long
    reasoning or quote continues on the next row instead of being clipped."""
    limit = max_lines or _max_lines(size)
    text = str(text or "").strip()
    if not text:
        return []
    if _lines(text, width_units, size) <= limit:
        return [text]
    chunks: list[str] = []
    cur = ""
    for tok in re.split(r"(\s+)", text):
        cand = cur + tok
        if _lines(cand, width_units, size) <= limit:
            cur = cand
            continue
        if cur.strip():
            chunks.append(cur.strip())
        cur = ""
        while _lines(tok, width_units, size) > limit:  # one unbroken token longer than a whole row
            n = _cap(width_units, size) * limit
            chunks.append(tok[:n])
            tok = tok[n:]
        cur = tok.lstrip()
    if cur.strip():
        chunks.append(cur.strip())
    return chunks


def _setup_print(ws, header_rows: tuple[int, int] | None = None, landscape: bool = True) -> None:
    if landscape:
        ws.set_landscape()
    ws.set_paper(9)
    ws.fit_to_pages(1, 0)
    ws.set_margins(0.5, 0.5, 0.6, 0.6)
    ws.set_footer("&L&8Quality Scorecard — confidential&C&A&RPage &P of &N")
    if header_rows:
        ws.repeat_rows(*header_rows)


def _title_block(bk: _Book, ws, title: str, subtitle: str | None, widths: list[float], span: int | None = None) -> None:
    """Rows 0-1: a dark title band and a sub-title band, merged over the first `span` columns (widened until the
    title fits on one line; if the whole sheet is still too narrow the title wraps)."""
    span = max(1, min(span or len(widths), len(widths)))
    need_units = (len(title) * 11.6 + 40 - 5) / 7  # 18pt bold glyphs + indent
    while sum(widths[:span]) < need_units and span < len(widths):
        span += 1
    total = sum(widths[:span])
    wrap = total < need_units
    ws.set_row(0, _height(_lines(title, total, 18, True), 18, 34) if wrap else 34)
    bk.merge(ws, 0, 0, 0, span - 1, title, bold=True, font_size=18, font_color="#FFFFFF", bg_color=P.HEADER_FILL,
             indent=1, text_wrap=wrap)
    sub = subtitle or ""
    ws.set_row(1, _height(_lines(sub, total), 11, 22))
    bk.merge(ws, 1, 0, 1, span - 1, sub, font_color="#D1D5DB", bg_color=P.SUBHEADER_FILL, text_wrap=True, indent=1)


def _default_note(ins: Insights) -> str:
    return (f" No target is set on at least one scorecard, so {P.DEFAULT_TARGET:.1f} is assumed for it."
            if ins.target_defaulted else "")


def _unique(headers: list[str]) -> list[str]:
    seen: dict[str, int] = {}
    out = []
    for h in headers:
        k = h.lower()
        seen[k] = seen.get(k, 0) + 1
        out.append(h if seen[k] == 1 else f"{h} ({seen[k]})")
    return out


def _header(bk: _Book, **extra):
    return bk.f(bold=True, font_color="#FFFFFF", bg_color=P.HEADER_FILL, text_wrap=True, align="center",
                border=1, border_color=P.BORDER, **extra)


def _table(ws, r1: int, c1: int, r2: int, c2: int, name: str, headers: list[str], header_fmt) -> None:
    ws.add_table(r1, c1, r2, c2, {
        "name": name, "style": "Table Style Light 1", "autofilter": True,
        "columns": [{"header": h, "header_format": header_fmt} for h in headers],
    })


def build_workbook(bundle: ExportBundle, options: ExportOptions | None = None, insights: Insights | None = None) -> bytes:
    options = options or ExportOptions()
    # Only completed, scored evaluations are exported — drop anything else even if the caller passed it.
    exported = [e for e in bundle.evaluations if e.is_scored]
    if len(exported) != len(bundle.evaluations):
        bundle = replace(bundle, evaluations=exported,
                         excluded_count=bundle.excluded_count + len(bundle.evaluations) - len(exported))
    ins = insights or compute_insights(bundle)
    generated_at = options.generated_at or datetime.now(UTC)
    buf = BytesIO()
    wb = xlsxwriter.Workbook(
        buf,
        {"in_memory": True, "strings_to_formulas": False, "strings_to_urls": False, "strings_to_numbers": False,
         "nan_inf_to_errors": True},
    )
    wb.set_properties({
        "title": "Evaluation report", "subject": "Scorecard evaluation results",
        "author": clean_text(options.generated_by or "Quality Scorecard", 100),
        "manager": "", "company": "Quality Scorecard", "category": "Evaluation report",
        "keywords": "evaluation, scorecard, KPI, leaderboard", "comments": "Generated by Quality Scorecard",
        "created": _naive_utc(generated_at),
    })
    wb.define_name("Score_Scale_Min", "=0")
    wb.define_name("Score_Scale_Max", "=10")
    bk = _Book(wb)

    # ---- plan sheet names up front (so sheets can link to each other) and create tabs in display order ------
    used: set[str] = set()
    names: dict[str, str] = {"summary": safe_sheet_name("Summary", used)}
    has_board = ins.n_scored >= 2
    if has_board:
        names["leaderboard"] = safe_sheet_name("Leaderboard", used)
    names["evaluations"] = safe_sheet_name("Evaluations", used)
    versions = _versions_with_scores(ins)
    shown_versions = versions[:MAX_MATRIX_SHEETS]
    matrix_names: dict = {}
    for vid, label in shown_versions:
        matrix_names[vid] = safe_sheet_name("KPI Matrix" if len(versions) == 1 else f"Matrix – {label}", used)
    has_detail = ins.n_scored >= 1
    if has_detail:
        names["detail"] = safe_sheet_name("KPI Detail", used)
    pad = max(2, len(str(ins.n_scored)))
    student_names: list[str] = [safe_sheet_name(f"{i:0{pad}d} {row.ev.display_name}", used)
                                for i, row in enumerate(ins.scored, start=1)]
    student_by_eval = {row.ev.id: nm for row, nm in zip(ins.scored, student_names, strict=True)}
    names["kpi_ref"] = safe_sheet_name("KPI Reference", used)
    names["guidelines"] = safe_sheet_name("Guidelines", used)
    names["notes"] = safe_sheet_name("Notes", used)

    sheets: dict[str, object] = {}
    for key in ("summary", "leaderboard", "evaluations"):
        if key in names:
            sheets[key] = wb.add_worksheet(names[key])
    matrix_ws = {vid: wb.add_worksheet(matrix_names[vid]) for vid, _ in shown_versions}
    if "detail" in names:
        sheets["detail"] = wb.add_worksheet(names["detail"])
    student_ws = [wb.add_worksheet(nm) for nm in student_names]
    sheets["kpi_ref"] = wb.add_worksheet(names["kpi_ref"])
    sheets["guidelines"] = wb.add_worksheet(names["guidelines"])
    sheets["notes"] = wb.add_worksheet(names["notes"])

    board_rows = _build_leaderboard(bk, sheets["leaderboard"], ins, student_by_eval, names) if has_board else None
    _build_evaluations(bk, sheets["evaluations"], ins, student_by_eval)
    for vid, label in shown_versions:
        _build_matrix(bk, matrix_ws[vid], bundle, ins, vid, label)
    if has_detail:
        _build_detail(bk, sheets["detail"], bundle, ins, options, student_by_eval)
    for i, (row, ws) in enumerate(zip(ins.scored, student_ws, strict=True), start=1):
        _build_student_sheet(bk, ws, bundle, ins, row, options, names, i)
    _build_kpi_reference(bk, sheets["kpi_ref"], bundle)
    _build_guidelines(bk, sheets["guidelines"], bundle)
    _build_summary(bk, sheets["summary"], ins, options, names, generated_at, matrix_names,
                   list(zip(ins.scored, student_names, strict=True)))
    _build_notes(bk, sheets["notes"], bundle, ins, options, versions, len(shown_versions), generated_at)
    if board_rows:
        r1, r2 = board_rows
        wb.define_name("Leaderboard_Scores", f"='{names['leaderboard']}'!$D${r1 + 1}:$D${r2 + 1}")
    sheets["summary"].activate()
    sheets["summary"].set_first_sheet()
    wb.close()
    return buf.getvalue()


def _versions_with_scores(ins: Insights) -> list[tuple]:
    """(version_id, label) for every scorecard version that has at least one scored evaluation."""
    seen: dict = {}
    for r in ins.scored:
        seen.setdefault(r.ev.version_id, f"{r.ev.scorecard_name} v{r.ev.version_number}")
    return list(seen.items())


# --------------------------------------------------------------------------------------------------- leaderboard
def _build_leaderboard(bk: _Book, ws, ins: Insights, student_by_eval: dict, names: dict[str, str]) -> tuple[int, int]:
    ws.set_tab_color(P.TAB["leaderboard"])
    cats: list[str] = []
    if not ins.multi_scorecard:
        for r in ins.scored:
            for c in r.ev.category_scores:
                if c not in cats:
                    cats.append(c)
    heads = ["Rank", "Name", "Email", "Overall score", "Band", "Δ vs target", "Percentile"]
    if ins.multi_scorecard:
        heads += ["Scorecard", "Target", "Rank in scorecard"]
    heads += [clean_text(c, 40) for c in cats]
    heads += ["Needs-review KPIs", "Evaluated on (UTC)"]
    first = 3
    last = first + len(ins.scored)
    heads = _unique(heads)
    n_cols = len(heads)
    widths: list[float] = [8, _fit([r.ev.display_name for r in ins.scored], 18, 36, bold=True),
                           _fit([r.ev.subject_email for r in ins.scored], 24, 44), 13, 20, 13, 12]
    if ins.multi_scorecard:
        widths += [_fit([r.ev.scorecard_name for r in ins.scored], 18, 34), 9, 18]
    widths += [15.0] * len(cats)
    widths += [12, 18]
    sub = (f"{ins.n_scored} evaluations ranked by overall score, highest first. Tied scores share a rank (shown as T2). "
           f"Scores are coloured against the scorecard's target ({target_phrase(ins)}).{_default_note(ins)} "
           "Click a name to open that student's sheet.")
    _title_block(bk, ws, "Leaderboard", sub, widths, min(n_cols, 8))
    if ins.multi_scorecard:
        ws.set_row(2, 22)
        bk.merge(ws, 2, 0, 2, min(n_cols, 8) - 1, "⚠ Scores come from different scorecards and may not be directly comparable. "
                 "“Rank in scorecard” ranks within each scorecard.", bold=True, font_color=P.WARN_TEXT, text_wrap=True)
        ws.set_row(2, _height(_lines("⚠ Scores come from different scorecards and may not be directly comparable. "
                                     "“Rank in scorecard” ranks within each scorecard.", sum(widths[:min(n_cols, 8)]), 11, True), 11, 22))
    _table(ws, first, 0, last, n_cols - 1, "tblLeaderboard", heads, _header(bk))
    ws.set_row(first, _header_height(heads, widths))
    medal = {1: P.GOLD, 2: P.SILVER, 3: P.BRONZE}
    border = dict(border=1, border_color=P.HAIRLINE)
    for i, row in enumerate(ins.scored, start=1):
        r = first + i
        ev = row.ev
        rank_props = dict(align="center", bold=True, num_format='"T"0' if row.tied else "0", **border)
        if row.rank in medal:
            rank_props["bg_color"] = medal[row.rank]
        bk.put(ws, r, 0, row.rank, **rank_props)
        sheet = student_by_eval.get(ev.id)
        if sheet:
            bk.link(ws, r, 1, sheet, ev.display_name, bold=True, text_wrap=True, **border)
        else:
            bk.put(ws, r, 1, ev.display_name, bold=True, text_wrap=True, **border)
        bk.email(ws, r, 2, ev.subject_email, text_wrap=True, **border)
        bk.score_cell(ws, r, 3, row.score, row.target)
        bk.band_cell(ws, r, 4, row.score, row.target)
        bk.delta_cell(ws, r, 5, row.delta_vs_target)
        bk.put(ws, r, 6, row.percentile, num_format="0%", align="center", **border)
        c = 7
        if ins.multi_scorecard:
            bk.put(ws, r, c, ev.scorecard_name, text_wrap=True, **border)
            bk.put(ws, r, c + 1, row.target, num_format="0.0", align="center", **border)
            bk.put(ws, r, c + 2, row.rank_in_card, align="center", **border)
            c += 3
        for cat in cats:
            bk.score_cell(ws, r, c, ev.category_scores.get(cat), row.target, "0.0")
            c += 1
        nr = sum(1 for x in ev.results.values() if x.needs_review)
        bk.put(ws, r, c, nr, align="center", **border)
        bk.put(ws, r, c + 1, ev.finished_at or ev.submitted_at, align="center", **border)
    for c, w in enumerate(widths):
        ws.set_column(c, c, w)
    ws.freeze_panes(first + 1, 2)
    _setup_print(ws, (first, first))
    return first + 1, last


# -------------------------------------------------------------------------------------------------- evaluations
def _build_evaluations(bk: _Book, ws, ins: Insights, student_by_eval: dict) -> None:
    ws.set_tab_color(P.TAB["data"])
    heads = ["#", "Evaluation", "Subject name", "Subject email", "Scorecard", "Version", "Domain", "Overall score",
             "Band", "Target", "Δ vs target", "Rank", "KPIs scored", "KPIs total", "Needs-review KPIs", "Evaluator",
             "Source", "Submitted (UTC)", "Finished (UTC)", "Evaluation ID"]
    ordered = ins.scored
    first = 3
    last = first + len(ordered)
    evs = [r.ev for r in ordered]
    widths = [5, _fit([e.name for e in evs], 24, 40), _fit([e.subject_name for e in evs], 20, 30, bold=True),
              _fit([e.subject_email for e in evs], 24, 40), _fit([e.scorecard_name for e in evs], 22, 34),
              9, 16, 13, 20, 9, 13, 8, 11, 10, 12, _fit([e.evaluator_name for e in evs], 16, 28), 10, 18, 18, 38]
    sub = ("One row per exported evaluation, best score first. Scores are coloured against each scorecard's target."
           + _default_note(ins) + " Click a subject name to open that student's sheet.")
    _title_block(bk, ws, "Evaluations", sub, widths, 8)
    _table(ws, first, 0, last, len(heads) - 1, "tblEvaluations", heads, _header(bk))
    ws.set_row(first, _header_height(heads, widths))
    b = dict(border=1, border_color=P.HAIRLINE)
    for i, row in enumerate(ordered, start=1):
        ev = row.ev
        r = first + i
        bk.put(ws, r, 0, i, align="center", **b)
        bk.put(ws, r, 1, ev.name, text_wrap=True, **b)
        sheet = student_by_eval.get(ev.id)
        subject = ev.subject_name or ""
        if sheet and subject:
            bk.link(ws, r, 2, sheet, subject, bold=True, text_wrap=True, **b)
        elif sheet:
            bk.link(ws, r, 2, sheet, ev.display_name, bold=True, text_wrap=True, **b)
        else:
            bk.put(ws, r, 2, subject, text_wrap=True, **b)
        bk.email(ws, r, 3, ev.subject_email, text_wrap=True, **b)
        bk.put(ws, r, 4, ev.scorecard_name, text_wrap=True, **b)
        bk.put(ws, r, 5, f"v{ev.version_number}", align="center", **b)
        bk.put(ws, r, 6, ev.domain or "", text_wrap=True, **b)
        bk.score_cell(ws, r, 7, row.score, row.target)
        bk.band_cell(ws, r, 8, row.score, row.target)
        bk.put(ws, r, 9, row.target, num_format="0.0", align="center", **b)
        bk.delta_cell(ws, r, 10, row.delta_vs_target)
        bk.put(ws, r, 11, row.rank, num_format='"T"0' if row.tied else "0", align="center", **b)
        bk.put(ws, r, 12, len(ev.results), align="center", **b)
        bk.put(ws, r, 13, ev.kpis_total, align="center", **b)
        bk.put(ws, r, 14, sum(1 for x in ev.results.values() if x.needs_review), align="center", **b)
        bk.put(ws, r, 15, ev.evaluator_name, text_wrap=True, **b)
        bk.put(ws, r, 16, ev.source, align="center", **b)
        bk.put(ws, r, 17, ev.submitted_at, align="center", **b)
        bk.put(ws, r, 18, ev.finished_at, align="center", **b)
        bk.put(ws, r, 19, str(ev.id), font_color=P.MUTED, font_size=9, **b)
    for c, w in enumerate(widths):
        ws.set_column(c, c, w)
    ws.freeze_panes(first + 1, 3)
    _setup_print(ws, (first, first))


# ------------------------------------------------------------------------------------------------------ matrix
def _build_matrix(bk: _Book, ws, bundle: ExportBundle, ins: Insights, vid, label: str) -> None:
    ws.set_tab_color(P.TAB["matrix"])
    kpis = [k for k in bundle.kpis_by_version.get(vid, []) if k.is_leaf]
    rows = [r for r in ins.scored if r.ev.version_id == vid]
    target = rows[0].target if rows else ins.display_target
    kw = 15  # KPI column width
    fixed = 3
    name_w = _fit([r.ev.display_name for r in rows] + ["Average (selected)"], 20, 30, bold=True)
    mail_w = _fit([r.ev.subject_email for r in rows], 22, 34)
    widths = [name_w, mail_w, 10] + [float(kw)] * len(kpis)
    sub = (f"Evaluations × KPIs for {label}. Each cell is coloured against the scorecard's target of {target:.1f}: "
           "green meets or exceeds it, amber is near, orange and red are below, dark red is critical. Grey = not scored.")
    _title_block(bk, ws, f"KPI Matrix — {label}", sub, widths, min(len(widths), 8))
    # row 2 category group headers, row 3 KPI names, row 4 average, data from row 5 (0-indexed; no spacer row so
    # the frozen header stays short)
    gh, kh, avg_r, data0 = 2, 3, 4, 5
    ws.set_row(kh, _height(max((_lines(k.name, kw, 10, True) for k in kpis), default=1) + 1, 10, 66))
    for c, h in enumerate(["Name", "Email", "Overall"]):
        bk.put(ws, kh, c, h, bold=True, font_color="#FFFFFF", bg_color=P.HEADER_FILL, text_wrap=True, align="center",
               border=1, border_color=P.BORDER)
        bk.put(ws, gh, c, "", bg_color=P.HEADER_FILL)
    c = fixed
    i = 0
    group_lines = 1
    while i < len(kpis):
        j = i
        while j + 1 < len(kpis) and kpis[j + 1].category == kpis[i].category:
            j += 1
        bk.merge(ws, gh, c + i, gh, c + j, kpis[i].category, bold=True, font_color="#FFFFFF", bg_color=P.SUBHEADER_FILL,
                 align="center", border=1, border_color=P.BORDER, text_wrap=True)
        group_lines = max(group_lines, _lines(kpis[i].category, kw * (j - i + 1), 11, True))
        i = j + 1
    ws.set_row(gh, _height(group_lines))
    for i, k in enumerate(kpis):
        tag = f"{k.name}\n" + (f"(w {k.weight:g}%)" if k.included and k.weight is not None else "(info only)")
        bk.put(ws, kh, fixed + i, tag, bold=True, font_color="#FFFFFF", bg_color=P.HEADER_FILL, text_wrap=True,
               align="center", border=1, border_color=P.BORDER, font_size=10)
        if k.guideline_summary:
            note = clean_text(k.guideline_summary, 900)
            ws.write_comment(kh, fixed + i, note,
                             {"x_scale": 2.4, "y_scale": min(6.0, max(1.6, (len(note) / 42 * 15 + 24) / 74))})
    last_row = data0 + len(rows) - 1
    bk.put(ws, avg_r, 0, "Average (selected)", bold=True, bg_color=P.PANEL, border=1, border_color=P.HAIRLINE)
    bk.put(ws, avg_r, 1, "", bg_color=P.PANEL, border=1, border_color=P.HAIRLINE)
    scores = [r.score for r in rows]
    bk.score_cell(ws, avg_r, 2, sum(scores) / len(scores) if scores else None, target, "0.0")
    for i, k in enumerate(kpis):
        vals = [r.ev.results[k.id].score for r in rows if k.id in r.ev.results]
        bk.score_cell(ws, avg_r, fixed + i, sum(vals) / len(vals) if vals else None, target, "0.0")
    b = dict(border=1, border_color=P.HAIRLINE)
    for n, row in enumerate(rows):
        r = data0 + n
        bk.put(ws, r, 0, row.ev.display_name, bold=True, text_wrap=True, **b)
        bk.email(ws, r, 1, row.ev.subject_email, text_wrap=True, **b)
        bk.score_cell(ws, r, 2, row.score, row.target, "0.0")
        for i, k in enumerate(kpis):
            res = row.ev.results.get(k.id)
            bk.score_cell(ws, r, fixed + i, res.score if res else None, row.target, "0.0")
    ws.set_column(0, 0, name_w)
    ws.set_column(1, 1, mail_w)
    ws.set_column(2, 2, 10)
    if kpis:
        ws.set_column(fixed, fixed + len(kpis) - 1, kw)
        ws.autofilter(kh, 0, max(last_row, avg_r), fixed + len(kpis) - 1)
    ws.freeze_panes(data0, fixed)
    _setup_print(ws, (gh, kh))


# ------------------------------------------------------------------------------------------------------ detail
def _clip_evidence(text: str) -> str:
    if len(text) <= TEXT_CAP:
        return text
    return text[: TEXT_CAP - 40].rstrip() + "… (see the student's sheet)"


def _build_detail(bk: _Book, ws, bundle: ExportBundle, ins: Insights, options: ExportOptions,
                  student_by_eval: dict) -> None:
    ws.set_tab_color(P.TAB["data"])
    heads = ["Evaluation", "Subject", "Email", "Scorecard", "Category", "KPI path", "KPI", "Weight (%)",
             "Counts toward overall", "Score", "Band", "Matched level"]
    if options.include_reasoning:
        heads += ["Matched guideline", "Reasoning", "Evidence", "Evidence source"]
    heads += ["Needs review", "Score variance", "Judge scores", "Student sheet"]
    rows: list[tuple] = []
    for r in ins.scored:
        kpis = {k.id: k for k in bundle.kpis_by_version.get(r.ev.version_id, [])}
        ordered = sorted((kid for kid in r.ev.results if kid in kpis), key=lambda kid: kpis[kid].order)
        for kid in ordered:
            rows.append((r, kpis[kid], r.ev.results[kid]))
    first = 3
    last = first + max(len(rows), 1)
    heads = _unique(heads)
    widths = [_fit([r.ev.name for r, _, _ in rows], 22, 34), _fit([r.ev.subject_name for r, _, _ in rows], 18, 28),
              _fit([r.ev.subject_email for r, _, _ in rows], 24, 36), _fit([r.ev.scorecard_name for r, _, _ in rows], 18, 30),
              _fit([k.category for _, k, _ in rows], 16, 30), _fit([k.path_text for _, k, _ in rows], 24, 44),
              _fit([k.name for _, k, _ in rows], 22, 36), 10, 14, 9, 20, 9]
    if options.include_reasoning:
        widths += [40, 60, 60, 26]
    widths += [10, 10, 16, 14]
    sub = ("One row per evaluation × KPI. Scores are coloured against the scorecard's target. Filter “Needs review” to "
           "see scores worth double-checking; evidence is laid out quote by quote on each student's sheet.")
    _title_block(bk, ws, "KPI Detail", sub, widths, 8)
    _table(ws, first, 0, last, len(heads) - 1, "tblKpiDetail", heads, _header(bk))
    ws.set_row(first, _header_height(heads, widths))
    b = dict(border=1, border_color=P.HAIRLINE)
    wrap = dict(text_wrap=True, valign="top", **b)
    for n, (r, k, res) in enumerate(rows, start=1):
        row = first + n
        guide = k.guidelines.get(res.matched_level) if res.matched_level is not None else ""
        bk.put(ws, row, 0, r.ev.name, text_wrap=True, **b)
        bk.put(ws, row, 1, r.ev.subject_name or "", text_wrap=True, **b)
        bk.email(ws, row, 2, r.ev.subject_email, text_wrap=True, **b)
        bk.put(ws, row, 3, r.ev.scorecard_name, text_wrap=True, **b)
        bk.put(ws, row, 4, k.category, text_wrap=True, **b)
        bk.put(ws, row, 5, k.path_text, text_wrap=True, **b)
        bk.put(ws, row, 6, k.name, text_wrap=True, **b)
        bk.put(ws, row, 7, k.weight, num_format="0.0", align="center", **b)
        bk.put(ws, row, 8, "Yes" if (k.included and k.is_leaf) else "No (info only)", align="center", **b)
        bk.score_cell(ws, row, 9, res.score, r.target, "0.0")
        bk.band_cell(ws, row, 10, res.score, r.target)
        bk.put(ws, row, 11, res.matched_level, align="center", **b)
        c = 12
        if options.include_reasoning:
            bk.put(ws, row, c, guide or "", **wrap)
            bk.put(ws, row, c + 1, res.reasoning, **wrap)
            bk.put(ws, row, c + 2, _clip_evidence(res.evidence_text), **wrap)
            bk.put(ws, row, c + 3, res.evidence_sources, **wrap)
            c += 4
        bk.put(ws, row, c, "Yes" if res.needs_review else "", align="center", bold=res.needs_review,
               **({**b, "font_color": "#B45309"} if res.needs_review else b))
        bk.put(ws, row, c + 1, res.variance, num_format="0.00", align="center", **b)
        bk.put(ws, row, c + 2, res.ensemble, align="center", **b)
        sheet = student_by_eval.get(r.ev.id)
        if sheet:
            bk.link(ws, row, c + 3, sheet, "Open ▸", align="center", **b)
        else:
            bk.put(ws, row, c + 3, "", **b)
    for c, w in enumerate(widths):
        ws.set_column(c, c, w)
    # freeze only Evaluation + Subject: freezing the first 7 columns left no room to scroll on a laptop screen
    ws.freeze_panes(first + 1, 2)
    _setup_print(ws, (first, first))


# ------------------------------------------------------------------------------------------------ student sheets
S_A, S_SCORE, S_BAND, S_WEIGHT, S_LEVEL, S_GUIDE = 46.0, 9.5, 20.0, 11.0, 10.0, 60.0


def _build_student_sheet(bk: _Book, ws, bundle: ExportBundle, ins: Insights, row: ScoredRow,
                         options: ExportOptions, names: dict[str, str], position: int) -> None:
    """One student: header card, category roll-up, then a KPI card per KPI (score row, then Why + Evidence rows)."""
    ev = row.ev
    ws.set_tab_color(row.band.fill)
    ws.hide_gridlines(2)
    ws.outline_settings(True, False, True, False)  # collapsible detail rows with the +/- control above them
    kpis = bundle.kpis_by_version.get(ev.version_id, [])
    text_mode = options.include_reasoning
    last = 5  # same width in both modes so the header card never gets cramped
    widths = [S_A, S_SCORE, S_BAND, S_WEIGHT, S_LEVEL, S_GUIDE]
    for c, w in enumerate(widths):
        ws.set_column(c, c, w)
    text_w = sum(widths[1:])  # width of the merged text area (columns B..last)
    pad = max(2, len(str(ins.n_scored)))

    # -- title band + sub-title + navigation ----------------------------------------------------------------
    _title_block(bk, ws, f"{position:0{pad}d}  ·  {ev.display_name}",
                 f"{ev.scorecard_name} (v{ev.version_number})  ·  evaluated by {ev.evaluator_name or '—'}"
                 f"  ·  {(_naive_utc(ev.finished_at or ev.submitted_at) or datetime(1970, 1, 1)).strftime('%Y-%m-%d %H:%M')} UTC",
                 widths)
    ws.set_row(2, 22)
    bk.link(ws, 2, 0, names["summary"], "◀ Summary", bold=True)
    if "leaderboard" in names:
        bk.link(ws, 2, 2, names["leaderboard"], "◀ Leaderboard", bold=True)
    ws.freeze_panes(3, 0)

    r = 4
    label = dict(bold=True, font_color=P.MUTED, valign="top")

    def info(name: str, writer) -> None:
        nonlocal r
        bk.put(ws, r, 0, name, **label)
        ws.merge_range(r, 1, r, last, "", bk.f())
        writer(r)
        ws.set_row(r, 20)
        r += 1

    info("Email", lambda rr: bk.email(ws, rr, 1, ev.subject_email) if (ev.subject_email or "").strip()
         else bk.put(ws, rr, 1, "—", font_color=P.MUTED))
    info("Scorecard", lambda rr: bk.put(ws, rr, 1, f"{ev.scorecard_name} (v{ev.version_number})"
                                        + (f" · {ev.domain}" if ev.domain else "")))
    info("Evaluation", lambda rr: bk.put(ws, rr, 1, ev.name))
    rank_text = f"{'Tied ' if row.tied else ''}#{row.rank} of {ins.n_scored}" + (
        f"  ·  percentile {row.percentile:.0%}" if row.percentile is not None else "")
    info("Rank", lambda rr: bk.put(ws, rr, 1, rank_text))
    r += 1

    # -- overall score ---------------------------------------------------------------------------------------
    ws.set_row(r, 32)
    bk.put(ws, r, 0, "Overall score", bold=True, font_size=14)
    bk.score_cell(ws, r, 1, row.score, row.target, "0.00", font_size=16)
    verdict = (f"{row.band.label}  ·  {row.delta_vs_target:+.2f} vs a target of {row.target:.1f}"
               + ("  (default target)" if row.target_defaulted else ""))
    bk.merge(ws, r, 2, r, last, verdict, bg_color=row.band.fill, font_color=row.band.font, bold=True, font_size=12,
             align="center", border=1, border_color=P.HAIRLINE)
    r += 2

    def section(title: str) -> None:
        nonlocal r
        ws.set_row(r, 22)
        bk.merge(ws, r, 0, r, last, title, bold=True, font_size=13, font_color="#FFFFFF", bg_color=P.SUBHEADER_FILL,
                 indent=1)
        r += 1

    # -- category roll-up --------------------------------------------------------------------------------------
    if ev.category_scores:
        section("Category roll-up")
        for c, h in enumerate(["Category", "Score", "Band", "vs target"]):
            bk.put(ws, r, c, h, bold=True, font_color="#FFFFFF", bg_color=P.HEADER_FILL, align="center",
                   border=1, border_color=P.BORDER)
        r += 1
        for cat, val in ev.category_scores.items():
            ws.set_row(r, _height(_lines(cat, S_A, 11, True)))
            bk.put(ws, r, 0, cat, bold=True, text_wrap=True, border=1, border_color=P.HAIRLINE)
            bk.score_cell(ws, r, 1, val, row.target, "0.0")
            bk.band_cell(ws, r, 2, val, row.target)
            bk.delta_cell(ws, r, 3, (val - row.target) if val is not None else None)
            r += 1
        r += 1

    # -- KPI cards ----------------------------------------------------------------------------------------------
    section("KPI scores" + ("  ·  reasoning and evidence below each score (use the +/- at the left to collapse)"
                            if text_mode else ""))
    heads = ["KPI", "Score", "Band", "Weight (%)", "Level", "Matched guideline" if text_mode else ""]
    for c, h in enumerate(heads):
        bk.put(ws, r, c, h, bold=True, font_color="#FFFFFF", bg_color=P.HEADER_FILL, align="center", text_wrap=True,
               border=1, border_color=P.BORDER)
    ws.set_row(r, 24)
    r += 1
    b = dict(border=1, border_color=P.HAIRLINE)
    last_cat = None
    for k in kpis:
        if not k.is_leaf:
            continue
        if k.category != last_cat:
            last_cat = k.category
            cat_val = ev.category_scores.get(k.category)
            txt = k.category if k.category != UNCATEGORISED else UNCATEGORISED
            if cat_val is not None:
                txt += f"  ·  category score {cat_val:.1f} ({P.target_band(cat_val, row.target).label})"
            ws.set_row(r, _height(_lines(txt, sum(widths), 11, True), 11, 22))
            bk.merge(ws, r, 0, r, last, txt, bold=True, bg_color=P.PANEL, text_wrap=True, indent=1,
                     border=1, border_color=P.HAIRLINE)
            r += 1
        res = ev.results.get(k.id)
        name = k.name + ("" if k.included else "  (info only)")
        guide = (k.guidelines.get(res.matched_level) or "") if (res and res.matched_level is not None) else ""
        nl = _lines(name, S_A - 2, 11, True)
        if text_mode:
            nl = max(nl, _lines(guide, S_GUIDE, 10))
        ws.set_row(r, _height(nl))
        bk.put(ws, r, 0, name, bold=True, text_wrap=True, valign="top", indent=1, **b)
        if res:
            bk.score_cell(ws, r, 1, res.score, row.target, "0.0", valign="top")
            bk.band_cell(ws, r, 2, res.score, row.target, valign="top")
        else:
            bk.put(ws, r, 1, "", bg_color=P.BLANK_FILL, **b)
            bk.put(ws, r, 2, "Not scored", align="center", font_color=P.MUTED, **b)
        bk.put(ws, r, 3, k.weight if k.included else None, num_format="0.0", align="center", valign="top", **b)
        bk.put(ws, r, 4, res.matched_level if res else None, align="center", valign="top", **b)
        bk.put(ws, r, 5, guide if text_mode else "", text_wrap=True, valign="top", font_size=10, font_color=P.MUTED, **b)
        r += 1
        if text_mode and res:
            r = _kpi_text_rows(bk, ws, r, res, row, text_w, last)
    _setup_print(ws)
    ws.repeat_rows(0, 1)


def _kpi_text_rows(bk: _Book, ws, r: int, res: ExportResult, row: ScoredRow, text_w: float, last: int) -> int:
    """Reasoning ("Why") and Evidence rows under a KPI's score row. Each is split across rows at 409pt so nothing is
    clipped; the rows are outline level 1 so the reader can collapse them to a plain score table."""
    size = 10
    band_color = row.band.fill if res is None else P.target_band(res.score, row.target).fill
    lab = dict(font_size=9, font_color=P.MUTED, italic=True, valign="top", text_wrap=True, align="right")

    def emit(label: str, chunks: list[str], first_fmt: dict, italic: bool = False) -> None:
        nonlocal r
        for i, chunk in enumerate(chunks):
            lbl = label if i == 0 else "(cont.)"
            h = _height(max(_lines(chunk, text_w, size), _lines(lbl, S_A, 9)), size)
            ws.set_row(r, h, None, {"level": 1})
            bk.put(ws, r, 0, lbl, **lab)
            bk.merge(ws, r, 1, r, last, chunk, font_size=size, italic=italic, text_wrap=True, valign="top",
                     indent=1, **first_fmt)
            r += 1

    why = split_for_cell(res.reasoning, text_w, size)
    if why:
        emit("Why", why, dict(bg_color=P.ZEBRA, border=1, border_color=P.HAIRLINE))
    for n, item in enumerate(res.evidence, start=1):
        label = f"Evidence {n}" + (f"\n{item.source}" if item.source else "")
        quote = f"“{item.quote}”"
        chunks = split_for_cell(quote, text_w, size)
        for i, chunk in enumerate(chunks):
            lbl = label if i == 0 else "(cont.)"
            h = _height(max(_lines(chunk, text_w, size), _lines(lbl, S_A, 9)), size)
            ws.set_row(r, h, None, {"level": 1})
            bk.put(ws, r, 0, lbl, **lab)
            bk.merge(ws, r, 1, r, last, chunk, font_size=size, italic=True, text_wrap=True, valign="top", indent=1,
                     left=5, left_color=band_color, bottom=1, bottom_color=P.HAIRLINE, right=1, right_color=P.HAIRLINE,
                     top=1, top_color=P.HAIRLINE)
            r += 1
    return r


# ------------------------------------------------------------------------------------------------------ notes
# ----------------------------------------------------------------------------------- KPI reference + guidelines
def _version_groups(bundle: ExportBundle) -> list[tuple]:
    """(version_id, scorecard, version number, domain, target, kpis) for every scorecard version in the selection,
    de-duplicated, ordered by scorecard name then version."""
    seen: dict = {}
    for ev in bundle.evaluations:
        seen.setdefault(ev.version_id, (ev.scorecard_name, ev.version_number, ev.domain, ev.target,
                                        bundle.kpis_by_version.get(ev.version_id, [])))
    groups = [(vid, *rest) for vid, rest in seen.items()]
    groups.sort(key=lambda g: (g[1].lower(), g[2]))
    return groups


def _section_weights(kpis: list) -> dict:
    """Total scoring weight under every non-leaf node (sum of its counting leaf descendants)."""
    by_id = {k.id: k for k in kpis}
    total: dict = {}
    for k in kpis:
        if not k.is_leaf or not k.included or k.weight is None:
            continue
        p = k.parent_id
        while p is not None and p in by_id:
            total[p] = total.get(p, 0.0) + k.weight
            p = by_id[p].parent_id
    return total


def _build_kpi_reference(bk: _Book, ws, bundle: ExportBundle) -> None:
    ws.set_tab_color(P.TAB["reference"])
    ws.hide_gridlines(2)
    groups = _version_groups(bundle)
    heads = ["Scorecard", "Version", "Type", "Section", "Sub-section", "KPI", "Weight (%)", "Counts toward overall",
             "Order", "Evaluations scored", "Average score"]
    n = len(heads)
    all_k = [k for g in groups for k in g[5]]
    widths = [_fit([g[1] for g in groups], 18, 30), 9, 13,
              _fit([k.path[0] if k.path else "" for k in all_k], 18, 30),
              _fit([" › ".join(k.path[1:]) for k in all_k], 16, 34), _fit([k.name for k in all_k], 28, 46) + 4,
              11, 14, 8, 12, 12]
    note = ("Every KPI of the scorecard version(s) in this export. Section = top-level group; Sub-section = nested groups; "
            "weights are the share of the overall score (info-only KPIs do not count). “Evaluations scored” and "
            "“Average score” cover only the exported evaluations; the average is coloured against the scorecard's target.")
    _title_block(bk, ws, "KPI Reference", note, widths)
    first = 3
    for c, h in enumerate(heads):
        bk.put(ws, first, c, h, bold=True, font_color="#FFFFFF", bg_color=P.HEADER_FILL, text_wrap=True,
               align="center", border=1, border_color=P.BORDER)
    ws.set_row(first, _header_height(heads, widths))
    r = first + 1
    b = dict(border=1, border_color=P.HAIRLINE)
    for vid, name, ver, domain, target, kpis in groups:
        scored = [e for e in bundle.evaluations if e.version_id == vid and e.is_scored]
        bits = [f"Scorecard: {name}", f"Version {ver}"]
        if domain:
            bits.append(f"Domain: {domain}")
        bits.append(f"Target score: {P.effective_target(target):.1f}" + (" (default; none set)" if P.target_is_default(target) else ""))
        bits.append(f"{sum(1 for k in kpis if k.is_leaf)} KPIs")
        title = "   ·   ".join(bits)
        ws.set_row(r, max(24.0, _height(_lines(title, sum(widths), 12, True), 12)))
        bk.merge(ws, r, 0, r, n - 1, title, bold=True, font_size=12, font_color="#FFFFFF", bg_color=P.SUBHEADER_FILL,
                 text_wrap=True, indent=1)
        r += 1
        if not kpis:
            bk.put(ws, r, 0, name, text_wrap=True, **b)
            bk.put(ws, r, 1, f"v{ver}", align="center", **b)
            bk.put(ws, r, 2, "—", align="center", **b)
            bk.merge(ws, r, 3, r, n - 1, "No KPIs are defined for this scorecard version.", font_color=P.MUTED, **b)
            r += 1
            continue
        totals = _section_weights(kpis)
        for k in kpis:
            depth = len(k.path)
            if k.is_leaf:
                typ, section = "KPI", (k.category if depth > 1 else UNCATEGORISED)
                sub = " › ".join(k.path[1:-1])
                kpi_name = k.name
                weight = k.weight if k.included else None
                counts = "Yes" if k.included else "No (info only)"
                vals = [x.results[k.id].score for x in scored if k.id in x.results]
                n_sc, avg = (len(vals), sum(vals) / len(vals)) if vals else (0, None)
                band = False
            else:
                typ = "Section" if depth == 1 else "Sub-section"
                section, sub, kpi_name = k.path[0], " › ".join(k.path[1:]), ""
                weight, counts, n_sc, avg, band = totals.get(k.id), "", None, None, True
            extra = dict(bg_color=P.PANEL, bold=True) if band else {}
            indent = 0 if band else min(6, max(0, depth - 1))
            bk.put(ws, r, 0, name, text_wrap=True, **extra, **b)
            bk.put(ws, r, 1, f"v{ver}", align="center", **extra, **b)
            bk.put(ws, r, 2, typ, align="center", **extra, **b)
            bk.put(ws, r, 3, section, text_wrap=True, **extra, **b)
            bk.put(ws, r, 4, sub, text_wrap=True, **extra, **b)
            bk.put(ws, r, 5, kpi_name, text_wrap=True, indent=indent, **extra, **b)
            bk.put(ws, r, 6, weight, num_format="0.0", align="center", **extra, **b)
            bk.put(ws, r, 7, counts, align="center", text_wrap=True, **extra, **b)
            bk.put(ws, r, 8, k.order + 1, align="center", **extra, **b)
            bk.put(ws, r, 9, n_sc, align="center", **extra, **b)
            if avg is not None:
                bk.score_cell(ws, r, 10, round(avg, 2), target, "0.0")
            else:
                bk.put(ws, r, 10, "", **extra, **b)
            nl = max(_lines(name, widths[0]), _lines(section, widths[3], bold=band), _lines(sub, widths[4], bold=band),
                     _lines(kpi_name, widths[5] - 2 * indent, bold=band), _lines(counts, widths[7]))
            ws.set_row(r, _height(nl))
            r += 1
    for c, w in enumerate(widths):
        ws.set_column(c, c, w)
    last = r - 1
    if last >= first + 1:
        ws.autofilter(first, 0, last, n - 1)
    ws.freeze_panes(first + 1, 0)
    _setup_print(ws, (first, first))


def _fit_cell_text(text: str, width_units: float, size: float = 11) -> str:
    """Trim `text` so its wrapped height stays within Excel's 409pt row limit; the cut is marked with an ellipsis."""
    max_lines = _max_lines(size)
    if _lines(text, width_units, size) <= max_lines:
        return text
    keep = int(max_lines * _cap(width_units, size) * 0.9)
    while keep > 10:
        cut = text[:keep].rstrip() + "…"
        if _lines(cut, width_units, size) <= max_lines:
            return cut
        keep = int(keep * 0.9)
    return text[:10] + "…"


def _build_guidelines(bk: _Book, ws, bundle: ExportBundle) -> None:
    ws.set_tab_color(P.TAB["reference"])
    ws.hide_gridlines(2)
    groups = _version_groups(bundle)
    rows = [(g, k) for g in groups for k in g[5] if k.is_leaf]
    has_zero = any(0 in k.guidelines for _, k in rows)
    levels = ([0] if has_zero else []) + list(range(1, 11))
    n = 4 + len(levels)
    fixed_w = [_fit([g[1] for g, _ in rows], 16, 22), 8, _fit([k.category for _, k in rows], 14, 20),
               _fit([k.name for _, k in rows], 22, 30)]
    widths = fixed_w + [LEVEL_COL_W] * len(levels)
    lvl_note = ("A Level 0 column is included because at least one KPI defines a level-0 rubric." if has_zero else
                "Levels 1–10 are shown; no KPI defines a level-0 rubric.")
    note = ("The scoring rubric: what each KPI must show to earn each score level. Columns run from red (low) to green "
            f"(high); these are absolute rubric levels, not target-relative. {lvl_note} A grey empty cell means no rubric "
            "text is recorded for that level.")
    merge_to = min(n - 1, 7)
    _title_block(bk, ws, "Scoring guidelines", note, widths, merge_to + 1)
    first = 3
    for c, h in enumerate(["Scorecard", "Version", "Section", "KPI"]):
        bk.put(ws, first, c, h, bold=True, font_color="#FFFFFF", bg_color=P.HEADER_FILL, text_wrap=True, align="center",
               border=1, border_color=P.BORDER)
    for i, lv in enumerate(levels):
        tag = "\n(lowest)" if lv == levels[0] else "\n(highest)" if lv == 10 else ""
        bk.put(ws, first, 4 + i, f"Level {lv}{tag}", bold=True, font_color=P.SCORE_FONT, bg_color=P.score_fill(lv),
               text_wrap=True, align="center", border=1, border_color=P.BORDER)
    ws.set_row(first, 36)
    b = dict(border=1, border_color=P.HAIRLINE)
    r = first + 1
    if not rows:
        bk.merge(ws, r, 0, r, 3, "No KPIs in this export.", font_color=P.MUTED)
        r += 1
    for (_vid, name, ver, _d, _t, _kp), k in rows:
        category = k.category if len(k.path) > 1 else UNCATEGORISED
        bk.put(ws, r, 0, name, text_wrap=True, valign="top", **b)
        bk.put(ws, r, 1, f"v{ver}", align="center", valign="top", **b)
        bk.put(ws, r, 2, category, text_wrap=True, valign="top", **b)
        bk.put(ws, r, 3, k.name, bold=True, text_wrap=True, valign="top", **b)
        nl = max(_lines(name, widths[0]), _lines(category, widths[2]), _lines(k.name, widths[3], bold=True))
        for i, lv in enumerate(levels):
            text = clean_text(k.guidelines.get(lv, ""), TEXT_CAP)
            if text:
                text = _fit_cell_text(text, LEVEL_COL_W)
                nl = max(nl, _lines(text, LEVEL_COL_W))
                bk.put(ws, r, 4 + i, text, text_wrap=True, valign="top", **b)
            else:
                bk.put(ws, r, 4 + i, "", bg_color=P.BLANK_FILL, **b)
        ws.set_row(r, _height(nl))
        r += 1
    for c, w in enumerate(widths):
        ws.set_column(c, c, w)
    last = r - 1
    if last >= first + 1:
        ws.autofilter(first, 0, last, n - 1)
    ws.freeze_panes(first + 1, 4)
    _setup_print(ws, (first, first))
    ws.set_paper(8)  # A3: ten wide rubric columns do not fit A4 legibly


def _legend_targets(ins: Insights) -> list[float]:
    return ins.targets[:3] or [ins.display_target]


def _build_notes(bk: _Book, ws, bundle: ExportBundle, ins: Insights, options: ExportOptions, versions: list,
                 shown: int, generated_at: datetime) -> None:
    ws.set_tab_color(P.TAB["notes"])
    ws.hide_gridlines(2)
    widths = [30.0, 110.0]
    ws.set_column(0, 0, widths[0])
    ws.set_column(1, 1, widths[1])
    _title_block(bk, ws, "Notes & methodology",
                 "How to read this workbook: definitions, the target-relative colours and anything left out.", widths)
    r = 3

    def section(title: str) -> None:
        nonlocal r
        ws.set_row(r, 22)
        bk.put(ws, r, 0, title, bold=True, font_color="#FFFFFF", bg_color=P.SUBHEADER_FILL, indent=1)
        bk.put(ws, r, 1, "", bg_color=P.SUBHEADER_FILL)
        r += 1

    def line(label: str, text: str) -> None:
        nonlocal r
        bk.put(ws, r, 0, label, bold=True, valign="top", text_wrap=True)
        bk.put(ws, r, 1, text, text_wrap=True, valign="top")
        ws.set_row(r, _height(max(_lines(text, widths[1]), _lines(label, widths[0], bold=True))))
        r += 1

    section("About this report")
    line("Generated", f"{generated_at.strftime('%Y-%m-%d %H:%M')} UTC by {clean_text(options.generated_by or 'Quality Scorecard', 100)}")
    line("Selection", f"{ins.n_total} evaluations exported."
         + (f" Filters in effect: {clean_text(options.filter_summary, 300)}" if options.filter_summary else ""))
    line("Contains personal data", "Names and email addresses of evaluated people. Share and store accordingly.")
    r += 1
    section("How scores work")
    line("Scale", "All scores are on a 0–10 scale.")
    line("Target score", "Each scorecard has a target score (set on the scorecard's Overview page). Every colour and every "
         "“vs target” figure compares a score with the target of ITS OWN scorecard. A scorecard with no target uses "
         f"{P.DEFAULT_TARGET:.1f}." + (" At least one scorecard in this export has no target." if ins.target_defaulted else ""))
    line("Colours", "Score cells carry a pale tint of the band colour; Band cells carry the solid colour. The number is always "
         "shown as well, and every overall score has a Band label, so colour is never the only signal.")
    line("Overall score", "The stored final score of the evaluation (a weighted average of KPI weights, or the scorecard's custom "
         "formula when one is defined — then category scores may not reproduce the overall).")
    line("Category score", "Weighted average of the scored KPIs inside a top-level category (renormalised over scored KPIs; "
         "info-only KPIs are excluded), the same way the app shows it.")
    line("Rank", "Competition ranking on the overall score, highest first: ties share a rank (1, 2, 2, 4) and show as T2.")
    line("Percentile", "(n − rank) / (n − 1): 100% = best, 0% = worst in this export.")
    line("At risk", f"An evaluation {AT_RISK_TEXT} (the Well below target and Critical bands). A KPI is “well below target” "
         "when its score is under 75% of the target; it is flagged when that happens in at least 30% of ≥3 evaluations. "
         "An uneven KPI has a standard deviation ≥ 2.0.")
    line("Needs review", "KPI scores the AI judge flagged for a human check (judge runs disagreed or confidence was low).")
    r += 1
    section("Sheets")
    line("Student sheets", "One sheet per exported student, in rank order (“01 Name”). Each has a header card, a category "
         "roll-up and a card per KPI with the reasoning (“Why”) and every piece of evidence on its own row. Use the +/- at "
         "the left of the sheet to collapse them. Not available when “include reasoning” is off: then only scores are shown.")
    line("KPI Reference", "Every KPI of the selected scorecard version(s), grouped by section and sub-section, with weights, "
         "whether it counts toward the overall score, and how many exported evaluations scored it.")
    line("Guidelines", "The scoring rubric text for each KPI at each score level (absolute levels, red = low, green = high), "
         "so a reader can see what a given score means. Always included, whatever the “include reasoning” setting.")
    r += 1
    for t in _legend_targets(ins):
        section(f"Colour bands — target {t:.1f}")
        for band, _lo, text in P.band_ranges(t):
            bk.put(ws, r, 0, band.label, bold=True, bg_color=band.fill, font_color=band.font, align="center")
            bk.put(ws, r, 1, text)
            r += 1
        r += 1
    if len(ins.targets) > 3:
        line("More targets", f"{len(ins.targets) - 3} further target(s) are in use ({', '.join(f'{t:.1f}' for t in ins.targets[3:])}); "
             "their bands follow the same rule: Meets = at the target, Near = at least 90% of it, Below = at least 75%, "
             "Well below = at least 50%, Critical = under 50%, Exceeds = 10% above (halfway to 10 for high targets).")
        r += 1
    section("Not exported & omitted")
    if ins.not_exported:
        line("Not exported", f"{ins.not_exported} selected evaluation{'s were' if ins.not_exported != 1 else ' was'} not exported "
             "(deleted, failed or not yet completed).")
    if len(versions) > shown:
        omitted = ", ".join(clean_text(label, 60) for _, label in versions[shown:])
        line("Matrices omitted", f"Only the first {shown} KPI matrices are included. Omitted: {omitted}.")
    if not ins.not_exported and len(versions) <= shown:
        line("None", "Every selected evaluation is included.")
    _setup_print(ws, landscape=False)


# ----------------------------------------------------------------------------------------------------- summary
def _build_summary(bk: _Book, ws, ins: Insights, options: ExportOptions, names: dict[str, str],
                   generated_at: datetime, matrix_names: dict, students: list[tuple[ScoredRow, str]]) -> None:
    ws.set_tab_color(P.TAB["summary"])
    ws.hide_gridlines(2)
    ws.set_default_row(18)
    # columns: A margin | B..F left panel | G gutter | H..L right panel
    widths = {0: 2, 1: 7, 2: 27, 3: 31, 4: 11, 5: 17, 6: 3, 7: 7, 8: 27, 9: 31, 10: 11, 11: 17, 12: 2}
    for c, w in widths.items():
        ws.set_column(c, c, w)
    L, R = 1, 7
    LAST = 11
    total_w = sum(widths[c] for c in range(1, LAST + 1))
    tdisp = ins.display_target
    ws.set_row(0, 38)
    bk.merge(ws, 0, 1, 0, LAST, "Evaluation Report", bold=True, font_size=22, font_color="#FFFFFF", bg_color=P.HEADER_FILL,
             indent=1)
    gen = f"Generated {generated_at.strftime('%Y-%m-%d %H:%M')} UTC by {clean_text(options.generated_by or 'Quality Scorecard', 80)}"
    cards = ", ".join(clean_text(n, 60) for n in ins.scorecard_names[:3]) + ("…" if len(ins.scorecard_names) > 3 else "")
    sub = f"{gen}  ·  {ins.n_total} evaluation{'s' if ins.n_total != 1 else ''}  ·  {cards}"
    if options.filter_summary:
        sub += f"  ·  {clean_text(options.filter_summary, 200)}"
    ws.set_row(1, _height(_lines(sub, total_w), 11, 22))
    bk.merge(ws, 1, 1, 1, LAST, sub, font_color="#D1D5DB", bg_color=P.SUBHEADER_FILL, text_wrap=True, indent=1)

    # --- verdict line ---------------------------------------------------------------------------------------------
    if ins.n_scored == 1 and ins.highest:
        h = ins.highest
        verdict = (f"{clean_text(h.ev.display_name, 60)}: {h.score:.1f} / 10 — {h.band.label} "
                   f"({h.delta_vs_target:+.1f} vs a target of {h.target:.1f})")
    elif ins.n_scored and ins.mean is not None and ins.pass_pct is not None:
        verdict = (f"Average {ins.mean:.1f} / 10 against {target_phrase(ins)}  ·  {ins.pass_pct:.0%} meet or exceed their "
                   f"target  ·  {len(ins.at_risk)} {AT_RISK_TEXT}")
    else:
        verdict = "Nothing to summarise: no evaluation has a final score."
    vfill = ins.highest.band.tint if ins.n_scored == 1 and ins.highest else P.target_band(ins.mean, tdisp).tint if ins.mean is not None else P.PANEL
    ws.set_row(2, _height(_lines(verdict, total_w, 13, True), 13, 28))
    bk.merge(ws, 2, 1, 2, LAST, verdict, bold=True, font_size=13, bg_color=vfill, text_wrap=True, indent=1,
             border=1, border_color=P.HAIRLINE)
    r = 4

    def section(title: str, col1: int = 1, col2: int = LAST, hint: str | None = None) -> None:
        nonlocal r
        bk.merge(ws, r, col1, r, col2, title + (f"   {hint}" if hint else ""), bold=True, font_size=13,
                 font_color="#FFFFFF", bg_color=P.HEADER_FILL, indent=1)
        ws.set_row(r, 22)

    # --- 1. Key takeaways -------------------------------------------------------------------------------
    section("Key takeaways")
    r += 1
    for i, t in enumerate(ins.takeaways, start=1):
        ws.set_row(r, _height(_lines(t, 182, 12), 12, 20))
        bk.put(ws, r, 1, i, bold=True, align="center", font_color="#FFFFFF", bg_color=P.SUBHEADER_FILL, font_size=12)
        bk.merge(ws, r, 2, r, LAST, t, text_wrap=True, font_size=12, bg_color=P.ZEBRA, border=1, border_color=P.HAIRLINE)
        r += 1
    r += 1

    # --- 2. Headline tiles --------------------------------------------------------------------------------
    section("At a glance")
    r += 1
    spans = [(1, 2), (3, 3), (4, 5), (7, 8), (9, 9), (10, 11)]
    spread = (ins.highest.score - ins.lowest.score) if ins.highest and ins.lowest and ins.n_scored > 1 else None
    tiles1 = [
        ("Evaluations exported", ins.n_total, "0", None),
        ("Average score", ins.mean, "0.0", (ins.mean, tdisp)),
        ("Median score", ins.median, "0.0", (ins.median, tdisp)),
        ("Highest", ins.highest.score if ins.highest else None, "0.0", (ins.highest.score, ins.highest.target) if ins.highest else None),
        ("Lowest", ins.lowest.score if ins.lowest else None, "0.0", (ins.lowest.score, ins.lowest.target) if ins.lowest else None),
        ("Meeting their target", ins.pass_pct, "0%", None),
    ]
    tiles2 = [
        ("Target score", f"{ins.common_target:.1f}" if ins.common_target is not None else "Mixed", "@", None),
        ("Below 75% of target", len(ins.at_risk), "0", None),
        ("KPIs needing review", ins.needs_review_total, "0", None),
        ("Score spread (max − min)", spread, "0.0", None),
        ("Std deviation", ins.stdev, "0.00", None),
        ("Missing emails", len(ins.missing_email), "0", None),
    ]
    for tiles in (tiles1, tiles2):
        ws.set_row(r, 18)
        ws.set_row(r + 1, 38)
        for (c1, c2), (label, val, nf, tint) in zip(spans, tiles, strict=True):
            bk.merge(ws, r, c1, r, c2, label, font_color=P.MUTED, font_size=10, align="center", bg_color=P.PANEL,
                     border=1, border_color=P.HAIRLINE)
            props = dict(bold=True, font_size=22, align="center", border=1, border_color=P.HAIRLINE, num_format=nf)
            props.update(bg_color=P.target_band(tint[0], tint[1]).tint if tint and tint[0] is not None else "#FFFFFF")
            if val is None:
                bk.merge(ws, r + 1, c1, r + 1, c2, "—", **{**props, "font_color": P.MUTED})
            elif isinstance(val, str):
                bk.merge(ws, r + 1, c1, r + 1, c2, val, **props)
            elif c1 == c2:
                bk.put(ws, r + 1, c1, val, **props)
            else:
                ws.merge_range(r + 1, c1, r + 1, c2, float(val), bk.f(**props))
        r += 3

    # --- 3. Distribution + chart ----------------------------------------------------------------------------
    if ins.n_scored >= 2:
        section("Score distribution", hint="how many evaluations fall in each target-relative band")
        r += 1
        top = r
        ranges = {b.key: txt for b, _lo, txt in P.band_ranges(tdisp)}
        mixed = len(ins.targets) > 1
        for c, h in zip((1, 2, 3, 4, 5), ("", "Band", "Score range" + ("" if mixed else f" (target {tdisp:.1f})"), "Count", "% of scored"), strict=True):
            bk.put(ws, r, c, h, bold=True, font_color="#FFFFFF", bg_color=P.SUBHEADER_FILL, align="center")
        for band, count, share in ins.distribution:
            r += 1
            bk.put(ws, r, 1, "", bg_color=band.fill, border=1, border_color=P.HAIRLINE)
            bk.put(ws, r, 2, band.label, bold=True, border=1, border_color=P.HAIRLINE)
            bk.put(ws, r, 3, "relative to each scorecard's target" if mixed else ranges.get(band.key, "—"),
                   border=1, border_color=P.HAIRLINE, align="center", text_wrap=True)
            bk.put(ws, r, 4, count, border=1, border_color=P.HAIRLINE, align="center", bold=True)
            bk.put(ws, r, 5, share, num_format="0%", border=1, border_color=P.HAIRLINE, align="center")
        ws.conditional_format(top + 1, 4, r, 4, {"type": "data_bar", "bar_color": "#9CA3AF", "bar_solid": True})
        chart = bk.wb.add_chart({"type": "column"})
        sn = ws.get_name()
        chart.add_series({
            "name": "Evaluations",
            "categories": [sn, top + 1, 2, r, 2],
            "values": [sn, top + 1, 4, r, 4],
            "points": [{"fill": {"color": b.fill}, "border": {"color": b.fill}} for b, _, _ in ins.distribution],
            "data_labels": {"value": True},
            "gap": 60,
        })
        chart.set_legend({"none": True})
        chart.set_title({"name": "Evaluations per band (against target)", "name_font": {"size": 12}})
        chart.set_y_axis({"major_gridlines": {"visible": True, "line": {"color": "#E5E7EB"}}, "min": 0,
                          "name": "Evaluations", "name_font": {"size": 10}})
        chart.set_size({"width": 640, "height": 215})
        ws.insert_chart(top, R, chart, {"x_offset": 4, "y_offset": 2, "object_position": 2,
                                        "description": "Column chart: number of evaluations in each target-relative band"})
        r += 2
        r = max(r, top + 11)

    # --- 4. Top performers / needs attention -----------------------------------------------------------------
    if ins.top:
        section("Top performers", L, 5)
        bk.merge(ws, r, R, r, LAST, "Needs attention (lowest scores)", bold=True, font_size=13, font_color="#FFFFFF",
                 bg_color=P.DANGER_FILL, indent=1)
        ws.set_row(r, 22)
        r += 1
        n_rows = max(len(ins.top), len(ins.attention))
        for i in range(1, n_rows + 1):  # both panels share rows: size each row for the taller side
            sides = [rows_[i - 1] for rows_ in (ins.top, ins.attention) if i <= len(rows_)]
            n = max(max(_lines(x.ev.display_name, 27, bold=True), _lines(x.ev.subject_email or "", 31)) for x in sides)
            ws.set_row(r + i, _height(n))
        for col0, rows_ in ((L, ins.top), (R, ins.attention)):
            for c, h in enumerate(("Rank", "Name", "Email", "Score", "Band")):
                bk.put(ws, r, col0 + c, h, bold=True, font_color="#FFFFFF", bg_color=P.SUBHEADER_FILL, align="center")
            for i, row in enumerate(rows_, start=1):
                rr = r + i
                b = dict(border=1, border_color=P.HAIRLINE)
                bk.put(ws, rr, col0, row.rank, align="center", num_format='"T"0' if row.tied else "0", bold=True, **b)
                bk.put(ws, rr, col0 + 1, row.ev.display_name, bold=True, text_wrap=True, **b)
                bk.email(ws, rr, col0 + 2, row.ev.subject_email, text_wrap=True, **b)
                bk.score_cell(ws, rr, col0 + 3, row.score, row.target, "0.00")
                bk.band_cell(ws, rr, col0 + 4, row.score, row.target)
        r += 1 + n_rows + 1

    # --- 5. Category averages + chart ------------------------------------------------------------------------
    if len(ins.categories) >= 2 or (ins.n_scored == 1 and ins.categories):
        section("Category scores", hint="average of the top-level categories" if ins.n_scored > 1 else "this evaluation")
        r += 1
        top = r
        for c, h in zip((1, 2, 3, 4, 5), ("Target", "Category", "Range (min – max)", "Average", "Evaluations"), strict=True):
            bk.put(ws, r, c, h, bold=True, font_color="#FFFFFF", bg_color=P.SUBHEADER_FILL, align="center")
        for cat in ins.categories:
            r += 1
            b = dict(border=1, border_color=P.HAIRLINE)
            ws.set_row(r, _height(_lines(cat.name, 27, bold=True)))
            bk.put(ws, r, 1, tdisp, num_format="0.0", align="center", font_color=P.MUTED, **b)
            bk.put(ws, r, 2, cat.name, bold=True, text_wrap=True, **b)
            bk.put(ws, r, 3, f"{cat.low:.1f} – {cat.high:.1f}", align="center", **b)
            bk.score_cell(ws, r, 4, cat.mean, tdisp, "0.0")
            bk.put(ws, r, 5, cat.n, align="center", **b)
        if len(ins.categories) >= 2:
            chart = bk.wb.add_chart({"type": "column"})
            sn = ws.get_name()
            chart.add_series({
                "name": "Average score",
                "categories": [sn, top + 1, 2, r, 2],
                "values": [sn, top + 1, 4, r, 4],
                "points": [{"fill": {"color": P.target_band(c.mean, tdisp).fill}, "border": {"color": "#6B7280"}}
                           for c in ins.categories],
                "data_labels": {"value": True, "num_format": "0.0"},
                "gap": 50,
            })
            line = bk.wb.add_chart({"type": "line"})
            line.add_series({
                "name": "Target", "categories": [sn, top + 1, 2, r, 2], "values": [sn, top + 1, 1, r, 1],
                "line": {"color": P.INK, "width": 1.75, "dash_type": "dash"}, "marker": {"type": "none"},
            })
            chart.combine(line)
            chart.set_legend({"position": "bottom"})
            chart.set_title({"name": "Average score by category vs target", "name_font": {"size": 12}})
            chart.set_y_axis({"min": 0, "max": 10, "major_gridlines": {"visible": True, "line": {"color": "#E5E7EB"}}})
            chart.set_size({"width": 640, "height": 260})
            ws.insert_chart(top, R, chart, {"x_offset": 4, "y_offset": 2, "object_position": 2,
                                            "description": "Column chart: average score for each category, with the target as a dashed line"})
            r = max(r, top + 14)
        r += 2

    # --- 6. Strongest / weakest KPIs ----------------------------------------------------------------------------
    if ins.strongest_kpis or ins.weakest_kpis:
        section("Strongest KPIs", L, 5)
        bk.merge(ws, r, R, r, LAST, "Weakest KPIs", bold=True, font_size=13, font_color="#FFFFFF", bg_color=P.DANGER_FILL,
                 indent=1)
        ws.set_row(r, 22)
        r += 1
        n_rows = max(len(ins.strongest_kpis), len(ins.weakest_kpis))
        for i in range(1, n_rows + 1):
            sides = [items[i - 1] for items in (ins.strongest_kpis, ins.weakest_kpis) if i <= len(items)]
            n = max(max(_lines(x.name, 27, bold=True), _lines(_kpi_category_text(x, ins.multi_scorecard), 31)) for x in sides)
            ws.set_row(r + i, _height(n))
        for col0, items in ((L, ins.strongest_kpis), (R, ins.weakest_kpis)):
            for c, h in enumerate(("#", "KPI", "Category" if not ins.multi_scorecard else "Scorecard · category", "Avg", "n")):
                bk.put(ws, r, col0 + c, h, bold=True, font_color="#FFFFFF", bg_color=P.SUBHEADER_FILL, align="center")
            for i, s in enumerate(items, start=1):
                _kpi_row(bk, ws, r + i, col0, i, s, ins.multi_scorecard)
        r += 1 + n_rows + 1

    # --- 7. Risk flags ---------------------------------------------------------------------------------------------
    section("Risk flags & things to check")
    r += 1
    for c1, c2, h in ((1, 1, "Count"), (2, 2, "Flag"), (3, LAST, "Details / where to look")):
        bk.merge(ws, r, c1, r, c2, h, bold=True, font_color="#FFFFFF", bg_color=P.SUBHEADER_FILL, align="center")
    if not ins.flags:
        r += 1
        bk.merge(ws, r, 1, r, LAST, "No risk flags — nothing unusual in this selection.", font_color="#065F46",
                 bg_color="#ECFDF5")
    for fl in ins.flags:
        r += 1
        fill = P.SEVERITY_FILL[fl.severity]
        ws.set_row(r, _height(max(_lines(fl.label, 27, bold=True), _lines(fl.details, 155))))
        bk.put(ws, r, 1, fl.count, bold=True, align="center", bg_color=fill, border=1, border_color=P.HAIRLINE)
        bk.put(ws, r, 2, fl.label, text_wrap=True, bold=True, bg_color=fill, border=1, border_color=P.HAIRLINE)
        bk.merge(ws, r, 3, r, LAST, fl.details, text_wrap=True, border=1, border_color=P.HAIRLINE)
    r += 2

    # --- 8. Legend (one band table per distinct target) ---------------------------------------------------------------
    section("How to read the colours", hint="every score is compared with its scorecard's target")
    r += 1
    legend_ts = _legend_targets(ins)
    for t in legend_ts:
        tag = f"Target {t:.1f}" + ("  (default: no target set)" if ins.target_defaulted and abs(t - P.DEFAULT_TARGET) < 1e-9 else "")
        bk.merge(ws, r, 1, r, 5, tag, bold=True, bg_color=P.PANEL, border=1, border_color=P.HAIRLINE)
        r += 1
        for c, h in zip((1, 2, 3, 4), ("", "Band", "Score range", "Cell tint"), strict=True):
            bk.put(ws, r, c, h, bold=True, font_color="#FFFFFF", bg_color=P.SUBHEADER_FILL, align="center")
        for band, _lo, text in P.band_ranges(t):
            r += 1
            bk.put(ws, r, 1, "", bg_color=band.fill, border=1, border_color=P.HAIRLINE)
            bk.put(ws, r, 2, band.label, bold=True, border=1, border_color=P.HAIRLINE)
            bk.put(ws, r, 3, text, border=1, border_color=P.HAIRLINE, align="center")
            bk.put(ws, r, 4, "score", bg_color=band.tint, border=1, border_color=P.HAIRLINE, align="center", font_size=9)
        r += 2
    if len(ins.targets) > 3:
        bk.merge(ws, r, 1, r, LAST, f"{len(ins.targets) - 3} further target(s) are in use; see the Notes sheet for how their bands are derived.",
                 font_color=P.MUTED)
        r += 1
    expl = ("Each score is compared with its scorecard's target: Exceeds / Meets = at or above the target, Near = within 10% "
            "below, Below = 10–25% below, Well below = 25–50% below, Critical = under half. Score cells use a pale tint of "
            "the band colour and Band cells the solid colour; the number is always shown too, so colour is never the only signal."
            + _default_note(ins))
    ws.set_row(r, _height(_lines(expl, total_w), 11, 32))
    bk.merge(ws, r, 1, r, LAST, expl, font_color=P.MUTED, text_wrap=True)
    r += 2

    # --- 9. Contents ----------------------------------------------------------------------------------------------------
    section("Contents")
    r += 1
    toc = []
    if "leaderboard" in names:
        toc.append((names["leaderboard"], "Everyone ranked by overall score, with name and email."))
    toc.append((names["evaluations"], "One row per evaluation: score, band, rank, evaluator, dates."))
    for nm in matrix_names.values():
        toc.append((nm, "Evaluations × KPIs, coloured against the target; spot weak KPIs at a glance."))
    if "detail" in names:
        toc.append((names["detail"], "Every KPI score with reasoning and evidence (filterable)."))
    toc.append((names["kpi_ref"], "The KPIs themselves: sections, sub-sections, weights and whether they count."))
    toc.append((names["guidelines"], "Scoring rubric for every KPI, level by level (red = low, green = high)."))
    toc.append((names["notes"], "Methodology, definitions and anything not exported."))
    for nm, desc in toc:
        # sheet names can be 31 chars: merge two columns for the link so it is never clipped by the description
        ws.merge_range(r, 2, r, 3, "", bk.f())
        bk.link(ws, r, 2, nm, nm)
        bk.merge(ws, r, 4, r, LAST, desc, font_color=P.MUTED)
        r += 1
    if students:
        r += 1
        shown = students[:MAX_SUMMARY_STUDENT_LINKS]
        bk.merge(ws, r, 1, r, LAST, f"Student sheets ({len(students)}) — one per student, in rank order; click to open",
                 bold=True, bg_color=P.PANEL, border=1, border_color=P.HAIRLINE)
        r += 1
        for i in range(0, len(shown), 2):
            for col, (row, nm) in zip((2, 8), shown[i:i + 2], strict=False):
                ws.merge_range(r, col, r, col + 1, "", bk.f())
                bk.link(ws, r, col, nm, f"{nm}  ·  {row.score:.1f}")
            r += 1
        if len(students) > len(shown):
            bk.merge(ws, r, 1, r, LAST, f"…and {len(students) - len(shown)} more: open them from the Leaderboard or Evaluations sheet.",
                     font_color=P.MUTED)
            r += 1
    _setup_print(ws, landscape=True)


def _kpi_category_text(s: KpiStat, multi: bool) -> str:
    return f"{s.scorecard} · {s.category}" if multi else (s.category if s.category != UNCATEGORISED else "")


def _kpi_row(bk: _Book, ws, r: int, col0: int, i: int, s: KpiStat, multi: bool) -> None:
    b = dict(border=1, border_color=P.HAIRLINE)
    bk.put(ws, r, col0, i, align="center", **b)
    bk.put(ws, r, col0 + 1, s.name, bold=True, text_wrap=True, **b)
    bk.put(ws, r, col0 + 2, _kpi_category_text(s, multi), text_wrap=True, **b)
    bk.score_cell(ws, r, col0 + 3, s.mean, s.target, "0.0")
    bk.put(ws, r, col0 + 4, s.n, align="center", **b)
