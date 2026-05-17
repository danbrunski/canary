"""Tests for FSM param-name extraction heuristics.

Pure-Python tests of the `looks_like_param_name` filter; PDF extraction is
exercised end-to-end by running `tools/extract_fsm_signals.py` against the
real FSM PDFs (intentionally not committed) and is sanity-checked manually.
"""

from __future__ import annotations

import pytest

from src.analysis.fsm import (
    classify_spec,
    clean_param_name,
    looks_like_param_name,
    parse_ranges,
)


@pytest.mark.parametrize(
    ("raw", "cleaned"),
    [
        # brackets are always units → strip
        ("BATTERY VOLT [V]", "BATTERY VOLT"),
        ("4WD MODE [2H/4H/4L]", "4WD MODE"),
        ("VHCL/S SEN·FR [km/h] or [mph]", "VHCL/S SEN·FR or"),
        # parens with lowercase or single-char content are units → strip
        ("OUTPUT REV (rpm)", "OUTPUT REV"),
        ("ATF PRES SW 1 (ON-OFF display)", "ATF PRES SW 1"),
        ("AC PRESS SEN (V)", "AC PRESS SEN"),
        # parens with all-uppercase multi-char content (bank/sensor) → keep
        ("A/F SEN1 (B1)", "A/F SEN1 (B1)"),
        ("HO2S2 MNTR (B2)", "HO2S2 MNTR (B2)"),
        ("INT/V TIM (B1)", "INT/V TIM (B1)"),
        # unchanged when no annotations
        ("ENG SPEED", "ENG SPEED"),
        ("MAS A/F SE-B1", "MAS A/F SE-B1"),
    ],
)
def test_clean_param_name(raw: str, cleaned: str):
    assert clean_param_name(raw) == cleaned


@pytest.mark.parametrize(
    "name",
    [
        "ENG SPEED",
        "MAS A/F SE-B1",
        "B/FUEL SCHDL",
        "A/F ALPHA-B1",
        "COOLAN TEMP/S",
        "A/F SEN1 (B1)",
        "HO2S2 MNTR (B1)",
        "VHCL SPEED SE",
        "BATTERY VOLT",
        "ACCEL SEN 1",
        "TP SEN 1-B1",
        "P/N POSI SW",
        "PW/ST SIGNAL",
        "INJ PULSE-B1",
        "VIAS S/V-1",
        "TRVL AFTER MIL",
        "I/P PULLY SPD",
        "STR ANGLE SIG",
        "FR RH IN SOL",
        "VHCL/S SEN·FR",
        "4WD MODE",
        "4L POSI SW",
        "DOOR SW-AS",
        "FR WIPER LOW",
        "OUTPUT REV",
        "TC SLIP SPEED",
        "ATF TEMP SE 1",
        # FSM raw forms with unit annotations — should pass after cleaning
        "BATTERY VOLT [V]",
        "4WD MODE [2H/4H/4L]",
        "OUTPUT REV (rpm)",
        "ATF PRES SW 1 (ON-OFF display)",
        "AC PRESS SEN (V)",
    ],
)
def test_real_param_names_accepted(name: str):
    assert looks_like_param_name(name), f"should accept {name!r}"


@pytest.mark.parametrize(
    "noise",
    [
        "MONITOR ITEM",
        "REVISION:",
        "ECU DIAGNOSIS",
        "REFERENCE",
        "REFERENCE VALUE",
        "TERMINAL",
        "CONSULT-III",
        "ECM",  # bare module name; meaningful as a header, not a param
        "ABS",
        "PREPARATION",  # all-letters one-word section label
        "DIAGNOSIS",
        "",
        "ab",  # too short
        "a sentence with lowercase words in it",
        "x" * 35,  # too long
        "(cid:122) Specification data",  # PDF artifact
        "IPDM E/R (INTELLIGENT",  # unbalanced parens — header fragment
        "ROOM)",  # unbalanced parens — header fragment
    ],
)
def test_noise_rejected(noise: str):
    assert not looks_like_param_name(noise), f"should reject {noise!r}"


# ---------------------------------------------------------------------------
# Numeric range parsing from spec text
# ---------------------------------------------------------------------------


def test_parse_simple_range_with_unit():
    ranges = parse_ranges("11 - 14V")
    assert len(ranges) == 1
    assert ranges[0]["min"] == 11.0
    assert ranges[0]["max"] == 14.0
    assert "V" in ranges[0]["unit"]


