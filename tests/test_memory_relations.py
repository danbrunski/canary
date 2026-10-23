"""Declared memory relations: the D03.38 hand checks the verifier now runs.

Each relation kind is shown three ways, because a relation that cannot fail
is not evidence (AGENTS.md, "RAM (memory) channels"):

* it passes on a synthetic RAM image where the firmware's relation holds;
* moved to the neighbouring bytes it fails, and the verifier reports that
  per neighbour;
* where the neighbours satisfy it too (or only zeros agree) the verdict is
  ``unverifiable``, never ``pass``.

Two kinds are also run over the committed slices of the 2026-10-04 ram-study
capture, the only stored session that read RAM windows. Its windows hold none
of the knock or lambda bytes, so the catalog's own relations cannot be run on
it. The tests declare relations that hold on the bytes it did read instead:
the same-reply 50-rpm map trace against the 12.5-rpm engine-speed word, and
the engine-period word against Mode 01 rpm across a session exit.
"""

from __future__ import annotations

from collections.abc import Callable
from pathlib import Path
from typing import Any

import duckdb
import pytest
from pydantic import ValidationError

from src.analysis.memory_verification import (
    ChannelVerdict,
    RelationSample,
    evaluate_relation,
    verify_channel,
)
from src.control.calibration import memory_channel_state
from src.decode.dids import (
    DidMap,
    MemoryChannel,
    is_memory_checkable,
    load_did_map,
    memory_checks,
    relation_reads,
)
from src.decode.memory_relations import (
    MemoryArithmeticRelation,
    Mode01PidRelation,
    trunc_div,
)
from src.paths import DIDS_YAML
from src.yaml_load import load_yaml

REPO = Path(__file__).resolve().parents[1]
CATALOG = load_did_map(DIDS_YAML)
MEMORY = CATALOG.memory
BY_NAME = {c.name: c for c in MEMORY}

#: Pseudo-random filler for RAM nobody set: it varies by address and sample,
#: so a neighbour is not accidentally a constant that satisfies a relation.
Image = dict[int, int]


def _filler(address: int, sample: int) -> int:
    return ((address * 131) ^ (sample * 0x5D) ^ 0xA7) & 0xFF


class _Reply:
    def __init__(self, payload: bytes) -> None:
        self.payload = payload


class _RamEcu:
    """Answers service 0x23 from a sequence of RAM images, one per sample.

    A sample ends when an address is read a second time, which is how the
    verifier's per-sample read list repeats. Mode 01 answers through
    ``pid_for(image, pid)``; every call is logged with the session in force.
    """

    def __init__(
        self,
        images: list[Image],
        pid_for: Callable[[Image, int], bytes | None] | None = None,
    ) -> None:
        self.images = images
        self.pid_for = pid_for
        self.index = 0
        self.served: set[int] = set()
        self.log: list[tuple[str, int]] = []

    @property
    def image(self) -> Image:
        return self.images[self.index % len(self.images)]

    def enter_diagnostic_session(self, session: int, *, request_id: int | None = None) -> bool:
        self.log.append(("session", session))
        return True

    def query_raw(self, service: int, body: bytes, *, request_id: int | None = None) -> _Reply:
        assert service == 0x23
        address = int.from_bytes(body[:4], "big")
        length = int.from_bytes(body[4:], "big")
        assert length <= 63, "the verifier asked for more than the ECM answers"
        if address in self.served:
            self.index += 1
            self.served = set()
        self.served.add(address)
        self.log.append(("read", address))
        image = self.image
        return _Reply(
            bytes(image.get(a, _filler(a, self.index)) for a in range(address, address + length))
        )

    def query_mode_22(self, did: int, *, request_id: int | None = None) -> _Reply:
        raise AssertionError("no DID is involved in a memory relation")

    def query_mode_01(self, pid: int) -> _Reply | None:
        self.log.append(("mode01", pid))
        assert self.pid_for is not None
        payload = self.pid_for(self.image, pid)
        return None if payload is None else _Reply(payload)


def _put(image: Image, channel: str, raw: int) -> Image:
    """Write `raw` into `channel`'s slice, two's-complement when signed."""
    c = BY_NAME[channel]
    data = raw.to_bytes(c.byte_length, c.byte_order, signed=c.signed)
    for i, byte in enumerate(data):
        image[c.address + c.byte_start + i] = byte
    return image


def _verify(ecu: _RamEcu, name: str, samples: int = 6, **kwargs: Any) -> ChannelVerdict:
    return verify_channel(
        ecu,
        BY_NAME[name],
        catalog=MEMORY,
        samples=samples,
        request_id=0x7E0,
        session=0xC0,
        **kwargs,
    )


def _failed_beside(verdict: ChannelVerdict) -> None:
    """Every check reports both neighbours read, and each below its threshold."""
    for check in verdict.checks:
        assert len(check.neighbours) == 2, check
        for neighbour in check.neighbours:
            assert neighbour.samples > 0, check
            assert neighbour.agreements < neighbour.samples, check


# ---------------------------------------------------------------------------
# Arithmetic


def test_trunc_rounds_toward_zero_as_the_firmware_does() -> None:
    assert trunc_div(-3, 2) == -1  # // would give -2
    assert trunc_div(3, 2) == 1
    assert trunc_div(-1281, 256) == -5
    assert trunc_div(-255, 256) == 0


# ---------------------------------------------------------------------------
# 1. Same-reply memory-to-memory: sum of halves, trunc-divide, ordering


def _learned(lo: int, hi: int, *, combined: int | None = None) -> Image:
    image: Image = {}
    _put(image, "ECM_KNOCK_LEARN_LO_RPM", lo)
    _put(image, "ECM_KNOCK_LEARN_HI_RPM", hi)
    total = trunc_div(lo, 2) + trunc_div(hi, 2) if combined is None else combined
    _put(image, "ECM_KNOCK_LEARN", total)
    _put(image, "ECM_KNOCK_RETARD", 0)
    return image


LEARNED = [(-301, -517), (-300, -517), (-255, -700), (-3, -1), (-768, -256), (-1, -1001)]


