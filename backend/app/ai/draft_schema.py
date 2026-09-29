"""`ScorecardDraft` — the persisted, validated shape of an in-progress scorecard being
built by the chat scorecard-builder graph (`scorecard_builder.py`).

Every `update_draft` tool call from the model is validated against this schema (via
`ScorecardDraft.model_validate(existing.model_dump() | patch)`) before it is committed to
LangGraph state — a malformed patch is rejected and the model is asked to retry, so the
LLM can never corrupt draft state (see `scorecard_builder.py::apply_patch`).

Field mapping to the existing CRUD schema (`app/models/scorecard.py` et al.), used by
`materialize_draft` when a confirmed draft is turned into real `Scorecard` /
`ScorecardVersion` / `KpiNode` / `KpiGuideline` rows:
- `name`, `purpose`, `domain`, `target_score` map 1:1 to `scorecards` columns.
- `audience` has no dedicated column on `scorecards` (see plan's schema sketch — no
  `audience` field). It is folded into `scorecards.scope` on materialization
  (`"Audience: {audience}\n\n{scope}"`), since `scope` is documented as "what is/isn't
  covered by this scorecard" — the intended audience is part of that framing. This is a
  documented, deliberate choice, not an oversight.
- `KpiDraft.level` / `.parent_name` build the `ltree` hierarchy by *name* reference
  (the LLM never sees or invents UUIDs); `materialize_draft` resolves `parent_name` to a
  real `kpi_nodes.id` as it creates nodes level-by-level.
- `KpiDraft.guidelines` (keyed by `0`-`10` score level) map 1:1 to `kpi_guidelines` rows.
"""

from __future__ import annotations

from pydantic import BaseModel, Field, field_validator, model_validator

# Bounds mirrored from the DB CHECK constraints in app/models/*.py, so a validation
# failure here is guaranteed to also be a validation failure there.
MIN_SCORE_LEVEL = 0
MAX_SCORE_LEVEL = 10
MIN_HIERARCHY_LEVEL = 1
MAX_HIERARCHY_LEVEL = 4


class GuidelineDraft(BaseModel):
    qualitative_text: str
    quantitative_criteria: dict | None = None


class KpiDraft(BaseModel):
    name: str
    weight: float | None = Field(default=None, ge=0, le=100)
    level: int = Field(default=1, ge=MIN_HIERARCHY_LEVEL, le=MAX_HIERARCHY_LEVEL)
    # Name-reference to the parent KPI within the same draft (None => a root/Level-1 KPI).
    # Resolved to a real kpi_nodes.id only at materialization time.
    parent_name: str | None = None
    # When False, this KPI is tracked/scored but excluded from the sibling
    # weight-sum-to-100 rule (see _sibling_weight_issue below) AND from the default
    # weighted-average formula (mirrors kpi_nodes.included_in_scoring — see migration
    # 0005_scoring_formula_and_kpi_flags). Defaults True (today's unchanged behavior).
    included_in_scoring: bool = True
    # Keyed by score_level (0-10) as a string, since JSON object keys are always strings
    # (this is exactly the shape the model's `update_draft` tool call will send).
    guidelines: dict[str, GuidelineDraft] = Field(default_factory=dict)

    @field_validator("guidelines")
    @classmethod
    def _validate_guideline_keys(cls, value: dict[str, GuidelineDraft]) -> dict[str, GuidelineDraft]:
        for key in value:
            try:
                level = int(key)
            except ValueError as exc:
                raise ValueError(f"guideline key {key!r} is not an integer score level") from exc
            if not (MIN_SCORE_LEVEL <= level <= MAX_SCORE_LEVEL):
                raise ValueError(
                    f"guideline score_level {level} out of range "
                    f"[{MIN_SCORE_LEVEL}, {MAX_SCORE_LEVEL}]"
                )
        return value


class ScorecardDraft(BaseModel):
    """The full in-progress scorecard state. Always valid on its own terms (every field
    that is present satisfies its constraints) — but may still be *incomplete*, which
    `missing_fields()` reports so the graph knows whether to keep asking clarifying
    questions or move to `confirm`."""

    name: str | None = None
    purpose: str | None = None
    domain: str | None = None
    audience: str | None = None
    target_score: float | None = Field(default=None, ge=0, le=10)
    kpis: list[KpiDraft] = Field(default_factory=list)
    # NULL (default) = the classic weighted-average formula. Set via the dedicated
    # `update_scoring_formula` tool (scorecard_builder.py), which validates it (see
    # app/ai/scoring_formula.py) against this draft's KPI names before it is ever stored
    # here — so, unlike `kpis`, this field is intentionally NOT re-validated by a
    # model_validator on every plain `update_draft` patch (a formula can only be set
    # through its own tool, never smuggled in via an unrelated patch).
    scoring_formula: str | None = None

    @model_validator(mode="after")
    def _validate_parent_references(self) -> ScorecardDraft:
        names = {k.name for k in self.kpis}
        for kpi in self.kpis:
            if kpi.parent_name is not None and kpi.parent_name not in names:
                raise ValueError(
                    f"KPI {kpi.name!r} references parent_name {kpi.parent_name!r}, "
                    "which is not (yet) a KPI name in this draft"
                )
            if kpi.parent_name == kpi.name:
                raise ValueError(f"KPI {kpi.name!r} cannot be its own parent")
        return self

    # --- completeness tracker -------------------------------------------------------

    def missing_fields(self) -> list[str]:
        """Fields the graph should keep asking about. Order is the suggested question
        order (purpose/domain first — per the framework's 7-step method — then KPIs)."""
        missing: list[str] = []
        if not self.name:
            missing.append("name")
        if not self.purpose:
            missing.append("purpose")
        if not self.domain:
            missing.append("domain")
        if not self.kpis:
            missing.append("kpis")
        else:
            for kpi in self.kpis:
                if kpi.weight is None:
                    missing.append(f"kpis[{kpi.name}].weight")
                if not kpi.guidelines:
                    missing.append(f"kpis[{kpi.name}].guidelines")
            weight_issue = self._sibling_weight_issue()
            if weight_issue:
                missing.append(weight_issue)
        if self.target_score is None:
            missing.append("target_score")
        return missing

    def _sibling_weight_issue(self) -> str | None:
        """Mirrors the DB's weight-sum-to-100-per-sibling-group rule (see
        app/models/kpi_node.py / migration 0005_scoring_formula_and_kpi_flags), checked
        here so the chat flow can catch it before the draft is ever materialized into real
        KpiNode rows. A KPI with `included_in_scoring=False` is informational-only and is
        excluded from its sibling group's sum entirely, exactly like the DB trigger."""
        groups: dict[str | None, list[float]] = {}
        for kpi in self.kpis:
            if kpi.weight is None or not kpi.included_in_scoring:
                continue
            groups.setdefault(kpi.parent_name, []).append(kpi.weight)
        for parent_name, weights in groups.items():
            total = sum(weights)
            if abs(total - 100.0) > 0.01:
                label = parent_name or "root"
                return f"sibling_weights[{label}]=~{total:.2f} (must sum to 100)"
        return None

    def is_complete(self) -> bool:
        return not self.missing_fields()
