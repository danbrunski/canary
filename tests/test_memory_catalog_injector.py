"""The injector-chain RAM channels and DID facts catalogued from the firmware (D03.38).

Addresses, widths and scales come from the ZS44A firmware hand-off
(docs/ecu-firmware-analysis.md, "Injector pulse chain, dead time and the BFS
ceiling"). The chain is code-verified and data-checked through DIDs
0x1206/0x1207, but none of these words has been read through service 0x23, so
they are pinned exactly as handed off: a later edit that shifts an address or
a scale, or pushes a window past the ECM's 63-byte ceiling, fails here rather
than on the truck. The known answers are relations the verifier cannot express
(a formula on a Mode 01 PID, a fuel-cut-only equality), so they live in notes,
and the notes are pinned to keep carrying them.
"""

from __future__ import annotations

from pathlib import Path

import pytest
import yaml

from src.collection.can_obd_client import ObdResponse
from src.collection.can_obd_collector import _decode_response
from src.collection.obd_scheduler import _PollRequest
from src.decode.dids import (
    apply_memory_scaling,
    apply_scaling,
    decode_memory_raw,
    load_did_map,
    plan_memory_reads,
)
from src.paths import DIDS_YAML

REPO = Path(__file__).resolve().parents[1]
CATALOG = load_did_map(DIDS_YAML)
BY_NAME = {c.name: c for c in CATALOG.memory}
DIDS = {e.did: e for e in CATALOG.dids}

_10US = 0.01  # ms per count: the injector timer's 10 us unit
_Q14 = 1 / 16384
_UNITY_6400 = 1 / 25600  # 0x6400 = 1.0
_AFR = 1 / 500

#: name -> (address, byte_length, scale, unit, window). All unsigned words.
PINNED: dict[str, tuple[int, int, float, str, str]] = {
    "ECM_INJECTOR_DEAD_TIME": (0xFFFF280C, 2, _10US, "ms", "injector_dead_time"),
    "ECM_INJECTOR_BANK_PULSE_B1": (0xFFFF1B9A, 2, _10US, "ms", "injector_bank_pulse"),
    "ECM_INJECTOR_BANK_PULSE_B2": (0xFFFF1B9C, 2, _10US, "ms", "injector_bank_pulse"),
    "ECM_INJECTOR_PRESSURE_FACTOR": (0xFFFF1800, 2, _Q14, "ratio", "injector_pressure_factor"),
    "ECM_PURGE_FUEL_FACTOR_B1": (0xFFFF4698, 2, _UNITY_6400, "ratio", "purge_fuel_factor"),
    "ECM_PURGE_FUEL_FACTOR_B2": (0xFFFF469A, 2, _UNITY_6400, "ratio", "purge_fuel_factor"),
    "ECM_AF_FEEDBACK_TARGET_PREV_B1": (0xFFFF46AC, 2, _AFR, "afr", "lambda_target"),
}

#: window -> (first address, read length) when every member is selected.
WINDOWS = {
    "injector_dead_time": (0xFFFF280C, 2),
    "injector_bank_pulse": (0xFFFF1B9A, 4),
    "injector_pressure_factor": (0xFFFF1800, 2),
    "purge_fuel_factor": (0xFFFF4698, 4),
    "lambda_target": (0xFFFF46AC, 10),  # 46AC copy + 46B2/46B4 targets
}


@pytest.mark.parametrize("name", sorted(PINNED))
def test_address_width_scale_unit_and_window_are_as_handed_off(name: str) -> None:
    address, width, scale, unit, window = PINNED[name]
    channel = BY_NAME[name]
    assert channel.address == address
    assert (channel.length, channel.byte_start, channel.byte_length) == (width, 0, width)
    assert channel.signed is False
    assert channel.scale == pytest.approx(scale, rel=1e-12)
    assert channel.offset == 0.0
    assert channel.unit == unit
    assert channel.window == window
    assert channel.verified_tune_revision == "ZS44A-20260920"


def test_every_window_spans_what_was_handed_off_and_fits_the_ceiling() -> None:
    ceiling = CATALOG.access.max_memory_read_length
    assert ceiling == 63
    reads = {read.name: read for read in plan_memory_reads(CATALOG.memory)}
    for name, (address, length) in WINDOWS.items():
        assert (reads[name].address, reads[name].length) == (address, length), name
        assert reads[name].length <= ceiling


def test_selecting_only_the_live_targets_still_reads_four_bytes() -> None:
    """Widening lambda_target to the 46AC copy changes no selection's request."""
    targets = [BY_NAME["ECM_AF_FEEDBACK_TARGET_B1"], BY_NAME["ECM_AF_FEEDBACK_TARGET_B2"]]
    (read,) = plan_memory_reads(targets)
    assert (read.name, read.address, read.length) == ("lambda_target", 0xFFFF46B2, 4)


