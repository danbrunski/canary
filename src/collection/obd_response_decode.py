"""Pure decoding of one OBD/UDS response into readings (roadmap R02.9).

``_response_to_reading`` turns an assembled ``ObdResponse`` for one poll
request into the ``ObdReading`` rows the collector emits: Mode 01 PIDs
through ``PID_DECODERS``, raw Mode 22 DID payloads, DTC lists, Mode 06
monitor results, freeze frames, custom PIDs and memory windows. No bus, no
scheduler state: the single-PID and batched paths decode through exactly the
same code. Moved out of ``can_obd_collector``, which re-exports it.
"""

from __future__ import annotations

import logging
import time
from dataclasses import replace

from src.collection.can_obd_client import (
    PID_DECODERS,
    decode_mil_on,
    parse_dtc_payload,
)
from src.collection.custom_pids import evaluate_custom_pid
from src.collection.frame_time import FrameArrival
from src.collection.isotp import (
    ObdResponse,
)
from src.collection.obd_reading import (
    ObdReading,
)
from src.collection.obd_registry import (
    misfire_count_channel,
    misfire_counts_from_results,
    parse_mode06_response,
)
from src.collection.obd_scheduler import (
    PollRequest,
    RoundState,
)
from src.decode.dids import (
    MEMORY_SOURCE,
    apply_memory_scaling,
    decode_memory_window,
    is_memory_sentinel,
)

log = logging.getLogger(__name__)


#: Mode 01 PID whose response carries both the continuous MONITOR_STATUS
#: count channel and (Task 1.4) the MIL bit `decode_monitor_status` drops.
_MONITOR_STATUS_PID = 0x01


_MIL_STATUS_CHANNEL = "MIL_STATUS"


#: Service whose "service" kind response carries per-cylinder misfire data
#: (Task 3.1/3.2) in addition to the raw sweep — see the `service` branch
#: below.
_MODE06_SERVICE = 0x06


def _response_to_reading(
    request: PollRequest,
    response: ObdResponse,
    timestamp: float | None = None,
    *,
    arrival: FrameArrival | None = None,
    response_interval_s: float | None = None,
    round_state: RoundState | None = None,
) -> list[ObdReading]:
    """Decode a response and attach its acquisition provenance (C02.6/C02.7).

    ``arrival`` is when the (first) response frame reached us; it becomes
    ``acquired_at`` and, unless ``timestamp`` supplies a round stamp, also the
    presentation ``timestamp``. Without ``arrival`` the reading is legacy
    shaped: decode-time stamp, no acquisition metadata.

    Every reading decoded from one response shares that arrival instant. For
    a batched Mode 01 response that is the *receipt* time of one payload — it
    is not proof that the ECU sampled the PIDs it carried atomically.
    """
    if timestamp is None and arrival is not None:
        timestamp = arrival.at
    readings = _decode_response(request, response, timestamp)
    if arrival is None:
        return readings
    provenance: dict[str, object] = {
        "acquired_at": arrival.at,
        "acquired_clock": arrival.clock,
        "response_interval_s": response_interval_s,
        "responder_id": response.responder_id,
    }
    if round_state is not None:
        provenance["generation"] = round_state.generation
        provenance["round_id"] = round_state.round_id
    return [replace(reading, **provenance) for reading in readings]  # type: ignore[arg-type]


