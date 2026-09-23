"""Detectors the evaluation harness can score, on the same rows (A02.4/A02.5).

Every detector here answers the same question for one session's featurized
rows: per row, a verdict (``True``/``False``/``None`` — ``None`` meaning it
did not look) and a score. :mod:`src.models.evaluation` turns those into
episodes, false alarms and detections identically for all of them.

* :class:`DeployedStack` — the served Isolation Forest, the served sequence
  detector (global score *or* the per-family path, exactly as served), the
  sequence detector's global score alone (the per-family ablation, A02.3), and
  the fused verdict built from them the way the daemon builds it.
* :class:`BatchedSequenceScorer` — the sequence engine's scoring rule applied
  to many windows per forward pass. Windows, masks and Δt come from the live
  :class:`~src.models.inference.SequenceWindowBuffer` itself, so they are the
  served windows; :func:`sequence_parity` checks the batched verdicts against
  ``SequenceInferenceEngine.observe`` row for row, and the harness records that
  check in its report.
* Baselines (A02.5): per-channel robust z-scores per operating mode, PCA
  reconstruction, and an untrained autoencoder of the deployed shape. They
  produce scores; :class:`ThresholdedScore` applies a threshold chosen from the
  alert budget on calibration drives (``evaluation.alert_budget_threshold``).
"""

from __future__ import annotations

import importlib
import math
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field, replace
from pathlib import Path
from types import SimpleNamespace
from typing import Any, Protocol

import numpy as np
import pandas as pd

from src.alerts.fusion import FusionScorer
from src.config import CanaryConfig
from src.models.inference import SequenceInferenceEngine
from src.models.isolation_forest import SCORE_COLUMN, TrainedIsolationForest
from src.models.sequence_protocol import TrainedSequenceModel
from src.models.sequence_training import SequenceThresholdCalibration
from src.models.synthetic_fault_eval import (
    _fused_anomalous,
    _if_anomalous,
    fusion_withheld_reason,
)
from src.preprocessing.feature_pipeline import align_features_to_schema, expand_suppress_channels
from src.preprocessing.normalization import ZScoreScaler
from src.preprocessing.resampler import GAP_SUFFIX, SESSION_COLUMN, TIMESTAMP_COLUMN
from src.preprocessing.segmenter import MODE_COLUMN


@dataclass(frozen=True)
class DetectorOutput:
    """Per-row verdicts and scores for one detector on one session."""

    #: ``True``/``False``/``None`` per row (object array).
    verdict: np.ndarray[Any, np.dtype[np.object_]]
    #: Score per row, NaN where the detector produced none.
    score: np.ndarray[Any, np.dtype[np.float64]]
    #: Verdicts already passed through persistence upstream (fused).
    pre_gated: bool = False


class Detector(Protocol):
    """Anything the harness can score."""

    @property
    def names(self) -> tuple[str, ...]: ...

    def score(self, features: pd.DataFrame) -> dict[str, DetectorOutput]: ...


def _verdicts_from_scores(
    scores: np.ndarray[Any, np.dtype[np.float64]], threshold: float
) -> np.ndarray[Any, np.dtype[np.object_]]:
    out = np.full(scores.size, None, dtype=object)
    finite = np.isfinite(scores)
    out[finite] = scores[finite] > threshold
    return out


# ---------------------------------------------------------------------------
# The served sequence rule, batched
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class SequenceScores:
    """Row-aligned sequence-detector outputs (NaN / ``None`` before a window)."""

    global_score: np.ndarray[Any, np.dtype[np.float64]]
    global_threshold: np.ndarray[Any, np.dtype[np.float64]]
    family_max: np.ndarray[Any, np.dtype[np.float64]]
    family_threshold: float | None
    is_anomaly: np.ndarray[Any, np.dtype[np.object_]]
    is_anomaly_global: np.ndarray[Any, np.dtype[np.object_]]

    def results(self) -> list[Any]:
        """``SequenceInferenceResult``-shaped rows for the fusion helper."""
        out: list[Any] = []
        for index, anomaly in enumerate(self.is_anomaly):
            if anomaly is None:
                out.append(None)
                continue
            out.append(
                SimpleNamespace(
                    global_score=float(self.global_score[index]),
                    global_threshold=float(self.global_threshold[index]),
                    is_anomaly=bool(anomaly),
                )
            )
        return out


