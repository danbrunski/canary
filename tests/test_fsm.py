"""Tests for FSM param-name extraction heuristics.

Pure-Python tests of the `looks_like_param_name` filter; PDF extraction is
exercised end-to-end by running `tools/extract_fsm_signals.py` against the
real FSM PDFs (intentionally not committed) and is sanity-checked manually.
"""

from __future__ import annotations

import pytest

from src.analysis.fsm import clean_param_name, looks_like_param_name


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
    ],
)
def test_noise_rejected(noise: str):
    assert not looks_like_param_name(noise), f"should reject {noise!r}"
