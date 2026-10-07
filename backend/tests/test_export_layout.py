"""Programmatic layout checks for the exported workbook (no database, no Excel needed).

Builds workbooks of several shapes and inspects them with openpyxl for the defects a human would see in Excel:
overlapping merges/tables, charts sitting on top of cells, clipped or `####` cells, wrapped text without enough
row height, and frozen panes that swallow the window.
"""

from __future__ import annotations

import math
import uuid
from datetime import datetime
from io import BytesIO

import pytest
from openpyxl import load_workbook
from openpyxl.utils import get_column_letter

from app.reporting.export_data import EvidenceItem, ExportBundle
from app.reporting.workbook import ExportOptions, build_workbook
from tests.reporting_fixtures import DEFAULT_LEAVES, make_bundle, make_eval, make_version

PX_PER_UNIT = 7.0  # Calibri 11 digit width
AVG_CHAR_PX = 6.3  # average mixed-text glyph at 11pt (conservative: real Calibri is ~5.8)
MAX_FROZEN_PX = 620  # frozen columns wider than this leave no room to scroll on a laptop screen


# ------------------------------------------------------------------------------------------------ geometry
def col_width(ws, idx: int) -> float:  # idx is 1-based
    for cd in ws.column_dimensions.values():
        if cd.min is not None and cd.min <= idx <= (cd.max or cd.min) and cd.width:
            return float(cd.width)
    return 8.43


def row_height(ws, r: int) -> float:  # r is 1-based
    rd = ws.row_dimensions.get(r)
    if rd is not None and rd.height:
        return float(rd.height)
    return float(ws.sheet_format.defaultRowHeight or 15)


def has_explicit_height(ws, r: int) -> bool:
    rd = ws.row_dimensions.get(r)
    return bool(rd is not None and rd.height)


def width_px(width_units: float) -> float:
    return width_units * PX_PER_UNIT + 5


def glyph_px(cell) -> float:
    size = (cell.font.sz or 11) / 11
    return AVG_CHAR_PX * size * (1.08 if cell.font.b else 1.0)


def text_lines(text: str, avail_px: float, gpx: float) -> int:
    per_line = max(1, int(avail_px / gpx))
    return sum(max(1, math.ceil(len(part) / per_line)) for part in str(text).split("\n"))


def needed_height(cell, avail_px: float) -> float:
    return text_lines(cell.value, avail_px, glyph_px(cell)) * (cell.font.sz or 11) * 1.32 + 2


def is_blank(v) -> bool:
    return v is None or v == ""


def formatted_number(v: float, fmt: str) -> str:
    f = fmt or "General"
    if "%" in f:
        return f"{v * 100:.0f}%"
    if f.startswith('"T"'):
        return f"T{int(v)}"
    if "0.00" in f:
        return f"{v:+.2f}" if f.startswith("+") else f"{v:.2f}"
    if "0.0" in f:
        return f"{v:.1f}"
    if f == "0":
        return f"{v:.0f}"
    return f"{v:g}"