def _base_channel(column: str) -> str:
    for suffix in ("__mean", "__std", "__roc"):
        if column.endswith(suffix):
            return column[: -len(suffix)]
    return column


class BatchedSequenceScorer:
    """``SequenceInferenceEngine.score_window``'s verdict rule over many windows.

    The per-tick engine spends ~13 ms per window on a single-window forward
    pass; the harness scores tens of thousands of windows. This keeps the
    engine's inputs (its own window buffer, its scaler, its excluded
    channels) and applies its rule — masked global MAE against the window's
    mode band, OR the worst normalized family error against the family band —
    to batches. :func:`sequence_parity` is the check that the two agree.
    """

    def __init__(
        self,
        trained: TrainedSequenceModel,
        *,
        suppress_channels: Sequence[str] = (),
        batch_size: int = 512,
        device: str = "cpu",
        use_family_path: bool = True,
    ) -> None:
        self.trained = trained
        self.engine = SequenceInferenceEngine(
            trained, device=device, suppress_channels=suppress_channels
        )
        self.calibration: SequenceThresholdCalibration = self.engine.calibration
        self.feature_columns = tuple(trained.feature_columns)
        self.sequence_length = self.engine.sequence_length
        self.batch_size = batch_size
        self.device = device
        self.use_family_path = use_family_path
        excluded = set(self.engine.suppressed_channels) | set(self.engine.uncalibrated_channels)
        keep = np.array([column not in excluded for column in self.feature_columns], dtype=bool)
        self._score_keep = keep.astype(np.float32)
        scaler_block = trained.manifest.scaler
        if scaler_block is None:
            raise ValueError("sequence artifact has no scaler; it cannot be scored")
        scaler = ZScoreScaler.from_dict(dict(scaler_block))
        self._means = np.array([scaler.means[c] for c in self.feature_columns], dtype=np.float64)
        stds = np.array([scaler.stds[c] for c in self.feature_columns], dtype=np.float64)
        self._stds = np.where(stds >= scaler.std_floor, stds, 1.0)
        self._families: dict[str, list[int]] = {}
        for index, column in enumerate(self.feature_columns):
            self._families.setdefault(_base_channel(column), []).append(index)
        self._torch = importlib.import_module("torch")

    def _windows(
        self, features: pd.DataFrame
    ) -> tuple[
        np.ndarray[Any, np.dtype[np.intp]],
        np.ndarray[Any, np.dtype[np.float64]],
        np.ndarray[Any, np.dtype[np.float32]],
        np.ndarray[Any, np.dtype[np.float32]],
        list[str | None],
    ]:
        """Every window the live :class:`SequenceWindowBuffer` would score, as
        the rows that end one plus per-row values, mask and Δt to slice from.

        A vectorized replica of ``SequenceWindowBuffer.append`` (per-row
        appends cost ~1 ms each): a session change, a step backwards or over
        ``WINDOW_MAX_GAP_SECONDS``, or a row with no finite feature clears the
        window and its Δt; the mask is ``¬<channel>__gap`` and value-is-finite;
        values are held at the buffer's float32. :func:`sequence_parity`
        compares the result against the engine itself.
        """
        from src.models.sequence_dataset import WINDOW_MAX_GAP_SECONDS

        aligned = align_features_to_schema(features, list(self.feature_columns))
        n = len(aligned)
        values = (
            aligned.loc[:, list(self.feature_columns)]
            .apply(pd.to_numeric, errors="coerce")
            .to_numpy(dtype=np.float64)
        )
        finite = np.isfinite(values)
        mask = finite.astype(np.float32)
        for index, column in enumerate(self.feature_columns):
            gap = f"{_base_channel(column)}{GAP_SUFFIX}"
            if gap in aligned.columns:
                raw = aligned[gap]
                stale = raw.isna().to_numpy() | raw.fillna(True).astype(bool).to_numpy()
                mask[stale, index] = 0.0
        stamps = pd.to_datetime(aligned[TIMESTAMP_COLUMN], utc=True).to_numpy()
        sessions = (
            aligned[SESSION_COLUMN].astype(str).to_numpy()
            if SESSION_COLUMN in aligned.columns
            else np.full(n, "", dtype=object)
        )
        live = finite.any(axis=1)
        step = np.zeros(n)
        if n > 1:
            step[1:] = (stamps[1:] - stamps[:-1]) / np.timedelta64(1, "s")
        fresh = np.ones(n, dtype=bool)
        if n > 1:
            fresh[1:] = (
                ~live[:-1]
                | (sessions[1:] != sessions[:-1])
                | (step[1:] < 0)
                | (step[1:] > WINDOW_MAX_GAP_SECONDS)
            )
        delta = np.zeros_like(values)
        run = np.zeros(n, dtype=np.int64)
        accumulator = np.zeros(values.shape[1])
        length = 0
        for row in range(n):
            if not live[row]:
                length = 0
                continue
            if fresh[row] or length == 0:
                accumulator = np.zeros(values.shape[1])
                length = 0
            else:
                accumulator = accumulator + max(step[row], 0.0)
            accumulator[mask[row] > 0.5] = 0.0
            delta[row] = accumulator
            length += 1
            run[row] = length
        rows = np.flatnonzero(live & (run >= self.sequence_length))
        stored = values.astype(np.float32).astype(np.float64)
        modes_column = (
            aligned[MODE_COLUMN].to_numpy() if MODE_COLUMN in aligned.columns else [None] * n
        )
        modes = [m if isinstance(m, str) else None for m in (modes_column[r] for r in rows)]
        # Per-row arrays; windows are sliced out per batch in ``score`` (a
        # two-hour drive's windows would otherwise be ~1 GB at once).
        # No cache across calls: an id-keyed one served one frame's windows
        # for another (freed ids are reused), and rebuilding costs ~0.2 s.
        return rows, stored, mask, delta.astype(np.float32), modes

    def score(self, features: pd.DataFrame) -> SequenceScores:
        n = len(features)
        global_score = np.full(n, np.nan)
        global_threshold = np.full(n, np.nan)
        family_max = np.full(n, np.nan)
        is_anomaly = np.full(n, None, dtype=object)
        is_anomaly_global = np.full(n, None, dtype=object)
        rows, values, masks, deltas, modes = self._windows(features)
        calibration = self.calibration
        per_mode = calibration.per_mode_errors or {}
        family_threshold = (
            calibration.family_max_score.threshold
            if self.use_family_path and calibration.family_errors and calibration.family_max_score
            else None
        )
        # Families the engine scores: a band with a positive threshold.
        scored_families = [
            (idx, dist.threshold)
            for family, idx in self._families.items()
            if (dist := (calibration.family_errors or {}).get(family)) is not None
            and dist.threshold > 0
        ]
        eps = np.finfo(np.float64).eps
        torch = self._torch
        model: Any = self.trained.model
        model.to(torch.device(self.device))
        model.eval()
        offsets = np.arange(-self.sequence_length + 1, 1)
        for start in range(0, len(rows), self.batch_size):
            stop = start + self.batch_size
            take = rows[start:stop, None] + offsets[None, :]
            scaled = (values[take] - self._means) / self._stds
            scaled = np.where(np.isfinite(scaled), scaled, 0.0).astype(np.float32)
            mask = masks[take]
            score_mask = mask * self._score_keep
            with torch.no_grad():
                value_t = torch.as_tensor(scaled)
                recon = model.forward(value_t, torch.as_tensor(mask), torch.as_tensor(deltas[take]))
                error = ((recon - value_t).abs() * torch.as_tensor(score_mask)).numpy()
            error = error.astype(np.float64)
            cells = score_mask.sum(axis=(1, 2)).astype(np.float64)
            batch_global = np.divide(
                error.sum(axis=(1, 2)), cells, out=np.zeros_like(cells), where=cells > 0
            )
            counts = score_mask.sum(axis=1).astype(np.float64)
            channel = np.divide(
                error.sum(axis=1), counts, out=np.zeros_like(counts), where=counts > 0
            )
            batch_rows = rows[start:stop]
            batch_modes = modes[start:stop]
            thresholds = np.array(
                [
                    band.threshold
                    if mode is not None and (band := per_mode.get(mode)) is not None
                    else calibration.global_error.threshold
                    for mode in batch_modes
                ]
            )
            global_anomaly = batch_global > thresholds
            family_anomaly = np.zeros(len(batch_rows), dtype=bool)
            if family_threshold is not None and scored_families:
                normalized = np.column_stack(
                    [channel[:, idx].max(axis=1) / max(t, eps) for idx, t in scored_families]
                )
                worst = normalized.max(axis=1)
                family_max[batch_rows] = worst
                family_anomaly = worst > family_threshold
            global_score[batch_rows] = batch_global
            global_threshold[batch_rows] = thresholds
            for offset, row in enumerate(batch_rows):
                is_anomaly_global[row] = bool(global_anomaly[offset])
                is_anomaly[row] = bool(global_anomaly[offset] or family_anomaly[offset])
        return SequenceScores(
            global_score=global_score,
            global_threshold=global_threshold,
            family_max=family_max,
            family_threshold=family_threshold,
            is_anomaly=is_anomaly,
            is_anomaly_global=is_anomaly_global,
        )


