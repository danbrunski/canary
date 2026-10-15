"""Phases three to five of a detector evaluation: assemble, score, aggregate (A02.4).

After :func:`~src.analysis.detector_evaluation_plan.plan_drives` has chosen the
drives and the sessions are loaded:

- :func:`scoring_keys` -> :class:`ScoringKeys`: the code identity and each
  frame's content key, from which every cached score is keyed;
- :func:`assemble_detectors` -> :class:`DetectorSet`: the deployed stack, the
  baselines, the arrived-sample detectors and their streams, and the sequence
  parity check, each with its cache identity;
- :func:`plan_scoring_tasks` -> the :class:`~src.models.evaluation_scorer.DriveTask`
  per evaluated drive, and :func:`score_planned_drives`, which hands them to
  the scoring pool (:mod:`src.models.evaluation_workers`);
- :func:`aggregate_results` -> :class:`Aggregate`: results merged in drive
  order, every drive's record built against the labels, in-sample detectors
  dropped from everything a summary reads, cross-era drives kept out of every
  summary;
- :func:`settle_score_cache`: commit the run's staged entries only if no key
  input moved while it ran.

Independent drives are the unit throughout, and the default-profile exclusion
and seen-session rules are applied in the plan, not here. Nothing here prints
or chooses an output path. Extracted from ``scripts/evaluate_detectors.py``
(R15.4).
"""

from __future__ import annotations

import logging
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from typing import Any

import pandas as pd

from src.analysis.detector_evaluation_drives import clean_drive_record
from src.analysis.detector_evaluation_inputs import (
    BehaviorInputs,
    EvaluationRequest,
    PreparedInputs,
)
from src.analysis.detector_evaluation_plan import DrivePlan, LoadedSessions
from src.decode.dids import DidMap, load_did_map
from src.features.feature_paths import DEFAULT_DIDS_PATH
from src.features.feature_transform import featurize_wide_frame
from src.features.session_resampling import default_decode_inputs
from src.models.evaluation_baselines import BaselineSet, prepare_baselines
from src.models.evaluation_cache import ScoreCache, digest, frame_key
from src.models.evaluation_detectors import (
    DeployedStack,
    Detector,
    PlausibilityDetector,
    RawStreamDetector,
    ScoreModel,
    sequence_parity,
    suppressed_for,
)
from src.models.evaluation_identity import (
    KeySources,
    changed_key_inputs,
    scoring_feature_key,
    sequence_identity,
)
from src.models.evaluation_records import DriveRecord, SessionScore, drop_in_sample
from src.models.evaluation_scorer import DriveResult, DriveScorer, DriveTask, SweepSpec
from src.models.evaluation_streams import CachedStreams, load_raw_streams
from src.models.evaluation_workers import ScorerSpec, score_drives

log = logging.getLogger(__name__)


@dataclass(frozen=True)
class ScoringKeys:
    """What every cached score of this run is keyed on besides its detector."""

    code: str
    #: Each loaded session's frame by content (``evaluation_cache.frame_key``).
    frame_keys: dict[str, str]
    #: Code, thread environment, config, behavior models, bindings and data.
    feature_key: str


def scoring_keys(
    request: EvaluationRequest, prepared: PreparedInputs, loaded: LoadedSessions
) -> ScoringKeys:
    """P02.5: every score is cached under its detector's identity and its unit's
    key; the unit key starts from the session's frame by content."""
    code = str(prepared.inputs["code"])
    frame_keys = {name: frame_key(frame) for name, frame in loaded.frames.items()}
    return ScoringKeys(code, frame_keys, scoring_feature_key(code, request.config, prepared.inputs))


@dataclass(frozen=True)
class DetectorSet:
    """Every compared detector, with its cache identity."""

    stack: DeployedStack
    #: The deployed stack, then each baseline that has a threshold.
    detectors: tuple[Detector, ...]
    #: Arrived-sample detectors (A05), scored beside the featurized ones.
    raw_detectors: tuple[RawStreamDetector, ...]
    identities: dict[str, str]
    baselines: BaselineSet
    #: Detectors that cannot run, by name, with the reason (reported, not scored).
    withheld: dict[str, str]
    #: Every reported detector name, featurized ones first.
    names: list[str]
    streams: CachedStreams
    #: The DID catalog the loaded frames were decoded with (P02.14).
    did_map: DidMap | None
    #: Sequence batched-vs-served parity on the first evaluated session.
    parity: dict[str, Any] | None


