"""The startup VIN / calibration-ID / CVN probe (OBD-II Mode 09).

Reads the vehicle's identity over the bus, once, before the collectors start:
the VIN (InfoType 0x02) on functional addressing (``0x7DF`` → ``0x7E8``),
then the calibration ID (0x04) and CVN (0x06). That is standard OBD-II and
does *not* need the UDS ``C0`` session that gates the Mode 22 DIDs, so the
probe never clashes with Mode 01 polling. It is an ordinary active diagnostic
request.

It transmits, so it is acquisition and lives here rather than beside the
profile registry it feeds (R09.5). Profile resolution
(:func:`src.vehicle.profiles.resolve_active_profile`) takes it as its
``probe`` argument from its caller, and it returns the resolution's own
result type, :class:`~src.vehicle.profiles.VehicleInfoProbe`.
"""

from __future__ import annotations

import logging
import time
from contextlib import suppress
from typing import Any

from src.config import CanaryConfig
from src.vehicle.profiles import VehicleInfoProbe

log = logging.getLogger(__name__)

# VINs are 17 alphanumeric characters (ISO 3779; excludes I/O/Q). Real ECU
# replies occasionally pad or truncate, so accept a forgiving length band and
# reject obviously-bogus reads rather than registering a junk profile.
_MIN_VIN_LEN = 11
_MAX_VIN_LEN = 17


# ---------------------------------------------------------------------------
# VIN probe
# ---------------------------------------------------------------------------


def _normalize_vin(vin: str) -> str | None:
    cleaned = "".join(ch for ch in vin if ch.isalnum()).upper()
    if _MIN_VIN_LEN <= len(cleaned) <= _MAX_VIN_LEN:
        return cleaned
    return None


def _hear_a_module(bus: Any, seconds: float) -> bool:
    """Whether any module (not a tester request) sends a frame within ``seconds``."""
    from src.collection.vehicle_power import is_tester_request

    deadline = time.monotonic() + seconds
    while (remaining := deadline - time.monotonic()) > 0:
        msg = bus.recv(timeout=remaining)
        if msg is not None and not is_tester_request(msg.arbitration_id):
            return True
    return False


def probe_vehicle_info(config: CanaryConfig) -> VehicleInfoProbe:
    """One-shot Mode 09 read at startup: VIN (InfoType 0x02), calibration ID
    (0x04), and CVN (0x06) over the same bus connection (Task 3.4).

    Same no-op/failure posture as the VIN-only probe this generalizes:
    ``None`` fields (never raises) when OBD is disabled, the bus is
    ``virtual``, the source isn't the CAN-native path, or the read fails.
    Opens its own short-lived bus and closes it before the main collectors
    start. A missing/failed calibration ID or CVN read doesn't invalidate a
    successful VIN read (or vice versa) — each InfoType is independent —
    except when the VIN request met silence (no frame at all from a
    diagnostic responder): then the other two are not asked (S01.4).
    """
    coll = config.collection
    if coll.obd_source != "canable" or config.hardware.can.interface == "virtual":
        return VehicleInfoProbe(vin=None, calibration_id=None, cvn=None)

    # On the bench (no vehicle on the port) nothing would answer, and the
    # request would sit in the transmit queue until ENOBUFS: one warning at
    # every bench start (2026-10-05). The collectors are not running yet, so
    # read the HAT's 12 V sense once here.
    from src.collection.supply_voltage_collector import read_supply_once
    from src.collection.vehicle_power import ASLEEP_SECONDS, classify_supply

    supply = read_supply_once(config.hardware.supply_voltage)
    if classify_supply(supply) == "bench":
        log.info(
            "vehicle info probe skipped: no vehicle on the OBD port (12 V sense reads %.2f V)",
            supply,
        )
        return VehicleInfoProbe(vin=None, calibration_id=None, cvn=None)

    # Imported lazily so this module (and config) stay importable without the
    # python-can collector deps present.
    import can

    from src.collection.can_bus import bus_factory
    from src.collection.can_obd_client import (
        DEFAULT_RESPONSE_ID_MAX,
        DEFAULT_RESPONSE_ID_MIN,
        CanObdClient,
    )
    from src.collection.obd_registry import decode_calibration_ids, decode_cvn, decode_vin

    try:
        bus = bus_factory(config.hardware.can)
    except Exception:
        log.warning("vehicle info probe: failed to open CAN bus", exc_info=True)
        return VehicleInfoProbe(vin=None, calibration_id=None, cvn=None)

    # A parked vehicle (battery on the port, every module asleep): asking would
    # only fill the transmit queue and may wake modules (operator, 2026-10-05).
    # Listen first; any module talking means the ignition is on.
    if (
        coll.obd_sleeps_with_bus
        and classify_supply(supply) == "vehicle"
        and not _hear_a_module(bus, ASLEEP_SECONDS)
    ):
        bus.shutdown()
        log.info(
            "vehicle info probe skipped: the vehicle is parked (12 V on the port, "
            "no module heard for %.0f s)",
            ASLEEP_SECONDS,
        )
        return VehicleInfoProbe(vin=None, calibration_id=None, cvn=None)

    vin: str | None = None
    calibration_id: str | None = None
    cvn: str | None = None
    # Any frame from a diagnostic responder, positive, negative or the first
    # frame of a transfer that never completes: the module is awake.
    responder_frames: list[int] = []

    def _tap(frame: can.Message) -> None:
        if DEFAULT_RESPONSE_ID_MIN <= frame.arbitration_id <= DEFAULT_RESPONSE_ID_MAX:
            responder_frames.append(frame.arbitration_id)

    try:
        client = CanObdClient(
            bus, timeout_seconds=config.profiles.vin_probe_timeout_seconds, on_frame=_tap
        )

        response = client.query_mode_09(0x02)
        if response is None and not responder_frames:
            # Silence, not a refusal: the ECU is asleep (key off), and the
            # calibration ID and CVN come from the same module, so asking
            # would cost two more full timeouts at every key-off boot (S01.4:
            # 6 s of the 2026-09-22 cold trace). An NRC or a partial answer
            # falls through to the independent reads below.
            log.info(
                "VIN probe: no Mode 09 response; calibration ID and CVN skipped "
                "(no responder on the bus)"
            )
            return VehicleInfoProbe(vin=None, calibration_id=None, cvn=None)
        if response is None:
            log.info("VIN probe: Mode 09 VIN refused or incomplete; reading the others")
        else:
            decoded = decode_vin(response.payload)
            if decoded is not None:
                vin = _normalize_vin(decoded[0])
                if vin is None:
                    log.warning("VIN probe: implausible VIN payload %r", decoded[0])

        response = client.query_mode_09(0x04)
        if response is not None:
            decoded = decode_calibration_ids(response.payload)
            if decoded is not None and decoded[0]:
                calibration_id = str(decoded[0])

        response = client.query_mode_09(0x06)
        if response is not None:
            decoded = decode_cvn(response.payload)
            if decoded is not None and decoded[0]:
                cvn = str(decoded[0])
    except Exception:
        log.warning("vehicle info probe failed", exc_info=True)
    finally:
        with suppress(Exception):
            bus.shutdown()

    return VehicleInfoProbe(vin=vin, calibration_id=calibration_id, cvn=cvn)


def read_vin(config: CanaryConfig) -> str | None:
    """One-shot VIN read at startup. ``None`` on any failure — never raises.

    Thin wrapper around :func:`probe_vehicle_info` (Task 3.4 generalized this
    to also read calibration ID/CVN) kept for callers that only need the VIN.
    """
    return probe_vehicle_info(config).vin
