"""Known-answer verification and provenance stamping for RAM channels.

The transport is deliberately expressed as a tiny protocol so the CLI and
the daemon use the same verification rules without either one importing the
other.  Reads are volatile ECU operations; the only local write is an atomic,
validated edit of an operational catalog copy.
"""

from __future__ import annotations

import json
import re
import time
from collections.abc import Callable, Mapping, Sequence
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any, Literal, Protocol

import yaml

from src.atomic_write import atomic_write_text
from src.collection.obd_scheduler.spec import DidSessionSpec
from src.decode.dids import (
    DidMap,
    MemoryChannel,
    apply_memory_scaling,
    check_known_answer,
    decode_memory_raw,
    is_memory_sentinel,
    memory_checks,
    relation_channel_names,
    relation_reads,
)
from src.decode.memory_relations import (
    VALUE_RELATION_KINDS,
    FlagEquivalenceRelation,
    InvariantRelation,
    MemoryArithmeticRelation,
    MemoryRelation,
    Mode01PidRelation,
)


class MemoryVerificationClient(Protocol):
    def enter_diagnostic_session(self, session: int, *, request_id: int | None = None) -> bool: ...

    def query_raw(self, service: int, body: bytes, *, request_id: int | None = None) -> Any: ...

    def query_mode_22(self, did: int, *, request_id: int | None = None) -> Any: ...

    def query_mode_01(self, pid: int) -> Any: ...


Outcome = Literal["pass", "fail", "unverifiable"]
_MARK = {"pass": "PASS", "fail": "FAIL", "unverifiable": "SKIP"}


class VerifyAbortedError(RuntimeError):
    """Raised by a ``before_sample`` hook to stop a verify mid-channel."""


@dataclass(frozen=True)
class NeighbourResult:
    """The relation re-applied with the verified channel shifted ``shift`` bytes."""

    shift: int
    address: int
    samples: int
    agreements: int


@dataclass(frozen=True)
class RelationVerdict:
    """One declared relation's verdict on one channel it reads."""

    channel: str
    owner: str
    relation: str
    outcome: Outcome
    reason: str
    samples: int = 0
    agreements: int = 0
    neighbours: tuple[NeighbourResult, ...] = ()


@dataclass(frozen=True)
class ChannelVerdict:
    channel: str
    outcome: Outcome
    reason: str
    samples: int = 0
    agreements: int = 0
    sentinels: int = 0
    #: Each memory relation that reads this channel, with its own verdict
    #: (empty when only a DID known answer applies).
    checks: tuple[RelationVerdict, ...] = ()

    @property
    def passed(self) -> bool:
        return self.outcome == "pass"

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    def render(self) -> str:
        counts = f"{self.agreements}/{self.samples} agreed"
        if self.sentinels:
            counts += f", {self.sentinels} sentinel"
        lines = [f"{_MARK[self.outcome]}  {self.channel:30s} {counts:26s} {self.reason}"]
        if len(self.checks) > 1 or (self.checks and self.checks[0].reason != self.reason):
            lines.extend(f"        {_MARK[c.outcome]}  {c.reason}" for c in self.checks)
        return "\n".join(lines)


@dataclass(frozen=True)
class RelationSample:
    """One sample of a relation check: the memory replies and the PID reply.

    ``replies`` maps each read's start address to its payload, so the same
    evaluation runs on a live read and on a stored session's window payloads.
    """

    replies: Mapping[int, bytes]
    pid_payload: bytes | None = None


def raw_at(channel: MemoryChannel, replies: Mapping[int, bytes], shift: int = 0) -> int | None:
    """`channel`'s raw value from whichever reply covers it, moved `shift` bytes."""
    start = channel.address + shift
    for address, payload in replies.items():
        offset = start - address
        if offset >= 0 and offset + channel.byte_start + channel.byte_length <= len(payload):
            return channel.raw_from(payload[offset:])
    return None


def relation_holds(
    owner: MemoryChannel,
    relation: MemoryRelation,
    raws: Mapping[str, int],
    pid_payload: bytes | None = None,
) -> bool | None:
    """Whether `relation` holds on one sample; ``None`` when it cannot be judged."""
    own = raws[owner.name]
    if isinstance(relation, InvariantRelation):
        return relation.holds(own)
    if isinstance(relation, MemoryArithmeticRelation):
        return relation.holds(own, [raws[name] for name in relation.operands])
    if isinstance(relation, FlagEquivalenceRelation):
        return relation.holds_on(raws)
    if pid_payload is None:
        return None
    pid_raw = relation.pid_raw(pid_payload)
    return None if pid_raw is None else relation.holds(own, pid_raw)


