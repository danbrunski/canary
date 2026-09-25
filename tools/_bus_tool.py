"""Shared preamble for the tools that transmit on the diagnostic CAN (R06.1).

``scripts/`` got ``scripts/_cli.py`` in July; the bus tools written since
(memory channels, DID logging and sweeps, batch probing) copied each other
instead and imported each other's private names. This is their one home:

* :func:`add_bus_tool_args` — config, header, session, read timeout, daemon
  URL, ``--no-daemon`` and the passive re-check interval, with one set of
  defaults;
* :func:`require_passive` — asks the daemon whether passive mode is armed and
  returns a :class:`PassiveVerdict`, or raises :class:`PassiveRefusedError`;
* :func:`open_client` — the only way a tool opens the bus, and it takes the
  verdict. A transmitting tool cannot open the bus without one, so it cannot
  skip the passive check by forgetting it (``probe_local_identifier`` and
  ``scan_constant_dids`` used to);
* :func:`resolve_session`, :func:`load_widths` and the JSONL record shape.

The HTTP half (:mod:`src.control.passive_client`) lives in ``src`` so nothing
here needs the daemon's own code.
"""

from __future__ import annotations

import argparse
import json
from collections.abc import Iterator, Mapping
from contextlib import contextmanager
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Literal

from src.collection.can_bus import bus_factory
from src.collection.can_obd_client import CanObdClient
from src.collection.isotp import (
    DEFAULT_REQUEST_ID,
    DEFAULT_RESPONSE_ID_MAX,
    DEFAULT_RESPONSE_ID_MIN,
    expected_responder,
)
from src.collection.obd_scheduler import did_access
from src.config import CanaryConfig
from src.control.passive_client import (
    DEFAULT_DAEMON_URL,
    PassiveChecker,
    PassiveStatus,
    http_passive_checker,
    passive_problem,
    resolve_token,
)

#: Why passive mode is mandatory for a session-holding tool — quoted in every
#: refusal and stop message.
PASSIVE_REASON = (
    "the daemon's OBD scheduler sends 10 81 and exits the diagnostic session this "
    "tool holds, and this tool's session blacks out the daemon's Mode 01 polling"
)

#: Exit status every bus tool returns when it refuses to transmit.
PASSIVE_REFUSED_EXIT = 2

#: The JSONL record discriminator every bus-tool log uses. ``can_memory_log``
#: wrote ``"type"`` until R06.1; :func:`record_kind` still reads those files.
RECORD_KEY = "kind"

VerdictSource = Literal["armed", "no_daemon", "job_controller"]

#: Only this module mints verdicts; see :class:`PassiveVerdict`.
_MINT = object()


class PassiveRefusedError(RuntimeError):
    """The daemon is (or may be) transmitting; the tool must not open the bus."""


@dataclass(frozen=True)
class PassiveVerdict:
    """Proof that the passive question was asked before the bus was opened.

    Built only by :func:`require_passive` (the daemon answered armed, or the
    operator said ``--no-daemon``) or :meth:`job_controller`. Constructing one
    by hand raises, so :func:`open_client` cannot be satisfied without asking.
    ``checker`` is kept for tools that re-check during a long run.
    """

    source: VerdictSource
    checker: PassiveChecker | None
    check_seconds: float
    _mint: object = field(repr=False, compare=False)

    def __post_init__(self) -> None:
        if self._mint is not _MINT:
            raise TypeError(
                "a PassiveVerdict comes from _bus_tool.require_passive (or "
                "PassiveVerdict.job_controller), never from a hand-built constructor"
            )

    @classmethod
    def job_controller(cls) -> PassiveVerdict:
        """The verdict for a tool the daemon's ``JobController`` launched.

        ``tools/did_sweep.py`` is the one such tool. It skips the HTTP check
        *deliberately*: the job's argv (``src/control/jobs.py``) carries no
        daemon URL or token, and whether the sweep may transmit beside the
        scheduler is decided by whoever started the job (the ``start_job``
        caller arms ``set_passive_mode`` first if it wants the scheduler quiet;
        nothing on that path arms it automatically). Do not use this in a
        hand-run tool.
        """
        return cls("job_controller", None, 0.0, _MINT)


