"""Detectors on the evaluation harness: the served stack, the batched sequence
rule's parity with the engine, and the A02.5 baselines."""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path

import numpy as np
import pandas as pd
import pytest

pytest.importorskip("sklearn")

from src.config import CanaryConfig  # noqa: E402
from src.models.evaluation_detectors import (  # noqa: E402
    DeployedStack,
    PCABaseline,
    RobustZBaseline,
    ThresholdedScore,
    raw_channels,
)
from src.models.isolation_forest import fit_isolation_forest  # noqa: E402
from src.preprocessing.feature_pipeline import featurize_wide_frame  # noqa: E402
from src.preprocessing.resampler import SESSION_COLUMN, TIMESTAMP_COLUMN  # noqa: E402
from src.preprocessing.segmenter import MODE_COLUMN  # noqa: E402

EXAMPLE_YAML = Path(__file__).resolve().parents[1] / "canary.example.yaml"


@dataclass(frozen=True)
class _IFConfig:
    enabled: bool = True
    n_estimators: int = 40
    contamination: float = 0.02
    per_mode_contamination: dict[str, float] = field(default_factory=dict)
    random_state: int = 7
    calibration_holdout_fraction: float = 0.2
    target_flag_rate: float = 0.05


def _resampled(seed: int = 0, *, gap_after: int | None = None) -> pd.DataFrame:
    rng = np.random.default_rng(seed)
    n = 80
    stamps = list(pd.date_range("2026-05-12T00:00:00Z", periods=n * 2, freq="1s"))
    if gap_after is not None:
        stamps = stamps[:gap_after] + [s + pd.Timedelta(seconds=60) for s in stamps[gap_after:]]
    return pd.DataFrame(
        {
            SESSION_COLUMN: ["clean"] * (n * 2),
            TIMESTAMP_COLUMN: stamps,
            "obd:RPM": np.concatenate([rng.normal(750, 8, n), rng.normal(2200, 50, n)]),
            "obd:SPEED": np.concatenate([np.zeros(n), rng.normal(35, 3, n)]),
            "obd:THROTTLE_POS": np.concatenate([rng.normal(18, 1, n), rng.normal(32, 2.5, n)]),
            "obd:COOLANT_TEMP": np.full(n * 2, 85.0),
        }
    )


def _config() -> CanaryConfig:
    return CanaryConfig.from_yaml(EXAMPLE_YAML)


def test_without_a_sequence_model_fused_is_withheld_not_zero() -> None:
    config = _config()
    features = featurize_wide_frame(_resampled(), config).features
    trained_if = fit_isolation_forest(
        features,
        _IFConfig(),
        feature_columns=["obd:RPM", "obd:SPEED", "obd:THROTTLE_POS"],
        fitted_at="2026-05-12T00:00:00Z",
    )
    stack = DeployedStack(config, trained_if, None)
    assert stack.names == ("isolation_forest",)
    assert "min_agreement" in stack.withheld["fused"]
    outputs = stack.score(features)
    assert set(outputs) == {"isolation_forest"}
    verdicts = outputs["isolation_forest"].verdict
    assert len(verdicts) == len(features)
    assert all(v in (True, False, None) for v in verdicts)


def test_batched_sequence_scoring_matches_the_served_engine() -> None:
    """Reproduce the known answer: the batched rule must agree with
    ``SequenceInferenceEngine.observe`` window for window — including across
    a time gap, where both restart the window."""
    pytest.importorskip("torch")
    from src.models.evaluation_detectors import BatchedSequenceScorer, sequence_parity
    from src.models.lstm_training import train_lstm_autoencoder
    from src.models.sequence_dataset import SequenceWindowDataset
    from src.preprocessing.feature_pipeline import sequence_feature_columns
    from src.preprocessing.normalization import ZScoreScaler

    config = _config()
    features = featurize_wide_frame(_resampled(gap_after=100), config).features
    columns = sequence_feature_columns(features)
    lstm_config = config.models.lstm_autoencoder.model_copy(
        update={
            "sequence_length": 4,
            "hidden_size": 6,
            "num_layers": 1,
            "latent_dim": 2,
            "batch_size": 8,
            "max_epochs": 1,
            "validation_fraction": 0.0,
            "random_state": 3,
            "threshold_quantile": 0.5,  # the median block maximum: some windows flag
        }
    )
    # As scripts/train_sequence.py does: the windows are z-scored with the
    # scaler the manifest carries, so calibration and serving share a scale.
    scaler = ZScoreScaler.fit(features, columns=columns)
    trained = train_lstm_autoencoder(
        SequenceWindowDataset(
            scaler.transform(features), sequence_length=4, feature_columns=columns
        ),
        lstm_config,
        trained_at="2026-05-12T00:00:00Z",
        allow_in_sample_calibration=True,
        scaler=scaler,
    )
    scorer = BatchedSequenceScorer(trained, batch_size=7)
    parity = sequence_parity(scorer, features, rows=len(features))
    assert parity.windows > 100
    assert parity.agrees, parity
    assert parity.max_score_difference < 1e-4
    scores = scorer.score(features)
    flagged = [v for v in scores.is_anomaly if v is not None]
    assert any(flagged) and not all(flagged)  # the check has teeth


