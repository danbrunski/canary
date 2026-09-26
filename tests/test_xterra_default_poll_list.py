"""The Xterra ``default`` profile polls an explicit Mode 01 list (D03.34).

``AUTO`` polled every PID the ECM answers. A map of what reads each channel
found ten that fed no detector, check, digest or display beyond the
evidence-role IF/LSTM, and the operator dropped them (2026-09-26). These
tests keep the list honest: every name is a PID the poller decodes, PID 0x01
(which also carries MIL_STATUS) is still polled, no behavior model is
starved, and the two demoted PIDs run at the slow cadence.
"""

from __future__ import annotations

from pathlib import Path

import pytest
import yaml

from src.collection.capture_profiles import (
    CaptureProfile,
    CaptureProfileCatalog,
    model_input_warnings,
)
from src.collection.obd_registry import MODE01_REGISTRY, PollTier
from src.collection.obd_scheduler.cadence import TIER_RATE_FACTORS
from src.collection.pid_decoders import PID_DECODERS

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


@pytest.fixture(scope="module")
def default_profile() -> CaptureProfile:
    catalog = CaptureProfileCatalog.model_validate(yaml.safe_load(CATALOG.read_text()))
    return next(p for p in catalog.profiles if p.name == "default")


def _tier(channel: str) -> PollTier:
    (definition,) = [d for d in MODE01_REGISTRY.values() if d.channel == channel]
    return definition.poll_tier


def test_the_default_profile_lists_its_pids_instead_of_auto(
    default_profile: CaptureProfile,
) -> None:
    assert "AUTO" not in default_profile.mode01
    assert len(default_profile.mode01) == 25


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
