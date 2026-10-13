"""Software update from the UI: the file contracts (phone-ui P3.1).

The daemon runs as ``canary`` with ``NoNewPrivileges`` and cannot run sudo, so
it never updates anything itself. It files a request, and a root unit does
the work -- the same shape as the UPS keep-alive (``src/daemon/ups.py``) and
the BLE provisioning window (``src/provisioning/gate.py``), through the shared
``/run/canary`` tmpfs:

- the daemon writes ``update_request`` (:func:`write_request`);
- ``canary-update.path`` (root) sees it and starts the oneshot
  ``canary-update.service``, whose ``ExecStartPre`` renames it to
  ``update_request.taken`` before any Python runs, so a request that crashes
  the runner cannot start it again;
- the runner (:mod:`src.control.update_runner`) reads the taken request, runs
  a check (``git fetch``) or ``canary-update`` (``scripts/update_canary.sh``),
  and reports progress and the outcome in ``update_status``
  (:func:`write_status`), which the daemon reads (:func:`read_status`).

Both sides share this module, so the request and status formats have one
definition. The directory is writable by the unprivileged daemon, so the root
side trusts nothing in it: the request is read without following symlinks and
validated field by field, and the status is written through a fresh
``O_EXCL`` temp file, never a predictable path a symlink could redirect.
"""

from __future__ import annotations

import json
import os
import stat
import tempfile
import time
import uuid
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from src.collection import tuning_profile, tx_gate
from src.control.rom_backup import HOLDER as ROM_BACKUP_HOLDER

RUN_DIR = Path("/run/canary")


@dataclass(frozen=True)
class UpdatePaths:
    """Every path either side of the contract touches; tests point them at
    ``tmp_path``."""

    request: Path = RUN_DIR / "update_request"
    #: Where ``canary-update.service``'s ``ExecStartPre`` moves the request.
    taken: Path = RUN_DIR / "update_request.taken"
    status: Path = RUN_DIR / "update_status"
    #: Installed by ``install_pi.sh``. Without it a request is never picked up.
    trigger_unit: Path = Path("/etc/systemd/system/canary-update.path")
    #: ``route`` and ``ipv6_route`` live here (:func:`has_default_route`).
    proc_net: Path = Path("/proc/net")


DEFAULT_UPDATE_PATHS = UpdatePaths()

ACTIONS = ("check", "update")
#: States the runner writes. ``lost`` is never written: :func:`read_status`
#: derives it from a stale heartbeat.
STATES = ("idle", "checking", "updating", "done", "failed", "refused")
ACTIVE_STATES = frozenset({"checking", "updating"})
LOST_STATE = "lost"
STATUS_SCHEMA = 1

#: A request is a few dozen bytes; anything bigger is not one.
MAX_REQUEST_BYTES = 4096
#: The runner ignores a request older than this: it was filed for a moment
#: that has passed (a unit that was not installed then, a crash before it ran).
REQUEST_MAX_AGE_S = 600.0
#: A request still waiting after this was not picked up: ``canary-update.path``
#: is missing or stopped. The daemon says so and lets a new request replace it.
PICKUP_TIMEOUT_S = 60.0
#: The runner rewrites the status at least this often while it works.
HEARTBEAT_S = 10.0
#: A ``checking``/``updating`` status whose heartbeat is older than this is
#: ``lost``: the runner died without writing an outcome (killed, out of memory).
LOST_AFTER_S = 180.0

#: ``scripts/update_canary.sh`` exit codes, word for word from its ``usage()``
#: (``tests/test_control_updater.py`` compares them, so they cannot drift).
EXIT_MEANINGS: dict[int, str] = {
    0: "updated (or already current) and healthy",
    1: "setup error (not root, missing checkout or installer)",
    2: "usage error",
    3: "refused: a capture or ECU backup is active (from restart_canary.sh --check)",
    4: "refused: the checkout has tracked local changes (outside models/)",
    5: "fetch or fast-forward failed; nothing was changed",
    6: "new code failed its health check; rolled back to the previous commit, "
    "which is healthy again",
    7: "new code failed its health check AND the rollback did not come up; "
    "inspect: sudo journalctl -u canary -n 100 --no-pager",
    8: "new code failed its health check and was left in place (--no-rollback, "
    "or there was nothing older to roll back to)",
}


