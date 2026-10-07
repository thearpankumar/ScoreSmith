"""End-to-end tests for `POST /api/v1/evaluations/export` (real FastAPI app + real Postgres).

Seeds scorecards / KPI trees / evaluations straight through the ORM (the DB triggers still run: leaf weights
must sum to 100 per version and results must belong to the evaluation's version), calls the endpoint, and
reopens the downloaded workbook with openpyxl.
"""

from __future__ import annotations

import re
import uuid
from io import BytesIO

from fastapi.testclient import TestClient
from openpyxl import load_workbook
from sqlalchemy import event
from sqlalchemy.orm import Session
from sqlalchemy_utils import Ltree

from app.models.enums import EvaluationStatus, rag_band_for_score
from app.models.evaluation import Evaluation
from app.models.evaluation_kpi_result import EvaluationKpiResult
from app.models.kpi_guideline import KpiGuideline
from app.models.kpi_node import KpiNode
from app.models.scorecard import Scorecard
from app.models.scorecard_version import ScorecardVersion
from app.models.user import User

XLSX = "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"
EVIL_NAMES = ['=HYPERLINK("http://evil","x")', "+cmd|' /C calc'!A0", "@SUM(1+1)", "-2+3"]
LEAVES = [("Communication", "Clarity", 30), ("Communication", "Listening", 20), ("Technical", "Depth", 30),
          ("Technical", "Edge cases", 20)]


def _user(db: Session, name: str = "Export Tester") -> User:
    u = User(email=f"exp-{uuid.uuid4().hex[:8]}@example.com", name=name)
    db.add(u)
    db.commit()
    return u


def _scorecard(db: Session, owner: User, name: str = "Interview Scorecard", target: float | None = None):
    card = Scorecard(name=name, owner_id=owner.id, domain="Hiring", target_score=target)
    db.add(card)
    db.flush()
    version = ScorecardVersion(scorecard_id=card.id, version_number=1, created_by=owner.id)
    db.add(version)
    db.flush()
    card.current_version_id = version.id
    cats: dict[str, KpiNode] = {}
    leaves: list[KpiNode] = []
    for i, (cat, name_, weight) in enumerate(LEAVES):
        if cat not in cats:
            cid = uuid.uuid4()
            cats[cat] = KpiNode(id=cid, scorecard_version_id=version.id, parent_id=None, path=Ltree(cid.hex), level=1,
                                name=cat, weight=None, display_order=len(cats))
            db.add(cats[cat])
            db.flush()
        lid = uuid.uuid4()
        leaf = KpiNode(id=lid, scorecard_version_id=version.id, parent_id=cats[cat].id,
                       path=Ltree(f"{cats[cat].id.hex}.{lid.hex}"), level=2, name=name_, weight=weight, display_order=i)
        db.add(leaf)
        leaves.append(leaf)
    db.flush()
    for leaf in leaves:
        for level in (0, 5, 10):
            db.add(KpiGuideline(kpi_node_id=leaf.id, score_level=level, qualitative_text=f"{leaf.name} level {level}"))
    db.commit()
    return card, version, leaves


def _evaluation(db: Session, owner: User, version: ScorecardVersion, leaves: list[KpiNode], name: str, email: str | None,
                score: float | None, leaf_scores: list[float], status: EvaluationStatus = EvaluationStatus.COMPLETED):
    ev = Evaluation(
        scorecard_version_id=version.id, name=f"{name} eval", evaluated_by=owner.id, status=status,
        final_weighted_score=score, rag_band=rag_band_for_score(score) if score is not None else None,
        subject_name=name, subject_email=email, source_kind="upload",
    )
    db.add(ev)
    db.flush()
    for leaf, s in zip(leaves, leaf_scores, strict=False):
        db.add(EvaluationKpiResult(evaluation_id=ev.id, kpi_node_id=leaf.id, score=s, matched_guideline_level=round(s),
                                   reasoning_text=f"Because {leaf.name}",
                                   evidence_quotes=["quote one", {"quote": "quote two", "section": "Section 3"}]))
    db.commit()
    return ev


def _post(client: TestClient, user: User, ids, **extra):
    return client.post("/api/v1/evaluations/export", json={"evaluation_ids": [str(i) for i in ids], **extra},
                       headers={"X-User-Id": str(user.id)})


def _wb(resp):
    assert resp.status_code == 200, resp.text
    return load_workbook(BytesIO(resp.content))


