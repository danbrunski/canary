"""Deterministic clock and ECU fakes for driving ``run_can_obd_loop`` (C02).

The collector loop reads ``time.monotonic()`` / ``time.time()`` through the
``time`` module bound in each collection module, so a test installs
:class:`FakeClock` in place of that module and the whole loop — request
timeouts, budget pacing, round deadlines, and the presentation stamps — runs on
simulated time. Nothing sleeps; a ``recv`` with no frame due simply advances the
clock by its timeout, which is what a real bus would have cost.
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping

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


#: UDS 0x10 sub-functions that put a session-gated ECU back in the default
#: session (the textbook 0x01 and the Nissan-verified 0x81, data/dids.yaml).
DEFAULT_SESSION_SUBFUNCTIONS: frozenset[int] = frozenset({0x01, 0x81})
#: NRC a session-gated ECU answers a bare Mode 22 with (``7F 22 80``).
NRC_SERVICE_NOT_SUPPORTED_IN_SESSION = 0x80

#: One scripted reply to a DiagnosticSessionControl request: an NRC byte, a
#: silence (``None``) or ``"ok"``; the script is consumed one entry per
#: request and answers positively once exhausted.
SessionReply = int | None | str


#: Mode 01 / Mode 06 support-bitmap group PIDs (SAE J1979 ``0x00, 0x20, …``).
SUPPORT_GROUPS: frozenset[int] = frozenset({0x00, 0x20, 0x40, 0x60, 0x80, 0xA0, 0xC0, 0xE0})
#: Arbitration id of the injected broadcast frames — outside the OBD response
#: range, so a receive path that filters on response ids must still tap them.
BROADCAST_ID = 0x280
ENGINE_ECU = 0x7E8


def support_bits(*ids: int, base: int = 0x00, more: bool = False) -> int:
    """A 32-bit J1979 support bitmask claiming ``ids`` (absolute) in group ``base``."""
    bits = 0
    for identifier in ids:
        offset = identifier - base - 1
        if not 0 <= offset < 32:
            raise ValueError(f"0x{identifier:02X} is not in group 0x{base:02X}")
        bits |= 1 << (31 - offset)
    return bits | (0x01 if more else 0)


def broadcast_sequence(msg: can.Message) -> int | None:
    """The sequence number carried by an injected broadcast frame, else ``None``."""
    if msg.arbitration_id != BROADCAST_ID:
        return None
    return int.from_bytes(bytes(msg.data)[:4], "big")


class ScriptedEcu:
    """A Mode 01 / Mode 22 ECU on the fake clock.

    Every request is answered ``latency`` seconds after it was sent (a
    multi-frame response spaces its consecutive frames ``frame_gap`` apart).
    ``batch_nrc`` refuses every multi-PID request with a negative response,
    ``answer_batches=False`` ignores them silently, and ``silent_pids`` never
    answer at all — the timeout, permanent-refusal and fallback cases the
    round accounting has to stay bounded under.

    ``session_gated`` models the session-exclusive ECM in data/dids.yaml
    (C04): Mode 22 is refused ``7F 22 80`` outside a non-default UDS session,
    Mode 01 is *silent* while one is held, the session expires after
    ``s3_seconds`` without diagnostic traffic, and after such a lapse Mode 01
    stays dead for ``lapse_blackout_seconds`` (measured >30 s on the Xterra;
    zero by default so tests choose the behaviour they pin). ``session_script``
    scripts the reply to each ``10 <sub>``; ``session_latency`` overrides the
    reply latency for session control alone.

    Discovery (C06): ``mode01_support`` / ``mode06_support`` map a support
    group (``0x00``, ``0x20``, …) to the 32-bit bitmask the engine ECU
    answers with (``None`` = the group is silent, a timeout);
    ``support_responders`` adds other ECUs' maps keyed by their response id
    (``0x7E9`` …) so a functional probe is answered by several modules;
    ``mode06_nrc`` refuses every Mode 06 support probe with that NRC;
    ``mode06_support_after_contact`` keeps Mode 06 support probes silent until
    a Mode 01 request has been answered. ``mode06_values`` answers a Mode 06
    MID read with ``46 <mid> <payload>``; with ``answer_freeze_frames`` Mode 02
    answers from ``values`` with the frame-number byte prepended (silent by
    default, as before).

    ``broadcast_per_request`` injects that many numbered broadcast frames
    (``BROADCAST_ID``, payload = sequence number) around every request's
    reply — one *before* the response lands and the rest inside the quiet
    window after it — so a receive path that discards non-response frames
    without tapping them is caught. Every frame ``recv`` returns is also
    appended to ``handed_out``: the fake bus never loses a frame upstream, so
    a frame in ``handed_out`` that never reached the tap was consumed
    untapped, not dropped by a kernel queue.
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
        session_gated: bool = False,
        session_script: dict[int, list[SessionReply]] | None = None,
        session_latency: float | None = None,
        s3_seconds: float = 5.0,
        lapse_blackout_seconds: float = 0.0,
        silent_dids: Iterable[int] = (),
        mode01_support: Mapping[int, int] | None = None,
        mode06_support: Mapping[int, int] | None = None,
        support_responders: Mapping[int, Mapping[int, int]] | None = None,
        mode06_nrc: int | None = None,
        mode06_support_after_contact: bool = False,
        mode06_values: Mapping[int, bytes] | None = None,
        answer_freeze_frames: bool = False,
        broadcast_per_request: int = 0,
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
        self.session_gated = session_gated
        self.session_script = {k: list(v) for k, v in (session_script or {}).items()}
        self.session_latency = latency if session_latency is None else session_latency
        self.s3_seconds = s3_seconds
        self.lapse_blackout_seconds = lapse_blackout_seconds
        self.silent_dids = frozenset(silent_dids)
        self.mode01_support = None if mode01_support is None else dict(mode01_support)
        self.mode06_support = None if mode06_support is None else dict(mode06_support)
        self.support_responders = {
            responder: dict(groups) for responder, groups in (support_responders or {}).items()
        }
        self.mode06_nrc = mode06_nrc
        self.mode06_support_after_contact = mode06_support_after_contact
        self.mode06_values = dict(mode06_values or {})
        self.answer_freeze_frames = answer_freeze_frames
        self.broadcast_per_request = broadcast_per_request
        self.broadcast_seq = 0
        self.mode01_answered = 0
        self.support_probes: list[tuple[float, int, int]] = []
        #: Every frame ``recv`` returned, in order — the fake never drops one.
        self.handed_out: list[can.Message] = []
        self.sent: list[tuple[float, can.Message]] = []
        self.recv_calls: list[float] = []
        self.batch_requests = 0
        self.single_requests = 0
        self.session_requests = 0
        #: ``(time, sub-function)`` of every accepted session change.
        self.session_changes: list[tuple[float, int]] = []
        self.lapses = 0
        self.on_send: list[object] = []
        self._pending: list[tuple[float, int, bytes]] = []
        self._session: int | None = None
        self._last_diag_at = 0.0
        self._blackout_until = 0.0

    @property
    def session(self) -> int | None:
        """The ECU's current non-default session, ``None`` when in default."""
        self._expire_session(self.clock.monotonic())
        return self._session

    def _expire_session(self, now: float) -> None:
        if self._session is not None and now - self._last_diag_at > self.s3_seconds:
            self._session = None
            self.lapses += 1
            self._blackout_until = now + self.lapse_blackout_seconds

    def _handle_session_control(self, sub: int, now: float) -> None:
        self.session_requests += 1
        self._last_diag_at = now
        script = self.session_script.get(sub)
        reply: SessionReply = "ok"
        if script:
            reply = script.pop(0)
        if reply is None:
            return
        if isinstance(reply, int):
            self._queue([bytes([0x03, 0x7F, 0x10, reply, 0, 0, 0, 0])], now, self.session_latency)
            return
        self._session = None if sub in DEFAULT_SESSION_SUBFUNCTIONS else sub
        self.session_changes.append((now, sub))
        self._queue([bytes([0x02, 0x50, sub, 0, 0, 0, 0, 0])], now, self.session_latency)

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
        self._inject_broadcasts(now)
        self._expire_session(now)
        if service == 0x10 and length >= 2:
            self._handle_session_control(data[2], now)
            return
        if service == 0x06 and length >= 2:
            self._handle_mode06(data[2], now)
            return
        if service == 0x02 and length >= 2:
            self._handle_mode02(data[2], now)
            return
        if service == 0x22 and length >= 3:
            self.single_requests += 1
            did = data[2:4]
            if self.session_gated and self._session is None:
                nrc = NRC_SERVICE_NOT_SUPPORTED_IN_SESSION
                self._queue([bytes([0x03, 0x7F, 0x22, nrc, 0, 0, 0, 0])], now)
                return
            self._last_diag_at = now
            if int.from_bytes(did, "big") in self.silent_dids:
                return
            self._queue([bytes([0x05, 0x62, *did, 0x12, 0x34, 0, 0])], now)
            return
        if service != 0x01:
            return
        if self.session_gated and (self._session is not None or now < self._blackout_until):
            # Mode 01 is silent, not refused, while the session is held (and
            # for the measured blackout after a lapse) — data/dids.yaml.
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
            if pid in SUPPORT_GROUPS:
                self.support_probes.append((now, 0x01, pid))
                self._answer_support(0x41, pid, self.mode01_support, now)
                return
            if pid in self.silent_pids:
                return
            if pid in self.nrc_pids:
                self._queue([bytes([0x03, 0x7F, 0x01, 0x12, 0, 0, 0, 0])], now)
                return
        body = bytearray([0x41])
        for pid in pids:
            if pid in self.values and pid not in self.silent_pids:
                body += bytes([pid]) + self.values[pid]
        self.mode01_answered += 1
        self._queue(isotp_frames(bytes(body)), now)

    def _handle_mode06(self, mid: int, now: float) -> None:
        self.single_requests += 1
        if mid in SUPPORT_GROUPS:
            self.support_probes.append((now, 0x06, mid))
            if self.mode06_nrc is not None:
                self._queue([bytes([0x03, 0x7F, 0x06, self.mode06_nrc, 0, 0, 0, 0])], now)
                return
            if self.mode06_support_after_contact and not self.mode01_answered:
                return
            self._answer_support(0x46, mid, self.mode06_support, now)
            return
        payload = self.mode06_values.get(mid)
        if payload is not None:
            self._queue(isotp_frames(bytes([0x46, mid]) + payload), now)

    def _handle_mode02(self, pid: int, now: float) -> None:
        self.single_requests += 1
        value = self.values.get(pid)
        if self.answer_freeze_frames and value is not None and pid not in self.silent_pids:
            self._queue(isotp_frames(bytes([0x42, pid, 0x00]) + value), now)

    def _answer_support(
        self, positive: int, group: int, engine: Mapping[int, int] | None, now: float
    ) -> None:
        """Every responder holding a map for ``group`` answers; others stay silent."""
        replies: list[tuple[int, int]] = []
        if engine is not None and group in engine:
            replies.append((ENGINE_ECU, engine[group]))
        for responder, groups in self.support_responders.items():
            if group in groups and positive == 0x41:
                replies.append((responder, groups[group]))
        for offset, (responder, bits) in enumerate(replies):
            frame = bytes([0x06, positive, group]) + bits.to_bytes(4, "big") + b"\x00"
            self._queue([frame], now, self.latency + offset * self.frame_gap, responder=responder)

    def _inject_broadcasts(self, sent_at: float) -> None:
        """Numbered broadcast frames around this request's reply (C06.1)."""
        for index in range(self.broadcast_per_request):
            self.broadcast_seq += 1
            data = self.broadcast_seq.to_bytes(4, "big") + bytes(4)
            # The first lands before the response, the rest inside the quiet
            # window that follows a support response (and during a timeout
            # wait when nothing answers at all).
            due = sent_at + (self.latency / 2 if index == 0 else self.latency + index * 0.004)
            self._insert(due, BROADCAST_ID, data)

    def _queue(
        self,
        frames: list[bytes],
        sent_at: float,
        latency: float | None = None,
        *,
        responder: int = ENGINE_ECU,
    ) -> None:
        due = sent_at + (self.latency if latency is None else latency)
        for frame in frames:
            self._insert(due, responder, frame)
            due += self.frame_gap

    def _insert(self, due: float, arbitration_id: int, frame: bytes) -> None:
        self._pending.append((due, arbitration_id, frame))
        self._pending.sort(key=lambda item: item[0])

    def recv(self, timeout: float | None = None) -> can.Message | None:
        wait = 0.0 if timeout is None else float(timeout)
        now = self.clock.monotonic()
        self.recv_calls.append(now)
        if self._pending and self._pending[0][0] <= now + wait:
            due, arbitration_id, frame = self._pending.pop(0)
            if due > now:
                self.clock.advance(due - now)
            msg = can.Message(
                arbitration_id=arbitration_id,
                data=frame,
                is_extended_id=False,
                timestamp=self.clock.time() if self.stamp_frames else 0.0,
            )
            self.handed_out.append(msg)
            return msg
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
