"""Cycle 1 scenario-based dataset generator.

Implements the full scenario catalogue from the plan
(`plans/polished-swimming-sun.md`, "Cycle 1 scenario catalogue"):

  - normal/happy-path
  - business variants
  - lifecycle states
  - boundary cases
  - invalid/flawed data (expected to be REJECTED by DB constraints)
  - migration cases

Each scenario is a small, self-contained function that takes an open SQLAlchemy `Session`
and a shared `Ctx` (a couple of already-created users to attribute rows to) and returns a
`ScenarioResult`. "Invalid/flawed data" scenarios attempt an insert that is expected to be
rejected by a DB constraint/trigger; the DB error is caught and reported as a *passing*
validation (proof the constraint works), not a crash.

This module only builds data — `app/scripts/seed.py` is the CLI entrypoint that opens the
session, calls `run_all_scenarios`, and prints/returns the report.
"""

from __future__ import annotations

import hashlib
import random
import uuid
from contextlib import contextmanager
from dataclasses import dataclass, field
from datetime import UTC, datetime

from sqlalchemy import text
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session
from sqlalchemy_utils import Ltree

from app.models.audit_log import AuditLog
from app.models.chat_message import ChatMessage
from app.models.chat_session import ChatSession
from app.models.enums import (
    AuditAction,
    ChatMessageRole,
    ChatSessionStatus,
    EvaluationStatus,
    ScorecardStatus,
    rag_band_for_score,
)
from app.models.evaluation import Evaluation
from app.models.evaluation_kpi_result import EvaluationKpiResult
from app.models.kpi_guideline import KpiGuideline
from app.models.kpi_node import KpiNode
from app.models.scorecard import Scorecard
from app.models.scorecard_embedding import EMBEDDING_DIM, ScorecardEmbedding
from app.models.scorecard_version import ScorecardVersion
from app.models.user import User

SEED_MARKER_EMAIL = "seed-marker@qualityscorecard.local"


# --------------------------------------------------------------------------- reporting ---


@dataclass
class ScenarioResult:
    category: str
    name: str
    status: str  # "created" | "rejected_as_expected" | "flawed_but_inserted" | "FAILED"
    detail: str = ""


@dataclass
class ScenarioReport:
    results: list[ScenarioResult] = field(default_factory=list)

    def add(self, category: str, name: str, status: str, detail: str = "") -> None:
        self.results.append(ScenarioResult(category, name, status, detail))

    def print_summary(self) -> None:
        by_category: dict[str, list[ScenarioResult]] = {}
        for r in self.results:
            by_category.setdefault(r.category, []).append(r)
        print("\n=== Scenario seed report ===")
        for category, items in by_category.items():
            print(f"\n-- {category} --")
            for r in items:
                marker = {
                    "created": "OK",
                    "rejected_as_expected": "OK (rejected, as expected)",
                    "flawed_but_inserted": "OK (inserted, flagged as flawed)",
                    "FAILED": "FAILED",
                }.get(r.status, r.status)
                print(f"  [{marker}] {r.name}" + (f" — {r.detail}" if r.detail else ""))
        n_failed = sum(1 for r in self.results if r.status == "FAILED")
        print(f"\nTotal scenarios: {len(self.results)}, failed: {n_failed}")


# ---------------------------------------------------------------------------- savepoints ---


@contextmanager
def savepoint(session: Session):
    """Run a block in a SAVEPOINT so a failure only unwinds that block, not the whole
    seeding session. Used for the invalid/flawed-data scenarios that are expected to fail."""
    nested = session.begin_nested()
    try:
        yield nested
        nested.commit()
    except Exception:
        nested.rollback()
        raise


# ------------------------------------------------------------------------------- helpers ---


def get_or_create_user(
    session: Session, email: str, name: str, role: str = "member"
) -> User:
    user = session.query(User).filter_by(email=email).one_or_none()
    if user is None:
        user = User(email=email, name=name, role=role)
        session.add(user)
        session.flush()
    return user


def _label(node_id: uuid.UUID) -> str:
    return node_id.hex


def make_kpi_node(
    session: Session,
    version: ScorecardVersion,
    name: str,
    weight: float,
    parent: KpiNode | None = None,
    display_order: int = 0,
) -> KpiNode:
    node_id = uuid.uuid4()
    label = _label(node_id)
    if parent is None:
        path, level = label, 1
    else:
        path, level = f"{parent.path}.{label}", parent.level + 1
    node = KpiNode(
        id=node_id,
        scorecard_version_id=version.id,
        parent_id=parent.id if parent else None,
        path=Ltree(path),
        level=level,
        name=name,
        weight=weight,
        display_order=display_order,
    )
    session.add(node)
    session.flush()
    return node


def add_full_guidelines(
    session: Session,
    node: KpiNode,
    style: str = "generic",
    metric_name: str = "score",
) -> None:
    """Add all 11 levels (0-10) of guideline for a node.

    style="numeric": quantitative_criteria is a numeric threshold, e.g.
      {"metric": "defect_rate_pct", "operator": "<=", "value": 20.0}
    style="categorical": quantitative_criteria is a category label, e.g.
      {"category": "Fully compliant"}
    style="generic": no quantitative_criteria, qualitative text only.
    """
    qualitative_bands = {
        0: "Absent / not attempted",
        1: "Severely deficient",
        2: "Deficient",
        3: "Well below expectations",
        4: "Below expectations",
        5: "Approaching expectations",
        6: "Meets baseline expectations",
        7: "Meets expectations",
        8: "Exceeds expectations",
        9: "Strongly exceeds expectations",
        10: "Exemplary / best-in-class",
    }
    categorical_labels = {
        0: "Not compliant",
        1: "Not compliant",
        2: "Not compliant",
        3: "Partially compliant",
        4: "Partially compliant",
        5: "Partially compliant",
        6: "Mostly compliant",
        7: "Mostly compliant",
        8: "Compliant",
        9: "Compliant",
        10: "Fully compliant",
    }
    for level in range(0, 11):
        quant: dict | None = None
        if style == "numeric":
            quant = {"metric": metric_name, "operator": "<=", "value": round((10 - level) * 10.0, 2)}
        elif style == "categorical":
            quant = {"category": categorical_labels[level]}
        session.add(
            KpiGuideline(
                kpi_node_id=node.id,
                score_level=level,
                qualitative_text=f"Level {level}: {qualitative_bands[level]} for {node.name}.",
                quantitative_criteria=quant,
            )
        )
    session.flush()


