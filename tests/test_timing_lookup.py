"""B08.4 — timing with the actual lookup context, on synthetic images and reads.

The provider is B08.2's (``tests.test_operating_context``'s synthetic image,
whose trim A and trim B differ in every cell); the existing surface is a
2-D grid fitted by B04's own fitter (:func:`fit_grid_binned_mean`). Nothing
here fits a timing candidate.
"""

from __future__ import annotations

import functools
import hashlib
import math
from pathlib import Path
from typing import Any

import numpy as np
import pandas as pd
import pytest
import yaml

from src.analysis.ecu_application import (
    MODEL_FILE,
    ApplicationModel,
    load_application_model,
    program_sha256,
)
from src.analysis.ecu_state_models import trim_lookup
from src.analysis.operating_context import (
    ContextDefinition,
    ContextProvider,
    ResolvedContext,
    image_tables,
)
from src.analysis.surface_2d import fit_grid_binned_mean
from src.analysis.timing_lookup import (
    MIN_BRANCH_DRIVES,
    CoordinateSample,
    TimingLookupRefusalError,
    branch_evidence,
    coordinate_samples,
    identical_samples,
    require_branch_validation,
    require_final_timing_target,
    score_identical_samples,
    summarize,
    trim_cell_coverage,
)
from src.behavior.behavior_models import BehaviorModel2D
from src.signals.pairing import PairingPolicy
from tests import test_operating_context as operating_context_tests
from tests.rom_fixtures import PROFILE, xterra_map
from tests.test_operating_context import (
    _feed,
    _long,
    context_image,
    definition,
    resolve,
    trim_a_cell,
    trim_b_cell,
)

DEFAULT = "2026-10-01T120000"
TUNING = "_tuning-2026-10-01T130000"
T0 = 1_790_000_000.0  # 2026-09-21, inside no particular session's name
POLICY = PairingPolicy.within("250ms")


@functools.cache
def _application_model() -> tuple[ApplicationModel, str]:
    """``load_application_model(PROFILE)`` parsed once with libyaml, as
    ``tests.test_residual_chain`` does: the pure-Python parse of this file was
    most of the module's setup (two loads, ~1.2 s)."""
    text = (PROFILE / MODEL_FILE).read_bytes()
    loaded = yaml.load(text, Loader=getattr(yaml, "CSafeLoader", yaml.SafeLoader))  # noqa: S506
    return ApplicationModel.model_validate(loaded), hashlib.sha256(text).hexdigest()


def _program(image: bytes) -> tuple[ApplicationModel, str]:
    """``synthetic_program`` on the cached model."""
    model, sha = _application_model()
    identity = model.program.identity.model_copy(update={"sha256": program_sha256(image, model)})
    return model.model_copy(
        update={"program": model.program.model_copy(update={"identity": identity})}
    ), sha


@pytest.fixture(scope="module")
def image() -> bytes:
    with pytest.MonkeyPatch.context() as patch:
        patch.setattr(
            operating_context_tests, "load_application_model", lambda _profile: _application_model()
        )
        return context_image()


def test_the_cached_application_model_is_the_loaders() -> None:
    assert _application_model() == load_application_model(PROFILE)


@pytest.fixture(scope="module")
def provider(image: bytes) -> ContextProvider:
    model = _program(image)
    tables = image_tables(
        image,
        xterra_map(),
        definition(),
        application_model=model,
        expected_sha256=hashlib.sha256(image).hexdigest(),
    )
    return ContextProvider.from_profile(
        PROFILE, rom_map=xterra_map(), application_model=model, tables=tables
    )


@pytest.fixture(scope="module")
def resolved(image: bytes) -> ResolvedContext:
    return resolve(image, model=_program(image))