def _seed_five(db: Session):
    owner = _user(db)
    card, version, leaves = _scorecard(db, owner)
    rows = [("Priya Shah", "priya@example.com", 9.1, [9, 9, 9, 9]), ("Rahul Kumar", "rahul@example.com", 3.4, [4, 3, 3, 3]),
            ("Asha Menon", "asha@example.com", 7.5, [8, 7, 8, 7]), ("Dev Patel", None, 7.5, [7, 8, 7, 8]),
            ("Omar Ali", "omar@example.com", 8.2, [8, 8, 9, 8])]
    evs = [_evaluation(db, owner, version, leaves, n, e, s, ls) for n, e, s, ls in rows]
    return owner, card, version, leaves, evs


# ----------------------------------------------------------------------------------------------- validation / auth
def test_export_requires_auth(client: TestClient) -> None:
    r = client.post("/api/v1/evaluations/export", json={"evaluation_ids": [str(uuid.uuid4())]})
    assert r.status_code == 401


def test_export_request_validation(client: TestClient, db_session: Session) -> None:
    user = _user(db_session)
    assert _post(client, user, []).status_code == 422
    assert _post(client, user, [uuid.uuid4() for _ in range(201)]).status_code == 422
    r = client.post("/api/v1/evaluations/export", json={"evaluation_ids": ["not-a-uuid"]},
                    headers={"X-User-Id": str(user.id)})
    assert r.status_code == 422


def test_export_unknown_ids_404(client: TestClient, db_session: Session) -> None:
    user = _user(db_session)
    assert _post(client, user, [uuid.uuid4(), uuid.uuid4()]).status_code == 404


def test_export_with_no_completed_evaluation_422(client: TestClient, db_session: Session) -> None:
    owner = _user(db_session)
    _, version, leaves = _scorecard(db_session, owner)
    queued = _evaluation(db_session, owner, version, leaves, "Waiting", None, None, [], EvaluationStatus.QUEUED)
    r = _post(client, owner, [queued.id])
    assert r.status_code == 422 and "nothing to export" in r.json()["detail"]


# ------------------------------------------------------------------------------------------------------ happy path
def test_export_workbook_structure_headers_and_leaderboard(client: TestClient, db_session: Session) -> None:
    owner, _, _, _, evs = _seed_five(db_session)
    r = _post(client, owner, [e.id for e in evs], filter_summary="Workflow: Interview Scorecard")
    assert r.status_code == 200
    assert r.headers["content-type"].startswith(XLSX)
    assert re.fullmatch(r'attachment; filename="evaluations_Interview-Scorecard_\d{8}-\d{4}\.xlsx"; filename\*=UTF-8\'\'.+',
                        r.headers["content-disposition"])
    assert r.headers["cache-control"] == "no-store" and r.headers["x-content-type-options"] == "nosniff"
    assert r.headers["x-export-count"] == "5" and r.headers["x-export-skipped"] == "0"

    wb = load_workbook(BytesIO(r.content))
    students = ["01 Priya Shah", "02 Omar Ali", "03 Asha Menon", "04 Dev Patel", "05 Rahul Kumar"]
    assert wb.sheetnames == ["Summary", "Leaderboard", "Evaluations", "KPI Matrix", "KPI Detail", *students,
                             "KPI Reference", "Guidelines", "Notes"]
    summary = wb["Summary"]
    assert summary["B1"].value == "Evaluation Report"
    rows = {str(c.value): c.row for row in summary.iter_rows() for c in row if isinstance(c.value, str)}
    assert rows["Key takeaways"] < rows["At a glance"] < rows["Score distribution   how many evaluations fall in each target-relative band"]
    assert any(t.startswith("5 evaluations are included") for t in rows)

    board = wb["Leaderboard"]
    head = next(c.row for row in board.iter_rows() for c in row if c.value == "Rank")
    data = [[c.value for c in row] for row in board.iter_rows(min_row=head + 1, max_row=head + 5)]
    assert [d[1] for d in data] == ["Priya Shah", "Omar Ali", "Asha Menon", "Dev Patel", "Rahul Kumar"]
    assert [d[0] for d in data] == [1, 2, 3, 3, 5]
    assert board.cell(head + 1, 3).hyperlink.target == "mailto:priya@example.com"
    assert board.cell(head + 4, 3).value is None  # Dev Patel: no email
    # colours are static and target-relative (these scorecards have no target, so the default 7.0 applies)
    for ws_ in wb.worksheets:
        assert not [r_ for rng in ws_.conditional_formatting for r_ in rng.rules if r_.type == "colorScale"]
    assert board.cell(head + 1, 5).value == "Exceeds target" and board.cell(head + 5, 5).value == "Critical"
    assert board.cell(head + 1, 4).fill.fgColor.rgb[-6:].upper() == "A5D6A7"

    # a student sheet shows the reasoning and every piece of evidence (plain string and object with its source)
    sheet = wb["01 Priya Shah"]
    col_a = {c.row: c.value for row in sheet.iter_rows(min_col=1, max_col=1) for c in row if c.value}
    clarity = next(r_ for r_, v in col_a.items() if v == "Clarity")
    assert col_a[clarity + 1] == "Why" and sheet.cell(clarity + 1, 2).value == "Because Clarity"
    assert sheet.cell(clarity + 2, 2).value == "“quote one”"
    assert col_a[clarity + 3] == "Evidence 2\nSection 3" and sheet.cell(clarity + 3, 2).value == "“quote two”"