def synthetic_embedding(seed_text: str) -> list[float]:
    """Deterministic pseudo-embedding (Bedrock/Titan calls are out of scope for Cycle 1;
    Cycle 1c wires the real embedding call). Unit-scaled for a meaningful cosine distance."""
    rng = random.Random(seed_text)
    vec = [rng.uniform(-1.0, 1.0) for _ in range(EMBEDDING_DIM)]
    norm = sum(v * v for v in vec) ** 0.5
    return [v / norm for v in vec]


def text_hash(s: str) -> str:
    return hashlib.sha256(s.encode("utf-8")).hexdigest()


# ------------------------------------------------------------------ normal / happy-path ---


def scenario_flat_6kpi(session: Session, report: ScenarioReport, owner: User) -> ScorecardVersion:
    scorecard = Scorecard(
        name="Customer Support Ticket Quality",
        owner_id=owner.id,
        domain="Customer Support",
        purpose_statement="Rate the quality of a single resolved customer support ticket.",
        scope="One ticket transcript + resolution notes.",
        target_score=7.5,
        status=ScorecardStatus.PUBLISHED,
    )
    session.add(scorecard)
    session.flush()
    version = ScorecardVersion(scorecard_id=scorecard.id, version_number=1, created_by=owner.id)
    session.add(version)
    session.flush()

    weights = [20, 20, 15, 15, 15, 15]
    names = [
        "Accuracy of resolution",
        "Tone & empathy",
        "Response time",
        "Policy compliance",
        "Clarity of communication",
        "Follow-up completeness",
    ]
    for i, (name, weight) in enumerate(zip(names, weights, strict=False)):
        node = make_kpi_node(session, version, name, weight, display_order=i)
        add_full_guidelines(session, node, style="numeric" if i % 2 == 0 else "generic")

    scorecard.current_version_id = version.id
    session.flush()
    report.add("normal/happy-path", "flat 6-KPI scorecard", "created", f"scorecard_id={scorecard.id}")
    return version


def scenario_4level_hierarchy(session: Session, report: ScenarioReport, owner: User) -> ScorecardVersion:
    scorecard = Scorecard(
        name="Enterprise Software Delivery Quality",
        owner_id=owner.id,
        domain="Software Engineering",
        purpose_statement="Rate the quality of a shipped feature across a 4-level KPI hierarchy.",
        status=ScorecardStatus.PUBLISHED,
    )
    session.add(scorecard)
    session.flush()
    version = ScorecardVersion(scorecard_id=scorecard.id, version_number=1, created_by=owner.id)
    session.add(version)
    session.flush()

    # Level 1: two pillars, 60/40
    code_quality = make_kpi_node(session, version, "Code Quality", 60, display_order=0)
    delivery = make_kpi_node(session, version, "Delivery Process", 40, display_order=1)

    # Level 2 under Code Quality: 3 children summing to 100
    correctness = make_kpi_node(session, version, "Correctness", 50, parent=code_quality, display_order=0)
    maintainability = make_kpi_node(
        session, version, "Maintainability", 30, parent=code_quality, display_order=1
    )
    # Boundary case: this Level-3-capable branch stops here — no children (childless leaf
    # under a parent that could otherwise have gone deeper).
    security = make_kpi_node(session, version, "Security", 20, parent=code_quality, display_order=2)
    add_full_guidelines(session, security, style="numeric", metric_name="critical_vulns")

    # Level 3 under Correctness: 2 children summing to 100
    test_coverage = make_kpi_node(
        session, version, "Test Coverage", 70, parent=correctness, display_order=0
    )
    edge_cases = make_kpi_node(
        session, version, "Edge Case Handling", 30, parent=correctness, display_order=1
    )
    add_full_guidelines(session, edge_cases, style="generic")

    # Level 4 under Test Coverage: 2 children summing to 100 (max depth reached)
    unit = make_kpi_node(session, version, "Unit Test Coverage %", 60, parent=test_coverage, display_order=0)
    integration = make_kpi_node(
        session, version, "Integration Test Coverage %", 40, parent=test_coverage, display_order=1
    )
    add_full_guidelines(session, unit, style="numeric", metric_name="unit_coverage_pct")
    add_full_guidelines(session, integration, style="numeric", metric_name="integration_coverage_pct")

    # Level 2 under Delivery Process: 2 children, "childless Level3 node" boundary case
    # (a Level-3 node with no children at all, i.e. hierarchy legitimately varies in depth
    # per branch — not every branch needs to reach Level 4).
    on_time = make_kpi_node(session, version, "On-time Delivery", 50, parent=delivery, display_order=0)
    add_full_guidelines(session, on_time, style="numeric", metric_name="days_late")
    comms = make_kpi_node(session, version, "Stakeholder Communication", 50, parent=delivery, display_order=1)
    l3_leaf = make_kpi_node(session, version, "Status Update Cadence", 100, parent=comms, display_order=0)
    add_full_guidelines(session, l3_leaf, style="generic")

    for n in (maintainability, code_quality, delivery):
        add_full_guidelines(session, n, style="generic")

    scorecard.current_version_id = version.id
    session.flush()
    report.add(
        "normal/happy-path",
        "4-level KPI hierarchy (also covers 'childless Level3 node' boundary case)",
        "created",
        f"scorecard_id={scorecard.id}",
    )
    return version


