"""Passive K-line decoding, replayed over UpRev's real session byte by byte."""

from __future__ import annotations

import gzip
import json
from collections import Counter
from pathlib import Path

import pytest

from src.collection.kline import protocol as kp
from src.collection.kline.profile import load_kline_profile
from src.collection.kline.sniff import (
    BREAK_FOLLOW_S,
    INTER_BYTE_GAP_S,
    UART_RX_TIMEOUT_CHARS,
    DefinitionTracker,
    FrameDecoder,
    SniffedBreak,
    SniffedFrame,
    exchanges,
)
from src.collection.kline.transport import KlineTransport, Mode, RxChunk
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
    """Kernel-timestamped bytes: every gap between them was really on the line."""
    decoder = FrameDecoder()
    frames: list[SniffedFrame] = []
    last = stream[0][0]
    for at, byte in stream:
        frames += decoder.feed(at, byte, quiet_s=at - last)
        last = at
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


def test_every_frame_after_a_start_names_the_tester_both_ways() -> None:
    """H06.4: length-byte frames carry no address, so the tester's comes from
    the StartCommunication, like the module; both directions carry it."""
    _, frames = _decode(STREAM)
    assert {f.tester for f in frames} == {0xFC}
    assert {f.direction for f in frames} == {"tester", "ecu"}


def test_the_tester_is_unknown_before_a_start_and_forgotten_after_a_stop() -> None:
    from tests.builders import abs_scan_tool_frames, kline_wire

    _, frames = _decode(kline_wire(abs_scan_tool_frames(cycles=1)))
    assert [f.tester for f in frames[:2]] == [None, None]  # attached mid-session
    stop = next(i for i, f in enumerate(frames) if f.payload == b"\xc2")
    assert {f.tester for f in frames[2 : stop + 1]} == {0xFC}
    # The next session is its own StartCommunication's, which names it again.
    assert {f.tester for f in frames[stop + 1 :]} == {0xFC}


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
    widths = did_response_widths()[0]
    del widths[0x1117]  # since 2026-10-07 the catalog knows every DID streamed here
    tracker = DefinitionTracker(widths)
    results = [r for pair in exchanges(frames) if (r := tracker.observe(pair)) is not None]
    assert sorted(tracker.definitions) == [0x81, 0x82, 0x83]
    assert len(results) == 590
    assert all(r.unresolved and "0x1117" in r.unresolved for r in results)
    assert all(r.raw for r in results)  # raw kept for a later re-decode


def test_with_the_catalog_widths_every_reply_lays_out_into_38_values() -> None:
    # The firmware DID audit (2026-10-07) gave 0x1117, 0x1204, 0x1209 and
    # 0x122B catalog widths (1, 2, 2, 2): the K-line layout in
    # docs/uprev-usb-kline-research.md places them the same way.
    _, frames = _decode(STREAM)
    tracker = DefinitionTracker(did_response_widths()[0])
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
    decoder.feed(0.0, frame[0], quiet_s=0.0)
    decoder.feed(0.001, frame[1], quiet_s=0.0)
    got = []
    for i, b in enumerate(frame):  # the whole frame again, after a long silence
        got += decoder.feed(1.0 + 0.001 * i, b, quiet_s=0.999 if i == 0 else 0.0)
    assert decoder.stats.abandoned == 1
    assert [f.raw for f in got] == [frame]


def test_uprevs_ten_millisecond_byte_spacing_is_not_a_gap() -> None:
    decoder = FrameDecoder()
    frame = kp.start_communication(0x10, 0xFC)
    got = []
    for i, b in enumerate(frame):
        got += decoder.feed(0.010 * i, b, quiet_s=0.009 if i else 0.0)
    assert 0.010 < INTER_BYTE_GAP_S
    assert [f.raw for f in got] == [frame]


# --------------------------------------------------------------------------- host stalls


class _StallingHost:
    """UpRev's session arriving on a virtual line, read by the real
    ``KlineTransport.listen`` on a host that stops reading for ``stall_s``
    once a second — what the Pi did on 2026-09-24 while UpRev logged."""

    def __init__(self, stream: list[tuple[float, int]], stall_s: float) -> None:
        self.stream = stream
        self.next = 0
        self.t = stream[0][0] - 0.1
        self.stall_s = stall_s
        self.next_stall = self.t + 1.0
        self.break_condition = False

    # SerialPort
    def read(self, size: int = 1, /) -> bytes:
        out = bytearray()
        while self.next < len(self.stream) and self.stream[self.next][0] <= self.t:
            out.append(self.stream[self.next][1])
            self.next += 1
        return bytes(out)

    def write(self, data: bytes, /) -> int:  # pragma: no cover - sniff never writes
        raise AssertionError("sniff mode transmitted")

    def reset_input_buffer(self) -> None:
        pass

    def flush(self) -> None:
        pass

    def close(self) -> None:
        pass

    # OutputLines
    def set(self, levels: object, /) -> None:
        pass

    # clock
    def clock(self) -> float:
        return self.t

    def sleep(self, seconds: float) -> None:
        self.t += seconds
        if self.t >= self.next_stall:
            self.t += self.stall_s
            self.next_stall = self.t + 1.0

    def done(self) -> bool:
        return self.next >= len(self.stream) and self.t > self.stream[-1][0] + 0.1


