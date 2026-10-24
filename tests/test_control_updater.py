"""The software-update file contracts (phone-ui P3.1, ``src/control/updater.py``)."""

from __future__ import annotations

import json
import os
import re
import subprocess
from pathlib import Path
from typing import Any, cast

import pytest

from src.collection import tuning_profile, tx_gate
from src.control import updater
from src.control.updater import (
    EXIT_MEANINGS,
    LOST_AFTER_S,
    PICKUP_TIMEOUT_S,
    REQUEST_MAX_AGE_S,
    UpdatePaths,
    UpdateRequestError,
    active_work,
    has_default_route,
    new_request,
    parse_request,
    pending_request,
    read_regular_file,
    read_status,
    utc_iso,
    write_request,
    write_status,
)

REPO_ROOT = Path(__file__).resolve().parents[1]
NOW = 1_800_000_000.0


def _paths(tmp_path: Path) -> UpdatePaths:
    return UpdatePaths(
        request=tmp_path / "update_request",
        taken=tmp_path / "update_request.taken",
        status=tmp_path / "update_status",
        trigger_unit=tmp_path / "canary-update.path",
        proc_net=tmp_path / "net",
    )


# -- the request -------------------------------------------------------------


def test_a_written_request_parses_back(tmp_path: Path) -> None:
    request = new_request("update", full=True, now=NOW)
    write_request(request, tmp_path / "update_request")

    parsed = parse_request((tmp_path / "update_request").read_text(), now=NOW + 5)

    assert parsed == request
    assert oct((tmp_path / "update_request").stat().st_mode & 0o777) == "0o644"


@pytest.mark.parametrize(
    ("body", "error"),
    [
        ("not json", "not JSON"),
        ("[]", "not a JSON object"),
        ({"action": "reflash"}, "action must be one of"),
        ({"action": "check", "request_id": "../x"}, "request_id"),
        ({"action": "check", "request_id": "a1", "requested_at": "yesterday"}, "requested_at"),
        ({"action": "check", "request_id": "a1", "requested_at": "2027-01-15T08:00:00"}, "zone"),
        ({"extra": 1}, "unknown request fields: extra"),
        ({"options": {"force": True}}, "unknown options: force"),
        ({"options": {"full": "yes"}}, "options.full"),
        ({"action": "check", "options": {"full": True}}, "only to the update action"),
    ],
)
def test_a_malformed_request_is_refused(body: object, error: str) -> None:
    base = {"action": "update", "request_id": "a1", "requested_at": utc_iso(NOW)}
    raw = body if isinstance(body, str) else json.dumps({**base, **cast(dict[str, Any], body)})
    with pytest.raises(UpdateRequestError, match=error):
        parse_request(raw, now=NOW)


def test_a_stale_request_is_ignored() -> None:
    raw = json.dumps(new_request("check", now=NOW).to_dict())
    parse_request(raw, now=NOW + REQUEST_MAX_AGE_S - 1)
    with pytest.raises(UpdateRequestError, match="s old"):
        parse_request(raw, now=NOW + REQUEST_MAX_AGE_S + 1)


def test_force_is_not_an_option() -> None:
    """Ending a capture from a button is deliberately impossible."""
    with pytest.raises(TypeError):
        new_request("update", force=True)  # type: ignore[call-arg]


def test_root_never_follows_a_symlink_or_reads_a_big_file(tmp_path: Path) -> None:
    secret = tmp_path / "secret"
    secret.write_text("root:x:0:0")
    link = tmp_path / "update_request.taken"
    link.symlink_to(secret)
    with pytest.raises(UpdateRequestError, match="could not be opened"):
        read_regular_file(link)

    link.unlink()
    link.mkdir()
    with pytest.raises(UpdateRequestError):
        read_regular_file(link)

    link.rmdir()
    link.write_text("x" * 5000)
    with pytest.raises(UpdateRequestError, match="limit"):
        read_regular_file(link)

    with pytest.raises(FileNotFoundError):
        read_regular_file(tmp_path / "absent")


def test_pending_request_is_stuck_after_the_pickup_timeout(tmp_path: Path) -> None:
    paths = _paths(tmp_path)
    assert pending_request(paths, now=NOW) is None
    request = new_request("check", now=NOW)
    write_request(request, paths.request)

    waiting = pending_request(paths, now=NOW + 5)
    stuck = pending_request(paths, now=NOW + PICKUP_TIMEOUT_S + 1)

    assert waiting == {
        "action": "check",
        "request_id": request.request_id,
        "age_s": 5.0,
        "stuck": False,
    }
    assert stuck is not None and stuck["stuck"] is True


def test_a_garbage_pending_request_is_replaceable(tmp_path: Path) -> None:
    paths = _paths(tmp_path)
    paths.request.write_text("{")
    pending = pending_request(paths, now=NOW)
    assert pending is not None and pending["stuck"] is True and "not JSON" in pending["error"]


# -- the status --------------------------------------------------------------


