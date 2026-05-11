"""Tests for the anomaly-model training CLI."""

from __future__ import annotations

import json
from pathlib import Path

import pandas as pd
import pyarrow as pa
import pyarrow.parquet as pq
import pytest

from scripts.train_anomaly_model import (
    ALL_MODE,
    _segment_or_constant,
    _trainable_feature_columns,
    build_feature_frame,
    main,
    train,
)
from src.config import CanaryConfig
from src.preprocessing import MODE_COLUMN
from src.storage.parquet_writer import TELEMETRY_SCHEMA

# ---------------------------------------------------------------------------
# Synthetic Parquet session + bindings fixtures
# ---------------------------------------------------------------------------


def _write_can_session(
    root: Path,
    name: str,
    *,
    can_id: int = 0x551,
    n_frames: int = 240,
    period_s: float = 0.25,
) -> Path:
    """A Parquet session with CAN frames on one ID, byte 0 ramping then steady.

    Mimics a coolant-warmup-then-plateau signal so the decoded byte has a
    realistic distribution for the IF to learn.
    """
    session = root / name
    session.mkdir()
    rows = []
    base = pd.Timestamp("2026-05-11T12:00:00Z")
    for i in range(n_frames):
        # Byte 0: ramp 80 → 200 over the first third, then hold ~200 with jitter.
        if i < n_frames // 3:
            b0 = 80 + int(120 * i / (n_frames // 3))
        else:
            b0 = 198 + (i % 3)
        payload = bytes([b0, (i % 7), 0, 0])
        rows.append(
            {
                "timestamp": (base + pd.Timedelta(seconds=i * period_s)).to_pydatetime(),
                "source": "can",
                "channel": f"0x{can_id:X}",
                "value_number": None,
                "value_text": None,
                "value_bool": None,
                "unit": "",
                "payload": payload,
                "metadata_json": json.dumps({"can_id": can_id, "dlc": 4}),
            }
        )
    table = pa.Table.from_pylist(rows, schema=TELEMETRY_SCHEMA)
    pq.write_table(table, session / "telemetry-00000.parquet")
    return session


def _write_bindings(path: Path, *, can_id: int = 0x551) -> None:
    path.write_text(
        f"""
schema_version: 1
signals:
  - name: ECM_COOLANT_TEMP
    module: ECM
    can_id: 0x{can_id:X}
    byte_start: 0
    byte_length: 1
    scale: 1.0
    offset: -40.0
    unit: degC
    confidence: high
  - name: ECM_AUX_BYTE
    module: ECM
    can_id: 0x{can_id:X}
    byte_start: 1
    byte_length: 1
    confidence: medium
"""
    )


def _config() -> CanaryConfig:
    return CanaryConfig.from_yaml(Path("canary.example.yaml"))


# ---------------------------------------------------------------------------
# build_feature_frame
# ---------------------------------------------------------------------------


def test_build_feature_frame_produces_decoded_feature_columns(tmp_path: Path):
    session = _write_can_session(tmp_path, "sess1")
    bindings = tmp_path / "bindings.yaml"
    _write_bindings(bindings)

    features = build_feature_frame([session], _config(), bindings_path=bindings)
    cols = set(features.columns)
    assert "decoded:ECM_COOLANT_TEMP" in cols
    assert "decoded:ECM_AUX_BYTE" in cols
    # Rolling-feature derivatives present
    assert "decoded:ECM_COOLANT_TEMP__mean" in cols
    assert "decoded:ECM_COOLANT_TEMP__std" in cols
    assert "decoded:ECM_COOLANT_TEMP__roc" in cols
    # Decoded columns are numeric
    assert pd.api.types.is_numeric_dtype(features["decoded:ECM_COOLANT_TEMP"])


def test_build_feature_frame_without_bindings_still_works(tmp_path: Path):
    """Missing bindings file → no decoded columns, but pipeline doesn't crash."""
    session = _write_can_session(tmp_path, "sess1")
    features = build_feature_frame([session], _config(), bindings_path=tmp_path / "nope.yaml")
    assert not any(str(c).startswith("decoded:") for c in features.columns)
    # Still has the structural columns
    assert "session" in features.columns
    assert "timestamp" in features.columns


# ---------------------------------------------------------------------------
# _segment_or_constant
# ---------------------------------------------------------------------------


def test_segment_or_constant_adds_constant_mode_when_no_obd(tmp_path: Path):
    resampled = pd.DataFrame(
        {
            "session": ["s1"] * 3,
            "timestamp": pd.date_range("2026-05-11T12:00:00Z", periods=3, freq="1s"),
            "decoded:ECM_COOLANT_TEMP": [100.0, 105.0, 110.0],
        }
    )
    out = _segment_or_constant(resampled, _config())
    assert MODE_COLUMN in out.columns
    assert (out[MODE_COLUMN] == ALL_MODE).all()


def test_segment_or_constant_uses_segmenter_when_obd_present(tmp_path: Path):
    resampled = pd.DataFrame(
        {
            "session": ["s1"] * 3,
            "timestamp": pd.date_range("2026-05-11T12:00:00Z", periods=3, freq="1s"),
            "obd:RPM": [800.0, 850.0, 900.0],
            "obd:SPEED": [0.0, 0.0, 0.0],
            "obd:THROTTLE_POS": [10.0, 12.0, 11.0],
        }
    )
    out = _segment_or_constant(resampled, _config())
    assert MODE_COLUMN in out.columns
    # With RPM<900 and speed 0, the example config's "idle" mode should match.
    assert (out[MODE_COLUMN] != ALL_MODE).any() or (out[MODE_COLUMN] == "idle").any()


# ---------------------------------------------------------------------------
# _trainable_feature_columns
# ---------------------------------------------------------------------------


def test_trainable_columns_excludes_structural_gap_and_allnan():
    import numpy as np

    df = pd.DataFrame(
        {
            "session": ["s1"] * 3,
            "timestamp": pd.date_range("2026-05-11T12:00:00Z", periods=3, freq="1s"),
            MODE_COLUMN: ["all"] * 3,
            "decoded:GOOD": [1.0, 2.0, 3.0],
            "decoded:GOOD__gap": [False, True, False],
            "decoded:ALLNAN": [np.nan, np.nan, np.nan],
            "decoded:STRINGY": ["a", "b", "c"],
        }
    )
    cols = _trainable_feature_columns(df)
    assert "decoded:GOOD" in cols
    assert "decoded:GOOD__gap" not in cols
    assert "decoded:ALLNAN" not in cols  # all-NaN dropped
    assert "decoded:STRINGY" not in cols  # non-numeric dropped
    assert "session" not in cols
    assert MODE_COLUMN not in cols


# ---------------------------------------------------------------------------
# train — end to end
# ---------------------------------------------------------------------------


def test_train_writes_artifact_and_manifest(tmp_path: Path):
    pytest.importorskip("joblib")
    pytest.importorskip("sklearn")
    session = _write_can_session(tmp_path, "sess1", n_frames=400)
    bindings = tmp_path / "bindings.yaml"
    _write_bindings(bindings)
    out = tmp_path / "model"

    trained = train([session], _config(), out, bindings_path=bindings)

    artifact = out / "model.joblib"
    manifest = out / "model.manifest.json"
    assert artifact.exists()
    assert manifest.exists()
    # Manifest is valid JSON with the schema we expect.
    m = json.loads(manifest.read_text())
    assert "feature_columns" in m or "schema" in m  # field name from IsolationForestManifest
    # The trained model exposes its feature columns and modes.
    assert len(trained.feature_columns) > 0
    assert "all" in trained.models  # single mode (no OBD)


def test_train_raises_when_no_trainable_columns(tmp_path: Path):
    pytest.importorskip("joblib")
    pytest.importorskip("sklearn")
    # A session whose only CAN ID has no binding → no decoded columns →
    # only the object-dtype can:* column, which is non-numeric → nothing
    # trainable.
    session = _write_can_session(tmp_path, "sess1", can_id=0x999)
    bindings = tmp_path / "bindings.yaml"
    _write_bindings(bindings, can_id=0x551)  # bindings for a different ID
    with pytest.raises(ValueError, match="no trainable feature columns"):
        train([session], _config(), tmp_path / "model", bindings_path=bindings)


# ---------------------------------------------------------------------------
# main — CLI flow
# ---------------------------------------------------------------------------


def test_main_errors_when_no_sessions(tmp_path: Path, capsys):
    rc = main(
        [
            "--sessions-root",
            str(tmp_path / "empty"),
            "--config",
            "canary.example.yaml",
            "--out",
            str(tmp_path / "model"),
        ]
    )
    assert rc == 1
    assert "no sessions" in capsys.readouterr().err


def test_main_trains_and_reports(tmp_path: Path, capsys):
    pytest.importorskip("joblib")
    pytest.importorskip("sklearn")
    session = _write_can_session(tmp_path, "sess1", n_frames=400)
    bindings = tmp_path / "bindings.yaml"
    _write_bindings(bindings)
    rc = main(
        [
            str(session),
            "--config",
            "canary.example.yaml",
            "--bindings",
            str(bindings),
            "--out",
            str(tmp_path / "model"),
            "--log-level",
            "ERROR",
        ]
    )
    assert rc == 0
    out = capsys.readouterr().out
    assert "Trained Isolation Forest saved" in out
    assert "feature columns:" in out
    assert (tmp_path / "model" / "model.joblib").exists()


def test_main_with_evaluate(tmp_path: Path, capsys):
    pytest.importorskip("joblib")
    pytest.importorskip("sklearn")
    session = _write_can_session(tmp_path, "sess1", n_frames=400)
    bindings = tmp_path / "bindings.yaml"
    _write_bindings(bindings)
    rc = main(
        [
            str(session),
            "--config",
            "canary.example.yaml",
            "--bindings",
            str(bindings),
            "--out",
            str(tmp_path / "model"),
            "--evaluate",
            str(session),
            "--log-level",
            "ERROR",
        ]
    )
    assert rc == 0
    out = capsys.readouterr().out
    assert "EVALUATION" in out
    # Some per-mode metrics line is printed.
    assert "precision=" in out