def scenario_mid_band_and_double_evaluation(
    session: Session, report: ScenarioReport, flat_version: ScorecardVersion, evaluator_a: User, evaluator_b: User
) -> None:
    nodes = session.query(KpiNode).filter_by(scorecard_version_id=flat_version.id).all()

    def make_evaluation(name: str, evaluator: User, scores: list[float]) -> Evaluation:
        evaluation = Evaluation(
            scorecard_version_id=flat_version.id,
            name=name,
            evaluated_by=evaluator.id,
            input_reference={"ticket_id": "TCK-10432", "source": "helpdesk_export"},
            status=EvaluationStatus.COMPLETED,
            domain="Customer Support",
            submitted_at=datetime.now(UTC),
        )
        session.add(evaluation)
        session.flush()
        total_weight = sum(float(n.weight) for n in nodes)
        weighted_sum = 0.0
        for node, score in zip(nodes, scores, strict=False):
            session.add(
                EvaluationKpiResult(
                    evaluation_id=evaluation.id,
                    kpi_node_id=node.id,
                    score=score,
                    matched_guideline_level=int(round(score)),
                    reasoning_text=(
                        f"Synthetic seed reasoning: observed behaviour most closely matches "
                        f"guideline level {int(round(score))} for '{node.name}'."
                    ),
                    evidence_quotes=[f"(seed placeholder evidence for {node.name})"],
                )
            )
            weighted_sum += score * float(node.weight)
        final_score = round(weighted_sum / total_weight, 2) if total_weight else 0.0
        evaluation.final_weighted_score = final_score
        evaluation.rag_band = rag_band_for_score(final_score)
        session.flush()
        return evaluation

    # Mid-band evaluation (~6-7 range -> band_6/band_7).
    make_evaluation("Ticket TCK-10432 — evaluator A", evaluator_a, [7, 6, 7, 6, 7, 6])
    report.add("normal/happy-path", "mid-band evaluation", "created")

    # Same scorecard evaluated twice by different users.
    make_evaluation("Ticket TCK-10432 — evaluator B (second opinion)", evaluator_b, [6, 7, 6, 8, 6, 7])
    report.add(
        "normal/happy-path", "same scorecard evaluated twice by different users", "created"
    )


def scenario_reuse_via_similarity_clone(
    session: Session, report: ScenarioReport, owner: User
) -> None:
    original = Scorecard(
        name="Sales Proposal Quality",
        owner_id=owner.id,
        domain="Sales",
        purpose_statement="Rate the quality of an outbound sales proposal document.",
        status=ScorecardStatus.PUBLISHED,
    )
    session.add(original)
    session.flush()
    original_version = ScorecardVersion(scorecard_id=original.id, version_number=1, created_by=owner.id)
    session.add(original_version)
    session.flush()
    a = make_kpi_node(session, original_version, "Value proposition clarity", 50, display_order=0)
    b = make_kpi_node(session, original_version, "Pricing transparency", 50, display_order=1)
    add_full_guidelines(session, a, style="generic")
    add_full_guidelines(session, b, style="generic")
    original.current_version_id = original_version.id
    session.flush()

    purpose_text = original.purpose_statement or ""
    session.add(
        ScorecardEmbedding(
            scorecard_version_id=original_version.id,
            embedding=synthetic_embedding(purpose_text),
            embedding_model="amazon.titan-embed-text-v2:0",
            source_text_hash=text_hash(purpose_text),
        )
    )
    session.flush()

    # A near-duplicate request clones the original (same purpose family, adapted name) —
    # simulates the "suggest similar scorecard -> Adapt" flow producing a new scorecard
    # whose embedding should be close (cosine) to the original's.
    clone = Scorecard(
        name="Sales Proposal Quality (EMEA adaptation)",
        owner_id=owner.id,
        domain="Sales",
        purpose_statement="Rate the quality of an outbound sales proposal document, EMEA region variant.",
        status=ScorecardStatus.DRAFT,
    )
    session.add(clone)
    session.flush()
    clone_version = ScorecardVersion(scorecard_id=clone.id, version_number=1, created_by=owner.id)
    session.add(clone_version)
    session.flush()
    ca = make_kpi_node(session, clone_version, "Value proposition clarity", 50, display_order=0)
    cb = make_kpi_node(session, clone_version, "Regional pricing transparency", 50, display_order=1)
    add_full_guidelines(session, ca, style="generic")
    add_full_guidelines(session, cb, style="generic")
    clone.current_version_id = clone_version.id
    session.flush()

    clone_purpose = clone.purpose_statement or ""
    session.add(
        ScorecardEmbedding(
            scorecard_version_id=clone_version.id,
            # Nudge the clone's embedding to be close-but-not-identical to the original's,
            # so a cosine similarity search would plausibly surface it as "similar".
            embedding=[
                min(1.0, max(-1.0, v + random.Random(clone_purpose).uniform(-0.05, 0.05)))
                for v in synthetic_embedding(purpose_text)
            ],
            embedding_model="amazon.titan-embed-text-v2:0",
            source_text_hash=text_hash(clone_purpose),
        )
    )
    session.add(
        AuditLog(
            actor_id=owner.id,
            entity_type="scorecard",
            entity_id=clone.id,
            action=AuditAction.CREATE,
            diff={"cloned_from_scorecard_id": str(original.id)},
        )
    )
    session.flush()
    report.add(
        "normal/happy-path",
        "reuse-via-similarity clone",
        "created",
        f"original={original.id} clone={clone.id}",
    )


# ------------------------------------------------------------------------ business variants ---


