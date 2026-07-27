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
* **Mode 03 / 07 / 0A** — stored / pending / permanent DTC lists (same
  wire shape). Returns ``["P0123", ...]``.
* **Mode 09** — InfoType payloads (VIN, calibration ID). Multi-frame
  ISO-TP reassembly with a flow-control "clear to send" response.

Not in v1:

* Flow-control with non-zero block-size or separation-time. We always
  request CTS=0 (all frames) which works for the response sizes
  Mode 01 / 03 / 07 / 0A / 09 / 22 produce.

Read-only by construction: no Mode 04 / Mode 0x2E / Mode 0x2F path
exists in this module. Tools that drive it can't accidentally send a
write because the request builder doesn't know how to construct one.
"""

from __future__ import annotations

import logging
import time
from collections.abc import Callable, Iterable, Sequence
from dataclasses import dataclass
from typing import Any

import can

from src.collection import tx_gate
from src.collection.isotp import (
    DEFAULT_REQUEST_ID,
    DEFAULT_RESPONSE_ID_MAX,
    DEFAULT_RESPONSE_ID_MIN,
    FLOW_CONTROL_CTS,
    PCI_SINGLE_FRAME,
    IsoTpResponseAssembler,
    ObdResponse,
    build_batched_request_payload,
    build_request_payload,
    extract_service_payload,
    handle_single_frame,
    is_negative_response,
    pci_type,
    positive_response_service,
    split_batched_response,
)

log = logging.getLogger(__name__)


@dataclass(frozen=True)
class BatchedMode01Response:
    """One multi-PID Mode 01 response, split back into per-PID payloads."""

    #: ``{pid: payload}`` for every PID actually present in the response.
    #: Keyed off what came back rather than what was asked for -- an ECU may
    #: omit a PID it does not support instead of rejecting the whole batch.
    readings: dict[int, bytes]
    #: False when the payload walk stopped early (an unknown PID whose width
    #: we cannot know, or a truncated payload). Entries collected before that
    #: point are still trustworthy; nothing after it would have been.
    clean: bool
    responder_id: int


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

    def query_mode_01_batch(
        self,
        pids: Sequence[int],
        *,
        byte_length_for: Callable[[int], int | None],
    ) -> BatchedMode01Response | None:
        """Mode 01 ShowCurrentData for several PIDs in one round trip.

        SAE J1979 permits up to six PIDs per request; the response carries a
        ``<pid> <data…>`` pair for each. On this bus the cost is the round
        trip rather than the bytes, so batching is worth roughly 5x — measured
        67.7 batched requests/sec against 85.1 single-PID, but each batched
        one returns six channels.

        ``byte_length_for`` supplies each PID's payload width. It is injected
        rather than imported because ``obd_registry`` imports *this* module,
        so reaching for it here would be a cycle. Callers that already depend
        on the registry pass a lookup against it.

        Returns ``None`` when nothing came back (timeout, or transmission
        suppressed by passive mode) — the same contract as
        :meth:`query_mode_01`.
        """
        payload = build_batched_request_payload(0x01, pids)
        message = can.Message(
            arbitration_id=self._request_id,
            data=payload,
            is_extended_id=False,
        )
        try:
            if not tx_gate.send(self._bus, message, what=f"batch {len(pids)} PIDs"):
                return None
        except can.CanError:
            log.warning("CAN send failed", extra={"service": 0x01, "batch_size": len(pids)})
            return None

        # pid_echo=None: the payload is a stream of pairs, not one identifier,
        # so there is no single echo to match against.
        response = self._collect_response(service=0x01, pid_echo=None)
        if response is None:
            return None
        readings, clean = split_batched_response(response.payload, byte_length_for)
        return BatchedMode01Response(
            readings=readings,
            clean=clean,
            responder_id=response.responder_id,
        )

    def query_mode_03(self) -> ObdResponse | None:
        """Mode 03 RequestStoredDTC: ECU returns the active stored DTC list."""
        return self._request(service=0x03, pid=None, expect_pid_echo=False)

    def query_mode_07(self) -> ObdResponse | None:
        """Mode 07 RequestPendingDTC: DTCs detected during current/last cycle.

        Same wire shape as Mode 03 (``parse_dtc_payload`` handles both) — a
        code here hasn't confirmed to a stored DTC yet.
        """
        return self._request(service=0x07, pid=None, expect_pid_echo=False)

    def query_mode_0A(self) -> ObdResponse | None:  # noqa: N802 - "0A" is the mode number, not a word
        """Mode 0A RequestPermanentDTC: confirmed DTCs that survive a clear
        until their own drive-cycle criteria re-pass.

        Same wire shape as Mode 03/07.
        """
        return self._request(service=0x0A, pid=None, expect_pid_echo=False)

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
            if not tx_gate.send(self._bus, message, what=f"request service=0x{service:02X}"):
                return None
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
            if not tx_gate.send(self._bus, message, what=f"probe service=0x{service:02X}"):
                return []
        except can.CanError:
            log.warning("CAN send failed", extra={"service": service, "pid": pid})
            return []

        expected_service = positive_response_service(service)
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
            if pci_type(data) != PCI_SINGLE_FRAME:
                continue
            if is_negative_response(data, service):
                quiet_deadline = self._time_fn() + quiet_seconds
                continue
            payload = handle_single_frame(data, expected_service, pid if expect_pid_echo else None)
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
        return build_request_payload(service, pid)

    def _collect_response(
        self,
        *,
        service: int,
        pid_echo: int | None,
    ) -> ObdResponse | None:
        """Read frames until a matching response is assembled or we time out."""
        deadline = self._time_fn() + self._timeout
        assembler = IsoTpResponseAssembler(service=service, identifier=pid_echo)

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
            responder = int(frame.arbitration_id)
            result = assembler.accept(responder, data)
            if result.negative_response:
                # Definitive negative response to *our* service (7F SS NRC):
                # stop now instead of waiting out the timeout. Critical for
                # DID sweeps — an absent DID otherwise costs a full timeout
                # each (hours over a 0x0000–0xFFFF range). Same return value
                # (None) as before, just immediate.
                return None
            if result.flow_control_id is not None:
                # Send flow control: continue, block-size=0, STmin=0.
                # ECUs typically address flow control to the requester's
                # functional address, but since we sent on 0x7DF and the
                # responder picked its own ID, we reply on the responder's
                # tester-address (one less for engine, etc.). Standard is
                # responder_id - 8 (e.g. 0x7E8 → 0x7E0). The Xterra's engine
                # ECU expects 0x7E0.
                try:
                    tx_gate.send(
                        self._bus,
                        can.Message(
                            arbitration_id=result.flow_control_id,
                            data=FLOW_CONTROL_CTS,
                            is_extended_id=False,
                        ),
                        what="flow-control",
                    )
                except can.CanError:
                    log.warning("CAN flow-control send failed", extra={"responder": responder})
                continue
            if result.response is not None:
                return result.response
            # Flow-control frames from another transfer are not ours; ignore.
        return None

    @staticmethod
    def _handle_single_frame(
        data: bytes, expected_service: int, pid_echo: int | None
    ) -> bytes | None:
        return handle_single_frame(data, expected_service, pid_echo)

    @staticmethod
    def _extract_service_payload(
        body: bytes, expected_service: int, pid_echo: int | None
    ) -> bytes | None:
        return extract_service_payload(body, expected_service, pid_echo)


# ---------------------------------------------------------------------------
# PID decoders — SAE J1979 standardized formulas.
# Each takes the response payload (bytes after `41 PID`) and returns
# (value, unit). Decoders match python-obd's output for the same PIDs.
# Where the PID returns a single byte and the formula is "A−40" or
# "A * 100 / 255", we keep it explicit for readability over micro-DRY.
# ---------------------------------------------------------------------------


def _need(data: bytes, n: int) -> bool:
    return len(data) >= n


def decode_monitor_status(data: bytes) -> tuple[float, str] | None:
    """PID 0x01 / 0x41: bit 7 of A = MIL on; bits 0-6 of A = DTC count.

    Returns just the count — numeric and model-friendly for the continuous
    slow-tier telemetry channel (Task 1.2). The MIL bit is decoded
    separately by :func:`decode_mil_on` for the periodic DTC poll (Task
    1.4), not folded into this channel's value.
    """
    if not _need(data, 1):
        return None
    return (float(data[0] & 0x7F), "count")


def decode_mil_on(data: bytes) -> bool | None:
    """PID 0x01 / 0x41, bit 7 of byte A: MIL (check-engine light) on/off.

    Reads the same byte as :func:`decode_monitor_status` but returns the bit
    ``decode_monitor_status`` deliberately drops — used when assembling a
    ``DtcReport`` (Task 1.4), not as a telemetry channel.
    """
    if not _need(data, 1):
        return None
    return bool(data[0] & 0x80)


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


#: Raw 0xFF on PID 0x0E is the ECU's "not available" sentinel, not a reading.
#: It decodes to exactly 63.5 degBTDC — the top of the A/2-64 scale — which is
#: not a physically achievable spark advance on a spark-ignition engine (this
#: one idles at ~17 and peaks around 40).
TIMING_ADVANCE_UNAVAILABLE = 0xFF


def decode_timing_advance(data: bytes) -> tuple[float, str] | None:
    """Mode 01 PID 0x0E — spark advance, A/2 - 64.

    Returns None for the 0xFF sentinel. Spark advance is undefined with no
    combustion, and this ECU reports 0xFF whenever the engine is not running:
    measured across four sessions on 2026-07-26, the value was 63.5 in 1012 of
    1012 samples taken at RPM == 0 and in 0 of 683 taken while running. No
    other polled PID behaves this way — ENGINE_LOAD reports a true 0.0,
    CONTROL_MODULE_VOLTAGE a true 11.9 V, INTAKE_TEMP a genuinely heat-soaked
    62 degC — so the sentinel handling belongs here rather than in a shared
    engine-state gate.

    Storing it as a number was actively harmful: `2026-07-22T154954` is 100%
    sentinel (engine never ran) and is in the deployed isolation forest's
    training set, so the model learned a constant. Emitting None drops the
    reading like any other unavailable PID instead of inventing a max-scale
    one, and downstream gap handling already copes with an absent sample.
    """
    if not _need(data, 1):
        return None
    if data[0] == TIMING_ADVANCE_UNAVAILABLE:
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


def decode_wide_lambda(data: bytes) -> tuple[float, str] | None:
    """PIDs 0x24-0x2B / 0x34-0x3B: wide-range O2 (Task 2.1).

    Four bytes: ``AB`` = equivalence ratio λ ``(A*256+B)/32768``, ``CD`` =
    sensor voltage or current. λ is the model-relevant channel; the raw
    voltage/current goes unused. Single-sourced here (not ``obd_registry.py``)
    because bank-1 sensor 1/2 (0x24/0x25/0x34/0x35) are now polled directly
    via ``PID_DECODERS``; the remaining wide-lambda PIDs still import this
    from here for their unpolled-but-decodable registry entries.
    """
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


def decode_absolute_load(data: bytes) -> tuple[float, str] | None:
    """ABSOLUTE_LOAD (PID 0x43): 2-byte percent, ``(A*256+B) * 100/255``.

    Task 2.3: SAE J1979 defines this PID as 2-byte (unlike the 1-byte
    ``decode_percent_a_over_255`` family) so it can read past 100% — real
    under boost/load on many engines. The old 1-byte decode silently
    truncated/wrapped above that.
    """
    if not _need(data, 2):
        return None
    return ((data[0] * 256 + data[1]) * 100.0 / 255.0, "%")


#: Registry: PID number → (name, decoder). Drives both the
#: ``read_supported`` flow and any caller that wants to translate a numeric
#: PID into a named telemetry channel.
PID_DECODERS: dict[int, tuple[str, Callable[[bytes], tuple[float, str] | None]]] = {
    0x01: ("MONITOR_STATUS", decode_monitor_status),
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
    # Wide-range O2 λ, bank-1 sensor 1/2 (Task 2.1): 0x24/0x25 are the
    # "equivalence ratio" variant, 0x34/0x35 the "current" variant — probe
    # which the ECM answers; the ECM claiming support for one over the
    # other is normal, so both are decoded identically (decode_wide_lambda
    # ignores the raw voltage/current bytes either way).
    0x24: ("O2_WR_LAMBDA_S1", decode_wide_lambda),
    0x25: ("O2_WR_LAMBDA_S2", decode_wide_lambda),
    0x2C: ("COMMANDED_EGR", decode_egr_percent),
    0x2D: ("EGR_ERROR", decode_egr_error),
    0x2E: ("EVAPORATIVE_PURGE", decode_evap_purge),
    0x2F: ("FUEL_LEVEL", decode_percent_a_over_255),
    0x30: ("WARMUPS_SINCE_DTC_CLEAR", decode_warmups),
    0x31: ("DISTANCE_SINCE_DTC_CLEAR", decode_distance_km),
    0x32: ("EVAP_VAPOR_PRESSURE", decode_evap_vapor_pressure_signed),
    0x33: ("BAROMETRIC_PRESSURE", decode_baro),
    0x34: ("O2_WR_CURRENT_S1", decode_wide_lambda),
    0x35: ("O2_WR_CURRENT_S2", decode_wide_lambda),
    0x3C: ("CATALYST_TEMP_B1S1", decode_catalyst_temp),
    0x3D: ("CATALYST_TEMP_B2S1", decode_catalyst_temp),
    0x3E: ("CATALYST_TEMP_B1S2", decode_catalyst_temp),
    0x3F: ("CATALYST_TEMP_B2S2", decode_catalyst_temp),
    0x42: ("CONTROL_MODULE_VOLTAGE", decode_control_module_voltage),
    0x43: ("ABSOLUTE_LOAD", decode_absolute_load),
    0x44: ("COMMANDED_EQUIV_RATIO", decode_commanded_lambda),
    0x45: ("RELATIVE_THROTTLE_POS", decode_percent_a_over_255),
    0x46: ("AMBIANT_AIR_TEMP", decode_temp_minus_40),
    # Throttle/pedal family (Task 2.2): one-time probe on the Xterra's real
    # ECM (both KOEO and engine-running) confirmed it answers these four but
    # not 0x48/0x4B — see data/nissan-xterra-2008/pid-probe-2026-07-22.md.
    0x47: ("ABS_THROTTLE_POS_B", decode_percent_a_over_255),
    0x49: ("ACCEL_PEDAL_POS_D", decode_percent_a_over_255),
    0x4A: ("ACCEL_PEDAL_POS_E", decode_percent_a_over_255),
    0x4C: ("COMMANDED_THROTTLE_ACTUATOR", decode_percent_a_over_255),
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
    "decode_absolute_load",
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
    "decode_mil_on",
    "decode_monitor_status",
    "decode_o2_voltage",
    "decode_percent_a_over_255",
    "decode_percent_b_minus_128",
    "decode_rpm",
    "decode_run_time",
    "decode_speed",
    "decode_temp_minus_40",
    "decode_timing_advance",
    "decode_warmups",
    "decode_wide_lambda",
    "parse_dtc_payload",
    "read_pid",
    "read_pids",
]


# A useful Any-typed re-export so callers don't need to import can themselves
# just to declare a bus parameter type. The Protocol is documented behavior
# rather than the python-can class hierarchy.
BusLike = Any
