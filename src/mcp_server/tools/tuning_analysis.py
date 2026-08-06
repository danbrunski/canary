"""Tuning-gate read tools (roadmap Phase 2.5).

Local, like ``get_behavior_deviations`` and ``query_telemetry`` — pandas over
stored Parquet, no daemon round-trip, so these work against a pulled corpus
with the truck switched off.

The gates these expose are the ones the 2026-08 campaign retracted findings
for skipping. They refuse the unsafe form rather than trusting the caller, and
return ``{"ok": False, "error": ...}`` on a gate refusal instead of raising,
matching the validation-error convention of the other local tools.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from src.analysis.afr import load_afr_calibration
from src.analysis.noise_floor import load_noise_floor
from src.analysis.tune_revisions import load_tune_revisions
from src.analysis.tuning import GateError, compare_sessions, delivery_error
from src.config import CanaryConfig
from src.control.sessions import resolve_session
from src.profiles import resolve_profile_for_tooling, scope_config


def _scoped(config_path: Path, profile: str | None) -> CanaryConfig:
    config = CanaryConfig.from_yaml(config_path)
    return scope_config(config, resolve_profile_for_tooling(config, profile))


def get_delivery_error(
    config_path: Path,
    *,
    session: str,
    measured_spec: str = "obd:O2_WR_LAMBDA_S1",
    min_load: float | None = 85.0,
    cell_agg: str = "last",
    profile: str | None = None,
) -> dict[str, Any]:
    """Measured-vs-commanded AFR at enrichment, with the gates applied.

    Gates, each reported in the response's ``gates`` block so a diluted result
    is visible rather than silent: same-instant pairing (no stale joins), open
    loop **and** a commanded mixture richer than stoich (so decel fuel cut is
    excluded by construction, not by a guessed threshold), an optional load
    floor, and band-averaging over rpm so band composition cannot masquerade
    as an effect.

    ``measured_spec`` is explicit on purpose: the ECM's own λ (PID 0x24) and
    the A/F sensor voltage path disagree by ~3.8 % at WOT and nobody yet knows
    which is right, so the tool must not quietly pick a side. The answer
    carries the source it used.

    Both ``pooled_pct`` and ``band_averaged_pct`` are returned. When they
    disagree, rpm coverage is uneven and the band-averaged figure is the one
    to trust — pooling across unequal coverage is what manufactured a
    retracted "K responds 1:1" result.
    """
    scoped = _scoped(config_path, profile)
    try:
        session_dir = resolve_session(scoped.collection.storage_path, session)
    except FileNotFoundError as exc:
        return {"ok": False, "error": str(exc)}

    calibration_path = scoped.daemon.afr_calibration
    if not calibration_path.exists():
        return {"ok": False, "error": f"no afr_calibration.yaml found ({calibration_path})"}

    try:
        revisions = load_tune_revisions(
            scoped.daemon.afr_calibration.parent / "tune_revisions.yaml"
        )
        result = delivery_error(
            session_dir,
            calibration=load_afr_calibration(calibration_path),
            measured_spec=measured_spec,
            min_load=min_load,
            cell_agg=cell_agg,
            revisions=revisions,
        )
    except (GateError, ValueError) as exc:
        return {"ok": False, "error": str(exc)}
    return {"ok": True, **result.as_dict()}


def compare_tuning_sessions(
    config_path: Path,
    *,
    session: str,
    against: str,
    value: str,
    axes: tuple[str, ...] = ("obd:RPM", "obd:MAF"),
    floor_channel: str | None = None,
    unsafe_axes: bool = False,
    profile: str | None = None,
) -> dict[str, Any]:
    """A/B two sessions on a matched grid, refusing calculated load as an axis.

    ``CAL/LD`` is derived from BFS, so conditioning on it while fuelling moves
    partly conditions on what moved — and it is not stable at fixed fuel
    either (it shifted 1.43 points at identical airflow across a same-tune
    null run). Grid on ``rpm × MAF`` (real airflow) or ``rpm × BFS`` (the
    actual table cell). ``unsafe_axes`` overrides, deliberately.

    ``floor_channel`` names an entry in the vehicle's ``noise_floor.yaml``; when
    it resolves, every cell carries ``delta_sigma`` and ``distinguishable``.
    Without it those read null rather than an invented significance — a delta
    under 2 sigma is not distinguishable from changing nothing at all.
    """
    scoped = _scoped(config_path, profile)
    try:
        a = resolve_session(scoped.collection.storage_path, session)
        b = resolve_session(scoped.collection.storage_path, against)
    except FileNotFoundError as exc:
        return {"ok": False, "error": str(exc)}

    floor = load_noise_floor(scoped.daemon.afr_calibration.parent / "noise_floor.yaml")
    revisions = load_tune_revisions(scoped.daemon.afr_calibration.parent / "tune_revisions.yaml")
    sd = floor.sd_for(floor_channel) if (floor is not None and floor_channel) else None

    try:
        result = compare_sessions(
            a,
            b,
            value=value,
            axes=tuple(axes),
            unsafe_axes=unsafe_axes,
            noise_floor=floor if floor_channel else None,
            floor_channel=floor_channel,
            revisions=revisions,
        )
    except (GateError, ValueError) as exc:
        return {"ok": False, "error": str(exc)}

    payload = result.as_dict()
    payload["noise_floor"] = (
        {"channel": floor_channel, "sd": sd, "conditions": result.floor_conditions}
        if sd is not None
        else "unavailable"
    )
    return {"ok": True, **payload}


__all__ = ["compare_tuning_sessions", "get_delivery_error"]
