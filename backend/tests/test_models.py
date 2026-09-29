"""Model creation tests: each table can be created with valid data and read back with its
relationships intact, against a real Postgres database."""

from __future__ import annotations

from sqlalchemy.orm import Session
from sqlalchemy_utils import Ltree

from app.models.chat_message import ChatMessage
from app.models.chat_session import ChatSession
from app.models.enums import (
    ChatMessageRole,
    ChatSessionStatus,
    EvaluationStatus,
    RagBand,
    ScorecardStatus,
)
from app.models.evaluation import Evaluation
from app.models.evaluation_kpi_result import EvaluationKpiResult
from app.models.kpi_guideline import KpiGuideline
from app.models.kpi_node import KpiNode
from app.models.scorecard import Scorecard
from app.models.scorecard_embedding import EMBEDDING_DIM, ScorecardEmbedding
from app.models.scorecard_version import ScorecardVersion
from app.models.user import User


def test_create_user(db_session: Session) -> None:
    user = User(email="model-test@example.com", name="Model Test", role="designer")
    db_session.add(user)
    db_session.commit()
    db_session.refresh(user)

    assert user.id is not None
    assert user.created_at is not None
    assert user.role == "designer"


def test_create_scorecard_with_version_and_kpi_tree(db_session: Session) -> None:
    owner = User(email="owner@example.com", name="Owner")
    db_session.add(owner)
    db_session.flush()

    scorecard = Scorecard(
        name="Test Scorecard",
        owner_id=owner.id,
        domain="Testing",
        purpose_statement="Verify model wiring.",
        target_score=8,
        status=ScorecardStatus.DRAFT,
    )
    db_session.add(scorecard)
    db_session.flush()

    version = ScorecardVersion(scorecard_id=scorecard.id, version_number=1, created_by=owner.id)
    db_session.add(version)
    db_session.flush()

    root = KpiNode(
        scorecard_version_id=version.id,
        parent_id=None,
        path=Ltree("root"),
        level=1,
        name="Root KPI",
        weight=100,
        display_order=0,
    )
    db_session.add(root)
    db_session.flush()

    guideline = KpiGuideline(
        kpi_node_id=root.id,
        score_level=10,
        qualitative_text="Exemplary.",
        quantitative_criteria={"metric": "score", "operator": ">=", "value": 95},
    )
    db_session.add(guideline)

    scorecard.current_version_id = version.id
    db_session.commit()

    db_session.refresh(scorecard)
    db_session.refresh(version)
    db_session.refresh(root)

    assert scorecard.current_version_id == version.id
    assert version.scorecard_id == scorecard.id
    assert root.scorecard_version_id == version.id
    assert str(root.path) == "root"
    assert len(root.guidelines) == 1
    assert root.guidelines[0].quantitative_criteria["value"] == 95


def test_create_scorecard_embedding(db_session: Session) -> None:
    owner = User(email="embed-owner@example.com", name="Embed Owner")
    db_session.add(owner)
    db_session.flush()
    scorecard = Scorecard(name="Embed SC", owner_id=owner.id)
    db_session.add(scorecard)
    db_session.flush()
    version = ScorecardVersion(scorecard_id=scorecard.id, version_number=1, created_by=owner.id)
    db_session.add(version)
    db_session.flush()

    embedding = ScorecardEmbedding(
        scorecard_version_id=version.id,
        embedding=[0.001 * i for i in range(EMBEDDING_DIM)],
        embedding_model="amazon.titan-embed-text-v2:0",
        source_text_hash="deadbeef",
    )
    db_session.add(embedding)
    db_session.commit()
    db_session.refresh(embedding)

    assert embedding.id is not None
    assert len(embedding.embedding) == EMBEDDING_DIM


def test_create_evaluation_with_results(db_session: Session) -> None:
    owner = User(email="eval-owner@example.com", name="Eval Owner")
    db_session.add(owner)
    db_session.flush()
    scorecard = Scorecard(name="Eval SC", owner_id=owner.id)
    db_session.add(scorecard)
    db_session.flush()
    version = ScorecardVersion(scorecard_id=scorecard.id, version_number=1, created_by=owner.id)
    db_session.add(version)
    db_session.flush()
    node = KpiNode(
        scorecard_version_id=version.id, path=Ltree("root"), level=1, name="KPI", weight=100
    )
    db_session.add(node)
    db_session.flush()

    evaluation = Evaluation(
        scorecard_version_id=version.id,
        name="Test evaluation",
        evaluated_by=owner.id,
        status=EvaluationStatus.COMPLETED,
        final_weighted_score=8.5,
        rag_band=RagBand.BAND_8,
    )
    db_session.add(evaluation)
    db_session.flush()
    result = EvaluationKpiResult(
        evaluation_id=evaluation.id,
        kpi_node_id=node.id,
        score=8.5,
        matched_guideline_level=8,
        reasoning_text="Matches level 8.",
        evidence_quotes=["quote 1"],
    )
    db_session.add(result)
    db_session.commit()
    db_session.refresh(evaluation)

    assert len(evaluation.kpi_results) == 1
    assert evaluation.kpi_results[0].score == 8.5


def test_create_chat_session_and_message(db_session: Session) -> None:
    user = User(email="chat-user@example.com", name="Chat User")
    db_session.add(user)
    db_session.flush()

    chat_session = ChatSession(user_id=user.id, status=ChatSessionStatus.ACTIVE)
    db_session.add(chat_session)
    db_session.flush()

    message = ChatMessage(
        session_id=chat_session.id,
        role=ChatMessageRole.USER,
        content="Hello",
        tool_calls=None,
    )
    db_session.add(message)
    db_session.commit()
    db_session.refresh(chat_session)

    assert len(chat_session.messages) == 1
    assert chat_session.messages[0].role == ChatMessageRole.USER
