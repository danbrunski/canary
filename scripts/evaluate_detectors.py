#!/usr/bin/env python3
"""Score detectors on held-out drives, on the same rows, by one protocol (A02.4).

One command for every detector — the served Isolation Forest, sequence model
and fusion, and the A02.5 baselines — on drives none of them trained or
calibrated on:

    .venv/bin/python scripts/evaluate_detectors.py \\
        --config canary.example.yaml --profile nissan-xterra-2008 \\
        --sessions-root ~/canary-data/sessions/nissan-xterra-2008 \\
        --sessions-root ~/canary-data/sessions-0918 \\
        --out data/analyses/<date>-detector-baseline.json

The protocol lives in :mod:`src.models.evaluation`; the detectors in
:mod:`src.models.evaluation_detectors`; the realistic faults in
:mod:`src.models.synthetic_fault_eval`. This script loads sessions (through
the derived-frame store under ``--derived-dir``), decides which drives are held
out for *every* compared detector, scores clean and injected rows, and writes
a JSON report and a Markdown table with one row per drive.

A drive is *skipped* (with the reason) when a detector trained or calibrated
on any of its sessions, when it straddles a tune or hardware boundary, or
when it has no telemetry. Captures are scored: modes come from the same
segmenter the daemon runs (``featurize_wide_frame``), never from sidecars.
Rows are featurized with the profile's behavior models, as the trainers and
the daemon featurize them, so an artifact trained on ``deviation:*`` columns
is scored on those columns (the retired grid's blind spot, R06.5).

Drives are scored in parallel (``--workers``, P02.5) and every detector's
scores are cached under ``--cache-dir/scores``, keyed by the artifact's
sha256, the session frame's content, the injection and the code
(:mod:`src.models.evaluation_cache`): an unchanged re-run reads everything
back, and a new artifact is rescored for its own detector alone. The report
does not depend on ``--workers``; ``--scoring-threads`` fixes torch's thread
count in every process, because the LSTM's scores move in the last bits
between thread counts. An injected slice is featurized from its clean slice,
recomputing only what the injected channels reach (P02.4).

It is also retrain's post-train regression gate (R06.5, replacing the retired
``detector_benchmark`` grid): ``--baseline <report.json>`` diffs this run
against an earlier report of the same drives under the same protocol
parameters (:mod:`src.models.benchmark_gate`) and exits 3 on a regression.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import logging
import multiprocessing
import os
import pickle
import sys
import time
from collections.abc import Callable, Iterator, Mapping, Sequence
from concurrent.futures import BrokenExecutor, ProcessPoolExecutor
from contextlib import ExitStack
from dataclasses import dataclass, field, replace
from datetime import UTC, datetime
from functools import partial
from pathlib import Path
from typing import Any

import numpy as np
import pandas as pd

try:
    from _cli import (
        add_bindings_arg,
        add_common_args,
        add_log_level_arg,
        add_profile_arg,
        load_config,
        scoped_models_dir,
    )
except ImportError:  # loaded as `scripts.evaluate_detectors` — repo root on path, not scripts/
    from scripts._cli import (  # type: ignore[no-redef]
        add_bindings_arg,
        add_common_args,
        add_log_level_arg,
        add_profile_arg,
        load_config,
        scoped_models_dir,
    )

from src.analysis.labelled_events import (
    LABELLED_EVENTS_FILENAME,
    LabelledEvent,
    load_labelled_events,
)
from src.artifact_hash import sha256_file_or_none
from src.columns import MODE_COLUMN, TIMESTAMP_COLUMN
from src.config import CanaryConfig
from src.decode.behavior_models import BehaviorModels, load_behavior_models
from src.decode.bindings import Bindings, load_bindings
from src.decode.dids import DidMap, load_did_map
from src.log import configure_logging
from src.models.benchmark_gate import (
    DEFAULT_TOLERANCE,
    PROTOCOL_NAME,
    PROTOCOL_VERSION,
    diff_reports,
    load_sequence_artifact,
    regression_summary,
)
from src.models.evaluation import (
    AMBIENT_BANDS_C,
    AMBIENT_CHANNEL,
    BARO_BANDS_KPA,
    BARO_CHANNEL,
    Drive,
    PersistenceSpec,
    ScoreTrace,
    alert_budget_threshold,
    alert_episodes,
    band_of,
    bootstrap_ratio_ci,
    default_profile_exclusion,
    drives_of_sessions,
    engine_hours,
    engine_running,
    era_resolver,
    fault_attributable_detection,
    observed_median,
    session_span,
    single_quantity_shift,
    sustained_alerts,
    vus_pr,
)
from src.models.evaluation_cache import ScoreCache, code_version, digest, frame_key
from src.models.evaluation_detectors import (
    DeployedStack,
    Detector,
    DetectorOutput,
    MahalanobisBaseline,
    PCABaseline,
    PlausibilityDetector,
    RawStreamDetector,
    RobustZBaseline,
    ScoreModel,
    ThresholdedScore,
    UntrainedAutoencoder,
    detector_roles,
    load_curated_channels,
    raw_channels,
    sequence_outputs,
    sequence_parity,
    suppressed_for,
)
from src.models.false_positive_benchmark import V1_ALERTS_PER_HOUR_BUDGET
from src.models.isolation_forest import TrainedIsolationForest, load_isolation_forest_model
from src.models.sequence_protocol import TrainedSequenceModel
from src.models.spec_producibility import (
    BEHAVIOR_PROVENANCE_ABSENT,
    behavior_fingerprint_provenance,
    manifest_behavior_fingerprints,
)
from src.paths import DATA_DIR
from src.plausibility.limits import plausibility_path
from src.preprocessing.anomaly_injection import (
    REALISTIC_FAULT_ROLES,
    REALISTIC_FAULTS,
    choose_fault_window,
    inject_realistic_fault,
    load_fault_channel_roles,
)
from src.preprocessing.derived_store import DEFAULT_DERIVED_DIR, DerivedStore, on_the_pi
from src.preprocessing.drives import parent_session
from src.preprocessing.feature_cache import build_feature_frame_cache_key
from src.preprocessing.feature_pipeline import (
    DEFAULT_DIDS_PATH,
    FeaturizedFrame,
    _available_memory_bytes,
    default_decode_inputs,
    featurize_perturbed_frame,
    featurize_wide_frame,
    load_resampled_session,
)
from src.preprocessing.resampler import resample_config_for
from src.session import RECORDED_SESSION_KINDS, SessionKind, session_sort_key
from src.storage.sessions import recorded_sessions, session_started_at
from src.vehicle.profiles import active_registry, active_registry_sources
from src.vehicle.tune_revisions import load_tune_revisions

log = logging.getLogger(__name__)

#: Detector score caches: gitignored, regenerable, keyed on every input. The
#: resampled frames themselves live in the derived-frame store (P01.2).
DEFAULT_CACHE_DIR = DATA_DIR / "cache" / "evaluation_frames"
#: Clean rows before an injected window: the segmenter's lookback plus the
#: rolling window and one sequence window, so every scorer is warm.
DEFAULT_LEAD_SECONDS = 240.0
DEFAULT_WINDOW_SECONDS = 240.0
#: Fault-injection faults need a grace past the window for rolling features
#: and a sequence window to see the change.
DEFAULT_GRACE_SECONDS = 60.0


# ---------------------------------------------------------------------------
# Loading
# ---------------------------------------------------------------------------


#: Every recording under ``--sessions-root``, captures and legacy ``_tuning-*``
#: included: they are kept for *drive grouping* only, a sibling recording
#: bridging its neighbours into one physical drive. Whether their rows are
#: scored is :func:`~src.models.evaluation.default_profile_exclusion`'s call
#: (R04.6): tuning captures are not. ``_archive`` (quarantine) and ``_replay-*``
#: (re-emitted copies) are not drives.
EVALUATION_SESSION_KINDS = RECORDED_SESSION_KINDS | {SessionKind.TUNING}


def _load_one(
    args: tuple[Path, str | None, CanaryConfig],
) -> tuple[str, pd.DataFrame | None, str | None]:
    # The config parsed once by the parent, not re-read here per session:
    # re-reading it made a mid-run edit of the YAML reach the sessions loaded
    # after it (2026-09-26: a key added under ``collection`` while a run was
    # loading failed 49 sessions on the loaded schema, and dropped 9 drives).
    session, derived_dir, config = args
    try:
        store = DerivedStore(Path(derived_dir)) if derived_dir is not None else None
        return session.name, load_resampled_session(session, config, store=store), None
    except Exception as exc:  # noqa: BLE001 - one bad session is a skip, not a crash
        return session.name, None, f"{type(exc).__name__}: {exc}"


def load_resampled_sessions(
    sessions: Sequence[Path],
    config: CanaryConfig,
    *,
    derived_dir: Path | None,
    workers: int,
    cache_only: bool = False,
) -> tuple[dict[str, pd.DataFrame], dict[str, str]]:
    """Resampled frames by session name, through the derived-frame store (P01.2).

    Each session's reduced frame is read back from ``derived_dir``, refreshed
    for the decode units whose catalog entry changed, or built and kept
    (:class:`~src.preprocessing.derived_store.DerivedStore`); a stale frame is
    detected by hash and never served. ``derived_dir=None`` reduces in memory
    and keeps nothing. ``cache_only`` skips (and names) every session whose
    derived frame is missing or stale instead of deriving it.
    """
    jobs = [
        (session, str(derived_dir) if derived_dir is not None else None, config)
        for session in sessions
    ]
    frames: dict[str, pd.DataFrame] = {}
    errors: dict[str, str] = {}
    if cache_only:
        store = DerivedStore(derived_dir) if derived_dir is not None else None
        decoders = default_decode_inputs()
        resample = resample_config_for(config)
        ready = []
        for job in jobs:
            reason = (
                "no --derived-dir" if store is None else store.status(job[0], decoders, resample)
            )
            if reason is None:
                ready.append(job)
            else:
                errors[job[0].name] = f"not derived yet (--cache-only): {reason}"
        jobs = ready
    results: list[tuple[str, pd.DataFrame | None, str | None]] = []
    if workers > 1 and len(jobs) > 1:
        try:
            with ProcessPoolExecutor(max_workers=workers) as pool:
                results = list(pool.map(_load_one, jobs))
        except BrokenExecutor as exc:
            # A worker killed for memory breaks the pool. Finished sessions
            # are already derived, so the serial pass reads them back.
            log.warning("parallel session decode aborted (%s); finishing serially", exc)
            results = []
    if not results:
        results = [_load_one(job) for job in jobs]
    for name, frame, error in results:
        if frame is None:
            errors[name] = error or "unreadable"
        elif frame.empty:
            errors[name] = "no rows after decode"
        else:
            frames[name] = frame
    return frames, errors


def _scoped(config: CanaryConfig, profile: str | None) -> CanaryConfig:
    """The profile-scoped config every read of this run goes through (P02.11):
    featurization's afr_calibration, the plausibility limits and the tune
    registry live in the profile's data directory."""
    from src.vehicle.profiles import resolve_profile_for_tooling, scope_config

    if not config.profiles.enabled:
        return config
    return scope_config(config, resolve_profile_for_tooling(config, profile))


class CachedStreams(Mapping[tuple[str, str], Any]):
    """Arrived-sample streams by ``(detector, session)``, read from their cache
    file on each access and never held.

    Holding every session's stream at once was most of the harness's memory:
    RSS rose from 2.3 GB to 6.0 GB while the A02.11 streams loaded (2.4 GB of
    samples plus what the allocator kept). Before P02.4 the longest session's
    stream was read twice per drive (clean scoring, then the injection sweep,
    which reused it for every injection); :class:`DriveScorer` now reads each
    once per drive, and not at all when every score it feeds is cached.
    """

    def __init__(self, paths: Mapping[tuple[str, str], Path]) -> None:
        self._paths = dict(paths)

    @property
    def paths(self) -> dict[tuple[str, str], Path]:
        return dict(self._paths)

    def name_of(self, key: tuple[str, str]) -> str:
        """The stream's cache-file name: its session key and catalog
        fingerprint, so a score keyed on it follows an edited limit."""
        return self._paths[key].name

    def __getitem__(self, key: tuple[str, str]) -> Any:
        from src.plausibility.offline import SessionStream

        frame = pd.read_parquet(self._paths[key])
        modes = frame.pop("mode").to_numpy(dtype=object)
        modes[modes == "None"] = None
        return SessionStream(key[1], frame.reset_index(drop=True), modes)

    def __iter__(self) -> Iterator[tuple[str, str]]:
        return iter(self._paths)

    def __len__(self) -> int:
        return len(self._paths)


