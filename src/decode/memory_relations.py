"""Declared relations that re-verify a RAM channel without a Mode 22 DID.

:class:`src.decode.dids.MemoryKnownAnswer` checks one memory word against one
DID. Most of the knock and λ-chain channels (D03.38) have no DID: what the
firmware guarantees about them is a relation to *other RAM* (a combined word
that is the sum of two halves, a whole-degree copy of a 1/256-degree word, a
flag that mirrors a fault bit), a bit pattern the code never sets, or a Mode
01 PID that reports the same register a cycle later. This module is the schema
for those facts; :mod:`src.analysis.memory_verification` evaluates them.

The relations are enumerations, not expressions, for the reason
``MemoryKnownAnswer`` gives: the arithmetic is code that a test can break, and
the catalog only selects which vehicle fact it asserts. Every relation names
the channels it reads, and the catalog loader refuses one that names an
unknown channel, crosses reads without saying how much skew it allows, or
asks for a read wider than the ECM answers.

A relation verifies *every* channel it reads, not only the one it is declared
on. The verifier proves that by shifting each channel to its neighbouring
bytes and requiring the relation to fail there: a relation that holds at the
neighbour too did not tell the address apart from the bytes beside it, and a
constant matching a constant is not evidence (AGENTS.md, "RAM (memory)
channels").
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from typing import TYPE_CHECKING, Annotated, Literal

from pydantic import Field, model_validator

from src.strict_model import StrictModel

if TYPE_CHECKING:
    # dids.py imports this module at load (its catalog validator reads
    # relations), so the channel type is for annotations only.
    from src.decode.dids import MemoryChannel


def trunc_div(numerator: int, divisor: int) -> int:
    """Integer division rounding toward zero, as the SH-2 code does.

    Python's ``//`` floors, so ``-3 // 2`` is ``-2`` where the firmware's
    truncating divide gives ``-1``; the learned-retard halves are negative
    exactly when they matter.
    """
    if divisor == 0:
        raise ZeroDivisionError("trunc_div by zero")
    quotient = abs(numerator) // abs(divisor)
    return quotient if (numerator >= 0) == (divisor > 0) else -quotient


class RawCondition(StrictModel):
    """A test on one raw (two's-complement when the channel is signed) value.

    Either a bit test (``mask`` and/or ``equals``) or a range (``min`` and/or
    ``max``), never both:

    * ``mask`` alone: ``raw & mask != 0`` (any masked bit set);
    * ``mask`` and ``equals``: ``raw & mask == equals``;
    * ``equals`` alone: ``raw == equals``;
    * ``min``/``max``: ``min <= raw <= max``, either bound optional.
    """

    mask: int | None = Field(default=None, ge=1)
    equals: int | None = None
    min: int | None = None
    max: int | None = None

    @model_validator(mode="after")
    def _one_form(self) -> RawCondition:
        bit_test = self.mask is not None or self.equals is not None
        range_test = self.min is not None or self.max is not None
        if bit_test == range_test:
            raise ValueError(
                "a raw condition is a bit test (mask/equals) or a range (min/max): "
                f"got mask={self.mask}, equals={self.equals}, min={self.min}, max={self.max}"
            )
        if self.min is not None and self.max is not None and self.min > self.max:
            raise ValueError(f"empty range: min {self.min} > max {self.max}")
        if self.mask is not None and self.equals is not None and self.equals & ~self.mask:
            raise ValueError(
                f"equals 0x{self.equals:X} sets bits outside mask 0x{self.mask:X}, "
                "so the condition could never hold"
            )
        return self

    def holds(self, raw: int) -> bool:
        if self.mask is not None:
            masked = raw & self.mask
            return masked != 0 if self.equals is None else masked == self.equals
        if self.equals is not None:
            return raw == self.equals
        return (self.min is None or raw >= self.min) and (self.max is None or raw <= self.max)

    def describe(self, subject: str) -> str:
        if self.mask is not None:
            if self.equals is None:
                return f"{subject} & 0x{self.mask:X} != 0"
            return f"{subject} & 0x{self.mask:X} == 0x{self.equals:X}"
        if self.equals is not None:
            return f"{subject} == {self.equals}"
        low = "" if self.min is None else f"{self.min} <= "
        high = "" if self.max is None else f" <= {self.max}"
        return f"{low}{subject}{high}"


class RawTest(RawCondition):
    """A :class:`RawCondition` on a named memory channel."""

    channel: str = Field(min_length=1)


class MemoryArithmeticRelation(StrictModel):
    """The declaring channel's raw value against other memory channels.

    * ``sum_of_halves`` — ``raw == trunc(a / 2) + trunc(b / 2)`` over two
      operands (the combined learned retard).
    * ``trunc_divide`` — ``raw == trunc(a / divisor)`` (whole degrees of a
      1/256-degree word).
    * ``ordering`` — ``raw >= operand_sign * a`` (``compare: ge``) or ``<=``
      (``le``); ``operand_sign: -1`` states "limit >= -retard". With
      ``operand_mask`` the operand is ``a & operand_mask`` first: a timing
      trim byte carries a cell flag in bit 7 (set on 143 of 256 cells of
      ZS44A-20261004), and unmasked a flagged byte >= 128 bounds any trim
      term, so the ordering would pass whatever the address held.

    ``read`` says whether every operand comes back in the declaring channel's
    own reply (``same_reply``: they share its ``window``, so there is no
    acquisition skew and the relation is exact) or from separate reads
    (``cross_read``: the loader then requires ``min_agreement`` to be stated,
    because the value may move between the two requests).
    """

    kind: Literal["memory"]
    op: Literal["sum_of_halves", "trunc_divide", "ordering"]
    read: Literal["same_reply", "cross_read"]
    operands: list[str] = Field(min_length=1)
    divisor: int | None = None
    compare: Literal["ge", "le"] | None = None
    operand_sign: Literal[1, -1] | None = None
    operand_mask: int | None = Field(default=None, ge=1)
    min_agreement: float = Field(default=1.0, gt=0.0, le=1.0)
    notes: str | None = None

    @model_validator(mode="after")
    def _op_has_its_parameters(self) -> MemoryArithmeticRelation:
        need = {"sum_of_halves": 2, "trunc_divide": 1, "ordering": 1}[self.op]
        if len(self.operands) != need:
            raise ValueError(f"{self.op} takes {need} operand(s), got {self.operands}")
        if len(set(self.operands)) != len(self.operands):
            raise ValueError(f"{self.op} names an operand twice: {self.operands}")
        if (self.divisor is not None) != (self.op == "trunc_divide"):
            raise ValueError("divisor is required by trunc_divide and refused elsewhere")
        if self.divisor == 0:
            raise ValueError("trunc_divide divisor cannot be 0")
        if (self.compare is not None) != (self.op == "ordering"):
            raise ValueError("compare is required by ordering and refused elsewhere")
        if self.operand_sign is not None and self.op != "ordering":
            raise ValueError("operand_sign applies only to ordering")
        if self.operand_mask is not None and self.op != "ordering":
            raise ValueError("operand_mask applies only to ordering")
        if self.read == "cross_read" and "min_agreement" not in self.model_fields_set:
            raise ValueError(
                "a cross_read relation must state min_agreement: the operand is read "
                "in another request, and how much it may move in between is a fact "
                "about the quantity, not a default"
            )
        return self

    @property
    def label(self) -> str:
        return f"memory/{self.op}"

    def channels(self) -> tuple[str, ...]:
        return tuple(self.operands)

    def witness(self, own: int, operands: Sequence[int]) -> int:
        """The value the relation compared and agreed on.

        For ``sum_of_halves`` and ``trunc_divide`` that is the declaring
        word (the result); for ``ordering`` it is the bound, the operand.
        Zero here means the agreement was zero matching zero: retard -100
        truncates to 0 and "matches" a whole-degree byte that reads 0
        whatever it is.
        """
        return self._operand(operands[0]) if self.op == "ordering" else own

    def _operand(self, raw: int) -> int:
        return raw if self.operand_mask is None else raw & self.operand_mask

    def holds(self, own: int, operands: Sequence[int]) -> bool:
        if self.op == "sum_of_halves":
            return own == trunc_div(operands[0], 2) + trunc_div(operands[1], 2)
        if self.op == "trunc_divide":
            assert self.divisor is not None
            return own == trunc_div(operands[0], self.divisor)
        bound = (self.operand_sign or 1) * self._operand(operands[0])
        return own >= bound if self.compare == "ge" else own <= bound


class FlagEquivalenceRelation(StrictModel):
    """A flag state across reads: ``when`` (all hold) ⇔ / ⇒ ``then``.

    ``then`` combines with ``then_combine`` (``any``: an OR over item bits;
    ``all``). ``logic: implies`` asserts only the forward direction, and holds
    vacuously whenever ``when`` is false. The verifier counts a pass only
    when ``when`` was seen true at least once, and for ``iff`` both true and
    false: a flag that never set (false ⇔ false) or never moved says nothing
    about which byte it is.

    The declaring channel must appear in ``when`` or ``then``: a relation that
    does not read the address cannot vouch for it.
    """

    kind: Literal["flag_equivalence"]
    logic: Literal["iff", "implies"] = "iff"
    when: list[RawTest] = Field(min_length=1)
    then: list[RawTest] = Field(min_length=1)
    then_combine: Literal["any", "all"] = "all"
    min_agreement: float = Field(default=1.0, gt=0.0, le=1.0)
    notes: str | None = None

    @property
    def label(self) -> str:
        return f"flag_equivalence/{self.logic}"

    def channels(self) -> tuple[str, ...]:
        return tuple(dict.fromkeys(t.channel for t in (*self.when, *self.then)))

    def antecedent(self, raws: Mapping[str, int]) -> bool:
        return all(t.holds(raws[t.channel]) for t in self.when)

    def holds_on(self, raws: Mapping[str, int]) -> bool:
        antecedent = self.antecedent(raws)
        results = [t.holds(raws[t.channel]) for t in self.then]
        consequent = any(results) if self.then_combine == "any" else all(results)
        if self.logic == "iff":
            return antecedent == consequent
        return (not antecedent) or consequent


class InvariantRelation(RawCondition):
    """A condition the declaring channel's raw value always satisfies.

    A bit-mask invariant (``mask`` + ``equals``: bits the code never sets) or
    a range. Weak on its own — many bytes satisfy ``& 0xC0 == 0`` — so it
    counts only where the neighbouring bytes break it AND the channel took at
    least two distinct raw values over the agreeing samples: a constant
    inside the range is a constant matching a constant.
    """

    kind: Literal["invariant"]
    min_agreement: float = Field(default=1.0, gt=0.0, le=1.0)
    notes: str | None = None

    @property
    def label(self) -> str:
        return "invariant"

    def channels(self) -> tuple[str, ...]:
        return ()


class Mode01PidRelation(StrictModel):
    """The declaring channel against a Mode 01 PID, read across a session exit.

    Mode 01 is silent inside the session that unlocks service 0x23, so the
    verifier reads the word, exits the session, waits the access block's
    ``exit_grace_seconds`` and reads the PID: the two are never simultaneous.
    The comparison is therefore a per-sample ratio — ``expected / pid_raw``
    within ``relative_tolerance`` — never exact equality, and both
    ``relative_tolerance`` and ``min_agreement`` are required.

    ``expected`` is ``raw * scale`` (``form: linear``) or ``scale / raw``
    (``reciprocal``), in PID counts, clamped at ``pid_saturation`` where the
    PID field saturates (PID 0x24's λ reads 2.0 at word >= 14,700).
    """

    kind: Literal["mode01_pid"]
    pid: int = Field(ge=0x01, le=0xFF)
    pid_byte_start: int = Field(default=0, ge=0, le=6)
    pid_byte_length: int = Field(ge=1, le=4)
    form: Literal["linear", "reciprocal"] = "linear"
    scale: float = Field(gt=0)
    pid_saturation: int | None = Field(default=None, ge=1)
    relative_tolerance: float = Field(gt=0.0, lt=1.0)
    min_agreement: float = Field(gt=0.0, le=1.0)
    notes: str | None = None

    @property
    def label(self) -> str:
        return f"mode01_pid/0x{self.pid:02X}"

    def channels(self) -> tuple[str, ...]:
        return ()

    def pid_raw(self, payload: bytes) -> int | None:
        end = self.pid_byte_start + self.pid_byte_length
        if len(payload) < end:
            return None
        return int.from_bytes(payload[self.pid_byte_start : end], "big")

    def holds(self, own: int, pid_raw: int) -> bool:
        if self.form == "reciprocal":
            if own <= 0:
                return False
            expected = self.scale / own
        else:
            expected = own * self.scale
        if self.pid_saturation is not None:
            expected = min(expected, float(self.pid_saturation))
        if pid_raw == 0:
            return expected == 0
        return abs(expected / pid_raw - 1.0) <= self.relative_tolerance


MemoryRelation = Annotated[
    MemoryArithmeticRelation | FlagEquivalenceRelation | InvariantRelation | Mode01PidRelation,
    Field(discriminator="kind"),
]

#: Relations whose holding means two *values* agree, so the agreement must be
#: shown on a non-zero value: zero matching zero is a constant matching a
#: constant. Flag and invariant relations are tests of state, where a stuck
#: flag or a clear mask is the normal reading.
VALUE_RELATION_KINDS = frozenset({"memory", "mode01_pid"})


# ---------------------------------------------------------------------------
# Verification helpers: which checks read a channel, what reads one sample of
# a check takes, and whether a DID known answer holds (R23.15 moved them here
# from ``decode/dids.py``; their callers are the verifier and the
# calibration view).
# ---------------------------------------------------------------------------


def check_known_answer(channel: MemoryChannel, word: int, did_raw: int) -> bool:
    """Whether `did_raw` is what `channel`'s declared relation predicts.

    ``word`` is the memory channel's raw value and ``did_raw`` the raw value
    of the cross-check DID, both read at the same instant.
    """
    known = channel.known_answer
    if known is None:
        raise ValueError(f"{channel.name} declares no known-answer check")
    if known.relation == "clamped_high_byte":
        return did_raw == max(known.floor, word >> 8)
    if known.relation == "equals_did":
        return did_raw == word
    if known.relation == "linear_did":
        assert known.word_scale is not None and known.did_scale is not None
        reference = did_raw * known.did_scale + (known.did_offset or 0.0)
        allowed = max(known.tolerance or 0.0, (known.relative_tolerance or 0.0) * abs(reference))
        return abs(word * known.word_scale - reference) <= allowed
    if known.relation == "reciprocal_did":
        assert known.constant is not None and known.tolerance is not None
        return word > 0 and abs(known.constant / word - did_raw) <= known.tolerance
    raise ValueError(f"unhandled known-answer relation {known.relation!r}")


def relation_channel_names(owner: MemoryChannel, relation: MemoryRelation) -> tuple[str, ...]:
    """Every memory channel `relation` (declared on `owner`) reads, owner first."""
    return tuple(dict.fromkeys((owner.name, *relation.channels())))


def memory_checks(
    channel: MemoryChannel, catalog: Sequence[MemoryChannel]
) -> list[tuple[MemoryChannel, MemoryRelation]]:
    """Every declared relation in `catalog` that reads `channel`.

    A relation verifies each channel it reads, not only the one it is
    declared on: ``0x1252 = trunc(0x124E/2) + trunc(0x1250/2)`` would fail if
    any of the three moved. So whether a channel is checkable is a question
    about the catalog, never about the channel's own entry.
    """
    return [
        (owner, relation)
        for owner in catalog
        for relation in owner.relations
        if channel.name in relation_channel_names(owner, relation)
    ]


def is_memory_checkable(channel: MemoryChannel, catalog: Sequence[MemoryChannel]) -> bool:
    """Whether anything can re-verify `channel`: a DID known answer or a relation."""
    return channel.known_answer is not None or bool(memory_checks(channel, catalog))


def window_key(channel: MemoryChannel) -> str:
    """What groups `channel` with the others it is read beside: its window,
    or a key no window can take."""
    return channel.window or f"\x00{channel.name}"


def relation_reads(
    subject: MemoryChannel,
    owner: MemoryChannel,
    relation: MemoryRelation,
    by_name: Mapping[str, MemoryChannel],
) -> list[tuple[int, int]]:
    """The service-0x23 reads (address, length) one sample of a check takes.

    Channels sharing a window come back in one reply, as the collector reads
    them. The read holding `subject` is widened by the subject's width on
    both sides, so the same reply also carries the neighbouring bytes the
    relation must fail on. The subject's read comes first.
    """
    groups: dict[str, list[MemoryChannel]] = {}
    for name in relation_channel_names(owner, relation):
        channel = by_name[name]
        groups.setdefault(window_key(channel), []).append(channel)
    reads: list[tuple[int, int]] = []
    subject_key = window_key(subject)
    for key in [subject_key, *(k for k in groups if k != subject_key)]:
        members = groups[key]
        start = min(c.address for c in members)
        end = max(c.address + c.length for c in members)
        if key == subject_key:
            span_start, span_length = neighbour_span(subject)
            start = min(start, span_start)
            end = max(end, span_start + span_length)
        reads.append((start, end - start))
    return reads


def neighbour_span(subject: MemoryChannel) -> tuple[int, int]:
    """The (address, length) read that carries `subject` and both neighbours.

    Widened by the subject's width on each side: a check is only evidence
    if it fails on the bytes beside the address, so every verifier read --
    a relation's or a DID known answer's -- takes them in the same reply.
    """
    start = subject.address - subject.byte_length
    end = subject.address + subject.length + subject.byte_length
    return start, end - start


__all__ = [
    "VALUE_RELATION_KINDS",
    "check_known_answer",
    "is_memory_checkable",
    "memory_checks",
    "neighbour_span",
    "relation_channel_names",
    "relation_reads",
    "window_key",
    "FlagEquivalenceRelation",
    "InvariantRelation",
    "MemoryArithmeticRelation",
    "MemoryRelation",
    "Mode01PidRelation",
    "RawCondition",
    "RawTest",
    "trunc_div",
]