def _session_featurizer(
    frames: Mapping[str, pd.DataFrame],
    request: EvaluationRequest,
    prepared: PreparedInputs,
    behavior: BehaviorInputs,
    did_map: DidMap | None,
) -> Callable[[str], pd.DataFrame]:
    def featurize(name: str) -> pd.DataFrame:
        return featurize_wide_frame(
            frames[name],
            request.config,
            bindings=prepared.bindings,
            behavior_models=behavior.behavior,
            did_map=did_map,
        ).features

    return featurize


def assemble_detectors(
    request: EvaluationRequest,
    prepared: PreparedInputs,
    plan: DrivePlan,
    loaded: LoadedSessions,
    keys: ScoringKeys,
    behavior: BehaviorInputs,
    cache: ScoreCache,
) -> DetectorSet:
    """Build the compared detectors in the order a report lists them."""
    config = request.config
    frames = loaded.frames
    trained_if, trained_seq = prepared.trained_if, prepared.trained_seq
    # The DID catalog load_resampled_session decoded every frame with (P02.14).
    decoded_with = default_decode_inputs().did_map
    featurize = _session_featurizer(frames, request, prepared, behavior, decoded_with)

    stack = DeployedStack(
        config,
        trained_if,
        trained_seq,
        suppress_channels=suppressed_for(trained_seq, config) if trained_seq else (),
    )
    identities: dict[str, str] = {}
    if trained_if is not None:
        identities["isolation_forest"] = str(prepared.inputs["isolation_forest"])
    if trained_seq is not None:
        # The serve-time suppress list comes from the config, which is in
        # every unit key (``feature_key``); the batch size is code. The
        # forward cadence is named in the identity itself (A06.6).
        identities["sequence"] = sequence_identity(prepared.inputs["sequence"], config)
    baselines = prepare_baselines(
        requested=request.baselines,
        reference=prepared.reference,
        trained_seq=trained_seq,
        config=config,
        persistence=prepared.persistence,
        fit_names=plan.fit_names,
        calibration_names=plan.calibration_names,
        present=frames,
        frame_keys=keys.frame_keys,
        feature_key=keys.feature_key,
        featurize=featurize,
        curated_channels_path=request.curated_channels_path,
        sequence_identity=identities.get("sequence"),
        seed=request.seed,
        budget=request.budget,
        cache=cache,
    )
    identities.update(baselines.identities)
    detectors: list[Detector] = [stack, *baselines.detectors]
    withheld = dict(stack.withheld)
    raw_detectors: list[RawStreamDetector] = []
    if "plausibility" in request.baselines:
        try:
            raw_detectors.append(
                PlausibilityDetector(
                    config,
                    bindings=prepared.bindings,
                    did_map=(
                        load_did_map(DEFAULT_DIDS_PATH) if DEFAULT_DIDS_PATH.exists() else None
                    ),
                )
            )
        except FileNotFoundError as exc:
            withheld["plausibility"] = f"withheld: {exc}"
    for raw_detector in raw_detectors:
        catalog = getattr(raw_detector, "catalog", None)
        identities[raw_detector.names[0]] = catalog.fingerprint() if catalog is not None else ""
    names = [n for d in detectors for n in d.names] + [n for r in raw_detectors for n in r.names]
    streams = load_raw_streams(
        raw_detectors,
        [plan.by_name[n] for d in plan.evaluation for n in d.session_names if n in frames],
        config,
        cache_dir=request.cache_dir,
    )

    parity: dict[str, Any] | None = None
    if stack.sequence is not None and plan.evaluation:
        first = next((n for d in plan.evaluation for n in d.session_names if n in frames), None)
        if first is not None:
            parity_unit = digest(
                keys.feature_key,
                "parity",
                keys.frame_keys[first],
                identities["sequence"],
                request.parity_rows,
            )
            parity = cache.get("_parity", parity_unit)
            if parity is None:
                parity = sequence_parity(
                    stack.sequence, featurize(first), rows=request.parity_rows
                ).to_dict()
                cache.put("_parity", parity_unit, parity)
            parity = {**parity, "session": first}
    return DetectorSet(
        stack,
        tuple(detectors),
        tuple(raw_detectors),
        identities,
        baselines,
        withheld,
        names,
        streams,
        decoded_with,
        parity,
    )


