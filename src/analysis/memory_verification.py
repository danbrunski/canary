"""Known-answer verification and provenance stamping for RAM channels.

The transport is deliberately expressed as a tiny protocol so the CLI and
the daemon use the same verification rules without either one importing the
other.  Reads are volatile ECU operations; the only local write is an atomic,
validated edit of an operational catalog copy.
"""

from __future__ import annotations

import json
import re
from collections.abc import Sequence
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any, Literal, Protocol

import yaml

from src.atomic_write import atomic_write_text
from src.decode.dids import (
    DidMap,
    MemoryChannel,
    apply_memory_scaling,
    check_known_answer,
    decode_memory_raw,
    is_memory_sentinel,
)


class MemoryVerificationClient(Protocol):
    def enter_diagnostic_session(self, session: int, *, request_id: int | None = None) -> bool: ...

    def query_raw(self, service: int, body: bytes, *, request_id: int | None = None) -> Any: ...

    def query_mode_22(self, did: int, *, request_id: int | None = None) -> Any: ...


@dataclass(frozen=True)
class ChannelVerdict:
    channel: str
    outcome: Literal["pass", "fail", "unverifiable"]
    reason: str
    samples: int = 0
    agreements: int = 0
    sentinels: int = 0

    @property
    def passed(self) -> bool:
        return self.outcome == "pass"

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    def render(self) -> str:
        mark = {"pass": "PASS", "fail": "FAIL", "unverifiable": "SKIP"}[self.outcome]
        counts = f"{self.agreements}/{self.samples} agreed"
        if self.sentinels:
            counts += f", {self.sentinels} sentinel"
        return f"{mark}  {self.channel:30s} {counts:26s} {self.reason}"


def _memory_body(address: int, length: int) -> bytes:
    """KWP32 address-and-length body accepted by this ECM."""
    return address.to_bytes(4, "big") + length.to_bytes(2, "big")


def verify_channel(
    client: MemoryVerificationClient,
    channel: MemoryChannel,
    *,
    samples: int,
    request_id: int,
    session: int | None,
) -> ChannelVerdict:
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

    agreements = observed = sentinels = 0
    out_of_range: list[float] = []
    for _ in range(samples):
        reply = client.query_raw(
            0x23, _memory_body(channel.address, channel.length), request_id=request_id
        )
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
        agreements += int(check_known_answer(channel, word, did_raw))
        if is_memory_sentinel(channel, word):
            sentinels += 1
            continue
        value = apply_memory_scaling(channel, word)
        if value is not None and (
            (channel.plausible_min is not None and value < channel.plausible_min)
            or (channel.plausible_max is not None and value > channel.plausible_max)
        ):
            out_of_range.append(value)

    if observed == 0:
        return ChannelVerdict(channel.name, "fail", "no usable memory/DID reply pairs")
    if agreements < observed * known.min_agreement:
        return ChannelVerdict(
            channel.name,
            "fail",
            f"known-answer relation {known.relation} held for {agreements}/{observed} reads; "
            "the address has probably moved",
            observed,
            agreements,
            sentinels,
        )
    reason = f"known-answer relation {known.relation} held for {agreements}/{observed} reads"
    if out_of_range:
        reason += f"; {len(out_of_range)} scaled value(s) were outside plausible bounds"
    return ChannelVerdict(channel.name, "pass", reason, observed, agreements, sentinels)


class RestampRefusedError(ValueError):
    pass


def plan_restamp(
    channels: Sequence[MemoryChannel],
    verdicts: Sequence[ChannelVerdict],
    *,
    carry_reason: str | None,
) -> tuple[dict[str, str | None], list[str]]:
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
            notes.append(f"{label}: not every member was checked; window left unchanged")
            continue
        if "fail" in outcomes.values():
            notes.append(f"{label}: a member failed; address window left unchanged")
            continue
        unverifiable = [name for name, outcome in outcomes.items() if outcome == "unverifiable"]
        if unverifiable and not carry_reason:
            notes.append(
                f"{label}: {', '.join(unverifiable)} declare no check, and a window "
                "cannot mix calibrations; left unstamped without an explicit carry reason"
            )
            continue
        for member in members:
            stamps[member.name] = carry_reason if member.name in unverifiable else None
    return stamps, notes


def _channel_block(text: str, name: str) -> tuple[int, int]:
    match = re.search(rf"^  - name: {re.escape(name)}[ \t]*$", text, flags=re.MULTILINE)
    if match is None:
        raise RestampRefusedError(f"no memory channel named {name} in the catalog text")
    following = re.search(r"^  - name: ", text[match.end() :], flags=re.MULTILINE)
    return match.start(), match.end() + following.start() if following else len(text)


def restamp(dids_path: Path, stamps: dict[str, str | None], revision: str) -> int:
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
    except Exception as exc:
        raise RestampRefusedError(f"the stamped catalog would not load: {exc}") from exc
    atomic_write_text(dids_path, text, mkdir=True)
    return changed


__all__ = [
    "ChannelVerdict",
    "MemoryVerificationClient",
    "RestampRefusedError",
    "plan_restamp",
    "restamp",
    "verify_channel",
]
