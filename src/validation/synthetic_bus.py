"""A paced synthetic bus for the C10 harness (``src/validation/scripted_ecu``).

:class:`SyntheticEcu` extends the fake-clock :class:`~src.validation.scripted_ecu.ScriptedEcu`
so the same scripted ECU that pins the C02/C04/C06 tests also runs on the
**real** clock: ``recv`` sleeps until the next due frame instead of advancing
a fake clock, so the actual collector thread, the asyncio fanout and the
Parquet writer all run against it unchanged.

What it adds:

* **Numbered broadcast frames on a timer** (``SYNTH_BROADCAST_ID``, payload =
  ``seq(4) rpm_raw(2) speed(1) 0``) at a configurable rate plus optional
  bursts. The sequence ledger is shared across bus instances so a reconnect
  keeps numbering, and every frame ``recv`` hands out is recorded — the set
  of *delivered* sequence numbers is what the Parquet must contain exactly
  once.
* **One signal model** for every source: the same ``rpm(t)`` is served as
  Mode 01 PID ``0x0C``, as bytes 4-5 of the broadcast frame and as DID
  ``0x1219`` — the C01 known-answer input.
* **Batched Mode 22** answers with catalogue widths (``data/dids.yaml``
  ``response_length``), session gating inherited from the base class.
* **An adapter fault** at a configured instant, so the reconnect path is
  exercised by a real exception from ``recv``.

The synthetic bindings (:func:`write_synthetic_bindings`) bind the broadcast
bytes as ``SYNTH_RPM`` (correct scale) and ``SYNTH_RPM_HALF`` (a deliberately
wrong scale) so the known-answer check has a known-different pair.
"""

from __future__ import annotations

import math
import time
from collections.abc import Iterable, Mapping
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import can
import yaml

from src.collection.obd_registry import lookup
from src.decode.dids import load_did_map
from src.paths import DIDS_YAML
from src.validation.scripted_ecu import (
    DEFAULT_VALUES,
    ENGINE_ECU,
    NRC_SERVICE_NOT_SUPPORTED_IN_SESSION,
    FakeClock,
    ScriptedEcu,
    isotp_frames,
    support_bits,
)

#: Arbitration id of the synthetic numbered broadcast. Chosen outside every id
#: bound in ``data/bindings.yaml`` / ``data/dbc`` so the vehicle catalogue
#: never decodes it and the harness's own bindings are the only reader.
SYNTH_BROADCAST_ID = 0x6A0
#: DID the signal model serves rpm on (``ECM_RPM`` in the catalogue, scale 1).
RPM_DID = 0x1219
DIDS_PATH = DIDS_YAML

#: PIDs the ECU answers, with the registry's payload widths asserted at
#: construction so a batched response can never be split off the wrong bytes.
ANSWERED_PIDS: tuple[int, ...] = (
    0x03,
    0x04,
    0x05,
    0x06,
    0x07,
    0x08,
    0x09,
    0x0C,
    0x0D,
    0x0E,
    0x0F,
    0x10,
    0x11,
    0x1F,
    0x24,
    0x2E,
    0x33,
    0x44,
)


class RealClock:
    """The ``FakeClock`` interface on the wall clock: ``advance`` sleeps."""

    def monotonic(self) -> float:
        return time.monotonic()

    def time(self) -> float:
        return time.time()

    def sleep(self, seconds: float) -> None:
        time.sleep(max(0.0, seconds))

    def advance(self, seconds: float) -> None:
        time.sleep(max(0.0, seconds))


Clock = RealClock | FakeClock


@dataclass(frozen=True)
class SignalModel:
    """A smooth, bounded rpm/speed trajectory shared by every service."""

    period_s: float = 8.0
    rpm_min: float = 800.0
    rpm_max: float = 2000.0
    kph_per_rpm: float = 1.0 / 25.0

    def rpm(self, t: float) -> float:
        phase = (1.0 - math.cos(2.0 * math.pi * t / self.period_s)) / 2.0
        return self.rpm_min + (self.rpm_max - self.rpm_min) * phase

    def rpm_raw(self, t: float) -> int:
        """Mode 01 ``0x0C`` encoding: ``(A*256+B)/4`` — quarter-rpm exact."""
        return int(round(self.rpm(t) * 4.0)) & 0xFFFF

    def speed_kph(self, t: float) -> float:
        return self.rpm(t) * self.kph_per_rpm

    def speed_raw(self, t: float) -> int:
        return int(round(self.speed_kph(t))) & 0xFF


