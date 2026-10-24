"""Re-verifying a RAM address against the running calibration."""

from __future__ import annotations

from collections.abc import Callable
from pathlib import Path
from typing import Any, cast

from src.analysis.memory_verification import MemoryVerificationClient
from src.decode.dids import MemoryChannel, MemoryKnownAnswer, is_memory_checkable, load_did_map
from tools.verify_memory_channels import (
    ChannelVerdict,
    plan_restamp,
    restamp,
    verify_channel,
)


def _client(ecu: object) -> MemoryVerificationClient:
    """A fake ECU as the verifier's protocol sees it (the fakes answer only
    the services their test exercises)."""
    return cast(MemoryVerificationClient, ecu)


class _Reply:
    def __init__(self, payload: bytes) -> None:
        self.payload = payload


def _filler(address: int, sample: int) -> int:
    """RAM nobody set: varies by address and sample, so a neighbour is never a
    constant that satisfies the relation by accident."""
    return ((address * 131) ^ (sample * 0x5D) ^ 0xA7) & 0xFF


class _FakeEcu:
    """Answers 0x23 with `words` and 0x22 through `did_for`.

    The verifier reads the word with both neighbouring slices (one word
    width either side); the word sits in the middle of the reply and the
    neighbours are filler that changes every sample.
    """

    def __init__(self, words: list[int], did_for: Callable[[int], int], width: int = 2) -> None:
        self._words = list(words)
        self._did_for = did_for
        self._width = width
        self.last_word = 0
        self.sample = 0
        self.sessions: list[int] = []

    def enter_diagnostic_session(self, subfunction: int, *, request_id: int | None = None) -> bool:
        self.sessions.append(subfunction)
        return True

    def query_raw(self, service: int, body: bytes, *, request_id: int | None = None) -> _Reply:
        assert service == 0x23
        address = int.from_bytes(body[:4], "big")
        length = int.from_bytes(body[4:], "big")
        assert length == 3 * self._width, "the known answer is read with its neighbours"
        self.sample += 1
        self.last_word = self._words.pop(0) if self._words else self.last_word
        before = bytes(_filler(address + i, self.sample) for i in range(self._width))
        after = bytes(
            _filler(address + 2 * self._width + i, self.sample) for i in range(self._width)
        )
        return _Reply(before + self.last_word.to_bytes(self._width, "big") + after)

    def query_mode_22(self, did: int, *, request_id: int | None = None) -> _Reply:
        return _Reply(bytes([self._did_for(self.last_word)]))


def _channel(**overrides: Any) -> MemoryChannel:
    base: dict[str, Any] = dict(
        name="ECM_TARGET_EQUIV_RATIO",
        address=0xFFFF26BE,
        length=2,
        byte_length=2,
        scale=1 / 16384,
        unit="phi",
        confidence="high",
        plausible_min=0.9,
        plausible_max=1.4,
        verified_tune_revision="ZS44A-k106",
        sentinel_raw=0,
        sentinel_meaning="fuel cut",
        known_answer=MemoryKnownAnswer(did=0x1134, relation="clamped_high_byte", floor=64),
    )
    base.update(overrides)
    return MemoryChannel(**base)


def _truthful(word: int) -> int:
    return max(64, word >> 8)


def test_address_that_still_holds_the_quantity_passes() -> None:
    ecu = _FakeEcu([0x4000, 0x4A80, 0x4380], _truthful)

    verdict = verify_channel(
        _client(ecu), _channel(), catalog=(), samples=3, request_id=0x7E0, session=0xC0
    )

    assert verdict.passed is True
    assert verdict.agreements == 3
    assert ecu.sessions == [0xC0]


def test_fuel_cut_still_satisfies_the_relation() -> None:
    """Word 0 and word 0x4000 both map to DID raw 64 — that is the clamp.
    The fuel-cut sample agrees; the exercised samples above it carry the pass."""
    ecu = _FakeEcu([0x0000, 0x4A80, 0x4380], _truthful)

    verdict = verify_channel(
        _client(ecu), _channel(), catalog=(), samples=3, request_id=0x7E0, session=0xC0
    )

    assert verdict.passed is True
    assert verdict.agreements == 3
    assert verdict.sentinels == 1