@dataclass(frozen=True)
class ParityReport:
    """Batched scorer vs. the per-tick engine on the same rows."""

    windows: int
    verdict_mismatches: int
    max_score_difference: float

    @property
    def agrees(self) -> bool:
        return self.windows > 0 and self.verdict_mismatches == 0

    def to_dict(self) -> dict[str, Any]:
        return {
            "windows": self.windows,
            "verdict_mismatches": self.verdict_mismatches,
            "max_score_difference": self.max_score_difference,
            "agrees": self.agrees,
        }


def sequence_parity(
    scorer: BatchedSequenceScorer, features: pd.DataFrame, *, rows: int = 200
) -> ParityReport:
    """Score the first ``rows`` rows both ways and compare verdicts and scores.

    Reproduce a known answer before trusting a novel one: the served engine is
    the known answer, and a batched shortcut that disagreed with it would be
    grading some other detector.
    """
    head = align_features_to_schema(features.iloc[:rows], list(scorer.feature_columns))
    batched = scorer.score(head)
    engine = scorer.engine
    engine.reset()
    windows = 0
    mismatches = 0
    worst = 0.0
    for index in range(len(head)):
        result = engine.observe(head.iloc[index])
        if result is None:
            if batched.is_anomaly[index] is not None:
                mismatches += 1
            continue
        windows += 1
        if batched.is_anomaly[index] is None or bool(batched.is_anomaly[index]) != bool(
            result.is_anomaly
        ):
            mismatches += 1
        if batched.is_anomaly[index] is not None:
            worst = max(worst, abs(float(batched.global_score[index]) - result.global_score))
    engine.reset()
    return ParityReport(windows, mismatches, worst)


