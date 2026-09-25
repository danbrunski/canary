#!/usr/bin/env python3
"""Canable-native OBD-II probe — no ELM327 required.

Connects to the configured CAN bus, walks the ECU's Mode 01 supported-PID
bitmask, optionally polls each supported PID once for a value snapshot, and
reads stored DTCs (Mode 03). ``--batch`` sends one multi-PID Mode 01 request
(up to six PIDs, the J1979 ceiling) and shows how the ECU answered it, so a
new ECU's batching can be characterised without a throwaway script. The same
Canable hardware that's running the
broadcast sniffer in production can also do this — see
``docs/data-architecture.md`` for why.

Usage:
  tools/can_obd_probe.py --config canary.yaml
  tools/can_obd_probe.py --config canary.yaml --read-pids
  tools/can_obd_probe.py --config canary.yaml --no-supported-walk --dtcs
  tools/can_obd_probe.py --config canary.yaml --no-supported-walk --batch 0C,0D,04,11,0E,10
  tools/can_obd_probe.py --config canary.yaml --batch-supported

The bus comes from ``canary.yaml``'s ``hardware.can`` block (slcan for
the Canable in WSL dev, socketcan for the Pi target). No write commands
are ever sent.
"""

from __future__ import annotations

import argparse
import sys
from collections.abc import Sequence

from src.collection.can_obd_client import (
    PID_DECODERS,
    BatchedMode01Response,
    CanObdClient,
    parse_dtc_payload,
    read_pid,
)
from src.collection.isotp import MAX_BATCHED_PIDS
from src.collection.obd_scheduler import mode01_registry_width
from src.log import configure_logging
from tools._bus_tool import (
    PASSIVE_REFUSED_EXIT,
    PassiveRefusedError,
    add_bus_tool_args,
    open_client,
    parse_header,
    require_passive,
)

#: Why this probe wants the daemon quiet: its functional (0x7DF) requests are
#: answered on the same ids as the scheduler's, so the two would read each
#: other's replies.
_PASSIVE_REASON = (
    "the daemon's scheduler polls Mode 01 on the same request/response ids, so "
    "this probe and the daemon would each read the other's replies"
)


