#!/usr/bin/env python3
"""Read-only UpRev K-line discovery and dynamic-stream probe.

The tool always runs the capture-proven bootstrap and known-answer check before
reporting novel data.  It has no raw-request option: every operation reachable
from this CLI is a diagnostic/session/read operation represented explicitly in
``src.collection.uprev_session``.
"""

from __future__ import annotations

import argparse
import json
import sys
import time
from datetime import UTC, datetime
from pathlib import Path

from src.collection.uprev_protocol import (
    STREAM_POLL,
    build_memory_byte_stream_definition,
    build_mixed_stream_definition,
    build_stream_definition,
    parse_stream_data,
    parse_stream_response,
)
from src.collection.uprev_session import (
    UpRevDiagnosticSession,
    UpRevNegativeResponseError,
    supported_dids,
)
from src.collection.uprev_usb import UpRevUsbError, UpRevUsbTransport


def _did(value: str) -> int:
    try:
        identifier = int(value, 0)
    except ValueError as exc:
        raise argparse.ArgumentTypeError(f"invalid DID {value!r}") from exc
    if not 0 <= identifier <= 0xFFFF:
        raise argparse.ArgumentTypeError("DID must fit in 16 bits")
    return identifier


def _stream_spec(value: str) -> tuple[int, int]:
    did_text, separator, width_text = value.partition(":")
    if not separator:
        raise argparse.ArgumentTypeError("stream item must be DID:WIDTH, e.g. 0x1201:2")
    identifier = _did(did_text)
    try:
        width = int(width_text)
    except ValueError as exc:
        raise argparse.ArgumentTypeError(f"invalid width {width_text!r}") from exc
    if not 1 <= width <= 32:
        raise argparse.ArgumentTypeError("width must be between 1 and 32")
    return identifier, width


def _address(value: str) -> int:
    try:
        address = int(value, 0)
    except ValueError as exc:
        raise argparse.ArgumentTypeError(f"invalid address {value!r}") from exc
    if not 0 <= address <= 0xFFFFFFFF:
        raise argparse.ArgumentTypeError("address must fit in 32 bits")
    return address


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--serial", default="12345678")
    parser.add_argument(
        "--scan-supported",
        action="store_true",
        help="Read every DID advertised by the ECU support masks once.",
    )
    parser.add_argument(
        "--stream-supported",
        action="store_true",
        help="Discover widths, then stream every supported DID together.",
    )
    parser.add_argument(
        "--did",
        action="append",
        type=_did,
        default=[],
        help="Read one explicit DID once; repeatable.",
    )
    parser.add_argument(
        "--stream",
        action="append",
        type=_stream_spec,
        default=[],
        metavar="DID:WIDTH",
        help="Add a DID and known byte width to one dynamic stream.",
    )
    parser.add_argument(
        "--memory-byte",
        action="append",
        type=_address,
        default=[],
        metavar="ADDRESS",
        help=(
            "Add one read-only absolute RAM byte to a memory stream; repeatable. "
            "This emits only the capture-proven 0x83 descriptor form."
        ),
    )
    parser.add_argument("--samples", type=int, default=100)
    parser.add_argument(
        "--attempts",
        type=int,
        default=5,
        help="Reconnect and repeat the complete bootstrap after a transient failure.",
    )
    parser.add_argument(
        "--chunk-size",
        type=int,
        default=12,
        help="DIDs per dynamic definition with --stream-supported (default: 12).",
    )
    parser.add_argument("--out", type=Path, help="Also write JSONL results to this path.")
    parser.add_argument(
        "--quiet",
        action="store_true",
        help="Write only to --out instead of echoing every JSON row.",
    )
    args = parser.parse_args()
    if (
        not args.scan_supported
        and not args.stream_supported
        and not args.did
        and not args.stream
        and not args.memory_byte
    ):
        parser.error(
            "choose --scan-supported, --stream-supported, --did, --stream, or --memory-byte"
        )
    if args.samples <= 0:
        parser.error("--samples must be positive")
    if args.attempts <= 0:
        parser.error("--attempts must be positive")
    if not 1 <= args.chunk_size <= 64:
        parser.error("--chunk-size must be between 1 and 64")
    if args.quiet and args.out is None:
        parser.error("--quiet requires --out")
    return args


