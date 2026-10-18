"""E08.3: the Monte Carlo engine, paired draws over declared uncertainty.

The synthetic images and kit are E07.6's (``tests.test_paired_scenarios``:
the 20260920 air-charge calibration, flat Fuel Comp 128, the real frozen
plant re-pinned to the image). The real plant is not qualified, so every
result carries that reason; nothing here is a prediction.
"""

from __future__ import annotations

import dataclasses
import inspect
import json
import subprocess
import sys
import textwrap
from pathlib import Path
from typing import Any

import numpy as np
import pytest
import yaml

from src.analysis import sim_montecarlo
from src.analysis.ecu_application import program_sha256
from src.analysis.paired_scenarios import (
    BATTERY_UNMEASURED,
    OUTPUTS,
    OVERRIDES_NOT_FROZEN,
    OperatingPoint,
    PairedScenario,
    SimKit,
    load_kit,
    plant_parameters,
    rank_as_improvement,
    run_paired,
)
from src.analysis.rom_effects import CellEdit, make_candidate
from src.analysis.rom_image import sha256_hex
from src.analysis.sim_contract import CONTRACT_FILE, SimContract, SimulationRefusalError
from src.analysis.sim_montecarlo import (
    FILL_BYTE,
    LUMPED_SCALE,
    STRUCTURAL_ERROR_NOT_SAMPLED,
    UNCERTAINTY_FILE,
    DesignPoint,
    LoadedUncertainty,
    MonteCarloDesign,
    MonteCarloResult,
    UncertaintyDeclaration,
    bracketed_parameters,
    check_uncertainty,
    kit_for_plant,
    load_uncertainty,
    run_monte_carlo,
)
from src.session import is_synthetic_session
from tests.rom_fixtures import PROFILE
from tests.test_paired_scenarios import (
    CRUISE,
    LEARNED,
    SPARK_TRACED,
    WOT,
    _edit_all,
    _identity,
    _image,
    _minted,
    _plant_with,
)

DESIGN_SHA = "d" * 64
SOURCE = f"design:{DESIGN_SHA}"


def build_kit_and_image() -> tuple[SimKit, bytes]:
    kit = load_kit(PROFILE)
    image = _image(kit.model, kit.plant)
    identity = kit.model.program.identity.model_copy(
        update={"sha256": program_sha256(image, kit.model)}
    )
    model = kit.model.model_copy(
        update={"program": kit.model.program.model_copy(update={"identity": identity})}
    )
    return dataclasses.replace(kit, model=model), image


@pytest.fixture(scope="module")
def kit_and_image() -> tuple[SimKit, bytes]:
    return build_kit_and_image()


@pytest.fixture(scope="module")
def uncertainty() -> LoadedUncertainty:
    return load_uncertainty(PROFILE / UNCERTAINTY_FILE)


def _point(base: OperatingPoint, **over: Any) -> OperatingPoint:
    return dataclasses.replace(base, conditioned={}, source=SOURCE, **over)


#: Open loop (enrichment) at WOT, and closed-loop cruise, from E07.6's grid.
P_WOT = _point(WOT)
P_CRUISE = _point(CRUISE)
#: Open loop on a short pulse: low airflow at high rpm, still an enrichment
#: target on the synthetic image; about 1 ms of metered open time, just
#: above the plant's 1.0 ms floor, so supported.
P_SHORT = _point(WOT, maf_gps=12.0, map_kpa=25.0, throttle_pct=8.0)
#: Below it: the pulse sits at the chain's minimum G on both ROMs, and is
#: unsupported as short_pulse_undecoded (computed all the same).
P_MIN_G = _point(WOT, maf_gps=10.5, map_kpa=25.0, throttle_pct=8.0)
#: Cold: the plant's outputs are unsupported, the ECU side is not.
P_COLD = _point(WOT, coolant_c=20.0)
#: The design's other route: MAP from the plant's own manifold model, no rail
#: reading (the working value applies), no module voltage read.
P_MODEL = _point(
    WOT, map_kpa_source="plant_model", rail_kpa_source="working_assumption", map_kpa=0.0
)
P_CRUISE_MODEL = _point(
    CRUISE, map_kpa_source="plant_model", rail_kpa_source="working_assumption", map_kpa=0.0
)
#: Part load, open loop, MAP from the model: below ambient at every drawn VE
#: (at WOT fuel-mv-1's speed-density MAP is capped at baro across the whole
#: VE bracket, so a VE draw cannot move it there).
P_PART_MODEL = _point(
    WOT,
    maf_gps=20.0,
    throttle_pct=8.0,
    map_kpa_source="plant_model",
    rail_kpa_source="working_assumption",
    map_kpa=0.0,
)
P_NO_VB = _point(WOT, battery_v_source="unmeasured")


def _design(image: bytes, *specs: tuple[OperatingPoint, int, int]) -> MonteCarloDesign:
    return MonteCarloDesign(
        sha256=DESIGN_SHA,
        base_image_sha256=sha256_hex(image),
        points=tuple(DesignPoint(p, lo, hi, 1.0 + k) for k, (p, lo, hi) in enumerate(specs)),
    )


def _ramped(image: bytes, kit: SimKit) -> bytes:
    """Fuel Comp rising along its load (column) axis, 96 + 4 per column."""
    table = kit.rom_map.table("fuel_compensation")
    edits = [
        CellEdit("fuel_compensation", r, c, 96 + 4 * c, slot=1)
        for r in range(table.rows)
        for c in range(table.cols)
        if 96 + 4 * c != 128
    ]
    return make_candidate(image, kit.rom_map, edits, base_label="synthetic").image


def _template(kit: SimKit, base: bytes, candidate: bytes, **over: Any) -> PairedScenario:
    from src.analysis.sim_contract import PlantIdentity

    fields: dict[str, Any] = dict(
        mode="conditional_rom_comparison",
        base=_identity(base),
        candidate=_identity(candidate),
        plant=PlantIdentity(
            version=kit.plant.plant_version,
            hardware_configuration=kit.plant.hardware_configuration,
        ),
        points=(),
        learned=LEARNED,
    )
    fields.update(over)
    return PairedScenario(**fields)


def _run(
    kit: SimKit,
    image: bytes,
    candidate: bytes,
    design: MonteCarloDesign,
    uncertainty: LoadedUncertainty,
    **kw: Any,
) -> MonteCarloResult:
    kw.setdefault("draws", 12)
    kw.setdefault("seed", 20261015)
    return run_monte_carlo(
        kit, _template(kit, image, candidate), design, image, candidate, uncertainty, **kw
    )


def _with_declaration(loaded: LoadedUncertainty, edit: Any) -> UncertaintyDeclaration:
    raw = loaded.declaration.model_dump(mode="json")
    edit(raw)
    return UncertaintyDeclaration.model_validate(raw)


def _refusal(fn: Any, *args: Any, **kw: Any) -> str:
    with pytest.raises(SimulationRefusalError) as err:
        fn(*args, **kw)
    return str(err.value.reason)


# -- the declaration ------------------------------------------------------------------------


