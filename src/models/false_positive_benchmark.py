"""False-positive benchmark for the sequence autoencoders (Task 3.6).

Runs a trained, threshold-calibrated sequence detector (LSTM or TCN) over
held-out *clean* sessions and reports how often it would fire. The headline
number is the false-positive rate (FPR): the fraction of evaluated windows
flagged anomalous. Because the windows are clean, every flag is by definition a
false positive.

The window flag **mirrors the live emit policy** (``SequenceInference`` in
``src.models.inference``): a window fires when its *global* (masked-mean)
reconstruction error exceeds the threshold for the window's **operating mode**
(``per_mode_errors[mode].threshold``), falling back to the single global
threshold when the mode has no calibrated band. It deliberately does **not**
OR in per-channel exceedances: the per-channel thresholds run far too tight
live (with a few hundred channels at idle/warmup at least one is essentially
always over its band, so a union flag saturates to ~100% even in-sample), which
is exactly why the daemon dropped its ``or bool(anomalous_channels)`` clause.
Per-channel exceedances are still computed and reported as drill-down
diagnostics (``channel_summaries``), never as the firing trigger.

The report also sweeps a grid of ``k`` multipliers, recomputing thresholds as
``mean + k * std`` from the *training* error distribution stored in the
manifest, so an operator can pick a ``k`` that meets the v1 done-definition
budget (<= 1 alert / hour) without retraining. ``alerts_per_hour`` is derived
from the wall-clock span of the held-out windows, which matches how the live
1 Hz inference loop would emit alerts.
"""

from __future__ import annotations

import importlib
from collections.abc import Sequence
from dataclasses import dataclass
from typing import Any

import numpy as np
import pandas as pd

from src.models.lstm_autoencoder import MissingLSTMAutoencoderDependencyError
from src.models.sequence_dataset import SequenceWindow, SequenceWindowDataset
from src.models.sequence_protocol import TrainedSequenceModel
from src.models.sequence_training import (
    ReconstructionErrorDistribution,
    SequenceThresholdCalibration,
    dataloader_kwargs_for_device,
)
from src.preprocessing.segmenter import MODE_COLUMN

#: Default ``k`` multipliers swept by the benchmark. The calibrated ``k`` from
#: the manifest is always merged in so the as-shipped point is visible.
DEFAULT_K_GRID: tuple[float, ...] = (1.0, 2.0, 3.0, 4.0, 5.0, 6.0, 8.0, 10.0)

#: v1 done-definition budget: at most one alert per hour on a clean drive.
V1_ALERTS_PER_HOUR_BUDGET = 1.0

SECONDS_PER_HOUR = 3600.0


@dataclass(frozen=True)
class ChannelFalsePositiveSummary:
    """Per-channel exceedance count and FPR at the calibrated threshold."""

    channel: str
    threshold: float
    exceedances: int
    false_positive_rate: float

    def to_dict(self) -> dict[str, Any]:
        return {
            "channel": self.channel,
            "threshold": self.threshold,
            "exceedances": self.exceedances,
            "false_positive_rate": self.false_positive_rate,
        }


@dataclass(frozen=True)
class SessionFalsePositiveSummary:
    """Per-session flag count, FPR, and alert rate over the held-out windows."""

    session: str
    windows: int
    flagged_windows: int
    false_positive_rate: float
    duration_seconds: float
    alerts_per_hour: float

    def to_dict(self) -> dict[str, Any]:
        return {
            "session": self.session,
            "windows": self.windows,
            "flagged_windows": self.flagged_windows,
            "false_positive_rate": self.false_positive_rate,
            "duration_seconds": self.duration_seconds,
            "alerts_per_hour": self.alerts_per_hour,
        }


@dataclass(frozen=True)
class KSweepPoint:
    """FPR and alert rate at one candidate ``k`` (threshold = mean + k * std)."""

    k: float
    global_threshold: float
    flagged_windows: int
    false_positive_rate: float
    alerts_per_hour: float
    meets_v1_budget: bool

    def to_dict(self) -> dict[str, Any]:
        return {
            "k": self.k,
            "global_threshold": self.global_threshold,
            "flagged_windows": self.flagged_windows,
            "false_positive_rate": self.false_positive_rate,
            "alerts_per_hour": self.alerts_per_hour,
            "meets_v1_budget": self.meets_v1_budget,
        }


