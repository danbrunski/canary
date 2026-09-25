"""Tests for :func:`compute_fingerprint` (Task 4.1, eval-longitudinal-and-dtc.md)."""

from __future__ import annotations

import json
from pathlib import Path

import pytest
import yaml

from src.analysis.fingerprint import (
    FINGERPRINT_FILENAME,
    FINGERPRINT_VERSION,
    SCHEMA_VERSION,
    SessionFingerprint,
    compute_fingerprint,
    is_stale,
    read_fingerprint,
    write_fingerprint,
)
from src.config import CanaryConfig
from src.decode.behavior_models import (
    Axis1D,
    BehaviorModel1D,
    BehaviorModels,
    PolynomialFit,
)
from src.storage.parquet_writer import write_records_to_parquet
from src.telemetry import TelemetryRecord
from tests.builders import write_rich_session

REPO_ROOT = Path(__file__).resolve().parents[1]
EXAMPLE_YAML = REPO_ROOT / "canary.example.yaml"


def _config() -> CanaryConfig:
    return CanaryConfig.from_yaml(EXAMPLE_YAML)


def _write_warm_start_session(root: Path, *, name: str = "2026-05-20T130000") -> Path:
    """No cold start, no cranking transient — engine already idling warm at
    the first sample. Used to prove preconditioned stats are omitted."""
    session = root / name
    records: list[TelemetryRecord] = []
    base = 1_779_400_000.0
    for i in range(30):
        t = base + i
        records.extend(
            [
                TelemetryRecord.obd(t, "RPM", 800.0, "rpm"),
                TelemetryRecord.obd(t, "SPEED", 0.0, "km/h"),
                TelemetryRecord.obd(t, "COOLANT_TEMP", 85.0, "C"),
            ]
        )
    write_records_to_parquet(records, session, max_rows=10_000)
    return session


@pytest.fixture
def rich_session(tmp_path: Path) -> Path:
    return write_rich_session(tmp_path)


def test_warmup_time_constant(rich_session: Path) -> None:
    stats = compute_fingerprint(rich_session, _config())
    assert "warmup_time_constant_s" in stats
    # Coolant (20 + 1.667*(i-5)) crosses 80C at i=41 -> ~41s from the first
    # (cold) sample at i=0. Raw-column-based, unaffected by mode dwell.
    assert 38.0 <= stats["warmup_time_constant_s"] <= 44.0


def test_idle_at_operating_temp_stats(rich_session: Path) -> None:
    stats = compute_fingerprint(rich_session, _config())
    # Mode-dwell hysteresis lets a couple of highway rows (rpm=2200,
    # maf=15, load=45) bleed into the tail of the ~80-row idle window
    # before the segmenter fully commits to the mode switch — tolerate
    # that with a loose relative bound rather than pinning exact means.
    assert stats["idle_rpm_mean"] == pytest.approx(800.0, rel=0.1)
    assert stats["idle_maf_mean"] == pytest.approx(3.0, rel=0.3)
    assert stats["idle_engine_load_mean"] == pytest.approx(25.0, rel=0.15)
    assert "idle_rpm_std" in stats
    assert 0.0 <= stats["idle_rpm_std"] < 800.0


def test_fuel_trim_stats_idle_and_cruise(rich_session: Path) -> None:
    stats = compute_fingerprint(rich_session, _config())
    assert stats["stft_mean_idle"] == pytest.approx(0.5, abs=0.2)  # mean(2.0, -1.0)
    assert stats["ltft_mean_idle"] == pytest.approx(0.5, abs=0.2)  # mean(3.0, -2.0)
    assert stats["trim_combined_mean_idle"] == pytest.approx(1.0, abs=0.4)  # mean(5.0, -3.0)
    # Cruise (highway) rows only ever lose a couple of dwell-delayed
    # samples to the idle side, never gain contaminated ones, so these are
    # exact.
    assert stats["stft_mean_cruise"] == pytest.approx(0.75)  # mean(1.0, 0.5)
    assert stats["ltft_mean_cruise"] == pytest.approx(1.5)  # mean(4.0, -1.0)
    assert stats["trim_combined_mean_cruise"] == pytest.approx(2.25)  # mean(5.0, -0.5)


def test_cranking_duration_and_voltage(rich_session: Path) -> None:
    stats = compute_fingerprint(rich_session, _config())
    assert stats["cranking_duration_s"] == pytest.approx(5.0)
    assert stats["voltage_at_crank_v"] == pytest.approx(10.5)
    assert stats["voltage_at_idle_v"] == pytest.approx(14.2)


def test_bank_balance_stats(rich_session: Path) -> None:
    stats = compute_fingerprint(rich_session, _config())
    # STFT bank balance is time-smoothed; both banks and both phases are
    # constant, so the smoothed mean still lands on the raw difference.
    assert "bank_balance_mean:stft" in stats
    assert "bank_balance_mean:ltft" in stats
    # AFR bank balance needs DID polling this fixture never provides.
    assert "bank_balance_mean:afr" not in stats


def test_committee_residual_stat(rich_session: Path) -> None:
    stats = compute_fingerprint(rich_session, _config())
    assert stats["committee_residual_mean:speed_source_divergence"] == pytest.approx(5.0)