def evaluate_relation(
    subject: MemoryChannel,
    owner: MemoryChannel,
    relation: MemoryRelation,
    by_name: Mapping[str, MemoryChannel],
    samples: Sequence[RelationSample],
) -> RelationVerdict:
    """Judge `relation` as evidence for `subject`'s address.

    Passing takes three things. The relation holds on at least
    ``min_agreement`` of the samples. Moved to each neighbouring slice (the
    subject's width either side, read in the same reply), it falls below that
    -- a relation that holds on the bytes beside the address did not tell
    them apart. And the agreement was *exercised*, by kind:

    * ``memory`` / ``mode01_pid``: the compared value (the result for
      sum/divide, the bound for ordering, the word for a PID) was non-zero on
      an agreeing sample -- zero matching zero is a constant matching a
      constant;
    * ``invariant``: the subject took at least two distinct raw values over
      the agreeing samples (the stricter of the two candidate rules: a
      non-zero constant inside a range is still a constant);
    * ``flag_equivalence``: ``when`` was seen true, and for ``iff`` false as
      well -- a flag that never set (false <=> false) proves nothing.

    Missing any of the last two is ``unverifiable``, never ``pass``. So is a
    PID that never answered (``pid_unanswered``): that is the bus, not the
    address.
    """
    names = relation_channel_names(owner, relation)
    width = subject.byte_length
    shifts = (-width, width)
    neighbour_counts = {shift: [0, 0] for shift in shifts}
    observed = agreements = pid_missing = 0
    non_zero = False
    subject_values: set[int] = set()
    antecedents: set[bool] = set()
    for sample in samples:
        raws: dict[str, int] = {}
        for name in names:
            raw = raw_at(by_name[name], sample.replies)
            if raw is None:
                break
            raws[name] = raw
        else:
            held = relation_holds(owner, relation, raws, sample.pid_payload)
            if held is None:
                pid_missing += int(isinstance(relation, Mode01PidRelation))
                continue
            observed += 1
            agreements += int(held)
            if held:
                subject_values.add(raws[subject.name])
                non_zero = non_zero or _witness(owner, relation, raws) != 0
                if isinstance(relation, FlagEquivalenceRelation):
                    antecedents.add(relation.antecedent(raws))
            for shift in shifts:
                moved = raw_at(subject, sample.replies, shift)
                if moved is None:
                    continue
                at_neighbour = relation_holds(
                    owner, relation, {**raws, subject.name: moved}, sample.pid_payload
                )
                if at_neighbour is None:
                    continue
                neighbour_counts[shift][0] += 1
                neighbour_counts[shift][1] += int(at_neighbour)
    neighbours = tuple(
        NeighbourResult(shift, subject.address + shift, counts[0], counts[1])
        for shift, counts in neighbour_counts.items()
    )
    where = f"{relation.label} on {owner.name}"

    def verdict(outcome: Outcome, reason: str) -> RelationVerdict:
        return RelationVerdict(
            subject.name,
            owner.name,
            relation.label,
            outcome,
            reason,
            observed,
            agreements,
            neighbours,
        )

    if observed == 0 and pid_missing:
        return verdict(
            "unverifiable",
            f"pid_unanswered: {where}: PID 0x{getattr(relation, 'pid', 0):02X} did not answer "
            f"on any of {pid_missing} samples; that is the bus, not the address",
        )
    if observed == 0:
        return verdict(
            "unverifiable",
            f"reads_unanswered: {where}: no sample had every memory read answered; "
            "that is the bus, not the address",
        )
    if agreements < observed * relation.min_agreement:
        return verdict(
            "fail",
            f"{where} held for {agreements}/{observed} reads, under its "
            f"{relation.min_agreement:.2f}; the address has moved or the relation does not hold",
        )
    unread = [n for n in neighbours if n.samples == 0]
    if unread:
        return verdict(
            "unverifiable",
            f"{where} held for {agreements}/{observed}, but the neighbouring bytes at "
            + ", ".join(f"0x{n.address:08X}" for n in unread)
            + " were not read, so it was never shown able to fail",
        )
    held_beside = [n for n in neighbours if n.agreements >= n.samples * relation.min_agreement]
    if held_beside:
        return verdict(
            "unverifiable",
            f"{where} held for {agreements}/{observed}, and also on the neighbouring bytes at "
            + ", ".join(f"0x{n.address:08X} ({n.agreements}/{n.samples})" for n in held_beside)
            + ": it did not tell this address from the bytes beside it",
        )
    if relation.kind in VALUE_RELATION_KINDS and not non_zero:
        return verdict(
            "unverifiable",
            f"{where} held for {agreements}/{observed}, but only on zero values: zero "
            "matching zero is not evidence",
        )
    if isinstance(relation, InvariantRelation) and len(subject_values) < 2:
        return verdict(
            "unverifiable",
            f"{where} held for {agreements}/{observed}, but {subject.name} read one constant "
            f"({', '.join(str(v) for v in sorted(subject_values))}): a constant matching a "
            "constant is not evidence",
        )
    if isinstance(relation, FlagEquivalenceRelation):
        needed = {True, False} if relation.logic == "iff" else {True}
        if not needed <= antecedents:
            seen = "never true" if True not in antecedents else "never false"
            return verdict(
                "unverifiable",
                f"{where} held for {agreements}/{observed}, but its condition was {seen}; "
                f"{relation.logic} needs to see "
                + ("both states" if relation.logic == "iff" else "it true")
                + " before it says which byte this is",
            )
    return verdict(
        "pass",
        f"{where} held for {agreements}/{observed} reads and failed beside it ("
        + ", ".join(f"0x{n.address:08X} {n.agreements}/{n.samples}" for n in neighbours)
        + ")",
    )