@dataclass(frozen=True)
class FalsePositiveBenchmarkReport:
    """JSON-safe false-positive benchmark for a trained LSTM autoencoder."""

    model_version: str
    threshold_k: float
    evaluated_sessions: tuple[str, ...]
    window_count: int
    sequence_length: int
    feature_column_count: int
    duration_seconds: float
    flagged_windows: int
    false_positive_rate: float
    alerts_per_hour: float
    meets_v1_budget: bool
    global_threshold: float
    global_exceedances: int
    global_false_positive_rate: float
    global_error_distribution: ReconstructionErrorDistribution
    channel_summaries: tuple[ChannelFalsePositiveSummary, ...]
    session_summaries: tuple[SessionFalsePositiveSummary, ...]
    k_sweep: tuple[KSweepPoint, ...]

    def to_dict(self) -> dict[str, Any]:
        return {
            "model_version": self.model_version,
            "threshold_k": self.threshold_k,
            "evaluated_sessions": list(self.evaluated_sessions),
            "window_count": self.window_count,
            "sequence_length": self.sequence_length,
            "feature_column_count": self.feature_column_count,
            "duration_seconds": self.duration_seconds,
            "flagged_windows": self.flagged_windows,
            "false_positive_rate": self.false_positive_rate,
            "alerts_per_hour": self.alerts_per_hour,
            "meets_v1_budget": self.meets_v1_budget,
            "global": {
                "threshold": self.global_threshold,
                "exceedances": self.global_exceedances,
                "false_positive_rate": self.global_false_positive_rate,
                "held_out_error_distribution": self.global_error_distribution.to_dict(),
            },
            "channel_exceedances": [summary.to_dict() for summary in self.channel_summaries],
            "per_session": [summary.to_dict() for summary in self.session_summaries],
            "k_sweep": [point.to_dict() for point in self.k_sweep],
        }


def _window_modes(dataset: SequenceWindowDataset) -> list[str | None]:
    """Operating mode for each window, taken from its *last* row.

    Mirrors the live path, which scores a window under ``buffer.last_mode`` —
    the mode of the most recently appended row (``inference.py``). Returns
    ``None`` per window when the frame carries no mode column (e.g. raw test
    frames that skip segmentation), so the caller falls back to the global
    threshold.
    """
    frame = dataset.frame
    if MODE_COLUMN not in frame.columns:
        return [None] * len(dataset.windows)
    modes = frame[MODE_COLUMN].to_numpy()
    row_count = len(modes)
    resolved: list[str | None] = []
    for window in dataset.windows:
        last_row = window.end_row - 1
        if 0 <= last_row < row_count and modes[last_row] is not None:
            value = modes[last_row]
            resolved.append(None if pd.isna(value) else str(value))
        else:
            resolved.append(None)
    return resolved


def _per_window_thresholds(
    modes: Sequence[str | None],
    calibration: SequenceThresholdCalibration,
    *,
    k: float | None = None,
) -> np.ndarray[Any, np.dtype[np.float64]]:
    """Per-window global-error threshold, per the window's operating mode.

    With ``k is None`` each band's calibrated ``threshold`` is used (the
    as-shipped point); otherwise the band is recomputed as ``mean + k * std``
    for the sweep. A window whose mode has no calibrated band — or any window
    when ``per_mode_errors`` is absent — falls back to the global band, exactly
    as the daemon does.
    """

    def band(distribution: ReconstructionErrorDistribution) -> float:
        return distribution.threshold if k is None else distribution.mean + k * distribution.std

    global_threshold = band(calibration.global_error)
    per_mode = calibration.per_mode_errors or {}
    mode_thresholds = {mode: band(distribution) for mode, distribution in per_mode.items()}

    def threshold_for(mode: str | None) -> float:
        if mode is not None and mode in mode_thresholds:
            return mode_thresholds[mode]
        return global_threshold

    return np.array([threshold_for(mode) for mode in modes], dtype=np.float64)