# ---------------------------------------------------------------------------------------------------- checks
def check_sheet(ws) -> list[str]:
    problems: list[str] = []
    where = ws.title

    # 1. merged ranges never overlap
    ranges = list(ws.merged_cells.ranges)
    for i, a in enumerate(ranges):
        for b in ranges[i + 1:]:
            if not (a.max_row < b.min_row or b.max_row < a.min_row or a.max_col < b.min_col or b.max_col < a.min_col):
                problems.append(f"{where}: merged ranges overlap {a} / {b}")

    # 2. tables never overlap each other or a merged range
    tables = [(t.name, t.ref) for t in ws.tables.values()]
    from openpyxl.worksheet.cell_range import CellRange

    trs = [(n, CellRange(ref)) for n, ref in tables]
    for i, (na, a) in enumerate(trs):
        for nb, b in trs[i + 1:]:
            if not (a.max_row < b.min_row or b.max_row < a.min_row or a.max_col < b.min_col or b.max_col < a.min_col):
                problems.append(f"{where}: tables overlap {na} / {nb}")
        for m in ranges:
            if not (a.max_row < m.min_row or m.max_row < a.min_row or a.max_col < m.min_col or m.max_col < a.min_col):
                problems.append(f"{where}: table {na} overlaps merged range {m}")

    # 3. charts must not sit on top of any non-empty cell
    for ch in ws._charts:
        an = ch.anchor
        fr, to = an._from, getattr(an, "to", None)
        if to is None:
            problems.append(f"{where}: chart has a one-cell anchor, cannot verify placement")
            continue
        r1, r2 = fr.row + 1, to.row + 1 if to.rowOff > 0 else to.row
        c1, c2 = fr.col + 1, to.col + 1 if to.colOff > 0 else to.col
        for row in ws.iter_rows(min_row=r1, max_row=r2, min_col=c1, max_col=c2):
            for c in row:
                if not is_blank(c.value):
                    problems.append(f"{where}: chart (rows {r1}-{r2}, cols {c1}-{c2}) covers {c.coordinate}={str(c.value)[:30]!r}")
        # and it must fit inside the used column span / not cover another chart
    for i, a in enumerate(ws._charts):
        for b in ws._charts[i + 1:]:
            ra = (a.anchor._from.row, a.anchor.to.row)
            rb = (b.anchor._from.row, b.anchor.to.row)
            ca = (a.anchor._from.col, a.anchor.to.col)
            cb = (b.anchor._from.col, b.anchor.to.col)
            if not (ra[1] < rb[0] or rb[1] < ra[0] or ca[1] < cb[0] or cb[1] < ca[0]):
                problems.append(f"{where}: two charts overlap")

    merged_tl: dict[tuple[int, int], object] = {(m.min_row, m.min_col): m for m in ranges}
    in_merge: set[tuple[int, int]] = set()
    for m in ranges:
        for rr in range(m.min_row, m.max_row + 1):
            for cc in range(m.min_col, m.max_col + 1):
                if (rr, cc) != (m.min_row, m.min_col):
                    in_merge.add((rr, cc))

    def neighbour_filled(r: int, c: int) -> bool:
        if (r, c) in in_merge:
            return True
        return not is_blank(ws.cell(row=r, column=c).value)

    for row in ws.iter_rows():
        for cell in row:
            v = cell.value
            if is_blank(v) or (cell.row, cell.column) in in_merge:
                continue
            r, c = cell.row, cell.column
            m = merged_tl.get((r, c))
            w_units = sum(col_width(ws, k) for k in range(m.min_col, m.max_col + 1)) if m else col_width(ws, c)
            avail = width_px(w_units) - 6
            wrap = bool(cell.alignment.wrap_text)

            if isinstance(v, bool):
                continue
            if isinstance(v, datetime):
                need = 16 * glyph_px(cell) + 6
                if need > width_px(w_units):
                    problems.append(f"{where}!{cell.coordinate}: date shows #### (needs {need:.0f}px, has {width_px(w_units):.0f})")
                continue
            if isinstance(v, int | float):
                txt = formatted_number(float(v), cell.number_format)
                digit = 7.0 * (cell.font.sz or 11) / 11 * (1.08 if cell.font.b else 1.0)
                if len(txt) * digit + 6 > width_px(w_units):
                    problems.append(f"{where}!{cell.coordinate}: number {txt!r} shows #### (col {w_units:.1f} wide)")
                continue

            text = str(v)
            if wrap:
                need_h = needed_height(cell, avail)
                have = sum(row_height(ws, k) for k in range(m.min_row, m.max_row + 1)) if m else row_height(ws, r)
                if m and need_h > have + 0.5:
                    problems.append(f"{where}!{cell.coordinate}: merged wrapped text needs {need_h:.0f}pt, rows are {have:.0f}pt")
                elif not m and has_explicit_height(ws, r) and need_h > have + 0.5:
                    problems.append(f"{where}!{cell.coordinate}: wrapped text needs {need_h:.0f}pt, row is {have:.0f}pt")
                continue
            need = len(text.split("\n")[0]) * glyph_px(cell) + 6
            if need <= width_px(w_units):
                continue
            h = cell.alignment.horizontal
            end_col = (m.max_col if m else c) + 1
            clipped_right = neighbour_filled(r, end_col)
            clipped_left = h == "center" and neighbour_filled(r, (m.min_col if m else c) - 1)
            if m or clipped_right or clipped_left:
                problems.append(f"{where}!{cell.coordinate}: text clipped ({len(text)} chars in {w_units:.0f} wide): {text[:40]!r}")
            elif "\n" in text and not wrap:
                problems.append(f"{where}!{cell.coordinate}: multi-line text without wrap")

    # 4. frozen panes must leave room to scroll
    fp = ws.freeze_panes
    if fp:
        from openpyxl.utils.cell import column_index_from_string, coordinate_from_string

        col_letter, frozen_row = coordinate_from_string(fp)
        frozen_cols = column_index_from_string(col_letter) - 1
        px = sum(width_px(col_width(ws, k)) for k in range(1, frozen_cols + 1))
        if px > MAX_FROZEN_PX:
            problems.append(f"{where}: frozen columns are {px:.0f}px wide (> {MAX_FROZEN_PX})")
        fh = sum(row_height(ws, k) * 4 / 3 for k in range(1, frozen_row))
        if fh > 260:
            problems.append(f"{where}: frozen rows are {fh:.0f}px tall (> 260)")

    # 5. Excel cannot show a row taller than 409pt
    for rr, rd in ws.row_dimensions.items():
        if rd.height and rd.height > 409.01:
            problems.append(f"{where}: row {rr} is {rd.height}pt (> 409)")

    # 6. every student sheet: title band, nav row, outline-grouped text rows, no merged range crossing the A column
    if ws.title[:2].isdigit():
        if ws.sheet_view.showGridLines is not False:
            problems.append(f"{where}: gridlines should be hidden on a student sheet")
        for m in ranges:
            if m.min_col == 1 and m.min_row > 3 and m.max_col != m.min_col and m.max_col < 5:
                problems.append(f"{where}: merged range {m} is too narrow for a student sheet text block")
    return problems


