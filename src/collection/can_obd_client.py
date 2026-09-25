"""ISO 15765-4 OBD-II client over python-can.

Any adapter python-can drives can transmit, not just sniff (SocketCAN on the
in-vehicle HAT, a Canable over slcan). This module implements the request
side: send a service-N request frame on the OBD-II functional ID
(0x7DF), reassemble the ECU's ISO-TP response (single-frame for most
Mode 01 PIDs, multi-frame for VIN and DTC lists), and return the decoded
payload. Pairs with ``can_collector.collect_can`` which sniffs broadcast
frames. For continuous capture, ``can_obd_collector`` owns one Bus and
multiplexes broadcast frames plus diagnostic responses so slcan does not need
two competing serial handles.

Alongside ``obd_collector.py`` (the ELM327 path) and the K-line collector,
not instead of them: the three transports are first-class and symmetric.
This one talks ISO-TP to the bus directly, so it polls at native CAN rate
and shares one bus handle with raw broadcast capture.

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

Any diagnostic service can be sent. ``query()`` passes a caller-supplied
service byte straight to the request builder, so write and actuation
services (Mode 04, UDS 0x2E / 0x2F, ...) are reachable from this module.
The fixed ``query_mode_*`` helpers still only cover the read services they
are named for; ``query()`` is the general path.

Receive ownership (roadmap C06): every frame this client takes off the bus
goes through :func:`receive_frame`, which hands it to the optional
``on_frame`` tap *before* the client decides whether it is the awaited
response. The collector loop constructs its client with the same tap it uses
for its own receives, so a broadcast frame arriving while a support probe or
an ad-hoc query is waiting still reaches raw capture exactly once.
Standalone use (``on_frame=None``: scripts, probe tools, the ad-hoc control
request on its own bus handle) is unchanged.
"""

from __future__ import annotations

import logging
import time
from collections.abc import Callable, Iterable, Sequence
from dataclasses import dataclass
from typing import Any

import can

from src.collection import tx_gate
from src.collection.can_bus import (  # re-exported: R02.12 moved the receive primitive
    DEFAULT_PARSE_ERROR_OWNER,
    FRAME_PARSE_ERRORS,
    FrameTap,
    ParseErrorTap,
    receive_frame,
)
from src.collection.discovery import (
    MODE01_SUPPORT_GROUPS,
    MODE06_SUPPORT_GROUPS,
    SupportProbe,
    walk_support,
)
from src.collection.isotp import (
    DEFAULT_REQUEST_ID,
    DEFAULT_RESPONSE_ID_MAX,
    DEFAULT_RESPONSE_ID_MIN,
    FLOW_CONTROL_CTS,
    PCI_SINGLE_FRAME,
    IsoTpResponseAssembler,
    ObdResponse,
    await_response,
    build_batched_did_request_payload,
    build_batched_request_payload,
    build_request_payload,
    handle_single_frame,
    is_negative_response,
    negative_response_code,
    pci_type,
    positive_response_service,
    split_batched_response,
)
from src.collection.obd_registry import lookup
from src.collection.pid_decoders import (  # re-exported: R02.13 moved them
    PID_DECODERS,
    TIMING_ADVANCE_UNAVAILABLE,
    decode_absolute_load,
    decode_baro,
    decode_catalyst_temp,
    decode_commanded_lambda,
    decode_control_module_voltage,
    decode_distance_km,
    decode_egr_error,
    decode_egr_percent,
    decode_evap_purge,
    decode_evap_vapor_pressure_signed,
    decode_fuel_status,
    decode_intake_pressure,
    decode_maf,
    decode_mil_on,
    decode_monitor_status,
    decode_o2_voltage,
    decode_percent_a_over_255,
    decode_percent_b_minus_128,
    decode_rpm,
    decode_run_time,
    decode_speed,
    decode_temp_minus_40,
    decode_timing_advance,
    decode_warmups,
    decode_wide_lambda,
    parse_dtc_payload,
)

log = logging.getLogger(__name__)

#: Longest single ``bus.recv`` wait. Bounding it keeps a stop request and a
#: capture deadline serviced within one slice, and is what the collector's
#: own receive helper has always used.
RECV_SLICE_SECONDS = 0.05


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


