"""N02.4: fuel-supply droop separated from the air-model error.

Synthetic pressure sessions (100 Hz rail pressure falling with a known
quadratic in MAF) exercise the measured path, the model, every fallback and
every "which kind of empty". The recorded figure -- about -1.9 psi at about
107 g/s on 2026-10-04 (``data/analyses/2026-10-04-fuel-pressure-first-drive.md``)
-- is then reproduced through the same path on the real session, which skips
by name when it is not on this machine.
"""

from __future__ import annotations

from pathlib import Path

import numpy as np
import pandas as pd
import pytest

from src.analysis.fuel_comp_droop import (
    DROOP_COLUMNS,
    PressureSession,
    apply_droop,
    drive_reference,
    droop_evidence,
    droop_known_answer,
    pressure_sessions,
    read_pressure_session,
)
from src.analysis.fuel_comp_inputs import DroopRules, load_fuel_comp_declaration
from tests.rom_fixtures import PROFILE

CORPUS = Path.home() / "canary-data" / "sessions" / "nissan-xterra-2008"
T0 = 1_790_000_000.0
#: psi droop = A x maf + B x maf^2: -1.9 psi at 107 g/s, steepening.
A, B = -0.005, -1.19e-4
REFERENCE_PSI = 50.8


@pytest.fixture(scope="module")
def rules() -> DroopRules:
    return load_fuel_comp_declaration(PROFILE).droop


def _maf(t: np.ndarray) -> np.ndarray:
    """30 s at 3 g/s (the reference), then a ramp to 120 g/s over 90 s."""
    return np.where(t < 30, 3.0, 3.0 + (t - 30) * 117.0 / 90.0)


def _pressure_session(
    name: str, rules: DroopRules, *, seconds: float = 120.0, fault_at: float | None = None
) -> PressureSession:
    tp = np.arange(0, seconds, 0.01)
    maf = _maf(tp)
    psi = REFERENCE_PSI + A * maf + B * maf * maf
    tm = np.arange(0.0, seconds, 0.2)
    return PressureSession(
        session=name,
        pressure_times=T0 + tp,
        pressure_kpa=psi * rules.kpa_per_psi,
        fault_times=np.array([] if fault_at is None else [T0 + fault_at]),
        maf_times=T0 + tm,
        maf_gs=_maf(tm),
        rpm_times=T0 + tm - 0.05,
        rpm=np.full(len(tm), 2000.0),
    )


def _samples(session: str, seconds: list[float], maf: list[float], error: float) -> pd.DataFrame:
    return pd.DataFrame(
        {
            "session": session,
            "timestamp": pd.to_datetime([T0 + s for s in seconds], unit="s", utc=True),
            "maf_gs": maf,
            "error_pct": error,
        }
    )


def test_the_reference_is_the_drives_own_low_flow_median(rules: DroopRules) -> None:
    ref = drive_reference(_pressure_session("a", rules), rules)
    assert ref.reason is None
    expected = REFERENCE_PSI + A * 3.0 + B * 9.0
    assert ref.psi == pytest.approx(expected, abs=1e-6)
    assert ref.n >= rules.reference.min_reads


