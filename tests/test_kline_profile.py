"""The Xterra's K-line facts load, and a profile without them says so."""

from __future__ import annotations

from pathlib import Path
from typing import Any

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


# --------------------------------------------------------------------------
# K01.1 / K01.4 — modules, their DIDs and the bindings a known answer backs
# --------------------------------------------------------------------------


def _with_abs_did(**did: object) -> dict[str, object]:
    raw = yaml.safe_load(XTERRA.read_text())
    raw["modules"][0x28]["dids"] = [{"did": 0x1303, "width": 2, "evidence": "test", **did}]
    return raw


def test_the_modules_are_named_by_address_and_unnamed_ones_by_hex() -> None:
    profile = load_kline_profile(XTERRA)
    assert {address: m.name for address, m in profile.modules.items()} == {
        0x10: "ECM",
        0x28: "ABS",
        0x58: "SRS",
    }
    assert profile.module_label(0x28) == "ABS"
    assert profile.module_label(0x33) == "33"
    assert profile.module_label(None) == "unknown"
    assert profile.module_address("abs") == 0x28
    assert profile.module_address("33") == 0x33
    with pytest.raises(LookupError, match="TCM"):
        profile.module_address("TCM")
    abs_module = profile.modules[0x28]
    assert abs_module.key_bytes == (0x6D, 0x8F)
    assert kp.start_communication(0x28, profile.tester_address) == bytes.fromhex("81 28 fc 81 26")
    # The airbag module, as the scan tool addressed it on 2026-10-04.
    assert profile.modules[0x58].key_bytes == (0x6D, 0x8F)
    assert kp.start_communication(0x58, profile.tester_address) == bytes.fromhex("81 58 fc 81 56")


def test_the_abs_bindings_decode_their_known_answers() -> None:
    abs_module = load_kline_profile(XTERRA).modules[0x28]
    speed, _ = abs_module.resolve("ENGINE_SPEED")
    # 245 counts displayed as 750-820 rpm at idle (2026-09-24).
    assert speed.decode(b"\x00\xf5") == {"ENGINE_SPEED": pytest.approx(765.625)}
    decel, _ = abs_module.resolve("0x111A")
    assert decel.decode(b"\x7f") == {"DECEL_G": pytest.approx(-0.01)}  # displayed -0.01 G
    battery, _ = abs_module.resolve("BATTERY_VOLTAGE")
    assert battery.decode(b"\xa1")["BATTERY_VOLTAGE"] == pytest.approx(12.58, abs=0.1)
    word, asked = abs_module.resolve("stop_lamp_switch")
    assert (word.did, asked) == (0x1303, "STOP_LAMP_SWITCH")
    brake = word.decode(b"\xa2\xfe")
    assert brake["STOP_LAMP_SWITCH"] is True and brake["OFF_SWITCH"] is False
    wheel, _ = abs_module.resolve("1102")
    assert not wheel.bound and wheel.decode(b"\x00") == {}  # a hypothesis, unbound
    with pytest.raises(ValueError, match="2 byte"):
        speed.raw(b"\x00")


def test_a_binding_without_its_known_answer_is_refused() -> None:
    with pytest.raises(ValueError, match="missing \\['known_answer'\\]"):
        KlineProfile.model_validate(
            _with_abs_did(name="WORD", unit="counts", scale=1.0, status="provisional")
        )


@pytest.mark.parametrize(
    ("mask", "named"), [(0x0300, "single bit"), (0x10000, "wider than its 2-byte reply")]
)
def test_a_flag_is_one_bit_inside_its_reply(mask: int, named: str) -> None:
    flag = {"name": "F", "mask": mask, "meaning": "m", "status": "provisional", "evidence": "e"}
    with pytest.raises(ValueError, match=named):
        KlineProfile.model_validate(_with_abs_did(flags=[flag]))


def test_a_module_name_that_reads_as_a_hex_address_is_refused() -> None:
    raw = yaml.safe_load(XTERRA.read_text())
    raw["modules"][0x28]["name"] = "AB"
    with pytest.raises(ValueError, match="reads as a hex address"):
        KlineProfile.model_validate(raw)


def test_a_local_id_reader_needs_its_reply_limit() -> None:
    raw = yaml.safe_load(XTERRA.read_text())
    del raw["modules"][0x10]["reader"]["max_local_id_reply_bytes"]
    with pytest.raises(ValueError, match="max_local_id_reply_bytes"):
        KlineProfile.model_validate(raw)


def test_the_airbag_module_records_its_code_read() -> None:
    read = load_kline_profile(XTERRA).modules[0x58].read_codes
    assert read is not None
    assert [(r.request, r.status, r.status_confidence) for r in read.requests] == [
        ((0xA2, 0x00), "current", "inferred"),
        ((0xA2, 0x01), "past", "inferred"),
    ]
    assert read.known_answer.codes == ("B1023",)
    assert "2026-10-08T172943" in read.evidence


def _srs_read() -> dict[str, Any]:
    raw: dict[str, Any] = yaml.safe_load(XTERRA.read_text())
    return raw


