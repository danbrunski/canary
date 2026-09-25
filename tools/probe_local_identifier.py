#!/usr/bin/env python3
"""Probe dynamically-defined local identifiers (0xAC define, 0x21 read) on CAN.

This ECM refuses batched Mode 22, so every DID is its own request and no two
values ever share an instant. UpRev does not live with that: its K-line
scheme defines a local identifier listing many DIDs and RAM bytes, then polls
it once per sample and gets all of them back in a single reply at ~14 Hz
(``data/analyses/2026-09-20-uprev-usb-road.md``). This asks whether the same
mechanism answers on the diagnostic CAN endpoint.

The 7-byte single-frame limit would normally cap a definition at one
descriptor. It does not, because a definition may *include* another local
identifier: build up one descriptor at a time, each definition naming the
previous one, and the poll returns the whole accumulated list. That is what
UpRev's 0x83 -> 0x82 -> 0x81 chain does, and it means no multi-frame transmit
is needed.

RESULT, 2026-09-20 KOEO, engine ECM at 0x7E0 in session 0xC0: **the define
does not exist on the CAN endpoint.** Every one-request-many-values route
was refused, so this is a settled negative, not an untried idea:

* ``0xAC`` DynamicallyDefineLocalIdentifier -> NRC 0x11 (serviceNotSupported)
* ``0x2A`` ReadDataByPeriodicIdentifier -> NRC 0x11, every mode tried
* ``0x2C`` DynamicallyDefineDataIdentifier -> NRC 0x12 for sub-functions
  0x01/0x02/0x03, i.e. the service is recognised and no sub-function works

``0x21`` itself *does* answer, but only for identifiers the ECM already
defines, and a sweep of all 256 found 14 -- all static identification
blocks, not live telemetry. The useful one is ``0x21 10``, which returns the
calibration id as ASCII (``1ZS44A00000`` on ZS44A-k106).

So UpRev's 14 Hz whole-scheme sampling is a K-line capability, not a CAN one.
The multi-value route that DOES work on CAN is service 0x23: one read returns
up to 63 bytes (``access.max_memory_read_length``), which is what
``MemoryChannel.window`` uses.

Read-only throughout. Sends 0x10 (session), 0xAC/0x2C (define a read list),
0x21 (read it), 0x22 and 0x23 (the baselines it checks against). Nothing
here writes non-volatile memory.
"""

from __future__ import annotations

import argparse
import sys
from collections.abc import Iterator, Sequence
from contextlib import contextmanager
from pathlib import Path

from src.collection.can_bus import bus_factory
from src.collection.can_obd_client import CanObdClient
from src.config import CanaryConfig

DEFINE_SERVICE = 0xAC
READ_LOCAL_SERVICE = 0x21


def did_descriptor(did: int) -> bytes:
    """``02 <hi> <lo>`` — the captured descriptor for a Mode-22 identifier."""
    return bytes([0x02]) + did.to_bytes(2, "big")


def memory_descriptor(address: int) -> bytes:
    """``83 <addr:4>`` — the captured descriptor for one absolute RAM byte."""
    return bytes([0x83]) + address.to_bytes(4, "big")


def _render(reply: object) -> str:
    payload = getattr(reply, "payload", None)
    nrc = getattr(reply, "negative_response_code", None)
    if nrc is not None:
        return f"NRC 0x{nrc:02X}"
    if payload is None:
        return "no reply"
    return payload.hex() or "<empty>"


@contextmanager
def _open_client(config_path: Path, *, request_id: int, timeout: float) -> Iterator[CanObdClient]:
    config = CanaryConfig.from_yaml(config_path)
    bus = bus_factory(config.hardware.can)
    try:
        yield CanObdClient(
            bus,
            request_id=request_id,
            response_id_min=request_id + 8,
            response_id_max=request_id + 8,
            timeout_seconds=timeout,
        )
    finally:
        bus.shutdown()


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", type=Path, default=Path("canary.yaml"))
    parser.add_argument("--header", default="7E0")
    parser.add_argument("--session", default="C0")
    parser.add_argument("--local-id", default="81")
    parser.add_argument("--timeout", type=float, default=1.0)
    args = parser.parse_args(argv)

    request_id = int(args.header, 16)
    session = int(args.session, 16)
    local = int(args.local_id, 16)

    with _open_client(args.config, request_id=request_id, timeout=args.timeout) as client:
        if not client.enter_diagnostic_session(session, request_id=request_id):
            print(f"session 0x{session:02X} refused", file=sys.stderr)
            return 2
        print(f"session 0x{session:02X} entered")

        baseline = client.query_mode_22(0x1101, request_id=request_id)
        print(f"\nbaseline  Mode 22 DID 0x1101 -> {_render(baseline)}")

        print("\n--- 1. define one DID, then read the local id ---")
        body = bytes([local]) + did_descriptor(0x1101)
        print(f"  define  AC {body.hex()}")
        print(f"    -> {_render(client.query_raw(DEFINE_SERVICE, body, request_id=request_id))}")
        for poll_body, label in (
            (bytes([local]), "21 xx"),
            (bytes([local, 0x04, 0x01]), "21 xx 04 01"),
        ):
            reply = client.query_raw(READ_LOCAL_SERVICE, poll_body, request_id=request_id)
            print(f"  poll {label:12s} -> {_render(reply)}")

        print("\n--- 2. define one RAM byte, then read it ---")
        body = bytes([local]) + memory_descriptor(0xFFFF26BE)
        print(f"  define  AC {body.hex()}")
        print(f"    -> {_render(client.query_raw(DEFINE_SERVICE, body, request_id=request_id))}")
        reply = client.query_raw(READ_LOCAL_SERVICE, bytes([local]), request_id=request_id)
        print(f"  poll          -> {_render(reply)}")

        print("\n--- 3. CHAIN: define 0x82 as (include 0x81) + one more DID ---")
        body = bytes([local + 1, 0x01, local]) + did_descriptor(0x1117)
        print(f"  define  AC {body.hex()}")
        print(f"    -> {_render(client.query_raw(DEFINE_SERVICE, body, request_id=request_id))}")
        reply = client.query_raw(READ_LOCAL_SERVICE, bytes([local + 1]), request_id=request_id)
        print(f"  poll 0x{local + 1:02X}      -> {_render(reply)}")
        base = client.query_mode_22(0x1117, request_id=request_id)
        print(f"  baseline DID 0x1117 -> {_render(base)}")

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
