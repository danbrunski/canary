"""ISO 15765-4 OBD-II client over python-can (Canable-native, no ELM327).

The Canable can transmit, not just sniff. This module implements the
request side: send a service-N request frame on the OBD-II functional ID
(0x7DF), reassemble the ECU's ISO-TP response (single-frame for most
Mode 01 PIDs, multi-frame for VIN and DTC lists), and return the decoded
payload. Pairs with ``can_collector.collect_can`` which sniffs broadcast
frames. For continuous capture, ``can_obd_collector`` owns one Bus and
multiplexes broadcast frames plus diagnostic responses so slcan does not need
two competing serial handles.

Why this exists alongside ``obd_collector.py``: the ELM327 path remains for
emulator coverage and legacy captures, but the active hardware direction is
Canable-only. A Canable-native client runs on the hardware already on the bus,
polls at native CAN rate, and removes a layer of WSL-USB flakiness.

Scope of v1:

* **Mode 01** — single-frame requests for any standardized PID. Decoders
  for ~20 common PIDs (RPM, SPEED, COOLANT_TEMP, MAF, fuel trims, O2
  voltages, throttle, load, baro, voltage, timing, ambient temp, …)
  matching the SAE J1979 scaling.
* **Mode 01 PID 0x00 / 0x20 / 0x40 / 0x60 / 0x80** — supported-PID
  bitmasks. ``discover_supported_pids`` walks them to enumerate what the
  ECU claims.
* **Mode 03** — stored DTC list. Returns ``["P0123", ...]``.
* **Mode 09** — InfoType payloads (VIN, calibration ID). Multi-frame
  ISO-TP reassembly with a flow-control "clear to send" response.

Not in v1:

* Flow-control with non-zero block-size or separation-time. We always
  request CTS=0 (all frames) which works for the response sizes
  Mode 01 / 03 / 09 / 22 produce.
* DTCs from Mode 07 (pending) or Mode 0A (permanent) — same wire shape
  as Mode 03 so a one-line addition once needed.

Read-only by construction: no Mode 04 / Mode 0x2E / Mode 0x2F path
exists in this module. Tools that drive it can't accidentally send a
write because the request builder doesn't know how to construct one.
"""

from __future__ import annotations

import logging
import time
from collections.abc import Callable, Iterable
from dataclasses import dataclass
from typing import Any

import can

log = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

#: Standard OBD-II functional request ID (broadcast to all responder ECUs).
DEFAULT_REQUEST_ID = 0x7DF

#: Response IDs span 0x7E8–0x7EF (engine, transmission, etc.). Setting the
#: range with a min+max lets the client accept whichever ECU answered.
DEFAULT_RESPONSE_ID_MIN = 0x7E8
DEFAULT_RESPONSE_ID_MAX = 0x7EF

#: ISO-TP PCI nibble values, in the high nibble of byte 0 of each frame.
_PCI_SINGLE_FRAME = 0x0
_PCI_FIRST_FRAME = 0x1
_PCI_CONSECUTIVE_FRAME = 0x2
_PCI_FLOW_CONTROL = 0x3

#: Default ISO-TP flow-control: continue, block-size=0 (all frames), STmin=0.
_FLOW_CONTROL_CTS = bytes([0x30, 0x00, 0x00, 0x00, 0x00, 0x00, 0x00, 0x00])


@dataclass(frozen=True)
class ObdResponse:
    """One service-N reply, post-ISO-TP-reassembly.

    ``payload`` is the *service data only*: bytes after the positive-response
    service ID (e.g. ``0x41`` for Mode 01) and any echoed PID byte.
    Decoders consume it directly.
    """

    service: int  # 0x41 / 0x43 / 0x49 — service ID of the positive response
    pid: int | None  # PID echo (Mode 01/09 carry this); None for Mode 03
    payload: bytes
    responder_id: int  # which ECU answered (0x7E8 = engine, 0x7E9 = trans, …)


