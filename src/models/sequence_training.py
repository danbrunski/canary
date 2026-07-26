"""Shared, model-agnostic training + threshold calibration for sequence detectors.

Both the GRU-D LSTM-AE and the TCN-AE train identically — masked MSE over
``(value, mask, delta)`` windows, an optional validation split with early
stopping, then masked reconstruction-error threshold calibration — because both
expose the same ``forward(value, mask, delta)`` and ``reconstruction_error``
surface (see :mod:`src.models.sequence_protocol`). That machinery lives here so
each backbone module owns only its architecture, manifest, and save/load.

The calibration dataclasses (:class:`EpochLoss`,
:class:`ReconstructionErrorDistribution`, :class:`SequenceThresholdCalibration`)
are the on-disk manifest threshold format the live
:class:`~src.models.inference.SequenceInferenceEngine` parses, so both backbones
write byte-identical threshold blocks and the engine needs no per-backbone
branching.

Imports stay lazy so non-ML tooling can import ``src.models`` without Torch.
"""

from __future__ import annotations

import hashlib
import json
import os
import platform
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any, Protocol

import numpy as np
import pandas as pd

from src.models.sequence_dataset import MissingSequenceDependencyError, SequenceWindowDataset
from src.preprocessing.segmenter import MODE_COLUMN
from src.timefmt import format_timestamp as _format_timestamp

#: Minimum validation-window count for a per-mode reconstruction-error band to
#: be persisted. Below this the mode falls back to the global threshold at
#: inference (mirrors the IF calibration floor).
MIN_CALIBRATION_SAMPLES = 10


class SequenceModelLike(Protocol):
    """The reconstruction surface calibration needs from any sequence model."""

    sequence_length: int

    def __call__(self, value: Any, mask: Any, delta: Any) -> Any: ...

    def reconstruction_error(
        self, value: Any, mask: Any, delta: Any, *, reduction: str = ...
    ) -> Any: ...


@dataclass(frozen=True)
class EpochLoss:
    """Training and validation loss for one epoch."""

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
        )


def masked_mse_loss(reconstruction: Any, value: Any, mask: Any) -> Any:
    """Masked MSE: ``sum(m ⊙ (recon − x)²) / sum(m)`` over the whole batch.

    Only observed cells (``mask == 1``) count, so the model is never trained to
    reconstruct a forward-filled fill. A degenerate all-stale batch (sum(m)==0)
    is clamped to a denominator of 1 so the loss is 0 rather than NaN.
    """
    squared = ((reconstruction - value) ** 2) * mask
    denominator = mask.sum().clamp(min=1.0)
    return squared.sum() / denominator


def train_one_epoch(
    model: Any,
    loader: Any,
    *,
    optimizer: Any,
    device: Any,
    amp_dtype: Any | None = None,
    grad_scaler: Any | None = None,
) -> float:
    torch = torch_module()
    model.train()
    losses: list[float] = []
    for value, mask, delta in loader:
        value = value.to(device, non_blocking=True)
        mask = mask.to(device, non_blocking=True)
        delta = delta.to(device, non_blocking=True)
        optimizer.zero_grad(set_to_none=True)
        with torch.autocast(
            device_type=device.type, dtype=amp_dtype, enabled=amp_dtype is not None
        ):
            reconstruction = model(value, mask, delta)
            loss = masked_mse_loss(reconstruction, value, mask)
        if grad_scaler is not None:
            grad_scaler.scale(loss).backward()
            grad_scaler.step(optimizer)
            grad_scaler.update()
        else:
            loss.backward()
            optimizer.step()
        losses.append(float(loss.detach().cpu().item()))
    return float(np.mean(losses))


def evaluate_loss(model: Any, loader: Any, *, device: Any, amp_dtype: Any | None = None) -> float:
    torch = torch_module()
    model.eval()
    losses: list[float] = []
    with torch.no_grad():
        for value, mask, delta in loader:
            value = value.to(device, non_blocking=True)
            mask = mask.to(device, non_blocking=True)
            delta = delta.to(device, non_blocking=True)
            with torch.autocast(
                device_type=device.type, dtype=amp_dtype, enabled=amp_dtype is not None
            ):
                reconstruction = model(value, mask, delta)
                loss = masked_mse_loss(reconstruction, value, mask)
            losses.append(float(loss.detach().cpu().item()))
    return float(np.mean(losses))


def cpu_state_dict(model: Any) -> dict[str, Any]:
    return {key: value.detach().cpu().clone() for key, value in model.state_dict().items()}