def add_bus_tool_args(
    parser: argparse.ArgumentParser,
    *,
    header_default: str = "7E0",
    session_default: str | None = "auto",
    passive: bool = True,
    passive_check_seconds: float = 5.0,
    timeout_alias: bool = False,
) -> None:
    """The bus-tool options, with one set of defaults.

    ``session_default=None`` omits ``--session`` (a tool that never holds a
    diagnostic session). ``passive=False`` omits the daemon options; only the
    ``JobController``-run ``did_sweep`` does that. ``timeout_alias`` keeps a
    tool's pre-R06.1 ``--timeout`` spelling working as a deprecated alias of
    ``--read-timeout``.
    """
    parser.add_argument(
        "--config", type=Path, default=Path("canary.yaml"), help="canary.yaml path."
    )
    parser.add_argument(
        "--header",
        default=header_default,
        help=f"ECU request CAN ID, hex (default {header_default}).",
    )
    if session_default is not None:
        parser.add_argument(
            "--session",
            default=session_default,
            help=(
                "UDS diagnostic-session sub-function (hex), 'auto' from data/dids.yaml's "
                f"access block, or 'none' for bare polling (default {session_default})."
            ),
        )
    parser.add_argument(
        "--read-timeout",
        *(("--timeout",) if timeout_alias else ()),
        dest="read_timeout",
        type=float,
        default=1.0,
        help="Per-request read timeout (default 1.0)."
        + (" --timeout is a deprecated alias." if timeout_alias else ""),
    )
    if not passive:
        return
    parser.add_argument(
        "--daemon-url",
        default=DEFAULT_DAEMON_URL,
        help=f"Daemon base URL for the passive-mode check (default {DEFAULT_DAEMON_URL}).",
    )
    parser.add_argument(
        "--passive-check-seconds",
        type=float,
        default=passive_check_seconds,
        help=f"Re-check daemon passive mode every N s (default {passive_check_seconds:g}).",
    )
    parser.add_argument(
        "--no-daemon",
        action="store_true",
        help="Skip the passive-mode check (bench use with no daemon running).",
    )


def parse_header(raw: str) -> int:
    """``--header`` -> an 11-bit request id, or ``ValueError`` naming the problem."""
    try:
        request_id = int(raw, 16)
    except ValueError:
        raise ValueError(f"--header must be hex (e.g. 7E0), got {raw!r}") from None
    if not 0 <= request_id <= 0x7FF:
        raise ValueError(f"--header out of 11-bit CAN range: 0x{request_id:X}")
    return request_id


def resolve_session(raw: str) -> int | None:
    """``--session``: hex, ``auto`` (``data/dids.yaml``'s access block) or ``none``."""
    normalized = raw.strip().lower()
    if normalized == "none":
        return None
    if normalized == "auto":
        access = did_access()
        return access.session if access is not None else None
    try:
        return int(raw, 16)
    except ValueError:
        raise ValueError(f"--session must be hex, 'auto', or 'none', got {raw!r}") from None


def require_passive(args: argparse.Namespace, *, reason: str = PASSIVE_REASON) -> PassiveVerdict:
    """Ask the daemon; return the verdict or raise :class:`PassiveRefusedError`.

    ``--no-daemon`` returns a verdict without asking (bench use). Otherwise
    the daemon must report passive armed with a TTL no shorter than
    ``--passive-check-seconds``; an unreachable daemon counts as not armed.
    """
    check_seconds = float(args.passive_check_seconds)
    if args.no_daemon:
        return PassiveVerdict("no_daemon", None, check_seconds, _MINT)
    checker = http_passive_checker(args.daemon_url, resolve_token())
    problem = passive_problem(checker(), check_seconds)
    if problem is not None:
        raise PassiveRefusedError(
            f"refusing: daemon passive mode is not usable ({problem}). It must stay armed "
            f"for the whole run because {reason}. Arm it with POST /control/passive "
            '{"enabled": true, "ttl_seconds": ...}, or pass --no-daemon when no daemon '
            "is running."
        )
    return PassiveVerdict("armed", checker, check_seconds, _MINT)


