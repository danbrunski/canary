#!/usr/bin/env python3
"""Probe for a same-instant multi-value read service on an ECM.

Batched Mode 22 is refused by the 2008 Nissan ECM (``7F 22 12`` to any
multi-DID read, measured 2026-09-17), so "batching" — one request that returns
several values sampled at one instant — has to come from another read service
if it exists at all. This tool asks the ECM, once, which of three candidates it
serves:

* ``0x21`` ReadDataByLocalIdentifier, swept over local IDs — a CONSULT-style
  data-monitor block shows up as a multi-byte (often multi-frame) positive
  response to one identifier.
* ``0x2A`` ReadDataByPeriodicIdentifier — the ECU streams values on a schedule.
  A positive answer is immediately followed by the *stop* request so the ECM
  is never left streaming.
* ``0x23`` ReadMemoryByAddress — how UpRev logs at 16 Hz over K-line. Tried in
  the KWP2000 3-byte-address form and the UDS ALFID form. On ``0x33``
  (securityAccessDenied) the tool sends ``0x27`` requestSeed, records the seed,
  and **stops**: no key is ever built (:data:`RAW_QUERY_SERVICES` and
  ``query_raw`` make a sendKey unconstructible, not merely un-sent).

Every result is classified as ``works`` (a payload came back) / ``answered``
(the service exists but refused the argument) / ``gated`` / ``unsupported`` /
``silent`` — five different empties that demand different next steps — and
written to a JSON snapshot beside a human summary. Only ``works`` makes a
service a same-instant-read candidate.

Like ``did_log``, this holds the diagnostic session the ECM gates its reads
behind, so the daemon must be in passive mode for the run (its scheduler's
``10 81`` would drop the session, and our ``10 C0`` blacks out its Mode 01).
The tool refuses to start otherwise. Run it with the engine idling and the
truck parked: a SecurityAccess request can start an ECM lockout timer, which
is harmless but not something to learn about at speed.

Sends only: 0x10 (session), 0x21, 0x23, 0x27 requestSeed, 0x2A (+ its stop).
"""

from __future__ import annotations

import argparse
import json
import sys
import time
from collections.abc import Callable, Iterator, Sequence
from contextlib import contextmanager
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Protocol

from src.collection.can_bus import bus_factory
from src.collection.can_obd_client import CanObdClient, RawReply
from src.config import CanaryConfig
from tools.did_log import (
    PASSIVE_REASON,
    PassiveChecker,
    _passive_problem,
    _resolve_session,
    _resolve_token,
    http_passive_checker,
)

#: NRCs that mean "this service/form does not exist here".
_UNSUPPORTED_NRCS = frozenset({0x11, 0x12, 0x7F})
#: NRCs that mean "exists, but not for you right now".
_GATED_NRCS = frozenset({0x33, 0x35, 0x36, 0x37, 0x7E})
#: NRCs that mean "the service exists; the argument was wrong" — for a probe
#: that is a positive finding about the service, so it counts as ``works``.
_ARGUMENT_NRCS = frozenset({0x13, 0x31})

#: Re-send the session prime at least this often during a sweep.
_SESSION_REFRESH_SECONDS = 1.5

#: ``works`` means a positive payload came back — nothing less. ``answered``
#: means the service exists but refused the argument (``0x13``/``0x31``): a
#: fact about the service, never a same-instant-read candidate.
WORKS, ANSWERED, GATED, UNSUPPORTED, SILENT = (
    "works",
    "answered",
    "gated",
    "unsupported",
    "silent",
)
#: Summary ranking when several request forms were tried for one service.
_RANK = (WORKS, GATED, ANSWERED, UNSUPPORTED, SILENT)


class RawQuerier(Protocol):
    def query_raw(
        self, service: int, body: bytes, *, request_id: int | None = None
    ) -> RawReply: ...

    def enter_diagnostic_session(
        self, subfunction: int, *, request_id: int | None = None
    ) -> bool: ...


@dataclass
class ProbeResult:
    service: str
    request: str
    verdict: str
    detail: str
    payload_hex: str | None = None
    nrc: str | None = None
    extra: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return {
            "service": self.service,
            "request": self.request,
            "verdict": self.verdict,
            "detail": self.detail,
            "payload_hex": self.payload_hex,
            "nrc": self.nrc,
            **({"extra": self.extra} if self.extra else {}),
        }


