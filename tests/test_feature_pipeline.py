"""Tests for the shared wide→features builder (7.1 keystone).

``featurize_wide_frame`` is the single transform both the offline batch path
(``build_feature_frame``) and the online streaming path
(``LiveFeatureSource.tick``) run, so the feature schema can never diverge by
hand. The live/offline equivalence itself is guarded by
``test_preprocessing_live.test_live_and_offline_obd_feature_rows_share_contract``;
these tests lock the builder's own contract.
"""

from __future__ import annotations

from pathlib import Path

import pandas as pd
import pytest

from src.config import CanaryConfig
from src.decode.behavior_models import BehaviorModels
from src.preprocessing.decode import decode_can_rows
from src.preprocessing.feature_pipeline import (
    ALL_MODE,
    NON_TRAINABLE_COUNTER_CHANNELS,
    FeaturizedFrame,
    _drop_fully_stale_rows,
    align_features_to_schema,
    candidate_feature_columns,
    featurize_wide_frame,
    restrict_isolation_forest_expansion,
    sequence_feature_columns,
    summarize_feature_health,
    trainable_feature_columns,
)
from src.preprocessing.features import MEAN_SUFFIX, ROC_SUFFIX, STD_SUFFIX, FeatureConfig
from src.preprocessing.resampler import GAP_SUFFIX
from src.preprocessing.segmenter import MODE_COLUMN

_ROLL_SUFFIXES = ("__mean", "__std", "__roc")


def test_drop_fully_stale_rows_removes_gap_filled_rows() -> None:
    # row 0: fresh; row 1: only one channel stale (kept); row 2: all stale (dropped).
    frame = pd.DataFrame(
        {
            "obd:RPM": [800.0, 800.0, 800.0],
            "obd:SPEED": [0.0, 0.0, 0.0],
            f"obd:RPM{GAP_SUFFIX}": [False, False, True],
            f"obd:SPEED{GAP_SUFFIX}": [False, True, True],
        }
    )
    out = _drop_fully_stale_rows(frame)
    assert len(out) == 2  # the all-stale row is gone; partial-stale stays
    assert list(out.index) == [0, 1]


def test_drop_fully_stale_rows_noop_without_gap_columns() -> None:
    frame = pd.DataFrame({"obd:RPM": [800.0, 810.0]})
    out = _drop_fully_stale_rows(frame)
    assert len(out) == 2


def test_sequence_feature_columns_keeps_sparse_channels() -> None:
    """GRU-D feeds missingness to the model, so the old density filter is gone:
    a sparse round-robin OBD PID is kept at its native rate (masking handles
    its staleness) rather than dropped from the sequence feature set."""
    n = 40
    frame = pd.DataFrame(
        {
            "session": ["s1"] * n,
            "timestamp": pd.date_range("2026-05-24T19:00:00Z", periods=n, freq="1s"),
            MODE_COLUMN: ["city"] * n,
            "dbc:ECM_RPM": [float(800 + i) for i in range(n)],
            # Varies slightly (a real fuel level drains) — corpus-constant
            # channels are now dropped as zero-variance, which is not what
            # this test is about (sparse ≠ constant).
            "obd:FUEL_LEVEL": [50.0 - 0.1 * i for i in range(n)],
            f"dbc:ECM_RPM{GAP_SUFFIX}": [False] * n,
            f"obd:FUEL_LEVEL{GAP_SUFFIX}": [i % 5 != 0 for i in range(n)],  # stale 80%
        }
    )
    cols = sequence_feature_columns(frame)
    bases = {c.split("__")[0] for c in cols}
    assert "dbc:ECM_RPM" in bases  # dense channel kept
    assert "obd:FUEL_LEVEL" in bases  # sparse round-robin channel now also kept


