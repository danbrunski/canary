"""Tests for the offline replay harness.

The operation is ``src.analysis.replay.replay_sessions`` (R12); ``scripts/replay.py``
is the entrypoint over it. The ``replay_sessions`` tests exercise the library
directly, ``main`` tests exercise the script's arguments, output and exit codes.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import pytest

import scripts.replay as replay_script
from scripts.replay import main
from src.analysis import replay as replay_library
from src.analysis.replay import replay_sessions
from src.config import CanaryConfig
from src.models.isolation_forest import fit_isolation_forest
from src.storage.parquet_writer import write_records_to_parquet
from src.telemetry import TelemetryRecord
from tests.configs import example_config

REPO_ROOT = Path(__file__).resolve().parents[1]
EXAMPLE_YAML = REPO_ROOT / "canary.example.yaml"


@dataclass(frozen=True)
class _IFConfig:
    enabled: bool = True
    n_estimators: int = 25
    contamination: float = 0.05
    per_mode_contamination: dict[str, float] = field(default_factory=dict)
    random_state: int = 42
    # Calibration on so the resulting artifact survives the deploy-time
    # guard in ``_validate_loaded_artifact``. The replay-script tests use
    # 40 rows per mode, so the holdout fraction needs to clear
    # ``_MIN_CALIBRATION_SAMPLES`` (10) — 0.3 → 12 holdout samples.
    calibration_holdout_fraction: float = 0.3
    target_flag_rate: float = 0.05


def _config() -> CanaryConfig:
    return example_config(EXAMPLE_YAML)


def _write_obd_session(root: Path, name: str = "2026-05-11T120000", n: int = 80) -> Path:
    session = root / name
    records: list[TelemetryRecord] = []
    for i in range(n):
        t = 1_778_500_000.0 + i
        if i < n // 2:
            rpm = 800.0 + (i % 3)
            speed = 0.0
            throttle = 5.0
        else:
            rpm = 1800.0 + (i % 5)
            speed = 35.0
            throttle = 18.0
        records.extend(
            [
                TelemetryRecord.obd(t, "RPM", rpm, "rpm"),
                TelemetryRecord.obd(t, "SPEED", speed, "km/h"),
                TelemetryRecord.obd(t, "THROTTLE_POS", throttle, "%"),
                # Warm coolant so the idle/warmup split in canary.example.yaml
                # resolves to ``idle`` for the stopped half rather than
                # ``unknown``. The synthetic test data isn't trying to
                # represent a cold start.
                TelemetryRecord.obd(t, "COOLANT_TEMP", 85.0, "C"),
            ]
        )
    write_records_to_parquet(records, session, max_rows=10_000)
    return session


def test_replay_sessions_builds_preprocessing_report(tmp_path: Path) -> None:
    session = _write_obd_session(tmp_path)

    result = replay_sessions(
        [session], _config(), behavior_models_path=tmp_path / "missing-behavior-models.yaml"
    )
    report = result.report

    # 4 OBD channels × 80 ticks each (RPM/SPEED/THROTTLE_POS/COOLANT_TEMP).
    # COOLANT_TEMP is now required by ``_SEGMENTER_SIGNAL_COLUMNS`` so the
    # synthetic session must include it for the idle/warmup split to resolve.
    assert report["rows"]["long"] == 320
    assert report["rows"]["wide"] == 80
    assert report["rows"]["resampled"] == 80
    assert report["rows"]["features"] == 80
    assert report["sources"] == {"obd": 320}
    assert report["channels"]["by_source"] == {"obd": 4}
    assert report["numeric_value_columns"] == [
        "obd:COOLANT_TEMP",
        "obd:RPM",
        "obd:SPEED",
        "obd:THROTTLE_POS",
    ]
    assert report["numeric_value_column_count"] == 4
    assert report["rolling_feature_column_count"] == 12
    # Phase 5.2: canary.example.yaml now sets mode_dwell_ticks: 3, so the
    # idle->city switch at the i=40 boundary doesn't take effect until the
    # 3rd consecutive city row — 2 rows lean idle relative to the raw 40/40
    # split.
    assert report["mode_counts"] == {"idle": 42, "city": 38}
    assert report["complete_model_input_rows"] > 0
    assert report["scoring"] is None


def test_replay_sessions_can_score_with_isolation_forest_artifact(tmp_path: Path) -> None:
    session = _write_obd_session(tmp_path)
    config = _config()
    missing_behavior = tmp_path / "missing-behavior-models.yaml"
    initial = replay_sessions([session], config, behavior_models_path=missing_behavior)
    trained = fit_isolation_forest(
        initial.frames.features,
        _IFConfig(),
        fitted_at="2026-05-12T00:00:00Z",
        model_version="replay-test",
    )
    artifact = tmp_path / "iforest.joblib"
    trained.save(artifact)

    result = replay_sessions(
        [session], config, model_path=artifact, behavior_models_path=missing_behavior
    )

    assert result.frames.scored is not None
    scoring = result.report["scoring"]
    assert scoring["model_version"] == "replay-test"
    assert scoring["rows"] == 80
    # Per-mode feature coverage (>=50%) keeps foreign-mode deviation leakage out
    # of mode_feats, so rows are no longer dropped on it: nearly all score now
    # (only the leading roc-warmup row is non-finite).
    assert scoring["score_distribution"]["finite_count"] == 79
    assert scoring["score_distribution"]["pos_inf_count"] == 1
    assert set(scoring["predicted_anomalies_by_mode"]) == {"city", "idle"}


def test_replay_main_prints_json_report(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    session = _write_obd_session(tmp_path, n=4)

    rc = main(["--config", str(EXAMPLE_YAML), str(session)])

    assert rc == 0
    out = capsys.readouterr().out
    assert '"rows"' in out
    assert '"numeric_value_column_count": 4' in out


def test_replay_main_returns_error_for_missing_session(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    rc = main(["--config", str(EXAMPLE_YAML), str(tmp_path / "missing")])

    assert rc == 1
    assert "error:" in capsys.readouterr().err


def test_replay_sessions_includes_derived_anomaly_signal_columns(tmp_path: Path) -> None:
    """2.1: replay_sessions routes through featurize_wide_frame, so a
    derived anomaly-signal column (compute_anomaly_signals, gated on
    obd:MAF + obd:RPM being present) actually appears — it used to be
    silently dropped by the old direct compute_features call."""
    session = tmp_path / "2026-05-11T120000"
    records: list[TelemetryRecord] = []
    for i in range(40):
        t = 1_778_500_000.0 + i
        records.extend(
            [
                TelemetryRecord.obd(t, "RPM", 1800.0 + (i % 5), "rpm"),
                TelemetryRecord.obd(t, "SPEED", 35.0, "km/h"),
                TelemetryRecord.obd(t, "THROTTLE_POS", 18.0, "%"),
                TelemetryRecord.obd(t, "COOLANT_TEMP", 85.0, "C"),
                TelemetryRecord.obd(t, "MAF", 12.0 + (i % 3), "g/s"),
            ]
        )
    write_records_to_parquet(records, session, max_rows=10_000)

    result = replay_sessions(
        [session], _config(), behavior_models_path=tmp_path / "missing-behavior-models.yaml"
    )

    assert "derived:AIR_MASS_PER_CYCLE" in result.frames.segmented.columns
    assert "derived:AIR_MASS_PER_CYCLE" in result.frames.features.columns


def test_replay_reports_can_only_payload_sessions_as_unusable_numeric_features(
    tmp_path: Path,
) -> None:
    session = tmp_path / "can-only"
    records = [
        TelemetryRecord.can(1_778_500_000.0, 0x100, bytes([1, 2, 3, 4])),
        TelemetryRecord.can(1_778_500_001.0, 0x100, bytes([2, 3, 4, 5])),
    ]
    write_records_to_parquet(records, session)

    result = replay_sessions(
        [session], _config(), behavior_models_path=tmp_path / "missing-behavior-models.yaml"
    )
    report = result.report

    assert report["sources"] == {"can": 2}
    assert report["numeric_value_columns"] == ["can:0x100"]
    assert report["rolling_feature_column_count"] == 3
    assert report["complete_model_input_rows"] == 0


def test_script_reexports_are_the_library_objects() -> None:
    """Old programmatic callers keep working, and the names are not copies."""
    assert replay_script.replay_sessions is replay_library.replay_sessions  # type: ignore[attr-defined]
    assert replay_script.ReplayFrames is replay_library.ReplayFrames
    assert replay_script.ReplayResult is replay_library.ReplayResult
    assert replay_library.ReplayResult.__module__ == "src.analysis.replay"


def test_script_is_an_entrypoint_not_the_implementation() -> None:
    """Nothing the replay does is defined in the script besides its CLI."""
    owned = {
        name
        for name, value in vars(replay_script).items()
        if getattr(value, "__module__", None) == replay_script.__name__
    }
    assert owned == {"parse_args", "main"}


def test_replay_main_runs_the_library_and_prints_its_report(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """``main`` prints exactly the library's report, sorted, one JSON document."""
    import json

    session = _write_obd_session(tmp_path, n=6)
    returned: list[Any] = []

    def _through(*args: Any, **kwargs: Any) -> Any:
        result = replay_library.replay_sessions(*args, **kwargs)
        returned.append(result)
        return result

    monkeypatch.setattr(replay_script, "replay_sessions", _through)

    rc = main(["--config", str(EXAMPLE_YAML), str(session)])

    assert rc == 0
    (result,) = returned
    assert capsys.readouterr().out == json.dumps(result.report, sort_keys=True) + "\n"


def test_replay_main_forwards_value_and_model_to_the_library(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    seen: dict[str, object] = {}

    class _Result:
        report = {"ok": True}

    def _spy(session_dirs: Any, config: Any, **kwargs: Any) -> Any:
        seen["sessions"] = list(session_dirs)
        seen.update(kwargs)
        return _Result()

    monkeypatch.setattr(replay_script, "replay_sessions", _spy)
    model = tmp_path / "m.joblib"
    rc = main(["--config", str(EXAMPLE_YAML), "--value", "value_bool", "--model", str(model), "s"])

    assert rc == 0
    assert seen == {"sessions": [Path("s")], "value": "value_bool", "model_path": model}
    assert capsys.readouterr().out == '{"ok": true}\n'


def test_replay_sessions_refuses_no_sessions() -> None:
    import pytest

    with pytest.raises(ValueError, match="at least one session"):
        replay_sessions([], _config())
