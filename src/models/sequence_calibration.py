"""Sequence-detector threshold calibration and its on-disk format (R04.11).

:class:`ReconstructionErrorDistribution` and
:class:`SequenceThresholdCalibration` are the manifest threshold block every
sequence backbone writes and the live
:class:`~src.models.inference.SequenceInferenceEngine` parses, so serve imports
them from here and never from the training module. The scoring rule applied
over these thresholds lives beside it in :mod:`src.models.sequence_scoring`;
calibration groups channels into families with the same
:func:`~src.models.sequence_scoring.family_index` serve scores them by.

Torch is imported lazily (only :func:`calibrate_sequence_thresholds` runs a
model).
"""

from __future__ import annotations

import hashlib
import json
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any

import numpy as np
import pandas as pd

from src.columns import MODE_COLUMN
from src.models.sequence_dataset import SequenceWindowDataset
from src.models.sequence_device import dataloader_kwargs_for_device, torch_training_modules
from src.models.sequence_scoring import family_index
from src.timefmt import format_timestamp as _format_timestamp

#: Minimum validation-window count for a per-mode reconstruction-error band to
#: be persisted. Below this the mode falls back to the global threshold at
#: inference (mirrors the IF calibration floor).
MIN_CALIBRATION_SAMPLES = 10


#: A02.12 — a channel observed in no calibration window, or *rarely* observed
#: in the calibration or the training windows, gets no threshold: it is recorded
#: in ``uncalibrated_channels`` with the reason and its threshold is 0.0, which
#: the serving engine reads as "not scored". A threshold from zero observations
#: was 0.0 in the 2026-09-20 artifact and flagged any nonzero error (1,525 of
#: 1,554 family flags on one capture); one from a handful is that handful's
#: maximum. "Rarely" is relative *and* absolute: fewer than
#: ``MIN_OBSERVED_BLOCKS`` independent windows AND fewer than
#: ``RARE_OBSERVED_FRACTION`` of the windows available — a channel present in
#: every window of a small dataset is not rare, one present in 5 of 200 is.
MIN_OBSERVED_BLOCKS = MIN_CALIBRATION_SAMPLES


RARE_OBSERVED_FRACTION = 0.5


#: A02.13 (operator decision 2026-10-13) — a family whose every independent
#: calibration block maximum is the same value has no spread to put a band on:
#: ``mean + k * 0`` (or any quantile) is that value whatever ``k`` is, so a
#: later error one float ulp higher flags. `_frame`'s periodic rows in the
#: FP-benchmark tests did this to ``speed`` (8 of 17 held-out windows flagged
#: at k = 1000). Such a family is uncalibrated, like A02.12's unobserved ones;
#: the band is never floored to a width nobody measured.
ZERO_SPREAD_REASON_PREFIX = "zero spread"


#: Reason recorded for an ``engine_response`` context input (A03.2).
CONTEXT_UNCALIBRATED_REASON = "context input: conditions the encoder, never scored (A03.2)"


@dataclass(frozen=True)
class EpochLoss:
    """Training and validation loss for one epoch (the manifest ``history``;
    here, not in the training module, so the registry that parses manifests
    stays training-free -- R04.11)."""

    epoch: int
    train_loss: float
    validation_loss: float | None

    def to_dict(self) -> dict[str, int | float | None]:
        return {
            "epoch": self.epoch,
            "train_loss": self.train_loss,
            "validation_loss": self.validation_loss,
        }

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> EpochLoss:
        raw_validation = data.get("validation_loss")
        return cls(
            epoch=int(data["epoch"]),
            train_loss=float(data["train_loss"]),
            validation_loss=None if raw_validation is None else float(raw_validation),
        )


