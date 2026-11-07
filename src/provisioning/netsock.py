"""The network-control socket (roadmap N5.1).

NetworkManager keeps its system connections for root, and the daemon does not
run as root. So the root ``canary-provision`` service is the one owner of WiFi
state, and the daemon asks it over a Unix socket -- the same "daemon asks, root
acts" shape ``canary-update`` uses. The daemon's routes
(``src/dashboard/routes/control/network.py``) are the only intended client.

Protocol: one JSON object per line in, one JSON line back. A connection may
send several requests; the daemon sends one. Requests::

    {"op": "list"}                                   # saved networks + the last scan
    {"op": "scan"}                                   # rescan first, then the same
    {"op": "add", "ssid": "...", "password": null, "hidden": false, "join": true}
    {"op": "connect", "id": "<profile uuid>"}
    {"op": "forget", "id": "<profile uuid>"}
    {"op": "order", "ids": ["<uuid>", ...]}          # first wins

Replies are ``{"ok": true, ...}`` or ``{"ok": false, "error": <kind>, "detail":
...}`` with ``kind`` one of ``not_found``, ``refused``, ``invalid``,
``join_failed``, ``internal``, or ``unauthorized`` (the peer may not use the
socket). The state shape is documented in ``docs/headless-api.md``.

**A passphrase arrives in** ``add`` **and nowhere else, and goes nowhere.** It
is handed to the keyfile writer and dropped: never logged, echoed, or put in a
reply. Logging is structural -- :func:`describe` builds the log line from the
operation and the network name alone, so there is no code path that formats a
request. A failure's text is scrubbed of the passphrase before it leaves.

Who may connect: the socket file is ``root:canary 0660``, which the kernel
enforces at ``connect``, and the server checks ``SO_PEERCRED`` as well, so a
loosened mode does not open it (:func:`peer_allowed`).
"""

from __future__ import annotations

import asyncio
import contextlib
import grp
import json
import logging
import os
import pwd
import socket
import stat
import struct
import traceback
from collections.abc import Callable
from pathlib import Path
from typing import Any, Protocol

from src.provisioning.network import (
    KIND_INTERNAL,
    KIND_INVALID,
    WifiOpError,
    WifiState,
)

log = logging.getLogger(__name__)

DEFAULT_SOCKET_PATH = Path("/run/canary/network.sock")
SOCKET_ENV = "CANARY_NETWORK_SOCKET"
DEFAULT_GROUP = "canary"
GROUP_ENV = "CANARY_NETWORK_SOCKET_GROUP"
#: ``connect`` and ``add`` with ``join`` wait this long for NetworkManager.
#: The daemon's client waits longer (``src.provisioning.client``).
DEFAULT_JOIN_TIMEOUT_S = 30.0
#: A request line is a few hundred bytes; anything bigger is not one.
MAX_REQUEST_BYTES = 16 * 1024
#: How long a connection may take to send its request line.
READ_TIMEOUT_S = 10.0
KIND_UNAUTHORIZED = "unauthorized"
OPS = ("list", "scan", "add", "connect", "forget", "order")

PeerPolicy = Callable[[int, int, int], bool]


def socket_path() -> Path:
    return Path(os.environ.get(SOCKET_ENV) or DEFAULT_SOCKET_PATH)


class NetworkOps(Protocol):
    """What the socket needs from NetworkManager (``NetworkManagerCli``)."""

    def wifi_state(self, *, rescan: bool = False) -> WifiState: ...
    def add_network(
        self, ssid: object, password: object, *, hidden: bool, join: bool, timeout_s: float
    ) -> str: ...
    def connect_network(self, con_id: object, *, timeout_s: float) -> None: ...
    def forget_network(self, con_id: object) -> None: ...
    def set_order(self, ids: object) -> None: ...


# --- who may ask ------------------------------------------------------------


def peer_credentials(conn: socket.socket) -> tuple[int, int, int]:
    """``(pid, uid, gid)`` of the process on the other end, from the kernel."""
    raw = conn.getsockopt(socket.SOL_SOCKET, socket.SO_PEERCRED, struct.calcsize("3i"))
    pid, uid, gid = struct.unpack("3i", raw)
    return pid, uid, gid