# ---------------------------------------------------------------------------
# Client
# ---------------------------------------------------------------------------


class CanObdClient:
    """Send OBD-II service-N requests on a python-can ``Bus``; collect responses.

    Constructed once per Canable session. Re-entrant *for sequential* calls
    only — the underlying ``Bus.recv`` is shared global state, so don't call
    ``query_*`` from two threads concurrently. Continuous capture uses
    ``can_obd_collector`` to multiplex raw-frame capture and diagnostic polling
    through one Bus.
    """

    def __init__(
        self,
        bus: can.BusABC,
        *,
        request_id: int = DEFAULT_REQUEST_ID,
        response_id_min: int = DEFAULT_RESPONSE_ID_MIN,
        response_id_max: int = DEFAULT_RESPONSE_ID_MAX,
        timeout_seconds: float = 1.0,
        time_fn: Callable[[], float] = time.monotonic,
    ) -> None:
        self._bus = bus
        self._request_id = request_id
        self._response_id_min = response_id_min
        self._response_id_max = response_id_max
        self._timeout = timeout_seconds
        self._time_fn = time_fn

    # ------------------------------------------------------------------
    # Public API: one method per service
    # ------------------------------------------------------------------

    def query_mode_01(self, pid: int) -> ObdResponse | None:
        """Mode 01 ShowCurrentData: standard PID."""
        return self._request(service=0x01, pid=pid, expect_pid_echo=True)

    def query_mode_03(self) -> ObdResponse | None:
        """Mode 03 RequestStoredDTC: ECU returns the active stored DTC list."""
        return self._request(service=0x03, pid=None, expect_pid_echo=False)

    def query_mode_09(self, info_type: int) -> ObdResponse | None:
        """Mode 09 VehicleInfo (VIN=0x02, calibration ID=0x04, ECU name=0x0A)."""
        return self._request(service=0x09, pid=info_type, expect_pid_echo=True)

    def query_mode_22(self, did: int, *, request_id: int | None = None) -> ObdResponse | None:
        """UDS Mode 22 ReadDataByIdentifier.

        ``did`` is the 16-bit data identifier (for example ``0x1101``).
        Most ECUs expect Mode 22 on a physical tester address such as
        ``0x7E0`` rather than the functional broadcast ``0x7DF`` used for
        generic Mode 01. Pass ``request_id=0x7E0`` for that common engine-ECU
        case; if omitted, the client's default request ID is used.
        """
        if not 0 <= did <= 0xFFFF:
            raise ValueError(f"DID must be a 16-bit integer, got 0x{did:X}")
        return self._request(
            service=0x22,
            pid=did,
            expect_pid_echo=True,
            request_id=request_id,
        )

    def enter_diagnostic_session(self, subfunction: int, *, request_id: int | None = None) -> bool:
        """UDS Service 0x10 DiagnosticSessionControl — enter a session.

        Returns True on a positive ``50 <subfunction>`` response. This is
        session *control* (no memory write, no actuation), but it is a
        precondition for manufacturer DID reads: e.g. the 2008 Nissan ECM
        gates Mode 22 behind a proprietary session (sub-function ``0xC0``)
        and refuses ``22`` with ``7F 22 80`` in the default session. The
        session also lapses after a few seconds, so callers must re-enter
        it periodically (TesterPresent ``0x3E`` is itself rejected on this
        ECM — re-sending ``10 <sf>`` is the keep-alive). Note: while a
        non-default session is active this ECM stops answering standard
        OBD Mode 01 — the two are mutually exclusive.
        """
        if not 0 <= subfunction <= 0xFF:
            raise ValueError(f"session sub-function must be a byte, got 0x{subfunction:X}")
        return (
            self._request(
                service=0x10,
                pid=subfunction,
                expect_pid_echo=True,
                request_id=request_id,
            )
            is not None
        )

    def query_supported_mode_01_pids(self) -> tuple[int, ...]:
        """Enumerate the Mode 01 PIDs responding ECUs claim to support.

        Walks PID 0x00 → 0x20 → 0x40 → 0x60 → 0x80 → 0xA0 → 0xC0, each of
        which is a 32-bit bitmask of the next 32 PIDs. Functional OBD
        requests can be answered by multiple modules, so each probe merges
        all responder bitmasks instead of trusting whichever ECU answered
        first. The walk continues while any responder sets the continuation
        marker, matching the SAE J1979 spec. Returns the sorted tuple of
        supported PID numbers.
        """
        supported: set[int] = set()
        for base in (0x00, 0x20, 0x40, 0x60, 0x80, 0xA0, 0xC0):
            responses = self._request_many(service=0x01, pid=base, expect_pid_echo=True)
            bitmasks = [
                int.from_bytes(resp.payload[:4], byteorder="big")
                for resp in responses
                if len(resp.payload) >= 4
            ]
            if not bitmasks:
                break
            for bits in bitmasks:
                for i in range(32):
                    if bits & (1 << (31 - i)):
                        supported.add(base + 1 + i)
            # The lowest bit of the bitmask indicates "more PIDs available
            # at base+32" — when no responder sets it, stop walking.
            if not any(bits & 0x01 for bits in bitmasks):
                break
        # The probe PIDs themselves (0x00 / 0x20 / …) aren't telemetry; drop them.
        for base in (0x00, 0x20, 0x40, 0x60, 0x80, 0xA0, 0xC0):
            supported.discard(base)
        return tuple(sorted(supported))

    # ------------------------------------------------------------------
    # Internal: ISO-TP framing
    # ------------------------------------------------------------------

    def _request(
        self,
        *,
        service: int,
        pid: int | None,
        expect_pid_echo: bool,
        request_id: int | None = None,
    ) -> ObdResponse | None:
        """Send a single-frame request and reassemble the response."""
        request_payload = self._build_request(service=service, pid=pid)
        message = can.Message(
            arbitration_id=self._request_id if request_id is None else request_id,
            data=request_payload,
            is_extended_id=False,
        )
        try:
            self._bus.send(message)
        except can.CanError:
            log.warning("CAN send failed", extra={"service": service, "pid": pid})
            return None

        return self._collect_response(
            service=service,
            pid_echo=pid if expect_pid_echo else None,
        )

    def _request_many(
        self,
        *,
        service: int,
        pid: int | None,
        expect_pid_echo: bool,
        request_id: int | None = None,
        quiet_seconds: float = 0.05,
    ) -> list[ObdResponse]:
        """Send a request and collect single-frame replies from all responders.

        Supported-PID probes are functional broadcasts, so more than one ECU
        may answer. Once the first relevant response arrives, keep listening
        for a short quiet window so slower responders are merged without
        waiting the full timeout on every successful probe.
        """
        request_payload = self._build_request(service=service, pid=pid)
        message = can.Message(
            arbitration_id=self._request_id if request_id is None else request_id,
            data=request_payload,
            is_extended_id=False,
        )
        try:
            self._bus.send(message)
        except can.CanError:
            log.warning("CAN send failed", extra={"service": service, "pid": pid})
            return []

        expected_service = 0x40 | service
        deadline = self._time_fn() + self._timeout
        quiet_deadline: float | None = None
        responses: dict[int, ObdResponse] = {}

        while True:
            now = self._time_fn()
            if now >= deadline or (quiet_deadline is not None and now >= quiet_deadline):
                break
            wait_until = deadline if quiet_deadline is None else min(deadline, quiet_deadline)
            frame = self._bus.recv(timeout=max(0.001, wait_until - now))
            if frame is None:
                continue
            if not (self._response_id_min <= frame.arbitration_id <= self._response_id_max):
                continue
            data = bytes(frame.data)
            if not data:
                continue
            pci_high = (data[0] >> 4) & 0x0F
            if pci_high != _PCI_SINGLE_FRAME:
                continue
            length = data[0] & 0x0F
            body = data[1 : 1 + length] if 1 <= length <= 7 and len(data) >= 1 + length else b""
            if len(body) >= 3 and body[0] == 0x7F and body[1] == service:
                quiet_deadline = self._time_fn() + quiet_seconds
                continue
            payload = self._handle_single_frame(
                data, expected_service, pid if expect_pid_echo else None
            )
            if payload is None:
                continue
            responder = int(frame.arbitration_id)
            responses[responder] = ObdResponse(
                service=expected_service,
                pid=pid if expect_pid_echo else None,
                payload=payload,
                responder_id=responder,
            )
            quiet_deadline = self._time_fn() + quiet_seconds

        return list(responses.values())

    def _build_request(self, *, service: int, pid: int | None) -> bytes:
        """Single-frame request body. Padded to 8 bytes with 0x00."""
        if pid is None:
            body = bytes([0x01, service])  # PCI=0x01 (1 data byte), service
        elif 0 <= pid <= 0xFF:
            body = bytes([0x02, service, pid])  # PCI=0x02 (2 data bytes)
        elif 0 <= pid <= 0xFFFF:
            body = bytes([0x03, service, (pid >> 8) & 0xFF, pid & 0xFF])
        else:
            raise ValueError(f"PID/DID identifier out of range: 0x{pid:X}")
        return body + bytes(8 - len(body))

    def _collect_response(
        self,
        *,
        service: int,
        pid_echo: int | None,
    ) -> ObdResponse | None:
        """Read frames until a matching response is assembled or we time out."""
        expected_service = 0x40 | service
        deadline = self._time_fn() + self._timeout
        # Per-responder ISO-TP reassembly buffer keyed by responder ID, since
        # multiple ECUs can answer a 0x7DF broadcast in quick succession.
        pending: dict[int, _ReassemblyBuffer] = {}

        while self._time_fn() < deadline:
            remaining = max(0.001, deadline - self._time_fn())
            frame = self._bus.recv(timeout=remaining)
            if frame is None:
                continue
            if not (self._response_id_min <= frame.arbitration_id <= self._response_id_max):
                continue
            data = bytes(frame.data)
            if not data:
                continue
            pci_high = (data[0] >> 4) & 0x0F
            responder = frame.arbitration_id

            if pci_high == _PCI_SINGLE_FRAME:
                length = data[0] & 0x0F
                body = data[1 : 1 + length] if 1 <= length <= 7 and len(data) >= 1 + length else b""
                # Definitive negative response to *our* service (7F SS NRC):
                # stop now instead of waiting out the timeout. Critical for
                # DID sweeps — an absent DID otherwise costs a full timeout
                # each (hours over a 0x0000–0xFFFF range). Same return value
                # (None) as before, just immediate.
                if len(body) >= 3 and body[0] == 0x7F and body[1] == service:
                    return None
                payload = self._handle_single_frame(data, expected_service, pid_echo)
                if payload is None:
                    continue
                return ObdResponse(
                    service=expected_service,
                    pid=pid_echo,
                    payload=payload,
                    responder_id=responder,
                )
            if pci_high == _PCI_FIRST_FRAME:
                total_len, first_bytes = _parse_first_frame(data)
                if first_bytes is None:
                    continue
                pending[responder] = _ReassemblyBuffer(
                    total_length=total_len,
                    received=bytearray(first_bytes),
                    next_sequence=1,
                )
                # Send flow control: continue, block-size=0, STmin=0.
                # ECUs typically address flow control to the requester's
                # functional address, but since we sent on 0x7DF and the
                # responder picked its own ID, we reply on the responder's
                # tester-address (one less for engine, etc.). Standard is
                # responder_id - 8 (e.g. 0x7E8 → 0x7E0). The Xterra's engine
                # ECU expects 0x7E0.
                tester_id = responder - 8
                try:
                    self._bus.send(
                        can.Message(
                            arbitration_id=tester_id,
                            data=_FLOW_CONTROL_CTS,
                            is_extended_id=False,
                        )
                    )
                except can.CanError:
                    log.warning("CAN flow-control send failed", extra={"responder": responder})
                continue
            if pci_high == _PCI_CONSECUTIVE_FRAME:
                buf = pending.get(responder)
                if buf is None:
                    continue  # consecutive frame for a transfer we didn't start
                sequence = data[0] & 0x0F
                expected_sequence = buf.next_sequence & 0x0F
                if sequence != expected_sequence:
                    log.debug(
                        "isotp sequence mismatch; dropping in-progress transfer",
                        extra={"got": sequence, "expected": expected_sequence},
                    )
                    pending.pop(responder, None)
                    continue
                remaining_bytes = buf.total_length - len(buf.received)
                take = min(7, remaining_bytes)
                buf.received.extend(data[1 : 1 + take])
                buf.next_sequence += 1
                if len(buf.received) >= buf.total_length:
                    payload = bytes(buf.received[: buf.total_length])
                    pending.pop(responder, None)
                    decoded = self._extract_service_payload(payload, expected_service, pid_echo)
                    if decoded is None:
                        continue
                    return ObdResponse(
                        service=expected_service,
                        pid=pid_echo,
                        payload=decoded,
                        responder_id=responder,
                    )
                continue
            # Flow-control frames from another transfer are not ours; ignore.
        return None

    @staticmethod
    def _handle_single_frame(
        data: bytes, expected_service: int, pid_echo: int | None
    ) -> bytes | None:
        length = data[0] & 0x0F
        if length == 0 or length > 7 or len(data) < 1 + length:
            return None
        body = data[1 : 1 + length]
        return CanObdClient._extract_service_payload(body, expected_service, pid_echo)

    @staticmethod
    def _extract_service_payload(
        body: bytes, expected_service: int, pid_echo: int | None
    ) -> bytes | None:
        if not body or body[0] != expected_service:
            return None
        offset = 1
        if pid_echo is not None:
            if 0 <= pid_echo <= 0xFF:
                if len(body) < 2 or body[1] != pid_echo:
                    return None
                offset = 2
            elif 0 <= pid_echo <= 0xFFFF:
                if len(body) < 3 or body[1] != (pid_echo >> 8) & 0xFF or body[2] != pid_echo & 0xFF:
                    return None
                offset = 3
            else:
                return None
        return body[offset:]