def test_agreement_only_at_the_clamp_floor_is_not_evidence() -> None:
    """Fuel cut and phi 1.0 both read DID 64: every word under the floor maps
    there, so agreement on it says nothing about the address."""
    ecu = _FakeEcu([0x0000, 0x4000, 0x0000, 0x4000], _truthful)

    verdict = verify_channel(
        _client(ecu), _channel(), catalog=(), samples=4, request_id=0x7E0, session=0xC0
    )

    assert verdict.outcome == "unverifiable"
    assert "did_constant" in verdict.reason


def test_moved_address_is_caught_even_while_decoding_plausibly() -> None:
    """The failure this exists for: still answers, still in range, wrong thing.

    0x4100 decodes to phi 1.0156, comfortably inside the plausible bounds. Only
    the relation to the cross-check DID reveals that the word is no longer the
    one 0x1134 is a view of.
    """
    dids = iter([70, 74, 79])
    ecu = _FakeEcu([0x4100, 0x4100, 0x4100], lambda _word: next(dids))

    verdict = verify_channel(
        _client(ecu), _channel(), catalog=(), samples=3, request_id=0x7E0, session=0xC0
    )

    assert verdict.outcome == "fail"
    assert "probably moved" in verdict.reason


def test_a_disagreement_with_a_constant_did_is_unverifiable_not_a_fail() -> None:
    """The same moved word, but the DID never moved: a fail must mean the bytes
    disagreed with a reference that said something (key-on 2026-10-10)."""
    ecu = _FakeEcu([0x4100, 0x4100], lambda _word: 70)

    verdict = verify_channel(
        _client(ecu), _channel(), catalog=(), samples=2, request_id=0x7E0, session=0xC0
    )

    assert verdict.outcome == "unverifiable"
    assert "did_constant" in verdict.reason


def test_channel_without_a_declared_check_is_skipped_not_failed() -> None:
    """A catalog gap must not block re-stamping every other channel."""
    verdict = verify_channel(
        _client(_FakeEcu([0x4000], _truthful)),
        _channel(known_answer=None),
        catalog=(),
        samples=1,
        request_id=0x7E0,
        session=0xC0,
    )

    assert verdict.outcome == "unverifiable"
    assert verdict.passed is False
    assert "plausibility alone" in verdict.reason


def test_refused_session_is_unverifiable_not_agreement_and_not_a_fail() -> None:
    """A refused session is the bus, not the address: it never reports
    agreement, and never records the channel as moved (D03.38 review)."""
    ecu = _FakeEcu([0x4000], _truthful)
    ecu.enter_diagnostic_session = lambda subfunction, *, request_id=None: False  # type: ignore[method-assign]

    verdict = verify_channel(
        _client(ecu), _channel(), catalog=(), samples=1, request_id=0x7E0, session=0xC0
    )

    assert verdict.passed is False
    assert verdict.outcome == "unverifiable"
    assert "session_entry_refused" in verdict.reason


def test_unanswered_reads_are_unverifiable_not_a_fail() -> None:
    """The gate suppressing our frames (another tester took the ECM) leaves
    every read empty: reads_unanswered, never "the address has moved"."""
    ecu = _FakeEcu([0x4000], _truthful)
    ecu.query_raw = lambda service, body, *, request_id=None: _Reply(b"")  # type: ignore[method-assign]

    verdict = verify_channel(
        _client(ecu), _channel(), catalog=(), samples=3, request_id=0x7E0, session=0xC0
    )

    assert verdict.outcome == "unverifiable"
    assert "reads_unanswered" in verdict.reason


class _TimingEcu:
    """0x23 answers one s8 timing byte between two neighbours; 0x22 answers
    DID 0x110A (110 - t)."""

    def __init__(self, timings: list[int], did_for: Callable[[int], int]) -> None:
        self._timings = list(timings)
        self._did_for = did_for
        self.last = 0
        self.sample = 0

    def enter_diagnostic_session(self, subfunction: int, *, request_id: int | None = None) -> bool:
        return True

    def query_raw(self, service: int, body: bytes, *, request_id: int | None = None) -> _Reply:
        assert service == 0x23 and body[-2:] == (3).to_bytes(2, "big")
        address = int.from_bytes(body[:4], "big")
        self.sample += 1
        self.last = self._timings.pop(0) if self._timings else self.last
        return _Reply(
            bytes([_filler(address, self.sample)])
            + self.last.to_bytes(1, "big", signed=True)
            + bytes([_filler(address + 2, self.sample)])
        )

    def query_mode_22(self, did: int, *, request_id: int | None = None) -> _Reply:
        assert did == 0x110A
        return _Reply(bytes([self._did_for(self.last)]))