def scenario_different_domains(session: Session, report: ScenarioReport, owner: User) -> None:
    domains = [
        ("Content / Editorial", "Rate the quality of a published editorial article."),
        ("Legal / Contracts", "Rate the quality of a drafted commercial contract."),
        ("Data Engineering", "Rate the quality of a delivered ETL pipeline."),
    ]
    for domain, purpose in domains:
        scorecard = Scorecard(
            name=f"{domain} Quality Scorecard",
            owner_id=owner.id,
            domain=domain,
            purpose_statement=purpose,
            status=ScorecardStatus.PUBLISHED,
        )
        session.add(scorecard)
        session.flush()
        version = ScorecardVersion(scorecard_id=scorecard.id, version_number=1, created_by=owner.id)
        session.add(version)
        session.flush()
        n1 = make_kpi_node(session, version, "Overall quality", 60, display_order=0)
        n2 = make_kpi_node(session, version, "Timeliness", 40, display_order=1)
        add_full_guidelines(session, n1, style="generic")
        add_full_guidelines(session, n2, style="numeric", metric_name="days_late")
        scorecard.current_version_id = version.id
        session.flush()
    report.add("business variants", "different domains (3 scorecards)", "created")


def scenario_numeric_vs_categorical_guidelines(
    session: Session, report: ScenarioReport, owner: User
) -> None:
    scorecard = Scorecard(
        name="Vendor Compliance Review",
        owner_id=owner.id,
        domain="Procurement",
        purpose_statement="Rate a vendor's compliance submission.",
        status=ScorecardStatus.PUBLISHED,
    )
    session.add(scorecard)
    session.flush()
    version = ScorecardVersion(scorecard_id=scorecard.id, version_number=1, created_by=owner.id)
    session.add(version)
    session.flush()
    numeric_node = make_kpi_node(session, version, "Defect rate", 50, display_order=0)
    add_full_guidelines(session, numeric_node, style="numeric", metric_name="defect_rate_pct")
    categorical_node = make_kpi_node(session, version, "Certification status", 50, display_order=1)
    add_full_guidelines(session, categorical_node, style="categorical")
    scorecard.current_version_id = version.id
    session.flush()
    report.add(
        "business variants",
        "numeric vs categorical quantitative guidelines",
        "created",
        f"scorecard_id={scorecard.id}",
    )


def scenario_skewed_weights(session: Session, report: ScenarioReport, owner: User) -> None:
    scorecard = Scorecard(
        name="Incident Postmortem Quality (skewed)",
        owner_id=owner.id,
        domain="Software Engineering",
        purpose_statement="Rate an incident postmortem doc, heavily weighted on root-cause analysis.",
        status=ScorecardStatus.PUBLISHED,
    )
    session.add(scorecard)
    session.flush()
    version = ScorecardVersion(scorecard_id=scorecard.id, version_number=1, created_by=owner.id)
    session.add(version)
    session.flush()
    weights = [85, 10, 5]
    names = ["Root-cause analysis depth", "Action item quality", "Formatting"]
    for i, (name, weight) in enumerate(zip(names, weights, strict=False)):
        node = make_kpi_node(session, version, name, weight, display_order=i)
        add_full_guidelines(session, node, style="generic")
    scorecard.current_version_id = version.id
    session.flush()
    report.add("business variants", "skewed weights (85/10/5)", "created", f"scorecard_id={scorecard.id}")


def scenario_2_vs_40_kpi(session: Session, report: ScenarioReport, owner: User) -> None:
    # 2-KPI scorecard
    small = Scorecard(
        name="Minimal Code Review Scorecard",
        owner_id=owner.id,
        domain="Software Engineering",
        purpose_statement="Rate a code review with the smallest sensible KPI set.",
        status=ScorecardStatus.PUBLISHED,
    )
    session.add(small)
    session.flush()
    small_version = ScorecardVersion(scorecard_id=small.id, version_number=1, created_by=owner.id)
    session.add(small_version)
    session.flush()
    n1 = make_kpi_node(session, small_version, "Correctness", 70, display_order=0)
    n2 = make_kpi_node(session, small_version, "Readability", 30, display_order=1)
    add_full_guidelines(session, n1, style="generic")
    add_full_guidelines(session, n2, style="generic")
    small.current_version_id = small_version.id
    session.flush()
    report.add("business variants", "2-KPI scorecard", "created", f"scorecard_id={small.id}")

    # 40-KPI scorecard: 5 level-1 groups @ 20% each, 8 level-2 children @ 12.5% each = 40 leaves.
    large = Scorecard(
        name="Comprehensive RFP Response Scorecard",
        owner_id=owner.id,
        domain="Sales",
        purpose_statement="Rate a large RFP response document across many granular KPIs.",
        status=ScorecardStatus.PUBLISHED,
    )
    session.add(large)
    session.flush()
    large_version = ScorecardVersion(scorecard_id=large.id, version_number=1, created_by=owner.id)
    session.add(large_version)
    session.flush()
    leaf_count = 0
    for g in range(5):
        group = make_kpi_node(session, large_version, f"Section {g + 1}", 20, display_order=g)
        add_full_guidelines(session, group, style="generic")
        for c in range(8):
            weight = 12.5
            leaf = make_kpi_node(
                session, large_version, f"Section {g + 1} — Criterion {c + 1}", weight, parent=group,
                display_order=c,
            )
            add_full_guidelines(session, leaf, style="generic")
            leaf_count += 1
    large.current_version_id = large_version.id
    session.flush()
    report.add(
        "business variants", "40-KPI scorecard", "created",
        f"scorecard_id={large.id} leaf_kpis={leaf_count}",
    )


# --------------------------------------------------------------------------- lifecycle ---