class UpdateRequestError(ValueError):
    """A request file that is not a valid request."""


def utc_iso(epoch: float) -> str:
    return datetime.fromtimestamp(epoch, UTC).isoformat()


def parse_iso(value: object) -> float | None:
    """Epoch seconds of an ISO-8601 timestamp, or ``None`` if it is not one."""
    if not isinstance(value, str):
        return None
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError:
        return None
    if parsed.tzinfo is None:
        return None
    return parsed.timestamp()


# ---------------------------------------------------------------------------
# The request (daemon writes, root reads)
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class UpdateRequest:
    action: str
    request_id: str
    requested_at: str
    #: ``canary-update --full``: every step regardless of the diff. ``--force``
    #: (end a running capture) is deliberately not offered from a button.
    full: bool = False

    def to_dict(self) -> dict[str, Any]:
        return {
            "action": self.action,
            "request_id": self.request_id,
            "requested_at": self.requested_at,
            "options": {"full": self.full},
        }


def new_request(action: str, *, full: bool = False, now: float | None = None) -> UpdateRequest:
    if action not in ACTIONS:
        raise UpdateRequestError(f"action must be one of {', '.join(ACTIONS)}, not {action!r}")
    if full and action != "update":
        raise UpdateRequestError("'full' applies only to the update action")
    return UpdateRequest(
        action=action,
        request_id=uuid.uuid4().hex,
        requested_at=utc_iso(time.time() if now is None else now),
        full=full,
    )


def write_request(request: UpdateRequest, path: Path = DEFAULT_UPDATE_PATHS.request) -> None:
    """File ``request`` for the root unit. Raises ``OSError`` when the
    directory is not writable (no ``/run/canary``: tmpfiles not installed)."""
    _atomic_write(path, json.dumps(request.to_dict()), mode=0o644)


def parse_request(
    raw: str, *, now: float | None = None, max_age_s: float | None = REQUEST_MAX_AGE_S
) -> UpdateRequest:
    """Validate a request's text field by field. The writer is the
    unprivileged daemon; the reader is root, so nothing is taken on trust."""
    try:
        data = json.loads(raw)
    except ValueError as exc:
        raise UpdateRequestError("the request is not JSON") from exc
    if not isinstance(data, dict):
        raise UpdateRequestError("the request is not a JSON object")
    unknown = set(data) - {"action", "request_id", "requested_at", "options"}
    if unknown:
        raise UpdateRequestError(f"unknown request fields: {', '.join(sorted(unknown))}")
    action = data.get("action")
    if action not in ACTIONS:
        raise UpdateRequestError(f"action must be one of {', '.join(ACTIONS)}")
    request_id = data.get("request_id")
    if not isinstance(request_id, str) or not (0 < len(request_id) <= 64 and request_id.isalnum()):
        raise UpdateRequestError("request_id must be 1-64 letters or digits")
    requested_epoch = parse_iso(data.get("requested_at"))
    if requested_epoch is None:
        raise UpdateRequestError("requested_at must be a timezone-aware ISO-8601 timestamp")
    age = (time.time() if now is None else now) - requested_epoch
    if max_age_s is not None and age > max_age_s:
        raise UpdateRequestError(
            f"the request is {age:.0f} s old (limit {max_age_s:.0f} s); ignored"
        )
    options = data.get("options", {})
    if not isinstance(options, dict):
        raise UpdateRequestError("options must be an object")
    unknown_options = set(options) - {"full"}
    if unknown_options:
        raise UpdateRequestError(f"unknown options: {', '.join(sorted(unknown_options))}")
    full = options.get("full", False)
    if not isinstance(full, bool):
        raise UpdateRequestError("options.full must be true or false")
    if full and action != "update":
        raise UpdateRequestError("'full' applies only to the update action")
    return UpdateRequest(
        action=str(action),
        request_id=request_id,
        requested_at=str(data["requested_at"]),
        full=full,
    )


