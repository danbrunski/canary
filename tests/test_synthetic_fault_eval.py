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
from pathlib import Path

import numpy as np
import pandas as pd
import pytest

pytest.importorskip("joblib")
pytest.importorskip("sklearn")

from src.columns import (  # noqa: E402
    MODE_COLUMN,  # noqa: E402
    SESSION_COLUMN,
    TIMESTAMP_COLUMN,
)
from src.config import CanaryConfig  # noqa: E402
from src.decode.dids import load_repo_did_map
from src.features.feature_pipeline import featurize_wide_frame  # noqa: E402
from src.models.fusion import PersistenceGate  # noqa: E402
from src.models.isolation_forest import fit_isolation_forest  # noqa: E402
from src.models.synthetic_fault_eval import (  # noqa: E402
    DEFAULT_FAULT_KINDS,
    INTERMITTENT_FAULT_KIND,
    _if_anomalous,
    evaluate_detectors,
)
from tests.builders import IFConfig
from tests.configs import example_config

EXAMPLE_YAML = Path(__file__).resolve().parent.parent / "canary.example.yaml"


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
    return example_config(EXAMPLE_YAML)


# ---------------------------------------------------------------------------
# Detector-quality regression guard
# ---------------------------------------------------------------------------


def test_evaluate_detectors_grades_each_fault_kind_and_keeps_nominal_fp_low() -> None:
    config = _config()
    resampled = _two_mode_resampled()
    # featurize → fit IF on the clean frame; calibration enabled in IFConfig.
    nominal_features = featurize_wide_frame(
        resampled, config, behavior_models=None, did_map=load_repo_did_map()
    ).features
    trained = fit_isolation_forest(
        nominal_features,
        IFConfig(),
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
        did_map=None,
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
    assert set(report) == {
        "fault_kinds",
        "n_injections",
        "seed",
        "nominal_fp",
        "per_fault",
        "withheld",
        "sequence_forward_every_n_ticks",
    }
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

    # Persistence intentionally suppresses isolated spikes and shallow drifts
    # in this tiny corpus. Sustained/structural faults must still be detected.
    metrics = {(m["detector"], m["fault_kind"]): m["detection_rate"] for m in report["per_fault"]}
    for kind in ("dropout", "stuck", "scale"):
        assert metrics[("isolation_forest", kind)] > 0.0, kind
    # A02.9: fusion needs two independent sources and only the IF ran, so fused
    # cannot fire by construction. It is withheld with that reason, never
    # graded as a recall of 0.0 (the 2026-09-20 benchmark's "fused 0 of 250").
    assert not any(detector == "fused" for detector, _ in metrics)
    assert "min_agreement" in report["withheld"]["fused"]
    # Every fault kind from DEFAULT_FAULT_KINDS got graded.
    assert {m["fault_kind"] for m in report["per_fault"]} == set(DEFAULT_FAULT_KINDS)

    # Nominal-FP ceiling: target_flag_rate=0.05 at 1 Hz is 180/hr, with
    # quantile-noise headroom on the tiny holdout split (~16 rows/mode). The
    # ceiling is set loose enough to absorb that noise while still catching
    # regressions that blow FP past ~10%; it is *not* a substitute for the
    # tighter Done-Definition v1 #7 (≤1 FP/hr) gate, which needs a real
    # held-out drive.
    assert report["nominal_fp"]["isolation_forest"]["per_hour"] < 400.0
    # 6.2: single-source fused can never satisfy the agreement gate, so it has
    # no nominal rate either — withheld, not a measured zero.
    assert "fused" not in report["nominal_fp"]


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
    from src.features.feature_pipeline import (
        restrict_isolation_forest_expansion,
        trainable_feature_columns,
    )

    config = _config()
    resampled = _two_mode_resampled()
    nominal_features = featurize_wide_frame(
        resampled, config, behavior_models=None, did_map=load_repo_did_map()
    ).features
    full_columns = [
        c
        for c in trainable_feature_columns(nominal_features)
        if c.split("__")[0] in {"obd:RPM", "obd:SPEED", "obd:THROTTLE_POS"}
    ]
    subset = restrict_isolation_forest_expansion(full_columns, drop_suffixes=drop_suffixes)
    assert subset  # the raw channels always survive every drop_suffixes combo

    trained = fit_isolation_forest(
        nominal_features,
        IFConfig(),
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
        did_map=None,
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
    nominal_features = featurize_wide_frame(
        resampled, config, behavior_models=None, did_map=load_repo_did_map()
    ).features
    trained_if = fit_isolation_forest(
        nominal_features,
        IFConfig(),
        feature_columns=["obd:RPM", "obd:SPEED", "obd:THROTTLE_POS"],
        fitted_at="2026-05-12T00:00:00Z",
    )

    report = evaluate_detectors(
        resampled,
        config,
        trained_if,
        fault_channel="obd:RPM",
        did_map=None,
        n_injections=3,
    )

    assert "sequence" not in report["nominal_fp"]
    assert {m["detector"] for m in report["per_fault"]} == {"isolation_forest"}
    assert set(report["withheld"]) == {"fused"}


def test_evaluate_detectors_covers_sequence_detector_when_available() -> None:
    """When a sequence-detector artifact is supplied, every injection ticks
    it through the real ``SequenceInferenceEngine.observe`` windowed
    contract (not a shortcut) and a third ``detector="sequence"`` entry
    shows up per fault kind, gated through its own fresh ``PersistenceGate``
    key, with the same latency/late-flag bookkeeping as IF/fused."""
    pytest.importorskip("torch")
    from src.features.feature_pipeline import sequence_feature_columns
    from src.models.inference import SequenceInferenceEngine
    from src.models.lstm_training import train_lstm_autoencoder
    from src.models.sequence_dataset import SequenceWindowDataset
    from src.models.synthetic_fault_eval import _sequence_anomalous

    config = _config()
    resampled = _two_mode_resampled()
    nominal_features = featurize_wide_frame(
        resampled, config, behavior_models=None, did_map=load_repo_did_map()
    ).features
    trained_if = fit_isolation_forest(
        nominal_features,
        IFConfig(),
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
        did_map=None,
        n_injections=3,
        trained_sequence=trained_sequence,
    )

    assert "sequence" in report["nominal_fp"]
    json.loads(json.dumps(report))  # still JSON-safe with the third detector
    # A02.9: two sources ran, so fusion is graded rather than withheld.
    assert report["withheld"] == {}
    assert "fused" in report["nominal_fp"]
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
# 6.5 — benchmark the M-of-N persistence gate (6.1) against the pre-6.1
# strict-N-consecutive gate on the fire-gap-fire "intermittent" fault kind.
# ---------------------------------------------------------------------------


def _gated_alert_count(
    signal: pd.Series, timestamps: pd.Series, gate: PersistenceGate, key: str
) -> int:
    """Count alert *events* (transitions into sustained), not gated-true
    ticks — the real analog of "how many times would this actually alert",
    since the live daemon only emits on the sustain transition, not once per
    still-firing tick after that."""
    count = 0
    was_sustained = False
    for ts, fired in zip(timestamps, signal, strict=True):
        verdict = None if fired is None or pd.isna(fired) else bool(fired)
        sustained = gate.observe(key, verdict, now=ts.timestamp())
        emitting = verdict is True and sustained
        if emitting and not was_sustained:
            count += 1
        was_sustained = emitting
    return count


def test_intermittent_fault_caught_by_m_of_n_not_strict_at_unchanged_clean_fp() -> None:
    """6.5: on the SAME injected fire-gap-fire placements (deterministic
    seed), 6.1's M-of-N gate (3-of-5, 8s) genuinely detects the
    ``"intermittent"`` fault kind more often than the pre-6.1 strict-3-
    consecutive gate would have — without inflating the clean-drive alert
    count. Uses a looser calibration (``target_flag_rate=0.15`` vs the
    CI-floor test's ``0.05``) so individual on-ticks cross IF's threshold
    often enough to exercise the gates at all; IsolationForest's score
    saturates for far-outlier single-feature excursions (verified
    empirically), so the tight production calibration barely fires per-tick
    on this tiny synthetic corpus regardless of gate — this test isolates
    the gate's own effect, not IF's absolute sensitivity (that's the
    existing CI-floor test's job)."""
    config = _config()
    resampled = _two_mode_resampled()
    nominal_features = featurize_wide_frame(
        resampled, config, behavior_models=None, did_map=load_repo_did_map()
    ).features
    trained = fit_isolation_forest(
        nominal_features,
        IFConfig(target_flag_rate=0.15),
        feature_columns=["obd:RPM", "obd:SPEED", "obd:THROTTLE_POS"],
        fitted_at="2026-05-12T00:00:00Z",
    )

    # config.alerts is already the 3-of-5/8s M-of-N default (canary.example.yaml,
    # 6.1). strict_config reproduces the exact pre-6.1 gate: `window=None`
    # falls back to `required` (strict-N-consecutive), `window_seconds=None`
    # disables the wall-clock bound entirely — see `AlertsConfig` docstring.
    strict_config = config.model_copy(
        update={
            "alerts": config.alerts.model_copy(
                update={"persistence_window_ticks": None, "persistence_window_seconds": None}
            )
        }
    )

    # `evaluate_detectors` seeds its own rng from `seed` (default 42), so
    # both calls draw the identical sequence of injection placements —
    # the comparison is apples-to-apples on the SAME injected data, only the
    # gate differs.
    strict_report = evaluate_detectors(
        resampled,
        strict_config,
        trained,
        fault_channel="obd:RPM",
        did_map=None,
        fault_kinds=(INTERMITTENT_FAULT_KIND,),
        n_injections=30,
    )
    m_of_n_report = evaluate_detectors(
        resampled,
        config,
        trained,
        fault_channel="obd:RPM",
        did_map=None,
        fault_kinds=(INTERMITTENT_FAULT_KIND,),
        n_injections=30,
    )

    def _if_rate(report: dict) -> float:
        (metric,) = [
            m
            for m in report["per_fault"]
            if m["detector"] == "isolation_forest" and m["fault_kind"] == "intermittent"
        ]
        return float(metric["detection_rate"])

    strict_rate = _if_rate(strict_report)
    m_of_n_rate = _if_rate(m_of_n_report)

    # The load-bearing claim: M-of-N does meaningfully better on the exact
    # same fire-gap-fire placements a strict gate structurally struggles
    # with (each on-burst is only 2 ticks — never 3 consecutive).
    assert m_of_n_rate >= strict_rate + 0.2, (strict_rate, m_of_n_rate)
    assert strict_rate <= 0.5, strict_rate
    assert m_of_n_rate >= 0.5, m_of_n_rate

    # Nominal FP is production-gated, so compare both the report's emitting
    # tick count and the transition/event count below.
    assert (
        m_of_n_report["nominal_fp"]["isolation_forest"]["alerts"]
        <= strict_report["nominal_fp"]["isolation_forest"]["alerts"]
    )
    nominal_scored = trained.score(nominal_features)
    nominal_signal = _if_anomalous(nominal_scored)
    nominal_timestamps = pd.to_datetime(nominal_features[TIMESTAMP_COLUMN], utc=True)
    strict_gate = PersistenceGate(required=3, window=3)
    m_of_n_gate = PersistenceGate(required=3, window=5, window_seconds=8.0)
    strict_clean_alerts = _gated_alert_count(
        nominal_signal, nominal_timestamps, strict_gate, "isolation_forest"
    )
    m_of_n_clean_alerts = _gated_alert_count(
        nominal_signal, nominal_timestamps, m_of_n_gate, "isolation_forest"
    )
    # M-of-N must not alert MORE than strict on clean data — the flood-
    # protection guarantee 6.1's docstring promises is preserved even
    # though it's now tolerant of flicker on real faults.
    assert m_of_n_clean_alerts <= strict_clean_alerts, (strict_clean_alerts, m_of_n_clean_alerts)


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


# ---------------------------------------------------------------------------
# A02.9 — fusion graded on the sources the daemon fuses
# ---------------------------------------------------------------------------


def _scored_frame(n: int, *, if_fires: bool) -> pd.DataFrame:
    from src.models.isolation_forest import (
        CALIBRATED_THRESHOLD_COLUMN,
        MISSING_FEATURES_COLUMN,
        MODEL_MODE_COLUMN,
        SCORE_COLUMN,
    )

    return pd.DataFrame(
        {
            TIMESTAMP_COLUMN: pd.date_range("2026-09-20T00:00:00Z", periods=n, freq="1s"),
            MODEL_MODE_COLUMN: ["city"] * n,
            CALIBRATED_THRESHOLD_COLUMN: [0.1] * n,
            SCORE_COLUMN: [0.5 if if_fires else 0.0] * n,
            MISSING_FEATURES_COLUMN: [False] * n,
        }
    )


def test_fused_fires_only_when_both_sources_are_sustained_together() -> None:
    """The daemon gates each source, then fuses; one voice never fuses (6.2)."""
    from types import SimpleNamespace

    from src.models.fusion import FusionScorer
    from src.models.synthetic_fault_eval import _fused_anomalous

    config = _config()
    fusion = FusionScorer(config.models.fusion)
    n = 6
    firing_sequence = [
        SimpleNamespace(global_score=2.0, global_threshold=1.0, is_anomaly=True) for _ in range(n)
    ]
    quiet_sequence = [
        SimpleNamespace(global_score=0.1, global_threshold=1.0, is_anomaly=False) for _ in range(n)
    ]

    both = _fused_anomalous(
        _scored_frame(n, if_fires=True),
        fusion=fusion,
        scales={"city": 0.1},
        config=config,
        sequence_results=firing_sequence,  # type: ignore[arg-type]
    )
    required = config.alerts.min_consecutive_ticks
    assert list(both) == [False] * (required - 1) + [True] * (n - required + 1)

    if_only = _fused_anomalous(
        _scored_frame(n, if_fires=True),
        fusion=fusion,
        scales={"city": 0.1},
        config=config,
        sequence_results=quiet_sequence,  # type: ignore[arg-type]
    )
    assert not any(bool(v) for v in if_only)


# ---------------------------------------------------------------------------
# A02.6 — realistic faults
# ---------------------------------------------------------------------------


def _engine_frame(n: int = 600, *, idle_rows: int = 0) -> pd.DataFrame:
    """A running, closed-loop drive: idle rows first, then cruise."""
    timestamps = pd.date_range("2026-09-20T00:00:00Z", periods=n, freq="1s")
    idle = np.arange(n) < idle_rows
    return pd.DataFrame(
        {
            SESSION_COLUMN: ["s"] * n,
            TIMESTAMP_COLUMN: timestamps,
            "obd:RPM": np.where(idle, 750.0, 2200.0),
            "obd:SPEED": np.where(idle, 0.0, 60.0),
            "obd:MAF": np.where(idle, 4.0, 20.0),
            "obd:ENGINE_LOAD": np.full(n, 30.0),
            "obd:SHORT_FUEL_TRIM_1": np.zeros(n),
            "obd:SHORT_FUEL_TRIM_2": np.zeros(n),
            "obd:FUEL_STATUS": np.full(n, 2.0),
            "obd:COOLANT_TEMP": np.full(n, 88.0),
            "obd:O2_WR_LAMBDA_S1": np.where(np.arange(n) % 20 < 10, 0.98, 1.02),
            "decoded:ECM_COOLANT_TEMP": np.full(n, 128.0),  # degC + 40 raw counts
        }
    )


def test_maf_gain_scales_every_airflow_channel_and_trims_compensate() -> None:
    from src.preprocessing.anomaly_injection import inject_realistic_fault

    frame = _engine_frame()
    start, end = frame[TIMESTAMP_COLUMN].iloc[200], frame[TIMESTAMP_COLUMN].iloc[440]
    out, injection = inject_realistic_fault(
        frame, kind="maf_gain", severity=0.95, start=start, end=end
    )
    inside = slice(200, 439)
    assert np.allclose(out["obd:MAF"].iloc[inside], 20.0 * 0.95)
    assert np.allclose(out["obd:ENGINE_LOAD"].iloc[inside], 30.0 * 0.95)
    assert np.allclose(out["obd:SHORT_FUEL_TRIM_1"].iloc[inside], (1 / 0.95 - 1) * 100)
    assert np.allclose(out["obd:SHORT_FUEL_TRIM_2"].iloc[inside], (1 / 0.95 - 1) * 100)
    # Outside the window nothing moves.
    assert out["obd:MAF"].iloc[:200].eq(20.0).all()
    assert out["obd:MAF"].iloc[440:].eq(20.0).all()
    # An empty result says which kind of empty: the absent sibling is named.
    assert injection.not_perturbed == {"obd:ABSOLUTE_LOAD": "absent"}
    assert set(injection.perturbed) == {
        "obd:MAF",
        "obd:ENGINE_LOAD",
        "obd:SHORT_FUEL_TRIM_1",
        "obd:SHORT_FUEL_TRIM_2",
    }


def test_thermostat_fault_moves_decoded_counts_through_the_binding_scale() -> None:
    """A physical 10 degC drop is 10 raw counts on a degC+40 broadcast channel
    — the fault is applied in engineering units, never to raw counts."""
    from src.decode.engineering import repo_bindings
    from src.preprocessing.anomaly_injection import THERMOSTAT_RAMP_SECONDS, inject_realistic_fault

    frame = _engine_frame()
    start = frame[TIMESTAMP_COLUMN].iloc[100]
    out, injection = inject_realistic_fault(
        frame,
        kind="thermostat",
        severity=10.0,
        start=start,
        end=start + pd.Timedelta(seconds=300),
        roles={"coolant": ("obd:COOLANT_TEMP", "decoded:ECM_COOLANT_TEMP")},
        bindings=repo_bindings(),
    )
    plateau = 100 + int(THERMOSTAT_RAMP_SECONDS) + 10
    assert out["obd:COOLANT_TEMP"].iloc[plateau] == pytest.approx(78.0)
    assert out["decoded:ECM_COOLANT_TEMP"].iloc[plateau] == pytest.approx(118.0)
    # The ramp starts at zero: the first window row is unchanged.
    assert out["obd:COOLANT_TEMP"].iloc[100] == pytest.approx(88.0)
    assert "decoded:ECM_COOLANT_TEMP" in injection.perturbed


def test_lazy_o2_lags_the_sensor_without_changing_its_mean_level() -> None:
    from src.preprocessing.anomaly_injection import inject_realistic_fault

    frame = _engine_frame()
    start, end = frame[TIMESTAMP_COLUMN].iloc[100], frame[TIMESTAMP_COLUMN].iloc[400]
    out, _ = inject_realistic_fault(frame, kind="lazy_o2", severity=10.0, start=start, end=end)
    before = frame["obd:O2_WR_LAMBDA_S1"].iloc[100:400]
    after = out["obd:O2_WR_LAMBDA_S1"].iloc[100:400]
    assert after.std() < 0.5 * before.std()  # the swing is damped
    assert after.mean() == pytest.approx(before.mean(), abs=0.01)


def test_idle_air_leak_signature_is_largest_at_idle() -> None:
    from src.preprocessing.anomaly_injection import inject_realistic_fault

    frame = _engine_frame(idle_rows=300)
    start, end = frame[TIMESTAMP_COLUMN].iloc[200], frame[TIMESTAMP_COLUMN].iloc[400]
    out, _ = inject_realistic_fault(frame, kind="idle_air_leak", severity=0.4, start=start, end=end)
    idle_trim = out["obd:SHORT_FUEL_TRIM_1"].iloc[250]
    cruise_trim = out["obd:SHORT_FUEL_TRIM_1"].iloc[350]
    assert idle_trim == pytest.approx(100 * 0.4 / 4.0)
    assert cruise_trim == pytest.approx(100 * 0.4 / 20.0)


def test_fault_window_names_why_a_drive_cannot_carry_the_fault() -> None:
    from src.preprocessing.anomaly_injection import choose_fault_window

    no_idle = _engine_frame(idle_rows=0)
    reason = choose_fault_window(
        no_idle,
        kind="idle_air_leak",
        length_seconds=120,
        lead_seconds=60,
        rng=np.random.default_rng(0),
    )
    assert isinstance(reason, str) and "idle_air_leak" in reason

    window = choose_fault_window(
        _engine_frame(idle_rows=600),
        kind="idle_air_leak",
        length_seconds=120,
        lead_seconds=60,
        rng=np.random.default_rng(0),
    )
    assert isinstance(window, tuple)


def test_fault_channel_roles_refuse_an_unknown_role(tmp_path: Path) -> None:
    from src.preprocessing.anomaly_injection import load_fault_channel_roles

    good = tmp_path / "good.yaml"
    good.write_text("roles:\n  coolant: [decoded:ECM_COOLANT_TEMP]\n")
    roles = load_fault_channel_roles(good)
    assert roles["coolant"] == ("obd:COOLANT_TEMP", "decoded:ECM_COOLANT_TEMP")

    bad = tmp_path / "bad.yaml"
    bad.write_text("roles:\n  coolent: [obd:COOLANT_TEMP]\n")
    with pytest.raises(ValueError, match="unknown fault-channel roles"):
        load_fault_channel_roles(bad)