def test_misfire_count_stat_is_the_session_ending_count(rich_session: Path) -> None:
    """Task 3.2 — the Mode 06 counter is cumulative within a drive, so the
    fingerprint stat is the session's max observed value, per cylinder."""
    stats = compute_fingerprint(rich_session, _config())
    assert stats["misfire_count_cyl:1"] == pytest.approx(5.0)
    # No MISFIRE_COUNT_CYL2 reading exists this session — no stat key either.
    assert "misfire_count_cyl:2" not in stats


def test_behavior_deviation_stat(tmp_path: Path, rich_session: Path) -> None:
    model = BehaviorModel1D(
        name="rpm_constant_750",
        kind="surface_1d",
        output="obd:RPM",
        output_unit="rpm",
        axis=Axis1D(name="t", channel="obd:RUN_TIME", unit="s", domain=(0.0, 1000.0)),
        fit=PolynomialFit(
            kind="poly",
            coefficients=[750.0],
            sample_count=10,
            r_squared=0.0,
            sessions=["s1"],
            fitted_at="now",
        ),
    )
    models = BehaviorModels(schema_version=1, models=[model])
    models_path = tmp_path / "behavior_models.yaml"
    models_path.write_text(yaml.safe_dump(models.model_dump(mode="json")))

    config = _config().model_copy(
        update={"daemon": _config().daemon.model_copy(update={"behavior_models": models_path})}
    )
    stats = compute_fingerprint(rich_session, config)
    assert "behavior_deviation_abs_mean:rpm_constant_750" in stats
    assert stats["behavior_deviation_abs_mean:rpm_constant_750"] > 0.0


def test_warmup_and_cranking_omitted_without_cold_start(tmp_path: Path) -> None:
    session = _write_warm_start_session(tmp_path)
    stats = compute_fingerprint(session, _config())
    assert "warmup_time_constant_s" not in stats
    assert "cranking_duration_s" not in stats
    assert "voltage_at_crank_v" not in stats
    # No NaN sentinels either — every value present must be finite.
    assert all(v == v for v in stats.values())  # v == v is False for NaN


def _write_lagged_rpm_session(root: Path, *, name: str = "2026-05-20T140000") -> Path:
    """Voltage present from tick 0; RPM missing for the first few ticks (a
    normal round-robin OBD polling artifact — resample_wide only
    forward-fills) then constant above ``_RUNNING_RPM``. The engine was
    idling the whole session; no cranking transient was ever captured, so
    the leading RPM NaNs must not be mistaken for a crank->running crossing.
    """
    session = root / name
    records: list[TelemetryRecord] = []
    base = 1_779_500_000.0
    for i in range(30):
        t = base + i
        records.append(TelemetryRecord.obd(t, "CONTROL_MODULE_VOLTAGE", 14.2, "V"))
        records.append(TelemetryRecord.obd(t, "COOLANT_TEMP", 85.0, "C"))
        records.append(TelemetryRecord.obd(t, "SPEED", 0.0, "km/h"))
        if i >= 3:
            records.append(TelemetryRecord.obd(t, "RPM", 800.0, "rpm"))
    write_records_to_parquet(records, session, max_rows=10_000)
    return session


def test_voltage_at_crank_omitted_when_rpm_merely_lags(tmp_path: Path) -> None:
    """Regression: a session that never captured a real crank (RPM just
    lagged voltage by a few resampled ticks, e.g. round-robin OBD polling)
    must not emit a fabricated ``voltage_at_crank_v``."""
    session = _write_lagged_rpm_session(tmp_path)
    stats = compute_fingerprint(session, _config())
    assert "voltage_at_crank_v" not in stats
    assert stats["voltage_at_idle_v"] == pytest.approx(14.2)


def _write_o2_session(root: Path, *, name: str = "2026-05-20T150000") -> Path:
    """150 s of steady warm idle (rpm/speed/coolant all idle-mode-compatible
    the whole session) with four O2 traces engineered to separate the
    catalyst-efficiency and O2-health stats cleanly (Task 4.4/4.5):

    * Bank 1 upstream/downstream: upstream toggles every sample (fast,
      closed-loop-typical); downstream dwells 20 samples high / 5 low
      (period 25, exactly 6 full periods over 150 samples) — a healthy,
      damping catalyst plus a response asymmetry to pin (~20:5 dwell
      ratio).
    * Bank 2 upstream/downstream: downstream mirrors upstream's every-
      sample toggle — an un-damping (degraded) catalyst.
    * Upstream wideband lambda: alternates +/-0.02 around 1.0 (stoich) —
      near-zero bias, nonzero noise, at steady idle.
    """
    session = root / name
    records: list[TelemetryRecord] = []
    base = 1_779_600_000.0
    for i in range(150):
        t = base + i
        upstream_high = i % 2 == 0
        downstream_b1_high = (i % 25) < 20
        downstream_b2_high = upstream_high
        lam = 1.02 if i % 2 == 0 else 0.98
        records.extend(
            [
                TelemetryRecord.obd(t, "RPM", 800.0, "rpm"),
                TelemetryRecord.obd(t, "SPEED", 0.0, "km/h"),
                TelemetryRecord.obd(t, "COOLANT_TEMP", 90.0, "C"),
                TelemetryRecord.obd(t, "O2_B1S1", 0.8 if upstream_high else 0.1, "V"),
                TelemetryRecord.obd(t, "O2_B2S1", 0.8 if upstream_high else 0.1, "V"),
                TelemetryRecord.obd(t, "O2_B1S2", 0.8 if downstream_b1_high else 0.1, "V"),
                TelemetryRecord.obd(t, "O2_B2S2", 0.8 if downstream_b2_high else 0.1, "V"),
                TelemetryRecord.obd(t, "O2_WR_LAMBDA_S1", lam, "ratio"),
            ]
        )
    write_records_to_parquet(records, session, max_rows=10_000)
    return session