_CATALOG_MEMORY = load_did_map(Path("data/dids.yaml")).memory


def _final_timing() -> MemoryChannel:
    return next(c for c in _CATALOG_MEMORY if c.name == "ECM_IGNITION_TIMING_FINAL")


def test_final_timing_passes_against_the_110_minus_did_encoding() -> None:
    """linear_did with did_offset: degrees = -1 x DID raw + 110, including
    the negative timing a signed byte exists for (-5 deg is 0xFB on the wire,
    DID raw 115)."""
    ecu = _TimingEcu([20, 35, -5], lambda t: 110 - t)

    verdict = verify_channel(
        _client(ecu),
        _final_timing(),
        catalog=_CATALOG_MEMORY,
        samples=3,
        request_id=0x7E0,
        session=0xC0,
    )

    assert verdict.passed is True
    assert verdict.agreements == 3


def test_final_timing_fails_when_the_byte_is_not_the_timing() -> None:
    """A byte off by one degree everywhere (a neighbouring register) fails,
    so the relation can fail."""
    ecu = _TimingEcu([20, 35, -5], lambda t: 110 - t + 1)

    verdict = verify_channel(
        _client(ecu),
        _final_timing(),
        catalog=_CATALOG_MEMORY,
        samples=3,
        request_id=0x7E0,
        session=0xC0,
    )

    assert verdict.outcome == "fail"


_CATALOG = Path("data/dids.yaml")


def _verdict(name: str, outcome: str) -> ChannelVerdict:
    return ChannelVerdict(name, outcome, "test")  # type: ignore[arg-type]


def _synthetic_verdicts(channels: list[MemoryChannel]) -> list[ChannelVerdict]:
    """Every checkable channel passes, the rest declare no check."""
    return [
        _verdict(c.name, "pass" if is_memory_checkable(c, channels) else "unverifiable")
        for c in channels
    ]


def test_a_window_is_stamped_whole_or_not_at_all() -> None:
    """The first version stamped only members that passed, which wrote a
    mixed-calibration window the loader rejects — a catalog the daemon could
    not load, produced by the tool meant to keep the catalog honest."""
    channels = load_did_map(_CATALOG).memory
    verdicts = _synthetic_verdicts(channels)

    stamps, notes = plan_restamp(channels, verdicts, carry_reason=None)

    # fuel_axis holds ECM_FUEL_TARGET_MAP_X_TRACE, which declares no check.
    assert "ECM_AIR_CHARGE_SLOT_103A" not in stamps
    assert "ECM_FUEL_TARGET_MAP_X_TRACE" not in stamps
    assert any("fuel_axis" in note and "cannot mix calibrations" in note for note in notes)
    # fuel_target's knock bytes have no DID, but since D03.38's relations
    # (trunc_divide on 0x26D2, ordering on 0x26E6) every member has a check,
    # so a passing read restamps the window without --carry-unverifiable.
    assert {
        "ECM_TARGET_EQUIV_RATIO",
        "ECM_ENGINE_PERIOD",
        "ECM_KNOCK_RETARD_DEG",
        "ECM_KNOCK_RETARD_LIMIT",
    } <= set(stamps)
    assert not any("fuel_target" in note for note in notes)
    # spark is fully checkable, so it goes through whole.
    assert {"ECM_IGNITION_TIMING_RAM", "ECM_ENGINE_SPEED_RAM", "ECM_CAM_MAP_RPM_TRACE"} <= set(
        stamps
    )
    assert all(reason is None for reason in stamps.values())


def test_carrying_an_unverifiable_channel_is_recorded_as_such() -> None:
    channels = load_did_map(_CATALOG).memory
    verdicts = _synthetic_verdicts(channels)

    stamps, _notes = plan_restamp(channels, verdicts, carry_reason="table-byte-only flash")

    assert stamps["ECM_FUEL_TARGET_MAP_X_TRACE"] == "table-byte-only flash"
    assert stamps["ECM_AIR_CHARGE_SLOT_103A"] is None  # it was actually verified


def test_a_failed_member_blocks_its_whole_window() -> None:
    channels = load_did_map(_CATALOG).memory
    verdicts = [
        _verdict(c.name, "fail" if c.name == "ECM_ENGINE_SPEED_RAM" else "pass")
        for c in channels
        if is_memory_checkable(c, channels)
    ] + [_verdict(c.name, "unverifiable") for c in channels if not is_memory_checkable(c, channels)]

    stamps, notes = plan_restamp(channels, verdicts, carry_reason="x")

    assert "ECM_IGNITION_TIMING_RAM" not in stamps  # same window as the failure
    assert "ECM_TARGET_EQUIV_RATIO" in stamps  # a different window is unaffected
    assert any("spark" in note and "failed" in note for note in notes)


