"""R29.7: the five-rung ladder that decides whether the ECU is asked for its VIN.

It was a closure in ``dashboard/routes/control/system.py``; ``test_profile_edit``
reaches it through ``POST /control/vin/read`` with an app around it. These call
:func:`src.control.vin_refusal.vin_refusal` with a config and a reading, so each
rung is one arrangement and one assertion.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from src.collection import tuning_profile, tx_gate
from src.config import CanaryConfig
from src.control.rom_backup import VehicleReading
from src.control.vin_refusal import vin_refusal
from tests.configs import isolated_config


@pytest.fixture
def config(tmp_path: Path) -> CanaryConfig:
    return isolated_config(
        tmp_path, collection={"obd_source": "canable"}, hardware={"can": {"interface": "virtual"}}
    )


class _Reading:
    """A ``take_reading`` that records whether it was called."""

    def __init__(self, state: str) -> None:
        self.state = state
        self.calls = 0

    def __call__(self) -> VehicleReading:
        self.calls += 1
        return VehicleReading(state=self.state)


def _refusal(config: CanaryConfig, state: str = "ignition_only") -> tuple[str | None, str]:
    outcome = vin_refusal(config, _Reading(state))
    return (None, "") if outcome is None else (outcome.refusal, outcome.detail)


def test_a_key_on_idle_bus_is_not_refused(config: CanaryConfig) -> None:
    assert vin_refusal(config, _Reading("ignition_only")) is None
    assert vin_refusal(config, _Reading("engine_running")) is None


def test_the_elm_path_cannot_read_a_vin_and_says_how_to_proceed(config: CanaryConfig) -> None:
    elm = config.model_copy(
        update={"collection": config.collection.model_copy(update={"obd_source": "elm327"})}
    )
    reading = _Reading("ignition_only")
    outcome = vin_refusal(elm, reading)
    assert outcome is not None
    assert outcome.refusal == "unsupported"
    assert outcome.detail == (
        "reading the VIN needs collection.obd_source=canable (currently 'elm327'); "
        "type it in instead"
    )
    assert reading.calls == 0


def test_passive_mode_refuses_before_anything_is_sent(config: CanaryConfig) -> None:
    tx_gate.set_passive(True, source="test")
    assert _refusal(config) == (
        "passive_armed",
        "passive armed: CANary is not transmitting; disarm it to read the VIN",
    )


def test_another_tester_holding_the_ecm_is_named(config: CanaryConfig) -> None:
    tx_gate.claim_ecm_session("rom_backup")
    refusal, detail = _refusal(config)
    assert refusal == "ecm_held"
    assert detail == (
        "another tool holds the ECM (rom_backup); CANary does not transmit to it until that ends"
    )


def test_a_paused_capture_is_not_polling_so_it_does_not_send_the_request(
    config: CanaryConfig,
) -> None:
    tuning_profile.set_paused(True)
    refusal, detail = _refusal(config)
    assert refusal == "capture_paused"
    assert detail.startswith("the capture is paused:")


def test_a_replay_is_a_stored_session_not_the_vehicle(config: CanaryConfig) -> None:
    assert _refusal(config, "replay") == (
        "replay",
        "a replay is running: the vehicle on the dashboard is a stored session",
    )


@pytest.mark.parametrize(
    ("state", "why"),
    [
        ("parked", "the vehicle is parked (12 V on the port, every module asleep)"),
        ("engine_off", "the key is off (broadcast CAN only; the ECM does not answer OBD)"),
        ("no_data", "neither bus is fresh: the key is off or nothing is connected"),
        ("bench", "no vehicle on the OBD port (bench power)"),
    ],
)
def test_each_key_off_state_is_named_with_its_reason(
    config: CanaryConfig, state: str, why: str
) -> None:
    assert _refusal(config, state) == (
        "key_off",
        f"key off: {why} (vehicle_state {state}); turn the key on, or type the VIN in",
    )


def test_a_state_that_is_not_key_off_or_replay_is_let_through(config: CanaryConfig) -> None:
    for state in ("unknown", "passive"):
        assert vin_refusal(config, _Reading(state)) is None, state


@pytest.mark.parametrize("setup", ["elm", "passive", "ecm", "paused"])
def test_the_vehicle_is_not_read_when_a_cheaper_rung_already_refused(
    config: CanaryConfig, setup: str
) -> None:
    """With no live source the reading is a read of the newest session's parquet."""
    arranged = config
    if setup == "elm":
        arranged = config.model_copy(
            update={"collection": config.collection.model_copy(update={"obd_source": "elm327"})}
        )
    elif setup == "passive":
        tx_gate.set_passive(True, source="test")
    elif setup == "ecm":
        tx_gate.claim_ecm_session("rom_backup")
    else:
        tuning_profile.set_paused(True)
    reading = _Reading("ignition_only")
    assert vin_refusal(arranged, reading) is not None
    assert reading.calls == 0


def test_the_rungs_are_checked_in_order(config: CanaryConfig) -> None:
    """Passive outranks a held ECM, which outranks a pause, which outranks the state."""
    tx_gate.claim_ecm_session("rom_backup")
    tuning_profile.set_paused(True)
    assert _refusal(config, "parked")[0] == "ecm_held"
    tx_gate.set_passive(True, source="test")
    assert _refusal(config, "parked")[0] == "passive_armed"