def _baseline_frame(rows: int = 400, seed: int = 0) -> pd.DataFrame:
    rng = np.random.default_rng(seed)
    idle = np.arange(rows) < rows // 2
    return pd.DataFrame(
        {
            MODE_COLUMN: np.where(idle, "idle", "city"),
            "x": np.where(idle, rng.normal(10, 1, rows), rng.normal(50, 5, rows)),
            "x__gap": False,
            # constant at idle, moving in the city
            "y": np.where(idle, 3.0, rng.normal(0, 1, rows)),
        }
    )


def test_robust_z_is_per_mode_and_skips_degenerate_channels() -> None:
    model = RobustZBaseline.fit([_baseline_frame()], ["x", "y"])
    assert "y" in model.degenerate["idle"]
    probe = pd.DataFrame(
        {
            MODE_COLUMN: ["idle", "city", "idle"],
            "x": [20.0, 20.0, 10.0],
            "x__gap": [False, False, True],  # a held value is not an observation
            "y": [3.5, 0.0, 3.0],
        }
    )
    scores = model.scores(probe)
    assert scores[0] == pytest.approx(10.0, rel=0.15)  # (20 - 10) / ~1, y skipped
    assert scores[1] == pytest.approx(6.0, rel=0.2)  # (50 - 20) / ~5
    assert np.isnan(scores[2])  # nothing freshly observed with a usable spread


def test_pca_reconstruction_scores_off_manifold_rows_higher() -> None:
    rng = np.random.default_rng(1)
    base = rng.normal(0, 1, 500)
    frame = pd.DataFrame({"a": base + rng.normal(0, 0.05, 500), "b": base})
    model = PCABaseline.fit([frame], ["a", "b"], variance=0.9)
    probe = pd.DataFrame({"a": [1.0, 2.0], "b": [1.0, -2.0]})
    on, off = model.scores(probe)
    assert off > 10 * on


def test_a_thresholded_baseline_withholds_rows_it_cannot_score() -> None:
    model = RobustZBaseline.fit([_baseline_frame()], ["x"])
    detector = ThresholdedScore(model, threshold=3.0)
    probe = pd.DataFrame(
        {MODE_COLUMN: ["idle", "idle"], "x": [30.0, np.nan], "x__gap": [False, False]}
    )
    output = detector.score(probe)["robust_z"]
    assert list(output.verdict) == [True, None]


def test_raw_channels_drop_rolling_siblings() -> None:
    assert raw_channels(["a", "a__mean", "a__std", "b__roc", "b"]) == ("a", "b")


def test_sequential_frames_of_equal_length_and_start_never_share_windows() -> None:
    """Regression: an id()-keyed window cache served one frame's windows for
    the next when the first was freed and its id reused — the injection sweep
    scores many throwaway frames of identical length and first timestamp."""
    pytest.importorskip("torch")
    import gc

    from src.models.evaluation_detectors import BatchedSequenceScorer
    from src.models.lstm_training import train_lstm_autoencoder
    from src.models.sequence_dataset import SequenceWindowDataset
    from src.preprocessing.feature_pipeline import sequence_feature_columns
    from src.preprocessing.normalization import ZScoreScaler

    config = _config()
    base = featurize_wide_frame(_resampled(), config).features
    columns = sequence_feature_columns(base)
    scaler = ZScoreScaler.fit(base, columns=columns)
    trained = train_lstm_autoencoder(
        SequenceWindowDataset(scaler.transform(base), sequence_length=4, feature_columns=columns),
        config.models.lstm_autoencoder.model_copy(
            update={
                "sequence_length": 4,
                "hidden_size": 6,
                "num_layers": 1,
                "latent_dim": 2,
                "batch_size": 8,
                "max_epochs": 1,
                "validation_fraction": 0.0,
                "random_state": 3,
            }
        ),
        trained_at="2026-05-12T00:00:00Z",
        allow_in_sample_calibration=True,
        scaler=scaler,
    )
    scorer = BatchedSequenceScorer(trained)
    raw = _resampled()

    def frame_for(offset: float) -> pd.DataFrame:
        shifted = raw.copy()
        shifted["obd:RPM"] = shifted["obd:RPM"] + offset
        return featurize_wide_frame(shifted, config).features

    # Deterministic form: one object scored, changed in place, scored again.
    # Same id, length, columns and first timestamp — every key the old cache
    # used — so a cache of any of those returns the first frame's windows.
    frame = frame_for(0.0)
    first = scorer.score(frame).global_score
    changed = frame_for(300.0)
    frame.loc[:, :] = changed.to_numpy()
    second = scorer.score(frame).global_score
    fresh = BatchedSequenceScorer(trained).score(changed).global_score
    np.testing.assert_allclose(np.nan_to_num(second), np.nan_to_num(fresh), rtol=1e-6)
    assert not np.allclose(np.nan_to_num(first), np.nan_to_num(second))

    # And the sweep's shape: throwaway frames of identical length and start,
    # freed between scores so an id can be reused.
    results = []
    for offset in range(0, 400, 20):
        results.append(scorer.score(frame_for(float(offset))).global_score)
        gc.collect()
    for offset, got in zip(range(0, 400, 20), results, strict=True):
        fresh = BatchedSequenceScorer(trained).score(frame_for(float(offset))).global_score
        np.testing.assert_allclose(np.nan_to_num(got), np.nan_to_num(fresh), rtol=1e-6)