@pytest.fixture(scope="module")
def surface() -> BehaviorModel2D:
    """B04's rpm x load timing grid, fitted by B04's fitter: 20 deg below
    2,000 rpm, 30 deg at or above, whatever the load."""
    rpm = np.repeat([1000.0, 1500.0, 2500.0, 3000.0], 50)
    load = np.tile(np.linspace(10.0, 60.0, 50), 4)
    frame = pd.DataFrame(
        {
            "obd:RPM": rpm,
            "obd:ENGINE_LOAD": load,
            "obd:TIMING_ADVANCE": np.where(rpm < 2000.0, 20.0, 30.0),
            "session": "train",
        }
    )
    return fit_grid_binned_mean(
        frame,
        x_column="obd:RPM",
        y_column="obd:ENGINE_LOAD",
        output_column="obd:TIMING_ADVANCE",
        name="timing_advance_vs_rpm_load",
        output="obd:TIMING_ADVANCE",
        x_edges=[500.0, 2000.0, 4000.0],
        y_edges=[0.0, 100.0],
        x_axis_name="rpm",
        y_axis_name="engine_load",
        output_unit="degBTDC",
    )


class _Collect:
    def __init__(self) -> None:
        self.reads: list[tuple[float, str, float]] = []

    def observe(self, spec: str, value: float, t: float, *, basis: str) -> None:
        self.reads.append((t, spec, value))


# -- the provider's timing coordinate -------------------------------------------------------


def test_the_coordinate_is_observed_without_identity_coolant_or_latch(
    provider: ContextProvider,
) -> None:
    """The trim needs the image, the slot, coolant and the latch; the
    coordinate it searches is RAM and needs none of them."""
    stream = provider.stream(role="serve", captured=None)
    _feed(
        stream,
        100.0,
        rpm_word=200,
        x=72,
        high_det=None,
        skip=("mem:ECM_UPREV_MAGIC", "did:ECM_COOLANT_TEMP"),
    )
    record = stream.context_at(100.1)
    assert record["timing_trim"]["refused"] == "missing:map_magic"
    coordinate = record["timing_coordinate"]
    assert (coordinate["rpm_word"], coordinate["rpm"], coordinate["load_raw"]) == (200, 2500.0, 72)
    assert coordinate["high_det"] is None
    assert coordinate["high_det_refused"] == "missing:high_det_flags"


@pytest.mark.parametrize(("high_det", "expected"), [(True, True), (False, False)])
def test_the_coordinate_carries_the_latch_as_read(
    provider: ContextProvider, high_det: bool, expected: bool
) -> None:
    stream = provider.stream(role="serve", captured=None)
    _feed(stream, 100.0, high_det=high_det)
    coordinate = stream.context_at(100.1)["timing_coordinate"]
    assert coordinate["high_det"] is expected and coordinate["high_det_refused"] is None


def test_a_stale_or_skewed_coordinate_is_refused_by_name(provider: ContextProvider) -> None:
    reads = [
        (100.0, "mem:ECM_ENGINE_SPEED_RAM", 2500.0),
        (100.4, "mem:ECM_FUEL_TARGET_MAP_X_TRACE", 40 * 0.250859),
    ]
    skewed, stale = provider.evaluate_frame(_long(reads), [100.45, 100.7])
    assert skewed["timing_coordinate"]["refused"] == "skew:timing_coordinate"
    assert stale["timing_coordinate"]["refused"] == "stale:engine_speed_word"


def test_the_contract_names_the_final_timing_channels(provider: ContextProvider) -> None:
    timing = provider.contract("fit")["lookups"]["timing_trim"]
    assert timing["final_timing_channels"] == [
        "obd:TIMING_ADVANCE",
        "did:ECM_IGNITION_TIMING",
        "mem:ECM_IGNITION_TIMING_RAM",
    ]


def test_a_definition_without_final_timing_channels_is_refused() -> None:
    raw: dict[str, Any] = yaml.safe_load((PROFILE / "operating_context.yaml").read_text())
    del raw["timing_trim"]["final_timing_channels"]
    with pytest.raises(ValueError, match="final_timing_channels"):
        ContextDefinition.model_validate(raw)


# -- the target ---------------------------------------------------------------------------