def _listen(stall_s: float) -> list[RxChunk]:
    host = _StallingHost(STREAM, stall_s)
    transport = KlineTransport(
        load_kline_profile(Path("data/nissan-xterra-2008/kline.yaml")),
        mode=Mode.SNIFF,
        open_port=lambda: host,
        open_lines=lambda: host,
        clock=host.clock,
        sleep=host.sleep,
        # The fixture was read unmarked (2026-09-23): its bytes are raw.
        mark_port=lambda port: False,
    )
    chunks: list[RxChunk] = []
    with transport:
        transport.listen(host.done, chunks.append)
    return chunks


def test_host_stalls_cost_no_frames() -> None:
    chunks = _listen(stall_s=0.35)
    assert sum(len(c.data) for c in chunks) == len(STREAM)
    decoder = FrameDecoder()
    frames = [f for c in chunks for f in decoder.feed_chunk(c)]
    assert decoder.stats.frames == 1462
    assert decoder.stats.abandoned == 0 and decoder.stats.checksum_resyncs == 0
    # The traffic that waited out a stall says so, never by more than the
    # stall; the rest is known to the 1 ms poll.
    late = [f for f in frames if f.timing_uncertainty_s >= 0.003]
    assert late and max(f.timing_uncertainty_s for f in late) <= 0.35 + 0.002
    assert len(late) < len(frames) / 2  # ~26 %: stalled 0.35 s of every 1.35 s


def test_the_stall_model_breaks_read_time_gaps() -> None:
    """The regression is real: judging gaps by read times, the 2026-09-24
    way, loses frames on these same reads."""
    decoder = FrameDecoder()
    last = None
    for chunk in _listen(stall_s=0.35):
        for byte in chunk.data:
            decoder.feed(chunk.at, byte, quiet_s=0.0 if last is None else chunk.at - last)
            last = chunk.at
    assert decoder.stats.abandoned > 0 and decoder.stats.frames < 1462


def test_bounds_bracket_each_bytes_arrival() -> None:
    chunk = RxChunk(at=10.0, data=b"abc", quiet_s=0.0, after=9.5, char_s=0.001)
    expected = [(9.501, 9.998), (9.502, 9.999), (9.503, 10.0)]
    assert [pytest.approx(b) for b in chunk.byte_bounds()] == expected
    prompt = RxChunk(at=10.0, data=b"ab", quiet_s=0.02, after=9.999, char_s=0.001)
    assert all(lo <= hi for lo, hi in prompt.byte_bounds())


def test_a_zero_length_format_byte_met_in_resync_is_skipped_not_fatal() -> None:
    """``80 80 64 64``: a 4-byte "addressed frame" whose checksum matches. The
    stall model found it; decoding it raised and would have ended sniffing."""
    decoder = FrameDecoder()
    got = [f for b in bytes.fromhex("80806464") for f in decoder.feed(0.0, b, quiet_s=0.0)]
    assert got == [] and decoder.stats.checksum_resyncs >= 1


# --------------------------------------------------------------------------
# K01.1 — every frame is attributed to its module
# --------------------------------------------------------------------------


def test_uprevs_whole_session_is_the_ecms() -> None:
    _, frames = _decode(STREAM)
    # Twelve wake-ups, and each session's frames carry the ECM's address.
    assert {f.module for f in frames} == {0x10}


def test_a_scan_tool_session_on_the_abs_is_stamped_until_it_stops() -> None:
    from tests.builders import abs_scan_tool_frames, kline_wire

    _, frames = _decode(kline_wire(abs_scan_tool_frames(cycles=2)))
    by_payload = [(f.payload.hex(), f.direction, f.module) for f in frames]
    # Heard before any StartCommunication: unknown, not guessed.
    assert by_payload[:2] == [("2212020401", "tester", None), ("62120200f5", "ecu", None)]
    # StartCommunication names the target; its reply names the source.
    assert by_payload[2][1:] == ("tester", 0x28) and by_payload[3][1:] == ("ecu", 0x28)
    stop = next(i for i, f in enumerate(frames) if f.payload == b"\xc2")
    assert {f.module for f in frames[2 : stop + 1]} == {0x28}
    # A new wake-up and StartCommunication: the ECM's 0x1210 is not the ABS's.
    ecm = frames[stop + 1 :]
    assert {f.module for f in ecm} == {0x10}
    assert [f.payload.hex() for f in ecm][-1] == "621210001a"