@dataclass
class _ReassemblyBuffer:
    total_length: int
    received: bytearray
    next_sequence: int


def _parse_first_frame(data: bytes) -> tuple[int, bytes | None]:
    """Pull total length + the first 6 payload bytes out of an ISO-TP first frame."""
    if len(data) < 2:
        return 0, None
    total_length = ((data[0] & 0x0F) << 8) | data[1]
    if total_length == 0:
        return 0, None
    return total_length, bytes(data[2:8])


# ---------------------------------------------------------------------------
# PID decoders — SAE J1979 standardized formulas.
# Each takes the response payload (bytes after `41 PID`) and returns
# (value, unit). Decoders match python-obd's output for the same PIDs.
# Where the PID returns a single byte and the formula is "A−40" or
# "A * 100 / 255", we keep it explicit for readability over micro-DRY.
# ---------------------------------------------------------------------------


def _need(data: bytes, n: int) -> bool:
    return len(data) >= n


def decode_rpm(data: bytes) -> tuple[float, str] | None:
    if not _need(data, 2):
        return None
    return ((data[0] * 256 + data[1]) / 4.0, "rpm")


def decode_speed(data: bytes) -> tuple[float, str] | None:
    if not _need(data, 1):
        return None
    return (float(data[0]), "kph")


def decode_temp_minus_40(data: bytes) -> tuple[float, str] | None:
    """COOLANT_TEMP / INTAKE_TEMP / AMBIANT_AIR_TEMP / OIL_TEMP."""
    if not _need(data, 1):
        return None
    return (float(data[0] - 40), "degC")


