"""R05.4 — the anomaly signals are declared once, as data.

``compute_anomaly_signals`` used to restate each signal's inputs inline, and
``DERIVED_CHANNEL_INPUTS`` declared only four of them, so
``capture_profiles._channel_satisfied`` treated the unmapped rest as satisfied
and ``model_input_warnings`` could never warn for the bank-balance and
total-trim models. The table is now the single declaration; the dependency
registry, the freshness gaps and the computation all read it.
"""

from __future__ import annotations

from pathlib import Path

import numpy as np
import pandas as pd
import pytest

from src.collection.capture_profiles import CaptureProfile, model_input_warnings
from src.columns import SESSION_COLUMN, TIMESTAMP_COLUMN, gap_column
from src.config import VehicleParameters
from src.signals.derived import ANOMALY_SIGNALS, DERIVED_CHANNEL_INPUTS, compute_anomaly_signals

BEHAVIOR_MODELS = Path("data/nissan-xterra-2008/behavior_models.yaml")


def _every_input_frame(n: int = 120) -> pd.DataFrame:
    rng = np.random.default_rng(3)
    frame = pd.DataFrame(
        {
            SESSION_COLUMN: ["2026-01-01T000000"] * n,
            TIMESTAMP_COLUMN: pd.date_range("2026-01-01", periods=n, freq="1s", tz="UTC"),
        }
    )
    columns = {
        column
        for signal in ANOMALY_SIGNALS
        for group in signal.input_groups
        for column in group
        if not column.startswith(("derived:", "decoded:"))
    }
    for column in sorted(columns):
        frame[column] = rng.uniform(1.0, 100.0, n)
    return frame


def test_the_registry_is_derived_from_the_table() -> None:
    for signal in ANOMALY_SIGNALS:
        assert DERIVED_CHANNEL_INPUTS[signal.name] == signal.input_groups
    assert len({signal.name for signal in ANOMALY_SIGNALS}) == len(ANOMALY_SIGNALS)


def test_every_declared_signal_is_produced_with_its_gap_in_table_order() -> None:
    frame = _every_input_frame()
    out = compute_anomaly_signals(frame, VehicleParameters())
    produced = [c for c in out.columns if c.startswith("derived:") and "__" not in c]
    assert produced == [signal.name for signal in ANOMALY_SIGNALS]
    for signal in ANOMALY_SIGNALS:
        assert gap_column(signal.name) in out.columns


def test_a_signal_missing_an_input_is_skipped_not_nan_filled() -> None:
    frame = _every_input_frame().drop(columns=["obd:SHORT_FUEL_TRIM_2"])
    out = compute_anomaly_signals(frame, VehicleParameters())
    assert "derived:STFT_BANK_BALANCE" not in out.columns
    assert "derived:TOTAL_TRIM_B2" not in out.columns
    assert "derived:TOTAL_TRIM_B1" in out.columns


@pytest.mark.skipif(not BEHAVIOR_MODELS.exists(), reason="vehicle behavior models absent")
def test_a_profile_missing_the_stft_inputs_warns_for_the_bank_balance_models() -> None:
    polls_everything_but_stft = CaptureProfile(
        name="default",
        mode01={
            "RPM": 1.0,
            "ENGINE_LOAD": 1.0,
            "MAF": 1.0,
            "SPEED": 1.0,
            "COOLANT_TEMP": 1.0,
            "RUN_TIME": 1.0,
            "FUEL_STATUS": 1.0,
            "LONG_FUEL_TRIM_1": 1.0,
            "LONG_FUEL_TRIM_2": 1.0,
        },
        corpus_eligible=True,
    )
    warnings = {
        w["model"]: w["missing_channels"]
        for w in model_input_warnings(polls_everything_but_stft, BEHAVIOR_MODELS)
    }
    stft_models = [name for name in warnings if name.startswith("stft_bank_balance")]
    assert stft_models, warnings
    for name in stft_models:
        assert "derived:STFT_BANK_BALANCE" in warnings[name]
    # LTFT is polled, so its balance models are not starved by it.
    for name, missing in warnings.items():
        assert "derived:LTFT_BANK_BALANCE" not in missing, name


# ---------------------------------------------------------------------------
# R05.4 remainder: vehicle-specific inputs come from the vehicle, not shared code
# ---------------------------------------------------------------------------


def test_shared_derived_code_names_no_manufacturer_did() -> None:
    import inspect

    from src.signals import afr, derived

    for module in (derived, afr):
        assert "did:" not in inspect.getsource(module), module.__name__
    for signal in derived.SHARED_ANOMALY_SIGNALS:
        assert not any(c.startswith("did:") for group in signal.input_groups for c in group)


