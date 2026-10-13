"""NetworkManager backend (src/provisioning/network.py) against recorded output."""

from __future__ import annotations

import json
import stat
from collections.abc import Sequence
from pathlib import Path

import pytest

from src.provisioning.improv import WifiCredentials
from src.provisioning.network import (
    PROFILE_PREFIX,
    JoinRefusedError,
    NetworkManagerCli,
    RunResult,
    WifiNetwork,
    choose_key_mgmt,
    parse_ip_addresses,
    parse_scan,
    profile_filename,
    profile_uuid,
    render_keyfile,
    split_terse,
    unescape_value,
)

SCAN_OUTPUT = "\n".join(
    [
        "Starlink:82:WPA2",
        "Starlink:64:WPA2",  # a second BSSID, weaker
        "home\\:net:70:WPA1 WPA2",  # an SSID with a colon
        ":55:WPA2",  # hidden
        "Corp:60:WPA2 802.1X",
        "Cafe:40:",
        "NewRouter:50:WPA3",
    ]
)

IP_JSON = json.dumps(
    [
        {"ifname": "lo", "addr_info": [{"family": "inet", "local": "127.0.0.1"}]},
        {
            "ifname": "eth0",
            "addr_info": [
                {"family": "inet", "local": "192.168.137.2", "scope": "global"},
                {"family": "inet6", "local": "fe80::1", "scope": "link"},
            ],
        },
        {
            "ifname": "wlan0",
            "addr_info": [
                {"family": "inet", "local": "192.168.1.23", "scope": "global"},
                {"family": "inet6", "local": "2001:db8::23", "scope": "global"},
            ],
        },
        {"ifname": "usb0", "addr_info": []},
    ]
)


def test_split_terse_honours_escapes() -> None:
    assert split_terse("a\\:b:c\\\\d:") == ["a:b", "c\\d", ""]
    assert unescape_value("fe80\\:\\:1/64") == "fe80::1/64"


def test_parse_scan_dedupes_sorts_and_drops_hidden() -> None:
    networks = parse_scan(SCAN_OUTPUT)
    assert [n.ssid for n in networks] == [
        "Starlink",
        "home:net",
        "Corp",
        "NewRouter",
        "Cafe",
    ]
    assert networks[0].signal == 82


@pytest.mark.parametrize(
    ("security", "expected"),
    [
        ("WPA2", "WPA2"),
        ("WPA1 WPA2", "WPA/WPA2"),
        ("WPA2 WPA3", "WPA2/WPA3"),
        ("WPA2 802.1X", "WPA2 EAP"),
        ("", "NO"),
        ("--", "NO"),
        ("WEP", "WEP"),
    ],
)
def test_improv_auth_vocabulary(security: str, expected: str) -> None:
    assert WifiNetwork("x", 50, security).improv_auth == expected


def test_rssi_inverts_networkmanager_quality() -> None:
    assert WifiNetwork("x", 100, "").rssi_dbm == -50
    assert WifiNetwork("x", 0, "").rssi_dbm == -100


def test_parse_ip_keeps_reachable_addresses() -> None:
    assert parse_ip_addresses(IP_JSON) == {
        "eth0": ["192.168.137.2"],
        "wlan0": ["192.168.1.23", "2001:db8::23"],
    }
    assert parse_ip_addresses("not json") == {}


@pytest.mark.parametrize(
    ("security", "password", "expected"),
    [
        ("WPA2", "longenough", "wpa-psk"),
        ("WPA1 WPA2", "a" * 63, "wpa-psk"),
        ("WPA2", "ab" * 32, "wpa-psk"),  # 64 hex digits
        ("WPA2 WPA3", "longenough", "wpa-psk"),
        ("WPA3", "short", "sae"),
        ("", "", None),
        ("", "ignored", None),
        (None, "longenough", "wpa-psk"),  # hidden, with a password
        (None, "", None),  # hidden, open
    ],
)
def test_choose_key_mgmt(security: str | None, password: str, expected: str | None) -> None:
    assert choose_key_mgmt(security, password) == expected


@pytest.mark.parametrize(
    ("security", "password"),
    [
        ("WPA2 802.1X", "whatever1"),
        ("WEP", "whatever1"),
        ("WPA2", "short"),
        ("WPA2", "x" * 64),  # 64 chars but not hex
        ("WPA3", ""),
    ],
)
def test_choose_key_mgmt_refuses(security: str, password: str) -> None:
    with pytest.raises(JoinRefusedError):
        choose_key_mgmt(security, password)


def test_keyfile_plain_ssid_and_escaped_secret() -> None:
    creds = WifiCredentials("Starlink", "pass word\\1")
    text = render_keyfile(creds, "wpa-psk", hidden=False, interface="wlan0")
    assert "ssid=Starlink\n" in text
    assert "psk=pass\\sword\\\\1\n" in text
    assert f"uuid={profile_uuid('Starlink')}\n" in text
    assert "interface-name=wlan0\n" in text
    assert "hidden" not in text