def load_raw_streams(
    detectors: Sequence[RawStreamDetector],
    sessions: Sequence[Path],
    config: CanaryConfig,
    *,
    cache_dir: Path,
) -> CachedStreams:
    """Each arrived-sample detector's stream per session, cached as parquet.

    The key is the session's feature-frame cache key (session file stats,
    bindings, DBC, DID map, preprocessing) plus the detector's own catalog
    fingerprint, so an edited limit or binding cannot be served a stale stream.
    Streams are built one at a time into the cache and read back on use
    (:class:`CachedStreams`), so only the one in use is in memory.
    """
    paths: dict[tuple[str, str], Path] = {}
    for detector in detectors:
        # Every resolved limit, policy and source, hashed stably across
        # processes (a repr hash followed PYTHONHASHSEED and never hit).
        catalog = getattr(detector, "catalog", None)
        fingerprint = (
            catalog.fingerprint()[:12]
            if catalog is not None
            else hashlib.sha256(b"no catalog").hexdigest()[:12]
        )
        for session in sessions:
            key = build_feature_frame_cache_key([session], config)
            path = cache_dir / detector.names[0] / f"{key}-{fingerprint}.parquet"
            if not path.exists():
                stream = detector.load(session)
                path.parent.mkdir(parents=True, exist_ok=True)
                stream.samples.assign(mode=stream.modes.astype(str)).to_parquet(path)
                del stream
            paths[(detector.names[0], session.name)] = path
    return CachedStreams(paths)


def record_seen(seen: dict[str, dict[str, str]], label: str, manifest: Any) -> None:
    """Note every recording ``manifest`` trained or calibrated on, by directory.

    Manifests name the pieces they used, and a recording split at a data gap
    is named ``<dir>#segment-00N``; drives list directories. Recording the
    parent directory is what lets a drive the detector saw part of be
    skipped (the 2026-09-23 candidates name ``2026-09-21T143331#segment-001``).
    """
    for role in ("training_sessions", "calibration_sessions"):
        for name in getattr(manifest, role, ()) or ():
            seen.setdefault(parent_session(str(name)), {})[label] = role.removesuffix("_sessions")


#: R01.6: the one definition is ``src.artifact_hash.sha256_file_or_none``.
file_sha256 = sha256_file_or_none


# ---------------------------------------------------------------------------
# Scoring bookkeeping
# ---------------------------------------------------------------------------


@dataclass
class SessionScore:
    """One detector's clean scoring of one session."""

    timestamps: pd.Series
    verdict: np.ndarray[Any, np.dtype[np.object_]]
    score: np.ndarray[Any, np.dtype[np.float64]]
    alerting: np.ndarray[Any, np.dtype[np.bool_]]


def session_scores(
    outputs: Mapping[str, DetectorOutput],
    timestamps: pd.Series,
    persistence: PersistenceSpec,
) -> dict[str, SessionScore]:
    """Every detector's output on one unit's rows, through the persistence gate."""
    out: dict[str, SessionScore] = {}
    for name, output in outputs.items():
        alerting = sustained_alerts(
            timestamps, list(output.verdict), persistence, pre_gated=output.pre_gated
        )
        out[name] = SessionScore(timestamps, output.verdict, output.score, alerting)
    return out


def _interval_mask(timestamps: pd.Series, event: LabelledEvent) -> np.ndarray[Any, Any]:
    start = pd.Timestamp(event.start)
    end = pd.Timestamp(event.end)
    return ((timestamps >= start) & (timestamps <= end)).to_numpy()


@dataclass
class DriveRecord:
    drive: Drive
    sessions: list[str]
    rows: int = 0
    hours: float = 0.0
    baro_kpa: float | None = None
    ambient_c: float | None = None
    labels: list[str] = field(default_factory=list)
    detectors: dict[str, dict[str, Any]] = field(default_factory=dict)
    #: Recorded in a tune/hardware era none of the scored artifacts trained
    #: in: reported per drive, kept out of every summary (no fold mixes eras).
    cross_era: bool = False
    #: Sessions of the drive kept out of every metric, and why (R04.6: tuning
    #: captures bridge the drive but are not the default profile).
    excluded_sessions: dict[str, str] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return {
            "drive": self.drive.drive_id,
            "cross_era": self.cross_era,
            "sessions": self.sessions,
            # Sessions of this drive that could not be loaded: the drive was
            # scored on the rest, and says so.
            "missing_sessions": sorted(
                set(self.drive.session_names) - set(self.sessions) - set(self.excluded_sessions)
            ),
            "excluded_sessions": dict(sorted(self.excluded_sessions.items())),
            "start": self.drive.start.isoformat(),
            "end": self.drive.end.isoformat(),
            "era": self.drive.era.label if self.drive.era else None,
            "rows": self.rows,
            "engine_hours": self.hours,
            "baro_kpa": self.baro_kpa,
            "baro_band": band_of(self.baro_kpa, BARO_BANDS_KPA),
            "ambient_c": self.ambient_c,
            "ambient_band": band_of(self.ambient_c, AMBIENT_BANDS_C),
            "labels": self.labels,
            "detectors": self.detectors,
        }


def clean_drive_record(
    drive: Drive,
    session_frames: Mapping[str, pd.DataFrame],
    scores: Mapping[str, Mapping[str, SessionScore]],
    names: Sequence[str],
    labels: Sequence[LabelledEvent],
) -> DriveRecord:
    """Per-drive clean metrics for every detector.

    Every episode is a false alarm unless it starts inside a ``fault`` label —
    a catch. Episodes inside ``novelty`` or ``intervention`` labels are still
    false alarms for a fault detector (an altitude trip is not a fault), and
    are broken out by kind so the report shows how many of them the labels
    explain. An earlier version exempted every labelled episode; the 81-day
    LTFT-cleared label then hid whole drives (133 robust-z episodes on one).
    """
    record = DriveRecord(drive, sorted(session_frames))
    frames = [session_frames[name] for name in record.sessions]
    joined = pd.concat(frames, ignore_index=True)
    record.rows = len(joined)
    record.hours = sum(
        engine_hours(pd.to_datetime(f[TIMESTAMP_COLUMN], utc=True), engine_running(f))
        for f in frames
    )
    record.baro_kpa = observed_median(joined, BARO_CHANNEL)
    record.ambient_c = observed_median(joined, AMBIENT_CHANNEL)
    for name in names:
        rows = scored = flagged = episodes = 0
        by_kind: dict[str, int] = {}
        for session in record.sessions:
            result = scores[session].get(name)
            if result is None:
                continue
            verdicts = list(result.verdict)
            rows += len(verdicts)
            scored += sum(1 for v in verdicts if v is not None)
            flagged += sum(1 for v in verdicts if v is True or v is np.True_)
            found = alert_episodes(result.timestamps, result.alerting, verdicts=verdicts)
            episodes += len(found)
            for episode in found:
                for event in labels:
                    if pd.Timestamp(event.start) <= episode.start <= pd.Timestamp(event.end):
                        by_kind[event.kind] = by_kind.get(event.kind, 0) + 1
                        break
        false_alarms = episodes - by_kind.get("fault", 0)
        record.detectors[name] = {
            "rows": rows,
            "scored_rows": scored,
            "withheld_rows": rows - scored,
            "flagged_rows": flagged,
            "episodes": episodes,
            "labelled_episodes": by_kind,
            "false_alarm_episodes": false_alarms,
            "false_alarms_per_engine_hour": (
                None if record.hours <= 0 else false_alarms / record.hours
            ),
        }
    record.labels = sorted(
        event.id
        for event in labels
        if pd.Timestamp(event.start) <= pd.Timestamp(drive.end)
        and pd.Timestamp(event.end) >= pd.Timestamp(drive.start)
    )
    return record


def _severity_sweep(
    kinds: Sequence[str],
) -> list[tuple[str, float]]:
    return [(kind, severity) for kind in kinds for severity in REALISTIC_FAULTS[kind].severities]


def _seed_for(*parts: object) -> int:
    digest = hashlib.sha256(":".join(str(p) for p in parts).encode()).digest()
    return int.from_bytes(digest[:4], "big")


# ---------------------------------------------------------------------------
# Scoring units, the score cache and the per-drive scorer (P02.4/P02.5)
# ---------------------------------------------------------------------------

#: Score-cache entry holding a unit's row timestamps: the grid every detector
#: on that unit is graded on, so a fully cached unit needs no featurization.
ROWS_ENTRY = "_rows"
#: Score-cache entry holding how many rows a shift slice has (0 = no run).
SHIFT_ROWS_ENTRY = "_shift_rows"


def _component_key(name: str, identity: str, code: str) -> str:
    return f"{name}-{digest(name, identity, code)[:20]}"


@dataclass(frozen=True)
class Component:
    """One separately cached part of the detector set.

    The deployed stack is three: the IF, the sequence detector (``sequence``
    and ``sequence_global`` share one forward pass) and the fusion over both,
    which reads their outputs and no features. Each baseline is one.
    """

    name: str
    key: str
    names: tuple[str, ...]
    compute: Callable[[pd.DataFrame | None, Mapping[str, Any]], Any]
    outputs: Callable[[Any], dict[str, DetectorOutput]]
    requires: tuple[str, ...] = ()
    needs_features: bool = True


def detector_components(
    detectors: Sequence[Detector], identities: Mapping[str, str], code: str
) -> list[Component]:
    """The cacheable parts of ``detectors``, in the order their outputs were
    always reported (a report's detector order is part of its identity)."""
    components: list[Component] = []
    for detector in detectors:
        if isinstance(detector, DeployedStack):
            stack = detector
            if stack.trained_if is not None:
                components.append(
                    Component(
                        "isolation_forest",
                        _component_key("isolation_forest", identities["isolation_forest"], code),
                        ("isolation_forest",),
                        lambda f, _d, s=stack: s.score_isolation_forest(f),
                        lambda p: {"isolation_forest": p.output},
                    )
                )
            if stack.sequence is not None:
                components.append(
                    Component(
                        "sequence",
                        _component_key("sequence", identities["sequence"], code),
                        ("sequence", "sequence_global"),
                        lambda f, _d, s=stack: s.score_sequence(f),
                        sequence_outputs,
                    )
                )
            if (
                "fused" not in stack.withheld
                and stack.trained_if is not None
                and stack.sequence is not None
            ):
                components.append(
                    Component(
                        "fused",
                        _component_key(
                            "fused",
                            digest(identities["isolation_forest"], identities["sequence"]),
                            code,
                        ),
                        ("fused",),
                        lambda _f, d, s=stack: s.fuse(d["isolation_forest"], d["sequence"]),
                        lambda p: {"fused": p},
                        requires=("isolation_forest", "sequence"),
                        needs_features=False,
                    )
                )
        elif isinstance(detector, ThresholdedScore):
            name = detector.model.name
            components.append(
                Component(
                    name,
                    _component_key(name, identities[name], code),
                    (name,),
                    lambda f, _d, t=detector: t.score(f),
                    dict,
                )
            )
        else:
            raise TypeError(f"no cache identity for detector {type(detector).__name__}")
    return components


def score_model_identity(
    model: ScoreModel, threshold: float, *, sequence: str | None, seed: int
) -> str:
    """A thresholded baseline's cache identity: its fitted parameters (or, for
    the untrained autoencoder, the artifact whose shape it copies and its
    seed) and its threshold."""
    if isinstance(model, UntrainedAutoencoder):
        return digest("untrained", sequence, seed, repr(threshold))
    return digest(hashlib.sha256(pickle.dumps(model, protocol=4)).hexdigest(), repr(threshold))


@dataclass
class DriveTask:
    """One held-out drive's rows, and what to run on them."""

    drive: Drive
    frames: dict[str, pd.DataFrame]
    frame_keys: dict[str, str]
    shift: bool
    sweep: bool


@dataclass
class DriveResult:
    """What scoring one drive produced, merged by the parent in drive order."""

    scores: dict[str, dict[str, SessionScore]]
    shift: dict[str, Any] | None
    outcomes: list[dict[str, Any]]
    skipped: list[dict[str, Any]]
    slices: list[dict[str, Any]]
    stream_reads: dict[str, int]
    peak_rss_bytes: int
    cache_counts: dict[str, dict[str, int]]
    #: The process that scored the drive (a pool worker's, or the parent's).
    pid: int = field(default_factory=os.getpid)


@dataclass(frozen=True)
class SweepSpec:
    """The injection sweep's and the shift test's parameters."""

    kinds: tuple[str, ...]
    reps: int
    seed: int
    lead_seconds: float
    window_seconds: float
    grace_seconds: float
    shift_rows: int


def _peak_rss_bytes() -> int:
    import resource

    return int(resource.getrusage(resource.RUSAGE_SELF).ru_maxrss) * 1024


class _Lazy:
    """A value computed on first use and kept."""

    def __init__(self, build: Callable[[], Any]) -> None:
        self._build = build
        self._done = False
        self._value: Any = None

    def __call__(self) -> Any:
        if not self._done:
            self._value = self._build()
            self._done = True
            self._build = lambda: None
        return self._value


def _features_of(featurized: Callable[[], FeaturizedFrame]) -> pd.DataFrame:
    return featurized().features