def test_the_xterra_supplies_its_did_inputs_from_its_vehicle_module() -> None:
    from src.profiles import profile_module_stem
    from src.signals.vehicle_signals import af_sensor_voltage_columns, nissan_xterra_2008

    assert nissan_xterra_2008.__name__.rsplit(".", 1)[1] == profile_module_stem(
        "nissan-xterra-2008"
    )
    by_name = {signal.name: signal.input_groups for signal in ANOMALY_SIGNALS}
    assert by_name["derived:AFR_BANK_BALANCE"] == (
        ("did:ECM_AF_SENSOR_V_B1",),
        ("did:ECM_AF_SENSOR_V_B2",),
    )
    assert by_name["derived:TPS_VOLTAGE_PER_PCT"] == (
        ("did:ECM_THROTTLE_POS_SENSOR_1",),
        ("obd:THROTTLE_POS",),
    )
    assert af_sensor_voltage_columns(1) == ("did:ECM_AF_SENSOR_V_B1",)
    # The vehicle-input signals follow the shared ones (pinned output order).
    assert [s.name for s in ANOMALY_SIGNALS][-2:] == [
        "derived:AFR_BANK_BALANCE",
        "derived:TPS_VOLTAGE_PER_PCT",
    ]


def _vehicle(name: str, **attrs: object) -> object:
    import types

    module = types.ModuleType(f"src.signals.vehicle_signals.{name}")
    for key, value in attrs.items():
        setattr(module, key, value)
    return module


def test_a_second_vehicle_adds_alternatives_not_replacements(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from src.signals import vehicle_signals

    xterra = vehicle_signals.vehicle_modules()[0]
    other = _vehicle(
        "work_truck",
        SIGNAL_INPUTS={"derived:AFR_BANK_BALANCE": (("did:WT_AF_V_1",), ("did:WT_AF_V_2",))},
        AF_SENSOR_VOLTAGE={1: "did:WT_AF_V_1"},
    )
    monkeypatch.setattr(vehicle_signals, "vehicle_modules", lambda: (xterra, other))
    known = ("derived:AFR_BANK_BALANCE", "derived:TPS_VOLTAGE_PER_PCT")
    merged = vehicle_signals.signal_inputs(known)
    assert merged["derived:TPS_VOLTAGE_PER_PCT"] == (
        ("did:ECM_THROTTLE_POS_SENSOR_1",),
        ("obd:THROTTLE_POS",),
    )
    assert merged["derived:AFR_BANK_BALANCE"] == (
        ("did:ECM_AF_SENSOR_V_B1", "did:WT_AF_V_1"),
        ("did:ECM_AF_SENSOR_V_B2", "did:WT_AF_V_2"),
    )
    assert vehicle_signals.af_sensor_voltage_columns(1) == (
        "did:ECM_AF_SENSOR_V_B1",
        "did:WT_AF_V_1",
    )

    # Its frames resolve to its own channels through the shared formula.
    from src.signals.derived import VEHICLE_INPUT_SIGNALS, DerivedSignal, SignalInputs, _series

    formulas = dict(VEHICLE_INPUT_SIGNALS)
    signal = DerivedSignal(
        "derived:AFR_BANK_BALANCE",
        merged["derived:AFR_BANK_BALANCE"],
        formulas["derived:AFR_BANK_BALANCE"],
    )
    frame = pd.DataFrame({"did:WT_AF_V_1": [2.0, 2.5], "did:WT_AF_V_2": [1.5, 2.0]})
    inputs = SignalInputs(
        frame=frame,
        vehicle=None,
        series=tuple(_series(frame, *group) for group in signal.input_groups),
        computed={},
    )
    values = signal.compute(inputs)
    assert values is not None and list(values) == [0.5, 0.5]


def test_a_vehicle_cannot_invent_a_signal_or_change_its_arity(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from src.signals import vehicle_signals

    invented = _vehicle("a", SIGNAL_INPUTS={"derived:NEW_THING": (("did:X",),)})
    monkeypatch.setattr(vehicle_signals, "vehicle_modules", lambda: (invented,))
    with pytest.raises(ValueError, match="no shared formula"):
        vehicle_signals.signal_inputs(("derived:AFR_BANK_BALANCE",))

    one = _vehicle("b", SIGNAL_INPUTS={"derived:AFR_BANK_BALANCE": (("did:A",), ("did:B",))})
    three = _vehicle("c", SIGNAL_INPUTS={"derived:AFR_BANK_BALANCE": (("x",), ("y",), ("z",))})
    monkeypatch.setattr(vehicle_signals, "vehicle_modules", lambda: (one, three))
    with pytest.raises(ValueError, match="arity"):
        vehicle_signals.signal_inputs(("derived:AFR_BANK_BALANCE",))
