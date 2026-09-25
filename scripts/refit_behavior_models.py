#!/usr/bin/env python3
"""Refit the behavior-model surfaces from recorded sessions (Task 4.E.8).

Manual trigger in v1; cron later (the daemon doesn't refit live yet).
Discovers recent Parquet sessions, runs the same preprocessing pipeline the
IF / LSTM trainers use (decode bindings.yaml → wide → resample → segment →
deviation columns from any previously-published behavior models), and runs
every recipe in ``default_recipes()`` (or a user-supplied subset). Skipped
recipes (missing columns, too few samples) are logged but don't fail the
run; surfaces that *did* refit get their ``version`` bumped one above the
previous ``behavior_models.yaml`` entry of the same name.

Usage:

    .venv/bin/python scripts/refit_behavior_models.py
    .venv/bin/python scripts/refit_behavior_models.py --days 14
    .venv/bin/python scripts/refit_behavior_models.py --dry-run
    .venv/bin/python scripts/refit_behavior_models.py \\
        --recipe coolant_warmup --recipe idle_rpm_vs_coolant

Output: ``data/behavior_models.yaml`` (or wherever ``--out`` points).
Atomic write — temp file + rename so a crash mid-write doesn't truncate
the previous version.
"""

from __future__ import annotations

import argparse
import json
import logging
import shutil
import sys
from collections.abc import Sequence
from datetime import UTC, datetime
from pathlib import Path

try:
    from _cli import (
        add_bindings_arg,
        add_common_args,
        add_log_level_arg,
        apply_since_boundary,
        filter_sessions_by_age,
        load_config,
        resolve_sessions,
    )
except ImportError:  # loaded as `scripts.refit_behavior_models` — repo root on path, not scripts/
    from scripts._cli import (  # type: ignore[no-redef]
        add_bindings_arg,
        add_common_args,
        add_log_level_arg,
        apply_since_boundary,
        filter_sessions_by_age,
        load_config,
        resolve_sessions,
    )

from src.analysis.behavior_validation import (
    BehaviorValidationDocument,
    ValidationContext,
    decoder_identity,
    file_identity,
    group_drives,
    load_validation_document,
    mode_labels_predate_fix,
    piece_time_ranges,
    validation_document_path,
)
from src.analysis.refit import Recipe, attach_criteria, default_recipes, refit_all
from src.analysis.session_classifier import read_classification
from src.analysis.tune_revisions import (
    TuneRevisionRegistry,
    load_tune_revisions,
    resolve_session_revision,
)
from src.control.capture_manifest import read_capture_manifest
from src.control.sessions import session_started_at
from src.decode.behavior_models import (
    apply_behavior_policy,
    behavior_policy_path,
    dump_behavior_models,
    load_behavior_models,
    load_behavior_policy,
    serving_status,
    unknown_operating_modes,
)
from src.decode.dbc import DEFAULT_DBC_DIR
from src.log import configure_logging
from src.preprocessing.feature_pipeline import DEFAULT_DIDS_PATH, build_feature_frame
from src.preprocessing.loader import has_telemetry
from src.profiles import active_registry_path, resolve_profile_for_tooling, scope_config
from src.session import format_session_timestamp, session_sort_key

log = logging.getLogger(__name__)


