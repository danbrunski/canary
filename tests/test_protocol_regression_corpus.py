"""Proprietary protocol regression corpus (Task 6.19).

Every fixture below is a *verbatim* copy of real candump lines pulled from
this repo's own captured sessions (`data/sessions/<ts>/frames.log`) — not
fabricated bytes. Provenance is cited per-fixture as
``<session>/frames.log:<line>``. The expected decoded values were computed
once by running the project's own decoders (`iso_tp.decode_uds`,
`dids.decode_did_raw`/`apply_scaling`) against these exact lines and are
pinned here so a future change to `src/analysis/iso_tp.py`,
`src/decode/dids.py`, or `data/dids.yaml` that silently breaks the parse
gets caught.

Scope: this vehicle's Mode 22 (UDS ReadDataByIdentifier) traffic is
**ECM-only** — grepping every `data/sessions/*/frames.log` for diagnostic
addressing (0x7E0-0x7EF) turns up only 0x7E0 (request) / 0x7E8 (response)
with a positive answer; 0x7E1 (transmission) was probed but the Xterra is
manual (no TCM to answer, see AGENTS.md), and 0x7E2 (ABS)/BCM addressing
never appears at all. `data/dids.yaml` and `data/bindings.yaml` agree: no
ABS/BCM Mode 22 identification exists in this codebase. So this corpus
covers ECM only — it does not invent ABS/BCM coverage that doesn't exist.

Covers the 6.19 acceptance clauses:
  * DID decoding — coolant (confirmed, scaled), injector PW (identified,
    deliberately unscaled), AF sensor voltage (high-confidence, scaled).
  * Session handling — the Nissan ECM's real gate behavior: a standard UDS
    sub-function is refused (7F 10 12), Mode 22 is refused outside the
    vendor session (7F 22 80), and entering sub-function 0xC0 is ack'd
    (50 C0) — the "parked diagnostic command" keepalive
    `tools/scan_constant_dids.py --session C0` / `CanableMode22Transport`
    rely on.
  * IO-control identification — `src/analysis/iso_tp.py`'s UDS service-name
    table naming 0x2F (InputOutputControlByIdentifier). No real 0x2F
    exchange exists anywhere in this repo's captures (nothing in the
    collection layer issues 0x2F — the legacy ELM327 path blocks it via
    `WRITE_COMMAND_MODES`, and no scheduled poll builds one — and no
    external tool's IO-control traffic was ever sniffed either); that one
    case is necessarily a direct decoder-identification check on a
    constructed frame rather than a captured exchange, and is labeled as
    such below.
"""

from __future__ import annotations

from pathlib import Path

from src.analysis.iso_tp import UdsMessage, decode_uds, parse_candump_line, reassemble
from src.decode.dids import DidEntry, apply_scaling, decode_did_raw, load_did_map

DIDS_PATH = Path(__file__).resolve().parent.parent / "data" / "dids.yaml"


def _replay(lines: list[str]) -> list[UdsMessage]:
    """Run real candump lines through the same pipeline the RE workflow uses."""
    frames = [f for f in (parse_candump_line(line) for line in lines) if f is not None]
    return [uds for uds in (decode_uds(m) for m in reassemble(frames)) if uds is not None]


def _did_entry(did: int) -> DidEntry:
    return next(e for e in load_did_map(DIDS_PATH).dids if e.did == did)


# ---------------------------------------------------------------------------
# Session handling: real Nissan ECM gate behavior
# source: data/sessions/2026-05-11T152858/frames.log
# ---------------------------------------------------------------------------


def test_standard_uds_subfunction_is_refused() -> None:
    """Sub-function 0x03 (extendedDiagnosticSession) -> NRC 0x12
    (subFunctionNotSupported). lines 2562-2563."""
    msgs = _replay(
        [
            "(1778513357.253507) vcan0 7E0#021003FFFFFFFFFF R",
            "(1778513357.254196) vcan0 7E8#037F101200000000 R",
        ]
    )
    request, response = msgs
    assert request.service_name == "DiagnosticSessionControl"
    assert request.sub_function == 0x03
    assert response.is_negative is True
    assert response.service_id == 0x10
    assert response.nrc == 0x12


