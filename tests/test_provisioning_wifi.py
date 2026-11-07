"""Saved WiFi networks (N5.2): list, add, connect, forget, order.

Against ``nmcli -t`` output written out as NetworkManager prints it, as the
join tests are -- none of this needs NetworkManager. The listing reads
``connection show`` rows and then each WiFi profile's own settings, so the
fixtures below build both from one description of the Pi's profiles.
"""

from __future__ import annotations

import json
import logging
import stat
import threading
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from pathlib import Path

import pytest

from src.provisioning.network import (
    KIND_INTERNAL,
    KIND_INVALID,
    KIND_JOIN_FAILED,
    KIND_NOT_FOUND,
    KIND_REFUSED,
    NetworkManagerCli,
    RunResult,
    WifiOpError,
    parse_profile_properties,
    profile_filename,
    profile_uuid,
)
from tests.test_provisioning_network import FakeRunner

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


# --- reading ----------------------------------------------------------------


def test_parse_profile_properties() -> None:
    parsed = parse_profile_properties(
        "802-11-wireless.ssid:home\\:net\n"
        "802-11-wireless-security.key-mgmt:--\n"
        "connection.autoconnect-priority:-5\n"
        "garbage line without a value\n"
    )
    assert parsed == {
        "802-11-wireless.ssid": "home:net",
        "802-11-wireless-security.key-mgmt": "",
        "connection.autoconnect-priority": "-5",
    }


def test_saved_networks_lists_ours_and_foreign_but_never_an_access_point(tmp_path: Path) -> None:
    nm, _ = cli(tmp_path)
    saved = {n.ssid: n for n in nm.saved_networks()}
    assert set(saved) == {"HomeWifi", "Starlink", "PhoneHotspot", "Offsite"}
    pre = saved["HomeWifi"]
    assert (pre.id, pre.managed_by, pre.priority, pre.autoconnect) == ("u-pre", "other", 0, True)
    assert saved["Starlink"].managed_by == "canary"
    assert saved["Starlink"].active and not saved["HomeWifi"].active
    assert saved["Offsite"].hidden and saved["Offsite"].security is None
    assert saved["Starlink"].security == "wpa-psk"


def test_a_wifi_profile_with_no_mode_is_a_client(tmp_path: Path) -> None:
    unset = Profile("u-x", "x", ssid="Unset", mode="")
    assert [n.ssid for n in cli(tmp_path, [unset])[0].saved_networks()] == ["Unset"]


def test_wifi_state_joins_the_scan_in_priority_order(tmp_path: Path) -> None:
    nm, _ = cli(tmp_path)
    state = nm.wifi_state()
    assert [n.ssid for n in state.networks] == ["Starlink", "Offsite", "PhoneHotspot", "HomeWifi"]
    body = state.to_dict()
    by_ssid = {n["ssid"]: n for n in body["networks"]}
    assert (by_ssid["Starlink"]["in_range"], by_ssid["Starlink"]["signal"]) == (True, 82)
    assert (by_ssid["PhoneHotspot"]["in_range"], by_ssid["PhoneHotspot"]["signal"]) == (True, 71)
    # Not in the scan: out of range, no signal. A hidden network cannot be heard.
    assert (by_ssid["HomeWifi"]["in_range"], by_ssid["HomeWifi"]["signal"]) == (False, None)
    assert (by_ssid["Offsite"]["in_range"], by_ssid["Offsite"]["signal"]) == (False, None)
    assert set(by_ssid["Starlink"]) == {
        "id",
        "ssid",
        "priority",
        "autoconnect",
        "hidden",
        "security",
        "active",
        "in_range",
        "signal",
        "managed_by",
    }
    # The scan: strongest first, one per SSID, hidden dropped, "saved" marked.
    assert [e["ssid"] for e in body["scan"]] == [
        "Starlink",
        "PhoneHotspot",
        "Corp",
        "NewRouter",
        "Cafe",
    ]
    assert {e["ssid"]: e["saved"] for e in body["scan"]} == {
        "Starlink": True,
        "PhoneHotspot": True,
        "Corp": False,
        "NewRouter": False,
        "Cafe": False,
    }
    assert body["scan"][-1] == {"ssid": "Cafe", "signal": 40, "security": "", "saved": False}
    assert body["interface"] == "wlan0"
    # The service has not rescanned, so the scan is NetworkManager's cache.
    assert body["scanned_at"] is None


