"""The baselines scored beside the served detectors (A02.5).

Robust-z, PCA, the untrained autoencoder and the curated-channel variants are
fitted on a spread of the sequence model's own training sessions and take
their thresholds from the served models' calibration sessions, so the
evaluation drives stay unseen by every compared detector. The fitted set is
one cache entry keyed on the frames' content, the channels, the requested
baselines, the sequence identity, the seed, the budget and the persistence
gate, so an unchanged re-run refits nothing. Extracted from
``scripts/evaluate_detectors.py`` (R15.4).

The untrained autoencoder is cached as ``None`` and rebuilt from its seed (it
has no fitted state to serialise), exactly as a spawned scoring worker
rebuilds it (:func:`~src.models.evaluation_workers.build_worker_scorer`).
"""

from __future__ import annotations

from collections.abc import Callable, Collection, Mapping, Sequence
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Any

import pandas as pd

from src.columns import TIMESTAMP_COLUMN
from src.config import CanaryConfig
from src.models.evaluation import (
    PersistenceSpec,
    ScoreTrace,
    alert_budget_threshold,
    engine_running,
)
from src.models.evaluation_cache import ScoreCache, digest
from src.models.evaluation_detectors import (
    Detector,
    MahalanobisBaseline,
    PCABaseline,
    RobustZBaseline,
    ScoreModel,
    ThresholdedScore,
    UntrainedAutoencoder,
    load_curated_channels,
    raw_channels,
    suppressed_for,
)
from src.models.evaluation_scorer import score_model_identity
from src.models.isolation_forest import TrainedIsolationForest
from src.models.sequence_protocol import TrainedSequenceModel


@dataclass(frozen=True)
class BaselineSet:
    """What the baselines add to the compared detectors."""

    #: One :class:`ThresholdedScore` per baseline that has a threshold.
    detectors: tuple[Detector, ...]
    #: ``(fitted model or None for the untrained autoencoder, threshold)``, in
    #: ``detectors`` order: what a spawned worker rebuilds the detectors from.
    specs: tuple[tuple[ScoreModel | None, float], ...]
    #: Every baseline's alert-budget threshold record, including those with no
    #: threshold (reported, not scored).
    thresholds: dict[str, Any]
    #: Cache identity of each scored baseline, by name.
    identities: dict[str, str]


def block_seconds_of(trained_seq: TrainedSequenceModel | None, config: CanaryConfig) -> float:
    """Independent blocks of one sequence window, or of the rolling window when
    no sequence model is scored."""
    return float(
        trained_seq.manifest.schema["sequence_length"]
        if trained_seq is not None
        else config.preprocessing.rolling_window_seconds
    )


def prepare_baselines(
    *,
    requested: Sequence[str],
    reference: TrainedIsolationForest | TrainedSequenceModel | None,
    trained_seq: TrainedSequenceModel | None,
    config: CanaryConfig,
    persistence: PersistenceSpec,
    fit_names: Sequence[str],
    calibration_names: Sequence[str],
    present: Collection[str],
    frame_keys: Mapping[str, str],
    feature_key: str,
    featurize: Callable[[str], pd.DataFrame],
    curated_channels_path: Path,
    sequence_identity: str | None,
    seed: int,
    budget: float,
    cache: ScoreCache,
) -> BaselineSet:
    """Fit (or read back) the requested baselines and give each its threshold.

    ``reference`` is the artifact whose raw channels and training sessions the
    baselines follow (the sequence model, else the IF); ``present`` names the
    sessions that loaded. With nothing to fit or calibrate on, no baseline is
    scored and the set is empty.
    """
    detectors: list[Detector] = []
    thresholds: dict[str, Any] = {}
    specs: list[tuple[ScoreModel | None, float]] = []
    identities: dict[str, str] = {}
    fit_present = [n for n in fit_names if n in present]
    calibration_present = [n for n in calibration_names if n in present]
    if reference is not None and requested and fit_present and calibration_present:
        # Baselines see the served models' own raw channels (rolling siblings
        # dropped) — the same rows, the same inputs.
        channels = raw_channels(reference.feature_columns)
        curated: tuple[str, ...] = ()
        if {"robust_z_curated", "mahalanobis_curated"} & set(requested):
            curated = load_curated_channels(curated_channels_path)
        block_seconds = block_seconds_of(trained_seq, config)
        fitted_unit = digest(
            feature_key,
            "baselines",
            [frame_keys[n] for n in fit_present],
            [frame_keys[n] for n in calibration_present],
            channels,
            curated,
            sorted(requested),
            sequence_identity,
            seed,
            budget,
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
            if "robust_z" in requested:
                models.append(RobustZBaseline.fit(fit_frames, channels))
            if "pca" in requested:
                models.append(PCABaseline.fit(fit_frames, channels))
            if "robust_z_curated" in requested:
                models.append(
                    replace(RobustZBaseline.fit(fit_frames, curated), name="robust_z_curated")
                )
            if "mahalanobis_curated" in requested:
                models.append(MahalanobisBaseline.fit(fit_frames, curated))
            if "untrained_autoencoder" in requested and trained_seq is not None:
                models.append(
                    UntrainedAutoencoder.like(
                        trained_seq,
                        seed=seed,
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
                budget_record = alert_budget_threshold(
                    traces,
                    persistence=persistence,
                    block_seconds=block_seconds,
                    budget_per_engine_hour=budget,
                )
                kept = None if isinstance(model, UntrainedAutoencoder) else model
                fitted.append((model.name, kept, budget_record.to_dict()))
            del fit_frames, calibration_frames
            cache.put("_baselines", fitted_unit, fitted)
        for name, kept, budget_dict in fitted:
            thresholds[name] = budget_dict
            threshold = budget_dict["threshold"]
            if threshold is None:
                continue
            model_now: ScoreModel
            if kept is None:
                assert trained_seq is not None
                model_now = UntrainedAutoencoder.like(
                    trained_seq,
                    seed=seed,
                    suppress_channels=suppressed_for(trained_seq, config),
                )
            else:
                model_now = kept
            detectors.append(ThresholdedScore(model_now, threshold))
            specs.append((kept, threshold))
            identities[name] = score_model_identity(
                model_now, threshold, sequence=sequence_identity, seed=seed
            )
    return BaselineSet(tuple(detectors), tuple(specs), thresholds, identities)
