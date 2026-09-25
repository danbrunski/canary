#!/usr/bin/env python3
"""Continuously log one read-only ECM RAM window over diagnostic CAN.

This is the time-resolved counterpart to ``tools.batch_probe``'s one-shot
service-0x23 probe. It emits only DiagnosticSessionControl (0x10) and the
KWP32 ReadMemoryByAddress form already proven on the vehicle::

    23 <address:4 big-endian bytes> <length:2 big-endian bytes>

The daemon must be passive because the proprietary diagnostic session blocks
its Mode 01 polling. Every response gets send/receive wall-clock timestamps so
changing bytes can later be paired with session telemetry. The tool has no
arbitrary-service or write path.
"""

from __future__ import annotations

import argparse
import json
import sys
import time
from collections.abc import Callable, Sequence
from pathlib import Path
from typing import Protocol, TextIO

from src.collection.can_obd_client import RawReply
from src.collection.isotp import ObdResponse
from tools._bus_tool import (
    PASSIVE_REFUSED_EXIT,
    RECORD_KEY,
    PassiveChecker,
    PassiveRefusedError,
    add_bus_tool_args,
    header_record,
    open_client,
    parse_header,
    passive_problem,
    require_passive,
    resolve_session,
)

SESSION_REFRESH_SECONDS = 1.5


class MemoryClient(Protocol):
    def query_raw(
        self, service: int, body: bytes, *, request_id: int | None = None
    ) -> RawReply: ...

    def enter_diagnostic_session(
        self, subfunction: int, *, request_id: int | None = None
    ) -> bool: ...

    def query_mode_22(self, did: int, *, request_id: int | None = None) -> ObdResponse | None: ...


def kwp32_memory_body(address: int, length: int) -> bytes:
    """Build the capture-proven 4-byte-address/2-byte-length request body."""
    if not 0 <= address <= 0xFFFFFFFF:
        raise ValueError("address must fit in 32 bits")
    if not 1 <= length <= 0xFFFF:
        raise ValueError("length must be 1-65535 bytes")
    return address.to_bytes(4, "big") + length.to_bytes(2, "big")


def log_memory(
    client: MemoryClient,
    writer: TextIO,
    *,
    address: int,
    length: int,
    samples: int,
    interval_seconds: float,
    request_id: int,
    session: int | None,
    dids: tuple[int, ...] = (),
    passive_checker: PassiveChecker | None = None,
    passive_check_seconds: float = 5.0,
    monotonic: Callable[[], float] = time.monotonic,
    wall_time: Callable[[], float] = time.time,
    sleep: Callable[[float], None] = time.sleep,
) -> dict[str, int | float | str]:
    """Write a bounded JSONL capture and return its summary."""
    if samples <= 0:
        raise ValueError("samples must be positive")
    if interval_seconds < 0:
        raise ValueError("interval_seconds must be non-negative")
    body = kwp32_memory_body(address, length)
    writer.write(
        json.dumps(
            header_record(
                "can_memory_log",
                started_at=wall_time(),
                request_id=f"0x{request_id:X}",
                session=None if session is None else f"0x{session:X}",
                service="0x23",
                form="kwp32",
                address=f"0x{address:08X}",
                length=length,
                samples_requested=samples,
                interval_seconds=interval_seconds,
                dids=[f"0x{did:04X}" for did in dids],
            )
        )
        + "\n"
    )
    writer.flush()

    started = monotonic()
    last_session_at = float("-inf")
    next_passive_check = started
    reads_ok = 0
    reads_missing = 0
    did_reads_ok = 0
    did_reads_missing = 0
    reason = "samples"

    for index in range(samples):
        now = monotonic()
        if passive_checker is not None and now >= next_passive_check:
            problem = passive_problem(passive_checker(), passive_check_seconds)
            if problem is not None:
                reason = f"passive_{problem}"
                break
            next_passive_check = now + passive_check_seconds

        if session is not None and now - last_session_at >= SESSION_REFRESH_SECONDS:
            if not client.enter_diagnostic_session(session, request_id=request_id):
                reason = "session_entry_failed"
                break
            last_session_at = monotonic()

        t_send = wall_time()
        reply = client.query_raw(0x23, body, request_id=request_id)
        t_recv = wall_time()
        payload = reply.payload
        if reply.positive:
            reads_ok += 1
        else:
            reads_missing += 1
        writer.write(
            json.dumps(
                {
                    RECORD_KEY: "read",
                    "index": index,
                    "t_send": t_send,
                    "t_recv": t_recv,
                    "payload_hex": None if payload is None else payload.hex(),
                    "sent": reply.sent,
                    "nrc": (
                        None
                        if reply.negative_response_code is None
                        else f"0x{reply.negative_response_code:02X}"
                    ),
                }
            )
            + "\n"
        )
        writer.flush()
        for did in dids:
            did_t_send = wall_time()
            response = client.query_mode_22(did, request_id=request_id)
            did_t_recv = wall_time()
            if response is None:
                did_reads_missing += 1
            else:
                did_reads_ok += 1
            writer.write(
                json.dumps(
                    {
                        RECORD_KEY: "did",
                        "memory_index": index,
                        "did": f"0x{did:04X}",
                        "t_send": did_t_send,
                        "t_recv": did_t_recv,
                        "payload_hex": None if response is None else response.payload.hex(),
                    }
                )
                + "\n"
            )
            writer.flush()
        if index + 1 < samples and interval_seconds:
            sleep(interval_seconds)

    summary: dict[str, int | float | str] = {
        RECORD_KEY: "summary",
        "reason": reason,
        "samples_requested": samples,
        "reads_ok": reads_ok,
        "reads_missing": reads_missing,
        "did_reads_ok": did_reads_ok,
        "did_reads_missing": did_reads_missing,
        "elapsed_seconds": round(monotonic() - started, 6),
    }
    writer.write(json.dumps(summary) + "\n")
    writer.flush()
    return summary


