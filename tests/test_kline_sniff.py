"""Passive K-line decoding, replayed over UpRev's real session byte by byte."""

from __future__ import annotations

import gzip
import json
from collections import Counter
from pathlib import Path

from src.collection.kline import protocol as kp
from src.collection.kline.sniff import (
    INTER_BYTE_GAP_S,
    DefinitionTracker,
    FrameDecoder,
    SniffedFrame,
    exchanges,
)
from src.collection.obd_scheduler import did_response_widths

FIXTURE = Path(__file__).parent / "fixtures" / "kline" / "uprev_session_bytes.json.gz"


def _stream() -> list[tuple[float, int]]:
    fixture = json.loads(gzip.open(FIXTURE, "rt").read())
    t = 0.0
    out = []
    for byte, dt in zip(bytes.fromhex(fixture["bytes"]), fixture["dt_us"], strict=True):
        t += dt / 1e6
        out.append((t, byte))
    return out


STREAM = _stream()


def _decode(stream: list[tuple[float, int]]) -> tuple[FrameDecoder, list[SniffedFrame]]:
    decoder = FrameDecoder()
    frames: list[SniffedFrame] = []
    for at, byte in stream:
        frames += decoder.feed(at, byte)
    return decoder, frames


def test_every_frame_of_uprevs_session_is_recovered() -> None:
    decoder, frames = _decode(STREAM)
    assert decoder.stats.frames == 1462
    assert decoder.stats.wakeups == 12
    assert decoder.stats.checksum_resyncs == 0 and decoder.stats.abandoned == 0
    assert Counter((f.kind, f.direction) for f in frames) == {
        ("length", "tester"): 723,
        ("length", "ecu"): 723,
        ("addressed", "tester"): 12,
        ("addressed", "ecu"): 4,
    }


def test_addressed_frames_name_uprevs_tester_address() -> None:
    _, frames = _decode(STREAM)
    starts = [f for f in frames if f.kind == "addressed" and f.direction == "tester"]
    assert {(f.source, f.target) for f in starts} == {(0xFC, 0x10)}


def test_requests_pair_with_their_replies() -> None:
    _, frames = _decode(STREAM)
    pairs = list(exchanges(frames))
    answered = [p for p in pairs if p.reply is not None]
    assert len(answered) == 723 + 4
    for pair in answered:
        assert pair.reply is not None
        sid = pair.request.payload[0]
        reply_sid = pair.reply.payload[0]
        assert reply_sid in ((sid + 0x40) & 0xFF, kp.SID_NEGATIVE_RESPONSE)
        assert pair.reply.started >= pair.request.finished


def test_definitions_are_tracked_and_unknown_widths_refused_by_name() -> None:
    _, frames = _decode(STREAM)
    tracker = DefinitionTracker(did_response_widths()[0])
    results = [r for pair in exchanges(frames) if (r := tracker.observe(pair)) is not None]
    assert sorted(tracker.definitions) == [0x81, 0x82, 0x83]
    assert len(results) == 590
    assert all(r.unresolved and "0x1117" in r.unresolved for r in results)
    assert all(r.raw for r in results)  # raw kept for a later re-decode


def test_with_the_missing_widths_every_reply_lays_out_into_38_values() -> None:
    widths = {**did_response_widths()[0], 0x1117: 1, 0x1126: 1, 0x1204: 1, 0x1209: 2, 0x122B: 2}
    _, frames = _decode(STREAM)
    tracker = DefinitionTracker(widths)
    results = [r for pair in exchanges(frames) if (r := tracker.observe(pair)) is not None]
    assert all(r.unresolved is None and len(r.values) == 38 for r in results)


def test_a_capture_that_starts_after_the_definitions_keeps_raw_replies() -> None:
    _, frames = _decode(STREAM)
    last_define = max(
        i for i, f in enumerate(frames) if f.payload[:1] == bytes([kp.SID_DEFINE_LOCAL_ID])
    )
    tracker = DefinitionTracker(did_response_widths()[0])
    late = [r for pair in exchanges(frames[last_define + 2 :]) if (r := tracker.observe(pair))]
    assert late and all(r.unresolved and "never defined" in r.unresolved for r in late)


def test_a_corrupted_bit_costs_a_few_frames_and_the_stream_recovers() -> None:
    """Resync must not stall. It once did: a resync that left a data 0x00 at
    the front of the buffer waited on it forever, and one flipped bit cost 547
    of 1,462 frames. 200 random single-bit flips now lose at most 7."""
    import random

    rng = random.Random(20260923)
    for _ in range(25):
        stream = list(STREAM)
        index = rng.randrange(1000, len(stream) - 1000)
        at, byte = stream[index]
        stream[index] = (at, byte ^ (1 << rng.randrange(8)))
        decoder, _ = _decode(stream)
        assert decoder.stats.frames >= 1462 - 8


def test_a_gap_inside_a_frame_abandons_it() -> None:
    decoder = FrameDecoder()
    frame = kp.encode_length_frame(bytes([0x22, 0x11, 0x03]))
    decoder.feed(0.0, frame[0])
    decoder.feed(0.001, frame[1])
    got = []
    for i, b in enumerate(frame):  # the whole frame again, after a long gap
        got += decoder.feed(1.0 + 0.001 * i, b)
    assert decoder.stats.abandoned == 1
    assert [f.raw for f in got] == [frame]


def test_uprevs_ten_millisecond_byte_spacing_is_not_a_gap() -> None:
    decoder = FrameDecoder()
    frame = kp.start_communication(0x10, 0xFC)
    got = []
    for i, b in enumerate(frame):
        got += decoder.feed(0.010 * i, b)
    assert 0.010 < INTER_BYTE_GAP_S
    assert [f.raw for f in got] == [frame]
