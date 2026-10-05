#!/usr/bin/env python3
"""Reproducible behavior-model evaluation report (B02.1).

The repo-owned replacement for the temporary review evaluator behind
``data/analyses/2026-09-21-behavior-model-review.md``. Every input is named
on the command line — config, vehicle profile, artifact, criteria and
session directories — and the JSON report records each one's content hash,
the git commit, the event policy and the row-selection rules, so a result
can be re-derived rather than trusted.

Two modes, never mixed in one report:

* ``historical`` — reproduce each artifact's *persisted* held-out numbers on
  its own held-out sessions: the known answer to check before believing any
  new number. Uses the historical input scope (``--decode-can none``: the
  July 2026 fits predate the segmenter's decoded fallback) and, for legacy
  artifacts without a persisted serving contract, their catalog recipe's
  contract for row selection. Any config is accepted; its taxonomy is
  recorded.
* ``corrected`` — evaluate each model on supplied drives that are
  independent of everything it trained or was validated on, under the
  operator-approved taxonomy (refused otherwise), and apply the vehicle's
  declared criteria. Supplied training sessions are used only to fit the
  simple baseline the skill criterion compares against.

Per model the report gives the denominators (rows, input-present, fresh,
eligible, evaluated), refusal reasons, coverage, MAE, signed bias, error
quantiles, R² as a descriptor, warning/critical half-widths in output units,
per-row band rates *and* events after the daemon's persistence gate and
dedupe, per drive and per regime, with drive-level uncertainty. Per-row band
exceedances are not false positives: none of these drives are labelled.

Usage:

    .venv/bin/python scripts/evaluate_behavior_models.py \\
        --config canary.pi.example.yaml --profile nissan-xterra-2008 \\
        --artifact data/nissan-xterra-2008/behavior_models.yaml \\
        --mode historical --decode-can none --out /tmp/historical.json \\
        ~/canary-data/sessions/nissan-xterra-2008/2026-07-26T225058

``--manifest`` reads the session list (and expected content hashes) from an
evidence manifest instead; a hash mismatch refuses to run. ``--export-rows``
writes the evaluated columns per session plus a hash manifest — how the
committed test fixture under ``tests/fixtures/behavior_validation`` is made.
"""

from __future__ import annotations

import argparse
import json
import logging
import subprocess
import sys
from collections.abc import Sequence
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Literal

import numpy as np
import pandas as pd
import yaml

try:
    from _cli import (
        add_bindings_arg,
        add_log_level_arg,
        load_config,
    )
except ImportError:  # loaded as `scripts.evaluate_behavior_models`: repo root on path
    from scripts._cli import (  # type: ignore[no-redef]
        add_bindings_arg,
        add_log_level_arg,
        load_config,
    )

from src.analysis.behavior_validation import (
    EVALUATOR_VERSION,
    BehaviorValidationDocument,
    EventPolicy,
    RowEvaluation,
    apply_criteria,
    breakdown,
    breakdown_by_labels,
    build_evaluation_frame,
    evaluate_rows,
    file_identity,
    fit_baseline,
    fold_metrics,
    group_drives,
    load_validation_document,
    parent_session,
    piece_time_ranges,
    profile_exclusions,
    qualifying_drive_count,
    regime_labels,
    session_content_identity,
    synthetic_sensitivity,
    training_leaks,
    transient_controls,
    validation_document_path,
)
from src.analysis.refit import default_recipes, legacy_reproduction_spec, recipe_serving_spec
from src.behavior.behavior_eligibility import ServingSpec, required_channel_groups, serving_spec
from src.behavior.behavior_models import (
    BehaviorModel1D,
    BehaviorModel2D,
    BehaviorModels,
    load_behavior_models,
    serving_status,
)
from src.columns import GAP_SUFFIX, MODE_COLUMN, ROC_SUFFIX, SESSION_COLUMN, TIMESTAMP_COLUMN
from src.features.feature_pipeline import DEFAULT_DIDS_PATH
from src.log import configure_logging
from src.signals.derived import DERIVED_CHANNEL_INPUTS, resolved_dependencies
from src.vehicle.profiles import (
    active_registry,
    active_registry_path,
    resolve_profile_for_tooling,
    scope_config,
)
from src.vehicle.tune_revisions import TuneRevisionRegistry, load_tune_revisions