def test_sum_of_halves_verifies_all_three_words_and_fails_beside_them() -> None:
    for name in ("ECM_KNOCK_LEARN", "ECM_KNOCK_LEARN_LO_RPM", "ECM_KNOCK_LEARN_HI_RPM"):
        ecu = _RamEcu([_learned(lo, hi) for lo, hi in LEARNED])
        verdict = _verify(ecu, name)
        assert verdict.outcome == "pass", verdict.reason
        assert verdict.agreements == verdict.samples == 6
        _failed_beside(verdict)
        # one reply carries all three words plus the neighbours: same-instant
        reads = {address for kind, address in ecu.log if kind == "read"}
        assert len(reads) == 1


def test_sum_of_halves_fails_when_the_combined_word_is_elsewhere() -> None:
    """A moved address: the combined word now lives at 0x1256 and 0x1252
    holds something else that still decodes as a plausible retard."""
    images = []
    for lo, hi in LEARNED:
        image = _learned(lo, hi, combined=-40)
        _put(image, "ECM_KNOCK_RETARD", trunc_div(lo, 2) + trunc_div(hi, 2))
        images.append(image)
    verdict = _verify(_RamEcu(images), "ECM_KNOCK_LEARN")
    assert verdict.outcome == "fail"
    assert "has moved" in verdict.reason


def test_reset_learned_words_are_not_evidence() -> None:
    """At key-on the learned words are reset: 0 = trunc(0/2) + trunc(0/2)
    holds, and holds just as well on the zero words beside it."""
    verdict = _verify(_RamEcu([_learned(0, 0)]), "ECM_KNOCK_LEARN")
    assert verdict.outcome == "unverifiable"


def _retard(retard: int, *, limit: int = 8, deg: int | None = None) -> Image:
    image: Image = {}
    _put(image, "ECM_KNOCK_RETARD", retard)
    _put(image, "ECM_KNOCK_RETARD_DEG", trunc_div(retard, 256) if deg is None else deg)
    _put(image, "ECM_KNOCK_RETARD_LIMIT", limit)
    # The limit's neighbours are small enough to break "limit >= -deg".
    image[0xFFFF26E5] = 0
    image[0xFFFF26E7] = 1
    return image


RETARDS = [-1280, -1281, -768, -300, -1535, -512]


def test_trunc_divide_across_two_reads_verifies_both_words() -> None:
    for name in ("ECM_KNOCK_RETARD_DEG", "ECM_KNOCK_RETARD"):
        verdict = _verify(_RamEcu([_retard(r) for r in RETARDS]), name)
        checks = {c.relation: c for c in verdict.checks}
        assert checks["memory/trunc_divide"].outcome == "pass", checks
        assert verdict.outcome == "pass"