def parse_args(argv: Sequence[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    add_bus_tool_args(parser)
    parser.add_argument("--out", type=Path, required=True)
    parser.add_argument("--address", type=lambda value: int(value, 0), required=True)
    parser.add_argument("--length", type=int, required=True)
    parser.add_argument("--samples", type=int, default=100)
    parser.add_argument("--interval-seconds", type=float, default=0.05)
    parser.add_argument(
        "--did",
        action="append",
        default=[],
        help="Mode 22 DID to read after every memory sample (hex; repeatable)",
    )
    return parser.parse_args(argv)


def main(argv: Sequence[str] | None = None) -> int:
    args = parse_args(argv)
    try:
        request_id = parse_header(args.header)
        session = resolve_session(args.session)
        kwp32_memory_body(args.address, args.length)
        dids = tuple(int(token, 16) for token in args.did)
        if any(not 0 <= did <= 0xFFFF for did in dids):
            raise ValueError("DIDs must fit in 16 bits")
    except ValueError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1
    if args.samples <= 0 or args.interval_seconds < 0 or args.passive_check_seconds <= 0:
        print(
            "error: samples/check interval must be positive; sample interval cannot be negative",
            file=sys.stderr,
        )
        return 1

    try:
        passive = require_passive(args)
    except PassiveRefusedError as exc:
        print(str(exc), file=sys.stderr)
        return PASSIVE_REFUSED_EXIT

    args.out.parent.mkdir(parents=True, exist_ok=True)
    with (
        open_client(args, passive) as client,
        args.out.open("w", encoding="utf-8", buffering=1) as writer,
    ):
        summary = log_memory(
            client,
            writer,
            address=args.address,
            length=args.length,
            samples=args.samples,
            interval_seconds=args.interval_seconds,
            request_id=request_id,
            session=session,
            dids=dids,
            passive_checker=passive.checker,
            passive_check_seconds=args.passive_check_seconds,
        )
    print(json.dumps(summary, indent=2))
    print(f"capture: {args.out}")
    return 0 if summary["reason"] == "samples" else 2


if __name__ == "__main__":
    raise SystemExit(main())