class _Emitter:
    def __init__(self, path: Path | None, *, quiet: bool) -> None:
        self._handle = None
        self._quiet = quiet
        if path is not None:
            path.parent.mkdir(parents=True, exist_ok=True)
            self._handle = path.open("x", encoding="utf-8")

    def emit(self, kind: str, **fields: object) -> None:
        row = {
            "timestamp": datetime.now(UTC).isoformat(),
            "time_ns": time.time_ns(),
            "kind": kind,
            **fields,
        }
        line = json.dumps(row, separators=(",", ":"))
        if not self._quiet:
            print(line, flush=True)
        if self._handle is not None:
            self._handle.write(line + "\n")
            self._handle.flush()

    def close(self) -> None:
        if self._handle is not None:
            self._handle.close()


def _scan(
    session: UpRevDiagnosticSession,
    identifiers: list[int] | tuple[int, ...],
    emitter: _Emitter,
) -> dict[int, int]:
    widths: dict[int, int] = {}
    for identifier in identifiers:
        started_ns = time.time_ns()
        try:
            raw = session.read_did(identifier)
            emitter.emit(
                "did",
                did=f"0x{identifier:04x}",
                raw=raw.hex(),
                raw_int=int.from_bytes(raw, "big"),
                width=len(raw),
                latency_ms=(time.time_ns() - started_ns) / 1_000_000,
            )
            widths[identifier] = len(raw)
        except UpRevNegativeResponseError as exc:
            emitter.emit(
                "did_negative",
                did=f"0x{identifier:04x}",
                service=f"0x{exc.service:02x}",
                nrc=f"0x{exc.code:02x}",
                latency_ms=(time.time_ns() - started_ns) / 1_000_000,
            )
    return widths


def _stream(
    session: UpRevDiagnosticSession,
    specs: list[tuple[int, int]],
    samples: int,
    emitter: _Emitter,
) -> None:
    identifiers = tuple(identifier for identifier, _width in specs)
    widths = dict(specs)
    acknowledgement = session.transport.exchange(
        build_stream_definition(identifiers), timeout_seconds=3.0
    ).payload
    if not acknowledgement.startswith(bytes.fromhex("6602ec81")):
        raise UpRevUsbError(f"stream definition was refused: {acknowledgement.hex()}")
    emitter.emit("stream_definition", dids=[f"0x{x:04x}" for x in identifiers])
    for sequence in range(samples):
        started_ns = time.time_ns()
        response = session.transport.exchange(STREAM_POLL, timeout_seconds=3.0).payload
        readings = parse_stream_response(response, identifiers, widths)
        emitter.emit(
            "stream_sample",
            sequence=sequence,
            latency_ms=(time.time_ns() - started_ns) / 1_000_000,
            values={
                f"0x{identifier:04x}": {
                    "raw": raw.hex(),
                    "raw_int": int.from_bytes(raw, "big"),
                    "width": len(raw),
                }
                for identifier, raw in readings.items()
            },
        )


def _memory_byte_stream(
    session: UpRevDiagnosticSession,
    addresses: list[int],
    samples: int,
    emitter: _Emitter,
) -> None:
    acknowledgement = session.transport.exchange(
        build_memory_byte_stream_definition(addresses), timeout_seconds=3.0
    ).payload
    if not acknowledgement.startswith(bytes.fromhex("6602ec81")):
        raise UpRevUsbError(f"memory stream definition was refused: {acknowledgement.hex()}")
    emitter.emit("memory_stream_definition", addresses=[f"0x{x:08x}" for x in addresses])
    for sequence in range(samples):
        started_ns = time.time_ns()
        response = session.transport.exchange(STREAM_POLL, timeout_seconds=3.0).payload
        readings = parse_stream_data(response, [1] * len(addresses))
        emitter.emit(
            "memory_stream_sample",
            sequence=sequence,
            latency_ms=(time.time_ns() - started_ns) / 1_000_000,
            values={
                f"0x{address:08x}": {"raw": raw.hex(), "raw_int": raw[0], "width": 1}
                for address, raw in zip(addresses, readings, strict=True)
            },
        )


