"""Scoring with analysed video frames: what the media note tells the models, when a video KPI is still flagged as
unverifiable, and what the AWS execution input carries for the frame analysis (leaf KPI names, knob overrides)."""

from __future__ import annotations

import asyncio
import uuid
from dataclasses import replace
from types import SimpleNamespace

import pytest
from sqlalchemy import select

from app.config import get_settings
from app.db import AsyncSessionLocal
from app.models.evaluation_kpi_result import EvaluationKpiResult
from app.models.kpi_node import KpiNode
from app.pipeline.corpus import Corpus, Section, read_media
from app.pipeline.dispatcher import Dispatcher, leaf_kpi_names, video_overrides
from app.pipeline.jev_scorer import MEDIA_NOTE_CHARS, build_score_request
from tests.ai_eval_helpers import seed_queued, seed_scorecard
from tests.fakes import DEFAULT_QUOTE, FakeAwsJobs, FakeBedrockClient, FakeJevScoreClient, master_converse_fn

_CREATED: list[Dispatcher] = []


@pytest.fixture(autouse=True)
async def _drain_dispatchers():
    yield
    while _CREATED:
        d = _CREATED.pop()
        pending = [t for t in d._tasks.values() if not t.done()]
        if pending:
            await asyncio.wait(pending, timeout=5)
        await d.stop()


def _video(**extra) -> dict:
    return {"name": "demo.mp4", "duration_sec": 754.0, "has_audio": True, "transcribed": True, **extra}


# ----------------------------------------------------------------------------- the media note
def test_note_for_a_video_whose_frames_were_analysed() -> None:
    corpus = Corpus("e1", media=[_video(visual="ok", frames=63, mean_gap_s=12.0)])
    note = corpus.media_note()
    assert "demo.mp4: video, 12:34 long; its audio was transcribed, and what it shows on screen was analysed from 63" \
        " sampled frames (about one every 12 s)." in note
    assert "[mm:ss] SHOWN:" in note and "weak evidence" in note and "NOT analysed" not in note
    assert not corpus.has_unanalysed_video()


def test_a_silent_video_with_analysed_frames_is_not_unverifiable() -> None:
    corpus = Corpus("e1", media=[_video(has_audio=False, transcribed=False, visual="ok", frames=9, mean_gap_s=6.7)])
    note = corpus.media_note()
    assert "no audio track (silent), and what it shows on screen was analysed from 9 sampled frames" in note
    assert not corpus.has_unanalysed_video()


def test_a_failed_frame_analysis_is_unverifiable_even_with_a_transcript() -> None:
    corpus = Corpus("e1", media=[_video(visual="failed", frames=0, mean_gap_s=None)])
    note = corpus.media_note()
    assert "its audio was transcribed, but the analysis of what it shows on screen failed" in note
    assert "weak evidence" not in note and corpus.has_unanalysed_video()


def test_frame_analysis_switched_off_keeps_the_old_wording_and_rule() -> None:
    corpus = Corpus("e1", media=[_video(visual="off")])
    assert "its audio was transcribed, but what it shows on screen was NOT analysed" in corpus.media_note()
    assert not corpus.has_unanalysed_video()
    corpus.media = [_video(has_audio=False, transcribed=False, visual="off")]
    assert "silent" in corpus.media_note() and corpus.has_unanalysed_video()


def test_the_scorer_reads_shown_lines_as_observations() -> None:
    from tests.test_pipeline_units import _kpi

    node, gl = _kpi("The demo shows a refund being approved")
    note = Corpus("e1", media=[_video(visual="ok", frames=63, mean_gap_s=12.0)] * 4).media_note()
    state, instructions, *_ = build_score_request(node.name, gl, ["[01:10] SHOWN: Refund approved."], None, note)
    assert "SHOWN lines in the evidence are observations of the video itself" in instructions
    assert "never credit a claim" in instructions
    assert state["submission_media"] == note[:MEDIA_NOTE_CHARS]