def split_dataset(
    dataset: SequenceWindowDataset,
    *,
    validation_fraction: float,
    random_state: int,
) -> tuple[Any, Any | None]:
    """Leakage-aware train/validation split into ``torch.utils.data.Subset`` views.

    Windows are built at stride 1, so adjacent windows share T−1 of T rows.
    A random window-level split therefore put each validation window between
    two train windows containing ~97% of its content — validation loss
    tracked train loss almost exactly, early stopping rarely stopped for the
    right reason, and ``best_validation_loss`` was optimistic. (The same
    mechanism inflated in-sample threshold calibration to FPR ≈ 1.0 on
    unseen drives before calibration moved to held-out sessions.)

    Strategy: hold out whole *sessions* (seeded shuffle, smallest-first so
    one long drive can't swallow the entire validation budget) until
    ``validation_fraction`` of windows is reached. A single-session corpus
    falls back to a contiguous tail block with a ``sequence_length`` gap of
    windows dropped between train and validation so they share no rows.
    """
    torch, _nn, _optim, data = torch_training_modules()
    n = len(dataset)
    validation_count = 0
    if validation_fraction > 0 and n > 1:
        validation_count = int(round(n * validation_fraction))
        validation_count = max(1, validation_count)
        validation_count = min(validation_count, n - 1)
    if validation_count == 0:
        return data.Subset(dataset, list(range(n))), None

    window_sessions = [window.session for window in dataset.windows]
    session_order = list(dict.fromkeys(window_sessions))
    if len(session_order) > 1:
        counts = {session: 0 for session in session_order}
        for session in window_sessions:
            counts[session] += 1
        generator = torch.Generator().manual_seed(random_state)
        shuffled = [
            session_order[i]
            for i in torch.randperm(len(session_order), generator=generator).tolist()
        ]
        # Smallest-first within the shuffle so the holdout lands near the
        # requested fraction instead of jumping to one huge drive.
        shuffled.sort(key=lambda session: counts[session])
        validation_sessions: set[str] = set()
        held = 0
        for session in shuffled:
            if held >= validation_count or len(validation_sessions) == len(session_order) - 1:
                break
            validation_sessions.add(session)
            held += counts[session]
        train_indexes = [
            i for i, session in enumerate(window_sessions) if session not in validation_sessions
        ]
        validation_indexes = [
            i for i, session in enumerate(window_sessions) if session in validation_sessions
        ]
        if train_indexes and validation_indexes:
            return data.Subset(dataset, train_indexes), data.Subset(dataset, validation_indexes)

    # Single session (or degenerate multi-session split): contiguous tail
    # block, with a sequence_length-window gap so train and validation
    # windows share no underlying rows.
    gap = int(dataset.sequence_length)
    validation_start = n - validation_count
    train_end = max(1, validation_start - gap)
    train_indexes = list(range(train_end))
    validation_indexes = list(range(validation_start, n))
    return data.Subset(dataset, train_indexes), data.Subset(dataset, validation_indexes)


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
) -> SequenceThresholdCalibration:
    """Compute global / per-channel / per-mode reconstruction-error thresholds.

    Global errors are per-window masked MAE over all cells; per-channel errors
    are per-window masked MAE over timesteps; thresholds are
    ``mean + threshold_k · population_std`` (or, with ``threshold_quantile``,
    that quantile of the errors — see :func:`error_distribution`).
    Backbone-agnostic — any model with
    ``reconstruction_error(value, mask, delta, reduction=...)`` works.
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

    global_chunks: list[np.ndarray[Any, np.dtype[np.float64]]] = []
    channel_chunks: list[np.ndarray[Any, np.dtype[np.float64]]] = []
    with torch.no_grad():
        for value, mask, delta in loader:
            value = value.to(device_obj, non_blocking=True)
            mask = mask.to(device_obj, non_blocking=True)
            delta = delta.to(device_obj, non_blocking=True)
            global_errors = model.reconstruction_error(value, mask, delta, reduction="sample")
            channel_errors = model.reconstruction_error(value, mask, delta, reduction="channel")
            global_chunks.append(global_errors.detach().cpu().numpy().astype(np.float64))
            channel_chunks.append(channel_errors.detach().cpu().numpy().astype(np.float64))

    global_values = np.concatenate(global_chunks)
    channel_matrix = np.concatenate(channel_chunks, axis=0)
    global_distribution = error_distribution(
        global_values, threshold_k=threshold_k, threshold_quantile=threshold_quantile
    )
    channel_distributions = {
        column: error_distribution(
            channel_matrix[:, index],
            threshold_k=threshold_k,
            threshold_quantile=threshold_quantile,
        )
        for index, column in enumerate(dataset.feature_columns)
    }
    family_indexes: dict[str, list[int]] = {}
    for index, column in enumerate(dataset.feature_columns):
        family_indexes.setdefault(_feature_family(column), []).append(index)
    family_values = {
        family: np.max(channel_matrix[:, indexes], axis=1)
        for family, indexes in family_indexes.items()
    }
    family_distributions = {
        family: error_distribution(
            values, threshold_k=threshold_k, threshold_quantile=threshold_quantile
        )
        for family, values in family_values.items()
    }
    normalized_family_matrix = np.column_stack(
        [
            values / max(family_distributions[family].threshold, np.finfo(np.float64).eps)
            for family, values in family_values.items()
        ]
    )
    family_max_distribution = error_distribution(
        np.max(normalized_family_matrix, axis=1),
        threshold_k=threshold_k,
        threshold_quantile=threshold_quantile,
    )

    # Per-operating-mode global thresholds. The mode for a window is its last
    # row's mode (the row the score is attributed to); ``shuffle=False`` keeps
    # the loader order aligned with ``global_values``. Modes below the floor
    # fall back to ``global_error`` at inference.
    per_mode_distributions: dict[str, ReconstructionErrorDistribution] = {}
    frame = dataset.frame
    if MODE_COLUMN in frame.columns:
        by_mode: dict[str, list[int]] = {}
        for index, window in enumerate(dataset.windows):
            raw = frame.iloc[window.end_row - 1].get(MODE_COLUMN)
            if raw is None or pd.isna(raw):
                continue
            by_mode.setdefault(str(raw), []).append(index)
        for mode, indexes in by_mode.items():
            # The floor counts EFFECTIVE (non-overlapping) windows: at
            # stride 1, ten windows share almost all their rows — ten
            # seconds of one idle is not enough evidence for a mode to own
            # its own mean+k·σ band.
            starts = [dataset.windows[i].start_row for i in indexes]
            if _non_overlapping_window_count(starts, dataset.sequence_length) < (
                MIN_CALIBRATION_SAMPLES
            ):
                continue
            per_mode_distributions[mode] = error_distribution(
                global_values[indexes],
                threshold_k=threshold_k,
                threshold_quantile=threshold_quantile,
            )

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
    )


def _feature_family(column: str) -> str:
    for suffix in ("__mean", "__std", "__roc"):
        if column.endswith(suffix):
            return column[: -len(suffix)]
    return column


def _non_overlapping_window_count(start_rows: list[int], sequence_length: int) -> int:
    """Largest set of mutually non-overlapping windows among ``start_rows``.

    Greedy earliest-end-first selection — the standard interval-scheduling
    argument makes this exact for fixed-length windows.
    """
    count = 0
    next_free: int | None = None
    for start in sorted(start_rows):
        if next_free is None or start >= next_free:
            count += 1
            next_free = start + max(1, int(sequence_length))
    return count


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


def training_sessions(dataset: SequenceWindowDataset) -> tuple[str, ...]:
    return tuple(sorted({window.session for window in dataset.windows}))


def _window_observed_fractions(dataset: SequenceWindowDataset) -> np.ndarray:
    cells_per_window = dataset.sequence_length * len(dataset.feature_columns)
    row_observed = dataset._mask.sum(axis=1, dtype=np.float64)
    prefix = np.concatenate(([0.0], np.cumsum(row_observed)))
    return np.asarray(
        [
            (prefix[window.end_row] - prefix[window.start_row]) / cells_per_window
            for window in dataset.windows
        ],
        dtype=np.float64,
    )


def dataset_observation_health(dataset: SequenceWindowDataset) -> dict[str, Any]:
    """Summarize real observed-cell coverage across overlapping windows."""
    fractions = _window_observed_fractions(dataset)
    if fractions.size == 0:
        return {"window_count": 0, "minimum": 0.0, "p10": 0.0, "p50": 0.0, "mean": 0.0}
    return {
        "window_count": int(fractions.size),
        "minimum": float(fractions.min()),
        "p10": float(np.quantile(fractions, 0.1)),
        "p50": float(np.quantile(fractions, 0.5)),
        "mean": float(fractions.mean()),
        "zero_observation_windows": int(np.count_nonzero(fractions == 0.0)),
    }


def validate_dataset_observations(
    dataset: SequenceWindowDataset,
    *,
    minimum_window_fraction: float,
    minimum_usable_fraction: float,
    context: str,
) -> dict[str, Any]:
    health = dataset_observation_health(dataset)
    fractions = _window_observed_fractions(dataset)
    usable_fraction = float(np.mean(fractions >= minimum_window_fraction))
    health["minimum_window_observed_fraction"] = minimum_window_fraction
    health["usable_window_fraction"] = usable_fraction
    if usable_fraction < minimum_usable_fraction:
        raise ValueError(
            f"{context} observation coverage is unusable: {usable_fraction:.1%} of windows "
            f"meet the {minimum_window_fraction:.1%} observed-cell floor; require "
            f"{minimum_usable_fraction:.1%}"
        )
    return health


def min_window_timestamp(dataset: SequenceWindowDataset) -> str | None:
    if not dataset.windows:
        return None
    timestamps = [pd.Timestamp(window.start_timestamp) for window in dataset.windows]
    return _format_timestamp(min(timestamps))


def max_window_timestamp(dataset: SequenceWindowDataset) -> str | None:
    if not dataset.windows:
        return None
    timestamps = [pd.Timestamp(window.end_timestamp) for window in dataset.windows]
    return _format_timestamp(max(timestamps))


def coerce_timestamp(value: datetime | pd.Timestamp | str | None) -> pd.Timestamp:
    if value is None:
        return pd.Timestamp(datetime.now(tz=UTC))
    timestamp = pd.Timestamp(value)
    if timestamp.tzinfo is None:
        return timestamp.tz_localize("UTC")
    return timestamp.tz_convert("UTC")


def torch_training_modules() -> tuple[Any, Any, Any, Any]:
    try:
        import torch
        from torch import nn, optim
        from torch.utils import data
    except ImportError as exc:
        raise MissingSequenceDependencyError(
            "torch is required to train sequence detectors; run scripts/install_deps.py "
            "or install the 'ml' optional dependency"
        ) from exc
    return torch, nn, optim, data


def torch_module() -> Any:
    try:
        import torch
    except ImportError as exc:
        raise MissingSequenceDependencyError(
            "torch is required for sequence-detector checkpoints"
        ) from exc
    return torch


def default_training_device() -> str:
    """``"cuda"`` when a CUDA device is available, otherwise ``"cpu"``.

    The training CLIs (``train_lstm``, ``train_tcn``, ``retrain``,
    ``sequence_bakeoff``) use this as their ``--device`` default so the dev
    laptop's GPU is exercised automatically while the Pi (no CUDA) falls
    through to CPU. Explicit ``--device`` overrides win. Library entry points
    keep ``device="cpu"`` as their hard default — the serve path on the Pi
    must stay deterministic regardless of where the trainer ran.
    """
    try:
        import torch
    except ImportError:
        return "cpu"
    try:
        return "cuda" if torch.cuda.is_available() else "cpu"
    except Exception:  # noqa: BLE001 — driver/library issue → CPU is always safe
        return "cpu"


def dataloader_kwargs_for_device(device: str, *, num_workers: int | None = None) -> dict[str, Any]:
    """``num_workers`` + ``pin_memory`` tuned to the training device.

    CUDA uses page-locked host memory so host→device copies can overlap kernels.
    Worker count remains conservative because this dataset owns a full frame
    plus dense arrays; WSL defaults to zero and other CUDA hosts to one. CPU
    stays single-process. An explicit config value always wins.
    """
    if num_workers is None:
        # WSL process workers frequently duplicate this dataset's full pandas
        # frame and dense arrays. Zero is also the safe 16-GB default elsewhere.
        is_wsl = "microsoft" in platform.release().lower()
        num_workers = 0 if is_wsl else (1 if str(device).startswith("cuda") else 0)
    if str(device).startswith("cuda"):
        return {"num_workers": num_workers, "pin_memory": True}
    return {"num_workers": 0, "pin_memory": False}


def configure_torch_threads(thread_count: int | None = None) -> None:
    """Pin torch's intra-op thread pool to ``thread_count`` (default: all CPUs).

    Training CLIs call this at startup so CPU-bound code paths (the FP
    benchmark, threshold calibration, and the CPU fall-back of the trainers
    themselves) saturate every available core instead of the conservative
    half-cores torch picks by default. A no-op when torch is not installed.
    """
    try:
        import torch
    except ImportError:
        return
    if thread_count is None:
        thread_count = os.cpu_count() or 1
    try:
        torch.set_num_threads(int(thread_count))
    except Exception:  # noqa: BLE001 — exotic builds without the OpenMP backend
        return


__all__ = [
    "MIN_CALIBRATION_SAMPLES",
    "EpochLoss",
    "ReconstructionErrorDistribution",
    "SequenceModelLike",
    "SequenceThresholdCalibration",
    "calibrate_sequence_thresholds",
    "coerce_timestamp",
    "configure_torch_threads",
    "cpu_state_dict",
    "dataloader_kwargs_for_device",
    "default_training_device",
    "error_distribution",
    "evaluate_loss",
    "masked_mse_loss",
    "max_window_timestamp",
    "min_window_timestamp",
    "split_dataset",
    "torch_module",
    "torch_training_modules",
    "train_one_epoch",
    "training_data_hash",
    "training_sessions",
    "dataset_observation_health",
    "validate_dataset_observations",
]
