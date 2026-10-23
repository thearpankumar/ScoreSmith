"""Evaluation names: renaming one from the Evaluations list, and the AI judge titling an evaluation from its content
when no submitter name or email is available (any input: uploaded files, Drive links or a spreadsheet row)."""

from __future__ import annotations

import uuid

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select, update
from sqlalchemy.orm import Session
from sqlalchemy_utils import Ltree

from app.db import AsyncSessionLocal
from app.models.audit_log import AuditLog
from app.models.enums import EvaluationStatus
from app.models.evaluation import Evaluation
from app.models.kpi_node import KpiNode
from app.models.scorecard import Scorecard
from app.models.scorecard_version import ScorecardVersion
from app.models.user import User
from app.pipeline import graph, master
from app.pipeline.corpus import Corpus, Section
from app.pipeline.dispatcher import Dispatcher
from tests.ai_eval_helpers import get_eval, seed_queued, seed_scorecard
from tests.fakes import (
    DEFAULT_QUOTE,
    FakeAwsJobs,
    FakeBedrockClient,
    FakeJevScoreClient,
    default_corpus,
    master_converse_fn,
    tool_use_result,
)


def _h(user_id) -> dict[str, str]:
    return {"X-User-Id": str(user_id)}


def _user(db: Session) -> User:
    u = User(email=f"name-{uuid.uuid4().hex[:8]}@example.com", name="Namer")
    db.add(u)
    db.commit()
    return u


def _evaluation(db: Session, owner: User) -> Evaluation:
    sc = Scorecard(name="sc", owner_id=owner.id)
    db.add(sc)
    db.flush()
    ver = ScorecardVersion(scorecard_id=sc.id, version_number=1, created_by=owner.id)
    db.add(ver)
    db.flush()
    sc.current_version_id = ver.id
    nid = uuid.uuid4()
    db.add(KpiNode(id=nid, scorecard_version_id=ver.id, parent_id=None, path=Ltree(nid.hex), level=1,
                   name="k", weight=100, display_order=0))
    ev = Evaluation(scorecard_version_id=ver.id, name="Priya Nair (priya@example.edu)", evaluated_by=owner.id,
                    owner_id=owner.id, status=EvaluationStatus.COMPLETED, subject_name="Priya Nair",
                    subject_email="priya@example.edu")
    db.add(ev)
    db.commit()
    return ev


def _audit_rows(db: Session, evaluation_id: uuid.UUID) -> list[AuditLog]:
    return list(db.execute(select(AuditLog).where(AuditLog.entity_id == evaluation_id)).scalars())


# --- renaming ---------------------------------------------------------------------------------------------------


def test_rename_trims_saves_keeps_the_submitter_and_is_audited(client: TestClient, db_session: Session) -> None:
    user = _user(db_session)
    ev = _evaluation(db_session, user)
    r = client.patch(f"/api/v1/evaluations/{ev.id}", json={"name": "  Round 2 – final pitch  "}, headers=_h(user.id))
    assert r.status_code == 200, r.text
    assert r.json()["name"] == "Round 2 – final pitch"

    db_session.expire_all()
    row = db_session.get(Evaluation, ev.id)
    assert row.name == "Round 2 – final pitch" and row.renamed_at is not None
    assert (row.subject_name, row.subject_email) == ("Priya Nair", "priya@example.edu")
    (entry,) = _audit_rows(db_session, ev.id)
    assert entry.actor_id == user.id and entry.entity_type == "evaluation"
    assert entry.diff == {"event": "renamed", "from": "Priya Nair (priya@example.edu)", "to": "Round 2 – final pitch"}


@pytest.mark.parametrize("bad", ["", "   ", None, "x" * 256])
def test_rename_rejects_blank_null_and_overlong_names(client: TestClient, db_session: Session, bad) -> None:
    user = _user(db_session)
    ev = _evaluation(db_session, user)
    r = client.patch(f"/api/v1/evaluations/{ev.id}", json={"name": bad}, headers=_h(user.id))
    assert r.status_code == 422, r.text
    db_session.expire_all()
    row = db_session.get(Evaluation, ev.id)
    assert row.name == "Priya Nair (priya@example.edu)" and row.renamed_at is None


def test_saving_the_same_name_is_not_a_rename(client: TestClient, db_session: Session) -> None:
    user = _user(db_session)
    ev = _evaluation(db_session, user)
    r = client.patch(f"/api/v1/evaluations/{ev.id}", json={"name": " Priya Nair (priya@example.edu) "},
                     headers=_h(user.id))
    assert r.status_code == 200, r.text
    db_session.expire_all()
    assert db_session.get(Evaluation, ev.id).renamed_at is None
    assert _audit_rows(db_session, ev.id) == []


def test_someone_without_access_cannot_rename(client: TestClient, db_session: Session) -> None:
    owner, stranger = _user(db_session), _user(db_session)
    ev = _evaluation(db_session, owner)
    r = client.patch(f"/api/v1/evaluations/{ev.id}", json={"name": "Hijacked"}, headers=_h(stranger.id))
    assert r.status_code == 404
    db_session.expire_all()
    assert db_session.get(Evaluation, ev.id).name == "Priya Nair (priya@example.edu)"


# --- the AI judge names an evaluation that has no submitter --------------------------------------------------------


def _doc(text: str, name: str = "submission_v2.pdf") -> Corpus:
    return Corpus("e", sections=[Section("s001", "src", name, "doc", f"DOC {name} p1", text)])