def test_a_code_read_whose_known_answer_does_not_decode_is_refused() -> None:
    raw = _srs_read()
    raw["modules"][0x58]["read_codes"]["known_answer"]["codes"] = ["B9023"]
    with pytest.raises(ValueError, match="decodes to \\['B1023'\\]"):
        KlineProfile.model_validate(raw)


def test_a_code_read_on_an_unknown_service_is_refused() -> None:
    raw = _srs_read()
    raw["modules"][0x58]["read_codes"]["requests"][0]["request"] = [0x18, 0x00]
    with pytest.raises(ValueError, match="reply layout is known"):
        KlineProfile.model_validate(raw)


@pytest.mark.parametrize("field", ["status_confidence", "evidence", "status"])
def test_a_code_read_request_needs_every_field(field: str) -> None:
    raw = _srs_read()
    del raw["modules"][0x58]["read_codes"]["requests"][1][field]
    with pytest.raises(ValueError, match=field):
        KlineProfile.model_validate(raw)


def test_two_code_reads_with_one_status_are_refused() -> None:
    raw = _srs_read()
    raw["modules"][0x58]["read_codes"]["requests"][0]["status"] = "past"
    with pytest.raises(ValueError, match="same status"):
        KlineProfile.model_validate(raw)


# --------------------------------------------------------------------------
# Per-module inter-byte gap (ISO P4): the airbag module, 2026-10-10
# --------------------------------------------------------------------------


def test_the_airbag_module_declares_the_scan_tools_fifteen_millisecond_gap() -> None:
    """Known answer: session 2026-10-10T153117's four sniffed starts measure
    14.97-15.17 ms between bytes; the ECM and the ABS keep timing.p4_ms."""
    profile = load_kline_profile(XTERRA)
    srs = profile.modules[0x58]
    char_ms = 10_000.0 / profile.baud
    gaps = [s.inter_byte_gap_ms(char_ms) for s in srs.sniffed_starts]
    assert len(gaps) == 4
    assert all(14.9 < g < 15.2 for g in gaps)
    assert profile.p4_ms(0x58) == 15.0
    assert profile.p4_ms(0x10) == profile.timing.p4_ms == 0.0
    assert profile.p4_ms(0x28) == 0.0
    assert profile.p4_ms(0x7E) == 0.0  # an address with no entry: the profile's


def test_a_spaced_sniffed_start_without_request_timing_is_refused() -> None:
    """Make it impossible to forget: evidence of a spaced tester start means
    CANary's own requests to that module must be spaced too."""
    raw = _srs_read()
    del raw["modules"][0x58]["request_timing"]
    with pytest.raises(ValueError, match=r"0x58 \(SRS\).*15\.2 ms apart.*request_timing\.p4_ms"):
        KlineProfile.model_validate(raw)


def test_a_back_to_back_sniffed_start_needs_no_request_timing() -> None:
    raw = _srs_read()
    del raw["modules"][0x58]["request_timing"]
    # UpRev's long-low start to the ECM: five bytes back to back (4 characters).
    char_ms = 10_000.0 / raw["baud"]
    raw["modules"][0x58]["sniffed_starts"] = [
        {
            "session": "test",
            "break_at": "test",
            "frame": "81 58 FC 81 56",
            "span_ms": 536.9,
            "frame_end_ms": 536.9 + 4 * char_ms,
        }
    ]
    assert KlineProfile.model_validate(raw).p4_ms(0x58) == 0.0


def test_a_declared_gap_the_sniffed_starts_disagree_with_is_refused() -> None:
    raw = _srs_read()
    raw["modules"][0x58]["request_timing"]["p4_ms"] = 10.0
    with pytest.raises(ValueError, match="p4_ms 10.0 ms, but its sniffed starts measured 15.05 ms"):
        KlineProfile.model_validate(raw)


def test_a_declared_gap_without_sniffed_starts_is_refused() -> None:
    raw = _srs_read()
    del raw["modules"][0x58]["sniffed_starts"]
    with pytest.raises(ValueError, match="needs sniffed_starts measuring it"):
        KlineProfile.model_validate(raw)


@pytest.mark.parametrize(
    ("where", "value", "match"),
    [
        ("p4_ms", 25.0, "less than or equal to 20"),
        ("evidence", "", "at least 1 character"),
    ],
)
def test_request_timing_is_bounded_by_iso_p4max_and_carries_evidence(
    where: str, value: object, match: str
) -> None:
    raw = _srs_read()
    raw["modules"][0x58]["request_timing"][where] = value
    with pytest.raises(ValueError, match=match):
        KlineProfile.model_validate(raw)


def test_a_sniffed_start_that_ends_before_it_begins_is_refused() -> None:
    raw = _srs_read()
    raw["modules"][0x58]["sniffed_starts"][0]["frame_end_ms"] = 40.0
    with pytest.raises(ValueError, match="is not after span_ms"):
        KlineProfile.model_validate(raw)
