"""Tests for the offline session-vs-behavior-surface deviation analyzer."""

from __future__ import annotations

from pathlib import Path

import pytest

from src.analysis.session_deviations import analyze_session, compare_sessions
from src.config import CanaryConfig
from src.decode.behavior_models import (
    Axis1D,
    Axis2D,
    BehaviorModel1D,
    BehaviorModel2D,
    BehaviorModels,
    DeviationConfig,
    GridBinnedMeanFit,
    GridCell,
    PolynomialFit,
)
from src.storage.parquet_writer import write_records_to_parquet
from src.telemetry import TelemetryRecord


@pytest.fixture
def config() -> CanaryConfig:
    return CanaryConfig.from_yaml(Path("canary.example.yaml"))


def _timing_model() -> BehaviorModel2D:
    """2x2 rpm x load grid. Row-major: axes[0]=rpm outer, axes[1]=load inner.
    cell(rpm_bin, load_bin) means: (0,0)=10, (0,1)=15, (1,0)=20, (1,1)=25."""
    return BehaviorModel2D(
        name="timing_vs_rpm_load",
        kind="surface_2d",
        output="obd:TIMING_ADVANCE",
        axes=(
            Axis2D(name="rpm", channel="obd:RPM", bin_edges=[0.0, 1000.0, 2000.0]),
            Axis2D(name="load", channel="obd:ENGINE_LOAD", bin_edges=[0.0, 50.0, 100.0]),
        ),
        fit=GridBinnedMeanFit(
            kind="grid_binned_mean",
            cells=[
                GridCell(mean=10.0, std=1.0, count=20),
                GridCell(mean=15.0, std=1.0, count=20),
                GridCell(mean=20.0, std=1.0, count=20),
                GridCell(mean=25.0, std=1.0, count=20),
            ],
            sample_count=80,
            fitted_at="2026-01-01T00:00:00Z",
        ),
        deviation=DeviationConfig(kind="absolute", warning_band=3.0, critical_band=8.0),
    )


def _coolant_model() -> BehaviorModel1D:
    """y = 25 + 0.15*x -> at x=100, expected = 40.0."""
    return BehaviorModel1D(
        name="coolant_warmup",
        kind="surface_1d",
        output="obd:COOLANT_TEMP",
        axis=Axis1D(name="t", channel="obd:RUN_TIME", domain=(0.0, 600.0)),
        fit=PolynomialFit(
            kind="poly",
            coefficients=[25.0, 0.15],
            sample_count=100,
            fitted_at="2026-01-01T00:00:00Z",
        ),
        deviation=DeviationConfig(kind="absolute", warning_band=5.0, critical_band=15.0),
    )


def _write_deviation_session(
    root: Path,
    *,
    rpm: float,
    load: float,
    run_time_base: float,
    coolant_offset: float,
    timing_offset: float,
    n: int = 10,
    session_id: str = "2026-07-19T000000",
) -> Path:
    """A session parked at one fixed (rpm, load) operating point, with a known
    constant offset injected into COOLANT_TEMP and TIMING_ADVANCE so the
    analyzer's recovered deviation is a known, exact number."""
    session = root / session_id
    records = []
    for i in range(n):
        t = 1_780_000_000.0 + i
        run_time = run_time_base + i
        expected_coolant = 25.0 + 0.15 * run_time
        expected_timing = 10.0 if (rpm < 1000 and load < 50) else None  # only the tested cell
        records.extend(
            [
                TelemetryRecord.obd(t, "RPM", rpm, "rpm"),
                TelemetryRecord.obd(t, "ENGINE_LOAD", load, "%"),
                TelemetryRecord.obd(t, "RUN_TIME", run_time, "s"),
                TelemetryRecord.obd(t, "COOLANT_TEMP", expected_coolant + coolant_offset, "degC"),
                TelemetryRecord.obd(
                    t,
                    "TIMING_ADVANCE",
                    (expected_timing or 0.0) + timing_offset,
                    "degBTDC",
                ),
            ]
        )
    write_records_to_parquet(records, session, max_rows=10_000)
    return session


