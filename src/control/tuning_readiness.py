"""``/status.tuning_readiness``: the Engine Tuning context bar's chips (W02.4).

Each entry is ``{ok, value, detail, fix}``:

* ``ok`` — ``True`` ready, ``False`` not ready, ``None`` reported but not
  judged (the UI's amber). An entry whose ``value`` is ``None`` was not
  reported at all, which the UI shows grey "not reported" — never green.
* ``fix`` — the sentence the UI shows on hover/tap, ``None`` when ``ok``.

Status only. Nothing reads this to refuse an action; the flash interlocks
(W07, :mod:`src.control.nonvolatile_write`) keep their own checks.
"""

from __future__ import annotations

from typing import Any


def readiness_entry(
    ok: bool | None, value: Any, detail: str, fix: str | None = None
) -> dict[str, Any]:
    return {"ok": ok, "value": value, "detail": detail, "fix": None if ok else fix}


def ups_soc_entry(power: dict[str, Any] | None, *, floor_percent: float) -> dict[str, Any]:
    """From the ``/control/power`` snapshot (``build_power_snapshot``)."""
    if power is None or not power.get("watchdog_running"):
        return readiness_entry(
            None,
            None,
            "UPS watchdog heartbeat not found",
            "Start ups-shutdown.service so the UPS charge is reported",
        )
    soc = power.get("soc")
    if not isinstance(soc, int | float):
        return readiness_entry(
            None, None, "UPS watchdog reports no state of charge", "Check the UPS fuel gauge"
        )
    if power.get("watchdog_stale"):
        return readiness_entry(
            None,
            float(soc),
            f"UPS heartbeat is stale; last charge {soc:.0f}%",
            "Check ups-shutdown.service; its heartbeat stopped updating",
        )
    ok = float(soc) >= floor_percent
    return readiness_entry(
        ok,
        float(soc),
        f"UPS at {soc:.0f}% (floor {floor_percent:.0f}%)",
        f"Charge the UPS above {floor_percent:.0f}% before a long capture; "
        "a key-off mid-capture runs on it",
    )


def storage_entry(free_mb: float | None, *, floor_mb: float) -> dict[str, Any]:
    if free_mb is None:
        return readiness_entry(None, None, "free storage not reported")
    ok = free_mb >= floor_mb
    return readiness_entry(
        ok,
        round(free_mb, 1),
        f"{free_mb:.0f} MB free (warning below {floor_mb:.0f} MB)",
        "Free space on the data partition: archive or pull old sessions",
    )


def memory_channels_entry(summary: dict[str, int] | None) -> dict[str, Any]:
    """From :func:`src.control.calibration.memory_verify_summary`."""
    if summary is None:
        return readiness_entry(None, None, "memory-channel catalog not readable")
    total = summary.get("total", 0)
    failed = summary.get("failed", 0)
    passed = summary.get("passed", 0)
    never = summary.get("never_ran", 0)
    detail = (
        f"{passed} passed, {failed} failed, {never} never verified, "
        f"{summary.get('unverifiable', 0)} unverifiable (no check, or one that "
        f"could not discriminate), of {total}"
    )
    if total == 0:
        return readiness_entry(None, summary, "no memory channels catalogued")
    if failed:
        return readiness_entry(
            False,
            summary,
            detail,
            "A RAM address failed its known-answer check: re-verify on the Calibration "
            "page before trusting its channel",
        )
    if never:
        return readiness_entry(
            None, summary, detail, "Run re-verify on the Calibration page (engine running)"
        )
    return readiness_entry(True, summary, detail)


def calibration_entry(calibration: dict[str, Any]) -> dict[str, Any]:
    """From :func:`src.control.calibration.calibration_status`."""
    ecu_id = calibration.get("ecu_calibration_id")
    if ecu_id is None:
        return readiness_entry(
            None,
            None,
            "ECM calibration id not yet read",
            "Key on: the id is read when the ECM answers",
        )
    matches = calibration.get("calibration_matches_registry")
    revision = calibration.get("running_revision")
    if matches is True:
        if calibration.get("calibration_changed"):
            return readiness_entry(
                None,
                ecu_id,
                f"{ecu_id} matches {revision}, but the id changed during this daemon lifetime",
                "Confirm the new flash is recorded on the Calibration page",
            )
        return readiness_entry(True, ecu_id, f"{ecu_id} matches {revision}")
    if matches is False:
        return readiness_entry(
            False,
            ecu_id,
            f"{ecu_id} is not in flash history "
            f"(running revision {revision} records {calibration.get('registry_calibration_id')})",
            "Record the flash on the Calibration page before trusting any RAM address",
        )
    return readiness_entry(
        None,
        ecu_id,
        f"{ecu_id} read; flash history records no calibration id for {revision or 'any revision'}",
        "Add calibration_id to the running revision in tune_revisions.yaml",
    )


_VEHICLE_STATE: dict[str, tuple[bool | None, str, str | None]] = {
    "engine_running": (True, "engine running", None),
    "ignition_only": (None, "key on, engine off", "Start the engine to log driving data"),
    "engine_off": (False, "key off: the ECM is not answering", "Turn the key on"),
    "parked": (False, "parked: the vehicle's bus is asleep (key off)", "Turn the key on"),
    "bench": (False, "on the bench: no vehicle on the OBD port", "Plug the Pi into the vehicle"),
    "no_data": (
        False,
        "no fresh CAN, OBD or 12 V supply reading",
        "Check the Pi's connection to the vehicle",
    ),
    "replay": (None, "replay active: data is re-emitted", "Stop the replay for live data"),
    "unknown": (None, "vehicle state not classified", "Read the raw vehicle_state fields"),
}


def vehicle_state_entry(state: str | None) -> dict[str, Any]:
    if state is None:
        return readiness_entry(None, None, "vehicle state not reported")
    ok, detail, fix = _VEHICLE_STATE.get(state, (None, f"state {state}", None))
    return readiness_entry(ok, state, detail, fix)


def build_tuning_readiness(
    *,
    power: dict[str, Any] | None,
    ups_floor_percent: float,
    free_mb: float | None,
    storage_floor_mb: float,
    memory_summary: dict[str, int] | None,
    calibration: dict[str, Any],
    vehicle_state: str | None,
) -> dict[str, dict[str, Any]]:
    return {
        "ups_soc": ups_soc_entry(power, floor_percent=ups_floor_percent),
        "storage_free_mb": storage_entry(free_mb, floor_mb=storage_floor_mb),
        "memory_channels": memory_channels_entry(memory_summary),
        "calibration": calibration_entry(calibration),
        "vehicle_state": vehicle_state_entry(vehicle_state),
    }


__all__ = [
    "build_tuning_readiness",
    "calibration_entry",
    "memory_channels_entry",
    "readiness_entry",
    "storage_entry",
    "ups_soc_entry",
    "vehicle_state_entry",
]
