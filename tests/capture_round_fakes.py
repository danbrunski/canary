"""Deterministic clock and ECU fakes for driving ``run_can_obd_loop`` (C02).

The collector loop reads ``time.monotonic()`` / ``time.time()`` through the
``time`` module bound in each collection module, so a test installs
:class:`FakeClock` in place of that module and the whole loop — request
timeouts, budget pacing, round deadlines, and the presentation stamps — runs on
simulated time. Nothing sleeps; a ``recv`` with no frame due simply advances the
clock by its timeout, which is what a real bus would have cost.
"""

from __future__ import annotations

from collections.abc import Iterable

import can
import pytest

from src.collection import can_obd_collector, obd_scheduler, tuning_profile

#: Mode 01 payloads the fake ECU answers with.
DEFAULT_VALUES: dict[int, bytes] = {
    0x0C: b"\x0b\xb8",  # RPM 750
    0x0D: b"\x32",  # SPEED 50
    0x04: b"\x40",  # ENGINE_LOAD
    0x11: b"\x19",  # THROTTLE_POS
    0x0E: b"\x50",  # TIMING_ADVANCE
    0x05: b"\x5a",  # COOLANT_TEMP
    0x0F: b"\x46",  # INTAKE_TEMP
    0x10: b"\x01\x00",  # MAF
    0x06: b"\x80",  # SHORT_FUEL_TRIM_1
    0x07: b"\x80",  # LONG_FUEL_TRIM_1
}


class FakeClock:
    """Monotonic + wall clocks that only move when told to."""

    def __init__(self, *, monotonic: float = 1000.0, wall: float = 1_700_000_000.0) -> None:
        self._monotonic = monotonic
        self._wall = wall

    def monotonic(self) -> float:
        return self._monotonic

    def time(self) -> float:
        return self._wall

    def sleep(self, seconds: float) -> None:
        self.advance(seconds)

    def advance(self, seconds: float) -> None:
        if seconds < 0:
            raise ValueError("the fake clock only runs forward")
        self._monotonic += seconds
        self._wall += seconds


def install_clock(monkeypatch: pytest.MonkeyPatch, clock: FakeClock) -> None:
    """Bind ``clock`` as the ``time`` module of every module the loop reads."""
    for module in (can_obd_collector, obd_scheduler, tuning_profile):
        monkeypatch.setattr(module, "time", clock)


def isotp_frames(body: bytes) -> list[bytes]:
    """Wrap a service body in single- or multi-frame ISO-TP."""
    if len(body) <= 7:
        return [bytes([len(body)]) + body + bytes(7 - len(body))]
    frames = [bytes([0x10 | (len(body) >> 8), len(body) & 0xFF]) + body[:6]]
    rest, seq = body[6:], 1
    while rest:
        chunk, rest = rest[:7], rest[7:]
        frames.append(bytes([0x20 | (seq & 0x0F)]) + chunk + bytes(7 - len(chunk)))
        seq += 1
    return frames


class ScriptedEcu:
    """A Mode 01 ECU on the fake clock.

    Every request is answered ``latency`` seconds after it was sent (a
    multi-frame response spaces its consecutive frames ``frame_gap`` apart).
    ``batch_nrc`` refuses every multi-PID request with a negative response,
    ``answer_batches=False`` ignores them silently, and ``silent_pids`` never
    answer at all — the timeout, permanent-refusal and fallback cases the
    round accounting has to stay bounded under.
    """

    def __init__(
        self,
        clock: FakeClock,
        *,
        latency: float = 0.005,
        frame_gap: float = 0.001,
        answer_batches: bool = True,
        batch_nrc: int | None = None,
        silent_pids: Iterable[int] = (),
        nrc_pids: Iterable[int] = (),
        stamp_frames: bool = True,
        values: dict[int, bytes] | None = None,
    ) -> None:
        self.clock = clock
        self.latency = latency
        self.frame_gap = frame_gap
        self.answer_batches = answer_batches
        self.batch_nrc = batch_nrc
        self.silent_pids = frozenset(silent_pids)
        self.nrc_pids = frozenset(nrc_pids)
        self.stamp_frames = stamp_frames
        self.values = dict(DEFAULT_VALUES if values is None else values)
        self.sent: list[tuple[float, can.Message]] = []
        self.batch_requests = 0
        self.single_requests = 0
        self.on_send: list[object] = []
        self._pending: list[tuple[float, bytes]] = []

    def send(self, msg: can.Message, timeout: float | None = None) -> None:
        now = self.clock.monotonic()
        self.sent.append((now, msg))
        for hook in self.on_send:
            hook(msg)  # type: ignore[operator]
        data = bytes(msg.data)
        if len(data) < 2:
            return
        length, service = data[0], data[1]
        if service == 0x30:  # flow control from the collector
            return
        if service == 0x22 and length >= 3:
            self.single_requests += 1
            did = data[2:4]
            self._queue([bytes([0x05, 0x62, *did, 0x12, 0x34, 0, 0])], now)
            return
        if service != 0x01:
            return
        pids = list(data[2 : 1 + length])
        if len(pids) > 1:
            self.batch_requests += 1
            if not self.answer_batches:
                return
            if self.batch_nrc is not None:
                self._queue([bytes([0x03, 0x7F, 0x01, self.batch_nrc, 0, 0, 0, 0])], now)
                return
        else:
            self.single_requests += 1
            (pid,) = pids
            if pid in self.silent_pids:
                return
            if pid in self.nrc_pids:
                self._queue([bytes([0x03, 0x7F, 0x01, 0x12, 0, 0, 0, 0])], now)
                return
        body = bytearray([0x41])
        for pid in pids:
            if pid in self.values and pid not in self.silent_pids:
                body += bytes([pid]) + self.values[pid]
        self._queue(isotp_frames(bytes(body)), now)

    def _queue(self, frames: list[bytes], sent_at: float) -> None:
        due = sent_at + self.latency
        for frame in frames:
            self._pending.append((due, frame))
            due += self.frame_gap

    def recv(self, timeout: float | None = None) -> can.Message | None:
        wait = 0.0 if timeout is None else float(timeout)
        now = self.clock.monotonic()
        if self._pending and self._pending[0][0] <= now + wait:
            due, frame = self._pending.pop(0)
            if due > now:
                self.clock.advance(due - now)
            return can.Message(
                arbitration_id=0x7E8,
                data=frame,
                is_extended_id=False,
                timestamp=self.clock.time() if self.stamp_frames else 0.0,
            )
        self.clock.advance(wait)
        return None

    def shutdown(self) -> None:  # pragma: no cover - interface completeness
        pass


def group_by_stamp(readings: Iterable[object]) -> dict[float, list[object]]:
    """Readings keyed by presentation timestamp."""
    grouped: dict[float, list[object]] = {}
    for reading in readings:
        grouped.setdefault(float(reading.timestamp), []).append(reading)  # type: ignore[attr-defined]
    return grouped


def stamp_span_seconds(readings: Iterable[object]) -> float:
    """Wall-clock spread of the acquisition instants behind one presentation stamp."""
    acquired = [
        float(reading.acquired_at)  # type: ignore[attr-defined]
        for reading in readings
        if getattr(reading, "acquired_at", None) is not None
    ]
    return max(acquired) - min(acquired) if acquired else 0.0