#: The read services :meth:`CanObdClient.query_raw` may send, and nothing
#: else: session control, the three "many values in one request" candidates
#: (ReadDataByLocalIdentifier, ReadMemoryByAddress, ReadDataByPeriodicIdentifier),
#: requestSeed, and TesterPresent. Batched ``0x22`` is refused by the 2008
#: Nissan ECM (``7F 22 12``), so these are the routes left to a same-instant
#: multi-value read. Every write/reflash service (``0x2E``, ``0x3B``,
#: ``0x3D``, ``0x31``, ``0x34``–``0x37``) is absent by construction.
#: ``0x2C`` (UDS DynamicallyDefineDataIdentifier) is the same operation as
#: ``0xAC`` in the newer dialect, and is here for the same reason.
#: ``0xAC`` (DynamicallyDefineLocalIdentifier) is here because it is the
#: *setup* half of a read: it tells the ECU which values a later ``0x21``
#: should return, and touches nothing else. The state it creates is volatile
#: and ends with the diagnostic session, which puts it on the same side of
#: the line as session control — not the non-volatile writes that line
#: exists to prevent. It is what lets one request return many values on an
#: ECM that refuses batched ``0x22``.
RAW_QUERY_SERVICES: frozenset[int] = frozenset({0x10, 0x21, 0x23, 0x27, 0x2A, 0x2C, 0x3E, 0xAC})


@dataclass(frozen=True)
class RawReply:
    """What one :meth:`CanObdClient.query_raw` probe got back.

    Three distinct empties, never collapsed: ``sent=False`` (the transmit
    gate or the bus refused to send), ``negative_response_code`` set (the ECU
    answered ``7F <service> <NRC>``), and both ``None`` (silence within the
    timeout). ``payload`` is the bytes after the positive service byte.
    """

    sent: bool
    payload: bytes | None = None
    negative_response_code: int | None = None

    @property
    def positive(self) -> bool:
        return self.payload is not None


@dataclass(frozen=True)
class BatchedMode22Reply:
    """What came back for one multi-DID Mode 22 request — including a refusal.

    A batched request has two failure shapes that look identical once reduced
    to "no payload": the ECU *rejected the whole request* (``7F 22 <NRC>``,
    e.g. ``0x12`` on an ECM that does not accept multi-DID ReadDataByIdentifier)
    or nothing answered. A caller that cannot tell those apart from "the ECU
    omitted one DID" will re-read individually and report a batched sweep that
    never batched anything (2026-08-08, 2026-09-17). So the NRC is carried
    here, not swallowed.
    """

    #: The positive response (payload after the ``62`` echo), or ``None``.
    response: ObdResponse | None
    #: The NRC byte when the ECU answered ``7F 22 <NRC>``; ``None`` otherwise.
    negative_response_code: int | None = None
    #: False when the request never went out (passive mode, send error).
    sent: bool = True


# ---------------------------------------------------------------------------
# Client
# ---------------------------------------------------------------------------