def test_the_active_network_is_in_range_even_if_the_scan_cannot_name_it(tmp_path: Path) -> None:
    hidden_active = Profile("u-h", "x", ssid="Quiet", hidden=True, active=True)
    state = cli(tmp_path, [hidden_active])[0].wifi_state()
    assert state.networks[0].in_range and state.networks[0].signal is None


def test_equal_priorities_sort_by_ssid(tmp_path: Path) -> None:
    profiles = [
        Profile("u-b", "b", ssid="Beta", priority=5),
        Profile("u-a", "a", ssid="Alpha", priority=5),
    ]
    assert [n.ssid for n in cli(tmp_path, profiles)[0].wifi_state().networks] == ["Alpha", "Beta"]


def test_rescan_records_when_it_happened(tmp_path: Path) -> None:
    nm, runner = cli(tmp_path, clock=1234.5)
    state = nm.wifi_state(rescan=True)
    assert state.scanned_at == 1234.5
    assert runner.called(*RESCAN)
    # A later plain read still reports when the last rescan was.
    assert nm.wifi_state().scanned_at == 1234.5


def test_a_refused_rescan_falls_back_to_the_cache_and_says_nothing_was_scanned(
    tmp_path: Path,
) -> None:
    refused = RunResult(1, "", "Error: Scanning not allowed immediately following previous scan.")
    nm, _ = cli(tmp_path, extra={RESCAN: refused})
    state = nm.wifi_state(rescan=True)
    assert state.scanned_at is None
    assert [e.ssid for e in state.scan][0] == "Starlink"


def test_no_wifi_device_is_an_interface_of_null_not_a_failure(tmp_path: Path) -> None:
    nm, runner = cli(tmp_path, extra={DEVICES: RunResult(0, "eth0:ethernet\n", "")})
    state = nm.wifi_state(rescan=True)
    assert state.interface is None and state.scan == []
    assert len(state.networks) == 4
    assert not runner.called(*SCAN)


def test_an_unreadable_listing_raises_instead_of_answering_empty(tmp_path: Path) -> None:
    nm, _ = cli(tmp_path, extra={LIST: RunResult(8, "", "NetworkManager is not running")})
    with pytest.raises(WifiOpError, match="not running") as caught:
        nm.wifi_state()
    assert caught.value.kind == KIND_INTERNAL


def test_listing_never_asks_for_a_secret(tmp_path: Path) -> None:
    nm, runner = cli(tmp_path)
    nm.wifi_state(rescan=True)
    asked = " ".join(" ".join(call) for call in runner.calls)
    assert "psk" not in asked.replace("wpa-psk", "")
    assert "secrets" not in asked and "--show-secrets" not in asked


# --- add --------------------------------------------------------------------


def test_add_and_join_is_the_verified_join(tmp_path: Path) -> None:
    nm, runner = cli(tmp_path, [PRECONFIGURED])
    con_id = nm.add_network("Starlink", "longenough", hidden=False, join=True, timeout_s=30)
    assert con_id == profile_uuid("Starlink")
    assert runner.called("nmcli", "--wait", "30", "connection", "up", "uuid", con_id)
    assert (tmp_path / profile_filename("Starlink")).exists()
    assert not any("longenough" in " ".join(call) for call in runner.calls)


def test_add_and_join_that_fails_is_rolled_back(tmp_path: Path) -> None:
    extra: dict[tuple[str, ...], RunResult] = {
        ("nmcli", "--wait"): RunResult(4, "", "Secrets were required, but not provided.")
    }
    nm, runner = cli(tmp_path, [PRECONFIGURED], extra=extra)
    with pytest.raises(WifiOpError, match="Secrets were required") as caught:
        nm.add_network("Starlink", "wrongpass1", hidden=False, join=True, timeout_s=30)
    assert caught.value.kind == KIND_JOIN_FAILED
    assert not (tmp_path / profile_filename("Starlink")).exists()
    assert runner.called("nmcli", "connection", "delete", "uuid", profile_uuid("Starlink"))


