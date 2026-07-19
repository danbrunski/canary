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
from src.preprocessing.feature_pipeline import (
    ALL_MODE,
    FeaturizedFrame,
    _drop_fully_stale_rows,
    align_features_to_schema,
    featurize_wide_frame,
    sequence_feature_columns,
)
from src.preprocessing.features import FeatureConfig
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
            "obd:FUEL_LEVEL": [50.0] * n,
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

    # features carries the raw channel + its rolling derivatives + the mode.
    for col in ("obd:RPM", "obd:RPM__mean", "obd:RPM__std", "obd:RPM__roc", MODE_COLUMN):
        assert col in feats.columns
    assert len(feats) == len(wide)


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