def _witness(owner: MemoryChannel, relation: MemoryRelation, raws: Mapping[str, int]) -> int:
    """The value a value relation agreed on (see ``MemoryArithmeticRelation.witness``)."""
    if isinstance(relation, MemoryArithmeticRelation):
        return relation.witness(raws[owner.name], [raws[name] for name in relation.operands])
    return raws[owner.name]


def _memory_body(address: int, length: int) -> bytes:
    """KWP32 address-and-length body accepted by this ECM."""
    return address.to_bytes(4, "big") + length.to_bytes(2, "big")


def verify_channel(
    client: MemoryVerificationClient,
    channel: MemoryChannel,
    *,
    catalog: Sequence[MemoryChannel],
    samples: int,
    request_id: int,
    session: int | None,
    exit_session: int | None = None,
    exit_grace_seconds: float | None = None,
    sleep: Callable[[float], None] = time.sleep,
    before_sample: Callable[[], None] | None = None,
) -> ChannelVerdict:
    """Re-verify `channel` by its DID known answer and every relation that reads it.

    ``catalog`` is required because a relation declared on another channel
    can be the only check this one has (an operand of a sum, an item an OR
    reads): without it the channel would read as unverifiable for a reason
    that has nothing to do with the address. ``exit_session`` and
    ``exit_grace_seconds`` (the access block's) are what a Mode 01 relation
    needs to leave the session between the word and the PID; an absent grace
    is the collector's own default (``DidSessionSpec``), as the access block
    documents.

    ``before_sample`` runs before every sample of every check: the dashboard
    route renews its ECM-session claim there and raises
    :class:`VerifyAbortedError` when another holder has taken the ECM, so a
    long channel neither outlives its claim nor reads through a foreign
    tester's session.

    A ``fail`` means bytes were read and disagreed. Silence is never a fail:
    a refused session or reads that all went unanswered (the gate suppressing
    our frames, another tester on the ECM) are ``unverifiable`` with a named
    cause (``session_entry_refused``, ``reads_unanswered``).
    """
    known = channel.known_answer
    checks = memory_checks(channel, catalog)
    if known is None and not checks:
        return ChannelVerdict(
            channel.name,
            "unverifiable",
            "no known-answer check declared; an address cannot be re-verified "
            "on plausibility alone",
        )
    if session is not None and not client.enter_diagnostic_session(session, request_id=request_id):
        return ChannelVerdict(
            channel.name,
            "unverifiable",
            f"session_entry_refused: session 0x{session:02X} refused or unanswered; "
            "that is the bus, not the address",
        )

    did_verdict = (
        None
        if known is None
        else _verify_did(
            client,
            channel,
            samples=samples,
            request_id=request_id,
            before_sample=before_sample,
        )
    )
    if not checks:
        assert did_verdict is not None
        return did_verdict
    by_name = {c.name: c for c in catalog}
    relation_verdicts = [
        _verify_relation(
            client,
            channel,
            owner,
            relation,
            by_name,
            samples=samples,
            request_id=request_id,
            session=session,
            exit_session=exit_session,
            exit_grace_seconds=(
                DidSessionSpec.exit_grace_seconds
                if exit_grace_seconds is None
                else exit_grace_seconds
            ),
            sleep=sleep,
            before_sample=before_sample,
        )
        for owner, relation in checks
    ]
    return _combine(channel, did_verdict, relation_verdicts)