def test_keyfile_awkward_ssid_is_a_byte_list() -> None:
    text = render_keyfile(WifiCredentials("a;b", ""), None, hidden=True, interface=None)
    assert "ssid=97;59;98;\n" in text
    assert "hidden=true" in text
    assert "[wifi-security]" not in text
    assert "ssid=49;50;\n" in render_keyfile(
        WifiCredentials("12", ""), None, hidden=False, interface=None
    )


def test_profile_identity_is_stable_per_ssid() -> None:
    assert profile_uuid("Starlink") == profile_uuid("Starlink")
    assert profile_uuid("Starlink") != profile_uuid("home")
    assert profile_filename("Starlink").startswith(PROFILE_PREFIX)
    assert profile_filename("Starlink").endswith(".nmconnection")


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


SCAN = ("nmcli", "-t", "-f", "SSID,SIGNAL,SECURITY", "device", "wifi", "list")


def _cli(runner: FakeRunner, tmp_path: Path) -> NetworkManagerCli:
    return NetworkManagerCli(runner, connections_dir=tmp_path)


def test_join_success_writes_a_0600_keyfile(tmp_path: Path) -> None:
    runner = FakeRunner({SCAN: RunResult(0, SCAN_OUTPUT, "")})
    result = _cli(runner, tmp_path).join(WifiCredentials("Starlink", "longenough"), timeout_s=30)
    assert result.ok
    path = tmp_path / profile_filename("Starlink")
    assert stat.S_IMODE(path.stat().st_mode) == 0o600
    assert "psk=longenough" in path.read_text()
    assert runner.called("nmcli", "connection", "load", str(path))
    assert runner.called("nmcli", "--wait", "30", "connection", "up", "uuid")
    # The secret never reaches an argv.
    assert not any("longenough" in " ".join(call) for call in runner.calls)


def test_failed_join_removes_a_new_profile(tmp_path: Path) -> None:
    runner = FakeRunner(
        {
            SCAN: RunResult(0, SCAN_OUTPUT, ""),
            ("nmcli", "--wait"): RunResult(4, "", "Secrets were required"),
        }
    )
    result = _cli(runner, tmp_path).join(WifiCredentials("Starlink", "wrongpass"), timeout_s=5)
    assert not result.ok
    assert "Secrets were required" in result.detail
    assert not (tmp_path / profile_filename("Starlink")).exists()
    assert runner.called("nmcli", "connection", "delete", "uuid", profile_uuid("Starlink"))


def test_failed_join_restores_the_previous_profile(tmp_path: Path) -> None:
    path = tmp_path / profile_filename("Starlink")
    path.write_text("previous profile\n")
    runner = FakeRunner(
        {
            SCAN: RunResult(0, SCAN_OUTPUT, ""),
            ("nmcli", "--wait"): RunResult(4, "", "timeout"),
        }
    )
    assert (
        not _cli(runner, tmp_path).join(WifiCredentials("Starlink", "newpassword"), timeout_s=5).ok
    )
    assert path.read_text() == "previous profile\n"
    assert not runner.called("nmcli", "connection", "delete")


def test_refused_credentials_touch_nothing(tmp_path: Path) -> None:
    runner = FakeRunner({SCAN: RunResult(0, SCAN_OUTPUT, "")})
    result = _cli(runner, tmp_path).join(WifiCredentials("Corp", "whatever1"), timeout_s=5)
    assert not result.ok
    assert "802.1X" in result.detail
    assert list(tmp_path.iterdir()) == []


def test_client_profiles_excludes_access_points(tmp_path: Path) -> None:
    runner = FakeRunner(
        {
            ("nmcli", "-t", "-f", "UUID,TYPE", "connection", "show"): RunResult(
                0, "u-home:802-11-wireless\nu-ap:802-11-wireless\nu-eth:802-3-ethernet\n", ""
            ),
            ("nmcli", "-g", "802-11-wireless.mode", "connection", "show", "uuid", "u-home"): (
                RunResult(0, "infrastructure\n", "")
            ),
            ("nmcli", "-g", "802-11-wireless.mode", "connection", "show", "uuid", "u-ap"): (
                RunResult(0, "ap\n", "")
            ),
        }
    )
    assert _cli(runner, tmp_path).client_profiles() == ["u-home"]


def test_client_profiles_failure_is_not_zero(tmp_path: Path) -> None:
    runner = FakeRunner({("nmcli", "-t", "-f", "UUID,TYPE"): RunResult(8, "", "NM not running")})
    with pytest.raises(RuntimeError, match="NM not running"):
        _cli(runner, tmp_path).client_profiles()


