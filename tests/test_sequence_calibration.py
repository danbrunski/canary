"""Sequence threshold calibration: observed-only thresholds and independent
windows (A02.12 calibration half, A02.3), and the coverage refusal (A02.2)."""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any

import numpy as np
import pandas as pd
import pytest

torch = pytest.importorskip("torch")

from src.models.sequence_dataset import SequenceWindowDataset  # noqa: E402
from src.models.sequence_training import (  # noqa: E402
    MIN_OBSERVED_BLOCKS,
    SequenceThresholdCalibration,
    calibrate_sequence_thresholds,
    observed_block_counts,
)
from src.preprocessing.resampler import SESSION_COLUMN, TIMESTAMP_COLUMN  # noqa: E402
from src.preprocessing.segmenter import MODE_COLUMN  # noqa: E402

SEQ = 5


class _ZeroModel:
    """Reconstructs every cell as 0, so a cell's error is its |value|."""

    sequence_length = SEQ

    def to(self, *args: object, **kwargs: object) -> _ZeroModel:
        return self

    def eval(self) -> _ZeroModel:
        return self

    def forward(self, value: Any, mask: Any, delta: Any) -> Any:
        return torch.zeros_like(value)

    def reconstruction_error(self, value: Any, mask: Any, delta: Any, *, reduction: str) -> Any:
        error = value.abs() * mask
        if reduction == "none":
            return error
        raise AssertionError(reduction)


def _frame(rows: int = 120, *, c_observed: bool = False, sessions: int = 1) -> pd.DataFrame:
    rng = np.random.default_rng(0)
    parts = []
    for index in range(sessions):
        start = pd.Timestamp("2026-09-20T00:00:00Z") + pd.Timedelta(hours=index)
        parts.append(
            pd.DataFrame(
                {
                    SESSION_COLUMN: f"s{index}",
                    TIMESTAMP_COLUMN: pd.date_range(start, periods=rows, freq="1s"),
                    MODE_COLUMN: "city",
                    "a": rng.normal(0.0, 1.0, rows),
                    "a__gap": False,
                    "b": rng.normal(0.0, 1.0, rows),
                    "b__gap": False,
                    # c is never freshly observed in calibration: every cell is
                    # a forward-filled hold.
                    "c": np.full(rows, 50.0),
                    "c__gap": not c_observed,
                }
            )
        )
    return pd.concat(parts, ignore_index=True)


def _dataset(frame: pd.DataFrame) -> SequenceWindowDataset:
    return SequenceWindowDataset(frame, sequence_length=SEQ, feature_columns=["a", "b", "c"])


def _calibrate(dataset: SequenceWindowDataset, **kwargs: Any) -> SequenceThresholdCalibration:
    return calibrate_sequence_thresholds(
        _ZeroModel(),
        dataset,
        feature_columns=("a", "b", "c"),
        threshold_k=3.0,
        threshold_quantile=0.99,
        **kwargs,
    )


def test_an_unobserved_channel_gets_no_threshold_and_never_flags() -> None:
    """A02.12: a calibration frame with an all-unobserved family yields no
    threshold for it — recorded as uncalibrated — and no flags from it."""
    from src.models.inference import SequenceInferenceEngine

    calibration = _calibrate(_dataset(_frame()))
    assert calibration.uncalibrated_channels == {"c": "never observed in calibration"}
    assert calibration.uncalibrated_families == {"c": "no calibrated member channel"}
    assert calibration.channel_errors["c"].threshold == 0.0
    assert calibration.channel_errors["c"].count == 0
    assert calibration.family_errors is not None
    assert calibration.family_errors["c"].threshold == 0.0
    # The global distribution never saw c's (huge) held value.
    assert calibration.global_error.maximum < 10.0
    # Round-trips through the manifest block.
    assert SequenceThresholdCalibration.from_dict(calibration.to_dict()) == calibration

    # Served: c now arrives fresh and wildly off, a and b stay normal.
    serve = _frame(rows=40, c_observed=True)
    serve["c"] = 1e6
    trained = SimpleNamespace(
        model=_ZeroModel(),
        feature_columns=("a", "b", "c"),
        manifest=SimpleNamespace(
            thresholds=calibration.to_dict(),
            schema={"sequence_length": SEQ},
            model_version="stub",
            scaler=None,
        ),
    )
    engine = SequenceInferenceEngine(trained)  # type: ignore[arg-type]
    assert engine.uncalibrated_channels == ("c",)
    results = [engine.observe(serve.iloc[i]) for i in range(len(serve))]
    scored = [r for r in results if r is not None]
    assert scored and not any(r.is_anomaly for r in scored)
    assert all("c" not in r.family_scores for r in scored)


def test_a_channel_rare_in_training_is_uncalibrated_with_the_reason() -> None:
    calibration = _calibrate(_dataset(_frame()), training_observed_blocks={"a": 50, "b": 3})
    assert calibration.uncalibrated_channels is not None
    assert "training" in calibration.uncalibrated_channels["b"]
    assert calibration.channel_errors["b"].threshold == 0.0
    assert calibration.channel_errors["a"].threshold > 0.0