def classify(reply: RawReply) -> tuple[str, str]:
    """Verdict + one-line reason for a single reply."""
    if not reply.sent:
        return SILENT, "request was not sent (transmit gate or bus refused)"
    if reply.positive:
        return WORKS, f"positive response, {len(reply.payload or b'')} byte(s)"
    nrc = reply.negative_response_code
    if nrc is None:
        return SILENT, "no response within the timeout"
    if nrc in _GATED_NRCS:
        return GATED, f"NRC 0x{nrc:02X} — exists but gated (security/session/conditions)"
    if nrc in _ARGUMENT_NRCS:
        return ANSWERED, f"NRC 0x{nrc:02X} — the service answered; the argument was refused"
    if nrc in _UNSUPPORTED_NRCS:
        return UNSUPPORTED, f"NRC 0x{nrc:02X} — service or form not supported"
    return UNSUPPORTED, f"NRC 0x{nrc:02X}"


def _nrc(reply: RawReply) -> str | None:
    n = reply.negative_response_code
    return None if n is None else f"0x{n:02X}"


class _Session:
    """Keeps the ECM's diagnostic session primed between probes."""

    def __init__(
        self, client: RawQuerier, session: int | None, request_id: int, clock: Callable[[], float]
    ) -> None:
        self._client = client
        self._session = session
        self._request_id = request_id
        self._clock = clock
        self._last: float | None = None

    def ensure(self) -> None:
        if self._session is None:
            return
        now = self._clock()
        if self._last is not None and now - self._last < _SESSION_REFRESH_SECONDS:
            return
        if self._client.enter_diagnostic_session(self._session, request_id=self._request_id):
            self._last = now


def probe_local_identifiers(
    client: RawQuerier,
    session: _Session,
    *,
    request_id: int,
    start: int,
    end: int,
    delay_seconds: float = 0.0,
    sleep: Callable[[float], None] = time.sleep,
) -> tuple[ProbeResult, list[ProbeResult]]:
    """Sweep ``0x21`` local identifiers; return the overall verdict and hits."""
    hits: list[ProbeResult] = []
    nrcs: dict[str, int] = {}
    silent = 0
    for lid in range(start, end + 1):
        session.ensure()
        reply = client.query_raw(0x21, bytes([lid]), request_id=request_id)
        verdict, detail = classify(reply)
        if reply.positive:
            payload = reply.payload or b""
            hits.append(
                ProbeResult(
                    service="0x21",
                    request=f"21 {lid:02X}",
                    verdict=WORKS,
                    detail=detail + (" (multi-frame)" if len(payload) > 6 else ""),
                    payload_hex=payload.hex(),
                    extra={"local_id": f"0x{lid:02X}", "length": len(payload)},
                )
            )
        elif reply.negative_response_code is not None:
            key = _nrc(reply) or "?"
            nrcs[key] = nrcs.get(key, 0) + 1
        else:
            silent += 1
        if delay_seconds > 0:
            sleep(delay_seconds)
    total = end - start + 1
    if hits:
        verdict = WORKS
        detail = f"{len(hits)} of {total} local identifiers answered"
    elif nrcs:
        top = max(nrcs.items(), key=lambda kv: kv[1])[0]
        codes = {int(k, 16) for k in nrcs}
        if codes & _GATED_NRCS:
            verdict = GATED
        elif codes & _ARGUMENT_NRCS:
            verdict = ANSWERED
        else:
            verdict = UNSUPPORTED
        detail = f"no positive answer in {total}; NRCs {nrcs} (most common {top})"
    else:
        verdict = SILENT
        detail = f"no response to any of {total} local identifiers"
    overall = ProbeResult(
        service="0x21",
        request=f"21 {start:02X}..{end:02X}",
        verdict=verdict,
        detail=detail,
        extra={"hits": len(hits), "nrcs": nrcs, "silent": silent},
    )
    return overall, hits


def probe_periodic(client: RawQuerier, session: _Session, *, request_id: int) -> list[ProbeResult]:
    """``0x2A`` at the slowest rate for one periodic identifier, then STOP.

    The stop is unconditional on a positive answer: an ECM left streaming
    would keep transmitting after this tool exits and pollute the daemon's
    receive path once passive mode lifts.
    """
    session.ensure()
    start = client.query_raw(0x2A, bytes([0x01, 0x01]), request_id=request_id)
    verdict, detail = classify(start)
    results = [
        ProbeResult(
            service="0x2A",
            request="2A 01 01",
            verdict=verdict,
            detail=detail,
            payload_hex=None if start.payload is None else start.payload.hex(),
            nrc=_nrc(start),
        )
    ]
    if start.positive:
        stop = client.query_raw(0x2A, bytes([0x04]), request_id=request_id)
        sv, sd = classify(stop)
        results.append(
            ProbeResult(
                service="0x2A",
                request="2A 04 (stop)",
                verdict=sv,
                detail="streaming stopped: " + sd,
                payload_hex=None if stop.payload is None else stop.payload.hex(),
                nrc=_nrc(stop),
            )
        )
    return results