# ---------------------------------------------------------------------------
# The deployed stack
# ---------------------------------------------------------------------------


class DeployedStack:
    """The served detectors, scored once per session, plus their fusion.

    Names: ``isolation_forest``; with a sequence artifact also ``sequence``
    (as served: global or per-family), ``sequence_global`` (the global score
    alone — the per-family ablation) and ``fused``. Without one, fused is in
    :attr:`withheld` with the reason (A02.9).
    """

    def __init__(
        self,
        config: CanaryConfig,
        trained_if: TrainedIsolationForest | None,
        trained_sequence: TrainedSequenceModel | None,
        *,
        suppress_channels: Sequence[str] = (),
        batch_size: int = 512,
    ) -> None:
        self.config = config
        self.trained_if = trained_if
        self.sequence = (
            BatchedSequenceScorer(
                trained_sequence, suppress_channels=suppress_channels, batch_size=batch_size
            )
            if trained_sequence is not None
            else None
        )
        self.fusion = FusionScorer(config.models.fusion, roles=config.models.roles.by_source())
        sources = [
            *(["isolation_forest"] if trained_if is not None else []),
            *(["sequence"] if self.sequence is not None else []),
        ]
        self.withheld: dict[str, str] = {}
        reason = fusion_withheld_reason(sources, config)
        if reason is not None:
            self.withheld["fused"] = reason

    @property
    def names(self) -> tuple[str, ...]:
        names: list[str] = []
        if self.trained_if is not None:
            names.append("isolation_forest")
        if self.sequence is not None:
            names.extend(["sequence", "sequence_global"])
        if "fused" not in self.withheld:
            names.append("fused")
        return tuple(names)

    def score(self, features: pd.DataFrame) -> dict[str, DetectorOutput]:
        out: dict[str, DetectorOutput] = {}
        scored: pd.DataFrame | None = None
        if self.trained_if is not None:
            aligned = align_features_to_schema(features, list(self.trained_if.feature_columns))
            scored = self.trained_if.score(aligned)
            verdict = _if_anomalous(scored).to_numpy(dtype=object)
            score = pd.to_numeric(scored[SCORE_COLUMN], errors="coerce").to_numpy(
                dtype=float, copy=True
            )
            score[~np.isfinite(score)] = np.nan
            out["isolation_forest"] = DetectorOutput(verdict, score)
        sequence: SequenceScores | None = None
        if self.sequence is not None:
            sequence = self.sequence.score(features)
            out["sequence"] = DetectorOutput(sequence.is_anomaly, sequence.global_score)
            out["sequence_global"] = DetectorOutput(
                sequence.is_anomaly_global, sequence.global_score
            )
        if "fused" not in self.withheld and scored is not None and sequence is not None:
            fused = _fused_anomalous(
                scored,
                fusion=self.fusion,
                scales=dict(self.trained_if.score_scales) if self.trained_if else {},
                config=self.config,
                sequence_results=sequence.results(),
            ).to_numpy(dtype=object)
            out["fused"] = DetectorOutput(fused, np.full(len(features), np.nan), pre_gated=True)
        return out


