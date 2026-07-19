"""Tests for the corpus-wide detector benchmark CLI (Phase 2B, Task 2.5)."""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path

import numpy as np
import pandas as pd
import pytest

pytest.importorskip("joblib")
pytest.importorskip("sklearn")

from scripts.detector_benchmark import (  # noqa: E402
    DEFAULT_TARGET_CHANNELS,
    GridCell,
    aggregate_cells,
    build_report,
    cell_seed,
    load_resampled_session,
    main,
    print_report,
    run_grid,
    select_target_channels,
)
from src.config import CanaryConfig  # noqa: E402
from src.models.isolation_forest import fit_isolation_forest  # noqa: E402
from src.preprocessing.resampler import SESSION_COLUMN, TIMESTAMP_COLUMN  # noqa: E402
from src.storage.parquet_writer import write_records_to_parquet  # noqa: E402
from src.telemetry import TelemetryRecord  # noqa: E402

REPO_ROOT = Path(__file__).resolve().parent.parent
EXAMPLE_YAML = REPO_ROOT / "canary.example.yaml"


@dataclass(frozen=True)
class _IFConfig:
    enabled: bool = True
    n_estimators: int = 60
    contamination: float = 0.02
    per_mode_contamination: dict[str, float] = field(default_factory=dict)
    random_state: int = 7
    calibration_holdout_fraction: float = 0.2
    target_flag_rate: float = 0.05


def _config() -> CanaryConfig:
    return CanaryConfig.from_yaml(EXAMPLE_YAML)


def _two_mode_resampled(session: str, seed: int = 0) -> pd.DataFrame:
    """80 rows of clean idle + 80 rows of clean city (mirrors
    test_synthetic_fault_eval.py's fixture builder) for `session`."""
    rng = np.random.default_rng(seed)
    n = 80
    timestamps = pd.date_range("2026-05-12T00:00:00Z", periods=n * 2, freq="1s")
    return pd.DataFrame(
        {
            SESSION_COLUMN: [session] * (n * 2),
            TIMESTAMP_COLUMN: timestamps,
            "obd:RPM": np.concatenate(
                [rng.normal(750.0, 8.0, size=n), rng.normal(2200.0, 50.0, size=n)]
            ),
            "obd:SPEED": np.concatenate([np.zeros(n), rng.normal(35.0, 3.0, size=n)]),
            "obd:THROTTLE_POS": np.concatenate(
                [rng.normal(18.0, 1.0, size=n), rng.normal(32.0, 2.5, size=n)]
            ),
            "obd:COOLANT_TEMP": np.full(n * 2, 85.0),
        }
    )


def _fit_if(resampled: pd.DataFrame, config: CanaryConfig):
    from src.preprocessing.feature_pipeline import featurize_wide_frame

    nominal_features = featurize_wide_frame(resampled, config).features
    return fit_isolation_forest(
        nominal_features,
        _IFConfig(),
        feature_columns=["obd:RPM", "obd:SPEED", "obd:THROTTLE_POS"],
        fitted_at="2026-05-12T00:00:00Z",
    )


# ---------------------------------------------------------------------------
# select_target_channels / cell_seed
# ---------------------------------------------------------------------------


def test_select_target_channels_drops_absent_and_all_nan_columns() -> None:
    frame = pd.DataFrame(
        {
            SESSION_COLUMN: ["a", "a"],
            TIMESTAMP_COLUMN: pd.date_range("2026-01-01", periods=2, freq="1s"),
            "obd:RPM": [700.0, 750.0],
            "obd:SPEED": [np.nan, np.nan],
        }
    )
    assert select_target_channels(frame, ["obd:RPM", "obd:SPEED", "obd:MISSING"]) == ["obd:RPM"]


def test_cell_seed_is_deterministic_and_identity_sensitive() -> None:
    a = cell_seed(42, "session-1", "obd:RPM")
    b = cell_seed(42, "session-1", "obd:RPM")
    assert a == b
    # Different channel/session/base-seed all change the derived seed —
    # otherwise every cell would replay the exact same injection windows.
    assert cell_seed(42, "session-1", "obd:SPEED") != a
    assert cell_seed(42, "session-2", "obd:RPM") != a
    assert cell_seed(7, "session-1", "obd:RPM") != a


# ---------------------------------------------------------------------------
# run_grid — grid construction / session-selection
# ---------------------------------------------------------------------------


def test_run_grid_covers_every_session_x_usable_channel_cell() -> None:
    config = _config()
    resampled_a = _two_mode_resampled("session-a", seed=1)
    resampled_b = _two_mode_resampled("session-b", seed=2)
    trained_if = _fit_if(resampled_a, config)

    cells = run_grid(
        [("session-a", resampled_a), ("session-b", resampled_b)],
        config,
        trained_if,
        fault_kinds=["spike", "dropout"],
        channels=["obd:RPM", "obd:SPEED", "obd:NOT_PRESENT"],
        n_injections=2,
        seed=11,
    )

    # obd:NOT_PRESENT isn't in either frame — dropped, not crashed.
    seen = {(c.session, c.channel) for c in cells}
    assert seen == {
        ("session-a", "obd:RPM"),
        ("session-a", "obd:SPEED"),
        ("session-b", "obd:RPM"),
        ("session-b", "obd:SPEED"),
    }
    for cell in cells:
        assert {m["fault_kind"] for m in cell.report["per_fault"]} == {"spike", "dropout"}
        assert cell.seed == cell_seed(11, cell.session, cell.channel)


