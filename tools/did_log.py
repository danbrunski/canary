#!/usr/bin/env python3
"""Time-resolved Mode 22 DID logger for road-load DID discovery.

``tools/did_sweep.py`` does one pass and stamps the whole sweep with a single
``captured_at``, so it cannot resolve a DID's behaviour against fast events
(decel fuel cut, WOT). This tool logs every answering DID in ``[--start,
--end]`` continuously for ``--duration`` seconds, stamping each request with
``t_send``/``t_recv`` (``time.time()`` — the same Pi wall clock the session
parquet uses), so candidates can be found afterwards by aligning the JSONL
against a drive.

This tool sends only UDS service 0x22 (ReadDataByIdentifier) and, when
``--session`` resolves to something, 0x10 (DiagnosticSessionControl — no
memory write, no actuation), both through ``CanableMode22Transport``. No
other service is ever sent.

The daemon MUST be in passive mode for the whole run: its OBD scheduler sends
``10 81`` (exiting the 0xC0 session this logger holds) and Mode 01 polls that
our 0xC0 session blacks out. So this is enforced, not documented: the tool
refuses to start unless ``GET /control/passive`` reports armed, re-checks every
``--passive-check-seconds``, and stops cleanly (reason ``passive_disarmed``)
the moment passive is off, the daemon is unreachable, or the remaining TTL is
shorter than the check interval. ``--no-daemon`` skips the check for bench use
where no daemon is running.

Output (``--out``) is JSONL, line-buffered so a killed run keeps its data:

- ``header`` — tool, started_at, request_id, session, range, batch, widths, label
- ``read`` — one per request: t_send, t_recv, batch, values (hex), missing.
  A DID missing from a *batched* reading is listed in that record's
  ``missing`` and then re-read individually as its own record (never guessed).
  A batched request the ECU refused outright additionally carries
  ``"rejected": true`` and ``nrc`` (hex, or null when nothing answered).
- ``batch_rejected`` — written once, when a refused batch's DIDs then answer
  singly: t, dids, nrc, nrc_name, detail. Every later request is a single read.
- ``pass`` — one per complete pass over the DID list
- ``summary`` — reason (``duration`` | ``passive_disarmed`` | ``signal``, or
  ``no_dids`` for an empty DID list), passes, requests, reads_ok,
  reads_missing, batch_gaps, batch_rejections, batch_requested,
  batch_effective, batch_nrc, elapsed_s. ``reads_missing`` counts DIDs with no
  value after the individual re-read; ``batch_gaps`` counts DIDs an *accepted*
  batched reading omitted; ``batch_rejections`` counts batched requests the ECU
  refused as a whole; ``batch_effective`` is 1 whenever batching was abandoned.

Batching (``--batch > 1``) is not something every ECU does: the 2008 Nissan ECM
answers any multi-DID ReadDataByIdentifier with ``7F 22 12`` while answering
the same DIDs one at a time. So before logging, one batched group is probed; if
it is refused while its DIDs answer singly the tool refuses (exit 2) and names
the NRC, instead of silently re-reading every DID individually at several
times the intended request rate.

Run on the Pi from the repo root (daemon passive mode armed first):
  .venv/bin/python -m tools.did_log --config /etc/canary/canary.yaml \\
      --duration 1200 --label drive --out /data/canary/did_log.jsonl
"""

from __future__ import annotations

import argparse
import json
import os
import signal
import sys
import threading
import time
import urllib.error
import urllib.request
from collections.abc import Callable, Iterator, Sequence
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Protocol, TextIO

from src.analysis.did_scan import BatchRead, CanableMode22Transport, sweep_dids
from src.collection.can_collector import bus_factory
from src.collection.can_obd_client import CanObdClient
from src.collection.isotp import MAX_BATCHED_DIDS, nrc_name
from src.collection.obd_scheduler import did_access
from src.config import CanaryConfig

DEFAULT_TOKEN_PATH = Path("/etc/canary/control.token")

#: Why passive mode is mandatory — quoted in every refusal and stop message.
PASSIVE_REASON = (
    "the daemon's OBD scheduler sends 10 81 and exits the diagnostic session this "
    "logger holds, and this logger's session blacks out the daemon's Mode 01 polling"
)