def test_the_xterra_declaration_describes_the_contract_plant_exactly(
    kit_and_image: tuple[SimKit, bytes], uncertainty: LoadedUncertainty
) -> None:
    kit, _ = kit_and_image
    decl = uncertainty.declaration
    check_uncertainty(decl, kit)
    assert decl.plant_version == kit.plant.plant_version == "fuel-mv-1"
    spread = bracketed_parameters(kit)
    assert set(decl.parameters) == set(spread)
    # The lumped scale's spread is the frozen baseline's measured interval.
    assert spread[LUMPED_SCALE] == ("baseline_interval", (0.050591, 0.051501))
    assert decl.parameters[LUMPED_SCALE].distribution == "truncated_normal"
    # Every bracket-only parameter starts uniform, with that said.
    for pid, entry in decl.parameters.items():
        if pid != LUMPED_SCALE:
            assert entry.distribution == "uniform"
            assert entry.evidence.startswith("Bracket only: no measurement says where inside")
    assert decl.inputs.fill_byte.distribution == "uniform_integer"


def test_an_undeclared_mismatched_or_unknown_parameter_is_refused_by_name(
    kit_and_image: tuple[SimKit, bytes], uncertainty: LoadedUncertainty
) -> None:
    kit, _ = kit_and_image

    def refused(edit: Any) -> str:
        return _refusal(check_uncertainty, _with_declaration(uncertainty, edit), kit)

    assert refused(lambda r: r["parameters"].pop("bank_air_split")) == "distribution_undeclared"
    assert refused(lambda r: r["parameters"][LUMPED_SCALE].update(bracket=[0.050, 0.052])) == (
        "bracket_mismatch"
    )
    assert refused(
        lambda r: r["parameters"]["rail_pressure"].update(bracket_from="baseline_interval")
    ) == ("bracket_mismatch")
    assert refused(
        lambda r: r["parameters"].update(fuel_density=r["parameters"]["rail_pressure"])
    ) == ("parameter_unknown")
    # A known parameter has no spread: a distribution would invent one.
    assert refused(
        lambda r: r["parameters"].update(reference_delta_p=r["parameters"]["rail_pressure"])
    ) == ("parameter_not_bracketed")
    assert refused(lambda r: r.update(plant_version="fuel-mv-3")) == "uncertainty_plant_mismatch"
    # The schema is strict, and a form needs exactly its own shape fields.
    raw = uncertainty.declaration.model_dump(mode="json")
    with pytest.raises(ValueError, match="extra"):
        UncertaintyDeclaration.model_validate({**raw, "default_distribution": "uniform"})
    raw["parameters"]["rail_pressure"]["distribution"] = "triangular"
    with pytest.raises(ValueError, match="triangular takes"):
        UncertaintyDeclaration.model_validate(raw)


def test_the_paired_hook_refuses_unknown_and_outside_overrides(
    kit_and_image: tuple[SimKit, bytes],
) -> None:
    kit, image = kit_and_image
    scenario = dataclasses.replace(
        _template(kit, image, image),
        points=(dataclasses.replace(WOT, source="design: hook test"),),
    )
    plain = run_paired(kit, scenario, image, bytes(image))
    # No overrides is the existing call, byte for byte.
    assert run_paired(kit, scenario, image, bytes(image), plant_overrides={}).to_dict() == (
        plain.to_dict()
    )
    assert "plant_overrides" not in plain.provenance

    def reason(overrides: dict[str, float]) -> str:
        return _refusal(run_paired, kit, scenario, image, bytes(image), plant_overrides=overrides)

    assert reason({"fuel_density": 0.74}) == "override_unknown"
    assert reason({"injector_dead_time": 0.2}) == "override_outside_bracket"
    assert reason({LUMPED_SCALE: 0.0600}) == "override_outside_bracket"
    # The lumped scale moves inside its measured interval: lambda as 1/s.
    s0 = kit.plant.baseline.values[LUMPED_SCALE] if kit.plant.baseline else 0.0
    moved = run_paired(kit, scenario, image, bytes(image), plant_overrides={LUMPED_SCALE: 0.0507})
    lam0 = plain.points[0]["outputs"]["delivered_lambda"]["base"]
    lam1 = moved.points[0]["outputs"]["delivered_lambda"]["base"]
    assert lam1 == pytest.approx(lam0 * s0 / 0.0507, rel=1e-12)
    assert moved.provenance["plant_overrides"] == {LUMPED_SCALE: 0.0507}
    assert plant_parameters(kit).values == plant_parameters(kit, {}).values


# -- the engine ------------------------------------------------------------------------------


def test_the_negative_control_is_exactly_zero_at_every_point_and_draw(
    kit_and_image: tuple[SimKit, bytes], uncertainty: LoadedUncertainty
) -> None:
    kit, image = kit_and_image
    design = _design(image, (P_WOT, 180, 220), (P_CRUISE, 40, 80), (P_SHORT, 100, 140))
    for seed in (1, 2):
        result = _run(kit, image, bytes(image), design, uncertainty, seed=seed, draws=20)
        assert result.outcome == "identical"
        for name in OUTPUTS:
            delta = result.delta(name)
            assert np.isfinite(delta).all(), name
            assert (delta == 0.0).all(), name
    one = _run(kit, image, bytes(image), design, uncertainty, seed=1, draws=20)
    two = _run(kit, image, bytes(image), design, uncertainty, seed=2, draws=20)
    # A different seed draws different numbers, and the control still holds.
    assert not np.array_equal(one.fill_bytes, two.fill_bytes)
    assert not np.array_equal(one.parameters[LUMPED_SCALE], two.parameters[LUMPED_SCALE])


def test_the_same_seed_gives_a_byte_identical_result(
    kit_and_image: tuple[SimKit, bytes], uncertainty: LoadedUncertainty
) -> None:
    kit, image = kit_and_image
    candidate = _edit_all(image, kit, "fuel_compensation", 1, 3)
    design = _design(image, (P_WOT, 180, 220), (P_CRUISE, 40, 80))
    a = _run(kit, image, candidate, design, uncertainty)
    b = _run(kit, image, candidate, design, uncertainty)
    assert a.digest() == b.digest()
    assert a.traces().to_json() == b.traces().to_json()
    for name in OUTPUTS:
        assert a.base[name].tobytes() == b.base[name].tobytes()
        assert a.candidate[name].tobytes() == b.candidate[name].tobytes()
    c = _run(kit, image, candidate, design, uncertainty, seed=7)
    assert c.digest() != a.digest()


