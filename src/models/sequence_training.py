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
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any, Protocol

import numpy as np
import pandas as pd

from src.models.sequence_dataset import MissingSequenceDependencyError, SequenceWindowDataset
from src.preprocessing.resampler import SESSION_COLUMN, TIMESTAMP_COLUMN
from src.preprocessing.segmenter import MODE_COLUMN
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
#: Reason recorded for an ``engine_response`` context input (A03.2).
CONTEXT_UNCALIBRATED_REASON = "context input: conditions the encoder, never scored (A03.2)"


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
    #: A02.12 — channel -> why it has no threshold (never, or too rarely,
    #: observed in calibration or in training). Their ``channel_errors`` entry
    #: is a zero distribution (threshold 0.0) and the global, per-mode and
    #: family thresholds were computed without them, exactly as the serving
    #: engine scores them (out of the score mask).
    uncalibrated_channels: Mapping[str, str] | None = None
    #: Families left without a threshold, same meaning.
    uncalibrated_families: Mapping[str, str] | None = None
    #: A02.3 — the distributions are per-block maxima over non-overlapping
    #: blocks of this many rows, pooled across sessions: independent samples,
    #: not ~30x-overlapping stride-1 windows. ``None`` on older manifests.
    block_rows: int | None = None
    #: Independent blocks behind ``global_error`` (its ``count``); the
    #: finest quantile these thresholds can resolve is about ``1 - 10/n``.
    effective_window_count: int | None = None

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


#: A03.5 — the stated rule validation sessions are chosen by, recorded in the
#: manifest's ``validation_holdout``.
#: v2 (2026-09-23): drives are physical drives (captures bridge, activity pauses split).
VALIDATION_RULE = "mode_coverage_by_drive_v2"
VALIDATION_RULE_STATEMENT = (
    "Whole physical drives are held out (sessions grouped as "
    "src.analysis.drives.piece_units groups them). Drives are taken in a seeded "
    "random order: first, while an operating mode the model will score (at least "
    "MIN_CALIBRATION_SAMPLES independent windows) is missing from validation, the "
    "drive adding the most missing modes; then drives in order until validation "
    "holds validation_fraction of the windows. A drive is skipped when taking it "
    "would put more than twice that fraction in validation, leave no drive to "
    "train on, or leave a scored mode with fewer than MIN_CALIBRATION_SAMPLES "
    "independent training windows. With one drive (or none eligible) the "
    "validation set is a contiguous tail block, gapped by one window length."
)
#: A drive is not taken when validation would then exceed this multiple of
#: the requested fraction of windows.
VALIDATION_MAX_OVERSHOOT = 2.0


@dataclass(frozen=True)
class ValidationChoice:
    """Which sessions validate a sequence model, and why (A03.5)."""

    sessions: tuple[str, ...]
    record: Mapping[str, Any]


