"""Why the ECU is not asked for its VIN right now (R29.7).

``POST /control/vin/read`` queues a Mode 09 request for the running collector to
send. Five things make that pointless or unwelcome, checked in order, each with
its own named refusal so the operator is told which one. The ladder lived in a
closure inside ``dashboard/routes/control/system.py``, testable only by building
an app and arranging the transmit gate through it; it is a plain function now.
"""

from __future__ import annotations

from collections.abc import Callable

from src.collection import tuning_profile, tx_gate, vin_read
from src.config import CanaryConfig
from src.control.rom_backup import VehicleReading

#: vehicle_state values where the OBD collector deliberately stays silent or
#: nothing answers: the key is off, or no vehicle is on the port.
_KEY_OFF_STATES = {
    "parked": "the vehicle is parked (12 V on the port, every module asleep)",
    "engine_off": "the key is off (broadcast CAN only; the ECM does not answer OBD)",
    "no_data": "neither bus is fresh: the key is off or nothing is connected",
    "bench": "no vehicle on the OBD port (bench power)",
}


def vin_refusal(
    config: CanaryConfig, take_reading: Callable[[], VehicleReading]
) -> vin_read.VinReadOutcome | None:
    """The named reason not to ask the ECU for its VIN now, if there is one.

    ``take_reading`` is called only when the four rungs that need no vehicle state
    have passed, as the closure it replaces did: taking it is a read of the
    newest session's parquet when there is no live source, not free.
    """
    if config.collection.obd_source != "canable":
        return vin_read.VinReadOutcome(
            None,
            "unsupported",
            "reading the VIN needs collection.obd_source=canable "
            f"(currently {config.collection.obd_source!r}); type it in instead",
        )
    if tx_gate.passive_armed():
        return vin_read.VinReadOutcome(
            None,
            "passive_armed",
            "passive armed: CANary is not transmitting; disarm it to read the VIN",
        )
    owner = tx_gate.ecm_session_owner()
    if owner != "can_diagnostics":
        return vin_read.VinReadOutcome(
            None,
            "ecm_held",
            f"another tool holds the ECM ({owner}); CANary does not transmit to it until that ends",
        )
    if tuning_profile.is_paused():
        return vin_read.VinReadOutcome(
            None,
            "capture_paused",
            "the capture is paused: the OBD collector is not polling, so it does not "
            "transmit the VIN request either; resume the capture to read the VIN",
        )
    reading = take_reading()
    if reading.state == "replay":
        return vin_read.VinReadOutcome(
            None,
            "replay",
            "a replay is running: the vehicle on the dashboard is a stored session",
        )
    if reading.state in _KEY_OFF_STATES:
        return vin_read.VinReadOutcome(
            None,
            "key_off",
            f"key off: {_KEY_OFF_STATES[reading.state]} (vehicle_state "
            f"{reading.state}); turn the key on, or type the VIN in",
        )
    return None


__all__ = ["vin_refusal"]