class CanObdClient:
    """Send OBD-II service-N requests on a python-can ``Bus``; collect responses.

    Constructed once per Canable session. Re-entrant *for sequential* calls
    only — the underlying ``Bus.recv`` is shared global state, so don't call
    ``query_*`` from two threads concurrently. Continuous capture uses
    ``can_obd_collector`` to multiplex raw-frame capture and diagnostic polling
    through one Bus; it passes its raw-capture tap as ``on_frame`` so every
    frame this client receives is captured too (C06.2).
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
        on_frame: FrameTap | None = None,
        on_parse_error: ParseErrorTap | None = None,
        parse_error_owner: str = DEFAULT_PARSE_ERROR_OWNER,
    ) -> None:
        self._bus = bus
        self._request_id = request_id
        self._response_id_min = response_id_min
        self._response_id_max = response_id_max
        self._timeout = timeout_seconds
        self._time_fn = time_fn
        self._on_frame = on_frame
        self._on_parse_error = on_parse_error
        self._parse_error_owner = parse_error_owner

    def _recv(self, timeout: float) -> can.Message | None:
        return receive_frame(
            self._bus,
            timeout=max(0.001, min(RECV_SLICE_SECONDS, timeout)),
            on_frame=self._on_frame,
            on_parse_error=self._on_parse_error,
            parse_error_owner=self._parse_error_owner,
        )

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
        byte_length_for: Callable[[int], int | None] | None = None,
    ) -> BatchedMode01Response | None:
        """Mode 01 ShowCurrentData for several PIDs in one round trip.

        SAE J1979 permits up to six PIDs per request; the response carries a
        ``<pid> <data…>`` pair for each. On this bus the cost is the round
        trip rather than the bytes, so batching is worth roughly 5x — measured
        67.7 batched requests/sec against 85.1 single-PID, but each batched
        one returns six channels.

        ``byte_length_for`` supplies each PID's payload width; by default the
        registry's (``obd_registry``). It used to be required, injected to
        dodge an import cycle that ended when the decoders moved to
        ``pid_decoders`` (R02.13); a caller holding measured widths still
        passes its own.

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
        response = self._collect_response(service=0x01, pid_echo=None, request_id=self._request_id)
        if response is None:
            return None
        readings, clean = split_batched_response(
            response.payload, _registry_width if byte_length_for is None else byte_length_for
        )
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

    def query_mode_22_batch(
        self, dids: Sequence[int], *, request_id: int | None = None
    ) -> BatchedMode22Reply:
        """UDS Mode 22 ReadDataByIdentifier for several DIDs in one request.

        ``.response.payload`` is the raw payload after the ``62`` echo — a
        stream of ``<did_hi> <did_lo> <data…>`` groups. Split it with
        :func:`~src.collection.isotp.split_batched_did_response`, which needs
        each DID's width because the response is not self-delimiting.

        Every DID in one request is sampled by the ECU at the same instant,
        which is the reason to use this over repeated ``query_mode_22``.
        Capped at :data:`~src.collection.isotp.MAX_BATCHED_DIDS` per request.

        Not every ECU accepts more than one DID per request: the 2008 Nissan
        ECM answers ``7F 22 12`` (subFunctionNotSupported) to any multi-DID
        read while answering the same DIDs singly. That refusal is returned as
        ``negative_response_code`` rather than collapsed into "no response".
        """
        payload = build_batched_did_request_payload(0x22, dids)
        message = can.Message(
            arbitration_id=self._request_id if request_id is None else request_id,
            data=payload,
            is_extended_id=False,
        )
        try:
            if not tx_gate.send(self._bus, message, what="request service=0x22 (batched)"):
                return BatchedMode22Reply(response=None, sent=False)
        except can.CanError:
            log.warning("CAN send failed", extra={"service": 0x22, "batch": len(dids)})
            return BatchedMode22Reply(response=None, sent=False)
        # identifier=None: the assembler must not try to match one DID echo,
        # because the response carries several.
        response, nrc = self._collect_response_or_nrc(
            service=0x22, pid_echo=None, request_id=message.arbitration_id
        )
        return BatchedMode22Reply(response=response, negative_response_code=nrc)

    def query_raw(
        self,
        service: int,
        body: bytes,
        *,
        request_id: int | None = None,
    ) -> RawReply:
        """One single-frame request with an arbitrary body, for read probes.

        ``body`` is everything after the service byte: a local identifier for
        ``0x21``, ``<mode> <pdid…>`` for ``0x2A``, an address and size for
        ``0x23``, a sub-function for ``0x27``. Multi-frame responses are
        reassembled like every other query; a ``7F`` comes back as
        ``negative_response_code`` so "refused" is never mistaken for "silent".

        Only the probe services in :data:`RAW_QUERY_SERVICES` may be sent
        here, and ``0x27`` only with an odd sub-function (requestSeed). This
        is where the vehicle skill's line is enforced rather than documented:
        nothing that writes non-volatile memory, and no ``sendKey``, can be
        built through this method whatever a caller asks for.
        """
        if service not in RAW_QUERY_SERVICES:
            raise ValueError(
                f"service 0x{service:02X} is not a probe read service; query_raw sends only "
                + ", ".join(f"0x{s:02X}" for s in sorted(RAW_QUERY_SERVICES))
            )
        if service == 0x27 and (not body or body[0] % 2 == 0):
            raise ValueError(
                "0x27 through query_raw is requestSeed only (odd sub-function); a sendKey "
                "must never be built here"
            )
        if len(body) > 6:
            raise ValueError(f"body of {len(body)} bytes does not fit a single frame")
        frame = bytes([1 + len(body), service]) + body
        message = can.Message(
            arbitration_id=self._request_id if request_id is None else request_id,
            data=frame + bytes(8 - len(frame)),
            is_extended_id=False,
        )
        try:
            if not tx_gate.send(self._bus, message, what=f"probe service=0x{service:02X}"):
                return RawReply(sent=False)
        except can.CanError:
            log.warning("CAN send failed", extra={"service": service, "body": body.hex()})
            return RawReply(sent=False)
        response, nrc = self._collect_response_or_nrc(
            service=service, pid_echo=None, request_id=message.arbitration_id
        )
        return RawReply(
            sent=True,
            payload=None if response is None else bytes(response.payload),
            negative_response_code=nrc,
        )

    def query(
        self,
        *,
        service: int,
        identifier: int | None = None,
        request_id: int | None = None,
    ) -> ObdResponse | None:
        """Generic request for exploratory / ad-hoc use.

        One escape hatch instead of a fixed ``query_mode_*`` method per
        service, for probing a service/identifier combination nobody has
        wired a dedicated method for yet (an unlisted Mode 06 MID, an unbound
        Mode 22 DID, ...). Backs the ``obd_request`` control route.

        ``service`` is caller-supplied and passed through unfiltered, so this
        reaches write and actuation services as well as read ones — the
        caller decides what is appropriate to send. ``identifier=None``
        matches the no-PID services (Mode 03/07/0A); anything else is passed
        through as the request's PID/DID/MID byte(s).
        """
        return self.query_or_nrc(service=service, identifier=identifier, request_id=request_id)[0]

    def query_or_nrc(
        self,
        *,
        service: int,
        identifier: int | None = None,
        request_id: int | None = None,
    ) -> tuple[ObdResponse | None, int | None]:
        """:meth:`query`, but a refusal comes back as its NRC instead of ``None``.

        ``(None, None)`` is silence; ``(None, 0x80)`` is the ECU saying the
        service is not available in the active session — a measured
        answer (2026-09-22: ``21 10`` in the default session), not a timeout.
        """
        return self._request_or_nrc(
            service=service,
            pid=identifier,
            expect_pid_echo=identifier is not None,
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
        marker, matching the SAE J1979 spec, and stops at the first silent
        or refused group. Returns the sorted tuple of supported PID numbers.

        Synchronous, for standalone callers. The collector loop drives the
        same walk one probe per budget slot through :meth:`probe_support`
        (``src.collection.discovery``), so there is one support-map parser.
        """
        return walk_support(
            lambda group: self.probe_support(0x01, group),
            service=0x01,
            groups=MODE01_SUPPORT_GROUPS,
        )

    def query_supported_mode_06_mids(self) -> tuple[int, ...]:
        """Enumerate Mode 06 monitor IDs using the J1979 support bitmaps.

        Mode 06 requires a MID byte even for discovery. Walk each 0x20-wide
        support group and merge all responders, mirroring Mode 01 discovery.
        """
        return walk_support(
            lambda group: self.probe_support(0x06, group),
            service=0x06,
            groups=MODE06_SUPPORT_GROUPS,
        )

    def probe_support(
        self,
        service: int,
        group: int,
        *,
        should_stop: Callable[[], bool] | None = None,
    ) -> SupportProbe:
        """One support-group probe: every responder's reply, or the NRC, or silence.

        ``should_stop`` is polled once per receive slice so a collector stop
        or deadline ends the wait promptly instead of after the full timeout.
        """
        return self._request_many(
            service=service, pid=group, expect_pid_echo=True, should_stop=should_stop
        )

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
        """Send a single-frame request and reassemble the response.

        A negative response reads as ``None`` here, the same as silence —
        fine for the polling paths, which retry either way. A caller that
        must tell "refused" from "silent" uses :meth:`_request_or_nrc`.
        """
        return self._request_or_nrc(
            service=service, pid=pid, expect_pid_echo=expect_pid_echo, request_id=request_id
        )[0]

    def _request_or_nrc(
        self,
        *,
        service: int,
        pid: int | None,
        expect_pid_echo: bool,
        request_id: int | None = None,
    ) -> tuple[ObdResponse | None, int | None]:
        """The one send path: ``(response, negative_response_code)``.

        ``(None, None)`` is silence or an unsent request; ``(None, nrc)`` is
        the ECU refusing — an answer, not an absence.
        """
        request_payload = self._build_request(service=service, pid=pid)
        message = can.Message(
            arbitration_id=self._request_id if request_id is None else request_id,
            data=request_payload,
            is_extended_id=False,
        )
        try:
            if not tx_gate.send(self._bus, message, what=f"request service=0x{service:02X}"):
                return None, None
        except can.CanError:
            log.warning("CAN send failed", extra={"service": service, "pid": pid})
            return None, None

        return self._collect_response_or_nrc(
            service=service,
            pid_echo=pid if expect_pid_echo else None,
            request_id=message.arbitration_id,
        )

    def _request_many(
        self,
        *,
        service: int,
        pid: int | None,
        expect_pid_echo: bool,
        request_id: int | None = None,
        quiet_seconds: float = 0.05,
        should_stop: Callable[[], bool] | None = None,
    ) -> SupportProbe:
        """Send a request and collect single-frame replies from all responders.

        Supported-PID probes are functional broadcasts, so more than one ECU
        may answer. Once the first relevant response arrives, keep listening
        for a short quiet window so slower responders are merged without
        waiting the full timeout on every successful probe. A negative
        response is reported beside any positive ones so the caller can tell
        a refusal from a silent bus (C06.5).
        """
        request_payload = self._build_request(service=service, pid=pid)
        message = can.Message(
            arbitration_id=self._request_id if request_id is None else request_id,
            data=request_payload,
            is_extended_id=False,
        )
        try:
            if not tx_gate.send(self._bus, message, what=f"probe service=0x{service:02X}"):
                return SupportProbe(sent=False)
        except can.CanError:
            log.warning("CAN send failed", extra={"service": service, "pid": pid})
            return SupportProbe(sent=False)

        expected_service = positive_response_service(service)
        deadline = self._time_fn() + self._timeout
        quiet_deadline: float | None = None
        responses: dict[int, ObdResponse] = {}
        nrc: int | None = None

        while True:
            now = self._time_fn()
            if now >= deadline or (quiet_deadline is not None and now >= quiet_deadline):
                break
            if should_stop is not None and should_stop():
                break
            wait_until = deadline if quiet_deadline is None else min(deadline, quiet_deadline)
            frame = self._recv(wait_until - now)
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
                nrc = negative_response_code(data, service)
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

        return SupportProbe(responses=tuple(responses.values()), negative_response_code=nrc)

    def _build_request(self, *, service: int, pid: int | None) -> bytes:
        """Single-frame request body. Padded to 8 bytes with 0x00."""
        return build_request_payload(service, pid)

    def _collect_response(
        self,
        *,
        service: int,
        pid_echo: int | None,
        request_id: int | None = None,
    ) -> ObdResponse | None:
        """Read frames until a matching response is assembled or we time out."""
        return self._collect_response_or_nrc(
            service=service, pid_echo=pid_echo, request_id=request_id
        )[0]

    def _collect_response_or_nrc(
        self,
        *,
        service: int,
        pid_echo: int | None,
        request_id: int | None = None,
    ) -> tuple[ObdResponse | None, int | None]:
        """:meth:`_collect_response`, plus the NRC when the ECU refused.

        Returns ``(response, None)`` on a positive answer, ``(None, nrc)`` on
        a definitive ``7F <service> <NRC>``, and ``(None, None)`` on timeout.
        The wait is ``isotp.await_response``, the collector's own policy: the
        reply is pinned to the physical responder of ``request_id`` (the
        client's default when ``None``), and ``0x78`` responsePending extends
        the wait instead of being scored as silence (R02.5). A negative
        response stops the wait at once — critical for DID sweeps, where an
        absent DID otherwise costs a full timeout each.
        """
        exchange = await_response(
            self._recv,
            self._send_flow_control,
            IsoTpResponseAssembler(service=service, identifier=pid_echo),
            self._request_id if request_id is None else request_id,
            self._time_fn() + self._timeout,
            clock=self._time_fn,
            response_ids=(self._response_id_min, self._response_id_max),
        )
        return exchange.response, exchange.negative_response_code

    def _send_flow_control(self, arbitration_id: int) -> None:
        """Continue-to-send (block size 0, STmin 0) to the responder's tester id."""
        try:
            tx_gate.send(
                self._bus,
                can.Message(
                    arbitration_id=arbitration_id,
                    data=FLOW_CONTROL_CTS,
                    is_extended_id=False,
                ),
                what="flow-control",
            )
        except can.CanError:
            log.warning("CAN flow-control send failed", extra={"flow_control_id": arbitration_id})


def _registry_width(pid: int) -> int | None:
    """The registry's Mode 01 payload width for ``pid``, or ``None``."""
    definition = lookup(0x01, pid)
    if definition is None or definition.byte_length <= 0:
        return None
    return definition.byte_length


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
    "DEFAULT_PARSE_ERROR_OWNER",
    "FRAME_PARSE_ERRORS",
    "FrameTap",
    "ParseErrorTap",
    "RAW_QUERY_SERVICES",
    "receive_frame",
    "RawReply",
    "DEFAULT_REQUEST_ID",
    "DEFAULT_RESPONSE_ID_MAX",
    "DEFAULT_RESPONSE_ID_MIN",
    "PID_DECODERS",
    "TIMING_ADVANCE_UNAVAILABLE",
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
