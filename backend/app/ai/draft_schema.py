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
  real `kpi_nodes.id` as it creates nodes level-by-level. The chat builder's research
  fan-out (`scorecard_builder.py::research_kpis`) uses exactly this mechanism to express
  KPI CATEGORIES: a category is simply a `level=1` `KpiDraft` with `parent_name=None` — no
  weight and no guidelines of its own (see `missing_fields()` below, which treats any KPI
  referenced as another KPI's `parent_name` as a purely organizational grouping node that
  needs neither — see migration 0008_category_nodes_no_weight), and each KPI researched
  under it is a `level=2` `KpiDraft` with `parent_name=<category name>`. No schema
  addition was needed for this — `level`/`parent_name` already supported it end to end.
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

    def has_all_guideline_levels(self) -> bool:
        """True iff this KPI carries a guideline for EVERY score level 0-10."""
        return {int(k) for k in self.guidelines} == set(range(MIN_SCORE_LEVEL, MAX_SCORE_LEVEL + 1))

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
            # A KPI referenced as some OTHER KPI's `parent_name` is a grouping/CATEGORY
            # node (see draft_schema.py module docstring) — it exists purely to group its
            # children (exactly mirroring `app/ai/judge.py::leaf_nodes`'s "only leaf KPI
            # nodes are judged directly; an internal node has no guidelines of its own to
            # score against"), so it never needs guidelines OR a weight of its own (see
            # migration 0008_category_nodes_no_weight) — only LEAF KPIs are weighted, and
            # every leaf's weight must sum to 100 across the WHOLE draft (see
            # `_sibling_weight_issue` below), not per category.
            parent_names = {kpi.parent_name for kpi in self.kpis if kpi.parent_name}
            for kpi in self.kpis:
                is_leaf = kpi.name not in parent_names
                if not is_leaf:
                    continue  # category/grouping node: no weight, no guidelines required
                # An informational leaf (`included_in_scoring=False`) is excluded from the weight sum and
                # from the weighted average, so it has nothing to be weighted against and needs no weight
                # (requiring one made such a draft impossible to complete, and so impossible to save).
                if kpi.weight is None and kpi.included_in_scoring:
                    missing.append(f"kpis[{kpi.name}].weight")
                if not kpi.guidelines:
                    missing.append(f"kpis[{kpi.name}].guidelines")
                elif not kpi.has_all_guideline_levels():
                    # A partial rubric (e.g. only levels 0/5/10, or a model that stopped writing
                    # after level 6) is NOT a rubric: the judge scores against all 11 levels.
                    missing.append(f"kpis[{kpi.name}].guidelines ({len(kpi.guidelines)}/11 levels)")
            weight_issue = self._sibling_weight_issue()
            if weight_issue:
                missing.append(weight_issue)
        if self.target_score is None:
            missing.append("target_score")
        return missing

    def _sibling_weight_issue(self) -> str | None:
        """Mirrors the DB's weight-sum rule (see app/models/kpi_node.py / migration
        0008_category_nodes_no_weight), checked here so the chat flow can catch it before
        the draft is ever materialized into real KpiNode rows.

        Only LEAF KPIs (never referenced as another KPI's `parent_name`) are weighted —
        a category/grouping node carries no weight and never participates here, at any
        level. Every included leaf across the WHOLE draft must sum to 100 together
        (regardless of which category, if any, it's nested under) — NOT per immediate
        parent group, since categories no longer have a weight share of their own for a
        leaf's weight to be relative to (see that migration's own docstring for why the
        old per-parent-group/multiplicative scheme was replaced with this flat one). A KPI
        with `included_in_scoring=False` is informational-only and is excluded from the
        sum entirely, exactly like the DB trigger."""
        parent_names = {kpi.parent_name for kpi in self.kpis if kpi.parent_name}
        weights: list[float] = []
        for kpi in self.kpis:
            is_leaf = kpi.name not in parent_names
            if not is_leaf or kpi.weight is None or not kpi.included_in_scoring:
                continue
            weights.append(kpi.weight)
        if not weights:
            return None
        total = sum(weights)
        if abs(total - 100.0) > 0.01:
            return f"sibling_weights[leaves]=~{total:.2f} (must sum to 100)"
        return None

    def is_complete(self) -> bool:
        return not self.missing_fields()
