"""Whole-session reads behind the Engine Tuning Logs and Analysis pages (W04).

Pure analysis over session directories: nothing here knows about HTTP, the
daemon, or capture state. :mod:`src.control.workbench_jobs` runs every entry
point in the bounded single-flight child the digest uses and refuses while a
capture is recording; call these directly only on the laptop.

Every function states what it could *not* answer and which kind of "could not"
it was, because the two kinds demand opposite responses (AGENTS.md, "An empty
result must say which kind of empty it is"):

* ``not_captured`` — the session never held an input. No amount of driving
  changes that; the capture profile has to.
* the gates removed everything — a real answer about the drive, reported as a
  gate report whose ``remaining`` is zero, never as an absent result.

Binning is on rpm x airflow, never calculated load: ``CAL/LD`` is derived from
BFS and moves when fuelling does (:data:`src.analysis.tuning.FORBIDDEN_AXES`).
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any, Literal, cast

import numpy as np
import pandas as pd

from src.analysis.channels import (
    ChannelSpecError,
    align_long,
    concat_channel_frames,
    parse_spec,
    read_channels,
)
from src.analysis.noise_floor import NoiseFloor, Significance, significance
from src.analysis.tuning import (
    DELIVERY_ENGINE_VERSION,
    DeliveryRoute,
    GateError,
    GateReport,
    InputsNotCapturedError,
    _assert_axis_allowed,
    bracket_ends,
    delay_sensitivity,
    delivery_error_from_gated,
    gate_delivery_samples,
    mode01_route,
    pairing_block,
    read_delivery_inputs,
    semantic_route,
)
from src.analysis.tuning_context import TuningContext
from src.analysis.tuning_frame import ProviderRegistry
from src.paths import BINDINGS_YAML, DIDS_YAML
from src.session import TAGS_FILENAME, load_tags
from src.storage.capture_manifest import read_capture_manifest
from src.storage.session_files import session_footer_span
from src.storage.sessions import session_started_at
from src.vehicle.tune_revisions import TuneRevisionRegistry, resolve_session_revision

Regime = Literal["all", "closed_loop", "enrichment", "fuel_cut", "idle"]
REGIMES: tuple[Regime, ...] = ("all", "closed_loop", "enrichment", "fuel_cut", "idle")

#: Hard ceiling on rows one frame response may carry. The caller pages with
#: ``offset``; a response over the cap says so (``capped``) rather than
#: silently dropping the tail.
FRAME_ROW_CAP = 20_000

#: Mode 01 inputs. J1979 names, not vehicle facts — any OBD-II vehicle
#: answers them under these names — so they are defaults, overridable per
#: request, rather than profile data.
RPM_SPEC = "obd:RPM"
AIRFLOW_SPEC = "obd:MAF"
FUEL_STATUS_SPEC = "obd:FUEL_STATUS"
EQUIV_RATIO_SPEC = "obd:COMMANDED_EQUIV_RATIO"
SPEED_SPEC = "obd:SPEED"
BARO_SPEC = "obd:BAROMETRIC_PRESSURE"
TIMING_SPEC = "obd:TIMING_ADVANCE"
TRIM_SPECS: dict[str, tuple[str, str]] = {
    "b1": ("obd:SHORT_FUEL_TRIM_1", "obd:LONG_FUEL_TRIM_1"),
    "b2": ("obd:SHORT_FUEL_TRIM_2", "obd:LONG_FUEL_TRIM_2"),
}
#: The regime a whole-drive rpm x airflow comparison is made under: every row,
#: unfiltered. A noise floor is quoted against it only if it was measured under
#: exactly this (R05.2).
ALL_CONDITIONS = "all conditions: every row, binned on rpm x airflow"
#: J1979 FUEL_STATUS value for "closed loop, using oxygen sensor feedback".
CLOSED_LOOP = 2.0

#: Default cell edges. rpm in 500s across the running range; airflow (g/s) in
#: steps that widen with flow so light-load cells are not one giant bucket.
DEFAULT_RPM_EDGES: tuple[float, ...] = tuple(float(v) for v in range(500, 7001, 500))
DEFAULT_AIRFLOW_EDGES: tuple[float, ...] = (
    0.0,
    3.0,
    6.0,
    10.0,
    15.0,
    20.0,
    30.0,
    40.0,
    60.0,
    80.0,
    100.0,
    130.0,
    170.0,
    250.0,
)

#: Cells with fewer samples than this are returned but flagged ``sparse``;
#: the page greys them rather than colouring a mean of two readings.
DEFAULT_MIN_CELL_N = 5


@dataclass(frozen=True, kw_only=True)
class WorkbenchInputs(TuningContext):
    """Everything a whole-session read needs besides the sessions themselves.

    The vehicle's :class:`~src.analysis.tuning_context.TuningContext` plus the
    catalogs to decode with. Plain data so it pickles into the analysis child.
    Vehicle facts arrive from profile data (calibration, providers, flash
    registry, noise floor, the ``idle`` operating mode, stoichiometry from the
    configured fuel); nothing vehicle-specific is coded.
    """

    bindings_path: Path = BINDINGS_YAML
    dids_path: Path = DIDS_YAML


@dataclass
class Unavailable:
    """A session (or a view) that could not contribute, and which kind."""

    session_id: str
    kind: Literal["not_captured", "refused", "over_budget"]
    reason: str
    missing: list[str] = field(default_factory=list)

    def as_dict(self) -> dict[str, Any]:
        return {
            "session_id": self.session_id,
            "kind": self.kind,
            "reason": self.reason,
            "missing": list(self.missing),
        }


def _clean(value: Any) -> Any:
    """JSON-safe scalar: NaN/inf become ``None``, numpy scalars become Python."""
    if value is None:
        return None
    if isinstance(value, (bool, np.bool_)):
        return bool(value)
    if isinstance(value, (int, np.integer)):
        return int(value)
    if isinstance(value, (float, np.floating)):
        f = float(value)
        return f if math.isfinite(f) else None
    if isinstance(value, pd.Timestamp):
        return value.isoformat()
    return value


def _column(series: pd.Series) -> list[Any]:
    return [_clean(v) for v in series.tolist()]


# ---------------------------------------------------------------------------
# Revision identity and boundaries


def revision_boundaries(
    registry: TuneRevisionRegistry | None,
    start: datetime | None,
    end: datetime | None,
) -> list[dict[str, Any]]:
    """Flashes that fall strictly inside ``(start, end]``.

    The registry assigns a revision by session *start*, so a log spanning a
    flash carries one label and two calibrations. Every figure computed across
    such a boundary mixes them; the page draws the marker and says so.
    """
    if registry is None or start is None or end is None:
        return []
    out: list[dict[str, Any]] = []
    for revision in registry.revisions:
        flashed = pd.Timestamp(revision.flashed_at)
        flashed = flashed.tz_localize("UTC") if flashed.tzinfo is None else flashed
        if pd.Timestamp(start) < flashed <= pd.Timestamp(end):
            out.append({"revision": revision.revision, "flashed_at": flashed.isoformat()})
    return out


def session_span(session_dir: Path) -> tuple[datetime | None, datetime | None, int | None]:
    """First/last timestamp and row count from Parquet footers alone.

    No data read — row-group statistics and ``num_rows`` — so the capture
    browser can list duration and size without loading a session. ``None``
    means unknown (nothing readable, or chunks written without statistics),
    never zero. The footer walk is the loader's
    (:func:`src.storage.session_files.session_footer_span`, R05.14).
    """
    footer = session_footer_span(session_dir)
    return (
        None if footer.first is None else footer.first.to_pydatetime(),
        None if footer.last is None else footer.last.to_pydatetime(),
        footer.rows,
    )


def session_revision(session_dir: Path, registry: TuneRevisionRegistry | None) -> dict[str, Any]:
    revision, source, conflict = resolve_session_revision(session_dir, registry)
    return {"revision": revision, "source": source, "conflict": conflict}


# ---------------------------------------------------------------------------
# W04.3/W04.4 — one log as a wide frame


def _commanded_target_provider(providers: ProviderRegistry | None) -> str | None:
    """The phi channel the vehicle's registry pins for commanded AFR, if any."""
    if providers is None:
        return None
    for provider in providers.providers:
        if provider.role == "commanded_afr" and provider.transform == "equiv_ratio_afr":
            return provider.spec
    return None