def _verify_did(
    client: MemoryVerificationClient,
    channel: MemoryChannel,
    *,
    samples: int,
    request_id: int,
    before_sample: Callable[[], None] | None = None,
) -> ChannelVerdict:
    known = channel.known_answer
    assert known is not None
    agreements = observed = sentinels = 0
    out_of_range: list[float] = []
    for _ in range(samples):
        if before_sample is not None:
            before_sample()
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
        return ChannelVerdict(
            channel.name,
            "unverifiable",
            f"reads_unanswered: no memory/DID reply pair in {samples} samples; "
            "that is the bus, not the address",
        )
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


def _verify_relation(
    client: MemoryVerificationClient,
    subject: MemoryChannel,
    owner: MemoryChannel,
    relation: MemoryRelation,
    by_name: Mapping[str, MemoryChannel],
    *,
    samples: int,
    request_id: int,
    session: int | None,
    exit_session: int | None,
    exit_grace_seconds: float,
    sleep: Callable[[float], None],
    before_sample: Callable[[], None] | None = None,
) -> RelationVerdict:
    """Read `samples` samples of one relation and judge it for `subject`."""
    needs_pid = isinstance(relation, Mode01PidRelation)
    if needs_pid and session is not None and exit_session is None:
        return RelationVerdict(
            subject.name,
            owner.name,
            relation.label,
            "unverifiable",
            f"{relation.label} on {owner.name}: Mode 01 does not answer inside session "
            f"0x{session:02X}, and no exit_session is configured to leave it",
        )
    reads = relation_reads(subject, owner, relation, by_name)
    collected: list[RelationSample] = []
    for _ in range(samples):
        if before_sample is not None:
            before_sample()
        replies: dict[int, bytes] = {}
        for address, length in reads:
            reply = client.query_raw(0x23, _memory_body(address, length), request_id=request_id)
            payload = getattr(reply, "payload", None)
            if not payload:
                break
            replies[address] = bytes(payload)
        else:
            pid_payload: bytes | None = None
            if isinstance(relation, Mode01PidRelation):
                if session is not None and exit_session is not None:
                    if not client.enter_diagnostic_session(exit_session, request_id=request_id):
                        return RelationVerdict(
                            subject.name,
                            owner.name,
                            relation.label,
                            "unverifiable",
                            f"session_exit_refused: {relation.label} on {owner.name}: the "
                            f"ECM refused exit 0x{exit_session:02X}, so Mode 01 could not "
                            "be read; that is the bus, not the address",
                        )
                    sleep(exit_grace_seconds)
                response = client.query_mode_01(relation.pid)
                pid_payload = (
                    bytes(response.payload)
                    if response is not None and getattr(response, "payload", None)
                    else None
                )
                if session is not None and not client.enter_diagnostic_session(
                    session, request_id=request_id
                ):
                    return RelationVerdict(
                        subject.name,
                        owner.name,
                        relation.label,
                        "unverifiable",
                        f"session_reentry_refused: {relation.label} on {owner.name}: "
                        f"session 0x{session:02X} refused on re-entry after the Mode 01 "
                        "read; that is the bus, not the address",
                    )
            collected.append(RelationSample(replies, pid_payload))
    return evaluate_relation(subject, owner, relation, by_name, collected)


def _combine(
    channel: MemoryChannel,
    did_verdict: ChannelVerdict | None,
    relation_verdicts: Sequence[RelationVerdict],
) -> ChannelVerdict:
    """One channel verdict: any failure fails; else any pass passes.

    A relation that held but could not discriminate (``unverifiable``) does
    not undo another check's pass, and does not count as one.
    """
    checks = tuple(relation_verdicts)
    parts: list[tuple[Outcome, str, int, int, int]] = []
    if did_verdict is not None:
        parts.append(
            (
                did_verdict.outcome,
                did_verdict.reason,
                did_verdict.samples,
                did_verdict.agreements,
                did_verdict.sentinels,
            )
        )
    parts.extend((v.outcome, v.reason, v.samples, v.agreements, 0) for v in relation_verdicts)
    for wanted in ("fail", "pass"):
        for outcome, reason, observed, agreed, sentinels in parts:
            if outcome == wanted:
                return ChannelVerdict(
                    channel.name, outcome, reason, observed, agreed, sentinels, checks
                )
    observed = max(part[2] for part in parts)
    return ChannelVerdict(
        channel.name,
        "unverifiable",
        "; ".join(part[1] for part in parts),
        observed,
        0,
        0,
        checks,
    )


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
                f"{label}: {', '.join(unverifiable)} could not be verified (no check "
                "declared, or a relation that held without discriminating), and a window "
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
    "NeighbourResult",
    "RelationSample",
    "RelationVerdict",
    "RestampRefusedError",
    "VerifyAbortedError",
    "evaluate_relation",
    "plan_restamp",
    "raw_at",
    "relation_holds",
    "restamp",
    "verify_channel",
]