@pytest.mark.parametrize("spec", ["mem:ECM_FUEL_TARGET_MAP_X_TRACE", "derived:TIMING_TRIM"])
def test_a_trim_or_coordinate_cannot_be_the_timing_target(
    provider: ContextProvider, surface: BehaviorModel2D, spec: str
) -> None:
    with pytest.raises(TimingLookupRefusalError) as caught:
        require_final_timing_target(provider, spec)
    assert caught.value.reason == f"not_final_timing:{spec}"
    with pytest.raises(TimingLookupRefusalError):
        identical_samples(
            provider,
            _long([(T0, spec, 1.0)]),
            surface.model_copy(update={"output": spec}),
            session=DEFAULT,
            policy=POLICY,
        )


# -- identical samples ------------------------------------------------------------------------


def _drive(t0: float) -> pd.DataFrame:
    """Five target reads, each a different case.

    - t0: every axis within 0.1 s, coordinate 0.05 s away: identical.
    - t0+10: rpm within 0.1 s, load 0.4 s away: unpaired load.
    - t0+20: axes paired, coordinate reads 0.3 s before: not fresh enough
      for the lookup's 0.25 s skew from the target read.
    - t0+30: axes paired, the last BFS-X read 10 s old: the coordinate's own
      refusal.
    - t0+30.1: a second target read whose only rpm partner was taken.
    """
    collect = _Collect()
    _feed(collect, t0 - 0.05, rpm_word=120, x=40, high_det=True)
    _feed(collect, t0 + 19.7, rpm_word=200, x=72, high_det=False)
    _feed(
        collect,
        t0 + 29.95,
        rpm_word=200,
        high_det=False,
        skip=("mem:ECM_FUEL_TARGET_MAP_X_TRACE",),
    )
    reads = collect.reads + [
        (t0, "obd:TIMING_ADVANCE", 21.0),
        (t0 + 0.1, "obd:RPM", 1500.0),
        (t0 - 0.1, "obd:ENGINE_LOAD", 30.0),
        (t0 + 10.0, "obd:TIMING_ADVANCE", 30.0),
        (t0 + 10.1, "obd:RPM", 2500.0),
        (t0 + 10.4, "obd:ENGINE_LOAD", 30.0),
        (t0 + 20.0, "obd:TIMING_ADVANCE", 33.0),
        (t0 + 20.05, "obd:RPM", 2500.0),
        (t0 + 20.05, "obd:ENGINE_LOAD", 30.0),
        (t0 + 30.0, "obd:TIMING_ADVANCE", 31.0),
        (t0 + 30.1, "obd:TIMING_ADVANCE", 31.0),
        (t0 + 30.05, "obd:RPM", 2500.0),
        (t0 + 30.05, "obd:ENGINE_LOAD", 30.0),
        (t0 + 30.12, "obd:ENGINE_LOAD", 30.0),
    ]
    return _long(reads)


def test_each_target_read_is_paired_once_and_every_unpaired_read_is_named(
    provider: ContextProvider, surface: BehaviorModel2D
) -> None:
    samples = identical_samples(provider, _drive(T0), surface, session=DEFAULT, policy=POLICY)
    assert [s.reason for s in samples] == [
        None,
        "unpaired:obd:ENGINE_LOAD",
        "skew:target_coordinate",
        "stale:bfs_x",
        "unpaired:obd:RPM",
    ]
    first = samples[0]
    assert first.axes == {"obd:RPM": 1500.0, "obd:ENGINE_LOAD": 30.0}
    assert first.axis_separation_s["obd:RPM"] == pytest.approx(0.1)
    assert (first.coordinate.rpm_word, first.coordinate.load_raw) == (120, 40)
    assert first.coordinate.high_det is True
    summary = summarize(samples, [])
    assert (summary.target_reads, summary.identical) == (5, 1)
    assert sum(summary.not_identical.values()) == 4


def test_an_axis_never_captured_is_named_not_captured(
    provider: ContextProvider, surface: BehaviorModel2D
) -> None:
    long = _drive(T0)
    long = long[long["spec"] != "obd:ENGINE_LOAD"]
    samples = identical_samples(provider, long, surface, session=DEFAULT, policy=POLICY)
    assert {s.reason for s in samples} >= {"not_captured:obd:ENGINE_LOAD"}
    assert all(s.reason is not None for s in samples)