def test_save_for_later_writes_and_loads_but_never_activates(tmp_path: Path) -> None:
    nm, runner = cli(tmp_path, [PRECONFIGURED])
    con_id = nm.add_network("PhoneHotspot", "hotspotpass", hidden=False, join=False, timeout_s=30)
    path = tmp_path / profile_filename("PhoneHotspot")
    assert stat.S_IMODE(path.stat().st_mode) == 0o600
    assert "psk=hotspotpass" in path.read_text()
    assert runner.called("nmcli", "connection", "load", str(path))
    assert not runner.called("nmcli", "--wait")
    assert not runner.called("nmcli", "connection", "up")
    assert con_id == profile_uuid("PhoneHotspot")
    # No journal: nothing was risked, so there is nothing to undo after a crash.
    assert [p.name for p in tmp_path.iterdir()] == [path.name]
    assert not any("hotspotpass" in " ".join(call) for call in runner.calls)


def test_save_for_later_out_of_range_decides_security_from_the_passphrase(tmp_path: Path) -> None:
    nm, _ = cli(tmp_path, [PRECONFIGURED])
    nm.add_network("Elsewhere", "somewherepass", hidden=False, join=False, timeout_s=30)
    text = (tmp_path / profile_filename("Elsewhere")).read_text()
    assert "key-mgmt=wpa-psk" in text
    # "Not here" is not "hidden": a saved-for-later network is written as broadcast.
    assert "hidden" not in text
    nm.add_network("OpenElsewhere", None, hidden=False, join=False, timeout_s=30)
    open_text = (tmp_path / profile_filename("OpenElsewhere")).read_text()
    assert "[wifi-security]" not in open_text and "hidden" not in open_text


def test_save_for_later_honours_the_hidden_flag(tmp_path: Path) -> None:
    nm, _ = cli(tmp_path, [PRECONFIGURED])
    nm.add_network("Stealth", "stealthpass", hidden=True, join=False, timeout_s=30)
    assert "hidden=true" in (tmp_path / profile_filename("Stealth")).read_text()


def test_save_for_later_in_range_uses_the_scanned_security(tmp_path: Path) -> None:
    nm, _ = cli(tmp_path, [PRECONFIGURED])
    nm.add_network("NewRouter", "anything", hidden=False, join=False, timeout_s=30)
    assert "key-mgmt=sae" in (tmp_path / profile_filename("NewRouter")).read_text()


def test_a_join_to_a_network_not_in_the_scan_is_written_hidden(tmp_path: Path) -> None:
    nm, _ = cli(tmp_path, [PRECONFIGURED])
    nm.add_network("Elsewhere", "somewherepass", hidden=False, join=True, timeout_s=30)
    assert "hidden=true" in (tmp_path / profile_filename("Elsewhere")).read_text()


@pytest.mark.parametrize(
    ("ssid", "password", "kind"),
    [
        ("Starlink", "short", KIND_INVALID),  # wrong passphrase length
        ("Starlink", None, KIND_INVALID),  # a secured network, no passphrase
        ("NewRouter", None, KIND_INVALID),  # WPA3 needs one too
        ("Corp", "whatever1", KIND_REFUSED),  # enterprise: a network type, not a typo
        ("", "longenough", KIND_INVALID),
        ("x" * 33, "longenough", KIND_INVALID),
        ("é" * 17, "longenough", KIND_INVALID),  # 34 bytes
        ("bad\ud800", "longenough", KIND_INVALID),  # not text
        (42, "longenough", KIND_INVALID),
    ],
)
@pytest.mark.parametrize("join", [True, False])
def test_add_refuses_before_writing_anything(
    tmp_path: Path, ssid: object, password: str | None, kind: str, join: bool
) -> None:
    nm, runner = cli(tmp_path, [PRECONFIGURED])
    with pytest.raises(WifiOpError) as caught:
        nm.add_network(ssid, password, hidden=False, join=join, timeout_s=30)
    assert caught.value.kind == kind
    assert list(tmp_path.iterdir()) == []
    assert not runner.called("nmcli", "connection", "load")


