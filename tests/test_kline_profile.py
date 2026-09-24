"""The Xterra's K-line facts load, and a profile without them says so."""

from __future__ import annotations

from pathlib import Path

import pytest
import yaml

from src.collection.kline import protocol as kp
from src.collection.kline.profile import (
    KlineNotConfiguredError,
    KlineProfile,
    load_kline_profile,
)

XTERRA = Path("data/nissan-xterra-2008/kline.yaml")


def test_the_xterra_profile_loads_with_its_measured_facts() -> None:
    profile = load_kline_profile(XTERRA)
    assert (profile.baud, profile.ecm_address, profile.tester_address) == (10400, 0x10, 0xFC)
    assert profile.wakeups[0].low_ms == pytest.approx(518.9)
    assert profile.trailer() == b"\x04\x01"


def test_the_profile_rebuilds_the_captured_frames() -> None:
    """The file is right only if it reproduces what was on the wire."""
    profile = load_kline_profile(XTERRA)
    assert kp.start_communication(profile.ecm_address, profile.tester_address) == bytes.fromhex(
        "81 10 fc 81 0e"
    )
    (answer,) = profile.known_answers
    assert kp.encode_length_frame(answer.request_bytes()) == bytes.fromhex("05 22 11 03 04 01 40")
    kp.check_request(answer.request_bytes())


def test_a_vehicle_without_a_kline_file_is_named_not_defaulted(tmp_path: Path) -> None:
    with pytest.raises(KlineNotConfiguredError, match="not configured"):
        load_kline_profile(tmp_path / "kline.yaml")


def test_vehicle_facts_have_no_defaults() -> None:
    raw = yaml.safe_load(XTERRA.read_text())
    for field in ("tester_address", "ecm_address", "wakeups", "baud", "timing"):
        broken = {k: v for k, v in raw.items() if k != field}
        with pytest.raises(ValueError, match=field):
            KlineProfile.model_validate(broken)


def test_same_ecm_and_tester_address_is_refused() -> None:
    raw = yaml.safe_load(XTERRA.read_text())
    with pytest.raises(ValueError, match="must differ"):
        KlineProfile.model_validate({**raw, "tester_address": raw["ecm_address"]})
