"""B05.1 / B05.2 — trim-recipe fuel-state semantics and the input inventory.

The total-trim recipes persist their fuel-state, mode and steady-state rules
on the serving contract, so the rows a fit learns from and the rows the
daemon grades are chosen by one evaluator. The inventory counts what a
recorded session can offer each recipe *through that contract*, keeping a
missing bank distinct from a zero bank difference.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import numpy as np
import pandas as pd
import pyarrow as pa
import pyarrow.parquet as pq
import pytest

from src.analysis.behavior_input_inventory import (
    fuel_system_state_agreement,
    recipe_input_inventory,
    summarize,
    trim_bank_facts,
    trim_unit_checks,
    with_contract_context,
)
from src.analysis.refit import (
    TOTAL_TRIM_STEADY_RATES,
    TOTAL_TRIM_UNIT,
    default_recipes,
    recipe_serving_spec,
)
from src.columns import MODE_COLUMN, SESSION_COLUMN, TIMESTAMP_COLUMN
from src.decode.behavior_eligibility import point_eligibility
from src.decode.behavior_models import CLOSED_LOOP_FUEL_STATES

CATALOG = {recipe.name: recipe for recipe in default_recipes("nissan-xterra-2008")}
TOTAL_TRIM = ("total_trim_vs_rpm_load_b1", "total_trim_vs_rpm_load_b2")


@pytest.mark.parametrize("name", TOTAL_TRIM)
def test_total_trim_contract_persists_fuel_state_mode_and_steady_state(name: str) -> None:
    recipe = CATALOG[name]
    spec = recipe_serving_spec(recipe)
    assert spec.eligibility is not None
    fuel = spec.eligibility.fuel_control
    assert fuel is not None and tuple(fuel.allowed_states) == CLOSED_LOOP_FUEL_STATES
    assert spec.eligibility.excluded_operating_modes == ["warmup"]
    assert {c.channel: c.max_abs_per_second for c in spec.conditions} == {
        f"{channel}__roc": limit for channel, limit in TOTAL_TRIM_STEADY_RATES.items()
    }
    # An additive percentage-point index, labelled as one.
    assert recipe.output_unit == TOTAL_TRIM_UNIT
    assert "additive" in recipe.output_unit


def _steady(**overrides: Any) -> dict[str, Any]:
    point: dict[str, Any] = {
        MODE_COLUMN: "city",
        "obd:RPM": 1800.0,
        "obd:ENGINE_LOAD": 35.0,
        "obd:FUEL_STATUS": 2.0,
        "obd:RPM__roc": 5.0,
        "obd:ENGINE_LOAD__roc": 0.2,
    }
    point.update(overrides)
    return point


@pytest.mark.parametrize(
    ("overrides", "reason"),
    [
        ({}, None),
        ({MODE_COLUMN: "warmup"}, "operating_mode_excluded:warmup"),
        ({"obd:FUEL_STATUS": 1.0}, "fuel_status_excluded:open_loop_insufficient_temperature"),
        ({"obd:FUEL_STATUS": 4.0}, "fuel_status_excluded:open_loop_load_or_fuel_cut"),
        ({"obd:FUEL_STATUS": 16.0}, "fuel_status_excluded:closed_loop_feedback_fault"),
        ({"obd:RPM__roc": 400.0}, "rate_exceeded:obd:RPM"),
        ({"obd:ENGINE_LOAD__roc": -12.0}, "rate_exceeded:obd:ENGINE_LOAD"),
    ],
)
def test_total_trim_rows_outside_steady_closed_loop_are_refused(
    overrides: dict[str, Any], reason: str | None
) -> None:
    spec = recipe_serving_spec(CATALOG["total_trim_vs_rpm_load_b1"])
    assert point_eligibility(_steady(**overrides), spec) == reason


def test_unlabelled_mode_is_refused_not_assumed_warm() -> None:
    spec = recipe_serving_spec(CATALOG["total_trim_vs_rpm_load_b1"])
    point = _steady()
    del point[MODE_COLUMN]
    assert point_eligibility(point, spec) == "operating_mode_unknown"


def _frame(n: int = 40) -> pd.DataFrame:
    stamps = pd.date_range("2026-09-21T22:00:00Z", periods=n, freq="1s")
    frame = pd.DataFrame(
        {
            TIMESTAMP_COLUMN: stamps,
            SESSION_COLUMN: "2026-09-21T220000",
            MODE_COLUMN: ["warmup"] * 10 + ["city"] * (n - 10),
            "obd:RPM": 1800.0,
            "obd:ENGINE_LOAD": 35.0,
            "obd:FUEL_STATUS": 2.0,
            "obd:SHORT_FUEL_TRIM_1": 1.5,
            "obd:LONG_FUEL_TRIM_1": 3.1,
            # Bank 2 never answered: missing coverage, not a zero difference.
            "obd:SHORT_FUEL_TRIM_2": np.nan,
            "obd:LONG_FUEL_TRIM_2": np.nan,
            "derived:TOTAL_TRIM_B1": 4.6,
        }
    )
    return frame


def test_inventory_counts_contract_eligible_rows_and_names_refusals() -> None:
    recipes = [CATALOG[name] for name in TOTAL_TRIM]
    frame = with_contract_context(_frame(), recipes)
    b1 = recipe_input_inventory(frame, CATALOG["total_trim_vs_rpm_load_b1"])
    b2 = recipe_input_inventory(frame, CATALOG["total_trim_vs_rpm_load_b2"])
    # Warmup rows and the rows before the rate horizon fills are refused by name.
    assert b1.refusals.get("operating_mode_excluded") == 10
    assert 0 < b1.eligible < 30
    assert b1.eligible + sum(b1.refusals.values()) == 40
    assert b2.eligible == 0
    assert "derived:TOTAL_TRIM_B2" in b2.absent


def test_missing_bank_is_not_a_zero_bank_difference() -> None:
    facts = trim_bank_facts(_frame())
    assert facts["paired_b1"] == 40
    assert facts["paired_b2"] == 0
    assert facts["paired_both"] == 0
    assert facts["missing_bank"] == 40
    assert facts["stft_bank_difference_exactly_zero"] == 0
    assert facts["closed_loop_both_banks_paired"] == 0
    assert facts["fuel_status_states"] == {"closed_loop": 40}


def test_fuel_system_states_are_compared_sample_by_sample(tmp_path: Path) -> None:
    session = tmp_path / "2026-09-21T220000"
    session.mkdir()
    payloads = [b"\x02\x02"] * 5 + [b"\x04\x02"] * 2 + [b"\x02\x04"] + [None]
    table = pa.table(
        {
            "timestamp": pa.array(
                pd.date_range("2026-09-21T22:00:00Z", periods=len(payloads), freq="1s"),
                type=pa.timestamp("us", tz="UTC"),
            ),
            "source": ["obd"] * len(payloads),
            "channel": ["FUEL_STATUS"] * len(payloads),
            "value_number": [2.0] * len(payloads),
            "payload": pa.array(payloads, type=pa.binary()),
        }
    )
    pq.write_table(table, session / "telemetry-000001.parquet")
    result = fuel_system_state_agreement(session)
    assert result["samples"] == 9
    assert result["with_payload"] == 8
    assert result["systems_agree"] == 5
    assert result["pairs"]["open_loop_load_or_fuel_cut/closed_loop"] == 2
    assert result["pairs"]["closed_loop/open_loop_load_or_fuel_cut"] == 1


def test_summary_counts_independent_drives_not_sessions() -> None:
    entry = {
        "absent_channels": [],
        "eligible": 20,
    }
    sessions = [
        {"drive_id": "d1", "recipes": {"r": entry}},
        {"drive_id": "d1", "recipes": {"r": entry}},  # same outing, second directory
        {"drive_id": "d2", "recipes": {"r": {**entry, "eligible": 5}}},
    ]
    out = summarize(sessions, min_rows_per_drive={"r": 30})
    assert out["r"]["eligible"] == 45
    assert out["r"]["qualifying_drives"] == ["d1"]
    assert out["r"]["drives_with_eligible_rows"] == 2


def test_trim_units_separate_quantization_from_an_all_zero_channel() -> None:
    frame = _frame().assign(
        **{
            # On the 100/128 lattice, as every real trim reading is.
            "obd:SHORT_FUEL_TRIM_1": 1.5625,
            # Relearned-from-zero long trims are a state, not trim content.
            "obd:LONG_FUEL_TRIM_1": 0.0,
            # Not a trim PID at all: off-lattice values.
            "obd:SHORT_FUEL_TRIM_2": 1.3,
        }
    )
    checks = trim_unit_checks(frame)
    assert checks["obd:SHORT_FUEL_TRIM_1"]["on_lsb_lattice"] == 1.0
    assert checks["obd:SHORT_FUEL_TRIM_1"]["exactly_zero"] == 0.0
    assert checks["obd:LONG_FUEL_TRIM_1"]["exactly_zero"] == 1.0
    assert checks["obd:SHORT_FUEL_TRIM_2"]["on_lsb_lattice"] == 0.0
    assert checks["obd:LONG_FUEL_TRIM_2"] == {"rows": 0, "recorded": False}
