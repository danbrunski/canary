"""DTC description lookup + report assembly (Task 6.4).

``can_obd_client.parse_dtc_payload`` turns a Mode 03/07/0A payload into
``["P0420", …]``; this adds the *human* layer. A full SAE J2012 table is
thousands of mostly-manufacturer codes — we ship the common generic
powertrain set verbatim and, for everything else, a **structural**
description derived from the code itself (system letter, generic-vs-
manufacturer bit, subsystem group) so an unknown code is still explained
rather than shown bare. ``build_dtc_report`` packages stored / pending /
permanent lists into one timestamped, exportable snapshot for the
dashboard + ``canaryctl`` DTC workflow.

Read-only: this module only *describes* codes. Clearing them (Mode 04) is
the deliberately-separate, parked-only, authenticated path (Task 6.5).
"""

from __future__ import annotations

from dataclasses import asdict, dataclass
from datetime import UTC, datetime
from typing import Any

__all__ = [
    "DtcDescription",
    "DtcReport",
    "build_dtc_report",
    "describe_dtc",
]

#: Curated generic powertrain codes (the ones an Xterra owner actually sees).
#: Not exhaustive by design — the structural fallback covers the long tail.
_GENERIC: dict[str, str] = {
    "P0011": "Intake Camshaft Position Timing Over-Advanced (Bank 1)",
    "P0021": "Intake Camshaft Position Timing Over-Advanced (Bank 2)",
    "P0031": "HO2S Heater Control Circuit Low (Bank 1 Sensor 1)",
    "P0037": "HO2S Heater Control Circuit Low (Bank 1 Sensor 2)",
    "P0075": "Intake Valve Control Solenoid Circuit (Bank 1)",
    "P0081": "Intake Valve Control Solenoid Circuit (Bank 2)",
    "P0101": "Mass or Volume Air Flow Circuit Range/Performance",
    "P0102": "Mass or Volume Air Flow Circuit Low Input",
    "P0103": "Mass or Volume Air Flow Circuit High Input",
    "P0113": "Intake Air Temperature Sensor 1 Circuit High",
    "P0118": "Engine Coolant Temperature Sensor 1 Circuit High",
    "P0125": "Insufficient Coolant Temp for Closed Loop Fuel Control",
    "P0128": "Coolant Thermostat (Temp Below Regulating Temperature)",
    "P0130": "O2 Sensor Circuit (Bank 1 Sensor 1)",
    "P0133": "O2 Sensor Circuit Slow Response (Bank 1 Sensor 1)",
    "P0137": "O2 Sensor Circuit Low Voltage (Bank 1 Sensor 2)",
    "P0171": "System Too Lean (Bank 1)",
    "P0172": "System Too Rich (Bank 1)",
    "P0174": "System Too Lean (Bank 2)",
    "P0175": "System Too Rich (Bank 2)",
    "P0300": "Random/Multiple Cylinder Misfire Detected",
    "P0301": "Cylinder 1 Misfire Detected",
    "P0302": "Cylinder 2 Misfire Detected",
    "P0303": "Cylinder 3 Misfire Detected",
    "P0304": "Cylinder 4 Misfire Detected",
    "P0305": "Cylinder 5 Misfire Detected",
    "P0306": "Cylinder 6 Misfire Detected",
    "P0325": "Knock Sensor 1 Circuit (Bank 1)",
    "P0335": "Crankshaft Position Sensor A Circuit",
    "P0340": "Camshaft Position Sensor A Circuit (Bank 1)",
    "P0420": "Catalyst System Efficiency Below Threshold (Bank 1)",
    "P0430": "Catalyst System Efficiency Below Threshold (Bank 2)",
    "P0440": "Evaporative Emission System",
    "P0442": "Evaporative Emission System Leak Detected (small leak)",
    "P0446": "Evaporative Emission System Vent Control Circuit",
    "P0455": "Evaporative Emission System Leak Detected (large leak)",
    "P0456": "Evaporative Emission System Leak Detected (very small leak)",
    "P0500": "Vehicle Speed Sensor A",
    "P0505": "Idle Air Control System",
    "P0507": "Idle Air Control System RPM Higher Than Expected",
    "P0600": "Serial Communication Link",
    "P0700": "Transmission Control System (MIL Request)",
    "P0720": "Output Speed Sensor Circuit",
    "U1000": "CAN Communication Bus",
    "U1001": "CAN Communication Bus",
    "C1130": "Engine Signal Circuit (ABS)",
}