def _regime_specs(inputs: WorkbenchInputs) -> dict[str, str]:
    specs = {
        "fuel_status": FUEL_STATUS_SPEC,
        "equiv_ratio": EQUIV_RATIO_SPEC,
        "rpm": RPM_SPEC,
        "speed": SPEED_SPEC,
    }
    target = _commanded_target_provider(inputs.providers)
    if target is not None and parse_spec(target).source == "mem":
        specs["target_phi"] = target
        specs["target_sentinel"] = f"{target}_SENTINEL"
    return specs


def _classify_regimes(
    aligned: pd.DataFrame,
    present: set[str],
    specs: dict[str, str],
    inputs: WorkbenchInputs,
) -> tuple[dict[str, pd.Series], dict[str, dict[str, Any]]]:
    """Per-cell regime flags (True/False/None = unknown) and availability.

    A regime whose inputs the session never held is *unavailable* with the
    channel named — not an empty filter result.
    """
    index = aligned.index

    def col(key: str) -> pd.Series | None:
        spec = specs.get(key)
        if spec is None or spec not in present:
            return None
        return pd.to_numeric(aligned[spec], errors="coerce")

    flags: dict[str, pd.Series] = {}
    availability: dict[str, dict[str, Any]] = {}

    fuel_status = col("fuel_status")
    if fuel_status is None:
        availability["closed_loop"] = {
            "available": False,
            "reason": f"This capture's profile does not poll {FUEL_STATUS_SPEC}",
        }
    else:
        flags["closed_loop"] = (
            fuel_status.eq(CLOSED_LOOP).astype("boolean").mask(fuel_status.isna())
        )
        availability["closed_loop"] = {"available": True, "source": FUEL_STATUS_SPEC}

    # Fuel cut and enrichment: from the registry's pinned phi word when the log
    # has it (sentinel = cut), else from PID 0x44 (0.0 = cut).
    target_phi = col("target_phi")
    sentinel = col("target_sentinel")
    ratio = col("equiv_ratio")
    if target_phi is not None or sentinel is not None:
        phi = target_phi if target_phi is not None else pd.Series(np.nan, index=index)
        cut_flag = sentinel.notna() if sentinel is not None else pd.Series(False, index=index)
        known = phi.notna() | cut_flag
        flags["fuel_cut"] = cut_flag.astype("boolean").mask(~known)
        flags["enrichment"] = (phi.gt(1.0) & ~cut_flag).astype("boolean").mask(~known)
        source = specs.get("target_phi")
        availability["fuel_cut"] = {
            "available": True,
            "source": specs.get("target_sentinel"),
        }
        availability["enrichment"] = {"available": True, "source": source}
    elif ratio is not None:
        flags["fuel_cut"] = ratio.le(0.0).astype("boolean").mask(ratio.isna())
        availability["fuel_cut"] = {"available": True, "source": EQUIV_RATIO_SPEC}
        convention = (
            None
            if inputs.calibration is None
            else inputs.calibration.commanded_equiv_ratio_convention
        )
        if convention in ("phi", "lambda"):
            rich = ratio.gt(1.0) if convention == "phi" else (ratio.lt(1.0) & ratio.gt(0.0))
            flags["enrichment"] = rich.astype("boolean").mask(ratio.isna())
            availability["enrichment"] = {"available": True, "source": EQUIV_RATIO_SPEC}
        else:
            availability["enrichment"] = {
                "available": False,
                "reason": (
                    "commanded_equiv_ratio_convention is unverified for this vehicle, so "
                    f"{EQUIV_RATIO_SPEC} cannot say which side of stoich is rich"
                ),
            }
    else:
        reason = f"This capture's profile does not poll {EQUIV_RATIO_SPEC}" + (
            f" or {specs['target_phi']}" if "target_phi" in specs else ""
        )
        availability["fuel_cut"] = {"available": False, "reason": reason}
        availability["enrichment"] = {"available": False, "reason": reason}

    idle = inputs.idle_spec
    rpm = col("rpm")
    speed = col("speed")
    if idle is None:
        availability["idle"] = {
            "available": False,
            "reason": "this vehicle's config defines no `idle` operating mode",
        }
    elif rpm is None or (idle.get("speed_max") is not None and speed is None):
        needed = [RPM_SPEC] + ([SPEED_SPEC] if idle.get("speed_max") is not None else [])
        availability["idle"] = {
            "available": False,
            "reason": f"This capture's profile does not poll {' and '.join(needed)}",
        }
    else:
        verdict = pd.Series(True, index=index)
        known = rpm.notna()
        if idle.get("rpm_min") is not None:
            verdict &= rpm.ge(float(idle["rpm_min"]))  # type: ignore[arg-type]
        if idle.get("rpm_max") is not None:
            verdict &= rpm.le(float(idle["rpm_max"]))  # type: ignore[arg-type]
        if idle.get("speed_max") is not None and speed is not None:
            verdict &= speed.le(float(idle["speed_max"]))  # type: ignore[arg-type]
            known &= speed.notna()
        flags["idle"] = verdict.astype("boolean").mask(~known)
        availability["idle"] = {
            "available": True,
            "source": "operating_modes.idle",
            "definition": {k: v for k, v in idle.items() if v is not None},
        }
    return flags, availability