def sweep_spec(request: EvaluationRequest) -> SweepSpec:
    """The injection sweep's and the shift test's parameters."""
    return SweepSpec(
        kinds=tuple(request.faults) if request.injections_per_drive > 0 else (),
        reps=request.injections_per_drive,
        seed=request.seed,
        lead_seconds=request.lead_seconds,
        window_seconds=request.window_seconds,
        grace_seconds=request.grace_seconds,
        shift_rows=request.shift_rows,
    )


def plan_scoring_tasks(
    request: EvaluationRequest, plan: DrivePlan, loaded: LoadedSessions, keys: ScoringKeys
) -> list[DriveTask]:
    """One task per evaluated drive that has a loaded, scored session.

    A cross-era drive is scored clean (reported per drive) but gets neither the
    shift test nor the injection sweep.
    """
    tasks: list[DriveTask] = []
    for drive in plan.evaluation:
        present = {
            n: loaded.frames[n]
            for n in drive.session_names
            if n in loaded.frames and n not in plan.excluded
        }
        if not present:
            continue
        cross_era = bool(plan.training_eras) and drive.era not in plan.training_eras
        tasks.append(
            DriveTask(
                drive,
                present,
                {n: keys.frame_keys[n] for n in present},
                shift=request.shift_test and not cross_era,
                sweep=not cross_era and bool(request.faults) and request.injections_per_drive > 0,
            )
        )
    return tasks


@dataclass(frozen=True)
class ScoredDrives:
    """The scoring pool's results, in task order."""

    results: list[DriveResult]
    #: The parent's own cache lookups (baselines, parity); each drive adds its own.
    parent_cache_counts: dict[str, dict[str, int]]


def score_planned_drives(
    request: EvaluationRequest,
    prepared: PreparedInputs,
    behavior: BehaviorInputs,
    detectors: DetectorSet,
    keys: ScoringKeys,
    cache: ScoreCache,
    roles: Mapping[str, Sequence[str]],
    sweep: SweepSpec,
    tasks: Sequence[DriveTask],
) -> ScoredDrives:
    """Score every task, in spawned workers when ``request.workers`` allows."""
    config = request.config

    def local_scorer() -> DriveScorer:
        return DriveScorer(
            detectors=detectors.detectors,
            raw_detectors=detectors.raw_detectors,
            identities=detectors.identities,
            stream_paths=detectors.streams.paths,
            cache=cache,
            code=keys.code,
            feature_key=keys.feature_key,
            config=config,
            bindings=prepared.bindings,
            did_map=detectors.did_map,
            behavior=behavior.behavior,
            persistence=prepared.persistence,
            roles=roles,
            sweep=sweep,
        )

    def worker_spec() -> ScorerSpec:
        baseline_specs: list[tuple[ScoreModel | None, float]] = list(detectors.baselines.specs)
        return ScorerSpec(
            config=config,
            if_path=request.if_path if prepared.trained_if is not None else None,
            sequence_path=request.sequence_path if prepared.trained_seq is not None else None,
            baselines=baseline_specs,
            untrained_seed=request.seed,
            plausibility=bool(detectors.raw_detectors),
            bindings_path=request.bindings_path,
            behavior_path=request.behavior_path if behavior.behavior is not None else None,
            identities=detectors.identities,
            stream_paths=detectors.streams.paths,
            cache_root=cache.root,
            cache_run=cache.run,
            key_sources=prepared.key_sources,
            key_inputs=prepared.inputs,
            code=keys.code,
            feature_key=keys.feature_key,
            persistence=prepared.persistence,
            roles={role: list(channels) for role, channels in roles.items()},
            sweep=sweep,
            threads=request.scoring_threads,
        )

    # The parent's own lookups (baselines, parity); each drive adds its own.
    parent_counts = cache.counts()
    results = score_drives(
        tasks,
        workers=request.workers,
        threads=request.scoring_threads,
        local=local_scorer,
        spec=worker_spec,
    )
    return ScoredDrives(list(results), parent_counts)


@dataclass(frozen=True)
class Aggregate:
    """Every drive's results merged in drive order, ready for the summaries."""

    scores: dict[str, dict[str, SessionScore]]
    records: list[DriveRecord]
    outcomes: list[dict[str, Any]]
    skipped_injections: list[dict[str, Any]]
    slices: list[dict[str, Any]]
    shift_runs: list[dict[str, Any]]
    #: Drives not scored: the plan's skips, then drives none of whose sessions loaded.
    skipped: list[dict[str, Any]]
    cache_counts: dict[str, dict[str, int]]

    @property
    def cache_hits(self) -> int:
        return sum(c["hits"] for c in self.cache_counts.values())

    @property
    def cache_lookups(self) -> int:
        return self.cache_hits + sum(c["misses"] for c in self.cache_counts.values())