def _identify_answer(answer: dict):
    def fn(*, messages, system, tools, force_tool_use, model_id):
        return tool_use_result("record_identity", answer)

    return FakeBedrockClient(converse_fn=fn)


async def test_identify_returns_a_content_title_when_no_submitter_is_stated() -> None:
    bedrock = _identify_answer({"name": None, "email": None, "title": '  "SmartPark – Parking Availability Platform" '})
    identity = await master.identify_subject(
        bedrock, None, _doc("SmartPark: a parking availability platform for city councils."),
        hint_name=None, hint_email=None, filenames=["submission_v2.pdf"],
    )
    assert (identity.name, identity.email, identity.source) == (None, None, "none")
    assert identity.title == "SmartPark – Parking Availability Platform"


@pytest.mark.parametrize("junk", [None, "", "   ", "Untitled", "document", "N/A", "null", "Submission"])
async def test_identify_drops_empty_or_generic_titles(junk) -> None:
    identity = await master.identify_subject(
        _identify_answer({"name": None, "email": None, "title": junk}), None, _doc("Some content."),
        hint_name=None, hint_email=None, filenames=[],
    )
    assert identity.title is None


async def test_identify_caps_a_long_title_at_a_word_boundary() -> None:
    long = "Quarterly Customer Support Quality Review " * 10
    identity = await master.identify_subject(
        _identify_answer({"name": None, "email": None, "title": long}), None, _doc("Some content."),
        hint_name=None, hint_email=None, filenames=[],
    )
    assert identity.title is not None and len(identity.title) <= master.MAX_TITLE_CHARS
    assert long.startswith(identity.title) and not identity.title.endswith(" ")


def test_name_order_person_then_ai_title_then_file_then_nothing() -> None:
    corpus = _doc("text", name="FleetGuard_Solution.pdf")
    person = master.Identity("Jane Doe", "jane@example.com", "model", title="FleetGuard Platform")
    assert graph._auto_name(person, corpus, []) == "Jane Doe (jane@example.com)"
    titled = master.Identity(None, None, "none", title="FleetGuard Fleet Safety Platform")
    assert graph._auto_name(titled, corpus, []) == "FleetGuard Fleet Safety Platform"
    untitled = master.Identity(None, None, "none")
    assert graph._auto_name(untitled, corpus, []) == "FleetGuard Solution"
    assert graph._auto_name(untitled, Corpus("e"), ["https://drive.google.com/x"]) is None


def _run(bedrock, aws: FakeAwsJobs | None = None) -> Dispatcher:
    return Dispatcher(aws or FakeAwsJobs(), bedrock, FakeJevScoreClient(), max_concurrent=1, poll_seconds=0.01,
                      jev_retry_delays=(0.0, 0.0), patience_waits=())


def _anonymous_master():
    """The scoring fakes, except the content names nobody: the judge has to title the evaluation itself."""
    base = master_converse_fn()

    def fn(*, messages, system, tools, force_tool_use, model_id):
        if tools and tools[0].name == "record_identity":
            return tool_use_result("record_identity", {"name": None, "email": None,
                                                       "title": "FleetPulse Telematics Dashboard"})
        return base(messages=messages, system=system, tools=tools, force_tool_use=force_tool_use, model_id=model_id)

    return FakeBedrockClient(converse_fn=fn)


async def test_pipeline_titles_an_evaluation_without_a_submitter(async_db_session) -> None:
    owner, _sc, version, _nodes = await seed_scorecard(async_db_session)
    (ev,) = await seed_queued(async_db_session, owner, version, 1)
    aws = FakeAwsJobs()
    corpus = default_corpus(str(ev.id))  # the usual fixture, minus the author line: nobody is named anywhere
    corpus["sections"][0]["text"] = f"FleetPulse. {DEFAULT_QUOTE}. It uses telemetry and ML models."
    aws.json_objects[f"derived/{ev.id}/corpus.json"] = corpus
    d = _run(_anonymous_master(), aws)
    try:
        await d.run_until_idle()
    finally:
        await d.stop()
    done = await get_eval(ev.id)
    assert done.status == EvaluationStatus.COMPLETED, done.error_message
    assert done.name == "FleetPulse Telematics Dashboard"
    assert done.subject_name is None and done.subject_email is None


async def test_pipeline_never_overwrites_a_rename_made_while_it_runs(async_db_session, monkeypatch) -> None:
    owner, _sc, version, _nodes = await seed_scorecard(async_db_session)
    (ev,) = await seed_queued(async_db_session, owner, version, 1)
    original = graph._load_evaluation

    async def load_then_user_renames(evaluation_id):
        loaded = await original(evaluation_id)
        async with AsyncSessionLocal() as db:  # the user renames between the load and the identify step
            await db.execute(update(Evaluation).where(Evaluation.id == evaluation_id).values(
                name="Renamed mid-run", renamed_at=Evaluation.created_at))
            await db.commit()
        return loaded

    monkeypatch.setattr(graph, "_load_evaluation", load_then_user_renames)
    d = _run(FakeBedrockClient(converse_fn=master_converse_fn()))
    try:
        await d.run_until_idle()
    finally:
        await d.stop()
    done = await get_eval(ev.id)
    assert done.status == EvaluationStatus.COMPLETED, done.error_message
    assert done.name == "Renamed mid-run"
    assert done.subject_name == "Jane Doe"  # the submitter is still recorded