def _obd_session(root: Path, name: str, rpm: list[float], *, start: float) -> Path:
    from src.storage.parquet_writer import write_records_to_parquet
    from src.telemetry import TelemetryRecord

    records = []
    for i, value in enumerate(rpm):
        records.append(TelemetryRecord.obd(start + i, "RPM", value, "rpm"))
        records.append(TelemetryRecord.obd(start + i, "SPEED", 40.0, "km/h"))
        records.append(TelemetryRecord.obd(start + i, "COOLANT_TEMP", 88.0, "degC"))
    session = root / name
    write_records_to_parquet(records, session, max_rows=10_000)
    return session


def test_plausibility_confirmations_land_on_the_shared_row_grid(tmp_path: Path) -> None:
    """A05 as a harness detector: arrived samples in, verdicts on the same
    1 Hz rows every other detector is graded on (A02.5)."""
    from src.decode.bindings import load_bindings
    from src.models.evaluation_detectors import PlausibilityDetector
    from src.profiles import resolve_profile_for_tooling, scope_config

    config = _config()
    config = scope_config(config, resolve_profile_for_tooling(config, "nissan-xterra-2008"))
    detector = PlausibilityDetector(
        config, bindings=load_bindings(Path("data/bindings.yaml")), did_map=None
    )
    start = 1_778_544_000.0
    clean = _obd_session(
        tmp_path, "2026-05-12T000000", [800.0 + i for i in range(120)], start=start
    )
    # RPM past what the J1979 PID can even encode, for a minute: a range fault.
    impossible = [800.0] * 40 + [20_000.0] * 60 + [800.0] * 20
    faulted = _obd_session(tmp_path, "2026-05-13T000000", impossible, start=start + 86_400)

    grid = pd.Series(pd.date_range(pd.Timestamp(start, unit="s", tz="UTC"), periods=120, freq="1s"))
    quiet = detector.score_stream(detector.load(clean), grid)["plausibility"]
    assert quiet.pre_gated
    assert list(quiet.verdict) == [False] * 120

    grid2 = grid + pd.Timedelta(days=1)
    loud = detector.score_stream(detector.load(faulted), grid2)["plausibility"]
    flagged = [i for i, v in enumerate(loud.verdict) if v is True]
    assert flagged and 40 <= flagged[0] < 100

    # Window + restriction keep only the fault's answer group and span.
    stream = detector.load(faulted)
    window = detector.window(
        stream, grid2.iloc[30], grid2.iloc[90], channels=["obd:SHORT_FUEL_TRIM_1"]
    )
    assert set(window.samples["channel"]) <= {"obd:RPM", "obd:SPEED", "obd:COOLANT_TEMP"}
    assert window.samples["t"].min() >= grid2.iloc[30].timestamp()