def test_sequence_feature_columns_drops_all_nan_and_deviations() -> None:
    """The only remaining drops are the deviation: family (NaN-by-contract,
    IF-only) and all-NaN-everywhere columns."""
    n = 5
    frame = pd.DataFrame(
        {
            "session": ["s1"] * n,
            "timestamp": pd.date_range("2026-05-24T19:00:00Z", periods=n, freq="1s"),
            MODE_COLUMN: ["city"] * n,
            "dbc:ECM_RPM": [float(800 + i) for i in range(n)],
            "deviation:coolant_warmup": [0.1] * n,
            "obd:NEVER_SEEN": [float("nan")] * n,
        }
    )
    cols = sequence_feature_columns(frame)
    bases = {c.split("__")[0] for c in cols}
    assert "dbc:ECM_RPM" in bases
    assert "deviation:coolant_warmup" not in bases  # deviation family dropped
    assert "obd:NEVER_SEEN" not in bases  # all-NaN dropped


def test_trainable_feature_columns_drops_corpus_constant_channels() -> None:
    """A channel constant across the ENTIRE corpus (a dead poll — the May/June
    models trained while obd:ABSOLUTE_LOAD read a constant) teaches nothing
    and floods attribution once the sensor comes alive; it must be dropped.
    A channel that merely holds steady within one session but moved in
    another stays trainable — constancy is corpus-wide."""
    n = 6
    frame = pd.DataFrame(
        {
            "session": ["s1"] * (n // 2) + ["s2"] * (n // 2),
            "timestamp": pd.date_range("2026-05-24T19:00:00Z", periods=n, freq="1s"),
            MODE_COLUMN: ["city"] * n,
            "obd:RPM": [float(800 + i) for i in range(n)],
            "obd:DEAD_POLL": [0.0] * n,  # constant everywhere
            # steady in s1 (90.0), moved in s2 — corpus-varying, kept
            "obd:COOLANT_TEMP": [90.0, 90.0, 90.0, 60.0, 75.0, 90.0],
        }
    )
    cols = trainable_feature_columns(frame)
    assert "obd:RPM" in cols
    assert "obd:COOLANT_TEMP" in cols
    assert "obd:DEAD_POLL" not in cols
    # The manifest documents the drop.
    health = summarize_feature_health(
        frame, cols, candidate_columns=candidate_feature_columns(frame)
    )
    assert health["dropped_constant_columns"] == ["obd:DEAD_POLL"]
    assert "obd:DEAD_POLL" not in health["dropped_all_nan_columns"]


@pytest.fixture
def config() -> CanaryConfig:
    return CanaryConfig.from_yaml(Path("canary.example.yaml"))


def _wide(n: int = 5) -> pd.DataFrame:
    return pd.DataFrame(
        {
            "session": ["s"] * n,
            "timestamp": pd.date_range("2026-05-13T12:00:00Z", periods=n, freq="1s"),
            "obd:RPM": [700.0 + 10 * i for i in range(n)],
        }
    )


def test_featurize_wide_frame_appends_rolling_features_and_mode(
    config: CanaryConfig,
) -> None:
    wide = _wide(5)

    result = featurize_wide_frame(wide, config)

    assert isinstance(result, FeaturizedFrame)
    feats = result.features
    op = result.operating_frame

    # operating_frame is the *pre-feature* row set: segmented but with no
    # rolling-derivative columns, same rows as the input.
    assert len(op) == len(wide)
    assert op[MODE_COLUMN].eq(ALL_MODE).all()  # only obd:RPM present -> single mode
    assert not [c for c in op.columns if c.endswith(_ROLL_SUFFIXES)]
    # 4.7 — missing segmenter inputs is a real runtime fallback, flagged.
    assert result.segmenter_fell_back

    # features carries the raw channel + its rolling derivatives + the mode.
    for col in ("obd:RPM", "obd:RPM__mean", "obd:RPM__std", "obd:RPM__roc", MODE_COLUMN):
        assert col in feats.columns
    assert len(feats) == len(wide)


def test_featurize_wide_frame_does_not_flag_fallback_with_real_segmentation(
    config: CanaryConfig,
) -> None:
    """4.7 — when the segmenter's inputs are present and it actually ran,
    ``segmenter_fell_back`` must stay False (this complements the
    load-time ``unknown_operating_modes`` warning, not replace it)."""
    n = 5
    wide = pd.DataFrame(
        {
            "session": ["s"] * n,
            "timestamp": pd.date_range("2026-05-13T12:00:00Z", periods=n, freq="1s"),
            "obd:RPM": [800.0] * n,
            "obd:SPEED": [0.0] * n,
            "obd:THROTTLE_POS": [10.0] * n,
            "obd:COOLANT_TEMP": [85.0] * n,
        }
    )
    result = featurize_wide_frame(wide, config)
    assert not result.segmenter_fell_back


def test_featurize_wide_frame_honors_feature_config_override(
    config: CanaryConfig,
) -> None:
    # The streaming caller passes its once-built FeatureConfig; prove the
    # override actually drives compute_features (std/roc disabled here).
    cfg = FeatureConfig(window_seconds=30, include_std=False, include_roc=False)

    feats = featurize_wide_frame(wide := _wide(4), config, feature_config=cfg).features

    assert "obd:RPM__mean" in feats.columns
    assert "obd:RPM__std" not in feats.columns
    assert "obd:RPM__roc" not in feats.columns
    assert len(feats) == len(wide)


@pytest.mark.parametrize(
    "behavior_models",
    [None, BehaviorModels(schema_version=1, models=[])],
    ids=["none", "empty-models"],
)
def test_featurize_wide_frame_skips_deviation_without_models(
    config: CanaryConfig,
    behavior_models: BehaviorModels | None,
) -> None:
    result = featurize_wide_frame(_wide(3), config, behavior_models=behavior_models)

    assert not [c for c in result.features.columns if str(c).startswith("deviation:")]
    assert not [c for c in result.operating_frame.columns if str(c).startswith("deviation:")]


def test_featurize_wide_frame_appends_instantaneous_derived_channels(
    config: CanaryConfig,
) -> None:
    """Task 1.1 — performance channels (fuel rate, boost/vacuum, estimated
    power/torque) ride the same shared builder as ``compute_anomaly_signals``,
    so both the batch and live paths get them for free."""
    n = 5
    wide = pd.DataFrame(
        {
            "session": ["s"] * n,
            "timestamp": pd.date_range("2026-05-13T12:00:00Z", periods=n, freq="1s"),
            "obd:RPM": [2000.0] * n,
            "obd:MAF": [12.0] * n,
            "obd:SPEED": [60.0] * n,
            "obd:INTAKE_PRESSURE": [95.0] * n,
        }
    )
    result = featurize_wide_frame(wide, config)
    op = result.operating_frame
    assert "derived:FUEL_RATE_LPH" in op.columns
    assert (op["derived:FUEL_RATE_LPH"] > 0).all()
    assert "derived:BOOST_VACUUM_KPA" in op.columns
    assert op["derived:BOOST_VACUUM_KPA"].iloc[0] == pytest.approx(95.0 - 101.325)
    assert "derived:ESTIMATED_POWER_HP" in op.columns
    # The cumulative trip integrals are NOT part of this shared builder —
    # this frame is a live ring-buffer *window* on the streaming path, and a
    # cumsum over a window would reset every eviction, not just a session
    # boundary (Task 1.3 handles those separately, in LiveFeatureSource).
    assert "derived:TRIP_DISTANCE_KM" not in op.columns
    assert "derived:FUEL_USED_L" not in op.columns
    # And it does show up in the rolling-feature output too, same as any
    # other channel — this is what makes it a model-feature candidate.
    assert "derived:FUEL_RATE_LPH" in result.features.columns
    assert "derived:FUEL_RATE_LPH__mean" in result.features.columns


def test_candidate_feature_columns_keeps_instantaneous_derived_channels() -> None:
    frame = pd.DataFrame(
        {
            "session": ["s", "s"],
            "timestamp": pd.to_datetime(["2026-05-24T00:00:00Z", "2026-05-24T00:00:01Z"]),
            "operating_mode": ["city", "city"],
            "obd:RPM": [800.0, 810.0],
            "derived:FUEL_RATE_LPH": [1.0, 1.1],
            "derived:BOOST_VACUUM_KPA": [-30.0, -29.0],
        }
    )
    cols = candidate_feature_columns(frame)
    assert "derived:FUEL_RATE_LPH" in cols
    assert "derived:BOOST_VACUUM_KPA" in cols


def test_candidate_feature_columns_excludes_derived_trip_integrals() -> None:
    """Task 1.1/1.3 — cumulative trip integrals are monotonic by construction
    (same failure mode as ``obd:RUN_TIME``) and must stay out of the trained
    feature set, same mechanism as the OBD counters."""
    frame = pd.DataFrame(
        {
            "session": ["s", "s"],
            "timestamp": pd.to_datetime(["2026-05-24T00:00:00Z", "2026-05-24T00:00:01Z"]),
            "operating_mode": ["city", "city"],
            "obd:RPM": [800.0, 810.0],
            "derived:TRIP_DISTANCE_KM": [0.0, 0.02],
            "derived:FUEL_USED_L": [0.0, 0.001],
            "derived:CO2_KG": [0.0, 0.002],
            "derived:IDLE_TIME_S": [0.0, 0.0],
            "derived:AVG_ECONOMY_L_PER_100KM": [float("nan"), 5.0],
        }
    )
    cols = candidate_feature_columns(frame)
    assert "obd:RPM" in cols
    for excluded in (
        "derived:TRIP_DISTANCE_KM",
        "derived:FUEL_USED_L",
        "derived:CO2_KG",
        "derived:IDLE_TIME_S",
        "derived:AVG_ECONOMY_L_PER_100KM",
    ):
        assert excluded in NON_TRAINABLE_COUNTER_CHANNELS
        assert excluded not in cols


def test_align_features_to_schema_adds_absent_columns_as_nan() -> None:
    """A drive lacking a sensor family (no GPS) gets the absent model columns
    back as all-NaN so they're scored as masked-missing, not a crash."""
    frame = pd.DataFrame({"decoded:ECM_RPM": [700.0, 705.0], "obd:SPEED": [0.0, 1.0]})

    aligned = align_features_to_schema(
        frame, ["decoded:ECM_RPM", "obd:SPEED", "gps:speed", "gps:speed__mean"]
    )

    # Present columns are untouched; absent ones are added as all-NaN.
    assert list(aligned["decoded:ECM_RPM"]) == [700.0, 705.0]
    assert "gps:speed" in aligned.columns
    assert aligned["gps:speed"].isna().all()
    assert aligned["gps:speed__mean"].isna().all()


def test_align_features_to_schema_is_noop_when_complete() -> None:
    frame = pd.DataFrame({"a": [1.0], "b": [2.0]})
    assert align_features_to_schema(frame, ["a", "b"]) is frame


def _write_session(root: Path, name: str, *, rows: int, start_epoch: float) -> Path:
    """Tiny OBD-only session for the parallel-feature-build regression test."""
    from src.storage.parquet_writer import write_records_to_parquet
    from src.telemetry import TelemetryRecord

    session = root / name
    records = []
    for i in range(rows):
        t = start_epoch + i
        records.extend(
            [
                TelemetryRecord.obd(t, "RPM", 780.0 + (i % 4), "rpm"),
                TelemetryRecord.obd(t, "SPEED", float(i % 5), "km/h"),
                TelemetryRecord.obd(t, "THROTTLE_POS", 6.0 + (i % 3), "%"),
            ]
        )
    write_records_to_parquet(records, session, max_rows=10_000)
    return session


def test_build_feature_frame_parallel_matches_serial(tmp_path: Path) -> None:
    """The ProcessPool-parallel per-session feature build must produce a frame
    byte-equal to the serial path — same rows in the same order, identical
    feature values. Sessions are independent so this should hold exactly."""
    from src.preprocessing.feature_pipeline import build_feature_frame

    config = CanaryConfig.from_yaml(Path("canary.example.yaml"))
    # 3 sessions ⇒ above the parallel threshold; forcing max_workers=2 forces
    # the pool path. Each session is distinct (different start epochs) so we
    # also indirectly check ordering is preserved.
    sessions = [
        _write_session(tmp_path, f"s{i}", rows=60, start_epoch=1_780_000_000.0 + i * 1000.0)
        for i in range(3)
    ]

    serial = build_feature_frame(sessions, config, max_workers=1)
    parallel = build_feature_frame(sessions, config, max_workers=2)

    pd.testing.assert_frame_equal(serial, parallel)


def test_build_feature_frame_skips_parallel_under_threshold(tmp_path: Path) -> None:
    """Below the threshold (here: 2 sessions), the pool isn't spun up even when
    ``max_workers`` would allow it — pool startup cost would outweigh the win.
    The result still has to equal the serial path."""
    from src.preprocessing.feature_pipeline import build_feature_frame

    config = CanaryConfig.from_yaml(Path("canary.example.yaml"))
    sessions = [
        _write_session(tmp_path, f"s{i}", rows=60, start_epoch=1_780_000_000.0 + i * 1000.0)
        for i in range(2)
    ]

    serial = build_feature_frame(sessions, config, max_workers=1)
    auto = build_feature_frame(sessions, config, max_workers=4)  # 4 ignored: N<threshold

    pd.testing.assert_frame_equal(serial, auto)


def test_build_feature_frame_quality_gate_rejects_impossible_cell(tmp_path: Path) -> None:
    """Task 1.2: a physically-impossible RPM reading (the 0x0C decode-
    rollover value) never survives into the trained feature frame — it's
    NaN'd before the resampler, so it ffills from the last good reading
    exactly like any other gap, rather than poisoning the corpus with a
    16383 rpm "reading"."""
    from src.preprocessing.feature_pipeline import build_feature_frame
    from src.storage.parquet_writer import write_records_to_parquet
    from src.telemetry import TelemetryRecord

    session = tmp_path / "s0"
    start = 1_780_000_000.0
    records = [
        TelemetryRecord.obd(start, "RPM", 900.0, "rpm"),
        TelemetryRecord.obd(start + 1, "RPM", 16383.0, "rpm"),  # decode-rollover garbage
        TelemetryRecord.obd(start + 2, "RPM", 950.0, "rpm"),
    ]
    write_records_to_parquet(records, session, max_rows=10_000)

    config = CanaryConfig.from_yaml(Path("canary.example.yaml"))
    frame = build_feature_frame([session], config, max_workers=1)

    assert 16383.0 not in frame["obd:RPM"].to_numpy()


def test_build_feature_frame_tail_matches_standalone_featurize_wide_frame(
    tmp_path: Path,
) -> None:
    """2.2: build_feature_frame's docstring claims its tail IS
    featurize_wide_frame — lock that so a future edit can't silently
    open-code a divergent copy again. Replicates build_feature_frame's own
    head (decode → resample, via the same top-level _decode_session_to_resampled
    it calls) with no bindings/DBC/DID (this session is OBD-only, so those
    are no-ops), then asserts featurize_wide_frame on that resampled frame
    is byte-identical to build_feature_frame's actual output."""
    from src.preprocessing.feature_pipeline import (
        _decode_session_to_resampled,
        build_feature_frame,
    )
    from src.preprocessing.resampler import ResampleConfig

    config = CanaryConfig.from_yaml(Path("canary.example.yaml"))
    session = _write_session(tmp_path, "s0", rows=60, start_epoch=1_780_000_000.0)

    resample_config = ResampleConfig(
        hz=config.preprocessing.resample_hz,
        slow_channel_max_gap_seconds=config.preprocessing.slow_channel_max_gap_seconds,
    )
    resampled = _decode_session_to_resampled(session, None, None, None, resample_config)
    resampled = _drop_fully_stale_rows(resampled)
    standalone = featurize_wide_frame(resampled, config).features

    combined = build_feature_frame([session], config)

    pd.testing.assert_frame_equal(standalone, combined)


def test_expand_suppress_channels_matches_base_and_rolling_siblings() -> None:
    """Listing a base channel auto-includes its rolling-derivative siblings
    (``__mean``, ``__std``, ``__roc``) — the operator lists the sensor once."""
    from src.preprocessing.feature_pipeline import expand_suppress_channels

    columns = [
        "obd:FUEL_LEVEL",
        "obd:FUEL_LEVEL__mean",
        "obd:FUEL_LEVEL__std",
        "obd:FUEL_LEVEL__roc",
        "dbc:RPM",
        "dbc:RPM__mean",
        "obd:UNRELATED",
    ]
    matched = expand_suppress_channels(["obd:FUEL_LEVEL"], columns)
    assert matched == (
        "obd:FUEL_LEVEL",
        "obd:FUEL_LEVEL__mean",
        "obd:FUEL_LEVEL__std",
        "obd:FUEL_LEVEL__roc",
    )


def test_expand_suppress_channels_silently_drops_unknown_entries() -> None:
    """A typo or a channel that disappears from the corpus should become a
    no-op, not break the serve path."""
    from src.preprocessing.feature_pipeline import expand_suppress_channels

    columns = ["obd:RPM", "obd:RPM__mean"]
    # ``obd:DOES_NOT_EXIST`` doesn't match anything → empty result
    assert expand_suppress_channels(["obd:DOES_NOT_EXIST"], columns) == ()
    # Empty input → empty output, no exceptions
    assert expand_suppress_channels([], columns) == ()


# ---------------------------------------------------------------------------
# 2.5 (ml-correctness-hardening) — IF feature-subset trial machinery
# ---------------------------------------------------------------------------


def test_restrict_isolation_forest_expansion_drops_only_requested_suffixes() -> None:
    columns = [
        "obd:RPM",
        "obd:RPM__mean",
        "obd:RPM__std",
        "obd:RPM__roc",
        "obd:SPEED",
        "obd:SPEED__mean",
        "obd:SPEED__std",
        "obd:SPEED__roc",
    ]
    raw_roc_only = restrict_isolation_forest_expansion(
        columns, drop_suffixes=(MEAN_SUFFIX, STD_SUFFIX)
    )
    assert raw_roc_only == ["obd:RPM", "obd:RPM__roc", "obd:SPEED", "obd:SPEED__roc"]

    raw_only = restrict_isolation_forest_expansion(
        columns, drop_suffixes=(MEAN_SUFFIX, STD_SUFFIX, ROC_SUFFIX)
    )
    assert raw_only == ["obd:RPM", "obd:SPEED"]

    # No suffixes requested -> identity (full 4x set unchanged).
    assert restrict_isolation_forest_expansion(columns, drop_suffixes=()) == columns


def test_restrict_isolation_forest_expansion_is_order_preserving_and_pure() -> None:
    """The helper must not mutate its input or reorder survivors — downstream
    callers (fit_isolation_forest's `feature_columns` override) rely on a
    stable, predictable column order for the manifest's feature spec."""
    columns = ["b__roc", "a", "a__mean", "b"]
    original = list(columns)
    out = restrict_isolation_forest_expansion(columns, drop_suffixes=(MEAN_SUFFIX,))
    assert columns == original  # input untouched
    assert out == ["b__roc", "a", "b"]


def _sized_sessions(tmp_path: Path, count: int, megabytes: float) -> list[Path]:
    """``count`` session dirs each holding ``megabytes`` of (dummy) parquet."""
    sessions = []
    for i in range(count):
        session = tmp_path / f"sized{i}"
        session.mkdir()
        (session / "telemetry-00000.parquet").write_bytes(b"\0" * int(megabytes * 1024 * 1024))
        sessions.append(session)
    return sessions


def test_resolve_worker_count_trims_pool_to_available_memory(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The OOM this replaces: 4 fixed workers × a multi-GB frame each on a box
    with ~2 GB actually free. The budget has to win over the CPU ceiling."""
    from src.preprocessing import feature_pipeline as fp

    sessions = _sized_sessions(tmp_path, 6, megabytes=45.0)  # ~1.8 GB/worker at 40×
    monkeypatch.setattr(fp, "_available_memory_bytes", lambda: 2 * 1024**3)
    monkeypatch.setattr(fp.os, "cpu_count", lambda: 12)

    assert fp._resolve_worker_count(sessions, None) == 1


def test_resolve_worker_count_uses_cpu_ceiling_when_memory_is_ample(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Small sessions on a roomy box: the memory budget must not throttle
    below what the CPU/cap ceiling already allows."""
    from src.preprocessing import feature_pipeline as fp

    sessions = _sized_sessions(tmp_path, 8, megabytes=1.0)
    monkeypatch.setattr(fp, "_available_memory_bytes", lambda: 64 * 1024**3)
    monkeypatch.setattr(fp.os, "cpu_count", lambda: 12)

    assert fp._resolve_worker_count(sessions, None) == fp._PARALLEL_WORKER_CAP


def test_resolve_worker_count_is_bounded_by_session_count(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from src.preprocessing import feature_pipeline as fp

    sessions = _sized_sessions(tmp_path, 2, megabytes=1.0)
    monkeypatch.setattr(fp, "_available_memory_bytes", lambda: 64 * 1024**3)
    monkeypatch.setattr(fp.os, "cpu_count", lambda: 12)

    assert fp._resolve_worker_count(sessions, None) == 2


def test_resolve_worker_count_honours_explicit_override(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """An explicit ``max_workers`` is the caller's call — the memory heuristic
    must not second-guess it (nor consult memory at all)."""
    from src.preprocessing import feature_pipeline as fp

    sessions = _sized_sessions(tmp_path, 6, megabytes=45.0)

    def _boom() -> int:
        raise AssertionError("memory must not be probed when max_workers is explicit")

    monkeypatch.setattr(fp, "_available_memory_bytes", _boom)

    assert fp._resolve_worker_count(sessions, 8) == 8
    assert fp._resolve_worker_count(sessions, 0) == 1


def test_resolve_worker_count_falls_back_when_memory_is_unknown(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """On a platform that won't report free memory, keep the previous
    CPU/cap-only behaviour rather than collapsing to serial."""
    from src.preprocessing import feature_pipeline as fp

    sessions = _sized_sessions(tmp_path, 6, megabytes=45.0)
    monkeypatch.setattr(fp, "_available_memory_bytes", lambda: None)
    monkeypatch.setattr(fp.os, "cpu_count", lambda: 12)

    assert fp._resolve_worker_count(sessions, None) == fp._PARALLEL_WORKER_CAP


def test_available_memory_bytes_is_plausible_or_none() -> None:
    from src.preprocessing import feature_pipeline as fp

    available = fp._available_memory_bytes()
    assert available is None or available > 0


# ---------------------------------------------------------------------------
# Chunked CAN decode (2026-07-26 OOM). Decoding 4.5M raw CAN rows in one pass
# held every raw row AND every decoded row at once (~14.6M object-dtype rows,
# ~7.6 GB RSS) and got feature-build workers OOM-killed. `_decode_long_frame`
# chunks it; these lock the equivalence that makes that safe.
# ---------------------------------------------------------------------------


def _synth_can_long(n: int, can_id: int = 0x551) -> pd.DataFrame:
    """Long-format CAN rows shaped like ``read_session`` output."""
    import json

    return pd.DataFrame(
        [
            {
                "timestamp": pd.Timestamp(float(i), unit="s", tz="UTC"),
                "source": "can",
                "channel": f"0x{can_id:X}",
                "value_number": float("nan"),
                "value_text": None,
                "value_bool": None,
                "unit": "",
                "payload": bytes([i % 256, (i * 7) % 256, 0]),
                "metadata_json": json.dumps({"can_id": can_id, "dlc": 3}),
                "session": "synth",
                "session_path": "/tmp/synth",
                "source_channel": f"can:0x{can_id:X}",
                "value": float("nan"),
            }
            for i in range(n)
        ]
    )


def _single_pass_decode(long: pd.DataFrame, bindings) -> pd.DataFrame:
    """The pre-chunking implementation, kept here as the parity reference."""
    from src.preprocessing.feature_pipeline import _LONG_SORT_COLUMNS

    out = decode_can_rows(long, bindings)
    out = out[out["source"] != "can"]
    return out.sort_values(_LONG_SORT_COLUMNS, kind="stable").reset_index(drop=True)


def test_chunked_can_decode_matches_single_pass(monkeypatch: pytest.MonkeyPatch) -> None:
    """Chunk boundaries must not change a single decoded value or row."""
    from src.decode.bindings import Bindings, Signal
    from src.preprocessing import feature_pipeline as fp

    bindings = Bindings(
        schema_version=1,
        signals=[
            Signal(
                name="A", module="ECM", can_id=0x551, byte_start=0, byte_length=1, confidence="high"
            ),
            Signal(
                name="B", module="ECM", can_id=0x551, byte_start=1, byte_length=1, confidence="high"
            ),
        ],
    )
    long = _synth_can_long(50)
    expected = _single_pass_decode(long, bindings)

    # Chunk size that does NOT divide the row count, so a chunk boundary lands
    # mid-frame and the last chunk is short.
    monkeypatch.setattr(fp, "_CAN_DECODE_CHUNK_ROWS", 7)
    got = fp._decode_long_frame(long, bindings, None, None).reset_index(drop=True)

    assert len(got) == 100  # 50 frames x 2 signals
    pd.testing.assert_frame_equal(got, expected)


def test_chunked_decode_drops_raw_can_rows() -> None:
    from src.decode.bindings import Bindings, Signal
    from src.preprocessing import feature_pipeline as fp

    bindings = Bindings(
        schema_version=1,
        signals=[
            Signal(
                name="A", module="ECM", can_id=0x551, byte_start=0, byte_length=1, confidence="high"
            )
        ],
    )
    out = fp._decode_long_frame(_synth_can_long(10), bindings, None, None)
    assert (out["source"] != "can").all()
    assert len(out) == 10


def test_decode_long_frame_without_decoders_keeps_non_can_rows() -> None:
    """No bindings and no DBC: the CAN rows still go (nothing can decode them)
    and the non-CAN rows survive untouched."""
    from src.preprocessing import feature_pipeline as fp

    can = _synth_can_long(4)
    obd = can.iloc[:2].copy()
    obd["source"] = "obd"
    obd["channel"] = "RPM"
    obd["source_channel"] = "obd:RPM"
    obd["value"] = [800.0, 900.0]
    out = fp._decode_long_frame(pd.concat([can, obd], ignore_index=True), None, None, None)
    assert out["source"].unique().tolist() == ["obd"]
    assert sorted(out["value"].tolist()) == [800.0, 900.0]


def test_decode_long_frame_handles_empty_and_missing_source() -> None:
    from src.preprocessing import feature_pipeline as fp

    assert fp._decode_long_frame(pd.DataFrame(), None, None, None).empty
    bare = pd.DataFrame({"timestamp": pd.to_datetime(["2026-05-13T12:00:00Z"])})
    pd.testing.assert_frame_equal(fp._decode_long_frame(bare, None, None, None), bare)