def test_cat_efficiency_stats_separate_healthy_from_degraded_bank(tmp_path: Path) -> None:
    """Task 4.4 — bank 1's slow-dwelling downstream (damping cat) should
    score much closer to 1 than bank 2's downstream, which mirrors the
    fast upstream toggle (no damping at all)."""
    session = _write_o2_session(tmp_path)
    stats = compute_fingerprint(session, _config())
    assert stats["cat_efficiency_mean:b1"] > 0.5
    assert stats["cat_efficiency_mean:b2"] < 0.3
    assert stats["cat_efficiency_mean:b1"] > stats["cat_efficiency_mean:b2"]


def test_o2_downstream_switch_rate_and_asymmetry(tmp_path: Path) -> None:
    """Task 4.5 — bank 1's downstream dwells 20-high/5-low per 25-sample
    period: ~12 transitions over ~149s, and a rich:lean dwell ratio near
    the engineered 20:5 = 4.0 (undercounted slightly by the run-duration
    convention, see ``_o2_run_durations``)."""
    session = _write_o2_session(tmp_path)
    stats = compute_fingerprint(session, _config())
    assert stats["o2_downstream_switch_rate_hz:b1"] == pytest.approx(12.0 / 149.0, rel=0.2)
    assert stats["o2_downstream_response_asymmetry:b1"] == pytest.approx(4.75, rel=0.3)
    # Bank 2 downstream toggles every sample (same as upstream) — a much
    # higher switch rate than bank 1's damped trace.
    assert stats["o2_downstream_switch_rate_hz:b2"] > stats["o2_downstream_switch_rate_hz:b1"]


def test_o2_upstream_wideband_lambda_noise_and_bias_at_idle(tmp_path: Path) -> None:
    """Task 4.5 — the whole session is steady idle; lambda alternates
    +/-0.02 around stoich (1.0), so bias ~ 0 and noise ~ 0.02."""
    session = _write_o2_session(tmp_path)
    stats = compute_fingerprint(session, _config())
    assert stats["o2_upstream_wideband_lambda_bias_idle"] == pytest.approx(0.0, abs=0.005)
    assert stats["o2_upstream_wideband_lambda_noise_idle"] == pytest.approx(0.02, rel=0.2)


def test_cat_and_o2_stats_omitted_without_o2_sensors(rich_session: Path) -> None:
    """The rich fixture never polls any O2 PID — none of the Task 4.4/4.5
    stats should appear (absent-input gating, not zero-filled)."""
    stats = compute_fingerprint(rich_session, _config())
    for key in stats:
        assert not key.startswith("cat_efficiency_mean:")
        assert not key.startswith("o2_downstream_")
        assert not key.startswith("o2_upstream_wideband_lambda_")


def test_raises_on_missing_parquet(tmp_path: Path) -> None:
    session = tmp_path / "missing"
    session.mkdir()
    with pytest.raises(FileNotFoundError):
        compute_fingerprint(session, _config())


def test_write_read_round_trip(tmp_path: Path, rich_session: Path) -> None:
    stats = compute_fingerprint(rich_session, _config())
    path = write_fingerprint(rich_session, stats)
    assert path == rich_session / FINGERPRINT_FILENAME

    payload = json.loads(path.read_text())
    assert payload["schema_version"] == SCHEMA_VERSION
    assert payload["fingerprint_version"] == FINGERPRINT_VERSION
    assert payload["session_id"] == rich_session.name
    assert payload["stats"]["idle_rpm_mean"] == pytest.approx(stats["idle_rpm_mean"])

    loaded = read_fingerprint(rich_session)
    assert loaded is not None
    assert loaded.stats == stats


def test_is_stale() -> None:
    assert is_stale(None) is True
    current = SessionFingerprint(
        schema_version=SCHEMA_VERSION,
        fingerprint_version=FINGERPRINT_VERSION,
        computed_at="2026-05-20T12:00:00Z",
        session_id="s",
        stats={},
    )
    assert is_stale(current) is False
    stale = SessionFingerprint(
        schema_version=SCHEMA_VERSION,
        fingerprint_version="0.0.0",
        computed_at="2026-05-20T12:00:00Z",
        session_id="s",
        stats={},
    )
    assert is_stale(stale) is True
