"""Tests for the tuning gates (roadmap Phase 2).

Each test pins a gate to the retraction that motivated it. The point of this
module is that a gate cannot be forgotten by the next caller, so the tests
assert refusal and visibility, not just arithmetic.
"""

from __future__ import annotations

from collections.abc import Sequence
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any, cast

import numpy as np
import pandas as pd
import pytest
from pydantic import ValidationError

from src.analysis.lambda_delivery import load_lambda_delivery
from src.analysis.noise_floor import (
    FloorRefusal,
    NoiseFloor,
    Significance,
    record_null_run,
    significance,
)
from src.analysis.tuning import (
    DeliveryError,
    GateError,
    compare_sessions,
    delivery_error,
    semantic_route,
)
from src.analysis.tuning_frame import load_provider_registry
from src.signals.afr import AfrCalibration
from src.storage.parquet_writer import write_records_to_parquet
from src.telemetry import TelemetryRecord

BINDINGS = Path("data/bindings.yaml")
#: The dev vehicle's mode01_wot.rpm_bands (profile data since R05.13).
RPM_BANDS = (2500.0, 3000.0, 3500.0, 4000.0, 4500.0, 5000.0, 6500.0)
DIDS = Path("data/dids.yaml")
T0 = datetime(2026, 8, 5, 12, 0, 0, tzinfo=UTC)
PHI = AfrCalibration(afr_scale=14.7, commanded_equiv_ratio_convention="phi")
#: The dev vehicle's lambda-delivery declaration: which channel is the target,
#: what each measured source is, and one eligibility rule per regime. Delivery
#: error runs on it since R05.16, so each synthetic row here is one
#: acquisition-paired target read. Closed loop: 3 s dwell, 15 s post-cut
#: holdoff. Enrichment: 0.6 s dwell at 0.01 lambda, 6 s holdoff, and a frozen
#: 0.10 s transport delay (the lambda read 0.1 s after the target pairs).
DECLARATION = load_lambda_delivery(Path("data/nissan-xterra-2008/lambda_delivery.yaml"))
#: Rows at the head of a constant closed-loop target that cannot show 3 s of dwell.
DWELL_ROWS = 3
#: Rows at the head of a constant enrichment target that cannot show 0.6 s of
#: dwell at one read per second: only the first (nothing earlier, or an earlier
#: target more than 0.01 lambda away).
ENRICH_DWELL_ROWS = 1


def _mode01(session: Path | pd.DataFrame, **overrides: object) -> DeliveryError:
    kwargs: dict[str, object] = {
        "declaration": DECLARATION,
        "calibration": PHI,
        "bindings_path": BINDINGS,
        "dids_path": DIDS,
        "rpm_bands": RPM_BANDS,
        "min_load": 85.0,
    }
    kwargs.update(overrides)
    return delivery_error(session, **kwargs)  # type: ignore[arg-type]