def test_add_accepts_a_32_byte_name(tmp_path: Path) -> None:
    nm, _ = cli(tmp_path, [PRECONFIGURED])
    nm.add_network("é" * 16, None, hidden=False, join=False, timeout_s=30)


def test_a_non_text_passphrase_is_invalid(tmp_path: Path) -> None:
    nm, _ = cli(tmp_path, [PRECONFIGURED])
    with pytest.raises(WifiOpError) as caught:
        nm.add_network("Starlink", 12345678, hidden=False, join=False, timeout_s=30)
    assert caught.value.kind == KIND_INVALID


@pytest.mark.parametrize("join", [True, False])
def test_an_ssid_held_by_a_foreign_profile_is_refused_not_duplicated(
    tmp_path: Path, join: bool
) -> None:
    nm, runner = cli(tmp_path, [PRECONFIGURED])
    with pytest.raises(WifiOpError, match="forget it first") as caught:
        nm.add_network("HomeWifi", "longenough", hidden=False, join=join, timeout_s=30)
    assert caught.value.kind == KIND_REFUSED
    assert "preconfigured" in caught.value.detail
    assert list(tmp_path.iterdir()) == []
    assert not runner.called("nmcli", "connection", "load")


def test_an_ssid_we_already_hold_is_replaced_under_the_same_identity(tmp_path: Path) -> None:
    nm, runner = cli(tmp_path, [STARLINK])
    path = tmp_path / profile_filename("Starlink")
    path.write_text("old profile\n")
    nm.add_network("Starlink", "newpassword", hidden=False, join=False, timeout_s=30)
    assert "psk=newpassword" in path.read_text()
    assert f"uuid={profile_uuid('Starlink')}" in path.read_text()
    assert runner.called("nmcli", "connection", "load", str(path))


def test_save_for_later_that_networkmanager_refuses_restores_the_old_profile(
    tmp_path: Path,
) -> None:
    extra: dict[tuple[str, ...], RunResult] = {
        ("nmcli", "connection", "load"): RunResult(1, "", "invalid connection: bad")
    }
    nm, _ = cli(tmp_path, [STARLINK], extra=extra)
    path = tmp_path / profile_filename("Starlink")
    path.write_text("old profile\n")
    with pytest.raises(WifiOpError, match="invalid connection") as caught:
        nm.add_network("Starlink", "newpassword", hidden=False, join=False, timeout_s=30)
    assert caught.value.kind == KIND_INVALID
    assert path.read_text() == "old profile\n"


def test_save_for_later_that_networkmanager_refuses_leaves_nothing_new(tmp_path: Path) -> None:
    extra: dict[tuple[str, ...], RunResult] = {
        ("nmcli", "connection", "load"): RunResult(1, "", "invalid connection: bad")
    }
    nm, runner = cli(tmp_path, [PRECONFIGURED], extra=extra)
    with pytest.raises(WifiOpError):
        nm.add_network("Newone", "newpassword", hidden=False, join=False, timeout_s=30)
    assert list(tmp_path.iterdir()) == []
    assert runner.called("nmcli", "connection", "delete", "uuid", profile_uuid("Newone"))


# --- connect ----------------------------------------------------------------


def test_connect_brings_the_profile_up_by_uuid(tmp_path: Path) -> None:
    nm, runner = cli(tmp_path)
    nm.connect_network("u-pre", timeout_s=25)
    assert runner.called("nmcli", "--wait", "25", "connection", "up", "uuid", "u-pre")


def test_connect_failure_names_why(tmp_path: Path) -> None:
    extra: dict[tuple[str, ...], RunResult] = {
        ("nmcli", "--wait"): RunResult(10, "", "Error: No network with SSID 'HomeWifi' found.")
    }
    nm, _ = cli(tmp_path, extra=extra)
    with pytest.raises(WifiOpError, match="No network with SSID") as caught:
        nm.connect_network("u-pre", timeout_s=25)
    assert caught.value.kind == KIND_JOIN_FAILED