# ---------------------------------------------------------------------------
# Baselines (A02.5)
# ---------------------------------------------------------------------------


class ScoreModel(Protocol):
    """A baseline that turns rows into scores (higher = more anomalous)."""

    name: str

    def scores(self, features: pd.DataFrame) -> np.ndarray[Any, np.dtype[np.float64]]: ...


@dataclass
class ThresholdedScore:
    """A score model plus the threshold the alert budget chose for it."""

    model: ScoreModel
    threshold: float

    @property
    def names(self) -> tuple[str, ...]:
        return (self.model.name,)

    def score(self, features: pd.DataFrame) -> dict[str, DetectorOutput]:
        values = self.model.scores(features)
        return {
            self.model.name: DetectorOutput(_verdicts_from_scores(values, self.threshold), values)
        }


def _observed(frame: pd.DataFrame, column: str) -> np.ndarray[Any, np.dtype[np.float64]]:
    """Values where the channel was freshly observed; NaN on forward-filled holds."""
    values = pd.to_numeric(frame[column], errors="coerce").to_numpy(dtype=np.float64)
    gap = f"{column}{GAP_SUFFIX}"
    if gap in frame.columns:
        stale = frame[gap].fillna(True).astype(bool).to_numpy()
        values = np.where(stale, np.nan, values)
    return values


#: Rows a (mode, channel) cell needs before it gets its own median/MAD; below
#: it the channel's pooled statistics are used.
MIN_MODE_ROWS = 30
_MAD_TO_SIGMA = 1.4826


@dataclass
class RobustZBaseline:
    """Per-channel robust z-scores per operating mode; score = max |z|.

    Median and MAD per (mode, channel) from the fit rows, over freshly observed
    samples only. A channel with zero spread in a mode is left out of that
    mode (``degenerate``) rather than turning every change into an infinite z
    — the quarantine trap the IF fell into (MAD = 0).
    """

    channels: tuple[str, ...]
    name: str = "robust_z"
    stats: dict[str, dict[str, tuple[float, float]]] = field(default_factory=dict)
    pooled: dict[str, tuple[float, float]] = field(default_factory=dict)
    degenerate: dict[str, list[str]] = field(default_factory=dict)

    @classmethod
    def fit(cls, frames: Sequence[pd.DataFrame], channels: Sequence[str]) -> RobustZBaseline:
        model = cls(channels=tuple(channels))
        frame = pd.concat(frames, ignore_index=True)
        modes = (
            frame[MODE_COLUMN].astype(str).to_numpy()
            if MODE_COLUMN in frame.columns
            else np.full(len(frame), "all", dtype=object)
        )
        for channel in model.channels:
            if channel not in frame.columns:
                continue
            values = _observed(frame, channel)
            center, spread = _median_mad(values)
            if spread > 0:
                model.pooled[channel] = (center, spread)
            for mode in np.unique(modes):
                subset = values[modes == mode]
                if np.isfinite(subset).sum() < MIN_MODE_ROWS:
                    continue
                center, spread = _median_mad(subset)
                if spread > 0:
                    model.stats.setdefault(str(mode), {})[channel] = (center, spread)
                else:
                    model.degenerate.setdefault(str(mode), []).append(channel)
        return model

    def scores(self, features: pd.DataFrame) -> np.ndarray[Any, np.dtype[np.float64]]:
        n = len(features)
        modes = (
            features[MODE_COLUMN].astype(str).to_numpy()
            if MODE_COLUMN in features.columns
            else np.full(n, "all", dtype=object)
        )
        best = np.full(n, np.nan)
        for channel in self.channels:
            if channel not in features.columns:
                continue
            values = _observed(features, channel)
            centers = np.full(n, np.nan)
            spreads = np.full(n, np.nan)
            for mode in np.unique(modes):
                rows = modes == mode
                pair = self.stats.get(str(mode), {}).get(channel)
                if pair is None and channel in self.degenerate.get(str(mode), []):
                    continue
                pair = pair or self.pooled.get(channel)
                if pair is None:
                    continue
                centers[rows], spreads[rows] = pair
            z = np.abs(values - centers) / spreads
            best = np.where(np.isfinite(z) & ~(z <= best), z, best)
        return best