def _read_optional(
    session: Path, specs: list[str], inputs: WorkbenchInputs
) -> tuple[pd.DataFrame, list[str]]:
    """Read what resolves; return the long frame and the specs that did not."""
    frames: list[pd.DataFrame] = []
    unresolved: list[str] = []
    for spec in dict.fromkeys(specs):
        try:
            frames.append(
                read_channels(
                    session,
                    [spec],
                    bindings_path=inputs.bindings_path,
                    dids_path=inputs.dids_path,
                    calibration=inputs.calibration,
                    afr_stoich=inputs.afr_stoich,
                )
            )
        except (ChannelSpecError, ValueError, KeyError):
            unresolved.append(spec)
    if not frames:
        return pd.DataFrame(columns=["timestamp", "spec", "value", "unit"]), unresolved
    return concat_channel_frames(frames), unresolved


def session_frame(
    session_dir: Path,
    specs: list[str],
    *,
    inputs: WorkbenchInputs,
    grid: str = "1s",
    regime: Regime = "all",
    offset: int = 0,
    max_rows: int = FRAME_ROW_CAP,
) -> dict[str, Any]:
    """One log's channels on one clock, with units, freshness and regime.

    Built on :func:`~src.analysis.channels.align_long` (the ``read_aligned``
    contract): a row exists only where **every** requested channel has its own
    sample in the grid cell. The cells that held some channels but not all are
    counted as ``freshness.dropped`` rather than forward-filled.

    Reads the session once — :func:`read_channels` for values and units — and
    raises :class:`ChannelSpecError` for a spec the catalogs cannot resolve.
    """
    if not specs:
        raise ChannelSpecError("name at least one channel")
    if regime not in REGIMES:
        raise ChannelSpecError(f"unknown regime {regime!r}; expected one of {', '.join(REGIMES)}")
    names = [parse_spec(text).text for text in specs]
    requested = read_channels(
        session_dir,
        names,
        bindings_path=inputs.bindings_path,
        dids_path=inputs.dids_path,
        calibration=inputs.calibration,
        afr_stoich=inputs.afr_stoich,
    )
    regime_specs = _regime_specs(inputs)
    aux_specs = [spec for spec in regime_specs.values() if spec not in names]
    aux, _unresolved = _read_optional(session_dir, aux_specs, inputs)
    long = concat_channel_frames([requested, aux]) if not aux.empty else requested
    present = set(long["spec"].unique()) if not long.empty else set()
    missing = [name for name in names if name not in present]

    units: dict[str, str | None] = {}
    for name in names:
        rows = requested[requested["spec"] == name]
        units[name] = None if rows.empty else str(rows["unit"].iloc[-1] or "") or None

    all_specs = names + [spec for spec in aux_specs if spec in present]
    aligned = align_long(long, all_specs, grid=grid, require_all=False) if present else None
    if aligned is None or aligned.empty:
        aligned = pd.DataFrame(columns=["timestamp", *all_specs])
    for spec in all_specs:
        if spec not in aligned:
            aligned[spec] = np.nan

    any_requested = aligned[names].notna().any(axis=1)
    all_requested = aligned[names].notna().all(axis=1)
    cells_with_any = int(any_requested.sum())
    kept = aligned[all_requested].reset_index(drop=True)
    freshness = {
        "cells_with_any": cells_with_any,
        "kept": int(len(kept)),
        "dropped": cells_with_any - int(len(kept)),
        "rule": "a row exists only where every requested channel was sampled in the cell",
    }

    flags, availability = _classify_regimes(kept, present, regime_specs, inputs)
    regime_report: dict[str, Any] = {"regime": regime, "before": int(len(kept))}
    if regime != "all":
        entry = availability.get(regime, {"available": False, "reason": "unknown regime"})
        if not entry.get("available"):
            regime_report.update({"available": False, "reason": entry.get("reason")})
            kept = kept.iloc[0:0]
            flags = {key: value.iloc[0:0] for key, value in flags.items()}
        else:
            verdict = flags[regime]
            regime_report["unknown"] = int(verdict.isna().sum())
            mask = verdict.fillna(False).astype(bool).to_numpy()
            regime_report["removed"] = int((~mask).sum()) - regime_report["unknown"]
            kept = kept[mask].reset_index(drop=True)
            flags = {key: value[mask].reset_index(drop=True) for key, value in flags.items()}
            regime_report["available"] = True
    regime_report["after"] = int(len(kept))

    total = int(len(kept))
    start = max(0, int(offset))
    cap = max(1, min(int(max_rows), FRAME_ROW_CAP))
    page = kept.iloc[start : start + cap]
    capped = total > start + cap

    timestamps = pd.to_datetime(page["timestamp"], utc=True) if not page.empty else pd.Series([])
    started = session_started_at(session_dir)
    first_ts = pd.to_datetime(aligned["timestamp"], utc=True).min() if len(aligned) else None
    last_ts = pd.to_datetime(aligned["timestamp"], utc=True).max() if len(aligned) else None
    manifest = read_capture_manifest(session_dir)
    tags = [
        {
            "t": pd.Timestamp(float(tag["t"]), unit="s", tz="UTC").isoformat(),
            "label": str(tag["label"]),
        }
        for tag in load_tags(session_dir / TAGS_FILENAME)
    ]
    boundaries = revision_boundaries(
        inputs.registry,
        started
        if started is not None
        else (None if first_ts is None else first_ts.to_pydatetime()),
        None if last_ts is None or pd.isna(last_ts) else last_ts.to_pydatetime(),
    )
    return {
        "session_id": session_dir.name,
        "profile": None if manifest is None else manifest.profile,
        "grid": grid,
        "columns": [{"spec": name, "unit": units[name]} for name in names],
        "missing": [
            {"spec": name, "reason": f"This capture's profile does not poll {name}"}
            for name in missing
        ],
        "timestamps": [ts.isoformat() for ts in timestamps],
        "values": {name: _column(page[name]) for name in names},
        "regime_flags": {
            key: _column(value.iloc[start : start + cap]) for key, value in flags.items()
        },
        "regimes": availability,
        "regime_report": regime_report,
        "freshness": freshness,
        "rows_total": total,
        "offset": start,
        "rows_returned": int(len(page)),
        "row_cap": cap,
        "capped": capped,
        "tags": tags,
        "revision": session_revision(session_dir, inputs.registry),
        "revision_boundaries": boundaries,
        "span": {
            "first": None if first_ts is None or pd.isna(first_ts) else first_ts.isoformat(),
            "last": None if last_ts is None or pd.isna(last_ts) else last_ts.isoformat(),
        },
    }


