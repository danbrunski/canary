"""The network-control socket (N5.1): protocol, who may use it, and that a
passphrase goes nowhere."""

from __future__ import annotations

import asyncio
import contextlib
import grp
import json
import logging
import os
import pwd
import signal
import socket
import stat
import threading
from collections.abc import AsyncIterator
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from src.provisioning import netsock, service
from src.provisioning.netsock import (
    KIND_UNAUTHORIZED,
    NetworkControlServer,
    describe,
    group_policy,
    handle_request,
    peer_credentials,
)
from src.provisioning.network import (
    KIND_INTERNAL,
    KIND_INVALID,
    KIND_REFUSED,
    SavedNetwork,
    WifiOpError,
    WifiState,
    profile_filename,
)
from tests.test_provisioning_wifi import PRECONFIGURED, STARLINK, cli

SECRET = "S3cretPassphrase!"
OWN_GROUP = grp.getgrgid(os.getgid()).gr_name


def _state() -> WifiState:
    return WifiState(
        [
            SavedNetwork(
                "u-1",
                "canary-wifi-Starlink",
                "Starlink",
                50,
                True,
                False,
                "wpa-psk",
                True,
                True,
                80,
            )
        ],
        [],
        None,
        "wlan0",
    )


class FakeOps:
    """Records every call; ``fail`` makes the next operation raise."""

    def __init__(self) -> None:
        self.calls: list[tuple[str, dict[str, Any]]] = []
        self.fail: Exception | None = None
        self.running = 0
        self.max_running = 0
        self.gate: threading.Event | None = None
        self._lock = threading.Lock()

    def _enter(self, name: str, **kw: Any) -> None:
        with self._lock:
            self.calls.append((name, kw))
            self.running += 1
            self.max_running = max(self.max_running, self.running)
        try:
            if self.gate is not None:
                self.gate.wait(5)
            if self.fail is not None:
                raise self.fail
        finally:
            with self._lock:
                self.running -= 1

    def wifi_state(self, *, rescan: bool = False) -> WifiState:
        self._enter("scan" if rescan else "list")
        return _state()

    def add_network(
        self, ssid: object, password: object, *, hidden: bool, join: bool, timeout_s: float
    ) -> str:
        self._enter("add", ssid=ssid, hidden=hidden, join=join, timeout_s=timeout_s)
        return "u-1"

    def connect_network(self, con_id: object, *, timeout_s: float) -> None:
        self._enter("connect", id=con_id)

    def forget_network(self, con_id: object) -> None:
        self._enter("forget", id=con_id)

    def set_order(self, ids: object) -> None:
        self._enter("order", ids=ids)


async def listening(path: Path) -> None:
    """Wait until something accepts connections at ``path``."""
    for _ in range(300):
        probe = socket.socket(socket.AF_UNIX)
        try:
            probe.connect(str(path))
            return
        except OSError:
            await asyncio.sleep(0.01)
        finally:
            probe.close()
    raise AssertionError(f"nothing listening at {path}")


@contextlib.asynccontextmanager
async def serving(
    ops: Any, tmp_path: Path, **kwargs: Any
) -> AsyncIterator[tuple[NetworkControlServer, Path]]:
    path = tmp_path / "n.sock"
    kwargs.setdefault("group", OWN_GROUP)
    server = NetworkControlServer(ops, path=path, **kwargs)
    task = asyncio.create_task(server.serve())
    await listening(path)
    try:
        yield server, path
    finally:
        task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await task


async def ask(path: Path, *lines: bytes | dict[str, Any]) -> list[dict[str, Any]]:
    """Send each request on one connection; read one reply line per request."""
    reader, writer = await asyncio.open_unix_connection(str(path))
    replies = []
    try:
        for line in lines:
            data = line if isinstance(line, bytes) else json.dumps(line).encode()
            writer.write(data + b"\n")
            await writer.drain()
            replies.append(json.loads(await asyncio.wait_for(reader.readline(), 5)))
    finally:
        writer.close()
    return replies


# --- requests -----------------------------------------------------------------


async def test_every_operation_answers_with_the_state(tmp_path: Path) -> None:
    ops = FakeOps()
    async with serving(ops, tmp_path) as (_, path):
        replies = await ask(
            path,
            {"op": "list"},
            {"op": "scan"},
            {"op": "add", "ssid": "Starlink", "password": None, "hidden": False, "join": True},
            {"op": "connect", "id": "u-1"},
            {"op": "forget", "id": "u-1"},
            {"op": "order", "ids": ["u-1"]},
        )
    assert all(r["ok"] for r in replies)
    # Each change is followed by a read of the state it produced.
    assert [name for name, _ in ops.calls] == [
        "list",
        "scan",
        "add",
        "list",
        "connect",
        "list",
        "forget",
        "list",
        "order",
        "list",
    ]
    assert replies[0]["wifi"]["networks"][0]["ssid"] == "Starlink"
    assert replies[2]["network"]["id"] == "u-1"
    assert replies[2]["wifi"]["interface"] == "wlan0"
    assert ops.calls[2][1] == {
        "ssid": "Starlink",
        "hidden": False,
        "join": True,
        "timeout_s": netsock.DEFAULT_JOIN_TIMEOUT_S,
    }