# ----------------------------------------------------------------------------- reading the AWS stage's files
def _aws_with_video(visual: dict | None) -> FakeAwsJobs:
    aws = FakeAwsJobs()
    aws.json_objects["derived/e1/manifest.json"] = {
        "files": [{"source_id": "v1", "kind": "video", "original_name": "demo.mp4", "status": "ok"}]
    }
    aws.json_objects["derived/e1/video/v1/plan.json"] = {"source": "demo.mp4", "duration_sec": 70.0, "has_audio": False}
    if visual is not None:
        aws.json_objects["derived/e1/video/v1/visual.json"] = visual
    return aws


def test_read_media_takes_the_frame_analysis_from_visual_json() -> None:
    shown_only = Section("s1", "v1", "demo.mp4", "video", "VIDEO demo.mp4 00:00-00:40", "[00:00] SHOWN: A login page.")
    corpus = Corpus("e1", sections=[shown_only])
    aws = _aws_with_video({"status": "ok", "frames": 9, "mean_gap_s": 7.8, "duration_sec": 70.0})
    (m,) = read_media(aws, "e1", corpus)
    # a video section with SHOWN lines only is not a transcript
    assert m == {"name": "demo.mp4", "duration_sec": 70.0, "has_audio": False, "transcribed": False, "visual": "ok",
                 "frames": 9, "mean_gap_s": 7.8}
    corpus.sections = [replace(shown_only, text="[00:00] SAID: Hello.\n[00:00] SHOWN: A login page.")]
    assert read_media(aws, "e1", corpus)[0]["transcribed"] is True
    # an older run (no visual.json): any video section is a transcript, no visual keys
    old = read_media(_aws_with_video(None), "e1", Corpus("e1", sections=[shown_only]))
    assert old == [{"name": "demo.mp4", "duration_sec": 70.0, "has_audio": False, "transcribed": True}]


# ----------------------------------------------------------------------------- the execution input
def test_leaf_kpi_names_follow_the_tree_and_are_clipped() -> None:
    def node(name, parent=None, order=0):
        return SimpleNamespace(id=uuid.uuid4(), parent_id=parent, name=name, display_order=order)

    safety = node("Safety", order=1)
    quality = node("Quality", order=0)
    rows = [
        safety, quality,
        node("Labels   every sample", quality.id, 1), node("Calibrates the scale", quality.id, 0),
        node("Wears gloves " + "x" * 300, safety.id, 0),
    ]
    names = leaf_kpi_names(rows)
    assert names[:2] == ["Calibrates the scale", "Labels every sample"]
    assert names[2].startswith("Wears gloves") and len(names[2]) == 120
    many = [node(f"KPI {i}", order=i) for i in range(100)]
    assert len(leaf_kpi_names(many)) == 60


def test_video_overrides_send_only_known_knobs(monkeypatch) -> None:
    s = SimpleNamespace(ai_eval_video_visual_mode=" OFF ", ai_eval_video_knobs={"grid": "2x2", "bogus": 1})
    assert video_overrides(s) == {"grid": "2x2", "video_visual_mode": "off"}
    assert video_overrides(SimpleNamespace(ai_eval_video_visual_mode="", ai_eval_video_knobs={})) == {}


def _make(aws, jev=None) -> Dispatcher:
    d = Dispatcher(aws, FakeBedrockClient(converse_fn=master_converse_fn()), jev or FakeJevScoreClient(),
                   max_concurrent=1, poll_seconds=0.01, jev_retry_delays=(0.0, 0.0), patience_waits=())
    _CREATED.append(d)
    return d