def parse_args(argv: Sequence[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        prog="can_obd_probe",
        description=(
            "Probe what Mode-01 PIDs the ECU supports + read DTCs over the "
            "Canable. No ELM327 needed — uses the same CAN bus the broadcast "
            "sniffer uses."
        ),
    )
    add_bus_tool_args(parser, header_default="7DF", session_default=None, timeout_alias=True)
    parser.add_argument(
        "--read-pids",
        action="store_true",
        help=(
            "After walking the supported-PID bitmask, query each supported PID "
            "once and print the decoded value. Slower (one CAN round-trip per "
            "PID) but produces a snapshot. Skipped by default — most users want "
            "just the capability list."
        ),
    )
    parser.add_argument(
        "--dtcs",
        action="store_true",
        help="Also read stored DTCs (Mode 03).",
    )
    parser.add_argument(
        "--no-supported-walk",
        dest="supported_walk",
        action="store_false",
        help="Skip the PID bitmask walk (only useful with --dtcs).",
    )
    parser.add_argument(
        "--batch",
        action="append",
        type=parse_pid_list,
        default=[],
        metavar="PIDS",
        help=(
            "Send ONE batched Mode 01 request for these PIDs (comma-separated "
            f"hex, e.g. 0C,0D,04; at most {MAX_BATCHED_PIDS}) and print the "
            "split response: per-PID raw bytes and decoded value, PIDs the ECU "
            "omitted, and whether the walk stayed clean. Repeatable."
        ),
    )
    parser.add_argument(
        "--batch-supported",
        action="store_true",
        help=(
            "After the bitmask walk, batch-read every supported PID that has a "
            f"registry width, {MAX_BATCHED_PIDS} per request."
        ),
    )
    parser.add_argument(
        "--log-level",
        default="WARNING",
        choices=["DEBUG", "INFO", "WARNING", "ERROR"],
    )
    return parser.parse_args(argv)


def parse_pid_list(text: str) -> list[int]:
    """``"0C,0d,0x04"`` -> ``[0x0C, 0x0D, 0x04]`` (validated like a request)."""
    try:
        pids = [int(token.strip(), 16) for token in text.split(",") if token.strip()]
    except ValueError as exc:
        raise argparse.ArgumentTypeError(f"PIDs are comma-separated hex: {text!r}") from exc
    if not 1 <= len(pids) <= MAX_BATCHED_PIDS:
        raise argparse.ArgumentTypeError(
            f"a batch holds 1..{MAX_BATCHED_PIDS} PIDs, got {len(pids)}: {text!r}"
        )
    if len(set(pids)) != len(pids):
        raise argparse.ArgumentTypeError(f"duplicate PID in batch: {text!r}")
    if any(not 0 <= pid <= 0xFF for pid in pids):
        raise argparse.ArgumentTypeError(f"Mode 01 PIDs are one byte: {text!r}")
    return pids


def batch_groups(pids: Sequence[int]) -> list[list[int]]:
    """Supported PIDs the registry can split, in request-sized groups.

    A PID without a registry width cannot be walked past (its boundary is
    unknown), so it is left out rather than allowed to truncate a batch.
    """
    splittable = [pid for pid in pids if mode01_registry_width(pid) is not None]
    return [
        splittable[i : i + MAX_BATCHED_PIDS] for i in range(0, len(splittable), MAX_BATCHED_PIDS)
    ]


def format_batch(pids: Sequence[int], response: BatchedMode01Response | None) -> list[str]:
    """Human-readable lines for one batched request and its split response."""
    request = " ".join(f"{pid:02X}" for pid in pids)
    if response is None:
        return [
            f"BATCH [{request}]: no answer (timeout, negative response, or passive mode)",
        ]
    lines = [
        f"BATCH [{request}]: responder 0x{response.responder_id:X}, "
        f"walk {'clean' if response.clean else 'STOPPED EARLY'}"
    ]
    for pid, data in response.readings.items():
        entry = PID_DECODERS.get(pid)
        decoded = entry[1](data) if entry is not None else None
        name = entry[0] if entry is not None else "—"
        value = "" if decoded is None else f"{decoded[0]:>10.3f} {decoded[1]}"
        lines.append(f"  0x{pid:02X}  {name:<28}  {data.hex().upper():<8}  {value}".rstrip())
    missing = [pid for pid in pids if pid not in response.readings]
    if missing:
        omitted = " ".join(f"0x{pid:02X}" for pid in missing)
        why = "omitted by the ECU" if response.clean else "not reached before the walk stopped"
        lines.append(f"  absent: {omitted} ({why})")
    return lines


def read_batch(client: CanObdClient, pids: Sequence[int]) -> list[str]:
    """One batched Mode 01 round trip through the shared client and splitter."""
    return format_batch(
        pids, client.query_mode_01_batch(pids, byte_length_for=mode01_registry_width)
    )


def main(argv: Sequence[str] | None = None) -> int:
    args = parse_args(argv)
    configure_logging(args.log_level)
    try:
        parse_header(args.header)
    except ValueError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1
    try:
        passive = require_passive(args, reason=_PASSIVE_REASON)
    except PassiveRefusedError as exc:
        print(str(exc), file=sys.stderr)
        return PASSIVE_REFUSED_EXIT
    with open_client(args, passive) as client:
        if args.supported_walk:
            print("walking Mode-01 supported-PID bitmask…", file=sys.stderr)
            supported = client.query_supported_mode_01_pids()
            print(f"\nSUPPORTED MODE-01 PIDS ({len(supported)}):")
            for pid in supported:
                name = PID_DECODERS.get(pid, (None,))[0] or "—"
                print(f"  0x{pid:02X}  {name}")
        else:
            supported = ()

        if args.read_pids and supported:
            print("\nSAMPLE READINGS:")
            for pid in supported:
                decoded = read_pid(client, pid)
                if decoded is None:
                    print(f"  0x{pid:02X}  (no decoder or no response)")
                else:
                    print(
                        f"  0x{pid:02X}  {decoded.name:<28}  {decoded.value:>10.3f} {decoded.unit}"
                    )

        batches = list(args.batch)
        if args.batch_supported:
            batches.extend(batch_groups(supported))
        if batches:
            print("\nBATCHED MODE-01 READS:")
            for pids in batches:
                for line in read_batch(client, pids):
                    print(line)

        if args.dtcs:
            print("\nMODE 03 STORED DTCS:")
            resp = client.query_mode_03()
            if resp is None:
                print("  (no response from ECU)")
            else:
                dtcs = parse_dtc_payload(resp.payload)
                if dtcs:
                    for code in dtcs:
                        print(f"  {code}")
                else:
                    print("  (none)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