def test_export_include_reasoning_false(client: TestClient, db_session: Session) -> None:
    owner, _, _, _, evs = _seed_five(db_session)
    detail = _wb(_post(client, owner, [e.id for e in evs], include_reasoning=False))["KPI Detail"]
    headers = {c.value for row in detail.iter_rows() for c in row if isinstance(c.value, str)}
    assert "Score" in headers and "Reasoning" not in headers and "Evidence" not in headers
    full = _wb(_post(client, owner, [e.id for e in evs]))["KPI Detail"]
    assert "Reasoning" in {c.value for row in full.iter_rows() for c in row if isinstance(c.value, str)}


def test_export_includes_kpi_reference_and_guidelines_sheets(client: TestClient, db_session: Session) -> None:
    owner, _, _, _, evs = _seed_five(db_session)
    for ids, flag in (([evs[0].id], True), ([e.id for e in evs], False)):  # one and many; reasoning flag irrelevant
        wb = _wb(_post(client, owner, ids, include_reasoning=flag))
        assert wb.sheetnames[-3:] == ["KPI Reference", "Guidelines", "Notes"]

        ref = [[c.value for c in row] for row in wb["KPI Reference"].iter_rows()]
        body = [r for r in ref if r[2] in ("Section", "Sub-section", "KPI")]
        assert [(r[2], r[3], r[5]) for r in body] == [
            ("Section", "Communication", None), ("KPI", "Communication", "Clarity"), ("KPI", "Communication", "Listening"),
            ("Section", "Technical", None), ("KPI", "Technical", "Depth"), ("KPI", "Technical", "Edge cases"),
        ]
        weights = {(r[2], r[3], r[5]): r[6] for r in body}
        assert weights[("Section", "Communication", None)] == 50.0 and weights[("KPI", "Technical", "Depth")] == 30.0
        assert all(r[7] == "Yes" for r in body if r[2] == "KPI")

        gl = [[c.value for c in row] for row in wb["Guidelines"].iter_rows()]
        header = next(r for r in gl if r[0] == "Scorecard")
        # the seeded rubrics define levels 0, 5 and 10, so the Level 0 column is present
        assert [str(h).split("\n")[0] for h in header[4:]] == [f"Level {i}" for i in range(0, 11)]
        clarity = next(r for r in gl if r[3] == "Clarity")
        assert clarity[4] == "Clarity level 0" and clarity[4 + 5] == "Clarity level 5" and clarity[4 + 10] == "Clarity level 10"
        assert clarity[4 + 1] is None  # level 1 has no rubric -> blank


def test_export_skips_deleted_and_non_completed_and_lists_them(client: TestClient, db_session: Session) -> None:
    owner, _, version, leaves, evs = _seed_five(db_session)
    failed = _evaluation(db_session, owner, version, leaves, "Broken Run", "b@example.com", None, [], EvaluationStatus.FAILED)
    ids = [evs[0].id, evs[1].id, failed.id, uuid.uuid4()]
    r = _post(client, owner, ids)
    assert r.status_code == 200
    assert r.headers["x-export-count"] == "2"  # only the 2 completed, scored evaluations are exported
    assert r.headers["x-export-skipped"] == "2"  # 1 deleted + 1 failed
    wb = load_workbook(BytesIO(r.content))
    notes = " ".join(str(c.value) for row in wb["Notes"].iter_rows() for c in row if c.value)
    assert "2 selected evaluations were not exported (deleted, failed or not yet completed)" in notes
    for ws_ in wb.worksheets:  # the failed run is never exported anywhere: no row, no sheet, no mention
        assert not any("Broken Run" in str(c.value) for row in ws_.iter_rows() for c in row if c.value), ws_.title
    assert not any("Broken" in n for n in wb.sheetnames)