async def test_the_aws_job_carries_the_kpi_names_and_the_video_knobs(async_db_session, monkeypatch) -> None:
    monkeypatch.setattr(get_settings(), "ai_eval_video_knobs", {"frame_max_gap_s": 30, "grid": "2x2"})
    monkeypatch.setattr(get_settings(), "ai_eval_video_visual_mode", "frames")
    owner, _sc, version, _nodes = await seed_scorecard(async_db_session, kpi_names=("Opens the console", "Approves"))
    await seed_queued(async_db_session, owner, version, 1)
    aws = FakeAwsJobs()
    await _make(aws).run_until_idle()
    payload = aws.start_payloads[0]
    assert payload["context"]["kpis"] == ["Opens the console", "Approves"]
    assert payload["video"] == {"frame_max_gap_s": 30, "grid": "2x2", "video_visual_mode": "frames"}


async def test_without_knobs_the_input_has_no_video_object(async_db_session) -> None:
    owner, _sc, version, _nodes = await seed_scorecard(async_db_session)
    await seed_queued(async_db_session, owner, version, 1)
    aws = FakeAwsJobs()
    await _make(aws).run_until_idle()
    assert "video" not in aws.start_payloads[0]


# ----------------------------------------------------------------------------- video_unverifiable end to end
def _corpus(eid) -> dict:
    return {
        "evaluation_id": str(eid), "stats": {}, "warnings": [],
        "sections": [
            {"id": "s001", "source_id": "a", "source_name": "a.pdf", "kind": "doc", "label": "DOC a.pdf p1",
             "text": f"FleetPulse by Jane Doe. {DEFAULT_QUOTE}. The demo video shows the live system."},
            {"id": "s002", "source_id": "v1", "source_name": "demo.mp4", "kind": "video",
             "label": "VIDEO demo.mp4 00:00-01:10",
             "text": "[00:00] SAID: Here is the live system.\n[00:04] SHOWN: A dashboard with live fleet positions."},
        ],
    }


async def _run_with_visual(async_db_session, visual: dict):
    owner, _sc, version, nodes = await seed_scorecard(
        async_db_session, kpi_names=("The live demo shows the system running", "API latency is measured")
    )
    (ev,) = await seed_queued(async_db_session, owner, version, 1)
    aws = FakeAwsJobs()
    aws.json_objects[f"derived/{ev.id}/corpus.json"] = _corpus(ev.id)
    aws.json_objects[f"derived/{ev.id}/manifest.json"] = {
        "files": [{"source_id": "v1", "kind": "video", "original_name": "demo.mp4", "status": "ok"}]
    }
    aws.json_objects[f"derived/{ev.id}/video/v1/plan.json"] = {"source": "demo.mp4", "duration_sec": 70.0,
                                                               "has_audio": True}
    aws.json_objects[f"derived/{ev.id}/video/v1/visual.json"] = visual
    jev = FakeJevScoreClient(position=6.0)
    await _make(aws, jev).run_until_idle()
    async with AsyncSessionLocal() as db:
        mine = {x.id for x in nodes}
        names = {n.id: n.name for n in (await db.execute(select(KpiNode))).scalars() if n.id in mine}
        rows = await db.execute(select(EvaluationKpiResult).where(EvaluationKpiResult.evaluation_id == ev.id))
        return {names[r.kpi_node_id]: r for r in rows.scalars()}, jev


async def test_a_video_kpi_is_not_flagged_when_the_frames_were_analysed(async_db_session) -> None:
    by_name, jev = await _run_with_visual(async_db_session, {"status": "ok", "frames": 12, "mean_gap_s": 5.8})
    demo = by_name["The live demo shows the system running"]
    assert "video_unverifiable" not in demo.jev_raw and demo.needs_review is False
    assert jev.calls and all("12 sampled frames" in c["state"]["submission_media"] for c in jev.calls)


async def test_a_video_kpi_is_flagged_when_the_frame_analysis_failed(async_db_session) -> None:
    by_name, _jev = await _run_with_visual(async_db_session, {"status": "failed", "frames": 0, "error": "x"})
    demo = by_name["The live demo shows the system running"]
    assert demo.jev_raw["video_unverifiable"] is True and demo.needs_review is True
    assert "video_unverifiable" not in by_name["API latency is measured"].jev_raw
