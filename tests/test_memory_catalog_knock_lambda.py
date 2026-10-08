"""The knock and lambda-chain RAM channels catalogued from the firmware (D03.38).

Addresses, widths, signedness and scales come from the ZS44A firmware hand-off
(docs/ecu-firmware-analysis.md, "Knock monitoring" and "A/F sensor and lambda
chain"). They are code-verified and not yet read live, so they are pinned here
exactly as handed off: a later edit that shifts an address, drops a sign or
pushes a window past the ECM's 63-byte service-0x23 ceiling fails here rather
than on the truck, where an oversized window loses every channel in it.
"""

from __future__ import annotations

from pathlib import Path

import pytest
import yaml
from pydantic import ValidationError

from src.collection.can_obd_client import ObdResponse
from src.collection.can_obd_collector import _decode_response
from src.collection.obd_scheduler import _PollRequest
from src.decode.dids import (
    MemoryChannel,
    MemoryKnownAnswer,
    apply_memory_scaling,
    check_known_answer,
    decode_memory_raw,
    load_did_map,
    plan_memory_reads,
)
from src.paths import DIDS_YAML

REPO = Path(__file__).resolve().parents[1]
CATALOG = load_did_map(DIDS_YAML)
BY_NAME = {c.name: c for c in CATALOG.memory}

_DEG = 1 / 256
_LAMBDA = 1 / 7350
_AFR = 1 / 500

#: name -> (address, byte_length, signed, scale, window). ``scale`` None is a
#: deliberately unscaled count, flag byte or status word.
PINNED: dict[str, tuple[int, int, bool, float | None, str]] = {
    # knock_chain
    "ECM_IGNITION_TIMING_FINAL": (0xFFFF232E, 1, True, 1.0, "knock_chain"),
    "ECM_KNOCK_RETARD_APPLIED": (0xFFFF2332, 2, True, 1.0, "knock_chain"),
    "ECM_TIMING_SUBTRACT_TERM": (0xFFFF2334, 2, False, 1.0, "knock_chain"),
    "ECM_TIMING_BASE_PLUS_RETARD": (0xFFFF2338, 4, True, 1.0, "knock_chain"),
    # knock_retard
    "ECM_KNOCK_LEARN_LO_RPM": (0xFFFF124E, 2, True, _DEG, "knock_retard"),
    "ECM_KNOCK_LEARN_HI_RPM": (0xFFFF1250, 2, True, _DEG, "knock_retard"),
    "ECM_KNOCK_LEARN": (0xFFFF1252, 2, True, _DEG, "knock_retard"),
    "ECM_KNOCK_RETARD": (0xFFFF1254, 2, True, _DEG, "knock_retard"),
    # knock_target
    "ECM_KNOCK_RETARD_TARGET": (0xFFFF13FE, 2, True, _DEG, "knock_target"),
    "ECM_KNOCK_HIGHDET_METRIC": (0xFFFF1400, 2, True, None, "knock_target"),
    # riding the existing fuel_target window
    "ECM_KNOCK_RETARD_DEG": (0xFFFF26D2, 1, True, 1.0, "fuel_target"),
    "ECM_KNOCK_RETARD_LIMIT": (0xFFFF26E6, 1, False, 1.0, "fuel_target"),
    # knock_detect
    "ECM_KNOCK_EVENT_COUNT": (0xFFFF249A, 1, False, None, "knock_detect"),
    "ECM_KNOCK_WINDOW_INDEX": (0xFFFF249B, 1, False, None, "knock_detect"),
    **{
        f"ECM_KNOCK_THRESHOLD_IDX{c}": (0xFFFF249C + 2 * (c - 1), 2, False, None, "knock_detect")
        for c in range(1, 7)
    },
    "ECM_KNOCK_FLAGS_BY_INDEX": (0xFFFF24A8, 1, False, None, "knock_detect"),
    # knock_intensity
    **{
        f"ECM_KNOCK_INTENSITY_IDX{c}": (0xFFFF2550 + 4 * (c - 1), 4, True, None, "knock_intensity")
        for c in range(1, 7)
    },
    # knock_flags and the widened flags_2841
    "ECM_KNOCK_EVENT_FLAGS": (0xFFFF28AA, 1, False, None, "knock_flags"),
    "ECM_KNOCK_CONTROL_FLAGS": (0xFFFF28AE, 1, False, None, "knock_flags"),
    "ECM_FUEL_TRANSIENT_FLAGS": (0xFFFF283C, 1, False, None, "flags_2841"),
    "ECM_HIGH_DET_FLAGS": (0xFFFF283F, 1, False, None, "flags_2841"),
    # knock fault state
    "ECM_KS_FAULT_FLAGS": (0xFFFF479D, 1, False, None, "knock_fault_flag"),
    "ECM_KS1_LEVEL": (0xFFFF2CEF, 1, False, None, "knock_fault_levels"),
    "ECM_KS2_LEVEL": (0xFFFF2CF0, 1, False, None, "knock_fault_levels"),
    # lambda chain
    "ECM_AF_LAMBDA_B1": (0xFFFF4722, 2, False, _LAMBDA, "lambda_word"),
    "ECM_AF_LAMBDA_B2": (0xFFFF4724, 2, False, _LAMBDA, "lambda_word"),
    "ECM_AF_FEEDBACK_TARGET_B1": (0xFFFF46B2, 2, False, _AFR, "lambda_target"),
    "ECM_AF_FEEDBACK_TARGET_B2": (0xFFFF46B4, 2, False, _AFR, "lambda_target"),
    "ECM_AF_LEARNED_OFFSET_B1": (0xFFFF4CA4, 2, True, _AFR, "lambda_learned_offset"),
    "ECM_AF_LEARNED_OFFSET_B2": (0xFFFF4CA6, 2, True, _AFR, "lambda_learned_offset"),
    "ECM_AF_BARO_GAIN_K": (0xFFFF36BB, 1, False, None, "lambda_baro_gain"),
}

