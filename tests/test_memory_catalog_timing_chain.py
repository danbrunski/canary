"""The ignition timing chain's RAM channels (E05.4 (b)/(c), E09.1).

The words the seven timing nodes of ``ecu_application_model.yaml`` read and
write, catalogued so a twin capture can locate the earliest disagreement from
the trim lookup to the timing byte. Width and signedness come from the
manifest's ``ram`` entries, and a word the manifest keeps in raw counts carries
no scale: these tests fail when the catalog and the manifest disagree, when a
window passes the ECM's 63-byte ceiling, or when a channel enters a profile
before a live read (the rule in ``tests/test_memory_catalog_knock_lambda.py``).
"""

from __future__ import annotations

from pathlib import Path

import pytest
from pydantic import ValidationError

from src.analysis.ecu_application import load_application_model
from src.decode.dids import (
    apply_memory_scaling,
    check_known_answer,
    decode_memory_raw,
    decode_memory_window,
    load_did_map,
    plan_memory_reads,
)
from src.decode.memory_relations import (
    InvariantRelation,
    MemoryArithmeticRelation,
    MemoryRelation,
)
from src.paths import DIDS_YAML
from tests.test_memory_catalog_knock_lambda import LIVE_READ_METHODS

REPO = Path(__file__).resolve().parents[1]
PROFILE_DIR = REPO / "data" / "nissan-xterra-2008"
CATALOG = load_did_map(DIDS_YAML)
BY_NAME = {c.name: c for c in CATALOG.memory}

#: name -> (address, byte_length, signed, scale, window). ``scale`` None: the
#: manifest keeps the word in raw counts (or a flag-carrying byte).
PINNED: dict[str, tuple[int, int, bool, float | None, str]] = {
    "ECM_IGNITION_TIMING_BYTE": (0xFFFF11FF, 1, False, -1.0, "timing_byte"),
    "ECM_TIMING_OTHER_RETARD": (0xFFFF1215, 1, True, 1.0, "timing_byte"),
    "ECM_TIMING_TRIM_SUBTRAHEND": (0xFFFF11CB, 1, False, None, "trim_subtrahend"),
    "ECM_TIMING_TERM_1344": (0xFFFF1344, 1, False, None, "timing_terms"),
    "ECM_TIMING_PRELIMIT": (0xFFFF1346, 1, True, 1.0, "timing_terms"),
    "ECM_TIMING_ENRICH_RETARD": (0xFFFF174A, 1, False, None, "timing_trim"),
    "ECM_TIMING_TRIM_SELECTED": (0xFFFF1777, 1, False, None, "timing_trim"),
    "ECM_TIMING_TRIM_A": (0xFFFF177A, 1, False, None, "timing_trim"),
    "ECM_BASE_TIMING_SUM": (0xFFFF1A58, 1, True, 1.0, "base_timing"),
    "ECM_TIMING_TERM_1D3E": (0xFFFF1D3E, 1, False, None, "valve_timing_term"),
    "ECM_TIMING_TRIM_TERM": (0xFFFF2793, 1, False, None, "timing_trim_term"),
    "ECM_TIMING_TERM_279A": (0xFFFF279A, 1, False, None, "timing_trim_term"),
    "ECM_TIMING_TERM_27C6": (0xFFFF27C6, 1, True, 1.0, "base_timing_term"),
    "ECM_BASE_TIMING": (0xFFFF27DA, 1, True, 1.0, "base_timing_term"),
    # E09.7: the base timing term's inputs (nodes base_timing_rate_word ..
    # timing_term_27c6), catalogued so a one-off read can check the formula.
    "ECM_TIMING_RATE_PRODUCT": (0xFFFF15DA, 1, False, None, "timing_rate"),
    "ECM_TIMING_RATE_MAP": (0xFFFF15DB, 1, False, None, "timing_rate"),
    "ECM_TIMING_RATE_WORD": (0xFFFF15DE, 2, False, None, "timing_rate"),
    "ECM_TIMING_CHARGE_TERM": (0xFFFF17C6, 2, False, None, "timing_charge_term"),
    "ECM_TIMING_LOAD_CURVE": (0xFFFF19CB, 1, False, None, "timing_load_curve"),
    "ECM_TIMING_CHARGE_WORD": (0xFFFF1B24, 2, False, None, "timing_charge_word"),
    "ECM_TIMING_CHARGE_FACTOR": (0xFFFF10EB, 1, False, None, "timing_charge_factor"),
}