def probe_memory(
    client: RawQuerier, session: _Session, *, request_id: int, address: int, length: int
) -> list[ProbeResult]:
    """``0x23`` in three request forms, because the ECM only answers one.

    ``kwp24``: 3-byte address + 1-byte size (ISO 14230). ``kwp32``: 4-byte
    address + 2-byte size, no format byte — the form D03.17 records as
    having answered ``23 00 08 00 00 00 10`` under session ``10 FB`` on this
    ECM. ``uds``: ISO 14229 with ALFID ``0x14`` (1-byte size, 4-byte
    address). A form the ECM does not parse comes back ``0x13``
    (incorrectMessageLengthOrInvalidFormat), which :func:`classify` counts as
    the service *answering* — so one positive form beside two ``0x13``\\ s is
    the expected shape of success, not a contradiction.
    """
    forms = [
        (
            "kwp24",
            bytes([(address >> 16) & 0xFF, (address >> 8) & 0xFF, address & 0xFF, length & 0xFF]),
        ),
        ("kwp32", address.to_bytes(4, "big") + (length & 0xFFFF).to_bytes(2, "big")),
        ("uds", bytes([0x14]) + address.to_bytes(4, "big") + bytes([length & 0xFF])),
    ]
    results: list[ProbeResult] = []
    for name, body in forms:
        session.ensure()
        reply = client.query_raw(0x23, body, request_id=request_id)
        verdict, detail = classify(reply)
        results.append(
            ProbeResult(
                service="0x23",
                request=f"23 {body.hex(' ').upper()} ({name})",
                verdict=verdict,
                detail=detail,
                payload_hex=None if reply.payload is None else reply.payload.hex(),
                nrc=_nrc(reply),
                extra={"form": name, "address": f"0x{address:08X}", "length": length},
            )
        )
    return results


def probe_seed(client: RawQuerier, session: _Session, *, request_id: int) -> ProbeResult:
    """``0x27 01`` requestSeed. The seed is evidence; no key is ever built."""
    session.ensure()
    reply = client.query_raw(0x27, bytes([0x01]), request_id=request_id)
    verdict, detail = classify(reply)
    if reply.positive:
        detail = f"seed returned ({len(reply.payload or b'')} byte(s)); no key sent"
    return ProbeResult(
        service="0x27",
        request="27 01 (requestSeed)",
        verdict=verdict,
        detail=detail,
        payload_hex=None if reply.payload is None else reply.payload.hex(),
        nrc=_nrc(reply),
    )


def run(
    client: RawQuerier,
    *,
    request_id: int,
    session_subfunction: int | None,
    lid_range: tuple[int, int],
    address: int,
    length: int,
    skip: frozenset[str] = frozenset(),
    delay_seconds: float = 0.0,
    clock: Callable[[], float] = time.monotonic,
    sleep: Callable[[float], None] = time.sleep,
) -> dict[str, Any]:
    """Run every probe not in ``skip`` and return the snapshot dict."""
    session = _Session(client, session_subfunction, request_id, clock)
    probes: dict[str, Any] = {}
    summary: dict[str, str] = {}

    if "0x21" not in skip:
        overall, hits = probe_local_identifiers(
            client,
            session,
            request_id=request_id,
            start=lid_range[0],
            end=lid_range[1],
            delay_seconds=delay_seconds,
            sleep=sleep,
        )
        probes["0x21"] = {"overall": overall.to_dict(), "hits": [h.to_dict() for h in hits]}
        summary["0x21"] = overall.verdict
    if "0x2A" not in skip:
        periodic = probe_periodic(client, session, request_id=request_id)
        probes["0x2A"] = [r.to_dict() for r in periodic]
        summary["0x2A"] = periodic[0].verdict
    if "0x23" not in skip:
        memory = probe_memory(
            client, session, request_id=request_id, address=address, length=length
        )
        probes["0x23"] = [r.to_dict() for r in memory]
        verdicts = {r.verdict for r in memory}
        summary["0x23"] = next(v for v in _RANK if v in verdicts)
    if "0x27" not in skip:
        seed = probe_seed(client, session, request_id=request_id)
        probes["0x27"] = seed.to_dict()
        summary["0x27"] = seed.verdict

    same_instant = [s for s in ("0x21", "0x23", "0x2A") if summary.get(s) == WORKS]
    return {
        "tool": "batch_probe",
        "started_at": datetime.now(UTC).isoformat(),
        "request_id": f"0x{request_id:X}",
        "session": None if session_subfunction is None else f"0x{session_subfunction:X}",
        "probes": probes,
        "summary": summary,
        "same_instant_candidates": same_instant,
    }