def decode_percent_a_over_255(data: bytes) -> tuple[float, str] | None:
    """THROTTLE_POS / ENGINE_LOAD / RELATIVE_THROTTLE_POS / FUEL_LEVEL / etc."""
    if not _need(data, 1):
        return None
    return (data[0] * 100.0 / 255.0, "%")


def decode_percent_b_minus_128(data: bytes) -> tuple[float, str] | None:
    """SHORT_FUEL_TRIM / LONG_FUEL_TRIM, both banks. Range −100% to +99.2%."""
    if not _need(data, 1):
        return None
    return ((data[0] - 128) * 100.0 / 128.0, "%")


def decode_maf(data: bytes) -> tuple[float, str] | None:
    if not _need(data, 2):
        return None
    return ((data[0] * 256 + data[1]) / 100.0, "g/s")


def decode_intake_pressure(data: bytes) -> tuple[float, str] | None:
    if not _need(data, 1):
        return None
    return (float(data[0]), "kPa")


def decode_baro(data: bytes) -> tuple[float, str] | None:
    if not _need(data, 1):
        return None
    return (float(data[0]), "kPa")


def decode_timing_advance(data: bytes) -> tuple[float, str] | None:
    if not _need(data, 1):
        return None
    return (data[0] / 2.0 - 64.0, "degBTDC")