def test_analyze_session_recovers_injected_offset_and_localizes_to_cell(
    config: CanaryConfig, tmp_path: Path
) -> None:
    """A constant +5 offset injected into TIMING_ADVANCE at a fixed (rpm, load)
    operating point must be recovered as mean_deviation ~= +5, localized to
    exactly the (rpm_bin=0, load_bin=0) cell — this is the whole point of the
    tool: showing *where* a shift lives, not just a session-wide average."""
    session = _write_deviation_session(
        tmp_path,
        rpm=500.0,
        load=25.0,
        run_time_base=100.0,
        coolant_offset=0.0,
        timing_offset=5.0,
        n=10,
    )
    models = BehaviorModels(schema_version=1, models=[_timing_model()])

    report = analyze_session(session, config, models)

    assert report.n_rows == 10
    (timing,) = report.surfaces
    assert timing.name == "timing_vs_rpm_load"
    assert timing.n_in_domain == 10
    assert timing.mean_deviation == pytest.approx(5.0, abs=1e-6)
    assert timing.mean_expected == pytest.approx(10.0, abs=1e-6)
    assert timing.warning_fraction == 1.0  # |5| >= warning_band(3), < critical_band(8)
    assert timing.critical_fraction == 0.0

    assert len(timing.cells) == 1  # only one operating point visited
    cell = timing.cells[0]
    assert cell["x_bin"] == [0.0, 1000.0]  # rpm bin 0
    assert cell["y_bin"] == [0.0, 50.0]  # load bin 0
    assert cell["n"] == 10
    assert cell["mean_deviation"] == pytest.approx(5.0, abs=1e-6)


def test_analyze_session_no_offset_reports_zero_deviation_and_ok_severity(
    config: CanaryConfig, tmp_path: Path
) -> None:
    session = _write_deviation_session(
        tmp_path,
        rpm=500.0,
        load=25.0,
        run_time_base=100.0,
        coolant_offset=0.0,
        timing_offset=0.0,
        n=5,
    )
    models = BehaviorModels(schema_version=1, models=[_timing_model()])

    report = analyze_session(session, config, models)

    (timing,) = report.surfaces
    assert timing.mean_deviation == pytest.approx(0.0, abs=1e-6)
    assert timing.warning_fraction == 0.0
    assert timing.critical_fraction == 0.0


def test_analyze_session_different_cell_gets_a_different_expected_value(
    config: CanaryConfig, tmp_path: Path
) -> None:
    """Sanity-check the grid lookup itself: a different (rpm, load) operating
    point must be graded against its own cell's mean, not the first cell's."""
    session = _write_deviation_session(
        tmp_path,
        rpm=1500.0,  # rpm bin 1
        load=75.0,  # load bin 1
        run_time_base=50.0,
        coolant_offset=0.0,
        timing_offset=0.0,
        n=5,
    )
    models = BehaviorModels(schema_version=1, models=[_timing_model()])

    report = analyze_session(session, config, models)
    (timing,) = report.surfaces
    assert timing.n_in_domain == 5
    assert timing.mean_expected == pytest.approx(25.0, abs=1e-6)  # cell (1,1)'s mean


def test_analyze_session_1d_surface_recovers_offset(config: CanaryConfig, tmp_path: Path) -> None:
    session = _write_deviation_session(
        tmp_path,
        rpm=500.0,
        load=25.0,
        run_time_base=100.0,
        coolant_offset=10.0,
        timing_offset=0.0,
        n=8,
    )
    models = BehaviorModels(schema_version=1, models=[_coolant_model()])

    report = analyze_session(session, config, models)
    (coolant,) = report.surfaces
    assert coolant.kind == "surface_1d"
    assert coolant.cells == []  # binning is 2D-only
    assert coolant.mean_deviation == pytest.approx(10.0, abs=1e-6)
    assert coolant.warning_fraction == 1.0  # 10 in [5, 15) -> warning
    assert coolant.critical_fraction == 0.0