def _frame_window_table(
    frame: pd.DataFrame, *, sequence_length: int, stride: int = 1
) -> pd.DataFrame:
    """One row per window the frame yields: session, end-row block, end-row mode.

    Mirrors ``build_sequence_windows`` (windows never cross a session or a
    contiguous-segment break) without materializing mask/Δt, so a caller can
    reason about the split before any scaling.
    """
    from src.models.sequence_dataset import contiguous_segments

    ordered = frame.sort_values([SESSION_COLUMN, TIMESTAMP_COLUMN], kind="stable").reset_index(
        drop=True
    )
    segments = contiguous_segments(ordered)
    sessions = ordered[SESSION_COLUMN].astype(str).to_numpy()
    modes = (
        ordered[MODE_COLUMN].to_numpy()
        if MODE_COLUMN in ordered.columns
        else np.full(len(ordered), None, dtype=object)
    )
    length = max(1, int(sequence_length))
    rows: list[tuple[str, int, Any]] = []
    boundaries = np.flatnonzero(np.diff(segments) != 0) + 1
    for run in np.split(np.arange(len(ordered)), boundaries):
        if len(run) < length:
            continue
        for end in run[length - 1 :: max(1, int(stride))]:
            rows.append((str(sessions[end]), int(end) // length, modes[end]))
    return pd.DataFrame(rows, columns=["session", "block", "mode"])


def _session_drives(frame: pd.DataFrame) -> list[tuple[str, ...]]:
    """Frame pieces that must be held out together: one physical drive
    (:func:`src.analysis.drives.piece_units`), so validation can never share a
    drive with training, even across a capture the frame does not contain."""
    from src.analysis.drives import piece_units

    return piece_units(frame, session_column=SESSION_COLUMN, timestamp_column=TIMESTAMP_COLUMN)


def choose_validation_sessions(
    frame: pd.DataFrame,
    *,
    sequence_length: int,
    validation_fraction: float,
    random_state: int,
    stride: int = 1,
) -> ValidationChoice:
    """Pick validation sessions by :data:`VALIDATION_RULE` (A03.5).

    The old split shuffled sessions and then sorted them by window count, a
    stable sort that left the shuffle only breaking ties — validation was
    always the smallest sessions, whatever modes they held. This takes whole
    drives, spans the scored modes first, and keeps every scored mode trainable.
    Operates on the frame (scaled or not — only sessions, times and modes are
    read), so a caller can fix the split before fitting its scaler (A03.4).
    """
    windows = _frame_window_table(frame, sequence_length=sequence_length, stride=stride)
    total = len(windows)
    target = 0
    if validation_fraction > 0 and total > 1:
        target = min(max(1, int(round(total * validation_fraction))), total - 1)
    cap = max(target, int(np.floor(total * validation_fraction * VALIDATION_MAX_OVERSHOOT)))
    blocks_by_mode = (
        windows.dropna(subset=["mode"]).assign(mode=lambda w: w["mode"].astype(str)).groupby("mode")
    )
    scored_modes = sorted(
        str(mode)
        for mode, group in blocks_by_mode
        if group[["session", "block"]].drop_duplicates().shape[0] >= MIN_CALIBRATION_SAMPLES
    )
    drives = _session_drives(frame) if total else []
    record: dict[str, Any] = {
        "rule": VALIDATION_RULE,
        "statement": VALIDATION_RULE_STATEMENT,
        "validation_fraction": validation_fraction,
        "random_state": random_state,
        "target_windows": target,
        "total_windows": total,
        "modes_scored": scored_modes,
    }
    if target == 0 or len(drives) < 2:
        record.update(
            sessions=[],
            drives=[],
            windows=0,
            modes_covered=[],
            fallback="tail_block"
            if target > 0
            else "no validation (validation_fraction 0 or one window)",
        )
        return ValidationChoice((), record)

    def blocks(sessions: set[str]) -> dict[str, int]:
        subset = windows[windows["session"].isin(sessions)].dropna(subset=["mode"])
        counts = subset.assign(mode=subset["mode"].astype(str)).drop_duplicates(
            ["session", "block", "mode"]
        )
        return {str(mode): int(n) for mode, n in counts.groupby("mode").size().items()}

    window_count = windows.groupby("session").size().to_dict()
    drive_windows = [sum(window_count.get(s, 0) for s in drive) for drive in drives]
    drive_modes = [set(blocks(set(drive))) for drive in drives]
    everything = {str(s) for drive in drives for s in drive}
    order = [int(i) for i in np.random.default_rng(random_state).permutation(len(drives))]
    chosen: list[int] = []
    held = 0

    def eligible(index: int) -> bool:
        if index in chosen or not drive_windows[index] or held + drive_windows[index] > cap:
            return False
        if len(chosen) + 1 >= len(drives):
            return False
        remaining = everything - {s for i in (*chosen, index) for s in drives[i]}
        left = blocks(remaining)
        return all(left.get(mode, 0) >= MIN_CALIBRATION_SAMPLES for mode in scored_modes)

    covered: set[str] = set()
    while True:
        missing = set(scored_modes) - covered
        if not missing:
            break
        best: int | None = None
        best_gain = 0
        for index in order:
            gain = len(drive_modes[index] & missing)
            if gain > best_gain and eligible(index):
                best, best_gain = index, gain
        if best is None:
            break
        chosen.append(best)
        held += drive_windows[best]
        covered |= drive_modes[best] & set(scored_modes)
    for index in order:
        if held >= target:
            break
        if eligible(index):
            chosen.append(index)
            held += drive_windows[index]
            covered |= drive_modes[index] & set(scored_modes)

    sessions = tuple(sorted(s for i in chosen for s in drives[i]))
    not_covered = {
        mode: "no eligible drive carries it (taking one would breach the window cap, "
        "empty the training side, or leave the mode untrainable)"
        for mode in scored_modes
        if mode not in covered
    }
    record.update(
        sessions=list(sessions),
        drives=[list(drives[i]) for i in sorted(chosen, key=lambda i: drives[i])],
        windows=int(held),
        modes_covered=sorted(covered),
        modes_not_covered=not_covered,
    )
    if not sessions:
        record["fallback"] = "tail_block"
    return ValidationChoice(sessions, record)


@dataclass(frozen=True)
class ValidationSplit:
    """Train/validation window indexes plus the manifest record of the choice."""

    train_indexes: list[int]
    validation_indexes: list[int]
    record: Mapping[str, Any] | None


def tail_block_validation_start(
    frame: pd.DataFrame,
    *,
    sequence_length: int,
    stride: int,
    validation_fraction: float,
) -> tuple[str, pd.Timestamp] | None:
    """Where :func:`validation_split`'s tail-block fallback starts, before any
    window is built: the (session, timestamp) of the first validation window's
    first row, in the dataset's (session, timestamp) order.

    A03.4 needs this. With no eligible drive the validation set is the tail of
    the windows, and the scaler must be fit on the rows before it; a
    session-level check cannot say which rows those are. ``None`` when there
    is no validation (fraction 0, or one window).
    """
    from src.models.sequence_dataset import WINDOW_MAX_GAP_SECONDS, contiguous_segments

    ordered = frame.sort_values([SESSION_COLUMN, TIMESTAMP_COLUMN], kind="stable").reset_index(
        drop=True
    )
    segments = contiguous_segments(ordered, max_gap_seconds=WINDOW_MAX_GAP_SECONDS)
    starts: list[int] = []
    length = max(1, int(sequence_length))
    for _session, group in ordered.groupby(SESSION_COLUMN, sort=True, dropna=False):
        positions = group.index.to_numpy(dtype=int)
        for run in np.split(positions, np.flatnonzero(np.diff(segments[positions]) != 0) + 1):
            if len(run) < length:
                continue
            starts.extend(int(run[i]) for i in range(0, len(run) - length + 1, max(1, stride)))
    n = len(starts)
    if validation_fraction <= 0 or n <= 1:
        return None
    stamps = pd.DatetimeIndex(pd.to_datetime(ordered[TIMESTAMP_COLUMN], utc=True))
    validation_count = min(max(1, int(round(n * validation_fraction))), n - 1)
    first = starts[n - validation_count]
    return (
        str(ordered.at[first, SESSION_COLUMN]),
        pd.Timestamp(stamps[first]),
    )


def validation_split(
    dataset: SequenceWindowDataset,
    *,
    validation_fraction: float,
    random_state: int,
    choice: ValidationChoice | None = None,
) -> ValidationSplit:
    """Split windows by :func:`choose_validation_sessions` (A03.5).

    ``choice`` is a split the caller already fixed (``train_sequence`` fixes
    it before fitting the scaler, A03.4); otherwise it is chosen here from the
    dataset's frame. No eligible drive (one drive, say) falls back to a
    contiguous tail block with a ``sequence_length`` gap of windows dropped
    between train and validation so they share no rows.
    """
    n = len(dataset)
    if validation_fraction <= 0 or n <= 1:
        return ValidationSplit(list(range(n)), [], None)
    if choice is None:
        choice = choose_validation_sessions(
            dataset.frame,
            sequence_length=dataset.sequence_length,
            validation_fraction=validation_fraction,
            random_state=random_state,
            stride=dataset.stride,
        )
    window_sessions = [window.session for window in dataset.windows]
    unknown = sorted(set(choice.sessions) - set(window_sessions))
    if unknown:
        raise ValueError(f"validation sessions {unknown} have no windows in the training dataset")
    held = set(choice.sessions)
    if held:
        train_indexes = [i for i, session in enumerate(window_sessions) if session not in held]
        validation_indexes = [i for i, session in enumerate(window_sessions) if session in held]
        if train_indexes and validation_indexes:
            return ValidationSplit(train_indexes, validation_indexes, dict(choice.record))

    validation_count = min(max(1, int(round(n * validation_fraction))), n - 1)
    gap = int(dataset.sequence_length)
    validation_start = n - validation_count
    train_end = max(1, validation_start - gap)
    record = dict(choice.record)
    record["fallback"] = "tail_block"
    record["tail_block"] = {
        "validation_windows": validation_count,
        "gap_windows": validation_start - train_end,
    }
    return ValidationSplit(list(range(train_end)), list(range(validation_start, n)), record)


def split_dataset(
    dataset: SequenceWindowDataset,
    *,
    validation_fraction: float,
    random_state: int,
    choice: ValidationChoice | None = None,
) -> tuple[Any, Any | None]:
    """Leakage-aware train/validation split into ``torch.utils.data.Subset`` views.

    Windows are built at stride 1, so adjacent windows share T−1 of T rows; a
    random window-level split put each validation window between two train
    windows holding ~97 % of its content. Whole drives are held out instead,
    chosen by :func:`choose_validation_sessions` (see :func:`validation_split`).
    """
    _torch, _nn, _optim, data = torch_training_modules()
    split = validation_split(
        dataset, validation_fraction=validation_fraction, random_state=random_state, choice=choice
    )
    if not split.validation_indexes:
        return data.Subset(dataset, split.train_indexes), None
    return data.Subset(dataset, split.train_indexes), data.Subset(dataset, split.validation_indexes)


# ---------------------------------------------------------------------------
# A03.8 — training envelope per operating mode
# ---------------------------------------------------------------------------

#: The driving conditions the envelope records, as generic J1979 Mode 01
#: channels (not a vehicle's catalog): name -> (column, histogram bin width in
#: the channel's own units). Values are the resampler's bounded-ffill cells
#: (NaN once stale), so hours at a condition count every fresh-held second.
ENVELOPE_QUANTITIES: dict[str, tuple[str, float]] = {
    "baro_kpa": ("obd:BAROMETRIC_PRESSURE", 1.0),
    "iat_c": ("obd:INTAKE_TEMP", 2.0),
    "speed_kph": ("obd:SPEED", 5.0),
}
#: A02.2 via A03.8: a calibration covers a mode's envelope on a quantity when
#: its [min, max] in that mode spans at least this share of the mode's
#: training rows. The 2026-09-20 calibration (71–73 kPa) would cover almost
#: none of a corpus trained mostly near 80 kPa.
MIN_ENVELOPE_COVERAGE = 0.8


def session_envelopes(frame: pd.DataFrame, *, interval_seconds: float) -> dict[str, dict[str, Any]]:
    """Per-session, per-mode envelope summaries of an *unscaled* frame.

    ``{session: {mode: {"rows": n, "quantities": {q: {"channel", "rows",
    "min", "max", "bin_width", "histogram": {bin_start: rows}}}}}}``. Kept
    per session so any subset (training minus validation, a calibration
    holdout) merges exactly: rows and histograms add, min/max combine.
    """
    if frame.empty or MODE_COLUMN not in frame.columns:
        return {}
    out: dict[str, dict[str, Any]] = {}
    keys = [frame[SESSION_COLUMN].astype(str), frame[MODE_COLUMN].astype(str)]
    for (session, mode), group in frame.groupby(keys, sort=True):
        quantities: dict[str, Any] = {}
        for name, (column, width) in ENVELOPE_QUANTITIES.items():
            if column not in group.columns:
                continue
            values = pd.to_numeric(group[column], errors="coerce").to_numpy(dtype=np.float64)
            values = values[np.isfinite(values)]
            if values.size == 0:
                continue
            bins = np.floor(values / width) * width
            labels, counts = np.unique(bins, return_counts=True)
            quantities[name] = {
                "channel": column,
                "rows": int(values.size),
                "min": float(values.min()),
                "max": float(values.max()),
                "bin_width": width,
                "histogram": {
                    f"{label:g}": int(count) for label, count in zip(labels, counts, strict=True)
                },
            }
        out.setdefault(str(session), {})[str(mode)] = {
            "rows": int(len(group)),
            "seconds": float(len(group)) * float(interval_seconds),
            "quantities": quantities,
        }
    return out


def merge_envelopes(
    per_session: Mapping[str, Mapping[str, Any]],
    sessions: Sequence[str] | None = None,
) -> dict[str, Any]:
    """The envelope of ``sessions`` (all when ``None``): per mode, the hours of
    data and each quantity's range, 5th/95th percentile and histogram. A
    quantity never measured in a mode is listed under ``unmeasured``."""
    names = sorted(per_session) if sessions is None else sorted(set(sessions) & set(per_session))
    modes: dict[str, dict[str, Any]] = {}
    for session in names:
        for mode, summary in per_session[session].items():
            merged = modes.setdefault(mode, {"rows": 0, "seconds": 0.0, "quantities": {}})
            merged["rows"] += int(summary["rows"])
            merged["seconds"] += float(summary["seconds"])
            for name, stats in summary["quantities"].items():
                into = merged["quantities"].setdefault(
                    name,
                    {
                        "channel": stats["channel"],
                        "rows": 0,
                        "min": float("inf"),
                        "max": float("-inf"),
                        "bin_width": stats["bin_width"],
                        "histogram": {},
                    },
                )
                into["rows"] += int(stats["rows"])
                into["min"] = min(into["min"], float(stats["min"]))
                into["max"] = max(into["max"], float(stats["max"]))
                for label, count in stats["histogram"].items():
                    into["histogram"][label] = into["histogram"].get(label, 0) + int(count)
    for merged in modes.values():
        merged["hours"] = round(merged.pop("seconds") / 3600.0, 4)
        for stats in merged["quantities"].values():
            stats["p05"], stats["p95"] = _histogram_quantiles(stats, (0.05, 0.95))
            stats["histogram"] = dict(
                sorted(stats["histogram"].items(), key=lambda item: float(item[0]))
            )
        merged["unmeasured"] = sorted(set(ENVELOPE_QUANTITIES) - set(merged["quantities"]))
    return {
        "sessions": names,
        "quantities": {name: column for name, (column, _w) in ENVELOPE_QUANTITIES.items()},
        "modes": dict(sorted(modes.items())),
    }


def _histogram_quantiles(stats: Mapping[str, Any], quantiles: Sequence[float]) -> list[float]:
    """Bin-start quantiles of a merged histogram (resolution: one bin)."""
    items = sorted((float(label), int(count)) for label, count in stats["histogram"].items())
    total = sum(count for _label, count in items)
    out: list[float] = []
    for q in quantiles:
        running = 0
        value = items[-1][0] if items else float("nan")
        for label, count in items:
            running += count
            if running >= q * total:
                value = label
                break
        out.append(value)
    return out


def envelope_coverage(
    training: Mapping[str, Any], calibration: Mapping[str, Any], *, mode: str, quantity: str
) -> float | None:
    """Share of ``mode``'s training rows (for ``quantity``) inside the
    calibration's [min, max] in that mode; ``None`` when training never
    measured it there. ``0.0`` when calibration has no such rows."""
    trained = training.get("modes", {}).get(mode, {}).get("quantities", {}).get(quantity)
    if not trained or not trained["rows"]:
        return None
    calibrated = calibration.get("modes", {}).get(mode, {}).get("quantities", {}).get(quantity)
    if not calibrated or not calibrated["rows"]:
        return 0.0
    width = float(trained["bin_width"])
    low = np.floor(float(calibrated["min"]) / width) * width
    high = float(calibrated["max"])
    inside = sum(
        int(count) for label, count in trained["histogram"].items() if low <= float(label) <= high
    )
    return inside / float(trained["rows"])


def envelope_coverage_violations(
    training: Mapping[str, Any],
    calibration: Mapping[str, Any],
    *,
    modes: Sequence[str],
    minimum: float = MIN_ENVELOPE_COVERAGE,
) -> list[str]:
    """A02.2's envelope refusal: for each scored mode and recorded quantity,
    the calibration must span :data:`MIN_ENVELOPE_COVERAGE` of the training
    rows. A mode the calibration never visited is the mode-coverage
    violation's to report, not this one's."""
    violations: list[str] = []
    for mode in sorted(modes):
        if mode not in calibration.get("modes", {}):
            continue
        trained_mode = training.get("modes", {}).get(mode, {})
        for quantity in ENVELOPE_QUANTITIES:
            share = envelope_coverage(training, calibration, mode=mode, quantity=quantity)
            if share is None or share >= minimum:
                continue
            trained = trained_mode["quantities"][quantity]
            calibrated = calibration["modes"][mode]["quantities"].get(quantity)
            span = (
                "never measured it"
                if not calibrated
                else f"spans {calibrated['min']:g}–{calibrated['max']:g}"
            )
            violations.append(
                f"mode {mode!r}: calibration {quantity} {span}, covering {share:.0%} of "
                f"{trained_mode.get('hours', 0):g} training hours "
                f"(training {trained['min']:g}–{trained['max']:g}; "
                f"{minimum:.0%} required)"
            )
    return violations


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

    global_maxima = _block_maxima(global_values, blocks)
    global_distribution = distribution(global_maxima)
    channel_distributions = {
        column: (
            distribution(_block_maxima(channel_matrix[:, index], blocks))
            if calibrated[index]
            else uncalibrated_distribution()
        )
        for index, column in enumerate(columns)
    }

    family_indexes: dict[str, list[int]] = {}
    for index, column in enumerate(columns):
        family_indexes.setdefault(_feature_family(column), []).append(index)
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
        family_distributions[family] = distribution(_block_maxima(values, blocks))
        threshold = max(family_distributions[family].threshold, np.finfo(np.float64).eps)
        # An unobserved family contributes 0 to the serving max (its channel
        # scores are 0 there), so it does here too.
        normalized_columns.append(np.where(present, values / threshold, 0.0))
    family_max_distribution = distribution(
        _block_maxima(np.max(np.column_stack(normalized_columns), axis=1), blocks)
    )

    # Per-operating-mode global thresholds. The mode for a window is its last
    # row's mode (the row the score is attributed to); ``shuffle=False`` keeps
    # the loader order aligned with the windows. Modes below the floor fall
    # back to ``global_error`` at inference.
    per_mode_distributions: dict[str, ReconstructionErrorDistribution] = {}
    for mode, indexes in window_modes(dataset).items():
        mode_maxima = _block_maxima(global_values[indexes], blocks[indexes])
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


def _block_maxima(
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
