#!/usr/bin/env python3
"""Re-verify catalogued RAM addresses against the running calibration.

A DID number is a contract the ECU keeps; a RAM address is an artifact of one
ROM build. After a reflash an address may still answer, still be in range, and
still decode to a plausible number that is now measuring something else --
silently. ``MemoryChannel.verified_tune_revision`` exists so the collector
withholds a channel in that situation, but re-stamping it by hand after every
flash is exactly the "fix every caller must remember" this project keeps
paying for.

So this tool does the re-stamping, and refuses to do it on anything weaker
than the channel's own declared known-answer check: it reads the memory word
and the cross-check DID in the same session and asserts the catalog's stated
relation between them. Plausibility bounds are reported but are never
sufficient -- a moved address lands inside them far too easily.

Read-only. It sends DiagnosticSessionControl (0x10), ReadMemoryByAddress
(0x23) and ReadDataByIdentifier (0x22), and nothing else. There is no write
path and no arbitrary-service escape hatch.

Run it as a module from the repo root -- ``python tools/verify_...py`` puts
``tools/`` on ``sys.path`` instead of the repo, and the sibling imports below
then fail::

    # daemon passive first; this tool owns the diagnostic session while it runs
    curl -X POST -H "Authorization: Bearer $CANARY_CONTROL_TOKEN" \
      -H 'Content-Type: application/json' -d '{"enabled":true,"ttl_seconds":120}' \
      http://localhost:8000/control/passive
    .venv/bin/python -m tools.verify_memory_channels --config /etc/canary/canary.yaml
"""

from __future__ import annotations

import argparse
import sys
from collections.abc import Iterator, Sequence
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Literal

import yaml

from src.atomic_write import atomic_write_text
from src.collection.can_collector import bus_factory
from src.collection.can_obd_client import CanObdClient
from src.config import CanaryConfig
from src.decode.dids import (
    MemoryChannel,
    apply_memory_scaling,
    check_known_answer,
    decode_memory_raw,
    is_memory_sentinel,
    load_did_map,
)
from tools.can_memory_log import MemoryClient, kwp32_memory_body
from tools.did_log import (
    PASSIVE_REASON,
    _passive_problem,
    _resolve_session,
    _resolve_token,
    http_passive_checker,
)


@dataclass(frozen=True)
class ChannelVerdict:
    """What one channel's re-verification found.

    Three outcomes, not two. "Failed its declared check" and "declares no
    check" are different facts: the first says the address moved, the second
    says nobody has written down how to tell. Collapsing them would either
    block re-stamping every other channel over a catalog gap, or quietly
    stamp an address nothing verified.
    """

    channel: str
    outcome: Literal["pass", "fail", "unverifiable"]
    reason: str
    samples: int = 0
    agreements: int = 0
    sentinels: int = 0

    @property
    def passed(self) -> bool:
        return self.outcome == "pass"

    def render(self) -> str:
        mark = {"pass": "PASS", "fail": "FAIL", "unverifiable": "SKIP"}[self.outcome]
        counts = f"{self.agreements}/{self.samples} agreed"
        if self.sentinels:
            counts += f", {self.sentinels} sentinel"
        return f"{mark}  {self.channel:30s} {counts:26s} {self.reason}"


def verify_channel(
    client: MemoryClient,
    channel: MemoryChannel,
    *,
    samples: int,
    request_id: int,
    session: int | None,
) -> ChannelVerdict:
    """Read `channel` and its cross-check DID together, `samples` times.

    Both reads go out back to back inside one held session, so the pair is as
    close to same-instant as this ECM allows (it refuses batched Mode 22, so
    there is no way to ask for both in one request).
    """
    known = channel.known_answer
    if known is None:
        return ChannelVerdict(
            channel.name,
            "unverifiable",
            "no known-answer check declared; an address cannot be re-verified "
            "on plausibility alone",
        )
    if session is not None and not client.enter_diagnostic_session(session, request_id=request_id):
        return ChannelVerdict(channel.name, "fail", f"session 0x{session:02X} refused")

    body = kwp32_memory_body(channel.address, channel.length)
    agreements = 0
    observed = 0
    sentinels = 0
    out_of_range: list[float] = []
    for _ in range(samples):
        reply = client.query_raw(0x23, body, request_id=request_id)
        payload = getattr(reply, "payload", None)
        if not payload:
            continue
        word = decode_memory_raw(channel, payload)
        if word is None:
            continue
        did_response = client.query_mode_22(known.did, request_id=request_id)
        if did_response is None or not did_response.payload:
            continue
        did_raw = int.from_bytes(did_response.payload, "big")
        observed += 1
        if check_known_answer(channel, word, did_raw):
            agreements += 1
        if is_memory_sentinel(channel, word):
            sentinels += 1
            continue
        value = apply_memory_scaling(channel, word)
        if value is None:
            continue
        low, high = channel.plausible_min, channel.plausible_max
        if (low is not None and value < low) or (high is not None and value > high):
            out_of_range.append(value)

    if observed == 0:
        return ChannelVerdict(channel.name, "fail", "no usable replies")
    if agreements != observed:
        return ChannelVerdict(
            channel.name,
            "fail",
            f"known-answer check failed on {observed - agreements} of {observed} "
            f"reads ({known.relation} vs DID 0x{known.did:04X}); the address has "
            "probably moved",
            observed,
            agreements,
            sentinels,
        )
    reason = "known-answer relation held on every read"
    if out_of_range:
        reason += f"; WARNING {len(out_of_range)} value(s) outside plausible bounds"
    return ChannelVerdict(channel.name, "pass", reason, observed, agreements, sentinels)