#: Item status: three bytes per diagnostic item at 0xFFFF657C + 3 x item; the
#: channel decodes bytes 1-2 (failing-now bit 15; stored/confirmed/pending
#: bits 0/2/3).
ITEMS = {"ECM_KS_P0327_STATUS": 0xAA, "ECM_KS_P0328_STATUS": 0xAB}
ITEMS |= {"ECM_KS_P0332_STATUS": 0xAC, "ECM_KS_P0333_STATUS": 0xAD}

#: window -> (first address, read length) when every member is selected.
WINDOWS = {
    "knock_chain": (0xFFFF232E, 14),  # ends with the s32 at 0xFFFF2338
    "knock_retard": (0xFFFF124E, 8),
    "knock_target": (0xFFFF13FE, 4),
    "fuel_target": (0xFFFF26AE, 57),  # 0x26AE..0x26E6
    "knock_detect": (0xFFFF249A, 15),
    "knock_intensity": (0xFFFF2550, 24),
    "knock_flags": (0xFFFF28AA, 5),
    "flags_2841": (0xFFFF283C, 7),  # widened down to 283C; name kept
    "knock_fault_items": (0xFFFF677A, 12),
    "knock_fault_flag": (0xFFFF479D, 1),
    "knock_fault_levels": (0xFFFF2CEF, 2),
    "lambda_word": (0xFFFF4722, 4),
    # widened down to 46AC, the PID 0x44 target copy (injector pass, 2026-10-05)
    "lambda_target": (0xFFFF46AC, 10),
    "lambda_learned_offset": (0xFFFF4CA4, 4),
    "lambda_baro_gain": (0xFFFF36BB, 1),
}

NEW = set(PINNED) | set(ITEMS)


@pytest.mark.parametrize("name", sorted(PINNED))
def test_address_width_sign_scale_and_window_are_as_handed_off(name: str) -> None:
    address, width, signed, scale, window = PINNED[name]
    channel = BY_NAME[name]
    assert channel.address == address
    assert (channel.length, channel.byte_start, channel.byte_length) == (width, 0, width)
    assert channel.signed is signed
    assert channel.scale == (None if scale is None else pytest.approx(scale, rel=1e-12))
    assert channel.window == window