def test_measured_droop_is_applied_per_sample_and_the_remainder_is_what_is_left(
    rules: DroopRules,
) -> None:
    sessions = {"a": _pressure_session("a", rules), "b": _pressure_session("b", rules)}
    evidence = droop_evidence(sessions, rules)
    # t = 112.3 s: MAF 110 g/s, 6 % lean in total.
    at = 30 + (110.0 - 3.0) * 90.0 / 117.0
    out = apply_droop(
        _samples("a", [at], [110.0], 6.0), sessions, evidence.references, evidence.model, rules
    )
    assert list(out.columns) == list(DROOP_COLUMNS)
    row = out.iloc[0]
    assert row["droop_kind"] == "measured"
    ref = evidence.references["a"].psi
    assert ref is not None
    expected_psi = REFERENCE_PSI + A * 110 + B * 110 * 110
    assert row["pressure_psi"] == pytest.approx(expected_psi, abs=0.01)
    flow = np.sqrt(expected_psi / ref)
    assert row["flow_factor"] == pytest.approx(flow, abs=1e-4)
    droop_error = 100 * (1 / flow - 1)
    assert row["droop_error_pct"] == pytest.approx(droop_error, abs=0.01)
    # (1 + total) = (1 + droop) x (1 + remainder): multiplicative, so the two
    # parts compose back to the total exactly.
    assert (1 + row["droop_error_pct"] / 100) * (1 + row["remainder_pct"] / 100) == (
        pytest.approx(1.06, abs=1e-12)
    )
    assert row["remainder_pct"] < 6.0


def test_elsewhere_the_droop_is_modelled_and_labelled_with_its_fit(rules: DroopRules) -> None:
    sessions = {"a": _pressure_session("a", rules), "b": _pressure_session("b", rules)}
    evidence = droop_evidence(sessions, rules)
    model = evidence.model
    assert model.ok and model.drives == ["a", "b"]
    assert model.n_pairs >= rules.model.min_pairs
    # Droop is measured from the 3 g/s reference, not from zero flow, so the
    # fitted curve is the true one less its value at 3 g/s (-0.016 psi).
    assert model.a == pytest.approx(A, abs=1e-3)
    assert model.b == pytest.approx(B, abs=1e-5)
    assert float(model.droop_psi(np.array([107.0]))[0]) == pytest.approx(-1.9 + 0.016, abs=0.03)
    out = apply_droop(
        _samples("august", [10.0, 20.0, 30.0], [107.0, 400.0, np.nan], 3.0),
        sessions,
        evidence.references,
        model,
        rules,
    )
    assert list(out["droop_kind"]) == ["modelled", "none", "none"]
    assert out["droop_basis"].iloc[0].startswith("modelled (no_pressure_logged")
    assert out["droop_basis"].iloc[1].startswith("maf_beyond_fit_range")
    assert out["droop_basis"].iloc[2].startswith("maf_unpaired")
    assert out["droop_psi"].iloc[0] == pytest.approx(-1.9, abs=0.05)
    assert np.isnan(out["remainder_pct"].iloc[1])


def test_a_fault_in_the_window_falls_back_to_the_model_and_says_why(rules: DroopRules) -> None:
    sessions = {
        "a": _pressure_session("a", rules, fault_at=100.0),
        "b": _pressure_session("b", rules),
    }
    evidence = droop_evidence(sessions, rules)
    out = apply_droop(
        _samples("a", [100.1], [90.0], 2.0), sessions, evidence.references, evidence.model, rules
    )
    assert out["droop_kind"].iloc[0] == "modelled"
    assert "pressure_fault_in_window" in out["droop_basis"].iloc[0]


def test_one_drive_is_not_a_model(rules: DroopRules) -> None:
    sessions = {"a": _pressure_session("a", rules)}
    evidence = droop_evidence(sessions, rules)
    assert not evidence.model.ok
    assert evidence.model.refusal is not None
    assert evidence.model.refusal.startswith("model_refused")
    out = apply_droop(
        _samples("august", [5.0], [50.0], 1.0), sessions, evidence.references, evidence.model, rules
    )
    assert out["droop_kind"].iloc[0] == "none"
    assert out["droop_basis"].iloc[0].startswith("model_refused")