def format_summary(snapshot: dict[str, Any]) -> str:
    lines = ["service  verdict      detail"]
    for service, verdict in snapshot["summary"].items():
        block = snapshot["probes"][service]
        if isinstance(block, dict) and "overall" in block:
            detail = block["overall"]["detail"]
        elif isinstance(block, list):
            detail = "; ".join(r["detail"] for r in block)
        else:
            detail = block["detail"]
        lines.append(f"{service:8} {verdict:12} {detail}")
    cands = snapshot["same_instant_candidates"]
    lines.append(
        "same-instant multi-value read: "
        + (", ".join(cands) if cands else "none of 0x21 / 0x23 / 0x2A answered positively")
    )
    return "\n".join(lines)


def parse_args(argv: Sequence[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        prog="batch_probe",
        description="Probe an ECM for a same-instant multi-value read service (0x21/0x23/0x2A).",
    )
    parser.add_argument("--config", type=Path, default=Path("canary.yaml"))
    parser.add_argument("--out", type=Path, required=True, help="JSON snapshot path.")
    parser.add_argument("--header", default="7E0", help="ECU request CAN ID, hex.")
    parser.add_argument(
        "--session",
        default="auto",
        help="Diagnostic session sub-function (hex), 'auto' from data/dids.yaml, or 'none'.",
    )
    parser.add_argument(
        "--lid-start", type=lambda s: int(s, 0), default=0x00, help="First 0x21 local ID."
    )
    parser.add_argument(
        "--lid-end", type=lambda s: int(s, 0), default=0xFF, help="Last 0x21 local ID."
    )
    parser.add_argument(
        "--address",
        type=lambda s: int(s, 0),
        default=0xFFFF8000,
        help="0x23 address to try (default: on-chip RAM base typical of SH-2 ECMs).",
    )
    parser.add_argument("--length", type=int, default=8, help="0x23 read size, bytes.")
    parser.add_argument(
        "--skip",
        default="",
        help="Comma-separated probes to skip, from 0x21,0x23,0x27,0x2A.",
    )
    parser.add_argument("--delay-seconds", type=float, default=0.0)
    parser.add_argument("--read-timeout", type=float, default=1.0)
    parser.add_argument("--daemon-url", default="http://127.0.0.1:8000")
    parser.add_argument("--no-daemon", action="store_true", help="Skip the passive check.")
    return parser.parse_args(argv)


@contextmanager
def _open_client(
    config_path: Path, *, request_id: int, read_timeout: float
) -> Iterator[CanObdClient]:
    config = CanaryConfig.from_yaml(config_path)
    bus = bus_factory(config.hardware.can)
    try:
        yield CanObdClient(
            bus,
            request_id=request_id,
            response_id_min=request_id + 8,
            response_id_max=request_id + 8,
            timeout_seconds=read_timeout,
        )
    finally:
        bus.shutdown()


def main(argv: Sequence[str] | None = None) -> int:
    args = parse_args(argv)
    try:
        request_id = int(args.header, 16)
    except ValueError:
        print(f"error: --header must be hex, got {args.header!r}", file=sys.stderr)
        return 1
    if not 0 <= args.lid_start <= args.lid_end <= 0xFF:
        print("error: local identifiers must satisfy 0 <= start <= end <= 0xFF", file=sys.stderr)
        return 1
    if not 1 <= args.length <= 0xFF:
        print("error: --length must be 1-255", file=sys.stderr)
        return 1
    skip = frozenset(s.strip() for s in args.skip.split(",") if s.strip())
    unknown = skip - {"0x21", "0x23", "0x27", "0x2A"}
    if unknown:
        print(f"error: unknown --skip entries {sorted(unknown)}", file=sys.stderr)
        return 1
    try:
        session = _resolve_session(args.session)
    except ValueError:
        print(
            f"error: --session must be hex, 'auto', or 'none', got {args.session!r}",
            file=sys.stderr,
        )
        return 1

    if not args.no_daemon:
        checker: PassiveChecker = http_passive_checker(args.daemon_url, _resolve_token())
        problem = _passive_problem(checker(), 30.0)
        if problem is not None:
            print(
                f"refusing: daemon passive mode is not usable ({problem}). It must be armed "
                f"for the run because {PASSIVE_REASON}. Arm it with POST /control/passive "
                '{"enabled": true, "ttl_seconds": 600}, or pass --no-daemon with no daemon.',
                file=sys.stderr,
            )
            return 2

    with _open_client(args.config, request_id=request_id, read_timeout=args.read_timeout) as client:
        snapshot = run(
            client,
            request_id=request_id,
            session_subfunction=session,
            lid_range=(args.lid_start, args.lid_end),
            address=args.address,
            length=args.length,
            skip=skip,
            delay_seconds=args.delay_seconds,
        )
    args.out.parent.mkdir(parents=True, exist_ok=True)
    args.out.write_text(json.dumps(snapshot, indent=2) + "\n")
    print(format_summary(snapshot))
    print(f"snapshot: {args.out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