def decode_o2_voltage(data: bytes) -> tuple[float, str] | None:
    """Narrowband O2 voltage; byte B is short-term fuel trim if present."""
    if not _need(data, 1):
        return None
    return (data[0] / 200.0, "V")


def decode_control_module_voltage(data: bytes) -> tuple[float, str] | None:
    if not _need(data, 2):
        return None
    return ((data[0] * 256 + data[1]) / 1000.0, "V")


def decode_run_time(data: bytes) -> tuple[float, str] | None:
    if not _need(data, 2):
        return None
    return (float(data[0] * 256 + data[1]), "s")


def decode_distance_km(data: bytes) -> tuple[float, str] | None:
    if not _need(data, 2):
        return None
    return (float(data[0] * 256 + data[1]), "km")


def decode_warmups(data: bytes) -> tuple[float, str] | None:
    if not _need(data, 1):
        return None
    return (float(data[0]), "count")


def decode_commanded_lambda(data: bytes) -> tuple[float, str] | None:
    """COMMANDED_EQUIV_RATIO: λ = (A*256+B) / 32768. λ=1.0 is stoichiometric."""
    if not _need(data, 2):
        return None
    return ((data[0] * 256 + data[1]) / 32768.0, "ratio")


def decode_fuel_status(data: bytes) -> tuple[float, str] | None:
    """PID 0x03 fuel-system status.

    SAE J1979 returns two bitmask bytes. The first byte carries the primary
    fuel-system state for vehicles with one bank; preserving it as a numeric
    bitmask keeps the channel model-friendly while avoiding a vehicle-specific
    enum interpretation in the collector.
    """
    if not _need(data, 1):
        return None
    return (float(data[0]), "bitmask")


