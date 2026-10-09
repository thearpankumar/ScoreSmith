"""Import every model module so `Base.metadata` is fully populated (needed by Alembic
autogenerate and by anything that calls `Base.metadata.create_all`)."""

from app.models.audit_log import AuditLog
from app.models.auth import OAuthIdentity, PasswordResetToken, RefreshToken
from app.models.base import Base
from app.models.chat_message import ChatMessage
from app.models.chat_session import ChatSession
from app.models.chat_turn_event import ChatTurnEvent
from app.models.evaluation import Evaluation
from app.models.evaluation_batch import EvaluationBatch
from app.models.evaluation_event import EvaluationEvent
from app.models.evaluation_kpi_result import EvaluationKpiResult
from app.models.evaluation_source import EvaluationSource
from app.models.idempotency_key import IdempotencyKey
from app.models.kpi_guideline import KpiGuideline
from app.models.kpi_node import KpiNode
from app.models.scorecard import Scorecard
from app.models.scorecard_embedding import ScorecardEmbedding
from app.models.scorecard_version import ScorecardVersion
from app.models.user import User

__all__ = [
    "Base",
    "User",
    "Scorecard",
    "ScorecardVersion",
    "KpiNode",
    "KpiGuideline",
    "ScorecardEmbedding",
    "Evaluation",
    "EvaluationKpiResult",
    "EvaluationBatch",
    "EvaluationSource",
    "EvaluationEvent",
    "ChatSession",
    "ChatMessage",
    "ChatTurnEvent",
    "AuditLog",
    "IdempotencyKey",
    "RefreshToken",
    "PasswordResetToken",
    "OAuthIdentity",
]