def restamp(dids_path: Path, passed: Sequence[str], revision: str) -> int:
    """Rewrite `verified_tune_revision` for the channels that passed.

    Edits only that one field, and only on named channels, so a hand-written
    catalog keeps everything else it says.
    """
    document = yaml.safe_load(dids_path.read_text())
    changed = 0
    for entry in document.get("memory") or []:
        if entry.get("name") in passed and entry.get("verified_tune_revision") != revision:
            entry["verified_tune_revision"] = revision
            changed += 1
    if changed:
        atomic_write_text(dids_path, yaml.safe_dump(document, sort_keys=False))
    return changed


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


def parse_args(argv: Sequence[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", type=Path, default=Path("canary.yaml"))
    parser.add_argument("--dids", type=Path, default=Path("data/dids.yaml"))
    parser.add_argument("--header", default="7E0", help="Request CAN id (hex).")
    parser.add_argument(
        "--session",
        default="auto",
        help="Gating diagnostic session sub-function; 'auto' reads it from the catalog.",
    )
    parser.add_argument("--samples", type=int, default=20)
    parser.add_argument("--read-timeout", type=float, default=1.0)
    parser.add_argument(
        "--channel",
        action="append",
        default=[],
        help="Verify only this channel (repeatable). Default: every catalogued channel.",
    )
    parser.add_argument(
        "--record-revision",
        default=None,
        help=(
            "On success, stamp this tune revision onto every channel that passed. "
            "Omit for a read-only check."
        ),
    )
    parser.add_argument("--daemon-url", default="http://localhost:8000")
    parser.add_argument("--passive-check-seconds", type=float, default=30.0)
    parser.add_argument(
        "--no-daemon",
        action="store_true",
        help="Skip the daemon passive-mode check (bench use, no daemon running).",
    )
    return parser.parse_args(argv)


def main(argv: Sequence[str] | None = None) -> int:
    args = parse_args(argv)
    try:
        request_id = int(args.header, 16)
        session = _resolve_session(args.session)
    except ValueError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1
    if args.samples <= 0:
        print("error: --samples must be positive", file=sys.stderr)
        return 1

    did_map = load_did_map(args.dids)
    channels = list(did_map.memory)
    if args.channel:
        wanted = {name.strip().upper() for name in args.channel}
        channels = [c for c in channels if c.name.upper() in wanted]
        missing = wanted - {c.name.upper() for c in channels}
        if missing:
            print(f"error: no such memory channel: {', '.join(sorted(missing))}", file=sys.stderr)
            return 1
    if not channels:
        print(f"error: {args.dids} declares no memory channels", file=sys.stderr)
        return 1

    if not args.no_daemon:
        checker = http_passive_checker(args.daemon_url, _resolve_token())
        problem = _passive_problem(checker(), args.passive_check_seconds)
        if problem is not None:
            print(
                f"refusing: daemon passive mode is not usable ({problem}); {PASSIVE_REASON}",
                file=sys.stderr,
            )
            return 2

    verdicts: list[ChannelVerdict] = []
    with _open_client(args.config, request_id=request_id, read_timeout=args.read_timeout) as client:
        for channel in channels:
            verdicts.append(
                verify_channel(
                    client,
                    channel,
                    samples=args.samples,
                    request_id=request_id,
                    session=session,
                )
            )

    for verdict in verdicts:
        print(verdict.render())

    passed = [v.channel for v in verdicts if v.outcome == "pass"]
    failed = [v.channel for v in verdicts if v.outcome == "fail"]
    skipped = [v.channel for v in verdicts if v.outcome == "unverifiable"]
    if args.record_revision:
        if failed:
            print(
                "not recording: "
                + ", ".join(failed)
                + " failed, and stamping a calibration onto a catalog that still "
                "contains a moved address is the failure this tool exists to prevent",
                file=sys.stderr,
            )
        else:
            changed = restamp(args.dids, passed, args.record_revision)
            print(f"stamped {changed} channel(s) as verified on {args.record_revision}")
            if skipped:
                print(
                    "left unstamped (no declared check): " + ", ".join(skipped),
                    file=sys.stderr,
                )

    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(main())
