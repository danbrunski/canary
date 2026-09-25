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

from typing import Any, Protocol

import numpy as np
import pandas as pd

from src.models.sequence_calibration import (
    CONTEXT_UNCALIBRATED_REASON,
    MIN_CALIBRATION_SAMPLES,
    MIN_OBSERVED_BLOCKS,
    RARE_OBSERVED_FRACTION,
    EpochLoss,
    ReconstructionErrorDistribution,
    SequenceThresholdCalibration,
    block_maxima,
    calibrate_sequence_thresholds,
    coerce_timestamp,
    error_distribution,
    is_rarely_observed,
    observed_block_counts,
    training_data_hash,
    uncalibrated_distribution,
    window_blocks,
    window_modes,
)
from src.models.sequence_dataset import SequenceWindowDataset
from src.models.sequence_device import (
    configure_torch_threads,
    dataloader_kwargs_for_device,
    default_training_device,
    torch_module,
    torch_training_modules,
)
from src.models.sequence_envelope import (
    ENVELOPE_QUANTITIES,
    MIN_ENVELOPE_COVERAGE,
    envelope_coverage,
    envelope_coverage_violations,
    merge_envelopes,
    session_envelopes,
)
from src.models.sequence_validation_split import (
    VALIDATION_MAX_OVERSHOOT,
    VALIDATION_RULE,
    VALIDATION_RULE_STATEMENT,
    ValidationChoice,
    ValidationSplit,
    choose_validation_sessions,
    split_dataset,
    tail_block_validation_start,
    validation_split,
)
from src.timefmt import format_timestamp as _format_timestamp


class SequenceModelLike(Protocol):
    """The reconstruction surface calibration needs from any sequence model."""

    sequence_length: int

    def __call__(self, value: Any, mask: Any, delta: Any) -> Any: ...

    def reconstruction_error(
        self, value: Any, mask: Any, delta: Any, *, reduction: str = ...
    ) -> Any: ...


def masked_mse_loss(
    reconstruction: Any, value: Any, mask: Any, loss_weights: Any | None = None
) -> Any:
    """Masked MSE: ``sum(m ⊙ (recon − x)²) / sum(m)`` over the whole batch.

    Only observed cells (``mask == 1``) count, so the model is never trained to
    reconstruct a forward-filled fill. A degenerate all-stale batch (sum(m)==0)
    is clamped to a denominator of 1 so the loss is 0 rather than NaN.

    ``loss_weights`` (``(F,)``, 0/1) restricts the loss to scored columns: the
    context inputs of an ``engine_response`` feature set (A03.2) condition the
    encoder through its input mask but are never reconstruction targets.
    """
    if loss_weights is not None:
        mask = mask * loss_weights
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
    loss_weights: Any | None = None,
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
            loss = masked_mse_loss(reconstruction, value, mask, loss_weights)
        if grad_scaler is not None:
            grad_scaler.scale(loss).backward()
            grad_scaler.step(optimizer)
            grad_scaler.update()
        else:
            loss.backward()
            optimizer.step()
        losses.append(float(loss.detach().cpu().item()))
    return float(np.mean(losses))


def evaluate_loss(
    model: Any,
    loader: Any,
    *,
    device: Any,
    amp_dtype: Any | None = None,
    loss_weights: Any | None = None,
) -> float:
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
                loss = masked_mse_loss(reconstruction, value, mask, loss_weights)
            losses.append(float(loss.detach().cpu().item()))
    return float(np.mean(losses))


def cpu_state_dict(model: Any) -> dict[str, Any]:
    return {key: value.detach().cpu().clone() for key, value in model.state_dict().items()}


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


__all__ = [
    "tail_block_validation_start",
    "CONTEXT_UNCALIBRATED_REASON",
    "ENVELOPE_QUANTITIES",
    "MIN_ENVELOPE_COVERAGE",
    "VALIDATION_RULE",
    "ValidationChoice",
    "ValidationSplit",
    "choose_validation_sessions",
    "envelope_coverage",
    "envelope_coverage_violations",
    "merge_envelopes",
    "session_envelopes",
    "validation_split",
    "MIN_CALIBRATION_SAMPLES",
    "MIN_OBSERVED_BLOCKS",
    "RARE_OBSERVED_FRACTION",
    "is_rarely_observed",
    "observed_block_counts",
    "uncalibrated_distribution",
    "window_blocks",
    "window_modes",
    "block_maxima",
    "VALIDATION_MAX_OVERSHOOT",
    "VALIDATION_RULE_STATEMENT",
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