def test_run_grid_skips_session_with_no_usable_channel() -> None:
    config = _config()
    resampled = _two_mode_resampled("session-a", seed=1)
    trained_if = _fit_if(resampled, config)

    cells = run_grid(
        [("session-a", resampled)],
        config,
        trained_if,
        channels=["obd:NOWHERE"],
        n_injections=2,
        seed=1,
    )
    assert cells == []


# ---------------------------------------------------------------------------
# aggregate_cells — rollup math
# ---------------------------------------------------------------------------


def _fault_metric(
    detector: str, fault_kind: str, *, runs: int, detected: int, latencies: list[float]
) -> dict:
    return {
        "detector": detector,
        "fault_kind": fault_kind,
        "runs": runs,
        "detected": detected,
        "detection_rate": detected / runs,
        "latencies_seconds": latencies,
        "late_flags": [False] * detected,
        "late_hits": 0,
        "median_time_to_detect_seconds": (float(np.median(latencies)) if latencies else None),
    }


def _nominal_block(alerts: int, rows: int, hours: float) -> dict:
    return {"alerts": alerts, "rows": rows, "hours": hours, "per_hour": alerts / hours}


def test_aggregate_cells_pools_recall_across_cells_and_dedups_nominal_per_session() -> None:
    # Two channels of the SAME session: nominal_fp is identical in both cells
    # (same underlying unmodified frame) — must be counted once, not twice.
    shared_nominal = {"isolation_forest": _nominal_block(alerts=1, rows=3600, hours=1.0)}
    cell_a = GridCell(
        session="s1",
        channel="obd:RPM",
        seed=1,
        report={
            "per_fault": [
                _fault_metric(
                    "isolation_forest", "spike", runs=4, detected=3, latencies=[1.0, 2.0, 3.0]
                )
            ],
            "nominal_fp": shared_nominal,
        },
    )
    cell_b = GridCell(
        session="s1",
        channel="obd:SPEED",
        seed=2,
        report={
            "per_fault": [
                _fault_metric("isolation_forest", "spike", runs=4, detected=1, latencies=[5.0])
            ],
            "nominal_fp": shared_nominal,
        },
    )

    aggregate = aggregate_cells([cell_a, cell_b])

    assert aggregate["sessions"] == ["s1"]
    assert aggregate["channels"] == ["obd:RPM", "obd:SPEED"]
    # 1 alert/hour total (from ONE session, not two) — not 2 alerts/2 hours-ish
    # double counted.
    assert aggregate["nominal_fp"]["isolation_forest"]["alerts"] == 1.0
    assert aggregate["nominal_fp"]["isolation_forest"]["hours"] == 1.0
    assert aggregate["nominal_fp"]["isolation_forest"]["per_hour"] == pytest.approx(1.0)

    [row] = aggregate["table"]
    assert row["detector"] == "isolation_forest"
    assert row["fault_kind"] == "spike"
    assert row["runs"] == 8
    assert row["detected"] == 4
    assert row["recall"] == pytest.approx(4 / 8)
    assert row["median_time_to_detect_seconds"] == pytest.approx(2.5)  # median of [1,2,3,5]
    assert row["alerts_per_hour_clean"] == pytest.approx(1.0)
    # precision = TP / (TP + per_hour * exposure_hours); exposure_hours here
    # is hours(1.0) * runs(4) per cell, summed = 8.0.
    expected_precision = 4 / (4 + 1.0 * 8.0)
    assert row["precision"] == pytest.approx(expected_precision)


def test_aggregate_cells_precision_is_none_with_zero_denominator() -> None:
    cell = GridCell(
        session="s1",
        channel="obd:RPM",
        seed=1,
        report={
            "per_fault": [
                _fault_metric("isolation_forest", "spike", runs=2, detected=0, latencies=[])
            ],
            "nominal_fp": {"isolation_forest": _nominal_block(alerts=0, rows=100, hours=1.0)},
        },
    )
    [row] = aggregate_cells([cell])["table"]
    assert row["detected"] == 0
    assert row["recall"] == 0.0
    assert row["precision"] is None
    assert row["median_time_to_detect_seconds"] is None


def test_aggregate_cells_empty_grid_is_a_valid_empty_report() -> None:
    aggregate = aggregate_cells([])
    assert aggregate["sessions"] == []
    assert aggregate["channels"] == []
    assert aggregate["table"] == []
    assert aggregate["nominal_fp"] == {}


# ---------------------------------------------------------------------------
# build_report / print_report — JSON round-trip
# ---------------------------------------------------------------------------


