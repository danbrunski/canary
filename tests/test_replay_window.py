"""Unit tests for the ``src.analysis.replay_window`` module.

These tests mock out :func:`src.analysis.replay.replay_sessions` so they don't
exercise the full preprocessing pipeline (that has its own integration
tests). The focus here is the post-processing: window filtering, the IF
block, the LSTM block (skipped when no artifact), historical lookup, and
the event diff.
"""

from __future__ import annotations

import json
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any
from unittest.mock import patch

import pandas as pd

from src.analysis import replay_window as rw

# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@dataclass
class _FakeFrames:
    long: pd.DataFrame
    wide: pd.DataFrame
    resampled: pd.DataFrame
    segmented: pd.DataFrame
    features: pd.DataFrame
    scored: pd.DataFrame | None


@dataclass
class _FakeReplayResult:
    frames: _FakeFrames


def _features_frame(rows: list[dict[str, Any]]) -> pd.DataFrame:
    """Build a minimal feature DataFrame with timestamp/session/mode columns."""
    return pd.DataFrame(rows)


def _scored_from(features: pd.DataFrame, scores: list[float], labels: list[int]) -> pd.DataFrame:
    out = features.copy()
    out[rw.SCORE_COLUMN] = scores
    from src.models.isolation_forest import LABEL_COLUMN

    out[LABEL_COLUMN] = labels
    return out


@contextmanager
def _patched_replay(
    features: pd.DataFrame,
    scored: pd.DataFrame | None,
) -> Iterator[None]:
    """Stand in for the ``replay_sessions`` binding ``replay_window`` calls."""

    def _fake(_sessions, _config, *, model_path=None):
        result_scored = scored if model_path is not None else None
        return _FakeReplayResult(
            frames=_FakeFrames(
                long=pd.DataFrame(),
                wide=pd.DataFrame(),
                resampled=pd.DataFrame(),
                segmented=pd.DataFrame(),
                features=features,
                scored=result_scored,
            )
        )

    with patch("src.analysis.replay_window.replay_sessions", side_effect=_fake):
        yield


def _config(tmp_path: Path) -> Any:
    """A minimal stand-in CanaryConfig — replay_sessions is mocked anyway."""
    return None  # patched replay_sessions ignores it


# ---------------------------------------------------------------------------
# Window filtering
# ---------------------------------------------------------------------------


def test_window_filter_inclusive_bounds(tmp_path: Path) -> None:
    """``start``/``end`` filter inclusively against the features timestamp."""
    base = datetime(2026, 5, 17, 12, 0, tzinfo=UTC)
    features = _features_frame(
        [
            {"timestamp": base + timedelta(seconds=i), "session": "S1", "operating_mode": "idle"}
            for i in range(10)
        ]
    )
    scored = _scored_from(features, scores=[0.1] * 10, labels=[1] * 10)
    if_model = tmp_path / "model.joblib"
    if_model.write_bytes(b"")  # exists check; mocked replay ignores contents
    with _patched_replay(features, scored):
        report = rw.replay_window(
            session_dir=tmp_path / "S1",
            config=_config(tmp_path),
            start=base + timedelta(seconds=2),
            end=base + timedelta(seconds=5),
            if_model_path=if_model,
            lstm_model_path=None,
        )
    # We touched 4 rows: seconds 2, 3, 4, 5.
    assert report.summary["rows_scored"] == 4
    assert report.isolation_forest["loaded"] is True
    assert len(report.isolation_forest["scores"]) == 4


def test_missing_if_model_marked_not_loaded(tmp_path: Path) -> None:
    """Pointing at a non-existent IF artifact reports loaded=False + warning."""
    features = _features_frame(
        [
            {
                "timestamp": datetime(2026, 5, 17, 12, 0, tzinfo=UTC),
                "session": "S1",
                "operating_mode": "idle",
            }
        ]
    )
    with _patched_replay(features, scored=None):
        report = rw.replay_window(
            session_dir=tmp_path / "S1",
            config=_config(tmp_path),
            if_model_path=tmp_path / "missing.joblib",
            lstm_model_path=None,
        )
    assert report.isolation_forest["loaded"] is False
    assert any("IF model not found" in w for w in report.warnings)


