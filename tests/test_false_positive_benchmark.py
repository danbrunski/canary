"""Tests for the LSTM autoencoder false-positive benchmark (Task 3.6)."""

from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace

import numpy as np
import pandas as pd
import pytest

from scripts.fp_benchmark import main as fp_benchmark_main
from scripts.fp_benchmark import run_false_positive_benchmark
from src.config import CanaryConfig
from src.models.false_positive_benchmark import (
    DEFAULT_K_GRID,
    DEFAULT_QUANTILE_GRID,
    _per_window_thresholds,
    _window_modes,
    benchmark_false_positives,
)
from src.models.lstm_training import train_lstm_autoencoder
from src.models.sequence_dataset import SequenceWindow, SequenceWindowDataset
from src.models.sequence_training import (
    ReconstructionErrorDistribution,
    SequenceThresholdCalibration,
)
from src.preprocessing.resampler import SESSION_COLUMN, TIMESTAMP_COLUMN
from src.preprocessing.segmenter import MODE_COLUMN
from src.storage.parquet_writer import write_records_to_parquet
from src.telemetry import TelemetryRecord
from tests.configs import example_config

# Torch training: not a commit gate (see the pyproject test tiers).
pytestmark = [pytest.mark.slow, pytest.mark.ml_training]

REPO_ROOT = Path(__file__).resolve().parents[1]
EXAMPLE_YAML = REPO_ROOT / "canary.example.yaml"

FEATURE_COLUMNS = ("rpm", "speed")


def _torch():
    return pytest.importorskip("torch")


def _config(**updates):
    base = CanaryConfig.from_yaml(EXAMPLE_YAML).models.lstm_autoencoder
    defaults = {
        "sequence_length": 4,
        "hidden_size": 6,
        "num_layers": 1,
        "latent_dim": 2,
        "batch_size": 4,
        "learning_rate": 0.01,
        "max_epochs": 3,
        "validation_fraction": 0.0,
        "early_stopping_patience": 2,
        "random_state": 7,
        "threshold_k": 3.0,
        # These tests exercise the k-sigma path explicitly (3.2 flipped the
        # config default to a quantile) — override per-test to cover quantile.
        "threshold_quantile": None,
    }
    defaults.update(updates)
    return base.model_copy(update=defaults)


def _frame(session: str, *, rows: int, start: str, jitter: float = 1.0) -> pd.DataFrame:
    return pd.DataFrame(
        {
            SESSION_COLUMN: [session] * rows,
            TIMESTAMP_COLUMN: pd.date_range(start, periods=rows, freq="s"),
            "rpm": [700.0 + jitter * (idx % 3) for idx in range(rows)],
            "speed": [jitter * (idx % 4) for idx in range(rows)],
        }
    )


def _dataset(frame: pd.DataFrame, sequence_length: int = 4) -> SequenceWindowDataset:
    return SequenceWindowDataset(
        frame,
        sequence_length=sequence_length,
        feature_columns=list(FEATURE_COLUMNS),
    )


def _trained(**config_updates):
    _torch()
    dataset = _dataset(_frame("train", rows=20, start="2026-05-12T00:00:00Z"))
    return train_lstm_autoencoder(
        dataset,
        _config(**config_updates),
        trained_at="2026-05-12T12:00:00Z",
        model_version="fp-benchmark-test",
        allow_in_sample_calibration=True,
    )


def test_benchmark_reports_zero_false_positives_with_loose_threshold() -> None:
    trained = _trained(threshold_k=1_000.0)
    held_out = _dataset(_frame("holdout", rows=20, start="2026-05-12T01:00:00Z"))

    report = benchmark_false_positives(trained, held_out)

    assert report.window_count == len(held_out)
    assert report.flagged_windows == 0
    assert report.false_positive_rate == 0.0
    assert report.alerts_per_hour == 0.0
    assert report.meets_v1_budget is True
    assert report.evaluated_sessions == ("holdout",)
    assert report.channel_summaries == ()
    assert {summary.session for summary in report.session_summaries} == {"holdout"}
    # The calibrated k is always merged into the sweep.
    assert any(point.k == report.threshold_k for point in report.k_sweep)
    assert [point.k for point in report.k_sweep] == sorted(point.k for point in report.k_sweep)