class DriveScorer:
    """Scores one drive's units through the score cache.

    A unit is a set of rows every detector is graded on: a held-out session,
    an injection window's clean slice, one injected slice, or a drive's shift
    slice. Each component's output on it is read back from the cache or
    computed and kept; the features are built only when some component
    misses, and an injected slice is featurized incrementally from its clean
    slice (``featurize_perturbed_frame``, P02.4).
    """

    def __init__(
        self,
        *,
        detectors: Sequence[Detector],
        raw_detectors: Sequence[RawStreamDetector],
        identities: Mapping[str, str],
        stream_paths: Mapping[tuple[str, str], Path],
        cache: ScoreCache,
        code: str,
        feature_key: str,
        config: CanaryConfig,
        bindings: Bindings | None,
        did_map: DidMap | None,
        behavior: BehaviorModels | None,
        persistence: PersistenceSpec,
        roles: Mapping[str, Sequence[str]],
        sweep: SweepSpec,
    ) -> None:
        self.detectors = list(detectors)
        self.components = detector_components(detectors, identities, code)
        self.raw = list(raw_detectors)
        self.raw_keys = {
            r.names[0]: _component_key(r.names[0], identities[r.names[0]], code) for r in self.raw
        }
        self.streams = CachedStreams(stream_paths)
        self.cache = cache
        self.feature_key = feature_key
        self.config = config
        self.bindings = bindings
        # The catalog the session frames were decoded with (P02.14).
        self.did_map = did_map
        self.behavior = behavior
        self.persistence = persistence
        self.roles = {role: list(channels) for role, channels in roles.items()}
        self.spec = sweep

    def unit(self, *parts: Any) -> str:
        return digest(self.feature_key, *parts)

    def featurize(self, frame: pd.DataFrame) -> FeaturizedFrame:
        return featurize_wide_frame(
            frame,
            self.config,
            bindings=self.bindings,
            behavior_models=self.behavior,
            did_map=self.did_map,
        )

    def score_unit(
        self,
        unit: str,
        features: Callable[[], pd.DataFrame],
        raw: Sequence[tuple[RawStreamDetector, str, Callable[[], Any]]] = (),
    ) -> tuple[dict[str, DetectorOutput], pd.Series]:
        """Every detector's output on ``unit``, and its row timestamps.

        ``features`` builds the unit's featurized rows; it is called at most
        once, and only when a component misses. ``raw`` pairs each
        arrived-sample detector with its unit-specific key part and a builder
        for its stream on these rows.
        """
        built = _Lazy(features)
        payloads: dict[str, Any] = {}
        for component in self.components:
            payload = self.cache.get(component.key, unit)
            if payload is None:
                payload = component.compute(
                    built() if component.needs_features else None,
                    {name: payloads[name] for name in component.requires},
                )
                self.cache.put(component.key, unit, payload)
            payloads[component.name] = payload
        timestamps = self.cache.get(ROWS_ENTRY, unit)
        if timestamps is None:
            timestamps = pd.to_datetime(built()[TIMESTAMP_COLUMN], utc=True).reset_index(drop=True)
            self.cache.put(ROWS_ENTRY, unit, timestamps)
        outputs: dict[str, DetectorOutput] = {}
        for component in self.components:
            outputs.update(component.outputs(payloads[component.name]))
        for detector, part, stream in raw:
            key = self.raw_keys[detector.names[0]]
            raw_unit = digest(unit, part)
            result = self.cache.get(key, raw_unit)
            if result is None:
                result = detector.score_stream(stream(), timestamps)
                self.cache.put(key, raw_unit, result)
            outputs.update(result)
        return outputs, timestamps

    def score_drive(self, task: DriveTask) -> DriveResult:
        """Clean scores for every session, then the shift test and the
        injection sweep on the drive's longest session."""
        stream_reads: dict[str, int] = {}
        loaded: dict[tuple[str, str], Any] = {}
        before = self.cache.counts()

        present = task.frames
        longest = max(present, key=lambda n: int(engine_running(present[n]).sum()))

        def stream_of(detector: RawStreamDetector, session: str) -> Any:
            # Each stream is read once per drive. The longest session's is
            # held for the drive: it serves its clean scoring and the whole
            # sweep (before P02.4 it was read twice). The others are used
            # once and never held (holding them all was most of the memory).
            key = (detector.names[0], session)
            if key in loaded:
                return loaded[key]
            stream_reads[session] = stream_reads.get(session, 0) + 1
            stream = self.streams[key]
            if session == longest:
                loaded[key] = stream
            return stream

        longest_features = _Lazy(lambda: self.featurize(present[longest]).features)
        scores: dict[str, dict[str, SessionScore]] = {}
        for name, frame in present.items():
            unit = self.unit("session", task.frame_keys[name])
            builder = (
                longest_features
                if name == longest
                else (lambda f=frame: self.featurize(f).features)
            )
            outputs, stamps = self.score_unit(
                unit,
                builder,
                raw=[
                    (
                        r,
                        self.streams.name_of((r.names[0], name)),
                        lambda r=r, n=name: stream_of(r, n),
                    )
                    for r in self.raw
                ],
            )
            scores[name] = session_scores(outputs, stamps, self.persistence)
        shift = (
            self.shift(task.drive, longest, task.frame_keys[longest], longest_features)
            if task.shift
            else None
        )
        outcomes: list[dict[str, Any]] = []
        skipped: list[dict[str, Any]] = []
        slices: list[dict[str, Any]] = []
        if task.sweep:
            outcomes, skipped, slices = self.injection_sweep(
                task.drive,
                present[longest],
                longest,
                task.frame_keys[longest],
                stream=lambda r: stream_of(r, longest),
            )
        after = self.cache.counts()
        counts = {
            name: {kind: value - before.get(name, {}).get(kind, 0) for kind, value in entry.items()}
            for name, entry in after.items()
        }
        return DriveResult(
            scores, shift, outcomes, skipped, slices, stream_reads, _peak_rss_bytes(), counts
        )

    def shift(
        self,
        drive: Drive,
        session: str,
        frame_key: str,
        features: Callable[[], pd.DataFrame],
    ) -> dict[str, Any] | None:
        """A04.1 on the drive's longest session, each component's report cached."""
        unit = self.unit("shift", self.unit("session", frame_key), self.spec.shift_rows, self.roles)
        rows = self.cache.get(SHIFT_ROWS_ENTRY, unit)
        # A slice known to be empty has no detector reports to look up: asking
        # would count a miss per component on a re-run that computes nothing.
        cached: dict[str, Any] = (
            {c.name: None for c in self.components}
            if rows == 0
            else {c.name: self.cache.get(c.key, unit) for c in self.components}
        )
        missing = [c for c in self.components if cached[c.name] is None]
        if rows is None or (rows and missing):
            piece = shift_slice(features(), self.spec.shift_rows)
            rows = len(piece)
            self.cache.put(SHIFT_ROWS_ENTRY, unit, rows)
            if rows and missing:
                needed = self._closure(missing)

                def score(frame: pd.DataFrame) -> dict[str, Any]:
                    payloads: dict[str, Any] = {}
                    for component in self.components:
                        if component.name in needed:
                            payloads[component.name] = component.compute(
                                frame if component.needs_features else None,
                                {name: payloads[name] for name in component.requires},
                            )
                    return {
                        name: output.verdict
                        for component in missing
                        for name, output in component.outputs(payloads[component.name]).items()
                    }

                reports = single_quantity_shift(score, piece, self.roles, mode_column=MODE_COLUMN)
                for component in missing:
                    cached[component.name] = {
                        name: reports[name].to_dict() for name in component.names if name in reports
                    }
                    self.cache.put(component.key, unit, cached[component.name])
        if not rows:
            return None
        return {
            "drive": drive.drive_id,
            "session": session,
            "rows": rows,
            "detectors": {
                name: report
                for component in self.components
                for name, report in (cached[component.name] or {}).items()
            },
        }

    def _closure(self, components: Sequence[Component]) -> set[str]:
        needed = {c.name for c in components}
        for component in reversed(self.components):
            if component.name in needed:
                needed.update(component.requires)
        return needed

    @staticmethod
    def _window(
        detector: RawStreamDetector,
        stream: Callable[[RawStreamDetector], Any],
        lo: pd.Timestamp,
        hi: pd.Timestamp,
        channels: Sequence[str],
    ) -> Any:
        return detector.window(stream(detector), lo, hi, channels=channels)

    def _injected_stream(
        self,
        detector: RawStreamDetector,
        window: Callable[[], Any],
        *,
        kind: str,
        severity: float,
        start: pd.Timestamp,
        end: pd.Timestamp,
    ) -> Any:
        return detector.inject(
            window(), kind=kind, severity=severity, start=start, end=end, roles=self.roles
        )

    def _perturbed_features(
        self, clean: Callable[[], FeaturizedFrame], piece: pd.DataFrame, faulted: pd.DataFrame
    ) -> pd.DataFrame:
        return featurize_perturbed_frame(
            clean(),
            piece,
            faulted,
            self.config,
            bindings=self.bindings,
            behavior_models=self.behavior,
            did_map=self.did_map,
        )

    def injection_sweep(
        self,
        drive: Drive,
        session_frame: pd.DataFrame,
        session_name: str,
        frame_key: str,
        *,
        stream: Callable[[RawStreamDetector], Any],
    ) -> tuple[list[dict[str, Any]], list[dict[str, Any]], list[dict[str, Any]]]:
        """Inject every (kind, severity) into the same windows of one session.

        One window per (kind, rep) — shared across severities so recall against
        severity compares the same rows. Each window's clean slice is scored once;
        a detection is an alert the fault caused on those rows. Returns
        ``(outcomes, skipped, slices)``; ``slices`` carry scores and labels for
        the threshold-free VUS-PR. An injected slice is featurized from its
        clean one, recomputing only what the injected channels reach (P02.4).
        """
        spec = self.spec
        outcomes: list[dict[str, Any]] = []
        skipped: list[dict[str, Any]] = []
        slices: list[dict[str, Any]] = []
        stamps = pd.to_datetime(session_frame[TIMESTAMP_COLUMN], utc=True)
        for kind in spec.kinds:
            for rep in range(spec.reps):
                rng = np.random.default_rng(_seed_for(spec.seed, drive.drive_id, kind, rep))
                window = choose_fault_window(
                    session_frame,
                    kind=kind,
                    length_seconds=spec.window_seconds,
                    lead_seconds=spec.lead_seconds,
                    rng=rng,
                )
                if isinstance(window, str):
                    skipped.append(
                        {"drive": drive.drive_id, "kind": kind, "rep": rep, "reason": window}
                    )
                    continue
                start, end = window
                lo = start - pd.Timedelta(seconds=spec.lead_seconds)
                hi = end + pd.Timedelta(seconds=spec.grace_seconds + spec.window_seconds / 4)
                piece = session_frame.loc[((stamps >= lo) & (stamps < hi)).to_numpy()].reset_index(
                    drop=True
                )
                fault_channels = [
                    channel
                    for role in REALISTIC_FAULT_ROLES[kind]
                    for channel in self.roles.get(role, ())
                ]
                clean_frame = _Lazy(partial(self.featurize, piece))
                windows = {
                    r.names[0]: _Lazy(partial(self._window, r, stream, lo, hi, fault_channels))
                    for r in self.raw
                }
                raw_parts = {
                    r.names[0]: digest(
                        self.streams.name_of((r.names[0], session_name)), fault_channels
                    )
                    for r in self.raw
                }
                slice_unit = self.unit("slice", frame_key, lo.isoformat(), hi.isoformat())
                clean_outputs, clean_stamps = self.score_unit(
                    slice_unit,
                    partial(_features_of, clean_frame),
                    raw=[(r, raw_parts[r.names[0]], windows[r.names[0]]) for r in self.raw],
                )
                clean = session_scores(clean_outputs, clean_stamps, self.persistence)
                for severity in REALISTIC_FAULTS[kind].severities:
                    faulted_frame, injection = inject_realistic_fault(
                        piece,
                        kind=kind,
                        severity=severity,
                        start=start,
                        end=end,
                        roles=self.roles,
                        bindings=self.bindings,
                    )
                    if not injection.perturbed:
                        skipped.append(
                            {
                                "drive": drive.drive_id,
                                "kind": kind,
                                "rep": rep,
                                "severity": severity,
                                "reason": f"no role channel could be perturbed: "
                                f"{dict(injection.not_perturbed)}",
                            }
                        )
                        continue
                    injected_unit = digest(
                        slice_unit,
                        "inject",
                        kind,
                        severity,
                        start.isoformat(),
                        end.isoformat(),
                        self.roles,
                    )
                    faulted_outputs, faulted_stamps = self.score_unit(
                        injected_unit,
                        partial(self._perturbed_features, clean_frame, piece, faulted_frame),
                        raw=[
                            (
                                r,
                                raw_parts[r.names[0]],
                                partial(
                                    self._injected_stream,
                                    r,
                                    windows[r.names[0]],
                                    kind=kind,
                                    severity=severity,
                                    start=start,
                                    end=end,
                                ),
                            )
                            for r in self.raw
                        ],
                    )
                    faulted = session_scores(faulted_outputs, faulted_stamps, self.persistence)
                    for name, result in faulted.items():
                        outcome = fault_attributable_detection(
                            result.timestamps,
                            faulted_alerting=result.alerting,
                            clean_alerting=clean[name].alerting,
                            faulted_verdicts=list(result.verdict),
                            start=start,
                            end=end,
                            grace_seconds=spec.grace_seconds,
                        )
                        outcomes.append(
                            {
                                "drive": drive.drive_id,
                                "session": session_name,
                                "kind": kind,
                                "severity": severity,
                                "rep": rep,
                                "detector": name,
                                "status": outcome.status,
                                "time_to_detect_seconds": outcome.time_to_detect_seconds,
                                "injection": injection,
                            }
                        )
                        labels = (
                            (result.timestamps >= start)
                            & (result.timestamps < end + pd.Timedelta(seconds=spec.grace_seconds))
                        ).to_numpy()
                        slices.append(
                            {
                                "drive": drive.drive_id,
                                "kind": kind,
                                "detector": name,
                                "scores": result.score,
                                "labels": labels,
                            }
                        )
        return outcomes, skipped, slices


