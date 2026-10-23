"""The Xterra ``default`` profile polls an explicit Mode 01 list (D03.34).

``AUTO`` polled every PID the ECM answers. A map of what reads each channel
found ten that fed no detector, check, digest or display beyond the
evidence-role IF/LSTM, and the operator dropped them (2026-09-26). These
tests keep the list honest: every name is a PID the poller decodes, PID 0x01
(which also carries MIL_STATUS) is still polled, no behavior model is
starved, and the two demoted PIDs run at the slow cadence.

The core fast set plans 1.5 Hz so it *achieves* at least 1 Hz (operator,
2026-09-26), with bank 2's wide-band sensor (PID 0x28) at the bank-1 rate;
``tests/test_default_profile_core_rate.py`` measures it on the real loop.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from src.collection.capture_profiles import (
    CaptureProfile,
    CaptureProfileCatalog,
)
from src.collection.obd_registry import MODE01_REGISTRY, PollTier
from src.collection.obd_scheduler.cadence import TIER_RATE_FACTORS
from src.collection.pid_decoders import PID_DECODERS
from src.features.capture_inputs import model_input_warnings
from src.yaml_load import load_yaml

CATALOG = Path("data/nissan-xterra-2008/capture_profiles.yaml")
BEHAVIOR_MODELS = Path("data/nissan-xterra-2008/behavior_models.yaml")

#: Dropped 2026-09-26: nothing read the first three, EVAP vapor pressure never
#: changed, and the rest were read only by the evidence-role IF/LSTM
#: (broadcast 0x23D already carries pedal and throttle voltage).
DROPPED = frozenset(
    {
        "DISTANCE_W_MIL",
        "WARMUPS_SINCE_DTC_CLEAR",
        "DISTANCE_SINCE_DTC_CLEAR",
        "EVAP_VAPOR_PRESSURE",
        "ABSOLUTE_LOAD",
        "RELATIVE_THROTTLE_POS",
        "ABS_THROTTLE_POS_B",
        "ACCEL_PEDAL_POS_D",
        "ACCEL_PEDAL_POS_E",
        "COMMANDED_THROTTLE_ACTUATOR",
    }
)
DEMOTED_TO_SLOW = frozenset({"RUN_TIME", "EVAPORATIVE_PURGE"})
#: The operator's core fast set (2026-09-26) plus bank 2's wide-band sensor.
CORE = frozenset(
    {
        "RPM",
        "SPEED",
        "MAF",
        "ENGINE_LOAD",
        "THROTTLE_POS",
        "TIMING_ADVANCE",
        "O2_WR_LAMBDA_S1",
        "O2_WR_LAMBDA_S5",
        "O2_B1S2",
        "O2_B2S2",
        "CONTROL_MODULE_VOLTAGE",
    }
)
CORE_PLANNED_HZ = 1.5


@pytest.fixture(scope="module")
def default_profile() -> CaptureProfile:
    catalog = CaptureProfileCatalog.model_validate(load_yaml(CATALOG.read_text()))
    return next(p for p in catalog.profiles if p.name == "default")


def _tier(channel: str) -> PollTier:
    (definition,) = [d for d in MODE01_REGISTRY.values() if d.channel == channel]
    return definition.poll_tier


def test_the_default_profile_lists_its_pids_instead_of_auto(
    default_profile: CaptureProfile,
) -> None:
    assert "AUTO" not in default_profile.mode01
    assert len(default_profile.mode01) == 26


def test_every_listed_pid_is_one_the_poller_decodes(default_profile: CaptureProfile) -> None:
    """A name the poller cannot decode is never requested: a silent gap."""
    pollable = {name for name, _decoder in PID_DECODERS.values()}
    assert set(default_profile.mode01) <= pollable


def test_pid_01_stays_because_it_carries_mil_status(default_profile: CaptureProfile) -> None:
    """MONITOR_STATUS itself is unread, but dropping it would drop the MIL."""
    assert MODE01_REGISTRY[0x01].channel == "MONITOR_STATUS"
    assert default_profile.mode01["MONITOR_STATUS"] == 1.0


def test_the_dropped_pids_are_not_polled(default_profile: CaptureProfile) -> None:
    assert not DROPPED & set(default_profile.mode01)


def test_no_behavior_model_is_starved(default_profile: CaptureProfile) -> None:
    assert model_input_warnings(default_profile, BEHAVIOR_MODELS) == []


def test_demoted_pids_poll_at_the_slow_cadence(default_profile: CaptureProfile) -> None:
    for channel in DEMOTED_TO_SLOW:
        rate = TIER_RATE_FACTORS[_tier(channel)] * default_profile.mode01[channel]
        assert rate == pytest.approx(TIER_RATE_FACTORS["slow"]), channel


def _planned_hz(profile: CaptureProfile, channel: str) -> float:
    return TIER_RATE_FACTORS[_tier(channel)] * profile.mode01[channel]


def test_the_core_set_plans_above_one_hz(default_profile: CaptureProfile) -> None:
    """Planned at exactly 1 Hz, Mode 22 windows hold it under 1 Hz achieved."""
    assert CORE <= set(default_profile.mode01)
    for channel in CORE:
        assert _tier(channel) == "fast", channel
        assert _planned_hz(default_profile, channel) == pytest.approx(CORE_PLANNED_HZ), channel


def test_bank_2_wide_band_polls_at_the_bank_1_rate(default_profile: CaptureProfile) -> None:
    assert MODE01_REGISTRY[0x28].channel == "O2_WR_LAMBDA_S5"
    assert PID_DECODERS[0x28][0] == "O2_WR_LAMBDA_S5"
    assert _planned_hz(default_profile, "O2_WR_LAMBDA_S5") == _planned_hz(
        default_profile, "O2_WR_LAMBDA_S1"
    )


def test_everything_outside_the_core_set_keeps_its_cadence(
    default_profile: CaptureProfile,
) -> None:
    """Medium at 1/3 Hz, slow at 1/15 Hz, as before the core set was raised."""
    for channel in set(default_profile.mode01) - CORE - DEMOTED_TO_SLOW:
        assert default_profile.mode01[channel] == 1.0, channel
        assert _tier(channel) in {"medium", "slow"}, channel
