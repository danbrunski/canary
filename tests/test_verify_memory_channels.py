"""Re-verifying a RAM address against the running calibration."""

from __future__ import annotations

from pathlib import Path

from src.decode.dids import MemoryChannel, MemoryKnownAnswer, is_memory_checkable, load_did_map
from tools.verify_memory_channels import (
    ChannelVerdict,
    plan_restamp,
    restamp,
    verify_channel,
)


class _Reply:
    def __init__(self, payload: bytes) -> None:
        self.payload = payload


class _FakeEcu:
    """Answers 0x23 with `words` and 0x22 through `did_for`."""

    def __init__(self, words: list[int], did_for) -> None:
        self._words = list(words)
        self._did_for = did_for
        self.last_word = 0
        self.sessions: list[int] = []

    def enter_diagnostic_session(self, subfunction: int, *, request_id: int | None = None) -> bool:
        self.sessions.append(subfunction)
        return True

    def query_raw(self, service: int, body: bytes, *, request_id: int | None = None) -> _Reply:
        assert service == 0x23
        self.last_word = self._words.pop(0) if self._words else self.last_word
        return _Reply(self.last_word.to_bytes(2, "big"))

    def query_mode_22(self, did: int, *, request_id: int | None = None) -> _Reply:
        return _Reply(bytes([self._did_for(self.last_word)]))


def _channel(**overrides) -> MemoryChannel:
    base = dict(
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

    verdict = verify_channel(ecu, _channel(), catalog=(), samples=3, request_id=0x7E0, session=0xC0)

    assert verdict.passed is True
    assert verdict.agreements == 3
    assert ecu.sessions == [0xC0]


def test_fuel_cut_still_satisfies_the_relation() -> None:
    """Word 0 and word 0x4000 both map to DID raw 64 — that is the clamp."""
    ecu = _FakeEcu([0x0000, 0x4000], _truthful)

    verdict = verify_channel(ecu, _channel(), catalog=(), samples=2, request_id=0x7E0, session=0xC0)

    assert verdict.passed is True
    assert verdict.sentinels == 1


def test_moved_address_is_caught_even_while_decoding_plausibly() -> None:
    """The failure this exists for: still answers, still in range, wrong thing.

    0x4100 decodes to phi 1.0156, comfortably inside the plausible bounds. Only
    the relation to the cross-check DID reveals that the word is no longer the
    one 0x1134 is a view of.
    """
    ecu = _FakeEcu([0x4100, 0x4100], lambda _word: 70)

    verdict = verify_channel(ecu, _channel(), catalog=(), samples=2, request_id=0x7E0, session=0xC0)

    assert verdict.outcome == "fail"
    assert "probably moved" in verdict.reason


def test_channel_without_a_declared_check_is_skipped_not_failed() -> None:
    """A catalog gap must not block re-stamping every other channel."""
    verdict = verify_channel(
        _FakeEcu([0x4000], _truthful),
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

    verdict = verify_channel(ecu, _channel(), catalog=(), samples=1, request_id=0x7E0, session=0xC0)

    assert verdict.passed is False
    assert verdict.outcome == "unverifiable"
    assert "session_entry_refused" in verdict.reason


def test_unanswered_reads_are_unverifiable_not_a_fail() -> None:
    """The gate suppressing our frames (another tester took the ECM) leaves
    every read empty: reads_unanswered, never "the address has moved"."""
    ecu = _FakeEcu([0x4000], _truthful)
    ecu.query_raw = lambda service, body, *, request_id=None: _Reply(b"")  # type: ignore[method-assign]

    verdict = verify_channel(ecu, _channel(), catalog=(), samples=3, request_id=0x7E0, session=0xC0)

    assert verdict.outcome == "unverifiable"
    assert "reads_unanswered" in verdict.reason


class _TimingEcu:
    """0x23 answers one s8 timing byte; 0x22 answers DID 0x110A (110 - t)."""

    def __init__(self, timings: list[int], did_for) -> None:
        self._timings = list(timings)
        self._did_for = did_for
        self.last = 0

    def enter_diagnostic_session(self, subfunction: int, *, request_id: int | None = None) -> bool:
        return True

    def query_raw(self, service: int, body: bytes, *, request_id: int | None = None) -> _Reply:
        assert service == 0x23 and body[-2:] == (1).to_bytes(2, "big")
        self.last = self._timings.pop(0) if self._timings else self.last
        return _Reply(self.last.to_bytes(1, "big", signed=True))

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
        ecu, _final_timing(), catalog=_CATALOG_MEMORY, samples=3, request_id=0x7E0, session=0xC0
    )

    assert verdict.passed is True
    assert verdict.agreements == 3


def test_final_timing_fails_when_the_byte_is_not_the_timing() -> None:
    """A byte off by one degree everywhere (a neighbouring register) fails,
    so the relation can fail."""
    ecu = _TimingEcu([20, 35, -5], lambda t: 110 - t + 1)

    verdict = verify_channel(
        ecu, _final_timing(), catalog=_CATALOG_MEMORY, samples=3, request_id=0x7E0, session=0xC0
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
    stamps = {c.name: None for c in memory}
    stale = [c for c in memory if c.verified_tune_revision != "ZS44A-20260920"]

    assert restamp(copy, stamps, "ZS44A-20260920") == len(stale)
    assert restamp(copy, stamps, "ZS44A-20260920") == 0