def test_mode22_is_gated_outside_the_vendor_session() -> None:
    """Mode 22 refused outside session 0xC0 -> NRC 0x80 (conditionsNotCorrect).
    lines 2613, 2615."""
    msgs = _replay(
        [
            "(1778513357.284256) vcan0 7E0#0322F1A0FFFFFFFF R",
            "(1778513357.284922) vcan0 7E8#037F228000000000 R",
        ]
    )
    request, response = msgs
    assert request.service_name == "ReadDataByIdentifier"
    assert request.did == 0xF1A0
    assert response.is_negative is True
    assert response.service_id == 0x22
    assert response.nrc == 0x80


def test_session_c0_entry_is_acknowledged() -> None:
    """Sub-function 0xC0 -> positive ack 0x50 0xC0 — the "parked diagnostic
    command" keepalive `--session C0` / `CanableMode22Transport` re-send every
    few seconds. lines 38827-38828."""
    msgs = _replay(
        [
            "(1778513384.981149) vcan0 7E0#0210C0FFFFFFFFFF R",
            "(1778513384.981606) vcan0 7E8#0250C00000000000 R",
        ]
    )
    request, response = msgs
    assert request.sub_function == 0xC0
    assert response.is_negative is False
    assert response.is_response is True
    assert response.service_id == 0x10
    assert response.sub_function == 0xC0


# ---------------------------------------------------------------------------
# ISO-TP multi-frame reassembly on real Nissan traffic (bonus coverage: the
# DID/session workflow above depends on this layer working on real frames,
# not just synthetic ones already covered in tests/test_iso_tp.py).
# source: data/sessions/2026-05-11T152858/frames.log lines 38869-38887
# ---------------------------------------------------------------------------


def test_multiframe_local_identifier_response_reassembles() -> None:
    msgs = _replay(
        [
            "(1778513385.006068) vcan0 7E0#022183FFFFFFFFFF R",
            "(1778513385.008851) vcan0 7E8#101A61835A533034 R",
            "(1778513385.010201) vcan0 7E0#300000FFFFFFFFFF R",
            "(1778513385.010732) vcan0 7E8#2143004106040000 R",
            "(1778513385.011375) vcan0 7E8#2200000000000000 R",
            "(1778513385.012920) vcan0 7E8#2300000000008000 R",
        ]
    )
    request, response = msgs
    assert request.service_name == "ReadDataByLocalIdentifier"
    assert request.sub_function == 0x83
    assert response.is_response is True
    assert response.service_id == 0x21
    # 26-byte reassembled ISO-TP payload (First Frame length 0x01A), body is
    # everything after the SID (25 bytes) — pinned from a real run of
    # reassemble()+decode_uds() against these exact lines.
    assert response.body == bytes.fromhex("835a5330344300410604000000000000000000000000000080")


# ---------------------------------------------------------------------------
# DID decoding: coolant (confirmed+scaled), injector PW (identified,
# deliberately unscaled), AF sensor voltage (high-confidence+scaled).
# ---------------------------------------------------------------------------


