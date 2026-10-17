"""``src.companion.discovery``: finding CANary daemons on the network, from facts alone."""

from __future__ import annotations

import ipaddress
from typing import Any

import pytest

from src.companion import discovery as d

IDENTITY = {
    "product": "canary",
    "api": "identity/1",
    "hostname": "canary-8974",
    "host_kind": "raspberry_pi",
    "profile": "nissan-xterra-2008",
    "version": "0.3.0",
    "git_sha": "abc1234",
    "dashboard_port": 8000,
}


def fetcher(answers: dict[str, tuple[int, Any]]):  # noqa: ANN201 - a test double
    calls: list[str] = []

    def fetch(url: str, timeout: float) -> tuple[int, Any]:
        calls.append(url)
        return answers.get(url, (0, None))

    fetch.calls = calls  # type: ignore[attr-defined]
    return fetch


def test_base_url_of_accepts_a_host_a_host_port_and_a_url() -> None:
    assert d.base_url_of("canary.local") == "http://canary.local:8000"
    assert d.base_url_of("192.168.0.42:8080") == "http://192.168.0.42:8080"
    assert d.base_url_of("http://pi:8000/") == "http://pi:8000"
    assert d.host_of("http://192.168.0.42:8000") == "192.168.0.42"


def test_a_daemon_is_recognised_by_its_identity_and_an_older_one_by_its_status() -> None:
    new = fetcher({"http://a:8000/identity": (200, IDENTITY)})
    identity = d.probe("http://a:8000", fetch=new)
    assert identity is not None
    assert (identity.hostname, identity.host_kind, identity.profile) == (
        "canary-8974",
        "raspberry_pi",
        "nissan-xterra-2008",
    )
    assert identity.source_path == "/identity"

    old = fetcher(
        {
            "http://b:8000/identity": (404, None),
            "http://b:8000/status?view=compact": (200, {"vehicle_state": {"state": "bench"}}),
        }
    )
    identity = d.probe("http://b:8000", fetch=old)
    assert identity is not None
    assert (identity.hostname, identity.host_kind, identity.source_path) == (
        "b",
        "unknown",
        "/status",
    )


def test_something_else_on_port_8000_is_not_a_canary() -> None:
    other = fetcher(
        {
            "http://c:8000/identity": (200, {"product": "nginx"}),
            "http://c:8000/status?view=compact": (200, {"uptime": 3}),
        }
    )
    assert d.probe("http://c:8000", fetch=other) is None
    silent = fetcher({})
    assert d.probe("http://d:8000", fetch=silent) is None
    # Nothing answered /identity at all: the status fallback is not even tried.
    assert silent.calls == ["http://d:8000/identity"]  # type: ignore[attr-defined]


def test_interface_networks_keeps_lan_prefixes_and_skips_loopback_docker_and_link_local() -> None:
    text = "\n".join(
        [
            "1: lo    inet 127.0.0.1/8 scope host lo",
            "2: eth0    inet 169.254.83.107/16 brd 169.254.255.255 scope link eth0",
            "3: eth3    inet 192.168.0.216/24 brd 192.168.0.255 scope global eth3",
            "4: docker0    inet 172.17.0.1/16 brd 172.17.255.255 scope global docker0",
            "5: wlan0    inet 10.0.0.5/16 scope global wlan0",
            "6: eth3    inet 192.168.0.217/24 scope global secondary eth3",
            "7: tun0    inet 10.8.0.2/32 scope global tun0",
        ]
    )
    assert d.interface_networks(text) == [ipaddress.IPv4Network("192.168.0.0/24")]


def test_sweep_candidates_are_bounded() -> None:
    nets = [ipaddress.IPv4Network("192.168.0.0/24"), ipaddress.IPv4Network("10.1.0.0/23")]
    out = d.sweep_candidates(nets, limit=300)
    assert len(out) == 300
    assert out[0] == ("http://192.168.0.1:8000", "sweep")
    assert out[253] == ("http://192.168.0.254:8000", "sweep")
    assert out[254] == ("http://10.1.0.1:8000", "sweep")


def test_candidates_are_env_then_names_then_wired_then_sweep_without_duplicates() -> None:
    out = d.candidates(
        environ={"CANARY_HOST": "http://192.168.0.42:8000"},
        names=("canary.local", "nowhere.local"),
        resolve=lambda name: name == "canary.local",
        networks=[ipaddress.IPv4Network("192.168.0.40/30")],
    )
    assert out == [
        ("http://192.168.0.42:8000", "env"),
        ("http://canary.local:8000", "name"),
        ("http://192.168.137.2:8000", "wired"),
        ("http://192.168.0.41:8000", "sweep"),
    ]


def test_discover_probes_in_parallel_and_keeps_one_entry_per_daemon_best_source_first() -> None:
    fetch = fetcher(
        {
            "http://192.168.0.42:8000/identity": (200, IDENTITY),
            "http://canary.local:8000/identity": (200, IDENTITY),
            "http://192.168.0.43:8000/identity": (200, {**IDENTITY, "hostname": "canary-0001"}),
        }
    )
    found = d.discover(
        fetch=fetch,
        environ={},
        names=("canary.local",),
        resolve=lambda _n: True,
        networks=[ipaddress.IPv4Network("192.168.0.40/29")],
    )
    assert [(f.identity.hostname, f.source, f.base_url) for f in found] == [
        ("canary-8974", "name", "http://canary.local:8000"),
        ("canary-0001", "sweep", "http://192.168.0.43:8000"),
    ]
    assert found[0].to_dict()["identity"]["profile"] == "nissan-xterra-2008"
    assert "canary-8974 (nissan-xterra-2008) at http://canary.local:8000 [name]" == found[0].label()


def test_choose_takes_one_lists_several_and_names_none() -> None:
    one = d.DiscoveredPi(
        "http://a:8000",
        "a",
        "wired",
        d.DaemonIdentity.from_payload(IDENTITY, path="/identity", host="a"),
    )
    two = d.DiscoveredPi(
        "http://b:8000",
        "b",
        "sweep",
        d.DaemonIdentity.from_payload(
            {**IDENTITY, "hostname": "canary-0001"}, path="/identity", host="b"
        ),
    )
    assert d.choose([]) == (None, "no CANary daemon found on the network")
    chosen, why = d.choose([one])
    assert chosen is one and why.startswith("found canary-8974")
    chosen, why = d.choose([one, two])
    assert chosen is None and why.startswith("2 CANary daemons found") and "--pi-host" in why


@pytest.mark.parametrize("payload", [{"product": "canary"}, {"product": "other", "hostname": "x"}])
def test_an_identity_without_a_product_match_is_refused_a_status_without_vehicle_state_too(
    payload: dict[str, Any],
) -> None:
    if payload["product"] == "canary":
        identity = d.DaemonIdentity.from_payload(payload, path="/identity", host="h")
        assert identity.hostname == "h" and identity.host_kind == "unknown"
    else:
        with pytest.raises(ValueError, match="not a CANary identity"):
            d.DaemonIdentity.from_payload(payload, path="/identity", host="h")
    with pytest.raises(ValueError, match="no vehicle_state"):
        d.DaemonIdentity.from_payload({"free_mb": 1}, path="/status?view=compact", host="h")