def test_build_report_round_trips_through_json() -> None:
    cell = GridCell(
        session="s1",
        channel="obd:RPM",
        seed=cell_seed(3, "s1", "obd:RPM"),
        report={
            "per_fault": [
                _fault_metric("fused", "stuck", runs=2, detected=2, latencies=[0.5, 1.5])
            ],
            "nominal_fp": {"fused": _nominal_block(alerts=2, rows=200, hours=2.0)},
        },
    )
    report = build_report([cell], seed=3, n_injections=2, generated_at="20260719-120000")

    encoded = json.dumps(report)
    decoded = json.loads(encoded)
    assert decoded == report
    assert decoded["generated_at"] == "20260719-120000"
    assert decoded["seed"] == 3
    assert decoded["grid_cells"] == 1
    assert decoded["cells"][0]["session"] == "s1"
    assert decoded["cells"][0]["channel"] == "obd:RPM"

    # print_report must not crash on either a populated or an empty report.
    print_report(report)
    print_report(build_report([], seed=3, n_injections=2, generated_at="20260719-120000"))


# ---------------------------------------------------------------------------
# load_resampled_session — on-disk fixture, no MODE_COLUMN (segmentation is
# featurize_wide_frame's job, not this loader's)
# ---------------------------------------------------------------------------


def _write_fixture_session(root: Path, name: str, *, seed: int = 0, n: int = 40) -> Path:
    rng = np.random.default_rng(seed)
    session = root / name
    records: list[TelemetryRecord] = []
    for i in range(n):
        t = 1_778_000_000.0 + i
        records.append(TelemetryRecord.obd(t, "RPM", float(rng.normal(1500.0, 50.0)), "rpm"))
        records.append(TelemetryRecord.obd(t, "SPEED", float(rng.normal(30.0, 3.0)), "mph"))
    write_records_to_parquet(records, session, max_rows=1000)
    return session


def test_load_resampled_session_reads_and_resamples_without_mode_column(tmp_path: Path) -> None:
    session = _write_fixture_session(tmp_path, "2026-05-12T000000")
    config = _config()

    resampled = load_resampled_session(session, config)

    assert "obd:RPM" in resampled.columns
    assert "obd:SPEED" in resampled.columns
    assert "operating_mode" not in resampled.columns
    assert len(resampled) > 0


# ---------------------------------------------------------------------------
# main() — end-to-end CLI over tiny on-disk fixture sessions
# ---------------------------------------------------------------------------


def test_main_runs_end_to_end_and_writes_json(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    from src.models.isolation_forest import default_manifest_path

    config = _config()
    # >=50 rows so the 0.2 calibration-holdout fraction clears the 7.2
    # minimum-holdout-samples floor (see isolation_forest._MIN_CALIBRATION_SAMPLES).
    session_a = _write_fixture_session(tmp_path / "sessions", "2026-05-12T000000", seed=1, n=120)

    # Train a tiny real IF artifact against the fixture's own schema and
    # write it to disk exactly like train_anomaly_model.py does.
    long = load_resampled_session(session_a, config)
    from src.preprocessing.feature_pipeline import featurize_wide_frame

    features = featurize_wide_frame(long, config).features
    trained = fit_isolation_forest(
        features,
        _IFConfig(),
        feature_columns=["obd:RPM", "obd:SPEED"],
        fitted_at="2026-05-12T00:00:00Z",
    )
    artifact_path = tmp_path / "models" / "if" / "model.joblib"
    trained.save(artifact_path, default_manifest_path(artifact_path))

    out_path = tmp_path / "out" / "benchmark.json"
    rc = main(
        [
            str(session_a),
            "--config",
            str(EXAMPLE_YAML),
            "--if-model",
            str(artifact_path),
            "--no-sequence",
            "--channels",
            "obd:RPM",
            "obd:SPEED",
            "--fault-kinds",
            "spike",
            "dropout",
            "--n-injections",
            "2",
            "--seed",
            "5",
            "--out",
            str(out_path),
            "--timestamp",
            "20260719-120000",
        ]
    )

    assert rc == 0
    assert out_path.exists()
    payload = json.loads(out_path.read_text())
    assert payload["generated_at"] == "20260719-120000"
    assert payload["sessions"] == [session_a.name]
    assert {row["fault_kind"] for row in payload["table"]} == {"spike", "dropout"}

    out = capsys.readouterr().out
    assert "DETECTOR BENCHMARK" in out
    assert f"wrote {out_path}" in out


def test_main_missing_if_model_reports_error(tmp_path: Path) -> None:
    session_a = _write_fixture_session(tmp_path / "sessions", "2026-05-12T000000")
    rc = main(
        [
            str(session_a),
            "--config",
            str(EXAMPLE_YAML),
            "--if-model",
            str(tmp_path / "nope" / "model.joblib"),
            "--no-sequence",
            "--out",
            str(tmp_path / "out.json"),
        ]
    )
    assert rc == 1


def test_default_target_channels_are_engine_telemetry_obd_columns() -> None:
    assert all(channel.startswith("obd:") for channel in DEFAULT_TARGET_CHANNELS)