def decode_evap_vapor_pressure_signed(data: bytes) -> tuple[float, str] | None:
    """PID 0x32: signed 16-bit / 4 Pa. Negative = vacuum."""
    if not _need(data, 2):
        return None
    raw = int.from_bytes(data[:2], byteorder="big", signed=True)
    return (raw / 4.0, "Pa")


def decode_egr_percent(data: bytes) -> tuple[float, str] | None:
    """COMMANDED_EGR: A * 100/255."""
    return decode_percent_a_over_255(data)


def decode_egr_error(data: bytes) -> tuple[float, str] | None:
    """EGR_ERROR: (A − 128) * 100/128. Range −100%..+99.2%."""
    return decode_percent_b_minus_128(data)


def decode_evap_purge(data: bytes) -> tuple[float, str] | None:
    return decode_percent_a_over_255(data)


def decode_catalyst_temp(data: bytes) -> tuple[float, str] | None:
    """CATALYST_TEMP_*: ((A*256+B) / 10) − 40. Often unsupported on US-spec
    light vehicles even when claimed."""
    if not _need(data, 2):
        return None
    return ((data[0] * 256 + data[1]) / 10.0 - 40.0, "degC")


#: Registry: PID number → (name, decoder). Drives both the
#: ``read_supported`` flow and any caller that wants to translate a numeric
#: PID into a named telemetry channel.
PID_DECODERS: dict[int, tuple[str, Callable[[bytes], tuple[float, str] | None]]] = {
    0x03: ("FUEL_STATUS", decode_fuel_status),
    0x04: ("ENGINE_LOAD", decode_percent_a_over_255),
    0x05: ("COOLANT_TEMP", decode_temp_minus_40),
    0x06: ("SHORT_FUEL_TRIM_1", decode_percent_b_minus_128),
    0x07: ("LONG_FUEL_TRIM_1", decode_percent_b_minus_128),
    0x08: ("SHORT_FUEL_TRIM_2", decode_percent_b_minus_128),
    0x09: ("LONG_FUEL_TRIM_2", decode_percent_b_minus_128),
    0x0B: ("INTAKE_PRESSURE", decode_intake_pressure),
    0x0C: ("RPM", decode_rpm),
    0x0D: ("SPEED", decode_speed),
    0x0E: ("TIMING_ADVANCE", decode_timing_advance),
    0x0F: ("INTAKE_TEMP", decode_temp_minus_40),
    0x10: ("MAF", decode_maf),
    0x11: ("THROTTLE_POS", decode_percent_a_over_255),
    0x14: ("O2_B1S1", decode_o2_voltage),
    0x15: ("O2_B1S2", decode_o2_voltage),
    0x18: ("O2_B2S1", decode_o2_voltage),
    0x19: ("O2_B2S2", decode_o2_voltage),
    0x1F: ("RUN_TIME", decode_run_time),
    0x21: ("DISTANCE_W_MIL", decode_distance_km),
    0x2C: ("COMMANDED_EGR", decode_egr_percent),
    0x2D: ("EGR_ERROR", decode_egr_error),
    0x2E: ("EVAPORATIVE_PURGE", decode_evap_purge),
    0x2F: ("FUEL_LEVEL", decode_percent_a_over_255),
    0x30: ("WARMUPS_SINCE_DTC_CLEAR", decode_warmups),
    0x31: ("DISTANCE_SINCE_DTC_CLEAR", decode_distance_km),
    0x32: ("EVAP_VAPOR_PRESSURE", decode_evap_vapor_pressure_signed),
    0x33: ("BAROMETRIC_PRESSURE", decode_baro),
    0x3C: ("CATALYST_TEMP_B1S1", decode_catalyst_temp),
    0x3D: ("CATALYST_TEMP_B2S1", decode_catalyst_temp),
    0x3E: ("CATALYST_TEMP_B1S2", decode_catalyst_temp),
    0x3F: ("CATALYST_TEMP_B2S2", decode_catalyst_temp),
    0x42: ("CONTROL_MODULE_VOLTAGE", decode_control_module_voltage),
    0x43: ("ABSOLUTE_LOAD", decode_percent_a_over_255),
    0x44: ("COMMANDED_EQUIV_RATIO", decode_commanded_lambda),
    0x45: ("RELATIVE_THROTTLE_POS", decode_percent_a_over_255),
    0x46: ("AMBIANT_AIR_TEMP", decode_temp_minus_40),
    0x5C: ("OIL_TEMP", decode_temp_minus_40),
}


