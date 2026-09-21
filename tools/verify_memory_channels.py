#!/usr/bin/env python3
"""Re-verify catalogued RAM addresses against the running calibration.

A RAM address is a location in the ECU program's memory map. Calibration
flashes on the same program leave it in place (operator, 2026-09-21), but a
different program -- or a wrong address found in the first place -- can still
answer, stay in range, and decode to a plausible number that measures
something else. This tool is the evidence check for that case.
``MemoryChannel.verified_tune_revision`` records which calibration it last
passed on; that is provenance, and the collector does not gate on it.

It re-stamps the provenance, and refuses to do it on anything weaker
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
import json
import re
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
    DidMap,
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
    if agreements < observed * known.min_agreement:
        return ChannelVerdict(
            channel.name,
            "fail",
            f"known-answer check failed: {agreements}/{observed} reads satisfied "
            f"{known.relation} vs DID 0x{known.did:04X}, below the "
            f"{known.min_agreement:.0%} this channel requires; the address has "
            "probably moved",
            observed,
            agreements,
            sentinels,
        )
    reason = (
        "known-answer relation held on every read"
        if agreements == observed
        else f"known-answer relation held on {agreements}/{observed} reads "
        f"(requires {known.min_agreement:.0%}; the rest is acquisition skew)"
    )
    if out_of_range:
        reason += f"; WARNING {len(out_of_range)} value(s) outside plausible bounds"
    return ChannelVerdict(channel.name, "pass", reason, observed, agreements, sentinels)


class RestampRefusedError(ValueError):
    """The requested stamp would leave the catalog invalid or dishonest."""


def _channel_block(text: str, name: str) -> tuple[int, int]:
    """Span of one ``memory:`` entry in the catalog text, by channel name."""
    match = re.search(rf"^  - name: {re.escape(name)}[ \t]*$", text, flags=re.MULTILINE)
    if match is None:
        raise RestampRefusedError(f"no memory channel named {name} in the catalog text")
    following = re.search(r"^  - name: ", text[match.end() :], flags=re.MULTILINE)
    end = match.end() + following.start() if following else len(text)
    return match.start(), end


def plan_restamp(
    channels: Sequence[MemoryChannel],
    verdicts: Sequence[ChannelVerdict],
    *,
    carry_reason: str | None,
) -> tuple[dict[str, str | None], list[str]]:
    """Decide what may be stamped: ``{channel: carried_reason_or_None}``, notes.

    Stamping is per WINDOW, because a window is one read and the loader
    refuses one that mixes calibrations. Stamping only the members that
    passed would therefore write a catalog the daemon cannot load — which is
    what the first version of this tool did.

    A window is stamped when every member that declares a check passed. Its
    members with no check are stamped only under an explicit
    ``carry_reason``, and that reason is recorded on them: "verified" and
    "carried across a flash on an argument" are different facts.
    """
    by_name = {v.channel: v for v in verdicts}
    groups: dict[str, list[MemoryChannel]] = {}
    for channel in channels:
        groups.setdefault(channel.window or f"\x00{channel.name}", []).append(channel)

    stamps: dict[str, str | None] = {}
    notes: list[str] = []
    for key, members in groups.items():
        label = key if not key.startswith("\x00") else members[0].name
        outcomes = {m.name: by_name[m.name].outcome for m in members if m.name in by_name}
        if len(outcomes) != len(members):
            notes.append(f"{label}: not every member was checked this run; left unstamped")
            continue
        if any(o == "fail" for o in outcomes.values()):
            notes.append(f"{label}: a member failed its check; left unstamped")
            continue
        unverifiable = [n for n, o in outcomes.items() if o == "unverifiable"]
        if unverifiable and carry_reason is None:
            notes.append(
                f"{label}: {', '.join(unverifiable)} declare no check, and a window "
                "cannot mix calibrations; left unstamped (pass --carry-unverifiable "
                "with a reason to stamp them on an argument instead of a measurement)"
            )
            continue
        for member in members:
            stamps[member.name] = carry_reason if member.name in unverifiable else None
    return stamps, notes


def restamp(dids_path: Path, stamps: dict[str, str | None], revision: str) -> int:
    """Stamp ``revision`` onto the named channels by editing the catalog TEXT.

    Text, not ``yaml.safe_dump``: this file carries the project's reasoning
    in comments, and a dump round-trip deletes all of it (the capture-profile
    catalogs lost 64 comment blocks that way). The edited text is parsed and
    validated before anything is written, so a stamp can never produce a
    catalog the daemon refuses to load.
    """
    text = dids_path.read_text()
    changed = 0
    for name, carried in stamps.items():
        begin, end = _channel_block(text, name)
        block = text[begin:end]
        updated = re.sub(
            r"^(    verified_tune_revision:)[ \t]*.*$",
            rf"\g<1> {revision}",
            block,
            count=1,
            flags=re.MULTILINE,
        )
        updated = re.sub(
            r"^    revision_carried_reason:.*\n", "", updated, count=1, flags=re.MULTILINE
        )
        if carried is not None:
            line = f"    revision_carried_reason: {json.dumps(carried)}\n"
            head, sep, tail = updated.partition("    verified_tune_revision:")
            stamp_line, newline, rest = tail.partition("\n")
            updated = head + sep + stamp_line + newline + line + rest
        if updated != block:
            text = text[:begin] + updated + text[end:]
            changed += 1
    if not changed:
        return 0
    try:
        DidMap.model_validate(yaml.safe_load(text))
    except Exception as exc:  # pydantic/YAML: either way the stamp must not land
        raise RestampRefusedError(f"the stamped catalog would not load: {exc}") from exc
    atomic_write_text(dids_path, text)
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
    parser.add_argument(
        "--carry-unverifiable",
        default=None,
        metavar="REASON",
        help=(
            "Also stamp window members that declare no known-answer check, recording "
            "REASON on them. For a flash known to change only table bytes."
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

    failed = [v.channel for v in verdicts if v.outcome == "fail"]
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
            stamps, notes = plan_restamp(channels, verdicts, carry_reason=args.carry_unverifiable)
            try:
                changed = restamp(args.dids, stamps, args.record_revision)
            except RestampRefusedError as exc:
                print(f"not recording: {exc}", file=sys.stderr)
                return 1
            print(f"stamped {changed} channel(s) as verified on {args.record_revision}")
            carried = sorted(name for name, reason in stamps.items() if reason is not None)
            if carried:
                print("  carried without a check: " + ", ".join(carried))
            for note in notes:
                print(f"  {note}", file=sys.stderr)

    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(main())
