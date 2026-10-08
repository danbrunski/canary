"""B04.9 — timing graded per read, its axes paired on the acquisition clock.

The 1 Hz evaluation grid files each channel's last read of a second and
forward-fills it, so one row can put a timing read beside an rpm read seconds
away. These tests pin the per-read frame that replaces that join for a model
whose criteria declare ``axis_pairing``, and the refusals that keep a
grid-joined verdict from passing.
"""

from __future__ import annotations

import json
from pathlib import Path

import numpy as np
import pandas as pd
import pytest

import scripts.evaluate_behavior_models as eval_cli
from src.analysis.behavior_validation import (
    AXIS_PAIRING_COLUMN,
    AXIS_PAIRING_POLICY_COLUMN,
    AxisPairingRefusedError,
    EventPolicy,
    apply_criteria,
    axis_separation_column,
    axis_separation_summary,
    evaluate_rows,
    fold_metrics,
    load_validation_document,
    per_read_frame,
    read_model_reads,
    require_axis_pairing_covers,
)
from src.analysis.refit import default_recipes
from src.analysis.surface_2d import fit_grid_binned_mean
from src.behavior.behavior_models import (
    AxisPairing,
    BehaviorModel2D,
    BehaviorModels,
    NoiseFloor,
    SyntheticFault,
    ValidationCriteria,
)
from src.columns import MODE_COLUMN, SESSION_COLUMN, TIMESTAMP_COLUMN, gap_column

SESSION = "2026-10-01T120000"
T0 = 1_790_000_000.0
TIMING = "obd:TIMING_ADVANCE"
RPM = "obd:RPM"
LOAD = "obd:ENGINE_LOAD"
PAIRING = AxisPairing(max_separation_s={RPM: 0.25, LOAD: 0.25}, basis="test: measured elsewhere")
PROFILE_YAML = Path("data/nissan-xterra-2008/behavior_validation.yaml")


@pytest.fixture(scope="module")
def surface() -> BehaviorModel2D:
    """20 deg below 2,000 rpm, 30 deg at or above, whatever the load."""
    rpm = np.repeat([1000.0, 1500.0, 2500.0, 3000.0], 50)
    load = np.tile(np.linspace(10.0, 60.0, 50), 4)
    frame = pd.DataFrame(
        {
            RPM: rpm,
            LOAD: load,
            TIMING: np.where(rpm < 2000.0, 20.0, 30.0),
            "session": "train",
        }
    )
    return fit_grid_binned_mean(
        frame,
        x_column=RPM,
        y_column=LOAD,
        output_column=TIMING,
        name="timing_advance_vs_rpm_load",
        output=TIMING,
        x_edges=[500.0, 2000.0, 4000.0],
        y_edges=[0.0, 100.0],
        output_unit="degBTDC",
    )


def _reads(rows: list[tuple[float, str, float]], *, basis: str = "acquired") -> pd.DataFrame:
    return pd.DataFrame(
        {
            "timestamp": pd.to_datetime([T0 + t for t, _, _ in rows], unit="s", utc=True),
            "spec": [spec for _, spec, _ in rows],
            "value": [value for _, _, value in rows],
            "unit": "",
            "timestamp_basis": basis,
        }
    )


def _grid(seconds: list[int], rows: dict[str, list[float]]) -> pd.DataFrame:
    frame = pd.DataFrame(
        {
            TIMESTAMP_COLUMN: pd.to_datetime([T0 + s for s in seconds], unit="s", utc=True),
            SESSION_COLUMN: SESSION,
            MODE_COLUMN: "city",
            **rows,
        }
    )
    for channel in rows:
        frame[gap_column(channel)] = False
    return frame


def _known_answer() -> tuple[pd.DataFrame, pd.DataFrame]:
    """One Mode 01 response at t=0.1 s: 2,500 rpm, 30 deg (on the surface).
    The next rpm read, 0.8 s later, is 1,500 rpm; the grid files it as the
    second's rpm, so its row says 30 deg at 1,500 rpm: 10 deg off the
    surface, from reads 0.8 s apart."""
    reads = _reads(
        [
            (0.1, TIMING, 30.0),
            (0.1, RPM, 2500.0),
            (0.1, LOAD, 40.0),
            (0.9, RPM, 1500.0),
            (0.9, LOAD, 40.0),
        ]
    )
    grid = _grid([0], {TIMING: [30.0], RPM: [1500.0], LOAD: [40.0]})
    return reads, grid


