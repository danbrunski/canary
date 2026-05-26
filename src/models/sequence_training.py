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


def train_one_epoch(model: Any, loader: Any, *, optimizer: Any, device: Any) -> float:
    model.train()
    losses: list[float] = []
    for value, mask, delta in loader:
        value = value.to(device)
        mask = mask.to(device)
        delta = delta.to(device)
        optimizer.zero_grad(set_to_none=True)
        reconstruction = model(value, mask, delta)
        loss = masked_mse_loss(reconstruction, value, mask)
        loss.backward()
        optimizer.step()
        losses.append(float(loss.detach().cpu().item()))
    return float(np.mean(losses))


def evaluate_loss(model: Any, loader: Any, *, device: Any) -> float:
    torch = torch_module()
    model.eval()
    losses: list[float] = []
    with torch.no_grad():
        for value, mask, delta in loader:
            value = value.to(device)
            mask = mask.to(device)
            delta = delta.to(device)
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
    """Random train/validation split into ``torch.utils.data.Subset`` views."""
    torch, _nn, _optim, data = torch_training_modules()
    n = len(dataset)
    validation_count = 0
    if validation_fraction > 0 and n > 1:
        validation_count = int(round(n * validation_fraction))
        validation_count = max(1, validation_count)
        validation_count = min(validation_count, n - 1)
    if validation_count == 0:
        return data.Subset(dataset, list(range(n))), None

    generator = torch.Generator().manual_seed(random_state)
    permutation = torch.randperm(n, generator=generator).tolist()
    validation_indexes = sorted(int(index) for index in permutation[:validation_count])
    train_indexes = sorted(int(index) for index in permutation[validation_count:])
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
) -> SequenceThresholdCalibration:
    """Compute global / per-channel / per-mode reconstruction-error thresholds.

    Global errors are per-window masked MAE over all cells; per-channel errors
    are per-window masked MAE over timesteps; thresholds are
    ``mean + threshold_k · population_std``. Backbone-agnostic — any model with
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
            value = value.to(device_obj)
            mask = mask.to(device_obj)
            delta = delta.to(device_obj)
            global_errors = model.reconstruction_error(value, mask, delta, reduction="sample")
            channel_errors = model.reconstruction_error(value, mask, delta, reduction="channel")
            global_chunks.append(global_errors.detach().cpu().numpy().astype(np.float64))
            channel_chunks.append(channel_errors.detach().cpu().numpy().astype(np.float64))

    global_values = np.concatenate(global_chunks)
    channel_matrix = np.concatenate(channel_chunks, axis=0)
    global_distribution = error_distribution(global_values, threshold_k=threshold_k)
    channel_distributions = {
        column: error_distribution(channel_matrix[:, index], threshold_k=threshold_k)
        for index, column in enumerate(dataset.feature_columns)
    }

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
            if len(indexes) < MIN_CALIBRATION_SAMPLES:
                continue
            per_mode_distributions[mode] = error_distribution(
                global_values[indexes], threshold_k=threshold_k
            )

    return SequenceThresholdCalibration(
        threshold_k=threshold_k,
        calibrated_at=_format_timestamp(coerce_timestamp(calibrated_at)),
        training_data_hash=training_data_hash(dataset),
        window_count=len(dataset),
        global_error=global_distribution,
        channel_errors=channel_distributions,
        per_mode_errors=per_mode_distributions if per_mode_distributions else None,
    )


def error_distribution(
    values: np.ndarray[Any, np.dtype[np.float64]],
    *,
    threshold_k: float,
) -> ReconstructionErrorDistribution:
    if values.size == 0:
        raise ValueError("cannot calibrate thresholds from an empty error distribution")
    finite = values[np.isfinite(values)]
    if finite.size == 0:
        raise ValueError("cannot calibrate thresholds from non-finite reconstruction errors")
    mean = float(np.mean(finite))
    std = float(np.std(finite, ddof=0))
    p50, p90, p95, p99 = np.quantile(finite, [0.5, 0.9, 0.95, 0.99])
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
        threshold=float(mean + threshold_k * std),
    )


def training_data_hash(dataset: SequenceWindowDataset) -> str:
    digest = hashlib.sha256()
    digest.update(
        json.dumps(
            {
                "feature_columns": list(dataset.feature_columns),
                "sequence_length": dataset.sequence_length,
                "windows": [window.to_dict() for window in dataset.windows],
            },
            sort_keys=True,
        ).encode("utf-8")
    )
    for window in dataset.windows:
        values = dataset.frame.iloc[window.start_row : window.end_row][
            list(dataset.feature_columns)
        ].to_numpy(dtype=np.float64)
        digest.update(np.ascontiguousarray(values).tobytes())
    return f"sha256:{digest.hexdigest()}"


def training_sessions(dataset: SequenceWindowDataset) -> tuple[str, ...]:
    return tuple(sorted({window.session for window in dataset.windows}))


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


def dataloader_kwargs_for_device(device: str) -> dict[str, Any]:
    """``num_workers`` + ``pin_memory`` tuned to the training device.

    On CUDA we ask for two prefetch workers and page-locked host memory so the
    host→device copy overlaps the kernel; on CPU there's no copy to overlap and
    the extra worker processes just add IPC noise, so we stay single-process
    (matching the conservative Pi-friendly defaults). Picked off ``device`` so
    callers don't need to know the rule.
    """
    if str(device).startswith("cuda"):
        return {"num_workers": 2, "pin_memory": True}
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
]