def _mixed_stream(
    session: UpRevDiagnosticSession,
    specs: list[tuple[int, int]],
    addresses: list[int],
    samples: int,
    emitter: _Emitter,
) -> None:
    identifiers = tuple(identifier for identifier, _width in specs)
    widths = [width for _identifier, width in specs]
    acknowledgement = session.transport.exchange(
        build_mixed_stream_definition(identifiers, addresses), timeout_seconds=3.0
    ).payload
    if not acknowledgement.startswith(bytes.fromhex("6602ec81")):
        raise UpRevUsbError(f"mixed stream definition was refused: {acknowledgement.hex()}")
    emitter.emit(
        "mixed_stream_definition",
        dids=[f"0x{x:04x}" for x in identifiers],
        addresses=[f"0x{x:08x}" for x in addresses],
    )
    for sequence in range(samples):
        started_ns = time.time_ns()
        response = session.transport.exchange(STREAM_POLL, timeout_seconds=3.0).payload
        readings = parse_stream_data(response, [*widths, *([1] * len(addresses))])
        did_readings = readings[: len(identifiers)]
        memory_readings = readings[len(identifiers) :]
        emitter.emit(
            "mixed_stream_sample",
            sequence=sequence,
            latency_ms=(time.time_ns() - started_ns) / 1_000_000,
            dids={
                f"0x{identifier:04x}": {
                    "raw": raw.hex(),
                    "raw_int": int.from_bytes(raw, "big"),
                    "width": len(raw),
                }
                for identifier, raw in zip(identifiers, did_readings, strict=True)
            },
            memory={
                f"0x{address:08x}": {"raw": raw.hex(), "raw_int": raw[0], "width": 1}
                for address, raw in zip(addresses, memory_readings, strict=True)
            },
        )


def _run_once(args: argparse.Namespace, emitter: _Emitter) -> None:
    with UpRevUsbTransport.connect(serial_number=args.serial) as transport:
        session = UpRevDiagnosticSession(transport)
        bootstrap = session.bootstrap()
        emitter.emit(
            "bootstrap",
            support_blocks={f"0x{k:04x}": v.hex() for k, v in bootstrap.support_blocks.items()},
            stop_partial_replies=bootstrap.stop_partial_replies,
            identity_failures=list(bootstrap.identity_failures),
        )
        identifiers = list(args.did)
        if args.scan_supported or args.stream_supported:
            identifiers.extend(supported_dids(bootstrap.support_blocks))
        discovered_widths = _scan(session, tuple(dict.fromkeys(identifiers)), emitter)
        if args.stream and args.memory_byte:
            _mixed_stream(session, args.stream, args.memory_byte, args.samples, emitter)
        elif args.stream:
            _stream(session, args.stream, args.samples, emitter)
        elif args.memory_byte:
            _memory_byte_stream(session, args.memory_byte, args.samples, emitter)
        if args.stream_supported:
            specs = list(discovered_widths.items())
            for offset in range(0, len(specs), args.chunk_size):
                _stream(
                    session,
                    specs[offset : offset + args.chunk_size],
                    args.samples,
                    emitter,
                )


def main() -> int:
    args = parse_args()
    emitter = _Emitter(args.out, quiet=args.quiet)
    try:
        for attempt in range(1, args.attempts + 1):
            try:
                _run_once(args, emitter)
                return 0
            except (UpRevUsbError, TimeoutError) as exc:
                emitter.emit(
                    "attempt_error",
                    attempt=attempt,
                    max_attempts=args.attempts,
                    error=str(exc),
                )
                if attempt == args.attempts:
                    raise
                time.sleep(1.0)
        raise AssertionError("attempt loop exited without a result")
    except (UpRevUsbError, TimeoutError) as exc:
        print(f"uprev_kline_probe: {exc}", file=sys.stderr)
        return 1
    finally:
        emitter.close()


if __name__ == "__main__":
    raise SystemExit(main())