def test_benchmark_flags_windows_when_threshold_is_tight() -> None:
    trained = _trained(threshold_k=0.0)
    held_out = _dataset(_frame("holdout", rows=20, start="2026-05-12T01:00:00Z", jitter=4.0))

    report = benchmark_false_positives(trained, held_out)

    assert report.flagged_windows > 0
    assert 0.0 < report.false_positive_rate <= 1.0
    assert report.alerts_per_hour > 0.0
    # A tighter k can only flag at least as many windows as a looser one.
    sweep = {point.k: point.flagged_windows for point in report.k_sweep}
    ks = sorted(sweep)
    for lower, higher in zip(ks, ks[1:], strict=False):
        assert sweep[lower] >= sweep[higher]


def test_benchmark_report_round_trips_through_json() -> None:
    trained = _trained()
    held_out = _dataset(_frame("holdout", rows=16, start="2026-05-12T01:00:00Z"))

    payload = json.loads(json.dumps(benchmark_false_positives(trained, held_out).to_dict()))

    assert payload["model_version"] == "fp-benchmark-test"
    assert payload["global"]["held_out_error_distribution"]["count"] == len(held_out)
    assert payload["k_sweep"]
    assert set(point["k"] for point in payload["k_sweep"]) >= set(DEFAULT_K_GRID)
    assert payload["feature_column_count"] == len(FEATURE_COLUMNS)


def test_benchmark_uses_quantile_sweep_when_calibrated_via_quantile() -> None:
    """8.3 end-to-end: a model calibrated with threshold_quantile sweeps
    quantile fractions, not k-sigma multipliers that were never actually
    used to set its production threshold."""
    trained = _trained(threshold_quantile=0.9)
    held_out = _dataset(_frame("holdout", rows=20, start="2026-05-12T01:00:00Z"))

    report = benchmark_false_positives(trained, held_out)

    assert report.threshold_provenance == "quantile"
    swept = {point.k for point in report.k_sweep}
    assert swept >= set(DEFAULT_QUANTILE_GRID)
    assert 0.9 in swept  # the calibrated quantile is always merged in
    # None of the k-sigma grid's larger values (5, 8, 10 — meaningless as
    # quantile fractions) leaked into the quantile sweep.
    assert all(0.0 <= k <= 1.0 for k in swept)
    assert [point.k for point in report.k_sweep] == sorted(point.k for point in report.k_sweep)


def test_benchmark_rejects_uncalibrated_model() -> None:
    _torch()
    dataset = _dataset(_frame("train", rows=20, start="2026-05-12T00:00:00Z"))
    trained = train_lstm_autoencoder(
        dataset,
        _config(),
        trained_at="2026-05-12T12:00:00Z",
        model_version="uncalibrated",
        calibrate_thresholds=False,
    )
    with pytest.raises(ValueError, match="no calibrated thresholds"):
        benchmark_false_positives(trained, dataset)


def test_benchmark_rejects_feature_column_mismatch() -> None:
    trained = _trained()
    frame = _frame("holdout", rows=16, start="2026-05-12T01:00:00Z")
    frame = frame.rename(columns={"speed": "vehicle_speed"})
    mismatched = SequenceWindowDataset(
        frame, sequence_length=4, feature_columns=["rpm", "vehicle_speed"]
    )
    with pytest.raises(ValueError, match="feature columns do not match"):
        benchmark_false_positives(trained, mismatched)


def test_benchmark_rejects_sequence_length_mismatch() -> None:
    trained = _trained()
    held_out = _dataset(_frame("holdout", rows=16, start="2026-05-12T01:00:00Z"), sequence_length=5)
    with pytest.raises(ValueError, match="sequence_length does not match"):
        benchmark_false_positives(trained, held_out)


def test_benchmark_rejects_empty_dataset() -> None:
    trained = _trained()
    empty = _dataset(_frame("holdout", rows=2, start="2026-05-12T01:00:00Z"))
    assert len(empty) == 0
    with pytest.raises(ValueError, match="no sequence windows"):
        benchmark_false_positives(trained, empty)


def test_benchmark_rejects_negative_k() -> None:
    trained = _trained()
    held_out = _dataset(_frame("holdout", rows=16, start="2026-05-12T01:00:00Z"))
    with pytest.raises(ValueError, match="non-negative"):
        benchmark_false_positives(trained, held_out, k_grid=[-1.0])