def scenario_lifecycle_states(session: Session, report: ScenarioReport, owner: User) -> None:
    for status_ in (ScorecardStatus.DRAFT, ScorecardStatus.PUBLISHED, ScorecardStatus.ARCHIVED):
        scorecard = Scorecard(
            name=f"Lifecycle Demo ({status_.value})",
            owner_id=owner.id,
            domain="Internal",
            purpose_statement=f"Demonstrates a scorecard in '{status_.value}' status.",
            status=status_,
        )
        session.add(scorecard)
        session.flush()
        version = ScorecardVersion(scorecard_id=scorecard.id, version_number=1, created_by=owner.id)
        session.add(version)
        session.flush()
        n1 = make_kpi_node(session, version, "Quality", 100, display_order=0)
        add_full_guidelines(session, n1, style="generic")
        scorecard.current_version_id = version.id
        session.flush()
        report.add("lifecycle states", f"scorecard status = {status_.value}", "created")

    # Versioned scorecard: v1 superseded (inactive) by v2 (active, current).
    scorecard = Scorecard(
        name="Versioned Scorecard Demo",
        owner_id=owner.id,
        domain="Internal",
        purpose_statement="Demonstrates version bump: v1 retired, v2 active/current.",
        status=ScorecardStatus.PUBLISHED,
    )
    session.add(scorecard)
    session.flush()
    v1 = ScorecardVersion(
        scorecard_id=scorecard.id, version_number=1, created_by=owner.id, is_active=False,
        guideline_notes="Initial version.",
    )
    session.add(v1)
    session.flush()
    n1 = make_kpi_node(session, v1, "Quality", 100, display_order=0)
    add_full_guidelines(session, n1, style="generic")

    v2 = ScorecardVersion(
        scorecard_id=scorecard.id, version_number=2, created_by=owner.id, is_active=True,
        guideline_notes="Reworded guideline language for clarity.",
    )
    session.add(v2)
    session.flush()
    n2 = make_kpi_node(session, v2, "Quality", 100, display_order=0)
    add_full_guidelines(session, n2, style="generic")

    scorecard.current_version_id = v2.id
    session.flush()
    report.add("lifecycle states", "versioned scorecard (v1 inactive, v2 current)", "created")

    # Partial-draft-evaluation: an in-progress evaluation against a draft version, missing
    # results for some of its KPI nodes.
    draft_scorecard = Scorecard(
        name="Draft Scorecard With Partial Evaluation",
        owner_id=owner.id,
        domain="Internal",
        status=ScorecardStatus.DRAFT,
    )
    session.add(draft_scorecard)
    session.flush()
    draft_version = ScorecardVersion(scorecard_id=draft_scorecard.id, version_number=1, created_by=owner.id)
    session.add(draft_version)
    session.flush()
    d1 = make_kpi_node(session, draft_version, "Draft KPI A", 50, display_order=0)
    d2 = make_kpi_node(session, draft_version, "Draft KPI B", 50, display_order=1)
    add_full_guidelines(session, d1, style="generic")
    add_full_guidelines(session, d2, style="generic")
    draft_scorecard.current_version_id = draft_version.id
    session.flush()

    partial_eval = Evaluation(
        scorecard_version_id=draft_version.id,
        name="In-progress evaluation on a draft scorecard",
        evaluated_by=owner.id,
        status=EvaluationStatus.IN_PROGRESS,
        domain="Internal",
    )
    session.add(partial_eval)
    session.flush()
    # Only KPI A has been scored so far — KPI B's result is intentionally absent.
    session.add(
        EvaluationKpiResult(
            evaluation_id=partial_eval.id,
            kpi_node_id=d1.id,
            score=7,
            matched_guideline_level=7,
            reasoning_text="Seed placeholder — evaluation still in progress.",
        )
    )
    session.flush()
    report.add(
        "lifecycle states",
        "partial-draft-evaluation (in-progress, one of two KPIs scored)",
        "created",
    )


# ---------------------------------------------------------------------------- boundary ---


def scenario_boundary_cases(session: Session, report: ScenarioReport, owner: User) -> None:
    # 0% weight KPI, alongside non-zero siblings still summing to 100.
    scorecard = Scorecard(
        name="Boundary: Zero-weight KPI",
        owner_id=owner.id,
        domain="Internal",
        status=ScorecardStatus.PUBLISHED,
    )
    session.add(scorecard)
    session.flush()
    version = ScorecardVersion(scorecard_id=scorecard.id, version_number=1, created_by=owner.id)
    session.add(version)
    session.flush()
    n1 = make_kpi_node(session, version, "Primary criterion", 70, display_order=0)
    n2 = make_kpi_node(session, version, "Secondary criterion", 30, display_order=1)
    n3 = make_kpi_node(session, version, "Not-yet-weighted criterion (0%)", 0, display_order=2)
    for n in (n1, n2, n3):
        add_full_guidelines(session, n, style="generic")
    scorecard.current_version_id = version.id
    session.flush()
    report.add("boundary cases", "0% weight KPI (siblings still sum to 100)", "created")

    # Single-KPI scorecard.
    single = Scorecard(
        name="Boundary: Single-KPI Scorecard",
        owner_id=owner.id,
        domain="Internal",
        status=ScorecardStatus.PUBLISHED,
    )
    session.add(single)
    session.flush()
    single_version = ScorecardVersion(scorecard_id=single.id, version_number=1, created_by=owner.id)
    session.add(single_version)
    session.flush()
    only_kpi = make_kpi_node(session, single_version, "Overall quality", 100, display_order=0)
    add_full_guidelines(session, only_kpi, style="generic")
    single.current_version_id = single_version.id
    session.flush()
    report.add("boundary cases", "single-KPI scorecard (weight=100)", "created")

    # Score 0 and score 10 in the same evaluation.
    evaluation = Evaluation(
        scorecard_version_id=version.id,
        name="Boundary evaluation: min and max scores",
        evaluated_by=owner.id,
        status=EvaluationStatus.COMPLETED,
        domain="Internal",
        submitted_at=datetime.now(UTC),
    )
    session.add(evaluation)
    session.flush()
    session.add_all(
        [
            EvaluationKpiResult(
                evaluation_id=evaluation.id, kpi_node_id=n1.id, score=0,
                matched_guideline_level=0, reasoning_text="Seed placeholder: worst-case score.",
            ),
            EvaluationKpiResult(
                evaluation_id=evaluation.id, kpi_node_id=n2.id, score=10,
                matched_guideline_level=10, reasoning_text="Seed placeholder: best-case score.",
            ),
        ]
    )
    session.flush()
    weighted = (0 * 70 + 10 * 30) / 100
    evaluation.final_weighted_score = weighted
    evaluation.rag_band = rag_band_for_score(weighted)
    session.flush()
    report.add("boundary cases", "score 0 and score 10 in one evaluation", "created")