_SYSTEM = {
    "P": "Powertrain",
    "C": "Chassis",
    "B": "Body",
    "U": "Network / Communication",
}

#: First-digit-after-letter groups for the structural fallback.
_P_SUBSYSTEM = {
    "0": "Fuel & Air Metering / Auxiliary Emission Controls",
    "1": "Fuel & Air Metering",
    "2": "Fuel & Air Metering (Injector Circuit)",
    "3": "Ignition System or Misfire",
    "4": "Auxiliary Emission Controls",
    "5": "Vehicle Speed, Idle Control, Auxiliary Inputs",
    "6": "Computer Output Circuit",
    "7": "Transmission",
    "8": "Transmission",
    "9": "Transmission (SAE Reserved)",
    "A": "Hybrid Propulsion",
    "B": "Hybrid Propulsion",
}


@dataclass(frozen=True)
class DtcDescription:
    code: str
    description: str
    system: str
    #: True = SAE generic (second char ``0``/``2``); False = manufacturer-
    #: specific (``1``/``3``) — the code means different things per OEM.
    generic: bool
    curated: bool  # True when the text is from the curated table, not derived

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


def describe_dtc(code: str) -> DtcDescription:
    """Describe a DTC. Always returns *something* — bare codes help nobody."""
    code = code.strip().upper()
    system = _SYSTEM.get(code[:1], "Unknown") if code else "Unknown"
    generic = len(code) >= 2 and code[1] in ("0", "2")
    if code in _GENERIC:
        return DtcDescription(code, _GENERIC[code], system, generic, curated=True)
    subsystem = _P_SUBSYSTEM.get(code[2], "") if code[:1] == "P" and len(code) >= 3 else ""
    kind = "SAE generic" if generic else "manufacturer-specific"
    desc = f"{system} {kind} code"
    if subsystem:
        desc += f" — {subsystem}"
    return DtcDescription(code, desc, system, generic, curated=False)


@dataclass
class DtcReport:
    """A timestamped, exportable DTC snapshot for the dashboard / canaryctl."""

    captured_at: str
    stored: list[dict[str, Any]]
    pending: list[dict[str, Any]]
    permanent: list[dict[str, Any]]
    mil_on: bool | None
    dtc_count: int
    #: Mode 02 freeze-frame reading (Task 3.3), read once when a *stored*
    #: DTC first appears this session — channel name (RPM, SPEED, ...) ->
    #: decoded value, same Mode 01 formulas. ``None`` when no freeze frame
    #: has been read yet (no stored DTC seen, or the read hasn't landed).
    freeze_frame: dict[str, Any] | None = None

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


def build_dtc_report(
    *,
    stored: list[str] | None = None,
    pending: list[str] | None = None,
    permanent: list[str] | None = None,
    mil_on: bool | None = None,
    freeze_frame: dict[str, Any] | None = None,
) -> DtcReport:
    """Assemble a described, timestamped snapshot from parsed code lists.

    ``dtc_count`` counts *stored* codes (the SAE convention for the MIL/
    Mode 01 PID 0x01 count); pending/permanent are reported but don't
    inflate the headline number.
    """
    stored = stored or []
    pending = pending or []
    permanent = permanent or []

    def _describe(codes: list[str]) -> list[dict[str, Any]]:
        return [describe_dtc(c).to_dict() for c in codes]

    return DtcReport(
        captured_at=datetime.now(UTC).isoformat(),
        stored=_describe(stored),
        pending=_describe(pending),
        permanent=_describe(permanent),
        mil_on=mil_on,
        dtc_count=len(stored),
        freeze_frame=dict(freeze_frame) if freeze_frame else None,
    )