# ---------------------------------------------------------------------------
# Conditions (W04.12)


def _baro_range(sessions: list[Path], inputs: WorkbenchInputs) -> dict[str, Any]:
    lows: list[float] = []
    highs: list[float] = []
    without: list[str] = []
    for session in sessions:
        frame, _ = _read_optional(session, [BARO_SPEC], inputs)
        values = pd.to_numeric(frame["value"], errors="coerce").dropna()
        if values.empty:
            without.append(session.name)
            continue
        lows.append(float(values.min()))
        highs.append(float(values.max()))
    return {
        "spec": BARO_SPEC,
        "min_kpa": min(lows) if lows else None,
        "max_kpa": max(highs) if highs else None,
        "sessions_without": without,
    }


def conditions_block(
    sessions: list[Path],
    inputs: WorkbenchInputs,
    *,
    gates: dict[str, Any] | None = None,
    extra: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Sessions pooled, revision(s), barometric range, gates and rows removed.

    A number without these is how this project got six retractions; every
    Analysis figure carries one.
    """
    revisions: dict[str, list[str]] = {}
    boundaries: dict[str, list[dict[str, Any]]] = {}
    for session in sessions:
        identity = session_revision(session, inputs.registry)
        key = identity["revision"] or "unknown"
        revisions.setdefault(key, []).append(session.name)
        first, last, _rows = session_span(session)
        crossed = revision_boundaries(inputs.registry, session_started_at(session) or first, last)
        if crossed:
            boundaries[session.name] = crossed
    return {
        "sessions": [session.name for session in sessions],
        "n_sessions": len(sessions),
        "revisions": revisions,
        "mixed_revisions": len(revisions) > 1 or bool(boundaries),
        "revision_boundaries": boundaries,
        "baro": _baro_range(sessions, inputs),
        "gates": gates,
        **(extra or {}),
    }


def noise_floor_payload(floor: NoiseFloor | None, channel: str) -> dict[str, Any]:
    """The measured floor for ``channel`` with its conditions, or unavailable."""
    entry = None if floor is None else floor.channels.get(channel)
    if entry is None:
        return {"available": False, "channel": channel, "reason": "no floor measured"}
    return {
        "available": True,
        "channel": channel,
        "unit": entry.unit,
        "sd": entry.sd,
        "mean": entry.mean,
        "n_cells": entry.n_cells,
        "min": entry.min,
        "max": entry.max,
        "conditions": entry.conditions,
        "provenance": dict(floor.provenance) if floor is not None else {},
    }


# ---------------------------------------------------------------------------
# W04.7 — delivery error, pooled


MEASURED_SEMANTIC_PREFIX = "semantic:"


def delivery_route(
    inputs: WorkbenchInputs, *, measured: str, min_load: float | None
) -> DeliveryRoute:
    """The declared route ``measured`` names.

    ``semantic:<role>`` is the all-DID route: the provider registry's
    commanded target, rpm and ``<role>`` channel (the A/F voltage through the
    calibrated transfer). Anything else is a Mode 01 measured spec against
    PID 0x44. Refuses (:class:`GateError`) without a ``lambda_delivery.yaml``
    or when ``measured`` is not a declared measured source.
    """
    declaration = inputs.lambda_delivery
    if declaration is None:
        raise GateError(
            "no lambda_delivery.yaml for this vehicle: nothing declares its commanded target "
            "or what each measured source is, so delivery error is refused"
        )
    if measured.startswith(MEASURED_SEMANTIC_PREFIX):
        if min_load is not None:
            raise GateError(
                "a load floor needs a validated load provider, and the semantic (A/F voltage) "
                "path has none: calculated load is Mode 01 only. Clear min_load, or use the "
                "Mode 01 measured source."
            )
        if inputs.providers is None:
            raise GateError("no tuning_providers.yaml for this vehicle")
        role = measured[len(MEASURED_SEMANTIC_PREFIX) :]
        return semantic_route(declaration, inputs.providers, measured_role=role)
    if inputs.calibration is None:
        raise GateError("no afr_calibration.yaml for this vehicle; commanded AFR cannot resolve")
    return mode01_route(declaration, measured_spec=measured, min_load=min_load)


def _session_delivery(
    session: Path,
    inputs: WorkbenchInputs,
    route: DeliveryRoute,
    *,
    bands: tuple[float, ...],
    min_load: float | None,
    ends: tuple[float, float] | None = None,
) -> dict[float | None, tuple[pd.DataFrame, GateReport]] | Unavailable:
    """One session's gated delivery samples, or which kind of unavailable.

    Keyed by enrichment delay: ``None`` is the route's declared one, and each
    bracket end in ``ends`` is the same session re-gated at that delay.

    A session whose inputs were never captured (or never overlapped) is
    ``not_captured`` with the absent inputs named (R05.11 — read from the
    error's fields, not its text); any other refusal is ``refused``.
    """
    assert inputs.lambda_delivery is not None  # delivery_route refused otherwise
    try:
        read = read_delivery_inputs(
            session,
            route,
            inputs.lambda_delivery,
            calibration=inputs.calibration,
            afr_stoich=inputs.afr_stoich,
            bindings_path=inputs.bindings_path,
            dids_path=inputs.dids_path,
            mode01_exclusion_note=inputs.mode01_exclusion_note,
        )
        declaration = inputs.lambda_delivery
        return {
            delay: gate_delivery_samples(
                read,
                route,
                declaration=declaration,
                calibration=inputs.calibration,
                afr_stoich=inputs.afr_stoich,
                rpm_bands=bands,
                min_load=min_load,
                session=session,
                mode01_exclusion_note=inputs.mode01_exclusion_note,
                enrichment_delay_s=delay,
            )
            for delay in (None, *(ends or ()))
        }
    except InputsNotCapturedError as exc:
        return Unavailable(session.name, "not_captured", str(exc), missing=list(exc.missing))
    except ValueError as exc:  # GateError and clock refusals included
        return Unavailable(session.name, "refused", str(exc))


def pooled_delivery_error(
    sessions: list[Path],
    *,
    inputs: WorkbenchInputs,
    measured: str = "obd:O2_WR_LAMBDA_S1",
    min_load: float | None = None,
    rpm_bands: tuple[float, ...] | None = None,
    unavailable: list[Unavailable] | None = None,
) -> dict[str, Any]:
    """Delivery error by rpm band over several sessions, with every gate.

    ``measured`` names the measured-AFR source and is never chosen for the
    caller: ``obd:O2_WR_LAMBDA_S1`` (the ECM's own λ, Mode 01 route) and
    ``semantic:measured_afr_b1``/``_b2`` (the A/F sensor voltage through the
    calibrated transfer) disagree by ~3.8 %.

    Each session is paired and gated on its own acquisition clock
    (:func:`src.analysis.tuning.gate_delivery_samples`, R05.16), then the
    pool is band-averaged once, so the band average weighs bands equally
    across the pool and per-session composition cannot masquerade as an
    effect. Each session's own n is reported beside it.
    """
    skipped = list(unavailable or [])
    bands = rpm_bands if rpm_bands is not None else inputs.rpm_bands
    if bands is None:
        raise GateError(
            "no rpm bands for this vehicle: declare mode01_wot.rpm_bands in its "
            "tuning_providers.yaml (the top edge is the engine's redline)"
        )
    route = delivery_route(inputs, measured=measured, min_load=min_load)
    assert inputs.lambda_delivery is not None
    pairing = pairing_block(route, inputs.lambda_delivery)
    ends = bracket_ends(route, inputs.lambda_delivery)
    end_parts: dict[float, list[tuple[pd.DataFrame, GateReport]]] = {e: [] for e in ends or ()}
    gated_parts: list[pd.DataFrame] = []
    pooled_report = GateReport()
    per_session: list[dict[str, Any]] = []
    for session in sessions:
        outcome = _session_delivery(
            session, inputs, route, bands=bands, min_load=min_load, ends=ends
        )
        if isinstance(outcome, Unavailable):
            skipped.append(outcome)
            continue
        gated, report = outcome[None]
        for end in ends or ():
            end_parts[end].append(outcome[end])
        own = delivery_error_from_gated(gated, report, measured_spec=measured, rpm_bands=bands)
        identity = session_revision(session, inputs.registry)
        per_session.append(
            {
                "session_id": session.name,
                "n": own.n,
                "gates": own.gates,
                "revision": identity["revision"],
                "band_averaged_pct": _clean(own.band_averaged_pct),
                # Not independent evidence: this drive chose the constants.
                "informed_constants": session.name in pairing["constants_informed_by"],
            }
        )
        gated_parts.append(gated)
        pooled_report.merge(report)

    contributing = [Path(s) for s in sessions if s.name in {p["session_id"] for p in per_session}]
    floor = noise_floor_payload(inputs.noise_floor, "delivery_error_pct")
    base: dict[str, Any] = {
        "engine_version": DELIVERY_ENGINE_VERSION,
        "measured": measured,
        "min_load": min_load,
        "rpm_bands": list(bands),
        "per_session": per_session,
        "unavailable": [item.as_dict() for item in skipped],
        "noise_floor": floor,
        # What "same instant" means here, stated rather than implied: each
        # commanded-target read is paired with at most one measured read
        # within the declared acquisition separation, and only steady-target
        # samples carry a residual (R05.16; it replaced a 1 s last-cell grid).
        "pairing": pairing,
        # The result at each end of the route's delay bracket; None when the
        # route models no delay (R05.16).
        "delay_sensitivity": None,
    }
    if not per_session:
        base.update(
            {
                "available": False,
                "unavailable_kind": _dominant_kind(skipped),
                "n": 0,
                "bands": [],
                "gates": None,
                "conditions": conditions_block([], inputs),
            }
        )
        return base
    pooled = pd.concat(gated_parts, ignore_index=True)
    result = delivery_error_from_gated(
        pooled, pooled_report, measured_spec=measured, rpm_bands=bands, pairing=pairing
    )
    payload = result.as_dict()
    if ends is not None:
        at_ends = {}
        for end, parts in end_parts.items():
            report_at = GateReport()
            for _frame, part_report in parts:
                report_at.merge(part_report)
            at_ends[end] = delivery_error_from_gated(
                pd.concat([frame for frame, _ in parts], ignore_index=True),
                report_at,
                measured_spec=measured,
                rpm_bands=bands,
            )
        base["delay_sensitivity"] = delay_sensitivity(
            result, at_ends, float(pairing["transport_delay_s"])
        )
    base.update(
        {
            "available": result.n > 0,
            "unavailable_kind": None if result.n > 0 else "gates_removed_everything",
            "n": result.n,
            "band_averaged_pct": _clean(result.band_averaged_pct),
            "pooled_pct": _clean(result.pooled_pct),
            "rpm_window": [_clean(v) for v in result.rpm_window],
            "bands": [{k: _clean(v) for k, v in band.items()} for band in payload["bands"]],
            "gates": result.gates,
            "conditions": conditions_block(
                contributing,
                inputs,
                gates=result.gates,
                extra={"measured_source": measured, "min_load": min_load},
            ),
        }
    )
    return base


def _dominant_kind(skipped: list[Unavailable]) -> str:
    if not skipped:
        return "no_sessions"
    kinds = {item.kind for item in skipped}
    if kinds == {"not_captured"}:
        return "not_captured"
    return "refused" if "refused" in kinds else next(iter(kinds))


# ---------------------------------------------------------------------------
# W04.7 — cell surfaces (trim, timing) on rpm x airflow


def _cells(
    frame: pd.DataFrame,
    *,
    rpm: str,
    airflow: str,
    values: dict[str, str],
    rpm_edges: tuple[float, ...],
    airflow_edges: tuple[float, ...],
    min_n: int,
) -> list[dict[str, Any]]:
    """Mean of each value column per rpm x airflow cell, with n and sparsity."""
    if frame.empty:
        return []
    rpm_bin = pd.cut(frame[rpm], list(rpm_edges), right=False)
    air_bin = pd.cut(frame[airflow], list(airflow_edges), right=False)
    grouped = frame.assign(_r=rpm_bin.cat.codes, _a=air_bin.cat.codes)
    grouped = grouped[(grouped["_r"] >= 0) & (grouped["_a"] >= 0)]
    out: list[dict[str, Any]] = []
    for key, cell in grouped.groupby(["_r", "_a"], sort=True):
        r, a = (int(cast(int, part)) for part in cast(tuple[Any, ...], key))
        entry: dict[str, Any] = {
            "rpm_index": r,
            "airflow_index": a,
            "rpm_range": [rpm_edges[r], rpm_edges[r + 1]],
            "airflow_range": [airflow_edges[a], airflow_edges[a + 1]],
            "n": int(len(cell)),
            "sparse": int(len(cell)) < min_n,
        }
        for name, column in values.items():
            entry[name] = _clean(cell[column].mean())
        out.append(entry)
    return out


def _check_axes(rpm: str, airflow: str) -> None:
    _assert_axis_allowed(rpm, unsafe=False)
    _assert_axis_allowed(airflow, unsafe=False)


def _edges_ok(edges: tuple[float, ...], name: str) -> None:
    if len(edges) < 2 or any(b <= a for a, b in zip(edges, edges[1:], strict=False)):
        raise GateError(f"{name} edges must be at least two strictly increasing values")


def _aligned_or_missing(
    session: Path, specs: list[str], inputs: WorkbenchInputs
) -> pd.DataFrame | Unavailable:
    """Aligned frame over ``specs`` (require_all), or which were never captured."""
    long, unresolved = _read_optional(session, specs, inputs)
    present = set(long["spec"].unique()) if not long.empty else set()
    missing = [spec for spec in specs if spec not in present]
    if missing:
        return Unavailable(
            session.name,
            "not_captured",
            "This capture's profile does not poll " + ", ".join(missing),
            missing=missing + [s for s in unresolved if s not in missing],
        )
    return align_long(long, specs, require_all=False)


def trim_surface(
    sessions: list[Path],
    *,
    inputs: WorkbenchInputs,
    rpm: str = RPM_SPEC,
    airflow: str = AIRFLOW_SPEC,
    rpm_edges: tuple[float, ...] = DEFAULT_RPM_EDGES,
    airflow_edges: tuple[float, ...] = DEFAULT_AIRFLOW_EDGES,
    min_n: int = DEFAULT_MIN_CELL_N,
    unavailable: list[Unavailable] | None = None,
) -> dict[str, Any]:
    """Closed-loop total correction (STFT + LTFT) per bank by rpm x airflow.

    Gated to closed loop (``FUEL_STATUS == 2``): outside it the trims are held
    and averaging them in dilutes the surface toward zero. Per-session cells
    are returned beside the pooled ones — the -5 % hump of 2026-09-18 existed
    only on the altitude trip, and a pooled surface hides exactly that.
    """
    _check_axes(rpm, airflow)
    _edges_ok(rpm_edges, "rpm")
    _edges_ok(airflow_edges, "airflow")
    skipped = list(unavailable or [])
    banks: dict[str, dict[str, Any]] = {}
    pooled_rows: dict[str, list[pd.DataFrame]] = {bank: [] for bank in TRIM_SPECS}
    per_session: list[dict[str, Any]] = []
    gates: dict[str, Any] = {"started": 0, "filtered": {}, "remaining": 0}
    contributing: list[Path] = []
    for session in sessions:
        specs = [rpm, airflow, FUEL_STATUS_SPEC]
        for stft, ltft in TRIM_SPECS.values():
            specs += [stft, ltft]
        # Bank 2 is optional (a single-bank engine has none); bank 1 is not.
        required = [rpm, airflow, FUEL_STATUS_SPEC, *TRIM_SPECS["b1"]]
        aligned = _aligned_or_missing(session, required, inputs)
        if isinstance(aligned, Unavailable):
            skipped.append(aligned)
            continue
        long, _ = _read_optional(session, specs, inputs)
        present = set(long["spec"].unique())
        use = [spec for spec in specs if spec in present]
        frame = align_long(long, use, require_all=False)
        session_cells: dict[str, list[dict[str, Any]]] = {}
        for bank, (stft, ltft) in TRIM_SPECS.items():
            if stft not in present or ltft not in present:
                continue
            cols = [rpm, airflow, FUEL_STATUS_SPEC, stft, ltft]
            rows = frame.dropna(subset=cols)
            started = len(rows)
            closed = rows[pd.to_numeric(rows[FUEL_STATUS_SPEC], errors="coerce").eq(CLOSED_LOOP)]
            if bank == "b1":
                gates["started"] += started
                gates["filtered"]["not_closed_loop"] = gates["filtered"].get(
                    "not_closed_loop", 0
                ) + (started - len(closed))
                gates["remaining"] += len(closed)
            closed = closed.assign(
                total=closed[stft].astype(float) + closed[ltft].astype(float),
                stft=closed[stft].astype(float),
                ltft=closed[ltft].astype(float),
            )
            pooled_rows[bank].append(closed[[rpm, airflow, "total", "stft", "ltft"]])
            session_cells[bank] = _cells(
                closed,
                rpm=rpm,
                airflow=airflow,
                values={"total": "total", "stft": "stft", "ltft": "ltft"},
                rpm_edges=rpm_edges,
                airflow_edges=airflow_edges,
                min_n=min_n,
            )
        contributing.append(session)
        per_session.append(
            {
                "session_id": session.name,
                "revision": session_revision(session, inputs.registry)["revision"],
                "cells": session_cells,
            }
        )
    for bank, parts in pooled_rows.items():
        if not parts:
            continue
        pooled = pd.concat(parts, ignore_index=True)
        banks[bank] = {
            "n": int(len(pooled)),
            "cells": _cells(
                pooled,
                rpm=rpm,
                airflow=airflow,
                values={"total": "total", "stft": "stft", "ltft": "ltft"},
                rpm_edges=rpm_edges,
                airflow_edges=airflow_edges,
                min_n=min_n,
            ),
        }
    n = int(banks.get("b1", {}).get("n", 0))
    return {
        "available": n > 0,
        "unavailable_kind": (
            None
            if n > 0
            else ("gates_removed_everything" if contributing else _dominant_kind(skipped))
        ),
        "value": "total correction = STFT + LTFT (%)",
        "axes": {
            "rpm": rpm,
            "airflow": airflow,
            "rpm_edges": list(rpm_edges),
            "airflow_edges": list(airflow_edges),
        },
        "min_n": min_n,
        "n": n,
        "banks": banks,
        "per_session": per_session,
        "gates": gates,
        "unavailable": [item.as_dict() for item in skipped],
        "conditions": conditions_block(contributing, inputs, gates=gates),
    }


def _value_cells(
    session: Path,
    *,
    value: str,
    rpm: str,
    airflow: str,
    inputs: WorkbenchInputs,
) -> pd.DataFrame | Unavailable:
    aligned = _aligned_or_missing(session, [rpm, airflow, value], inputs)
    if isinstance(aligned, Unavailable):
        return aligned
    return aligned.dropna(subset=[rpm, airflow, value])


def timing_surface(
    sessions: list[Path],
    *,
    inputs: WorkbenchInputs,
    timing: str = TIMING_SPEC,
    rpm: str = RPM_SPEC,
    airflow: str = AIRFLOW_SPEC,
    rpm_edges: tuple[float, ...] = DEFAULT_RPM_EDGES,
    airflow_edges: tuple[float, ...] = DEFAULT_AIRFLOW_EDGES,
    min_n: int = DEFAULT_MIN_CELL_N,
    unavailable: list[Unavailable] | None = None,
) -> dict[str, Any]:
    """Final ignition timing per rpm x airflow cell, pooled over ``sessions``."""
    _check_axes(rpm, airflow)
    _edges_ok(rpm_edges, "rpm")
    _edges_ok(airflow_edges, "airflow")
    skipped = list(unavailable or [])
    parts: list[pd.DataFrame] = []
    contributing: list[Path] = []
    started = 0
    for session in sessions:
        frame = _value_cells(session, value=timing, rpm=rpm, airflow=airflow, inputs=inputs)
        if isinstance(frame, Unavailable):
            skipped.append(frame)
            continue
        started += len(frame)
        parts.append(frame[[rpm, airflow, timing]])
        contributing.append(session)
    pooled = pd.concat(parts, ignore_index=True) if parts else pd.DataFrame()
    cells = _cells(
        pooled,
        rpm=rpm,
        airflow=airflow,
        values={"timing": timing},
        rpm_edges=rpm_edges,
        airflow_edges=airflow_edges,
        min_n=min_n,
    )
    binned = sum(cell["n"] for cell in cells)
    gates = {
        "started": started,
        "filtered": {"outside_grid": started - binned} if started != binned else {},
        "remaining": binned,
    }
    return {
        "available": binned > 0,
        "unavailable_kind": (
            None
            if binned > 0
            else ("gates_removed_everything" if contributing else _dominant_kind(skipped))
        ),
        "value": timing,
        "axes": {
            "rpm": rpm,
            "airflow": airflow,
            "rpm_edges": list(rpm_edges),
            "airflow_edges": list(airflow_edges),
        },
        "min_n": min_n,
        "n": binned,
        "cells": cells,
        "gates": gates,
        "unavailable": [item.as_dict() for item in skipped],
        "conditions": conditions_block(contributing, inputs, gates=gates),
    }


def pull_comparison(
    sessions_a: list[Path],
    sessions_b: list[Path],
    *,
    revision_a: str,
    revision_b: str,
    inputs: WorkbenchInputs,
    value: str = TIMING_SPEC,
    rpm: str = RPM_SPEC,
    airflow: str = AIRFLOW_SPEC,
    rpm_edges: tuple[float, ...] = DEFAULT_RPM_EDGES,
    airflow_edges: tuple[float, ...] = DEFAULT_AIRFLOW_EDGES,
    min_n: int = DEFAULT_MIN_CELL_N,
    floor_channel: str | None = None,
    unavailable: list[Unavailable] | None = None,
) -> dict[str, Any]:
    """``value`` per rpm x airflow cell on revision A vs revision B.

    Only cells with at least ``min_n`` samples on *both* sides are compared.
    ``delta = B - A``; a negative delta in a cell A had advanced is flagged
    ``dropped`` — the ECM's emergency retard is the only knock proxy this ECM
    offers. ``floor_channel`` asks for significance; the cells are binned
    across every operating regime (:data:`ALL_CONDITIONS`), so a floor
    measured under narrower conditions is refused and the refusal is returned
    as ``significance`` — a WOT floor is never quoted against an
    all-conditions comparison. The floor itself is still described in
    ``noise_floor``.
    """
    _check_axes(rpm, airflow)
    _edges_ok(rpm_edges, "rpm")
    _edges_ok(airflow_edges, "airflow")
    skipped = list(unavailable or [])
    sides: dict[str, dict[str, Any]] = {}
    contributing: list[Path] = []
    for side, sessions in (("a", sessions_a), ("b", sessions_b)):
        parts: list[pd.DataFrame] = []
        for session in sessions:
            frame = _value_cells(session, value=value, rpm=rpm, airflow=airflow, inputs=inputs)
            if isinstance(frame, Unavailable):
                skipped.append(frame)
                continue
            parts.append(frame[[rpm, airflow, value]])
            contributing.append(session)
        pooled = pd.concat(parts, ignore_index=True) if parts else pd.DataFrame()
        sides[side] = {
            "n": int(len(pooled)),
            "sessions": [s.name for s in sessions],
            "cells": _cells(
                pooled,
                rpm=rpm,
                airflow=airflow,
                values={"mean": value},
                rpm_edges=rpm_edges,
                airflow_edges=airflow_edges,
                min_n=min_n,
            ),
        }
    floor = noise_floor_payload(inputs.noise_floor, floor_channel) if floor_channel else None
    index_b = {(c["rpm_index"], c["airflow_index"]): c for c in sides["b"]["cells"]}
    compared: list[dict[str, Any]] = []
    for cell_a in sides["a"]["cells"]:
        key = (cell_a["rpm_index"], cell_a["airflow_index"])
        cell_b = index_b.get(key)
        if cell_b is None or cell_a["sparse"] or cell_b["sparse"]:
            continue
        if cell_a["mean"] is None or cell_b["mean"] is None:
            continue
        delta = cell_b["mean"] - cell_a["mean"]
        compared.append(
            {
                "rpm_index": key[0],
                "airflow_index": key[1],
                "rpm_range": cell_a["rpm_range"],
                "airflow_range": cell_a["airflow_range"],
                "n_a": cell_a["n"],
                "n_b": cell_b["n"],
                "mean_a": cell_a["mean"],
                "mean_b": cell_b["mean"],
                "delta": delta,
                "dropped": delta < 0 and cell_a["mean"] > 0,
                "delta_sigma": None,
            }
        )
    # R05.2: every row of both sides is binned, whatever the operating regime,
    # so this comparison is an all-conditions one. The refusing significance
    # quotes a floor only if it was measured under the same conditions — a WOT
    # floor against these cells would understate noise ~16x.
    verdict = (
        significance(
            inputs.noise_floor,
            floor_channel,
            pd.Series([c["delta"] for c in compared], dtype=float),
            conditions=ALL_CONDITIONS,
        )
        if floor_channel
        else None
    )
    if isinstance(verdict, Significance):
        for cell, sigma in zip(compared, verdict.delta_sigma, strict=True):
            cell["delta_sigma"] = _clean(sigma)
    available = bool(compared)
    if available:
        kind = None
    elif sides["a"]["n"] and sides["b"]["n"]:
        kind = "gates_removed_everything"
    else:
        kind = _dominant_kind(skipped) if skipped else "no_sessions"
    return {
        "available": available,
        "unavailable_kind": kind,
        "value": value,
        "revision_a": revision_a,
        "revision_b": revision_b,
        "axes": {
            "rpm": rpm,
            "airflow": airflow,
            "rpm_edges": list(rpm_edges),
            "airflow_edges": list(airflow_edges),
        },
        "min_n": min_n,
        "sides": sides,
        "cells": compared,
        "n_cells": len(compared),
        "gates": {
            "started": sides["a"]["n"] + sides["b"]["n"],
            "filtered": {},
            "remaining": sum(c["n_a"] + c["n_b"] for c in compared),
            "rule": f"cells with >= {min_n} samples on both revisions",
        },
        "noise_floor": floor,
        "significance": None if verdict is None else verdict.as_dict(),
        "unavailable": [item.as_dict() for item in skipped],
        "conditions": conditions_block(contributing, inputs),
    }


__all__ = [
    "AIRFLOW_SPEC",
    "DEFAULT_AIRFLOW_EDGES",
    "DEFAULT_RPM_EDGES",
    "FRAME_ROW_CAP",
    "REGIMES",
    "RPM_SPEC",
    "TIMING_SPEC",
    "Unavailable",
    "WorkbenchInputs",
    "conditions_block",
    "delivery_route",
    "noise_floor_payload",
    "pooled_delivery_error",
    "pull_comparison",
    "revision_boundaries",
    "session_frame",
    "session_revision",
    "session_span",
    "timing_surface",
    "trim_surface",
]