def test_no_status_file_is_idle(tmp_path: Path) -> None:
    assert read_status(tmp_path / "update_status") == {
        "schema": 1,
        "state": "idle",
        "last_check": None,
    }


def test_a_malformed_status_is_unknown_not_idle(tmp_path: Path) -> None:
    path = tmp_path / "update_status"
    path.write_text("{")
    assert read_status(path)["state"] == "unknown"
    path.write_text(json.dumps({"state": "exploded"}))
    assert read_status(path)["state"] == "unknown"


def test_an_updating_status_without_a_heartbeat_is_lost(tmp_path: Path) -> None:
    path = tmp_path / "update_status"
    write_status({"state": "updating", "updated_at": utc_iso(NOW), "phase": "Fetching"}, path)

    assert read_status(path, now=NOW + 30)["state"] == "updating"
    lost = read_status(path, now=NOW + LOST_AFTER_S + 1)

    assert lost["state"] == "lost"
    assert lost["lost_from"] == "updating"
    assert "journalctl -u canary-update" in lost["message"]
    assert not updater.in_progress(lost), "a lost runner does not block a new request"


def test_a_finished_status_never_goes_lost(tmp_path: Path) -> None:
    path = tmp_path / "update_status"
    write_status({"state": "done", "updated_at": utc_iso(NOW)}, path)
    assert read_status(path, now=NOW + 10 * LOST_AFTER_S)["state"] == "done"


def test_status_write_replaces_a_planted_symlink_instead_of_following_it(tmp_path: Path) -> None:
    target = tmp_path / "passwd"
    target.write_text("untouched")
    path = tmp_path / "update_status"
    path.symlink_to(target)

    write_status({"state": "done"}, path)

    assert target.read_text() == "untouched"
    assert not path.is_symlink()
    assert json.loads(path.read_text()) == {"state": "done"}
    assert oct(path.stat().st_mode & 0o777) == "0o644", "the daemon must be able to read it"
    assert [p.name for p in tmp_path.iterdir() if p.name.startswith(".")] == []


# -- what the daemon checks first --------------------------------------------


_ROUTE_HEADER = "Iface\tDestination\tGateway\tFlags\tRefCnt\tUse\tMetric\tMask\n"


def test_default_route_from_proc_net(tmp_path: Path) -> None:
    net = tmp_path / "net"
    net.mkdir()
    assert has_default_route(net) is None, "nothing readable is unknown, not 'no'"

    (net / "route").write_text(_ROUTE_HEADER + "wlan0\t0001A8C0\t00000000\t0001\t0\t0\t600\n")
    assert has_default_route(net) is False

    (net / "route").write_text(_ROUTE_HEADER + "wlan0\t00000000\t0101A8C0\t0003\t0\t0\t600\n")
    assert has_default_route(net) is True


def test_ipv6_default_route_counts_but_not_the_loopback_reject(tmp_path: Path) -> None:
    net = tmp_path / "net"
    net.mkdir()
    (net / "route").write_text(_ROUTE_HEADER)
    zero = "0" * 32
    reject = f"{zero} 00 {zero} 00 {zero} ffffffff 00000001 00000000 00200200       lo\n"
    (net / "ipv6_route").write_text(reject)
    assert has_default_route(net) is False

    gateway = "fe800000000000000000000000000001"
    via_ra = f"{zero} 00 {zero} 00 {gateway} 00000400 00000001 00000000 00450003 wlan0\n"
    (net / "ipv6_route").write_text(reject + via_ra)
    assert has_default_route(net) is True


def test_active_work_names_a_capture_and_an_ecu_backup(monkeypatch: pytest.MonkeyPatch) -> None:
    assert active_work() == []
    monkeypatch.setattr(
        tuning_profile,
        "snapshot",
        lambda: {"active": True, "name": "fuel-map", "session_dir": "_capture-fuel-map-x"},
    )
    tx_gate.claim_ecm_session("rom_backup")
    try:
        blockers = active_work()
    finally:
        tx_gate.release_ecm_session("rom_backup")

    assert len(blockers) == 2
    assert "capture 'fuel-map' is recording (_capture-fuel-map-x)" in blockers[0]
    assert "ECU backup" in blockers[1]


# -- the exit codes stay canary-update's -------------------------------------


def test_exit_meanings_are_update_canary_shs_usage_word_for_word() -> None:
    usage = subprocess.run(
        ["bash", str(REPO_ROOT / "scripts" / "update_canary.sh"), "--help"],
        capture_output=True,
        text=True,
        check=True,
        env={**os.environ, "CANARY_HOME": "/nonexistent"},
    ).stdout
    block = usage.split("Exit codes:\n", 1)[1].split("\n\n", 1)[0]
    documented: dict[int, str] = {}
    code = None
    for line in block.splitlines():
        match = re.match(r"^  (\d+)  (.*)$", line)
        if match:
            code = int(match.group(1))
            documented[code] = match.group(2)
        elif code is not None:
            documented[code] += " " + line.strip()

    assert EXIT_MEANINGS == documented
