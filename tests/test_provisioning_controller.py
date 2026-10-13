"""The Improv state machine and its gate (src/provisioning/controller.py, gate.py)."""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path

import pytest

from src.provisioning.controller import DeviceInfo, ImprovController
from src.provisioning.gate import (
    MAX_WINDOW_MINUTES,
    decide,
    read_window_remaining,
    write_window,
)
from src.provisioning.improv import (
    CHAR_ERROR_UUID,
    CHAR_RPC_RESULT_UUID,
    CHAR_STATE_UUID,
    Command,
    ImprovError,
    State,
    WifiCredentials,
    parse_command,
    parse_strings,
)
from src.provisioning.network import JoinResult, NetworkStatus, WifiNetwork

NOW = 1_800_000_000.0


def _frame(command: int, data: bytes = b"") -> bytes:
    body = bytes([command, len(data)]) + data
    return body + bytes([sum(body) & 0xFF])


def _wifi(ssid: str, password: str) -> bytes:
    s, p = ssid.encode(), password.encode()
    return _frame(Command.WIFI_SETTINGS, bytes([len(s)]) + s + bytes([len(p)]) + p)


@dataclass
class FakeNetwork:
    profiles: list[str] = field(default_factory=list)
    join_ok: bool = True
    profiles_fail: bool = False
    joined: list[WifiCredentials] = field(default_factory=list)
    networks: list[WifiNetwork] = field(default_factory=list)

    def scan(self) -> list[WifiNetwork]:
        return self.networks

    def client_profiles(self) -> list[str]:
        if self.profiles_fail:
            raise RuntimeError("nmcli unavailable")
        return list(self.profiles)

    def status(self) -> NetworkStatus:
        connected = bool(self.joined) and self.join_ok
        return NetworkStatus(
            hostname="canary-1a2b",
            mode="client" if connected else "disconnected",
            ssid=self.joined[-1].ssid if connected else None,
            wifi_interface="wlan0",
            addresses={"wlan0": ["192.168.1.23"]} if connected else {},
        )

    def join(self, creds: WifiCredentials, *, timeout_s: float) -> JoinResult:
        self.joined.append(creds)
        if self.join_ok:
            self.profiles.append(f"uuid-{creds.ssid}")
            return JoinResult(True)
        return JoinResult(False, "Secrets were required")


@dataclass
class RecordingSink:
    notified: list[tuple[str, bytes]] = field(default_factory=list)
    states: list[State] = field(default_factory=list)

    def notify(self, char_uuid: str, value: bytes) -> None:
        self.notified.append((char_uuid, value))

    def state_changed(self, state: State) -> None:
        self.states.append(state)

    def errors(self) -> list[ImprovError]:
        return [ImprovError(v[0]) for c, v in self.notified if c == CHAR_ERROR_UUID]

    def results(self) -> list[bytes]:
        return [v for c, v in self.notified if c == CHAR_RPC_RESULT_UUID]


@dataclass
class Rig:
    controller: ImprovController
    network: FakeNetwork
    sink: RecordingSink
    window: Path


@pytest.fixture
def rig(tmp_path: Path) -> Rig:
    network = FakeNetwork()
    sink = RecordingSink()
    window = tmp_path / "provision_window"
    controller = ImprovController(
        network,
        sink,
        device_info=DeviceInfo("CANary", "0.1.0", "Raspberry Pi 5", "canary-1a2b"),
        dashboard_port=8000,
        window_path=window,
        clock=lambda: NOW,
    )
    return Rig(controller, network, sink, window)


async def _provision(rig: Rig, ssid: str = "Starlink", password: str = "longenough") -> None:
    await rig.controller.on_write(_wifi(ssid, password))
    await rig.controller.wait_for_join()


# --- gate -----------------------------------------------------------------


def test_gate_decisions() -> None:
    assert decide(client_profiles=0, window_remaining_s=0).reason == "unprovisioned"
    assert decide(client_profiles=2, window_remaining_s=30).reason == "window"
    locked = decide(client_profiles=2, window_remaining_s=0)
    assert (locked.authorized, locked.reason) == (False, "locked")