# --------------------------------------------------------------------------------------------------- shapes
LONG = "Alexandria Bartholomew-Featherstonehaugh III"
LONG_EMAIL = "alexandria.bartholomew-featherstonehaugh@a-really-long-subdomain.example-company.co.uk"


def _rows(n: int, long: bool = False):
    out = []
    for i in range(n):
        score = round(10 - (i * 9.0 / max(n - 1, 1)), 1)
        name = f"{LONG} {i}" if long else f"Person {i}"
        email = (f"{i}." + LONG_EMAIL) if long else (None if i == 3 else f"person{i}@example.com")
        base = max(0, min(10, round(score)))
        out.append((name, email, score, [base, max(0, base - 1), base, min(10, base + 1)]))
    return out


def _many_categories(n_cat: int):
    leaves = []
    for i in range(n_cat):
        leaves.append((f"Category number {i} with a rather long descriptive name", f"KPI {i}.a — evaluates the candidate's depth", 10))
        leaves.append((f"Category number {i} with a rather long descriptive name", f"KPI {i}.b", 10))
    return leaves


def shape_bundles() -> dict[str, ExportBundle]:
    shapes: dict[str, ExportBundle] = {}
    shapes["one"] = make_bundle(_rows(1))
    shapes["two"] = make_bundle(_rows(2))
    shapes["six"] = make_bundle()
    shapes["forty_long"] = make_bundle(_rows(40, long=True))

    # many categories + very long KPI names
    leaves = _many_categories(12)
    vid, kpis = make_version(leaves)
    n = len([k for k in kpis if k.is_leaf])
    evs = [make_eval(vid, kpis, f"Cat Person {i}", f"cp{i}@example.com", 9 - i, [max(0, 9 - i - (j % 3)) for j in range(n)])
           for i in range(5)]
    shapes["many_categories"] = ExportBundle(evaluations=evs, kpis_by_version={vid: kpis})

    # reference sheets stress: 120 KPIs, long section names, long rubric text at every level, a level-0 rubric and one
    # rubric far beyond what a 409pt row can show
    leaves = [(f"Section {i // 6} with a fairly long descriptive title", f"KPI {i} measuring something quite specific and wordy", 1)
              for i in range(120)]
    vid, kpis = make_version(leaves)
    leaf_kpis = [k for k in kpis if k.is_leaf]
    for j, k in enumerate(leaf_kpis):
        k.guidelines = {lv: "Evidence of strong, consistent practice across several examples. " * (1 + (j + lv) % 6)
                        for lv in range(1, 11)}
    leaf_kpis[0].guidelines[0] = "Level zero rubric"
    leaf_kpis[1].guidelines[4] = "unbroken-rubric-text " * 400
    leaf_kpis[2].guidelines = {5: "Only the middle level is defined"}
    evs = [make_eval(vid, kpis, f"Ref Person {i}", f"rp{i}@example.com", 9 - i, [7] * 120) for i in range(3)]
    shapes["reference_stress"] = ExportBundle(evaluations=evs, kpis_by_version={vid: kpis})

    # mixed scorecards, one unscored/failed evaluation, long scorecard names
    v1, k1 = make_version()
    v2, k2 = make_version([("Delivery", "Estimation accuracy and planning discipline", 50), ("Quality", "Test coverage", 50)])
    sc1, sc2 = uuid.uuid4(), uuid.uuid4()
    evs = [
        make_eval(v1, k1, "Priya Shah", "priya@example.com", 9.1, [9, 9, 9, 9], scorecard="Senior Engineer Interview Scorecard 2026 Edition", scorecard_id=sc1),
        make_eval(v1, k1, "Rahul Kumar", None, 3.4, [4, 3, 3, 3], scorecard="Senior Engineer Interview Scorecard 2026 Edition", scorecard_id=sc1),
        make_eval(v2, k2, "Asha Menon", "asha@example.com", 7.5, [8, 7], scorecard="Delivery Review", scorecard_id=sc2),
        make_eval(v2, k2, "Dev Patel", "dev@example.com", 6.0, [6, 6], scorecard="Delivery Review", scorecard_id=sc2),
        make_eval(v2, k2, LONG, LONG_EMAIL, None, [], scorecard="Delivery Review", scorecard_id=sc2, status="failed"),
    ]
    evs[-1].error = "Transcription failed: the uploaded file was empty or corrupt. " * 4
    for e in evs[:2]:
        e.target = 9.0  # different scorecards, different targets (and one with none at all)
    for e in evs[2:4]:
        e.target = 4.0
    shapes["mixed"] = ExportBundle(evaluations=evs, kpis_by_version={v1: k1, v2: k2}, missing_ids=[uuid.uuid4()],
                                   excluded_count=2)

    # evidence stress: every shape of evidence on every KPI, plus a reasoning / quote far beyond one row
    vid, kpis = make_version(_many_categories(3))
    n = len([k for k in kpis if k.is_leaf])
    big = [EvidenceItem("An extremely long verbatim passage from the submission. " * 160, "Appendix C — Architecture review, section 4.2.1"),
           EvidenceItem("short", ""), EvidenceItem("Another mid-sized quote with a source. " * 9, "Slide 14 of the demo video, 03:12")]
    evs = []
    for i in range(3):
        e = make_eval(vid, kpis, f"Evidence Person {i}", f"ev{i}@example.com", 8 - i, [7 - i + (j % 3) for j in range(n)], evidence=big)
        for res in e.results.values():
            res.reasoning = ("The submission explains the approach clearly but misses the trade-offs. " * 90)[:6000]
        evs.append(e)
    shapes["evidence_stress"] = ExportBundle(evaluations=evs, kpis_by_version={vid: kpis})

    # a long name / email / scorecard on the student sheet header card, with a tiny target
    vid, kpis = make_version()
    e = make_eval(vid, kpis, LONG * 2, LONG_EMAIL, 3.9, [4, 4, 3, 4], scorecard="Senior Engineer Interview Scorecard 2026 Edition",
                  target=4.0)
    e2 = make_eval(vid, kpis, "Short", None, 2.0, [2] * 4, scorecard="Senior Engineer Interview Scorecard 2026 Edition", target=4.0)
    shapes["long_header_low_target"] = ExportBundle(evaluations=[e, e2], kpis_by_version={vid: kpis})
    return shapes


