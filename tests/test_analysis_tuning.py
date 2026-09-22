"""Tests for the tuning gates (roadmap Phase 2).

Each test pins a gate to the retraction that motivated it. The point of this
module is that a gate cannot be forgotten by the next caller, so the tests
assert refusal and visibility, not just arithmetic.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from pathlib import Path

import numpy as np
import pandas as pd
import pytest

from src.analysis.afr import AfrCalibration
from src.analysis.noise_floor import NoiseFloor, annotate, record_null_run
from src.analysis.tuning import (
    GateError,
    compare_sessions,
    delivery_error,
    delivery_error_from_semantic_frame,
    scale_stability,
)
from src.storage.parquet_writer import write_records_to_parquet
from src.telemetry import TelemetryRecord

BINDINGS = Path("data/bindings.yaml")
DIDS = Path("data/dids.yaml")
T0 = datetime(2026, 8, 5, 12, 0, 0, tzinfo=UTC)
PHI = AfrCalibration(commanded_equiv_ratio_convention="phi")


def _rows(
    tmp_path: Path,
    samples: list[tuple[float, float, float, float, float]],
    *,
    name: str = "2026-08-05T120000",
) -> Path:
    """Build a session from (rpm, load, fuel_status, equiv_ratio, lambda) rows."""
    records: list[TelemetryRecord] = []
    for i, (rpm, load, fs, ratio, lam) in enumerate(samples):
        ts = T0 + timedelta(seconds=i)
        records += [
            TelemetryRecord(timestamp=ts, source="obd", channel="RPM", value=rpm),
            TelemetryRecord(timestamp=ts, source="obd", channel="ENGINE_LOAD", value=load),
            TelemetryRecord(timestamp=ts, source="obd", channel="FUEL_STATUS", value=fs),
            TelemetryRecord(
                timestamp=ts, source="obd", channel="COMMANDED_EQUIV_RATIO", value=ratio
            ),
            TelemetryRecord(timestamp=ts, source="obd", channel="O2_WR_LAMBDA_S1", value=lam),
        ]
    out = tmp_path / name
    write_records_to_parquet(records, out)
    return out


def test_closed_loop_samples_are_excluded_and_the_count_is_reported(tmp_path: Path) -> None:
    """Closed loop dilutes any fuelling effect toward zero by construction.

    In closed loop the ECU corrects to target whatever K is, so pooling those
    samples is how a real +0.95 % K step read as "unchanged" — 92 % of the
    samples were at stoich. The gate must both drop them and *say* it did.
    """
    session = _rows(
        tmp_path,
        [(3000, 90, 2, 1.0, 1.0)] * 20  # closed loop, stoich
        + [(3000, 90, 4, 1.2, 0.83)] * 5,  # open loop enrichment
    )
    result = delivery_error(session, calibration=PHI, bindings_path=BINDINGS, dids_path=DIDS)
    assert result.n == 5
    assert result.gates["filtered"]["closed_loop"] == 20


def test_decel_fuel_cut_is_excluded_structurally(tmp_path: Path) -> None:
    """`FUEL_STATUS == 4` is open loop, which covers DFCO as well as enrichment.

    A bare `fs == 4` filter over the July corpus is 88 % overrun. The gate keys
    on the commanded mixture being richer than stoich, so overrun is excluded
    by construction rather than by a guessed lean threshold.
    """
    session = _rows(
        tmp_path,
        [(3000, 90, 4, 1.2, 0.83)] * 4  # enrichment: commanded ~12.25 AFR
        + [(3000, 90, 4, 0.5, 1.9)] * 6,  # DFCO: commanded ~29 AFR, way lean
    )
    result = delivery_error(session, calibration=PHI, bindings_path=BINDINGS, dids_path=DIDS)
    assert result.n == 4
    assert result.gates["filtered"]["not_enrichment"] == 6


def test_delivery_error_reports_band_average_and_pooled_separately(tmp_path: Path) -> None:
    """Band-averaging is the defence against composition bias, so both show.

    Pooling across datasets with different rpm coverage compares coverage, not
    fuelling — that manufactured the "K responds 1:1" result. Reporting both
    makes a disagreement between them visible instead of a choice made silently.
    """
    session = _rows(
        tmp_path,
        # 10 samples in one band, 2 in another: pooled is dominated by the first.
        [(2600, 90, 4, 1.2, 0.90)] * 10 + [(4600, 90, 4, 1.2, 0.80)] * 2,
    )
    result = delivery_error(session, calibration=PHI, bindings_path=BINDINGS, dids_path=DIDS)
    assert len(result.bands) == 2
    assert result.pooled_pct != pytest.approx(result.band_averaged_pct)
    # Band-averaging weights the sparse high-rpm band equally, so it must sit
    # between the two band values rather than tracking the dense one.
    band_values = sorted(result.bands["error_pct"])
    assert band_values[0] < result.band_averaged_pct < band_values[1]


def test_measured_source_is_carried_in_the_result(tmp_path: Path) -> None:
    """The lambda path and the voltage path disagree 3.8 %; neither is default truth."""
    session = _rows(tmp_path, [(3000, 90, 4, 1.2, 0.83)] * 4)
    result = delivery_error(session, calibration=PHI, bindings_path=BINDINGS, dids_path=DIDS)
    assert result.measured_spec == "obd:O2_WR_LAMBDA_S1"


def test_semantic_delivery_adapter_excludes_censored_samples_without_mode01() -> None:
    """All-DID analysis consumes transformed roles, never raw transport values."""
    frame = pd.DataFrame(
        {
            "timestamp": [T0 + timedelta(seconds=offset) for offset in range(4)],
            "rpm": [3000.0] * 4,
            "measured_afr": [12.4, 12.6, 18.2, 12.5],
            "commanded_afr": [12.5] * 4,
            "fueling_active": [True, True, True, False],
            "measured_afr_censored": [False, False, True, False],
        }
    )

    result = delivery_error_from_semantic_frame(
        frame,
        measured_spec="did:ECM_AF_SENSOR_V_B1 (voltage_afr)",
    )

    assert result.n == 2
    assert result.gates["filtered"] == {"fueling_inactive": 1, "censored_measured_afr": 1}
    assert result.measured_spec.endswith("(voltage_afr)")


def test_compare_sessions_refuses_load_as_an_axis(tmp_path: Path) -> None:
    """CAL/LD is derived from BFS, so it moves when fuelling moves.

    It is not stable at fixed fuel either: it shifted 1.43 points at identical
    airflow across a same-tune null run. Gridding on it is what produced the
    retracted "spark fell 0.7-1.6 deg" claim.
    """
    session = _rows(tmp_path, [(3000, 90, 4, 1.2, 0.83)] * 4)
    for axis in ("obd:ENGINE_LOAD", "ENGINE_LOAD", "obd:CALCULATED_LOAD"):
        with pytest.raises(GateError, match="derived from BFS"):
            compare_sessions(
                session,
                session,
                value="obd:RPM",
                axes=("obd:RPM", axis),
                bindings_path=BINDINGS,
                dids_path=DIDS,
            )


def test_unsafe_axes_is_an_explicit_escape_hatch(tmp_path: Path) -> None:
    """Refusal must be overridable — but only on purpose, never by default."""
    samples = [(2000 + 100 * i, 50 + i, 4, 1.2, 0.83) for i in range(30)]
    session = _rows(tmp_path, samples)
    result = compare_sessions(
        session,
        session,
        value="obd:O2_WR_LAMBDA_S1",
        axes=("obd:RPM", "obd:ENGINE_LOAD"),
        unsafe_axes=True,
        bindings_path=BINDINGS,
        dids_path=DIDS,
    )
    # Same session against itself: every matched cell delta is exactly zero.
    assert result.n_cells > 0
    assert result.cells["delta"].abs().max() == pytest.approx(0.0)


def test_comparison_without_a_floor_says_unavailable_rather_than_guessing(
    tmp_path: Path,
) -> None:
    samples = [(2000 + 100 * i, 50, 4, 1.2, 0.83) for i in range(30)]
    session = _rows(tmp_path, samples)
    result = compare_sessions(
        session,
        session,
        value="obd:O2_WR_LAMBDA_S1",
        axes=("obd:RPM",),
        bindings_path=BINDINGS,
        dids_path=DIDS,
    )
    assert result.cells["delta_sigma"].isna().all()


def test_floor_annotation_calls_small_deltas_indistinguishable() -> None:
    """The phrasing that was missing from the retracted spark claims.

    Those quoted per-cell deltas of -1.57, -1.01 and -0.74 deg against a
    null-run sd of 0.64. Two of the three sit inside 2 sigma outright; the
    largest is 2.45 sigma, i.e. marginal rather than the confident finding it
    was published as — and the null run's observed per-cell *range* reached
    -1.03..+0.52, so even that one is close to what changing nothing produced.
    The gate's job is to surface that arithmetic, not to make it vanish.
    """
    floor = NoiseFloor(
        channels={"timing_advance_deg": {"unit": "deg", "sd": 0.64, "mean": 0.01, "n_cells": 5}}
    )
    out = annotate(pd.Series([-1.57, -1.01, -0.74, 3.0]), floor, "timing_advance_deg")
    assert out["delta_sigma"].iloc[0] == pytest.approx(-2.45, abs=0.01)
    assert list(out["distinguishable"][1:3]) == [False, False]
    assert bool(out["distinguishable"].iloc[3])


def test_missing_floor_degrades_to_unavailable() -> None:
    out = annotate(pd.Series([1.0, 2.0]), None, "timing_advance_deg")
    assert out.attrs["floor"] == "unavailable"
    assert out["delta_sigma"].isna().all()


def test_record_null_run_needs_more_than_one_cell() -> None:
    with pytest.raises(ValueError, match="at least|>=2"):
        record_null_run(
            {"x": pd.Series([1.0])},
            sessions=("a", "b"),
            units={"x": "deg"},
            conditions="WOT",
        )


def test_scale_stability_reproduces_the_0x23d_verdicts() -> None:
    """Regression against the measured binding outcomes (commit c341570).

    Pedal position held its scale to +0.24 % across independent windows and
    was promoted to `confirmed`; the best airflow candidate correlated at
    r = 0.98 but drifted -6.5 % and was deliberately left unpinned. Correlation
    could not tell those apart — scale stability could.
    """
    rng = np.random.default_rng(0)
    x_a = rng.uniform(0, 255, 200)

    # Pedal: same map in both windows, small noise.
    stable = scale_stability(
        x_a,
        0.01485 * x_a + 0.795 + rng.normal(0, 0.002, 200),
        x_b := rng.uniform(0, 255, 200),
        0.01485 * x_b + 0.795 + rng.normal(0, 0.002, 200),
    )
    assert stable.verdict == "stable"
    assert abs(stable.drift_pct) < 1.5
    assert stable.oos_r2 > 0.99

    # Airflow: strongly correlated, but the scale moves 6.5 % between windows.
    unstable = scale_stability(
        x_a,
        6.88 * x_a + 5.57 + rng.normal(0, 1.0, 200),
        x_b,
        6.88 * 0.935 * x_b + 6.92 + rng.normal(0, 1.0, 200),
    )
    assert unstable.verdict == "unstable"
    assert unstable.drift_pct < -3.0


def test_scale_stability_refuses_degenerate_windows() -> None:
    with pytest.raises(GateError, match="non-zero variance"):
        scale_stability([1.0] * 5, [2.0] * 5, [1.0, 2.0, 3.0], [1.0, 2.0, 3.0])


def test_the_shipped_noise_floor_is_wot_scoped() -> None:
    """Regression guard on the seeded artifact.

    Measured across all conditions instead of WOT, spark spread is 7.7 deg
    rather than sub-degree, because idle timing swings 17-45 deg. A floor is
    only valid for the conditions it was measured under, and this one must
    stay the WOT-scoped measurement the comparisons need.
    """
    from src.analysis.noise_floor import load_noise_floor

    floor = load_noise_floor(Path("data/nissan-xterra-2008/noise_floor.yaml"))
    assert floor is not None
    spark = floor.sd_for("timing_advance_deg")
    delivery = floor.sd_for("delivery_error_pct")
    assert spark is not None and 0.2 < spark < 1.5
    assert delivery is not None and 0.5 < delivery < 3.0
    assert "WOT" in floor.provenance["note"]


def test_mcp_delivery_error_returns_ok_false_on_a_bad_session(tmp_path: Path) -> None:
    """Local tools report validation errors rather than raising (query_telemetry convention)."""
    from src.mcp_server.tools.tuning_analysis import get_delivery_error

    result = get_delivery_error(Path("canary.example.yaml"), session="no-such-session")
    assert result["ok"] is False
    assert "no-such-session" in result["error"]


def test_mcp_compare_surfaces_the_axis_gate_as_an_error(tmp_path: Path) -> None:
    """A refused gate must reach the agent as a message, not a stack trace."""
    from src.mcp_server.tools.tuning_analysis import compare_tuning_sessions

    session = _rows(tmp_path, [(3000, 90, 4, 1.2, 0.83)] * 4)
    result = compare_tuning_sessions(
        Path("canary.example.yaml"),
        session=str(session),
        against=str(session),
        value="obd:RPM",
        axes=("obd:RPM", "obd:ENGINE_LOAD"),
    )
    assert result["ok"] is False
    # Either the session lookup or the axis gate refuses; both must be messages.
    assert "error" in result


def test_tune_revision_resolution_matches_the_campaign_timeline() -> None:
    """ "Delivery error by tune revision" should be a groupby, not archaeology.

    The 2026-07-26 boundary is pinned by capture metadata rather than
    recollection: stock through 16:21 MDT, Osiris from the 16:31 session.
    """
    from src.analysis.tune_revisions import load_tune_revisions

    registry = load_tune_revisions(Path("data/nissan-xterra-2008/tune_revisions.yaml"))
    assert registry is not None
    expected = {
        "2026-07-19T202721": "ZS04C-stock",
        "2026-07-27T152444": "ZS44A-osiris-initial",
        "2026-08-03T163440": "ZS44A-k105",
        # Capture sessions are `_`-prefixed; they must still resolve.
        "_tuning-2026-08-05T142720": "ZS44A-k106",
    }
    for session, revision in expected.items():
        found = registry.for_session(session)
        assert found is not None and found.revision == revision


def test_sessions_before_the_first_flash_have_no_revision() -> None:
    """Better `None` than forcing a session into a calibration that did not exist."""
    from datetime import UTC, datetime

    from src.analysis.tune_revisions import TuneRevision, TuneRevisionRegistry

    registry = TuneRevisionRegistry(
        revisions=[
            TuneRevision(revision="a", flashed_at=datetime(2026, 7, 1, tzinfo=UTC)),
        ]
    )
    assert registry.at(datetime(2026, 6, 1, tzinfo=UTC)) is None
    assert registry.at(datetime(2026, 7, 2, tzinfo=UTC)) is not None


def test_registry_rejects_out_of_order_revisions() -> None:
    """Resolution walks the list in order; unordered input would resolve wrongly."""
    from datetime import UTC, datetime

    from pydantic import ValidationError

    from src.analysis.tune_revisions import TuneRevision, TuneRevisionRegistry

    with pytest.raises(ValidationError, match="oldest-first"):
        TuneRevisionRegistry(
            revisions=[
                TuneRevision(revision="b", flashed_at=datetime(2026, 8, 1, tzinfo=UTC)),
                TuneRevision(revision="a", flashed_at=datetime(2026, 7, 1, tzinfo=UTC)),
            ]
        )


def test_comparison_flags_whether_both_sides_share_a_revision(tmp_path: Path) -> None:
    """Same revision means the spread IS noise; different means a flash is in it."""
    from src.analysis.tune_revisions import load_tune_revisions

    registry = load_tune_revisions(Path("data/nissan-xterra-2008/tune_revisions.yaml"))
    samples = [(2000 + 100 * i, 50, 4, 1.2, 0.83) for i in range(30)]
    a = _rows(tmp_path / "a", samples, name="2026-08-03T163440")
    b = _rows(tmp_path / "b", samples, name="2026-08-05T142720")
    result = compare_sessions(
        a,
        b,
        value="obd:O2_WR_LAMBDA_S1",
        axes=("obd:RPM",),
        revisions=registry,
        bindings_path=BINDINGS,
        dids_path=DIDS,
    )
    payload = result.as_dict()
    assert payload["tune_revisions"] == ["ZS44A-k105", "ZS44A-k106"]
    assert payload["same_revision"] is False


def test_floor_conditions_travel_with_the_sigma_figures(tmp_path: Path) -> None:
    """A floor is only valid for the regime it was measured in.

    The same null pair gives 0.48 deg of spark spread at WOT and 7.7 deg across
    all conditions, so quoting one against the other understates noise ~16x and
    turns ordinary idle jitter into a "finding". Passing the floor object keeps
    the regime attached to the numbers it gates.
    """
    from src.analysis.noise_floor import NoiseFloor

    floor = NoiseFloor(
        channels={
            "spark": {
                "unit": "deg",
                "sd": 0.48,
                "mean": 0.07,
                "n_cells": 5,
                "conditions": "WOT: throttle>=85, 3000-5000 rpm",
            }
        }
    )
    samples = [(2000 + 100 * i, 50, 4, 1.2, 0.83) for i in range(30)]
    session = _rows(tmp_path, samples)
    result = compare_sessions(
        session,
        session,
        value="obd:O2_WR_LAMBDA_S1",
        axes=("obd:RPM",),
        noise_floor=floor,
        floor_channel="spark",
        bindings_path=BINDINGS,
        dids_path=DIDS,
    )
    assert result.floor_conditions == "WOT: throttle>=85, 3000-5000 rpm"
    assert result.as_dict()["floor_conditions"] == "WOT: throttle>=85, 3000-5000 rpm"


def test_a_floor_object_without_a_named_channel_is_refused(tmp_path: Path) -> None:
    """Silently picking a channel would be picking a regime too."""
    from src.analysis.noise_floor import NoiseFloor

    floor = NoiseFloor(
        channels={"spark": {"unit": "deg", "sd": 0.48, "n_cells": 5, "conditions": "WOT"}}
    )
    session = _rows(tmp_path, [(2000 + 100 * i, 90, 4, 1.2, 0.83) for i in range(30)])
    with pytest.raises(GateError, match="floor_channel"):
        compare_sessions(
            session,
            session,
            value="obd:RPM",
            axes=("obd:RPM",),
            noise_floor=floor,
            bindings_path=BINDINGS,
            dids_path=DIDS,
        )


def test_the_shipped_floor_records_its_regime() -> None:
    from src.analysis.noise_floor import load_noise_floor

    floor = load_noise_floor(Path("data/nissan-xterra-2008/noise_floor.yaml"))
    assert floor is not None
    for entry in floor.channels.values():
        assert "WOT" in entry.conditions
        assert entry.conditions != "unspecified"


def test_delivery_error_refuses_a_voltage_channel_as_the_measurement() -> None:
    """2026-09-21: ~1.5 V was compared against a 12.6 AFR target and the
    function returned a confident -87 % instead of refusing."""
    from pathlib import Path

    import pandas as pd

    from src.analysis.afr import AfrCalibration
    from src.analysis.tuning import GateError, delivery_error

    with pytest.raises(GateError, match="sensor voltage"):
        delivery_error(
            pd.DataFrame(),
            calibration=AfrCalibration(commanded_equiv_ratio_convention="phi"),
            measured_spec="did:ECM_AF_SENSOR_V_B1",
            measured_is_lambda=False,
            dids_path=Path("data/dids.yaml"),
        )


def test_fuel_cut_samples_are_gated_not_fatal(tmp_path: Path) -> None:
    """W04.8: commanded phi 0.0 is decel fuel cut, and must be gated by name.

    Converting it raised ``equivalence ratio must be positive, got 0.0`` and
    cost four sessions on 2026-09-21. The cut cells are now removed as the
    ``fuel_cut`` gate — counted, so a session that is mostly overrun says so —
    and the enrichment samples beside them still answer.
    """
    session = _rows(
        tmp_path,
        [(3000, 20, 4, 0.0, 5.0)] * 3  # opens on decel fuel cut
        + [(3000, 90, 4, 1.2, 0.83)] * 4  # enrichment
        + [(3000, 20, 4, 0.0, 5.0)] * 2,  # and cuts again
    )
    result = delivery_error(session, calibration=PHI, bindings_path=BINDINGS, dids_path=DIDS)
    assert result.n == 4
    assert result.gates["filtered"]["fuel_cut"] == 5
    assert result.gates["started"] == 9


def test_an_all_fuel_cut_session_is_gates_removed_everything(tmp_path: Path) -> None:
    """Every sample in fuel cut is an answer about the drive, not missing inputs."""
    session = _rows(tmp_path, [(3000, 20, 4, 0.0, 5.0)] * 5)
    result = delivery_error(session, calibration=PHI, bindings_path=BINDINGS, dids_path=DIDS)
    assert result.n == 0
    assert result.gates == {"started": 5, "filtered": {"fuel_cut": 5}, "remaining": 0}


def test_commanded_afr_series_maps_fuel_cut_to_nan() -> None:
    """The vectorised conversion never raises on a zero ratio."""
    out = PHI.commanded_afr_series(pd.Series([0.0, 1.2, -1.0]), afr_stoich=14.7)
    assert np.isnan(out.iloc[0]) and np.isnan(out.iloc[2])
    assert out.iloc[1] == pytest.approx(14.7 / 1.2)