def test_a_grid_row_mixes_instants_and_the_per_read_frame_does_not(
    surface: BehaviorModel2D,
) -> None:
    reads, grid = _known_answer()
    on_grid = evaluate_rows(surface, grid)
    assert on_grid.residual[0] == pytest.approx(10.0)
    assert on_grid.axis_pairing_s is None

    frame = per_read_frame(surface, grid, {SESSION: reads}, PAIRING, interval_seconds=1.0)
    assert len(frame) == 1
    assert frame[RPM].iloc[0] == 2500.0
    assert frame[axis_separation_column(RPM)].iloc[0] == 0.0
    assert frame[TIMESTAMP_COLUMN].iloc[0] == pd.Timestamp(T0 + 0.1, unit="s", tz="UTC")
    assert frame[MODE_COLUMN].iloc[0] == "city"  # context from the read's grid second
    per_read = evaluate_rows(surface, frame)
    assert per_read.evaluated.all()
    assert per_read.residual[0] == pytest.approx(0.0)
    assert per_read.axis_pairing_s == {RPM: 0.25, LOAD: 0.25}


def test_an_axis_read_beyond_its_bound_is_refused_by_name(surface: BehaviorModel2D) -> None:
    reads = _reads([(0.1, TIMING, 30.0), (0.5, RPM, 2500.0), (0.1, LOAD, 40.0)])
    grid = _grid([0], {TIMING: [30.0], RPM: [2500.0], LOAD: [40.0]})
    frame = per_read_frame(surface, grid, {SESSION: reads}, PAIRING, interval_seconds=1.0)
    assert frame[AXIS_PAIRING_COLUMN].tolist() == [f"unpaired:{RPM}"]
    assert np.isnan(frame[RPM].iloc[0])
    assert bool(frame[gap_column(RPM)].iloc[0])
    ev = evaluate_rows(surface, frame)
    assert ev.reasons.tolist() == [f"unpaired:{RPM}"]
    # Exactly at the bound pairs (inclusive, as PairingPolicy states).
    reads = _reads([(0.1, TIMING, 30.0), (0.35, RPM, 2500.0), (0.1, LOAD, 40.0)])
    frame = per_read_frame(surface, grid, {SESSION: reads}, PAIRING, interval_seconds=1.0)
    assert frame[AXIS_PAIRING_COLUMN].isna().all()
    assert frame[axis_separation_column(RPM)].iloc[0] == pytest.approx(0.25)


def test_each_axis_read_pairs_with_one_output_read(surface: BehaviorModel2D) -> None:
    """Two timing reads 0.1 s apart and one rpm read: the earlier timing read
    takes it, the later one is unpaired rather than reusing it."""
    reads = _reads(
        [(0.1, TIMING, 30.0), (0.2, TIMING, 30.0), (0.1, RPM, 2500.0), (0.1, LOAD, 40.0)]
        + [(0.2, LOAD, 40.0)]
    )
    grid = _grid([0], {TIMING: [30.0], RPM: [2500.0], LOAD: [40.0]})
    frame = per_read_frame(surface, grid, {SESSION: reads}, PAIRING, interval_seconds=1.0)
    reasons = frame[AXIS_PAIRING_COLUMN]
    assert pd.isna(reasons.iloc[0]) and reasons.iloc[1] == f"unpaired:{RPM}"