def test_restamping_the_real_catalog_keeps_every_comment_and_still_loads(
    tmp_path: Path,
) -> None:
    """Rehearsal of the post-flash step on a copy of the shipped file.

    A yaml.safe_dump round-trip here would delete the reasoning this file
    carries in comments — the same loss the capture-profile catalogs took.
    """
    copy = tmp_path / "dids.yaml"
    copy.write_text(_CATALOG.read_text())
    comments_before = sum(1 for line in copy.read_text().splitlines() if "#" in line)
    channels = load_did_map(copy).memory
    verdicts = _synthetic_verdicts(channels)
    stamps, _notes = plan_restamp(channels, verdicts, carry_reason="table-byte-only flash")

    changed = restamp(copy, stamps, "ZS44A-20260920")

    # A block changes unless it already carries this revision and keeps no
    # carry reason (a checked channel located on this image, e.g. the
    # knock_chain's final-timing byte).
    unchanged = {
        c.name
        for c in channels
        if c.verified_tune_revision == "ZS44A-20260920"
        and c.revision_carried_reason is None
        and stamps[c.name] is None
    }
    # Since D03.38's relations that is every checkable channel catalogued on
    # this image: final timing (DID) and the relation-checked knock/lambda ones.
    assert unchanged == {
        c.name
        for c in channels
        if c.verified_tune_revision == "ZS44A-20260920"
        and c.revision_carried_reason is None
        and is_memory_checkable(c, channels)
    }
    assert {"ECM_IGNITION_TIMING_FINAL", "ECM_KNOCK_LEARN", "ECM_AF_LAMBDA_B1"} <= unchanged
    assert changed == len(channels) - len(unchanged)
    after = load_did_map(copy)  # must still load: no mixed windows
    assert {c.verified_tune_revision for c in after.memory} == {"ZS44A-20260920"}
    carried = {c.name for c in after.memory if c.revision_carried_reason}
    # Carried = no known answer to re-check: the two trace channels, the
    # firmware-located ones added 2026-10-04, and the knock / lambda-chain
    # channels added 2026-10-05 that no relation reads (all hypothesized
    # until a key-on read).
    assert carried == {c.name for c in channels if not is_memory_checkable(c, channels)}
    assert {
        "ECM_FUEL_TARGET_MAP_X_TRACE",
        "ECM_THROTTLE_MAP_X_TRACE",
        "ECM_UPREV_MAP_SELECTOR",
        "ECM_UPREV_MAGIC",
        "ECM_ACTIVE_MAP",
        "ECM_FUEL_COMP_X_INPUT",
        "ECM_PEDAL_ENRICH_FLAGS",
        "ECM_KNOCK_TRIM_DIFF_FLAGS",
        "ECM_MAP_SELECT_INPUT",
        "ECM_KNOCK_HIGHDET_METRIC",
        "ECM_KS1_LEVEL",
        "ECM_AF_FEEDBACK_TARGET_B1",
    } <= carried
    assert "ECM_IGNITION_TIMING_FINAL" not in carried  # it has a DID check
    # D03.38's relations make these checkable without a DID.
    assert not {"ECM_KNOCK_RETARD_DEG", "ECM_AF_LAMBDA_B1", "ECM_KS_FAULT_FLAGS"} & carried
    assert sum(1 for line in copy.read_text().splitlines() if "#" in line) == comments_before
    # and only the memory section moved: every DID entry is byte-identical
    assert copy.read_text().split("\nmemory:")[0] == _CATALOG.read_text().split("\nmemory:")[0]


def test_restamp_is_idempotent(tmp_path: Path) -> None:
    copy = tmp_path / "dids.yaml"
    copy.write_text(_CATALOG.read_text())
    memory = load_did_map(copy).memory
    stamps: dict[str, str | None] = {c.name: None for c in memory}
    stale = [c for c in memory if c.verified_tune_revision != "ZS44A-20260920"]

    assert restamp(copy, stamps, "ZS44A-20260920") == len(stale)
    assert restamp(copy, stamps, "ZS44A-20260920") == 0


