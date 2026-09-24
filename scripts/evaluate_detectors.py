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
:mod:`src.models.synthetic_fault_eval`. This script loads sessions (with a
resampled-frame cache under ``--cache-dir``), decides which drives are held
out for *every* compared detector, scores clean and injected rows, and writes
a JSON report and a Markdown table with one row per drive.

A drive is *skipped* (with the reason) when a detector trained or calibrated
on any of its sessions, when it straddles a tune or hardware boundary, or
when it has no telemetry. Captures are scored: modes come from the same
segmenter the daemon runs (``featurize_wide_frame``), never from sidecars.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import logging
import sys
from collections.abc import Mapping, Sequence
from concurrent.futures import BrokenExecutor, ProcessPoolExecutor
from dataclasses import dataclass, field, replace
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import numpy as np
import pandas as pd

try:
    from _cli import add_common_args, add_profile_arg, load_config, scoped_models_dir
    from detector_benchmark import load_resampled_session
except ImportError:  # loaded as `scripts.evaluate_detectors`
    from scripts._cli import (  # type: ignore[no-redef]
        add_common_args,
        add_profile_arg,
        load_config,
        scoped_models_dir,
    )
    from scripts.detector_benchmark import load_resampled_session  # type: ignore[no-redef]

from src.analysis.drives import parent_session
from src.analysis.labelled_events import (
    LABELLED_EVENTS_FILENAME,
    LabelledEvent,
    load_labelled_events,
)
from src.analysis.tune_revisions import load_tune_revisions
from src.config import CanaryConfig
from src.control.sessions import session_started_at
from src.decode.bindings import Bindings, load_bindings
from src.decode.dids import load_did_map
from src.log import configure_logging
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
    sequence_parity,
    suppressed_for,
)
from src.models.false_positive_benchmark import V1_ALERTS_PER_HOUR_BUDGET
from src.models.isolation_forest import TrainedIsolationForest, load_isolation_forest_model
from src.models.sequence_protocol import TrainedSequenceModel
from src.models.synthetic_fault_eval import (
    REALISTIC_FAULT_ROLES,
    REALISTIC_FAULTS,
    choose_fault_window,
    inject_realistic_fault,
    load_fault_channel_roles,
)
from src.preprocessing.feature_cache import build_feature_frame_cache_key
from src.preprocessing.feature_pipeline import (
    DEFAULT_BINDINGS_PATH,
    DEFAULT_DIDS_PATH,
    featurize_wide_frame,
)
from src.preprocessing.resampler import TIMESTAMP_COLUMN
from src.preprocessing.segmenter import MODE_COLUMN

log = logging.getLogger(__name__)

#: Resampled frames are gitignored, regenerable, and keyed on every input.
DEFAULT_CACHE_DIR = Path("data/cache/evaluation_frames")
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


def discover_session_dirs(roots: Sequence[Path]) -> list[Path]:
    """Every recorded session under ``roots``, captures included.

    ``_archive`` (quarantine) and ``_replay-*`` (re-emitted copies) are not
    drives. Ordered by parsed start time, never by name.
    """
    found: dict[str, Path] = {}
    for root in roots:
        if not root.is_dir():
            continue
        for child in root.iterdir():
            if not child.is_dir() or child.name.startswith(("_archive", "_replay")):
                continue
            if session_started_at(child) is None:
                continue
            found.setdefault(child.name, child)
    return sorted(found.values(), key=lambda p: (session_started_at(p), p.name))


def _load_one(args: tuple[Path, Path, str]) -> tuple[str, pd.DataFrame | None, str | None]:
    session, cache_path, config_path = args
    try:
        if cache_path.exists():
            return session.name, pd.read_pickle(cache_path), None
        config = CanaryConfig.from_yaml(Path(config_path))
        frame = load_resampled_session(session, config)
        cache_path.parent.mkdir(parents=True, exist_ok=True)
        frame.to_pickle(cache_path)
        return session.name, frame, None
    except Exception as exc:  # noqa: BLE001 - one bad session is a skip, not a crash
        return session.name, None, f"{type(exc).__name__}: {exc}"