def test_the_lumped_scale_cancels_in_relative_delta_lambda_and_dead_time_does_not(
    kit_and_image: tuple[SimKit, bytes], uncertainty: LoadedUncertainty
) -> None:
    """lambda ~ 1/s on both ROMs, so a draw of s alone leaves the relative
    change exactly where it was; a dead-time offset changes the two ROMs'
    metered open times by different fractions, so it does not cancel. Both
    are asserted, so the cancellation claim can fail."""
    kit, image = kit_and_image
    candidate = _edit_all(image, kit, "fuel_compensation", 1, 3)
    # Fill bytes known exactly: only the named parameter varies.
    design = _design(image, (P_WOT, 200, 200), (P_SHORT, 120, 120))
    scale = _run(kit, image, candidate, design, uncertainty, draws=40, vary=[LUMPED_SCALE])
    assert scale.provenance["varied"] == [LUMPED_SCALE]
    assert "parameter_held:injector_dead_time" in scale.caveats
    assert (scale.loop == "open_loop").all()
    rel = scale.relative_delta("delivered_lambda")
    lam = scale.base["delivered_lambda"]
    for i in range(scale.n_points):
        assert np.ptp(lam[i]) > 1e-4  # the draws do move lambda...
        assert np.ptp(rel[i]) <= 1e-12  # ...and not the paired change
    assert (rel < 0).all()
    dead = _run(kit, image, candidate, design, uncertainty, draws=40, vary=["injector_dead_time"])
    rel_dead = dead.relative_delta("delivered_lambda")
    # The shorter the pulse, the larger the share an offset takes from it.
    short, long_ = np.ptp(rel_dead[1]), np.ptp(rel_dead[0])
    assert short > 1e-3
    assert short > 10 * long_ > 0.0


def test_cached_and_vectorised_equals_the_plain_run_paired_loop_exactly(
    kit_and_image: tuple[SimKit, bytes], uncertainty: LoadedUncertainty
) -> None:
    """Every value, delta, relative lambda delta and reason at every point and
    draw, for edits that exercise each branch: open and closed loop, a moved
    closed-loop setpoint, a gated fill-byte edit, cold and short pulses."""
    kit, image = kit_and_image
    candidates = {
        "fuel_comp": _edit_all(image, kit, "fuel_compensation", 1, 3),
        "target_afr": _edit_all(image, kit, "fuel_targets", 1, 4),
        "fill_curve": _edit_all(image, kit, "fill_curve_low", None, 2),
        "k": make_candidate(
            image, kit.rom_map, [CellEdit("k_constant", 0, 0, 30150, slot=1)], base_label="s"
        ).image,
    }
    design = _design(
        image,
        (P_WOT, 190, 200),
        (P_CRUISE, 50, 70),
        (P_SHORT, 110, 113),
        (P_MIN_G, 110, 111),
        (P_COLD, 200, 201),
        (P_MODEL, 190, 195),
        (P_CRUISE_MODEL, 50, 55),
        (P_NO_VB, 199, 200),
    )
    for label, candidate in candidates.items():
        result = _run(kit, image, candidate, design, uncertainty, draws=5, seed=3)
        template = _template(kit, image, candidate)
        support: dict[tuple[int, int, str], tuple[str, ...]] = {}
        for d in range(result.draws):
            points = tuple(
                dataclasses.replace(
                    dp.point, conditioned={FILL_BYTE: float(result.fill_bytes[i, d])}
                )
                for i, dp in enumerate(design.points)
            )
            overrides = {pid: float(v[d]) for pid, v in result.parameters.items()}
            plain = run_paired(
                kit,
                dataclasses.replace(template, points=points),
                image,
                candidate,
                plant_overrides=overrides,
            )
            for i, p in enumerate(plain.points):
                for name in OUTPUTS:
                    o = p["outputs"][name]
                    for side, arr in (("base", result.base), ("candidate", result.candidate)):
                        got = arr[name][i, d]
                        want = o[side]
                        assert (want is None and np.isnan(got)) or got == want, (
                            label,
                            name,
                            side,
                            i,
                            d,
                        )
                    want_delta = o["delta"]
                    got_delta = result.delta(name)[i, d]
                    assert (want_delta is None and np.isnan(got_delta)) or got_delta == want_delta
                    assert result.reasons(name, i, d) == tuple(o["reasons"]), (label, name)
                    assert result.supported(name)[i, d] == (o["status"] == "supported")
                    # Support at a fixed fill byte does not move with the plant draw.
                    key = (i, int(result.fill_bytes[i, d]), name)
                    assert support.setdefault(key, tuple(o["reasons"])) == tuple(o["reasons"])
                lam = p["outputs"]["delivered_lambda"]
                if "relative_delta" in lam:
                    assert result.relative_delta("delivered_lambda")[i, d] == lam["relative_delta"]
            assert all(a in result.caveats for a in plain.assumptions), label
        assert len(result.provenance["self_checked_pairs"]) >= 2
    # The gated edit gives no candidate value at all, as run_paired does.
    gated = _run(kit, image, candidates["fill_curve"], design, uncertainty, draws=3)
    for name in OUTPUTS:
        assert np.isnan(gated.candidate[name]).all()
        assert not gated.supported(name).any()


def test_support_does_not_vary_by_plant_draw(
    kit_and_image: tuple[SimKit, bytes], uncertainty: LoadedUncertainty
) -> None:
    kit, image = kit_and_image
    candidate = _edit_all(image, kit, "fuel_compensation", 1, 3)
    design = _design(image, (P_WOT, 200, 200), (P_CRUISE, 60, 60), (P_SHORT, 120, 120))
    result = _run(kit, image, candidate, design, uncertainty, draws=30)
    for i in range(result.n_points):
        for name in OUTPUTS:
            entries = result.point_support(i, name)
            assert len(entries) == 1 and entries[0]["draws"] == 30, (i, name)
    wot = result.point_support(0, "delivered_lambda")[0]
    assert wot["status"] == "supported"
    cruise = result.point_support(1, "delivered_lambda")[0]
    assert cruise["reasons"] == ["closed_loop_trim_authority_undecoded"]
    assert result.coverage["delivered_lambda"] == pytest.approx(2 / 3)


def test_the_fill_byte_is_drawn_over_its_whole_interval(
    kit_and_image: tuple[SimKit, bytes], uncertainty: LoadedUncertainty
) -> None:
    kit, image = kit_and_image
    design = _design(image, (P_WOT, 190, 194))
    result = _run(kit, image, _ramped(image, kit), design, uncertainty, draws=500)
    values, counts = np.unique(result.fill_bytes[0], return_counts=True)
    assert values.tolist() == [190, 191, 192, 193, 194]
    assert counts.min() > 70  # 100 expected each
    # On a Fuel Comp that varies along its load axis, BFS moves with the
    # drawn byte: the interval is carried, not a midpoint.
    assert len(np.unique(result.candidate["base_fuel_schedule"][0])) > 1
    assert len(np.unique(result.base["base_fuel_schedule"][0])) == 1  # flat 128
    # Parameters stay inside their declared brackets.
    for pid, values_drawn in result.parameters.items():
        lo, hi = uncertainty.declaration.parameters[pid].bracket
        assert (values_drawn >= lo).all() and (values_drawn <= hi).all(), pid