@pytest.mark.parametrize("name", sorted(ITEMS))
def test_item_status_reads_its_three_bytes_and_decodes_bytes_one_and_two(name: str) -> None:
    channel = BY_NAME[name]
    assert channel.address == 0xFFFF657C + 3 * ITEMS[name]
    assert (channel.length, channel.byte_start, channel.byte_length) == (3, 1, 2)
    assert channel.signed is False and channel.window == "knock_fault_items"
    # byte 1 bit 7 (failing now) -> bit 15; byte 2 bits 0/2/3 -> bits 0/2/3
    assert decode_memory_raw(channel, bytes([0xFF, 0x80, 0x0D])) == 0x800D


def test_every_window_spans_what_was_handed_off_and_fits_the_ceiling() -> None:
    ceiling = CATALOG.access.max_memory_read_length
    assert ceiling == 63
    reads = {read.name: read for read in plan_memory_reads(CATALOG.memory)}
    for name, (address, length) in WINDOWS.items():
        assert (reads[name].address, reads[name].length) == (address, length), name
    assert all(read.length <= ceiling for read in reads.values())


def test_selecting_the_old_members_still_sends_the_old_requests() -> None:
    """Widening fuel_target and flags_2841 changes no saved profile's request:
    a read is planned from the members a profile selects, and the window
    names (which the Pi's saved profiles poll under) are unchanged."""
    tuning = yaml.safe_load((REPO / "data/nissan-xterra-2008/capture_profiles.yaml").read_text())
    selected = next(p for p in tuning["profiles"] if p["name"] == "tuning")["memory"]
    reads = {r.name: r for r in plan_memory_reads([BY_NAME[n] for n in selected])}
    assert (reads["fuel_target"].address, reads["fuel_target"].length) == (0xFFFF26AE, 18)

    flags = [BY_NAME["ECM_PEDAL_ENRICH_FLAGS"], BY_NAME["ECM_KNOCK_TRIM_DIFF_FLAGS"]]
    (read,) = plan_memory_reads(flags)
    assert (read.name, read.address, read.length) == ("flags_2841", 0xFFFF2841, 2)


def test_the_2842_byte_is_named_for_the_knock_gate_and_the_old_name_is_refused() -> None:
    """0xFFFF2842 does not select the timing-trim table (0xFFFF283F bit 6
    does); its bit 2 is the knock task's lagged copy of HI DET, gating the
    trim-difference branch. Renamed 2026-10-05 with no alias (2026-09-18
    policy): the old name is gone from the catalog, and a saved profile still
    naming it is refused instead of running with the byte silently unpolled.
    The Pi's ``ram-study`` override is migrated by
    tools/channel_renames/2026-10-05-knock-trim-diff-flags.yaml."""
    from src.collection.capture_profiles import CaptureProfile
    from src.collection.obd_scheduler import profile_memory_union
    from src.decode.dids import select_memory_channels

    old = "ECM_TIMING_TRIM_SELECT_FLAGS"
    channel = BY_NAME["ECM_KNOCK_TRIM_DIFF_FLAGS"]
    assert (channel.address, channel.byte_length, channel.window) == (0xFFFF2842, 1, "flags_2841")
    assert channel.verified_tune_revision == "ZS44A-20260920"
    assert old not in BY_NAME
    assert all(old != e.name and old not in e.aliases for e in CATALOG.dids)
    # The wrong reading stays on record as evidence against, not as the role.
    assert any(old in item.finding for item in channel.evidence_against)

    with pytest.raises(ValueError, match=old):
        select_memory_channels([old])
    ram_study = CaptureProfile(
        name="ram-study",
        memory=("ECM_PEDAL_ENRICH_FLAGS", old),
        storage_prefix="_capture-ram-study",
    )
    with pytest.raises(ValueError, match=rf"'ram-study'.*{old}"):
        profile_memory_union([ram_study])


def test_new_channels_are_code_verified_and_not_yet_in_any_profile() -> None:
    """Hypothesized until a key-on read passes, and only then in a profile."""
    for name in NEW:
        channel = BY_NAME[name]
        assert channel.confidence == "hypothesized", name
        assert any(e.method == "disassembly" for e in channel.evidence), name
    for path in (REPO / "data").rglob("capture_profiles*.yaml"):
        for profile in yaml.safe_load(path.read_text()).get("profiles", []):
            assert not NEW & set(profile.get("memory") or ()), (path, profile["name"])