async def test_join_is_never_defaulted(tmp_path: Path) -> None:
    ops = FakeOps()
    async with serving(ops, tmp_path) as (_, path):
        (reply,) = await ask(path, {"op": "add", "ssid": "Starlink", "password": "x"})
    assert reply["ok"] is False and reply["error"] == KIND_INVALID
    assert "join" in reply["detail"]
    assert ops.calls == []


@pytest.mark.parametrize(
    "request_line",
    [
        {"op": "nope"},
        {},
        {"op": ["list"]},
        {"op": "add", "ssid": "x", "join": "yes"},
        {"op": "add", "ssid": "x", "join": True, "hidden": 1},
        b"not json at all",
        b"[1, 2]",
        b'"list"',
    ],
)
async def test_malformed_requests_are_invalid_and_do_nothing(
    tmp_path: Path, request_line: bytes | dict[str, Any]
) -> None:
    ops = FakeOps()
    async with serving(ops, tmp_path) as (_, path):
        (reply,) = await ask(path, request_line)
    assert (reply["ok"], reply["error"]) == (False, KIND_INVALID)
    assert ops.calls == []


async def test_an_oversized_request_is_refused_and_the_connection_closed(tmp_path: Path) -> None:
    ops = FakeOps()
    async with serving(ops, tmp_path) as (_, path):
        reader, writer = await asyncio.open_unix_connection(str(path))
        writer.write(b"x" * (netsock.MAX_REQUEST_BYTES * 2) + b"\n")
        with contextlib.suppress(ConnectionError):
            await writer.drain()
        reply = json.loads(await asyncio.wait_for(reader.readline(), 5))
        assert await asyncio.wait_for(reader.read(), 5) == b""
        writer.close()
    assert reply["error"] == KIND_INVALID and ops.calls == []


async def test_a_failing_operation_reports_its_kind(tmp_path: Path) -> None:
    ops = FakeOps()
    ops.fail = WifiOpError(KIND_REFUSED, "held by another profile")
    async with serving(ops, tmp_path) as (_, path):
        (reply,) = await ask(path, {"op": "forget", "id": "u-1"})
    assert reply == {"ok": False, "error": KIND_REFUSED, "detail": "held by another profile"}


async def test_a_crash_is_an_internal_error_not_a_dropped_connection(tmp_path: Path) -> None:
    ops = FakeOps()
    ops.fail = RuntimeError("boom")
    async with serving(ops, tmp_path) as (_, path):
        replies = await ask(path, {"op": "list"}, {"op": "list"})
    assert [r["error"] for r in replies] == [KIND_INTERNAL, KIND_INTERNAL]
    assert "boom" in replies[0]["detail"]


async def test_operations_run_one_at_a_time(tmp_path: Path) -> None:
    ops = FakeOps()
    ops.gate = threading.Event()
    async with serving(ops, tmp_path) as (_, path):
        pending = [asyncio.create_task(ask(path, {"op": "list"})) for _ in range(4)]
        await asyncio.sleep(0.3)
        assert ops.running == 1 and len(ops.calls) == 1  # the rest are waiting their turn
        ops.gate.set()
        results = await asyncio.gather(*pending)
    assert all(r[0]["ok"] for r in results)
    assert ops.max_running == 1 and len(ops.calls) == 4


# --- the socket file ------------------------------------------------------------


async def test_socket_is_group_only_and_removed_on_stop(tmp_path: Path) -> None:
    ops = FakeOps()
    async with serving(ops, tmp_path) as (_, path):
        info = path.stat()
        assert stat.S_ISSOCK(info.st_mode)
        assert stat.S_IMODE(info.st_mode) == 0o660
        assert info.st_gid == os.getgid()
    assert not path.exists()


async def test_umask_is_restored_after_binding(tmp_path: Path) -> None:
    before = os.umask(0o022)
    try:
        async with serving(FakeOps(), tmp_path):
            assert os.umask(0o022) == 0o022
    finally:
        os.umask(before)


