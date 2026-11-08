"""Paired devices: a control token per phone or laptop (docs/roadmap/archive/phone-ui.md P2.4).

The shared control token (``/etc/canary/control.token``) stays the key for
SSH, the MCP server and scripts. A person's devices get their own tokens
instead, issued by pairing, so a phone never needs that file and a lost one
can be removed without changing the token everything else uses.

Only a SHA-256 of each token is stored: the file is a list of who may
connect, not a list of secrets. Tokens carry 256 random bits, so an unsalted
fast hash is enough; there is nothing to brute-force.

The devices belong to the Pi, not to a vehicle, so the file sits beside the
control state rather than under a vehicle profile.
"""

from __future__ import annotations

import hashlib
import json
import logging
import os
import secrets
import threading
import time
from collections.abc import Callable
from dataclasses import asdict, dataclass, replace
from pathlib import Path
from typing import Any

from src.paths import CONTROL_DIR

log = logging.getLogger(__name__)

TOKEN_PREFIX = "cd_"
MAX_NAME_LENGTH = 64
#: ``last_seen`` is kept in memory on every request but written at most this
#: often, so an active phone does not turn every poll into a disk write.
LAST_SEEN_WRITE_S = 600.0


def default_path(state_dir: Path | None) -> Path:
    root = state_dir / "control" if state_dir is not None else CONTROL_DIR
    return root / "paired_devices.json"


def token_digest(token: str) -> str:
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


def clean_name(name: str) -> str:
    printable = "".join(ch for ch in name if ch.isprintable()).strip()
    return printable[:MAX_NAME_LENGTH] or "unnamed device"


@dataclass(frozen=True)
class PairedDevice:
    id: str
    name: str
    paired_at: float
    #: How it was paired: ``key-cycle``, or ``approved-by:<device id>`` /
    #: ``approved-by:shared-token`` when a trusted caller approved it.
    method: str
    token_sha256: str
    last_seen: float | None = None

    def public(self) -> dict[str, Any]:
        """The API shape: everything but the token digest."""
        data = asdict(self)
        del data["token_sha256"]
        return data


def _backup(path: Path) -> Path:
    return path.with_name(f"{path.name}.bak")


def _parse(text: str) -> dict[str, PairedDevice]:
    """Parse the store; raises ValueError or TypeError on any malformed part."""
    raw = json.loads(text)
    entries = raw.get("devices") if isinstance(raw, dict) else None
    if not isinstance(entries, list):
        raise ValueError("no 'devices' list")
    devices = {}
    for entry in entries:
        if not isinstance(entry, dict):
            raise ValueError(f"device entry is {type(entry).__name__}, not an object")
        device = PairedDevice(**entry)
        if not isinstance(device.token_sha256, str) or len(device.token_sha256) != 64:
            raise ValueError(f"device {device.id!r} has no valid token digest")
        devices[device.token_sha256] = device
    return devices


def _durable_write(path: Path, text: str) -> None:
    """Write, fsync, rename, fsync the directory: survives a power cut, 0600."""
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(f".{path.name}.tmp")
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    try:
        os.write(fd, text.encode("utf-8"))
        os.fsync(fd)
    finally:
        os.close(fd)
    os.replace(tmp, path)
    dir_fd = os.open(path.parent, os.O_RDONLY)
    try:
        os.fsync(dir_fd)
    finally:
        os.close(dir_fd)


class PairedDevices:
    def __init__(self, path: Path, *, clock: Callable[[], float] = time.time) -> None:
        self._path = path
        self._clock = clock
        self._lock = threading.Lock()
        self._devices = self._load()
        self._written_at: dict[str, float] = {}

    def _load(self) -> dict[str, PairedDevice]:
        """Read the store, falling back to the last good copy.

        The rig loses power without warning, so a torn file is expected, not
        exotic. An unreadable file is moved aside rather than overwritten by
        the next pairing, so it can still be recovered by hand.
        """
        for candidate in (self._path, _backup(self._path)):
            try:
                return _parse(candidate.read_text())
            except FileNotFoundError:
                continue
            except (OSError, ValueError, TypeError) as exc:
                log.error("paired devices unreadable at %s: %s", candidate, exc)
                if candidate == self._path:
                    aside = candidate.with_name(f"{candidate.name}.unreadable-{int(time.time())}")
                    try:
                        candidate.replace(aside)
                        log.error("moved it to %s; trying the backup", aside)
                    except OSError:
                        log.exception("could not move %s aside", candidate)
        return {}

    def _save(self, devices: dict[str, PairedDevice]) -> None:
        payload = {"devices": [asdict(d) for d in devices.values()]}
        if self._path.exists():
            # The last good copy, for _load when power dies mid-write.
            os.replace(self._path, _backup(self._path))
        _durable_write(self._path, json.dumps(payload, indent=2) + "\n")

    def pair(self, name: str, *, method: str) -> tuple[PairedDevice, str]:
        """Issue a token for a new device; the plaintext is returned once."""
        token = TOKEN_PREFIX + secrets.token_urlsafe(32)
        device = PairedDevice(
            id=secrets.token_hex(6),
            name=clean_name(name),
            paired_at=self._clock(),
            method=method,
            token_sha256=token_digest(token),
        )
        with self._lock:
            # Saved before it is live: a pairing the disk did not take must
            # not work until the next restart and then silently vanish.
            devices = {**self._devices, device.token_sha256: device}
            self._save(devices)
            self._devices = devices
        log.info("paired device %s (%s) by %s", device.id, device.name, method)
        return device, token

    def authenticate(self, token: str) -> PairedDevice | None:
        if not token.startswith(TOKEN_PREFIX):
            return None
        digest = token_digest(token)
        now = self._clock()
        with self._lock:
            device = self._devices.get(digest)
            if device is None:
                return None
            device = replace(device, last_seen=now)
            self._devices[digest] = device
            if now - self._written_at.get(device.id, float("-inf")) > LAST_SEEN_WRITE_S:
                self._written_at[device.id] = now
                try:
                    self._save(self._devices)
                except OSError as exc:
                    log.warning("could not record last_seen: %s", exc)
        return device

    def is_paired(self, device_id: str) -> bool:
        with self._lock:
            return any(d.id == device_id for d in self._devices.values())

    def list(self) -> list[PairedDevice]:
        with self._lock:
            return sorted(self._devices.values(), key=lambda d: d.paired_at)

    def rename(self, device_id: str, name: str) -> PairedDevice | None:
        """Give a device a new display name; ``None`` if no such device.

        Saved before it is live, like :meth:`pair`: a name the disk refused
        must not show until the next restart and then revert.
        """
        with self._lock:
            for digest, device in self._devices.items():
                if device.id == device_id:
                    renamed = replace(device, name=clean_name(name))
                    devices = {**self._devices, digest: renamed}
                    self._save(devices)
                    self._devices = devices
                    log.info("renamed paired device %s to %s", device.id, renamed.name)
                    return renamed
        return None

    def remove(self, device_id: str) -> PairedDevice | None:
        with self._lock:
            for digest, device in self._devices.items():
                if device.id == device_id:
                    remaining = {k: v for k, v in self._devices.items() if k != digest}
                    self._save(remaining)
                    self._devices = remaining
                    log.info("removed paired device %s (%s)", device.id, device.name)
                    return device
        return None