#: window -> (first address, read length) with every member selected.
WINDOWS = {
    "timing_byte": (0xFFFF11FF, 23),
    "trim_subtrahend": (0xFFFF11CB, 1),
    "timing_terms": (0xFFFF1344, 3),
    "timing_trim": (0xFFFF174A, 49),
    "base_timing": (0xFFFF1A58, 1),
    "valve_timing_term": (0xFFFF1D3E, 1),
    "timing_trim_term": (0xFFFF2793, 8),
    "base_timing_term": (0xFFFF27C6, 21),
    "timing_rate": (0xFFFF15DA, 6),
    "timing_charge_term": (0xFFFF17C6, 2),
    "timing_load_curve": (0xFFFF19CB, 1),
    "timing_charge_word": (0xFFFF1B24, 2),
    "timing_charge_factor": (0xFFFF10EB, 1),
}

#: How each channel can be checked, as the catalog declares it. Everything
#: else is checked only by the twin's chain nodes on a capture: the relation
#: schema has no sum, mask or two-channel equality.
CHECKS = {
    "ECM_IGNITION_TIMING_BYTE": "known_answer equals_did 0x110A",
    "ECM_TIMING_OTHER_RETARD": "memory ordering (declared on ECM_KNOCK_RETARD_APPLIED)",
    "ECM_TIMING_TRIM_SUBTRAHEND": "invariant max 7",
    "ECM_TIMING_TRIM_TERM": "memory ordering <= ECM_TIMING_TRIM_SELECTED & 0x7F",
    "ECM_TIMING_TRIM_SELECTED": "memory ordering (declared on ECM_TIMING_TRIM_TERM)",
    "ECM_TIMING_RATE_PRODUCT": "memory ordering <= ECM_TIMING_RATE_MAP, same reply",
    "ECM_TIMING_RATE_MAP": "memory ordering (declared on ECM_TIMING_RATE_PRODUCT)",
}


@pytest.mark.parametrize("name", sorted(PINNED))
def test_address_width_sign_scale_and_window_are_as_traced(name: str) -> None:
    channel = BY_NAME[name]
    address, width, signed, scale, window = PINNED[name]
    assert (channel.address, channel.byte_length, channel.signed) == (address, width, signed)
    assert (channel.scale, channel.window) == (scale, window)


def test_every_window_is_contiguous_and_under_the_ceiling() -> None:
    reads = {r.name: r for r in plan_memory_reads(CATALOG.memory)}
    ceiling = CATALOG.access.max_memory_read_length
    assert ceiling is not None
    for name, (start, length) in WINDOWS.items():
        assert (reads[name].address, reads[name].length) == (start, length), name
        assert length <= ceiling


def test_the_windows_come_after_the_fill_byte_in_catalog_order() -> None:
    """A window read before fuel_comp_x moves the fill byte a request later
    (E07.4's 0.1 s pairing): every timing window is planned after it."""
    order = [r.name for r in plan_memory_reads(CATALOG.memory)]
    fill = order.index("fuel_comp_x")
    assert all(order.index(window) > fill for window in WINDOWS)


def test_decode_follows_the_manifest_ram_entries() -> None:
    """Width and signedness agree with the manifest's ``ram`` entry at the
    same address, for every channel the model observes; a raw-count word has
    no scale."""
    model, _ = load_application_model(PROFILE_DIR)
    ram = {v.address: v for v in model.ram}
    observed = {o.spec.split(":", 1)[1] for n in model.nodes for o in n.observed_as}
    observed |= {o.spec.split(":", 1)[1] for out in model.outputs for o in out.observed_as}
    for name in sorted(set(PINNED) | {"ECM_KNOCK_RETARD_APPLIED", "ECM_IGNITION_TIMING_FINAL"}):
        channel = BY_NAME[name]
        entry = ram[channel.address]
        assert name in observed, name
        assert (channel.byte_length * 8, channel.signed) == (entry.width, entry.signed), name
        if entry.units == "raw_counts" and name != "ECM_IGNITION_TIMING_BYTE":
            assert channel.scale is None, name


#: The timing_trim window was read on the truck on 2026-11-11 (KOEO, an engine
#: start and three full-pedal stabs in neutral). name -> confidence after it.
#: The enrichment retard read 0 throughout, beside an input byte that
#: reproduced its table, so it left ``hypothesized`` at ``low`` and may be
#: polled; the two trim bytes moved but were not checked against their tables
#: (X is in another window), so they carry the read and stay hypothesized.
READ_LIVE = {
    "ECM_TIMING_ENRICH_RETARD": "low",
    "ECM_TIMING_TRIM_SELECTED": "hypothesized",
    "ECM_TIMING_TRIM_A": "hypothesized",
}


def test_only_the_timing_trim_window_was_read_live() -> None:
    """Every other channel is code-verified from the listing and never read;
    0xFFFF11CB's constant 0 inside the spark reply on the ram-study is
    recorded under a method that is not a live read, because a constant
    cannot identify a byte."""
    for name in PINNED:
        channel = BY_NAME[name]
        assert any(e.method == "disassembly" for e in channel.evidence), name
        live = any(e.method in LIVE_READ_METHODS for e in channel.evidence)
        assert live is (name in READ_LIVE), name
        assert channel.confidence == READ_LIVE.get(name, "hypothesized"), name