def test_trunc_divide_fails_on_a_floor_divide() -> None:
    """The relation tells truncation from flooring: -300/256 is -1 truncated
    but -2 floored, so a byte holding the floored value fails."""
    images = [_retard(r, deg=r // 256) for r in (-300, -1281, -1535, -10, -700, -900)]
    verdict = _verify(_RamEcu(images), "ECM_KNOCK_RETARD_DEG")
    assert verdict.outcome == "fail"


def test_ordering_verifies_the_limit_while_a_retard_is_applied() -> None:
    verdict = _verify(_RamEcu([_retard(r) for r in RETARDS]), "ECM_KNOCK_RETARD_LIMIT")
    assert verdict.outcome == "pass", verdict.reason
    _failed_beside(verdict)


def test_ordering_with_no_retard_cannot_tell_the_limit_from_its_neighbours() -> None:
    """-0 <= any unsigned byte: the honest verdict is unverifiable."""
    images = [_retard(0) for _ in range(3)]
    for image in images:
        image[0xFFFF26E5] = 40
        image[0xFFFF26E7] = 90
    verdict = _verify(_RamEcu(images), "ECM_KNOCK_RETARD_LIMIT")
    assert verdict.outcome == "unverifiable"
    assert "neighbouring bytes" in verdict.reason


def test_zero_matching_zero_is_not_evidence() -> None:
    """At rest the retard and its whole-degree copy both read 0. The bytes
    beside the copy are not 0, so the neighbour test alone would pass it."""
    verdict = _verify(_RamEcu([_retard(0) for _ in range(3)]), "ECM_KNOCK_RETARD_DEG")
    check = next(c for c in verdict.checks if c.relation == "memory/trunc_divide")
    assert all(n.agreements < n.samples for n in check.neighbours)
    assert check.outcome == "unverifiable"
    assert "zero matching zero" in check.reason


# ---------------------------------------------------------------------------
# 2. Cross-read flag equivalence


def _fallback(engaged: bool, *, item: str = "ECM_KS_P0328_STATUS") -> Image:
    image: Image = {}
    _put(image, "ECM_KNOCK_RETARD_TARGET", -1280 if engaged else -300)
    _put(image, "ECM_KNOCK_HIGHDET_METRIC", 4000)
    _put(image, "ECM_KS_FAULT_FLAGS", 0x80 if engaged else 0x00)
    for name in ("ECM_KS_P0327_STATUS", "ECM_KS_P0328_STATUS"):
        _put(image, name, 0x0000)
    for name in ("ECM_KS_P0332_STATUS", "ECM_KS_P0333_STATUS"):
        _put(image, name, 0x0000)
    if engaged:
        _put(image, item, 0x0001)  # byte 2 bit 0: the early flag 479D ORs
    # Item byte 0 of each record, and the fault byte's neighbours, clear.
    for address in (0xFFFF677A, 0xFFFF677D, 0xFFFF6780, 0xFFFF6783, 0xFFFF6786):
        image[address] = 0x00
    image[0xFFFF479C] = 0x11
    image[0xFFFF479E] = 0x22
    return image


def test_fallback_target_iff_fault_flag_verifies_the_target_word() -> None:
    images = [_fallback(True), _fallback(True), _fallback(False), _fallback(True)]
    verdict = _verify(_RamEcu(images), "ECM_KNOCK_RETARD_TARGET", samples=8)
    assert verdict.outcome == "pass", verdict.reason
    _failed_beside(verdict)


def test_fault_flag_iff_or_of_item_bits_and_iff_target() -> None:
    images = [_fallback(True), _fallback(True, item="ECM_KS_P0333_STATUS"), _fallback(False)]
    verdict = _verify(_RamEcu(images), "ECM_KS_FAULT_FLAGS", samples=6)
    assert verdict.outcome == "pass", verdict.reason
    relations = sorted(c.owner for c in verdict.checks)
    assert relations == ["ECM_KNOCK_RETARD_TARGET", "ECM_KS_FAULT_FLAGS"]
    assert all(c.outcome == "pass" for c in verdict.checks)


def test_the_set_item_is_verified_and_a_clear_item_stays_unverifiable() -> None:
    """Moving P0328's slice breaks the OR while its bit carries it; P0332's
    bit is clear beside clear bytes, so moving it changes nothing."""
    images = [_fallback(True), _fallback(False)]
    assert _verify(_RamEcu(images), "ECM_KS_P0328_STATUS").outcome == "pass"
    assert _verify(_RamEcu(images), "ECM_KS_P0332_STATUS").outcome == "unverifiable"


def test_flag_equivalence_fails_when_the_flag_disagrees() -> None:
    image = _fallback(True)
    _put(image, "ECM_KS_FAULT_FLAGS", 0x00)  # moved: the flag byte is clear
    verdict = _verify(_RamEcu([image]), "ECM_KNOCK_RETARD_TARGET")
    assert verdict.outcome == "fail"


def test_flag_equivalence_with_the_flag_clear_is_not_evidence() -> None:
    """false <=> false holds at the target word and every byte beside it."""
    verdict = _verify(_RamEcu([_fallback(False)]), "ECM_KNOCK_RETARD_TARGET")
    assert verdict.outcome == "unverifiable"


# ---------------------------------------------------------------------------
# 3. Bit-mask invariant (and range)


def test_bit_mask_invariant_passes_and_fails_beside_it() -> None:
    images = []
    for flags, neighbour in ((0x01, 0xC3), (0x3F, 0x80), (0x00, 0x41)):
        image: Image = {}
        _put(image, "ECM_KNOCK_FLAGS_BY_INDEX", flags)
        image[0xFFFF24A7] = neighbour  # threshold idx6 low byte
        image[0xFFFF24A9] = 0xF0
        images.append(image)
    verdict = _verify(_RamEcu(images), "ECM_KNOCK_FLAGS_BY_INDEX")
    assert verdict.outcome == "pass", verdict.reason
    _failed_beside(verdict)


def test_bit_mask_invariant_fails_on_a_byte_with_bit_7_set() -> None:
    image: Image = {}
    _put(image, "ECM_KNOCK_FLAGS_BY_INDEX", 0x81)
    assert _verify(_RamEcu([image]), "ECM_KNOCK_FLAGS_BY_INDEX").outcome == "fail"


def test_range_invariant_beside_a_small_counter_is_unverifiable() -> None:
    """0x249B in 0..6 beside the event count, which is also small."""
    image: Image = {}
    _put(image, "ECM_KNOCK_EVENT_COUNT", 2)
    _put(image, "ECM_KNOCK_WINDOW_INDEX", 4)
    image[0xFFFF249C] = 0x00
    verdict = _verify(_RamEcu([image]), "ECM_KNOCK_WINDOW_INDEX")
    assert verdict.outcome == "unverifiable"


# ---------------------------------------------------------------------------
# 4. Mode 01 PID across a session exit


def _lambda(b1: int, b2: int) -> Image:
    image: Image = {}
    _put(image, "ECM_AF_LAMBDA_B1", b1)
    _put(image, "ECM_AF_LAMBDA_B2", b2)
    _put(image, "ECM_AF_FEEDBACK_TARGET_B1", 7350)
    image[0xFFFF4720] = 0x05
    image[0xFFFF4721] = 0x10
    return image


def _pid_from_word(image: Image, pid: int) -> bytes:
    """PID A,B = (word << 14) / 3675, saturating; C,D the sensor voltage."""
    name = {0x24: "ECM_AF_LAMBDA_B1", 0x28: "ECM_AF_LAMBDA_B2"}[pid]
    c = BY_NAME[name]
    word = int.from_bytes(bytes(image[c.address + i] for i in range(2)), "big")
    return min((word << 14) // 3675, 0xFFFF).to_bytes(2, "big") + b"\x33\x44"


BANKS = [(7350, 7700), (7000, 7360), (7700, 7010), (6900, 7350), (7350, 8000), (15000, 7350)]


def test_lambda_word_against_pid_0x24_across_a_session_exit() -> None:
    sleeps: list[float] = []
    ecu = _RamEcu([_lambda(b1, b2) for b1, b2 in BANKS], _pid_from_word)
    verdict = _verify(
        ecu, "ECM_AF_LAMBDA_B1", exit_session=0x81, exit_grace_seconds=0.05, sleep=sleeps.append
    )
    assert verdict.outcome == "pass", verdict.reason
    assert verdict.agreements == 6  # 15000 (fuel cut) meets the saturated 2.0
    _failed_beside(verdict)
    # Every PID read sits between an exit (0x81) and a re-entry (0xC0).
    for i, entry in enumerate(ecu.log):
        if entry[0] == "mode01":
            assert entry == ("mode01", 0x24)
            assert ecu.log[i - 1] == ("session", 0x81)
            assert ecu.log[i + 1] == ("session", 0xC0)
    assert sleeps == [0.05] * 6


def test_lambda_relation_takes_the_collector_default_grace_when_none_is_recorded() -> None:
    sleeps: list[float] = []
    ecu = _RamEcu([_lambda(b1, b2) for b1, b2 in BANKS], _pid_from_word)
    _verify(ecu, "ECM_AF_LAMBDA_B1", samples=2, exit_session=0x81, sleep=sleeps.append)
    assert sleeps == [0.05, 0.05]


def test_lambda_relation_is_a_ratio_not_an_equality() -> None:
    """3 % of skew passes; a word 5 % off on every sample fails."""

    def skewed(factor: float) -> Callable[[Image, int], bytes]:
        def pid(image: Image, pid: int) -> bytes:
            raw = int.from_bytes(_pid_from_word(image, pid)[:2], "big")
            return min(int(raw * factor), 0xFFFF).to_bytes(2, "big")

        return pid

    images = [_lambda(b1, b2) for b1, b2 in BANKS[:5]]
    near = _verify(
        _RamEcu(images, skewed(1.02)), "ECM_AF_LAMBDA_B1", exit_session=0x81, sleep=lambda _s: None
    )
    assert near.outcome == "pass"
    far = _verify(
        _RamEcu(images, skewed(1.05)), "ECM_AF_LAMBDA_B1", exit_session=0x81, sleep=lambda _s: None
    )
    assert far.outcome == "fail"


def test_banks_that_move_together_cannot_be_told_apart() -> None:
    """Bank 2 beside bank 1 at the same lambda: the PID check holds at the
    neighbour too, so it is unverifiable rather than a pass."""
    images = [_lambda(w, w) for w in (7000, 7350, 7700)]
    verdict = _verify(
        _RamEcu(images, _pid_from_word),
        "ECM_AF_LAMBDA_B1",
        exit_session=0x81,
        sleep=lambda _s: None,
    )
    assert verdict.outcome == "unverifiable"
    assert "0xFFFF4724" in verdict.reason


def test_a_pid_relation_without_a_session_exit_is_unverifiable() -> None:
    ecu = _RamEcu([_lambda(7350, 7000)], _pid_from_word)
    verdict = _verify(ecu, "ECM_AF_LAMBDA_B1", exit_session=None)
    assert verdict.outcome == "unverifiable"
    assert "exit_session" in verdict.reason
    assert not any(kind == "mode01" for kind, _ in ecu.log)


# ---------------------------------------------------------------------------
# The catalog


def test_the_d03_38_hand_checks_are_declared_on_their_channels() -> None:
    declared = {(c.name, r.kind) for c in MEMORY for r in c.relations}
    assert declared == {
        ("ECM_KNOCK_LEARN", "memory"),
        ("ECM_KNOCK_RETARD", "invariant"),
        ("ECM_KNOCK_RETARD_DEG", "memory"),
        ("ECM_KNOCK_RETARD_LIMIT", "memory"),
        ("ECM_KNOCK_RETARD_TARGET", "flag_equivalence"),
        ("ECM_KS_FAULT_FLAGS", "flag_equivalence"),
        ("ECM_KNOCK_WINDOW_INDEX", "invariant"),
        ("ECM_KNOCK_FLAGS_BY_INDEX", "invariant"),
        ("ECM_AF_LAMBDA_B1", "mode01_pid"),
        ("ECM_AF_LAMBDA_B2", "mode01_pid"),
        ("ECM_AF_LEARNED_OFFSET_B1", "invariant"),
        ("ECM_AF_LEARNED_OFFSET_B2", "invariant"),
        # E05.4 (c), the ignition timing chain (2026-10-17): 0xFFFF2332 <=
        # -0xFFFF1215, 0xFFFF2793 <= 0xFFFF1777, 0xFFFF11CB in 0..7.
        ("ECM_KNOCK_RETARD_APPLIED", "memory"),
        ("ECM_TIMING_TRIM_TERM", "memory"),
        ("ECM_TIMING_TRIM_SUBTRAHEND", "invariant"),
        # E09.7, the base timing term (2026-10-17): 0xFFFF15DA <= 0xFFFF15DB
        # in one reply.
        ("ECM_TIMING_RATE_PRODUCT", "memory"),
    }


def test_every_fuel_target_member_is_checkable_so_a_restamp_needs_no_carry() -> None:
    members = [c for c in MEMORY if c.window == "fuel_target"]
    assert {c.name for c in members} >= {"ECM_KNOCK_RETARD_DEG", "ECM_KNOCK_RETARD_LIMIT"}
    assert all(is_memory_checkable(c, MEMORY) for c in members)


def test_an_operand_is_checkable_through_a_relation_declared_elsewhere() -> None:
    lo = BY_NAME["ECM_KNOCK_LEARN_LO_RPM"]
    assert not lo.relations and lo.known_answer is None
    assert [owner.name for owner, _ in memory_checks(lo, MEMORY)] == ["ECM_KNOCK_LEARN"]
    assert memory_channel_state(lo, None, checkable=is_memory_checkable(lo, MEMORY)) == (
        "checked earlier"
    )
    plain = BY_NAME["ECM_UPREV_MAGIC"]
    assert memory_channel_state(plain, None, checkable=False) == "unverifiable"


def test_every_catalog_relation_reads_within_the_ceiling() -> None:
    ceiling = CATALOG.access.max_memory_read_length
    assert ceiling is not None
    for owner in MEMORY:
        for relation in owner.relations:
            for name in (owner.name, *relation.channels()):
                for _address, length in relation_reads(BY_NAME[name], owner, relation, BY_NAME):
                    assert length <= ceiling, (owner.name, name)


# Schema refusals, each naming the channel and relation ----------------------


def _catalog_with(name: str, relations: list[dict[str, Any]]) -> dict[str, Any]:
    raw = CATALOG.model_dump(mode="json", exclude_none=True)
    for entry in raw["memory"]:
        if entry["name"] == name:
            entry["relations"] = relations
    return raw


@pytest.mark.parametrize(
    ("name", "relation", "message"),
    [
        (
            "ECM_KNOCK_LEARN",
            {
                "kind": "memory",
                "op": "sum_of_halves",
                "read": "same_reply",
                "operands": ["ECM_KNOCK_LEARN_LO_RPM", "NO_SUCH_CHANNEL"],
            },
            "ECM_KNOCK_LEARN: relation memory/sum_of_halves names unknown",
        ),
        (
            "ECM_KNOCK_RETARD_DEG",
            {
                "kind": "memory",
                "op": "trunc_divide",
                "read": "same_reply",
                "operands": ["ECM_KNOCK_RETARD"],
                "divisor": 256,
            },
            "ECM_KNOCK_RETARD_DEG: relation memory/trunc_divide is declared same_reply",
        ),
        (
            "ECM_KNOCK_RETARD_LIMIT",
            {
                "kind": "memory",
                "op": "ordering",
                "read": "cross_read",
                "operands": ["ECM_KNOCK_RETARD_DEG"],
                "compare": "ge",
                "min_agreement": 0.9,
            },
            "is declared cross_read, but every operand shares",
        ),
        (
            "ECM_KNOCK_RETARD_TARGET",
            {
                "kind": "flag_equivalence",
                "when": [{"channel": "ECM_KNOCK_RETARD_TARGET", "equals": -1280}],
                "then": [{"channel": "ECM_KS_FAULT_FLAGS", "mask": 0x80}],
            },
            "relation flag_equivalence/iff reads 2 separate windows, so it must state",
        ),
        (
            "ECM_KNOCK_LEARN",
            {
                "kind": "flag_equivalence",
                "when": [{"channel": "ECM_KNOCK_LEARN", "mask": 1}],
                "then": [{"channel": "ECM_KNOCK_INTENSITY_IDX1", "mask": 1}],
                "min_agreement": 1.0,
            },
            None,  # same window? no: knock_retard vs knock_intensity -> accepted
        ),
    ],
)
def test_a_malformed_relation_is_refused_by_name(
    name: str, relation: dict[str, Any], message: str | None
) -> None:
    raw = _catalog_with(name, [relation])
    if message is None:
        DidMap.model_validate(raw)
        return
    with pytest.raises(ValidationError, match=message.replace("(", r"\(")):
        DidMap.model_validate(raw)


def test_a_relation_reading_past_the_ceiling_is_refused() -> None:
    """fuel_target is 57 bytes; verifying the limit against the period word at
    its far end needs one more byte for the neighbour."""
    raw = _catalog_with(
        "ECM_KNOCK_RETARD_LIMIT",
        [
            {
                "kind": "memory",
                "op": "ordering",
                "read": "same_reply",
                "operands": ["ECM_ENGINE_PERIOD"],
                "compare": "le",
            }
        ],
    )
    raw["access"]["max_memory_read_length"] = 63
    DidMap.model_validate(raw)
    raw["access"]["max_memory_read_length"] = 57  # the window itself still fits
    with pytest.raises(ValidationError, match="ECM_KNOCK_RETARD_LIMIT: relation memory/ordering"):
        DidMap.model_validate(raw)


@pytest.mark.parametrize(
    ("relation", "message"),
    [
        (
            {"kind": "memory", "op": "trunc_divide", "read": "cross_read", "operands": ["A"]},
            "divisor is required",
        ),
        (
            {
                "kind": "memory",
                "op": "trunc_divide",
                "read": "cross_read",
                "operands": ["A"],
                "divisor": 256,
            },
            "must state min_agreement",
        ),
        (
            {"kind": "memory", "op": "ordering", "read": "same_reply", "operands": ["A"]},
            "compare is required",
        ),
        (
            {"kind": "memory", "op": "sum_of_halves", "read": "same_reply", "operands": ["A"]},
            "takes 2 operand",
        ),
        ({"kind": "invariant", "mask": 0xC0, "min": 0}, "bit test .* or a range"),
        ({"kind": "invariant", "mask": 0x0F, "equals": 0x10}, "outside mask"),
        ({"kind": "invariant", "min": 5, "max": 1}, "empty range"),
        (
            {"kind": "mode01_pid", "pid": 0x24, "pid_byte_length": 2, "scale": 4.46},
            "relative_tolerance",
        ),
        (
            {
                "kind": "mode01_pid",
                "pid": 0x24,
                "pid_byte_length": 2,
                "scale": 4.46,
                "relative_tolerance": 0.03,
            },
            "min_agreement",
        ),
        ({"kind": "equals_constant", "value": 7350}, "kind"),
    ],
)
def test_a_relation_missing_its_parameters_is_refused(
    relation: dict[str, Any], message: str
) -> None:
    base = BY_NAME["ECM_KNOCK_LEARN"].model_dump(mode="json", exclude_none=True)
    base["relations"] = [relation]
    with pytest.raises(ValidationError, match=message):
        MemoryChannel.model_validate(base)


def test_a_relation_on_its_own_channel_is_refused() -> None:
    base = BY_NAME["ECM_KNOCK_LEARN"].model_dump(mode="json", exclude_none=True)
    base["relations"] = [
        {
            "kind": "memory",
            "op": "trunc_divide",
            "read": "same_reply",
            "operands": ["ECM_KNOCK_LEARN"],
            "divisor": 2,
        }  # noqa: E501
    ]
    with pytest.raises(ValidationError, match="names its own channel"):
        MemoryChannel.model_validate(base)
    base["relations"] = [
        {
            "kind": "flag_equivalence",
            "when": [{"channel": "ECM_KS_FAULT_FLAGS", "mask": 0x80}],
            "then": [{"channel": "ECM_KS_P0327_STATUS", "mask": 1}],
            "min_agreement": 1.0,
        }
    ]
    with pytest.raises(ValidationError, match="never reads ECM_KNOCK_LEARN"):
        MemoryChannel.model_validate(base)


# ---------------------------------------------------------------------------
# Stored ram-study slices (2026-10-04)

FUEL_COMP = REPO / "tests/fixtures/fuel_comp/_capture-ram-study-2026-10-04T204557"
ROM_TRACE = REPO / "tests/fixtures/rom_trace/_capture-ram-study-2026-10-04T204557"
#: Where the stored windows started: spark at the timing copy 0xFFFF11C7 (19
#: bytes to the engine-speed word), fuel_target at the period word 0xFFFF26AE
#: (18 bytes then, before the knock bytes widened it).
SPARK_START = 0xFFFF11C7
FUEL_TARGET_START = 0xFFFF26AE


def _rows(session: Path, sql_where: str) -> list[tuple[Any, ...]]:
    parquet = session / "telemetry-00000.parquet"
    return (
        duckdb.connect()
        .execute(
            f"select epoch(timestamp), payload, value_number from '{parquet}' "
            f"where {sql_where} order by 1"
        )
        .fetchall()
    )


def _with(name: str, relation: Any) -> dict[str, MemoryChannel]:
    by_name = dict(BY_NAME)
    by_name[name] = BY_NAME[name].model_copy(update={"relations": [relation]})
    return by_name


def test_same_reply_trunc_divide_holds_on_the_stored_spark_window() -> None:
    """The 50-rpm map trace is trunc(engine-speed word / 4) on all 180 stored
    replies (12.5 x 4 = 50 rpm), and fails at the trace's neighbours."""
    rows = _rows(ROM_TRACE, "channel = 'mem:ECM_ENGINE_SPEED_RAM'")
    speed = BY_NAME["ECM_ENGINE_SPEED_RAM"]
    samples = [RelationSample({SPARK_START: bytes(payload)}) for _t, payload, _v in rows]
    # the stored payload is aligned where this test says it starts
    for sample, (_t, _payload, value) in zip(samples, rows, strict=True):
        raw = speed.raw_from(sample.replies[SPARK_START][speed.address - SPARK_START :])
        assert raw is not None and raw * 12.5 == value
    relation = MemoryArithmeticRelation(
        kind="memory",
        op="trunc_divide",
        read="same_reply",
        operands=["ECM_ENGINE_SPEED_RAM"],
        divisor=4,
    )
    by_name = _with("ECM_CAM_MAP_RPM_TRACE", relation)
    owner = by_name["ECM_CAM_MAP_RPM_TRACE"]

    trace = evaluate_relation(owner, owner, relation, by_name, samples)
    assert trace.outcome == "pass", trace.reason
    assert trace.agreements == trace.samples == 180
    assert all(n.samples == 180 and n.agreements < 18 for n in trace.neighbours)

    # The speed word is an operand: 0x11D6 is in the stored reply and fails;
    # 0x11DA was never read, so the word stays unverifiable on this data.
    word = evaluate_relation(speed, owner, relation, by_name, samples)
    below, above = word.neighbours
    assert below.samples == 180 and below.agreements < 18
    assert above.samples == 0
    assert word.outcome == "unverifiable" and "were not read" in word.reason


def test_period_word_against_mode01_rpm_across_the_stored_session_exits() -> None:
    """Each fuel_target read paired with the next Mode 01 rpm answered after
    the session exit (a median ~0.6 s later on this drive). As a per-sample
    ratio it holds on most pairs; the word beside it never does."""
    memory = _rows(FUEL_COMP, "channel like 'mem:ECM_TARGET_EQUIV_RATIO%'")
    rpm = _rows(FUEL_COMP, "channel = 'RPM'")
    samples = []
    for when, payload, _value in memory:
        after = next((row for row in rpm if row[0] > when), None)
        if after is not None:
            samples.append(
                RelationSample({FUEL_TARGET_START: bytes(payload)}, pid_payload=bytes(after[1]))
            )
    assert len(samples) >= 150
    relation = Mode01PidRelation(
        kind="mode01_pid",
        pid=0x0C,
        pid_byte_length=2,
        form="reciprocal",
        scale=25_000_000,  # PID 0x0C = rpm x 4, rpm = 6,250,000 / word
        relative_tolerance=0.05,
        min_agreement=0.80,
    )
    by_name = _with("ECM_ENGINE_PERIOD", relation)
    owner = by_name["ECM_ENGINE_PERIOD"]

    result = evaluate_relation(owner, owner, relation, by_name, samples)
    assert result.agreements >= 0.85 * result.samples
    below, above = result.neighbours
    assert above.samples == result.samples and above.agreements == 0
    # The stored window started at the word itself, so 0x26AC was not read.
    assert below.samples == 0 and result.outcome == "unverifiable"

    tight = relation.model_copy(update={"relative_tolerance": 0.005})
    assert (
        evaluate_relation(owner, owner, tight, _with("ECM_ENGINE_PERIOD", tight), samples).outcome
        == "fail"
    )  # noqa: E501


# ---------------------------------------------------------------------------
# Adversarial cases (D03.38 verification, 2026-10-08): a wrong or unexercised
# address must not pass. Each was a pass before the exercise rules.


def _const(name: str, raw: int, neighbour: int) -> Image:
    image: Image = {}
    _put(image, name, raw)
    c = BY_NAME[name]
    for offset in range(1, c.byte_length + 1):
        image[c.address - offset] = neighbour
        image[c.address + c.byte_length - 1 + offset] = neighbour
    return image


@pytest.mark.parametrize(
    ("name", "raw", "neighbour"),
    [
        ("ECM_KNOCK_WINDOW_INDEX", 0, 0xFF),  # in 0..6, neighbours far outside
        ("ECM_KNOCK_FLAGS_BY_INDEX", 0, 0xFF),  # & 0xC0 == 0 on a zero byte
        ("ECM_AF_LEARNED_OFFSET_B1", 0, 0x7F),  # 0 inside +-500, 0x7F7F outside
        ("ECM_AF_LEARNED_OFFSET_B1", -37, 0x7F),  # a non-zero constant is still one
    ],
)
def test_a_constant_inside_an_invariant_is_not_evidence(
    name: str, raw: int, neighbour: int
) -> None:
    verdict = _verify(_RamEcu([_const(name, raw, neighbour)]), name)
    assert verdict.outcome == "unverifiable", verdict.reason
    assert "constant" in verdict.reason


def test_a_constant_zero_retard_with_a_zero_copy_is_not_evidence() -> None:
    image = _const("ECM_KNOCK_RETARD", 0, 0x10)
    _put(image, "ECM_KNOCK_RETARD_DEG", 0)
    verdict = _verify(_RamEcu([image]), "ECM_KNOCK_RETARD")
    assert verdict.outcome == "unverifiable"
    assert {c.relation for c in verdict.checks} == {"invariant", "memory/trunc_divide"}


def test_a_small_retard_truncating_to_zero_is_not_evidence() -> None:
    """retard -100 truncates to 0 and "matches" a copy that reads 0: the
    compared result is zero, whatever the subject word is."""
    images = []
    for retard in (-100, -60, -200):  # distinct, so the invariant can pass
        image = _const("ECM_KNOCK_RETARD", retard, 0x40)
        _put(image, "ECM_KNOCK_RETARD_DEG", 0)
        images.append(image)
    verdict = _verify(_RamEcu(images), "ECM_KNOCK_RETARD")
    divide = next(c for c in verdict.checks if c.relation == "memory/trunc_divide")
    assert divide.outcome == "unverifiable"
    assert "zero matching zero" in divide.reason
    deg = _verify(_RamEcu(images), "ECM_KNOCK_RETARD_DEG")
    assert deg.outcome == "unverifiable"


def test_a_positive_retard_word_fails_its_invariant() -> None:
    image = _const("ECM_KNOCK_RETARD", 37, 0x40)
    _put(image, "ECM_KNOCK_RETARD_DEG", 0)
    assert _verify(_RamEcu([image]), "ECM_KNOCK_RETARD").outcome == "fail"


def test_a_wrong_low_half_summing_to_zero_is_not_evidence() -> None:
    """LO=1, HI=0, combined 0: trunc(1/2) + 0 = 0 agrees, on zero."""
    image: Image = {}
    _put(image, "ECM_KNOCK_LEARN_LO_RPM", 1)
    _put(image, "ECM_KNOCK_LEARN_HI_RPM", 0)
    _put(image, "ECM_KNOCK_LEARN", 0)
    lo = BY_NAME["ECM_KNOCK_LEARN_LO_RPM"]
    image[lo.address - 2] = 0x12
    image[lo.address - 1] = 0x34
    assert _verify(_RamEcu([image]), "ECM_KNOCK_LEARN_LO_RPM").outcome == "unverifiable"


def test_a_fault_flag_that_never_set_is_not_evidence() -> None:
    """false <=> false, with neighbours that have bit 7 set and so fail."""
    image: Image = {}
    for name in ("ECM_KS_P0327_STATUS", "ECM_KS_P0328_STATUS"):
        c = BY_NAME[name]
        for a in range(c.address, c.address + c.length):
            image[a] = 0
    for name in ("ECM_KS_P0332_STATUS", "ECM_KS_P0333_STATUS"):
        c = BY_NAME[name]
        for a in range(c.address, c.address + c.length):
            image[a] = 0
    _put(image, "ECM_KS_FAULT_FLAGS", 0)
    _put(image, "ECM_KNOCK_RETARD_TARGET", 0)
    flags = BY_NAME["ECM_KS_FAULT_FLAGS"]
    image[flags.address - 1] = 0x80
    image[flags.address + 1] = 0x80
    verdict = _verify(_RamEcu([image]), "ECM_KS_FAULT_FLAGS")
    assert verdict.outcome == "unverifiable", verdict.reason
    assert all("never true" in c.reason for c in verdict.checks)


def test_an_iff_whose_flag_never_cleared_is_not_evidence() -> None:
    """The fallback engaged on every sample: true <=> true, never false."""
    verdict = _verify(_RamEcu([_fallback(True)]), "ECM_KNOCK_RETARD_TARGET")
    assert verdict.outcome == "unverifiable"
    assert "never false" in verdict.reason


# ---------------------------------------------------------------------------
# The bus, not the address


def test_a_refused_session_exit_is_unverifiable_and_named() -> None:
    ecu = _RamEcu([_lambda(b1, b2) for b1, b2 in BANKS], _pid_from_word)
    real = ecu.enter_diagnostic_session

    def refuse_exit(session: int, *, request_id: int | None = None) -> bool:
        real(session, request_id=request_id)
        return session != 0x81

    ecu.enter_diagnostic_session = refuse_exit  # type: ignore[method-assign]
    verdict = _verify(ecu, "ECM_AF_LAMBDA_B1", exit_session=0x81, sleep=lambda _s: None)
    assert verdict.outcome == "unverifiable"
    assert "session_exit_refused" in verdict.reason
    assert not any(kind == "mode01" for kind, _ in ecu.log)


def test_a_pid_that_never_answers_is_unverifiable_and_named() -> None:
    ecu = _RamEcu([_lambda(b1, b2) for b1, b2 in BANKS], lambda _image, _pid: None)
    verdict = _verify(ecu, "ECM_AF_LAMBDA_B1", exit_session=0x81, sleep=lambda _s: None)
    assert verdict.outcome == "unverifiable"
    assert "pid_unanswered" in verdict.reason


def test_a_refused_reentry_is_unverifiable_and_named() -> None:
    ecu = _RamEcu([_lambda(b1, b2) for b1, b2 in BANKS], _pid_from_word)
    calls = {"n": 0}

    def refuse_second_entry(session: int, *, request_id: int | None = None) -> bool:
        calls["n"] += int(session == 0xC0)
        return not (session == 0xC0 and calls["n"] > 1)

    ecu.enter_diagnostic_session = refuse_second_entry  # type: ignore[method-assign]
    verdict = _verify(ecu, "ECM_AF_LAMBDA_B1", exit_session=0x81, sleep=lambda _s: None)
    assert verdict.outcome == "unverifiable"
    assert "session_reentry_refused" in verdict.reason


# ---------------------------------------------------------------------------
# The dashboard route takes the ECM session, or refuses by name


def _route_config(tmp_path: Path) -> Any:

    from src.config import CanaryConfig
    from src.vehicle.profiles import resolve_profile_for_tooling, scope_config

    raw = load_yaml((REPO / "canary.example.yaml").read_text())
    raw.setdefault("collection", {})["storage_path"] = str(tmp_path / "sessions")
    raw.setdefault("daemon", {})["jobs_dir"] = str(tmp_path / "control" / "jobs")
    raw["daemon"]["afr_calibration"] = str(tmp_path / "data" / "afr_calibration.yaml")
    raw.setdefault("profiles", {})["registry_path"] = str(tmp_path / "profiles.yaml")
    raw["profiles"]["registry_overlay_path"] = str(tmp_path / "profiles.overlay.yaml")
    base = CanaryConfig.model_validate(raw)
    return scope_config(base, resolve_profile_for_tooling(base, None))


@pytest.fixture
def verify_app(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Any:
    from fastapi import FastAPI
    from fastapi.testclient import TestClient

    from src.collection import tx_gate
    from src.dashboard.routes import calibration as routes

    seen: dict[str, Any] = {"owners": [], "holders": [], "shutdown": 0}

    class _Bus:
        def shutdown(self) -> None:
            seen["shutdown"] += 1

        def recv(self, timeout: float | None = None) -> None:
            return None

    class _Client:
        def __init__(self, bus: Any, **kwargs: Any) -> None:
            seen["holders"].append(kwargs.get("tx_holder"))

        def enter_diagnostic_session(self, *_a: Any, **_k: Any) -> bool:
            return True

    def fake_verify(_client: Any, channel: MemoryChannel, **_kwargs: Any) -> ChannelVerdict:
        seen["owners"].append(tx_gate.ecm_session_owner())
        return ChannelVerdict(channel.name, "unverifiable", "test")

    monkeypatch.setattr(routes, "VERIFY_SETTLE_SECONDS", 0.0)
    monkeypatch.setattr(routes, "bus_factory", lambda _cfg: _Bus())
    monkeypatch.setattr(routes, "CanObdClient", _Client)
    monkeypatch.setattr(routes, "verify_channel", fake_verify)
    tx_gate.reset_ecm_session_claims()
    app = FastAPI()
    routes.register_calibration_routes(
        app, config=_route_config(tmp_path), dids_path=DIDS_YAML, resolved_control_token="t"
    )
    yield TestClient(app, headers={"Authorization": "Bearer t"}), seen
    tx_gate.reset_ecm_session_claims()


def test_the_verify_route_claims_the_ecm_session_and_releases_it(verify_app: Any) -> None:
    from src.collection import tx_gate

    client, seen = verify_app
    response = client.post(
        "/control/memory-channels/verify", json={"channels": ["ECM_KNOCK_LEARN"], "samples": 1}
    )
    assert response.status_code == 200, response.text
    assert seen["owners"] == ["memory_verify"]  # the poll loop is paused while it reads
    assert seen["holders"] == ["memory_verify"]  # and its frames go out as that holder
    assert seen["shutdown"] == 1
    assert tx_gate.ecm_session_owner() == "can_diagnostics"


def test_the_verify_route_refuses_by_name_while_another_holder_owns_the_ecm(
    verify_app: Any,
) -> None:
    from src.collection import tx_gate

    client, seen = verify_app
    tx_gate.claim_ecm_session("rom_backup")
    response = client.post("/control/memory-channels/verify", json={"channels": []})
    assert response.status_code == 409
    assert "'rom_backup'" in response.json()["detail"]
    assert seen["owners"] == [] and seen["shutdown"] == 0
    assert tx_gate.claim_held("rom_backup") and not tx_gate.claim_held("memory_verify")


def test_the_verify_route_refuses_while_passive_is_armed(
    verify_app: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    from src.collection import tx_gate

    client, seen = verify_app
    monkeypatch.setattr(tx_gate, "passive_armed", lambda: True)
    response = client.post("/control/memory-channels/verify", json={"channels": []})
    assert response.status_code == 409
    assert "passive mode is armed" in response.json()["detail"]
    assert seen["shutdown"] == 0


def test_a_foreign_claim_mid_verify_stops_it_and_records_no_fail(
    verify_app: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A tester takes the ECM halfway through a channel. The verify stops at
    the next sample with the claimant named, records nothing (so no correct
    channel is marked moved), and releases its claim."""
    from src.analysis import memory_verification
    from src.collection import tx_gate
    from src.dashboard.routes import calibration as routes

    client, _seen = verify_app
    ecu = _RamEcu([_learned(lo, hi) for lo, hi in LEARNED])
    real_read = ecu.query_raw

    def read_then_lose_the_ecm(service: int, body: bytes, **kwargs: Any) -> _Reply:
        if len([e for e in ecu.log if e[0] == "read"]) == 3:
            tx_gate.claim_ecm_session("foreign", ttl_seconds=60.0)
        return real_read(service, body, **kwargs)

    ecu.query_raw = read_then_lose_the_ecm  # type: ignore[method-assign]
    monkeypatch.setattr(routes, "CanObdClient", lambda *_a, **_k: ecu)
    monkeypatch.setattr(routes, "verify_channel", memory_verification.verify_channel)
    recorded: list[Any] = []
    monkeypatch.setattr(routes, "record_verify_results", lambda *a: recorded.append(a))

    response = client.post(
        "/control/memory-channels/verify", json={"channels": ["ECM_KNOCK_LEARN"], "samples": 6}
    )
    assert response.status_code == 409
    assert "'foreign'" in response.json()["detail"]
    assert recorded == []
    assert not tx_gate.claim_held("memory_verify")
    # The claim landed during the 4th sample's read; the 5th never started.
    assert len([e for e in ecu.log if e[0] == "read"]) == 4


def test_the_claim_is_renewed_every_sample_so_a_long_channel_cannot_outlive_it(
    verify_app: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    from src.analysis import memory_verification
    from src.collection import tx_gate
    from src.dashboard.routes import calibration as routes

    client, _seen = verify_app
    ecu = _RamEcu([_learned(lo, hi) for lo, hi in LEARNED])
    renewals: list[float | None] = []
    real_claim = tx_gate.claim_ecm_session

    def counting_claim(holder: Any, **kwargs: Any) -> Any:
        if holder == "memory_verify":
            renewals.append(kwargs.get("ttl_seconds"))
        return real_claim(holder, **kwargs)

    monkeypatch.setattr(tx_gate, "claim_ecm_session", counting_claim)
    monkeypatch.setattr(routes, "CanObdClient", lambda *_a, **_k: ecu)
    monkeypatch.setattr(routes, "verify_channel", memory_verification.verify_channel)
    response = client.post(
        "/control/memory-channels/verify", json={"channels": ["ECM_KNOCK_LEARN"], "samples": 6}
    )
    assert response.status_code == 200, response.text
    assert response.json()["verdicts"][0]["outcome"] == "pass"
    assert len(renewals) == 1 + 6  # the claim, then once per sample
    assert not tx_gate.claim_held("memory_verify")


def test_session_entry_is_retried_once_then_refused_by_name(
    verify_app: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    from src.dashboard.routes import calibration as routes

    client, seen = verify_app
    answers: list[bool] = []

    class _Refusing:
        def __init__(self, *_a: Any, **_k: Any) -> None:
            pass

        def enter_diagnostic_session(self, session: int, **_k: Any) -> bool:
            answers.append(session == 0xC0)
            return False

    monkeypatch.setattr(routes, "CanObdClient", _Refusing)
    response = client.post("/control/memory-channels/verify", json={"channels": []})
    assert response.status_code == 503
    assert "session_entry_refused" in response.json()["detail"]
    assert answers.count(True) == 2  # the entry and its one retry
    assert seen["owners"] == []


def test_two_verifies_cannot_run_at_once(verify_app: Any) -> None:
    from src.collection import tx_gate
    from src.dashboard.routes import calibration as routes

    client, seen = verify_app
    assert routes._VERIFY_LOCK.acquire(blocking=False)
    try:
        response = client.post("/control/memory-channels/verify", json={"channels": []})
    finally:
        routes._VERIFY_LOCK.release()
    assert response.status_code == 409
    assert "already running" in response.json()["detail"]

    tx_gate.claim_ecm_session("memory_verify", ttl_seconds=60.0)
    response = client.post("/control/memory-channels/verify", json={"channels": []})
    assert response.status_code == 409
    assert "another verification" in response.json()["detail"]
    assert seen["owners"] == []
