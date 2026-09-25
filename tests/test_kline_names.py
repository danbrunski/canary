"""K01.1: a K-line value row names its module, never a bare DID."""

from __future__ import annotations

import pytest

from src.collection.kline.names import (
    FRAME_CHANNEL,
    KlineChannel,
    did_channel,
    is_legacy,
    mem_channel,
    parse_channel,
    ram_channel,
)


def test_channels_are_module_scoped() -> None:
    assert did_channel("ABS", 0x1303) == "ABS:1303"
    assert did_channel("28", 0x1303) == "28:1303"  # an address the profile does not name
    assert did_channel("unknown", 0x1202) == "unknown:1202"
    assert ram_channel("ECM", 0xFFFF13FE) == "ECM:RAM_FFFF13FE"
    assert mem_channel("ECM", "ECM_TARGET_EQUIV_RATIO") == "ECM:MEM_ECM_TARGET_EQUIV_RATIO"


def test_the_abs_and_ecm_0x1210_are_different_rows() -> None:
    """2026-09-24: the scan tool's ABS yaw rate and UpRev's ECM TPS2 are both
    ``22 12 10``; before K01.1 both were stored as ``DID_1210``."""
    assert did_channel("ABS", 0x1210) != did_channel("ECM", 0x1210)


@pytest.mark.parametrize("channel", ["DID_1202", "RAM_FFFF13FE", "MEM_ECM_TARGET_EQUIV_RATIO"])
def test_pre_k01_names_are_legacy_and_parse_to_nothing(channel: str) -> None:
    assert is_legacy(channel)
    assert parse_channel(channel) is None


@pytest.mark.parametrize("channel", ["ABS:1202", FRAME_CHANNEL, "ECM:MEM_X"])
def test_current_names_and_the_frame_channel_are_not_legacy(channel: str) -> None:
    assert not is_legacy(channel)


def test_parse_channel_gives_module_item_and_did() -> None:
    assert parse_channel("ABS:1303") == KlineChannel("ABS", "1303")
    assert parse_channel("ABS:1303").did == 0x1303  # type: ignore[union-attr]
    ram = parse_channel("ECM:RAM_FFFF13FE")
    assert ram is not None and ram.module == "ECM" and ram.did is None
    assert parse_channel(FRAME_CHANNEL) is None
    assert parse_channel("ABS:") is None and parse_channel(":1303") is None