def test_new_channels_are_code_verified_carry_their_known_answers_and_no_profile() -> None:
    """Hypothesized until a key-on read passes, and only then in a profile."""
    for name in PINNED:
        channel = BY_NAME[name]
        assert channel.confidence == "hypothesized", name
        assert any(e.method == "disassembly" for e in channel.evidence), name
        # No relation the verifier can run, so the check is stated in notes.
        assert channel.known_answer is None, name
        assert channel.notes and "Known answer" in channel.notes, name
    for path in (REPO / "data").rglob("capture_profiles*.yaml"):
        for profile in yaml.safe_load(path.read_text()).get("profiles", []):
            assert not set(PINNED) & set(profile.get("memory") or ()), (path, profile["name"])


def test_the_dead_time_known_answers_are_recorded_exactly() -> None:
    notes = BY_NAME["ECM_INJECTOR_DEAD_TIME"].notes or ""
    assert "max(0, 47 + ((175 - Vb) x 92) >> 7)" in notes
    assert "PID 0x42 / 0.08" in notes
    assert "+-2 counts" in notes and "fuel cut" in notes


@pytest.mark.parametrize(
    ("name", "wire", "expected"),
    [
        ("ECM_INJECTOR_DEAD_TIME", "002f", 0.47),  # 47 counts: Ts at 14.0 V
        ("ECM_INJECTOR_BANK_PULSE_B1", "01f4", 5.0),
        ("ECM_INJECTOR_PRESSURE_FACTOR", "293f", 10559 / 16384),  # the 0x9254 cap
        ("ECM_PURGE_FUEL_FACTOR_B1", "6400", 1.0),  # purge off
        ("ECM_AF_FEEDBACK_TARGET_PREV_B1", "1cb6", 14.7),  # 7350: PID 0x44 = phi
    ],
)
def test_words_decode_to_the_firmware_units(name: str, wire: str, expected: float) -> None:
    channel = BY_NAME[name]
    raw = decode_memory_raw(channel, bytes.fromhex(wire))
    assert raw is not None
    assert apply_memory_scaling(channel, raw) == pytest.approx(expected)


def test_one_bank_pulse_reply_decodes_both_banks() -> None:
    """The collector path: one 4-byte reply, two readings on one timestamp."""
    read = next(r for r in plan_memory_reads(CATALOG.memory) if r.name == "injector_bank_pulse")
    request = _PollRequest(
        kind="memory",
        service=0x23,
        identifier=0,
        request_id=0x7E0,
        channel="mem:injector_bank_pulse",
        memory_read=read,
    )
    readings = _decode_response(
        request,
        ObdResponse(service=0x63, pid=None, payload=bytes.fromhex("010e0113"), responder_id=0x7E8),
        timestamp=3.0,
    )
    assert {r.pid: r.value for r in readings} == {
        "mem:ECM_INJECTOR_BANK_PULSE_B1": pytest.approx(2.70),
        "mem:ECM_INJECTOR_BANK_PULSE_B2": pytest.approx(2.75),
    }


@pytest.mark.parametrize(
    ("did", "name", "ram"),
    [(0x1206, "ECM_INJECTOR_PULSE_1", 0xFFFF2710), (0x1207, "ECM_INJECTOR_PULSE_2", 0xFFFF2712)],
)
def test_injector_pulse_dids_keep_their_names_and_scale_and_cite_the_can_pointer(
    did: int, name: str, ram: int
) -> None:
    """The firmware's 10 us unit matches the catalogued x0.01 ms, so nothing a
    trained model serves was rescaled; the names stay _1/_2 (a rename needs a
    migration) while the notes carry the firmware bank."""
    entry = DIDS[did]
    assert entry.name == name
    assert (entry.scale, entry.offset, entry.unit) == (0.01, 0.0, "ms")
    assert apply_scaling(entry, 47) == pytest.approx(0.47)  # a fuel-cut read is Ts
    can_code = [e for e in entry.evidence if e.method == "disassembly" and e.transport == "can"]
    assert can_code and f"0x{ram:08X}" in can_code[0].finding
    assert entry.notes and "Ts" in entry.notes


def test_pid_0x44_caveat_is_on_the_phi_word() -> None:
    notes = BY_NAME["ECM_TARGET_EQUIV_RATIO"].notes or ""
    assert "0xFFFF46AC" in notes and "3675" in notes and "7350" in notes