# ---------------------------------------------------------------- invalid / flawed data ---


def scenario_invalid_weight_sum(
    session: Session, report: ScenarioReport, owner: User, target_total: int
) -> None:
    name = f"invalid: sibling weights sum to {target_total}%"
    scorecard = Scorecard(
        name=f"Invalid Weight Sum Demo ({target_total}%)", owner_id=owner.id, domain="Internal"
    )
    session.add(scorecard)
    session.flush()
    version = ScorecardVersion(scorecard_id=scorecard.id, version_number=1, created_by=owner.id)
    session.add(version)
    session.flush()

    try:
        with savepoint(session):
            w1 = target_total - 50
            make_kpi_node(session, version, "KPI A", w1, display_order=0)
            make_kpi_node(session, version, "KPI B", 50, display_order=1)
            # The weight-sum trigger is a DEFERRED CONSTRAINT TRIGGER — it only fires at
            # COMMIT by default. Force it to fire now so the violation is caught inside
            # this savepoint rather than at the very end of the whole seeding run.
            session.execute(text("SET CONSTRAINTS trg_kpi_node_weight_sum IMMEDIATE"))
        report.add(
            "invalid/flawed data", name, "FAILED",
            "insert unexpectedly succeeded — constraint did not fire",
        )
    except IntegrityError as exc:
        report.add("invalid/flawed data", name, "rejected_as_expected", str(exc.orig).strip())


def scenario_invalid_duplicate_names(session: Session, report: ScenarioReport, owner: User) -> None:
    name = "invalid: duplicate sibling KPI names"
    scorecard = Scorecard(name="Invalid Duplicate Names Demo", owner_id=owner.id, domain="Internal")
    session.add(scorecard)
    session.flush()
    version = ScorecardVersion(scorecard_id=scorecard.id, version_number=1, created_by=owner.id)
    session.add(version)
    session.flush()
    try:
        with savepoint(session):
            make_kpi_node(session, version, "Duplicate Name", 50, display_order=0)
            make_kpi_node(session, version, "Duplicate Name", 50, display_order=1)
        report.add(
            "invalid/flawed data", name, "FAILED",
            "insert unexpectedly succeeded — unique index did not fire",
        )
    except IntegrityError as exc:
        report.add("invalid/flawed data", name, "rejected_as_expected", str(exc.orig).strip())


def scenario_invalid_cross_version_reference(
    session: Session, report: ScenarioReport, owner: User
) -> None:
    name = "invalid: evaluation_kpi_result references a KPI node from a different scorecard_version"
    scorecard = Scorecard(name="Invalid Cross-Version Reference Demo", owner_id=owner.id, domain="Internal")
    session.add(scorecard)
    session.flush()
    v1 = ScorecardVersion(scorecard_id=scorecard.id, version_number=1, created_by=owner.id)
    v2 = ScorecardVersion(scorecard_id=scorecard.id, version_number=2, created_by=owner.id)
    session.add_all([v1, v2])
    session.flush()
    node_v1 = make_kpi_node(session, v1, "KPI (v1 only)", 100, display_order=0)
    make_kpi_node(session, v2, "KPI (v2 only)", 100, display_order=0)

    try:
        with savepoint(session):
            evaluation = Evaluation(
                scorecard_version_id=v2.id,
                name="Evaluation against v2, wrongly citing a v1 KPI node",
                evaluated_by=owner.id,
                status=EvaluationStatus.PENDING,
            )
            session.add(evaluation)
            session.flush()
            session.add(
                EvaluationKpiResult(evaluation_id=evaluation.id, kpi_node_id=node_v1.id, score=5)
            )
            session.flush()
        report.add(
            "invalid/flawed data", name, "FAILED",
            "insert unexpectedly succeeded — cross-version trigger did not fire",
        )
    except IntegrityError as exc:
        report.add("invalid/flawed data", name, "rejected_as_expected", str(exc.orig).strip())


def scenario_invalid_out_of_range_score(session: Session, report: ScenarioReport, owner: User) -> None:
    name = "invalid: evaluation_kpi_result.score out of [0, 10] range"
    scorecard = Scorecard(name="Invalid Out-of-Range Score Demo", owner_id=owner.id, domain="Internal")
    session.add(scorecard)
    session.flush()
    version = ScorecardVersion(scorecard_id=scorecard.id, version_number=1, created_by=owner.id)
    session.add(version)
    session.flush()
    node = make_kpi_node(session, version, "KPI", 100, display_order=0)

    try:
        with savepoint(session):
            evaluation = Evaluation(
                scorecard_version_id=version.id,
                name="Evaluation with an out-of-range score",
                evaluated_by=owner.id,
                status=EvaluationStatus.PENDING,
            )
            session.add(evaluation)
            session.flush()
            session.add(EvaluationKpiResult(evaluation_id=evaluation.id, kpi_node_id=node.id, score=15))
            session.flush()
        report.add(
            "invalid/flawed data", name, "FAILED",
            "insert unexpectedly succeeded — CHECK constraint did not fire",
        )
    except IntegrityError as exc:
        report.add("invalid/flawed data", name, "rejected_as_expected", str(exc.orig).strip())


