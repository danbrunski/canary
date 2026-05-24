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


def test_sequence_feature_columns_drops_sparse_channels() -> None:
    # dbc:* fresh every tick (gap-rate 0); obd:* round-robin-stale (gap-rate ~1).
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
    assert "obd:FUEL_LEVEL" not in bases  # sparse round-robin channel dropped


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