def test_the_enrichment_retard_was_read_with_its_input_and_its_cap() -> None:
    """The three bytes a profile polls for the enrichment retard share one
    3-byte read, and the input byte decodes as phi (raw / 128)."""
    names = ("ECM_TIMING_ENRICH_RETARD", "ECM_TARGET_LOOKUP_GATED", "ECM_TIMING_ENRICH_RETARD_CAP")
    (read,) = plan_memory_reads([BY_NAME[n] for n in names])
    assert (read.name, read.address, read.length) == ("timing_trim", 0xFFFF174A, 3)
    for name in names:
        channel = BY_NAME[name]
        assert channel.confidence != "hypothesized", name
        assert any(e.method in LIVE_READ_METHODS for e in channel.evidence), name
    lookup = BY_NAME["ECM_TARGET_LOOKUP_GATED"]
    for raw, phi in ((0x80, 1.0), (149, 149 / 128)):
        word = decode_memory_raw(lookup, bytes([raw]))
        assert word == raw
        assert apply_memory_scaling(lookup, word) == phi


def test_the_declared_checks_are_the_ones_the_window_plan_states() -> None:
    timing_byte = BY_NAME["ECM_IGNITION_TIMING_BYTE"]
    assert timing_byte.known_answer is not None
    assert (timing_byte.known_answer.did, timing_byte.known_answer.relation) == (
        0x110A,
        "equals_did",
    )
    relations = {(c.name, r.kind, *r.channels()) for c in CATALOG.memory for r in c.relations}
    assert ("ECM_KNOCK_RETARD_APPLIED", "memory", "ECM_TIMING_OTHER_RETARD") in relations
    assert ("ECM_TIMING_TRIM_TERM", "memory", "ECM_TIMING_TRIM_SELECTED") in relations
    assert ("ECM_TIMING_TRIM_SUBTRAHEND", "invariant") in relations
    assert ("ECM_TIMING_RATE_PRODUCT", "memory", "ECM_TIMING_RATE_MAP") in relations
    # No ordering against X: it would make the X trace checkable through an
    # unread channel (tests/test_verify_memory_channels.py).
    assert not any(name == "ECM_TIMING_LOAD_CURVE" for name, *_ in relations)
    checkable = {name for name in PINNED if BY_NAME[name].known_answer is not None}
    checkable |= {n for c in CATALOG.memory for r in c.relations for n in (c.name, *r.channels())}
    assert checkable & set(PINNED) == set(CHECKS)


def test_the_timing_byte_known_answer_is_the_did_byte_itself() -> None:
    channel = BY_NAME["ECM_IGNITION_TIMING_BYTE"]
    for did_raw in (80, 95, 110, 117):
        word = decode_memory_raw(channel, bytes([did_raw]))
        assert word is not None
        assert check_known_answer(channel, word, did_raw)
        assert not check_known_answer(channel, word, did_raw + 1)
        assert apply_memory_scaling(channel, word) == pytest.approx(110 - did_raw)


def test_one_timing_trim_reply_keeps_the_cell_flag_and_one_term_reply_its_sign() -> None:
    """The collector path's slices: a trim byte with bit 7 set stays the raw
    byte (the trim is raw & 0x7F), and a negative base timing decodes
    negative."""
    trim = next(r for r in plan_memory_reads(CATALOG.memory) if r.name == "timing_trim")
    payload = bytearray(trim.length)
    payload[0xFFFF1777 - trim.address] = 0x80 | 12
    payload[0xFFFF177A - trim.address] = 9
    raws = {
        c.name: decode_memory_window(c, bytes(payload), offset)
        for c, offset in zip(trim.channels, trim.offsets, strict=True)
    }
    assert raws == {
        "ECM_TIMING_ENRICH_RETARD": 0,
        "ECM_TARGET_LOOKUP_GATED": 0,
        "ECM_TIMING_ENRICH_RETARD_CAP": 0,
        "ECM_TIMING_TRIM_SELECTED": 0x8C,
        "ECM_TIMING_TRIM_A": 9,
    }
    term = next(r for r in plan_memory_reads(CATALOG.memory) if r.name == "base_timing_term")
    payload = bytearray(term.length)
    payload[0xFFFF27DA - term.address] = (-6) & 0xFF
    base = BY_NAME["ECM_BASE_TIMING"]
    raw = decode_memory_window(base, bytes(payload), 0xFFFF27DA - term.address)
    assert raw == -6 and apply_memory_scaling(base, raw) == -6.0