def parse_args(argv: Sequence[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        prog="refit_behavior_models",
        description=(
            "Refit every behavior-model recipe from recently-captured sessions. "
            "Existing models in --out have their `version` bumped on successful refit; "
            "skipped recipes inherit the prior model unchanged."
        ),
    )
    parser.add_argument(
        "sessions",
        nargs="*",
        type=Path,
        help="Session directories. Default: all under --sessions-root within --days.",
    )
    parser.add_argument(
        "--sessions-root",
        type=Path,
        default=None,
        help=(
            "Where to discover sessions when none are given explicitly. "
            "Default: the (profile-scoped) collection.storage_path from --config."
        ),
    )
    parser.add_argument(
        "--profile",
        type=str,
        default=None,
        help=(
            "Vehicle profile slug to scope sessions/output to (see profiles.yaml). "
            "Default: the vehicle: block's profile. No VIN probe is performed."
        ),
    )
    parser.add_argument(
        "--days",
        type=int,
        default=30,
        help=(
            "Only refit using sessions captured in the last N days. Default 30. "
            "Use --all to override (or 0)."
        ),
    )
    parser.add_argument(
        "--all",
        dest="use_all_sessions",
        action="store_true",
        help="Override --days and use every session under --sessions-root.",
    )
    parser.add_argument(
        "--since-boundary",
        action="store_true",
        help=(
            "Exclude sessions captured before the latest corpus_boundary event in "
            "the vehicle ledger (Task 3.5) — e.g. don't blend pre/post-mod behavior "
            "into one corpus. No-op if no boundary event is recorded."
        ),
    )
    add_common_args(parser, help="Path to canary.yaml (preprocessing config).")
    add_bindings_arg(parser)
    parser.add_argument(
        "--out",
        type=Path,
        default=None,
        help="Output YAML. Default: the (profile-scoped) daemon.behavior_models from --config.",
    )
    parser.add_argument(
        "--recipe",
        action="append",
        default=None,
        help=(
            "Recipe name(s) to refit; repeatable. Default: all recipes from "
            "default_recipes(). Useful for iterating on one surface."
        ),
    )
    parser.add_argument(
        "--regrid",
        action="append",
        default=None,
        help=(
            "2D recipe name(s) to explicitly re-grid this run (recompute auto bin "
            "edges + bump grid_version) instead of reusing the prior model's stored "
            "edges (4.3). Repeatable. Default: none — every 2D recipe reuses its "
            "prior edges when a prior model exists."
        ),
    )
    parser.add_argument(
        "--validation",
        type=Path,
        default=None,
        help=(
            "Per-recipe validation criteria (B02). Default: behavior_validation.yaml "
            "next to the profile's behavior_models.yaml. Recipes without declared "
            "criteria may publish but stay diagnostic-only."
        ),
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Print what would refit (recipe → fitted / skipped) but don't write --out.",
    )
    parser.add_argument(
        "--report-json",
        type=Path,
        default=None,
        help="Machine-readable bake-off report (default: <out>.report.json).",
    )
    add_log_level_arg(parser)
    return parser.parse_args(argv)


def _select_recipes(catalog: list[Recipe], names: list[str] | None) -> list[Recipe]:
    if names is None:
        return catalog
    by_name = {r.name: r for r in catalog}
    unknown = [n for n in names if n not in by_name]
    if unknown:
        raise SystemExit(f"unknown recipe(s): {unknown}; available: {sorted(by_name)}")
    return [by_name[n] for n in names]


def _discover_behavior_capture_sessions(
    root: Path,
    recipe_names: set[str],
) -> list[Path]:
    """Discover isolated captures explicitly allowlisted for these recipes.

    General training discovery must continue to exclude every ``_capture-*``
    directory. Behavior surfaces are narrower: a capture sidecar may name the
    exact recipes it is evidence for, and no others. Keeping this resolver in
    the refit CLI makes it impossible for IF/LSTM callers to accidentally opt
    into research captures.
    """
    if not root.exists() or not recipe_names:
        return []
    sessions: list[Path] = []
    for child in root.iterdir():
        if not child.is_dir() or not child.name.startswith("_capture-"):
            continue
        manifest = read_capture_manifest(child)
        if manifest is None or not recipe_names.intersection(manifest.behavior_model_recipes):
            continue
        if has_telemetry(child):
            sessions.append(child)
    return sorted(sessions, key=session_sort_key)


def _consistent_tune_revision(
    sessions: list[Path],
    registry: TuneRevisionRegistry | None,
    document: BehaviorValidationDocument | None = None,
) -> str | None:
    """Return the single calibration identity, refusing ambiguity when known.

    An unknown revision is refused unless ``behavior_validation.yaml`` names a
    resolution for that session (B02.4); excluded sessions must already have
    been removed by :func:`_apply_exclusions`.
    """
    if registry is None:
        return None
    resolved: dict[str, list[str]] = {}
    unknown: list[str] = []
    conflicts: list[str] = []
    for session in sessions:
        resolution = document.resolution_for(session.name) if document is not None else None
        if resolution is not None and resolution.revision is not None:
            resolved.setdefault(resolution.revision, []).append(session.name)
            continue
        revision, _source, conflict = resolve_session_revision(session, registry)
        if conflict:
            conflicts.append(session.name)
        if revision is None:
            unknown.append(session.name)
        else:
            resolved.setdefault(revision, []).append(session.name)
    if conflicts:
        raise ValueError(
            "capture-manifest tune revision conflicts with the vehicle registry for: "
            + ", ".join(sorted(conflicts))
        )
    if unknown:
        raise ValueError("tune revision is unknown for sessions: " + ", ".join(sorted(unknown)))
    if len(resolved) != 1:
        details = "; ".join(
            f"{revision}={','.join(names)}" for revision, names in sorted(resolved.items())
        )
        raise ValueError(
            "behavior-model refit refuses mixed tune revisions; select one revision "
            f"with explicit session paths or a corpus boundary ({details})"
        )
    return next(iter(resolved))


def _apply_exclusions(
    sessions: list[Path], document: BehaviorValidationDocument | None
) -> tuple[list[Path], list[str]]:
    """Drop sessions a named provenance resolution excludes (B02.4)."""
    if document is None:
        return sessions, []
    kept: list[Path] = []
    notes: list[str] = []
    for session in sessions:
        resolution = document.resolution_for(session.name)
        if resolution is not None and resolution.exclude:
            notes.append(f"{session.name}: excluded ({resolution.reason})")
            continue
        kept.append(session)
    return kept, notes


def _flash_crossings(
    frame_ranges: dict[str, tuple[datetime | None, datetime | None]],
    registry: TuneRevisionRegistry | None,
    document: BehaviorValidationDocument | None,
) -> tuple[list[str], list[str]]:
    """Sessions whose recording spans a flash (``(refusals, resolved notes)``).

    A session is assigned the revision in force when it *started*; one that
    kept recording past the next flash mixes two calibrations and needs a
    named resolution, not an assumed calibration (B02.4).
    """
    if registry is None:
        return [], []
    spans: dict[str, tuple[datetime, datetime]] = {}
    for piece, (first, last) in frame_ranges.items():
        parent = piece.split("#segment-", 1)[0]
        start = session_started_at(parent) or first
        end = last or start
        if start is None or end is None:
            continue
        lo, hi = spans.get(parent, (start, end))
        spans[parent] = (min(lo, start), max(hi, end))
    refusals: list[str] = []
    notes: list[str] = []
    for parent, (start, end) in sorted(spans.items(), key=lambda item: item[1][0]):
        at_start, at_end = registry.at(start), registry.at(end)
        if (at_start and at_start.revision) == (at_end and at_end.revision):
            continue
        crossing = (
            f"{parent} starts on {at_start.revision if at_start else 'unknown'} and ends on "
            f"{at_end.revision if at_end else 'unknown'}"
        )
        resolution = document.resolution_for(parent) if document is not None else None
        if resolution is not None and resolution.revision is not None:
            notes.append(f"{crossing}: resolved to {resolution.revision} ({resolution.reason})")
        else:
            refusals.append(crossing)
    return refusals, notes


def _event_sidecars(
    sessions: Sequence[Path],
) -> tuple[dict[str, set[str]], list[str]]:
    """Session events from classifier sidecars, excluding pre-D03.22 labels."""
    session_events: dict[str, set[str]] = {}
    stale: list[str] = []
    for session in sessions:
        classification = read_classification(session)
        if classification is None:
            continue
        if mode_labels_predate_fix(classification.classified_at):
            stale.append(
                f"{session.name}: sidecar classified {classification.classified_at} predates "
                "the mode-label fix f8b7d38; events unusable until D03.22 relabels it"
            )
            continue
        session_events[session.name] = set(classification.event_counts.keys())
    return session_events, stale


def _capture_recipe_allowlists(
    sessions: Sequence[Path],
    *,
    explicit_recipe_grants: set[str] | None = None,
) -> dict[str, set[str]]:
    """Return exact per-capture grants from sidecars or an explicit CLI selection."""
    allowlists: dict[str, set[str]] = {}
    for session in sessions:
        if not session.name.startswith("_capture-"):
            continue
        if explicit_recipe_grants is not None:
            allowlists[session.name] = set(explicit_recipe_grants)
            continue
        manifest = read_capture_manifest(session)
        if manifest is not None:
            allowlists[session.name] = set(manifest.behavior_model_recipes)
    return allowlists


def main(argv: Sequence[str] | None = None) -> int:
    args = parse_args(argv)
    configure_logging(args.log_level)
    config = load_config(args)

    # Scope sessions/output to a vehicle profile so a refit reads and writes the
    # right vehicle's tree. Offline tool: no VIN probe — --profile or default.
    scoped = (
        scope_config(config, resolve_profile_for_tooling(config, args.profile))
        if config.profiles.enabled
        else config
    )
    sessions_root = args.sessions_root or scoped.collection.storage_path
    out: Path = args.out or scoped.daemon.behavior_models

    validation_path: Path = args.validation or validation_document_path(
        scoped.daemon.behavior_models
    )
    try:
        validation_document = load_validation_document(validation_path)
    except ValueError as exc:
        print(f"error: invalid {validation_path}: {exc}", file=sys.stderr)
        return 2
    catalog = attach_criteria(default_recipes(), validation_document)
    recipes = _select_recipes(catalog, args.recipe)
    explicit_sessions = bool(args.sessions)
    sessions = resolve_sessions(args, sessions_root)
    if not explicit_sessions:
        capture_sessions = _discover_behavior_capture_sessions(
            sessions_root, {recipe.name for recipe in recipes}
        )
        if not args.use_all_sessions:
            capture_sessions = filter_sessions_by_age(
                capture_sessions, args.days, datetime.now(UTC)
            )
        sessions = sorted({*sessions, *capture_sessions})
    sessions = apply_since_boundary(args, config, sessions)
    if not sessions:
        print(
            f"error: no sessions found under {sessions_root} within --days {args.days}. "
            "Pass session paths directly or use --all.",
            file=sys.stderr,
        )
        return 1

    sessions, exclusion_notes = _apply_exclusions(sessions, validation_document)
    if not sessions:
        print("error: every session is excluded by a provenance resolution", file=sys.stderr)
        return 1
    revision_registry = load_tune_revisions(active_registry_path(scoped))
    try:
        tune_revision = _consistent_tune_revision(sessions, revision_registry, validation_document)
    except ValueError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2
    capture_recipe_allowlists = _capture_recipe_allowlists(
        sessions,
        explicit_recipe_grants=({recipe.name for recipe in recipes} if explicit_sessions else None),
    )
    log.info(
        "starting refit",
        extra={
            "sessions": len(sessions),
            "recipes": [r.name for r in recipes],
            "out": str(out),
            "dry_run": args.dry_run,
            "tune_revision": tune_revision,
        },
    )

    existing = load_behavior_models(out) if out.exists() else None
    if existing is not None:
        log.info("loaded prior models", extra={"count": len(existing.models)})

    # The feature pipeline gives the wide, resampled, segmented frame the
    # fitters expect — same path the IF / LSTM trainers consume.
    frame = build_feature_frame(
        sessions, config, bindings_path=args.bindings, behavior_models_path=out
    )
    log.info("built feature frame", extra={"rows": len(frame), "columns": len(frame.columns)})

    # B02.4 — a session recording across a flash mixes two calibrations.
    frame_ranges = piece_time_ranges(frame)
    crossings, crossing_notes = _flash_crossings(
        frame_ranges, revision_registry, validation_document
    )
    if crossings:
        print(
            "error: sessions cross a flash boundary and need a named resolution in "
            f"{validation_path}: " + "; ".join(crossings),
            file=sys.stderr,
        )
        return 2

    # Load each session's classifier sidecar so recipes that require
    # specific events (e.g. ``coolant_warmup`` requires ``cold_start``)
    # filter the input frame down to eligible sessions. Sessions
    # without a sidecar yet are excluded from event-gated recipes; they
    # still feed un-gated ones. Run ``scripts/label_sessions.py`` to
    # close the gap before relying on event-gated training. Sidecars
    # classified before the mode-label fix are excluded too (D03.22).
    session_events, stale_labels = _event_sidecars(sessions)
    log.info(
        "loaded session classifications for event-gated recipes",
        extra={
            "classified": len(session_events),
            "stale_mode_labels": len(stale_labels),
            "total_sessions": len(sessions),
        },
    )
    drives = group_drives(
        frame_ranges,
        join_gap_seconds=(
            validation_document.drive_join_gap_seconds if validation_document else 600.0
        ),
    )
    provenance = {
        "sessions": [s.name for s in sessions],
        "independent_drives": [f"{drive.drive_id}: {', '.join(drive.pieces)}" for drive in drives],
        "capture_grants": [
            f"{name}: {', '.join(sorted(grants))}"
            for name, grants in sorted(capture_recipe_allowlists.items())
        ],
        "provenance_resolutions": exclusion_notes + crossing_notes,
        "mode_labels_stale": stale_labels,
        "config": [file_identity(args.config)],
    }
    context = ValidationContext.for_run(
        config,
        validation_document,
        decoder=decoder_identity(
            bindings_path=args.bindings, dids_path=DEFAULT_DIDS_PATH, dbc_dir=DEFAULT_DBC_DIR
        ),
        criteria_source=file_identity(validation_path),
        provenance=provenance,
    )
    if context.taxonomy_problems:
        log.warning(
            "config operating modes differ from the approved taxonomy; new evidence "
            "cannot pass: %s",
            list(context.taxonomy_problems),
        )

    models, results = refit_all(
        recipes,
        frame,
        sessions=[s.name for s in sessions],
        existing=existing,
        session_events=session_events,
        catalog=catalog,
        regrid_recipes=set(args.regrid or ()),
        tune_revision=tune_revision,
        capture_recipe_allowlists=capture_recipe_allowlists,
        validation_context=context,
    )

    # B03.3 — the profile policy governs fresh fits too, so the report cannot
    # show a demoted/quarantined model as trusted just because it was refit.
    apply_behavior_policy(models, load_behavior_policy(behavior_policy_path(out)))

    _print_report(results, frame_rows=len(frame))

    # A model gated on a mode this config's segmenter never emits can't be
    # fit from this frame (and won't evaluate on a daemon running the same
    # mode set) — usually a sign the refit ran with canary.yaml where
    # canary.train.yaml was intended.
    mismatched = unknown_operating_modes(models, scoped.preprocessing.operating_modes)
    if mismatched:
        log.warning(
            "catalog models gated on operating modes missing from %s — "
            "they were carried over un-refit and won't evaluate under this "
            "mode set: %s",
            args.config,
            mismatched,
        )

    if args.dry_run:
        log.info("dry-run; not writing")
        return 0

    report_path = args.report_json or out.with_suffix(out.suffix + ".report.json")
    # B03.10 — keep the artifact set being replaced (and its report) as a
    # recoverable prior before anything is overwritten.
    prior_dir = preserve_prior_artifact(out, report_path)
    if prior_dir is not None:
        log.info("preserved prior behavior models", extra={"path": str(prior_dir)})
    _write_bakeoff_report(report_path, results, frame_rows=len(frame))

    out.parent.mkdir(parents=True, exist_ok=True)
    tmp = out.with_suffix(out.suffix + ".tmp")
    dump_behavior_models(models, tmp)
    tmp.replace(out)
    log.info("wrote behavior models", extra={"path": str(out), "count": len(models.models)})
    return 0


#: Recoverable priors live beside the artifact under an ``_``-prefixed
#: directory (excluded from discovery like every ``_`` path).
PRIOR_DIRNAME = "_prior"


def preserve_prior_artifact(
    out: Path, report_path: Path | None = None, *, now: datetime | None = None
) -> Path | None:
    """Copy the artifact about to be replaced (and its report) to ``_prior/<UTC stamp>/``.

    B03.10: the previous compatible set and its evidence (the evidence lives
    inside the YAML; the report beside it) stay recoverable. Rolling back is
    copying the files back — ``poll_behavior_models`` reloads on any stamp
    change, and ``behavior_policy.yaml`` still governs the restored artifact.
    ``None`` when there is nothing to preserve.
    """
    if not out.exists():
        return None
    stamp = format_session_timestamp(now or datetime.now(UTC))
    target = out.parent / PRIOR_DIRNAME / stamp
    target.mkdir(parents=True, exist_ok=True)
    shutil.copy2(out, target / out.name)
    if report_path is not None and report_path.exists():
        shutil.copy2(report_path, target / report_path.name)
    return target


def _print_report(results: Sequence, frame_rows: int) -> None:
    """Stdout summary so an interactive run shows which recipes won / lost."""
    fitted = [r for r in results if r.fitted]
    skipped = [r for r in results if r.skipped]
    print("=" * 72)
    print(f"BEHAVIOR MODEL REFIT  ({frame_rows} feature rows)")
    print("=" * 72)
    if fitted:
        print(f"\n  fitted ({len(fitted)}):")
        for res in fitted:
            assert res.model is not None
            fit = res.model.fit
            r2 = f"r²={fit.r_squared:.3f}" if fit.r_squared is not None else "r²=N/A"
            validation = (
                f"  heldout={fit.validation_sample_count} "
                f"coverage={fit.validation_coverage:.1%} "
                f"warn={fit.validation_warning_rate:.1%}"
                if fit.validation_coverage is not None and fit.validation_warning_rate is not None
                else ""
            )
            status = serving_status(res.model)
            status_note = (
                "" if status.alerting else f"  [diagnostic-only: {', '.join(status.reasons[:3])}]"
            )
            print(
                f"    ✓ {res.name:35s}  n={fit.sample_count:>6d}  {r2}  "
                f"v{res.model.version}{validation}{status_note}"
            )
    if skipped:
        print(f"\n  skipped ({len(skipped)}):")
        for res in skipped:
            print(f"    ✗ {res.name:35s}  {res.reason}")
    print()


def _write_bakeoff_report(path: Path, results: Sequence, *, frame_rows: int) -> None:
    payload: dict[str, object] = {"frame_rows": frame_rows, "recipes": []}
    recipes: list[dict[str, object]] = []
    for result in results:
        model = result.model or getattr(result, "candidate", None)
        entry: dict[str, object] = {
            "name": result.name,
            "published": bool(result.fitted),
            "reason": result.reason,
        }
        if model is not None:
            fit = model.fit
            entry.update(
                {
                    "kind": model.kind,
                    "training_samples": fit.sample_count,
                    "training_sessions": fit.sessions,
                    "tune_revision": fit.tune_revision,
                    "calibration_sessions": fit.calibration_sessions,
                    "r_squared": fit.r_squared,
                    "validation_samples": fit.validation_sample_count,
                    "validation_coverage": fit.validation_coverage,
                    "validation_warning_rate": fit.validation_warning_rate,
                    "validation_critical_rate": fit.validation_critical_rate,
                    "validation_residual_median": fit.validation_residual_median,
                    "validation_residual_mad": fit.validation_residual_mad,
                    "validation_r_squared": fit.validation_r_squared,
                    "diagnostic_capabilities": model.diagnostic_capabilities,
                    "diagnostic_limitations": model.diagnostic_limitations,
                    "conditions": [condition.model_dump() for condition in model.conditions],
                    "eligibility": (
                        model.eligibility.model_dump(mode="json")
                        if model.eligibility is not None
                        else None
                    ),
                    "serving_status": serving_status(model).to_dict(),
                    "evidence": (
                        model.evidence.model_dump(mode="json")
                        if model.evidence is not None
                        else None
                    ),
                }
            )
            if model.kind == "surface_2d":
                entry["cells"] = [cell.model_dump() for cell in model.fit.cells]
        recipes.append(entry)
    payload["recipes"] = recipes
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n")
    tmp.replace(path)


if __name__ == "__main__":
    sys.exit(main())