def group_policy(group: str = DEFAULT_GROUP) -> PeerPolicy:
    """Root, or a user in ``group`` (primary or supplementary, per the user database)."""

    def allowed(pid: int, uid: int, gid: int) -> bool:
        if uid == 0:
            return True
        try:
            wanted = grp.getgrnam(group).gr_gid
        except KeyError:
            return False
        if gid == wanted:
            return True
        try:
            name = pwd.getpwuid(uid).pw_name
        except KeyError:
            return False
        return wanted in os.getgrouplist(name, gid)

    return allowed


# --- requests ----------------------------------------------------------------


def describe(request: object) -> str:
    """The only text a request contributes to a log line: the operation and the
    network it names. Nothing else in a request is ever formatted."""
    if not isinstance(request, dict):
        return "malformed request"
    op = request.get("op")
    op = op if isinstance(op, str) and op in OPS else "unknown op"
    if op == "add":
        ssid = request.get("ssid")
        return f"add ssid={ssid!r}" if isinstance(ssid, str) else "add"
    if op in ("connect", "forget"):
        con_id = request.get("id")
        return f"{op} id={con_id!r}" if isinstance(con_id, str) else op
    return op


def _scrub(text: str, secret: object) -> str:
    return text.replace(secret, "***") if isinstance(secret, str) and len(secret) >= 4 else text


def _failure(kind: str, detail: str) -> dict[str, Any]:
    return {"ok": False, "error": kind, "detail": detail}


def _flag(request: dict[str, Any], key: str, *, default: bool | None) -> bool:
    value = request.get(key, default)
    if not isinstance(value, bool):
        raise WifiOpError(KIND_INVALID, f"{key} must be true or false")
    return value


def handle_request(
    ops: NetworkOps, request: object, *, join_timeout_s: float = DEFAULT_JOIN_TIMEOUT_S
) -> dict[str, Any]:
    """Run one request and build its reply. Blocking; never raises."""
    password = request.get("password") if isinstance(request, dict) else None
    try:
        if not isinstance(request, dict):
            raise WifiOpError(KIND_INVALID, "a request is a JSON object")
        op = request.get("op")
        if op == "list":
            return {"ok": True, "wifi": ops.wifi_state().to_dict()}
        if op == "scan":
            return {"ok": True, "wifi": ops.wifi_state(rescan=True).to_dict()}
        if op == "add":
            # `join` has no default: whether to switch the radio to a new network
            # is the caller's decision, never a fallback.
            join = _flag(request, "join", default=None)
            hidden = _flag(request, "hidden", default=False)
            con_id = ops.add_network(
                request.get("ssid"), password, hidden=hidden, join=join, timeout_s=join_timeout_s
            )
            state = ops.wifi_state()
            network = state.network(con_id)
            if network is None:
                raise WifiOpError(KIND_INTERNAL, "the network was saved but is not listed")
            return {"ok": True, "network": network.to_dict(), "wifi": state.to_dict()}
        if op == "connect":
            ops.connect_network(request.get("id"), timeout_s=join_timeout_s)
            return {"ok": True, "wifi": ops.wifi_state().to_dict()}
        if op == "forget":
            ops.forget_network(request.get("id"))
            return {"ok": True, "wifi": ops.wifi_state().to_dict()}
        if op == "order":
            ops.set_order(request.get("ids"))
            return {"ok": True, "wifi": ops.wifi_state().to_dict()}
        raise WifiOpError(KIND_INVALID, f"unknown op; expected one of {', '.join(OPS)}")
    except WifiOpError as exc:
        return _failure(exc.kind, _scrub(exc.detail, password))
    except Exception as exc:  # noqa: BLE001 -- a request must get an answer
        trace = "".join(traceback.format_exception(exc))
        log.error("network control: %s crashed\n%s", describe(request), _scrub(trace, password))
        return _failure(KIND_INTERNAL, _scrub(f"{type(exc).__name__}: {exc}", password))


# --- the server --------------------------------------------------------------