def test_thresholds_rest_on_independent_blocks_pooled_across_sessions() -> None:
    """A02.3: distributions are per-block maxima, not overlapping windows."""
    dataset = _dataset(_frame(rows=100, sessions=3))
    calibration = _calibrate(dataset)
    blocks = 3 * (100 // SEQ)  # each session's windows end in 20 distinct blocks
    assert calibration.window_count == len(dataset) == 3 * (100 - SEQ + 1)
    assert calibration.effective_window_count == blocks
    assert calibration.global_error.count == blocks
    assert calibration.block_rows == SEQ
    assert calibration.per_mode_errors is not None
    assert calibration.per_mode_errors["city"].count == blocks


def test_observed_block_counts_reads_the_mask_without_a_model() -> None:
    counts = observed_block_counts(_dataset(_frame(rows=100)))
    assert counts == {"a": 20, "b": 20, "c": 0}
    assert MIN_OBSERVED_BLOCKS > 0


# ---------------------------------------------------------------------------
# A02.2 — refuse thresholds that do not cover what they will score
# ---------------------------------------------------------------------------


def _mode_frame(
    sessions: list[tuple[str, str, str]], *, rows: int = 100, c_observed: bool = True
) -> pd.DataFrame:
    """``sessions``: (name, start ISO, mode) triples, one mode per session."""
    rng = np.random.default_rng(1)
    parts = []
    for name, start, mode in sessions:
        parts.append(
            pd.DataFrame(
                {
                    SESSION_COLUMN: name,
                    TIMESTAMP_COLUMN: pd.date_range(start, periods=rows, freq="1s"),
                    MODE_COLUMN: mode,
                    "a": rng.normal(0.0, 1.0, rows),
                    "a__gap": False,
                    "b": rng.normal(0.0, 1.0, rows),
                    "b__gap": False,
                    "c": rng.normal(0.0, 1.0, rows),
                    "c__gap": not c_observed,
                }
            )
        )
    return pd.concat(parts, ignore_index=True)


_TRAINING = [
    ("2026-09-01T100000", "2026-09-01T10:00:00Z", "city"),
    ("2026-09-02T100000", "2026-09-02T10:00:00Z", "highway"),
]


def _tiny_lstm_config() -> Any:
    from pathlib import Path

    from src.config import CanaryConfig

    example = Path(__file__).resolve().parents[1] / "canary.example.yaml"
    return CanaryConfig.from_yaml(example).models.lstm_autoencoder.model_copy(
        update={
            "sequence_length": SEQ,
            "hidden_size": 4,
            "num_layers": 1,
            "latent_dim": 2,
            "batch_size": 16,
            "max_epochs": 1,
            "validation_fraction": 0.0,
            "early_stopping_patience": 1,
            "random_state": 0,
        }
    )


def test_a_thin_held_out_calibration_is_refused_before_training() -> None:
    """The 2026-09-20 shape: one session, one mode, a channel never observed."""
    from src.models.lstm_training import train_lstm_autoencoder
    from src.models.sequence_autoencoder_training import CalibrationCoverageError

    training = _dataset(_mode_frame(_TRAINING))
    calibration = _dataset(
        _mode_frame([("2026-09-18T134345", "2026-09-18T13:43:45Z", "city")], c_observed=False)
    )
    with pytest.raises(CalibrationCoverageError) as excinfo:
        train_lstm_autoencoder(training, _tiny_lstm_config(), calibration_dataset=calibration)
    violations = excinfo.value.violations
    assert any("1 physical drive" in v for v in violations)
    assert any("['highway']" in v for v in violations)
    assert any("never observed in calibration: ['c']" in v for v in violations)


def test_sessions_of_one_drive_count_once() -> None:
    from src.models.sequence_autoencoder_training import calibration_coverage_violations

    one_drive = _dataset(
        _mode_frame(
            [
                ("2026-09-18T100000", "2026-09-18T10:00:00Z", "city"),
                ("2026-09-18T100200", "2026-09-18T10:01:45Z", "highway"),
                ("2026-09-18T100400", "2026-09-18T10:03:30Z", "city"),
            ]
        )
    )
    violations = calibration_coverage_violations(
        _dataset(_mode_frame(_TRAINING)), one_drive, held_out=True
    )
    assert violations == [
        "calibration rests on 1 physical drive(s) (2026-09-18T100000, 2026-09-18T100200, "
        "2026-09-18T100400); 3 are required"
    ]

    three_drives = _dataset(
        _mode_frame(
            [
                ("2026-09-18T100000", "2026-09-18T10:00:00Z", "city"),
                ("2026-09-19T100000", "2026-09-19T10:00:00Z", "highway"),
                ("2026-09-20T100000", "2026-09-20T10:00:00Z", "city"),
            ]
        )
    )
    assert (
        calibration_coverage_violations(
            _dataset(_mode_frame(_TRAINING)), three_drives, held_out=True
        )
        == []
    )


def test_an_explicit_override_publishes_and_is_stamped() -> None:
    from src.models.lstm_training import train_lstm_autoencoder

    training = _dataset(_mode_frame(_TRAINING))
    calibration = _dataset(
        _mode_frame([("2026-09-18T134345", "2026-09-18T13:43:45Z", "city")], c_observed=False)
    )
    trained = train_lstm_autoencoder(
        training,
        _tiny_lstm_config(),
        calibration_dataset=calibration,
        calibration_override="operator 2026-09-22: only drive available before the trip",
        trained_at="2026-09-22T00:00:00Z",
    )
    holdout = trained.manifest.calibration_holdout
    assert holdout is not None
    assert holdout["override"]["reason"].startswith("operator 2026-09-22")
    assert len(holdout["override"]["violations"]) == 3
    calibration_block = SequenceThresholdCalibration.from_dict(trained.manifest.thresholds)
    assert calibration_block.uncalibrated_channels == {"c": "never observed in calibration"}
