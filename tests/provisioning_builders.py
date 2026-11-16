"""Network-provisioning fakes: the command runner and the Wi-Fi profiles and responses (R30.2).

Moved out of the ``tests/test_*.py`` files that provided them, bodies unchanged; private helpers
got public names."""

from __future__ import annotations

import json
from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path

from src.provisioning.network import NetworkManagerCli, RunResult, profile_uuid


class FakeRunner:
    """Answers nmcli/ip by argv prefix; records every call."""

    def __init__(self, responses: dict[tuple[str, ...], RunResult]) -> None:
        self.responses = responses
        self.calls: list[list[str]] = []

    def __call__(self, argv: Sequence[str], *, timeout: float) -> RunResult:
        self.calls.append(list(argv))
        best: tuple[str, ...] = ()
        for prefix in self.responses:
            if tuple(argv[: len(prefix)]) == prefix and len(prefix) > len(best):
                best = prefix
        return self.responses.get(best, RunResult(0, "", ""))

    def called(self, *prefix: str) -> bool:
        return any(tuple(c[: len(prefix)]) == prefix for c in self.calls)


PROPERTIES = (
    "connection.autoconnect-priority,connection.autoconnect,802-11-wireless.mode,"
    "802-11-wireless.ssid,802-11-wireless.hidden,802-11-wireless-security.key-mgmt"
)


LIST = ("nmcli", "-t", "-f", "UUID,NAME,TYPE,ACTIVE", "connection", "show")


DEVICES = ("nmcli", "-t", "-f", "DEVICE,TYPE", "device", "status")


SCAN = ("nmcli", "-t", "-f", "SSID,SIGNAL,SECURITY", "device", "wifi", "list")


CACHED = (*SCAN, "ifname", "wlan0")


RESCAN = (*CACHED, "--rescan", "yes")


SCAN_OUTPUT = "\n".join(
    [
        "Starlink:82:WPA2",
        "Starlink:64:WPA2",
        "Cafe:40:",
        "PhoneHotspot:71:WPA2",
        "Corp:60:WPA2 802.1X",
        "NewRouter:50:WPA3",
        ":55:WPA2",  # hidden
    ]
)


@dataclass(frozen=True)
class Profile:
    uuid: str
    name: str
    kind: str = "802-11-wireless"
    ssid: str = ""
    mode: str = "infrastructure"
    priority: int = 0
    autoconnect: bool = True
    hidden: bool = False
    key_mgmt: str = "wpa-psk"
    active: bool = False

    def row(self) -> str:
        return f"{self.uuid}:{self.name}:{self.kind}:{'yes' if self.active else 'no'}"

    def properties(self) -> str:
        return "\n".join(
            [
                f"connection.autoconnect-priority:{self.priority}",
                f"connection.autoconnect:{'yes' if self.autoconnect else 'no'}",
                f"802-11-wireless.mode:{self.mode}",
                f"802-11-wireless.ssid:{self.ssid.replace(':', chr(92) + ':')}",
                f"802-11-wireless.hidden:{'yes' if self.hidden else 'no'}",
                f"802-11-wireless-security.key-mgmt:{self.key_mgmt or '--'}",
            ]
        )


PRECONFIGURED = Profile("u-pre", "preconfigured", ssid="HomeWifi", priority=0)


STARLINK = Profile(
    profile_uuid("Starlink"), "canary-wifi-Starlink", ssid="Starlink", priority=50, active=True
)


HOTSPOT = Profile(
    profile_uuid("PhoneHotspot"),
    "canary-wifi-PhoneHotspot",
    ssid="PhoneHotspot",
    priority=20,
    hidden=False,
)


OFFSITE = Profile(
    "u-off", "canary-wifi-Offsite", ssid="Offsite", priority=20, hidden=True, key_mgmt=""
)


AP = Profile("u-ap", "CANary-ap", ssid="CANary-setup", mode="ap")


WIRED = Profile("u-wired", "canary-wired", kind="802-3-ethernet", active=True)


LOOPBACK = Profile("u-lo", "lo", kind="loopback", active=True)


EVERYTHING = [PRECONFIGURED, STARLINK, HOTSPOT, OFFSITE, AP, WIRED, LOOPBACK]


def nm_responses(
    profiles: Sequence[Profile], *, scan: str = SCAN_OUTPUT
) -> dict[tuple[str, ...], RunResult]:
    responses: dict[tuple[str, ...], RunResult] = {
        LIST: RunResult(0, "\n".join(p.row() for p in profiles) + "\n", ""),
        DEVICES: RunResult(0, "eth0:ethernet\nwlan0:wifi\nlo:loopback\n", ""),
        CACHED: RunResult(0, scan, ""),
        RESCAN: RunResult(0, scan, ""),
        SCAN: RunResult(0, scan, ""),
    }
    for p in profiles:
        responses[("nmcli", "-t", "-f", PROPERTIES, "connection", "show", "uuid", p.uuid)] = (
            RunResult(0, p.properties() + "\n", "")
        )
    return responses


def cli(
    tmp_path: Path,
    profiles: Sequence[Profile] = EVERYTHING,
    *,
    extra: dict[tuple[str, ...], RunResult] | None = None,
    scan: str = SCAN_OUTPUT,
    clock: float = 1000.0,
) -> tuple[NetworkManagerCli, FakeRunner]:
    runner = FakeRunner({**nm_responses(profiles, scan=scan), **(extra or {})})
    return NetworkManagerCli(runner, connections_dir=tmp_path, clock=lambda: clock), runner


IP_JSON = json.dumps(
    [
        {"ifname": "lo", "addr_info": [{"family": "inet", "local": "127.0.0.1"}]},
        {"ifname": "eth0", "addr_info": [{"family": "inet", "local": "192.168.137.2"}]},
        {
            "ifname": "wlan0",
            "addr_info": [
                {"family": "inet", "local": "192.168.1.23"},
                {"family": "inet6", "local": "2001:db8::23", "scope": "global"},
            ],
        },
    ]
)


HEALTHY: dict[tuple[str, ...], RunResult] = {
    ("ip",): RunResult(0, IP_JSON, ""),
    ("nmcli", "-t", "-f", "DEVICE,TYPE,STATE,CON-UUID"): RunResult(
        0, "eth0:ethernet:connected:u-eth\nwlan0:wifi:connected:u-home\n", ""
    ),
    ("nmcli", "-g", "802-11-wireless.mode"): RunResult(0, "infrastructure\n", ""),
    ("nmcli", "-g", "802-11-wireless.ssid"): RunResult(0, "Starlink\n", ""),
}


class ReaderFakeRunner:
    """Answers by the longest matching argv prefix; counts every call."""

    def __init__(self, responses: dict[tuple[str, ...], RunResult]) -> None:
        self.responses = responses
        self.calls: list[list[str]] = []

    def __call__(self, argv: Sequence[str], *, timeout: float) -> RunResult:
        self.calls.append(list(argv))
        best: tuple[str, ...] = ()
        for prefix in self.responses:
            if tuple(argv[: len(prefix)]) == prefix and len(prefix) > len(best):
                best = prefix
        return self.responses.get(best, RunResult(0, "", ""))