def test_provenance_caveats_and_synthetic_traces(
    kit_and_image: tuple[SimKit, bytes], uncertainty: LoadedUncertainty
) -> None:
    kit, image = kit_and_image
    candidate = _edit_all(image, kit, "fuel_compensation", 1, 3)
    design = _design(image, (P_WOT, 195, 200), (P_CRUISE, 55, 60))
    result = _run(kit, image, candidate, design, uncertainty, draws=4, seed=11)
    prov = result.provenance
    assert prov["base_image_sha256"] == sha256_hex(image)
    assert prov["candidate_image_sha256"] == sha256_hex(candidate)
    assert prov["design_sha256"] == DESIGN_SHA
    assert prov["uncertainty_sha256"] == uncertainty.sha256
    assert prov["plant_version"] == "fuel-mv-1"
    assert prov["baseline_sha256"] == result.plant["baseline_sha256"] != ""
    assert prov["contract_sha256"] == kit.contract_sha256
    assert (prov["seed"], prov["draws"], prov["tool_version"]) == (11, 4, "3")
    assert result.caveats[0] == STRUCTURAL_ERROR_NOT_SAMPLED
    assert result.caveats[1].startswith("plant_not_qualified")
    assert any(a.startswith("LTFT acts in open loop") for a in result.caveats)
    assert any(a.startswith("closed loop:") for a in result.caveats)
    # MAP and rail are stated on every point; the delay is not read at a
    # steady point: their draws move nothing, and the result says so.
    assert result.inert_parameters == [
        "lambda_observation_delay",
        "manifold_pressure",
        "rail_pressure",
    ]
    assert "parameter_inert:manifold_pressure" in result.caveats
    traces = result.traces()
    assert traces.attrs["synthetic"] is True
    assert set(traces["session"]) == {result.session}
    assert is_synthetic_session(result.session)
    assert len(traces) == 2 * 4 * len(OUTPUTS)
    assert result.weights.tolist() == [1.0, 2.0]


def test_draws_and_seed_are_required_never_defaulted() -> None:
    params = inspect.signature(run_monte_carlo).parameters
    for name in ("draws", "seed"):
        assert params[name].default is inspect.Parameter.empty
        assert params[name].kind is inspect.Parameter.KEYWORD_ONLY


def test_the_design_and_template_are_refused_by_name(
    kit_and_image: tuple[SimKit, bytes], uncertainty: LoadedUncertainty
) -> None:
    kit, image = kit_and_image

    def reason(design: MonteCarloDesign, **kw: Any) -> str:
        return _refusal(_run, kit, image, bytes(image), design, uncertainty, **kw)

    good = _design(image, (P_WOT, 190, 200))
    recorded = dataclasses.replace(P_WOT, source="median of _capture-tuning-2026-10-05T120000")
    assert reason(_design(image, (recorded, 190, 200))) == "source_not_design"
    stated = dataclasses.replace(P_WOT, conditioned={FILL_BYTE: 195.0})
    assert reason(_design(image, (stated, 190, 200))) == "conditioned_stated"
    assert reason(_design(image, (P_WOT, 200, 190))) == "fill_byte_interval_invalid"
    assert reason(_design(image, (P_WOT, 250, 256))) == "fill_byte_interval_invalid"
    assert reason(dataclasses.replace(good, base_image_sha256="0" * 64)) == (
        "design_revision_mismatch"
    )
    zero = dataclasses.replace(good, points=(dataclasses.replace(good.points[0], weight=0.0),))
    assert reason(zero) == "weight_invalid"
    assert reason(good, draws=0) == "draws_invalid"
    assert reason(good, vary=["fuel_density"]) == "parameter_unknown"
    with_points = dataclasses.replace(_template(kit, image, image), points=(WOT,))
    assert (
        _refusal(
            run_monte_carlo, kit, with_points, good, image, image, uncertainty, draws=2, seed=1
        )
        == "template_has_points"
    )


def test_a_named_plant_version_must_match_its_declaration_and_the_contract(
    kit_and_image: tuple[SimKit, bytes], uncertainty: LoadedUncertainty
) -> None:
    kit, image = kit_and_image
    design = _design(image, (P_WOT, 195, 200))
    same = kit_for_plant(kit, PROFILE, "fuel-mv-1")
    a = _run(same, image, bytes(image), design, uncertainty, draws=3)
    b = _run(kit, image, bytes(image), design, uncertainty, draws=3)
    assert a.digest() == b.digest()
    mv3 = kit_for_plant(kit, PROFILE, "fuel-mv-3")
    assert mv3.plant.plant_version == "fuel-mv-3"
    assert _refusal(_run, mv3, image, bytes(image), design, uncertainty) == (
        "uncertainty_plant_mismatch"
    )

    def for_mv3(raw: dict[str, Any]) -> None:
        raw["plant_version"] = "fuel-mv-3"
        lo, hi = mv3.plant.baseline.intervals[LUMPED_SCALE] if mv3.plant.baseline else (0, 1)
        raw["parameters"][LUMPED_SCALE] = {
            "distribution": "uniform",
            "bracket_from": "baseline_interval",
            "bracket": [lo, hi],
            "evidence": "test",
        }
        raw["parameters"]["exhaust_pressure_ratio"] = {
            "distribution": "uniform",
            "bracket_from": "working_assumption",
            "bracket": [1.0, 1.1],
            "evidence": "test",
        }

    declared = LoadedUncertainty(_with_declaration(uncertainty, for_mv3), "0" * 64, "test")
    check_uncertainty(declared.declaration, mv3)
    # The contract still decides which plant a scenario may run.
    template = _template(mv3, image, bytes(image))
    assert (
        _refusal(
            run_monte_carlo, mv3, template, design, image, bytes(image), declared, draws=2, seed=1
        )
        == "plant_version_mismatch"
    )


# -- no write path ---------------------------------------------------------------------------

_WRITE_PATHS = ("src.control", "src.daemon", "src.dashboard", "src.mcp_server", "can", "serial")
_ALLOWED_COLLECTION = {"src.collection", "src.collection.kline", "src.collection.kline.names"}


def test_a_monte_carlo_run_imports_nothing_that_talks_to_the_ecu() -> None:
    script = textwrap.dedent(
        """
        import json, sys
        import dataclasses
        from src.analysis.sim_montecarlo import UNCERTAINTY_FILE, load_uncertainty
        from tests.rom_fixtures import PROFILE
        from tests.test_sim_montecarlo import P_WOT, _design, _run, build_kit_and_image
        kit, image = build_kit_and_image()
        loaded = load_uncertainty(PROFILE / UNCERTAINTY_FILE)
        _run(kit, image, bytes(image), _design(image, (P_WOT, 195, 200)), loaded, draws=2)
        print(json.dumps(sorted(sys.modules)))
        """
    )
    done = subprocess.run(
        [sys.executable, "-c", script],
        capture_output=True,
        text=True,
        cwd=Path(__file__).resolve().parents[1],
        check=True,
        timeout=300,
    )
    loaded = json.loads(done.stdout.strip().splitlines()[-1])
    assert "src.analysis.sim_montecarlo" in loaded
    reached = [
        m
        for m in loaded
        if any(m == p or m.startswith(p + ".") for p in _WRITE_PATHS)
        or (m.startswith("src.collection") and m not in _ALLOWED_COLLECTION)
    ]
    assert reached == []


def test_the_declaration_file_is_plain_yaml_with_every_form_explained() -> None:
    raw = yaml.safe_load((PROFILE / UNCERTAINTY_FILE).read_text())
    assert raw["schema_version"] == 1
    assert all(len(e["evidence"]) > 40 for e in raw["parameters"].values())


