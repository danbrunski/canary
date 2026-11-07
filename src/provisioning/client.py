"""The daemon's side of the network-control socket (roadmap N5.3).

A small blocking client for ``canary-provision``'s socket
(:mod:`src.provisioning.netsock`). The routes call it from FastAPI's
threadpool, so blocking is the point: one connection, one request line, one
reply line, with a deadline.

Every way this can fail raises :class:`WifiServiceError` with a ``kind`` that
says which kind of failure it is. In particular an unreachable service is
``unreachable`` and carries the socket path, never an empty list: "no networks
saved" and "nobody to ask" demand opposite responses.

A request is never formatted into an error. The passphrase rides in ``add``;
nothing here logs, stores, or echoes a request.
"""

from __future__ import annotations

import json
import socket
import time
from pathlib import Path
from typing import Any

from src.provisioning.netsock import socket_path

#: Client-side kinds, beside the service's own (``not_found``, ``refused``...).
KIND_UNREACHABLE = "unreachable"
KIND_TIMEOUT = "timeout"
KIND_BAD_REPLY = "bad_reply"

#: A join (``add`` with ``join``, ``connect``) waits on NetworkManager for up
#: to the service's join timeout (30 s) plus a rescan and, on failure, the
#: rollback; everything else is a handful of ``nmcli`` calls.
JOIN_TIMEOUT_S = 60.0
READ_TIMEOUT_S = 10.0
#: A rescan waits for the radio.
SCAN_TIMEOUT_S = 30.0
_MAX_REPLY_BYTES = 1024 * 1024


class WifiServiceError(Exception):
    """The service did not do what was asked, or could not be asked."""

    def __init__(self, kind: str, detail: str) -> None:
        # Both in args, so pickle rebuilds it (tests/test_exceptions_pickle.py).
        super().__init__(kind, detail)
        self.kind = kind
        self.detail = detail

    def __str__(self) -> str:
        return self.detail


def _unreachable(path: Path, reason: str) -> WifiServiceError:
    return WifiServiceError(
        KIND_UNREACHABLE,
        f"WiFi control service not reachable at {path}: {reason} (is canary-provision running?)",
    )


def call(
    path: Path | None, payload: dict[str, Any], *, timeout_s: float = READ_TIMEOUT_S
) -> dict[str, Any]:
    """Send one request; return the reply when ``ok``, else raise."""
    path = path or socket_path()
    # Encoded before connecting, so a request that cannot be sent never opens
    # a connection, and no error below can carry it.
    line = json.dumps(payload, separators=(",", ":")).encode("utf-8") + b"\n"
    deadline = time.monotonic() + timeout_s
    conn = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    try:
        conn.settimeout(timeout_s)
        try:
            conn.connect(str(path))
            conn.sendall(line)
            reply = _read_line(conn, deadline)
        except TimeoutError:
            raise WifiServiceError(
                KIND_TIMEOUT,
                f"WiFi control service at {path} did not answer within {timeout_s:g} s; "
                "the operation may still be running, so read the list again before retrying",
            ) from None
        except OSError as exc:
            raise _unreachable(path, exc.strerror or str(exc) or type(exc).__name__) from None
    finally:
        conn.close()
    try:
        body = json.loads(reply)
    except ValueError:
        raise WifiServiceError(
            KIND_BAD_REPLY, f"WiFi control service at {path} sent something that is not JSON"
        ) from None
    if not isinstance(body, dict) or not isinstance(body.get("ok"), bool):
        raise WifiServiceError(KIND_BAD_REPLY, f"WiFi control service at {path} sent an odd reply")
    if not body["ok"]:
        raise WifiServiceError(str(body.get("error") or "internal"), str(body.get("detail") or ""))
    return body


def _read_line(conn: socket.socket, deadline: float) -> bytes:
    chunks: list[bytes] = []
    size = 0
    while True:
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            raise TimeoutError
        conn.settimeout(remaining)
        data = conn.recv(65536)
        if not data:
            return b"".join(chunks)  # closed without a newline; parsed (or rejected) as is
        chunks.append(data)
        size += len(data)
        if b"\n" in data:
            return b"".join(chunks)
        if size > _MAX_REPLY_BYTES:
            raise WifiServiceError(
                KIND_BAD_REPLY, "WiFi control service sent a reply too large to read"
            )


class WifiClient:
    """The six operations, one per socket request."""

    def __init__(self, path: Path | None = None) -> None:
        self.path = path or socket_path()

    def _wifi(self, payload: dict[str, Any], timeout_s: float) -> dict[str, Any]:
        reply = call(self.path, payload, timeout_s=timeout_s)
        if not isinstance(reply.get("wifi"), dict):
            raise WifiServiceError(
                KIND_BAD_REPLY, f"WiFi control service at {self.path} sent no network state"
            )
        return reply

    def state(self) -> dict[str, Any]:
        return self._wifi({"op": "list"}, READ_TIMEOUT_S)

    def scan(self) -> dict[str, Any]:
        return self._wifi({"op": "scan"}, SCAN_TIMEOUT_S)

    def add(self, ssid: str, password: str | None, *, hidden: bool, join: bool) -> dict[str, Any]:
        reply = self._wifi(
            {"op": "add", "ssid": ssid, "password": password, "hidden": hidden, "join": join},
            JOIN_TIMEOUT_S if join else READ_TIMEOUT_S,
        )
        if not isinstance(reply.get("network"), dict):
            raise WifiServiceError(
                KIND_BAD_REPLY, f"WiFi control service at {self.path} sent no network"
            )
        return reply

    def connect(self, con_id: str) -> dict[str, Any]:
        return self._wifi({"op": "connect", "id": con_id}, JOIN_TIMEOUT_S)

    def forget(self, con_id: str) -> dict[str, Any]:
        return self._wifi({"op": "forget", "id": con_id}, READ_TIMEOUT_S)

    def order(self, ids: list[str]) -> dict[str, Any]:
        return self._wifi({"op": "order", "ids": ids}, READ_TIMEOUT_S)


__all__ = [
    "JOIN_TIMEOUT_S",
    "KIND_BAD_REPLY",
    "KIND_TIMEOUT",
    "KIND_UNREACHABLE",
    "WifiClient",
    "WifiServiceError",
    "call",
]
