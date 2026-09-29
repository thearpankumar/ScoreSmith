"""Safe evaluation of a scorecard's optional custom `scoring_formula`
(`scorecard_versions.scoring_formula`, nullable text — see Alembic migration
0005_scoring_formula_and_kpi_flags).

`NULL` (the default for every scorecard unless a user explicitly customizes it) means
"use the classic weighted-average behavior" — callers (`app/ai/judge.py`,
`app/api/v1/evaluations.py`) never call into this module for that case; they keep using
`compute_weighted_score`/`effective_leaf_weights` exactly as before, byte-for-byte
unchanged, for backward compatibility.

A non-null formula is an arbitrary mathematical expression referencing KPI scores as
`kpi["KPI Name"]` (dict-subscript by the KPI's own display name — natural for both a human
and an LLM to write, and needs no separate stable-id/slug scheme since a scorecard's leaf
KPI names are already its natural human-facing identifier). Supports `+ - * /` and `**` for
exponentiation (see `_FORMULA_SYNTAX_HELP` for why `^` is intentionally NOT remapped to
power: Python's `^` operator is bitwise XOR with LOWER precedence than `+`/`-`, so
naively remapping it to `pow` would silently compute the WRONG value for an expression like
`kpi["A"] ^ 2 + kpi["B"]`, which most people would expect to parse as `(kpi["A"] ** 2) +
kpi["B"]` but Python's grammar actually parses as `kpi["A"] ^ (2 + kpi["B"])`. Since KPI
scores are floats, Python's own `^`/XOR operator raises a clean `TypeError` on them anyway
(`float.__xor__` doesn't exist) — `evaluate()` below catches that specific case and raises a
friendly `FormulaError` telling the author to use `**` instead, rather than silently
computing a wrong number), parentheses, comparisons, and the functions `min`, `max`,
`avg`/`mean`, `sqrt`, `abs`.

**Safe-eval library choice**: a live websearch (2026-09-29, see this pass's task notes) was
done specifically to confirm this — `simpleeval` (https://github.com/danthedeckie/simpleeval,
on PyPI as `simpleeval`, pinned in `pyproject.toml`) is actively maintained (a release as
recently as March 2026 per its PyPI page) and is the standard, current choice for
"evaluate an arbitrary human-authored math expression safely in Python" — it parses via
`ast` and walks the tree against an explicit whitelist of operators/names/functions (no
`eval`/`exec`, no access to `__import__`/builtins/attribute traversal onto unsafe objects),
which is exactly the mitigation this task's owner asked for. `EvalWithCompoundTypes` (the
subclass used here) additionally supports the dict-subscript syntax (`kpi["Name"]`) this
module's variable-naming scheme depends on.
"""

from __future__ import annotations

import ast
import math
from dataclasses import dataclass, field

from simpleeval import EvalWithCompoundTypes, FunctionNotDefined, InvalidExpression, NameNotDefined

# The one name every formula may reference: a dict subscripted by exact KPI name, e.g.
# `kpi["Response Time"]`. Kept as a module-level constant since both `_extract_kpi_refs`
# (AST-walk, no evaluation) and `evaluate` (real evaluation) must agree on it.
KPI_VAR_NAME = "kpi"


def _avg(*args: float) -> float:
    """`avg`/`mean`: accepts either variadic scalar args (`avg(a, b, c)`) or a single
    list/tuple (`avg([a, b, c])`) — mirrors how a non-programmer would naturally try to
    call it either way. `min`/`max` already support both call shapes via Python's own
    builtins, so only `avg` needs this small shim."""
    values = args[0] if len(args) == 1 and isinstance(args[0], list | tuple) else args
    if not values:
        raise ValueError("avg()/mean() requires at least one value")
    return sum(values) / len(values)


_ALLOWED_FUNCTIONS = {
    "min": min,
    "max": max,
    "avg": _avg,
    "mean": _avg,
    "sqrt": math.sqrt,
    "abs": abs,
}

_FORMULA_SYNTAX_HELP = (
    'Reference a KPI\'s score as kpi["Exact KPI Name"]. Supports + - * / ** (use ** for '
    "exponentiation, not ^) and the functions min, max, avg, mean, sqrt, abs."
)


@dataclass
class FormulaValidationResult:
    valid: bool
    error: str | None = None
    # KPI names in `available_kpi_names` that the formula never references — surfaced as a
    # dismissable warning by the frontend ("these KPIs aren't referenced by your formula").
    unused_kpis: list[str] = field(default_factory=list)

    def to_dict(self) -> dict:
        return {"valid": self.valid, "error": self.error, "unused_kpis": self.unused_kpis}


class FormulaError(ValueError):
    """Raised by `evaluate()` on any invalid/unsafe formula or evaluation-time failure
    (unknown KPI reference, disallowed name/function, non-finite result, etc). A ValueError
    subclass so existing `except ValueError` call sites (mirroring `draft_materialize.py`'s
    own incomplete-draft ValueError, and `update_draft`'s patch-rejection pattern in
    `scorecard_builder.py`) catch it without special-casing."""