@dataclass(frozen=True)
class BroadcastPlan:
    """Timer-driven broadcast load: a steady rate plus periodic bursts."""

    rate_hz: float = 100.0
    burst_size: int = 0
    burst_interval_s: float = 0.0


@dataclass
class BroadcastLedger:
    """Sequence numbers across every bus instance of one scenario."""

    origin_monotonic: float
    next_seq: int = 1
    generated: int = 0
    delivered: list[int] = field(default_factory=list)
    #: Broadcast frames queued in an adapter that faulted before handing them
    #: out — the synthetic analogue of a kernel/adapter receive drop.
    dropped_on_fault: int = 0

    @property
    def pending_at_stop(self) -> int:
        """Generated ahead of time but never due before the run ended: not on
        the wire, not a loss."""
        return self.generated - len(self.delivered) - self.dropped_on_fault

    def take(self) -> int:
        seq = self.next_seq
        self.next_seq += 1
        self.generated += 1
        return seq


def sequence_of_payload(payload: bytes) -> int | None:
    """Sequence number of a synthetic broadcast payload (first four bytes)."""
    if len(payload) < 4:
        return None
    return int.from_bytes(payload[:4], "big")


def catalogue_did_widths(path: Path = DIDS_PATH) -> dict[int, int]:
    """Full Mode 22 response width per DID: the provenance-backed
    ``response_length`` when recorded, else the decoded byte slice."""
    if not path.exists():
        return {}
    did_map = load_did_map(path)
    widths: dict[int, int] = {}
    for entry in did_map.dids:
        # ``response_length`` arrived with C09; a pre-C09 tree has only the
        # decoded slice, which is the width a single-DID answer needs anyway.
        recorded = getattr(entry, "response_length", None)
        widths[entry.did] = (
            int(recorded) if recorded is not None else entry.byte_start + entry.byte_length
        )
    return widths