def scenario_flawed_missing_guideline_level(
    session: Session, report: ScenarioReport, owner: User
) -> None:
    """Unlike the scenarios above, an incomplete guideline set (e.g. level 5 missing out of
    0-10) is NOT something a DB constraint rejects — a KPI node is allowed to have fewer
    than 11 guideline rows (e.g. mid-draft). This is recorded as a successfully-inserted
    'flawed' dataset, flagged for app-level completeness validation in a later cycle."""
    name = "flawed (allowed by DB): KPI node missing one guideline level (5 of 11 present)"
    scorecard = Scorecard(name="Flawed: Missing Guideline Level Demo", owner_id=owner.id, domain="Internal")
    session.add(scorecard)
    session.flush()
    version = ScorecardVersion(scorecard_id=scorecard.id, version_number=1, created_by=owner.id)
    session.add(version)
    session.flush()
    node = make_kpi_node(session, version, "Incompletely-defined KPI", 100, display_order=0)
    for level in range(0, 11):
        if level == 5:
            continue  # deliberately missing
        session.add(
            KpiGuideline(kpi_node_id=node.id, score_level=level, qualitative_text=f"Level {level} text.")
        )
    session.flush()
    report.add("invalid/flawed data", name, "flawed_but_inserted", f"kpi_node_id={node.id}")


# --------------------------------------------------------------------------- migration ---


def scenario_version_bump_preserves_evaluations(
    session: Session, report: ScenarioReport, owner: User
) -> None:
    scorecard = Scorecard(
        name="Migration: Version Bump Preserves Evaluations",
        owner_id=owner.id,
        domain="Internal",
        status=ScorecardStatus.PUBLISHED,
    )
    session.add(scorecard)
    session.flush()
    v1 = ScorecardVersion(scorecard_id=scorecard.id, version_number=1, created_by=owner.id)
    session.add(v1)
    session.flush()
    n1 = make_kpi_node(session, v1, "Quality", 100, display_order=0)
    add_full_guidelines(session, n1, style="generic")
    scorecard.current_version_id = v1.id
    session.flush()

    evaluation = Evaluation(
        scorecard_version_id=v1.id,
        name="Evaluation submitted against v1",
        evaluated_by=owner.id,
        status=EvaluationStatus.COMPLETED,
        submitted_at=datetime.now(UTC),
    )
    session.add(evaluation)
    session.flush()
    session.add(
        EvaluationKpiResult(evaluation_id=evaluation.id, kpi_node_id=n1.id, score=8, matched_guideline_level=8)
    )
    evaluation.final_weighted_score = 8
    evaluation.rag_band = rag_band_for_score(8)
    session.flush()

    # Bump to v2; v1 is retired but the v1 evaluation must remain intact and queryable.
    v2 = ScorecardVersion(
        scorecard_id=scorecard.id, version_number=2, created_by=owner.id,
        guideline_notes="v2: clarified guideline wording (no scoring semantics changed).",
    )
    session.add(v2)
    session.flush()
    n2 = make_kpi_node(session, v2, "Quality", 100, display_order=0)
    add_full_guidelines(session, n2, style="generic")
    v1.is_active = False
    scorecard.current_version_id = v2.id
    session.flush()

    session.refresh(evaluation)
    assert evaluation.scorecard_version_id == v1.id, "v1 evaluation must still point at v1"
    still_there = (
        session.query(EvaluationKpiResult).filter_by(evaluation_id=evaluation.id).count()
    )
    assert still_there == 1, "v1 evaluation's KPI results must survive the version bump"

    report.add(
        "migration cases",
        "version bump preserves old evaluations (v1 evaluation intact after v2 becomes current)",
        "created",
    )


def scenario_embedding_model_backfill(session: Session, report: ScenarioReport, owner: User) -> None:
    scorecard = Scorecard(
        name="Migration: Embedding Model Backfill",
        owner_id=owner.id,
        domain="Internal",
        purpose_statement="Demonstrates re-embedding after an embedding model change.",
        status=ScorecardStatus.PUBLISHED,
    )
    session.add(scorecard)
    session.flush()
    version = ScorecardVersion(scorecard_id=scorecard.id, version_number=1, created_by=owner.id)
    session.add(version)
    session.flush()
    n1 = make_kpi_node(session, version, "Quality", 100, display_order=0)
    add_full_guidelines(session, n1, style="generic")
    scorecard.current_version_id = version.id
    session.flush()

    old_text = scorecard.purpose_statement or ""
    embedding_row = ScorecardEmbedding(
        scorecard_version_id=version.id,
        embedding=synthetic_embedding(old_text + "-v1-model"),
        embedding_model="amazon.titan-embed-text-v1",  # superseded model
        source_text_hash=text_hash(old_text),
    )
    session.add(embedding_row)
    session.flush()

    # Backfill: re-embed the same source text with the new model, updating in place
    # (unique FK on scorecard_version_id means this is an UPDATE, not a new row).
    embedding_row.embedding = synthetic_embedding(old_text + "-v2-model")
    embedding_row.embedding_model = "amazon.titan-embed-text-v2:0"
    session.flush()

    report.add(
        "migration cases",
        "embedding-model change requiring backfill (row updated in place, same unique FK)",
        "created",
    )


def scenario_flat_to_hierarchical_import(session: Session, report: ScenarioReport, owner: User) -> None:
    scorecard = Scorecard(
        name="Migration: Flat-to-Hierarchical Import",
        owner_id=owner.id,
        domain="Internal",
        status=ScorecardStatus.PUBLISHED,
    )
    session.add(scorecard)
    session.flush()
    v1 = ScorecardVersion(scorecard_id=scorecard.id, version_number=1, created_by=owner.id)
    session.add(v1)
    session.flush()
    flat_names_weights = [("A", 25), ("B", 25), ("C", 25), ("D", 25)]
    for i, (name, weight) in enumerate(flat_names_weights):
        node = make_kpi_node(session, v1, f"Flat KPI {name}", weight, display_order=i)
        add_full_guidelines(session, node, style="generic")
    scorecard.current_version_id = v1.id
    session.flush()

    # v2 imports the same 4 KPIs, now grouped under two new parent groups (hierarchical).
    v2 = ScorecardVersion(
        scorecard_id=scorecard.id, version_number=2, created_by=owner.id,
        guideline_notes="Imported/reorganized from v1's flat structure into 2 groups.",
    )
    session.add(v2)
    session.flush()
    group1 = make_kpi_node(session, v2, "Group 1 (A+B)", 50, display_order=0)
    group2 = make_kpi_node(session, v2, "Group 2 (C+D)", 50, display_order=1)
    a = make_kpi_node(session, v2, "Flat KPI A", 50, parent=group1, display_order=0)
    b = make_kpi_node(session, v2, "Flat KPI B", 50, parent=group1, display_order=1)
    c = make_kpi_node(session, v2, "Flat KPI C", 50, parent=group2, display_order=0)
    d = make_kpi_node(session, v2, "Flat KPI D", 50, parent=group2, display_order=1)
    for n in (group1, group2, a, b, c, d):
        add_full_guidelines(session, n, style="generic")
    v1.is_active = False
    scorecard.current_version_id = v2.id
    session.flush()
    report.add(
        "migration cases",
        "flat-to-hierarchical import (v1 flat 4-KPI -> v2 grouped 2x2 hierarchy)",
        "created",
    )


