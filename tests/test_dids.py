"""Tests for the Mode 22 DID map loader (src/decode/dids.py + data/dids.yaml)."""

from __future__ import annotations

from pathlib import Path

import pytest
from pydantic import ValidationError

from src.decode.dids import (
    DidEntry,
    apply_scaling,
    decode_did_raw,
    load_did_map,
)

DIDS_PATH = Path(__file__).resolve().parent.parent / "data" / "dids.yaml"


# --- decode helpers (pure) ---


def _entry(**kw: object) -> DidEntry:
    base: dict[str, object] = {
        "did": 0x1101,
        "name": "X",
        "byte_start": 0,
        "byte_length": 1,
        "confidence": "high",
    }
    base.update(kw)
    return DidEntry.model_validate(base)


def test_decode_did_raw_big_endian() -> None:
    e = _entry(byte_length=2, byte_order="big")
    assert decode_did_raw(e, bytes.fromhex("1234")) == 0x1234


def test_decode_did_raw_offset_into_payload() -> None:
    e = _entry(byte_start=1, byte_length=1)
    assert decode_did_raw(e, bytes.fromhex("AABB")) == 0xBB


def test_decode_did_raw_short_payload_returns_none() -> None:
    assert decode_did_raw(_entry(byte_length=2), b"\x01") is None


def test_apply_scaling_with_offset() -> None:
    e = _entry(scale=1.0, offset=-40.0)
    assert apply_scaling(e, 101) == 61.0  # coolant A-40


def test_apply_scaling_none_when_unpinned() -> None:
    assert apply_scaling(_entry(scale=None), 1234) is None


def test_strict_schema_rejects_unknown_field() -> None:
    with pytest.raises(ValidationError):
        _entry(typo_field=1)


def test_did_out_of_range_rejected() -> None:
    with pytest.raises(ValidationError):
        _entry(did=0x1_0000)


# --- the real data/dids.yaml ---


def test_real_did_map_loads_and_validates() -> None:
    m = load_did_map(DIDS_PATH)
    assert m.schema_version == 1
    assert m.access.session == 0xC0
    assert m.access.ecu_request_id == 0x7E0
    assert len(m.dids) >= 10


def test_real_did_map_has_no_duplicate_dids() -> None:
    dids = [e.did for e in load_did_map(DIDS_PATH).dids]
    dupes = {d for d in dids if dids.count(d) > 1}
    assert not dupes, f"duplicate DID entries: {[hex(d) for d in dupes]}"


def test_real_did_map_entries_are_self_consistent() -> None:
    for e in load_did_map(DIDS_PATH).dids:
        # scaled entries must declare a unit other than the default sentinel
        if e.scale is not None:
            assert e.unit != "unknown", f"{e.name}: scaled but unit unknown"
        # plausible bounds, if set, must be ordered
        if e.plausible_min is not None and e.plausible_max is not None:
            assert e.plausible_min <= e.plausible_max, f"{e.name}: bounds inverted"


def test_injector_pw_is_present_and_unscaled() -> None:
    """0x1208 identification is firm but its scale constant is still
    outstanding — guard that it stays unscaled until calibrated."""
    inj = next(e for e in load_did_map(DIDS_PATH).dids if e.did == 0x1208)
    assert inj.name == "ECM_INJECTOR_PW_B1"
    assert inj.scale is None, "0x1208 scale was pinned — update the calibration evidence"
