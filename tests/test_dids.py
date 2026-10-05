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
    approved by the operator, renamed from ECM_INJECTOR_PW_B1 the same day).

    It must never be scaled or unit-labelled as pulse width, or a consumer
    reading the catalog would get injector milliseconds that are air.
    """
    inj = next(e for e in load_did_map(DIDS_PATH).dids if e.did == 0x1208)
    assert inj.name == "ECM_AIR_CHARGE"
    assert inj.scale is None, "0x1208 was scaled — it is air charge, not a pulse width"
    assert inj.unit != "ms", "0x1208 labelled as ms — it is air charge, not a pulse width"


def test_the_2026_09_18_wrong_names_are_gone_not_aliased() -> None:
    """Operator decision 2026-09-18: the mislabelled names are removed, not kept
    as aliases. Stored artifacts are migrated by tools/migrate_channel_names.py
    instead, so a surviving old name here would silently undo that decision.
    """
    from src.collection.obd_scheduler import parse_did_token

    by_did = {e.did: e for e in load_did_map(DIDS_PATH).dids}
    assert by_did[0x1206].name == "ECM_INJECTOR_PULSE_1"
    assert by_did[0x1207].name == "ECM_INJECTOR_PULSE_2"
    assert by_did[0x1134].name == "ECM_ENRICHMENT_TARGET_EQUIV_RATIO_CANDIDATE"
    for old in (
        "ECM_INJECTOR_PW_B1",
        "ECM_INTAKE_PRESSURE_1206",
        "ECM_INTAKE_PRESSURE_1207",
        "ECM_TARGET_EQUIV_RATIO_CANDIDATE",
    ):
        with pytest.raises(ValueError):
            parse_did_token(old)


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
    cannot see. A rename either MOVES the old name into ``aliases`` (checked
    here) or, when the old name must not survive, ships a map for
    tools/migrate_channel_names.py (checked in test_migrate_channel_names).
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
    """No DID *name* presents itself as engineering BFS milliseconds.

    Hoisted off 0x1209 so a rename cannot quietly retire it. BFS is located
    now: 0x1208 (``ECM_AIR_CHARGE``), accepted 2026-10-02 (P02.14) on a
    405/405 same-reply identity with UpRev's display. It keeps its name and
    raw counts because the deployed models carry it as a feature, and the ms
    reading reaches target-map code only as ``derived:BASE_FUEL_SCHEDULE``,
    whose scale is that one evidence record (``tests/test_base_fuel_schedule.py``).
    A DID named for BFS would be a second, unchecked path to the same claim.
    """
    for e in load_did_map(DIDS_PATH).dids:
        name = e.name.upper()
        claims_bfs = ("BASE_FUEL" in name or "BFS" in name) and "CANDIDATE" not in name
        assert not claims_bfs, (
            f"0x{e.did:04X} {e.name} presents as a Base Fuel Schedule provider; BFS in ms "
            "is derived:BASE_FUEL_SCHEDULE, the evidence-scaled view of 0x1208 (P02.14)."
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


# --- the catalog is CAN's; a K-line observation cannot name a CAN DID ---


def test_kline_evidence_alone_cannot_establish_a_can_did() -> None:
    """2026-10-04: 0x1102 was catalogued as long-term trim B1 from UpRev's K-line
    stream; on CAN it is road speed, because the ECM keeps separate K-line and
    CAN DID tables. A K-line finding may corroborate or refute, never stand alone.
    """
    kline = {"session": "s", "method": "uprev_stream", "finding": "f", "transport": "kline"}
    can = {"session": "s", "method": "firmware", "finding": "f", "transport": "can"}
    with pytest.raises(ValidationError, match="K-line and CAN DID tables differ"):
        _entry(evidence=[kline])
    assert _entry(evidence=[kline, can]).evidence[1].transport == "can"
    assert _entry(evidence=[can], evidence_against=[kline]).evidence_against[0].transport == "kline"
    # Legacy evidence (no transport recorded) is not judged.
    assert _entry(evidence=[{"session": "s", "method": "m", "finding": "f"}]).evidence


def test_the_2026_10_04_can_corrections_hold() -> None:
    """0x1102 is road speed, 0x1123/0x1124 short-term and 0x1125/0x1126 long-term
    trim, banks 1/2, on CAN (docs/ecu-firmware-analysis.md, firmware pointer
    tables). Exact decode parameters are pinned in the ground-truth suite."""
    by_did = {e.did: e for e in load_did_map(DIDS_PATH).dids}
    assert by_did[0x1102].name == "ECM_VEHICLE_SPEED_COARSE"
    assert by_did[0x1102].unit == "km/h"
    assert [by_did[d].name for d in (0x1123, 0x1124, 0x1125, 0x1126)] == [
        "ECM_SHORT_FUEL_TRIM_B1",
        "ECM_SHORT_FUEL_TRIM_B2",
        "ECM_LONG_FUEL_TRIM_B1",
        "ECM_LONG_FUEL_TRIM_B2",
    ]
    for did in (0x1102, 0x1123, 0x1124, 0x1125, 0x1126):
        assert "can" in {item.transport for item in by_did[did].evidence}, f"0x{did:04X}"
        assert "kline" not in {item.transport for item in by_did[did].evidence}, f"0x{did:04X}"


def test_0x1110_is_commanded_evap_purge_not_vvt_duty() -> None:
    """2026-10-04: the CAN pointer table sends 0x1110 to 0xFFFF272E, the byte
    the PID 0x2E (EVAP purge) handler reads, so it is commanded purge at 0.5 %
    per count. It was catalogued as intake VVT solenoid duty from a zero at
    idle that purge shows too. The exact decode is pinned against the PID
    handler's arithmetic in the ground-truth suite."""
    from src.collection.obd_scheduler import parse_did_token

    entry = {e.did: e for e in load_did_map(DIDS_PATH).dids}[0x1110]
    assert entry.name == "ECM_EVAP_PURGE_CMD"
    assert entry.scale == 0.5
    assert entry.unit == "percent"
    assert "can" in {item.transport for item in entry.evidence}
    # The refuted reading stays on record, tagged with the bus it came from.
    assert any("VVT" in item.finding for item in entry.evidence_against)
    assert all(item.transport for item in entry.evidence_against)
    # Saved profiles and the Pi's obd_dids still name the old one; it must
    # resolve to this DID rather than stop the daemon at start.
    assert parse_did_token("ECM_INTAKE_VVT_SOLENOID_DUTY") == 0x1110