def _status_runner(device_line: str, mode: str = "infrastructure") -> FakeRunner:
    return FakeRunner(
        {
            ("ip", "-j", "addr", "show"): RunResult(0, IP_JSON, ""),
            ("nmcli", "-t", "-f", "DEVICE,TYPE,STATE,CON-UUID"): RunResult(
                0, f"eth0:ethernet:connected:u-eth\n{device_line}\n", ""
            ),
            ("nmcli", "-g", "802-11-wireless.mode"): RunResult(0, f"{mode}\n", ""),
            ("nmcli", "-g", "802-11-wireless.ssid"): RunResult(0, "home\\:net\n", ""),
        }
    )


def test_status_client(tmp_path: Path) -> None:
    status = _cli(_status_runner("wlan0:wifi:connected:u-home"), tmp_path).status()
    assert (status.mode, status.ssid, status.wifi_interface) == ("client", "home:net", "wlan0")
    assert status.wifi_ipv4 == "192.168.1.23"


def test_status_ap_and_disconnected(tmp_path: Path) -> None:
    assert _cli(_status_runner("wlan0:wifi:connected:u-ap", "ap"), tmp_path).status().mode == "ap"
    assert (
        _cli(_status_runner("wlan0:wifi:disconnected:"), tmp_path).status().mode == "disconnected"
    )
    assert _cli(_status_runner("p2p:wifi-p2p:disconnected:"), tmp_path).status().mode == "no-wifi"


def test_client_profiles_accepts_the_wifi_alias(tmp_path: Path) -> None:
    runner = FakeRunner(
        {
            ("nmcli", "-t", "-f", "UUID,TYPE", "connection", "show"): RunResult(
                0, "u-home:wifi\n", ""
            ),
        }
    )
    assert _cli(runner, tmp_path).client_profiles() == ["u-home"]


def test_client_profiles_mode_failure_fails_closed(tmp_path: Path) -> None:
    runner = FakeRunner(
        {
            ("nmcli", "-t", "-f", "UUID,TYPE", "connection", "show"): RunResult(
                0, "u-home:802-11-wireless\n", ""
            ),
            ("nmcli", "-g", "802-11-wireless.mode"): RunResult(124, "", "timed out"),
        }
    )
    with pytest.raises(RuntimeError):
        _cli(runner, tmp_path).client_profiles()


def test_join_leaves_no_journal(tmp_path: Path) -> None:
    ok = FakeRunner({SCAN: RunResult(0, SCAN_OUTPUT, "")})
    _cli(ok, tmp_path).join(WifiCredentials("Starlink", "longenough"), timeout_s=5)
    failing = FakeRunner(
        {SCAN: RunResult(0, SCAN_OUTPUT, ""), ("nmcli", "--wait"): RunResult(4, "", "no")}
    )
    _cli(failing, tmp_path).join(WifiCredentials("home:net", "longenough"), timeout_s=5)
    assert [p.name for p in tmp_path.iterdir()] == [profile_filename("Starlink")]


def _interrupted(tmp_path: Path, previous: str | None) -> Path:
    """The files a join leaves when the process dies during `connection up`."""
    path = tmp_path / profile_filename("Starlink")
    path.write_text("unvalidated profile\n")
    journal = tmp_path / f".{path.name}.pending"
    journal.write_text(json.dumps({"uuid": profile_uuid("Starlink"), "previous": previous}))
    return path


def test_recover_deletes_an_interrupted_new_profile(tmp_path: Path) -> None:
    path = _interrupted(tmp_path, None)
    runner = FakeRunner({("nmcli", "-g", "GENERAL.STATE"): RunResult(0, "activating\n", "")})
    assert _cli(runner, tmp_path).recover() == [path.name]
    assert list(tmp_path.iterdir()) == []
    assert runner.called("nmcli", "connection", "delete", "uuid", profile_uuid("Starlink"))


def test_recover_restores_a_replaced_profile(tmp_path: Path) -> None:
    path = _interrupted(tmp_path, "previous profile\n")
    runner = FakeRunner({("nmcli", "-g", "GENERAL.STATE"): RunResult(0, "", "")})
    _cli(runner, tmp_path).recover()
    assert path.read_text() == "previous profile\n"
    assert [p.name for p in tmp_path.iterdir()] == [path.name]


def test_recover_keeps_a_join_that_worked(tmp_path: Path) -> None:
    path = _interrupted(tmp_path, None)
    runner = FakeRunner({("nmcli", "-g", "GENERAL.STATE"): RunResult(0, "activated\n", "")})
    assert _cli(runner, tmp_path).recover() == []
    assert [p.name for p in tmp_path.iterdir()] == [path.name]