# -- the rework: overrides are identified, MAP/rail reach the spread, battery -----------------


def test_a_run_on_overridden_parameters_is_its_own_run_and_never_ranks(
    kit_and_image: tuple[SimKit, bytes],
) -> None:
    """Two draws, or a draw and the frozen plant, must not share a session or
    rank: the result says which parameters it ran on."""
    kit, image = kit_and_image
    candidate = _edit_all(image, kit, "fuel_targets", 1, 4)
    qualified = dataclasses.replace(kit, plant=_plant_with("pass"))
    scenario = dataclasses.replace(
        _template(kit, image, candidate),
        points=(dataclasses.replace(WOT, source="design: override test"),),
    )
    frozen = run_paired(qualified, scenario, image, candidate)
    one = run_paired(qualified, scenario, image, candidate, plant_overrides={LUMPED_SCALE: 0.0507})
    two = run_paired(qualified, scenario, image, candidate, plant_overrides={LUMPED_SCALE: 0.0509})
    assert len({frozen.scenario_sha256, one.scenario_sha256, two.scenario_sha256}) == 3
    assert len({frozen.session, one.session, two.session}) == 3
    # The frozen run is refused only for the traced spark change (a Target AFR
    # edit reaches 27C6 through charge word 1B24); the override adds its own.
    frozen_verdict = rank_as_improvement(frozen, intervention=_minted(frozen, "pass"))
    assert frozen_verdict["reasons"] == [SPARK_TRACED]
    verdict = rank_as_improvement(one, intervention=_minted(one, "pass"))
    assert not verdict["rankable"]
    assert [r.split(":")[0] for r in verdict["reasons"]] == [OVERRIDES_NOT_FROZEN, "unsupported"]
    assert verdict["reasons"][1] == SPARK_TRACED