def read_regular_file(path: Path, *, limit: int = MAX_REQUEST_BYTES) -> str:
    """The text of ``path`` if it is a regular file of at most ``limit`` bytes.

    Opened with ``O_NOFOLLOW``: ``/run/canary`` belongs to the unprivileged
    daemon, so a symlink there must not make root read some other file.
    Raises ``FileNotFoundError`` when absent and :class:`UpdateRequestError`
    for anything that is not a small regular file.
    """
    try:
        fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    except FileNotFoundError:
        raise
    except OSError as exc:
        raise UpdateRequestError(f"{path.name} could not be opened: {exc.strerror}") from exc
    try:
        info = os.fstat(fd)
        if not stat.S_ISREG(info.st_mode):
            raise UpdateRequestError(f"{path.name} is not a regular file")
        if info.st_size > limit:
            raise UpdateRequestError(f"{path.name} is {info.st_size} bytes (limit {limit})")
        data = os.read(fd, limit + 1)
    finally:
        os.close(fd)
    if len(data) > limit:
        raise UpdateRequestError(f"{path.name} is over {limit} bytes")
    return data.decode("utf-8", errors="replace")


def pending_request(
    paths: UpdatePaths = DEFAULT_UPDATE_PATHS, *, now: float | None = None
) -> dict[str, Any] | None:
    """The request waiting for the root unit, as the daemon sees it.

    ``stuck`` is true once it has waited past :data:`PICKUP_TIMEOUT_S`: the
    trigger is not running, and a new request may replace this one.
    """
    try:
        raw = read_regular_file(paths.request)
        request = parse_request(raw, max_age_s=None)
    except FileNotFoundError:
        return None
    except UpdateRequestError as exc:
        return {"action": None, "request_id": None, "age_s": None, "stuck": True, "error": str(exc)}
    requested = parse_iso(request.requested_at) or 0.0
    age = max(0.0, (time.time() if now is None else now) - requested)
    return {
        "action": request.action,
        "request_id": request.request_id,
        "age_s": round(age, 1),
        "stuck": age > PICKUP_TIMEOUT_S,
    }


# ---------------------------------------------------------------------------
# The status (root writes, daemon reads)
# ---------------------------------------------------------------------------


def idle_status() -> dict[str, Any]:
    return {"schema": STATUS_SCHEMA, "state": "idle", "last_check": None}


def write_status(status: dict[str, Any], path: Path = DEFAULT_UPDATE_PATHS.status) -> None:
    """Atomically replace the status file, readable by the daemon (0644)."""
    _atomic_write(path, json.dumps(status, sort_keys=True), mode=0o644)


def read_status(
    path: Path = DEFAULT_UPDATE_PATHS.status, *, now: float | None = None
) -> dict[str, Any]:
    """The runner's last status, with ``lost`` derived from a stale heartbeat.

    A missing file is ``idle`` (``/run`` is a tmpfs: a reboot clears it). An
    unreadable or malformed one is ``unknown`` with the reason, never a guess.
    """
    try:
        raw = path.read_text()
    except FileNotFoundError:
        return idle_status()
    except OSError as exc:
        return {**idle_status(), "state": "unknown", "error": f"unreadable: {exc.strerror}"}
    try:
        data = json.loads(raw)
    except ValueError:
        return {**idle_status(), "state": "unknown", "error": "the status file is not JSON"}
    if not isinstance(data, dict) or data.get("state") not in STATES:
        return {**idle_status(), "state": "unknown", "error": "the status file has no valid state"}
    status: dict[str, Any] = {**idle_status(), **data}
    if status["state"] in ACTIVE_STATES:
        heartbeat = parse_iso(status.get("updated_at"))
        current = time.time() if now is None else now
        if heartbeat is None or current - heartbeat > LOST_AFTER_S:
            silent = "never" if heartbeat is None else f"{current - heartbeat:.0f} s ago"
            return {
                **status,
                "state": LOST_STATE,
                "lost_from": status["state"],
                "message": (
                    f"The update runner stopped reporting while {status['state']} "
                    f"(last heartbeat {silent}). See: journalctl -u canary-update"
                ),
            }
    return status