def test_window_file_round_trip(tmp_path: Path) -> None:
    path = tmp_path / "w"
    assert read_window_remaining(path, now=NOW) == 0.0
    write_window(5, path, now=NOW)
    assert read_window_remaining(path, now=NOW) == pytest.approx(300)
    assert read_window_remaining(path, now=NOW + 301) == 0.0
    write_window(0, path, now=NOW)
    assert read_window_remaining(path, now=NOW) == 0.0
    path.write_text("garbage")
    assert read_window_remaining(path, now=NOW) == 0.0


def test_window_reader_does_not_trust_the_file(tmp_path: Path) -> None:
    # The daemon user can write this file; the root reader caps what it says.
    path = tmp_path / "w"
    for text in ("inf", "nan", "1e18"):
        path.write_text(text)
        assert read_window_remaining(path, now=NOW) <= MAX_WINDOW_MINUTES * 60


def test_window_length_is_capped(tmp_path: Path) -> None:
    with pytest.raises(ValueError, match="at most"):
        write_window(MAX_WINDOW_MINUTES + 1, tmp_path / "w")


# --- states ---------------------------------------------------------------


async def test_unprovisioned_pi_is_authorized(rig: Rig) -> None:
    await rig.controller.refresh()
    assert rig.controller.state == State.AUTHORIZED
    assert rig.sink.states == [State.AUTHORIZED]


async def test_provisioned_pi_without_window_is_locked(rig: Rig) -> None:
    rig.network.profiles = ["uuid-home"]
    await rig.controller.refresh()
    assert rig.controller.state == State.AUTHORIZATION_REQUIRED
    await _provision(rig)
    assert rig.network.joined == []
    assert rig.sink.errors()[-1] == ImprovError.NOT_AUTHORIZED


async def test_window_authorizes_a_provisioned_pi(rig: Rig) -> None:
    rig.network.profiles = ["uuid-home"]
    write_window(5, rig.window, now=NOW)
    await rig.controller.refresh()
    assert rig.controller.state == State.AUTHORIZED
    await _provision(rig)
    assert rig.controller.state == State.PROVISIONED


async def test_nmcli_failure_fails_closed(rig: Rig) -> None:
    rig.network.profiles_fail = True
    await rig.controller.refresh()
    assert rig.controller.state == State.AUTHORIZATION_REQUIRED


async def test_successful_provisioning_returns_the_dashboard_url(rig: Rig) -> None:
    await rig.controller.refresh()
    await _provision(rig)
    assert rig.network.joined == [WifiCredentials("Starlink", "longenough")]
    assert rig.sink.states == [State.AUTHORIZED, State.PROVISIONING, State.PROVISIONED]
    command = parse_command(rig.sink.results()[-1])
    assert command.command == Command.WIFI_SETTINGS
    assert parse_strings(command.data) == [b"http://192.168.1.23:8000/"]
    # The result is notified after the state, as ESPHome's server does.
    chars = [c for c, _ in rig.sink.notified]
    assert chars.index(CHAR_RPC_RESULT_UUID) > max(
        i for i, c in enumerate(chars) if c == CHAR_STATE_UUID
    )


async def test_provisioned_state_survives_the_gate_closing(rig: Rig) -> None:
    await rig.controller.refresh()
    await _provision(rig)
    # Now a profile exists, so the gate is locked; the state still says provisioned.
    await rig.controller.refresh()
    assert rig.controller.state == State.PROVISIONED
    await _provision(rig, "Other")
    assert rig.sink.errors()[-1] == ImprovError.NOT_AUTHORIZED
    # A window opened afterwards reads as "change networks".
    write_window(5, rig.window, now=NOW)
    await rig.controller.refresh()
    assert rig.controller.state == State.AUTHORIZED


async def test_failed_join_reports_and_returns_to_authorized(rig: Rig) -> None:
    rig.network.join_ok = False
    await rig.controller.refresh()
    await _provision(rig, password="wrongpass")
    assert rig.sink.errors()[-1] == ImprovError.UNABLE_TO_CONNECT
    assert rig.controller.state == State.AUTHORIZED
    assert rig.sink.results() == []