# ---------------------------------------------------------------------------
# DTC parsing (Mode 03 / 07 share the same on-wire shape)
# ---------------------------------------------------------------------------


def parse_dtc_payload(data: bytes) -> list[str]:
    """Parse a Mode 03 (or Mode 07) payload into ``"P0123"``-style codes.

    Encoding: count byte, then N × 2 bytes per DTC. Each DTC's high nibble
    of byte 0 encodes the system letter (P/C/B/U). The rest is hex. Trailing
    null DTCs (0x0000) are filler — dropped.
    """
    if not data:
        return []
    count = data[0]
    dtcs: list[str] = []
    # The count byte is the *number* of DTCs reported by the ECU; the payload
    # carries ``count * 2`` more bytes. Some ECUs only emit the DTC bytes
    # without the count prefix in single-frame replies (their `length` is 1+
    # already accounted for), so be forgiving: scan in 2-byte pairs.
    body = data[1 : 1 + count * 2] if 1 + count * 2 <= len(data) else data
    for i in range(0, len(body) - 1, 2):
        b0, b1 = body[i], body[i + 1]
        if b0 == 0 and b1 == 0:
            continue
        system = "PCBU"[(b0 >> 6) & 0x03]
        first_digit = (b0 >> 4) & 0x03
        second_digit = b0 & 0x0F
        third_fourth = b1
        dtcs.append(f"{system}{first_digit}{second_digit:X}{third_fourth:02X}")
    return dtcs