@dataclass(frozen=True)
class ReconstructionErrorDistribution:
    """Summary statistics and threshold for one reconstruction-error distribution."""

    count: int
    mean: float
    std: float
    minimum: float
    maximum: float
    p50: float
    p90: float
    p95: float
    p99: float
    threshold: float

    def to_dict(self) -> dict[str, int | float]:
        return {
            "count": self.count,
            "mean": self.mean,
            "std": self.std,
            "minimum": self.minimum,
            "maximum": self.maximum,
            "p50": self.p50,
            "p90": self.p90,
            "p95": self.p95,
            "p99": self.p99,
            "threshold": self.threshold,
        }

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> ReconstructionErrorDistribution:
        return cls(
            count=int(data["count"]),
            mean=float(data["mean"]),
            std=float(data["std"]),
            minimum=float(data["minimum"]),
            maximum=float(data["maximum"]),
            p50=float(data["p50"]),
            p90=float(data["p90"]),
            p95=float(data["p95"]),
            p99=float(data["p99"]),
            threshold=float(data["threshold"]),
        )


@dataclass(frozen=True)
class SequenceThresholdCalibration:
    """Global, per-channel, and per-mode reconstruction-error thresholds.

    The on-disk manifest threshold format for every sequence backbone.
    """

    threshold_k: float
    calibrated_at: str
    training_data_hash: str
    window_count: int
    global_error: ReconstructionErrorDistribution
    channel_errors: Mapping[str, ReconstructionErrorDistribution]
    #: Per-operating-mode distribution, computed on each window's end-row mode.
    #: ``None`` when no mode cleared :data:`MIN_CALIBRATION_SAMPLES`; inference
    #: then falls back to ``global_error.threshold``.
    per_mode_errors: Mapping[str, ReconstructionErrorDistribution] | None = None
    #: When set, thresholds were taken as this quantile of the error
    #: distribution instead of ``mean + k·σ`` (recorded for provenance —
    #: the threshold values themselves are already baked in).
    threshold_quantile: float | None = None
    #: Errors aggregated by physical signal family (raw + rolling siblings).
    #: ``family_max_score`` calibrates the maximum normalized family error,
    #: preserving localized faults without an uncorrected many-channel OR.
    family_errors: Mapping[str, ReconstructionErrorDistribution] | None = None
    family_max_score: ReconstructionErrorDistribution | None = None
    #: A02.12 — channel -> why it has no threshold (never, or too rarely,
    #: observed in calibration or in training). Their ``channel_errors`` entry
    #: is a zero distribution (threshold 0.0) and the global, per-mode and
    #: family thresholds were computed without them, exactly as the serving
    #: engine scores them (out of the score mask).
    uncalibrated_channels: Mapping[str, str] | None = None
    #: Families left without a threshold: no calibrated member (A02.12), or
    #: zero spread -- every block maximum the same float (A02.13).
    uncalibrated_families: Mapping[str, str] | None = None
    #: A02.3 — the distributions are per-block maxima over non-overlapping
    #: blocks of this many rows, pooled across sessions: independent samples,
    #: not ~30x-overlapping stride-1 windows. ``None`` on older manifests.
    block_rows: int | None = None
    #: Independent blocks behind ``global_error`` (its ``count``); the
    #: finest quantile these thresholds can resolve is about ``1 - 10/n``.
    effective_window_count: int | None = None
    #: A02.3 — the alert level chosen from the alert budget on held-out
    #: drives (:mod:`src.models.budget_level`): the budget, the drives, the
    #: level every verdict band was scaled by, and the rate it achieved there.
    #: ``None`` when no level was set (older manifests, standalone trainers).
    alert_budget: Mapping[str, Any] | None = None

    def threshold_for_mode(self, mode: str | None) -> float:
        """The global-score band for a window whose last row is in ``mode``.

        The mode's held-out band when calibration kept one (Phase 7.5), else
        the single global band -- also for ``None`` and for manifests from
        before per-mode bands existed.
        """
        if mode is not None and self.per_mode_errors:
            band = self.per_mode_errors.get(mode)
            if band is not None:
                return band.threshold
        return self.global_error.threshold

    def to_dict(self) -> dict[str, Any]:
        out: dict[str, Any] = {
            "threshold_k": self.threshold_k,
            "calibrated_at": self.calibrated_at,
            "training_data_hash": self.training_data_hash,
            "window_count": self.window_count,
            "global_error": self.global_error.to_dict(),
            "channel_errors": {
                channel: distribution.to_dict()
                for channel, distribution in self.channel_errors.items()
            },
        }
        if self.per_mode_errors is not None:
            out["per_mode_errors"] = {
                mode: distribution.to_dict() for mode, distribution in self.per_mode_errors.items()
            }
        if self.threshold_quantile is not None:
            out["threshold_quantile"] = self.threshold_quantile
        if self.family_errors is not None:
            out["family_errors"] = {
                family: distribution.to_dict()
                for family, distribution in self.family_errors.items()
            }
        if self.family_max_score is not None:
            out["family_max_score"] = self.family_max_score.to_dict()
        if self.uncalibrated_channels is not None:
            out["uncalibrated_channels"] = dict(self.uncalibrated_channels)
        if self.uncalibrated_families is not None:
            out["uncalibrated_families"] = dict(self.uncalibrated_families)
        if self.block_rows is not None:
            out["block_rows"] = self.block_rows
        if self.effective_window_count is not None:
            out["effective_window_count"] = self.effective_window_count
        if self.alert_budget is not None:
            out["alert_budget"] = dict(self.alert_budget)
        return out

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> SequenceThresholdCalibration:
        raw_per_mode = data.get("per_mode_errors")
        per_mode = (
            None
            if raw_per_mode is None
            else {
                str(mode): ReconstructionErrorDistribution.from_dict(distribution)
                for mode, distribution in dict(raw_per_mode).items()
            }
        )
        raw_quantile = data.get("threshold_quantile")
        raw_family_errors = data.get("family_errors")
        raw_family_max = data.get("family_max_score")
        return cls(
            threshold_k=float(data["threshold_k"]),
            calibrated_at=str(data["calibrated_at"]),
            training_data_hash=str(data["training_data_hash"]),
            window_count=int(data["window_count"]),
            global_error=ReconstructionErrorDistribution.from_dict(data["global_error"]),
            channel_errors={
                str(channel): ReconstructionErrorDistribution.from_dict(distribution)
                for channel, distribution in dict(data["channel_errors"]).items()
            },
            per_mode_errors=per_mode,
            threshold_quantile=None if raw_quantile is None else float(raw_quantile),
            family_errors=(
                None
                if raw_family_errors is None
                else {
                    str(family): ReconstructionErrorDistribution.from_dict(distribution)
                    for family, distribution in dict(raw_family_errors).items()
                }
            ),
            family_max_score=(
                None
                if raw_family_max is None
                else ReconstructionErrorDistribution.from_dict(raw_family_max)
            ),
            uncalibrated_channels=_optional_reasons(data.get("uncalibrated_channels")),
            uncalibrated_families=_optional_reasons(data.get("uncalibrated_families")),
            block_rows=None if data.get("block_rows") is None else int(data["block_rows"]),
            effective_window_count=(
                None
                if data.get("effective_window_count") is None
                else int(data["effective_window_count"])
            ),
            alert_budget=(None if data.get("alert_budget") is None else dict(data["alert_budget"])),
        )


