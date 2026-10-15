"""Scoring one held-out drive through the score cache (P02.4).

The deployed scoring kernel of the evaluation harness. A *unit* is a set of
rows every detector is graded on: a held-out session, an injection window's
clean slice, one injected slice, or a drive's shift slice. :class:`DriveScorer`
reads each :class:`Component`'s output on a unit back from the cache or
computes and keeps it; features are built only when some component misses, and
an injected slice is featurized incrementally from its clean slice.
:class:`DriveTask` and :class:`DriveResult` are what crosses the process
boundary (they are pickled to and from the spawned scoring workers of
:mod:`src.models.evaluation_workers`), so they live beside the scorer.

Seeds are derived per ``(seed, drive, kind, rep)`` by :func:`_seed_for`, never
from process state, so serial and spawned scoring agree. Extracted from
``scripts/evaluate_detectors.py`` (R15.3); that script re-exports these names.
"""

from __future__ import annotations

import hashlib
import os
import pickle
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field
from functools import partial
from pathlib import Path
from typing import Any

import numpy as np
import pandas as pd

from src.behavior.behavior_models import BehaviorModels
from src.columns import MODE_COLUMN, TIMESTAMP_COLUMN
from src.config import CanaryConfig
from src.decode.bindings import Bindings
from src.decode.dids import DidMap
from src.features.feature_perturbation import featurize_perturbed_frame
from src.features.feature_transform import FeaturizedFrame, featurize_wide_frame
from src.models.evaluation import (
    Drive,
    PersistenceSpec,
    engine_running,
    fault_attributable_detection,
    single_quantity_shift,
)
from src.models.evaluation_cache import ScoreCache, digest
from src.models.evaluation_detectors import (
    DeployedStack,
    Detector,
    DetectorOutput,
    RawStreamDetector,
    ScoreModel,
    ThresholdedScore,
    UntrainedAutoencoder,
    sequence_outputs,
)
from src.models.evaluation_records import SessionScore, session_scores
from src.models.evaluation_streams import CachedStreams
from src.preprocessing.anomaly_injection import (
    REALISTIC_FAULT_ROLES,
    REALISTIC_FAULTS,
    choose_fault_window,
    inject_realistic_fault,
)

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


def _features_only(
    score: Callable[[Any], Any], features: pd.DataFrame | None, _deps: Mapping[str, Any]
) -> Any:
    """A one-argument scorer as a :class:`Component` ``compute``: the unit's
    features in, the other components' payloads ignored."""
    return score(features)


def _fuse_components(
    stack: DeployedStack, _features: pd.DataFrame | None, deps: Mapping[str, Any]
) -> Any:
    """The fused component: reads the IF's and the sequence detector's payloads
    and no features."""
    return stack.fuse(deps["isolation_forest"], deps["sequence"])


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
                        partial(_features_only, stack.score_isolation_forest),
                        lambda p: {"isolation_forest": p.output},
                    )
                )
            if stack.sequence is not None:
                components.append(
                    Component(
                        "sequence",
                        _component_key("sequence", identities["sequence"], code),
                        ("sequence", "sequence_global"),
                        partial(_features_only, stack.score_sequence),
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
                        partial(_fuse_components, stack),
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
                    partial(_features_only, detector.score),
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
                        partial(stream_of, r, name),
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


def shift_slice(features: pd.DataFrame, rows: int) -> pd.DataFrame:
    """``rows`` contiguous rows centred on the session's engine-running rows
    (empty when the engine never ran)."""
    running = np.flatnonzero(engine_running(features))
    if running.size == 0:
        return features.iloc[0:0]
    centre = int(running[running.size // 2])
    start = max(0, centre - rows // 2)
    return features.iloc[start : start + rows].reset_index(drop=True)


def _seed_for(*parts: object) -> int:
    digest = hashlib.sha256(":".join(str(p) for p in parts).encode()).digest()
    return int.from_bytes(digest[:4], "big")
