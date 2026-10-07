"""Parity + edge tests for the target-relative band model (app/reporting/palette.py).

`tests/data/target_band_cases.json` is shared with the frontend (`getTargetBand` in frontend/lib/rag.ts), so the
Excel export and the UI can never disagree about which band a score falls in.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from app.reporting import palette as P

FIXTURE = Path(__file__).resolve().parent / "data" / "target_band_cases.json"
DATA = json.loads(FIXTURE.read_text(encoding="utf-8"))
CASES = DATA["cases"]


def _target(key: str) -> float | None:
    return None if key == "None" else float(key)


def test_fixture_is_substantial() -> None:
    assert len(CASES) > 300
    assert set(DATA["thresholds"]) == {"None", "0", "4", "5.5", "7", "8", "9.5", "10"}


@pytest.mark.parametrize("key", list(DATA["thresholds"]))
def test_thresholds_match_the_shared_fixture(key: str) -> None:
    assert P.band_thresholds(_target(key)) == DATA["thresholds"][key]


def test_every_shared_case_gets_the_expected_band() -> None:
    wrong = [c for c in CASES if P.target_band(c["score"], c["target"]).key != c["band"]]
    assert not wrong, wrong[:5]


def test_band_order_labels_and_distinct_colours() -> None:
    assert [b.key for b in P.TARGET_BANDS] == ["exceeds", "meets", "near", "below", "well_below", "critical"]
    assert len({b.fill for b in P.TARGET_BANDS}) == 6 and len({b.tint for b in P.TARGET_BANDS}) == 6
    assert P.TARGET_BAND_BY_KEY["meets"].fill == "#66BB6A"


def test_missing_or_nonpositive_target_uses_the_default() -> None:
    for t in (None, 0, -1, float("nan"), "abc"):
        assert P.target_is_default(t) and P.effective_target(t) == P.DEFAULT_TARGET  # type: ignore[arg-type]
    assert not P.target_is_default(4) and P.effective_target(4) == 4.0


def test_low_target_meets_means_meets_the_low_target() -> None:
    """A target of 4: a 4.0 is green ('meets'), 3.6 is 'near', 2.9 is 'well below' — not the absolute 'Poor' colours."""
    assert P.target_band(4.0, 4).key == "meets"
    assert P.target_band(4.4, 4).key == "exceeds"
    assert P.target_band(3.6, 4).key == "near"
    assert P.target_band(3.0, 4).key == "below"
    assert P.target_band(2.99, 4).key == "well_below"
    assert P.target_band(1.99, 4).key == "critical"
    # the very same 4.0 is only 'well below' for a target of 7
    assert P.target_band(4.0, 7).key == "well_below"


def test_exceeds_collapses_for_a_target_of_ten() -> None:
    assert P.band_thresholds(10)["exceeds"] is None
    assert P.target_band(10, 10).key == "meets"
    assert [b.key for b, _lo, _t in P.band_ranges(10)] == ["meets", "near", "below", "well_below", "critical"]


def test_rounding_is_half_up_at_two_decimals() -> None:
    assert P.target_band(6.995, 7).key == "meets"  # rounds to 7.00
    assert P.target_band(6.994, 7).key == "near"


def test_band_ranges_text_is_contiguous_and_readable() -> None:
    rows = P.band_ranges(7)
    assert [t for _b, _lo, t in rows] == ["≥ 7.7", "7.0 – 7.69", "6.3 – 6.99", "5.25 – 6.29", "3.5 – 5.24", "< 3.5"]
    assert P.band_ranges(4)[0][2] == "≥ 4.4" and P.band_ranges(4)[-1][2] == "< 2.0"


def test_absolute_bands_still_exist_for_rubric_levels() -> None:
    assert P.band_for_score(7.0).label == "Acceptable" and P.band_for_score(7.0).fill == "#9CCC65"
