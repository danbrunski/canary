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
    assert by_did[0x1134].name == "ECM_ENRICHMENT_PHI_REQUEST"
    for old in (
        "ECM_INJECTOR_PW_B1",
        "ECM_INTAKE_PRESSURE_1206",
        "ECM_INTAKE_PRESSURE_1207",
        "ECM_TARGET_EQUIV_RATIO_CANDIDATE",
    ):
        with pytest.raises(ValueError):
            parse_did_token(old)


def test_the_2026_10_08_enrichment_phi_rename_holds() -> None:
    """Operator decision 2026-10-08 (D03.40): once the firmware fixed 0x1134 as
    the Target-AFR-map enrichment request (high byte of Q14 0xFFFF26C0, phi =
    raw / 64), its stale ``_CANDIDATE`` name was replaced with no alias. Stored
    artifacts migrate via tools/channel_renames/2026-10-08-enrichment-phi.yaml.
    """
    from src.collection.obd_scheduler import parse_did_token

    assert parse_did_token("ECM_ENRICHMENT_PHI_REQUEST") == 0x1134
    with pytest.raises(ValueError):
        parse_did_token("ECM_ENRICHMENT_TARGET_EQUIV_RATIO_CANDIDATE")


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
    stream; it is road speed. The 2026-10-05 firmware audit showed the ECM's
    K-line table sends 0x1102 to the same speed byte, so the error was trusting
    a third-party tool's label, not a transport difference, and the refusal says
    so. A K-line finding may corroborate or refute, never stand alone.
    """
    kline = {"session": "s", "method": "uprev_stream", "finding": "f", "transport": "kline"}
    can = {"session": "s", "method": "firmware", "finding": "f", "transport": "can"}
    with pytest.raises(ValidationError, match="third-party tool's labels") as refused:
        _entry(evidence=[kline])
    # The refusal must not repeat the disproved "the tables differ" claim.
    assert "differ" not in str(refused.value)
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


def test_the_2026_10_07_did_audit_corrections_hold() -> None:
    """Firmware DID audit (2026-10-05, applied 2026-10-07, D03.40).

    0x1107 is the final timing byte (the 0x110A / PID 0x0E byte, 110 - raw),
    not A/F heater duty; the heater duties are 0x114E/0x114F at 0.4 %. 0x123F
    is a monitor run counter, not intake air temperature; IAT is 0x1106 at
    raw - 50. 0x1117 is the PID 0x04 byte at 100/255 %. Exact decodes and RAM
    bytes are pinned in the ground-truth suite (`did_pins`). The wrong names
    are refused, not aliased (tools/channel_renames/2026-10-07-did-audit.yaml).
    """
    from src.collection.obd_scheduler import parse_did_token

    by_did = {e.did: e for e in load_did_map(DIDS_PATH).dids}
    timing = by_did[0x1107]
    assert timing.name == "ECM_IGNITION_TIMING_1107"
    assert (timing.scale, timing.offset, timing.unit) == (-1.0, 110.0, "degBTDC")
    assert apply_scaling(timing, 93) == pytest.approx(17.0)  # the warm-idle raw
    assert any("HEATER" in item.finding.upper() for item in timing.evidence_against)
    for did, bank in ((0x114E, 1), (0x114F, 2)):
        heater = by_did[did]
        assert heater.name == f"ECM_AF_SENSOR_HEATER_DUTY_B{bank}"
        assert (heater.scale, heater.unit) == (0.4, "percent")

    counter = by_did[0x123F]
    assert counter.name == "ECM_MONITOR_RUN_COUNTER_123F"
    assert counter.unit == "count" and "TEMP" not in counter.name
    assert (counter.scale, counter.offset) == (1.0, 0.0)
    iat = [e for e in by_did.values() if "INTAKE_AIR_TEMP" in e.name]
    assert [e.did for e in iat] == [0x1106], "only 0x1106 may present as intake air temperature"
    assert (iat[0].scale, iat[0].offset, iat[0].unit) == (1.0, -50.0, "degC")

    load = by_did[0x1117]
    assert load.scale == pytest.approx(100 / 255)
    assert apply_scaling(load, 255) == pytest.approx(100.0)

    enrichment = by_did[0x1134]
    assert (enrichment.scale, enrichment.unit) == (1 / 64, "phi")
    assert any("0xFFFF26C0" in item.finding for item in enrichment.evidence)

    for name in ("ECM_IGNITION_TIMING_1107", "ECM_MONITOR_RUN_COUNTER_123F"):
        assert parse_did_token(name) in (0x1107, 0x123F)
    for old in ("ECM_AF_SENSOR_HEATER_DUTY", "ECM_INTAKE_AIR_TEMP"):
        with pytest.raises(ValueError):
            parse_did_token(old)
    # Every corrected or added entry rests on CAN (firmware) evidence.
    for did in (0x1106, 0x1107, 0x110A, 0x1117, 0x1134, 0x123F, 0x114E, 0x114F):
        assert "can" in {item.transport for item in by_did[did].evidence}, f"0x{did:04X}"


def test_signed_audit_dids_are_not_given_an_unsigned_scale() -> None:
    """The DID schema decodes unsigned. A signed word given a scale would turn a
    small negative value into a huge positive one (0x122C raw 65482 is -54), so
    the s16 DIDs the audit found stay raw, and 0x1249/0x124A are not duplicated:
    mem:ECM_AF_LEARNED_OFFSET_B1/B2 read the same words and decode them signed.
    """
    by_did = {e.did: e for e in load_did_map(DIDS_PATH).dids}
    assert by_did[0x122C].scale is None
    assert 0x1249 not in by_did and 0x124A not in by_did
    memory = {m.name: m for m in load_did_map(DIDS_PATH).memory}
    for bank, did in ((1, "0x1249"), (2, "0x124A")):
        channel = memory[f"ECM_AF_LEARNED_OFFSET_B{bank}"]
        assert channel.signed
        assert any(did in item.finding for item in channel.evidence)


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
    # No alias (2026-09-18 policy): the wrong names are migrated, not kept,
    # so a config still naming them fails loudly at daemon start instead of
    # silently polling a DID under a meaning it does not have.
    with pytest.raises(ValueError):
        parse_did_token("ECM_INTAKE_VVT_SOLENOID_DUTY")
    with pytest.raises(ValueError):
        parse_did_token("ECM_INTAKE_VVT_SOLENOID_DUTY_CANDIDATE_B2")
    # 0x1139 was ECM_UNIDENTIFIED_1139 for a day; it is the bank-2 VTC
    # solenoid duty (D03.37, test below).
    assert parse_did_token("ECM_INTAKE_VTC_SOLENOID_DUTY_B2") == 0x1139


#: D03.37 (firmware VTC pass 2026-10-04): bank -> (measured angle, solenoid
#: duty, the DTCs that controller index reports). Index 0 is bank 1.
_VTC_BANKS = {
    1: (0x1135, 0x1138, ("P0011", "P0075")),
    2: (0x1163, 0x1139, ("P0021", "P0081")),
}


def test_intake_vtc_dids_carry_the_firmware_banks_and_duty_scale() -> None:
    """2026-10-05: the ECM's DTC item table puts 0x1135 (angle) and 0x1138
    (duty) on the controller that reports P0011/P0075, bank 1, and 0x1163 /
    0x1139 on P0021/P0081, bank 2. The catalog had called 0x1135 "probably
    bank 2" from UpRev parked snapshots; a swap back fails here. Timer channel
    7 makes the duty raw x 0.39 %. Exact decode and RAM addresses are pinned
    in the ground-truth suite (`did_bank_pins`)."""
    from src.collection.obd_scheduler import parse_did_token

    by_did = {e.did: e for e in load_did_map(DIDS_PATH).dids}
    for bank, (angle_did, duty_did, (perf_dtc, circuit_dtc)) in _VTC_BANKS.items():
        other_perf, other_circuit = _VTC_BANKS[3 - bank][2]
        angle, duty = by_did[angle_did], by_did[duty_did]
        assert angle.name == f"ECM_INTAKE_CAM_ANGLE_B{bank}"
        assert (angle.scale, angle.offset, angle.unit) == (0.5, -64.0, "degCA")
        assert duty.name == f"ECM_INTAKE_VTC_SOLENOID_DUTY_B{bank}"
        assert (duty.scale, duty.offset or 0.0, duty.unit) == (0.39, 0.0, "percent")
        assert duty.confidence == "high"
        assert apply_scaling(duty, 255) == pytest.approx(99.45)
        # The scale is a conditioned constant, not a bare number.
        constants = [item for item in duty.evidence if item.constant is not None]
        assert [item.constant for item in constants] == [0.39]
        assert constants[0].conditions and constants[0].transport == "can"
        # The bank rests on CAN firmware evidence naming that bank's DTC, and
        # never the other bank's: renaming without moving the case fails.
        angle_can = " ".join(i.finding for i in angle.evidence if i.transport == "can")
        duty_can = " ".join(i.finding for i in duty.evidence if i.transport == "can")
        assert perf_dtc in angle_can and other_perf not in angle_can
        assert circuit_dtc in duty_can and other_circuit not in duty_can
        assert parse_did_token(angle.name) == angle_did
        assert parse_did_token(duty.name) == duty_did
    assert by_did[0x113A].name == "ECM_INTAKE_CAM_ANGLE_COMMANDED"  # one target, both banks
    # No alias (2026-09-18 policy): the old names were migrated
    # (tools/channel_renames/2026-10-05-vtc.yaml), so a config still naming
    # one fails at daemon start instead of polling a bank it does not mean.
    for old in (
        "ECM_INTAKE_CAM_ANGLE_MEASURED",
        "ECM_INTAKE_CAM_ANGLE_MEASURED_B",
        "ECM_UNIDENTIFIED_1139",
    ):
        with pytest.raises(ValueError):
            parse_did_token(old)