def _optional_reasons(raw: Any) -> dict[str, str] | None:
    return None if raw is None else {str(k): str(v) for k, v in dict(raw).items()}


def uncalibrated_distribution() -> ReconstructionErrorDistribution:
    """The record of a channel with no calibration: threshold 0.0, count 0.

    The serving engine treats a non-positive threshold as "never observed in
    calibration" and keeps the channel out of the score (A02.12).
    """
    return ReconstructionErrorDistribution(
        count=0,
        mean=0.0,
        std=0.0,
        minimum=0.0,
        maximum=0.0,
        p50=0.0,
        p90=0.0,
        p95=0.0,
        p99=0.0,
        threshold=0.0,
    )


def calibrate_sequence_thresholds(
    model: Any,
    dataset: SequenceWindowDataset,
    *,
    feature_columns: tuple[str, ...],
    threshold_k: float,
    batch_size: int = 64,
    device: str = "cpu",
    calibrated_at: datetime | pd.Timestamp | str | None = None,
    threshold_quantile: float | None = None,
    training_observed_blocks: Mapping[str, int] | None = None,
    context_columns: Sequence[str] = (),
) -> SequenceThresholdCalibration:
    """Compute global / per-channel / per-family / per-mode thresholds.

    ``context_columns`` (A03.2) are encoder inputs that are never scored: they
    are recorded as uncalibrated with that reason and kept out of every
    distribution, as the serving engine keeps them out of the score.

    Per window: each channel's masked MAE over the timesteps where it was
    observed, and the global masked MAE over the *calibrated* channels' cells.
    Thresholds are ``mean + threshold_k · population_std`` (or, with
    ``threshold_quantile``, that quantile — see :func:`error_distribution`).

    **Only observed windows count (A02.12).** A channel's (and family's)
    distribution uses only windows where it was observed. A channel observed
    in fewer than :data:`MIN_OBSERVED_BLOCKS` independent calibration windows —
    or, via ``training_observed_blocks``, training windows — is recorded in
    ``uncalibrated_channels`` with the reason, gets threshold 0.0, and is left
    out of the global, per-mode and family distributions, which is how the
    serving engine scores it.

    **No band without spread (A02.13).** A family whose every independent
    block maximum is the same float is recorded in ``uncalibrated_families``
    with the tied value (:func:`zero_spread_reason`) and gets threshold 0.0,
    so serve leaves it out of the family path; its channels still score into
    the global error. When no family is left, ``family_max_score`` is
    ``None`` and the family path is off.

    **Independent windows (A02.3).** Stride-1 windows overlap by all but one
    row, so a 0.999 quantile over a session's ~1,000 windows is its maximum.
    Every distribution is taken over per-block maxima: the worst window ending
    in each non-overlapping ``sequence_length``-row block, pooled across
    sessions. ``effective_window_count`` records how many there were.
    """
    if threshold_k < 0:
        raise ValueError("threshold_k must be non-negative")
    if batch_size <= 0:
        raise ValueError("batch_size must be positive")
    if len(dataset) == 0:
        raise ValueError("no sequence windows available to calibrate thresholds")
    if tuple(dataset.feature_columns) != tuple(feature_columns):
        raise ValueError("calibration dataset feature columns do not match trained model")
    if dataset.sequence_length != int(model.sequence_length):
        raise ValueError("calibration dataset sequence_length does not match trained model")

    torch, _nn, _optim, data = torch_training_modules()
    device_obj = torch.device(device)
    loader = data.DataLoader(
        dataset,
        batch_size=batch_size,
        shuffle=False,
        **dataloader_kwargs_for_device(device),
    )
    model.to(device_obj)
    model.eval()

    sum_chunks: list[np.ndarray[Any, np.dtype[np.float64]]] = []
    count_chunks: list[np.ndarray[Any, np.dtype[np.float64]]] = []
    with torch.no_grad():
        for value, mask, delta in loader:
            value = value.to(device_obj, non_blocking=True)
            mask = mask.to(device_obj, non_blocking=True)
            delta = delta.to(device_obj, non_blocking=True)
            cell_errors = model.reconstruction_error(value, mask, delta, reduction="none")
            sum_chunks.append(cell_errors.sum(dim=1).detach().cpu().numpy().astype(np.float64))
            count_chunks.append(mask.sum(dim=1).detach().cpu().numpy().astype(np.float64))

    sums = np.concatenate(sum_chunks, axis=0)
    counts = np.concatenate(count_chunks, axis=0)
    columns = tuple(dataset.feature_columns)
    blocks = window_blocks(dataset)
    observed = counts > 0
    with np.errstate(invalid="ignore", divide="ignore"):
        channel_matrix = np.where(observed, sums / np.where(observed, counts, 1.0), np.nan)

    calibration_blocks = _observed_blocks(observed, blocks)
    available_blocks = len(set(blocks.tolist()))
    training_available = (
        max(training_observed_blocks.values(), default=0) if training_observed_blocks else 0
    )
    uncalibrated: dict[str, str] = {}
    context = set(context_columns)
    for index, column in enumerate(columns):
        if column in context:
            uncalibrated[column] = CONTEXT_UNCALIBRATED_REASON
            continue
        seen = int(calibration_blocks[index])
        trained = None if training_observed_blocks is None else training_observed_blocks.get(column)
        if seen == 0:
            uncalibrated[column] = "never observed in calibration"
        elif is_rarely_observed(seen, available_blocks):
            uncalibrated[column] = (
                f"observed in {seen} of {available_blocks} independent calibration windows"
            )
        elif trained is not None and is_rarely_observed(trained, training_available):
            uncalibrated[column] = (
                f"observed in {trained} of ~{training_available} independent training windows"
            )
    calibrated = np.array([column not in uncalibrated for column in columns], dtype=bool)
    if not calibrated.any():
        raise ValueError(
            "no channel was observed in enough independent calibration windows to calibrate"
        )

    global_cells = counts[:, calibrated].sum(axis=1)
    with np.errstate(invalid="ignore", divide="ignore"):
        global_values = np.where(
            global_cells > 0, sums[:, calibrated].sum(axis=1) / global_cells, np.nan
        )

    def distribution(
        values: np.ndarray[Any, np.dtype[np.float64]],
    ) -> ReconstructionErrorDistribution:
        return error_distribution(
            values, threshold_k=threshold_k, threshold_quantile=threshold_quantile
        )

    global_maxima = block_maxima(global_values, blocks)
    global_distribution = distribution(global_maxima)
    channel_distributions = {
        column: (
            distribution(block_maxima(channel_matrix[:, index], blocks))
            if calibrated[index]
            else uncalibrated_distribution()
        )
        for index, column in enumerate(columns)
    }

    # The same grouping every scorer uses (R04.2), so a family band here is
    # the band serve compares that family against.
    family_indexes = family_index(columns)
    family_distributions: dict[str, ReconstructionErrorDistribution] = {}
    uncalibrated_families: dict[str, str] = {}
    normalized_columns: list[np.ndarray[Any, np.dtype[np.float64]]] = []
    for family, indexes in family_indexes.items():
        members = [i for i in indexes if calibrated[i]]
        if not members:
            uncalibrated_families[family] = "no calibrated member channel"
            family_distributions[family] = uncalibrated_distribution()
            continue
        member_values = channel_matrix[:, members]
        present: np.ndarray[Any, np.dtype[np.bool_]] = np.asarray(
            np.isfinite(member_values).any(axis=1), dtype=bool
        )
        filled = np.where(np.isfinite(member_values), member_values, -np.inf)
        values = np.where(present, filled.max(axis=1), np.nan)
        family_distribution = distribution(block_maxima(values, blocks))
        if (reason := zero_spread_reason(family_distribution)) is not None:
            uncalibrated_families[family] = reason
            family_distributions[family] = uncalibrated_distribution()
            continue
        family_distributions[family] = family_distribution
        threshold = max(family_distribution.threshold, np.finfo(np.float64).eps)
        # An unobserved family contributes 0 to the serving max (its channel
        # scores are 0 there), so it does here too.
        normalized_columns.append(np.where(present, values / threshold, 0.0))
    # With no family left to score, the family path is off (the engine reads
    # a missing ``family_max_score`` that way) rather than banded on nothing.
    family_max_distribution = (
        distribution(block_maxima(np.max(np.column_stack(normalized_columns), axis=1), blocks))
        if normalized_columns
        else None
    )

    # Per-operating-mode global thresholds. The mode for a window is its last
    # row's mode (the row the score is attributed to); ``shuffle=False`` keeps
    # the loader order aligned with the windows. Modes below the floor fall
    # back to ``global_error`` at inference.
    per_mode_distributions: dict[str, ReconstructionErrorDistribution] = {}
    for mode, indexes in window_modes(dataset).items():
        mode_maxima = block_maxima(global_values[indexes], blocks[indexes])
        if mode_maxima.size < MIN_CALIBRATION_SAMPLES:
            continue
        per_mode_distributions[mode] = distribution(mode_maxima)

    return SequenceThresholdCalibration(
        threshold_k=threshold_k,
        calibrated_at=_format_timestamp(coerce_timestamp(calibrated_at)),
        training_data_hash=training_data_hash(dataset),
        window_count=len(dataset),
        global_error=global_distribution,
        channel_errors=channel_distributions,
        per_mode_errors=per_mode_distributions if per_mode_distributions else None,
        threshold_quantile=threshold_quantile,
        family_errors=family_distributions,
        family_max_score=family_max_distribution,
        uncalibrated_channels=uncalibrated,
        uncalibrated_families=uncalibrated_families,
        block_rows=int(dataset.sequence_length),
        effective_window_count=int(global_maxima.size),
    )


