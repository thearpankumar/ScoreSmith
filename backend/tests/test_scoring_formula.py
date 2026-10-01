"""Unit tests for the `simpleeval`-based safe scoring-formula evaluator
(`app/ai/scoring_formula.py`) — pure logic, no DB/Bedrock, previously only exercised
indirectly through chat-builder/API integration tests. These directly verify the
formula-builder widget's actual computational core end to end: everything
`ScoringFormulaBuilderDialog`/`ScoringFormulaPanel` on the frontend ultimately calls
through `POST /scorecards/validate-formula` and `POST /scorecards/{id}/versions/{id}/
validate-formula` (see app/api/v1/scorecards.py) reduces to `validate()`/`evaluate()`
here.
"""

from __future__ import annotations

import pytest

from app.ai.scoring_formula import FormulaError, evaluate, validate

_KPI_NAMES = ["Accuracy", "Tone", "Response Time"]
_SCORES = {"Accuracy": 8.0, "Tone": 6.0, "Response Time": 10.0}


# --- evaluate() ---------------------------------------------------------------------


def test_evaluate_basic_arithmetic() -> None:
    assert evaluate('kpi["Accuracy"] + kpi["Tone"]', _SCORES) == pytest.approx(14.0)


def test_evaluate_weighted_average_equivalent() -> None:
    formula = '(kpi["Accuracy"] * 0.6 + kpi["Tone"] * 0.4)'
    assert evaluate(formula, _SCORES) == pytest.approx(8.0 * 0.6 + 6.0 * 0.4)


def test_evaluate_exponentiation_with_double_star() -> None:
    assert evaluate('kpi["Accuracy"] ** 2', _SCORES) == pytest.approx(64.0)


def test_evaluate_min_max_avg_sqrt_abs() -> None:
    assert evaluate('min(kpi["Accuracy"], kpi["Tone"])', _SCORES) == pytest.approx(6.0)
    assert evaluate('max(kpi["Accuracy"], kpi["Tone"])', _SCORES) == pytest.approx(8.0)
    assert evaluate('avg(kpi["Accuracy"], kpi["Tone"], kpi["Response Time"])', _SCORES) == pytest.approx(8.0)
    assert evaluate('mean([kpi["Accuracy"], kpi["Tone"]])', _SCORES) == pytest.approx(7.0)
    assert evaluate('sqrt(kpi["Accuracy"])', _SCORES) == pytest.approx(8.0**0.5)
    assert evaluate('abs(kpi["Accuracy"] - kpi["Response Time"])', _SCORES) == pytest.approx(2.0)


def test_evaluate_empty_formula_raises() -> None:
    with pytest.raises(FormulaError, match="empty"):
        evaluate("   ", _SCORES)


def test_evaluate_syntax_error_raises_friendly_message() -> None:
    with pytest.raises(FormulaError, match="Syntax error"):
        evaluate('kpi["Accuracy"] +', _SCORES)


def test_evaluate_unknown_kpi_reference_raises() -> None:
    with pytest.raises(FormulaError, match="not in scope"):
        evaluate('kpi["Nonexistent KPI"]', _SCORES)


def test_evaluate_caret_xor_raises_friendly_exponentiation_hint() -> None:
    """`^` is bitwise XOR in Python, not exponentiation — `evaluate()` must catch the
    resulting TypeError on float operands and redirect the author to `**` rather than
    producing a cryptic simpleeval/Python traceback or (worse) silently computing the
    wrong value (see the module's own docstring on precedence)."""
    with pytest.raises(FormulaError, match=r"\*\*"):
        evaluate('kpi["Accuracy"] ^ 2', _SCORES)


def test_evaluate_division_by_zero_raises() -> None:
    with pytest.raises(FormulaError):
        evaluate('kpi["Accuracy"] / 0', _SCORES)


def test_evaluate_disallowed_function_raises() -> None:
    with pytest.raises(FormulaError):
        evaluate('open("/etc/passwd")', _SCORES)


def test_evaluate_disallowed_name_raises() -> None:
    with pytest.raises(FormulaError):
        evaluate("__import__('os')", _SCORES)


def test_evaluate_non_finite_result_raises() -> None:
    with pytest.raises(FormulaError):
        evaluate('1 / (kpi["Accuracy"] - kpi["Accuracy"])', _SCORES)


def test_evaluate_float_overflow_raises_formula_error_not_uncaught_overflowerror() -> None:
    """REGRESSION (real bug found while verifying the formula builder end to end, per
    this task's instruction to actually verify it works): `kpi["Accuracy"] ** 400` with a
    real, plausible KPI score (8.0 here — well within the normal 0-10 range) computes a
    result so large it overflows a Python float, raising a plain `OverflowError`.
    `simpleeval`'s own `safe_power` guard (`MAX_POWER`) only rejects a huge BASE or
    EXPONENT value — 400 is nowhere near that threshold — so it does NOT catch this; only
    `evaluate()`'s own exception handling does. Before the fix, this exception was not in
    the caught tuple and escaped `evaluate()` entirely (confirmed live), which would have
    surfaced as an unhandled 500 during a real evaluation run rather than the documented
    "never lets a raw exception escape" `FormulaError` contract — notably, `validate()`'s
    dry-run (against a dummy mid-scale score of 5.0, see below) does NOT catch this
    specific case, so a formula like this could be saved as "valid" and only crash later."""
    with pytest.raises(FormulaError, match="Formula evaluation error"):
        evaluate('kpi["Accuracy"] ** 400', {"Accuracy": 8.0})


# --- validate() ----------------------------------------------------------------------


def test_validate_none_or_blank_is_valid_default_case() -> None:
    assert validate(None, _KPI_NAMES).valid is True
    assert validate("   ", _KPI_NAMES).valid is True


def test_validate_good_formula_reports_unused_kpis() -> None:
    result = validate('kpi["Accuracy"] + kpi["Tone"]', _KPI_NAMES)
    assert result.valid is True
    assert result.error is None
    assert result.unused_kpis == ["Response Time"]


def test_validate_fully_referenced_formula_has_no_unused_kpis() -> None:
    result = validate('avg(kpi["Accuracy"], kpi["Tone"], kpi["Response Time"])', _KPI_NAMES)
    assert result.valid is True
    assert result.unused_kpis == []


def test_validate_unknown_kpi_name_is_invalid() -> None:
    result = validate('kpi["Not A Real KPI"]', _KPI_NAMES)
    assert result.valid is False
    assert "Not A Real KPI" in (result.error or "")


def test_validate_syntax_error_is_invalid() -> None:
    result = validate('kpi["Accuracy"] + + +', _KPI_NAMES)
    assert result.valid is False
    assert "Syntax error" in (result.error or "")


def test_validate_catches_division_by_zero_via_dry_run() -> None:
    """validate() must dry-run evaluate() against dummy scores so a formula that is
    syntactically fine but blows up at evaluation time (e.g. a KPI difference used as a
    divisor that CAN be zero) is still rejected before ever being saved."""
    result = validate('kpi["Accuracy"] / (kpi["Tone"] - kpi["Tone"])', _KPI_NAMES)
    assert result.valid is False
    assert result.error is not None