def load_resampled_sessions(
    sessions: Sequence[Path],
    config: CanaryConfig,
    config_path: Path,
    *,
    cache_dir: Path,
    workers: int,
    cache_only: bool = False,
) -> tuple[dict[str, pd.DataFrame], dict[str, str]]:
    """Resampled frames by session name (the training decode chain), cached.

    The cache key is the feature-frame key of that one session — session file
    stats, preprocessing config, bindings, DBC, DID map and the cache version
    — so a stale entry cannot be served. ``cache_only`` skips (and names)
    every session not decoded yet instead of decoding it.
    """
    jobs = [
        (
            session,
            cache_dir / f"{build_feature_frame_cache_key([session], config)}-resampled.pkl",
            str(config_path),
        )
        for session in sessions
    ]
    frames: dict[str, pd.DataFrame] = {}
    errors: dict[str, str] = {}
    if cache_only:
        for session, cache_path, _ in jobs:
            if not cache_path.exists():
                errors[session.name] = "not decoded yet (--cache-only)"
        jobs = [job for job in jobs if job[1].exists()]
    results: list[tuple[str, pd.DataFrame | None, str | None]] = []
    if workers > 1 and len(jobs) > 1:
        try:
            with ProcessPoolExecutor(max_workers=workers) as pool:
                results = list(pool.map(_load_one, jobs))
        except BrokenExecutor as exc:
            # A dense drive's decode can take several GB; a worker killed for
            # memory breaks the pool. Finished sessions are already cached, so
            # the serial pass below only decodes what is left.
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
    """The profile-scoped config (plausibility limits live in the profile's
    data directory)."""
    from src.profiles import resolve_profile_for_tooling, scope_config

    if not config.profiles.enabled:
        return config
    return scope_config(config, resolve_profile_for_tooling(config, profile))


def load_raw_streams(
    detectors: Sequence[RawStreamDetector],
    sessions: Sequence[Path],
    config: CanaryConfig,
    *,
    cache_dir: Path,
) -> dict[tuple[str, str], Any]:
    """Each arrived-sample detector's stream per session, cached as parquet.

    The key is the session's feature-frame cache key (session file stats,
    bindings, DBC, DID map, preprocessing) plus the detector's own catalog
    fingerprint, so an edited limit or binding cannot be served a stale stream.
    """
    from src.plausibility.offline import SessionStream

    streams: dict[tuple[str, str], Any] = {}
    for detector in detectors:
        # Every resolved limit, policy and source is in the catalog's repr.
        fingerprint = hashlib.sha256(repr(getattr(detector, "catalog", None)).encode()).hexdigest()[
            :12
        ]
        for session in sessions:
            key = build_feature_frame_cache_key([session], config)
            path = cache_dir / detector.names[0] / f"{key}-{fingerprint}.parquet"
            if path.exists():
                frame = pd.read_parquet(path)
                modes = frame.pop("mode").to_numpy(dtype=object)
                modes[modes == "None"] = None
                streams[(detector.names[0], session.name)] = SessionStream(
                    session.name, frame.reset_index(drop=True), modes
                )
                continue
            stream = detector.load(session)
            path.parent.mkdir(parents=True, exist_ok=True)
            stream.samples.assign(mode=stream.modes.astype(str)).to_parquet(path)
            streams[(detector.names[0], session.name)] = stream
    return streams


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


def file_sha256(path: Path) -> str | None:
    if not path.exists():
        return None
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1 << 20), b""):
            digest.update(chunk)
    return digest.hexdigest()


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