def _median_mad(values: np.ndarray[Any, np.dtype[np.float64]]) -> tuple[float, float]:
    finite = values[np.isfinite(values)]
    if finite.size == 0:
        return math.nan, 0.0
    center = float(np.median(finite))
    return center, float(_MAD_TO_SIGMA * np.median(np.abs(finite - center)))


@dataclass
class PCABaseline:
    """PCA reconstruction error over standardized channels.

    Standardized with the fit rows' median/MAD; unobserved cells are imputed
    to the centre and left out of the error (masked mean of squared residuals
    over observed cells). Components explain ``variance`` of the fit rows.
    """

    channels: tuple[str, ...]
    centers: np.ndarray[Any, np.dtype[np.float64]]
    spreads: np.ndarray[Any, np.dtype[np.float64]]
    components: np.ndarray[Any, np.dtype[np.float64]]
    name: str = "pca"

    @classmethod
    def fit(
        cls,
        frames: Sequence[pd.DataFrame],
        channels: Sequence[str],
        *,
        variance: float = 0.95,
        max_components: int = 20,
    ) -> PCABaseline:
        frame = pd.concat(frames, ignore_index=True)
        usable: list[str] = []
        centers: list[float] = []
        spreads: list[float] = []
        for channel in channels:
            if channel not in frame.columns:
                continue
            center, spread = _median_mad(_observed(frame, channel))
            if spread > 0 and math.isfinite(center):
                usable.append(channel)
                centers.append(center)
                spreads.append(spread)
        if not usable:
            raise ValueError("no channel with spread in the PCA fit rows")
        center_arr = np.asarray(centers)
        spread_arr = np.asarray(spreads)
        matrix, observed = _standardize(frame, usable, center_arr, spread_arr)
        rows = observed.mean(axis=1) >= 0.5
        fit_rows = matrix[rows]
        if fit_rows.shape[0] < 2:
            raise ValueError("fewer than two PCA fit rows with half their channels observed")
        _, singular, vt = np.linalg.svd(fit_rows - fit_rows.mean(axis=0), full_matrices=False)
        explained = np.cumsum(singular**2) / np.sum(singular**2)
        keep = min(max_components, int(np.searchsorted(explained, variance)) + 1)
        return cls(tuple(usable), center_arr, spread_arr, vt[:keep])

    def scores(self, features: pd.DataFrame) -> np.ndarray[Any, np.dtype[np.float64]]:
        matrix, observed = _standardize(features, self.channels, self.centers, self.spreads)
        recon = matrix @ self.components.T @ self.components
        residual = ((matrix - recon) ** 2) * observed
        cells = observed.sum(axis=1)
        out: np.ndarray[Any, np.dtype[np.float64]] = np.full(len(features), np.nan)
        np.divide(residual.sum(axis=1), cells, out=out, where=cells > 0)
        return out