def test_a_drive_without_low_flow_has_no_reference_and_says_so(rules: DroopRules) -> None:
    session = _pressure_session("a", rules)
    busy = PressureSession(
        session="busy",
        pressure_times=session.pressure_times,
        pressure_kpa=session.pressure_kpa,
        fault_times=session.fault_times,
        maf_times=session.maf_times,
        maf_gs=session.maf_gs + 20.0,
        rpm_times=session.rpm_times,
        rpm=session.rpm,
    )
    ref = drive_reference(busy, rules)
    assert ref.psi is None and ref.reason is not None
    assert ref.reason.startswith("no_low_flow_reference")
    parked = PressureSession(**{**busy.__dict__, "session": "parked", "rpm": busy.rpm * 0})
    assert drive_reference(parked, rules).reason.startswith("no_low_flow_reference")  # type: ignore[union-attr]


def test_the_known_answer_says_which_kind_of_empty(rules: DroopRules) -> None:
    evidence = droop_evidence({}, rules)
    result = droop_known_answer({}, evidence, rules)
    assert result["passed"] is False
    assert result["reason"].startswith("unresolvable")
    for name in rules.known_answer.sessions:
        assert result["sessions"][name].startswith("never_captured")


def test_a_frame_session_reads_fault_rows_as_states(rules: DroopRules) -> None:
    rows = []
    for i in range(200):
        stamp = pd.Timestamp(T0 + i * 0.01, unit="s", tz="UTC")
        fault = 100 <= i < 110
        rows.append(
            {
                "timestamp": stamp,
                "source": "sensor",
                "channel": "FUEL_PRESSURE_FAULT" if fault else "FUEL_PRESSURE",
                "value_number": None if fault else 350.0,
                "value_text": "below_window" if fault else None,
                "unit": "kPa",
                "metadata_json": "{}",
            }
        )
    session = read_pressure_session(pd.DataFrame(rows), rules, name="frame")
    assert len(session.pressure_times) == 190
    assert len(session.fault_times) == 10


@pytest.mark.slow  # reads two whole 2026-10-04 sessions (~200k pressure rows)
@pytest.mark.skipif(
    not (CORPUS / "2026-10-04T194342").is_dir(),
    reason="2026-10-04T194342 (the first fuel-pressure drive) is not on this machine",
)
def test_the_first_pressure_drive_reproduces_the_recorded_droop(rules: DroopRules) -> None:
    """-1.9 psi at about 107 g/s (first-drive analysis, 8 pairs at 100-118
    g/s against the 0-5 g/s median), through this module's path."""
    sessions = pressure_sessions([CORPUS / name for name in rules.known_answer.sessions], rules)
    evidence = droop_evidence(sessions, rules)
    result = droop_known_answer(sessions, evidence, rules)
    assert result["passed"], result
    assert result["n"] >= rules.known_answer.min_pairs
    assert result["median_droop_psi"] == pytest.approx(-1.9, abs=rules.known_answer.tolerance_psi)
    assert 100 <= result["median_maf_gs"] <= 118
    # The half-drive before the reboot forms no reference of its own: it
    # says so instead of borrowing one.
    assert result["sessions"]["2026-10-04T190056"].startswith("no_low_flow_reference")
    assert result["flow_change_pct"] == pytest.approx(-1.9, abs=0.3)


def test_sessions_of_one_drive_share_one_pooled_reference(rules: DroopRules) -> None:
    """A Pi reboot splits a drive; the half with no low-flow read takes the
    drive's reference instead of none (fit.drive_unit, used here too)."""
    whole = _pressure_session("a", rules)
    late = whole.maf_times > T0 + 60
    busy = PressureSession(
        **{
            **whole.__dict__,
            "session": "a2",
            "maf_times": whole.maf_times[late],
            "maf_gs": whole.maf_gs[late],
            "rpm_times": whole.rpm_times[late],
            "rpm": whole.rpm[late],
        }
    )
    alone = droop_evidence({"a2": busy}, rules)
    assert alone.references["a2"].psi is None
    pooled = droop_evidence({"a1": whole, "a2": busy}, rules, {"a1": "A", "a2": "A"})
    assert pooled.references["a2"].psi == pooled.references["a1"].psi is not None
    assert pooled.references["a2"].session == "A"
