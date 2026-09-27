"""The drive-grouped validation split for sequence training (A03.5, R04.11).

Split out of :mod:`src.models.sequence_training`, which re-exports every name.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any

import numpy as np
import pandas as pd

from src.columns import MODE_COLUMN, SESSION_COLUMN, TIMESTAMP_COLUMN
from src.models.sequence_calibration import MIN_CALIBRATION_SAMPLES
from src.models.sequence_dataset import SequenceWindowDataset
from src.models.sequence_device import torch_training_modules

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


__all__ = [
    "VALIDATION_MAX_OVERSHOOT",
    "VALIDATION_RULE",
    "VALIDATION_RULE_STATEMENT",
    "ValidationChoice",
    "ValidationSplit",
    "choose_validation_sessions",
    "split_dataset",
    "tail_block_validation_start",
    "validation_split",
]
