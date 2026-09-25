"""Registry of the OBD-II PIDs the ELM327 path (``obd_collector``) polls.

The source of truth for that one transport only, through python-OBD's
command objects. The CAN-native path decodes through
``pid_decoders.PID_DECODERS`` with its metadata in ``obd_registry``; the two
PID sets are complementary by design (the July decision), not a single table.
Adding a PID here reaches the ELM327 collector only.

The set comes from FR-01: RPM, Vehicle Speed, Engine Load, Coolant Temp,
Intake Air Temp, MAF Air Flow Rate, Throttle Position, Short/Long Fuel Trim
Bank 1, O2 voltages 0x14-0x1B, Barometric Pressure, Engine Run Time.
"""

from __future__ import annotations

from dataclasses import dataclass

import obd
from obd import OBDCommand


@dataclass(frozen=True)
class PidSpec:
    """A PID we want the collector to poll."""

    name: str  # canonical short name used downstream (e.g. "rpm")
    command: OBDCommand
    unit: str  # human-readable unit


REQUIRED_PIDS: tuple[PidSpec, ...] = (
    PidSpec("rpm", obd.commands.RPM, "rpm"),
    PidSpec("speed", obd.commands.SPEED, "km/h"),
    PidSpec("engine_load", obd.commands.ENGINE_LOAD, "%"),
    PidSpec("coolant_temp", obd.commands.COOLANT_TEMP, "°C"),
    PidSpec("intake_temp", obd.commands.INTAKE_TEMP, "°C"),
    PidSpec("maf", obd.commands.MAF, "g/s"),
    PidSpec("throttle_pos", obd.commands.THROTTLE_POS, "%"),
    PidSpec("short_fuel_trim_1", obd.commands.SHORT_FUEL_TRIM_1, "%"),
    PidSpec("long_fuel_trim_1", obd.commands.LONG_FUEL_TRIM_1, "%"),
    PidSpec("o2_b1s1", obd.commands.O2_B1S1, "V"),
    PidSpec("o2_b1s2", obd.commands.O2_B1S2, "V"),
    PidSpec("o2_b1s3", obd.commands.O2_B1S3, "V"),
    PidSpec("o2_b1s4", obd.commands.O2_B1S4, "V"),
    PidSpec("o2_b2s1", obd.commands.O2_B2S1, "V"),
    PidSpec("o2_b2s2", obd.commands.O2_B2S2, "V"),
    PidSpec("o2_b2s3", obd.commands.O2_B2S3, "V"),
    PidSpec("o2_b2s4", obd.commands.O2_B2S4, "V"),
    PidSpec("barometric_pressure", obd.commands.BAROMETRIC_PRESSURE, "kPa"),
    PidSpec("run_time", obd.commands.RUN_TIME, "s"),
)


PIDS_BY_NAME: dict[str, PidSpec] = {spec.name: spec for spec in REQUIRED_PIDS}
