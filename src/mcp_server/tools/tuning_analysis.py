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

from src.analysis.tuning import GateError, compare_sessions, delivery_error
from src.analysis.tuning_context import TuningContext
from src.analysis.tuning_frame import build_tuning_frame, section_capabilities
from src.config import CanaryConfig
from src.storage.sessions import resolve_session
from src.vehicle.profiles import resolve_profile_for_tooling, scope_config


def _scoped(config_path: Path, profile: str | None) -> CanaryConfig:
    config = CanaryConfig.from_yaml(config_path)
    return scope_config(config, resolve_profile_for_tooling(config, profile))


def _capability_payload(session_dir: Path, context: TuningContext) -> dict[str, Any] | None:
    if context.providers_path is None:
        return None
    frame = build_tuning_frame(
        session_dir,
        providers_path=context.providers_path,
        calibration=context.calibration,
        afr_stoich=context.afr_stoich,
    )
    return {"roles": frame.capability, "sections": section_capabilities(frame.capability)}


def get_delivery_error(
    config_path: Path,
    *,
    session: str,
    measured_spec: str = "obd:O2_WR_LAMBDA_S1",
    min_load: float | None = None,
    profile: str | None = None,
) -> dict[str, Any]:
    """Measured-vs-commanded AFR at enrichment, with the gates applied.

    Pairing is by acquisition time (``pairing`` in the response, R05.16):
    each commanded-target read meets at most one measured read within the
    vehicle's declared separation (``lambda_delivery.yaml``), and only a
    steady target outside the post-fuel-cut holdoff carries a residual.
    Every other sample is a named gate in ``gates`` — fuel cut, target
    transient, closed loop, below the load floor, outside the rpm bands and
    the rest — so a diluted result is visible rather than silent. Bands
    carry their per-sample p05/p95 beside the mean.

    ``measured_spec`` is explicit on purpose: the ECM's own λ (PID 0x24) and
    the A/F sensor voltage path disagree by ~3.8 % at WOT and nobody yet knows
    which is right, so the tool must not quietly pick a side. The answer
    carries the source it used.

    Both ``pooled_pct`` and ``band_averaged_pct`` are returned. When they
    disagree, rpm coverage is uneven and the band-averaged figure is the one
    to trust — pooling across unequal coverage is what manufactured a
    retracted "K responds 1:1" result.

    ``min_load`` ``None`` (the default) is the vehicle profile's WOT floor,
    ``mode01_wot.load_floor_pct`` in its ``tuning_providers.yaml``; the value
    used is echoed as ``min_load`` with ``min_load_source`` ``profile`` or
    ``caller``.
    """
    scoped = _scoped(config_path, profile)
    try:
        session_dir = resolve_session(scoped.collection.storage_path, session)
    except FileNotFoundError as exc:
        return {"ok": False, "error": str(exc)}

    context = TuningContext.from_config(scoped)
    if context.calibration is None:
        return {
            "ok": False,
            "error": f"no afr_calibration.yaml found ({scoped.daemon.afr_calibration})",
        }
    if context.rpm_bands is None:
        return {
            "ok": False,
            "error": "no mode01_wot.rpm_bands in this vehicle's tuning_providers.yaml",
        }

    min_load_source = "caller" if min_load is not None else "profile"
    try:
        floor = context.resolve_min_load(min_load)
        result = delivery_error(
            session_dir,
            declaration=context.lambda_delivery,
            calibration=context.calibration,
            measured_spec=measured_spec,
            min_load=floor,
            revisions=context.registry,
            afr_stoich=context.afr_stoich,
            mode01_exclusion_note=context.mode01_exclusion_note,
            rpm_bands=context.rpm_bands,
        )
    except (GateError, ValueError) as exc:
        return {"ok": False, "error": str(exc)}
    return {
        "ok": True,
        "capabilities": _capability_payload(session_dir, context),
        "min_load": floor,
        "min_load_source": min_load_source,
        **result.as_dict(),
    }


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
    conditions: str | None = None,
) -> dict[str, Any]:
    """A/B two sessions on a matched grid, refusing calculated load as an axis.

    ``CAL/LD`` is derived from BFS, so conditioning on it while fuelling moves
    partly conditions on what moved — and it is not stable at fixed fuel
    either (it shifted 1.43 points at identical airflow across a same-tune
    null run). Grid on ``rpm × MAF`` (real airflow) or ``rpm × BFS`` (the
    actual table cell). ``unsafe_axes`` overrides, deliberately.

    ``floor_channel`` names an entry in the vehicle's ``noise_floor.yaml`` and
    needs ``conditions``, the regime this comparison was made under. When the
    floor was measured under exactly those conditions every cell carries
    ``delta_sigma`` and ``distinguishable``; otherwise the floor is refused
    (``floor_refusal`` names both regimes) and those read null rather than an
    invented significance — a delta under 2 sigma is not distinguishable from
    changing nothing at all.
    """
    scoped = _scoped(config_path, profile)
    try:
        a = resolve_session(scoped.collection.storage_path, session)
        b = resolve_session(scoped.collection.storage_path, against)
    except FileNotFoundError as exc:
        return {"ok": False, "error": str(exc)}

    context = TuningContext.from_config(scoped)
    floor = context.noise_floor
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
            conditions=conditions,
            revisions=context.registry,
            afr_stoich=context.afr_stoich,
        )
    except (GateError, ValueError) as exc:
        return {"ok": False, "error": str(exc)}

    payload = result.as_dict()
    payload["noise_floor"] = (
        {"channel": floor_channel, "sd": sd, "conditions": result.floor_conditions}
        if sd is not None and result.floor_refusal is None
        else "unavailable"
    )
    return {"ok": True, "capabilities": _capability_payload(a, context), **payload}


__all__ = ["compare_tuning_sessions", "get_delivery_error"]