# ---------------------------------------------------------------------------
# Reading + decoding loop
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class DecodedPid:
    pid: int
    name: str
    value: float
    unit: str
    responder_id: int


def read_pid(client: CanObdClient, pid: int) -> DecodedPid | None:
    """Convenience: query a Mode 01 PID and apply the registry's decoder.

    Returns ``None`` if the ECU didn't answer OR no decoder is registered
    OR the response was too short. Unknown PIDs (not in ``PID_DECODERS``)
    return None; the caller decides whether to log or surface raw bytes.
    """
    resp = client.query_mode_01(pid)
    if resp is None:
        return None
    entry = PID_DECODERS.get(pid)
    if entry is None:
        return None
    name, decoder = entry
    result = decoder(resp.payload)
    if result is None:
        return None
    value, unit = result
    return DecodedPid(pid=pid, name=name, value=value, unit=unit, responder_id=resp.responder_id)


def read_pids(client: CanObdClient, pids: Iterable[int]) -> list[DecodedPid]:
    """Sequentially query a set of Mode 01 PIDs; return decoded results in order."""
    out: list[DecodedPid] = []
    for pid in pids:
        decoded = read_pid(client, pid)
        if decoded is not None:
            out.append(decoded)
    return out


__all__ = [
    "DEFAULT_REQUEST_ID",
    "DEFAULT_RESPONSE_ID_MAX",
    "DEFAULT_RESPONSE_ID_MIN",
    "PID_DECODERS",
    "CanObdClient",
    "DecodedPid",
    "ObdResponse",
    "decode_baro",
    "decode_catalyst_temp",
    "decode_commanded_lambda",
    "decode_control_module_voltage",
    "decode_distance_km",
    "decode_egr_error",
    "decode_egr_percent",
    "decode_evap_purge",
    "decode_evap_vapor_pressure_signed",
    "decode_fuel_status",
    "decode_intake_pressure",
    "decode_maf",
    "decode_o2_voltage",
    "decode_percent_a_over_255",
    "decode_percent_b_minus_128",
    "decode_rpm",
    "decode_run_time",
    "decode_speed",
    "decode_temp_minus_40",
    "decode_timing_advance",
    "decode_warmups",
    "parse_dtc_payload",
    "read_pid",
    "read_pids",
]


# A useful Any-typed re-export so callers don't need to import can themselves
# just to declare a bus parameter type. The Protocol is documented behavior
# rather than the python-can class hierarchy.
BusLike = Any