def test_the_existing_surface_grades_identical_samples_and_refuses_a_tuning_capture(
    provider: ContextProvider, surface: BehaviorModel2D, tmp_path: Path
) -> None:
    """Graded through the B02 evaluator on the samples' own reads; the
    residual is the target read minus the cell, per sample. A tuning
    session's sample is refused by name (R03.12), never graded."""
    for name in (DEFAULT, TUNING):
        (tmp_path / name).mkdir()
    sessions = [tmp_path / DEFAULT, tmp_path / TUNING]
    default = identical_samples(provider, _drive(T0), surface, session=DEFAULT, policy=POLICY)
    tuning = identical_samples(provider, _drive(T0 + 600), surface, session=TUNING, policy=POLICY)
    scored = score_identical_samples(provider, surface, [*default, *tuning], sessions=sessions)
    assert len(scored) == 2
    graded, refused = scored
    assert graded.reason is None
    assert graded.expected == pytest.approx(20.0)
    assert graded.residual == pytest.approx(1.0)
    assert refused.residual is None
    assert str(refused.reason).startswith("profile_excluded")
    summary = summarize([*default, *tuning], scored)
    assert (summary.identical, summary.graded) == (2, 1)
    assert list(summary.not_graded) == [refused.reason]


# -- branches ----------------------------------------------------------------------------------


def _coordinate(session: str, t: float, high_det: bool | None) -> CoordinateSample:
    return CoordinateSample(
        session=session,
        t=t,
        refused=None,
        rpm_word=200,
        rpm=2500.0,
        load_raw=72,
        high_det=high_det,
        high_det_refused=None if high_det is not None else "not_captured:high_det_flags",
    )


DAY1 = 1_790_000_000.0
DAY2 = DAY1 + 86_400.0


@pytest.mark.parametrize(
    ("samples", "reason"),
    [
        ([_coordinate("2026-09-21T000000", DAY1, None)], "selection_unobserved"),
        # A latch read constant on one drive: the other branch was never seen.
        (
            [_coordinate("2026-09-21T000000", DAY1 + i, True) for i in range(500)],
            "branch_unobserved:clear",
        ),
        # Both states, one drive: neither rests on two independent drives.
        (
            [
                _coordinate("2026-09-21T000000", DAY1, True),
                _coordinate("2026-09-21T000000", DAY1 + 5, False),
            ],
            "branch_too_few_drives:set",
        ),
        # Both on two drives, but clear only on one.
        (
            [
                _coordinate("2026-09-21T000000", DAY1, True),
                _coordinate("2026-09-21T000000", DAY1 + 5, False),
                _coordinate("2026-09-22T000000", DAY2, True),
            ],
            "branch_too_few_drives:clear",
        ),
    ],
)
def test_a_branch_is_validated_only_where_it_was_observed_on_independent_drives(
    samples: list[CoordinateSample], reason: str
) -> None:
    evidence = branch_evidence(samples)
    with pytest.raises(TimingLookupRefusalError) as caught:
        require_branch_validation(evidence, min_drives=2)
    assert caught.value.reason == reason


def test_both_branches_on_two_drives_each_pass() -> None:
    samples = [
        _coordinate("2026-09-21T000000", DAY1, True),
        _coordinate("2026-09-21T000000", DAY1 + 5, False),
        _coordinate("2026-09-22T000000", DAY2, True),
        _coordinate("2026-09-22T000000", DAY2 + 5, False),
        CoordinateSample(session="2026-09-22T000000", t=DAY2 + 9, refused="stale:bfs_x"),
    ]
    evidence = branch_evidence(samples)
    require_branch_validation(evidence, min_drives=2)
    assert evidence.samples == {"set": 2, "clear": 2}
    assert evidence.unobserved == {"stale:bfs_x": 1}


# -- the coordinate on the image's trim tables --------------------------------------------------


