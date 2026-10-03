"""Operating constants shared across detection, analysis and status (R01.7).

The engine-running floor was defined four times (``refit``, ``fingerprint``,
``cold_start_inventory``, ``dashboard.status``) and ``models.evaluation``
imported the 2,000-line ``analysis.refit`` only to read it. One home, a leaf
module, so any layer can import it.

Vehicle-specific operating points (WOT floors, rpm bands ending at a given
engine's redline) belong to the vehicle profile (R05.13), not here.
"""

from __future__ import annotations

#: RPM at or above which the engine is running. Below it with OBD fresh the
#: vehicle is ``ignition_only`` (key on, engine off): KOEO rows read RPM == 0
#: with off-map outputs (TIMING_ADVANCE ~63 deg vs ~17 deg at idle), so every
#: learned surface, statistic and state classifier gates on this. 400 sits well
#: below any running idle (~700-800) and well above sensor jitter.
ENGINE_RUNNING_MIN_RPM = 400.0

#: The operating mode the segmenter assigns at highway speed. Speed-band
#: decisions (``events.cruise``'s regime) read the segmenter's label rather
#: than restating its band: the cruise detector said >= 70 km/h while the
#: segmenter's highway band starts at 80.
HIGHWAY_MODE = "highway"


#: SAE J1979 Mode 01 PID 0x03 fuel-system status (first byte), by the single
#: bit a conforming ECU sets. Standard, not vehicle-specific. Membership is
#: always an explicit allowlist of these codes: a nonzero status is not
#: "closed loop" (0x04 is open loop for load *or* decel fuel cut, 0x10 is
#: closed loop with a feedback fault).
FUEL_STATUS_STATES: dict[int, str] = {
    0x01: "open_loop_insufficient_temperature",
    0x02: "closed_loop",
    0x04: "open_loop_load_or_fuel_cut",
    0x08: "open_loop_system_failure",
    0x10: "closed_loop_feedback_fault",
}

#: Healthy closed-loop feedback only. ``0x10`` is excluded on purpose: a
#: model of normal trims must not learn from, or grade, rows the ECU itself
#: reports as having a faulty feedback loop.
CLOSED_LOOP_FUEL_STATES: tuple[int, ...] = (0x02,)


def fuel_status_name(code: float) -> str:
    """Stable name for a fuel-status value (``invalid(<n>)`` if not J1979)."""
    as_int = int(code)
    if float(as_int) == float(code) and as_int in FUEL_STATUS_STATES:
        return FUEL_STATUS_STATES[as_int]
    return f"invalid({code:g})"


__all__ = [
    "CLOSED_LOOP_FUEL_STATES",
    "ENGINE_RUNNING_MIN_RPM",
    "FUEL_STATUS_STATES",
    "HIGHWAY_MODE",
    "fuel_status_name",
]