def test_a_reply_whose_request_was_missed_still_reads_as_a_reply() -> None:
    """189 ABS replies were stored as requests on 2026-09-24: direction was
    judged against the pending request, which the sniffer had missed."""
    from tests.builders import abs_scan_tool_frames, kline_wire

    _, frames = _decode(kline_wire(abs_scan_tool_frames(cycles=1)))
    at = max(i for i, f in enumerate(frames) if f.payload == bytes.fromhex("621118a1"))
    previous, orphan = frames[at - 1], frames[at]
    assert previous.payload == b"\x7e"  # nothing pending when it arrived
    assert orphan.direction == "ecu"


# --------------------------------------------------------------------------- breaks (H05.9)

SRS_START = bytes.fromhex("81 58 fc 81 56")
SRS_START_REPLY = bytes.fromhex("83 fc 58 c1 6d 8f 94")
BREAK = b"\xff\x00\x00"


def _marked(data: bytes) -> bytes:
    """``data`` as a PARMRK port delivers it: every literal 0xFF doubled."""
    return data.replace(b"\xff", b"\xff\xff")


def _chunk(at: float, data: bytes, *, quiet_s: float = 0.1, char_s: float = 0.0) -> RxChunk:
    return RxChunk(
        at=at, data=data, quiet_s=quiet_s, after=at - quiet_s, char_s=char_s, marked=True
    )


def test_a_break_then_the_srs_start_gives_one_wake_up_spanning_fifty_ms() -> None:
    """Known answer: ``FF 00 00`` at t0, ``81 58 FC 81 56`` at t0 + 50 ms."""
    decoder = FrameDecoder()
    t0 = 100.0
    events = decoder.feed_chunk_events(_chunk(t0, BREAK))
    assert events == []  # the break waits for its next byte
    events = decoder.feed_chunk_events(_chunk(t0 + 0.050, SRS_START, quiet_s=0.049))
    breaks = [e for e in events if isinstance(e, SniffedBreak)]
    frames = [e for e in events if isinstance(e, SniffedFrame)]
    assert len(breaks) == 1 and events.index(breaks[0]) < events.index(frames[0])
    assert [f.raw for f in frames] == [SRS_START]
    brk = breaks[0]
    assert brk.span_s == pytest.approx(0.050, abs=1e-9)
    assert brk.detected == t0 and brk.next_byte == t0 + 0.050
    assert decoder.stats.breaks == 1 and decoder.stats.wakeups == 1


def test_at_ten_k_four_hundred_baud_the_span_carries_its_honest_bounds() -> None:
    """Real character time: the span estimate sits inside its bounds, and the
    uncertainty covers the read model plus the UART's receive timeout."""
    char_s = 10 / 10400
    decoder = FrameDecoder()
    t0 = 100.0
    decoder.feed_chunk_events(_chunk(t0, BREAK, char_s=char_s))
    events = decoder.feed_chunk_events(_chunk(t0 + 0.050, SRS_START, quiet_s=0.045, char_s=char_s))
    brk = next(e for e in events if isinstance(e, SniffedBreak))
    span = brk.span_s
    bounds = brk.span_bounds_s
    assert span is not None and bounds is not None
    # The first byte of the frame arrived four characters before the read.
    assert span == pytest.approx(0.050 - 4 * char_s)
    assert bounds[0] <= span <= bounds[1]
    assert brk.timing_uncertainty_s >= UART_RX_TIMEOUT_CHARS * char_s
    low_min, low_max = brk.low_bounds_s
    assert low_min == char_s and low_max == bounds[1]


def test_escaped_ff_is_one_literal_byte_even_split_across_reads() -> None:
    reply = kp.encode_length_frame(b"\x61\xff\x01")  # a value byte of 0xFF
    assert b"\xff" in reply
    decoder = FrameDecoder()
    got = decoder.feed_chunk(_chunk(1.0, _marked(SRS_START)))
    got += decoder.feed_chunk(_chunk(1.03, SRS_START_REPLY, quiet_s=0.029))
    marked = _marked(reply)
    cut = marked.index(b"\xff") + 1  # between the two FFs
    got += decoder.feed_chunk(_chunk(1.06, marked[:cut], quiet_s=0.029))
    got += decoder.feed_chunk(_chunk(1.061, marked[cut:], quiet_s=0.0))
    assert [f.raw for f in got] == [SRS_START, SRS_START_REPLY, reply]
    assert got[-1].module == 0x58
    assert decoder.stats.breaks == 0