def test_the_coordinate_lands_on_the_trim_cells_both_tables_read(
    provider: ContextProvider, resolved: ResolvedContext
) -> None:
    """On breakpoints (rpm word 128 = axis byte 32 x 4, row 4; X 40, column
    4) each table reads its own cell; the selected one equals the provider's
    resolved trim for the same reads with the latch set."""
    collect = _Collect()
    _feed(collect, 100.0, rpm_word=128, x=40, high_det=True)
    _feed(collect, 200.0, rpm_word=128, x=40, skip=("mem:ECM_FUEL_TARGET_MAP_X_TRACE",))
    long = _long(collect.reads)
    samples = coordinate_samples(provider, long, session=DEFAULT)
    assert [s.refused for s in samples] == [None, "stale:bfs_x"]
    cov = trim_cell_coverage(samples, resolved, assumptions=("synthetic",))
    assert cov.samples == 1 and cov.cells == {(4, 4): 1}
    assert cov.trim_a_intermediate == (trim_a_cell(4, 4) & 0x7F,)
    assert cov.trim_b_intermediate == (trim_b_cell(4, 4) & 0x7F,)
    assert cov.samples_reading_distinguishable == 1
    assert len(cov.distinguishable_cells) == 256
    trim = provider.evaluate_frame(long, [100.1])[0]["timing_trim"]
    assert (trim["table"], trim["trim"]) == ("timing_trim_b", cov.trim_b_intermediate[0])


def test_identical_tables_leave_no_branch_distinguishable(resolved: ResolvedContext) -> None:
    from dataclasses import replace

    same = replace(resolved, trim_b=replace(resolved.trim_b, cells=resolved.trim_a.cells))
    cov = trim_cell_coverage([_coordinate(DEFAULT, T0, True)], same)
    assert cov.distinguishable_cells == () and cov.samples_reading_distinguishable == 0
    expected = trim_lookup(
        same.trim_a.cells, same.trim_a.row_axis_raw, same.trim_a.column_axis_raw, 200, 72
    )
    assert cov.trim_a_intermediate == cov.trim_b_intermediate == (expected.trim,)


def test_a_clamped_coordinate_is_counted(resolved: ResolvedContext) -> None:
    sample = CoordinateSample(
        session=DEFAULT,
        t=T0,
        refused=None,
        rpm_word=4,
        rpm=50.0,
        load_raw=250,
        high_det=False,
    )
    cov = trim_cell_coverage([sample], resolved)
    assert cov.clamped == {"rows:below": 1, "columns:above": 1}
    assert not math.isnan(float(cov.trim_a_intermediate[0]))


# -- the committed real slice --------------------------------------------------------------------


def test_the_october_4_slice_resolves_no_coordinate_and_observes_no_latch(
    provider: ContextProvider,
) -> None:
    """The committed slice of _capture-ram-study-2026-10-04T204557 reads the
    rpm word and BFS-X in different memory windows, polled seconds apart, and
    never reads HI DET: no coordinate is fresh at an rpm-word read, so the
    selected table is unobserved, by name."""
    from src.analysis.channels import read_channels
    from tests.test_operating_context import _ram_study_frame

    long = read_channels(_ram_study_frame(), provider.channels(), acquired_time=True)
    samples = coordinate_samples(provider, long, session="_capture-ram-study-2026-10-04T204557")
    assert samples and not any(s.observed for s in samples)
    assert {str(s.refused).split(":")[0] for s in samples} <= {"stale", "skew", "missing"}
    assert "high_det_flags" in provider.coverage(long["spec"])["not_captured"]
    evidence = branch_evidence(samples)
    with pytest.raises(TimingLookupRefusalError) as caught:
        require_branch_validation(evidence, min_drives=2)
    assert caught.value.reason == "selection_unobserved"


# -- verification fixes ----------------------------------------------------------------------------


def _one_sample_reads(rpm_dt: float, load_dt: float) -> list[tuple[float, str, float]]:
    collect = _Collect()
    _feed(collect, T0 - 0.05, rpm_word=120, x=40, high_det=True)
    return [
        *collect.reads,
        (T0, "obd:TIMING_ADVANCE", 21.0),
        (T0 + rpm_dt, "obd:RPM", 1500.0),
        (T0 + load_dt, "obd:ENGINE_LOAD", 30.0),
    ]