def test_every_read_is_counted_with_the_kind_of_refusal(surface: BehaviorModel2D) -> None:
    grid = _grid([0], {TIMING: [30.0], RPM: [2500.0], LOAD: [40.0]})
    # A read in a second the grid has no row for keeps its row, refused.
    reads = _reads([(5.1, TIMING, 30.0), (5.1, RPM, 2500.0), (5.1, LOAD, 40.0)])
    frame = per_read_frame(surface, grid, {SESSION: reads}, PAIRING, interval_seconds=1.0)
    assert frame[AXIS_PAIRING_COLUMN].tolist() == ["no_grid_context"]
    # An axis never read is not captured, not unpaired.
    reads = _reads([(0.1, TIMING, 30.0), (0.1, RPM, 2500.0)])
    frame = per_read_frame(surface, grid, {SESSION: reads}, PAIRING, interval_seconds=1.0)
    assert frame[AXIS_PAIRING_COLUMN].tolist() == [f"not_captured:{LOAD}"]
    # A round stamp cannot carry a 250 ms claim.
    rounds = _reads([(0.1, TIMING, 30.0), (0.1, RPM, 2500.0), (0.1, LOAD, 40.0)], basis="round")
    frame = per_read_frame(surface, grid, {SESSION: rounds}, PAIRING, interval_seconds=1.0)
    assert frame[AXIS_PAIRING_COLUMN].tolist() == [f"clock_unsupported:{TIMING}"]
    # A session with no grid rows contributes nothing; nothing at all is an
    # empty per-read frame, still marked as one.
    frame = per_read_frame(surface, grid, {"other": reads}, PAIRING, interval_seconds=1.0)
    assert frame.empty and AXIS_PAIRING_POLICY_COLUMN in frame.columns


def test_a_frame_paired_for_another_model_is_refused(surface: BehaviorModel2D) -> None:
    reads, grid = _known_answer()
    frame = per_read_frame(surface, grid, {SESSION: reads}, PAIRING, interval_seconds=1.0)
    other = frame.copy()
    other[AXIS_PAIRING_POLICY_COLUMN] = json.dumps(
        {"output": "obd:SHORT_FUEL_TRIM_1", "axes": {RPM: 0.25, LOAD: 0.25}}
    )
    with pytest.raises(AxisPairingRefusedError) as refused:
        evaluate_rows(surface, other)
    assert refused.value.reason == "axis_pairing_other_output"
    other[AXIS_PAIRING_POLICY_COLUMN] = json.dumps({"output": TIMING, "axes": {RPM: 0.25}})
    with pytest.raises(AxisPairingRefusedError) as refused:
        evaluate_rows(surface, other)
    assert refused.value.reason == "axis_pairing_other_axes"
    with pytest.raises(AxisPairingRefusedError) as refused:
        evaluate_rows(surface, frame.drop(columns=[AXIS_PAIRING_POLICY_COLUMN]))
    assert refused.value.reason == "axis_pairing_unstated"
    mixed = pd.concat([frame, frame.assign(**{AXIS_PAIRING_POLICY_COLUMN: "{}"})])
    with pytest.raises(AxisPairingRefusedError) as refused:
        evaluate_rows(surface, mixed)
    assert refused.value.reason == "axis_pairing_mixed"


def test_a_grid_row_concatenated_onto_a_per_read_frame_is_refused(
    surface: BehaviorModel2D,
) -> None:
    """The grid row carries no policy. Reading the policy past it would grade
    its 10 deg mis-join (residuals [0, 10]) under the per-read stamp."""
    reads, grid = _known_answer()
    frame = per_read_frame(surface, grid, {SESSION: reads}, PAIRING, interval_seconds=1.0)
    shifted = grid.assign(**{TIMESTAMP_COLUMN: grid[TIMESTAMP_COLUMN] + pd.Timedelta("500ms")})
    mixed = pd.concat([frame, shifted], ignore_index=True)
    with pytest.raises(AxisPairingRefusedError) as refused:
        evaluate_rows(surface, mixed)
    assert refused.value.reason == "axis_pairing_mixed"


def test_a_declaration_must_cover_every_axis(surface: BehaviorModel2D) -> None:
    with pytest.raises(AxisPairingRefusedError) as refused:
        require_axis_pairing_covers(surface, AxisPairing(max_separation_s={RPM: 0.25}, basis="x"))
    assert refused.value.reason == f"axis_pairing_incomplete:{LOAD}"
    extra = AxisPairing(max_separation_s={RPM: 0.25, LOAD: 0.25, "obd:MAF": 0.25}, basis="x")
    with pytest.raises(AxisPairingRefusedError) as refused:
        require_axis_pairing_covers(surface, extra)
    assert refused.value.reason == "axis_pairing_unknown_axis:obd:MAF"
    with pytest.raises(ValueError, match="positive"):
        AxisPairing(max_separation_s={RPM: 0.0}, basis="x")
    with pytest.raises(ValueError):
        AxisPairing(max_separation_s={RPM: 0.25}, basis="")