class LogTransport(Protocol):
    def read_did(self, did: int) -> bytes | None: ...

    def read_dids(
        self, dids: Sequence[int], byte_length_for: Callable[[int], int | None]
    ) -> BatchRead: ...


@dataclass(frozen=True)
class PassiveStatus:
    armed: bool
    ttl_remaining_seconds: float | None
    detail: str


PassiveChecker = Callable[[], PassiveStatus]


def _did_key(did: int) -> str:
    return f"0x{did:04X}"


def _nrc_hex(nrc: int | None) -> str | None:
    return None if nrc is None else f"0x{nrc:02X}"


def _describe_rejection(nrc: int | None) -> str:
    if nrc is None:
        return "no response to multi-DID ReadDataByIdentifier"
    return f"NRC 0x{nrc:02X} ({nrc_name(nrc)}) to multi-DID ReadDataByIdentifier"


def parse_args(argv: Sequence[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        prog="did_log",
        description="Continuous, per-request-timestamped Mode 22 DID logger (JSONL).",
    )
    parser.add_argument(
        "--config", type=Path, default=Path("canary.yaml"), help="canary.yaml path."
    )
    parser.add_argument(
        "--start", type=lambda s: int(s, 0), default=0x1100, help="First DID (default 0x1100)."
    )
    parser.add_argument(
        "--end", type=lambda s: int(s, 0), default=0x12FF, help="Last DID inclusive (0x12FF)."
    )
    parser.add_argument(
        "--duration", type=float, required=True, help="Stop after this many seconds."
    )
    parser.add_argument("--label", default="", help="Free-form tag written into the header.")
    parser.add_argument("--out", type=Path, required=True, help="JSONL output path.")
    parser.add_argument(
        "--header", default="7E0", help="ECU request CAN ID, hex (default 7E0 = engine)."
    )
    parser.add_argument(
        "--session",
        default="auto",
        help=(
            "UDS diagnostic-session sub-function (hex). 'auto' (default) resolves from "
            "data/dids.yaml's access block; 'none' forces bare polling."
        ),
    )
    parser.add_argument(
        "--keepalive", type=float, default=2.0, help="Re-enter --session every N s (default 2.0)."
    )
    parser.add_argument(
        "--delay-seconds", type=float, default=0.0, help="Pause between requests, seconds."
    )
    parser.add_argument(
        "--read-timeout", type=float, default=1.0, help="Per-request read timeout (default 1.0)."
    )
    parser.add_argument(
        "--batch",
        type=int,
        default=1,
        help=(
            f"DIDs per request, 1-{MAX_BATCHED_DIDS} (default 1). Values > 1 are probed "
            "first and refused (exit 2) if the ECU rejects multi-DID requests."
        ),
    )
    parser.add_argument(
        "--widths-from",
        type=Path,
        default=None,
        help=(
            "Skip discovery: take measured payload widths from a did_sweep snapshot "
            "JSON or a previous did_log JSONL (its header line)."
        ),
    )
    parser.add_argument(
        "--daemon-url",
        default="http://127.0.0.1:8000",
        help="Daemon base URL for the passive-mode check (default http://127.0.0.1:8000).",
    )
    parser.add_argument(
        "--passive-check-seconds",
        type=float,
        default=5.0,
        help="Re-check daemon passive mode every N s (default 5).",
    )
    parser.add_argument(
        "--no-daemon",
        action="store_true",
        help="Skip the passive-mode check (bench use with no daemon running).",
    )
    return parser.parse_args(argv)


def _resolve_session(raw: str) -> int | None:
    normalized = raw.strip().lower()
    if normalized == "none":
        return None
    if normalized == "auto":
        access = did_access()
        return access.session if access is not None else None
    return int(raw, 16)


def _resolve_token() -> str | None:
    token = os.environ.get("CANARY_CONTROL_TOKEN")
    if token:
        return token.strip()
    try:
        return DEFAULT_TOKEN_PATH.read_text().strip() or None
    except OSError:
        return None


def http_passive_checker(
    base_url: str, token: str | None, *, timeout: float = 2.0
) -> PassiveChecker:
    """``GET {base_url}/control/passive`` → :class:`PassiveStatus`.

    Any failure (unreachable, auth refused, unexpected shape) reads as NOT
    armed: a daemon we cannot see may have restarted, and a restart comes
    back with passive off and its scheduler polling.
    """
    url = base_url.rstrip("/") + "/control/passive"

    def _check() -> PassiveStatus:
        request = urllib.request.Request(url, method="GET")
        if token:
            request.add_header("Authorization", f"Bearer {token}")
        try:
            with urllib.request.urlopen(request, timeout=timeout) as response:
                body = json.loads(response.read().decode("utf-8"))
        except urllib.error.HTTPError as exc:
            return PassiveStatus(False, None, f"GET {url} -> HTTP {exc.code}")
        except (urllib.error.URLError, OSError, ValueError) as exc:
            return PassiveStatus(False, None, f"GET {url} failed: {exc}")
        passive = body.get("passive") if isinstance(body, dict) else None
        if not isinstance(passive, dict) or "enabled" not in passive:
            return PassiveStatus(False, None, f"GET {url}: unexpected response {body!r}")
        ttl = passive.get("ttl_remaining_seconds")
        ttl_value = float(ttl) if isinstance(ttl, int | float) else None
        armed = passive["enabled"] is True
        return PassiveStatus(armed, ttl_value, "armed" if armed else "passive mode is off")

    return _check


def _passive_problem(status: PassiveStatus, check_seconds: float) -> str | None:
    """Why the run must not (continue to) transmit, or None if it may."""
    if not status.armed:
        return status.detail
    ttl = status.ttl_remaining_seconds
    if ttl is not None and ttl < check_seconds:
        return (
            f"passive TTL {ttl:.1f}s remaining is below the {check_seconds:g}s check "
            "interval; the daemon will resume polling"
        )
    return None


def load_widths(path: Path) -> dict[int, int]:
    """Measured payload widths from a did_sweep snapshot or a did_log JSONL header."""
    text = path.read_text()
    try:
        raw: Any = json.loads(text)
    except json.JSONDecodeError:
        first = next((line for line in text.splitlines() if line.strip()), "")
        raw = json.loads(first)
    if not isinstance(raw, dict):
        raise ValueError(f"{path}: not a did_sweep snapshot or did_log header")
    if raw.get("kind") == "header" and isinstance(raw.get("widths"), dict):
        return {int(key, 16): int(width) for key, width in raw["widths"].items()}
    if isinstance(raw.get("values"), dict):
        return {int(key, 16): len(hexvalue) // 2 for key, hexvalue in raw["values"].items()}
    raise ValueError(f"{path}: not a did_sweep snapshot or did_log header")


def discover_widths(
    transport: LogTransport, start: int, end: int, *, delay_seconds: float = 0.0
) -> dict[int, int]:
    """Unbatched sweep; each answering DID's payload length IS its measured width."""
    snapshot = sweep_dids(transport, range(start, end + 1), delay_seconds=delay_seconds)
    return {did: len(data) for did, data in snapshot.values.items()}


def _batch_groups(widths: dict[int, int], batch: int) -> list[list[int]]:
    batchable = [d for d in sorted(widths) if widths[d] > 0]
    return [batchable[i : i + batch] for i in range(0, len(batchable), batch)]


@dataclass(frozen=True)
class BatchProbe:
    """One batched group sent before logging, to learn whether the ECU batches."""

    dids: tuple[int, ...]
    rejected: bool
    negative_response_code: int | None
    #: How many of ``dids`` answered when then read one at a time (only read
    #: after a rejection; 0 otherwise).
    singles_ok: int

    @property
    def unsupported(self) -> bool:
        """Refused together, answered apart: this ECU does not batch."""
        return self.rejected and self.singles_ok > 0

    def refusal(self) -> str:
        return (
            f"refusing: --batch > 1 but this ECU rejects batched Mode 22 requests: "
            f"{_describe_rejection(self.negative_response_code)} for "
            f"[{', '.join(_did_key(d) for d in self.dids)}], while "
            f"{self.singles_ok} of those DIDs answered singly. Every DID would be "
            "re-read individually, so nothing would share a sample instant. Rerun "
            "with --batch 1."
        )


def probe_batching(
    transport: LogTransport, widths: dict[int, int], batch: int
) -> BatchProbe | None:
    """Send the first batched group once; ``None`` when there is nothing to batch.

    A group of fewer than two DIDs is not a multi-DID request, so it proves
    nothing about batching and is not probed.
    """
    batch = max(1, min(batch, MAX_BATCHED_DIDS))
    groups = _batch_groups(widths, batch) if batch > 1 else []
    if not groups or len(groups[0]) < 2:
        return None
    group = groups[0]

    def width_for(did: int) -> int | None:
        width = widths.get(did)
        return width if width is not None and width > 0 else None

    result = transport.read_dids(group, width_for)
    singles_ok = 0
    if result.rejected:
        singles_ok = sum(1 for did in group if transport.read_did(did) is not None)
    return BatchProbe(
        dids=tuple(group),
        rejected=result.rejected,
        negative_response_code=result.negative_response_code,
        singles_ok=singles_ok,
    )


class _Writer:
    def __init__(self, out: TextIO) -> None:
        self._out = out

    def write(self, record: dict[str, Any]) -> None:
        self._out.write(json.dumps(record, sort_keys=False) + "\n")
        self._out.flush()


def run(
    transport: LogTransport,
    widths: dict[int, int],
    out: TextIO,
    *,
    duration_seconds: float,
    batch: int = 3,
    request_id: int = 0x7E0,
    session: int | None = None,
    did_range: tuple[int, int] = (0x1100, 0x12FF),
    label: str = "",
    passive_checker: PassiveChecker | None = None,
    passive_check_seconds: float = 5.0,
    delay_seconds: float = 0.0,
    stop_event: threading.Event | None = None,
    wall_clock: Callable[[], float] = time.time,
    monotonic: Callable[[], float] = time.monotonic,
    sleep: Callable[[float], None] = time.sleep,
) -> dict[str, Any]:
    """Log ``widths``' DIDs until duration / passive disarm / stop_event.

    ``passive_checker=None`` means no daemon (bench). Returns the summary
    record, which is also the last line written.
    """
    writer = _Writer(out)
    batch = max(1, min(batch, MAX_BATCHED_DIDS))
    dids = sorted(widths)
    unbatchable = [d for d in dids if widths[d] <= 0]
    groups: list[tuple[list[int], bool]] = []
    if batch > 1:
        groups += [(group, True) for group in _batch_groups(widths, batch)]
        groups += [([d], False) for d in unbatchable]
    else:
        groups += [([d], False) for d in dids]

    def width_for(did: int) -> int | None:
        width = widths.get(did)
        return width if width is not None and width > 0 else None

    writer.write(
        {
            "kind": "header",
            "tool": "did_log",
            "started_at": wall_clock(),
            "request_id": f"0x{request_id:X}",
            "session": f"0x{session:X}" if session is not None else None,
            "range": [_did_key(did_range[0]), _did_key(did_range[1])],
            "batch": batch,
            "widths": {_did_key(d): widths[d] for d in dids},
            "label": label,
            "daemon_checked": passive_checker is not None,
        }
    )

    counts = {
        "passes": 0,
        "requests": 0,
        "reads_ok": 0,
        "reads_missing": 0,
        "batch_gaps": 0,
        "batch_rejections": 0,
    }
    # Flipped off, for the rest of the run, the first time a refused batch's
    # DIDs answer singly: a batch this ECU is known to refuse is never re-sent.
    batching = any(batched for _, batched in groups)
    last_nrc: int | None = None
    t0 = monotonic()
    last_check: float | None = None
    reason: str | None = None
    detail: str | None = None

    def should_stop() -> bool:
        nonlocal last_check, reason, detail
        if stop_event is not None and stop_event.is_set():
            reason = "signal"
            return True
        now = monotonic()
        if now - t0 >= duration_seconds:
            reason = "duration"
            return True
        if passive_checker is not None and (
            last_check is None or now - last_check >= passive_check_seconds
        ):
            last_check = now
            problem = _passive_problem(passive_checker(), passive_check_seconds)
            if problem is not None:
                reason, detail = "passive_disarmed", problem
                return True
        return False

    def single(did: int) -> bytes | None:
        t_send = wall_clock()
        data = transport.read_did(did)
        t_recv = wall_clock()
        counts["requests"] += 1
        if data is None:
            counts["reads_missing"] += 1
        else:
            counts["reads_ok"] += 1
        writer.write(
            {
                "kind": "read",
                "t_send": t_send,
                "t_recv": t_recv,
                "batch": False,
                "values": {} if data is None else {_did_key(did): data.hex()},
                "missing": [_did_key(did)] if data is None else [],
            }
        )
        if delay_seconds > 0:
            sleep(delay_seconds)
        return data

    if not groups:
        reason, detail = "no_dids", "no DIDs to log"

    while reason is None:
        pass_start = wall_clock()
        completed = True
        for group, batched in groups:
            if should_stop():
                completed = False
                break
            if not batched or not batching:
                for did in group:
                    single(did)
                continue
            t_send = wall_clock()
            result = transport.read_dids(group, width_for)
            t_recv = wall_clock()
            counts["requests"] += 1
            if result.rejected:
                counts["batch_rejections"] += 1
                if result.negative_response_code is not None:
                    last_nrc = result.negative_response_code
                writer.write(
                    {
                        "kind": "read",
                        "t_send": t_send,
                        "t_recv": t_recv,
                        "batch": True,
                        "values": {},
                        "missing": [_did_key(d) for d in group],
                        "rejected": True,
                        "nrc": _nrc_hex(result.negative_response_code),
                    }
                )
                if delay_seconds > 0:
                    sleep(delay_seconds)
                answered = [did for did in group if single(did) is not None]
                if answered:
                    batching = False
                    writer.write(
                        {
                            "kind": "batch_rejected",
                            "t": wall_clock(),
                            "dids": [_did_key(d) for d in group],
                            "nrc": _nrc_hex(result.negative_response_code),
                            "nrc_name": (
                                None
                                if result.negative_response_code is None
                                else nrc_name(result.negative_response_code)
                            ),
                            "detail": (
                                f"{_describe_rejection(result.negative_response_code)}, while "
                                f"{len(answered)} of {len(group)} DIDs answered singly; "
                                "switching to single reads for the rest of the run"
                            ),
                        }
                    )
                continue
            readings = result.readings
            gaps = [d for d in group if readings.get(d) is None]
            present = {d: readings[d] for d in group if readings.get(d) is not None}
            counts["reads_ok"] += len(present)
            counts["batch_gaps"] += len(gaps)
            writer.write(
                {
                    "kind": "read",
                    "t_send": t_send,
                    "t_recv": t_recv,
                    "batch": True,
                    "values": {_did_key(d): data.hex() for d, data in present.items()},
                    "missing": [_did_key(d) for d in gaps],
                }
            )
            if delay_seconds > 0:
                sleep(delay_seconds)
            for did in gaps:
                # The ECU omitted it or the walk desynced before reaching it —
                # re-read individually rather than guess.
                single(did)
        if completed:
            counts["passes"] += 1
            writer.write(
                {
                    "kind": "pass",
                    "n": counts["passes"],
                    "t_start": pass_start,
                    "t_end": wall_clock(),
                }
            )

    summary: dict[str, Any] = {
        "kind": "summary",
        "reason": reason,
        **counts,
        "batch_requested": batch,
        "batch_effective": batch if batching else 1,
        "batch_nrc": _nrc_hex(last_nrc),
        "elapsed_s": round(monotonic() - t0, 3),
    }
    if detail is not None:
        summary["detail"] = detail
    writer.write(summary)
    return summary


@contextmanager
def _open_transport(
    config_path: Path,
    *,
    request_id: int,
    session: int | None,
    keepalive: float,
    read_timeout: float,
) -> Iterator[LogTransport]:
    config = CanaryConfig.from_yaml(config_path)
    bus = bus_factory(config.hardware.can)
    try:
        client = CanObdClient(
            bus,
            request_id=request_id,
            response_id_min=request_id + 8,
            response_id_max=request_id + 8,
            timeout_seconds=read_timeout,
        )
        yield CanableMode22Transport(
            client, request_id=request_id, session=session, keepalive_seconds=keepalive
        )
    finally:
        bus.shutdown()


def main(argv: Sequence[str] | None = None) -> int:
    args = parse_args(argv)
    if not 0 <= args.start <= args.end <= 0xFFFF:
        print("error: DID range must satisfy 0 <= start <= end <= 0xFFFF", file=sys.stderr)
        return 1
    try:
        request_id = int(args.header, 16)
    except ValueError:
        print(f"error: --header must be hex (e.g. 7E0), got {args.header!r}", file=sys.stderr)
        return 1
    if not 0 <= request_id <= 0x7FF:
        print(f"error: --header out of 11-bit CAN range: 0x{request_id:X}", file=sys.stderr)
        return 1
    if not 1 <= args.batch <= MAX_BATCHED_DIDS:
        print(f"error: --batch must be 1-{MAX_BATCHED_DIDS}, got {args.batch}", file=sys.stderr)
        return 1
    if args.duration <= 0 or args.passive_check_seconds <= 0:
        print("error: --duration and --passive-check-seconds must be > 0", file=sys.stderr)
        return 1
    try:
        session = _resolve_session(args.session)
    except ValueError:
        print(
            f"error: --session must be hex, 'auto', or 'none', got {args.session!r}",
            file=sys.stderr,
        )
        return 1

    widths: dict[int, int] | None = None
    if args.widths_from is not None:
        try:
            loaded = load_widths(args.widths_from)
        except (OSError, ValueError) as exc:
            print(f"error: --widths-from unreadable: {exc}", file=sys.stderr)
            return 1
        widths = {d: w for d, w in loaded.items() if args.start <= d <= args.end}

    checker: PassiveChecker | None = None
    if not args.no_daemon:
        checker = http_passive_checker(args.daemon_url, _resolve_token())
        problem = _passive_problem(checker(), args.passive_check_seconds)
        if problem is not None:
            print(
                f"refusing: daemon passive mode is not usable ({problem}). It must stay "
                f"armed for the whole run because {PASSIVE_REASON}. Arm it with "
                'POST /control/passive {"enabled": true, "ttl_seconds": ...}, or pass '
                "--no-daemon when no daemon is running.",
                file=sys.stderr,
            )
            return 2

    stop_event = threading.Event()
    previous_handlers = {
        sig: signal.signal(sig, lambda *_: stop_event.set())
        for sig in (signal.SIGINT, signal.SIGTERM)
    }
    try:
        with _open_transport(
            args.config,
            request_id=request_id,
            session=session,
            keepalive=args.keepalive,
            read_timeout=args.read_timeout,
        ) as transport:
            session_desc = f"0x{session:X}" if session is not None else "none"
            if widths is None:
                print(
                    f"did_log: discovery sweep 0x{args.start:04X}-0x{args.end:04X} "
                    f"req=0x{request_id:X} session={session_desc}",
                    file=sys.stderr,
                )
                widths = discover_widths(
                    transport, args.start, args.end, delay_seconds=args.delay_seconds
                )
            if not widths:
                print(
                    "error: no DIDs answered in 0x"
                    f"{args.start:04X}-0x{args.end:04X} (or none in --widths-from); "
                    "nothing to log",
                    file=sys.stderr,
                )
                return 1
            if args.batch > 1:
                probe = probe_batching(transport, widths, args.batch)
                if probe is not None and probe.unsupported:
                    print(probe.refusal(), file=sys.stderr)
                    return 2
            print(
                f"did_log: logging {len(widths)} DIDs batch={args.batch} for "
                f"{args.duration:g}s -> {args.out}",
                file=sys.stderr,
            )
            args.out.parent.mkdir(parents=True, exist_ok=True)
            with args.out.open("w", buffering=1) as out:
                summary = run(
                    transport,
                    widths,
                    out,
                    duration_seconds=args.duration,
                    batch=args.batch,
                    request_id=request_id,
                    session=session,
                    did_range=(args.start, args.end),
                    label=args.label,
                    passive_checker=checker,
                    passive_check_seconds=args.passive_check_seconds,
                    delay_seconds=args.delay_seconds,
                    stop_event=stop_event,
                )
    finally:
        for sig, handler in previous_handlers.items():
            signal.signal(sig, handler)

    print(
        f"did_log: stopped ({summary['reason']}) after {summary['passes']} passes, "
        f"{summary['requests']} requests, {summary['reads_ok']} ok / "
        f"{summary['reads_missing']} missing, batch_effective={summary['batch_effective']}",
        file=sys.stderr,
    )
    if summary["batch_effective"] < summary["batch_requested"]:
        print(
            f"did_log: WARNING batching was abandoned mid-run after "
            f"{summary['batch_rejections']} rejected batch(es) "
            f"(NRC {summary['batch_nrc']}); later reads are unbatched — see the "
            "batch_rejected record",
            file=sys.stderr,
        )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
