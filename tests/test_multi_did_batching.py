"""Multi-DID Mode 22 batching — the 2-byte-identifier sibling of
``test_multi_pid_batching.py``.

The reason to batch 0x22 is NOT throughput: an unbatched sweep already
manages ~270 DIDs/s on this bus. It is that DIDs in one request share a
single ECU sample instant, so their values can be compared with each other.
Sequentially polled DIDs cannot — they are rotation-sampled with an unknown
relative age.

That makes misattribution the failure that matters. A Mode 22 response is
NOT self-delimiting, so splitting it depends entirely on knowing each DID's
payload width; get one width wrong and every later DID in that response is
silently read off the wrong bytes. The tests below pin the refusals that
keep that impossible.
"""

from __future__ import annotations

import pytest

from src.analysis.did_scan import sweep_dids_batched
from src.collection.isotp import (
    MAX_BATCHED_DIDS,
    build_batched_did_request_payload,
    split_batched_did_response,
)

# Widths as MEASURED payload lengths, which is what the splitter needs.
WIDTHS = {0x1101: 1, 0x1219: 2, 0x1208: 2, 0x1100: 4, 0x1103: 1}


def width_for(did: int) -> int | None:
    return WIDTHS.get(did)


# ---------------------------------------------------------------------------
# building the request
# ---------------------------------------------------------------------------


def test_batched_did_request_matches_the_frame_uprev_was_observed_sending() -> None:
    """UpRev's tester sent ``05 22 11 03 04 01`` to this ECM on 2026-08-08 —
    service 0x22 carrying two DIDs. That capture is why we know the ECM
    accepts batched 0x22 at all, so the builder must reproduce it exactly."""
    payload = build_batched_did_request_payload(0x22, [0x1103, 0x0401])
    assert payload[:6] == bytes([0x05, 0x22, 0x11, 0x03, 0x04, 0x01])


def test_batched_did_request_is_always_padded_to_eight_bytes() -> None:
    """This ECM ignores short frames outright — no response, not even a
    negative one — which is indistinguishable from an unsupported service."""
    for count in range(1, MAX_BATCHED_DIDS + 1):
        payload = build_batched_did_request_payload(0x22, list(range(0x1100, 0x1100 + count)))
        assert len(payload) == 8


def test_batched_did_request_rejects_more_than_fits_one_frame() -> None:
    """1 PCI + 1 service + 3 x 2 DID bytes = 8. A fourth DID cannot fit."""
    assert MAX_BATCHED_DIDS == 3
    with pytest.raises(ValueError, match="at most"):
        build_batched_did_request_payload(0x22, [0x1101, 0x1102, 0x1103, 0x1104])


def test_batched_did_request_rejects_duplicates() -> None:
    """A duplicate makes the response ambiguous to walk and wastes a slot."""
    with pytest.raises(ValueError, match="duplicate"):
        build_batched_did_request_payload(0x22, [0x1101, 0x1101])


def test_batched_did_request_rejects_out_of_range_identifiers() -> None:
    with pytest.raises(ValueError, match="out of range"):
        build_batched_did_request_payload(0x22, [0x1_0000])


def test_batched_did_request_rejects_empty() -> None:
    with pytest.raises(ValueError, match="at least one"):
        build_batched_did_request_payload(0x22, [])


# ---------------------------------------------------------------------------
# splitting the response
# ---------------------------------------------------------------------------


def test_split_walks_every_did_group() -> None:
    payload = bytes([0x11, 0x01, 0x8E, 0x12, 0x19, 0x02, 0xEE, 0x12, 0x08, 0x16, 0xAC])
    readings, clean = split_batched_did_response(payload, width_for)
    assert clean is True
    assert readings == {0x1101: b"\x8e", 0x1219: b"\x02\xee", 0x1208: b"\x16\xac"}


def test_split_stops_clean_false_on_an_unknown_width() -> None:
    """The whole point. An unknown width means the next boundary cannot be
    found, so everything after it would be read off the wrong bytes. What was
    walked before the unknown is still trustworthy and is returned; the flag
    is what tells the caller not to trust completeness."""
    payload = bytes([0x11, 0x01, 0x8E, 0x9F, 0x9F, 0x01, 0x02, 0x12, 0x19, 0x02, 0xEE])
    readings, clean = split_batched_did_response(payload, width_for)
    assert clean is False
    assert readings == {0x1101: b"\x8e"}
    assert 0x1219 not in readings