def _rows(
    tmp_path: Path,
    samples: Sequence[tuple[float, float, float, float, float]],
    *,
    name: str = "2026-08-05T120000",
    start: datetime = T0,
) -> Path:
    """Build a session from (rpm, load, fuel_status, equiv_ratio, lambda) rows."""
    records: list[TelemetryRecord] = []
    for i, (rpm, load, fs, ratio, lam) in enumerate(samples):
        ts = start + timedelta(seconds=i)
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
    PID 0x44 is declared valid only for enrichment, so its stoich reads are
    ``source_not_valid_in_regime``; the enrichment block's first read is the
    tip-in step (``target_transient``).
    """
    session = _rows(
        tmp_path,
        [(3000, 90, 2, 1.0, 1.0)] * 20  # closed loop, stoich
        + [(3000, 90, 4, 1.2, 0.83)] * 8,  # open loop enrichment
    )
    result = _mode01(session)
    assert result.n == 8 - ENRICH_DWELL_ROWS
    filtered = result.gates["filtered"]
    assert filtered["source_not_valid_in_regime"] == 20 - DWELL_ROWS
    assert filtered["target_transient"] == DWELL_ROWS + ENRICH_DWELL_ROWS
    assert result.gates["started"] == 28


def test_an_enrichment_reading_in_closed_loop_is_removed_by_the_open_loop_gate(
    tmp_path: Path,
) -> None:
    """PID 0x44 is not the final target in closed loop: it reads sensor-like
    values off 1.0 there (declaration limitations). FUEL_STATUS still gates it."""
    session = _rows(tmp_path, [(3000, 90, 2, 1.1, 0.9)] * 6 + [(3000, 90, 4, 1.1, 0.9)] * 4)
    result = _mode01(session)
    assert result.n == 4
    assert result.gates["filtered"]["closed_loop"] == 6 - ENRICH_DWELL_ROWS


def test_a_lean_commanded_target_is_not_a_delivery_sample(tmp_path: Path) -> None:
    """`FUEL_STATUS == 4` is open loop, which covers DFCO as well as enrichment.

    A bare `fs == 4` filter over the July corpus is 88 % overrun. A commanded
    phi of 0.5 (λ 2.0) is outside the declared plausible target range, so it
    is a named ``target_invalid`` state rather than a lean "delivery error".
    """
    session = _rows(
        tmp_path,
        [(3000, 90, 4, 1.2, 0.83)] * 7  # enrichment: commanded ~12.25 AFR
        + [(3000, 90, 4, 0.5, 1.9)] * 6,  # commanded ~29 AFR, way lean
    )
    result = _mode01(session)
    assert result.n == 7 - ENRICH_DWELL_ROWS
    assert result.gates["filtered"]["target_invalid"] == 6


def test_delivery_error_reports_band_average_and_pooled_separately(tmp_path: Path) -> None:
    """Band-averaging is the defence against composition bias, so both show.

    Pooling across datasets with different rpm coverage compares coverage, not
    fuelling — that manufactured the "K responds 1:1" result. Reporting both
    makes a disagreement between them visible instead of a choice made silently.
    """
    session = _rows(
        tmp_path,
        # 9 eligible samples in one band, 2 in another: pooled is dominated by the first.
        [(2600, 90, 4, 1.2, 0.90)] * 10 + [(4600, 90, 4, 1.2, 0.80)] * 2,
    )
    result = _mode01(session)
    assert list(result.bands["n"]) == [10 - ENRICH_DWELL_ROWS, 2]
    assert result.pooled_pct != pytest.approx(result.band_averaged_pct)
    # Band-averaging weights the sparse high-rpm band equally, so it must sit
    # between the two band values rather than tracking the dense one.
    band_values = sorted(result.bands["error_pct"])
    assert band_values[0] < result.band_averaged_pct < band_values[1]
    # Each band carries its per-sample spread beside the mean.
    assert set(result.bands.columns) >= {"p05_pct", "p95_pct"}


def test_measured_source_and_pairing_are_carried_in_the_result(tmp_path: Path) -> None:
    """The lambda path and the voltage path disagree 3.8 %; neither is default truth."""
    session = _rows(tmp_path, [(3000, 90, 4, 1.2, 0.83)] * 5)
    result = _mode01(session)
    assert result.measured_spec == "obd:O2_WR_LAMBDA_S1"
    payload = result.as_dict()
    assert payload["engine_version"] == 2
    assert payload["pairing"]["method"] == "acquisition-time match_ordered"
    assert payload["pairing"]["target_source"] == "mode01_pid_0x44"
    assert payload["pairing"]["max_separation_s"] == 0.25
    # R05.16 verification: PID 0x44 shares a batch with the lambda read, so no
    # delay is modelled on this route, and the result says so.
    assert payload["pairing"]["transport_delay_modelled"] is False
    assert payload["pairing"]["transport_delay_s"] is None
    assert "not modelled on this route" in payload["pairing"]["transport_delay_note"]
    assert payload["delay_sensitivity"] is None


def test_a_target_and_measurement_further_apart_than_declared_do_not_pair(
    tmp_path: Path,
) -> None:
    """R05.16: the 1 s last-cell grid joined a target and a measurement up to a
    second apart. The acquisition-paired engine refuses anything past the
    declared 250 ms, and says the inputs never overlapped."""
    from src.analysis.tuning import InputsNotCapturedError

    records: list[TelemetryRecord] = []
    for i in range(8):
        ts = T0 + timedelta(seconds=i)
        late = ts + timedelta(milliseconds=600)
        records += [
            TelemetryRecord(timestamp=ts, source="obd", channel="RPM", value=3000),
            TelemetryRecord(timestamp=ts, source="obd", channel="ENGINE_LOAD", value=90),
            TelemetryRecord(timestamp=ts, source="obd", channel="FUEL_STATUS", value=4),
            TelemetryRecord(timestamp=ts, source="obd", channel="COMMANDED_EQUIV_RATIO", value=1.2),
            TelemetryRecord(timestamp=late, source="obd", channel="O2_WR_LAMBDA_S1", value=0.83),
        ]
    session = tmp_path / "2026-08-05T120000"
    write_records_to_parquet(records, session)
    with pytest.raises(InputsNotCapturedError) as caught:
        _mode01(session)
    assert caught.value.kind == "not_co_observed"


def _all_did_session(
    tmp_path: Path, voltages: list[str], *, baro_kpa: float | None = 101.0
) -> Path:
    """RAM target + sentinel, DID rpm and DID A/F voltage, one read per second.

    ``baro_kpa``: a PID 0x33 read at each second (B06.7: the voltage route
    needs it). 101 kPa is inside the ECM's skip window, gain 1; ``None``
    writes none."""
    records: list[TelemetryRecord] = []
    for i, volts in enumerate(voltages):
        ts = T0 + timedelta(seconds=i)
        if baro_kpa is not None:
            records.append(
                TelemetryRecord(
                    timestamp=ts - timedelta(milliseconds=10),
                    source="obd",
                    channel="BAROMETRIC_PRESSURE",
                    value=baro_kpa,
                )
            )
        records += [
            TelemetryRecord(timestamp=ts, source="obd", channel="DID_1219", value="0bb8"),
            TelemetryRecord(
                timestamp=ts, source="obd", channel="mem:ECM_TARGET_EQUIV_RATIO", value=1.17
            ),
            TelemetryRecord(timestamp=ts, source="obd", channel="DID_1225", value=volts),
        ]
    session = tmp_path / "_capture-tuning-did-2026-08-05T120000"
    write_records_to_parquet(records, session)
    return session


def test_the_semantic_route_converts_voltage_and_names_censored_samples(tmp_path: Path) -> None:
    """All-DID analysis pairs the declared RAM target with the declared A/F
    voltage source through the calibrated transfer, never the raw volts."""
    from src.signals.afr import load_afr_calibration

    calibration = load_afr_calibration(Path("data/nissan-xterra-2008/afr_calibration.yaml"))
    providers = load_provider_registry(Path("data/nissan-xterra-2008/tuning_providers.yaml"))
    # 0x0152 = 1.690 V -> 13.74 AFR; 0x0000 is below the transfer's censoring floor.
    session = _all_did_session(tmp_path, ["0152"] * 6 + ["0000"] + ["0152"])
    result = delivery_error(
        session,
        declaration=DECLARATION,
        route=semantic_route(DECLARATION, providers, measured_role="measured_afr_b1"),
        calibration=calibration,
        bindings_path=BINDINGS,
        dids_path=DIDS,
        rpm_bands=RPM_BANDS,
        min_load=None,
    )
    assert result.gates["filtered"]["measured_censored"] == 1
    assert result.n == 8 - ENRICH_DWELL_ROWS - 1
    assert result.measured_spec == "semantic:measured_afr_b1 (did:ECM_AF_SENSOR_V_B1)"
    # 13.75 AFR against a 14.7/1.17 = 12.56 AFR target: lean by ~9 %, not the
    # confident -87 % a raw 1.69 V against 12.6 AFR once produced.
    assert 5.0 < result.pooled_pct < 15.0
    # The RAM route models a frozen 0.10 s delay, so the result is re-run at
    # both ends of its bracket (0.0 / 0.5 s). At one read per second a 0.5 s
    # shift pairs nothing, so the answer does not survive the bracket and says so.
    assert result.pairing["transport_delay_modelled"] is True
    assert result.pairing["transport_delay_s"] == 0.1
    sensitivity = result.delay_sensitivity
    assert sensitivity is not None
    assert [row["delay_s"] for row in sensitivity["at"]] == [0.1, 0.0, 0.5]
    assert [row["n"] for row in sensitivity["at"]][:2] == [result.n, result.n]
    assert sensitivity["at"][2]["n"] == 0
    assert sensitivity["stable"] is False
    assert "exists at some delays" in sensitivity["reason"]


def test_the_voltage_route_applies_the_baro_gain_or_refuses_without_baro(
    tmp_path: Path,
) -> None:
    """B06.7: at 80 kPa the same 1.69 V reads richer than at sea level (the
    ECM's gain about stoich), and a session with no baro cannot answer
    through the voltage route at all -- by name, not at gain 1."""
    from src.analysis.tuning import InputsNotCapturedError
    from src.signals.afr import load_afr_calibration

    calibration = load_afr_calibration(Path("data/nissan-xterra-2008/afr_calibration.yaml"))
    providers = load_provider_registry(Path("data/nissan-xterra-2008/tuning_providers.yaml"))
    route = semantic_route(DECLARATION, providers, measured_role="measured_afr_b1")
    common: dict[str, Any] = {
        "declaration": DECLARATION,
        "route": route,
        "calibration": calibration,
        "bindings_path": BINDINGS,
        "dids_path": DIDS,
        "rpm_bands": RPM_BANDS,
        "min_load": None,
    }
    sea = delivery_error(_all_did_session(tmp_path / "sea", ["0152"] * 8), **common)
    alt = delivery_error(_all_did_session(tmp_path / "alt", ["0152"] * 8, baro_kpa=80.0), **common)
    assert sea.n == alt.n > 0
    # 1.69 V is x2 676; at 80 kPa (k 112.9) x2' = 880 - 204 x 1.134 = 648.7, 1.622 V:
    # 13.59 AFR on table 0xE644 against the gain-1 13.74, ~1 % less lean of 12.56.
    target = 14.7 / 1.17
    assert sea.pooled_pct == pytest.approx(100 * (13.74 / target - 1), abs=0.01)
    assert alt.pooled_pct == pytest.approx(100 * (13.5947 / target - 1), abs=0.01)
    with pytest.raises(InputsNotCapturedError, match="obd:BAROMETRIC_PRESSURE"):
        delivery_error(_all_did_session(tmp_path / "none", ["0152"] * 8, baro_kpa=None), **common)


def test_the_semantic_route_refuses_a_load_floor(tmp_path: Path) -> None:
    providers = load_provider_registry(Path("data/nissan-xterra-2008/tuning_providers.yaml"))
    session = _all_did_session(tmp_path, ["0152"] * 4)
    with pytest.raises(GateError, match="load floor"):
        delivery_error(
            session,
            declaration=DECLARATION,
            route=semantic_route(DECLARATION, providers, measured_role="measured_afr_b1"),
            calibration=PHI,
            rpm_bands=RPM_BANDS,
            min_load=85.0,
        )


def test_a_clock_that_cannot_carry_the_declared_separation_refuses() -> None:
    """A frame with no timestamp provenance may be a round stamp a second
    wide; pairing it at 250 ms would claim a simultaneity it cannot show."""
    from src.analysis.tuning import DeliveryInputs, gate_delivery_samples, mode01_route
    from src.signals.pairing import ComparisonRefusedError

    stamps = pd.to_datetime([T0 + timedelta(seconds=i) for i in range(5)], utc=True)

    def long(value: float) -> pd.DataFrame:
        return pd.DataFrame({"timestamp": stamps, "value": [value] * 5})  # no basis column

    inputs = DeliveryInputs(
        target=long(1.2),
        sentinel=None,
        measured=long(0.83),
        rpm=long(3000.0),
        fuel_status=long(4.0),
        load=None,
    )
    route = mode01_route(DECLARATION, measured_spec="obd:O2_WR_LAMBDA_S1", min_load=None)
    with pytest.raises(ComparisonRefusedError, match="cannot support a 250 ms"):
        gate_delivery_samples(
            inputs,
            route,
            declaration=DECLARATION,
            calibration=PHI,
            rpm_bands=RPM_BANDS,
            min_load=None,
        )


def test_delivery_error_refuses_without_a_declaration(tmp_path: Path) -> None:
    session = _rows(tmp_path, [(3000, 90, 4, 1.2, 0.83)] * 5)
    with pytest.raises(GateError, match="no lambda_delivery.yaml"):
        _mode01(session, declaration=None)


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


WOT = "WOT: throttle>=85, 3000-5000 rpm"
IDLE = "idle: 600-800 rpm, closed loop"


def test_floor_significance_calls_small_deltas_indistinguishable() -> None:
    """The phrasing that was missing from the retracted spark claims.

    Those quoted per-cell deltas of -1.57, -1.01 and -0.74 deg against a
    null-run sd of 0.64. Two of the three sit inside 2 sigma outright; the
    largest is 2.45 sigma, i.e. marginal rather than the confident finding it
    was published as — and the null run's observed per-cell *range* reached
    -1.03..+0.52, so even that one is close to what changing nothing produced.
    The gate's job is to surface that arithmetic, not to make it vanish.
    """
    floor = NoiseFloor(
        channels=cast(
            Any,
            {
                "timing_advance_deg": {
                    "unit": "deg",
                    "sd": 0.64,
                    "mean": 0.01,
                    "n_cells": 5,
                    "conditions": WOT,
                }
            },
        )
    )
    out = floor.significance(
        "timing_advance_deg", pd.Series([-1.57, -1.01, -0.74, 3.0]), conditions=WOT
    )
    assert isinstance(out, Significance)
    assert out.delta_sigma.iloc[0] == pytest.approx(-2.45, abs=0.01)
    assert list(out.distinguishable[1:3]) == [False, False]
    assert bool(out.distinguishable.iloc[3])


def test_an_idle_comparison_against_the_wot_floor_refuses() -> None:
    """R05.2: a WOT floor quoted against idle cells understates noise ~16x."""
    floor = NoiseFloor(
        channels=cast(
            Any,
            {"timing_advance_deg": {"unit": "deg", "sd": 0.48, "n_cells": 5, "conditions": WOT}},
        )
    )
    out = floor.significance("timing_advance_deg", pd.Series([1.0, 2.0]), conditions=IDLE)
    assert isinstance(out, FloorRefusal)
    assert out.kind == "conditions_mismatch"
    # The refusal names both regimes.
    assert WOT in out.reason and IDLE in out.reason
    assert out.as_dict()["floor_conditions"] == WOT
    assert out.as_dict()["requested_conditions"] == IDLE


def test_missing_floor_refuses_as_no_floor() -> None:
    out = significance(None, "timing_advance_deg", pd.Series([1.0, 2.0]), conditions=WOT)
    assert isinstance(out, FloorRefusal) and out.kind == "no_floor"
    empty = NoiseFloor(channels={})
    out = empty.significance("timing_advance_deg", pd.Series([1.0]), conditions=WOT)
    assert isinstance(out, FloorRefusal) and out.kind == "no_floor"


def test_a_floor_without_its_conditions_is_not_representable() -> None:
    with pytest.raises(ValidationError, match="conditions"):
        NoiseFloor(channels=cast(Any, {"spark": {"unit": "deg", "sd": 0.48, "n_cells": 5}}))


def test_an_idle_session_comparison_against_the_wot_floor_refuses(tmp_path: Path) -> None:
    floor = NoiseFloor(
        channels=cast(Any, {"spark": {"unit": "deg", "sd": 0.48, "n_cells": 5, "conditions": WOT}})
    )
    session = _rows(tmp_path, [(2000 + 100 * i, 50, 4, 1.2, 0.83) for i in range(30)])
    result = compare_sessions(
        session,
        session,
        value="obd:O2_WR_LAMBDA_S1",
        axes=("obd:RPM",),
        noise_floor=floor,
        floor_channel="spark",
        conditions=IDLE,
        bindings_path=BINDINGS,
        dids_path=DIDS,
    )
    assert result.cells["delta_sigma"].isna().all()
    assert result.floor_refusal is not None
    assert result.floor_refusal["kind"] == "conditions_mismatch"
    assert result.as_dict()["floor_refusal"]["floor_conditions"] == WOT


def test_a_floor_without_comparison_conditions_is_refused(tmp_path: Path) -> None:
    floor = NoiseFloor(
        channels=cast(Any, {"spark": {"unit": "deg", "sd": 0.48, "n_cells": 5, "conditions": WOT}})
    )
    session = _rows(tmp_path, [(2000 + 100 * i, 90, 4, 1.2, 0.83) for i in range(30)])
    with pytest.raises(GateError, match="without conditions"):
        compare_sessions(
            session,
            session,
            value="obd:RPM",
            axes=("obd:RPM",),
            noise_floor=floor,
            floor_channel="spark",
            bindings_path=BINDINGS,
            dids_path=DIDS,
        )


def test_record_null_run_needs_more_than_one_cell() -> None:
    with pytest.raises(ValueError, match="at least|>=2"):
        record_null_run(
            {"x": pd.Series([1.0])},
            sessions=("a", "b"),
            units={"x": "deg"},
            conditions="WOT",
        )


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
    from src.vehicle.tune_revisions import load_tune_revisions

    registry = load_tune_revisions(Path("data/nissan-xterra-2008/tune_revisions.yaml"))
    assert registry is not None
    expected = {
        "2026-07-19T202721": "ZS04C-stock",
        "2026-07-27T152444": "ZS44A-osiris-initial",
        # Capture sessions are `_`-prefixed; they must still resolve.
        "_tuning-2026-08-05T142720": "ZS44A-k106",
    }
    for session, revision in expected.items():
        found = registry.for_session(session)
        assert found is not None and found.revision == revision


def test_a_session_is_dated_by_its_first_sample_not_its_name(tmp_path: Path) -> None:
    """2026-08-03T163440 is named 16:34:40Z but its first stored sample is
    23:11:54Z (the clock stepped after the directory was made). The k106
    flash was at 18:45:21Z, so by its name it ran k105 and by its data k106 —
    and the data is right (tune_revisions.yaml; E01.6 known-answer case)."""
    from src.vehicle.tune_revisions import load_tune_revisions, resolve_session_revision

    registry = load_tune_revisions(Path("data/nissan-xterra-2008/tune_revisions.yaml"))
    assert registry is not None
    samples = [(2000.0, 50.0, 4.0, 1.2, 0.83)] * 3
    drive = _rows(
        tmp_path,
        samples,
        name="2026-08-03T163440",
        start=datetime(2026, 8, 3, 23, 11, 54, tzinfo=UTC),
    )
    found = registry.for_session(drive)
    assert found is not None and found.revision == "ZS44A-k106"
    assert resolve_session_revision(drive, registry) == ("ZS44A-k106", "registry", False)
    # The name alone (no telemetry to read) is all a bare name can be dated by.
    by_name = registry.for_session(drive.name)
    assert by_name is not None and by_name.revision == "ZS44A-k105"
    empty = tmp_path / "empty" / "2026-08-03T163440"
    empty.mkdir(parents=True)
    by_empty = registry.for_session(empty)
    assert by_empty is not None and by_empty.revision == "ZS44A-k105"


def test_sessions_before_the_first_flash_have_no_revision() -> None:
    """Better `None` than forcing a session into a calibration that did not exist."""
    from datetime import UTC, datetime

    from src.vehicle.tune_revisions import TuneRevision, TuneRevisionRegistry

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

    from src.vehicle.tune_revisions import TuneRevision, TuneRevisionRegistry

    with pytest.raises(ValidationError, match="oldest-first"):
        TuneRevisionRegistry(
            revisions=[
                TuneRevision(revision="b", flashed_at=datetime(2026, 8, 1, tzinfo=UTC)),
                TuneRevision(revision="a", flashed_at=datetime(2026, 7, 1, tzinfo=UTC)),
            ]
        )


def test_comparison_flags_whether_both_sides_share_a_revision(tmp_path: Path) -> None:
    """Same revision means the spread IS noise; different means a flash is in it."""
    from src.vehicle.tune_revisions import load_tune_revisions

    registry = load_tune_revisions(Path("data/nissan-xterra-2008/tune_revisions.yaml"))
    samples = [(2000 + 100 * i, 50, 4, 1.2, 0.83) for i in range(30)]
    # Data times agree with the names: a session is dated by its first sample.
    a = _rows(
        tmp_path / "a",
        samples,
        name="2026-08-02T120000",
        start=datetime(2026, 8, 2, 12, 0, 0, tzinfo=UTC),
    )
    b = _rows(
        tmp_path / "b",
        samples,
        name="2026-08-05T142720",
        start=datetime(2026, 8, 5, 14, 27, 20, tzinfo=UTC),
    )
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
        channels=cast(
            Any,
            {
                "spark": {
                    "unit": "deg",
                    "sd": 0.48,
                    "mean": 0.07,
                    "n_cells": 5,
                    "conditions": "WOT: throttle>=85, 3000-5000 rpm",
                }
            },
        )
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
        conditions="WOT: throttle>=85, 3000-5000 rpm",
        bindings_path=BINDINGS,
        dids_path=DIDS,
    )
    assert result.floor_refusal is None
    assert result.cells["delta_sigma"].notna().all()
    assert result.floor_conditions == "WOT: throttle>=85, 3000-5000 rpm"
    assert result.as_dict()["floor_conditions"] == "WOT: throttle>=85, 3000-5000 rpm"


def test_a_floor_object_without_a_named_channel_is_refused(tmp_path: Path) -> None:
    """Silently picking a channel would be picking a regime too."""
    from src.analysis.noise_floor import NoiseFloor

    floor = NoiseFloor(
        channels=cast(
            Any, {"spark": {"unit": "deg", "sd": 0.48, "n_cells": 5, "conditions": "WOT"}}
        )
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


def test_delivery_error_refuses_an_undeclared_measurement() -> None:
    """2026-09-21: ~1.5 V was compared against a 12.6 AFR target and the
    function returned a confident -87 % instead of refusing. A measurement is
    now used only as a source the vehicle's declaration names, with its
    conversion; anything else refuses, listing the declared sources."""
    with pytest.raises(GateError, match="is not a measured-mixture source") as caught:
        _mode01(pd.DataFrame(), measured_spec="obd:SHORT_FUEL_TRIM_1")
    assert "obd:O2_WR_LAMBDA_S1 (ecm_lambda_pid_0x24)" in str(caught.value)


def test_fuel_cut_samples_are_gated_not_fatal(tmp_path: Path) -> None:
    """W04.8: commanded phi 0.0 is decel fuel cut, and must be gated by name.

    Converting it raised ``equivalence ratio must be positive, got 0.0`` and
    cost four sessions on 2026-09-21. The cut reads are the named
    ``fuel_cut`` state — counted, so a session that is mostly overrun says so
    — and the enrichment rule's 6 s after a cut are ``post_fuel_cut``
    recovery; enrichment samples past it still answer.
    """
    session = _rows(
        tmp_path,
        [(3000, 20, 4, 0.0, 5.0)] * 3  # opens on decel fuel cut
        + [(3000, 90, 4, 1.2, 0.83)] * 20  # enrichment, rows 3..22
        + [(3000, 20, 4, 0.0, 5.0)] * 2,  # and cuts again
    )
    result = _mode01(session)
    # The last cut is at t=2 s: enrichment rows at t = 3..7 s are inside the 6 s
    # holdoff (the closed-loop rule's 15 s would have taken 14 of them).
    assert result.gates["filtered"]["fuel_cut"] == 5
    assert result.gates["filtered"]["post_fuel_cut"] == 5
    assert result.n == 20 - 5
    assert result.gates["started"] == 25


def test_an_all_fuel_cut_session_is_gates_removed_everything(tmp_path: Path) -> None:
    """Every sample in fuel cut is an answer about the drive, not missing inputs."""
    session = _rows(tmp_path, [(3000, 20, 4, 0.0, 5.0)] * 5)
    result = _mode01(session)
    assert result.n == 0
    assert result.gates == {"started": 5, "filtered": {"fuel_cut": 5}, "remaining": 0}


def test_commanded_afr_series_maps_fuel_cut_to_nan() -> None:
    """The vectorised conversion never raises on a zero ratio."""
    out = PHI.commanded_afr_series(pd.Series([0.0, 1.2, -1.0]))
    assert np.isnan(out.iloc[0]) and np.isnan(out.iloc[2])
    assert out.iloc[1] == pytest.approx(14.7 / 1.2)


def test_auto_edges_keep_the_minimum_sample(tmp_path: Path) -> None:
    """R05.10: ``linspace(lo, hi, 7)`` edges with right-closed ``pd.cut`` put
    the minimum on no bin's inside, so every sample at the lowest rpm was
    silently dropped from the comparison."""
    rows = [(1000, 50, 4, 1.2, 0.83)] * 5 + [(4000, 50, 4, 1.2, 0.83)] * 5
    a = _rows(tmp_path / "a", rows)
    b = _rows(tmp_path / "b", rows)
    result = compare_sessions(
        a,
        b,
        value="obd:O2_WR_LAMBDA_S1",
        axes=("obd:RPM",),
        bindings_path=BINDINGS,
        dids_path=DIDS,
    )
    assert result.n_cells == 2
    assert sorted(result.cells["n_a"].tolist()) == [5, 5]


def test_explicit_edges_still_exclude_out_of_range_samples(tmp_path: Path) -> None:
    rows = [(500, 50, 4, 1.2, 0.83)] * 5 + [(1500, 50, 4, 1.2, 0.83)] * 5
    a = _rows(tmp_path / "a", rows)
    b = _rows(tmp_path / "b", rows)
    result = compare_sessions(
        a,
        b,
        value="obd:O2_WR_LAMBDA_S1",
        axes=("obd:RPM",),
        bins=((1000.0, 2000.0),),
        bindings_path=BINDINGS,
        dids_path=DIDS,
    )
    assert result.n_cells == 1
    assert result.cells["n_a"].tolist() == [5]


def test_uncaptured_inputs_raise_a_structured_refusal(tmp_path: Path) -> None:
    """R05.11: callers read ``kind`` and ``missing``, never the message."""
    import json

    from src.analysis.tuning import InputsNotCapturedError

    records = [
        TelemetryRecord(
            timestamp=T0 + timedelta(seconds=i), source="obd", channel="RPM", value=3000
        )
        for i in range(5)
    ]
    session = tmp_path / "_capture-tuning-did-2026-08-05T120000"
    write_records_to_parquet(records, session)
    (session / "capture_profile.json").write_text(
        json.dumps(
            {
                "profile": "tuning-did",
                "started_at": T0.isoformat(),
                "profile_snapshot": {"mode01_enabled": False, "did_continuous": True},
            }
        )
    )
    with pytest.raises(InputsNotCapturedError) as caught:
        _mode01(session, mode01_exclusion_note="VEHICLE-NOTE")
    err = caught.value
    assert err.kind == "not_captured"
    assert "obd:COMMANDED_EQUIV_RATIO" in err.missing
    assert "obd:RPM" not in err.missing
    # The vehicle's own sentence comes from profile data; shared code carries
    # no ECM-specific claim.
    assert "VEHICLE-NOTE" in str(err)
    assert "this ECM" not in str(err)


def test_a_result_whose_sign_flips_across_the_delay_bracket_is_unstable() -> None:
    from src.analysis.tuning import DeliveryError, delay_sensitivity

    def result(n: int, value: float) -> DeliveryError:
        return DeliveryError(
            bands=pd.DataFrame(),
            band_averaged_pct=value,
            pooled_pct=value,
            n=n,
            gates={},
            rpm_window=(0.0, 0.0),
            measured_spec="x",
        )

    flips = delay_sensitivity(result(20, 0.7), {0.0: result(22, -0.3), 0.5: result(18, 1.1)}, 0.1)
    assert flips["stable"] is False
    assert "sign changes" in flips["reason"]
    assert flips["spread_pct"] == pytest.approx(1.4)
    holds = delay_sensitivity(result(20, 2.1), {0.0: result(22, 2.6), 0.5: result(18, 1.9)}, 0.1)
    assert holds["stable"] is True and holds["reason"] is None