@contextmanager
def open_client(args: argparse.Namespace, passive: PassiveVerdict) -> Iterator[CanObdClient]:
    """Open the configured CAN bus and yield a client for ``--header``.

    Requires a :class:`PassiveVerdict`. A physical header gets the response
    window ``request + 8`` exactly: the client's default window is
    0x7E8-0x7EF, so without it a module outside that range (``--header 740``
    answers on 0x748) would never be heard. The functional header 0x7DF keeps
    the default window, where any ECU may answer.
    """
    if not isinstance(passive, PassiveVerdict):
        raise TypeError(
            "open_client needs a PassiveVerdict from require_passive(args); "
            f"got {type(passive).__name__}"
        )
    request_id = parse_header(args.header)
    config = CanaryConfig.from_yaml(args.config)
    bus = bus_factory(config.hardware.can)
    try:
        yield CanObdClient(
            bus,
            request_id=request_id,
            response_id_min=response_window(request_id)[0],
            response_id_max=response_window(request_id)[1],
            timeout_seconds=args.read_timeout,
        )
    finally:
        bus.shutdown()


def response_window(request_id: int) -> tuple[int, int]:
    """The response ids :func:`open_client`'s client accepts for ``request_id``:
    ``request + 8`` for a physical header, the default window for 0x7DF."""
    if request_id == DEFAULT_REQUEST_ID:
        return (DEFAULT_RESPONSE_ID_MIN, DEFAULT_RESPONSE_ID_MAX)
    return (request_id + 8, request_id + 8)


def responder_for(request_id: int) -> int | None:
    """The one response id :func:`open_client`'s client accepts, or ``None`` for any."""
    return expected_responder(request_id, response_window(request_id))


def load_widths(path: Path) -> dict[int, int]:
    """Measured payload widths from a did_sweep snapshot or a did_log JSONL header.

    Only a measurement counts: a catalog ``byte_length`` is the decoded value's
    width inside the payload, not the payload's own length.
    """
    text = path.read_text()
    try:
        raw: Any = json.loads(text)
    except json.JSONDecodeError:
        first = next((line for line in text.splitlines() if line.strip()), "")
        raw = json.loads(first)
    if not isinstance(raw, dict):
        raise ValueError(f"{path}: not a did_sweep snapshot or did_log header")
    if record_kind(raw) == "header" and isinstance(raw.get("widths"), dict):
        return {int(key, 16): int(width) for key, width in raw["widths"].items()}
    if isinstance(raw.get("values"), dict):
        return {int(key, 16): len(hexvalue) // 2 for key, hexvalue in raw["values"].items()}
    raise ValueError(f"{path}: not a did_sweep snapshot or did_log header")


def header_record(tool: str, *, started_at: float, **fields: Any) -> dict[str, Any]:
    """The first line of every bus-tool JSONL log.

    ``started_at`` is epoch seconds from the same wall clock as each record's
    ``t_send``/``t_recv`` (and the session parquet), never an ISO string.
    """
    return {RECORD_KEY: "header", "tool": tool, "started_at": float(started_at), **fields}


def record_kind(row: Mapping[str, Any]) -> str | None:
    """A JSONL record's discriminator; reads pre-R06.1 ``"type"`` logs too."""
    kind = row.get(RECORD_KEY, row.get("type"))
    return kind if isinstance(kind, str) else None


__all__ = [
    "PASSIVE_REASON",
    "PASSIVE_REFUSED_EXIT",
    "RECORD_KEY",
    "PassiveChecker",
    "PassiveRefusedError",
    "PassiveStatus",
    "PassiveVerdict",
    "add_bus_tool_args",
    "header_record",
    "http_passive_checker",
    "load_widths",
    "open_client",
    "parse_header",
    "passive_problem",
    "record_kind",
    "require_passive",
    "resolve_session",
    "resolve_token",
]