# ---------------------------------------------------------------------------
# Parallel scoring (P02.5)
# ---------------------------------------------------------------------------

#: Peak RSS of one scoring worker on A02.11's drives, measured 2026-09-27
#: (P02.5): 3,272 / 3,291 / 3,251 MB at 2 / 3 / 4 workers, set by the worker
#: that scores the drive holding ``2026-09-20T152758`` (8,570 rows, whose
#: injection sweep holds the longest arrived-sample stream). The pool is sized
#: so this many bytes per worker fit in :data:`SCORING_MEMORY_FRACTION` of
#: ``MemAvailable``, as if every worker peaked at once; the peaks do not
#: coincide (4 workers peaked at 9.40 GB for the whole tree, parent included).
SCORING_WORKER_PEAK_BYTES = 3_300_000_000
SCORING_MEMORY_FRACTION = 0.8


@dataclass
class ScorerSpec:
    """What a spawned scoring worker rebuilds its :class:`DriveScorer` from.

    Spawned, not forked: the parent has already run torch (the parity check
    and the untrained autoencoder's calibration), and a forked child of a
    process with a live OpenMP pool can hang in its first parallel region.
    Artifacts are reloaded from their paths; fitted baselines travel as
    fitted (the untrained autoencoder as ``None``, rebuilt from its seed).
    """

    #: Profile-scoped (P02.11): featurization refuses anything else.
    config: CanaryConfig
    if_path: Path | None
    sequence_path: Path | None
    baselines: list[tuple[ScoreModel | None, float]]
    untrained_seed: int
    plausibility: bool
    bindings_path: Path | None
    behavior_path: Path | None
    identities: dict[str, str]
    stream_paths: dict[tuple[str, str], Path]
    cache_root: Path | None
    cache_run: str
    key_sources: KeySources
    key_inputs: dict[str, str | None]
    code: str
    feature_key: str
    persistence: PersistenceSpec
    roles: dict[str, list[str]]
    sweep: SweepSpec
    threads: int


def build_worker_scorer(spec: ScorerSpec) -> DriveScorer:
    """The parent's detectors, rebuilt the way ``main`` builds them."""
    config = spec.config
    trained_if = load_isolation_forest_model(spec.if_path) if spec.if_path is not None else None
    trained_seq = (
        load_sequence_artifact(spec.sequence_path) if spec.sequence_path is not None else None
    )
    stack = DeployedStack(
        config,
        trained_if,
        trained_seq,
        suppress_channels=suppressed_for(trained_seq, config) if trained_seq else (),
    )
    detectors: list[Detector] = [stack]
    for model, threshold in spec.baselines:
        if model is None:
            assert trained_seq is not None
            model = UntrainedAutoencoder.like(
                trained_seq,
                seed=spec.untrained_seed,
                suppress_channels=suppressed_for(trained_seq, config),
            )
        detectors.append(ThresholdedScore(model, threshold))
    bindings = (
        load_bindings(spec.bindings_path)
        if spec.bindings_path is not None and spec.bindings_path.exists()
        else None
    )
    # The catalog load_resampled_session decoded the frames with.
    did_map = default_decode_inputs().did_map
    raw: list[RawStreamDetector] = []
    if spec.plausibility:
        raw.append(
            PlausibilityDetector(
                spec.config,
                bindings=bindings,
                did_map=load_did_map(DEFAULT_DIDS_PATH) if DEFAULT_DIDS_PATH.exists() else None,
            )
        )
    behavior = (
        load_behavior_models(spec.behavior_path)
        if spec.behavior_path is not None and spec.behavior_path.exists()
        else None
    )
    return DriveScorer(
        detectors=detectors,
        raw_detectors=raw,
        identities=spec.identities,
        stream_paths=spec.stream_paths,
        cache=ScoreCache(spec.cache_root, run=spec.cache_run),
        code=spec.code,
        feature_key=spec.feature_key,
        config=config,
        bindings=bindings,
        did_map=did_map,
        behavior=behavior,
        persistence=spec.persistence,
        roles=spec.roles,
        sweep=spec.sweep,
    )


_WORKER_SCORER: DriveScorer | None = None

#: Torch threads per scoring process (``--scoring-threads``). Fixed, never
#: derived from ``--workers``: the LSTM's float32 sums differ in the last
#: bits between thread counts (measured: 6 vs 4 threads moved scores by up to
#: 7.8e-07), so a count that followed the pool size would make the report
#: depend on it. One thread per process makes it depend on neither the
#: worker count nor the machine's core count; the pool supplies parallelism.
DEFAULT_SCORING_THREADS = 1


def pin_threads(threads: int) -> Callable[[], None]:
    """Hold this process's torch intra-op pool to ``threads`` (``0`` leaves
    torch's own default); returns what puts the previous count back. NumPy's
    BLAS pool is left alone on purpose: its results also move with the thread
    count, and every process then uses the same machine default, as the
    harness always did."""
    if threads <= 0:
        return lambda: None
    try:
        import torch
    except ImportError:  # torch-free installs score without the sequence model
        return lambda: None
    previous = torch.get_num_threads()
    torch.set_num_threads(threads)
    return lambda: torch.set_num_threads(previous)


def _init_scoring_worker(spec: ScorerSpec) -> None:
    global _WORKER_SCORER
    pin_threads(spec.threads)
    # This worker reloads the artifacts, catalogs and code from disk: if any
    # of them is not what the parent keyed this run on, its scores would be
    # staged under a key they do not match. Refuse; the parent then scores
    # in-process with what it loaded, and commits nothing (P02.5).
    changed = changed_key_inputs(spec.key_sources, spec.key_inputs)
    if changed:
        raise RuntimeError(
            "scoring worker refused: key inputs changed since the run started: "
            + ", ".join(changed)
        )
    _WORKER_SCORER = build_worker_scorer(spec)


def _score_drive_task(task: DriveTask) -> DriveResult:
    assert _WORKER_SCORER is not None, "scoring worker was not initialized"
    return _WORKER_SCORER.score_drive(task)


def _cgroup_memory_headroom(
    *, cgroup_root: Path | None = None, self_cgroup: Path | None = None
) -> int | None:
    """The least ``memory.max - memory.current`` over this process's cgroup
    and every parent group below the root (a parent's ceiling binds its
    children too), or ``None`` when no level sets a byte ceiling (``max``) or
    the files cannot be read. The group is found by the daemon's own parser
    (``memory_limit_status``)."""
    from src.daemon.health import CGROUP_ROOT, memory_limit_status

    root = cgroup_root or CGROUP_ROOT
    status = memory_limit_status(
        cgroup_root=root, self_cgroup=self_cgroup or Path("/proc/self/cgroup")
    )
    if status["memory_max_source"] is None:
        return None
    group = Path(status["memory_max_source"]).parent
    headroom: int | None = None
    while group != root and group.is_relative_to(root):
        try:
            ceiling = (group / "memory.max").read_text().strip()
            if ceiling != "max":
                current = int((group / "memory.current").read_text())
                room = max(0, int(ceiling) - current)
                headroom = room if headroom is None else min(headroom, room)
        except (OSError, ValueError):
            pass
        group = group.parent
    return headroom


