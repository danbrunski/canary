"""Phase two of a detector evaluation: which drives are scored, and what is loaded (A02.4).

:func:`plan_drives` decides, before anything is loaded, which physical drives
are evaluated and which are skipped with a named reason:

- a drive with no telemetry rows is skipped;
- a drive some artifact trained or calibrated on is skipped (an artifact that
  only chose its alert level on it makes just that artifact's detectors
  in-sample there, A02.3);
- a drive made only of non-default-profile rows is skipped (R04.6: tuning
  captures bridge a drive but their rows reach no metric);
- a drive that straddles a tune or hardware boundary is skipped.

It also names the sessions the baselines fit and threshold on, and the
sessions to load. The era a drive was recorded in comes from the flash history
and the hardware-boundary labels, never from a session sidecar. Extracted from
``scripts/evaluate_detectors.py`` (R15.4).
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import numpy as np
import pandas as pd

from src.analysis.detector_evaluation_inputs import (
    EvaluationRefusedError,
    EvaluationRequest,
    PreparedInputs,
)
from src.analysis.labelled_events import LabelledEventRegistry, load_labelled_events
from src.models.evaluation import Drive, drives_of_sessions, era_resolver, session_span
from src.models.evaluation_heldout import in_sample_detectors
from src.models.evaluation_loading import EVALUATION_SESSION_KINDS, load_resampled_sessions
from src.session import session_sort_key
from src.storage.corpus_eligibility import default_profile_exclusion
from src.storage.sessions import recorded_sessions, session_started_at
from src.vehicle.profiles import active_registry, active_registry_sources
from src.vehicle.tune_revisions import load_tune_revisions


def spread_in_time(names: Sequence[str], count: int) -> list[str]:
    # session_sort_key is total: the old (session_started_at(n), n) key raised
    # TypeError comparing None with a datetime (R01.1).
    ordered = sorted(names, key=session_sort_key)
    if count >= len(ordered):
        return ordered
    picks = np.linspace(0, len(ordered) - 1, count).round().astype(int)
    return [ordered[i] for i in sorted(set(picks.tolist()))]


@dataclass(frozen=True)
class DrivePlan:
    """The drives to evaluate and skip, and the sessions to load, before any is loaded."""

    #: Every recording by directory name (explicit sessions win a name clash).
    by_name: dict[str, Path]
    #: The labelled events, with a flash per tune revision.
    registry: LabelledEventRegistry
    #: The tune/hardware eras the scored artifacts trained in. A drive recorded
    #: in none of them is reported but kept out of every summary (cross-era).
    training_eras: frozenset[Any]
    #: Drives scored, in time order.
    evaluation: tuple[Drive, ...]
    #: ``{"drive", "sessions", "reason"}`` for each drive not scored.
    skipped: tuple[dict[str, Any], ...]
    #: Per evaluated drive, the detectors in-sample there and why (A02.3).
    in_sample_by_drive: dict[str, dict[str, str]]
    #: Sessions of an evaluated drive kept out of every metric, and why (R04.6).
    excluded: dict[str, str]
    #: Sessions the baselines fit on, and take their thresholds from.
    fit_names: tuple[str, ...]
    calibration_names: tuple[str, ...]
    #: Every session to load: evaluated, fit and calibration.
    needed: tuple[str, ...]


def plan_drives(request: EvaluationRequest, prepared: PreparedInputs) -> DrivePlan:
    """Decide which physical drives are scored, and name every skip."""
    config = request.config
    seen = prepared.seen
    trained_if = prepared.trained_if
    trained_seq = prepared.trained_seq
    roots = list(request.sessions_roots)
    recorded = recorded_sessions(roots, kinds=EVALUATION_SESSION_KINDS)
    session_dirs = list(request.sessions) or recorded
    by_name = {path.name: path for path in recorded}
    by_name.update({path.name: path for path in request.sessions})

    # R05.1/R05.17: the profile's flash history, tracked and operational
    # registries merged; an explicit --tune-revisions file is read as given.
    if request.tune_revisions_path:
        tune_registry = load_tune_revisions(request.tune_revisions_path)
        tune_sources: tuple[Path, ...] = (request.tune_revisions_path,)
    else:
        tune_registry = active_registry(config)
        tune_sources = active_registry_sources(config)
    registry = load_labelled_events(
        request.labels_path, tune_registry=tune_registry, tune_sources=tune_sources
    )
    flash_times = {r.flashed_at for r in (tune_registry.revisions if tune_registry else [])}
    hardware = [
        e.start for e in registry.events if e.corpus_boundary and e.start not in flash_times
    ]
    resolve_era = era_resolver(tune_registry, hardware)
    training_eras = {
        resolve_era(started)
        for name, roles in seen.items()
        if "training" in roles.values() and (started := session_started_at(name)) is not None
    }

    no_span = [path.name for path in session_dirs if session_span(path) is None]
    # Physical drives: vehicle activity, sibling recordings (captures) bridging,
    # and a pause inside one recording splitting it (src.preprocessing.drives).
    drives = drives_of_sessions(
        [path for path in session_dirs if path.name not in set(no_span)], era_of=resolve_era
    )

    skipped: list[dict[str, Any]] = [
        {"drive": name, "sessions": [name], "reason": "skipped: no telemetry rows"}
        for name in no_span
    ]
    evaluation: list[Drive] = []
    in_sample_by_drive: dict[str, dict[str, str]] = {}
    drives = [
        d
        for d in drives
        if (request.since is None or d.start >= request.since)
        and (request.until is None or d.start < request.until)
    ]
    # R04.6 (operator decision 2026-09-24): only default-profile rows are
    # scored. Captures still group drives above; their rows reach nothing below.
    excluded: dict[str, str] = {
        name: reason
        for drive in drives
        for name in drive.session_names
        if (reason := default_profile_exclusion(by_name[name])) is not None
    }
    for drive in drives:
        seen_by = {name: seen[name] for name in drive.session_names if name in seen}
        in_sample = in_sample_detectors(seen_by) if seen_by else {}
        if all(name in excluded for name in drive.session_names):
            reason = (
                "skipped: no default-profile rows ("
                + "; ".join(f"{name}: {excluded[name]}" for name in drive.session_names)
                + ")"
            )
        elif in_sample is None:
            reason = "skipped: seen by " + "; ".join(
                f"{session} ({', '.join(f'{d} {r}' for d, r in roles.items())})"
                for session, roles in sorted(seen_by.items())
            )
        elif drive.straddles:
            reason = f"skipped: {drive.straddles}"
        else:
            evaluation.append(drive)
            if in_sample:
                in_sample_by_drive[drive.drive_id] = in_sample
            continue
        skipped.append(
            {"drive": drive.drive_id, "sessions": list(drive.session_names), "reason": reason}
        )

    # Baselines fit on the sequence model's training sessions (the same corpus
    # the served models learned), and take their thresholds from the served
    # models' calibration sessions — so the evaluation drives stay unseen by
    # every compared detector.
    fit_names: list[str] = []
    calibration_names: list[str] = []
    reference = prepared.reference
    if reference is not None and request.baselines:
        fit_names = spread_in_time(
            [n for n in reference.manifest.training_sessions if n in by_name],
            request.baseline_fit_sessions,
        )
    calibration_names = sorted(
        {
            str(n)
            for m in (
                trained_if.manifest if trained_if else None,
                trained_seq.manifest if trained_seq else None,
            )
            if m is not None
            for n in (getattr(m, "calibration_sessions", ()) or ())
            if str(n) in by_name
        }
    )
    needed = sorted(
        {name for drive in evaluation for name in drive.session_names if name not in excluded}
        | set(fit_names)
        | set(calibration_names)
    )
    return DrivePlan(
        by_name,
        registry,
        frozenset(training_eras),
        tuple(evaluation),
        tuple(skipped),
        in_sample_by_drive,
        excluded,
        tuple(fit_names),
        tuple(calibration_names),
        tuple(needed),
    )


def render_plan(plan: DrivePlan) -> list[str]:
    """The ``--plan`` listing: what would be evaluated, skipped and loaded."""
    lines: list[str] = []
    for drive in plan.evaluation:
        lines.append(
            f"evaluate {drive.drive_id} [{drive.era.label if drive.era else '?'}]: "
            + ", ".join(
                f"{n} (not scored: {plan.excluded[n]})" if n in plan.excluded else n
                for n in drive.session_names
            )
        )
    for entry in plan.skipped:
        lines.append(f"skip     {entry['drive']}: {entry['reason']}")
    lines.append(f"baseline fit: {', '.join(plan.fit_names) or 'none'}")
    lines.append(f"baseline thresholds: {', '.join(plan.calibration_names) or 'none'}")
    for name in plan.needed:
        lines.append(f"load {plan.by_name[name]}")
    return lines


@dataclass(frozen=True)
class LoadedSessions:
    """The resampled frames of the planned sessions, and why any failed to load."""

    frames: dict[str, pd.DataFrame]
    load_errors: dict[str, str]


def load_planned_sessions(request: EvaluationRequest, plan: DrivePlan) -> LoadedSessions:
    """Load every planned session through the derived-frame store.

    "An empty result must say which kind of empty it is": zero drives scored
    because every load failed is not an evaluation, so this refuses and names
    each failure rather than report nothing (P02.5: a Pi retrain once passed a
    derived store the Pi refuses, and its gate scored nothing).
    """
    frames, load_errors = load_resampled_sessions(
        [plan.by_name[n] for n in plan.needed],
        request.config,
        derived_dir=request.derived_dir,
        workers=request.workers,
        cache_only=request.cache_only,
    )
    evaluated = [n for d in plan.evaluation for n in d.session_names if n not in plan.excluded]
    if (plan.needed and not frames) or (evaluated and not any(n in frames for n in evaluated)):
        failed = {n: load_errors[n] for n in plan.needed if n in load_errors}
        if failed:
            raise EvaluationRefusedError(
                [
                    f"error: no evaluation drive loaded; {len(failed)} session load(s) failed:",
                    *(f"  {name}: {error}" for name, error in failed.items()),
                ]
            )
    return LoadedSessions(frames, load_errors)