# ---------------------------------------------------------------------------
# The legacy known answers under the relations' rules (key-on run, 2026-10-10)
#
# POST /control/memory-channels/verify at ignition_only, 20 samples: the
# legacy kinds passed seven channels on a constant matching a constant (rpm 0
# = 0, a timing byte at its key-off zero) and failed three where the DID side
# had nothing to say (the reciprocal of rpm 0, a reference that never moved).

_LEGACY = [c for c in _CATALOG_MEMORY if c.known_answer is not None]
_RPM_DID, _TIMING_DID, _PHI_DID, _BFS_DID = 0x1201, 0x110A, 0x1134, 0x1208


class _CatalogEcu:
    """Answers 0x23 from a per-sample RAM image and 0x22 from per-sample DIDs.

    A sample is one memory read and the DID read after it, which is how the
    known-answer verifier pairs them. Bytes the image does not set are
    ``unset(address, sample)``.
    """

    def __init__(
        self,
        ram: Callable[[int], dict[int, int]],
        dids: Callable[[int], dict[int, int]],
        unset: Callable[[int, int], int],
    ) -> None:
        self.ram, self.dids, self.unset = ram, dids, unset
        self.sample = 0

    def enter_diagnostic_session(self, subfunction: int, *, request_id: int | None = None) -> bool:
        return True

    def query_raw(self, service: int, body: bytes, *, request_id: int | None = None) -> _Reply:
        assert service == 0x23
        address = int.from_bytes(body[:4], "big")
        length = int.from_bytes(body[4:], "big")
        image = self.ram(self.sample)
        return _Reply(
            bytes(
                image.get(a, self.unset(a, self.sample)) for a in range(address, address + length)
            )
        )

    def query_mode_22(self, did: int, *, request_id: int | None = None) -> _Reply:
        raw = self.dids(self.sample)[did]
        self.sample += 1
        return _Reply(raw.to_bytes(2, "big"))


def _image(words: dict[str, int], channels: list[MemoryChannel] = _LEGACY) -> dict[int, int]:
    by_name = {c.name: c for c in channels}
    image: dict[int, int] = {}
    for name, raw in words.items():
        c = by_name[name]
        data = raw.to_bytes(c.byte_length, c.byte_order, signed=c.signed)
        for i, byte in enumerate(data):
            image[c.address + c.byte_start + i] = byte
    return image


def _key_on_ecu() -> _CatalogEcu:
    """KOEO: rpm 0, timing DID 0, phi at the clamp floor, no air charge; RAM
    zero around every word, a nonzero throttle-axis trace and final-timing
    byte (the two the old rules failed), and a zero period word."""
    ram = _image(
        {
            "ECM_TARGET_EQUIV_RATIO": 0x4000,
            "ECM_THROTTLE_MAP_Y_TRACE": 0x0100,
            "ECM_IGNITION_TIMING_FINAL": 10,
        }
    )
    dids = {_RPM_DID: 0, _TIMING_DID: 0, _PHI_DID: 64, _BFS_DID: 0}
    return _CatalogEcu(lambda _k: ram, lambda _k: dids, lambda _a, _k: 0)


def _verify_legacy(ecu: _CatalogEcu, channel: MemoryChannel, samples: int) -> ChannelVerdict:
    ecu.sample = 0
    return verify_channel(
        _client(ecu),
        channel,
        catalog=_CATALOG_MEMORY,
        samples=samples,
        request_id=0x7E0,
        session=0xC0,
    )


def test_the_key_on_read_verifies_nothing_and_fails_nothing() -> None:
    """Every legacy known answer at rest is unverifiable with a named cause.
    The seven old passes still *agree* (that is the trap); none passes."""
    verdicts = {c.name: _verify_legacy(_key_on_ecu(), c, 20) for c in _LEGACY}

    assert {v.outcome for v in verdicts.values()} == {"unverifiable"}
    assert "did_degenerate" in verdicts["ECM_ENGINE_PERIOD"].reason  # reciprocal of rpm 0
    for name in set(verdicts) - {"ECM_ENGINE_PERIOD"}:
        assert "did_constant" in verdicts[name].reason, name
    # The old passes agreed 20/20; the old fails agreed 0/20 -- neither counts.
    assert verdicts["ECM_ENGINE_SPEED_RAM"].agreements == 20
    assert verdicts["ECM_IGNITION_TIMING_RAM"].agreements == 20
    assert verdicts["ECM_THROTTLE_MAP_Y_TRACE"].agreements == 0
    assert verdicts["ECM_IGNITION_TIMING_FINAL"].agreements == 0

    # And a record run stamps none of their windows without a carry reason.
    stamps, notes = plan_restamp(_LEGACY, list(verdicts.values()), carry_reason=None)
    assert stamps == {}
    assert notes