def _standardize(
    frame: pd.DataFrame,
    channels: Sequence[str],
    centers: np.ndarray[Any, np.dtype[np.float64]],
    spreads: np.ndarray[Any, np.dtype[np.float64]],
) -> tuple[np.ndarray[Any, np.dtype[np.float64]], np.ndarray[Any, np.dtype[np.float64]]]:
    columns = []
    for channel in channels:
        columns.append(
            _observed(frame, channel) if channel in frame.columns else np.full(len(frame), np.nan)
        )
    raw = np.column_stack(columns) if columns else np.zeros((len(frame), 0))
    z = (raw - centers) / spreads
    observed = np.isfinite(z).astype(np.float64)
    return np.where(np.isfinite(z), z, 0.0), observed


@dataclass
class UntrainedAutoencoder:
    """The deployed sequence architecture with random weights, scored through
    the same windows and masked global error as the trained model.

    If a trained model does not beat its own untrained shape, the training
    added nothing the architecture's inductive bias did not already give.
    """

    scorer: BatchedSequenceScorer
    name: str = "untrained_autoencoder"

    @classmethod
    def like(
        cls,
        trained: TrainedSequenceModel,
        *,
        seed: int = 0,
        suppress_channels: Sequence[str] = (),
        batch_size: int = 512,
    ) -> UntrainedAutoencoder:
        from src.models.sequence_registry import sequence_training_backend_for_model_type

        torch = importlib.import_module("torch")
        manifest = trained.manifest
        backend = sequence_training_backend_for_model_type(manifest.model_type)
        torch.manual_seed(seed)
        model = backend.reconstruct_model(manifest.architecture)
        bundle = SimpleNamespace(
            model=model,
            manifest=replace(manifest, model_version=f"untrained-{manifest.model_version}"),
            feature_columns=tuple(trained.feature_columns),
        )
        scorer = BatchedSequenceScorer(
            bundle,
            suppress_channels=suppress_channels,
            batch_size=batch_size,
            use_family_path=False,
        )
        return cls(scorer)

    def scores(self, features: pd.DataFrame) -> np.ndarray[Any, np.dtype[np.float64]]:
        return self.scorer.score(features).global_score


# ---------------------------------------------------------------------------
# Detectors that read arrived samples, not the resampled grid (A05 in A02.5)
# ---------------------------------------------------------------------------


class RawStreamDetector(Protocol):
    """A detector that sees every arrived sample (before resampling).

    The harness loads its stream per session from the session directory,
    scores it onto the same 1 Hz row grid every other detector is graded on,
    and injects faults into the arrived samples themselves.
    """

    @property
    def names(self) -> tuple[str, ...]: ...

    def load(self, session_dir: Path) -> Any: ...

    def score_stream(self, stream: Any, timestamps: pd.Series) -> dict[str, DetectorOutput]: ...

    def window(
        self,
        stream: Any,
        start: pd.Timestamp,
        end: pd.Timestamp,
        *,
        channels: Sequence[str] | None = None,
    ) -> Any: ...

    def inject(
        self,
        stream: Any,
        *,
        kind: str,
        severity: float,
        start: pd.Timestamp,
        end: pd.Timestamp,
        roles: Mapping[str, Sequence[str]],
    ) -> Any: ...