def test_analyze_session_gated_on_missing_mode_reported(
    config: CanaryConfig, tmp_path: Path
) -> None:
    """A surface gated on an operating_mode the config's segmenter can't emit
    is flagged via unknown_operating_modes, distinguishing "silently never
    evaluates" from "evaluated and matched fine"."""
    session = _write_deviation_session(
        tmp_path,
        rpm=500.0,
        load=25.0,
        run_time_base=100.0,
        coolant_offset=0.0,
        timing_offset=0.0,
        n=3,
    )
    gated_model = _timing_model().model_copy(
        update={"name": "timing_gated", "operating_mode": "totally_not_a_real_mode"}
    )
    models = BehaviorModels(schema_version=1, models=[gated_model])

    report = analyze_session(session, config, models)

    assert report.gated_on_missing_mode == {"timing_gated": "totally_not_a_real_mode"}
    (surface,) = report.surfaces
    assert surface.n_observed == 0  # every row failed the mode gate
    assert surface.n_in_domain == 0


def test_analyze_session_multiple_surfaces_independent(
    config: CanaryConfig, tmp_path: Path
) -> None:
    session = _write_deviation_session(
        tmp_path,
        rpm=500.0,
        load=25.0,
        run_time_base=100.0,
        coolant_offset=10.0,
        timing_offset=5.0,
        n=6,
    )
    models = BehaviorModels(schema_version=1, models=[_timing_model(), _coolant_model()])

    report = analyze_session(session, config, models)

    by_name = {s.name: s for s in report.surfaces}
    assert set(by_name) == {"timing_vs_rpm_load", "coolant_warmup"}
    assert by_name["timing_vs_rpm_load"].mean_deviation == pytest.approx(5.0, abs=1e-6)
    assert by_name["coolant_warmup"].mean_deviation == pytest.approx(10.0, abs=1e-6)


def test_report_to_dict_round_trips_json_safely(config: CanaryConfig, tmp_path: Path) -> None:
    import json

    session = _write_deviation_session(
        tmp_path,
        rpm=500.0,
        load=25.0,
        run_time_base=100.0,
        coolant_offset=0.0,
        timing_offset=2.0,
        n=4,
    )
    models = BehaviorModels(schema_version=1, models=[_timing_model()])
    report = analyze_session(session, config, models)

    payload = json.loads(json.dumps(report.to_dict()))
    assert payload["n_rows"] == 4
    assert payload["surfaces"][0]["name"] == "timing_vs_rpm_load"


# ---------------------------------------------------------------------------
# compare_sessions (Task 5.4)
# ---------------------------------------------------------------------------


def test_compare_sessions_computes_per_surface_and_per_cell_deltas(
    config: CanaryConfig, tmp_path: Path
) -> None:
    """Session A has a +2 timing offset, session B has a +7 offset at the
    same operating point — the A/B delta must recover the exact +5 shift,
    localized to the one cell both sessions actually visited."""
    session_a = _write_deviation_session(
        tmp_path,
        rpm=500.0,
        load=25.0,
        run_time_base=100.0,
        coolant_offset=0.0,
        timing_offset=2.0,
        n=8,
        session_id="2026-07-01T000000",
    )
    session_b = _write_deviation_session(
        tmp_path,
        rpm=500.0,
        load=25.0,
        run_time_base=100.0,
        coolant_offset=0.0,
        timing_offset=7.0,
        n=8,
        session_id="2026-07-19T000000",
    )
    models = BehaviorModels(schema_version=1, models=[_timing_model()])

    comparison = compare_sessions(session_a, session_b, config, models)

    (timing,) = comparison.surfaces
    assert timing.name == "timing_vs_rpm_load"
    assert timing.mean_deviation_a == pytest.approx(2.0, abs=1e-6)
    assert timing.mean_deviation_b == pytest.approx(7.0, abs=1e-6)
    assert timing.mean_deviation_delta == pytest.approx(5.0, abs=1e-6)

    assert len(timing.cells) == 1
    cell = timing.cells[0]
    assert cell["x_bin"] == [0.0, 1000.0]
    assert cell["y_bin"] == [0.0, 50.0]
    assert cell["n_a"] == 8
    assert cell["n_b"] == 8
    assert cell["delta"] == pytest.approx(5.0, abs=1e-6)