async def test_repeated_error_still_notifies(rig: Rig) -> None:
    rig.network.profiles = ["uuid-home"]
    await rig.controller.refresh()
    await _provision(rig)
    await _provision(rig)
    assert rig.sink.errors() == [
        ImprovError.NOT_AUTHORIZED,
        ImprovError.NONE,
        ImprovError.NOT_AUTHORIZED,
    ]


# --- other commands -------------------------------------------------------


async def test_bad_checksum_is_an_invalid_packet(rig: Rig) -> None:
    frame = bytearray(_wifi("Starlink", "longenough"))
    frame[-1] ^= 0xFF
    await rig.controller.on_write(bytes(frame))
    assert rig.controller.error == ImprovError.INVALID_RPC_PACKET
    assert rig.network.joined == []


async def test_stale_partial_frame_is_abandoned(tmp_path: Path) -> None:
    clock = [NOW]
    network, sink = FakeNetwork(), RecordingSink()
    controller = ImprovController(
        network,
        sink,
        device_info=DeviceInfo("CANary", "0.1.0", "Raspberry Pi 5", "canary-1a2b"),
        dashboard_port=8000,
        window_path=tmp_path / "w",
        clock=lambda: clock[0],
    )
    await controller.refresh()
    await controller.on_write(b"\x00\xff")  # a stray partial frame from someone else
    clock[0] += 5
    await controller.on_write(_wifi("Starlink", "longenough"))
    await controller.wait_for_join()
    assert network.joined == [WifiCredentials("Starlink", "longenough")]


async def test_fragmented_write_is_reassembled(rig: Rig) -> None:
    await rig.controller.refresh()
    frame = _wifi("Starlink", "longenough")
    for i in range(0, len(frame), 20):
        await rig.controller.on_write(frame[i : i + 20])
    await rig.controller.wait_for_join()
    assert rig.network.joined == [WifiCredentials("Starlink", "longenough")]


@pytest.mark.parametrize("command", [Command.IDENTIFY, Command.HOSTNAME, 0x7F])
async def test_unsupported_commands(rig: Rig, command: int) -> None:
    await rig.controller.on_write(_frame(command))
    assert rig.controller.error == ImprovError.UNKNOWN_RPC_COMMAND


async def test_device_info_needs_no_authorization(rig: Rig) -> None:
    rig.network.profiles = ["uuid-home"]
    await rig.controller.refresh()
    await rig.controller.on_write(_frame(Command.DEVICE_INFO))
    strings = parse_strings(parse_command(rig.sink.results()[-1]).data)
    assert strings == [b"CANary", b"0.1.0", b"Raspberry Pi 5", b"canary-1a2b"]


async def test_scan_is_cut_to_the_negotiated_mtu(rig: Rig) -> None:
    rig.network.networks = [WifiNetwork(f"net-{i:02d}", 90 - i, "WPA2") for i in range(30)]
    await rig.controller.on_write(_frame(Command.SCAN_WIFI), mtu=64)
    frame = rig.sink.results()[-1]
    assert len(frame) <= 61
    strings = parse_strings(parse_command(frame).data)
    assert strings[:3] == [b"net-00", b"-55", b"WPA2"]


async def test_status_value_is_json_with_the_gate(rig: Rig) -> None:
    await rig.controller.refresh()
    await _provision(rig)
    status = json.loads(await rig.controller.status_value())
    assert status["url"] == "http://192.168.1.23:8000/"
    assert status["prov"] == "locked"
    assert status["ssid"] == "Starlink"


async def test_success_url_carries_no_secret(rig: Rig) -> None:
    # Pairing is by key cycle; nothing secret crosses Bluetooth.
    await rig.controller.refresh()
    await _provision(rig)
    url = parse_strings(parse_command(rig.sink.results()[-1]).data)[0]
    assert url == b"http://192.168.1.23:8000/"
    assert rig.controller.value(CHAR_RPC_RESULT_UUID) == rig.sink.results()[-1]
