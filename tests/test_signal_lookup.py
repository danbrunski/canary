"""Tests for FSM signal lookup + tag-label fuzzy matching."""

from __future__ import annotations

import json
from pathlib import Path

from src.analysis.signal_lookup import (
    ParamRef,
    fuzzy_match_tag,
    load_catalog,
    module_for_can_id,
)


def _ecm() -> list[ParamRef]:
    return [
        ParamRef(name="ENG SPEED", module="ECM", page=431),
        ParamRef(name="MASS AIRFLOW", module="ECM", page=432),
        ParamRef(name="MAS A/F SE-B1", module="ECM", page=431),
        ParamRef(name="COOLAN TEMP/S", module="ECM", page=431),
        ParamRef(name="BATTERY VOLT", module="ECM", page=431),
        ParamRef(name="TP SEN 1-B1", module="ECM", page=431),
        ParamRef(name="IGN TIMING", module="ECM", page=432),
        ParamRef(name="VHCL SPEED SE", module="ECM", page=431),
    ]


def _abs() -> list[ParamRef]:
    return [
        ParamRef(name="STR ANGLE SIG", module="ABS", page=84),
        ParamRef(name="FR LH SENSOR", module="ABS", page=84),
        ParamRef(name="FR RH SENSOR", module="ABS", page=84),
        ParamRef(name="YAW RATE SEN", module="ABS", page=84),
    ]


# ---------------------------------------------------------------------------
# CAN ID → module
# ---------------------------------------------------------------------------


def test_can_id_to_module_known():
    assert module_for_can_id(0x7E0) == "ECM"
    assert module_for_can_id(0x7E8) == "ECM"


def test_can_id_to_module_unknown_returns_none():
    assert module_for_can_id(0x123) is None
    assert module_for_can_id(0x740) is None  # Nissan body range, mapping TBD


# ---------------------------------------------------------------------------
# Catalog loading
# ---------------------------------------------------------------------------


def test_load_catalog_round_trip(tmp_path: Path) -> None:
    payload = {
        "modules": [
            {
                "module": "ECM",
                "params": [
                    {"name": "ENG SPEED", "page": 431},
                    {"name": "MASS AIRFLOW", "page": 432},
                ],
            },
            {
                "module": "ABS",
                "params": [{"name": "STR ANGLE SIG", "page": 84}],
            },
        ]
    }
    path = tmp_path / "signals.json"
    path.write_text(json.dumps(payload))
    cat = load_catalog(path)
    assert cat.modules == ["ABS", "ECM"]
    ecm = cat.for_module("ECM")
    assert [p.name for p in ecm] == ["ENG SPEED", "MASS AIRFLOW"]
    assert ecm[0].page == 431
    assert ecm[0].module == "ECM"
    assert cat.for_module("UNKNOWN") == []


# ---------------------------------------------------------------------------
# Fuzzy matching: the actual tag → FSM-name bindings from our session
# ---------------------------------------------------------------------------


def test_fuzzy_match_maf_picks_mass_airflow():
    matches = fuzzy_match_tag("ecm_livedata_maf", _ecm())
    assert matches, "expected at least one match for 'ecm_livedata_maf'"
    assert matches[0].name == "MASS AIRFLOW"
    # MAS A/F SE-B1 should also score (shares 'mas' / 'a/f' tokens) but be ranked lower
    names = [m.name for m in matches]
    assert "MAS A/F SE-B1" in names


def test_fuzzy_match_rpm_picks_eng_speed():
    matches = fuzzy_match_tag("ecm_livedata_rpm", _ecm())
    assert matches[0].name == "ENG SPEED"


def test_fuzzy_match_coolant_picks_coolan_temp():
    matches = fuzzy_match_tag("ecm_livedata_coolant", _ecm())
    assert matches[0].name == "COOLAN TEMP/S"


def test_fuzzy_match_throttle_picks_tp_sen():
    matches = fuzzy_match_tag("ecm_livedata_throttle", _ecm())
    assert matches[0].name == "TP SEN 1-B1"


def test_fuzzy_match_steering_picks_str_angle():
    matches = fuzzy_match_tag("abs_livedata_steeringangle", _abs())
    assert matches[0].name == "STR ANGLE SIG"


def test_fuzzy_match_no_overlap_returns_empty():
    matches = fuzzy_match_tag("idle_baseline_start", _ecm())
    assert matches == []


def test_fuzzy_match_strips_workflow_noise_tokens():
    """`select_module_ecm` is pure workflow: nothing should match."""
    matches = fuzzy_match_tag("select_module_ecm", _ecm())
    assert matches == []


def test_fuzzy_match_top_k_caps_results():
    matches = fuzzy_match_tag("ecm_livedata_maf", _ecm(), top_k=1)
    assert len(matches) == 1


def test_fuzzy_match_specificity_weighted():
    """A name covered entirely by matched tokens scores higher than a longer
    name with one matching token."""
    candidates = [
        ParamRef(name="ENG SPEED", module="ECM", page=1),
        ParamRef(name="ENG SPEED COMPENSATION FACTOR", module="ECM", page=1),
    ]
    matches = fuzzy_match_tag("ecm_rpm", candidates)
    assert matches[0].name == "ENG SPEED", "shorter name should win on specificity"


def test_load_real_catalog_has_known_modules():
    """Smoke test against the committed signals.json — guards against
    accidental schema drift."""
    cat = load_catalog(Path("data/fsm/signals.json"))
    expected = {"ECM", "ABS", "BCM", "TRANSFER_4WD", "DIFF_LOCK", "CLUSTER", "TCM_5AT", "IPDM_ER"}
    assert expected.issubset(set(cat.modules))
    assert len(cat.for_module("ECM")) > 50
