"""The Improv WiFi wire format (src/provisioning/improv.py).

The worked example is the spec's own (improv-wifi.com/ble): SSID
``MyWirelessAP``, password ``mysecurepassword`` -> ``01 1E 0C {ssid} 10
{password} CS``. Home Assistant's client (py-improv-ble-client) frames packets
the same way; its parsing rules are mirrored in the scan-result test.
"""

from __future__ import annotations

import pytest

from src.provisioning.improv import (
    MAX_DATA_LEN,
    Capability,
    Command,
    CommandAssembler,
    ImprovError,
    ImprovPacketError,
    State,
    checksum,
    encode_result,
    encode_scan_result,
    parse_command,
    parse_strings,
    parse_wifi_settings,
    service_data,
)


def _frame(command: int, data: bytes) -> bytes:
    body = bytes([command, len(data)]) + data
    return body + bytes([sum(body) & 0xFF])


SPEC_DATA = b"\x0cMyWirelessAP\x10mysecurepassword"
SPEC_FRAME = _frame(0x01, SPEC_DATA)


def test_spec_example_frames_as_documented() -> None:
    assert SPEC_FRAME[:3] == bytes([0x01, 0x1E, 0x0C])
    assert len(SPEC_FRAME) == 0x1E + 3


def test_parse_spec_example() -> None:
    command = parse_command(SPEC_FRAME)
    assert command.command == Command.WIFI_SETTINGS
    creds = parse_wifi_settings(command.data)
    assert creds.ssid == "MyWirelessAP"
    assert creds.password == "mysecurepassword"


def test_credentials_repr_hides_the_password() -> None:
    creds = parse_wifi_settings(SPEC_DATA)
    assert "mysecurepassword" not in repr(creds)
    assert "16 chars" in repr(creds)


@pytest.mark.parametrize(
    "frame",
    [
        b"",
        b"\x01",
        b"\x01\x05\x00\x06",  # length field says 5, carries 1
        SPEC_FRAME[:-1] + bytes([(SPEC_FRAME[-1] + 1) & 0xFF]),  # bad checksum
        SPEC_FRAME + b"\x00",  # trailing byte
    ],
)
def test_parse_command_rejects_malformed_frames(frame: bytes) -> None:
    with pytest.raises(ImprovPacketError) as exc:
        parse_command(frame)
    assert exc.value.error == ImprovError.INVALID_RPC_PACKET


def test_string_overrun_is_refused() -> None:
    with pytest.raises(ImprovPacketError):
        parse_strings(b"\x05abc")


@pytest.mark.parametrize(
    "data",
    [
        b"\x04abcd",  # one string
        b"\x00\x08password",  # empty SSID
        b"\x02\xff\xfe\x00",  # not UTF-8
    ],
)
def test_wifi_settings_must_be_ssid_and_password(data: bytes) -> None:
    with pytest.raises(ImprovPacketError):
        parse_wifi_settings(data)


def test_open_network_password_may_be_empty() -> None:
    creds = parse_wifi_settings(b"\x04Open\x00")
    assert (creds.ssid, creds.password) == ("Open", "")


def test_result_round_trips_through_the_parser() -> None:
    frame = encode_result(Command.WIFI_SETTINGS, ["http://192.168.1.23:8000/"])
    command = parse_command(frame)
    assert command.command == Command.WIFI_SETTINGS
    assert parse_strings(command.data) == [b"http://192.168.1.23:8000/"]


def test_empty_result_is_a_valid_frame() -> None:
    frame = encode_result(Command.WIFI_SETTINGS, [])
    assert frame == bytes([0x01, 0x00, 0x01])
    assert checksum(frame[:-1]) == frame[-1]


def test_scan_result_strongest_kept_when_cut() -> None:
    networks = [(f"network-{i:02d}", -40 - i, "WPA2") for i in range(40)]
    frame = encode_scan_result(networks, max_frame=512)
    strings = parse_strings(parse_command(frame).data)
    assert len(strings) % 3 == 0
    assert len(frame) - 3 <= MAX_DATA_LEN
    ssids = strings[0::3]
    assert ssids[0] == b"network-00"
    # Prefix of the input order: the cut drops the weakest, never a middle one.
    assert ssids == [f"network-{i:02d}".encode() for i in range(len(ssids))]
    # Same triple layout Home Assistant's ScanWifiRes reads.
    assert strings[1:3] == [b"-40", b"WPA2"]


def test_scan_result_respects_a_small_mtu() -> None:
    networks = [("home", -50, "WPA2"), ("neighbour", -70, "WPA2")]
    frame = encode_scan_result(networks, max_frame=20)
    assert len(frame) <= 20
    assert parse_strings(parse_command(frame).data) == [b"home", b"-50", b"WPA2"]


def test_service_data_layout() -> None:
    data = service_data(State.AUTHORIZED, Capability.DEVICE_INFO | Capability.SCAN_WIFI)
    assert data == bytes([0x02, 0x06, 0, 0, 0, 0])


def test_assembler_rebuilds_a_fragmented_frame() -> None:
    assembler = CommandAssembler()
    chunks = [SPEC_FRAME[i : i + 20] for i in range(0, len(SPEC_FRAME), 20)]
    assert len(chunks) == 2
    assert assembler.feed(chunks[0]) is None
    assert assembler.feed(chunks[1]) == SPEC_FRAME


def test_assembler_passes_whole_frames_through() -> None:
    assembler = CommandAssembler()
    frame = _frame(Command.DEVICE_INFO, b"")
    assert assembler.feed(frame) == frame
    assert assembler.feed(frame) == frame