def score_session(
    detectors: Sequence[Detector],
    features: pd.DataFrame,
    persistence: PersistenceSpec,
    *,
    raw: Sequence[tuple[RawStreamDetector, Any]] = (),
) -> dict[str, SessionScore]:
    """Every detector on one session's rows. ``raw`` pairs each arrived-sample
    detector with its stream for the same rows; its verdicts land on this
    frame's row grid, so every detector is graded on the same rows."""
    timestamps = pd.to_datetime(features[TIMESTAMP_COLUMN], utc=True).reset_index(drop=True)
    outputs: dict[str, DetectorOutput] = {}
    for detector in detectors:
        outputs.update(detector.score(features))
    for raw_detector, stream in raw:
        outputs.update(raw_detector.score_stream(stream, timestamps))
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

    def to_dict(self) -> dict[str, Any]:
        return {
            "drive": self.drive.drive_id,
            "cross_era": self.cross_era,
            "sessions": self.sessions,
            # Sessions of this drive that could not be loaded: the drive was
            # scored on the rest, and says so.
            "missing_sessions": sorted(set(self.drive.session_names) - set(self.sessions)),
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


def injection_sweep(
    drive: Drive,
    session_frame: pd.DataFrame,
    session_name: str,
    *,
    detectors: Sequence[Detector],
    config: CanaryConfig,
    bindings: Bindings | None,
    roles: Mapping[str, Sequence[str]],
    kinds: Sequence[str],
    reps: int,
    seed: int,
    persistence: PersistenceSpec,
    lead_seconds: float,
    window_seconds: float,
    grace_seconds: float,
    raw: Sequence[tuple[RawStreamDetector, Any]] = (),
) -> tuple[list[dict[str, Any]], list[dict[str, Any]], list[dict[str, Any]]]:
    """Inject every (kind, severity) into the same windows of one session.

    One window per (kind, rep) — shared across severities so recall against
    severity compares the same rows. Each window's clean slice is scored once;
    a detection is an alert the fault caused on those rows. Returns
    ``(outcomes, skipped, slices)``; ``slices`` carry scores and labels for
    the threshold-free VUS-PR.
    """
    outcomes: list[dict[str, Any]] = []
    skipped: list[dict[str, Any]] = []
    slices: list[dict[str, Any]] = []
    stamps = pd.to_datetime(session_frame[TIMESTAMP_COLUMN], utc=True)
    for kind in kinds:
        for rep in range(reps):
            rng = np.random.default_rng(_seed_for(seed, drive.drive_id, kind, rep))
            window = choose_fault_window(
                session_frame,
                kind=kind,
                length_seconds=window_seconds,
                lead_seconds=lead_seconds,
                rng=rng,
            )
            if isinstance(window, str):
                skipped.append(
                    {"drive": drive.drive_id, "kind": kind, "rep": rep, "reason": window}
                )
                continue
            start, end = window
            lo = start - pd.Timedelta(seconds=lead_seconds)
            hi = end + pd.Timedelta(seconds=grace_seconds + window_seconds / 4)
            piece = session_frame.loc[((stamps >= lo) & (stamps < hi)).to_numpy()].reset_index(
                drop=True
            )
            fault_channels = [
                channel for role in REALISTIC_FAULT_ROLES[kind] for channel in roles.get(role, ())
            ]
            raw_clean = [
                (detector, detector.window(stream, lo, hi, channels=fault_channels))
                for detector, stream in raw
            ]
            clean = score_session(
                detectors,
                featurize_wide_frame(piece, config, bindings=bindings).features,
                persistence,
                raw=raw_clean,
            )
            for severity in REALISTIC_FAULTS[kind].severities:
                faulted_frame, injection = inject_realistic_fault(
                    piece,
                    kind=kind,
                    severity=severity,
                    start=start,
                    end=end,
                    roles=roles,
                    bindings=bindings,
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
                faulted = score_session(
                    detectors,
                    featurize_wide_frame(faulted_frame, config, bindings=bindings).features,
                    persistence,
                    raw=[
                        (
                            detector,
                            detector.inject(
                                window_stream,
                                kind=kind,
                                severity=severity,
                                start=start,
                                end=end,
                                roles=roles,
                            ),
                        )
                        for detector, window_stream in raw_clean
                    ],
                )
                for name, result in faulted.items():
                    outcome = fault_attributable_detection(
                        result.timestamps,
                        faulted_alerting=result.alerting,
                        clean_alerting=clean[name].alerting,
                        faulted_verdicts=list(result.verdict),
                        start=start,
                        end=end,
                        grace_seconds=grace_seconds,
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
                        & (result.timestamps < end + pd.Timedelta(seconds=grace_seconds))
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


def shift_slice(features: pd.DataFrame, rows: int) -> pd.DataFrame:
    """``rows`` contiguous rows centred on the session's engine-running rows
    (empty when the engine never ran)."""
    running = np.flatnonzero(engine_running(features))
    if running.size == 0:
        return features.iloc[0:0]
    centre = int(running[running.size // 2])
    start = max(0, centre - rows // 2)
    return features.iloc[start : start + rows].reset_index(drop=True)


def shift_known_answer(
    detectors: Sequence[Detector],
    features: pd.DataFrame,
    quantities: Mapping[str, Sequence[str]],
) -> dict[str, Any]:
    """A04.1 on one slice: every detector's single-quantity-shift report."""

    def score(frame: pd.DataFrame) -> dict[str, Any]:
        out: dict[str, Any] = {}
        for detector in detectors:
            out.update({n: o.verdict for n, o in detector.score(frame).items()})
        return out

    reports = single_quantity_shift(score, features, quantities, mode_column=MODE_COLUMN)
    return {name: report.to_dict() for name, report in reports.items()}


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
    parser.add_argument("--bindings", type=Path, default=DEFAULT_BINDINGS_PATH)
    parser.add_argument("--cache-dir", type=Path, default=DEFAULT_CACHE_DIR)
    parser.add_argument("--workers", type=int, default=2)
    parser.add_argument("--seed", type=int, default=42)
    parser.add_argument("--parity-rows", type=int, default=200)
    parser.add_argument("--budget", type=float, default=V1_ALERTS_PER_HOUR_BUDGET)
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
        help="Score only sessions already in --cache-dir; name the rest as skipped.",
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
    parser.add_argument("--log-level", default="WARNING")
    return parser.parse_args(argv)


def _utc_date(value: str) -> datetime:
    parsed = datetime.fromisoformat(value)
    return parsed if parsed.tzinfo is not None else parsed.replace(tzinfo=UTC)


def _profile_data_dir(config: CanaryConfig, profile: str | None) -> Path:
    """``data/<slug>/`` — the vehicle's labels, tune registry and fault roles."""
    from src.profiles import resolve_profile_for_tooling

    return Path("data") / resolve_profile_for_tooling(config, profile).slug


def _spread_in_time(names: Sequence[str], count: int) -> list[str]:
    ordered = sorted(names, key=lambda n: (session_started_at(n), n))
    if count >= len(ordered):
        return ordered
    picks = np.linspace(0, len(ordered) - 1, count).round().astype(int)
    return [ordered[i] for i in sorted(set(picks.tolist()))]


def main(argv: Sequence[str] | None = None) -> int:
    args = parse_args(argv)
    configure_logging(args.log_level)
    logging.getLogger("src.preprocessing.feature_pipeline").setLevel(logging.WARNING)
    config = load_config(args)
    models_dir = scoped_models_dir(config, args.profile)
    data_dir = _profile_data_dir(config, args.profile)
    persistence = PersistenceSpec.from_config(config)
    bindings = load_bindings(args.bindings) if args.bindings.exists() else None

    if_path = args.if_model or models_dir / "isolation_forest" / "model.joblib"
    trained_if: TrainedIsolationForest | None = (
        load_isolation_forest_model(if_path) if if_path.exists() else None
    )
    trained_seq: TrainedSequenceModel | None = None
    seq_path = args.sequence_model or models_dir / "lstm_autoencoder" / "model.pt"
    if not args.no_sequence and seq_path.exists():
        from src.models.lstm_training import load_lstm_autoencoder_model

        trained_seq = load_lstm_autoencoder_model(seq_path)
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
    session_dirs = list(args.sessions) or discover_session_dirs(roots)
    by_name = {path.name: path for path in discover_session_dirs(roots)}
    by_name.update({path.name: path for path in args.sessions})

    tune_path = args.tune_revisions or data_dir / "tune_revisions.yaml"
    labels_path = args.labels or data_dir / LABELLED_EVENTS_FILENAME
    registry = load_labelled_events(labels_path, tune_revisions_path=tune_path)
    tune_registry = load_tune_revisions(tune_path)
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
    # and a pause inside one recording splitting it (src.analysis.drives).
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
    for drive in drives:
        seen_by = {name: seen[name] for name in drive.session_names if name in seen}
        if seen_by:
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
        {name for drive in evaluation for name in drive.session_names}
        | set(fit_names)
        | set(calibration_names)
    )
    if args.plan:
        for drive in evaluation:
            print(
                f"evaluate {drive.drive_id} [{drive.era.label if drive.era else '?'}]: "
                f"{', '.join(drive.session_names)}"
            )
        for entry in skipped:
            print(f"skip     {entry['drive']}: {entry['reason']}")
        print(f"baseline fit: {', '.join(fit_names) or 'none'}")
        print(f"baseline thresholds: {', '.join(calibration_names) or 'none'}")
        for name in needed:
            print(f"load {by_name[name]}")
        return 0
    frames, load_errors = load_resampled_sessions(
        [by_name[n] for n in needed],
        config,
        args.config,
        cache_dir=args.cache_dir,
        workers=args.workers,
        cache_only=args.cache_only,
    )
    features = {
        name: featurize_wide_frame(frame, config, bindings=bindings).features
        for name, frame in frames.items()
    }

    stack = DeployedStack(
        config,
        trained_if,
        trained_seq,
        suppress_channels=suppressed_for(trained_seq, config) if trained_seq else (),
    )
    detectors: list[Detector] = [stack]
    baseline_thresholds: dict[str, Any] = {}
    fit_frames = [features[n] for n in fit_names if n in features]
    calibration_frames = [features[n] for n in calibration_names if n in features]
    if reference is not None and args.baselines and fit_frames and calibration_frames:
        # Baselines see the served models' own raw channels (rolling siblings
        # dropped) — the same rows, the same inputs.
        channels = raw_channels(reference.feature_columns)
        models: list[ScoreModel] = []
        if "robust_z" in args.baselines:
            models.append(RobustZBaseline.fit(fit_frames, channels))
        if "pca" in args.baselines:
            models.append(PCABaseline.fit(fit_frames, channels))
        if {"robust_z_curated", "mahalanobis_curated"} & set(args.baselines):
            curated = load_curated_channels(
                args.curated_channels or data_dir / "curated_outlier_channels.yaml"
            )
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
        # Independent blocks of one sequence window, or of the rolling window
        # when no sequence model is scored.
        block_seconds = float(
            trained_seq.manifest.schema["sequence_length"]
            if trained_seq is not None
            else config.preprocessing.rolling_window_seconds
        )
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
            baseline_thresholds[model.name] = budget.to_dict()
            if budget.threshold is not None:
                detectors.append(ThresholdedScore(model, budget.threshold))
    withheld_detectors = dict(stack.withheld)
    raw_detectors: list[RawStreamDetector] = []
    if "plausibility" in args.baselines:
        try:
            raw_detectors.append(
                PlausibilityDetector(
                    _scoped(config, args.profile),
                    bindings=bindings,
                    did_map=(
                        load_did_map(DEFAULT_DIDS_PATH) if DEFAULT_DIDS_PATH.exists() else None
                    ),
                )
            )
        except FileNotFoundError as exc:
            withheld_detectors["plausibility"] = f"withheld: {exc}"
    names = [n for d in detectors for n in d.names] + [n for r in raw_detectors for n in r.names]
    streams = load_raw_streams(
        raw_detectors,
        [by_name[n] for d in evaluation for n in d.session_names if n in frames],
        config,
        cache_dir=args.cache_dir,
    )

    parity: dict[str, Any] | None = None
    if stack.sequence is not None and evaluation:
        first = next((n for d in evaluation for n in d.session_names if n in features), None)
        if first is not None:
            parity = sequence_parity(
                stack.sequence, features[first], rows=args.parity_rows
            ).to_dict()
            parity["session"] = first

    scores: dict[str, dict[str, SessionScore]] = {}
    records: list[DriveRecord] = []
    outcomes: list[dict[str, Any]] = []
    skipped_injections: list[dict[str, Any]] = []
    slices: list[dict[str, Any]] = []
    roles = load_fault_channel_roles(args.fault_roles or data_dir / "fault_injection.yaml")
    shift_runs: list[dict[str, Any]] = []
    for drive in evaluation:
        present = {n: frames[n] for n in drive.session_names if n in features}
        if not present:
            skipped.append(
                {
                    "drive": drive.drive_id,
                    "sessions": list(drive.session_names),
                    "reason": "skipped: "
                    + "; ".join(
                        f"{n}: {load_errors.get(n, 'not loaded')}" for n in drive.session_names
                    ),
                }
            )
            continue
        for name in present:
            scores[name] = score_session(
                detectors,
                features[name],
                persistence,
                raw=[(r, streams[(r.names[0], name)]) for r in raw_detectors],
            )
        record = clean_drive_record(drive, present, scores, names, registry.events)
        record.cross_era = bool(training_eras) and drive.era not in training_eras
        records.append(record)
        if not args.no_shift_test and not record.cross_era:
            longest = max(present, key=lambda n: int(engine_running(present[n]).sum()))
            piece = shift_slice(features[longest], args.shift_rows)
            if len(piece):
                shift_runs.append(
                    {
                        "drive": drive.drive_id,
                        "session": longest,
                        "rows": len(piece),
                        "detectors": shift_known_answer(detectors, piece, roles),
                    }
                )
        if record.cross_era:
            skipped_injections.append(
                {
                    "drive": drive.drive_id,
                    "reason": f"cross-era ({drive.era.label if drive.era else '?'}; "
                    f"artifacts trained in {sorted(e.label for e in training_eras)})",
                }
            )
            continue
        if args.faults and args.injections_per_drive > 0:
            longest = max(present, key=lambda n: int(engine_running(present[n]).sum()))
            found, skip, cut = injection_sweep(
                drive,
                present[longest],
                longest,
                detectors=detectors,
                config=config,
                bindings=bindings,
                roles=roles,
                kinds=args.faults,
                reps=args.injections_per_drive,
                seed=args.seed,
                persistence=persistence,
                lead_seconds=args.lead_seconds,
                window_seconds=args.window_seconds,
                grace_seconds=args.grace_seconds,
                raw=[(r, streams[(r.names[0], longest)]) for r in raw_detectors],
            )
            outcomes.extend(found)
            skipped_injections.extend(skip)
            slices.extend(cut)

    role_of = detector_roles(config, names)
    report: dict[str, Any] = {
        "generated_at": datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "command": " ".join(sys.argv),
        "config": str(args.config),
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
        "baseline_fit_sessions": [n for n in fit_names if n in features],
        "baseline_threshold_sessions": [n for n in calibration_names if n in features],
        "baseline_fit_sessions_not_loaded": [n for n in fit_names if n not in features],
        "baseline_threshold_sessions_not_loaded": [
            n for n in calibration_names if n not in features
        ],
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
    args.out.parent.mkdir(parents=True, exist_ok=True)
    args.out.write_text(json.dumps(report, indent=2, default=str) + "\n")
    markdown = args.markdown or args.out.with_suffix(".md")
    markdown.write_text(render_markdown(report))
    print(f"wrote {args.out} and {markdown}")
    print(
        f"{len(records)} held-out drive(s) scored, {len(skipped)} skipped; "
        f"{len(outcomes)} injection outcome(s)"
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
