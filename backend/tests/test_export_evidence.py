"""Pure tests: evidence normalisation (every stored shape), the exportable-evaluation filter, and row splitting."""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from app.models.enums import EvaluationStatus
from app.reporting.export_data import EvidenceItem, ExportResult, _is_exportable, normalize_evidence
from app.reporting.workbook import _lines, _max_lines, split_for_cell


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        (None, []),
        ("", []),
        ([], []),
        ({}, []),
        (["a", "b", "a"], [EvidenceItem("a"), EvidenceItem("b")]),  # de-duplicated, order kept
        ("plain text", [EvidenceItem("plain text")]),
        ("• one\n• two", [EvidenceItem("one"), EvidenceItem("two")]),
        ("1. first\n2) second", [EvidenceItem("first"), EvidenceItem("second")]),
        ([{"quote": "q", "section": "S1", "label": "Slide 4"}], [EvidenceItem("q", "Slide 4 · S1")]),
        ([{"text": "t", "page": 3}], [EvidenceItem("t", "3")]),
        ({"quote": "z", "source": "doc.pdf"}, [EvidenceItem("z", "doc.pdf")]),
        ({"p1": ["x", "y"]}, [EvidenceItem("x", "p1"), EvidenceItem("y", "p1")]),
        ([["nested"]], [EvidenceItem("nested")]),
        ([{"weird": "shape", "n": 1}], [EvidenceItem("weird: shape; n: 1")]),  # unknown shape is kept, not dropped
        ([" ", "", "ok"], [EvidenceItem("ok")]),
    ],
)
def test_normalize_evidence_shapes(raw, expected) -> None:
    assert normalize_evidence(raw) == expected


def test_evidence_is_capped_and_cleaned() -> None:
    (item,) = normalize_evidence(["x" * 20000 + "\x00\x01"])
    assert len(item.quote) <= 8000 and "\x00" not in item.quote


def test_export_result_accepts_raw_text_and_exposes_text_and_sources() -> None:
    res = ExportResult("k", 7.0, 7, "why", "• first\n• second", False, None, "")  # type: ignore[arg-type]
    assert [i.quote for i in res.evidence] == ["first", "second"]
    res2 = ExportResult("k", 7.0, 7, "why", [EvidenceItem("q1", "A"), EvidenceItem("q2", "A"), EvidenceItem("q3", "B")],
                        False, None, "")  # type: ignore[arg-type]
    assert res2.evidence_text == "• “q1” — A\n• “q2” — A\n• “q3” — B"
    assert res2.evidence_sources == "A; B"


@pytest.mark.parametrize(
    ("status", "score", "ok"),
    [
        (EvaluationStatus.COMPLETED, 7.0, True),
        ("completed", 0.0, True),
        (EvaluationStatus.COMPLETED, None, False),  # completed but unscored
        (EvaluationStatus.FAILED, 5.0, False),
        (EvaluationStatus.QUEUED, None, False),
        ("processing", 4.0, False),
    ],
)
def test_only_completed_scored_evaluations_are_exportable(status, score, ok) -> None:
    assert _is_exportable(SimpleNamespace(status=status, final_weighted_score=score)) is ok


def test_split_for_cell_keeps_every_chunk_within_one_row_and_loses_no_words() -> None:
    text = " ".join(f"word{i}" for i in range(4000))  # ~30k chars
    chunks = split_for_cell(text, 110.0, 10)
    assert len(chunks) > 1
    assert all(_lines(c, 110.0, 10) <= _max_lines(10) for c in chunks)
    assert " ".join(chunks).split() == text.split()


def test_split_for_cell_handles_an_unbroken_token_and_empty_text() -> None:
    token = "x" * 30000
    chunks = split_for_cell(token, 110.0, 10)
    assert all(_lines(c, 110.0, 10) <= _max_lines(10) for c in chunks) and "".join(chunks) == token
    assert split_for_cell("   ", 110.0, 10) == [] and split_for_cell("short", 110.0, 10) == ["short"]