def test_an_axis_pairing_looser_than_the_lookup_skew_is_refused(
    provider: ContextProvider, surface: BehaviorModel2D
) -> None:
    """rpm and load read 4 s from the timing read do not describe it: a
    5 s policy is refused, never used to call them identical."""
    long = _long(_one_sample_reads(4.0, -4.0))
    with pytest.raises(TimingLookupRefusalError) as caught:
        identical_samples(
            provider, long, surface, session=DEFAULT, policy=PairingPolicy.within("5s")
        )
    assert caught.value.reason == "pairing_exceeds_lookup_skew"
    # At the declared limit the same reads are unpaired, by name.
    samples = identical_samples(provider, long, surface, session=DEFAULT, policy=POLICY)
    assert [s.reason for s in samples] == ["unpaired:obd:RPM"]


def test_the_tool_refuses_a_pairing_looser_than_the_lookup_skew(tmp_path: Path) -> None:
    from tools.timing_lookup_coverage import main

    with pytest.raises(SystemExit, match="exceeds the timing lookup's skew limit"):
        main(["--session", "x", "--pairing-ms", "5000", "--out", str(tmp_path / "o.json")])


def test_an_axis_read_on_a_logical_clock_is_named_clock_unacquired(
    provider: ContextProvider, surface: BehaviorModel2D
) -> None:
    acquired = _long(_one_sample_reads(0.1, -0.1)[:-2])
    logical = _long([(T0, "obd:RPM", 1500.0), (T0, "obd:ENGINE_LOAD", 30.0)], basis="logical")
    samples = identical_samples(
        provider,
        pd.concat([acquired, logical], ignore_index=True),
        surface,
        session=DEFAULT,
        policy=POLICY,
    )
    assert [s.reason for s in samples] == ["clock_unacquired:obd:RPM"]


def test_scoring_refuses_a_model_that_does_not_grade_final_timing(
    provider: ContextProvider, surface: BehaviorModel2D, tmp_path: Path
) -> None:
    (tmp_path / DEFAULT).mkdir()
    samples = identical_samples(
        provider, _long(_one_sample_reads(0.1, -0.1)), surface, session=DEFAULT, policy=POLICY
    )
    assert [s.reason for s in samples] == [None]
    trim_model = surface.model_copy(update={"output": "derived:TIMING_TRIM"})
    with pytest.raises(TimingLookupRefusalError) as caught:
        score_identical_samples(provider, trim_model, samples, sessions=[tmp_path / DEFAULT])
    assert caught.value.reason == "not_final_timing:derived:TIMING_TRIM"
    # A declared final-timing model that is not the one the samples read.
    other = surface.model_copy(update={"output": "mem:ECM_IGNITION_TIMING_RAM"})
    with pytest.raises(TimingLookupRefusalError) as caught:
        score_identical_samples(provider, other, samples, sessions=[tmp_path / DEFAULT])
    assert caught.value.reason == "target_mismatch"


def test_one_drive_split_into_two_sessions_validates_no_branch() -> None:
    """Two recordings 60 s apart are one physical drive: both latch states
    on it rest on one drive, however the sessions are named."""
    samples = [
        _coordinate("2026-09-21T000000", DAY1, True),
        _coordinate("2026-09-21T000000", DAY1 + 5, False),
        _coordinate("2026-09-21T000100", DAY1 + 60, True),
        _coordinate("2026-09-21T000100", DAY1 + 65, False),
    ]
    evidence = branch_evidence(samples)
    assert len(evidence.drives["set"]) == 1
    with pytest.raises(TimingLookupRefusalError) as caught:
        require_branch_validation(evidence, min_drives=2)
    assert caught.value.reason == "branch_too_few_drives:set"


def test_min_drives_cannot_fall_below_two() -> None:
    samples = [_coordinate("2026-09-21T000000", DAY1, True)]
    with pytest.raises(ValueError, match="at least 2"):
        require_branch_validation(branch_evidence(samples), min_drives=1)
    assert MIN_BRANCH_DRIVES == 2
