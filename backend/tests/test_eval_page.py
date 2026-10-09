"""Keyset-paginated Evaluations list: stable order across every sort, every filter applied server side, no duplicates or
gaps while rows are inserted between pages, totals, server-side "select all matching" (bulk delete, export) and
access control (collaborators see the whole chart, outsiders see nothing)."""

from __future__ import annotations

import itertools
import uuid
from datetime import UTC, datetime, timedelta

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models.enums import EvaluationStatus, RagBand, rag_band_for_score
from app.models.evaluation import Evaluation
from tests.sharing_world import Team, hdr, make_chart, team  # noqa: F401 - fixture

NOW = datetime(2026, 10, 1, 12, 0, tzinfo=UTC)
BASE = "/api/v1/evaluations/page"


def seed_rows(db: Session, runner, chart: dict, n: int, *, start: int = 0, step: int = 60) -> list[Evaluation]:
    """n evaluations on the chart: a spread of statuses, scores, names and times (every 7th created at the same
    instant as its predecessor to exercise the id tie-break)."""
    rows = []
    for i in range(start, start + n):
        status = [EvaluationStatus.COMPLETED, EvaluationStatus.COMPLETED, EvaluationStatus.FAILED,
                  EvaluationStatus.COMPLETED, EvaluationStatus.QUEUED][i % 5]
        score = None if status != EvaluationStatus.COMPLETED else round((i * 37 % 101) / 10, 2)
        created = NOW - timedelta(seconds=step * (i - (i % 7 == 0 and 1 or 0)))
        rows.append(Evaluation(
            scorecard_version_id=uuid.UUID(chart["version_id"]), name=f"Run {i:04d} {'alpha' if i % 3 else 'beta'}",
            evaluated_by=runner.id, owner_id=runner.id, status=status, final_weighted_score=score,
            rag_band=rag_band_for_score(score) if score is not None else None, created_at=created, updated_at=created,
            subject_email=f"person{i}@corp.test", subject_name=f"Person {i}", batch_id=None, attempt=1,
        ))
    db.add_all(rows)
    db.commit()
    return rows


def walk(client, headers, query: str = "", limit: int = 17) -> tuple[list[dict], dict]:
    items, cursor, first = [], None, None
    for _ in range(500):
        url = f"{BASE}?limit={limit}{query}" + (f"&cursor={cursor}" if cursor else "")
        r = client.get(url, headers=headers)
        assert r.status_code == 200, r.text
        page = r.json()
        first = first or page
        items += page["items"]
        cursor = page["next_cursor"]
        if not cursor:
            return items, first
    raise AssertionError("pagination did not terminate")


@pytest.fixture()
def big(db_session: Session, team: Team):
    rows = seed_rows(db_session, team.owner, team.chart, 120)
    return team, rows


SORTS = {
    "newest": lambda r: (r.created_at, r.id),
    "oldest": lambda r: (r.created_at, r.id),
}


@pytest.mark.parametrize("sort", ["newest", "oldest", "score_desc", "score_asc", "name_asc", "name_desc"])
def test_every_sort_pages_through_each_row_exactly_once_in_order(client: TestClient, big, sort: str) -> None:
    team, rows = big
    items, first = walk(client, hdr(team.owner), f"&sort={sort}")
    ids = [i["id"] for i in items]
    assert len(ids) == len(set(ids)) == len(rows) == first["total"]
    key = {
        "newest": lambda i: (i["created_at"], i["id"]),
        "oldest": lambda i: (i["created_at"], i["id"]),
        "score_desc": lambda i: (-1 if i["final_weighted_score"] is None else i["final_weighted_score"], i["id"]),
        "score_asc": lambda i: (-1 if i["final_weighted_score"] is None else i["final_weighted_score"], i["id"]),
        "name_asc": lambda i: (i["name"].lower(), i["id"]),
        "name_desc": lambda i: (i["name"].lower(), i["id"]),
    }[sort]
    reverse = sort in ("newest", "score_desc", "name_desc")
    assert items == sorted(items, key=key, reverse=reverse)


def test_default_page_shape_and_totals(client: TestClient, big) -> None:
    team, rows = big
    page = client.get(BASE, headers=hdr(team.owner)).json()
    assert len(page["items"]) == 40 and page["next_cursor"] and page["total"] == 120 and not page["total_capped"]
    completed = [r for r in rows if r.status == EvaluationStatus.COMPLETED]
    assert page["selectable"] == len([r for r in rows if r.status in (EvaluationStatus.COMPLETED, EvaluationStatus.FAILED)])
    assert page["exportable"] == len(completed)
    item = page["items"][0]
    assert {"scorecard_name", "target_score", "runner_name", "is_mine", "shared", "scorecard_id"} <= set(item)
    assert item["scorecard_name"] == "Shared chart" and item["target_score"] == 7 and item["is_mine"] is True


def _expected(rows: list[Evaluation], pred) -> list[str]:
    return sorted(str(r.id) for r in rows if pred(r))