@pytest.mark.parametrize(
    ("con_id", "kind"),
    [
        ("nope", KIND_NOT_FOUND),
        (None, KIND_NOT_FOUND),
        ("u-wired", KIND_REFUSED),
        ("u-ap", KIND_REFUSED),
        ("u-lo", KIND_REFUSED),
    ],
)
def test_connect_and_forget_only_touch_wifi_client_profiles(
    tmp_path: Path, con_id: object, kind: str
) -> None:
    nm, runner = cli(tmp_path)
    acts: list[Callable[[], None]] = [
        lambda: nm.connect_network(con_id, timeout_s=5),
        lambda: nm.forget_network(con_id),
    ]
    for act in acts:
        with pytest.raises(WifiOpError) as caught:
            act()
        assert caught.value.kind == kind
    assert not runner.called("nmcli", "--wait")
    assert not runner.called("nmcli", "connection", "delete")


def test_refusal_is_by_what_the_profile_is_not_by_its_name(tmp_path: Path) -> None:
    # Named like ours, but NetworkManager says it is wired: still untouchable.
    sneaky = Profile("u-sneaky", "canary-wifi-sneaky", kind="802-3-ethernet")
    nm, runner = cli(tmp_path, [sneaky])
    with pytest.raises(WifiOpError) as caught:
        nm.forget_network("u-sneaky")
    assert caught.value.kind == KIND_REFUSED
    assert not runner.called("nmcli", "connection", "delete")


# --- forget -----------------------------------------------------------------


def test_forget_deletes_a_foreign_wifi_profile_by_uuid(tmp_path: Path) -> None:
    nm, runner = cli(tmp_path)
    nm.forget_network("u-pre")
    assert runner.called("nmcli", "connection", "delete", "uuid", "u-pre")
    assert sum(1 for c in runner.calls if c[1:3] == ["connection", "delete"]) == 1


def test_forget_failure_is_reported(tmp_path: Path) -> None:
    extra: dict[tuple[str, ...], RunResult] = {
        ("nmcli", "connection", "delete"): RunResult(1, "", "Error: permission denied")
    }
    nm, _ = cli(tmp_path, extra=extra)
    with pytest.raises(WifiOpError, match="permission denied") as caught:
        nm.forget_network("u-pre")
    assert caught.value.kind == KIND_INTERNAL


# --- order ------------------------------------------------------------------


def _modifies(runner: FakeRunner) -> list[tuple[str, int]]:
    return [
        (call[4], int(call[6]))
        for call in runner.calls
        if call[:3] == ["nmcli", "connection", "modify"]
    ]


def test_order_sets_descending_priorities_first_wins(tmp_path: Path) -> None:
    nm, runner = cli(tmp_path)
    first, second, third, fourth = "u-pre", STARLINK.uuid, HOTSPOT.uuid, "u-off"
    nm.set_order([first, second, third, fourth])
    assert _modifies(runner) == [(first, 100), (second, 90), (third, 80), (fourth, 70)]
    assert runner.called(
        "nmcli", "connection", "modify", "uuid", first, "connection.autoconnect-priority", "100"
    )


def test_order_with_many_networks_stays_distinct_and_positive(tmp_path: Path) -> None:
    profiles = [Profile(f"u-{i}", f"p{i}", ssid=f"Net{i:02d}") for i in range(25)]
    nm, runner = cli(tmp_path, profiles)
    nm.set_order([p.uuid for p in profiles])
    priorities = [priority for _, priority in _modifies(runner)]
    assert priorities == sorted(priorities, reverse=True)
    assert len(set(priorities)) == 25 and min(priorities) >= 1 and priorities[0] == 100


