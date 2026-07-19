"""CI regression guard for detector quality (Phase 7.5).

Builds a tiny seeded multi-mode clean frame, fits IF with held-out
calibration, runs the synthetic-fault harness, and asserts both that
each fault kind is detected at a useful rate AND that the nominal
false-positive rate stays below a sane ceiling. The same module also
asserts that LSTM training completes and writes per-mode thresholds when
the validation set spans multiple modes — the explicit "LSTM training
completion" check 7.5 requires.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path

import numpy as np
import pandas as pd
import pytest

pytest.importorskip("joblib")
pytest.importorskip("sklearn")

from src.config import CanaryConfig  # noqa: E402
from src.models.isolation_forest import fit_isolation_forest  # noqa: E402
from src.models.synthetic_fault_eval import (  # noqa: E402
    DEFAULT_FAULT_KINDS,
    evaluate_detectors,
)
from src.preprocessing.feature_pipeline import featurize_wide_frame  # noqa: E402
from src.preprocessing.resampler import SESSION_COLUMN, TIMESTAMP_COLUMN  # noqa: E402
from src.preprocessing.segmenter import MODE_COLUMN  # noqa: E402

EXAMPLE_YAML = Path(__file__).resolve().parent.parent / "canary.example.yaml"


@dataclass(frozen=True)
class _IFConfig:
    enabled: bool = True
    n_estimators: int = 60
    contamination: float = 0.02
    per_mode_contamination: dict[str, float] = field(default_factory=dict)
    random_state: int = 7
    calibration_holdout_fraction: float = 0.2
    target_flag_rate: float = 0.05


def _two_mode_resampled(seed: int = 0) -> pd.DataFrame:
    """80 rows of clean idle + 80 rows of clean city, with the OBD signals the
    segmenter needs to actually produce two modes via the example config."""
    rng = np.random.default_rng(seed)
    n = 80
    timestamps = pd.date_range("2026-05-12T00:00:00Z", periods=n * 2, freq="1s")
    return pd.DataFrame(
        {
            SESSION_COLUMN: ["clean-a"] * n + ["clean-b"] * n,
            TIMESTAMP_COLUMN: timestamps,
            "obd:RPM": np.concatenate(
                [
                    rng.normal(750.0, 8.0, size=n),  # idle (rpm_max=900)
                    rng.normal(2200.0, 50.0, size=n),  # city
                ]
            ),
            "obd:SPEED": np.concatenate(
                [
                    np.zeros(n),  # idle (speed_max=0)
                    rng.normal(35.0, 3.0, size=n),  # city (speed_max=50)
                ]
            ),
            "obd:THROTTLE_POS": np.concatenate(
                [
                    rng.normal(18.0, 1.0, size=n),
                    rng.normal(32.0, 2.5, size=n),
                ]
            ),
            # Warm coolant for both halves so the warmup/idle split in the
            # example config resolves to idle (warmup requires
            # coolant_temp <= 70). Synthetic test data isn't modeling cold
            # starts; this just keeps the test's mode count stable across
            # the warmup-mode refactor.
            "obd:COOLANT_TEMP": np.full(n * 2, 85.0),
        }
    )


def _config() -> CanaryConfig:
    return CanaryConfig.from_yaml(EXAMPLE_YAML)


# ---------------------------------------------------------------------------
# Detector-quality regression guard
# ---------------------------------------------------------------------------


def test_evaluate_detectors_grades_each_fault_kind_and_keeps_nominal_fp_low() -> None:
    config = _config()
    resampled = _two_mode_resampled()
    # featurize → fit IF on the clean frame; calibration enabled in _IFConfig.
    nominal_features = featurize_wide_frame(resampled, config).features
    trained = fit_isolation_forest(
        nominal_features,
        _IFConfig(),
        feature_columns=["obd:RPM", "obd:SPEED", "obd:THROTTLE_POS"],
        fitted_at="2026-05-12T00:00:00Z",
    )
    # Sanity: held-out calibration produced per-mode thresholds for both modes.
    assert set(trained.score_thresholds) >= {"idle", "city"}

    report = evaluate_detectors(
        resampled,
        config,
        trained,
        fault_channel="obd:RPM",
        # 2.4 scores through the production PersistenceGate (3 consecutive
        # sustained ticks by default) instead of "any single row in-window" —
        # a real, more honest bar, but one a single random window placement
        # can legitimately miss (e.g. one that straddles the idle/city mode
        # boundary). More injections make the aggregate rate stable enough
        # to assert against without the test itself becoming placement-luck
        # flaky; deterministic (fixed default seed) so it never flakes in CI.
        n_injections=12,
    )

    # Report shape + JSON-safe (the training ledger / dashboard consume it).
    assert set(report) == {"fault_kinds", "n_injections", "seed", "nominal_fp", "per_fault"}
    json.loads(json.dumps(report))
    for metric in report["per_fault"]:
        assert set(metric) == {
            "detector",
            "fault_kind",
            "runs",
            "detected",
            "detection_rate",
            "latencies_seconds",
            "late_flags",
            "late_hits",
            "median_time_to_detect_seconds",
        }
        # 2.4: latency bookkeeping is internally consistent with `detected`.
        assert len(metric["latencies_seconds"]) == metric["detected"]
        assert all(latency >= 0.0 for latency in metric["latencies_seconds"])
        # 8.2: late_flags is parallel to latencies_seconds, one bool per hit.
        assert len(metric["late_flags"]) == metric["detected"]
        assert metric["late_hits"] == sum(metric["late_flags"])
        if metric["detected"] == 0:
            assert metric["median_time_to_detect_seconds"] is None
        else:
            assert metric["median_time_to_detect_seconds"] is not None

    # Every real sensor-failure mode (spike/drift/dropout/stuck) plus the
    # multiplicative `scale` fault must be caught by *some* placement — the
    # regression floor a persistence-gated, randomly-placed harness can
    # honestly promise. A detector that catches NONE of many placements of
    # an unambiguous fault (5x-clamped values, etc.) is broken, not unlucky.
    metrics = {(m["detector"], m["fault_kind"]): m["detection_rate"] for m in report["per_fault"]}
    for kind in DEFAULT_FAULT_KINDS:
        assert metrics[("isolation_forest", kind)] > 0.0, kind
        assert metrics[("fused", kind)] > 0.0, kind
    # dropout (8.1's realistic hold-then-NaN+gap signature) is the most
    # reliably-flagged fault in this corpus — every placement caught it.
    assert metrics[("isolation_forest", "dropout")] == 1.0
    # Every fault kind from DEFAULT_FAULT_KINDS got graded.
    assert {m["fault_kind"] for m in report["per_fault"]} == set(DEFAULT_FAULT_KINDS)

    # Nominal-FP ceiling: target_flag_rate=0.05 at 1 Hz is 180/hr, with
    # quantile-noise headroom on the tiny holdout split (~16 rows/mode). The
    # ceiling is set loose enough to absorb that noise while still catching
    # regressions that blow FP past ~10%; it is *not* a substitute for the
    # tighter Done-Definition v1 #7 (≤1 FP/hr) gate, which needs a real
    # held-out drive.
    assert report["nominal_fp"]["isolation_forest"]["per_hour"] < 400.0
    assert report["nominal_fp"]["fused"]["per_hour"] < 400.0


# ---------------------------------------------------------------------------
# 2.5 — IF feature-subset trial machinery (full 4x vs raw+__roc vs raw-only)
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "drop_suffixes",
    [(), ("__mean", "__std"), ("__mean", "__std", "__roc")],
    ids=["full_4x", "raw_roc_only", "raw_only"],
)
def test_if_feature_subset_trains_and_evaluates_through_the_benchmark_harness(
    drop_suffixes: tuple[str, ...],
) -> None:
    """The 2.5 trial machinery: restrict the full 4x IF feature set down to a
    subset, fit IF on exactly those columns, and grade it through the same
    ``evaluate_detectors`` harness used for the real full-vs-subset trial.
    Proves the plumbing works for every candidate subset (not that any one of
    them wins — that's a real-data call documented in the roadmap doc, not a
    test assertion)."""
    from src.preprocessing.feature_pipeline import (
        restrict_isolation_forest_expansion,
        trainable_feature_columns,
    )

    config = _config()
    resampled = _two_mode_resampled()
    nominal_features = featurize_wide_frame(resampled, config).features
    full_columns = [
        c
        for c in trainable_feature_columns(nominal_features)
        if c.split("__")[0] in {"obd:RPM", "obd:SPEED", "obd:THROTTLE_POS"}
    ]
    subset = restrict_isolation_forest_expansion(full_columns, drop_suffixes=drop_suffixes)
    assert subset  # the raw channels always survive every drop_suffixes combo

    trained = fit_isolation_forest(
        nominal_features,
        _IFConfig(),
        feature_columns=subset,
        fitted_at="2026-05-12T00:00:00Z",
    )
    # The trained artifact's schema reflects exactly the requested subset.
    assert set(trained.feature_columns) == set(subset)

    report = evaluate_detectors(
        resampled,
        config,
        trained,
        fault_channel="obd:RPM",
        n_injections=4,
    )
    json.loads(json.dumps(report))  # still JSON-safe on every subset
    assert {m["fault_kind"] for m in report["per_fault"]} == set(DEFAULT_FAULT_KINDS)
    assert "isolation_forest" in report["nominal_fp"]


# ---------------------------------------------------------------------------
# 2.3 — sequence-detector coverage (optional third detector)
# ---------------------------------------------------------------------------


def test_evaluate_detectors_skips_sequence_metrics_when_no_artifact_supplied() -> None:
    """No ``trained_sequence`` (the common case — no manifest configured/
    available) must still run the harness end-to-end with IF/fused only,
    not crash and not fabricate a ``sequence`` entry."""
    config = _config()
    resampled = _two_mode_resampled()
    nominal_features = featurize_wide_frame(resampled, config).features
    trained_if = fit_isolation_forest(
        nominal_features,
        _IFConfig(),
        feature_columns=["obd:RPM", "obd:SPEED", "obd:THROTTLE_POS"],
        fitted_at="2026-05-12T00:00:00Z",
    )

    report = evaluate_detectors(
        resampled,
        config,
        trained_if,
        fault_channel="obd:RPM",
        n_injections=3,
    )

    assert "sequence" not in report["nominal_fp"]
    assert {m["detector"] for m in report["per_fault"]} == {"isolation_forest", "fused"}


def test_evaluate_detectors_covers_sequence_detector_when_available() -> None:
    """When a sequence-detector artifact is supplied, every injection ticks
    it through the real ``SequenceInferenceEngine.observe`` windowed
    contract (not a shortcut) and a third ``detector="sequence"`` entry
    shows up per fault kind, gated through its own fresh ``PersistenceGate``
    key, with the same latency/late-flag bookkeeping as IF/fused."""
    pytest.importorskip("torch")
    from src.models.inference import SequenceInferenceEngine
    from src.models.lstm_training import train_lstm_autoencoder
    from src.models.sequence_dataset import SequenceWindowDataset
    from src.models.synthetic_fault_eval import _sequence_anomalous
    from src.preprocessing.feature_pipeline import sequence_feature_columns

    config = _config()
    resampled = _two_mode_resampled()
    nominal_features = featurize_wide_frame(resampled, config).features
    trained_if = fit_isolation_forest(
        nominal_features,
        _IFConfig(),
        feature_columns=["obd:RPM", "obd:SPEED", "obd:THROTTLE_POS"],
        fitted_at="2026-05-12T00:00:00Z",
    )

    seq_feature_columns = sequence_feature_columns(nominal_features)
    lstm_config = config.models.lstm_autoencoder.model_copy(
        update={
            "sequence_length": 4,
            "hidden_size": 6,
            "num_layers": 1,
            "latent_dim": 2,
            "batch_size": 4,
            "learning_rate": 0.01,
            "max_epochs": 1,
            "validation_fraction": 0.0,
            "early_stopping_patience": 2,
            "random_state": 3,
            "threshold_k": 2.0,
            # 3.2 flipped the config default to a quantile — pin the k-sigma
            # path this test's threshold_k was tuned against.
            "threshold_quantile": None,
        }
    )
    dataset = SequenceWindowDataset(
        nominal_features,
        sequence_length=4,
        feature_columns=seq_feature_columns,
    )
    trained_sequence = train_lstm_autoencoder(
        dataset,
        lstm_config,
        trained_at="2026-05-12T00:00:00Z",
        model_version="ci-sequence-eval",
        allow_in_sample_calibration=True,
    )

    report = evaluate_detectors(
        resampled,
        config,
        trained_if,
        fault_channel="obd:RPM",
        n_injections=3,
        trained_sequence=trained_sequence,
    )

    assert "sequence" in report["nominal_fp"]
    json.loads(json.dumps(report))  # still JSON-safe with the third detector
    sequence_metrics = [m for m in report["per_fault"] if m["detector"] == "sequence"]
    assert {m["fault_kind"] for m in sequence_metrics} == set(DEFAULT_FAULT_KINDS)
    for metric in sequence_metrics:
        assert metric["runs"] == 3
        assert len(metric["latencies_seconds"]) == metric["detected"]
        assert all(latency >= 0.0 for latency in metric["latencies_seconds"])
        assert len(metric["late_flags"]) == metric["detected"]
        assert metric["late_hits"] == sum(metric["late_flags"])
        if metric["detected"] == 0:
            assert metric["median_time_to_detect_seconds"] is None
        else:
            assert metric["median_time_to_detect_seconds"] is not None

    # The bookkeeping above holds vacuously if `_sequence_anomalous` never
    # actually flags a `True` — confirmed empirically that a 1-epoch (even
    # 20-epoch) toy LSTM this tiny catches none of the 5 synthetic fault
    # kinds across only 3 placements each, so asserting `detected > 0`
    # through the full injection pipeline would be either always-false or
    # flaky depending on training luck, not a real regression gate. Prove
    # the observe-loop wiring is actually live instead: feed an obviously
    # out-of-distribution window (RPM channels blown up ~1000x) through the
    # exact same `_sequence_anomalous` helper `evaluate_detectors` calls
    # internally. If that helper (or the `observe`/`score_window` wiring
    # behind it) were stubbed to always return `False`, this fails.
    engine = SequenceInferenceEngine(trained_sequence)
    ood_features = nominal_features.copy()
    tail = ood_features.index[-10:]
    for column in seq_feature_columns:
        if "RPM" in column:
            ood_features.loc[tail, column] = ood_features.loc[tail, column] * 1000.0 + 1_000_000.0
    ood_result = _sequence_anomalous(engine, ood_features)
    assert bool(ood_result.any()), (
        "sequence-detector wiring never fires even on an obvious OOD window"
    )


# ---------------------------------------------------------------------------
# 8.2 — post-window detection grace
# ---------------------------------------------------------------------------


def _fired_series(n: int, *, fire_at: int) -> pd.Series:
    """All-False boolean series of length ``n`` except row ``fire_at`` and
    every row after it (a sustained streak from that point on)."""
    values = [i >= fire_at for i in range(n)]
    return pd.Series(values)


def test_first_sustained_detection_counts_post_window_hit_within_grace() -> None:
    """A detection that lands just after the fault window's `end` but
    inside `end + window_seconds` counts as a hit, flagged late — not
    conflated with an in-window hit."""
    from src.alerts.fusion import PersistenceGate
    from src.models.synthetic_fault_eval import _first_sustained_detection

    timestamps = pd.Series(pd.date_range("2026-05-12T00:00:00Z", periods=21, freq="1s"))
    match_start = timestamps.iloc[5]
    match_end = timestamps.iloc[10]  # fault window is [5, 10)
    window_seconds = 3
    grace_end = match_end + pd.Timedelta(seconds=window_seconds)  # ts[13]

    # Detector only sustains starting at row 11 (1s after `end`, i.e. inside
    # the 3s grace period that closes at row 13).
    fired = _fired_series(len(timestamps), fire_at=11)

    hits = _first_sustained_detection(
        signals={"isolation_forest": fired, "fused": fired},
        timestamps=timestamps,
        persistence=PersistenceGate(required=1),
        match_start=match_start,
        match_end=match_end,
        grace_end=grace_end,
    )

    if_hit = hits["isolation_forest"]
    assert if_hit is not None
    if_ts, if_late = if_hit
    assert if_ts == timestamps.iloc[11]
    assert if_late is True  # detected at/after `end`, not strictly inside [start, end)

    fused_hit = hits["fused"]
    assert fused_hit is not None
    _, fused_late = fused_hit
    assert fused_late is True


def test_first_sustained_detection_still_misses_beyond_grace_period() -> None:
    """No regression on the boundary: a detection beyond `end + window_seconds`
    still counts as a miss."""
    from src.alerts.fusion import PersistenceGate
    from src.models.synthetic_fault_eval import _first_sustained_detection

    timestamps = pd.Series(pd.date_range("2026-05-12T00:00:00Z", periods=21, freq="1s"))
    match_start = timestamps.iloc[5]
    match_end = timestamps.iloc[10]  # fault window is [5, 10)
    window_seconds = 3
    grace_end = match_end + pd.Timedelta(seconds=window_seconds)  # ts[13]

    # Detector only sustains starting at row 15 — 5s after `end`, past the
    # 3s grace period.
    fired = _fired_series(len(timestamps), fire_at=15)

    hits = _first_sustained_detection(
        signals={"isolation_forest": fired, "fused": fired},
        timestamps=timestamps,
        persistence=PersistenceGate(required=1),
        match_start=match_start,
        match_end=match_end,
        grace_end=grace_end,
    )

    assert hits["isolation_forest"] is None
    assert hits["fused"] is None


def test_first_sustained_detection_in_window_hit_is_not_late() -> None:
    """A hit strictly inside [start, end) stays distinguishable from a late
    (post-`end`) one."""
    from src.alerts.fusion import PersistenceGate
    from src.models.synthetic_fault_eval import _first_sustained_detection

    timestamps = pd.Series(pd.date_range("2026-05-12T00:00:00Z", periods=21, freq="1s"))
    match_start = timestamps.iloc[5]
    match_end = timestamps.iloc[10]  # fault window is [5, 10)
    grace_end = match_end + pd.Timedelta(seconds=3)

    fired = _fired_series(len(timestamps), fire_at=6)  # inside [5, 10)

    hits = _first_sustained_detection(
        signals={"isolation_forest": fired, "fused": fired},
        timestamps=timestamps,
        persistence=PersistenceGate(required=1),
        match_start=match_start,
        match_end=match_end,
        grace_end=grace_end,
    )

    if_hit = hits["isolation_forest"]
    assert if_hit is not None
    if_ts, if_late = if_hit
    assert if_ts == timestamps.iloc[6]
    assert if_late is False


# ---------------------------------------------------------------------------
# LSTM training completion + per-mode thresholds (the 7.5 "assert LSTM
# training completion" check; per_mode_errors populated when modes present).
# ---------------------------------------------------------------------------


def test_lstm_training_completes_and_populates_per_mode_thresholds() -> None:
    pytest.importorskip("torch")

    from src.models.lstm_training import train_lstm_autoencoder
    from src.models.sequence_dataset import SequenceWindowDataset

    rng = np.random.default_rng(1)
    n = 60
    frame = pd.DataFrame(
        {
            SESSION_COLUMN: ["m"] * (n * 2),
            TIMESTAMP_COLUMN: pd.date_range("2026-05-12T00:00:00Z", periods=n * 2, freq="1s"),
            "rpm": np.concatenate([rng.normal(700, 5, size=n), rng.normal(2000, 30, size=n)]),
            "speed": np.concatenate([np.zeros(n), rng.normal(35, 2, size=n)]),
            MODE_COLUMN: ["idle"] * n + ["city"] * n,
        }
    )
    dataset = SequenceWindowDataset(
        frame,
        sequence_length=4,
        feature_columns=["rpm", "speed"],
    )

    config = CanaryConfig.from_yaml(EXAMPLE_YAML).models.lstm_autoencoder
    tiny = config.model_copy(
        update={
            "sequence_length": 4,
            "hidden_size": 6,
            "num_layers": 1,
            "latent_dim": 2,
            "batch_size": 4,
            "learning_rate": 0.01,
            "max_epochs": 2,
            "validation_fraction": 0.25,
            "early_stopping_patience": 2,
            "random_state": 5,
            "threshold_k": 2.0,
        }
    )
    trained = train_lstm_autoencoder(
        dataset,
        tiny,
        trained_at="2026-05-12T00:00:00Z",
        model_version="ci-lstm",
        allow_in_sample_calibration=True,
    )
    assert trained.manifest.thresholds is not None
    thresholds = dict(trained.manifest.thresholds)
    per_mode = thresholds.get("per_mode_errors")
    assert per_mode is not None and per_mode, "per-mode thresholds missing in multi-mode dataset"
    # Each persisted mode has a finite threshold (the calibration completed).
    for mode, dist in per_mode.items():
        assert isinstance(mode, str)
        assert np.isfinite(dist["threshold"])