def test_curated_mahalanobis_sees_a_broken_correlation_per_mode(tmp_path: Path) -> None:
    """A04.13: two trims that move together, each in its normal range, but
    against each other — invisible per channel, plain to the per-mode shrunk
    covariance. A row observing under half its channels is withheld."""
    from src.models.evaluation_detectors import MahalanobisBaseline, load_curated_channels

    rng = np.random.default_rng(3)
    n = 400
    common = rng.normal(0.0, 3.0, n)
    frame = pd.DataFrame(
        {
            MODE_COLUMN: ["city"] * n,
            "obd:STFT1": common + rng.normal(0, 0.3, n),
            "obd:STFT2": common + rng.normal(0, 0.3, n),
            "obd:MAF": rng.normal(20.0, 2.0, n),
        }
    )
    channels_file = tmp_path / "curated.yaml"
    channels_file.write_text("channels: [obd:STFT1, obd:STFT2, obd:MAF]\n")
    channels = load_curated_channels(channels_file)
    model = MahalanobisBaseline.fit([frame], channels)
    probe = pd.DataFrame(
        {
            MODE_COLUMN: ["city", "city", "city", "idle"],
            "obd:STFT1": [3.0, 3.0, 3.0, 3.0],
            "obd:STFT2": [3.0, -3.0, np.nan, 3.0],
            "obd:MAF": [20.0, 20.0, np.nan, 20.0],
        }
    )
    scores = model.scores(probe)
    assert scores[1] > 10 * scores[0]  # the anti-correlated row
    assert np.isnan(scores[2])  # 1 of 3 channels observed: withheld
    assert np.isnan(scores[3])  # no model for this mode

    empty = tmp_path / "empty.yaml"
    empty.write_text("channels: []\n")
    with pytest.raises(ValueError, match="non-empty"):
        load_curated_channels(empty)


def test_batched_scoring_follows_the_artifacts_mask_rule_and_context_columns() -> None:
    """A03.1/A03.2 on the harness: an artifact recording the arrival mask and
    context columns is scored by the batched rule exactly as the served
    engine scores it — ``__arrived`` (absent -> not arrived) for the mask, and
    context columns out of the score."""
    pytest.importorskip("torch")
    from dataclasses import replace as dc_replace

    from src.models.evaluation_detectors import BatchedSequenceScorer, sequence_parity
    from src.models.lstm_training import train_lstm_autoencoder
    from src.models.sequence_dataset import (
        MASK_ARRIVED_SAMPLE,
        OBSERVATION_MASK_SCHEMA_KEY,
        SequenceWindowDataset,
    )
    from src.models.sequence_feature_set import FEATURE_SET_SCHEMA_KEY
    from src.preprocessing.feature_pipeline import sequence_feature_columns
    from src.preprocessing.normalization import ZScoreScaler

    config = _config()
    features = featurize_wide_frame(_resampled(gap_after=100), config).features
    columns = sequence_feature_columns(features)
    lstm_config = config.models.lstm_autoencoder.model_copy(
        update={
            "sequence_length": 4,
            "hidden_size": 6,
            "num_layers": 1,
            "latent_dim": 2,
            "batch_size": 8,
            "max_epochs": 1,
            "validation_fraction": 0.0,
            "random_state": 3,
            "threshold_quantile": 0.5,
        }
    )
    scaler = ZScoreScaler.fit(features, columns=columns)
    trained = train_lstm_autoencoder(
        SequenceWindowDataset(
            scaler.transform(features), sequence_length=4, feature_columns=columns
        ),
        lstm_config,
        trained_at="2026-05-12T00:00:00Z",
        allow_in_sample_calibration=True,
        scaler=scaler,
    )
    context = [c for c in columns if c.startswith("obd:SPEED")]
    schema = {
        **dict(trained.manifest.schema),
        OBSERVATION_MASK_SCHEMA_KEY: {
            "rule": MASK_ARRIVED_SAMPLE,
            "freshness_fallback_columns": ["obd:COOLANT_TEMP"],
        },
        FEATURE_SET_SCHEMA_KEY: {"context_columns": context},
    }
    trained = dc_replace(trained, manifest=dc_replace(trained.manifest, schema=schema))
    frame = features.copy()
    for channel in ("obd:RPM", "obd:THROTTLE_POS", "obd:SPEED"):
        frame[f"{channel}__arrived"] = (np.arange(len(frame)) % 3) != 0
    frame["obd:RPM__arrived"] = frame["obd:RPM__arrived"].astype(object)
    frame.loc[frame.index[::7], "obd:RPM__arrived"] = np.nan  # unknown -> not arrived

    scorer = BatchedSequenceScorer(trained, batch_size=7)
    assert set(context) <= set(scorer.engine.context_columns)
    parity = sequence_parity(scorer, frame, rows=len(frame))
    assert parity.windows > 100
    assert parity.agrees, parity
    assert parity.max_score_difference < 1e-4
    # The rule really changed the input: freshness masking scores differently.
    legacy = BatchedSequenceScorer(
        dc_replace(
            trained,
            manifest=dc_replace(
                trained.manifest,
                schema={k: v for k, v in schema.items() if k != OBSERVATION_MASK_SCHEMA_KEY},
            ),
        ),
        batch_size=7,
    )
    arrival_scores = scorer.score(frame).global_score
    freshness_scores = legacy.score(frame).global_score
    assert not np.allclose(np.nan_to_num(arrival_scores), np.nan_to_num(freshness_scores))