def test_a_framing_error_byte_is_counted_and_still_decoded_as_data() -> None:
    raw = bytearray(SRS_START)
    marked = raw[:2] + b"\xff\x00" + raw[2:3] + raw[3:]  # FF 00 FC: FC with a framing error
    decoder = FrameDecoder()
    frames = decoder.feed_chunk(_chunk(1.0, bytes(marked)))
    assert [f.raw for f in frames] == [SRS_START]
    assert decoder.stats.framing_errors == 1 and decoder.stats.breaks == 0


def test_in_a_marked_stream_a_bare_zero_is_data_not_a_wake_up() -> None:
    decoder = FrameDecoder()
    decoder.feed_chunk(_chunk(1.0, SRS_START + SRS_START_REPLY))
    decoder.feed_chunk(_chunk(1.5, b"\x00", quiet_s=0.4))
    assert decoder.stats.wakeups == 0
    # Still in the SRS session: the next length frame keeps its module.
    frames = decoder.feed_chunk(_chunk(2.0, kp.encode_length_frame(b"\x3e"), quiet_s=0.4))
    assert frames[0].module == 0x58


def test_a_break_mid_frame_abandons_it_and_ends_the_session() -> None:
    decoder = FrameDecoder()
    decoder.feed_chunk(_chunk(1.0, SRS_START + SRS_START_REPLY))
    decoder.feed_chunk(_chunk(1.5, b"\x03\x21", quiet_s=0.4))
    decoder.feed_chunk(_chunk(1.501, BREAK, quiet_s=0.0))
    assert decoder.stats.abandoned == 1 and decoder.stats.wakeups == 1
    frames = decoder.feed_chunk(_chunk(1.6, kp.encode_length_frame(b"\x3e"), quiet_s=0.09))
    # The session ended with the wake-up: the frame belongs to no known module.
    assert [f.module for f in frames] == [None]


def test_a_break_nothing_follows_is_reported_without_a_next_byte() -> None:
    decoder = FrameDecoder()
    decoder.feed_chunk_events(_chunk(5.0, BREAK))
    assert decoder.flush_breaks(5.0 + BREAK_FOLLOW_S / 2) == []
    (brk,) = decoder.flush_breaks(5.0 + BREAK_FOLLOW_S * 2)
    assert brk.next_byte is None and brk.span_s is None and brk.span_bounds_s is None
    assert brk.timing_uncertainty_s > 0
    assert decoder.flush_breaks(100.0, force=True) == []


def test_a_break_followed_by_another_names_it() -> None:
    decoder = FrameDecoder()
    decoder.feed_chunk_events(_chunk(5.0, BREAK))
    (first,) = decoder.feed_chunk_events(_chunk(5.2, BREAK, quiet_s=0.19))
    assert isinstance(first, SniffedBreak)
    assert first.next_break == 5.2 and first.next_byte is None
    assert decoder.stats.breaks == 2


def test_uprevs_session_marked_decodes_byte_for_byte_as_unmarked() -> None:
    """The whole UpRev session as a PARMRK port would deliver it: FFs doubled,
    each wake-up zero as ``FF 00 00``. Same frames, and one break per wake-up."""
    plain = FrameDecoder()
    plain_frames: list[SniffedFrame] = []
    marked_bytes = bytearray()
    last = STREAM[0][0]
    for at, byte in STREAM:
        before = plain.stats.wakeups
        plain_frames += plain.feed(at, byte, quiet_s=at - last)
        last = at
        if plain.stats.wakeups > before:
            marked_bytes += BREAK
        else:
            marked_bytes += _marked(bytes([byte]))
    assert plain.stats.wakeups >= 1
    # Feed the marked stream in 7-byte reads, splitting escapes anywhere.
    decoder = FrameDecoder()
    marked_frames: list[SniffedFrame] = []
    for k in range(0, len(marked_bytes), 7):
        piece = bytes(marked_bytes[k : k + 7])
        marked_frames += decoder.feed_chunk(_chunk(1.0 + k, piece, quiet_s=0.0))
    assert [f.raw for f in marked_frames] == [f.raw for f in plain_frames]
    assert [f.module for f in marked_frames] == [f.module for f in plain_frames]
    assert decoder.stats.breaks == plain.stats.wakeups