def benchmark_false_positives(
    trained: TrainedSequenceModel,
    dataset: SequenceWindowDataset,
    *,
    k_grid: Sequence[float] | None = None,
    batch_size: int = 64,
    device: str = "cpu",
) -> FalsePositiveBenchmarkReport:
    """Score held-out clean windows and report the false-positive rate.

    The caller is responsible for passing only clean-baseline sessions in
    ``dataset`` (held out from training). ``dataset`` must use the same feature
    columns and sequence length as ``trained``.
    """
    if trained.manifest.thresholds is None:
        raise ValueError("trained LSTM manifest has no calibrated thresholds")
    if batch_size <= 0:
        raise ValueError("batch_size must be positive")
    if len(dataset) == 0:
        raise ValueError("no sequence windows available for the false-positive benchmark")
    if tuple(dataset.feature_columns) != trained.feature_columns:
        raise ValueError("benchmark dataset feature columns do not match trained model")
    manifest_sequence_length = int(trained.manifest.schema["sequence_length"])
    if dataset.sequence_length != manifest_sequence_length:
        raise ValueError("benchmark dataset sequence_length does not match trained model")

    calibration = SequenceThresholdCalibration.from_dict(trained.manifest.thresholds)
    feature_columns = tuple(dataset.feature_columns)
    global_errors, channel_errors = _reconstruction_errors(
        trained, dataset, batch_size=batch_size, device=device
    )

    global_threshold = calibration.global_error.threshold
    channel_thresholds = np.array(
        [calibration.channel_errors[column].threshold for column in feature_columns],
        dtype=np.float64,
    )
    # Fire on the global error vs the window's operating-mode threshold, exactly
    # as the live emitter does. The per-channel exceedances below are diagnostics
    # only — never part of the flag (a union over hundreds of tight per-channel
    # bands saturates to ~100%, the flood the daemon already removed).
    window_modes = _window_modes(dataset)
    per_window_threshold = _per_window_thresholds(window_modes, calibration)
    flagged = global_errors > per_window_threshold
    # Mode-agnostic single-threshold view, reported alongside as a reference.
    global_exceed = global_errors > global_threshold
    channel_exceed = channel_errors > channel_thresholds

    window_count = int(global_errors.size)
    durations = _session_durations(dataset.windows)
    total_duration = float(sum(durations.values()))

    channel_summaries = _channel_summaries(
        feature_columns, channel_thresholds, channel_exceed, window_count
    )
    session_summaries = _session_summaries(dataset.windows, flagged, durations)
    k_sweep = _k_sweep(
        global_errors=global_errors,
        window_modes=window_modes,
        calibration=calibration,
        total_duration_seconds=total_duration,
        window_count=window_count,
        k_grid=k_grid,
    )

    flagged_count = int(flagged.sum())
    alerts_per_hour = _alerts_per_hour(flagged_count, total_duration)
    return FalsePositiveBenchmarkReport(
        model_version=trained.manifest.model_version,
        threshold_k=calibration.threshold_k,
        evaluated_sessions=tuple(sorted({window.session for window in dataset.windows})),
        window_count=window_count,
        sequence_length=dataset.sequence_length,
        feature_column_count=len(feature_columns),
        duration_seconds=total_duration,
        flagged_windows=flagged_count,
        false_positive_rate=flagged_count / window_count,
        alerts_per_hour=alerts_per_hour,
        meets_v1_budget=alerts_per_hour <= V1_ALERTS_PER_HOUR_BUDGET,
        global_threshold=global_threshold,
        global_exceedances=int(global_exceed.sum()),
        global_false_positive_rate=int(global_exceed.sum()) / window_count,
        global_error_distribution=_error_distribution(global_errors, calibration.threshold_k),
        channel_summaries=channel_summaries,
        session_summaries=session_summaries,
        k_sweep=k_sweep,
    )


def _reconstruction_errors(
    trained: TrainedSequenceModel,
    dataset: SequenceWindowDataset,
    *,
    batch_size: int,
    device: str,
) -> tuple[np.ndarray[Any, np.dtype[np.float64]], np.ndarray[Any, np.dtype[np.float64]]]:
    torch = _torch_module()
    data = importlib.import_module("torch.utils.data")
    device_obj = torch.device(device)
    loader = data.DataLoader(
        dataset,
        batch_size=batch_size,
        shuffle=False,
        **dataloader_kwargs_for_device(device),
    )
    # ``reconstruction_error`` isn't on the SequenceAutoencoder protocol (only
    # forward/to/eval are); both backbones implement it, so reach it via Any.
    model: Any = trained.model
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
    return np.concatenate(global_chunks), np.concatenate(channel_chunks, axis=0)


def _channel_summaries(
    feature_columns: Sequence[str],
    channel_thresholds: np.ndarray[Any, np.dtype[np.float64]],
    channel_exceed: np.ndarray[Any, np.dtype[np.bool_]],
    window_count: int,
) -> tuple[ChannelFalsePositiveSummary, ...]:
    counts = channel_exceed.sum(axis=0)
    summaries = [
        ChannelFalsePositiveSummary(
            channel=str(column),
            threshold=float(channel_thresholds[index]),
            exceedances=int(counts[index]),
            false_positive_rate=int(counts[index]) / window_count,
        )
        for index, column in enumerate(feature_columns)
        if counts[index] > 0
    ]
    summaries.sort(key=lambda summary: (-summary.exceedances, summary.channel))
    return tuple(summaries)