def aggregate_results(
    plan: DrivePlan,
    loaded: LoadedSessions,
    scored: ScoredDrives,
    names: Sequence[str],
) -> Aggregate:
    """Merge the scoring results in drive order.

    A drive with no loaded session is skipped here, naming each session's load
    failure; a drive in-sample for some detector loses that detector's results
    everywhere a summary reads them; a cross-era drive keeps its record but
    contributes no injection outcome or slice.
    """
    frames = loaded.frames
    excluded = plan.excluded
    results = iter(scored.results)
    cache_counts = scored.parent_cache_counts
    skipped = list(plan.skipped)
    scores: dict[str, dict[str, SessionScore]] = {}
    records: list[DriveRecord] = []
    outcomes: list[dict[str, Any]] = []
    skipped_injections: list[dict[str, Any]] = []
    slices: list[dict[str, Any]] = []
    shift_runs: list[dict[str, Any]] = []
    stream_reads: dict[str, int] = {}
    worker_peak = 0
    for drive in plan.evaluation:
        present = {n: frames[n] for n in drive.session_names if n in frames and n not in excluded}
        if not present:
            skipped.append(
                {
                    "drive": drive.drive_id,
                    "sessions": list(drive.session_names),
                    "reason": "skipped: "
                    + "; ".join(
                        f"{n}: {loaded.load_errors.get(n, 'not loaded')}"
                        for n in drive.session_names
                        if n not in excluded
                    ),
                }
            )
            continue
        result = next(results)
        scores.update(result.scores)
        for session, reads in result.stream_reads.items():
            stream_reads[session] = stream_reads.get(session, 0) + reads
        worker_peak = max(worker_peak, result.peak_rss_bytes)
        for component, counts in result.cache_counts.items():
            for kind, value in counts.items():
                cache_counts.setdefault(component, {"hits": 0, "misses": 0})[kind] += value
        record = clean_drive_record(drive, present, scores, names, plan.registry.events)
        record.excluded_sessions = {n: excluded[n] for n in drive.session_names if n in excluded}
        record.cross_era = bool(plan.training_eras) and drive.era not in plan.training_eras
        drive_outcomes = list(result.outcomes)
        drive_slices = list(result.slices)
        drop_in_sample(
            plan.in_sample_by_drive.get(drive.drive_id, {}),
            sessions=list(present),
            scores=scores,
            record=record,
            outcomes=drive_outcomes,
            slices=drive_slices,
            shift=result.shift,
        )
        records.append(record)
        if result.shift is not None:
            shift_runs.append(result.shift)
        if record.cross_era:
            skipped_injections.append(
                {
                    "drive": drive.drive_id,
                    "reason": f"cross-era ({drive.era.label if drive.era else '?'}; "
                    f"artifacts trained in {sorted(e.label for e in plan.training_eras)})",
                }
            )
            continue
        outcomes.extend(drive_outcomes)
        skipped_injections.extend(result.skipped)
        slices.extend(drive_slices)
    log.info(
        "stream reads per session %s; peak RSS of a scoring process %.0f MB",
        stream_reads,
        worker_peak / 1e6,
    )
    log.info("score cache by component: %s", cache_counts)
    return Aggregate(
        scores,
        records,
        outcomes,
        skipped_injections,
        slices,
        shift_runs,
        skipped,
        cache_counts,
    )


def settle_score_cache(
    cache: ScoreCache,
    key_sources: KeySources,
    inputs: Mapping[str, str | None],
    *,
    started: float,
) -> list[str]:
    """Keep this run's staged scores only if no key input moved while it ran.

    Returns the names of the inputs that changed (the staged entries are then
    discarded: some score may have come from the new bytes), else commits,
    prunes and returns ``[]``. A cache with no root keeps nothing either way.
    """
    if cache.root is None:
        return []
    changed = changed_key_inputs(key_sources, inputs)
    if changed:
        # Some score may have come from the new bytes: keep none of them.
        cache.discard()
        log.warning(
            "score cache: nothing kept; key inputs changed during the run: %s",
            ", ".join(changed),
        )
        return changed
    cache.commit()
    cache.prune(keep_since=started)
    return []