@pytest.mark.parametrize(
    ("name", "wire", "expected"),
    [
        ("ECM_KNOCK_RETARD", "ff00", -1.0),  # 1 degree of retard, not +255
        ("ECM_KNOCK_RETARD_TARGET", "fb00", -5.0),  # the KS-fault fallback
        ("ECM_IGNITION_TIMING_FINAL", "fb", -5.0),
        ("ECM_KNOCK_RETARD_DEG", "fd", -3.0),
        ("ECM_AF_LEARNED_OFFSET_B1", "fe0c", -1.0),  # -500 raw = -1.0 AFR
        ("ECM_AF_LAMBDA_B1", "1cb6", 1.0),  # 7350 = lambda 1
        ("ECM_AF_FEEDBACK_TARGET_B1", "1cb6", 14.7),
    ],
)
def test_signed_and_scaled_words_decode_to_the_firmware_units(
    name: str, wire: str, expected: float
) -> None:
    channel = BY_NAME[name]
    raw = decode_memory_raw(channel, bytes.fromhex(wire))
    assert raw is not None
    assert apply_memory_scaling(channel, raw) == pytest.approx(expected)


def test_one_knock_chain_reply_decodes_every_signed_field() -> None:
    """The collector path: one 14-byte reply, four readings, signs intact."""
    read = next(r for r in plan_memory_reads(CATALOG.memory) if r.name == "knock_chain")
    request = _PollRequest(
        kind="memory",
        service=0x23,
        identifier=0,
        request_id=0x7E0,
        channel="mem:knock_chain",
        memory_read=read,
    )
    # 232E = 12 deg; 2332 = -3; 2334 = 2; 2338 = 15 + -3 = 12 (s32)
    payload = bytes.fromhex("0c000000fffd000200000000000c")
    readings = _decode_response(
        request,
        ObdResponse(service=0x63, pid=None, payload=payload, responder_id=0x7E8),
        timestamp=3.0,
    )
    values = {r.pid: r.value for r in readings}
    assert values == {
        "mem:ECM_IGNITION_TIMING_FINAL": 12.0,
        "mem:ECM_KNOCK_RETARD_APPLIED": -3.0,
        "mem:ECM_TIMING_SUBTRACT_TERM": 2.0,
        "mem:ECM_TIMING_BASE_PLUS_RETARD": 12.0,
    }


def test_window_index_zero_is_a_state_not_cylinder_zero() -> None:
    channel = BY_NAME["ECM_KNOCK_WINDOW_INDEX"]
    assert apply_memory_scaling(channel, 0) is None
    assert channel.sentinel_meaning and "sync" in channel.sentinel_meaning


def test_final_timing_known_answer_uses_the_did_offset() -> None:
    """timing = 110 - DID 0x110A raw, which needs did_offset."""
    channel = BY_NAME["ECM_IGNITION_TIMING_FINAL"]
    for timing in (-7, -5, 0, 12, 50):
        word = decode_memory_raw(channel, timing.to_bytes(1, "big", signed=True))
        assert word is not None
        assert check_known_answer(channel, word, 110 - timing)
        assert not check_known_answer(channel, word, 110 - timing + 1)
    # Read unsigned, -5 deg (0xFB = 251) would fail the same relation.
    unsigned = channel.model_copy(update={"signed": False})
    word = decode_memory_raw(unsigned, (-5).to_bytes(1, "big", signed=True))
    assert word == 251 and not check_known_answer(unsigned, word, 115)


def test_did_offset_is_refused_outside_linear_did() -> None:
    with pytest.raises(ValidationError, match="did_offset applies only to linear_did"):
        MemoryKnownAnswer(did=0x110A, relation="equals_did", did_offset=110.0)


def test_a_signed_sentinel_must_be_written_in_the_signed_range() -> None:
    """0xFB00 spelled unsigned could never match an s16 slice."""
    with pytest.raises(ValidationError, match="cannot occur in a 2-byte signed slice"):
        MemoryChannel(
            name="X",
            address=0xFFFF13FE,
            length=2,
            byte_length=2,
            signed=True,
            confidence="hypothesized",
            verified_tune_revision="rev",
            sentinel_raw=0xFB00,
            sentinel_meaning="fallback",
        )