def mode01_support_map(pids: Iterable[int] = ANSWERED_PIDS) -> dict[int, int]:
    """Support bitmaps claiming exactly ``pids``, continuation bits set."""
    groups: dict[int, list[int]] = {}
    for pid in pids:
        groups.setdefault((pid - 1) // 32 * 32, []).append(pid)
    highest = max(groups) if groups else 0
    return {
        base: support_bits(*members, base=base, more=base < highest)
        for base, members in sorted(groups.items())
    }


class SyntheticEcu(ScriptedEcu):
    """See the module docstring. ``**kwargs`` reach :class:`ScriptedEcu`."""

    def __init__(
        self,
        clock: Clock,
        *,
        ledger: BroadcastLedger,
        signal: SignalModel | None = None,
        broadcast: BroadcastPlan | None = None,
        did_widths: Mapping[int, int] | None = None,
        fault_at_elapsed_s: float | None = None,
        **kwargs: Any,
    ) -> None:
        values = dict(DEFAULT_VALUES)
        for pid in ANSWERED_PIDS:
            definition = lookup(0x01, pid)
            assert definition is not None, f"PID 0x{pid:02X} missing from the registry"
            values[pid] = bytes(definition.byte_length)
        kwargs.setdefault("values", values)
        kwargs.setdefault("broadcast_per_request", 0)
        super().__init__(clock, **kwargs)  # type: ignore[arg-type]
        self.ledger = ledger
        self.signal = signal if signal is not None else SignalModel()
        self.broadcast = broadcast if broadcast is not None else BroadcastPlan()
        self.did_widths = dict(did_widths) if did_widths is not None else catalogue_did_widths()
        self.fault_at_elapsed_s = fault_at_elapsed_s
        self.faulted = False
        self.did_single_requests = 0
        self.did_batch_requests = 0
        self.did_responses = 0
        now = clock.monotonic()
        self._next_broadcast_at = now
        self._next_burst_at = (
            now + self.broadcast.burst_interval_s if self.broadcast.burst_size > 0 else None
        )
        self.channel_info = "synthetic"

    # ------------------------------------------------------------------ time

    def elapsed(self, now: float | None = None) -> float:
        current = self.clock.monotonic() if now is None else now
        return current - self.ledger.origin_monotonic

    # ------------------------------------------------------------ broadcasts

    def _broadcast_payload(self, at: float) -> bytes:
        t = self.elapsed(at)
        return (
            self.ledger.take().to_bytes(4, "big")
            + self.signal.rpm_raw(t).to_bytes(2, "big")
            + bytes([self.signal.speed_raw(t), 0])
        )

    def _generate_broadcasts(self, until: float) -> None:
        if self.broadcast.rate_hz > 0:
            period = 1.0 / self.broadcast.rate_hz
            while self._next_broadcast_at <= until:
                at = self._next_broadcast_at
                self._insert(at, SYNTH_BROADCAST_ID, self._broadcast_payload(at))
                self._next_broadcast_at += period
        if self._next_burst_at is not None:
            while self._next_burst_at <= until:
                at = self._next_burst_at
                for index in range(self.broadcast.burst_size):
                    due = at + index * 1e-4
                    self._insert(due, SYNTH_BROADCAST_ID, self._broadcast_payload(due))
                self._next_burst_at += self.broadcast.burst_interval_s

    def recv(self, timeout: float | None = None) -> can.Message | None:
        now = self.clock.monotonic()
        if (
            self.fault_at_elapsed_s is not None
            and not self.faulted
            and self.elapsed(now) >= self.fault_at_elapsed_s
        ):
            self.faulted = True
            self.ledger.dropped_on_fault += sum(
                1
                for _due, arbitration_id, _frame in self._pending
                if arbitration_id == SYNTH_BROADCAST_ID
            )
            self._pending.clear()
            raise can.exceptions.CanOperationError("synthetic adapter fault (scripted)")
        wait = 0.0 if timeout is None else float(timeout)
        self._generate_broadcasts(now + wait)
        msg = super().recv(timeout)
        if msg is not None and msg.arbitration_id == SYNTH_BROADCAST_ID:
            seq = sequence_of_payload(bytes(msg.data))
            if seq is not None:
                self.ledger.delivered.append(seq)
        return msg

    # -------------------------------------------------------------- services

    def _refresh_values(self, now: float) -> None:
        t = self.elapsed(now)
        rpm = self.signal.rpm(t)
        frac = (rpm - self.signal.rpm_min) / max(1.0, self.signal.rpm_max - self.signal.rpm_min)
        wobble = int(400 * math.sin(2.0 * math.pi * t / 3.0))
        self.values[0x0C] = self.signal.rpm_raw(t).to_bytes(2, "big")
        self.values[0x0D] = bytes([self.signal.speed_raw(t)])
        self.values[0x04] = bytes([int(40 + 150 * frac)])
        self.values[0x11] = bytes([int(30 + 120 * frac)])
        self.values[0x10] = (int(rpm * 0.3) & 0xFFFF).to_bytes(2, "big")
        self.values[0x03] = b"\x02\x00"
        self.values[0x24] = (0x8000 + wobble).to_bytes(2, "big") + b"\x80\x00"
        self.values[0x44] = (0x8000 - wobble).to_bytes(2, "big")
        self.values[0x2E] = b"\x20"
        self.values[0x08] = b"\x80"
        self.values[0x09] = b"\x80"
        self.values[0x33] = b"\x00\x65"
        self.values[0x1F] = (int(t) & 0xFFFF).to_bytes(2, "big")
        # A refused PID is refused singly (NRC) and *omitted* from a batched
        # response, as a real ECU does; the base class only knows the former.
        for pid in self.nrc_pids:
            self.values.pop(pid, None)

    def _did_payload(self, did: int, now: float) -> bytes:
        width = self.did_widths.get(did, 2)
        if did == RPM_DID:
            return (
                (int(round(self.signal.rpm(self.elapsed(now)))) & 0xFFFF)
                .to_bytes(2, "big")[:width]
                .ljust(width, b"\x00")
            )
        base = (did * 37 + int(self.elapsed(now) * 10)) & 0xFF
        return bytes(((base + index) & 0xFF) for index in range(width))

    def _handle_mode22(self, data: bytes, now: float) -> None:
        length = data[0]
        count = max(1, (length - 1) // 2)
        dids = [int.from_bytes(data[2 + 2 * i : 4 + 2 * i], "big") for i in range(count)]
        if count > 1:
            self.did_batch_requests += 1
        else:
            self.did_single_requests += 1
        self._expire_session(now)
        if self.session_gated and self._session is None:
            nrc = NRC_SERVICE_NOT_SUPPORTED_IN_SESSION
            self._queue([bytes([0x03, 0x7F, 0x22, nrc, 0, 0, 0, 0])], now)
            return
        self._last_diag_at = now
        body = bytearray([0x62])
        for did in dids:
            if did in self.silent_dids:
                continue
            body += did.to_bytes(2, "big") + self._did_payload(did, now)
        if len(body) == 1:
            return
        self.did_responses += 1
        self._queue(isotp_frames(bytes(body)), now)

    def send(self, msg: can.Message, timeout: float | None = None) -> None:
        now = self.clock.monotonic()
        self._refresh_values(now)
        data = bytes(msg.data)
        if len(data) >= 4 and data[1] == 0x22:
            self.sent.append((now, msg))
            for hook in self.on_send:
                hook(msg)  # type: ignore[operator]
            self._handle_mode22(data, now)
            return
        super().send(msg, timeout)


def service_of(msg: can.Message) -> str:
    """Classify a sent frame by service for request accounting."""
    data = bytes(msg.data)
    if len(data) < 2:
        return "other"
    if data[0] & 0xF0 == 0x30:
        return "flow_control"
    service = data[1]
    names = {0x01: "mode01", 0x02: "mode02", 0x03: "mode03", 0x06: "mode06", 0x07: "mode07"}
    if service == 0x10:
        return "session_control"
    if service == 0x22:
        return "mode22_batched" if data[0] > 3 else "mode22"
    if service == 0x01:
        return "mode01_batched" if data[0] > 2 else "mode01"
    return names.get(service, f"service_0x{service:02X}")


def responder_of(msg: can.Message) -> str:
    if msg.arbitration_id == SYNTH_BROADCAST_ID:
        return "broadcast"
    if msg.arbitration_id == ENGINE_ECU:
        return "engine_ecu"
    return f"0x{msg.arbitration_id:X}"


SYNTH_BINDINGS: dict[str, Any] = {
    "schema_version": 1,
    "vehicle": {"make": "synthetic", "model": "bus", "year": 2026},
    "signals": [
        {
            "name": "SYNTH_RPM",
            "module": "synthetic",
            "can_id": SYNTH_BROADCAST_ID,
            "byte_start": 4,
            "byte_length": 2,
            "scale": 0.25,
            "offset": 0.0,
            "unit": "rpm",
            "confidence": "confirmed",
            "persist": True,
            "plausible_min": 0.0,
            "plausible_max": 8000.0,
            "notes": "Same quarter-rpm raw the ECU serves as Mode 01 0x0C.",
        },
        {
            "name": "SYNTH_RPM_HALF",
            "module": "synthetic",
            "can_id": SYNTH_BROADCAST_ID,
            "byte_start": 4,
            "byte_length": 2,
            "scale": 0.125,
            "offset": 0.0,
            "unit": "rpm",
            "confidence": "hypothesized",
            "persist": False,
            "notes": "Deliberately wrong scale: the known-different pair for C10.7.",
        },
        {
            "name": "SYNTH_SPEED",
            "module": "synthetic",
            "can_id": SYNTH_BROADCAST_ID,
            "byte_start": 6,
            "byte_length": 1,
            "scale": 1.0,
            "offset": 0.0,
            "unit": "kph",
            "confidence": "confirmed",
            "persist": True,
        },
    ],
}


def write_synthetic_bindings(path: Path) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(yaml.safe_dump(SYNTH_BINDINGS, sort_keys=False))
    return path


__all__ = [
    "ANSWERED_PIDS",
    "RPM_DID",
    "SYNTH_BINDINGS",
    "SYNTH_BROADCAST_ID",
    "BroadcastLedger",
    "BroadcastPlan",
    "RealClock",
    "SignalModel",
    "SyntheticEcu",
    "catalogue_did_widths",
    "mode01_support_map",
    "responder_of",
    "sequence_of_payload",
    "service_of",
    "write_synthetic_bindings",
]