def test_an_out_of_plausible_value_that_moves_is_still_not_evidence() -> None:
    """A timing byte at 0/1 decodes to 110/109 degrees: the DID agrees and
    moves, the neighbours disagree, but nothing plausible was compared."""
    timing = next(c for c in _LEGACY if c.name == "ECM_IGNITION_TIMING_RAM")
    ecu = _CatalogEcu(
        lambda k: _image({timing.name: k % 2}),
        lambda k: {_TIMING_DID: k % 2},
        _filler,
    )

    verdict = _verify_legacy(ecu, timing, 10)

    assert verdict.outcome == "unverifiable"
    assert verdict.agreements == 10
    assert "implausible" in verdict.reason


#: Engine running: rpm DID (12.5 rpm/count), timing DID (110 - deg), phi word.
_RUNNING_RPM = [64, 72, 80, 96, 120, 160, 200, 240]
_RUNNING_TIMING = [90, 85, 80, 95, 100, 75, 88, 92]
_RUNNING_PHI = [0x4400, 0x4800, 0x4C00, 0x4500, 0x4A00, 0x4300, 0x4600, 0x4900]
_RUNNING_BFS = [3000, 3400, 3900, 4400, 5200, 6100, 7000, 8100]


def _running_words(k: int) -> dict[str, int]:
    rpm, timing, bfs = _RUNNING_RPM[k], _RUNNING_TIMING[k], _RUNNING_BFS[k]
    return {
        "ECM_TARGET_EQUIV_RATIO": _RUNNING_PHI[k],
        "ECM_ENGINE_PERIOD": round(500_000 / rpm),
        "ECM_IGNITION_TIMING_RAM": timing,
        "ECM_ENGINE_SPEED_RAM": rpm,
        "ECM_CAM_MAP_RPM_TRACE": round(rpm * 12.5 / 50),
        "ECM_THROTTLE_MAP_Y_TRACE": round(rpm * 12.5 / 3.124834),
        "ECM_AIR_CHARGE_SLOT_1038": bfs,
        "ECM_AIR_CHARGE_SLOT_103A": bfs,
        "ECM_AIR_CHARGE_SLOT_103C": bfs,
        "ECM_IGNITION_TIMING_FINAL": 110 - timing,
        # The byte DID 0x110A reads (E05.4 (c)), 110 - degrees as the DID.
        "ECM_IGNITION_TIMING_BYTE": timing,
    }


def _running_ecu(channels: list[MemoryChannel] = _LEGACY) -> _CatalogEcu:
    def dids(k: int) -> dict[int, int]:
        return {
            _RPM_DID: _RUNNING_RPM[k],
            _TIMING_DID: _RUNNING_TIMING[k],
            _PHI_DID: _RUNNING_PHI[k] >> 8,
            _BFS_DID: _RUNNING_BFS[k],
        }

    return _CatalogEcu(lambda k: _image(_running_words(k), channels), dids, _filler)


def test_engine_running_the_correct_addresses_pass() -> None:
    verdicts = {c.name: _verify_legacy(_running_ecu(), c, len(_RUNNING_RPM)) for c in _LEGACY}

    slots = {"ECM_AIR_CHARGE_SLOT_1038", "ECM_AIR_CHARGE_SLOT_103A", "ECM_AIR_CHARGE_SLOT_103C"}
    for name in set(verdicts) - slots:
        assert verdicts[name].outcome == "pass", (name, verdicts[name].reason)
        assert "failed beside it" in verdicts[name].reason
    # The three slots hold the same air charge side by side: the neighbour
    # rule says what their catalog note does -- the region, not the register.
    for name in slots:
        assert verdicts[name].outcome == "unverifiable", name
        assert "did not tell this address" in verdicts[name].reason


def test_engine_running_a_moved_address_fails() -> None:
    """The engine-speed word read two words off: filler against a moving rpm."""
    speed = next(c for c in _LEGACY if c.name == "ECM_ENGINE_SPEED_RAM")
    moved = speed.model_copy(update={"address": speed.address + 4})

    verdict = _verify_legacy(_running_ecu(), moved, len(_RUNNING_RPM))

    assert verdict.outcome == "fail"
    assert "probably moved" in verdict.reason