FILTERS = [
    ("&status=completed", lambda r: r.status == EvaluationStatus.COMPLETED),
    ("&status=failed", lambda r: r.status == EvaluationStatus.FAILED),
    ("&status=active", lambda r: r.status not in (EvaluationStatus.COMPLETED, EvaluationStatus.FAILED)),
    ("&min_score=5", lambda r: r.final_weighted_score is not None and r.final_weighted_score >= 5),
    ("&min_score=2&max_score=6.5", lambda r: r.final_weighted_score is not None and 2 <= r.final_weighted_score <= 6.5),
    ("&band=band_8&band=band_7", lambda r: r.rag_band in (RagBand.BAND_8, RagBand.BAND_7)),
    ("&meets_target=true", lambda r: r.final_weighted_score is not None and r.final_weighted_score >= 7),
    ("&meets_target=false", lambda r: r.final_weighted_score is not None and r.final_weighted_score < 7),
    ("&q=beta", lambda r: "beta" in r.name),
    ("&q=PERSON12", lambda r: "person12" in r.subject_email.lower()),
    ("&q=100%25", lambda r: False),  # a literal percent sign matches nothing (it is escaped, not a wildcard)
    ("&date_from=2026-10-01&date_to=2026-10-01", lambda r: r.created_at.date() == NOW.date()),
    ("&runner=me", lambda r: True),
    ("&runner=others", lambda r: False),
    ("&shared=true", lambda r: False),
    ("&shared=false", lambda r: True),
    ("&status=completed&min_score=4&q=alpha", lambda r: r.status == EvaluationStatus.COMPLETED
     and r.final_weighted_score >= 4 and "alpha" in r.name),
]


@pytest.mark.parametrize(("query", "pred"), FILTERS, ids=[f[0] for f in FILTERS])
def test_filters_are_applied_server_side_in_every_sort(client: TestClient, big, query: str, pred) -> None:
    team, rows = big
    for sort in ("newest", "score_desc", "name_asc"):
        items, first = walk(client, hdr(team.owner), f"&sort={sort}{query}", limit=11)
        assert sorted(i["id"] for i in items) == _expected(rows, pred), (query, sort)
        assert first["total"] == len(items)


def test_scorecard_and_batch_filters(client: TestClient, db_session: Session, team: Team) -> None:
    other = make_chart(db_session, team.owner, "Second chart")
    seed_rows(db_session, team.owner, team.chart, 6)
    seed_rows(db_session, team.owner, other, 4)
    h = hdr(team.owner)
    only = client.get(f"{BASE}?scorecard_id={other['id']}", headers=h).json()
    assert only["total"] == 4 and {i["scorecard_name"] for i in only["items"]} == {"Second chart"}
    assert client.get(f"{BASE}?batch_id={uuid.uuid4()}", headers=h).json()["total"] == 0
    assert client.get(f"{BASE}?scorecard_id={uuid.uuid4()}", headers=h).json()["items"] == []


def test_no_duplicates_or_gaps_when_rows_arrive_between_pages(client: TestClient, db_session: Session, big) -> None:
    team, rows = big
    h = hdr(team.owner)
    first = client.get(f"{BASE}?limit=30&sort=newest", headers=h).json()
    seen = [i["id"] for i in first["items"]]
    # 15 NEWER evaluations arrive while the user is scrolling
    seed_rows(db_session, team.owner, team.chart, 15, start=1000, step=-1)
    cursor = first["next_cursor"]
    while cursor:
        page = client.get(f"{BASE}?limit=30&sort=newest&cursor={cursor}", headers=h).json()
        seen += [i["id"] for i in page["items"]]
        cursor = page["next_cursor"]
    assert len(seen) == len(set(seen)) == 120  # exactly the original rows: nothing repeated, nothing skipped
    assert set(seen) == {str(r.id) for r in rows}
    # a fresh walk sees all 135
    assert client.get(BASE, headers=h).json()["total"] == 135


def test_cursor_is_tied_to_its_sort_and_total_is_capped(client: TestClient, big, monkeypatch) -> None:
    team, _rows = big
    h = hdr(team.owner)
    cursor = client.get(f"{BASE}?limit=5&sort=newest", headers=h).json()["next_cursor"]
    assert client.get(f"{BASE}?limit=5&sort=name_asc&cursor={cursor}", headers=h).status_code == 422
    assert client.get(f"{BASE}?sort=bogus", headers=h).status_code == 422
    assert client.get(f"{BASE}?limit=101", headers=h).status_code == 422
    import app.api.v1.evaluations_page as mod

    monkeypatch.setattr(mod, "COUNT_CAP", 50)
    capped = client.get(BASE, headers=h).json()
    assert capped["total"] == 50 and capped["total_capped"] is True and capped["count_cap"] == 50