class NetworkControlServer:
    """Serves :func:`handle_request` on a Unix socket until cancelled."""

    def __init__(
        self,
        ops: NetworkOps,
        *,
        path: Path | None = None,
        group: str | None = None,
        join_timeout_s: float = DEFAULT_JOIN_TIMEOUT_S,
        peer_policy: PeerPolicy | None = None,
    ) -> None:
        self._ops = ops
        self.path = path or socket_path()
        self._group = group or os.environ.get(GROUP_ENV) or DEFAULT_GROUP
        self._join_timeout_s = join_timeout_s
        self._allowed = peer_policy or group_policy(self._group)
        #: One operation at a time: a join takes ~30 s and a rescan during it
        #: would drop the very association being made.
        self._lock = asyncio.Lock()

    def _bind(self) -> socket.socket:
        """A listening socket at ``path``, ``root:<group> 0660`` from its first instant."""
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with contextlib.suppress(FileNotFoundError):
            if stat.S_ISSOCK(os.lstat(self.path).st_mode):
                os.unlink(self.path)  # left by a service that did not stop cleanly
        try:
            gid: int | None = grp.getgrnam(self._group).gr_gid
        except KeyError:
            gid = None
            log.error("group %r does not exist; the network socket is root-only", self._group)
        sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        # The mode is decided by the umask at bind(), not fixed by a chmod
        # afterwards: no instant has the socket more open than it ends up.
        old_umask = os.umask(0o117 if gid is not None else 0o177)
        try:
            sock.bind(str(self.path))
        except BaseException:
            sock.close()
            raise
        finally:
            os.umask(old_umask)
        try:
            if gid is not None:
                os.chown(self.path, -1, gid, follow_symlinks=False)
            sock.listen(8)
        except BaseException:
            sock.close()
            with contextlib.suppress(OSError):
                os.unlink(self.path)
            raise
        return sock

    async def serve(self) -> None:
        sock = self._bind()
        server = await asyncio.start_unix_server(
            self._connection, sock=sock, limit=MAX_REQUEST_BYTES
        )
        log.info("network control: listening on %s (group %s)", self.path, self._group)
        try:
            await server.serve_forever()
        finally:
            server.close()
            with contextlib.suppress(OSError):
                os.unlink(self.path)

    async def _connection(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        try:
            if not self._peer_ok(writer):
                await self._send(
                    writer,
                    _failure(KIND_UNAUTHORIZED, "this process may not use the network socket"),
                )
                return
            while True:
                try:
                    line = await asyncio.wait_for(reader.readline(), READ_TIMEOUT_S)
                except TimeoutError:
                    return
                except (asyncio.LimitOverrunError, ValueError):
                    # Neither the size nor the content of the line is logged.
                    log.warning("network control: request line too long")
                    await self._send(writer, _failure(KIND_INVALID, "request too long"))
                    return
                if not line:
                    return
                await self._send(writer, await self._answer(line))
        except (ConnectionError, asyncio.IncompleteReadError):
            pass
        finally:
            writer.close()
            with contextlib.suppress(ConnectionError, OSError):
                await writer.wait_closed()

    def _peer_ok(self, writer: asyncio.StreamWriter) -> bool:
        conn = writer.get_extra_info("socket")
        try:
            pid, uid, gid = peer_credentials(conn)
        except (OSError, AttributeError):
            log.warning("network control: cannot read the peer's credentials; refusing")
            return False
        if self._allowed(pid, uid, gid):
            return True
        log.warning("network control: refused pid %d (uid %d, gid %d)", pid, uid, gid)
        return False

    async def _answer(self, line: bytes) -> dict[str, Any]:
        try:
            request = json.loads(line)
        except ValueError:
            # The decoder's message names a position, never the text.
            log.warning("network control: a request was not JSON (%d bytes)", len(line))
            return _failure(KIND_INVALID, "a request is one JSON object per line")
        label = describe(request)
        async with self._lock:
            reply = await asyncio.to_thread(
                handle_request, self._ops, request, join_timeout_s=self._join_timeout_s
            )
        if reply["ok"]:
            log.info("network control: %s ok", label)
        else:
            log.warning(
                "network control: %s failed: %s: %s", label, reply["error"], reply["detail"]
            )
        return reply

    @staticmethod
    async def _send(writer: asyncio.StreamWriter, reply: dict[str, Any]) -> None:
        writer.write(json.dumps(reply, separators=(",", ":")).encode("utf-8") + b"\n")
        await writer.drain()


__all__ = [
    "DEFAULT_GROUP",
    "DEFAULT_SOCKET_PATH",
    "GROUP_ENV",
    "KIND_UNAUTHORIZED",
    "NetworkControlServer",
    "NetworkOps",
    "OPS",
    "SOCKET_ENV",
    "describe",
    "group_policy",
    "handle_request",
    "peer_credentials",
    "socket_path",
]