def test_a_derived_channel_has_no_read_to_pair(surface: BehaviorModel2D) -> None:
    derived = surface.model_copy(update={"output": "derived:VE_AMBIENT_PERCENT"})
    with pytest.raises(AxisPairingRefusedError) as refused:
        read_model_reads(derived, [])
    assert refused.value.reason == "axis_pairing_not_a_read:derived:VE_AMBIENT_PERCENT"


def _criteria(pairing: AxisPairing | None) -> ValidationCriteria:
    return ValidationCriteria(
        kind="predictive_surface",
        unit="deg",
        noise_floor=NoiseFloor(value=0.5, basis="LSB"),
        meaningful_deviation=5.0,
        meaningful_deviation_basis="test",
        min_eligible_observations=1,
        min_validation_drives=1,
        min_observations_per_drive=1,
        min_envelope_coverage=0.0,
        min_skill_vs_baseline=0.3,
        synthetic_faults=[SyntheticFault(magnitude=6.0, direction="negative", duration_seconds=60)],
        axis_pairing=pairing,
    )


def test_a_grid_joined_verdict_cannot_pass_criteria_that_declare_pairing(
    surface: BehaviorModel2D,
) -> None:
    reads, grid = _known_answer()
    policy = EventPolicy()
    on_grid = fold_metrics(surface, evaluate_rows(surface, grid), drive_of_piece={}, policy=policy)
    _, failures = apply_criteria(_criteria(PAIRING), on_grid, [], qualifying_test_drives=1)
    assert any(f.startswith("axis_pairing=grid") for f in failures)
    # Undeclared, the grid is judged as before.
    _, failures = apply_criteria(_criteria(None), on_grid, [], qualifying_test_drives=1)
    assert not any("axis_pairing" in f for f in failures)

    frame = per_read_frame(surface, grid, {SESSION: reads}, PAIRING, interval_seconds=1.0)
    ev = evaluate_rows(surface, frame)
    per_read = fold_metrics(surface, ev, drive_of_piece={}, policy=policy)
    assert per_read.axis_pairing_s == {RPM: 0.25, LOAD: 0.25}
    _, failures = apply_criteria(_criteria(PAIRING), per_read, [], qualifying_test_drives=1)
    assert not any("axis_pairing" in f for f in failures)
    tighter = AxisPairing(max_separation_s={RPM: 0.1, LOAD: 0.25}, basis="x")
    _, failures = apply_criteria(_criteria(tighter), per_read, [], qualifying_test_drives=1)
    assert f"axis_pairing:{RPM}=0.25s>0.1s" in failures
    summary = axis_separation_summary(frame, ev, surface)
    assert summary[RPM]["graded"] == 1 and summary[RPM]["max"] == 0.0


def test_the_report_refuses_a_pairing_model_without_its_reads(surface: BehaviorModel2D) -> None:
    document = load_validation_document(PROFILE_YAML)
    assert document is not None
    _, grid = _known_answer()
    with pytest.raises(AxisPairingRefusedError) as refused:
        eval_cli.evaluate(
            BehaviorModels(schema_version=2, models=[surface]),
            grid,
            mode="corrected",
            document=document,
            policy=EventPolicy(),
            taxonomy_problems=[],
            profile="nissan-xterra-2008",
        )
    assert refused.value.reason == "axis_pairing_no_reads"


def test_the_profile_pairs_every_timing_recipe_on_all_its_axes() -> None:
    """Every Xterra recipe grading TIMING_ADVANCE declares axis_pairing over
    exactly its axes: none of them can be graded on the grid's join."""
    document = load_validation_document(PROFILE_YAML)
    assert document is not None
    timing = [r for r in default_recipes("nissan-xterra-2008") if r.output == TIMING]
    assert timing
    for recipe in timing:
        criteria = document.recipes[recipe.name]
        assert criteria.axis_pairing is not None, recipe.name
        axes = {recipe.x_column, recipe.y_column}  # type: ignore[attr-defined]
        assert set(criteria.axis_pairing.max_separation_s) == axes, recipe.name
        assert criteria.axis_pairing.max_separation_s[RPM] <= 0.5