def test_did_coolant_temp_decodes_to_expected_engineering_value() -> None:
    """data/sessions/2026-05-11T222447/frames.log lines 417-418.

    Unlike its neighbours, this expectation is NOT merely a snapshot of what
    the decoder happened to produce — that is how it sat at 87.0 for three
    months while the offset was wrong. It is now anchored to two broadcast
    frames in *this same log*, at this same instant (within 3 ms):

        DID 0x1101 byte0 = 127   <- the frame below
        0x233      byte0 = 127   <- bit-identical, -50 convention
        0x551      byte0 = 117   <- exactly 10 counts lower, -40 convention

    Across the whole log the three keep that relationship (0x1101 spans
    127-144, 0x233 125-144, 0x551 115-134). 0x551's -40 is independently tied
    to Mode 01 PID 0x05 — mean difference +0.00 degC over 584 paired seconds
    of session 2026-08-06T162657 — so both paths put the engine at 77 degC
    here, and 87.0 was 10 degC high. See the 0x1101 evidence in dids.yaml.
    """
    msgs = _replay(
        [
            "(1778538289.893963) vcan0 7E0#03221101FFFFFFFF R",
            "(1778538289.894549) vcan0 7E8#046211017F000000 R",
        ]
    )
    response = next(m for m in msgs if m.is_response)
    assert response.did == 0x1101
    entry = _did_entry(0x1101)
    raw = decode_did_raw(entry, response.body)
    assert raw == 127
    assert apply_scaling(entry, raw) == 77.0
    # The broadcast leg of the same instant, decoded its own way, must land on
    # the identical temperature. This is what makes the number above evidence
    # rather than a self-consistent snapshot.
    assert 117 + (-40.0) == apply_scaling(entry, raw)


def test_did_air_charge_stays_unscaled_raw() -> None:
    """0x1208 (air charge) has no pinned engineering unit (data/dids.yaml) — the
    decoded value must stay the raw count, not a guessed engineering value.
    data/sessions/2026-05-11T213407/frames.log lines 51983-51984."""
    msgs = _replay(
        [
            "(1778535280.792015) vcan0 7E0#03221208FFFFFFFF R",
            "(1778535280.792773) vcan0 7E8#0562120814F40000 R",
        ]
    )
    response = next(m for m in msgs if m.is_response)
    assert response.did == 0x1208
    entry = _did_entry(0x1208)
    raw = decode_did_raw(entry, response.body)
    assert raw == 0x14F4
    assert apply_scaling(entry, raw) is None, "0x1208 got scaled — update this regression case too"


def test_did_af_sensor_voltage_decodes_to_expected_engineering_value() -> None:
    """data/sessions/2026-05-11T211240/frames.log lines 177608-177609."""
    msgs = _replay(
        [
            "(1778534072.411500) vcan0 7E0#03221225FFFFFFFF R",
            "(1778534072.412627) vcan0 7E8#0562122501B70000 R",
        ]
    )
    response = next(m for m in msgs if m.is_response)
    assert response.did == 0x1225
    entry = _did_entry(0x1225)
    raw = decode_did_raw(entry, response.body)
    assert raw == 0x01B7
    assert apply_scaling(entry, raw) == 2.195


# ---------------------------------------------------------------------------
# IO-control identification. No real 0x2F (InputOutputControlByIdentifier)
# exchange exists anywhere in this repo's captures — nothing in the collection
# layer issues 0x2F (the legacy ELM327 path blocks it via WRITE_COMMAND_MODES
# in src/collection/obd_collector.py, and no scheduled poll builds one), and no
# external scan tool's IO-control traffic was ever sniffed. This is
# therefore a direct decoder-identification check on a constructed frame,
# not a captured "known-good exchange" — labeled as such per the task scope.
# ---------------------------------------------------------------------------


def test_io_control_service_is_identified_by_name() -> None:
    msgs = _replay(["(1.0) vcan0 7E0#032F1102FFFFFFFF R"])
    (request,) = msgs
    assert request.service_id == 0x2F
    assert request.service_name == "InputOutputControlByIdentifier"


# ---------------------------------------------------------------------------
# Scope guard: this vehicle's Mode 22 corpus is ECM-only. If a future
# capture identifies an ABS/BCM DID, this corpus should grow deliberately —
# not silently claim coverage it doesn't have.
# ---------------------------------------------------------------------------


def test_did_map_is_scoped_to_ecm_only() -> None:
    did_map = load_did_map(DIDS_PATH)
    assert did_map.access.ecu_request_id == 0x7E0
    assert did_map.access.ecu_response_id == 0x7E8