def test_lstm_block_skipped_when_artifact_missing(tmp_path: Path) -> None:
    """A missing LSTM path is a warning, not an error."""
    features = _features_frame(
        [
            {
                "timestamp": datetime(2026, 5, 17, 12, 0, tzinfo=UTC),
                "session": "S1",
                "operating_mode": "idle",
            }
        ]
    )
    with _patched_replay(features, scored=None):
        report = rw.replay_window(
            session_dir=tmp_path / "S1",
            config=_config(tmp_path),
            if_model_path=None,
            lstm_model_path=tmp_path / "missing-lstm",
        )
    assert report.lstm_autoencoder["loaded"] is False
    assert any("LSTM model not found" in w for w in report.warnings)


def _touch(path: Path) -> Path:
    """Create an empty file so ``path.exists()`` is True. Contents irrelevant
    to the tests that mock ``replay_sessions`` (which never reads the file)."""
    path.write_bytes(b"")
    return path


def test_if_anomalies_are_label_minus_one(tmp_path: Path) -> None:
    """Rows with label=-1 land in the IF anomalies sub-list."""
    base = datetime(2026, 5, 17, 12, 0, tzinfo=UTC)
    features = _features_frame(
        [
            {"timestamp": base + timedelta(seconds=i), "session": "S1", "operating_mode": "idle"}
            for i in range(3)
        ]
    )
    scored = _scored_from(features, scores=[0.1, 0.95, 0.2], labels=[1, -1, 1])
    with _patched_replay(features, scored):
        report = rw.replay_window(
            session_dir=tmp_path / "S1",
            config=_config(tmp_path),
            if_model_path=_touch(tmp_path / "model.joblib"),
            lstm_model_path=None,
        )
    assert len(report.isolation_forest["anomalies"]) == 1
    assert report.isolation_forest["anomalies"][0]["label"] == -1
    assert report.summary["if_anomalies_replayed"] == 1


def test_max_rows_truncates_scores_with_warning(tmp_path: Path) -> None:
    base = datetime(2026, 5, 17, 12, 0, tzinfo=UTC)
    features = _features_frame(
        [
            {"timestamp": base + timedelta(seconds=i), "session": "S1", "operating_mode": "idle"}
            for i in range(50)
        ]
    )
    scored = _scored_from(features, scores=[0.1] * 50, labels=[1] * 50)
    with _patched_replay(features, scored):
        report = rw.replay_window(
            session_dir=tmp_path / "S1",
            config=_config(tmp_path),
            if_model_path=_touch(tmp_path / "model.joblib"),
            lstm_model_path=None,
            max_rows=10,
        )
    assert len(report.isolation_forest["scores"]) == 10
    assert report.isolation_forest["rows_scored"] == 50  # total, not truncated
    assert any("truncated" in w for w in report.warnings)


# ---------------------------------------------------------------------------
# Historical lookup + diff
# ---------------------------------------------------------------------------


def _write_anomaly(directory: Path, date: str, events: list[dict[str, Any]]) -> None:
    directory.mkdir(parents=True, exist_ok=True)
    path = directory / f"anomalies-{date}.jsonl"
    path.write_text("\n".join(json.dumps(e) for e in events) + "\n")


def test_historical_events_filtered_by_session_and_window(tmp_path: Path) -> None:
    base = datetime(2026, 5, 17, 12, 0, tzinfo=UTC)
    anomalies_dir = tmp_path / "anom"
    _write_anomaly(
        anomalies_dir,
        "2026-05-17",
        [
            {
                "timestamp": (base + timedelta(seconds=5)).isoformat(),
                "source": "isolation_forest",
                "score": 0.9,
                "session": "S1",
                "model_version": "1.0",
            },
            {  # wrong session
                "timestamp": (base + timedelta(seconds=10)).isoformat(),
                "source": "isolation_forest",
                "score": 0.9,
                "session": "S2",
                "model_version": "1.0",
            },
            {  # out of window
                "timestamp": (base + timedelta(seconds=600)).isoformat(),
                "source": "isolation_forest",
                "score": 0.9,
                "session": "S1",
                "model_version": "1.0",
            },
        ],
    )
    features = _features_frame(
        [
            {"timestamp": base + timedelta(seconds=i), "session": "S1", "operating_mode": "idle"}
            for i in range(10)
        ]
    )
    with _patched_replay(features, scored=None):
        report = rw.replay_window(
            session_dir=tmp_path / "S1",
            config=_config(tmp_path),
            start=base,
            end=base + timedelta(seconds=60),
            if_model_path=None,
            lstm_model_path=None,
            anomalies_dir=anomalies_dir,
        )
    assert len(report.historical_events) == 1
    assert report.historical_events[0]["session"] == "S1"


