"""Re-verifying a RAM address against the running calibration."""

from __future__ import annotations

from pathlib import Path

import yaml

from src.decode.dids import MemoryChannel, MemoryKnownAnswer
from tools.verify_memory_channels import restamp, verify_channel


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

    verdict = verify_channel(ecu, _channel(), samples=3, request_id=0x7E0, session=0xC0)

    assert verdict.passed is True
    assert verdict.agreements == 3
    assert ecu.sessions == [0xC0]


def test_fuel_cut_still_satisfies_the_relation() -> None:
    """Word 0 and word 0x4000 both map to DID raw 64 — that is the clamp."""
    ecu = _FakeEcu([0x0000, 0x4000], _truthful)

    verdict = verify_channel(ecu, _channel(), samples=2, request_id=0x7E0, session=0xC0)

    assert verdict.passed is True
    assert verdict.sentinels == 1


def test_moved_address_is_caught_even_while_decoding_plausibly() -> None:
    """The failure this exists for: still answers, still in range, wrong thing.

    0x4100 decodes to phi 1.0156, comfortably inside the plausible bounds. Only
    the relation to the cross-check DID reveals that the word is no longer the
    one 0x1134 is a view of.
    """
    ecu = _FakeEcu([0x4100, 0x4100], lambda _word: 70)

    verdict = verify_channel(ecu, _channel(), samples=2, request_id=0x7E0, session=0xC0)

    assert verdict.outcome == "fail"
    assert "probably moved" in verdict.reason


def test_channel_without_a_declared_check_is_skipped_not_failed() -> None:
    """A catalog gap must not block re-stamping every other channel."""
    verdict = verify_channel(
        _FakeEcu([0x4000], _truthful),
        _channel(known_answer=None),
        samples=1,
        request_id=0x7E0,
        session=0xC0,
    )

    assert verdict.outcome == "unverifiable"
    assert verdict.passed is False
    assert "plausibility alone" in verdict.reason


def test_refused_session_fails_rather_than_reporting_agreement() -> None:
    ecu = _FakeEcu([0x4000], _truthful)
    ecu.enter_diagnostic_session = lambda subfunction, *, request_id=None: False  # type: ignore[method-assign]

    verdict = verify_channel(ecu, _channel(), samples=1, request_id=0x7E0, session=0xC0)

    assert verdict.passed is False
    assert "refused" in verdict.reason


def test_restamp_touches_only_the_named_channel_and_field(tmp_path: Path) -> None:
    path = tmp_path / "dids.yaml"
    path.write_text(
        yaml.safe_dump(
            {
                "schema_version": 1,
                "memory": [
                    {"name": "A", "verified_tune_revision": "old", "notes": "keep me"},
                    {"name": "B", "verified_tune_revision": "old"},
                ],
            },
            sort_keys=False,
        )
    )

    assert restamp(path, ["A"], "NEW") == 1

    after = yaml.safe_load(path.read_text())
    assert after["memory"][0]["verified_tune_revision"] == "NEW"
    assert after["memory"][0]["notes"] == "keep me"
    assert after["memory"][1]["verified_tune_revision"] == "old"


def test_restamp_is_a_noop_when_already_current(tmp_path: Path) -> None:
    path = tmp_path / "dids.yaml"
    path.write_text(
        yaml.safe_dump(
            {"memory": [{"name": "A", "verified_tune_revision": "NEW"}]}, sort_keys=False
        )
    )

    assert restamp(path, ["A"], "NEW") == 0