def test_export_of_only_failed_evaluations_is_422_not_404(client: TestClient, db_session: Session) -> None:
    owner = _user(db_session)
    _, version, leaves = _scorecard(db_session, owner)
    failed = _evaluation(db_session, owner, version, leaves, "Broken Run", None, None, [], EvaluationStatus.FAILED)
    r = _post(client, owner, [failed.id])
    assert r.status_code == 422 and "nothing to export" in r.json()["detail"]


def test_export_colours_follow_the_scorecards_target(client: TestClient, db_session: Session) -> None:
    owner = _user(db_session)
    _, version, leaves = _scorecard(db_session, owner, "Low Target Card", target=4.0)
    evs = [_evaluation(db_session, owner, version, leaves, "Pat", "pat@example.com", 4.2, [4, 4, 4, 5]),
           _evaluation(db_session, owner, version, leaves, "Quinn", "quinn@example.com", 1.9, [2, 2, 2, 2])]
    wb = _wb(_post(client, owner, [e.id for e in evs]))
    board = wb["Leaderboard"]
    head = next(c.row for row in board.iter_rows() for c in row if c.value == "Rank")
    assert board.cell(head + 1, 2).value == "Pat" and board.cell(head + 1, 5).value == "Meets target"  # 4.2 vs target 4
    assert board.cell(head + 2, 5).value == "Critical"  # 2.0 < half of the target
    assert board.cell(head + 1, 4).fill.fgColor.rgb[-6:].upper() == "C8E6C9"
    texts = " ".join(str(c.value) for row in wb["Summary"].iter_rows() for c in row if isinstance(c.value, str))
    assert "Target 4.0" in texts and "≥ 4.4" in texts


def test_export_neutralises_formula_injection(client: TestClient, db_session: Session) -> None:
    owner = _user(db_session)
    _, version, leaves = _scorecard(db_session, owner)
    evs = [_evaluation(db_session, owner, version, leaves, bad, f"{i}@example.com", 8.0 - i, [8, 8, 8, 8])
           for i, bad in enumerate(EVIL_NAMES)]
    evs.append(_evaluation(db_session, owner, version, leaves, "Plain", "=1+1@example.com", 5.0, [5, 5, 5, 5]))
    wb = _wb(_post(client, owner, [e.id for e in evs], filter_summary="=cmd|' /C calc'!A0"))
    seen = set()
    for ws in wb.worksheets:
        for row in ws.iter_rows():
            for c in row:
                assert c.data_type != "f", (ws.title, c.coordinate)
                if c.value in EVIL_NAMES:
                    seen.add(c.value)
                    assert c.data_type == "s" and c.quotePrefix
    assert seen == set(EVIL_NAMES)


def test_export_mixed_scorecards_get_separate_matrices(client: TestClient, db_session: Session) -> None:
    owner = _user(db_session)
    _, v1, l1 = _scorecard(db_session, owner, "Scorecard / One: [x]")
    _, v2, l2 = _scorecard(db_session, owner, "Scorecard / One: [x]!!")
    evs = [_evaluation(db_session, owner, v1, l1, "A", "a@example.com", 9.0, [9] * 4),
           _evaluation(db_session, owner, v2, l2, "B", "b@example.com", 6.0, [6] * 4)]
    r = _post(client, owner, [e.id for e in evs])
    assert "evaluations_multi_" in r.headers["content-disposition"]
    wb = load_workbook(BytesIO(r.content))
    matrices = [n for n in wb.sheetnames if n.startswith("Matrix")]
    assert len(matrices) == 2 and len({m.lower() for m in matrices}) == 2
    assert all(len(n) <= 31 and not any(ch in n for ch in "[]:*?/\\") for n in wb.sheetnames)


def test_export_single_evaluation_has_no_leaderboard(client: TestClient, db_session: Session) -> None:
    owner, _, _, _, evs = _seed_five(db_session)
    wb = _wb(_post(client, owner, [evs[0].id]))
    assert "Leaderboard" not in wb.sheetnames and "01 Priya Shah" in wb.sheetnames


def test_export_uses_a_bounded_number_of_queries(client: TestClient, db_session: Session) -> None:
    from app.db import engine

    owner, _, _, _, evs = _seed_five(db_session)
    statements: list[str] = []

    def count(conn, cursor, statement, parameters, context, executemany):  # noqa: ARG001
        statements.append(statement)

    event.listen(engine.sync_engine, "before_cursor_execute", count)
    try:
        assert _post(client, owner, [e.id for e in evs]).status_code == 200
    finally:
        event.remove(engine.sync_engine, "before_cursor_execute", count)
    assert len(statements) <= 8, statements