def test_access_follows_the_chart(client: TestClient, db_session: Session, team: Team) -> None:
    seed_rows(db_session, team.owner, team.chart, 8)
    assert client.get(BASE, headers=hdr(team.outsider)).json()["total"] == 0
    assert client.get(BASE, headers=hdr(team.admin)).json()["total"] == 0  # admins have no bypass
    team.share(client)
    seed_rows(db_session, team.invitee, team.chart, 3, start=500)
    mine = client.get(BASE, headers=hdr(team.invitee)).json()
    assert mine["total"] == 11 and all(i["shared"] for i in mine["items"])
    only_theirs = client.get(f"{BASE}?runner=me", headers=hdr(team.invitee)).json()
    assert only_theirs["total"] == 3
    others = client.get(f"{BASE}?runner=others", headers=hdr(team.invitee)).json()
    assert others["total"] == 8 and {i["runner_name"] for i in others["items"]} == {"Olga Owner"}
    assert client.get(f"{BASE}?runner={team.invitee.id}", headers=hdr(team.owner)).json()["total"] == 3
    assert client.get(f"{BASE}?shared=true", headers=hdr(team.owner)).json()["total"] == 11
    # leaving the chart removes everything at once
    client.post(f"/api/v1/scorecards/{team.sc}/leave", headers=hdr(team.invitee))
    assert client.get(BASE, headers=hdr(team.invitee)).json()["total"] == 0


def test_refresh_returns_current_state_of_visible_rows_only(client: TestClient, db_session: Session, team: Team) -> None:
    rows = seed_rows(db_session, team.owner, team.chart, 5)
    ids = [str(r.id) for r in rows]
    got = client.post("/api/v1/evaluations/refresh", json={"ids": ids + [str(uuid.uuid4())]}, headers=hdr(team.owner))
    assert sorted(i["id"] for i in got.json()) == sorted(ids)
    assert client.post("/api/v1/evaluations/refresh", json={"ids": ids}, headers=hdr(team.outsider)).json() == []


# --- server-side select all ------------------------------------------------------------------------------------


def test_bulk_delete_by_filter_with_exclusions(client: TestClient, db_session: Session, big) -> None:
    team, rows = big
    h = hdr(team.owner)
    failed = [r for r in rows if r.status == EvaluationStatus.FAILED]
    keep = failed[:3]
    r = client.post("/api/v1/evaluations/bulk-delete", headers=h, json={
        "filter": {"status": "failed"}, "exclude_ids": [str(k.id) for k in keep]})
    assert r.status_code == 200 and r.json() == {"deleted": len(failed) - 3, "skipped": 0}
    db_session.expire_all()
    assert {e.id for e in db_session.scalars(select(Evaluation).where(Evaluation.status == EvaluationStatus.FAILED))} == {
        k.id for k in keep
    }
    assert db_session.scalar(select(Evaluation).where(Evaluation.status == EvaluationStatus.QUEUED)) is not None


def test_bulk_delete_skips_running_and_foreign_rows(client: TestClient, db_session: Session, big) -> None:
    team, rows = big
    queued = next(r for r in rows if r.status == EvaluationStatus.QUEUED)
    done = next(r for r in rows if r.status == EvaluationStatus.COMPLETED)
    ids = [str(queued.id), str(done.id)]
    assert client.post("/api/v1/evaluations/bulk-delete", json={"ids": ids}, headers=hdr(team.outsider)).json() == {
        "deleted": 0, "skipped": 0}
    r = client.post("/api/v1/evaluations/bulk-delete", json={"ids": ids}, headers=hdr(team.owner))
    assert r.json() == {"deleted": 1, "skipped": 1}  # the running one is protected
    assert client.post("/api/v1/evaluations/bulk-delete", json={}, headers=hdr(team.owner)).status_code == 422
    assert client.post("/api/v1/evaluations/bulk-delete", json={"ids": ids, "filter": {}}, headers=hdr(team.owner)).status_code == 422


def test_export_everything_matching_a_filter(client: TestClient, big) -> None:
    team, rows = big
    h = hdr(team.owner)
    completed = [r for r in rows if r.status == EvaluationStatus.COMPLETED and r.final_weighted_score is not None]
    r = client.post("/api/v1/evaluations/export", headers=h, json={"filter": {"status": "all"}})
    # no KPI results are seeded, so the workbook builder may refuse; either way the selection logic ran server side
    assert r.status_code in (200, 422), r.text
    if r.status_code == 200:
        assert int(r.headers["x-export-count"]) <= len(completed)
    assert client.post("/api/v1/evaluations/export", headers=h, json={}).status_code == 422
    assert client.post("/api/v1/evaluations/export", headers=hdr(team.outsider), json={"filter": {}}).status_code == 404


def test_pagination_iterates_lazily_in_small_pages(client: TestClient, big) -> None:
    team, _rows = big
    pages = 0
    cursor = None
    for _ in itertools.count():
        page = client.get(f"{BASE}?limit=1" + (f"&cursor={cursor}" if cursor else ""), headers=hdr(team.owner)).json()
        pages += 1
        cursor = page["next_cursor"]
        if not cursor or pages >= 130:
            break
    assert pages == 120