log = logging.getLogger(__name__)

#: Absolute tolerance for "reproduced" on persisted fractions / R². Counts
#: must match exactly. Float32 derived channels move R² in the 8th digit.
REPRODUCTION_TOLERANCE = 1e-6

SELECTION_RULES = {
    "historical": [
        "rows: the artifact's own held-out sessions (fit.calibration_sessions), every "
        "resampled second of them",
        "contract: the artifact's persisted serving contract, or for a legacy artifact "
        "without one, its catalog recipe's contract (the rows the fit actually used)",
        "denominators: eligible = passes the contract (off-map included); evaluated = "
        "on-map with a finite actual; coverage = evaluated / eligible",
        "reproduced: evaluated count exact; coverage, R², warning and critical rates "
        f"within {REPRODUCTION_TOLERANCE}",
    ],
    "corrected": [
        "rows: supplied drives sharing no directory with the model's training or held-out "
        "sessions (pieces grouped into physical drives; leaks listed and excluded)",
        "contract: the artifact's persisted serving contract, or the current catalog recipe "
        "contract for a legacy artifact (labelled per model)",
        "baseline: the artifact's recorded training baseline, else fitted on supplied "
        "training sessions' eligible rows; never on evaluation drives",
        "events: the daemon's persistence gate then emitter dedupe (event_policy), per drive",
        "synthetic faults: declared step offsets on the actual output; synthetic "
        "sensitivity, not fault recall",
        "per-row band exceedance and events on unlabelled drives are not false positives",
    ],
}