OPTION_SETS = {
    "reasoning": ExportOptions(include_reasoning=True, generated_by="A Fairly Long Evaluator Name Here",
                               filter_summary="Workflow: Senior Engineer Interview Scorecard 2026 · Status: Completed · " * 2),
    "no_reasoning": ExportOptions(include_reasoning=False),
}


@pytest.mark.parametrize("opts", list(OPTION_SETS))
@pytest.mark.parametrize("shape", list(shape_bundles()))
def test_workbook_layout_has_no_defects(shape: str, opts: str) -> None:
    bundle = shape_bundles()[shape]
    data = build_workbook(bundle, OPTION_SETS[opts])
    wb = load_workbook(BytesIO(data))
    problems: list[str] = []
    for ws in wb.worksheets:
        problems += check_sheet(ws)
    assert not problems, f"[{shape}/{opts}] " + "\n".join(problems[:40]) + f"\n({len(problems)} total)"


def test_checker_detects_a_planted_chart_overlap() -> None:
    """Guard the checker itself: a chart over a filled cell must be reported."""
    import xlsxwriter

    buf = BytesIO()
    wb = xlsxwriter.Workbook(buf, {"in_memory": True})
    ws = wb.add_worksheet("S")
    ws.write(2, 2, "in the way")
    ws.write_column(0, 5, [1, 2, 3])
    ch = wb.add_chart({"type": "column"})
    ch.add_series({"values": ["S", 0, 5, 2, 5]})
    ws.insert_chart(1, 1, ch)
    wb.close()
    problems = check_sheet(load_workbook(BytesIO(buf.getvalue())).worksheets[0])
    assert any("chart" in p and "C3" in p for p in problems)


def test_student_sheet_per_exported_student_and_evidence_never_lost() -> None:
    bundle = shape_bundles()["evidence_stress"]
    wb = load_workbook(BytesIO(build_workbook(bundle, ExportOptions(include_reasoning=True))))
    students = [n for n in wb.sheetnames if n[:2].isdigit()]
    assert len(students) == 3
    ws = wb[students[0]]
    quote_chars = sum(len(str(c.value)) for row in ws.iter_rows(min_col=2, max_col=2) for c in row
                      if isinstance(c.value, str) and c.value.startswith(("“An extremely", "An extremely")))
    assert quote_chars > 6000  # the 8000-char quote is split across continuation rows, not clipped
    conts = [c.value for row in ws.iter_rows(min_col=1, max_col=1) for c in row if c.value == "(cont.)"]
    assert conts, "long reasoning / evidence must continue on following rows"


def test_col_letters_helper_sanity() -> None:
    assert get_column_letter(1) == "A" and DEFAULT_LEAVES