def _write_obd_session(root: Path, name: str, *, rows: int, start_epoch: float) -> Path:
    session = root / name
    records: list[TelemetryRecord] = []
    for i in range(rows):
        t = start_epoch + i
        rpm = 780.0 + (i % 4)
        speed = float(i % 5)
        throttle = 6.0 + (i % 3)
        records.extend(
            [
                TelemetryRecord.obd(t, "RPM", rpm, "rpm"),
                TelemetryRecord.obd(t, "SPEED", speed, "km/h"),
                TelemetryRecord.obd(t, "THROTTLE_POS", throttle, "%"),
            ]
        )
    write_records_to_parquet(records, session, max_rows=10_000)
    return session


def test_run_false_positive_benchmark_end_to_end(tmp_path: Path) -> None:
    _torch()
    config = example_config(EXAMPLE_YAML)
    train_session = _write_obd_session(tmp_path, "train", rows=120, start_epoch=1_778_500_000.0)
    held_out_session = _write_obd_session(
        tmp_path, "holdout", rows=120, start_epoch=1_778_600_000.0
    )

    train_dataset = SequenceWindowDataset.from_sessions(
        [train_session],
        config,
        sequence_length=8,
    )
    trained = train_lstm_autoencoder(
        train_dataset,
        _config(sequence_length=8, threshold_k=8.0),
        trained_at="2026-05-12T12:00:00Z",
        model_version="fp-e2e",
        allow_in_sample_calibration=True,
    )
    artifact = tmp_path / "lstm.pt"
    trained.save(artifact)

    report = run_false_positive_benchmark(artifact, [held_out_session], config)
    assert report.model_version == "fp-e2e"
    assert report.window_count > 0
    assert 0.0 <= report.false_positive_rate <= 1.0
    assert report.evaluated_sessions == ("holdout",)
    assert report.feature_column_count == len(trained.feature_columns)