def parse_args(argv: Sequence[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        prog="evaluate_behavior_models",
        description="Reproducible behavior-model evaluation report (B02.1).",
    )
    parser.add_argument("sessions", nargs="*", type=Path, help="Session directories.")
    parser.add_argument(
        "--manifest",
        type=Path,
        default=None,
        help="Evidence manifest (YAML) naming sessions and their content hashes.",
    )
    parser.add_argument(
        "--corpus-root",
        type=Path,
        default=None,
        help="Root that manifest session paths are relative to.",
    )
    # Required, unlike other scripts' canary.yaml default: an implicit local
    # config is exactly what made the review evaluator irreproducible.
    parser.add_argument("--config", type=Path, required=True, help="Preprocessing config.")
    parser.add_argument("--profile", required=True, help="Vehicle profile slug.")
    parser.add_argument("--artifact", type=Path, required=True, help="behavior_models.yaml")
    parser.add_argument(
        "--validation",
        type=Path,
        default=None,
        help="behavior_validation.yaml (default: next to the profile's behavior_models.yaml).",
    )
    parser.add_argument(
        "--tune-revisions",
        type=Path,
        default=None,
        help=(
            "tune_revisions.yaml for the calibration stratum (default: the profile's "
            "active flash registry)."
        ),
    )
    add_bindings_arg(parser)
    parser.add_argument("--dids", type=Path, default=DEFAULT_DIDS_PATH)
    parser.add_argument("--mode", choices=["historical", "corrected"], required=True)
    parser.add_argument(
        "--decode-can",
        choices=["none", "mode-inputs"],
        default=None,
        help="Raw CAN decode scope. Default: none for historical, mode-inputs for corrected.",
    )
    parser.add_argument("--model", action="append", default=None, help="Limit to model name(s).")
    parser.add_argument("--out", type=Path, required=True, help="Report JSON path.")
    parser.add_argument(
        "--export-rows",
        type=Path,
        default=None,
        help="Write the evaluated columns per session (parquet) + a hash manifest here.",
    )
    add_log_level_arg(parser, default="WARNING")
    return parser.parse_args(argv)


# ---------------------------------------------------------------------------
# Inputs
# ---------------------------------------------------------------------------


def _git_identity() -> dict[str, Any]:
    root = Path(__file__).resolve().parents[1]
    try:
        commit = subprocess.run(
            ["git", "rev-parse", "HEAD"], cwd=root, capture_output=True, text=True, check=True
        ).stdout.strip()
        dirty = subprocess.run(
            ["git", "status", "--porcelain", "--", "src", "scripts", "data"],
            cwd=root,
            capture_output=True,
            text=True,
            check=True,
        ).stdout.rstrip()
    except (OSError, subprocess.CalledProcessError):
        return {"commit": None, "dirty_paths": None}
    return {"commit": commit, "dirty_paths": dirty.splitlines()}


def _resolve_sessions(args: argparse.Namespace) -> tuple[list[Path], list[str]]:
    """Explicit sessions, or a manifest whose hashes must still match."""
    if args.manifest is None:
        return [Path(s).expanduser() for s in args.sessions], []
    manifest = yaml.safe_load(args.manifest.read_text())
    root = (args.corpus_root or Path(manifest.get("corpus_root", "."))).expanduser()
    sessions: list[Path] = []
    mismatches: list[str] = []
    for entry in manifest["sessions"]:
        path = root / entry["path"]
        if not path.exists():
            mismatches.append(f"{entry['path']}: missing")
            continue
        expected = entry.get("sha256")
        if expected is not None:
            actual = session_content_identity(path)["sha256"]
            if actual != expected:
                mismatches.append(f"{entry['path']}: sha256 {actual} != manifest {expected}")
        sessions.append(path)
    return sessions, mismatches


# ---------------------------------------------------------------------------
# Evaluation
# ---------------------------------------------------------------------------


def _row_spec(
    model: BehaviorModel1D | BehaviorModel2D, catalog: dict[str, Any]
) -> tuple[ServingSpec | None, str]:
    if model.eligibility is not None:
        return None, "artifact contract"
    recipe = catalog.get(model.name)
    if recipe is None:
        return None, "artifact legacy gates only (no catalog recipe)"
    spec, substituted = legacy_reproduction_spec(recipe, model)
    label = "catalog recipe contract (artifact has none)"
    return spec, label if substituted is None else f"{label}; {substituted}"


def _pieces_of(frame: pd.DataFrame, parents: set[str]) -> np.ndarray:
    pieces = frame[SESSION_COLUMN].astype(str)
    return np.asarray(pieces.map(parent_session).isin(parents), dtype=bool)


def _historical(
    model: BehaviorModel1D | BehaviorModel2D,
    frame: pd.DataFrame,
    *,
    spec: ServingSpec | None,
    policy: EventPolicy,
    drive_of_piece: dict[str, str],
) -> dict[str, Any]:
    fit = model.fit
    held_out = {parent_session(s) for s in fit.calibration_sessions}
    if not held_out or fit.validation_sample_count == 0:
        return {"status": "no_held_out_evidence", "persisted_validation_samples": 0}
    mask = _pieces_of(frame, held_out)
    if not mask.any():
        return {"status": "held_out_sessions_not_supplied", "held_out": sorted(held_out)}
    # B05.3 — the one opt-out: re-derive what the artifact recorded, captures
    # included, before the default-profile rule (the frame is unstamped here).
    ev = evaluate_rows(
        model,
        frame[mask],
        spec=spec,
        interval_seconds=policy.interval_seconds,
        profile_scope="every_recording",
    )
    metrics = fold_metrics(model, ev, drive_of_piece=drive_of_piece, policy=policy)
    persisted = {
        "evaluated": fit.validation_sample_count,
        "coverage": fit.validation_coverage,
        "r_squared": fit.validation_r_squared,
        "warning_row_rate": fit.validation_warning_rate,
        "critical_row_rate": fit.validation_critical_rate,
    }
    recomputed = {key: getattr(metrics, key) for key in persisted}
    mismatches = []
    for key, expected in persisted.items():
        got = recomputed[key]
        if key == "evaluated":
            ok = got == expected
        elif expected is None or got is None:
            ok = expected is None and got is None
        else:
            ok = abs(float(got) - float(expected)) <= REPRODUCTION_TOLERANCE
        if not ok:
            mismatches.append(key)
    return {
        "status": "reproduced" if not mismatches else "not_reproduced",
        "mismatched": mismatches,
        "persisted": persisted,
        "recomputed": recomputed,
        "held_out": sorted(held_out),
        "metrics": metrics.model_dump(mode="json"),
    }


def _baseline(
    model: BehaviorModel1D | BehaviorModel2D,
    frame: pd.DataFrame,
    *,
    spec: ServingSpec | None,
    kind: str,
    policy: EventPolicy,
) -> tuple[dict[str, float], str]:
    if model.evidence is not None and model.evidence.baseline:
        return dict(model.evidence.baseline), "artifact evidence (training-fitted)"
    trained = {parent_session(s) for s in model.fit.sessions}
    mask = _pieces_of(frame, trained)
    if not mask.any():
        return {}, "unavailable: training sessions not supplied"
    ev = evaluate_rows(model, frame[mask], spec=spec, interval_seconds=policy.interval_seconds)
    eligible = ev.eligible
    baseline = fit_baseline(ev.actual[eligible], ev.modes[eligible], kind)  # type: ignore[arg-type]
    supplied = sorted({parent_session(p) for p in ev.pieces})
    return baseline, f"fitted on {len(supplied)} supplied training session(s)"


def _corrected(
    model: BehaviorModel1D | BehaviorModel2D,
    frame: pd.DataFrame,
    *,
    spec: ServingSpec | None,
    document: BehaviorValidationDocument | None,
    policy: EventPolicy,
    drives: Sequence[Any],
    drive_of_piece: dict[str, str],
    taxonomy_problems: list[str],
    regimes: dict[str, np.ndarray] | None = None,
) -> dict[str, Any]:
    fit = model.fit
    leaks = training_leaks(drives, [*fit.sessions, *fit.calibration_sessions])
    independent = [d for d in drives if d.drive_id not in leaks]
    pieces = {p for d in independent for p in d.pieces}
    mask = np.asarray(frame[SESSION_COLUMN].astype(str).isin(pieces), dtype=bool)
    criteria = document.recipes.get(model.name) if document is not None else None
    kind = criteria.baseline if criteria is not None else "training_mode_median"
    baseline, baseline_source = _baseline(model, frame, spec=spec, kind=kind, policy=policy)
    out: dict[str, Any] = {
        "excluded_training_drives": leaks,
        "independent_drives": [d.drive_id for d in independent],
        "baseline_source": baseline_source,
        "criteria_declared": criteria is not None,
    }
    if not mask.any():
        out["status"] = "no_independent_drives_supplied"
        return out
    sub = frame[mask]
    ev = evaluate_rows(model, sub, spec=spec, interval_seconds=policy.interval_seconds)
    metrics = fold_metrics(
        model, ev, drive_of_piece=drive_of_piece, policy=policy, baseline=baseline
    )
    sensitivity = (
        synthetic_sensitivity(model, sub, ev, criteria, policy=policy, spec=spec)
        if criteria is not None and ev.evaluated.any()
        else []
    )
    transients = (
        transient_controls(model, sub, ev, criteria, policy=policy, spec=spec)
        if criteria is not None and ev.evaluated.any()
        else []
    )
    minimum = criteria.min_observations_per_drive if criteria is not None else 1
    verdict, failures = apply_criteria(
        criteria,
        metrics,
        sensitivity,
        qualifying_test_drives=qualifying_drive_count(ev, drive_of_piece, minimum),
        transients=transients,
    )
    if taxonomy_problems:
        failures = [*failures, *(f"mode_taxonomy:{p}" for p in taxonomy_problems)]
        verdict = "failed" if verdict == "failed" else "insufficient_evidence"
    out.update(
        status="evaluated",
        metrics=metrics.model_dump(mode="json"),
        per_drive={
            k: v.model_dump(mode="json")
            for k, v in breakdown(
                model,
                ev,
                by="drive",
                drive_of_piece=drive_of_piece,
                policy=policy,
                baseline=baseline,
            ).items()
        },
        per_regime={
            k: v.model_dump(mode="json")
            for k, v in breakdown(
                model,
                ev,
                by="regime",
                drive_of_piece=drive_of_piece,
                policy=policy,
                baseline=baseline,
            ).items()
        },
        per_context={
            dimension: {
                label: metrics_for.model_dump(mode="json")
                for label, metrics_for in breakdown_by_labels(
                    model,
                    ev,
                    labels[mask],
                    drive_of_piece=drive_of_piece,
                    policy=policy,
                    baseline=baseline,
                ).items()
            }
            for dimension, labels in (regimes or {}).items()
        },
        synthetic_sensitivity=[case.model_dump(mode="json") for case in sensitivity],
        transient_controls=[case.model_dump(mode="json") for case in transients],
        would_be_verdict=verdict,
        failures=failures,
        input_ranges=_input_ranges(model, ev),
    )
    return out


def _revision_of(
    registry: TuneRevisionRegistry, piece: str, starts: dict[str, datetime]
) -> str | None:
    """The revision in force when ``piece``'s session started, dated by the
    session's first row in the frame (its data), the name only as a fallback:
    a name can predate a clock step by hours (2026-08-03T163440, E01.6)."""
    parent = parent_session(piece)
    started = starts.get(parent)
    found = registry.at(started) if started is not None else registry.for_session(parent)
    return None if found is None else found.revision


def _session_starts(
    ranges: dict[str, tuple[datetime | None, datetime | None]],
) -> dict[str, datetime]:
    starts: dict[str, datetime] = {}
    for piece, (first, _last) in ranges.items():
        if first is None:
            continue
        parent = parent_session(piece)
        starts[parent] = min(starts.get(parent, first), first)
    return starts


def _input_resolution(
    model: BehaviorModel1D | BehaviorModel2D, frame: pd.DataFrame
) -> dict[str, list[str | None]]:
    """B04.5 — per session, which inputs a derived output actually resolved to.

    The legacy ``derived:VE_PERCENT`` is manifold- or ambient-referenced
    depending on what a session polled; this names which one each session's
    rows are, instead of letting a report treat them as one quantity.
    """
    output = model.output
    if output not in DERIVED_CHANNEL_INPUTS or SESSION_COLUMN not in frame.columns:
        return {}
    parents = frame[SESSION_COLUMN].astype(str).map(parent_session)
    return {
        str(parent): resolved_dependencies(frame[parents == parent], output)
        for parent in sorted(parents.unique())
    }


def _input_ranges(model: BehaviorModel1D | BehaviorModel2D, ev: RowEvaluation) -> dict[str, Any]:
    """Observed input range on fresh rows vs the model's axis domain (off-map context)."""
    present = ev.fresh
    if isinstance(model, BehaviorModel1D):
        axes = [(model.axis.channel, ev.x, list(model.axis.domain))]
    else:
        axes = [
            (
                model.axes[0].channel,
                ev.x,
                [model.axes[0].bin_edges[0], model.axes[0].bin_edges[-1]],
            ),
            (
                model.axes[1].channel,
                ev.y,
                [model.axes[1].bin_edges[0], model.axes[1].bin_edges[-1]],
            ),
        ]
    out: dict[str, Any] = {}
    for channel, values, domain in axes:
        finite = values[present & np.isfinite(values)]
        out[channel] = {
            "fresh_rows": int(finite.size),
            "observed": [float(finite.min()), float(finite.max())] if finite.size else None,
            "model_domain": [float(domain[0]), float(domain[1])],
        }
    return out


def evaluate(
    models: BehaviorModels,
    frame: pd.DataFrame,
    *,
    mode: str,
    document: BehaviorValidationDocument | None,
    policy: EventPolicy,
    taxonomy_problems: list[str],
    profile: str | None,
    revisions: TuneRevisionRegistry | None = None,
) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    catalog = {recipe.name: recipe for recipe in default_recipes(profile)}
    join_gap = document.drive_join_gap_seconds if document is not None else 600.0
    ranges = piece_time_ranges(frame)
    starts = _session_starts(ranges)
    drives = group_drives(ranges, join_gap_seconds=join_gap)
    drive_of_piece = {piece: d.drive_id for d in drives for piece in d.pieces}
    regimes = regime_labels(
        frame,
        document.report_regimes if document is not None else None,
        revision_of_piece=(
            None if revisions is None else lambda piece: _revision_of(revisions, piece, starts)
        ),
    )
    results: dict[str, Any] = {}
    for model in models.models:
        spec, contract = _row_spec(model, catalog)
        entry: dict[str, Any] = {
            "kind": model.kind,
            "output": model.output,
            "output_unit": model.output_unit,
            "operating_mode": model.operating_mode,
            "version": model.version,
            "tune_revision": model.fit.tune_revision,
            "row_contract": contract,
            "serving_status": serving_status(model).to_dict(),
            "training_sessions": list(model.fit.sessions),
            "held_out_sessions": list(model.fit.calibration_sessions),
        }
        resolution = _input_resolution(model, frame)
        if resolution:
            entry["derived_input_resolution"] = resolution
        if mode == "historical":
            entry["historical"] = _historical(
                model, frame, spec=spec, policy=policy, drive_of_piece=drive_of_piece
            )
        else:
            entry["corrected"] = _corrected(
                model,
                frame,
                spec=spec,
                document=document,
                policy=policy,
                drives=drives,
                drive_of_piece=drive_of_piece,
                taxonomy_problems=taxonomy_problems,
                regimes=regimes,
            )
        results[model.name] = entry
    return results, [d.record().model_dump(mode="json") for d in drives]


# ---------------------------------------------------------------------------
# Row export (committed fixtures)
# ---------------------------------------------------------------------------


def export_columns(
    models: BehaviorModels, frame: pd.DataFrame, *, profile: str | None
) -> list[str]:
    """Every column the evaluator reads for these models (+ catalog contracts)."""
    catalog = {recipe.name: recipe for recipe in default_recipes(profile)}
    bases: set[str] = set()
    specs: list[Any] = []
    for model in models.models:
        specs.append(serving_spec(model))
        if model.name in catalog:
            specs.append(recipe_serving_spec(catalog[model.name]))
    for spec in specs:
        for group in required_channel_groups(spec):
            bases.update(group)
    wanted = [SESSION_COLUMN, TIMESTAMP_COLUMN, MODE_COLUMN]
    for column in frame.columns:
        base = str(column).removesuffix(GAP_SUFFIX).removesuffix(ROC_SUFFIX)
        if base in bases or str(column).startswith("context:"):
            wanted.append(str(column))
    return [c for c in dict.fromkeys(wanted) if c in frame.columns]


def export_rows(
    directory: Path,
    models: BehaviorModels,
    frame: pd.DataFrame,
    *,
    inputs: dict[str, Any],
    profile: str | None,
) -> Path:
    directory.mkdir(parents=True, exist_ok=True)
    columns = export_columns(models, frame, profile=profile)
    entries = []
    for parent in sorted({parent_session(p) for p in frame[SESSION_COLUMN].astype(str)}):
        rows = frame[_pieces_of(frame, {parent})][columns]
        path = directory / f"{parent}.parquet"
        rows.to_parquet(path, index=False)
        entries.append(
            {"file": path.name, "rows": len(rows), "sha256": file_identity(path).split("@")[1]}
        )
    manifest = {
        "generated_by": "scripts/evaluate_behavior_models.py --export-rows",
        **inputs,
        "files": entries,
    }
    out = directory / "export_manifest.yaml"
    out.write_text(yaml.safe_dump(manifest, sort_keys=False))
    return out


# ---------------------------------------------------------------------------
# main
# ---------------------------------------------------------------------------


def main(argv: Sequence[str] | None = None) -> int:
    args = parse_args(argv)
    configure_logging(args.log_level)
    config = load_config(args)
    scoped = scope_config(config, resolve_profile_for_tooling(config, args.profile))
    validation_path: Path = args.validation or validation_document_path(
        scoped.daemon.behavior_models
    )
    document = load_validation_document(validation_path)
    models = load_behavior_models(args.artifact)
    if args.model:
        unknown = sorted(set(args.model) - {m.name for m in models.models})
        if unknown:
            print(f"error: unknown model(s) {unknown}", file=sys.stderr)
            return 2
        models = models.model_copy(
            update={"models": [m for m in models.models if m.name in set(args.model)]}
        )
    sessions, mismatches = _resolve_sessions(args)
    if mismatches:
        print("error: evidence manifest mismatch: " + "; ".join(mismatches), file=sys.stderr)
        return 2
    if not sessions:
        print("error: name sessions explicitly or pass --manifest", file=sys.stderr)
        return 1
    configured = list(scoped.preprocessing.operating_modes)
    taxonomy_problems = document.taxonomy_problems(configured) if document is not None else []
    if args.mode == "corrected" and (document is None or taxonomy_problems):
        print(
            "error: corrected evaluation needs the approved operating-mode taxonomy; "
            f"{validation_path}: {taxonomy_problems or 'missing'}",
            file=sys.stderr,
        )
        return 2
    decode_can: Literal["none", "mode-inputs"] = args.decode_can or (
        "none" if args.mode == "historical" else "mode-inputs"
    )
    policy = EventPolicy.from_config(scoped, document)
    revisions_path: Path = args.tune_revisions or active_registry_path(scoped)
    # R05.17: the default history is the tracked and operational registries
    # merged; an explicit --tune-revisions file is read as given.
    revisions = (
        load_tune_revisions(args.tune_revisions) if args.tune_revisions else active_registry(scoped)
    )

    frame = build_evaluation_frame(
        sessions,
        scoped,
        models,
        bindings_path=args.bindings,
        dids_path=args.dids,
        decode_can=decode_can,
        # B05.3 — corrected evidence is default-profile rows only (R03.12);
        # a historical reproduction re-derives what an artifact recorded.
        profile_scope="default_profile" if args.mode == "corrected" else "every_recording",
    )
    inputs: dict[str, Any] = {
        "config": file_identity(args.config),
        "profile": args.profile,
        "artifact": file_identity(args.artifact),
        "artifact_schema_version": models.schema_version,
        "validation": file_identity(validation_path),
        "tune_revisions": file_identity(revisions_path) if revisions_path.exists() else None,
        "bindings": file_identity(args.bindings),
        "dids": file_identity(args.dids),
        "git": _git_identity(),
        "decode_can": decode_can,
        "event_policy": policy.describe(),
        "profile_exclusions": (
            profile_exclusions(sessions) if args.mode == "corrected" else "not applied (historical)"
        ),
        "sessions": [{**session_content_identity(s), "path": str(s)} for s in sessions],
    }
    results, drives = evaluate(
        models,
        frame,
        mode=args.mode,
        document=document,
        policy=policy,
        taxonomy_problems=taxonomy_problems,
        profile=args.profile,
        revisions=revisions,
    )
    report = {
        "report": "behavior-validation",
        "evaluator_version": EVALUATOR_VERSION,
        "generated_at": datetime.now(UTC).isoformat(),
        "mode": args.mode,
        "inputs": inputs,
        "selection_rules": SELECTION_RULES[args.mode],
        "taxonomy": {
            "configured": configured,
            "approved": document.approved_operating_modes if document is not None else None,
            "problems": taxonomy_problems,
        },
        "frame_rows": len(frame),
        "drives": drives,
        "models": results,
    }
    args.out.parent.mkdir(parents=True, exist_ok=True)
    args.out.write_text(json.dumps(report, indent=2, sort_keys=False, default=str) + "\n")
    if args.export_rows is not None:
        export_rows(args.export_rows, models, frame, inputs=inputs, profile=args.profile)
    _print_summary(report)
    return 0


def _print_summary(report: dict[str, Any]) -> None:
    print(
        f"behavior validation ({report['mode']}): {report['frame_rows']} rows, "
        f"{len(report['drives'])} drive(s)"
    )
    for name, entry in report["models"].items():
        section = entry.get("historical") or entry.get("corrected") or {}
        status = section.get("status")
        metrics = section.get("metrics") or {}
        detail = ""
        if metrics:
            detail = (
                f" evaluated={metrics.get('evaluated')}/{metrics.get('eligible')} "
                f"mae={_fmt(metrics.get('mae'))} r2={_fmt(metrics.get('r_squared'))} "
                f"warn_hw={_fmt(metrics.get('warning_half_width_median'))} "
                f"events={metrics.get('events')}"
            )
        verdict = section.get("would_be_verdict")
        print(f"  {name:34s} {status}{detail}" + (f" -> {verdict}" if verdict else ""))


def _fmt(value: Any) -> str:
    return "n/a" if value is None else f"{float(value):.4g}"


if __name__ == "__main__":
    sys.exit(main())
