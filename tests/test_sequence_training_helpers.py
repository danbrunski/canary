"""Unit tests for the small training helpers in ``src.models.sequence_training``
that don't fit the trainer's larger end-to-end fixtures."""

from __future__ import annotations

import numpy as np
import pandas as pd
import pytest

from src.models.sequence_dataset import SequenceWindowDataset
from src.models.sequence_training import (
    dataloader_kwargs_for_device,
    default_training_device,
    error_distribution,
    split_dataset,
)
from src.preprocessing.resampler import SESSION_COLUMN, TIMESTAMP_COLUMN


def _session_block(session: str, rows: int, start: str) -> pd.DataFrame:
    rng = np.random.default_rng(hash(session) % 2**32)
    return pd.DataFrame(
        {
            SESSION_COLUMN: [session] * rows,
            TIMESTAMP_COLUMN: pd.date_range(start, periods=rows, freq="1s", tz="UTC"),
            "rpm": rng.normal(800.0, 30.0, rows),
            "speed": rng.normal(0.0, 1.0, rows),
        }
    )


def _two_session_frame() -> pd.DataFrame:
    return pd.concat(
        [
            _session_block("A", 30, "2026-05-11T00:00:00Z"),
            _session_block("B", 30, "2026-05-11T01:00:00Z"),
            _session_block("C", 30, "2026-05-11T02:00:00Z"),
        ],
        ignore_index=True,
    )


def _single_session_frame(rows: int) -> pd.DataFrame:
    return _session_block("only", rows, "2026-05-11T00:00:00Z")


def test_dataloader_kwargs_for_device_cuda_opts_in_pin_memory_and_workers() -> None:
    """CUDA training overlaps host→device copies with kernel time; that needs
    pinned host memory and at least one prefetch worker, so the helper opts in."""
    kwargs = dataloader_kwargs_for_device("cuda")
    assert kwargs["pin_memory"] is True
    assert kwargs["num_workers"] in {0, 1}


def test_dataloader_workers_can_be_forced_off_for_memory_constrained_wsl() -> None:
    assert dataloader_kwargs_for_device("cuda", num_workers=0) == {
        "num_workers": 0,
        "pin_memory": True,
    }
    # ``cuda:0`` (explicit index) is still CUDA and honors an explicit override.
    assert dataloader_kwargs_for_device("cuda:0", num_workers=0) == {
        "num_workers": 0,
        "pin_memory": True,
    }


def test_dataloader_kwargs_for_device_cpu_stays_conservative() -> None:
    """CPU has no copy to overlap; the extra worker processes only add IPC
    noise. Stay on the conservative Pi-friendly defaults."""
    assert dataloader_kwargs_for_device("cpu") == {"num_workers": 0, "pin_memory": False}
    # Unknown device strings fall back to CPU defaults (safer than guessing).
    assert dataloader_kwargs_for_device("mps") == {"num_workers": 0, "pin_memory": False}


def test_default_training_device_returns_cpu_or_cuda() -> None:
    """The helper just asks torch; result depends on the test host's GPU. The
    invariant is that it returns one of the two strings, never raises, and
    never silently picks something unknown."""
    assert default_training_device() in {"cpu", "cuda"}


def test_split_dataset_holds_out_whole_sessions() -> None:
    """Multi-session corpora split at session granularity — no validation
    window shares rows with any train window."""
    torch = pytest.importorskip("torch")
    del torch
    frame = _two_session_frame()
    dataset = SequenceWindowDataset(frame, sequence_length=4, feature_columns=("rpm", "speed"))
    train, validation = split_dataset(dataset, validation_fraction=0.3, random_state=7)
    assert validation is not None
    train_sessions = {dataset.windows[i].session for i in train.indices}
    val_sessions = {dataset.windows[i].session for i in validation.indices}
    assert train_sessions and val_sessions
    assert train_sessions.isdisjoint(val_sessions)


def test_split_dataset_single_session_uses_gapped_tail_block() -> None:
    """A single-session corpus falls back to a contiguous tail block with a
    sequence_length gap, so train and validation share no rows."""
    torch = pytest.importorskip("torch")
    del torch
    frame = _single_session_frame(rows=60)
    dataset = SequenceWindowDataset(frame, sequence_length=4, feature_columns=("rpm", "speed"))
    train, validation = split_dataset(dataset, validation_fraction=0.2, random_state=7)
    assert validation is not None
    last_train_end = max(dataset.windows[i].end_row for i in train.indices)
    first_val_start = min(dataset.windows[i].start_row for i in validation.indices)
    assert first_val_start >= last_train_end  # no shared rows


def test_error_distribution_quantile_threshold() -> None:
    values = np.linspace(0.0, 1.0, 1001)
    dist = error_distribution(values, threshold_k=3.0, threshold_quantile=0.99)
    assert dist.threshold == pytest.approx(0.99, abs=1e-6)
    sigma = error_distribution(values, threshold_k=3.0)
    assert sigma.threshold == pytest.approx(
        float(values.mean()) + 3.0 * float(values.std()), abs=1e-9
    )
    with pytest.raises(ValueError, match="threshold_quantile"):
        error_distribution(values, threshold_k=3.0, threshold_quantile=1.5)