def scenario_bulk_spreadsheet_import(session: Session, report: ScenarioReport, owner: User) -> None:
    scorecard = Scorecard(
        name="Migration: Bulk Spreadsheet Import",
        owner_id=owner.id,
        domain="Internal",
        purpose_statement="Simulates importing many KPI rows from a spreadsheet in one batch.",
        status=ScorecardStatus.PUBLISHED,
    )
    session.add(scorecard)
    session.flush()
    version = ScorecardVersion(scorecard_id=scorecard.id, version_number=1, created_by=owner.id)
    session.add(version)
    session.flush()
    # 3 groups (33.34/33.33/33.33) of 5 leaves each (20% within each group) = 15 rows,
    # imported in a single transaction, as a bulk spreadsheet upload would be.
    group_weights = [33.34, 33.33, 33.33]
    total_leaves = 0
    for gi, gw in enumerate(group_weights):
        group = make_kpi_node(session, version, f"Imported Group {gi + 1}", gw, display_order=gi)
        add_full_guidelines(session, group, style="generic")
        for li in range(5):
            leaf = make_kpi_node(
                session, version, f"Imported Row {gi + 1}.{li + 1}", 20, parent=group, display_order=li
            )
            add_full_guidelines(session, leaf, style="generic")
            total_leaves += 1
    scorecard.current_version_id = version.id
    session.flush()
    report.add(
        "migration cases",
        "bulk spreadsheet import (15 KPI rows in one batch)",
        "created",
        f"scorecard_id={scorecard.id} rows={total_leaves}",
    )


# ------------------------------------------------------------------------------- chat ---


def scenario_chat_session_stub(session: Session, report: ScenarioReport, owner: User) -> None:
    """Minimal chat_sessions/chat_messages rows so the schema is exercised end-to-end.
    The real LangGraph/Bedrock-backed chat builder is Cycle 1c, out of scope here."""
    chat_session = ChatSession(
        user_id=owner.id,
        status=ChatSessionStatus.COMPLETED,
        context_summary="Seed placeholder chat session — no live Bedrock call was made.",
    )
    session.add(chat_session)
    session.flush()
    session.add_all(
        [
            ChatMessage(
                session_id=chat_session.id,
                role=ChatMessageRole.USER,
                content="I need a scorecard for rating onboarding emails.",
            ),
            ChatMessage(
                session_id=chat_session.id,
                role=ChatMessageRole.ASSISTANT,
                content="Seed placeholder assistant reply (Cycle 1c wires the real Bedrock call).",
                tool_calls={"tool": "ask_clarification", "args": {"missing_fields": ["target_score"]}},
            ),
        ]
    )
    session.flush()
    report.add("schema coverage (not part of the formal catalogue)", "chat_sessions/chat_messages rows", "created")


# ------------------------------------------------------------------------------- runner ---


def run_all_scenarios(session: Session) -> ScenarioReport:
    report = ScenarioReport()

    owner = get_or_create_user(session, "designer@qualityscorecard.local", "Designer One", role="designer")
    evaluator_a = get_or_create_user(session, "evaluator-a@qualityscorecard.local", "Evaluator A", role="evaluator")
    evaluator_b = get_or_create_user(session, "evaluator-b@qualityscorecard.local", "Evaluator B", role="evaluator")
    # Marker user: its presence signals "this DB has already been seeded" for idempotency.
    get_or_create_user(session, SEED_MARKER_EMAIL, "Seed Marker (do not delete)", role="system")
    session.commit()

    flat_version = scenario_flat_6kpi(session, report, owner)
    scenario_4level_hierarchy(session, report, owner)
    scenario_mid_band_and_double_evaluation(session, report, flat_version, evaluator_a, evaluator_b)
    scenario_reuse_via_similarity_clone(session, report, owner)
    session.commit()

    scenario_different_domains(session, report, owner)
    scenario_numeric_vs_categorical_guidelines(session, report, owner)
    scenario_skewed_weights(session, report, owner)
    scenario_2_vs_40_kpi(session, report, owner)
    session.commit()

    scenario_lifecycle_states(session, report, owner)
    session.commit()

    scenario_boundary_cases(session, report, owner)
    session.commit()

    scenario_invalid_weight_sum(session, report, owner, target_total=97)
    scenario_invalid_weight_sum(session, report, owner, target_total=103)
    scenario_invalid_duplicate_names(session, report, owner)
    scenario_invalid_cross_version_reference(session, report, owner)
    scenario_invalid_out_of_range_score(session, report, owner)
    scenario_flawed_missing_guideline_level(session, report, owner)
    session.commit()

    scenario_version_bump_preserves_evaluations(session, report, owner)
    scenario_embedding_model_backfill(session, report, owner)
    scenario_flat_to_hierarchical_import(session, report, owner)
    scenario_bulk_spreadsheet_import(session, report, owner)
    session.commit()

    scenario_chat_session_stub(session, report, owner)
    session.commit()

    return report