def _extract_kpi_refs(tree: ast.Expression) -> set[str]:
    """Walks the parsed AST (no evaluation) collecting every `kpi["Name"]` string-literal
    subscript reference. Used by both `validate()` (to catch an unknown-KPI reference
    before ever evaluating, and to compute `unused_kpis`) — deliberately AST-based rather
    than a regex, so it can't be fooled by a KPI name that itself contains something
    regex-special, and stays in sync with whatever `KPI_VAR_NAME` is set to."""
    refs: set[str] = set()
    for node in ast.walk(tree):
        if not isinstance(node, ast.Subscript):
            continue
        if not (isinstance(node.value, ast.Name) and node.value.id == KPI_VAR_NAME):
            continue
        key_node = node.slice
        if isinstance(key_node, ast.Constant) and isinstance(key_node.value, str):
            refs.add(key_node.value)
    return refs


def _build_evaluator(kpi_scores: dict[str, float]) -> EvalWithCompoundTypes:
    return EvalWithCompoundTypes(
        names={KPI_VAR_NAME: dict(kpi_scores)},
        functions=dict(_ALLOWED_FUNCTIONS),
    )


def evaluate(formula: str, kpi_scores: dict[str, float]) -> float:
    """Evaluates `formula` against `kpi_scores` (KPI display name -> its 0-10 score) and
    returns the resulting number. Raises `FormulaError` for anything that goes wrong —
    never lets a raw `simpleeval`/Python exception (or, since `eval`/`exec` are never used
    at all, any arbitrary code execution) escape to the caller.

    Used by BOTH `app/ai/judge.py` (the AI judge path) and the manual-evaluation finalize
    endpoint (`app/api/v1/evaluations.py`) whenever `scorecard_versions.scoring_formula is
    not NULL` — the one shared implementation of "what does a custom formula compute",
    so the two paths can never silently disagree.
    """
    stripped = (formula or "").strip()
    if not stripped:
        raise FormulaError("Formula is empty.")
    try:
        tree = ast.parse(stripped, mode="eval")
    except SyntaxError as exc:
        raise FormulaError(f"Syntax error: {exc.msg} (near {exc.text!r})") from exc

    unknown = sorted(_extract_kpi_refs(tree) - set(kpi_scores))
    if unknown:
        raise FormulaError(
            f"Formula references KPI name(s) not in scope: {', '.join(unknown)}. "
            f"{_FORMULA_SYNTAX_HELP}"
        )

    evaluator = _build_evaluator(kpi_scores)
    try:
        result = evaluator.eval(stripped)
    except TypeError as exc:
        if "^" in stripped and "unsupported operand type" in str(exc):
            raise FormulaError(
                "'^' is not exponentiation in this formula language (it's bitwise XOR, "
                "which doesn't apply to KPI scores). Use ** for exponentiation instead."
            ) from exc
        raise FormulaError(f"Formula evaluation error: {exc}") from exc
    except (FunctionNotDefined, NameNotDefined, InvalidExpression, KeyError, ZeroDivisionError, ValueError) as exc:
        raise FormulaError(f"Formula evaluation error: {exc}") from exc

    try:
        numeric_result = float(result)
    except (TypeError, ValueError) as exc:
        raise FormulaError(f"Formula must evaluate to a number, got {result!r}.") from exc
    if not math.isfinite(numeric_result):
        raise FormulaError(f"Formula evaluated to a non-finite number ({numeric_result!r}).")
    return numeric_result


def validate(formula: str | None, available_kpi_names: list[str]) -> FormulaValidationResult:
    """Parses (and, via a dry run against dummy scores, semantically checks) `formula`
    WITHOUT requiring real KPI scores — used both server-side (to reject an invalid
    `update_scoring_formula` tool call from the LLM builder, and by the
    `POST /scorecards/{id}/validate-formula`-style endpoint the frontend's live-validation
    editor calls) and is the single source of truth `evaluate()` itself is dry-run through,
    so validation can never drift from what evaluation would actually do.

    `formula=None` (or an empty/whitespace string) is treated as valid — the "clear the
    custom formula, revert to the default weighted average" case — with no unused_kpis
    warning, since there is no formula to have missed anything."""
    if formula is None or not formula.strip():
        return FormulaValidationResult(valid=True, error=None, unused_kpis=[])

    stripped = formula.strip()
    try:
        tree = ast.parse(stripped, mode="eval")
    except SyntaxError as exc:
        return FormulaValidationResult(valid=False, error=f"Syntax error: {exc.msg} (near {exc.text!r})")

    referenced = _extract_kpi_refs(tree)
    unknown = sorted(referenced - set(available_kpi_names))
    if unknown:
        return FormulaValidationResult(
            valid=False,
            error=(
                f"Formula references KPI name(s) not in this scorecard: {', '.join(unknown)}. "
                f"{_FORMULA_SYNTAX_HELP}"
            ),
        )

    # Dry-run against dummy mid-scale scores to catch evaluation-time problems (disallowed
    # function/name, div-by-zero, etc) that pure AST inspection can't see — reuses
    # `evaluate()` itself so validation can never disagree with real evaluation.
    dummy_scores = {name: 5.0 for name in available_kpi_names}
    try:
        evaluate(stripped, dummy_scores)
    except FormulaError as exc:
        return FormulaValidationResult(valid=False, error=str(exc))

    unused = sorted(set(available_kpi_names) - referenced)
    return FormulaValidationResult(valid=True, error=None, unused_kpis=unused)