def test_order_must_be_exactly_the_saved_set(tmp_path: Path) -> None:
    nm, runner = cli(tmp_path)
    everyone = ["u-pre", STARLINK.uuid, HOTSPOT.uuid, "u-off"]
    for bad in (
        everyone[:-1],  # one missing
        [*everyone, "u-extra"],  # one unknown
        [*everyone, "u-wired"],  # a profile that is not a WiFi client
        [*everyone[:-1], everyone[0]],  # a duplicate
        [],
        "u-pre",
        [1, 2],
    ):
        with pytest.raises(WifiOpError) as caught:
            nm.set_order(bad)
        assert caught.value.kind == KIND_INVALID
    assert _modifies(runner) == []


def test_order_failure_puts_the_old_priorities_back(tmp_path: Path) -> None:
    profiles = [
        Profile("u-a", "a", ssid="A", priority=7),
        Profile("u-b", "b", ssid="B", priority=3),
    ]
    extra: dict[tuple[str, ...], RunResult] = {
        (
            "nmcli",
            "connection",
            "modify",
            "uuid",
            "u-b",
            "connection.autoconnect-priority",
            "90",
        ): RunResult(1, "", "Error: nope")
    }
    nm, runner = cli(tmp_path, profiles, extra=extra)
    with pytest.raises(WifiOpError, match="nope") as caught:
        nm.set_order(["u-a", "u-b"])
    assert caught.value.kind == KIND_INTERNAL
    assert _modifies(runner)[-1] == ("u-a", 7)


# --- one operation at a time ------------------------------------------------


class _BlockingRunner(FakeRunner):
    """Holds ``connection up`` open until released, like a 30 s join."""

    def __init__(self, responses: dict[tuple[str, ...], RunResult]) -> None:
        super().__init__(responses)
        self.entered = threading.Event()
        self.release = threading.Event()

    def __call__(self, argv: Sequence[str], *, timeout: float) -> RunResult:
        if list(argv[:2]) == ["nmcli", "--wait"] or "up" in list(argv[:5]):
            self.entered.set()
            assert self.release.wait(10), "the test never released the join"
        return super().__call__(argv, timeout=timeout)


def test_operations_do_not_overlap_a_join_in_progress(tmp_path: Path) -> None:
    runner = _BlockingRunner(nm_responses(EVERYTHING))
    nm = NetworkManagerCli(runner, connections_dir=tmp_path)
    joining = threading.Thread(target=lambda: nm.connect_network("u-pre", timeout_s=5))
    forgetting = threading.Thread(target=lambda: nm.forget_network(HOTSPOT.uuid))
    joining.start()
    assert runner.entered.wait(5)
    forgetting.start()
    forgetting.join(0.3)
    assert forgetting.is_alive()  # waiting its turn
    assert not runner.called("nmcli", "connection", "delete")
    runner.release.set()
    joining.join(5)
    forgetting.join(5)
    assert runner.called("nmcli", "connection", "delete", "uuid", HOTSPOT.uuid)


def test_the_listing_is_valid_json_for_the_socket(tmp_path: Path) -> None:
    nm, _ = cli(tmp_path)
    assert json.loads(json.dumps(nm.wifi_state(rescan=True).to_dict()))["interface"] == "wlan0"


@pytest.mark.parametrize("join", [True, False])
def test_a_passphrase_nmcli_echoes_back_is_scrubbed_from_the_failure_and_the_log(
    tmp_path: Path, caplog: pytest.LogCaptureFixture, join: bool
) -> None:
    secret = "S3cretPassphrase!"
    extra: dict[tuple[str, ...], RunResult] = {
        ("nmcli", "connection", "load"): RunResult(1, "", f"invalid connection: psk={secret}"),
        ("nmcli", "--wait"): RunResult(4, "", f"Secrets were required: {secret}"),
    }
    nm, _ = cli(tmp_path, [PRECONFIGURED], extra=extra)
    with caplog.at_level(logging.DEBUG), pytest.raises(WifiOpError) as caught:
        nm.add_network("Starlink", secret, hidden=False, join=join, timeout_s=5)
    assert "***" in caught.value.detail and secret not in caught.value.detail
    assert secret not in caplog.text
    # And the scrubbing ends with the install: the next listing is untouched.
    assert nm._secret == ""