def test_cli_main_prints_json_report(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    _torch()
    config_path = tmp_path / "canary.yaml"
    config_path.write_text(EXAMPLE_YAML.read_text())
    config = example_config(config_path)
    train_session = _write_obd_session(tmp_path, "train", rows=120, start_epoch=1_778_500_000.0)
    held_out_session = _write_obd_session(
        tmp_path, "holdout", rows=120, start_epoch=1_778_600_000.0
    )

    train_dataset = SequenceWindowDataset.from_sessions([train_session], config, sequence_length=8)
    trained = train_lstm_autoencoder(
        train_dataset,
        _config(sequence_length=8, threshold_k=8.0),
        trained_at="2026-05-12T12:00:00Z",
        model_version="fp-cli",
        allow_in_sample_calibration=True,
    )
    artifact = tmp_path / "lstm.pt"
    trained.save(artifact)

    exit_code = fp_benchmark_main(
        [
            "--model",
            str(artifact),
            str(held_out_session),
            "--config",
            str(config_path),
            "--k",
            "3",
            "5",
            "--pretty",
        ]
    )
    assert exit_code == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["model_version"] == "fp-cli"
    assert {point["k"] for point in payload["k_sweep"]} >= {3.0, 5.0, 8.0}


def test_cli_main_reports_errors(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    exit_code = fp_benchmark_main(["--model", str(tmp_path / "missing.pt"), str(tmp_path / "nope")])
    assert exit_code == 1
    assert "error:" in capsys.readouterr().err


def _dist(mean: float, std: float, threshold: float) -> ReconstructionErrorDistribution:
    return ReconstructionErrorDistribution(
        count=100,
        mean=mean,
        std=std,
        minimum=0.0,
        maximum=mean + 5 * std,
        p50=mean,
        p90=mean + std,
        p95=mean + 1.5 * std,
        p99=mean + 2 * std,
        threshold=threshold,
    )


def _calibration(
    *,
    per_mode: dict[str, ReconstructionErrorDistribution] | None = None,
    threshold_quantile: float | None = None,
) -> SequenceThresholdCalibration:
    return SequenceThresholdCalibration(
        threshold_k=3.0,
        threshold_quantile=threshold_quantile,
        calibrated_at="2026-05-12T00:00:00Z",
        training_data_hash="hash",
        window_count=100,
        global_error=_dist(0.1, 0.1, 0.5),
        channel_errors={"rpm": _dist(0.05, 0.05, 0.2), "speed": _dist(0.05, 0.05, 0.2)},
        per_mode_errors=per_mode,
    )


def test_per_window_thresholds_use_mode_band_with_global_fallback() -> None:
    calibration = _calibration(
        per_mode={"idle": _dist(0.05, 0.02, 0.30), "highway": _dist(0.2, 0.3, 0.95)}
    )
    # idle/highway resolve to their bands; an uncalibrated mode and a missing
    # mode both fall back to the global threshold (0.5) — exactly as the daemon.
    thresholds = _per_window_thresholds(["idle", "highway", "city", None], calibration)
    assert list(thresholds) == pytest.approx([0.30, 0.95, 0.5, 0.5])


def test_per_window_thresholds_recompute_band_at_k() -> None:
    calibration = _calibration(per_mode={"idle": _dist(0.05, 0.02, 0.30)})
    # k=0 → band collapses to the mean; an unbanded mode uses the global mean.
    at_zero = _per_window_thresholds(["idle", "highway"], calibration, k=0.0)
    assert at_zero == pytest.approx([0.05, 0.1])
    # k=2 → mean + 2*std per band.
    at_two = _per_window_thresholds(["idle"], calibration, k=2.0)
    assert at_two == pytest.approx([0.05 + 2 * 0.02])


def test_per_window_thresholds_quantile_uses_named_fields() -> None:
    """8.3: a quantile band looks up the distribution's persisted p50/p90/
    p95/p99 field directly — no mean+k*std formula involved."""
    calibration = _calibration(per_mode={"idle": _dist(0.05, 0.02, 0.30)})
    idle = calibration.per_mode_errors["idle"]  # type: ignore[index]
    at_p90 = _per_window_thresholds(["idle"], calibration, quantile=0.9)
    assert at_p90 == pytest.approx([idle.p90])
    at_p99 = _per_window_thresholds(["idle", "highway"], calibration, quantile=0.99)
    # "highway" has no band -> falls back to the global distribution's p99.
    assert at_p99 == pytest.approx([idle.p99, calibration.global_error.p99])


def test_per_window_thresholds_quantile_matching_calibrated_uses_threshold_field() -> None:
    """The model's own calibrated quantile reuses `.threshold` directly
    (that's exactly how it was computed at train time) rather than
    requiring a matching named p50/p90/p95/p99 field."""
    calibration = _calibration(per_mode={"idle": _dist(0.05, 0.02, 0.42)}, threshold_quantile=0.999)
    at_calibrated = _per_window_thresholds(["idle"], calibration, quantile=0.999)
    assert at_calibrated == pytest.approx([0.42])  # the band's .threshold, not a p999 field


def test_per_window_thresholds_quantile_rejects_unrecoverable_quantile() -> None:
    calibration = _calibration()
    with pytest.raises(ValueError, match="not available"):
        _per_window_thresholds(["idle"], calibration, quantile=0.75)


def test_per_window_thresholds_k_and_quantile_are_mutually_exclusive() -> None:
    calibration = _calibration()
    with pytest.raises(ValueError, match="mutually exclusive"):
        _per_window_thresholds(["idle"], calibration, k=3.0, quantile=0.9)


def test_window_modes_reads_last_row_else_none() -> None:
    frame = pd.DataFrame({MODE_COLUMN: ["idle", "idle", "city", "highway", "highway"]})
    windows = [
        SequenceWindow("s", 0, 3, "t0", "t2"),
        SequenceWindow("s", 2, 5, "t2", "t4"),
    ]
    dataset = SimpleNamespace(frame=frame, windows=windows)
    # Window mode is its *last* row's mode, mirroring buffer.last_mode at serve.
    assert _window_modes(dataset) == ["city", "highway"]  # type: ignore[arg-type]
    # No mode column → every window falls back to None (→ global threshold).
    no_mode = SimpleNamespace(frame=pd.DataFrame({"rpm": [1.0, 2.0, 3.0]}), windows=windows)
    assert _window_modes(no_mode) == [None, None]  # type: ignore[arg-type]


def test_window_flag_uses_global_error_not_channel_union() -> None:
    """Regression: a window fires on the global error only, never the per-channel
    union — the saturating flood the live emitter already dropped."""
    torch = _torch()
    seq_len, n_features, n_windows = 4, 2, 5

    class _StubModel:
        def to(self, *args: object, **kwargs: object) -> _StubModel:
            return self

        def eval(self) -> _StubModel:
            return self

        def reconstruction_error(
            self, value: object, mask: object, delta: object, *, reduction: str
        ) -> object:
            # R04.1: the benchmark reads the masked cell error in one pass and
            # applies the served rule to it.
            assert reduction == "none", reduction
            batch = int(value.shape[0])  # type: ignore[attr-defined]
            out = torch.zeros((batch, seq_len, n_features))
            # Channel 0 blows past its band (0.2) every window; the global mean
            # over both channels (0.45) stays under the global threshold (0.5).
            out[:, :, 0] = 0.9
            return out

    class _StubDataset:
        def __init__(self) -> None:
            self.feature_columns = ["rpm", "speed"]
            self.sequence_length = seq_len
            self.frame = pd.DataFrame({"rpm": [0.0] * 64})  # no mode column → global band
            self.windows = [
                SequenceWindow(
                    "h",
                    i,
                    i + seq_len,
                    f"2026-05-12T00:00:0{i}Z",
                    f"2026-05-12T00:00:0{i + seq_len}Z",
                )
                for i in range(n_windows)
            ]

        def __len__(self) -> int:
            return n_windows

        def __getitem__(self, index: int) -> tuple[object, object, object]:
            zeros = torch.zeros((seq_len, n_features))
            return zeros, torch.ones((seq_len, n_features)), zeros

    trained = SimpleNamespace(
        model=_StubModel(),
        feature_columns=("rpm", "speed"),
        manifest=SimpleNamespace(
            thresholds=_calibration().to_dict(),
            schema={"sequence_length": seq_len},
            model_version="stub",
        ),
    )

    report = benchmark_false_positives(trained, _StubDataset())  # type: ignore[arg-type]

    # The channels did exceed — that stays visible as drill-down evidence …
    assert any(
        summary.channel == "rpm" and summary.exceedances == n_windows
        for summary in report.channel_summaries
    )
    # … but the window flag fires on the (sub-threshold) global error only.
    assert report.flagged_windows == 0
    assert report.false_positive_rate == 0.0
    assert report.global_exceedances == 0


# ---------------------------------------------------------------- R04.1


def _noisy_frame(session: str, *, rows: int, start: str, noise: float, seed: int) -> pd.DataFrame:
    rng = np.random.default_rng(seed)
    frame = _frame(session, rows=rows, start=start)
    frame["noisy"] = rng.normal(0.0, noise, rows)
    return frame


def test_benchmark_scores_the_served_rule_with_a_suppressed_noisy_channel() -> None:
    """R04.1: a model with one suppressed, noisy channel flags the same windows
    in the benchmark as ``SequenceInferenceEngine.score_window`` over the same
    windows. The benchmark used to average every channel (the suppressed one
    included) and skip the family path, so it graded a detector the daemon
    does not run."""
    _torch()
    from src.models.inference import SequenceInferenceEngine
    from src.preprocessing.normalization import ZScoreScaler

    columns = ["rpm", "speed", "noisy"]
    train = _noisy_frame("train", rows=40, start="2026-05-12T00:00:00Z", noise=1.0, seed=1)
    trained = train_lstm_autoencoder(
        SequenceWindowDataset(train, sequence_length=4, feature_columns=columns),
        _config(threshold_k=2.0),
        trained_at="2026-05-12T12:00:00Z",
        model_version="fp-suppressed",
        allow_in_sample_calibration=True,
    )
    # Held out: the suppressed channel is far noisier than anything calibrated.
    raw = _noisy_frame("holdout", rows=40, start="2026-05-12T01:00:00Z", noise=40.0, seed=2)
    scaler = ZScoreScaler.from_dict(dict(trained.manifest.scaler))
    held_out = SequenceWindowDataset(
        scaler.transform(raw), sequence_length=4, feature_columns=columns
    )

    report = benchmark_false_positives(trained, held_out, suppress_channels=("noisy",))

    engine = SequenceInferenceEngine(trained, suppress_channels=("noisy",))
    values = raw.loc[:, columns].to_numpy(dtype=np.float32)
    served = 0
    for window in held_out.windows:
        rows = slice(window.start_row, window.end_row)
        result = engine.score_window(
            values[rows],
            mask=held_out._mask[rows],
            delta=held_out._delta[rows],
            timestamp=window.end_timestamp,
            session=window.session,
            window_start_timestamp=window.start_timestamp,
            window_end_timestamp=window.end_timestamp,
        )
        served += int(result.is_anomaly)

    assert report.excluded_columns == ("noisy",)
    assert report.window_count == len(held_out.windows)
    assert report.flagged_windows == served
    # Unsuppressed, the noisy channel dominates the all-channel mean: the
    # suppression is what the count above depends on, not a coincidence.
    unsuppressed = benchmark_false_positives(trained, held_out)
    assert unsuppressed.flagged_windows > report.flagged_windows