class PlausibilityDetector:
    """A05's deterministic checks on the harness.

    ``src.plausibility.offline`` rebuilds the arrived-sample stream the live
    evaluator sees; this runs a fresh :class:`PlausibilityEvaluator` over it
    and puts each *confirmed* detection (A05.6's confirmation is its
    persistence gate, so the output is pre-gated) on the first grid row at or
    after it. A session with no observed checked channel is withheld. There is
    no score, so no VUS-PR and no budget threshold.
    """

    name = "plausibility"

    def __init__(self, config: CanaryConfig, *, bindings: Any, did_map: Any) -> None:
        from src.plausibility.limits import resolve_limits_for_config

        self.config = config
        self.bindings = bindings
        self.did_map = did_map
        self.catalog = resolve_limits_for_config(config, bindings=bindings, did_map=did_map)

    @property
    def names(self) -> tuple[str, ...]:
        return (self.name,)

    def load(self, session_dir: Path) -> Any:
        from src.plausibility.offline import load_stream

        return load_stream(
            session_dir,
            self.config,
            self.catalog,
            bindings=self.bindings,
            did_map=self.did_map,
        )

    def score_stream(self, stream: Any, timestamps: pd.Series) -> dict[str, DetectorOutput]:
        from src.plausibility.evaluator import PlausibilityEvaluator
        from src.plausibility.offline import epoch_seconds, run_evaluator

        n = len(timestamps)
        evaluator = PlausibilityEvaluator(self.catalog, session=str(stream.name))
        records = run_evaluator(evaluator, stream)
        if evaluator.coverage_summary()["observed_channels_checked"] == 0:
            empty = np.full(n, None, dtype=object)
            return {self.name: DetectorOutput(empty, np.full(n, np.nan), pre_gated=True)}
        grid = epoch_seconds(pd.Series(timestamps).reset_index(drop=True))
        verdict = np.full(n, False, dtype=object)
        for record in records:
            if record.state != "confirmed":
                continue
            at = pd.Timestamp(record.detected_at).timestamp()
            row = int(np.searchsorted(grid, at, side="left"))
            if row < n and grid[row] - at <= 5.0:
                verdict[row] = True
        return {self.name: DetectorOutput(verdict, np.full(n, np.nan), pre_gated=True)}

    def window(
        self,
        stream: Any,
        start: pd.Timestamp,
        end: pd.Timestamp,
        *,
        channels: Sequence[str] | None = None,
    ) -> Any:
        """Samples in ``[start, end)``; with ``channels``, only their answer
        groups — nothing else can change a detection on them (A05.8's own
        restriction), and without broadcast frames it is a tenth of the
        samples."""
        from src.plausibility.limits import channel_group

        times = stream.samples["t"].to_numpy()
        keep = (times >= start.timestamp()) & (times < end.timestamp())
        if channels is not None:
            groups = {channel_group(c) for c in channels}
            keep &= stream.samples["channel"].map(channel_group).isin(groups).to_numpy()
        return stream.with_samples(
            stream.samples.loc[keep].reset_index(drop=True), stream.modes[keep]
        )

    def inject(
        self,
        stream: Any,
        *,
        kind: str,
        severity: float,
        start: pd.Timestamp,
        end: pd.Timestamp,
        roles: Mapping[str, Sequence[str]],
    ) -> Any:
        from src.plausibility.offline import inject_realistic_samples

        faulted, _report = inject_realistic_samples(
            stream,
            kind=kind,
            severity=severity,
            start=start.timestamp(),
            end=end.timestamp(),
            roles=roles,
            bindings=self.bindings,
        )
        return faulted


#: Harness detector name -> the ``AnomalyEvent.source`` its configured role
#: (``models.roles``, A06.5) is keyed by. ``fused`` needs an alert source to
#: fire and is always an alert; baselines and plausibility are candidates
#: being graded as alerters.
_ROLE_SOURCE = {
    "isolation_forest": "isolation_forest",
    "sequence": "lstm_autoencoder",
    "sequence_global": "lstm_autoencoder",
}


def detector_roles(config: CanaryConfig, names: Sequence[str]) -> dict[str, str]:
    """``alert`` or ``evidence`` per harness detector, from the configured roles.

    An ``evidence`` detector's episodes are not alerts: the harness still
    reports them, but never counts them against the alert budget.
    """
    by_source = config.models.roles.by_source()
    return {name: str(by_source.get(_ROLE_SOURCE.get(name, ""), "alert")) for name in names}


def raw_channels(columns: Sequence[str]) -> tuple[str, ...]:
    """The raw channels among model columns (rolling siblings dropped)."""
    return tuple(dict.fromkeys(c for c in columns if _base_channel(c) == c))


def suppressed_for(trained: TrainedSequenceModel, config: CanaryConfig) -> tuple[str, ...]:
    """The serve-time suppress list resolved against the model's columns."""
    block = getattr(config.models, str(trained.manifest.model_type), None)
    names = tuple(getattr(block, "suppress_channels", ()) or ())
    return expand_suppress_channels(names, tuple(trained.feature_columns))


__all__ = [
    "BatchedSequenceScorer",
    "DeployedStack",
    "Detector",
    "DetectorOutput",
    "PCABaseline",
    "PlausibilityDetector",
    "RawStreamDetector",
    "ParityReport",
    "RobustZBaseline",
    "ScoreModel",
    "SequenceScores",
    "ThresholdedScore",
    "UntrainedAutoencoder",
    "detector_roles",
    "raw_channels",
    "sequence_parity",
    "suppressed_for",
]