def test_diff_matches_within_tolerance(tmp_path: Path) -> None:
    """A replayed IF anomaly within 2s of a historical IF event counts as matched."""
    base = datetime(2026, 5, 17, 12, 0, tzinfo=UTC)
    anomalies_dir = tmp_path / "anom"
    historical_ts = base + timedelta(seconds=5)
    _write_anomaly(
        anomalies_dir,
        "2026-05-17",
        [
            {
                "timestamp": historical_ts.isoformat(),
                "source": "isolation_forest",
                "score": 0.9,
                "session": "S1",
                "model_version": "1.0",
            }
        ],
    )
    # Replayed anomaly 1s after the historical one — still within tolerance.
    features = _features_frame(
        [
            {
                "timestamp": base + timedelta(seconds=i),
                "session": "S1",
                "operating_mode": "idle",
            }
            for i in range(10)
        ]
    )
    scored = _scored_from(
        features,
        scores=[0.1] * 10,
        labels=[1, 1, 1, 1, 1, 1, -1, 1, 1, 1],  # row 6 (T+6s) is the anomaly
    )
    with _patched_replay(features, scored):
        report = rw.replay_window(
            session_dir=tmp_path / "S1",
            config=_config(tmp_path),
            if_model_path=_touch(tmp_path / "model.joblib"),
            lstm_model_path=None,
            anomalies_dir=anomalies_dir,
            diff_tolerance_seconds=2.0,
        )
    assert report.diff["matched"] == 1
    assert report.diff["replayed_only"] == 0
    assert report.diff["historical_only"] == 0


def test_diff_reports_replayed_only_when_no_history(tmp_path: Path) -> None:
    """A replayed event with no nearby historical event is ``replayed_only``."""
    base = datetime(2026, 5, 17, 12, 0, tzinfo=UTC)
    features = _features_frame(
        [
            {"timestamp": base + timedelta(seconds=i), "session": "S1", "operating_mode": "idle"}
            for i in range(3)
        ]
    )
    scored = _scored_from(features, scores=[0.1, 0.95, 0.1], labels=[1, -1, 1])
    anomalies_dir = tmp_path / "anom"
    anomalies_dir.mkdir()
    with _patched_replay(features, scored):
        report = rw.replay_window(
            session_dir=tmp_path / "S1",
            config=_config(tmp_path),
            if_model_path=_touch(tmp_path / "model.joblib"),
            lstm_model_path=None,
            anomalies_dir=anomalies_dir,
        )
    assert report.diff["replayed_only"] == 1
    assert report.diff["matched"] == 0


def test_diff_reports_historical_only_when_replay_silent(tmp_path: Path) -> None:
    """A historical event with no replayed match is ``historical_only``."""
    base = datetime(2026, 5, 17, 12, 0, tzinfo=UTC)
    anomalies_dir = tmp_path / "anom"
    _write_anomaly(
        anomalies_dir,
        "2026-05-17",
        [
            {
                "timestamp": (base + timedelta(seconds=5)).isoformat(),
                "source": "isolation_forest",
                "score": 0.9,
                "session": "S1",
                "model_version": "1.0",
            }
        ],
    )
    features = _features_frame(
        [
            {"timestamp": base + timedelta(seconds=i), "session": "S1", "operating_mode": "idle"}
            for i in range(10)
        ]
    )
    scored = _scored_from(features, scores=[0.1] * 10, labels=[1] * 10)
    with _patched_replay(features, scored):
        report = rw.replay_window(
            session_dir=tmp_path / "S1",
            config=_config(tmp_path),
            if_model_path=_touch(tmp_path / "model.joblib"),
            lstm_model_path=None,
            anomalies_dir=anomalies_dir,
        )
    assert report.diff["historical_only"] == 1
    assert report.diff["replayed_only"] == 0


def test_to_dict_round_trips_json(tmp_path: Path) -> None:
    """The report must JSON-serialise so the job script can write it to disk."""
    features = _features_frame(
        [
            {
                "timestamp": datetime(2026, 5, 17, 12, 0, tzinfo=UTC),
                "session": "S1",
                "operating_mode": "idle",
            }
        ]
    )
    with _patched_replay(features, scored=None):
        report = rw.replay_window(
            session_dir=tmp_path / "S1",
            config=_config(tmp_path),
            if_model_path=None,
            lstm_model_path=None,
        )
    encoded = json.dumps(report.to_dict(), default=str)
    decoded = json.loads(encoded)
    assert decoded["session_id"] == "S1"
    assert "summary" in decoded