def test_split_stops_clean_false_on_a_truncated_payload() -> None:
    payload = bytes([0x11, 0x01, 0x8E, 0x12, 0x19, 0x02])  # 0x1219 wants 2, has 1
    readings, clean = split_batched_did_response(payload, width_for)
    assert clean is False
    assert readings == {0x1101: b"\x8e"}


def test_split_stops_clean_false_on_a_dangling_identifier_byte() -> None:
    """A single trailing byte cannot even form a DID."""
    payload = bytes([0x11, 0x01, 0x8E, 0x12])
    readings, clean = split_batched_did_response(payload, width_for)
    assert clean is False
    assert readings == {0x1101: b"\x8e"}


def test_split_keys_off_the_response_not_the_request() -> None:
    """An ECU may omit a DID it does not support rather than reject the whole
    batch. Matching positionally against the request would then misattribute
    every later value, so the walk reads identifiers out of the response."""
    payload = bytes([0x12, 0x19, 0x02, 0xEE, 0x11, 0x01, 0x8E])  # reversed, 0x1208 absent
    readings, clean = split_batched_did_response(payload, width_for)
    assert clean is True
    assert readings == {0x1219: b"\x02\xee", 0x1101: b"\x8e"}


# ---------------------------------------------------------------------------
# the sweep layer
# ---------------------------------------------------------------------------


class _FakeTransport:
    """Records how each DID was actually read: batched or individually."""

    def __init__(self, values: dict[int, bytes]) -> None:
        self._values = values
        self.batched_groups: list[list[int]] = []
        self.single_reads: list[int] = []

    def read_did(self, did: int) -> bytes | None:
        self.single_reads.append(did)
        return self._values.get(did)

    def read_dids(self, dids, byte_length_for):  # type: ignore[no-untyped-def]
        self.batched_groups.append(list(dids))
        payload = b"".join(
            bytes([d >> 8, d & 0xFF]) + self._values[d] for d in dids if d in self._values
        )
        return split_batched_did_response(payload, byte_length_for)


def test_sweep_batches_known_widths_and_falls_back_for_unknown() -> None:
    """A mixed list must still be correct: unknown-width DIDs lose the
    same-instant guarantee but must not desync the ones that have it."""
    values = {0x1101: b"\x8e", 0x1219: b"\x02\xee", 0x1208: b"\x16\xac", 0x1999: b"\x01"}
    transport = _FakeTransport(values)
    snapshot = sweep_dids_batched(
        transport, [0x1101, 0x1219, 0x1208, 0x1999], width_for, batch_size=3
    )
    assert snapshot.values == values
    assert transport.batched_groups == [[0x1101, 0x1219, 0x1208]]
    assert transport.single_reads == [0x1999]  # unknown width -> individual


def test_sweep_rereads_individually_when_the_ecu_omits_a_did() -> None:
    """A gap in a batched response must be re-read, never inferred."""
    values = {0x1101: b"\x8e", 0x1208: b"\x16\xac"}
    transport = _FakeTransport(values)
    snapshot = sweep_dids_batched(transport, [0x1101, 0x1219, 0x1208], width_for, batch_size=3)
    assert snapshot.values == values
    assert 0x1219 in transport.single_reads


class _SingleOnlyTransport:
    """A bare Mode22Transport — no ``read_dids``, e.g. the ELM327 path."""

    def __init__(self, values: dict[int, bytes]) -> None:
        self._values = values
        self.single_reads: list[int] = []

    def read_did(self, did: int) -> bytes | None:
        self.single_reads.append(did)
        return self._values.get(did)


def test_sweep_degrades_to_single_reads_without_batch_support() -> None:
    values = {0x1101: b"\x8e", 0x1219: b"\x02\xee"}
    transport = _SingleOnlyTransport(values)
    snapshot = sweep_dids_batched(transport, [0x1101, 0x1219], width_for, batch_size=3)
    assert snapshot.values == values
    assert transport.single_reads == [0x1101, 0x1219]


def test_sweep_batch_size_is_capped_at_the_frame_ceiling() -> None:
    values = {0x1101: b"\x8e", 0x1219: b"\x02\xee", 0x1208: b"\x16\xac", 0x1103: b"\xa5"}
    transport = _FakeTransport(values)
    sweep_dids_batched(transport, list(values), width_for, batch_size=99)
    assert all(len(g) <= MAX_BATCHED_DIDS for g in transport.batched_groups)
