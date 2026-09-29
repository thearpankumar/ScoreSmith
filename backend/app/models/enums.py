"""Python-side enums backing Postgres native ENUM columns.

Kept centralized so the Alembic migration and the ORM models share one source of truth
for allowed values.
"""

from __future__ import annotations

import enum


class ScorecardStatus(str, enum.Enum):
    DRAFT = "draft"
    PUBLISHED = "published"
    ARCHIVED = "archived"


class EvaluationStatus(str, enum.Enum):
    PENDING = "pending"
    IN_PROGRESS = "in_progress"
    COMPLETED = "completed"
    FAILED = "failed"


class RagBand(str, enum.Enum):
    """7-band RAG colour result. Value = the score-range label from the Quality
    Scorecard Framework's RAG palette (kept distinct from the app's brand palette)."""

    BAND_10_9 = "band_10_9"  # dark green  #1B5E20
    BAND_8 = "band_8"  # green       #66BB6A
    BAND_7 = "band_7"  # grey        #9E9E9E
    BAND_6 = "band_6"  # amber       #F9A825
    BAND_5 = "band_5"  # orange      #E65100
    BAND_4 = "band_4"  # red         #D32F2F
    BAND_3_0 = "band_3_0"  # dark red    #7F0000


def rag_band_for_score(score: float) -> RagBand:
    """Map a 0-10 weighted score to its RAG band per the framework's 7-band palette."""
    if score >= 9:
        return RagBand.BAND_10_9
    if score >= 8:
        return RagBand.BAND_8
    if score >= 7:
        return RagBand.BAND_7
    if score >= 6:
        return RagBand.BAND_6
    if score >= 5:
        return RagBand.BAND_5
    if score >= 4:
        return RagBand.BAND_4
    return RagBand.BAND_3_0


class ChatSessionStatus(str, enum.Enum):
    ACTIVE = "active"
    COMPLETED = "completed"
    ABANDONED = "abandoned"


class ChatMessageRole(str, enum.Enum):
    USER = "user"
    ASSISTANT = "assistant"
    SYSTEM = "system"
    TOOL = "tool"


class AuditAction(str, enum.Enum):
    CREATE = "create"
    UPDATE = "update"
    DELETE = "delete"
