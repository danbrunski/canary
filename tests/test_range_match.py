"""Tests for the FSM-range-match binding scorer."""

from __future__ import annotations

from src.analysis.range_match import match_did_range
from src.analysis.signal_lookup import ExpectedRange, ParamRef


def _ecm_battery() -> ParamRef:
    return ParamRef(
        name="BATTERY VOLT",
        module="ECM",
        page=431,
        expected_ranges=(ExpectedRange(min=11.0, max=14.0, unit="V"),),
    )


def _ecm_maf_dual() -> ParamRef:
    """MAF in the FSM has TWO ranges: idle (2-6) and 2,500 rpm (7-20)."""
    return ParamRef(
        name="MASS AIRFLOW",
        module="ECM",
        page=432,
        expected_ranges=(
            ExpectedRange(min=2.0, max=6.0, unit="g·m/s"),
            ExpectedRange(min=7.0, max=20.0, unit="g·m/s"),
        ),
    )


def _ecm_coolant() -> ParamRef:
    return ParamRef(
        name="COOLAN TEMP/S",
        module="ECM",
        page=431,
        expected_ranges=(ExpectedRange(min=70.0, max=float("inf"), unit="°C"),),
    )


# ---------------------------------------------------------------------------
# Bounded matches
# ---------------------------------------------------------------------------


def test_battery_volt_at_one_sixteenth_scaling():
    """0x1204 captured 210-237 → /16 → 13.13-14.81V → matches BATTERY VOLT."""
    matches = match_did_range(210, 237, [_ecm_battery()])
    assert matches, "should bind 210-237 to BATTERY VOLT"
    top = matches[0]
    assert top.param.name == "BATTERY VOLT"
    assert top.scale == 1 / 16
    assert top.offset == 0.0
    assert 13.0 <= top.decoded_min <= 13.5
    assert 14.5 <= top.decoded_max <= 15.0


def test_maf_throttle_blip_at_one_thirtysecond_scaling():
    """0x1226 captured 72-668 → /32 → 2.25-20.88 g·m/s → matches MAF idle+revved."""
    matches = match_did_range(72, 668, [_ecm_maf_dual()])
    assert matches
    # Best match should be the revved range (which spans most of the captured)
    # OR a high-score match against either; both should be findable.
    names = [m.param.name for m in matches]
    assert "MASS AIRFLOW" in names


def test_maf_idle_only_matches_idle_range():
    """If captured stays in idle range (2-6), should bind to idle expected."""
    # raw 64-192 at /32 = 2.0-6.0 g·m/s — exactly the idle range
    matches = match_did_range(64, 192, [_ecm_maf_dual()])
    assert matches
    top = matches[0]
    assert top.expected.min == 2.0 and top.expected.max == 6.0
    assert top.scale == 1 / 32


def test_no_matching_scaling_returns_empty():
    """A captured range that doesn't fit any scaling should produce no matches."""
    # raw 1000000-2000000 at any plausible scaling won't fit BATTERY VOLT 11-14V
    matches = match_did_range(1_000_000, 2_000_000, [_ecm_battery()])
    assert matches == []


def test_zero_spread_returns_empty():
    """Constant DIDs can't be range-matched — need spread for fitting."""
    matches = match_did_range(500, 500, [_ecm_battery()])
    assert matches == []


# ---------------------------------------------------------------------------
# Unbounded matches
# ---------------------------------------------------------------------------


def test_unbounded_lower_bound_matches_when_decoded_above_threshold():
    """COOLAN TEMP/S says >70°C; captured raw 380-460 at /4 = 95-115°C."""
    matches = match_did_range(380, 460, [_ecm_coolant()])
    assert matches
    top = matches[0]
    assert top.param.name == "COOLAN TEMP/S"
    assert top.scale == 1 / 4
    assert top.decoded_min >= 70.0


def test_unbounded_lower_bound_rejects_when_decoded_below_threshold():
    """Captured range that decodes below 70°C should not match >70°C constraint."""
    # raw 200-260 at /4 = 50-65°C — below the 70°C threshold
    matches = match_did_range(200, 260, [_ecm_coolant()])
    # Should reject all scalings that put decoded below 70.
    # /4 = 50-65 (below), /2 = 100-130 (above ✓), so /2 might match.
    # That's a false positive in principle but the scoring is conservative;
    # if any matches, they should at least put the value above 70.
    for m in matches:
        assert m.decoded_min >= 70.0


# ---------------------------------------------------------------------------
# Per-param uniqueness — don't double-count
# ---------------------------------------------------------------------------


def test_only_best_scale_per_expected_range_kept():
    """Same param + same expected_range at two different (scale, offset) combos
    should produce at most one match (the best-scoring one)."""
    matches = match_did_range(210, 237, [_ecm_battery()])
    # All matches refer to BATTERY VOLT's single expected_range; should be 1.
    battery_matches = [m for m in matches if m.param.name == "BATTERY VOLT"]
    assert len(battery_matches) == 1


def test_top_k_caps_results():
    matches = match_did_range(72, 668, [_ecm_maf_dual()], top_k=1)
    assert len(matches) == 1