def _decode_response(
    request: PollRequest,
    response: ObdResponse,
    timestamp: float | None = None,
) -> list[ObdReading]:
    """Turn one raw response into zero or more decoded readings.

    Usually one reading per request; PID 0x01 emits a second (MIL_STATUS,
    Task 1.4) derived from the same payload instead of issuing another bus
    request just for the bit ``decode_monitor_status`` doesn't surface.

    ``timestamp`` overrides the decode-time clock read. A batched response
    carries several PIDs that were sampled by the ECU in *one* operation, so
    they must land on one timestamp; letting each decode call its own
    ``time.time()`` spreads a single measurement across several instants and
    forces every consumer to re-group them by proximity.
    """
    if timestamp is None:
        timestamp = time.time()
    if request.kind == "did":
        if not response.payload:
            return []
        return [ObdReading(timestamp, request.channel, response.payload.hex(), "")]

    if request.kind == "dtc":
        # Mode 03/07/0A (Task 1.3) — always emit, even with zero codes, so
        # downstream DTC state can tell "polled, none found" from "not
        # polled yet". Comma-joined string keeps the value on the same
        # TelemetryValue contract as every other reading.
        codes = parse_dtc_payload(response.payload)
        return [ObdReading(timestamp, request.channel, ",".join(codes), "")]

    if request.kind == "service":
        # Mode 06 (Task 3.1) — a whole monitor-test-result sweep is a list
        # of 9-byte records, which doesn't fit the scalar TelemetryValue
        # contract. Ship the raw payload as hex (matching the `did` branch
        # above); `src.control.monitor_state.MonitorState` parses it back
        # with `parse_mode06_response`. Always emit, even empty, so "polled,
        # nothing reported" is distinguishable from "not polled yet".
        # The ISO-TP matcher strips the echoed MID. Reattach it so the shared
        # Mode 06 parser receives its documented 9-byte
        # ``MID TID UAS value min max`` record.
        payload = response.payload
        if response.pid is not None:
            payload = bytes([response.pid]) + payload
        readings = [ObdReading(timestamp, request.channel, payload.hex(), "")]
        if request.service == _MODE06_SERVICE:
            # Task 3.2: also land per-cylinder misfire counts as ordinary
            # numeric channels (mirrors the MIL_STATUS derived-reading
            # pattern below) so they flow through storage into the wide
            # telemetry frame the session fingerprint scans — the hex sweep
            # reading above can't be aggregated numerically.
            counts = misfire_counts_from_results(parse_mode06_response(payload))
            for cylinder, count in sorted(counts.items()):
                readings.append(
                    ObdReading(timestamp, misfire_count_channel(cylinder), float(count), "count")
                )
        return readings

    if request.kind == "freeze_frame":
        # Mode 02 (Task 3.3) — same wire formula as Mode 01 for this PID
        # (`obd_registry.lookup(0x02, pid)`'s Mode-01 remap), but the
        # response carries one extra leading frame-number byte ahead of the
        # formula bytes that Mode 01 doesn't have; strip it before decoding.
        entry = PID_DECODERS.get(request.identifier)
        if entry is None or len(response.payload) < 2:
            return []
        _name, decoder = entry
        decoded = decoder(response.payload[1:])
        if decoded is None:
            return []
        value, unit = decoded
        return [ObdReading(timestamp, request.channel, value, unit, payload=response.payload)]

    if request.kind == "memory":
        assert request.memory_read is not None
        window_readings: list[ObdReading] = []
        for memory_channel, offset in zip(
            request.memory_read.channels, request.memory_read.offsets, strict=True
        ):
            channel = f"{MEMORY_SOURCE}:{memory_channel.name}"
            raw = decode_memory_window(memory_channel, response.payload, offset)
            if raw is None:
                continue
            if is_memory_sentinel(memory_channel, raw):
                # "Polled, and the ECU is commanding nothing" is an answer
                # about the engine; emitting no row would be
                # indistinguishable from "not polled", and scaling the
                # sentinel would put a fabricated value into every mean
                # that follows.
                window_readings.append(
                    ObdReading(
                        timestamp,
                        f"{channel}_SENTINEL",
                        memory_channel.sentinel_meaning or "sentinel",
                        "",
                        payload=response.payload,
                    )
                )
                continue
            scaled = apply_memory_scaling(memory_channel, raw)
            if scaled is None:
                # Scale deliberately unpinned: report raw counts rather than
                # inventing engineering units, matching the `did:` contract.
                window_readings.append(
                    ObdReading(timestamp, channel, float(raw), "counts", payload=response.payload)
                )
                continue
            window_readings.append(
                ObdReading(
                    timestamp,
                    channel,
                    scaled,
                    memory_channel.unit,
                    payload=response.payload,
                )
            )
        # Every reading here shares one ECU operation, not merely one round.
        return window_readings

    if request.kind == "custom":
        # Task 2.6 — decode through the definition's sandboxed formula
        # instead of a PID_DECODERS lookup. `build_poll_requests` only ever
        # builds a "custom" request from a CustomPid, so this is set.
        assert request.custom_pid is not None
        decoded = evaluate_custom_pid(request.custom_pid, response.payload)
        if decoded is None:
            return []
        value, unit = decoded
        return [ObdReading(timestamp, request.channel, value, unit, payload=response.payload)]

    # Reuse the public PID decoder behavior without issuing another request:
    # synthesize the same DecodedPid logic locally from PID_DECODERS.
    entry = PID_DECODERS.get(request.identifier)
    if entry is None:
        return []
    name, decoder = entry
    decoded = decoder(response.payload)
    if decoded is None:
        return []
    value, unit = decoded
    readings = [ObdReading(timestamp, name, value, unit, payload=response.payload)]
    if request.identifier == _MONITOR_STATUS_PID:
        mil_on = decode_mil_on(response.payload)
        if mil_on is not None:
            readings.append(
                ObdReading(timestamp, _MIL_STATUS_CHANNEL, mil_on, "bool", payload=response.payload)
            )
    return readings
