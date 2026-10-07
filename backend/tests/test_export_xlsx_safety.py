"""Pure tests for app/reporting/xlsx_safety.py."""

from __future__ import annotations

from app.reporting.xlsx_safety import (
    clean_text,
    is_plausible_email,
    needs_quote_prefix,
    safe_sheet_name,
    slugify_filename,
)


def test_sheet_name_strips_forbidden_chars_and_caps_at_31() -> None:
    used: set[str] = set()
    name = safe_sheet_name("Matrix: Q3/Q4 [draft]? *final* \\ report with a very long scorecard name", used)
    assert len(name) <= 31
    assert not any(ch in name for ch in "[]:*?/\\")


def test_sheet_name_unique_case_insensitive() -> None:
    used: set[str] = set()
    a = safe_sheet_name("Notes", used)
    b = safe_sheet_name("NOTES", used)
    c = safe_sheet_name("notes", used)
    assert len({a.lower(), b.lower(), c.lower()}) == 3
    assert all(len(x) <= 31 for x in (a, b, c))


def test_sheet_name_no_edge_apostrophes_and_reserved() -> None:
    used: set[str] = set()
    assert not safe_sheet_name("'quoted'", used).startswith("'")
    assert not safe_sheet_name("'quoted2'", used).endswith("'")
    assert safe_sheet_name("History", set()).lower() != "history"
    assert safe_sheet_name("   ", set()) == "Sheet"
    long = safe_sheet_name("x" * 40, {"x" * 31})
    assert len(long) <= 31 and long.lower() != "x" * 31


def test_clean_text_removes_illegal_control_chars_and_truncates() -> None:
    assert clean_text("a\x00b\x0bc\x1fd\te\nf") == "abcd\te\nf"
    assert clean_text(None) == ""
    out = clean_text("y" * 50_000)
    assert len(out) <= 32_000 and out.endswith("[truncated]")
    assert len(clean_text("z" * 100, max_len=50)) <= 50


def test_quote_prefix_detection() -> None:
    for s in ('=HYPERLINK("http://evil","x")', "+cmd|' /C calc'!A0", "@SUM(1+1)", "-2+3", "\tTabbed", "\rCR"):
        assert needs_quote_prefix(s)
    for s in ("Priya", "a=b", "", " =leading space"):
        assert not needs_quote_prefix(s)


def test_email_plausibility() -> None:
    assert is_plausible_email("priya.shah+x@example.co.uk")
    for bad in (None, "", "no-at.example.com", "a@b", "a b@c.com", "a@b.com, c@d.com", "a@b.com;c@d.com", "a@@b.com",
                "a@b.com\x00", "<a@b.com>", "x" * 250 + "@b.com"):
        assert not is_plausible_email(bad)


def test_slugify_filename_is_ascii_safe() -> None:
    assert slugify_filename("Hackathon Scoring!") == "Hackathon-Scoring"
    assert slugify_filename("Café ünï/\\:*?") == "Cafe-uni"
    assert slugify_filename("日本語") == "export"
    assert len(slugify_filename("a" * 100)) <= 40