def _session_summaries(
    windows: Sequence[SequenceWindow],
    flagged: np.ndarray[Any, np.dtype[np.bool_]],
    durations: dict[str, float],
) -> tuple[SessionFalsePositiveSummary, ...]:
    totals: dict[str, int] = {}
    flags: dict[str, int] = {}
    for window, is_flagged in zip(windows, flagged, strict=True):
        totals[window.session] = totals.get(window.session, 0) + 1
        flags[window.session] = flags.get(window.session, 0) + int(bool(is_flagged))
    summaries = []
    for session in sorted(totals):
        count = totals[session]
        flagged_count = flags[session]
        duration = durations.get(session, 0.0)
        summaries.append(
            SessionFalsePositiveSummary(
                session=session,
                windows=count,
                flagged_windows=flagged_count,
                false_positive_rate=flagged_count / count,
                duration_seconds=duration,
                alerts_per_hour=_alerts_per_hour(flagged_count, duration),
            )
        )
    return tuple(summaries)


def _k_sweep(
    *,
    global_errors: np.ndarray[Any, np.dtype[np.float64]],
    window_modes: Sequence[str | None],
    calibration: SequenceThresholdCalibration,
    total_duration_seconds: float,
    window_count: int,
    k_grid: Sequence[float] | None,
) -> tuple[KSweepPoint, ...]:
    points: list[KSweepPoint] = []
    for k in _resolve_k_grid(k_grid, calibration.threshold_k):
        # Same firing rule as the headline number, swept across k: global error
        # vs the window's per-mode band recomputed at mean + k * std.
        per_window_threshold = _per_window_thresholds(window_modes, calibration, k=k)
        global_threshold = calibration.global_error.mean + k * calibration.global_error.std
        flagged = global_errors > per_window_threshold
        flagged_count = int(flagged.sum())
        alerts_per_hour = _alerts_per_hour(flagged_count, total_duration_seconds)
        points.append(
            KSweepPoint(
                k=float(k),
                global_threshold=float(global_threshold),
                flagged_windows=flagged_count,
                false_positive_rate=flagged_count / window_count,
                alerts_per_hour=alerts_per_hour,
                meets_v1_budget=alerts_per_hour <= V1_ALERTS_PER_HOUR_BUDGET,
            )
        )
    return tuple(points)


def _resolve_k_grid(k_grid: Sequence[float] | None, calibrated_k: float) -> tuple[float, ...]:
    values = list(DEFAULT_K_GRID if k_grid is None else k_grid)
    values.append(calibrated_k)
    for value in values:
        if value < 0:
            raise ValueError("k values must be non-negative")
    return tuple(sorted({float(value) for value in values}))


def _session_durations(windows: Sequence[SequenceWindow]) -> dict[str, float]:
    starts: dict[str, pd.Timestamp] = {}
    ends: dict[str, pd.Timestamp] = {}
    for window in windows:
        start = pd.Timestamp(window.start_timestamp)
        end = pd.Timestamp(window.end_timestamp)
        if window.session not in starts or start < starts[window.session]:
            starts[window.session] = start
        if window.session not in ends or end > ends[window.session]:
            ends[window.session] = end
    return {
        session: max((ends[session] - starts[session]).total_seconds(), 0.0) for session in starts
    }


def _alerts_per_hour(flagged: int, duration_seconds: float) -> float:
    if duration_seconds <= 0.0:
        return 0.0
    return flagged / (duration_seconds / SECONDS_PER_HOUR)


def _error_distribution(
    values: np.ndarray[Any, np.dtype[np.float64]],
    threshold_k: float,
) -> ReconstructionErrorDistribution:
    finite = values[np.isfinite(values)]
    if finite.size == 0:
        raise ValueError("cannot summarize a held-out distribution with no finite errors")
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


def _torch_module() -> Any:
    try:
        return importlib.import_module("torch")
    except ImportError as exc:
        raise MissingLSTMAutoencoderDependencyError(
            "torch is required for the LSTM false-positive benchmark; run "
            "scripts/install_deps.py or install the 'ml' optional dependency"
        ) from exc