def in_progress(status: dict[str, Any]) -> bool:
    """Whether a runner is working right now (a ``lost`` one is not)."""
    return status.get("state") in ACTIVE_STATES


def _atomic_write(path: Path, text: str, *, mode: int) -> None:
    """Write via a fresh ``mkstemp`` file (``O_EXCL``: a planted symlink at
    the temp name cannot redirect the write) and ``rename`` over ``path``
    (which replaces a symlink rather than following it)."""
    fd, tmp_name = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
    try:
        with os.fdopen(fd, "w") as handle:
            handle.write(text)
        os.chmod(tmp_name, mode)
        os.replace(tmp_name, path)
    except BaseException:
        Path(tmp_name).unlink(missing_ok=True)
        raise


# ---------------------------------------------------------------------------
# What the daemon checks before filing a request
# ---------------------------------------------------------------------------

_RTF_UP = 0x0001
_RTF_REJECT = 0x0200


def has_default_route(proc_net: Path = DEFAULT_UPDATE_PATHS.proc_net) -> bool | None:
    """Whether the kernel has a usable default route (IPv4 or IPv6).

    ``None`` when neither table can be read, which is "unknown", not "no":
    the runner's own probe then decides. Readable by any user, so the daemon
    can answer "no internet" before filing anything (P3.4).
    """
    readable = False
    try:
        lines = (proc_net / "route").read_text().splitlines()[1:]
        readable = True
    except OSError:
        lines = []
    for line in lines:
        fields = line.split()
        if len(fields) >= 4 and fields[1] == "00000000" and _flags(fields[3]) & _RTF_UP:
            return True
    try:
        lines6 = (proc_net / "ipv6_route").read_text().splitlines()
        readable = True
    except OSError:
        lines6 = []
    for line in lines6:
        fields = line.split()
        if len(fields) < 10 or fields[9] == "lo":
            continue
        flags = _flags(fields[8])
        if fields[0] == "0" * 32 and fields[1] == "00" and flags & _RTF_UP:
            if not flags & _RTF_REJECT:
                return True
    return False if readable else None


def _flags(text: str) -> int:
    try:
        return int(text, 16)
    except ValueError:
        return 0


def active_work() -> list[str]:
    """What an update would end, with the same two facts
    ``restart_canary.sh --check`` reads from ``/status``: an active capture
    profile and an ECU backup holding the ECM session. Empty when an update
    may start."""
    blockers: list[str] = []
    capture = tuning_profile.snapshot()
    if capture.get("active"):
        where = capture.get("session_dir") or "no session directory"
        blockers.append(
            f"capture '{capture.get('name')}' is recording ({where}); an update restarts "
            "CANary and ends it. Stop it first (or let it finish)."
        )
    if tx_gate.claim_held(ROM_BACKUP_HOLDER):
        blockers.append(
            "an ECU backup (ROM read) is running and holds the ECM session; an update "
            "restarts CANary and throws the read away. Wait for it or cancel it first."
        )
    return blockers


def exit_meaning(code: int | None) -> str | None:
    if code is None:
        return None
    return EXIT_MEANINGS.get(code, f"exit {code} (not a documented canary-update code)")


__all__ = [
    "ACTIONS",
    "ACTIVE_STATES",
    "DEFAULT_UPDATE_PATHS",
    "EXIT_MEANINGS",
    "HEARTBEAT_S",
    "LOST_AFTER_S",
    "LOST_STATE",
    "PICKUP_TIMEOUT_S",
    "REQUEST_MAX_AGE_S",
    "STATES",
    "STATUS_SCHEMA",
    "UpdatePaths",
    "UpdateRequest",
    "UpdateRequestError",
    "active_work",
    "exit_meaning",
    "has_default_route",
    "idle_status",
    "in_progress",
    "new_request",
    "parse_iso",
    "parse_request",
    "pending_request",
    "read_regular_file",
    "read_status",
    "utc_iso",
    "write_request",
    "write_status",
]
