"""Is there a vehicle on the OBD port? The HAT's 12 V sense answers.

The CANary-HAT reads the protected ``+12V`` rail at the OBD connector
(``hardware.supply_voltage``, :mod:`src.collection.supply_voltage_collector`).
Pin 16 is the battery, live with the key out, so:

- ``vehicle``: a battery is there (>= :data:`VEHICLE_MIN_VOLTS`). The bus may
  be asleep, but there is something to talk to.
- ``bench``: nothing is there (<= :data:`BENCH_MAX_VOLTS`). The Pi runs from
  USB-C or the UPS with no vehicle connected, the normal desk setup. Nothing
  answers on CAN, and transmitting only fills the controller's queue (every
  minute the VIN probe ran into ``ENOBUFS`` and the OBD collector logged a
  reconnect, 2026-10-05), so the OBD collector waits instead.
- ``unknown``: no fresh reading (no 12 V sense configured, or it stopped), or
  a reading between the two that explains nothing. Callers behave as they
  did before this module existed.

The supply collector notes every reading here; the vehicle-state classifier
uses the same thresholds (:mod:`src.vehicle_state`).
"""

from __future__ import annotations

import threading
import time
from typing import Literal

#: A battery on the port: well under a flat battery's ~11.5 V and a crank
#: dip's ~9 V.
VEHICLE_MIN_VOLTS = 8.0
#: Nothing on the port: the divider reads ~0 V with the connector unplugged.
BENCH_MAX_VOLTS = 2.0
#: A reading older than this says nothing (the collector reads at 10 Hz).
FRESH_SECONDS = 10.0

Connection = Literal["vehicle", "bench", "unknown"]


def classify_supply(volts: float | None) -> Connection:
    """What a (fresh) supply reading says about the port."""
    if volts is None:
        return "unknown"
    if volts >= VEHICLE_MIN_VOLTS:
        return "vehicle"
    if volts <= BENCH_MAX_VOLTS:
        return "bench"
    return "unknown"


class VehiclePower:
    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._volts: float | None = None
        self._at: float | None = None
        self._expected = False

    def expect_readings(self) -> None:
        """A 12 V sense is configured: its first reading is worth waiting for
        (the OBD collector started 30 ms before it on 2026-10-05 and hit the
        empty bus once per restart)."""
        with self._lock:
            self._expected = True

    @property
    def expected(self) -> bool:
        with self._lock:
            return self._expected

    def note(self, volts: float, *, at: float | None = None) -> None:
        """The supply collector's latest reading (``at``: ``time.monotonic()``)."""
        with self._lock:
            self._volts = volts
            self._at = time.monotonic() if at is None else at

    def latest(self, *, now: float | None = None) -> float | None:
        """The latest reading while fresh, else ``None``."""
        now = time.monotonic() if now is None else now
        with self._lock:
            if self._at is None or now - self._at > FRESH_SECONDS:
                return None
            return self._volts

    def connection(self, *, now: float | None = None) -> Connection:
        return classify_supply(self.latest(now=now))

    def reset(self) -> None:
        with self._lock:
            self._volts = None
            self._at = None
            self._expected = False


#: The daemon's one reading, shared by the supply collector, the OBD
#: collector and anything else that asks.
VEHICLE_POWER = VehiclePower()


__all__ = [
    "BENCH_MAX_VOLTS",
    "FRESH_SECONDS",
    "VEHICLE_MIN_VOLTS",
    "VEHICLE_POWER",
    "Connection",
    "VehiclePower",
    "classify_supply",
]