def test_parse_range_with_decimal_and_compound_unit():
    ranges = parse_ranges("2.0 - 6.0 g·m/s")
    assert len(ranges) == 1
    assert ranges[0]["min"] == 2.0
    assert ranges[0]["max"] == 6.0
    assert "g" in ranges[0]["unit"]


def test_parse_percent_range():
    ranges = parse_ranges("0% - 35%")
    assert len(ranges) == 1
    assert ranges[0]["min"] == 0.0
    assert ranges[0]["max"] == 35.0
    assert "%" in ranges[0]["unit"]


def test_parse_multiple_ranges_in_one_string():
    """FSM often gives both idle and 2,500 rpm conditions in one cell."""
    ranges = parse_ranges("Idle 2.0 - 6.0 g·m/s | 2,500 rpm 7.0 - 20.0 g·m/s")
    assert len(ranges) == 2


def test_parse_unbounded_more_than():
    ranges = parse_ranges("More than 70°C (158°F)")
    assert len(ranges) == 1
    assert ranges[0]["min"] == 70.0
    assert ranges[0]["max"] == float("inf")


def test_parse_unbounded_less_than():
    ranges = parse_ranges("Less than 4.75V")
    assert len(ranges) == 1
    assert ranges[0]["min"] == float("-inf")
    assert ranges[0]["max"] == 4.75


def test_parse_approx_value_yields_narrow_range():
    ranges = parse_ranges("Approximately 2.5V")
    assert len(ranges) == 1
    assert ranges[0]["min"] < 2.5 < ranges[0]["max"]
    assert ranges[0]["max"] - ranges[0]["min"] < 1.0  # ~10% wide


def test_parse_negative_minus_sign_unicode():
    """FSM often uses unicode '−' for negative numbers."""
    ranges = parse_ranges("−5° - 5°CA")
    assert len(ranges) == 1
    assert ranges[0]["min"] == -5.0
    assert ranges[0]["max"] == 5.0


def test_parse_no_match_returns_empty():
    assert parse_ranges("ON ↔ OFF") == []
    assert parse_ranges("See EC-79.") == []
    assert parse_ranges("") == []


# --- classify_spec: golden cases from real FSM spec_text (Slice 1) ---


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("", "empty"),
        ("   ", "empty"),
        ("2.0 - 6.0 g·m/s", "numeric_range"),
        ("11 - 14V at idle | 7 - 20 g·m/s at 2,500 rpm", "numeric_range"),
        ("Approx. 2.5V", "numeric_range"),
        ("More than 70°C", "numeric_range"),
        ("See EC-79.", "cross_ref"),
        ("See EC-79. | F", "cross_ref"),
        ("Refer to EC-123 for the standard value", "cross_ref"),
        ("LEAN ←→ RICH", "categorical"),
        ("ON ↔ OFF", "categorical"),
        ("Engine running: ON  Engine stopped: OFF", "categorical"),
        ("OPEN / CLOSE", "categorical"),
        # garbled column-split / prose with no extractable number → descriptive,
        # NOT a false "numeric" or silently-empty
        ("tion. tachometer indication. | E", "descriptive"),
        (
            "• Engine: After warming up Maintaining engine speed at 2,000 "
            "rpm Fluctuates around 2.2 V",
            "descriptive",
        ),
        ("| K | SULT-III value. CONSULT-III value | The preset ve", "descriptive"),
    ],
)
def test_classify_spec_golden(text: str, expected: str) -> None:
    assert classify_spec(text, parse_ranges(text)) == expected


def test_classify_numeric_wins_over_cross_ref() -> None:
    # If a number IS extractable, that beats a stray "See EC-NN" mention.
    assert classify_spec("0 - 0.3V (See EC-79)", parse_ranges("0 - 0.3V")) == "numeric_range"


def test_real_signals_kinds_consistent() -> None:
    """Every param in data/fsm/signals.json must carry a spec_kind, and a
    'numeric_range' kind iff it actually has expected_ranges."""
    import json
    from pathlib import Path

    sig = Path("data/fsm/signals.json")
    if not sig.exists():
        pytest.skip("data/fsm/signals.json not present")
    doc = json.loads(sig.read_text())
    for m in doc["modules"]:
        for p in m["params"]:
            assert "spec_kind" in p, f"{p['name']}: missing spec_kind"
            has_ranges = bool(p["expected_ranges"])
            assert (p["spec_kind"] == "numeric_range") == has_ranges, (
                f"{p['name']}: spec_kind={p['spec_kind']} but "
                f"expected_ranges={'present' if has_ranges else 'empty'}"
            )
