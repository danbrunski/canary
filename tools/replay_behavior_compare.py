#!/usr/bin/env python
"""Replay known recordings through the live behavior pipeline, served set vs candidate (B03.7).

The behavior-event path for replay. ``scripts/replay.py`` featurizes a
session offline and scores the IF; it never runs the daemon's behavior
detector, so it cannot say what a behavior-model artifact would *emit*. This
tool does, without a second replay engine: every side is one
``tools/replay_event_diff.drive_session`` pass — the stored session's rows fed
to ``LiveFeatureSource`` on its own clock, ticked through
``LiveInferencePipeline`` — so ``BehaviorDetector`` grades, gates
(``PersistenceGate``), dedupes, emits and fuses exactly as the daemon does.
Per tick it records each model's residual, so a side reports:

* **coverage** — in-mode ticks that produced a deviation;
* **refusals** — why the rest did not (the residual's eligibility reason,
  ``operating_mode_mismatch`` for an off-mode tick), so no-data and off-map
  never read as a zero residual;
* **residuals** — every scored tick's deviation, keyed by session-clock tick.
  The comparison reports MAE, bias and p95 **only on the ticks both sides
  scored** (``n_pairs``); ticks one side scored and the other refused are
  reported apart with the other side's refusal reasons. Two aggregates over
  different tick sets would move with composition, not with the model;
* **persisted events** — what the emitter wrote to the side's isolated
  ``_replay-*`` anomaly log, per model;
* **fusion contribution** — behavior sources handed to fusion, and fused
  events that name a behavior model among their contributors.

Each side runs in its own ``<out>/_replay-<run>-<side>/`` state directory
(the ``_`` prefix the training loader, session listing and retention sweep
already exclude). The artifact is staged at ``models/<profile>/`` under it,
so the profile's tracked ``behavior_policy.yaml`` and validation document
govern it exactly as they govern a deployed artifact on the Pi; a staging
that would escape that governance (a candidate's demotions and quarantines
silently dropped) is refused.

``--shadow-alerting`` grades every model as if it were alerting. A served
set with no validated model emits nothing by contract, so the only way to
compare persistence between two diagnostic-only sets is to let the daemon's
own gate run on them; the result says ``shadow`` and is a model comparison,
never a vehicle-health finding.

Usage::

    .venv/bin/python tools/replay_behavior_compare.py \\
        --session ~/canary-data/sessions/nissan-xterra-2008/2026-09-22T233855 \\
        --candidate data/nissan-xterra-2008/_candidates/2026-09-23/behavior_models.yaml \\
        --out <scratch>/b03-7 [--served <path>] [--shadow-alerting]

``--served`` defaults to the profile-scoped ``daemon.behavior_models``.
Sessions recorded under a non-corpus-eligible capture profile are refused:
the daemon withholds behavior events for them (R03.12b), so a replay that
emitted them would describe a pipeline that does not exist.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import math
import shutil
import sys
import uuid
from collections import Counter
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field, replace
from pathlib import Path
from typing import Any

REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from src.analysis.behavior_inventory import dependent_manifest_block  # noqa: E402
from src.behavior.behavior_models import (  # noqa: E402
    BEHAVIOR_POLICY_FILENAME,
    artifact_identity,
    behavior_policy_path,
    load_behavior_models,
    serving_status,
)
from src.paths import DATA_DIR  # noqa: E402
from src.storage.capture_manifest import read_capture_manifest  # noqa: E402
from tools.replay_event_diff import drive_session  # noqa: E402

COMPARISON_VERSION = 2
#: The refusal recorded for a tick the model's operating mode excludes.
OFF_MODE = "operating_mode_mismatch"
SIDES = ("served", "candidate")


class ReplayCompareRefusedError(ValueError):
    """The comparison cannot be faithful; the message names why."""


# --------------------------------------------------------------------- record


@dataclass
class ModelTally:
    """One model's residuals over a replay, keyed by tick (the session-clock time).

    Keyed, not pooled: two artifacts score different ticks (one refuses
    off-map where the other's wider map does not), so any comparison of their
    residuals is made on the ticks both scored (:func:`paired_residuals`),
    never on two aggregates over different tick sets.
    """

    ticks: int = 0
    in_mode: int = 0
    scored: dict[str, float] = field(default_factory=dict)
    bands: dict[str, str] = field(default_factory=dict)
    refused: dict[str, str] = field(default_factory=dict)
    alerting_as_served: bool | None = None
    fusion_votes: int = 0

    def observe(self, residual: Any, tick: str) -> None:
        self.ticks += 1
        if self.alerting_as_served is None:
            self.alerting_as_served = bool(residual.alerting)
        if not residual.mode_match:
            self.refused[tick] = OFF_MODE
            return
        self.in_mode += 1
        if residual.reason is not None or residual.deviation is None:
            self.refused[tick] = residual.reason or "no_deviation"
            return
        value = float(residual.deviation)
        if not math.isfinite(value):
            self.refused[tick] = "non_finite_deviation"
            return
        self.scored[tick] = value
        self.bands[tick] = str(residual.severity)

    def summary(self) -> dict[str, Any]:
        """This side alone: coverage and refusal counts, plus the per-tick record.

        Residual statistics over this side's own scored ticks are deliberately
        absent: they are not comparable across sides (see the class docstring).
        """
        scored = len(self.scored)
        return {
            "ticks": self.ticks,
            "in_mode_ticks": self.in_mode,
            "scored_ticks": scored,
            "coverage": None if self.in_mode == 0 else scored / self.in_mode,
            "refusals": dict(sorted(Counter(self.refused.values()).items())),
            "band_ticks": dict(sorted(Counter(self.bands.values()).items())),
            "alerting_as_served": self.alerting_as_served,
            "fusion_votes": self.fusion_votes,
            "deviation_by_tick": dict(self.scored),
            "refusal_by_tick": dict(self.refused),
        }


def _quantile(ordered: Sequence[float], q: float) -> float:
    """Linear-interpolated quantile of an already sorted sequence."""
    position = (len(ordered) - 1) * q
    low = math.floor(position)
    high = min(low + 1, len(ordered) - 1)
    return ordered[low] + (ordered[high] - ordered[low]) * (position - low)


def _residual_stats(values: Sequence[float]) -> dict[str, float | None]:
    if not values:
        return {"mae": None, "bias": None, "abs_deviation_p95": None}
    absolute = sorted(abs(v) for v in values)
    return {
        "mae": sum(absolute) / len(values),
        "bias": sum(values) / len(values),
        "abs_deviation_p95": _quantile(absolute, 0.95),
    }


def paired_residuals(served: Mapping[str, Any], candidate: Mapping[str, Any]) -> dict[str, Any]:
    """Residual metrics on the ticks both sides scored, and what only one side scored.

    ``n_pairs`` counts ticks scored by both, never either side's own count.
    A tick only one side scored is reported with the *other* side's refusal
    reason for it (``not_ticked`` when the other side never graded it), so a
    coverage gain reads as what it is — the extra ticks — instead of moving
    the MAE by changing its composition.
    """
    left: Mapping[str, float] = served.get("deviation_by_tick", {})
    right: Mapping[str, float] = candidate.get("deviation_by_tick", {})
    both = sorted(set(left) & set(right))
    out: dict[str, Any] = {"n_pairs": len(both)}
    sides = {"served": [left[t] for t in both], "candidate": [right[t] for t in both]}
    stats = {label: _residual_stats(values) for label, values in sides.items()}
    for key in ("mae", "bias", "abs_deviation_p95"):
        pair = [stats["served"][key], stats["candidate"][key]]
        out[key] = pair
        out[f"{key}_delta"] = _delta(pair[0], pair[1])
    for label, mine, theirs in (
        ("served", left, candidate.get("refusal_by_tick", {})),
        ("candidate", right, served.get("refusal_by_tick", {})),
    ):
        only = sorted(set(mine) - (set(right) if label == "served" else set(left)))
        out[f"only_{label}_scored"] = {
            "ticks": len(only),
            "other_side_refusals": dict(
                sorted(Counter(theirs.get(t, "not_ticked") for t in only).items())
            ),
            **_residual_stats([mine[t] for t in only]),
        }
    return out


@dataclass
class BehaviorReplayRecorder:
    """Wraps one pipeline's ``BehaviorDetector`` to record every residual.

    ``prepare`` is the daemon's grading; ``apply`` its persistence, emit and
    fusion-source step. Both stay the daemon's own code: the recorder only
    reads what passes between them. Under ``shadow_alerting`` every residual
    is handed to ``apply`` as alerting.
    """

    shadow_alerting: bool = False
    ticks: int = 0
    #: The session-clock time of every graded tick, in order: the key the
    #: two sides' residuals are paired on.
    tick_stamps: list[str] = field(default_factory=list)
    models: dict[str, ModelTally] = field(default_factory=dict)

    def install(self, pipeline: Any) -> None:
        detector = pipeline.detectors[2]
        prepare, apply = detector.prepare, detector.apply

        def recording_prepare(row: Any, ctx: Any) -> list[Any]:
            residuals: list[Any] = prepare(row, ctx)
            self.ticks += 1
            tick = ctx.now.isoformat()
            self.tick_stamps.append(tick)
            for residual in residuals:
                self.models.setdefault(residual.name, ModelTally()).observe(residual, tick)
            if self.shadow_alerting:
                residuals = [replace(r, alerting=True) for r in residuals]
            return residuals

        def recording_apply(residuals: list[Any], ctx: Any) -> list[Any]:
            sources: list[Any] = apply(residuals, ctx)
            for source in sources:
                name = str(source.name).removeprefix("behavior:")
                self.models.setdefault(name, ModelTally()).fusion_votes += 1
            return sources

        detector.prepare = recording_prepare
        detector.apply = recording_apply


# ----------------------------------------------------------------------- side


def _profile_slug(config: Any) -> str:
    return str(Path(config.daemon.behavior_models).parent.name)


def stage_artifact(artifact: Path, scratch: Path, slug: str) -> Path:
    """Copy ``artifact`` to ``scratch/models/<slug>/`` and prove the profile governs it.

    A candidate's own directory may carry a stale ``behavior_policy.yaml`` or
    none at all (``_candidates/<run>/`` usually has none), in which case
    loading it in place drops the profile's demotions and quarantines. Staged
    like a deployed artifact, the tracked ``data/<slug>/`` policy governs it.
    """
    staged = scratch / "models" / slug / "behavior_models.yaml"
    staged.parent.mkdir(parents=True, exist_ok=True)
    shutil.copy2(artifact, staged)
    governing = behavior_policy_path(staged)
    tracked = DATA_DIR / slug / BEHAVIOR_POLICY_FILENAME
    if governing != tracked or not tracked.exists():
        raise ReplayCompareRefusedError(
            f"the staged artifact {staged} would be governed by {governing}, not the "
            f"profile's tracked policy {tracked}; put --out outside {DATA_DIR} so a "
            "candidate cannot shed the profile's demotions and quarantines"
        )
    return staged


def refuse_ineligible_session(session: Path) -> None:
    """The daemon withholds behavior events for non-corpus-eligible captures (R03.12b)."""
    manifest = read_capture_manifest(session)
    if manifest is not None and not manifest.corpus_eligible:
        raise ReplayCompareRefusedError(
            f"{session.name} was recorded under capture profile {manifest.profile!r}, which "
            "is not corpus-eligible: the daemon emits no behavior events for it "
            "(R03.12b, detectors default profile only), so replaying it would measure "
            "a pipeline that does not exist"
        )


def _model_block(models: Any) -> dict[str, dict[str, Any]]:
    return {
        model.name: {
            "artifact": artifact_identity(model),
            "serving_status": serving_status(model).to_dict(),
            "operating_mode": model.operating_mode,
            "output": model.output,
        }
        for model in models.models
    }


def _calibration_context(model_tune: object, session_tune: str | None) -> str:
    if session_tune is None:
        return "session_revision_unknown"
    if model_tune is None:
        return "model_revision_unknown"
    return "match" if model_tune == session_tune else "mismatch"


def _events(run: Mapping[str, Any]) -> tuple[Counter[str], Counter[str], int]:
    """Behavior events per model, fused-event contributions per model, fused count."""
    emitted: Counter[str] = Counter()
    fused_with: Counter[str] = Counter()
    fused = 0
    for event in run.get("events", []):
        detail = event.get("detail") or {}
        if event.get("source") == "behavior":
            name = detail.get("model") or str(event.get("model_version", "")).removeprefix(
                "behavior/"
            )
            emitted[str(name)] += 1
        elif event.get("source") == "fused":
            fused += 1
            for contributor in detail.get("contributing_sources") or []:
                if str(contributor).startswith("behavior:"):
                    fused_with[str(contributor).removeprefix("behavior:")] += 1
    return emitted, fused_with, fused


def _detectors_off(config: Any) -> Any:
    models = config.models.model_copy(
        update={
            "isolation_forest": config.models.isolation_forest.model_copy(
                update={"enabled": False}
            ),
            "lstm_autoencoder": config.models.lstm_autoencoder.model_copy(
                update={"enabled": False}
            ),
        }
    )
    return config.model_copy(update={"models": models})


async def replay_side(
    session: Path,
    artifact: Path,
    *,
    label: str,
    out_dir: Path,
    run_id: str,
    shadow_alerting: bool = False,
    with_detectors: bool = False,
    models_dir: Path | None = None,
    start_offset_seconds: float = 0.0,
    seconds: float | None = None,
) -> dict[str, Any]:
    """One ``drive_session`` pass of ``session`` with ``artifact`` as the behavior set."""
    from src.validation.harness import harness_config
    from src.vehicle.profiles import active_registry
    from src.vehicle.tune_revisions import resolve_session_revision

    scratch = out_dir / f"_replay-{run_id}-{label}"
    base = harness_config(scratch, models_dir=models_dir)
    slug = _profile_slug(base)
    staged = stage_artifact(artifact, scratch, slug)
    models = load_behavior_models(staged)
    session_tune, tune_source, tune_conflict = resolve_session_revision(
        session, active_registry(base)
    )

    def configure(config: Any) -> Any:
        daemon = config.daemon.model_copy(update={"behavior_models": staged})
        config = config.model_copy(update={"daemon": daemon})
        return config if with_detectors else _detectors_off(config)

    recorder = BehaviorReplayRecorder(shadow_alerting=shadow_alerting)
    run = await drive_session(
        session,
        scratch=scratch,
        models_dir=models_dir,
        start_offset_seconds=start_offset_seconds,
        seconds=seconds,
        configure=configure,
        observe_pipeline=recorder.install,
    )
    emitted, fused_with, fused = _events(run)
    identities = _model_block(models)
    per_model: dict[str, dict[str, Any]] = {}
    for name in sorted(set(identities) | set(recorder.models)):
        tally = recorder.models.get(name, ModelTally()).summary()
        identity = identities.get(name, {})
        tune = (identity.get("artifact") or {}).get("tune_revision")
        per_model[name] = {
            **identity,
            **tally,
            "calibration_context": _calibration_context(tune, session_tune),
            "persisted_events": emitted.get(name, 0),
            "fused_events_contributed": fused_with.get(name, 0),
        }
    manifests = sorted(Path(base.daemon.models_dir).glob("**/model.manifest.json"))
    return {
        "side": label,
        "artifact": str(artifact),
        "artifact_sha256": _sha256(artifact),
        "governing_policy": str(behavior_policy_path(staged)),
        "governing_policy_sha256": _sha256(behavior_policy_path(staged)),
        "state_dir": str(scratch),
        "shadow_alerting": shadow_alerting,
        "detectors": "if+sequence" if with_detectors else "behavior_only",
        "session_tune_revision": session_tune,
        "session_tune_source": tune_source,
        "session_tune_conflict": tune_conflict,
        "ticks": run["meta"]["ticks"],
        "first_tick": run["meta"]["first_tick"],
        "last_tick": run["meta"]["last_tick"],
        "behavior_ticks": recorder.ticks,
        "tick_stamps": list(recorder.tick_stamps),
        "events_total": len(run["events"]),
        "fused_events": fused,
        "counters": run["counters"],
        "models": per_model,
        # B03.4 — IF/sequence artifacts that consume a deviation column this
        # set produces: match / mismatch / unknown per consumed model.
        "dependent_manifests": [dependent_manifest_block(path, models) for path in manifests],
    }


def _sha256(path: Path) -> str | None:
    import hashlib

    return hashlib.sha256(path.read_bytes()).hexdigest() if path.exists() else None


# -------------------------------------------------------------------- compare


def _delta(before: Any, after: Any) -> float | None:
    if isinstance(before, int | float) and isinstance(after, int | float):
        return float(after) - float(before)
    return None


def compare_sides(served: Mapping[str, Any], candidate: Mapping[str, Any]) -> dict[str, Any]:
    """Per model: what changes if ``candidate`` replaces ``served`` on this recording.

    Refuses two sides that did not grade the same ticks: a count or rate
    compared across different tick grids is the composition error AGENTS.md
    names ("never compare two signals by their aggregates").
    """
    if served.get("tick_stamps") != candidate.get("tick_stamps"):
        raise ReplayCompareRefusedError(
            "the two replays graded different ticks "
            f"({len(served.get('tick_stamps') or [])} vs "
            f"{len(candidate.get('tick_stamps') or [])}); nothing compares across them"
        )
    out: dict[str, Any] = {}
    names = sorted(set(served["models"]) | set(candidate["models"]))
    for name in names:
        left = served["models"].get(name)
        right = candidate["models"].get(name)
        if left is None or right is None:
            out[name] = {"presence": "candidate_only" if left is None else "served_only"}
            continue
        same = left.get("artifact", {}).get("fingerprint") == right.get("artifact", {}).get(
            "fingerprint"
        )
        entry: dict[str, Any] = {
            "presence": "both",
            "same_artifact": same,
            "version": [
                left.get("artifact", {}).get("version"),
                right.get("artifact", {}).get("version"),
            ],
            "lifecycle": [
                left.get("serving_status", {}).get("lifecycle"),
                right.get("serving_status", {}).get("lifecycle"),
            ],
            "alerting": [left.get("alerting_as_served"), right.get("alerting_as_served")],
        }
        # Counts over the tick grid; residuals only ever on paired ticks.
        for key in (
            "scored_ticks",
            "coverage",
            "persisted_events",
            "fusion_votes",
            "fused_events_contributed",
        ):
            entry[key] = [left.get(key), right.get(key)]
            entry[f"{key}_delta"] = _delta(left.get(key), right.get(key))
        refusals = sorted(set(left.get("refusals", {})) | set(right.get("refusals", {})))
        entry["refusals"] = {
            reason: [left["refusals"].get(reason, 0), right["refusals"].get(reason, 0)]
            for reason in refusals
        }
        entry["paired"] = paired_residuals(left, right)
        entry["calibration_context"] = [
            left.get("calibration_context"),
            right.get("calibration_context"),
        ]
        out[name] = entry
    return out


def _incompatible(side: Mapping[str, Any]) -> list[str]:
    found = []
    for manifest in side.get("dependent_manifests", []):
        for name, verdict in (manifest.get("behavior_fingerprint_provenance") or {}).items():
            if verdict.get("status") in ("mismatch", "absent"):
                found.append(f"{manifest.get('path')}:{name}:{verdict.get('status')}")
    return found


async def compare_on_session(
    session: Path,
    *,
    served: Path,
    candidate: Path,
    out_dir: Path,
    run_id: str | None = None,
    **side_kwargs: Any,
) -> dict[str, Any]:
    """Replay ``session`` once per side and compare (B03.7)."""
    refuse_ineligible_session(session)
    run_id = run_id or uuid.uuid4().hex[:8]
    sides: dict[str, dict[str, Any]] = {}
    for label, artifact in (("served", served), ("candidate", candidate)):
        sides[label] = await replay_side(
            session, artifact, label=label, out_dir=out_dir, run_id=run_id, **side_kwargs
        )
    return {
        "comparison_version": COMPARISON_VERSION,
        "session": str(session),
        "run_id": run_id,
        "shadow_alerting": sides["served"]["shadow_alerting"],
        "same_ticks": sides["served"]["tick_stamps"] == sides["candidate"]["tick_stamps"],
        "served": sides["served"],
        "candidate": sides["candidate"],
        "per_model": compare_sides(sides["served"], sides["candidate"]),
        "dependent_manifest_conflicts": {
            label: _incompatible(side) for label, side in sides.items()
        },
    }


#: Per-tick records: what the pairing is computed from, too bulky to archive.
PER_TICK_KEYS = frozenset({"tick_stamps", "deviation_by_tick", "refusal_by_tick"})


def without_per_tick(value: Any) -> Any:
    """``value`` with the per-tick records dropped, for an archived summary.

    The paired metrics computed from them stay; the full report keeps them.
    """
    if isinstance(value, Mapping):
        return {k: without_per_tick(v) for k, v in value.items() if k not in PER_TICK_KEYS}
    if isinstance(value, list):
        return [without_per_tick(v) for v in value]
    return value


# ------------------------------------------------------------------------ cli


def _fmt(value: Any) -> str:
    if value is None:
        return "-"
    if isinstance(value, float):
        return f"{value:.3g}"
    return str(value)


def print_summary(result: Mapping[str, Any]) -> None:
    served, candidate = result["served"], result["candidate"]
    print(
        f"{Path(result['session']).name}: {served['ticks']} ticks per side "
        f"({served['first_tick']} .. {served['last_tick']}), tune {served['session_tune_revision']}"
        f"{' [shadow alerting]' if result['shadow_alerting'] else ''}"
    )
    print(f"  served    {served['artifact_sha256'][:12]}  {served['artifact']}")
    print(f"  candidate {candidate['artifact_sha256'][:12]}  {candidate['artifact']}")
    print(
        f"  events: served {served['events_total']} (fused {served['fused_events']}), "
        f"candidate {candidate['events_total']} (fused {candidate['fused_events']})"
    )
    for name, entry in result["per_model"].items():
        if entry.get("presence") != "both":
            print(f"  {name}: {entry['presence']}")
            continue
        top = sorted(entry["refusals"].items(), key=lambda kv: -max(kv[1]))[:2]
        paired = entry["paired"]
        print(
            f"  {name}{'' if not entry['same_artifact'] else ' (same artifact)'}: "
            f"coverage {_fmt(entry['coverage'][0])}->{_fmt(entry['coverage'][1])}, "
            f"paired n={paired['n_pairs']} mae {_fmt(paired['mae'][0])}->"
            f"{_fmt(paired['mae'][1])}, only served/candidate scored "
            f"{paired['only_served_scored']['ticks']}/{paired['only_candidate_scored']['ticks']}, "
            f"events {entry['persisted_events'][0]}->{entry['persisted_events'][1]}, "
            f"fusion votes {entry['fusion_votes'][0]}->{entry['fusion_votes'][1]}, "
            f"refusals {dict(top)}"
        )
    for label, conflicts in result["dependent_manifest_conflicts"].items():
        print(f"  IF/sequence conflicts ({label}): {conflicts or 'none'}")


def parse_args(argv: Sequence[str] | None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    parser.add_argument(
        "--session", type=Path, action="append", required=True, help="Stored session (repeat)."
    )
    parser.add_argument("--candidate", type=Path, required=True, help="Candidate artifact.")
    parser.add_argument(
        "--served",
        type=Path,
        default=None,
        help="Served artifact (default: the profile-scoped daemon.behavior_models).",
    )
    parser.add_argument("--out", type=Path, required=True, help="Output and _replay-* state dir.")
    parser.add_argument("--shadow-alerting", action="store_true")
    parser.add_argument(
        "--with-detectors",
        action="store_true",
        help="Also run IF/sequence (needs real artifacts under --models-dir).",
    )
    parser.add_argument("--models-dir", type=Path, default=None)
    parser.add_argument("--start-offset", type=float, default=0.0)
    parser.add_argument("--seconds", type=float, default=None)
    return parser.parse_args(argv)


def main(argv: Sequence[str] | None = None) -> int:
    args = parse_args(argv)
    out_dir = args.out.expanduser().resolve()
    served = args.served
    if served is None:
        from src.validation.harness import harness_config

        served = Path(harness_config(out_dir / "_replay-probe").daemon.behavior_models)
    out_dir.mkdir(parents=True, exist_ok=True)
    results = []
    try:
        for session in args.session:
            result = asyncio.run(
                compare_on_session(
                    session.expanduser().resolve(),
                    served=served.resolve(),
                    candidate=args.candidate.resolve(),
                    out_dir=out_dir,
                    shadow_alerting=args.shadow_alerting,
                    with_detectors=args.with_detectors,
                    models_dir=None if args.models_dir is None else args.models_dir.resolve(),
                    start_offset_seconds=args.start_offset,
                    seconds=args.seconds,
                )
            )
            print_summary(result)
            results.append(result)
    except ReplayCompareRefusedError as exc:
        print(f"refused: {exc}", file=sys.stderr)
        return 2
    report = out_dir / ("comparison-shadow.json" if args.shadow_alerting else "comparison.json")
    report.write_text(json.dumps(results, indent=1, sort_keys=True, default=str))
    summary = report.with_suffix(".summary.json")
    summary.write_text(
        json.dumps([without_per_tick(r) for r in results], indent=1, sort_keys=True, default=str)
    )
    print(f"wrote {report} and {summary}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