def scoring_workers(requested: int, tasks: int, *, threads: int = 1) -> int:
    """Workers for ``tasks`` drives: at most ``requested``, one per drive, no
    more than the cores hold at ``threads`` torch threads each, and as many
    as :data:`SCORING_WORKER_PEAK_BYTES` fit in the memory there is: the
    lesser of ``MemAvailable`` and the cgroup's headroom (the Pi's
    ``canary.service`` runs under ``MemoryMax``). On the Pi it is always one,
    in-process: a pool of 3.3 GB workers is no thing to fork beside the
    collector (AGENTS.md: no analysis on the Pi while it collects)."""
    if requested <= 1 or tasks <= 1 or on_the_pi():
        return 1
    requested = min(requested, max(1, (os.cpu_count() or 1) // max(1, threads)))
    limits = [m for m in (_available_memory_bytes(), _cgroup_memory_headroom()) if m is not None]
    available = min(limits) if limits else None
    by_memory = (
        requested
        if available is None
        else int(available * SCORING_MEMORY_FRACTION // SCORING_WORKER_PEAK_BYTES)
    )
    return max(1, min(requested, tasks, by_memory))


def score_drives(
    tasks: Sequence[DriveTask],
    *,
    workers: int,
    threads: int,
    local: Callable[[], DriveScorer],
    spec: Callable[[], ScorerSpec],
) -> list[DriveResult]:
    """Every drive's :class:`DriveResult`, in ``tasks`` order.

    Drives are independent, so with ``workers > 1`` they are scored in
    spawned processes, largest first; results are merged back in drive order,
    so the report does not depend on the worker count. A worker killed for
    memory breaks the pool: the drives are then scored in this process, and
    whatever the workers finished is read back from the score cache.
    """
    count = scoring_workers(workers, len(tasks), threads=threads)
    if count > 1:
        log.info("scoring %d drives on %d workers (%d threads each)", len(tasks), count, threads)
        order = sorted(
            range(len(tasks)),
            key=lambda i: -sum(len(frame) for frame in tasks[i].frames.values()),
        )
        results: dict[int, DriveResult] = {}
        try:
            with ProcessPoolExecutor(
                max_workers=count,
                mp_context=multiprocessing.get_context("spawn"),
                initializer=_init_scoring_worker,
                initargs=(spec(),),
            ) as pool:
                futures = {i: pool.submit(_score_drive_task, tasks[i]) for i in order}
                results = {i: future.result() for i, future in futures.items()}
        except BrokenExecutor as exc:
            log.warning("parallel scoring aborted (%s); finishing in this process", exc)
            results = {}
        if len(results) == len(tasks):
            return [results[i] for i in range(len(tasks))]
    scorer = local()
    return [scorer.score_drive(task) for task in tasks]


def _thread_environment() -> dict[str, Any]:
    """What sets the thread counts a score was computed at: torch's pool in
    effect and the BLAS pool's defaults. Both move scores in the last bits,
    so a score computed at other counts is not served (P02.5)."""
    try:
        import torch

        torch_threads: int | None = torch.get_num_threads()
    except ImportError:
        torch_threads = None
    return {
        "torch_threads": torch_threads,
        "cpu_count": os.cpu_count(),
        **{
            name: os.environ.get(name)
            for name in ("OMP_NUM_THREADS", "OPENBLAS_NUM_THREADS", "MKL_NUM_THREADS")
        },
    }


def _data_inputs_digest(config_path: Path, profile_dir: Path, extra: Sequence[Path] = ()) -> str:
    """The data files a score can depend on besides the artifacts: the
    config, the repo catalogs, the DBCs, the profile's own files and
    ``extra`` (catalogs a config may point outside ``data/``). Hashed
    wholesale (a hand-kept list is a thing to forget to edit)."""
    files = [config_path, *sorted(DATA_DIR.glob("*.yaml")), *sorted((DATA_DIR / "dbc").glob("*"))]
    files += sorted(p for p in profile_dir.glob("*") if p.suffix in (".yaml", ".yml", ".json"))
    files += sorted(set(extra))
    return digest(*[(str(p), file_sha256(p)) for p in files if p.is_file()])


@dataclass(frozen=True)
class KeySources:
    """Every file a score-cache key is computed from (P02.5).

    :func:`key_inputs` hashes them once, before anything is loaded; scoring
    workers re-hash them before they load, and the parent re-hashes them
    after scoring and commits the run's entries only if nothing moved. So a
    score computed from bytes another session wrote mid-run can never be
    kept under a key computed from the bytes before (or the reverse).
    """

    config_path: Path
    profile_dir: Path
    extra_data: tuple[Path, ...]
    if_path: Path | None
    sequence_path: Path | None
    bindings_path: Path
    behavior_path: Path
    harness_files: tuple[Path, ...]


def _artifact_input(path: Path | None) -> str | None:
    if path is None or not path.exists():
        return None
    return digest(file_sha256(path), file_sha256(path.with_name("model.manifest.json")))


def key_inputs(sources: KeySources) -> dict[str, str | None]:
    """Each key input by name: the code, each artifact (with its manifest),
    the bindings, the behavior models and the data catalogs."""
    return {
        "code": code_version(sources.harness_files),
        "config_file": (
            file_sha256(sources.config_path) if sources.config_path.is_file() else None
        ),
        "isolation_forest": _artifact_input(sources.if_path),
        "sequence": _artifact_input(sources.sequence_path),
        "bindings": file_sha256(sources.bindings_path) if sources.bindings_path.exists() else None,
        "behavior_models": (
            file_sha256(sources.behavior_path) if sources.behavior_path.exists() else None
        ),
        "data": _data_inputs_digest(sources.config_path, sources.profile_dir, sources.extra_data),
    }


def changed_key_inputs(sources: KeySources, expected: Mapping[str, str | None]) -> list[str]:
    """The names of the key inputs whose files no longer hash as ``expected``."""
    now = key_inputs(sources)
    return [name for name, value in now.items() if expected.get(name) != value]


def shift_slice(features: pd.DataFrame, rows: int) -> pd.DataFrame:
    """``rows`` contiguous rows centred on the session's engine-running rows
    (empty when the engine never ran)."""
    running = np.flatnonzero(engine_running(features))
    if running.size == 0:
        return features.iloc[0:0]
    centre = int(running[running.size // 2])
    start = max(0, centre - rows // 2)
    return features.iloc[start : start + rows].reset_index(drop=True)


def summarize_shift(per_drive: Sequence[Mapping[str, Any]], names: Sequence[str]) -> dict[str, Any]:
    """Per detector: how many drives passed, failed, were invalid or withheld,
    and per (quantity, size) the flag rate on each drive (never pooled)."""
    out: dict[str, Any] = {}
    for name in names:
        verdicts: dict[str, int] = {}
        cells: dict[str, dict[str, list[float | None]]] = {}
        for entry in per_drive:
            report = entry["detectors"].get(name)
            if report is None:
                continue
            verdicts[report["verdict"]] = verdicts.get(report["verdict"], 0) + 1
            for outcome in [*report["outcomes"], report.get("control") or {}]:
                if not outcome:
                    continue
                cells.setdefault(outcome["quantity"], {}).setdefault(
                    f"{outcome['sigma']:g}", []
                ).append(outcome["flag_rate"])
        out[name] = {"verdicts": verdicts, "flag_rate_per_drive": cells}
    return out


# ---------------------------------------------------------------------------
# Summaries
# ---------------------------------------------------------------------------


def compact_injections(outcomes: Sequence[Mapping[str, Any]]) -> list[dict[str, Any]]:
    """One record per injection (what was changed, and where) with every
    detector's outcome inside it — not the injection repeated per detector."""
    by_injection: dict[tuple[Any, ...], dict[str, Any]] = {}
    for outcome in outcomes:
        key = (outcome["drive"], outcome["kind"], outcome["severity"], outcome["rep"])
        entry = by_injection.get(key)
        if entry is None:
            injection = outcome["injection"]
            entry = {
                "drive": outcome["drive"],
                "session": outcome["session"],
                "rep": outcome["rep"],
                **(injection.to_dict() if hasattr(injection, "to_dict") else dict(injection)),
                "outcomes": {},
            }
            by_injection[key] = entry
        entry["outcomes"][outcome["detector"]] = {
            "status": outcome["status"],
            "time_to_detect_seconds": outcome["time_to_detect_seconds"],
        }
    return list(by_injection.values())


def summarize_false_alarms(
    records: Sequence[DriveRecord],
    names: Sequence[str],
    *,
    seed: int,
    roles: Mapping[str, str] | None = None,
) -> dict[str, Any]:
    """False alarms per engine-hour per detector, overall and per stratum,
    with drive-bootstrap intervals.

    Only ``alert``-role detectors (``models.roles``, A06.5) raise alarms; an
    ``evidence`` detector's episodes are reported per drive but are not
    counted against the alert budget here.
    """
    out: dict[str, Any] = {}
    for name in names:
        if (roles or {}).get(name, "alert") != "alert":
            out[name] = {
                "role": "evidence",
                "false_alarms_per_engine_hour": {
                    "estimate": None,
                    "note": "evidence role: its episodes are not alerts",
                },
                "withheld_share": None,
                "strata": {"baro": {}, "ambient": {}},
            }
            continue
        with_time = [r for r in records if r.hours > 0 and name in r.detectors]
        overall = bootstrap_ratio_ci(
            [r.detectors[name]["false_alarm_episodes"] for r in with_time],
            [r.hours for r in with_time],
            seed=seed,
        )
        strata: dict[str, Any] = {}
        for axis, key in (("baro", "baro_band"), ("ambient", "ambient_band")):
            groups: dict[str, list[DriveRecord]] = {}
            for record in with_time:
                groups.setdefault(record.to_dict()[key], []).append(record)
            strata[axis] = {
                band: bootstrap_ratio_ci(
                    [r.detectors[name]["false_alarm_episodes"] for r in members],
                    [r.hours for r in members],
                    seed=seed,
                ).to_dict()
                for band, members in sorted(groups.items())
            }
        rows = sum(r.detectors[name]["rows"] for r in records if name in r.detectors)
        withheld = sum(r.detectors[name]["withheld_rows"] for r in records if name in r.detectors)
        out[name] = {
            "role": "alert",
            "false_alarms_per_engine_hour": overall.to_dict(),
            "drives_with_engine_time": len(with_time),
            "withheld_share": None if rows == 0 else withheld / rows,
            "strata": strata,
        }
    return out


def summarize_recall(
    outcomes: Sequence[Mapping[str, Any]], names: Sequence[str], *, seed: int
) -> dict[str, Any]:
    """Recall per (detector, kind, severity), bootstrapped over drives.

    ``withheld`` (the detector did not look) and ``masked`` (it was already
    alerting on the clean rows of that window) outcomes are excluded from the
    denominator and counted separately: neither is a miss, and neither is a
    catch.
    """
    table: dict[str, Any] = {}
    for name in names:
        per_kind: dict[str, Any] = {}
        for kind in sorted({o["kind"] for o in outcomes}):
            per_severity: dict[str, Any] = {}
            for severity in sorted(
                {o["severity"] for o in outcomes if o["kind"] == kind}, key=float
            ):
                rows = [
                    o
                    for o in outcomes
                    if o["detector"] == name and o["kind"] == kind and o["severity"] == severity
                ]
                graded = [o for o in rows if o["status"] in ("detected", "missed")]
                drives = sorted({o["drive"] for o in graded})
                estimate = bootstrap_ratio_ci(
                    [
                        sum(1 for o in graded if o["drive"] == d and o["status"] == "detected")
                        for d in drives
                    ],
                    [sum(1 for o in graded if o["drive"] == d) for d in drives],
                    seed=seed,
                )
                latencies = [
                    o["time_to_detect_seconds"] for o in graded if o["status"] == "detected"
                ]
                per_severity[str(severity)] = {
                    "recall": estimate.to_dict(),
                    "injections": len(rows),
                    "graded": len(graded),
                    "detected": sum(1 for o in graded if o["status"] == "detected"),
                    "withheld": sum(1 for o in rows if o["status"] == "withheld"),
                    "masked": sum(1 for o in rows if o["status"] == "masked"),
                    "median_time_to_detect_seconds": (
                        float(np.median(latencies)) if latencies else None
                    ),
                }
            per_kind[kind] = per_severity
        table[name] = per_kind
    return table


def summarize_vus(
    slices: Sequence[Mapping[str, Any]], names: Sequence[str], *, seed: int
) -> dict[str, Any]:
    """Threshold-free VUS-PR per (detector, fault kind): computed per drive on
    that drive's injected slices, then averaged over drives (bootstrap CI)."""
    out: dict[str, Any] = {}
    for name in names:
        per_kind: dict[str, Any] = {}
        for kind in sorted({s["kind"] for s in slices}):
            values: list[float] = []
            for drive in sorted({s["drive"] for s in slices if s["kind"] == kind}):
                picked = [
                    s
                    for s in slices
                    if s["detector"] == name and s["kind"] == kind and s["drive"] == drive
                ]
                if not picked or not any(np.isfinite(x["scores"]).any() for x in picked):
                    # A verdict-only detector (fused) has no score to rank.
                    continue
                value = vus_pr(
                    np.concatenate([s["scores"] for s in picked]),
                    np.concatenate([s["labels"] for s in picked]),
                )
                if value is not None:
                    values.append(value)
            per_kind[kind] = (
                bootstrap_ratio_ci(values, [1.0] * len(values), seed=seed).to_dict()
                if values
                else {"estimate": None, "note": "withheld: no score to rank"}
            )
        out[name] = per_kind
    # What a ranking with no information scores: the labelled share of the
    # slices. Every VUS-PR above is read against it.
    out["_base_rate"] = {
        kind: float(np.mean(np.concatenate([s["labels"] for s in slices if s["kind"] == kind])))
        for kind in sorted({s["kind"] for s in slices})
    }
    return out


def budget_thresholds_on(
    records: Sequence[DriveRecord],
    scores: Mapping[str, Mapping[str, SessionScore]],
    frames: Mapping[str, pd.DataFrame],
    names: Sequence[str],
    *,
    persistence: PersistenceSpec,
    block_seconds: float,
    budget: float,
    roles: Mapping[str, str] | None = None,
) -> dict[str, Any]:
    """A02.3: the single score threshold that would meet the alert budget on
    these drives, per detector with a score — the level a recalibration would
    choose on its evaluation sessions. In-sample to the drives it is measured
    on, so it is a target for the next calibration, never a held-out number.
    """
    out: dict[str, Any] = {}
    sessions = [name for record in records for name in record.sessions]
    for name in names:
        if (roles or {}).get(name, "alert") != "alert":
            out[name] = {"note": "evidence role: no alerts to hold to the budget"}
            continue
        traces = [
            ScoreTrace(
                scores[session][name].timestamps,
                scores[session][name].score,
                engine_running(frames[session]),
            )
            for session in sessions
            if name in scores.get(session, {})
        ]
        if not traces or not any(np.isfinite(t.scores).any() for t in traces):
            out[name] = {"note": "withheld: this detector reports no score"}
            continue
        out[name] = alert_budget_threshold(
            traces,
            persistence=persistence,
            block_seconds=block_seconds,
            budget_per_engine_hour=budget,
        ).to_dict()
    return out


def summarize_events(
    events: Sequence[LabelledEvent],
    scores: Mapping[str, Mapping[str, SessionScore]],
    names: Sequence[str],
) -> list[dict[str, Any]]:
    """How each detector behaved inside each labelled interval.

    ``fault`` events would count toward recall; ``novelty`` and
    ``intervention`` responses are reported, never counted as catches.
    """
    out: list[dict[str, Any]] = []
    for event in events:
        entry: dict[str, Any] = {
            "id": event.id,
            "kind": event.kind,
            "start": event.start.isoformat(),
            "end": event.end.isoformat(),
            "detectors": {},
        }
        covered_sessions = []
        for session, by_detector in scores.items():
            for name in names:
                result = by_detector.get(name)
                if result is None:
                    continue
                inside = _interval_mask(result.timestamps, event)
                if not inside.any():
                    continue
                if session not in covered_sessions:
                    covered_sessions.append(session)
                block = entry["detectors"].setdefault(
                    name, {"rows": 0, "scored_rows": 0, "flagged_rows": 0, "episodes_started": 0}
                )
                verdicts = [result.verdict[i] for i in np.flatnonzero(inside)]
                block["rows"] += int(inside.sum())
                block["scored_rows"] += sum(1 for v in verdicts if v is not None)
                block["flagged_rows"] += sum(1 for v in verdicts if v is True or v is np.True_)
                block["episodes_started"] += sum(
                    1
                    for episode in alert_episodes(
                        result.timestamps, result.alerting, verdicts=list(result.verdict)
                    )
                    if pd.Timestamp(event.start) <= episode.start <= pd.Timestamp(event.end)
                )
        entry["evaluated_sessions"] = sorted(covered_sessions)
        if not covered_sessions:
            entry["status"] = "skipped: no evaluated drive overlaps this interval"
        out.append(entry)
    return out


# ---------------------------------------------------------------------------
# Markdown
# ---------------------------------------------------------------------------


def _fmt(value: Any, digits: int = 2) -> str:
    if value is None:
        return "—"
    if isinstance(value, float):
        return f"{value:.{digits}f}"
    return str(value)


def _ci(block: Mapping[str, Any], digits: int = 2) -> str:
    estimate = block.get("estimate")
    if estimate is None:
        return block.get("note") or "—"
    low, high = block.get("low"), block.get("high")
    if low is None or high is None:
        return f"{estimate:.{digits}f} ({block.get('note') or 'no interval'})"
    return f"{estimate:.{digits}f} [{low:.{digits}f}, {high:.{digits}f}]"


def render_markdown(report: Mapping[str, Any]) -> str:
    names = report["detectors"]
    lines = [
        f"# Detector evaluation — {report['generated_at']}",
        "",
        "Generated by `scripts/evaluate_detectors.py` (A02.4). Held-out drives only; one "
        "row per physical drive; intervals are 95 % drive-bootstrap percentiles.",
        "",
        "## Inputs",
        "",
    ]
    for name, artifact in report["artifacts"].items():
        lines.append(f"- **{name}**: `{artifact.get('path')}` sha256 `{artifact.get('sha256')}`")
    lines += [
        f"- Persistence gate: {report['persistence']}",
        f"- Alert budget: {report['budget_per_engine_hour']} per engine-hour "
        "(alert-role detectors only)",
        f"- Detector roles (`models.roles`): {report.get('roles')}",
        f"- Sequence batched-vs-served parity: {report.get('sequence_parity')}",
        f"- Baseline fit sessions: {report.get('baseline_fit_sessions')}"
        + (
            f" (requested but not loaded: {report['baseline_fit_sessions_not_loaded']})"
            if report.get("baseline_fit_sessions_not_loaded")
            else ""
        ),
        f"- Baseline threshold sessions: {report.get('baseline_threshold_sessions')}"
        + (
            f" (requested but not loaded: {report['baseline_threshold_sessions_not_loaded']})"
            if report.get("baseline_threshold_sessions_not_loaded")
            else ""
        ),
        "",
        "## Per drive (clean rows)",
        "",
        "| drive | sessions | era | engine h | baro kPa | "
        + " | ".join(f"{n} FA/eh (ep, withheld %)" for n in names)
        + " |",
        "|" + "---|" * (5 + len(names)),
    ]
    for drive in report["drives"]:
        cells = []
        for name in names:
            block = drive["detectors"].get(name)
            if block is None:
                cells.append("—")
                continue
            share = 100 * block["withheld_rows"] / block["rows"] if block["rows"] else None
            cells.append(
                f"{_fmt(block['false_alarms_per_engine_hour'])} "
                f"({block['false_alarm_episodes']}, {_fmt(share, 0)})"
            )
        era = f"{drive['era']} (cross-era)" if drive.get("cross_era") else drive["era"]
        missing = drive.get("missing_sessions") or []
        count = f"{len(drive['sessions'])}" + (f" (+{len(missing)} not loaded)" if missing else "")
        lines.append(
            f"| {drive['drive']} | {count} | {era} | "
            f"{_fmt(drive['engine_hours'])} | {_fmt(drive['baro_kpa'], 1)} | "
            + " | ".join(cells)
            + " |"
        )
    lines += [
        "",
        "Cells: false alarms per engine-hour (false-alarm episodes, withheld % of rows). "
        "Only episodes inside a `fault` label are catches; those inside `novelty` or "
        "`intervention` labels still count as false alarms and are broken out by label "
        "kind in the JSON (`labelled_episodes`). Cross-era drives were recorded under a tune "
        f"or hardware era none of the artifacts trained in ({report.get('training_eras')}); "
        "they appear here and in no summary.",
        "",
        "## False alarms per engine-hour",
        "",
    ]
    lines.append(
        "| detector | overall | withheld share | "
        + " | ".join(f"baro {band}" for _, _, band in BARO_BANDS_KPA)
        + " |"
    )
    lines.append("|" + "---|" * (3 + len(BARO_BANDS_KPA)))
    for name in names:
        block = report["false_alarms"][name]
        strata = block["strata"]["baro"]
        lines.append(
            f"| {name} | {_ci(block['false_alarms_per_engine_hour'])} | "
            f"{_fmt(block['withheld_share'])} | "
            + " | ".join(_ci(strata.get(band, {})) for _, _, band in BARO_BANDS_KPA)
            + " |"
        )
    if report.get("recall"):
        lines += [
            "",
            "## Recall against severity (realistic faults)",
            "",
            "Each cell: recall [drive-bootstrap interval] over n graded injections — "
            "windows where the detector was quiet on the identical clean rows. "
            "*withheld*: no verdict in the window; *masked*: already alerting there on "
            "the clean rows, so a detection cannot be told from its own flood. Neither "
            "counts as a miss or a catch. The same window carries every severity of a "
            "fault, so a row compares the same rows.",
            "",
        ]
        for kind, spec in REALISTIC_FAULTS.items():
            if not any(kind in report["recall"][n] for n in names):
                continue
            lines += [f"### {kind} ({spec.severity_unit})", "", spec.description, ""]
            lines.append("| severity | " + " | ".join(names) + " |")
            lines.append("|" + "---|" * (1 + len(names)))
            for severity in spec.severities:
                cells = []
                for name in names:
                    block = report["recall"][name].get(kind, {}).get(str(severity))
                    if block is None:
                        cells.append("—")
                        continue
                    extras = [
                        f"{label} {block.get(label, 0)}"
                        for label in ("withheld", "masked")
                        if block.get(label, 0)
                    ]
                    cells.append(
                        f"{_ci(block['recall'])} n={block['graded']}"
                        + (f" ({', '.join(extras)})" if extras else "")
                    )
                lines.append(f"| {severity:g} | " + " | ".join(cells) + " |")
            lines.append("")
    if report.get("vus_pr"):
        lines += ["## VUS-PR (threshold-free, comparison only)", ""]
        kinds = sorted({k for n in names for k in report["vus_pr"].get(n, {})} - {"_base_rate"})
        lines.append("| detector | " + " | ".join(kinds) + " |")
        lines.append("|" + "---|" * (1 + len(kinds)))
        for name in names:
            lines.append(
                f"| {name} | "
                + " | ".join(_ci(report["vus_pr"][name].get(k, {})) for k in kinds)
                + " |"
            )
        base = report["vus_pr"].get("_base_rate", {})
        lines.append(
            "| *labelled share (no-skill level)* | "
            + " | ".join(_fmt(base.get(k)) for k in kinds)
            + " |"
        )
        lines.append("")
    shift = (report.get("known_answers") or {}).get("single_quantity_shift") or {}
    if shift.get("per_drive"):
        lines += [
            "## Known answer: single-quantity shift (A04.1)",
            "",
            "One quantity (every channel carrying it, raw and rolling mean) shifted by k "
            "standard deviations of its operating mode, on rows the detector scored quiet; "
            "row-level verdicts, no persistence gate. Pass: +10 sigma in any single "
            "quantity flags at least half of them. Control: every numeric column shifted "
            "by the largest size. Verdicts count drives; cells list each drive's flag rate.",
            "",
            "| detector | pass | fail | invalid | withheld |",
            "|---|---|---|---|---|",
        ]
        for name, block in shift["summary"].items():
            v = block["verdicts"]
            lines.append(
                f"| {name} | {v.get('pass', 0)} | {v.get('fail', 0)} | "
                f"{v.get('invalid', 0)} | {v.get('withheld', 0)} |"
            )
        lines.append("")
        for name, block in shift["summary"].items():
            lines.append(f"- **{name}**")
            for quantity, sizes in block["flag_rate_per_drive"].items():
                cells = "; ".join(
                    f"+{sigma}σ: " + ", ".join(_fmt(rate) for rate in rates)
                    for sigma, rates in sizes.items()
                )
                lines.append(f"  - {quantity}: {cells}")
        for name, reason in (shift.get("not_applicable") or {}).items():
            lines.append(f"- {name}: not applicable ({reason})")
        lines.append("")
    lines += ["## Labelled real events", ""]
    for event in report["events"]:
        status = event.get("status")
        header = f"- **{event['id']}** ({event['kind']}, {event['start']} – {event['end']})"
        if status:
            lines.append(f"{header}: {status}")
            continue
        parts = [
            f"{n}: {b['flagged_rows']}/{b['scored_rows']} flagged, {b['episodes_started']} episodes"
            for n, b in event["detectors"].items()
        ]
        lines.append(f"{header}: " + "; ".join(parts))
    lines += ["", "## Skipped", ""]
    for entry in report["skipped_drives"]:
        lines.append(f"- {entry['drive']} ({', '.join(entry['sessions'])}): {entry['reason']}")
    for entry in report.get("skipped_injections", []):
        lines.append(f"- injection {entry}")
    for name, reason in report.get("withheld_detectors", {}).items():
        lines.append(f"- detector {name} withheld: {reason}")
    regressions = report.get("regressions")
    if regressions:
        lines += [
            "",
            f"## Regression compare (tolerance {regressions['tolerance']:.0%})",
            "",
            f"Baseline: `{regressions.get('baseline')}`",
            "",
        ]
        lines += [f"- {line}" for line in regression_summary(regressions)] or [
            "- no regressions beyond tolerance"
        ]
    load_errors = report.get("load_errors") or {}
    if load_errors:
        lines += ["", "## Not loaded", ""]
        lines += [f"- {name}: {reason}" for name, reason in sorted(load_errors.items())]
    return "\n".join(lines) + "\n"


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------


def parse_args(argv: Sequence[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(prog="evaluate_detectors", description=__doc__.split("\n")[0])
    parser.add_argument("sessions", nargs="*", type=Path, help="Session directories.")
    parser.add_argument(
        "--sessions-root",
        action="append",
        type=Path,
        default=[],
        help="Directory of sessions to consider (repeatable).",
    )
    add_common_args(parser)
    add_profile_arg(parser)
    parser.add_argument("--if-model", type=Path, default=None)
    parser.add_argument("--sequence-model", type=Path, default=None)
    parser.add_argument("--no-sequence", action="store_true")
    parser.add_argument(
        "--baselines",
        nargs="*",
        default=["robust_z", "pca", "untrained_autoencoder", "plausibility"],
        choices=[
            "robust_z",
            "pca",
            "untrained_autoencoder",
            "plausibility",
            "robust_z_curated",
            "mahalanobis_curated",
        ],
        help="Baselines scored beside the served detectors. `plausibility` is A05's "
        "deterministic checks over arrived samples (needs the profile's plausibility.yaml). "
        "`robust_z_curated` / `mahalanobis_curated` (A04.13) read the profile's "
        "curated_outlier_channels.yaml (--curated-channels).",
    )
    parser.add_argument(
        "--curated-channels",
        type=Path,
        default=None,
        help="Curated channel list for the *_curated baselines "
        "(default: data/<profile>/curated_outlier_channels.yaml).",
    )
    parser.add_argument(
        "--baseline-fit-sessions",
        type=int,
        default=12,
        help="Fit baselines on this many of the sequence model's training sessions, "
        "spread evenly in time (default 12).",
    )
    parser.add_argument(
        "--faults",
        nargs="*",
        default=list(REALISTIC_FAULTS),
        choices=list(REALISTIC_FAULTS),
    )
    parser.add_argument("--injections-per-drive", type=int, default=2)
    parser.add_argument("--window-seconds", type=float, default=DEFAULT_WINDOW_SECONDS)
    parser.add_argument("--lead-seconds", type=float, default=DEFAULT_LEAD_SECONDS)
    parser.add_argument("--grace-seconds", type=float, default=DEFAULT_GRACE_SECONDS)
    parser.add_argument("--labels", type=Path, default=None)
    parser.add_argument("--tune-revisions", type=Path, default=None)
    parser.add_argument("--fault-roles", type=Path, default=None)
    add_bindings_arg(parser)
    parser.add_argument(
        "--cache-dir",
        type=Path,
        default=DEFAULT_CACHE_DIR,
        help="Detector score caches (default data/cache/evaluation_frames).",
    )
    parser.add_argument(
        "--no-score-cache",
        action="store_true",
        help="Score everything afresh and keep nothing (P02.5; default: detector scores "
        "are cached under --cache-dir/scores by artifact, frame, injection and code).",
    )
    parser.add_argument(
        "--derived-dir",
        type=Path,
        default=DEFAULT_DERIVED_DIR,
        help="Derived session frames, read back or built here (P01.2; default "
        "data/cache/derived_sessions).",
    )
    parser.add_argument(
        "--no-derived-store",
        dest="derived_dir",
        action="store_const",
        const=None,
        help="Reduce every session in memory and keep nothing.",
    )
    parser.add_argument(
        "--workers",
        type=int,
        default=2,
        help="Processes for session loading and for scoring drives (P02.5; the pool is "
        "capped by memory and cores). The report does not depend on it.",
    )
    parser.add_argument(
        "--scoring-threads",
        type=int,
        default=DEFAULT_SCORING_THREADS,
        help="Torch threads in every scoring process, the same whatever --workers is "
        "(the LSTM's scores move in the last bits between thread counts). 0 keeps torch's "
        "default, the harness's behaviour before P02.5 (default %(default)s).",
    )
    parser.add_argument("--seed", type=int, default=42)
    parser.add_argument("--parity-rows", type=int, default=200)
    parser.add_argument("--budget", type=float, default=V1_ALERTS_PER_HOUR_BUDGET)
    parser.add_argument(
        "--behavior-models",
        type=Path,
        default=None,
        help="Behavior-model surfaces the rows are featurized with (default: the "
        "profile-scoped daemon.behavior_models, as the trainers and the daemon use).",
    )
    parser.add_argument(
        "--baseline",
        type=Path,
        default=None,
        help="An earlier report of the same drives under the same protocol parameters "
        "(R06.5): diff this run against it and exit 3 on a regression beyond --tolerance.",
    )
    parser.add_argument(
        "--tolerance",
        type=float,
        default=DEFAULT_TOLERANCE,
        # A literal "%" in argparse help must be doubled.
        help=f"Relative regression tolerance for --baseline (default "
        f"{DEFAULT_TOLERANCE * 100:.0f}%%; see benchmark_gate.DEFAULT_TOLERANCE).",
    )
    parser.add_argument("--out", type=Path, required=True, help="JSON report path.")
    parser.add_argument("--markdown", type=Path, default=None, help="Default: --out with .md.")
    parser.add_argument(
        "--since",
        type=_utc_date,
        default=None,
        help="Only evaluate drives starting at/after this UTC date or time.",
    )
    parser.add_argument(
        "--until",
        type=_utc_date,
        default=None,
        help="Only evaluate drives starting before this UTC date or time.",
    )
    parser.add_argument(
        "--cache-only",
        action="store_true",
        help="Score only sessions whose derived frame in --derived-dir is fresh; name "
        "the rest as skipped.",
    )
    parser.add_argument(
        "--plan",
        action="store_true",
        help="Print which drives would be evaluated or skipped (and why) and which "
        "sessions would be loaded, then exit without loading anything.",
    )
    parser.add_argument(
        "--no-shift-test",
        action="store_true",
        help="Skip the A04.1 known answer (a single quantity shifted by 3-100 sigma).",
    )
    parser.add_argument(
        "--shift-rows",
        type=int,
        default=900,
        help="Rows per drive the single-quantity shift is scored on (default 900).",
    )
    add_log_level_arg(parser, default="WARNING")
    return parser.parse_args(argv)


def _utc_date(value: str) -> datetime:
    parsed = datetime.fromisoformat(value)
    return parsed if parsed.tzinfo is not None else parsed.replace(tzinfo=UTC)


def _profile_data_dir(config: CanaryConfig, profile: str | None) -> Path:
    """``data/<slug>/`` — the vehicle's labels, tune registry and fault roles."""
    from src.vehicle.profiles import resolve_profile_for_tooling

    return DATA_DIR / resolve_profile_for_tooling(config, profile).slug


def _spread_in_time(names: Sequence[str], count: int) -> list[str]:
    # session_sort_key is total: the old (session_started_at(n), n) key raised
    # TypeError comparing None with a datetime (R01.1).
    ordered = sorted(names, key=session_sort_key)
    if count >= len(ordered):
        return ordered
    picks = np.linspace(0, len(ordered) - 1, count).round().astype(int)
    return [ordered[i] for i in sorted(set(picks.tolist()))]


def _behavior_record(
    path: Path,
    behavior: BehaviorModels | None,
    trained_if: TrainedIsolationForest | None,
    if_path: Path,
) -> dict[str, Any]:
    """Which behavior models the rows were featurized with, and whether each
    one the IF consumes is the artifact it was trained against (B03.4)."""
    provenance: dict[str, dict[str, Any]] = {}
    if trained_if is not None:
        try:
            manifest = json.loads(if_path.with_name("model.manifest.json").read_text())
        except (OSError, json.JSONDecodeError):
            manifest = {}
        provenance = behavior_fingerprint_provenance(
            trained_if.feature_columns, manifest_behavior_fingerprints(manifest), behavior
        )
    return {
        "path": str(path),
        "loaded": behavior is not None,
        "sha256": file_sha256(path) if behavior is not None else None,
        "provenance": provenance,
    }


def _harness_files() -> tuple[Path, ...]:
    here = Path(__file__).resolve()
    return (here, here.with_name("_cli.py"))


def main(argv: Sequence[str] | None = None) -> int:
    args = parse_args(argv)
    # P02.5: the code and the config file are hashed first, before the config
    # is parsed or any module does work, so the key they give is what this
    # run executes; ``changed_key_inputs`` re-checks both later.
    early = {
        "code": code_version(_harness_files()),
        "config_file": file_sha256(Path(args.config)) if Path(args.config).is_file() else None,
    }
    configure_logging(args.log_level)
    # Before any torch work: the parity check and the untrained autoencoder's
    # calibration run here, and must match what the scoring workers compute.
    restore = pin_threads(args.scoring_threads)
    try:
        with ExitStack() as cleanup:
            return _evaluate(args, cleanup, early)
    finally:
        restore()


def _evaluate(args: argparse.Namespace, cleanup: ExitStack, early: Mapping[str, str | None]) -> int:
    started = time.time()
    logging.getLogger("src.preprocessing.feature_pipeline").setLevel(logging.WARNING)
    # Scoped once, here (P02.11): featurization, the plausibility limits and
    # the tune registry all read the vehicle's data directory. This harness
    # used to featurize through the unscoped config — without the vehicle's
    # afr_calibration.yaml the daemon applies — while hashing that absent file.
    config = _scoped(load_config(args), args.profile)
    models_dir = scoped_models_dir(config, args.profile)
    data_dir = _profile_data_dir(config, args.profile)
    persistence = PersistenceSpec.from_config(config)
    if_path = args.if_model or models_dir / "isolation_forest" / "model.joblib"
    seq_path = args.sequence_model or models_dir / "lstm_autoencoder" / "model.pt"
    behavior_path = args.behavior_models or config.daemon.behavior_models
    # P02.5: every score-cache key input is hashed here, before anything is
    # loaded; the run's entries are committed only if they still hash so
    # after scoring (``changed_key_inputs`` below).
    key_sources = KeySources(
        config_path=Path(args.config).resolve(),
        profile_dir=data_dir,
        # The AFR calibration featurization reads and the limits the
        # plausibility detector reads, both from the scoped config: the files
        # the run actually loads.
        extra_data=(
            config.daemon.afr_calibration.resolve(),
            plausibility_path(config).resolve(),
        ),
        if_path=if_path,
        sequence_path=None if args.no_sequence else seq_path,
        bindings_path=args.bindings,
        behavior_path=behavior_path,
        harness_files=_harness_files(),
    )
    # The earliest hashes win: a change after them is caught by the re-checks.
    inputs = {**key_inputs(key_sources), **early}
    bindings = load_bindings(args.bindings) if args.bindings.exists() else None

    trained_if: TrainedIsolationForest | None = (
        load_isolation_forest_model(if_path) if if_path.exists() else None
    )
    trained_seq: TrainedSequenceModel | None = None
    if not args.no_sequence and seq_path.exists():
        # The backend the artifact's manifest names (LSTM or TCN), not a
        # hard-coded LSTM loader (R06.5: retrain's gate scores either).
        trained_seq = load_sequence_artifact(seq_path)
    artifacts: dict[str, Any] = {}
    seen: dict[str, dict[str, str]] = {}
    for label, path, manifest in (
        ("isolation_forest", if_path, trained_if.manifest if trained_if else None),
        ("sequence", seq_path, trained_seq.manifest if trained_seq else None),
    ):
        if manifest is None:
            continue
        artifacts[label] = {
            "path": str(path),
            "sha256": file_sha256(path),
            "manifest_sha256": file_sha256(path.with_name("model.manifest.json")),
            "model_version": manifest.model_version,
        }
        record_seen(seen, label, manifest)

    roots = list(args.sessions_root)
    recorded = recorded_sessions(roots, kinds=EVALUATION_SESSION_KINDS)
    session_dirs = list(args.sessions) or recorded
    by_name = {path.name: path for path in recorded}
    by_name.update({path.name: path for path in args.sessions})

    # R05.1/R05.17: the profile's flash history, tracked and operational
    # registries merged; an explicit --tune-revisions file is read as given.
    if args.tune_revisions:
        tune_registry = load_tune_revisions(args.tune_revisions)
        tune_sources: tuple[Path, ...] = (args.tune_revisions,)
    else:
        tune_registry = active_registry(config)
        tune_sources = active_registry_sources(config)
    labels_path = args.labels or data_dir / LABELLED_EVENTS_FILENAME
    registry = load_labelled_events(
        labels_path, tune_registry=tune_registry, tune_sources=tune_sources
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
    drives = [
        d
        for d in drives
        if (args.since is None or d.start >= args.since)
        and (args.until is None or d.start < args.until)
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
        if all(name in excluded for name in drive.session_names):
            reason = (
                "skipped: no default-profile rows ("
                + "; ".join(f"{name}: {excluded[name]}" for name in drive.session_names)
                + ")"
            )
        elif seen_by:
            reason = "skipped: seen by " + "; ".join(
                f"{session} ({', '.join(f'{d} {r}' for d, r in roles.items())})"
                for session, roles in sorted(seen_by.items())
            )
        elif drive.straddles:
            reason = f"skipped: {drive.straddles}"
        else:
            evaluation.append(drive)
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
    reference = trained_seq if trained_seq is not None else trained_if
    if reference is not None and args.baselines:
        fit_names = _spread_in_time(
            [n for n in reference.manifest.training_sessions if n in by_name],
            args.baseline_fit_sessions,
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
    if args.plan:
        for drive in evaluation:
            print(
                f"evaluate {drive.drive_id} [{drive.era.label if drive.era else '?'}]: "
                + ", ".join(
                    f"{n} (not scored: {excluded[n]})" if n in excluded else n
                    for n in drive.session_names
                )
            )
        for entry in skipped:
            print(f"skip     {entry['drive']}: {entry['reason']}")
        print(f"baseline fit: {', '.join(fit_names) or 'none'}")
        print(f"baseline thresholds: {', '.join(calibration_names) or 'none'}")
        for name in needed:
            print(f"load {by_name[name]}")
        return 0
    behavior = load_behavior_models(behavior_path) if behavior_path.exists() else None
    behavior_record = _behavior_record(behavior_path, behavior, trained_if, if_path)
    absent = {
        name: entry["reason"]
        for name, entry in behavior_record["provenance"].items()
        if entry["status"] == BEHAVIOR_PROVENANCE_ABSENT
    }
    if absent:
        # The IF would be scored with those columns empty: not the detector
        # the daemon serves. Refuse rather than report its numbers.
        for reason in absent.values():
            print(f"error: {reason} ({behavior_path})", file=sys.stderr)
        return 2
    frames, load_errors = load_resampled_sessions(
        [by_name[n] for n in needed],
        config,
        derived_dir=args.derived_dir,
        workers=args.workers,
        cache_only=args.cache_only,
    )
    # "An empty result must say which kind of empty it is": zero drives scored
    # because every load failed is not an evaluation, so refuse and name each
    # failure rather than report nothing with exit 0 (P02.5: a Pi retrain once
    # passed a derived store the Pi refuses, and its gate scored nothing).
    evaluated = [n for d in evaluation for n in d.session_names if n not in excluded]
    if (needed and not frames) or (evaluated and not any(n in frames for n in evaluated)):
        failed = {n: load_errors[n] for n in needed if n in load_errors}
        if failed:
            print(
                f"error: no evaluation drive loaded; {len(failed)} session load(s) failed:",
                file=sys.stderr,
            )
            for name, error in failed.items():
                print(f"  {name}: {error}", file=sys.stderr)
            return 2
    # P02.5: every score is cached under its detector's identity and its
    # unit's key; the unit key starts from the session's frame by content.
    code = str(inputs["code"])
    cache = ScoreCache(None if args.no_score_cache else args.cache_dir / "scores")
    # Staged entries of a run that fails or is interrupted are dropped.
    cleanup.callback(cache.discard)
    frame_keys = {name: frame_key(frame) for name, frame in frames.items()}
    feature_key = digest(
        "features",
        code,
        _thread_environment(),
        config.model_dump(mode="json"),
        inputs["behavior_models"],
        inputs["bindings"],
        inputs["data"],
    )

    # The DID catalog load_resampled_session decoded every frame with (P02.14).
    decoded_with = default_decode_inputs().did_map

    def featurize(name: str) -> pd.DataFrame:
        return featurize_wide_frame(
            frames[name],
            config,
            bindings=bindings,
            behavior_models=behavior,
            did_map=decoded_with,
        ).features

    stack = DeployedStack(
        config,
        trained_if,
        trained_seq,
        suppress_channels=suppressed_for(trained_seq, config) if trained_seq else (),
    )
    identities: dict[str, str] = {}
    if trained_if is not None:
        identities["isolation_forest"] = str(inputs["isolation_forest"])
    if trained_seq is not None:
        # The serve-time suppress list comes from the config, which is in
        # every unit key (``feature_key``); the batch size is code.
        identities["sequence"] = str(inputs["sequence"])
    detectors: list[Detector] = [stack]
    baseline_thresholds: dict[str, Any] = {}
    baseline_specs: list[tuple[ScoreModel | None, float]] = []
    fit_present = [n for n in fit_names if n in frames]
    calibration_present = [n for n in calibration_names if n in frames]
    if reference is not None and args.baselines and fit_present and calibration_present:
        # Baselines see the served models' own raw channels (rolling siblings
        # dropped) — the same rows, the same inputs.
        channels = raw_channels(reference.feature_columns)
        curated: tuple[str, ...] = ()
        if {"robust_z_curated", "mahalanobis_curated"} & set(args.baselines):
            curated = load_curated_channels(
                args.curated_channels or data_dir / "curated_outlier_channels.yaml"
            )
        # Independent blocks of one sequence window, or of the rolling window
        # when no sequence model is scored.
        block_seconds = float(
            trained_seq.manifest.schema["sequence_length"]
            if trained_seq is not None
            else config.preprocessing.rolling_window_seconds
        )
        fitted_unit = digest(
            feature_key,
            "baselines",
            [frame_keys[n] for n in fit_present],
            [frame_keys[n] for n in calibration_present],
            channels,
            curated,
            sorted(args.baselines),
            identities.get("sequence"),
            args.seed,
            args.budget,
            block_seconds,
            persistence,
        )
        fitted: list[tuple[str, ScoreModel | None, dict[str, Any]]] | None = cache.get(
            "_baselines", fitted_unit
        )
        if fitted is None:
            fit_frames = [featurize(n) for n in fit_present]
            calibration_frames = [featurize(n) for n in calibration_present]
            models: list[ScoreModel] = []
            if "robust_z" in args.baselines:
                models.append(RobustZBaseline.fit(fit_frames, channels))
            if "pca" in args.baselines:
                models.append(PCABaseline.fit(fit_frames, channels))
            if "robust_z_curated" in args.baselines:
                models.append(
                    replace(RobustZBaseline.fit(fit_frames, curated), name="robust_z_curated")
                )
            if "mahalanobis_curated" in args.baselines:
                models.append(MahalanobisBaseline.fit(fit_frames, curated))
            if "untrained_autoencoder" in args.baselines and trained_seq is not None:
                models.append(
                    UntrainedAutoencoder.like(
                        trained_seq,
                        seed=args.seed,
                        suppress_channels=suppressed_for(trained_seq, config),
                    )
                )
            fitted = []
            for model in models:
                traces = [
                    ScoreTrace(
                        pd.to_datetime(frame[TIMESTAMP_COLUMN], utc=True).reset_index(drop=True),
                        model.scores(frame),
                        engine_running(frame),
                    )
                    for frame in calibration_frames
                ]
                budget = alert_budget_threshold(
                    traces,
                    persistence=persistence,
                    block_seconds=block_seconds,
                    budget_per_engine_hour=args.budget,
                )
                kept = None if isinstance(model, UntrainedAutoencoder) else model
                fitted.append((model.name, kept, budget.to_dict()))
            del fit_frames, calibration_frames
            cache.put("_baselines", fitted_unit, fitted)
        for name, kept, budget_dict in fitted:
            baseline_thresholds[name] = budget_dict
            threshold = budget_dict["threshold"]
            if threshold is None:
                continue
            model_now: ScoreModel
            if kept is None:
                assert trained_seq is not None
                model_now = UntrainedAutoencoder.like(
                    trained_seq,
                    seed=args.seed,
                    suppress_channels=suppressed_for(trained_seq, config),
                )
            else:
                model_now = kept
            detectors.append(ThresholdedScore(model_now, threshold))
            baseline_specs.append((kept, threshold))
            identities[name] = score_model_identity(
                model_now, threshold, sequence=identities.get("sequence"), seed=args.seed
            )
    withheld_detectors = dict(stack.withheld)
    raw_detectors: list[RawStreamDetector] = []
    if "plausibility" in args.baselines:
        try:
            raw_detectors.append(
                PlausibilityDetector(
                    config,
                    bindings=bindings,
                    did_map=(
                        load_did_map(DEFAULT_DIDS_PATH) if DEFAULT_DIDS_PATH.exists() else None
                    ),
                )
            )
        except FileNotFoundError as exc:
            withheld_detectors["plausibility"] = f"withheld: {exc}"
    for raw_detector in raw_detectors:
        catalog = getattr(raw_detector, "catalog", None)
        identities[raw_detector.names[0]] = catalog.fingerprint() if catalog is not None else ""
    names = [n for d in detectors for n in d.names] + [n for r in raw_detectors for n in r.names]
    streams = load_raw_streams(
        raw_detectors,
        [by_name[n] for d in evaluation for n in d.session_names if n in frames],
        config,
        cache_dir=args.cache_dir,
    )

    parity: dict[str, Any] | None = None
    if stack.sequence is not None and evaluation:
        first = next((n for d in evaluation for n in d.session_names if n in frames), None)
        if first is not None:
            parity_unit = digest(
                feature_key, "parity", frame_keys[first], identities["sequence"], args.parity_rows
            )
            parity = cache.get("_parity", parity_unit)
            if parity is None:
                parity = sequence_parity(
                    stack.sequence, featurize(first), rows=args.parity_rows
                ).to_dict()
                cache.put("_parity", parity_unit, parity)
            parity = {**parity, "session": first}

    sweep = SweepSpec(
        kinds=tuple(args.faults) if args.injections_per_drive > 0 else (),
        reps=args.injections_per_drive,
        seed=args.seed,
        lead_seconds=args.lead_seconds,
        window_seconds=args.window_seconds,
        grace_seconds=args.grace_seconds,
        shift_rows=args.shift_rows,
    )
    roles = load_fault_channel_roles(args.fault_roles or data_dir / "fault_injection.yaml")
    tasks: list[DriveTask] = []
    for drive in evaluation:
        present = {n: frames[n] for n in drive.session_names if n in frames and n not in excluded}
        if not present:
            continue
        cross_era = bool(training_eras) and drive.era not in training_eras
        tasks.append(
            DriveTask(
                drive,
                present,
                {n: frame_keys[n] for n in present},
                shift=not args.no_shift_test and not cross_era,
                sweep=not cross_era and bool(args.faults) and args.injections_per_drive > 0,
            )
        )

    def local_scorer() -> DriveScorer:
        return DriveScorer(
            detectors=detectors,
            raw_detectors=raw_detectors,
            identities=identities,
            stream_paths=streams.paths,
            cache=cache,
            code=code,
            feature_key=feature_key,
            config=config,
            bindings=bindings,
            did_map=decoded_with,
            behavior=behavior,
            persistence=persistence,
            roles=roles,
            sweep=sweep,
        )

    def worker_spec() -> ScorerSpec:
        return ScorerSpec(
            config=config,
            if_path=if_path if trained_if is not None else None,
            sequence_path=seq_path if trained_seq is not None else None,
            baselines=baseline_specs,
            untrained_seed=args.seed,
            plausibility=bool(raw_detectors),
            bindings_path=args.bindings,
            behavior_path=behavior_path if behavior is not None else None,
            identities=identities,
            stream_paths=streams.paths,
            cache_root=cache.root,
            cache_run=cache.run,
            key_sources=key_sources,
            key_inputs=inputs,
            code=code,
            feature_key=feature_key,
            persistence=persistence,
            roles={role: list(channels) for role, channels in roles.items()},
            sweep=sweep,
            threads=args.scoring_threads,
        )

    # The parent's own lookups (baselines, parity); each drive adds its own.
    cache_counts = cache.counts()
    results = iter(
        score_drives(
            tasks,
            workers=args.workers,
            threads=args.scoring_threads,
            local=local_scorer,
            spec=worker_spec,
        )
    )

    scores: dict[str, dict[str, SessionScore]] = {}
    records: list[DriveRecord] = []
    outcomes: list[dict[str, Any]] = []
    skipped_injections: list[dict[str, Any]] = []
    slices: list[dict[str, Any]] = []
    shift_runs: list[dict[str, Any]] = []
    stream_reads: dict[str, int] = {}
    worker_peak = 0
    for drive in evaluation:
        present = {n: frames[n] for n in drive.session_names if n in frames and n not in excluded}
        if not present:
            skipped.append(
                {
                    "drive": drive.drive_id,
                    "sessions": list(drive.session_names),
                    "reason": "skipped: "
                    + "; ".join(
                        f"{n}: {load_errors.get(n, 'not loaded')}"
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
        record = clean_drive_record(drive, present, scores, names, registry.events)
        record.excluded_sessions = {n: excluded[n] for n in drive.session_names if n in excluded}
        record.cross_era = bool(training_eras) and drive.era not in training_eras
        records.append(record)
        if result.shift is not None:
            shift_runs.append(result.shift)
        if record.cross_era:
            skipped_injections.append(
                {
                    "drive": drive.drive_id,
                    "reason": f"cross-era ({drive.era.label if drive.era else '?'}; "
                    f"artifacts trained in {sorted(e.label for e in training_eras)})",
                }
            )
            continue
        outcomes.extend(result.outcomes)
        skipped_injections.extend(result.skipped)
        slices.extend(result.slices)
    log.info(
        "stream reads per session %s; peak RSS of a scoring process %.0f MB",
        stream_reads,
        worker_peak / 1e6,
    )
    hits = sum(c["hits"] for c in cache_counts.values())
    lookups = hits + sum(c["misses"] for c in cache_counts.values())
    log.info("score cache by component: %s", cache_counts)
    if cache.root is not None:
        changed = changed_key_inputs(key_sources, inputs)
        if changed:
            # Some score may have come from the new bytes: keep none of them.
            cache.discard()
            log.warning(
                "score cache: nothing kept; key inputs changed during the run: %s",
                ", ".join(changed),
            )
            print(
                "warning: score cache not updated: key inputs changed during the run: "
                + ", ".join(changed),
                file=sys.stderr,
            )
        else:
            cache.commit()
            cache.prune(keep_since=started)

    role_of = detector_roles(config, names)
    report: dict[str, Any] = {
        "generated_at": datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "command": " ".join(sys.argv),
        "config": str(args.config),
        # R06.5: what makes two reports comparable (benchmark_gate): the
        # protocol, and every parameter that decides which windows are
        # injected, with what, and when an alert counts.
        "protocol": {
            "name": PROTOCOL_NAME,
            "version": PROTOCOL_VERSION,
            "seed": args.seed,
            "faults": sorted(args.faults),
            "injections_per_drive": args.injections_per_drive,
            "window_seconds": args.window_seconds,
            "lead_seconds": args.lead_seconds,
            "grace_seconds": args.grace_seconds,
            "persistence": {
                "required": persistence.required,
                "window": persistence.window,
                "window_seconds": persistence.window_seconds,
            },
        },
        "behavior_models": behavior_record,
        "artifacts": artifacts,
        "persistence": {
            "required": persistence.required,
            "window": persistence.window,
            "window_seconds": persistence.window_seconds,
        },
        "budget_per_engine_hour": args.budget,
        "detectors": names,
        "withheld_detectors": withheld_detectors,
        # What the baselines actually rest on: a requested session that did
        # not load is named under ``baseline_*_not_loaded``, never counted.
        "baseline_fit_sessions": [n for n in fit_names if n in frames],
        "baseline_threshold_sessions": [n for n in calibration_names if n in frames],
        "baseline_fit_sessions_not_loaded": [n for n in fit_names if n not in frames],
        "baseline_threshold_sessions_not_loaded": [n for n in calibration_names if n not in frames],
        "baseline_thresholds": baseline_thresholds,
        "sequence_parity": parity,
        "drives": [r.to_dict() for r in records],
        "skipped_drives": skipped,
        "training_eras": sorted(e.label for e in training_eras),
        "roles": role_of,
        "false_alarms": summarize_false_alarms(
            [r for r in records if not r.cross_era], names, seed=args.seed, roles=role_of
        ),
        "recall": summarize_recall(outcomes, names, seed=args.seed) if outcomes else {},
        "vus_pr": summarize_vus(slices, names, seed=args.seed) if slices else {},
        "budget_threshold_on_evaluation": budget_thresholds_on(
            [r for r in records if not r.cross_era],
            scores,
            frames,
            names,
            persistence=persistence,
            block_seconds=float(
                trained_seq.manifest.schema["sequence_length"]
                if trained_seq is not None
                else config.preprocessing.rolling_window_seconds
            ),
            budget=args.budget,
            roles=role_of,
        ),
        "events": summarize_events(registry.events, scores, names),
        "fault_events": len(registry.of_kind("fault")),
        "known_answers": {
            "single_quantity_shift": {
                "quantities": {role: list(channels) for role, channels in roles.items()},
                "not_applicable": {
                    r.names[0]: "reads arrived samples, not the featurized rows"
                    for r in raw_detectors
                },
                "per_drive": shift_runs,
                "summary": summarize_shift(shift_runs, [n for d in detectors for n in d.names]),
            }
        },
        "injections": compact_injections(outcomes),
        "skipped_injections": skipped_injections,
        "load_errors": load_errors,
    }
    regression: dict[str, Any] | None = None
    if args.baseline is not None:
        try:
            baseline_report = json.loads(args.baseline.read_text())
        except (OSError, json.JSONDecodeError) as exc:
            print(f"error: cannot read baseline report {args.baseline}: {exc}", file=sys.stderr)
            return 2
        # Round-trip through JSON first: the diff reads the report as written.
        regression = diff_reports(
            baseline_report,
            json.loads(json.dumps(report, default=str)),
            tolerance=args.tolerance,
        )
        report["regressions"] = {**regression, "baseline": str(args.baseline)}
    args.out.parent.mkdir(parents=True, exist_ok=True)
    args.out.write_text(json.dumps(report, indent=2, default=str) + "\n")
    markdown = args.markdown or args.out.with_suffix(".md")
    markdown.write_text(render_markdown(report))
    print(f"wrote {args.out} and {markdown}")
    print(
        f"{len(records)} held-out drive(s) scored, {len(skipped)} skipped; "
        f"{len(outcomes)} injection outcome(s)"
    )
    print(
        f"score cache: {hits} of {lookups} lookups read back"
        + ("" if cache.root is not None else " (--no-score-cache)")
    )
    if regression is not None:
        lines = regression_summary(regression)
        print(f"regression compare vs {args.baseline} (tolerance {args.tolerance:.0%}):")
        for line in lines or ["no regressions beyond tolerance"]:
            print(f"  {line}")
        if regression["regressed"]:
            print("error: regression(s) detected vs baseline", file=sys.stderr)
            return 3
    return 0


if __name__ == "__main__":
    sys.exit(main())
