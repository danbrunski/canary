"""Tests for the Mode 22 DID map loader (src/decode/dids.py + data/dids.yaml)."""

from __future__ import annotations

from pathlib import Path

import pytest
from pydantic import ValidationError

from src.decode.dids import (
    DidEntry,
    Evidence,
    apply_scaling,
    decode_did_raw,
    is_plausible,
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


def test_is_plausible_bounds() -> None:
    e = _entry(plausible_min=-40.0, plausible_max=130.0)
    assert is_plausible(e, 61.0) is True
    assert is_plausible(e, -50.0) is False
    assert is_plausible(e, 200.0) is False


def test_is_plausible_none_when_no_bounds() -> None:
    assert is_plausible(_entry(), 999.0) is None


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


def test_0x1208_is_air_charge_and_cannot_present_as_injector_milliseconds() -> None:
    """0x1208 is air charge per intake event, not injected fuel (2026-09-18:
    it ignores a measured 20 % enrichment and does not drop in DFCO; relabel
    approved by the operator). The name ECM_INJECTOR_PW_B1 survives only as a
    stored column / model input until a desk rename with aliases + retrain.

    Until then it must never be scaled or unit-labelled as pulse width, or a
    consumer reading the catalog would get injector milliseconds that are air.
    """
    inj = next(e for e in load_did_map(DIDS_PATH).dids if e.did == 0x1208)
    assert inj.name == "ECM_INJECTOR_PW_B1"
    assert inj.scale is None, "0x1208 was scaled — it is air charge, not a pulse width"
    assert inj.unit != "ms", "0x1208 labelled as ms — it is air charge, not a pulse width"


def test_0x1209_is_airflow_not_base_fuel_schedule() -> None:
    """0x1209 was ECM_BASE_FUEL_SCHEDULE_CANDIDATE until 2026-08-08, when four
    steady rungs against a simultaneous UpRev K-line log identified it as mass
    airflow (raw/100 g/s; idle matched 3.62 vs 3.62 exactly, and the 2026-05
    values corroborate once divided by 100).

    The BFS reading rested on "changes with cold enrichment and load", which
    airflow does too — a signature broad enough to fit either.
    """
    entry = next(e for e in load_did_map(DIDS_PATH).dids if e.did == 0x1209)
    assert entry.name == "ECM_MAF_GPS"
    assert entry.scale == 0.01
    assert entry.unit == "g/s"


def test_renaming_a_did_cannot_break_a_saved_capture_profile() -> None:
    """Capture profiles reference DIDs BY NAME and ``parse_did_token`` raises on
    an unknown one, so a rename silently breaks every saved profile still using
    the old name — including vehicle-scoped profiles on the Pi that this repo
    cannot see. Renaming means MOVING the old name into ``aliases``, and this
    test is what makes that non-optional rather than a docstring nobody reads.
    """
    from src.collection.obd_scheduler import parse_did_token

    assert parse_did_token("ECM_BASE_FUEL_SCHEDULE_CANDIDATE") == 0x1209
    assert parse_did_token("ECM_MAF_GPS") == 0x1209

    # Every alias must resolve, and must not collide with a live name.
    entries = load_did_map(DIDS_PATH).dids
    live = {e.name for e in entries}
    for e in entries:
        for alias in e.aliases:
            assert parse_did_token(alias) == e.did, f"alias {alias} does not resolve"
            assert alias not in live, f"alias {alias} shadows a current DID name"


def test_no_did_claims_to_be_a_base_fuel_schedule_provider() -> None:
    """Base Fuel Schedule is still UNLOCATED and must not be faked.

    This is the guarantee the old 0x1209 test was really protecting, hoisted off
    that one DID so renaming it cannot quietly retire the protection. UpRev's
    "B-FUEL SCHDL" moved 2.92/2.65/2.88/3.98 ms across the 2026-08-08 rungs and
    NO catalogued DID matched that shape. Until one does, nothing may present
    itself to target-map code as engineering BFS milliseconds.
    """
    for e in load_did_map(DIDS_PATH).dids:
        name = e.name.upper()
        claims_bfs = ("BASE_FUEL" in name or "BFS" in name) and "CANDIDATE" not in name
        assert not claims_bfs, (
            f"0x{e.did:04X} {e.name} presents as a Base Fuel Schedule provider, but BFS "
            "has never been matched to a DID — see the 2026-08-08 rung evidence."
        )


# --- quantitative evidence must carry its operating envelope ---


def test_a_measured_constant_cannot_be_recorded_without_its_conditions() -> None:
    """A bare number invites being quoted against data it was never measured on.

    The 0x1208 fuel constant holds at steady state in closed loop only; outside
    that envelope the same fit is not a calibration. Recording the number
    without the envelope is the shape of every retracted finding in this
    project, so the schema refuses it rather than a docstring discouraging it.
    """
    with pytest.raises(ValidationError, match="conditions"):
        Evidence(session="s", method="m", finding="f", constant=916416.0, constant_unit="counts/g")
    with pytest.raises(ValidationError, match="constant_unit"):
        Evidence(session="s", method="m", finding="f", constant=916416.0, conditions="steady state")


def test_qualitative_evidence_still_needs_no_conditions() -> None:
    """Most evidence is an identification, not a measurement — unchanged."""
    assert Evidence(session="s", method="m", finding="f").constant is None


def test_the_shipped_catalog_states_conditions_for_every_constant() -> None:
    """Whole-file guarantee, not just the entry that prompted the rule."""
    did_map = load_did_map(DIDS_PATH)
    for entry in did_map.dids:
        for evidence in entry.evidence:
            if evidence.constant is not None:
                assert evidence.conditions, f"0x{entry.did:04X} constant without conditions"
                assert evidence.constant_unit, f"0x{entry.did:04X} constant without unit"