async def test_a_missing_group_leaves_a_root_only_socket(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    with caplog.at_level(logging.ERROR, logger="src.provisioning.netsock"):
        async with serving(FakeOps(), tmp_path, group="no-such-group-canary-test") as (_, path):
            assert stat.S_IMODE(path.stat().st_mode) == 0o600
    assert "root-only" in caplog.text


async def test_a_stale_socket_file_is_replaced(tmp_path: Path) -> None:
    stale = tmp_path / "n.sock"
    old = socket.socket(socket.AF_UNIX)
    old.bind(str(stale))
    old.close()  # leaves the file behind, as a killed service would
    async with serving(FakeOps(), tmp_path) as (_, path):
        (reply,) = await ask(path, {"op": "list"})
    assert reply["ok"]


async def test_a_regular_file_at_the_path_is_not_deleted(tmp_path: Path) -> None:
    (tmp_path / "n.sock").write_text("someone's file")
    server = NetworkControlServer(FakeOps(), path=tmp_path / "n.sock", group=OWN_GROUP)
    with pytest.raises(OSError):
        await server.serve()
    assert (tmp_path / "n.sock").read_text() == "someone's file"


# --- who may ask -----------------------------------------------------------------


def test_peer_credentials_come_from_the_kernel() -> None:
    left, right = socket.socketpair()
    try:
        assert peer_credentials(left) == (os.getpid(), os.getuid(), os.getgid())
    finally:
        left.close()
        right.close()


async def test_a_refused_peer_is_told_so_and_nothing_runs(tmp_path: Path) -> None:
    ops = FakeOps()
    seen: list[tuple[int, int, int]] = []

    def refuse(pid: int, uid: int, gid: int) -> bool:
        seen.append((pid, uid, gid))
        return False

    async with serving(ops, tmp_path, peer_policy=refuse) as (_, path):
        (reply,) = await ask(path, {"op": "forget", "id": "u-1"})
    assert reply["error"] == KIND_UNAUTHORIZED
    # (the readiness probe in `serving` knocks first, and is refused too)
    assert seen and set(seen) == {(os.getpid(), os.getuid(), os.getgid())}
    assert ops.calls == []


async def test_the_default_policy_admits_a_member_of_the_group(tmp_path: Path) -> None:
    async with serving(FakeOps(), tmp_path, group=OWN_GROUP) as (_, path):
        (reply,) = await ask(path, {"op": "list"})
    assert reply["ok"]


@pytest.fixture
def fake_user_db(monkeypatch: pytest.MonkeyPatch) -> dict[str, Any]:
    db: dict[str, Any] = {"gid": 990, "members": {1000: [990, 27]}, "users": {1000: "pi"}}

    def getgrnam(name: str) -> SimpleNamespace:
        if name != "canary":
            raise KeyError(name)
        return SimpleNamespace(gr_gid=db["gid"])

    def getpwuid(uid: int) -> SimpleNamespace:
        if uid not in db["users"]:
            raise KeyError(uid)
        return SimpleNamespace(pw_name=db["users"][uid])

    monkeypatch.setattr(grp, "getgrnam", getgrnam)
    monkeypatch.setattr(pwd, "getpwuid", getpwuid)
    monkeypatch.setattr(os, "getgrouplist", lambda name, gid: [gid, *db["members"].get(1000, [])])
    return db


def test_policy_admits_root_the_group_and_its_members(fake_user_db: dict[str, Any]) -> None:
    allowed = group_policy("canary")
    assert allowed(1, 0, 0)  # root, whatever its group
    assert allowed(1, 1000, 990)  # primary group
    assert allowed(1, 1000, 1000)  # supplementary group, from the user database


def test_policy_refuses_everyone_else(fake_user_db: dict[str, Any]) -> None:
    allowed = group_policy("canary")
    fake_user_db["members"] = {1000: [27]}
    assert not allowed(1, 1000, 1000)  # a user outside the group
    assert not allowed(1, 4242, 4242)  # no passwd entry
    assert not group_policy("not-canary")(1, 1000, 990)  # the group does not exist


# --- a passphrase goes nowhere -------------------------------------------------


def test_describe_names_the_operation_and_the_network_only() -> None:
    assert describe({"op": "add", "ssid": "Starlink", "password": SECRET, "join": True}) == (
        "add ssid='Starlink'"
    )
    assert describe({"op": "forget", "id": "u-1"}) == "forget id='u-1'"
    assert describe({"op": SECRET}) == "unknown op"
    assert describe([SECRET]) == "malformed request"
    assert SECRET not in describe({"op": "add", "ssid": 5, "password": SECRET})


def _everything_said(replies: list[dict[str, Any]], caplog: pytest.LogCaptureFixture) -> str:
    formatted = "\n".join(
        r.getMessage() + (caplog.handler.format(r) if r.exc_info else "") for r in caplog.records
    )
    return json.dumps(replies) + caplog.text + formatted


async def test_a_passphrase_is_never_logged_or_returned(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    ops = FakeOps()
    replies: list[dict[str, Any]] = []
    add = {"op": "add", "ssid": "Starlink", "password": SECRET, "hidden": False, "join": True}
    with caplog.at_level(logging.DEBUG):
        async with serving(ops, tmp_path) as (_, path):
            replies += await ask(path, add)  # succeeds
            ops.fail = WifiOpError("join_failed", f"nmcli echoed {SECRET} back")  # a leaky error
            replies += await ask(path, add)
            ops.fail = RuntimeError(f"crash carrying {SECRET}")  # a crash
            replies += await ask(path, add)
            ops.fail = None
            # Malformed requests that carry it: not JSON, wrong shape, bad flag.
            replies += await ask(path, f"{{not json {SECRET}".encode())
            replies += await ask(path, {"op": "add", "ssid": "x", "password": SECRET, "join": 7})
            replies += await ask(path, {"op": SECRET, "password": SECRET})
    assert len(replies) == 6
    assert ops.calls[0][1].keys() == {"ssid", "hidden", "join", "timeout_s"}  # never in a kwarg log
    assert SECRET not in _everything_said(replies, caplog)
    assert "***" in replies[1]["detail"]


async def test_a_passphrase_is_never_logged_through_the_real_operations(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    keyfiles = tmp_path / "nm"
    keyfiles.mkdir()
    # The fixture "NetworkManager" already lists Starlink, so the read-back after an add finds it.
    nm, runner = cli(keyfiles, [PRECONFIGURED, STARLINK])
    replies: list[dict[str, Any]] = []
    with caplog.at_level(logging.DEBUG):
        async with serving(nm, tmp_path) as (_, path):
            for join in (False, True):
                replies += await ask(
                    path,
                    {
                        "op": "add",
                        "ssid": "Starlink",
                        "password": SECRET,
                        "hidden": False,
                        "join": join,
                    },
                )
            replies += await ask(
                path, {"op": "add", "ssid": "Starlink", "password": "short", "join": False}
            )
            replies += await ask(
                path, {"op": "add", "ssid": "Corp", "password": SECRET, "join": True}
            )
    assert replies[0]["ok"] and replies[1]["ok"]
    assert [r.get("error") for r in replies[2:]] == [KIND_INVALID, KIND_REFUSED]
    # It reached the keyfile (that is its job) and nothing else.
    assert f"psk={SECRET}" in (tmp_path / "nm" / profile_filename("Starlink")).read_text()
    assert not any(SECRET in " ".join(call) for call in runner.calls)
    assert SECRET not in _everything_said(replies, caplog)


def test_handle_request_answers_even_a_crash() -> None:
    ops = FakeOps()
    ops.fail = ValueError("x")
    reply = handle_request(ops, {"op": "list"})
    assert reply["ok"] is False and reply["error"] == KIND_INTERNAL


# --- served with or without Bluetooth ---------------------------------------------


async def test_the_socket_serves_while_ble_keeps_failing(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(service, "unblock_bluetooth", lambda: None)
    attempts = 0

    async def no_bluez() -> None:
        nonlocal attempts
        attempts += 1
        raise RuntimeError("org.bluez is not on the system bus")

    path = tmp_path / "n.sock"
    monkeypatch.setenv(netsock.GROUP_ENV, OWN_GROUP)
    runner = asyncio.create_task(
        service.run_service(
            FakeOps(), no_bluez, socket_path=path, retry_first_s=0.01, retry_max_s=0.02
        )
    )
    try:
        await listening(path)
        for _ in range(300):
            if attempts >= 3:
                break
            await asyncio.sleep(0.01)
        assert attempts >= 3  # it kept trying, and did not take the process down
        (reply,) = await ask(path, {"op": "list"})
        assert reply["ok"] and not runner.done()
    finally:
        runner.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await runner
    assert not path.exists()


async def test_a_ble_job_that_returns_is_restarted(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(service, "unblock_bluetooth", lambda: None)
    runs = 0

    async def returns() -> None:
        nonlocal runs
        runs += 1

    task = asyncio.create_task(
        service.supervise("job", returns, retry_first_s=0.01, retry_max_s=0.02)
    )
    await asyncio.sleep(0.2)
    task.cancel()
    with contextlib.suppress(asyncio.CancelledError):
        await task
    assert runs >= 3


def test_cancel_on_signals_cancels_the_task() -> None:
    async def main() -> bool:
        task = asyncio.create_task(asyncio.sleep(30))
        service.cancel_on_signals(task)
        os.kill(os.getpid(), signal.SIGTERM)
        with contextlib.suppress(asyncio.CancelledError):
            await task
        return task.cancelled()

    assert asyncio.run(main())


def test_the_unit_names_the_socket_and_does_not_require_bluetooth() -> None:
    unit = (
        Path(__file__).resolve().parents[1] / "systemd" / "canary-provision.service"
    ).read_text()
    assert "CANARY_NETWORK_SOCKET=/run/canary/network.sock" in unit
    assert "Requires=bluetooth" not in unit  # a Requires= would stop the socket with BlueZ
    assert "Wants=bluetooth.service" in unit
