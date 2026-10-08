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
from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator


class _Strict(BaseModel):
    model_config = ConfigDict(extra="forbid")


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


class RawCondition(_Strict):
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


class MemoryArithmeticRelation(_Strict):
    """The declaring channel's raw value against other memory channels.

    * ``sum_of_halves`` — ``raw == trunc(a / 2) + trunc(b / 2)`` over two
      operands (the combined learned retard).
    * ``trunc_divide`` — ``raw == trunc(a / divisor)`` (whole degrees of a
      1/256-degree word).
    * ``ordering`` — ``raw >= operand_sign * a`` (``compare: ge``) or ``<=``
      (``le``); ``operand_sign: -1`` states "limit >= -retard".

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
        return operands[0] if self.op == "ordering" else own

    def holds(self, own: int, operands: Sequence[int]) -> bool:
        if self.op == "sum_of_halves":
            return own == trunc_div(operands[0], 2) + trunc_div(operands[1], 2)
        if self.op == "trunc_divide":
            assert self.divisor is not None
            return own == trunc_div(operands[0], self.divisor)
        bound = (self.operand_sign or 1) * operands[0]
        return own >= bound if self.compare == "ge" else own <= bound


class FlagEquivalenceRelation(_Strict):
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


class Mode01PidRelation(_Strict):
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


__all__ = [
    "VALUE_RELATION_KINDS",
    "FlagEquivalenceRelation",
    "InvariantRelation",
    "MemoryArithmeticRelation",
    "MemoryRelation",
    "Mode01PidRelation",
    "RawCondition",
    "RawTest",
    "trunc_div",
]