def test_a_wrong_assembly_is_refused_by_the_run_paired_check(
    kit_and_image: tuple[SimKit, bytes],
    uncertainty: LoadedUncertainty,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The self-check compares the assembled arrays, not only the cache: a
    plant pass that puts a value in the wrong place is refused."""
    kit, image = kit_and_image
    candidate = _edit_all(image, kit, "fuel_compensation", 1, 3)
    design = _design(image, (P_WOT, 190, 200), (P_MODEL, 190, 200))
    real = sim_montecarlo._plant

    def shifted(*args: Any, **kw: Any) -> dict[str, Any]:
        out = real(*args, **kw)
        return {k: v[::-1] for k, v in out.items()}  # base and candidate swapped

    monkeypatch.setattr(sim_montecarlo, "_plant", shifted)
    assert _refusal(_run, kit, image, candidate, design, uncertainty) == "fast_path_diverged"


def test_map_and_rail_reach_the_spread_where_the_plant_computes_them(
    kit_and_image: tuple[SimKit, bytes], uncertainty: LoadedUncertainty
) -> None:
    kit, image = kit_and_image
    candidate = _edit_all(image, kit, "fuel_compensation", 1, 3)
    design = _design(image, (P_PART_MODEL, 120, 120), (P_MODEL, 200, 200))
    computed = _run(kit, image, candidate, design, uncertainty)
    assert computed.inert_parameters == ["lambda_observation_delay"]
    assert computed.provenance["point_sources"]["map_kpa"] == {"plant_model": 2}
    for pid in ("manifold_pressure", "rail_pressure"):
        alone = _run(kit, image, candidate, design, uncertainty, vary=[pid])
        lam = alone.base["delivered_lambda"]
        assert np.ptp(lam[0]) > 1e-4, pid
        assert alone.supported("delivered_lambda").all()
        # Both ROMs share the drawn MAP and rail: the paired change barely moves.
        assert np.ptp(alone.relative_delta("delivered_lambda")[0]) < 1e-3
    # At WOT the capped MAP sits at baro across the VE bracket; rail still moves it.
    ve_wot = _run(kit, image, candidate, design, uncertainty, vary=["manifold_pressure"])
    assert np.ptp(ve_wot.base["delivered_lambda"][1]) == 0.0
    # A stated or measured MAP and rail is a boundary: held for every draw.
    for source in ("stated", "measured"):
        held = _point(WOT, map_kpa_source=source, rail_kpa_source=source)
        stated = _run(kit, image, candidate, _design(image, (held, 200, 200)), uncertainty)
        assert stated.inert_parameters == [
            "lambda_observation_delay",
            "manifold_pressure",
            "rail_pressure",
        ]


def test_an_unstated_map_is_the_contracts_declared_internal_model(
    kit_and_image: tuple[SimKit, bytes],
) -> None:
    """The comparison may leave MAP out only because the contract says the
    plant's internal model computes it there; without that, it is missing."""
    kit, image = kit_and_image
    model_point = dataclasses.replace(P_MODEL, conditioned={FILL_BYTE: 200.0})
    scenario = dataclasses.replace(_template(kit, image, image), points=(model_point,))
    [contract_scenario] = scenario.contract_scenarios(kit.contract, kit.contract_sha256)
    assert "manifold_pressure" not in contract_scenario.boundaries
    result = run_paired(kit, scenario, image, bytes(image))
    assert result.points[0]["outputs"]["delivered_lambda"]["status"] == "supported"
    raw = yaml.safe_load((PROFILE / CONTRACT_FILE).read_text())
    mode = next(m for m in raw["modes"] if m["id"] == "conditional_rom_comparison")
    del mode["roles"]["manifold_pressure"]["unstated"]
    strict = dataclasses.replace(kit, contract=SimContract.model_validate(raw))
    assert _refusal(run_paired, strict, scenario, image, bytes(image)) == "boundary_missing"
    # A stated MAP is still a boundary, and still accepted.
    run_paired(strict, dataclasses.replace(scenario, points=(WOT,)), image, bytes(image))


def test_an_unmeasured_battery_voltage_unsupports_only_the_absolute_pulse(
    kit_and_image: tuple[SimKit, bytes], uncertainty: LoadedUncertainty
) -> None:
    kit, image = kit_and_image
    candidate = _edit_all(image, kit, "fuel_compensation", 1, 3)
    result = _run(kit, image, candidate, _design(image, (P_NO_VB, 200, 200)), uncertainty)
    assert result.reasons("injector_pulse_ms", 0, 0) == (BATTERY_UNMEASURED,)
    assert not result.supported("injector_pulse_ms").any()
    for name in OUTPUTS:
        if name != "injector_pulse_ms":
            assert result.supported(name).all(), name
    # The dead time it reads cancels out of the metered part: lambda and its
    # change do not move with the stated voltage.
    other = _point(WOT, battery_v_source="unmeasured", battery_v=12.5)
    low = _run(kit, image, candidate, _design(image, (other, 200, 200)), uncertainty)
    assert np.allclose(
        low.relative_delta("delivered_lambda"),
        result.relative_delta("delivered_lambda"),
        rtol=0,
        atol=1e-12,
    )
    assert not np.array_equal(low.base["injector_pulse_ms"], result.base["injector_pulse_ms"])


def test_an_e08_2_design_adapts_with_its_sources() -> None:
    from tests.test_sim_design import _grid

    grid = _grid()
    design = MonteCarloDesign.from_sim_design(grid)
    assert design.sha256 == grid.sha256()
    assert design.base_image_sha256 == grid.base_image_sha256
    assert design.plant_version == grid.plant_version
    assert len(design.points) == len(grid.points)
    for dp, gp in zip(design.points, grid.points, strict=True):
        assert dp.point.conditioned == {}
        assert dp.point.source == grid.source
        assert (dp.fill_byte_lo, dp.fill_byte_hi, dp.weight) == (
            gp.fill_byte_lo,
            gp.fill_byte_hi,
            gp.weight,
        )
        assert dp.point.map_kpa_source == "plant_model"
        assert dp.point.rail_kpa_source == "working_assumption"
        assert dp.point.battery_v_source == "stated"


def test_a_design_built_on_another_plant_is_refused(
    kit_and_image: tuple[SimKit, bytes], uncertainty: LoadedUncertainty
) -> None:
    kit, image = kit_and_image
    design = dataclasses.replace(_design(image, (P_WOT, 190, 200)), plant_version="fuel-mv-3")
    assert _refusal(_run, kit, image, bytes(image), design, uncertainty) == (
        "design_plant_mismatch"
    )


# -- second re-verify: explicit sources, the rail caveat, refusing manifold models -----------


def test_an_e08_2_design_with_an_unknown_source_is_refused_by_name() -> None:
    """A typo or a new source must not become a held measured boundary."""
    from tests.test_sim_design import _grid

    grid = _grid()
    first = grid.points[0]

    def with_point(**update: str) -> Any:
        return grid.model_copy(
            update={"points": [first.model_copy(update=update), *grid.points[1:]]}
        )

    for update, reason in (
        ({"map_kpa_source": "speed_density_capd"}, "map_source_unknown"),
        ({"rail_kpa_source": "guessed"}, "rail_source_unknown"),
        ({"battery_v_source": "assumed"}, "battery_source_unknown"),
    ):
        assert _refusal(MonteCarloDesign.from_sim_design, with_point(**update)) == reason
    for source, mapped in (("read:did:MAP_KPA", "measured"), ("measured", "measured")):
        adapted = MonteCarloDesign.from_sim_design(with_point(map_kpa_source=source))
        assert adapted.points[0].point.map_kpa_source == mapped
    adapted = MonteCarloDesign.from_sim_design(with_point(map_kpa_source="stated"))
    assert adapted.points[0].point.map_kpa_source == "stated"


def test_the_rail_working_assumption_is_named_in_every_result_that_rests_on_it(
    kit_and_image: tuple[SimKit, bytes], uncertainty: LoadedUncertainty
) -> None:
    kit, image = kit_and_image
    rail = "plant:rail_pressure_unmeasured"
    on_rail = dataclasses.replace(P_MODEL, conditioned={FILL_BYTE: 200.0})
    template = _template(kit, image, image)
    paired = run_paired(kit, dataclasses.replace(template, points=(on_rail,)), image, bytes(image))
    assert rail in paired.unresolved
    stated = run_paired(kit, dataclasses.replace(template, points=(WOT,)), image, bytes(image))
    assert rail not in stated.unresolved
    mc = _run(kit, image, bytes(image), _design(image, (P_MODEL, 200, 200)), uncertainty)
    assert rail in mc.unresolved
    held = _run(kit, image, bytes(image), _design(image, (P_WOT, 200, 200)), uncertainty)
    assert rail not in held.unresolved


def _mv2(kit: SimKit) -> tuple[SimKit, LoadedUncertainty]:
    """The synthetic-image kit on the simulator's fuel-mv-2, and fuel-mv-2's
    own uncertainty declaration (``sim_kit.yaml`` names both, E08.11)."""
    loaded = sim_montecarlo.load_sim_kit(PROFILE)
    assert loaded.kit.plant.plant_version == "fuel-mv-2"
    mv2 = kit_for_plant(kit, PROFILE, "fuel-mv-2")
    check_uncertainty(loaded.uncertainty.declaration, mv2)
    return mv2, loaded.uncertainty


#: Warm idle on fuel-mv-2's identification drives: charge fraction p50 0.151
#: (762 rpm, 3.46 g/s, baro 81 kPa; fuel_plant_mv2.yaml), MAP from the model.
P_IDLE_MODEL = _point(
    WOT,
    rpm=762.0,
    maf_gps=3.46,
    baro_kpa=81.0,
    iat_c=30.0,
    throttle_pct=2.0,
    map_kpa_source="plant_model",
    rail_kpa_source="working_assumption",
    map_kpa=0.0,
)


def _map_at(kit: SimKit, rpm: float, maf: float, overrides: dict[str, float]) -> tuple[float, str]:
    from src.analysis.fuel_plant import PlantInputs, predict

    out = predict(
        PlantInputs(
            pulse_ms=np.array([2.0]),
            ecu_dead_time_ms=np.array([0.0]),
            rpm=np.array([rpm]),
            maf_gps=np.array([maf]),
            baro_kpa=np.array([81.0]),
            iat_c=np.array([30.0]),
            rail_kpa=np.array([350.0]),
        ),
        plant_parameters(kit, overrides),
        kit.plant.geometry,
    )
    refusal = "" if out.manifold_refusal is None else str(out.manifold_refusal[0])
    return float(out.map_kpa[0]), refusal


def test_a_refusing_manifold_model_names_its_refusal_and_is_refused_where_a_draw_reaches_it(
    kit_and_image: tuple[SimKit, bytes],
) -> None:
    """fuel-mv-2 refuses MAP above ambient per sample. In the paired chain the
    refusal is a named reason. In a Monte Carlo MAP is recomputed per draw, so
    a model-MAP point refused at any draw is refused outright
    (``manifold_refusal_varies_by_draw``, the backstop); a run whose every
    draw stays below ambient runs. The contract admits fuel-mv-2 (E08.11)."""
    from src.analysis.paired_scenarios import _ecu_chain, _fuel_chain, _side
    from src.analysis.twin_runner import stage_order

    kit, image = kit_and_image
    mv2, declared = _mv2(kit)
    side = _side(mv2, image, _identity(image), 0.0, "base")
    stages = stage_order(mv2.model)
    for maf, reasons in ((150.0, ["plant:manifold_above_ambient"]), (20.0, [])):
        point = dataclasses.replace(P_MODEL, maf_gps=maf, conditioned={FILL_BYTE: 200.0})
        out = _fuel_chain(
            mv2, side, point, _ecu_chain(mv2, stages, side, point), stft_pct=0.0, overrides={}
        )
        assert out["reasons"] == reasons
        assert np.isnan(out["delivered_lambda"]) == bool(reasons)

    template = _template(mv2, image, bytes(image))
    model = _design(image, (P_MODEL, 200, 200))
    with pytest.raises(SimulationRefusalError) as refused:
        run_monte_carlo(mv2, template, model, image, bytes(image), declared, draws=2, seed=1)
    assert refused.value.reason == "manifold_refusal_varies_by_draw"
    assert "manifold_above_ambient" in str(refused.value) and "at draw 0" in str(refused.value)
    # The backstop is for hand-built designs only. A point below ambient on
    # the frozen plant that a VE-floor draw lifts above it (800 rpm, a floor
    # band, MAP 0.875 x baro) is refused when handed in directly; the design
    # builders never place it (``manifold_above_ambient_in_bracket``, E08.11,
    # tests/test_sim_design.py).
    maf = 3.46 * (0.875 * 0.7 / 0.151) * (800.0 / 762.0)
    assert _map_at(mv2, 800.0, maf, {})[1] == ""
    assert _map_at(mv2, 800.0, maf, {"manifold_pressure": 0.4})[1] == "manifold_above_ambient"
    lifted = _point(P_IDLE_MODEL, rpm=800.0, maf_gps=maf)
    with pytest.raises(SimulationRefusalError) as refused:
        run_monte_carlo(
            mv2,
            template,
            _design(image, (lifted, 120, 120)),
            image,
            bytes(image),
            declared,
            draws=50,
            seed=3,
            vary=["manifold_pressure"],
        )
    assert refused.value.reason == "manifold_refusal_varies_by_draw"
    # Stated MAP: the model refuses nothing, and the contract admits fuel-mv-2.
    stated = _run(mv2, image, bytes(image), _design(image, (P_WOT, 200, 200)), declared, draws=2)
    assert stated.provenance["plant_version"] == "fuel-mv-2"
    assert stated.provenance["uncertainty_sha256"] == declared.sha256


def test_drawing_the_ve_floor_at_warm_idle_cannot_reach_ambient(
    kit_and_image: tuple[SimKit, bytes],
) -> None:
    """E08.11's expectation for the backstop: at warm idle (charge fraction
    ~0.15) even the floor's lower bracket 0.4, with the bank split at its
    upper 0.52, keeps MAP far below baro, so a 400-draw run over every
    declared parameter never trips ``manifold_refusal_varies_by_draw``."""
    kit, image = kit_and_image
    mv2, declared = _mv2(kit)
    worst, refusal = _map_at(mv2, 762.0, 3.46, {"manifold_pressure": 0.4, "bank_air_split": 0.52})
    assert refusal == "" and worst < 0.45 * 81.0
    result = _run(
        mv2,
        image,
        bytes(image),
        _design(image, (P_IDLE_MODEL, 40, 60)),
        declared,
        draws=400,
        seed=11,
    )
    assert "manifold_pressure" in result.provenance["varied"]
    assert "manifold_pressure" not in result.inert_parameters
    assert np.isfinite(result.base["delivered_lambda"]).all()


def test_a_ve_floor_draw_moves_only_the_floor_bands_never_the_fit(
    kit_and_image: tuple[SimKit, bytes],
) -> None:
    """Above the floor bands fuel-mv-2's VE is the frozen fit, held: drawing
    the floor anywhere in [0.4, 1.0] leaves MAP at 4,800 rpm bit-identical,
    and moves it at 1,500 rpm (a floor band), never below that band's own
    envelope."""
    kit, _ = kit_and_image
    mv2, _ = _mv2(kit)
    draws = (0.4, 0.55, 0.7, 0.85, 1.0)
    assert len({_map_at(mv2, 4800.0, 60.0, {"manifold_pressure": ve}) for ve in draws}) == 1
    floor = [_map_at(mv2, 1500.0, 10.0, {"manifold_pressure": ve})[0] for ve in draws]
    # 1000-2000 rpm: envelope 0.67674, so 0.4 and 0.55 both run at it.
    assert floor[0] == floor[1] > floor[2] > floor[3] > floor[4]
    curve = mv2.plant.baseline.ve_curve if mv2.plant.baseline else None
    assert curve is not None
    assert curve.band_values(0.4, fitted_at=0.7)[:2] == [0.4, curve.envelope[1]]
    assert curve.band_values(1.0, fitted_at=0.7) == [1.0, 1.0, *curve.envelope[2:]]
    assert curve.floor_bands(0.7) == [True, True, False, False, False, False]


def test_the_sim_kit_is_declared_data_with_its_own_uncertainty(
    uncertainty: LoadedUncertainty, tmp_path: Path
) -> None:
    """``load_sim_kit`` (E08.11): the plant ``sim_kit.yaml`` declares
    (fuel-mv-2) with fuel-mv-2's declaration; fuel-mv-1 and its untouched
    declaration on request (old runs); a version with no declaration, a
    profile with no sim_kit.yaml, and a declaration naming another plant are
    each refused by name. The contract's default stays fuel-mv-1."""
    import shutil

    loaded = sim_montecarlo.load_sim_kit(PROFILE)
    assert loaded.kit.plant.plant_version == "fuel-mv-2"
    assert loaded.kit.plant_file == "nissan-xterra-2008/fuel_plant_mv2.yaml"
    assert Path(loaded.uncertainty.path).name == "sim_uncertainty_mv2.yaml"
    assert loaded.kit.contract.plant_version == "fuel-mv-1"
    assert load_kit(PROFILE).plant.plant_version == "fuel-mv-1"
    old = sim_montecarlo.load_sim_kit(PROFILE, "fuel-mv-1")
    assert old.kit.plant.plant_version == "fuel-mv-1"
    assert old.uncertainty.sha256 == uncertainty.sha256
    assert _refusal(sim_montecarlo.load_sim_kit, PROFILE, "fuel-mv-3") == "uncertainty_undeclared"
    bare = tmp_path / "bare"
    shutil.copytree(PROFILE, bare, ignore=shutil.ignore_patterns("_*", "sim_kit.yaml"))
    assert _refusal(sim_montecarlo.load_sim_kit, bare) == "sim_kit_undeclared"
    crossed = tmp_path / "crossed"
    shutil.copytree(PROFILE, crossed, ignore=shutil.ignore_patterns("_*"))
    kit_file = crossed / "sim_kit.yaml"
    raw = yaml.safe_load(kit_file.read_text())
    raw["uncertainty"]["fuel-mv-2"] = "sim_uncertainty.yaml"
    kit_file.write_text(yaml.safe_dump(raw))
    assert _refusal(sim_montecarlo.load_sim_kit, crossed) == "uncertainty_plant_mismatch"


def test_fuel_mv_2s_uncertainty_brackets_are_its_registrys(
    kit_and_image: tuple[SimKit, bytes],
) -> None:
    """Every bracket in sim_uncertainty_mv2.yaml is fuel-mv-2's registry
    bracket (or its frozen baseline's interval for the lumped scale), checked
    entry by entry, and the declaration covers exactly what the plant
    brackets; moving one bracket is refused (bracket_mismatch)."""
    kit, _ = kit_and_image
    mv2, declared = _mv2(kit)
    decl = declared.declaration
    assert decl.plant_version == "fuel-mv-2"
    assert set(decl.parameters) == set(bracketed_parameters(mv2))
    assert mv2.plant.baseline is not None
    for pid, entry in decl.parameters.items():
        if pid == LUMPED_SCALE:
            assert tuple(entry.bracket) == tuple(mv2.plant.baseline.intervals[pid])
            assert entry.mean == mv2.plant.baseline.values[pid]
        else:
            wa = mv2.plant.registry.get(pid).working_assumption
            assert wa is not None and tuple(entry.bracket) == tuple(wa.bracket)
    assert tuple(decl.parameters["manifold_pressure"].bracket) == (0.4, 1.0)

    def moved(raw: dict[str, Any]) -> None:
        raw["parameters"]["manifold_pressure"]["bracket"] = [0.45, 1.0]

    assert _refusal(check_uncertainty, _with_declaration(declared, moved), mv2) == (
        "bracket_mismatch"
    )
    # fuel-mv-1's declaration is not fuel-mv-2's.
    old = load_uncertainty(PROFILE / UNCERTAINTY_FILE)
    assert _refusal(check_uncertainty, old.declaration, mv2) == "uncertainty_plant_mismatch"


#: The W09 form grid (tests/test_rom_editor_private_rom.py, ``FORM_*``, ``ENV``,
#: ``SEED``, ``DRAWS``: read, not imported, so this file needs no private ROM).
FORM_RPM_EDGES = [200.0 + 400.0 * i for i in range(17)]
FORM_LOAD_EDGES = [
    3.571429, 10.71429, 17.85714, 25.0, 32.14286, 39.28571, 46.42857, 53.57143,
    60.71429, 67.85714, 75.0, 82.14286, 89.28571, 96.42857, 100.0,
]  # fmt: skip
FORM_ENV: dict[str, Any] = dict(
    load_basis="fill_fraction_as_manifold_ratio",
    baro_kpa=81.0,
    iat_c=25.0,
    coolant_c=88.0,
    battery_v=14.0,
    rail_kpa=350.0,
    throttle_pct=20.0,
    pedal_flag=0,
)


def _built(image: bytes, rpm_edges: list[float], load_edges: list[float]) -> MonteCarloDesign:
    """A fuel-mv-2 grid built by the real builder on the profile, re-pinned to
    the synthetic image (the builder names the real base revision's image)."""
    from src.analysis.sim_design import build_grid_design

    grid = build_grid_design(
        rpm_edges, load_edges, profile_dir=PROFILE, base_revision="ZS44A-20260920", **FORM_ENV
    )
    assert grid.plant_version == "fuel-mv-2"
    design = MonteCarloDesign.from_sim_design(grid)
    return dataclasses.replace(design, base_image_sha256=sha256_hex(image))


@pytest.mark.parametrize(
    ("label", "rpm_edges", "load_edges", "placed", "draws", "seed"),
    [
        # E07.6's 4,800 rpm at the form's top fill cell (the verifier's repro):
        # a drawn bank split used to push it above ambient past split ~0.509.
        ("full_fill_4800", [4600.0, 5000.0], [96.42857, 100.0], 1, 200, 20261015),
        # The form's own grid and seed; its 400 rpm row is outside the VE fit,
        # 9 cells (800 rpm above ~57 % fill, 1,200-1,600 rpm at the top cell)
        # are above ambient at the VE floor's low end and are counted.
        ("form_grid", FORM_RPM_EDGES, FORM_LOAD_EDGES, 201, 4, 20261015),
        ("form_grid_more_draws", FORM_RPM_EDGES, FORM_LOAD_EDGES, 201, 60, 1),
    ],
)
def test_a_default_draw_monte_carlo_on_a_built_fuel_mv_2_grid_never_refuses(
    kit_and_image: tuple[SimKit, bytes],
    label: str,
    rpm_edges: list[float],
    load_edges: list[float],
    placed: int,
    draws: int,
    seed: int,
) -> None:
    """E08.11 rework: with one plenum (MAP from the total air, the split acting
    on per-bank fuelling only) and bracket-safe placement, no draw of any
    declared parameter can refuse a point a builder placed, so neither the
    backstop nor ``support_varies_by_draw`` fires, and every supported output
    is a number at every draw."""
    kit, image = kit_and_image
    mv2, declared = _mv2(kit)
    design = _built(image, rpm_edges, load_edges)
    assert len(design.points) == placed, label
    result = _run(mv2, image, bytes(image), design, declared, draws=draws, seed=seed)
    assert set(result.provenance["varied"]) >= {"manifold_pressure", "bank_air_split"}
    for name in OUTPUTS:
        ok = result.supported(name)
        assert np.isfinite(result.base[name][ok]).all(), (label, name)


def test_run_paired_and_the_fast_path_run_the_twin_runners_one_ecu_path(
    kit_and_image: tuple[SimKit, bytes],
    uncertainty: LoadedUncertainty,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """E09.2c: run_paired and the Monte Carlo fast path call the twin runner's
    stage kernels (``twin_runner.stated_ecu``'s steady BFS, its plant stage's
    ``plant_inputs``), not copies. One mutation there moves run_paired, and
    the fast path moves with it: its ``fast_path_diverged`` self-check, which
    re-runs run_paired, still agrees, and its arrays carry the mutation."""
    from src.analysis import twin_runner

    kit, image = kit_and_image
    candidate = _edit_all(image, kit, "fuel_compensation", 1, 3)
    design = _design(image, (P_WOT, 190, 200), (P_CRUISE, 50, 70))
    template = _template(kit, image, candidate)
    points = tuple(
        dataclasses.replace(dp.point, conditioned={FILL_BYTE: float(dp.fill_byte_lo)})
        for dp in design.points
    )
    scenario = dataclasses.replace(template, points=points)
    before = run_paired(kit, scenario, image, candidate)
    mc_before = _run(kit, image, candidate, design, uncertainty, draws=4)

    real_bfs, real_inputs = twin_runner.steady_bfs, twin_runner.plant_inputs

    def bfs_plus(air: Any, **kw: Any) -> Any:
        out = real_bfs(air, **kw)
        return dataclasses.replace(out, word=out.word + 64)

    def leaner(frame: Any) -> Any:
        inputs = real_inputs(frame)
        return dataclasses.replace(inputs, maf_gps=inputs.maf_gps * 1.05)

    monkeypatch.setattr(twin_runner, "steady_bfs", bfs_plus)
    monkeypatch.setattr(twin_runner, "plant_inputs", leaner)
    after = run_paired(kit, scenario, image, candidate)
    mc_after = _run(kit, image, candidate, design, uncertainty, draws=4)
    for i, (b, a) in enumerate(zip(before.points, after.points, strict=True)):
        bfs_b, bfs_a = b["outputs"]["base_fuel_schedule"], a["outputs"]["base_fuel_schedule"]
        assert bfs_a["base"] == bfs_b["base"] + 64
        assert bfs_a["candidate"] == bfs_b["candidate"] + 64
        lam_b, lam_a = b["outputs"]["delivered_lambda"], a["outputs"]["delivered_lambda"]
        assert lam_a["base"] != lam_b["base"]
        assert (
            mc_after.base["base_fuel_schedule"][i] == mc_before.base["base_fuel_schedule"][i] + 64
        ).all()
        assert (mc_after.base["delivered_lambda"][i] != mc_before.base["delivered_lambda"][i]).all()