def _relation(owner: str, kind: str) -> MemoryRelation:
    return next(r for r in BY_NAME[owner].relations if r.kind == kind)


@pytest.mark.parametrize(
    ("w2332", "w1215", "holds"),
    [
        (1, 0, False),  # an advance with no retard to negate: not -1215
        (-3, 3, True),  # knock control off: -1215 exactly
        (-5, 3, True),  # knock retard deeper than -1215
        (4, -4, True),  # 1215 is s8: a negative retard gives a positive word
        (5, -4, False),
    ],
)
def test_the_retard_ordering_can_fail(w2332: int, w1215: int, holds: bool) -> None:
    relation = _relation("ECM_KNOCK_RETARD_APPLIED", "memory")
    assert isinstance(relation, MemoryArithmeticRelation)
    assert relation.holds(w2332, [w1215]) is holds


@pytest.mark.parametrize(
    ("w2793", "w1777", "holds"),
    [
        (50, 0x80 | 40, False),  # flagged cell: 50 > 40 once the flag is masked
        (40, 0x80 | 40, True),
        (12, 30, True),
        (31, 30, False),
    ],
)
def test_the_trim_term_ordering_masks_the_cell_flag(w2793: int, w1777: int, holds: bool) -> None:
    relation = _relation("ECM_TIMING_TRIM_TERM", "memory")
    assert isinstance(relation, MemoryArithmeticRelation)
    assert relation.operand_mask == 0x7F
    assert relation.holds(w2793, [w1777]) is holds
    # The bound the verifier counts as exercised is the masked trim.
    assert relation.witness(w2793, [w1777]) == w1777 & 0x7F
    # Unmasked, a flagged byte bounds any trim term: the vacuous form.
    unmasked = relation.model_copy(update={"operand_mask": None})
    assert unmasked.holds(w2793, [w1777]) is (w2793 <= w1777)


def test_operand_mask_belongs_to_ordering_only() -> None:
    with pytest.raises(ValidationError, match="operand_mask applies only to ordering"):
        MemoryArithmeticRelation(
            kind="memory",
            op="trunc_divide",
            read="same_reply",
            operands=["A"],
            divisor=256,
            operand_mask=0x7F,
        )


@pytest.mark.parametrize(("w11cb", "holds"), [(0, True), (7, True), (8, False), (255, False)])
def test_the_subtrahend_invariant_refuses_eight(w11cb: int, holds: bool) -> None:
    relation = _relation("ECM_TIMING_TRIM_SUBTRAHEND", "invariant")
    assert isinstance(relation, InvariantRelation)
    assert relation.holds(w11cb) is holds


@pytest.mark.parametrize(
    ("w15da", "w15db", "holds"),
    [
        (57, 57, True),  # 15D7 x 15DC = 0x4000: equal (the hot, phi 1 vectors)
        (14, 57, True),  # a smaller charge or coolant factor
        (58, 57, False),  # a product above its map term: wrong address
        (255, 0, False),
    ],
)
def test_the_rate_product_ordering_can_fail(w15da: int, w15db: int, holds: bool) -> None:
    relation = _relation("ECM_TIMING_RATE_PRODUCT", "memory")
    assert isinstance(relation, MemoryArithmeticRelation)
    assert relation.read == "same_reply" and relation.min_agreement == 1.0
    assert relation.holds(w15da, [w15db]) is holds


def test_the_base_timing_term_channels_enter_no_capture_profile() -> None:
    """Hypothesized until a one-off read checks the formula (the live-read
    rule in tests/test_memory_catalog_knock_lambda.py): no profile, saved or
    starter, names them."""
    added = {
        "ECM_TIMING_RATE_PRODUCT",
        "ECM_TIMING_RATE_MAP",
        "ECM_TIMING_RATE_WORD",
        "ECM_TIMING_CHARGE_TERM",
        "ECM_TIMING_LOAD_CURVE",
        "ECM_TIMING_CHARGE_WORD",
        "ECM_TIMING_CHARGE_FACTOR",
    }
    for path in (REPO / "data").rglob("capture_profiles*.yaml"):
        text = path.read_text()
        assert not {name for name in added if name in text}, path


def test_a_big_endian_word_decodes_as_the_sh2_stores_it() -> None:
    rate = next(r for r in plan_memory_reads(CATALOG.memory) if r.name == "timing_rate")
    payload = bytes([57, 57, 0, 0, 0x02, 0xD8])  # 15DE = 0x02D8 = 728
    raws = {
        c.name: decode_memory_window(c, payload, offset)
        for c, offset in zip(rate.channels, rate.offsets, strict=True)
    }
    assert raws == {
        "ECM_TIMING_RATE_PRODUCT": 57,
        "ECM_TIMING_RATE_MAP": 57,
        "ECM_TIMING_RATE_WORD": 728,
    }