def test_compare_sessions_cell_present_only_on_one_side(
    config: CanaryConfig, tmp_path: Path
) -> None:
    """Session A visits the low-rpm/low-load cell; session B visits a
    different cell entirely — each cell should carry a delta of ``None``
    (unmatched), not a fabricated comparison against an unvisited cell."""
    session_a = _write_deviation_session(
        tmp_path,
        rpm=500.0,
        load=25.0,
        run_time_base=100.0,
        coolant_offset=0.0,
        timing_offset=2.0,
        n=5,
        session_id="2026-07-01T000000",
    )
    session_b = _write_deviation_session(
        tmp_path,
        rpm=1500.0,
        load=75.0,
        run_time_base=100.0,
        coolant_offset=0.0,
        timing_offset=2.0,
        n=5,
        session_id="2026-07-19T000000",
    )
    models = BehaviorModels(schema_version=1, models=[_timing_model()])

    comparison = compare_sessions(session_a, session_b, config, models)

    (timing,) = comparison.surfaces
    # Two distinct operating points visited (one per session) -> two cells,
    # each populated on only one side -> no cross-session delta for either.
    assert len(timing.cells) == 2
    by_bin = {(tuple(c["x_bin"]), tuple(c["y_bin"])): c for c in timing.cells}
    low_cell = by_bin[((0.0, 1000.0), (0.0, 50.0))]
    high_cell = by_bin[((1000.0, 2000.0), (50.0, 100.0))]
    assert low_cell["n_a"] == 5
    assert low_cell["n_b"] == 0
    assert low_cell["delta"] is None
    assert high_cell["n_a"] == 0
    assert high_cell["n_b"] == 5
    assert high_cell["delta"] is None


def test_compare_sessions_to_dict_json_safe(config: CanaryConfig, tmp_path: Path) -> None:
    import json

    session_a = _write_deviation_session(
        tmp_path,
        rpm=500.0,
        load=25.0,
        run_time_base=100.0,
        coolant_offset=0.0,
        timing_offset=1.0,
        n=3,
        session_id="2026-07-01T000000",
    )
    session_b = _write_deviation_session(
        tmp_path,
        rpm=500.0,
        load=25.0,
        run_time_base=100.0,
        coolant_offset=0.0,
        timing_offset=4.0,
        n=3,
        session_id="2026-07-19T000000",
    )
    models = BehaviorModels(schema_version=1, models=[_timing_model()])

    comparison = compare_sessions(session_a, session_b, config, models)
    payload = json.loads(json.dumps(comparison.to_dict()))
    assert payload["surfaces"][0]["mean_deviation_delta"] == pytest.approx(3.0, abs=1e-6)


def test_analyze_session_resamples_with_the_training_recipe(
    config: CanaryConfig, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Parity: the analysis must resample exactly as training does.

    ``analyze_session`` used to build its own ``ResampleConfig`` from ``hz`` and
    the slow-channel ceiling only, silently taking the dataclass defaults for
    ``max_gap_factor`` and ``session_gap_seconds``. Any config that set either
    then analysed a session on a different grid from the one the surfaces were
    fit on. Non-default values here make that divergence visible.
    """
    import src.analysis.session_deviations as session_deviations
    from src.preprocessing.feature_pipeline import resample_config_for

    tuned = config.model_copy(
        update={
            "preprocessing": config.preprocessing.model_copy(
                update={"max_gap_factor": 5.0, "session_gap_seconds": 45.0}
            )
        }
    )
    real = session_deviations._decode_session_to_resampled
    seen = []

    def spy(session_dir, bindings, dbc_database, did_map, resample_config):
        seen.append(resample_config)
        return real(session_dir, bindings, dbc_database, did_map, resample_config)

    monkeypatch.setattr(session_deviations, "_decode_session_to_resampled", spy)
    session = _write_deviation_session(
        tmp_path, rpm=500.0, load=25.0, run_time_base=100.0, coolant_offset=0.0, timing_offset=0.0
    )

    analyze_session(session, tuned, BehaviorModels(schema_version=1, models=[_timing_model()]))

    assert seen == [resample_config_for(tuned)]
    assert seen[0].max_gap_factor == 5.0
    assert seen[0].session_gap_seconds == 45.0