def zero_spread_reason(distribution: ReconstructionErrorDistribution) -> str | None:
    """Why ``distribution`` cannot carry a band, or ``None`` when it can (A02.13).

    Zero spread means every sample is the *same float* -- exact ties, not a
    small spread -- so any threshold taken from it sits on the tie itself.
    """
    if distribution.count == 0 or distribution.maximum != distribution.minimum:
        return None
    return (
        f"{ZERO_SPREAD_REASON_PREFIX}: all {distribution.count} independent calibration "
        f"windows peak at {distribution.maximum:.6g}, no band to set (A02.13)"
    )


def is_rarely_observed(observed_blocks: int, available_blocks: int) -> bool:
    """Fewer than :data:`MIN_OBSERVED_BLOCKS` independent windows *and* under
    :data:`RARE_OBSERVED_FRACTION` of those available (see the constants)."""
    return (
        observed_blocks < MIN_OBSERVED_BLOCKS
        and observed_blocks < RARE_OBSERVED_FRACTION * available_blocks
    )


def window_blocks(dataset: SequenceWindowDataset) -> np.ndarray[Any, np.dtype[np.int64]]:
    """Block id per window: its session and which non-overlapping
    ``sequence_length``-row block its last row falls in (A02.3)."""
    length = max(1, int(dataset.sequence_length))
    keys = [(window.session, (window.end_row - 1) // length) for window in dataset.windows]
    return np.asarray(pd.factorize(pd.Series(keys, dtype=object))[0], dtype=np.int64)


def window_modes(dataset: SequenceWindowDataset) -> dict[str, list[int]]:
    """Window indexes by the operating mode of each window's last row."""
    frame = dataset.frame
    if MODE_COLUMN not in frame.columns:
        return {}
    modes = frame[MODE_COLUMN].to_numpy()
    by_mode: dict[str, list[int]] = {}
    for index, window in enumerate(dataset.windows):
        raw = modes[window.end_row - 1]
        if raw is None or pd.isna(raw):
            continue
        by_mode.setdefault(str(raw), []).append(index)
    return by_mode


def block_maxima(
    values: np.ndarray[Any, np.dtype[np.float64]],
    blocks: np.ndarray[Any, np.dtype[np.int64]],
) -> np.ndarray[Any, np.dtype[np.float64]]:
    """Maximum finite value per block (blocks with no finite value dropped)."""
    finite = np.isfinite(values)
    if not finite.any():
        return np.zeros(0, dtype=np.float64)
    series = pd.Series(values[finite], index=blocks[finite])
    return series.groupby(level=0).max().to_numpy(dtype=np.float64)


def _observed_blocks(
    observed: np.ndarray[Any, np.dtype[np.bool_]],
    blocks: np.ndarray[Any, np.dtype[np.int64]],
) -> np.ndarray[Any, np.dtype[np.int64]]:
    """Per channel: how many blocks hold at least one window observing it."""
    if observed.size == 0:
        return np.zeros(observed.shape[1] if observed.ndim == 2 else 0, dtype=np.int64)
    frame = pd.DataFrame(observed, index=blocks)
    return frame.groupby(level=0).any().sum(axis=0).to_numpy(dtype=np.int64)


def observed_block_counts(dataset: SequenceWindowDataset) -> dict[str, int]:
    """Per channel: independent windows (blocks) in which it was observed.

    Read from the dataset's mask alone — no model needed — so training can
    record how often each channel was seen before any calibration runs.
    """
    if len(dataset) == 0:
        return {column: 0 for column in dataset.feature_columns}
    mask = np.asarray(dataset._mask, dtype=np.float64)
    prefix = np.vstack([np.zeros((1, mask.shape[1])), np.cumsum(mask, axis=0)])
    starts = np.array([window.start_row for window in dataset.windows], dtype=np.int64)
    ends = np.array([window.end_row for window in dataset.windows], dtype=np.int64)
    observed = (prefix[ends] - prefix[starts]) > 0
    counts = _observed_blocks(observed, window_blocks(dataset))
    return {column: int(counts[i]) for i, column in enumerate(dataset.feature_columns)}


def error_distribution(
    values: np.ndarray[Any, np.dtype[np.float64]],
    *,
    threshold_k: float,
    threshold_quantile: float | None = None,
) -> ReconstructionErrorDistribution:
    """Summarize an error distribution and pick its alert threshold.

    Default threshold is ``mean + k·σ``; pass ``threshold_quantile`` to use
    a quantile instead. Reconstruction errors are right-skewed and stride-1
    windows are heavily autocorrelated, so σ is a poor tail estimator and
    ``k`` has no calibrated false-positive meaning — a quantile (e.g. 0.999
    of held-out errors) targets the alert budget directly, matching how the
    IF side calibrates to ``target_flag_rate``.
    """
    if threshold_quantile is not None and not 0.0 < threshold_quantile < 1.0:
        raise ValueError("threshold_quantile must be in (0, 1)")
    if values.size == 0:
        raise ValueError("cannot calibrate thresholds from an empty error distribution")
    finite = values[np.isfinite(values)]
    if finite.size == 0:
        raise ValueError("cannot calibrate thresholds from non-finite reconstruction errors")
    mean = float(np.mean(finite))
    std = float(np.std(finite, ddof=0))
    p50, p90, p95, p99 = np.quantile(finite, [0.5, 0.9, 0.95, 0.99])
    if threshold_quantile is not None:
        threshold = float(np.quantile(finite, threshold_quantile))
    else:
        threshold = float(mean + threshold_k * std)
    return ReconstructionErrorDistribution(
        count=int(finite.size),
        mean=mean,
        std=std,
        minimum=float(np.min(finite)),
        maximum=float(np.max(finite)),
        p50=float(p50),
        p90=float(p90),
        p95=float(p95),
        p99=float(p99),
        threshold=threshold,
    )


def training_data_hash(dataset: SequenceWindowDataset) -> str:
    cached = getattr(dataset, "_training_data_hash", None)
    if cached is not None:
        return str(cached)
    digest = hashlib.sha256()
    digest.update(
        json.dumps(
            {
                "feature_columns": list(dataset.feature_columns),
                "sequence_length": dataset.sequence_length,
                "windows": [window.to_dict() for window in dataset.windows],
                "hash_layout": "contiguous-frame-v2",
            },
            sort_keys=True,
        ).encode("utf-8")
    )
    # Windows heavily overlap. Hash the model-ready frame once instead of
    # materializing and hashing the same row sequence_length times.
    values = dataset.frame.loc[:, list(dataset.feature_columns)].to_numpy(dtype=np.float64)
    digest.update(np.ascontiguousarray(values).tobytes())
    result = f"sha256:{digest.hexdigest()}"
    dataset._training_data_hash = result
    return result


def coerce_timestamp(value: datetime | pd.Timestamp | str | None) -> pd.Timestamp:
    if value is None:
        return pd.Timestamp(datetime.now(tz=UTC))
    timestamp = pd.Timestamp(value)
    if timestamp.tzinfo is None:
        return timestamp.tz_localize("UTC")
    return timestamp.tz_convert("UTC")


#: Pre-R04.11 private name.
_block_maxima = block_maxima

__all__ = [
    "CONTEXT_UNCALIBRATED_REASON",
    "EpochLoss",
    "MIN_CALIBRATION_SAMPLES",
    "MIN_OBSERVED_BLOCKS",
    "RARE_OBSERVED_FRACTION",
    "ZERO_SPREAD_REASON_PREFIX",
    "ReconstructionErrorDistribution",
    "SequenceThresholdCalibration",
    "block_maxima",
    "calibrate_sequence_thresholds",
    "coerce_timestamp",
    "error_distribution",
    "is_rarely_observed",
    "observed_block_counts",
    "training_data_hash",
    "uncalibrated_distribution",
    "window_blocks",
    "window_modes",
    "zero_spread_reason",
]
