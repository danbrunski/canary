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
from functools import partial
from pathlib import Path
from typing import Any, Literal, Protocol

from src.atomic_write import atomic_write_text
from src.collection.obd_scheduler.spec import DidSessionSpec
from src.decode.dids import (
    DidMap,
    MemoryChannel,
    apply_memory_scaling,
    check_known_answer,
    is_memory_sentinel,
    memory_checks,
    neighbour_span,
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
from src.yaml_load import load_yaml


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
class KnownAnswerSample:
    """One sample of a DID known answer: the memory reply and the DID's raw value.

    ``replies`` maps the read's start address to its payload, widened to the
    neighbouring slices (:func:`neighbour_span`); ``did_raw`` is ``None`` when
    the DID did not answer.
    """

    replies: Mapping[int, bytes]
    did_raw: int | None


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
    neighbour_counts = _neighbour_counts(subject)
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
            _count_neighbours(
                subject,
                sample.replies,
                neighbour_counts,
                partial(_holds_with, owner, relation, raws, subject.name, sample.pid_payload),
            )
    neighbours = _neighbour_results(subject, neighbour_counts)
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
    undiscriminating = _undiscriminating(
        where, agreements, observed, neighbours, relation.min_agreement
    )
    if undiscriminating is not None:
        return verdict("unverifiable", undiscriminating)
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
    return verdict("pass", _held_and_failed_beside(where, agreements, observed, neighbours))


def _holds_with(
    owner: MemoryChannel,
    relation: MemoryRelation,
    raws: Mapping[str, int],
    subject_name: str,
    pid_payload: bytes | None,
    moved: int,
) -> bool | None:
    """`relation` on one sample with the subject's raw replaced by `moved`."""
    return relation_holds(owner, relation, {**raws, subject_name: moved}, pid_payload)


def _neighbour_counts(subject: MemoryChannel) -> dict[int, list[int]]:
    """``{shift: [judged, held]}`` for the slices one subject-width either side."""
    width = subject.byte_length
    return {shift: [0, 0] for shift in (-width, width)}


def _count_neighbours(
    subject: MemoryChannel,
    replies: Mapping[int, bytes],
    counts: dict[int, list[int]],
    holds_at: Callable[[int], bool | None],
) -> None:
    """Re-apply a check (``holds_at(moved_raw)``) to each neighbouring slice."""
    for shift, tally in counts.items():
        moved = raw_at(subject, replies, shift)
        if moved is None:
            continue
        held = holds_at(moved)
        if held is None:
            continue
        tally[0] += 1
        tally[1] += int(held)


def _neighbour_results(
    subject: MemoryChannel, counts: Mapping[int, list[int]]
) -> tuple[NeighbourResult, ...]:
    return tuple(
        NeighbourResult(shift, subject.address + shift, tally[0], tally[1])
        for shift, tally in counts.items()
    )


def _undiscriminating(
    where: str,
    agreements: int,
    observed: int,
    neighbours: Sequence[NeighbourResult],
    min_agreement: float,
) -> str | None:
    """Why a check that held did not single out its address, or ``None``.

    The neighbour rule every check shares, a relation's and a DID known
    answer's alike: a check never read beside the address was never shown
    able to fail, and one that holds there too did not tell the address from
    the bytes beside it. Either is ``unverifiable``, never ``pass``.
    """
    unread = [n for n in neighbours if n.samples == 0]
    if unread:
        return (
            f"{where} held for {agreements}/{observed}, but the neighbouring bytes at "
            + ", ".join(f"0x{n.address:08X}" for n in unread)
            + " were not read, so it was never shown able to fail"
        )
    held_beside = [n for n in neighbours if n.agreements >= n.samples * min_agreement]
    if held_beside:
        return (
            f"{where} held for {agreements}/{observed}, and also on the neighbouring bytes at "
            + ", ".join(f"0x{n.address:08X} ({n.agreements}/{n.samples})" for n in held_beside)
            + ": it did not tell this address from the bytes beside it"
        )
    return None


def _held_and_failed_beside(
    where: str, agreements: int, observed: int, neighbours: Sequence[NeighbourResult]
) -> str:
    return (
        f"{where} held for {agreements}/{observed} reads and failed beside it ("
        + ", ".join(f"0x{n.address:08X} {n.agreements}/{n.samples}" for n in neighbours)
        + ")"
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

    A ``fail`` means bytes were read and disagreed with a reference that
    moved (a DID stuck at one value, or at rpm 0 under a reciprocal, is
    ``did_constant`` / ``did_degenerate``). Silence is never a fail:
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
    """Read `samples` (word, DID) pairs and judge them by :func:`evaluate_known_answer`.

    The memory read is widened to carry both neighbouring slices
    (:func:`neighbour_span`), the same read a relation takes, so the known
    answer is tried beside the address in the same reply.
    """
    known = channel.known_answer
    assert known is not None
    address, length = neighbour_span(channel)
    collected: list[KnownAnswerSample] = []
    for _ in range(samples):
        if before_sample is not None:
            before_sample()
        reply = client.query_raw(0x23, _memory_body(address, length), request_id=request_id)
        payload = getattr(reply, "payload", None)
        if not payload:
            continue
        did_response = client.query_mode_22(known.did, request_id=request_id)
        if did_response is None or not did_response.payload:
            continue
        did_raw = int.from_bytes(did_response.payload, "big")
        collected.append(KnownAnswerSample({address: bytes(payload)}, did_raw))
    return evaluate_known_answer(channel, collected, requested=samples)


def evaluate_known_answer(
    channel: MemoryChannel,
    samples: Sequence[KnownAnswerSample],
    *,
    requested: int | None = None,
) -> ChannelVerdict:
    """Judge `channel`'s DID known answer by the rules the D03.38 relations use.

    The legacy kinds (``clamped_high_byte``, ``equals_did``, ``linear_did``,
    ``reciprocal_did``) used to pass on agreement alone. A key-on read on
    2026-10-10 (``ignition_only``, 20 samples, recorded 0) showed what that
    costs: seven passes were a constant matching a constant -- rpm 0 equal
    to rpm 0, a timing byte at its key-off zero, every value outside its
    plausible bounds -- and three fails were the DID side having nothing to
    say (the reciprocal of rpm 0; a reference that never moved). So:

    * **The DID side must say something.** A DID reading on which the
      relation is undefined (``reciprocal_did`` of 0: no period at 0 rpm) is
      not a sample; a DID that read one constant over every sample cannot
      separate agreement from coincidence. Both are ``unverifiable`` with a
      named cause (``did_degenerate``, ``did_constant``), checked *before*
      the agreement threshold: a ``fail`` means the bytes disagreed with a
      reference that moved.
    * **It must fail beside the address** (:func:`_undiscriminating`, shared
      with the relations).
    * **The agreement must be exercised.** An agreeing sample counts as a
      witness only when it is neither a sentinel nor outside the channel's
      plausible bounds, and the compared value is not trivial for the kind
      (:func:`_known_answer_exercised`: non-zero, or above the clamp floor);
      at least two distinct DID values must be witnessed. One constant
      witnessed twenty times is still a constant.

    Anything short of that is ``unverifiable``, never ``pass``, so a record
    run (``plan_restamp``) cannot stamp an address from an unexercised read.
    """
    known = channel.known_answer
    if known is None:
        raise ValueError(f"{channel.name} declares no known-answer check")
    where = f"known-answer relation {known.relation} (DID 0x{known.did:04X})"
    neighbour_counts = _neighbour_counts(channel)
    observed = agreements = sentinels = degenerate = out_of_range = 0
    did_values: set[int] = set()
    witnessed: set[int] = set()
    for sample in samples:
        word = raw_at(channel, sample.replies)
        did_raw = sample.did_raw
        if word is None or did_raw is None:
            continue
        if not _did_defined(channel, did_raw):
            degenerate += 1
            continue
        observed += 1
        did_values.add(did_raw)
        held = check_known_answer(channel, word, did_raw)
        agreements += int(held)
        sentinel = is_memory_sentinel(channel, word)
        sentinels += int(sentinel)
        plausible = not sentinel and _plausible(channel, word)
        out_of_range += int(not sentinel and not plausible)
        if held and plausible and _known_answer_exercised(channel, word, did_raw):
            witnessed.add(did_raw)
        _count_neighbours(
            channel,
            sample.replies,
            neighbour_counts,
            partial(_known_answer_at, channel, did_raw=did_raw),
        )
    neighbours = _neighbour_results(channel, neighbour_counts)

    def verdict(outcome: Outcome, reason: str) -> ChannelVerdict:
        return ChannelVerdict(channel.name, outcome, reason, observed, agreements, sentinels)

    if observed == 0 and degenerate:
        return verdict(
            "unverifiable",
            f"did_degenerate: {where}: the DID read 0 on all {degenerate} samples, where "
            f"{known.relation} is undefined (no period at 0 rpm); nothing was exercised -- "
            "verify with the engine running",
        )
    if observed == 0:
        asked = f" in {requested} samples" if requested is not None else ""
        return verdict(
            "unverifiable",
            f"reads_unanswered: no memory/DID reply pair{asked}; that is the bus, not the address",
        )
    if len(did_values) < 2:
        (constant,) = did_values
        return verdict(
            "unverifiable",
            f"did_constant: {where}: the DID read one value ({constant}) on all {observed} "
            f"samples ({agreements} agreed); a reference that never moved cannot tell "
            "agreement from coincidence -- verify with the engine running",
        )
    if agreements < observed * known.min_agreement:
        return verdict(
            "fail",
            f"{where} held for {agreements}/{observed} reads against a DID that moved "
            f"({len(did_values)} distinct values); the address has probably moved",
        )
    undiscriminating = _undiscriminating(
        where, agreements, observed, neighbours, known.min_agreement
    )
    if undiscriminating is not None:
        return verdict("unverifiable", undiscriminating)
    if len(witnessed) < 2:
        seen = "no agreeing sample" if not witnessed else f"one DID value ({next(iter(witnessed))})"
        return verdict(
            "unverifiable",
            f"{where} held for {agreements}/{observed}, but {seen} was exercised -- the "
            "rest agreed only on zero, clamped, sentinel or implausible values: a constant "
            "matching a constant is not evidence",
        )
    reason = _held_and_failed_beside(where, agreements, observed, neighbours)
    if out_of_range:
        reason += f"; {out_of_range} scaled value(s) were outside plausible bounds"
    return verdict("pass", reason)


def _known_answer_at(channel: MemoryChannel, moved: int, *, did_raw: int) -> bool:
    return check_known_answer(channel, moved, did_raw)


def _did_defined(channel: MemoryChannel, did_raw: int) -> bool:
    """Whether the known answer is defined at `did_raw` (a reciprocal of 0 is not)."""
    known = channel.known_answer
    assert known is not None
    return not (known.relation == "reciprocal_did" and did_raw == 0)


def _plausible(channel: MemoryChannel, word: int) -> bool:
    value = apply_memory_scaling(channel, word)
    if value is None:
        return True  # no scale pinned: bounds cannot be applied, the kind rule still is
    return not (
        (channel.plausible_min is not None and value < channel.plausible_min)
        or (channel.plausible_max is not None and value > channel.plausible_max)
    )


def _known_answer_exercised(channel: MemoryChannel, word: int, did_raw: int) -> bool:
    """Whether an agreeing (word, DID) pair compared a non-trivial value.

    The legacy kinds' counterpart of the relations' non-zero witness:
    ``clamped_high_byte`` must sit above its floor (every word under it maps
    to the floor, so agreement there says nothing about the low bits'
    owner); the others must compare a non-zero value on both sides.
    """
    known = channel.known_answer
    assert known is not None
    if known.relation == "clamped_high_byte":
        return (word >> 8) > known.floor
    if known.relation == "linear_did":
        assert known.did_scale is not None and known.word_scale is not None
        reference = did_raw * known.did_scale + (known.did_offset or 0.0)
        return word * known.word_scale != 0 and reference != 0
    return word != 0 and did_raw != 0


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
        DidMap.model_validate(load_yaml(text))
    except Exception as exc:
        raise RestampRefusedError(f"the stamped catalog would not load: {exc}") from exc
    atomic_write_text(dids_path, text, mkdir=True)
    return changed


__all__ = [
    "ChannelVerdict",
    "KnownAnswerSample",
    "MemoryVerificationClient",
    "NeighbourResult",
    "RelationSample",
    "RelationVerdict",
    "RestampRefusedError",
    "VerifyAbortedError",
    "evaluate_known_answer",
    "evaluate_relation",
    "plan_restamp",
    "raw_at",
    "relation_holds",
    "restamp",
    "verify_channel",
]
