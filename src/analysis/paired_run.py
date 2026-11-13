"""What a paired run checks before it computes anything, and the learned state
it runs from: one copy for both paired entry points (R25.2).

:func:`~src.analysis.paired_scenarios.run_paired` and
:func:`~src.analysis.sim_montecarlo.run_monte_carlo` both refuse a recorded
source, a mode the slice does not run, image bytes that are not the named
image and a plant other than the kit's, and both derive the candidate's
learned trims from the events it implies. Each used to carry its own copy of
that prologue, and the copies drifted: Monte Carlo's plant refusal omitted the
hardware configuration, and it wrote a literal ``0.0`` where the paired run
used :data:`NEUTRAL_TRIM_PCT`. Here they are written once.

The run-level unresolved effects of the plant (:func:`plant_unresolved`) are
the third shared piece: both results name the same plant effects for the same
points.
"""

from __future__ import annotations

import re
from collections.abc import Iterable, Sequence
from typing import TYPE_CHECKING, Any

from src.analysis.fuel_plant import Qualification, resolve_parameters
from src.analysis.rom_image import sha256_hex
from src.analysis.sim_contract import (
    SCENARIO_ONLY_MODES,
    SimContract,
    SimulationRefusalError,
    check_scenario,
)

if TYPE_CHECKING:
    from src.analysis.paired_scenarios import OperatingPoint, PairedScenario, SimKit

#: The value a trim is reset to by an event that resets it: its neutral value.
NEUTRAL_TRIM_PCT = 0.0
#: Learned ECU states this slice carries, by contract quantity id.
LEARNED_TRIMS: tuple[str, ...] = ("stft_b1", "ltft_b1")

# A session-directory stamp inside a value's stated source: a number copied
# out of a recording.
_RECORDING = re.compile(r"\d{4}-\d{2}-\d{2}T\d{6}")


def check_contract_mode(
    contract: SimContract, scenario: PairedScenario, contract_sha256: str
) -> None:
    """The contract's refusals, per point, then the mode this slice runs.

    A point prescribes rpm, airflow and the trims; in a coupled rollout those
    are computed, so the contract refuses the point itself
    (``boundary_not_supplied_here``): recorded or stated, they cannot be fed
    back into a rollout's prediction of them."""
    mode = contract.mode(scenario.mode)
    for sc in scenario.contract_scenarios(contract, contract_sha256):
        check_scenario(contract, contract_sha256, sc)
    if mode.kind not in SCENARIO_ONLY_MODES:
        raise SimulationRefusalError(
            "not_a_paired_mode", f"{mode.id} replays a recording; a paired run states its inputs"
        )
    if mode.kind == "coupled_rollout":
        raise SimulationRefusalError(
            "dynamic_plant_unbuilt",
            "a coupled rollout recomputes airflow, rpm, BFS and the trims from driver demand; "
            "that needs E07.5's dynamic plant, so this slice runs the steady comparison only",
        )


def refuse_recorded_sources(scenario: PairedScenario) -> None:
    """Refuse any stated value whose source names a recording
    (``recorded_value_refused``)."""
    named = [p.source for p in scenario.points if _RECORDING.search(p.source)]
    if _RECORDING.search(scenario.learned.source):
        named.append(scenario.learned.source)
    for value in (*scenario.extra_boundaries.values(), *scenario.initial_state.values()):
        if _RECORDING.search(value.source):
            named.append(value.source)
    if named:
        raise SimulationRefusalError(
            "recorded_value_refused",
            f"stated values whose source is a recording {sorted(set(named))}: a number copied "
            "from a drive recorded under one calibration is that recording, not a scenario",
        )


def candidate_resets(contract: SimContract, events: Sequence[str]) -> dict[str, str]:
    """Learned trims each event resets, from the contract's declared inits."""
    declared = {e.id for e in contract.events}
    unknown = [e for e in events if e not in declared]
    if unknown:
        raise SimulationRefusalError("event_undeclared", f"the contract declares no {unknown}")
    out: dict[str, str] = {}
    for mode in contract.modes:
        for q, assignment in mode.roles.items():
            if assignment.init is None:
                continue
            for event in assignment.init.reset_on:
                if event in events:
                    if q not in LEARNED_TRIMS:
                        raise SimulationRefusalError(
                            "reset_unmodelled",
                            f"{event} resets {q}, which this slice does not carry",
                        )
                    out[q] = event
    return out


def check_run_inputs(
    kit: SimKit, scenario: PairedScenario, base_image: bytes, candidate_image: bytes
) -> dict[str, str]:
    """Every refusal a paired run makes before computing, in one order:
    a recorded source, the contract and its mode, each image's bytes against
    its named sha256 (``image_hash_mismatch``), and the scenario's plant
    against the kit's (``plant_mismatch``, version and hardware
    configuration). Returns the learned trims the candidate's events reset
    (:func:`candidate_resets`)."""
    refuse_recorded_sources(scenario)
    check_contract_mode(kit.contract, scenario, kit.contract_sha256)
    for label, image, identity in (
        ("base", base_image, scenario.base),
        ("candidate", candidate_image, scenario.candidate),
    ):
        if sha256_hex(image) != identity.image_sha256:
            raise SimulationRefusalError(
                "image_hash_mismatch", f"{label} bytes are not image {identity.image_sha256}"
            )
    if (scenario.plant.version, scenario.plant.hardware_configuration) != (
        kit.plant.plant_version,
        kit.plant.hardware_configuration,
    ):
        raise SimulationRefusalError(
            "plant_mismatch",
            f"the scenario names plant {scenario.plant.version}/"
            f"{scenario.plant.hardware_configuration}; the kit's is {kit.plant.plant_version}/"
            f"{kit.plant.hardware_configuration}",
        )
    return candidate_resets(kit.contract, scenario.candidate_events)


def learned_state(scenario: PairedScenario, resets: dict[str, str]) -> dict[str, Any]:
    """The trims each side runs from: the base at the stated learned state, the
    candidate with every trim its events reset at :data:`NEUTRAL_TRIM_PCT`
    (relearning is not modelled)."""
    base_ltft = scenario.learned.ltft_pct
    return {
        "base": {"stft_pct": scenario.learned.stft_pct, "ltft_pct": base_ltft},
        "candidate": {
            "stft_pct": NEUTRAL_TRIM_PCT if "stft_b1" in resets else scenario.learned.stft_pct,
            "ltft_pct": NEUTRAL_TRIM_PCT if "ltft_b1" in resets else base_ltft,
        },
        "resets": resets,
        "relearning": "not modelled: the candidate's trims stay at their reset values",
        "source": scenario.learned.source,
    }


def plant_unresolved(
    kit: SimKit, points: Iterable[OperatingPoint], qualification: Qualification
) -> set[str]:
    """The plant's run-level unresolved effects: the effects its working
    parameters rest on, the rail working value where a point has no rail
    reading, and why the plant is not qualified."""
    resolved = resolve_parameters(kit.plant, kit.delay)
    out = {f"plant:{e}" for e in resolved.effects}
    if any(p.rail_kpa_source == "working_assumption" for p in points):
        # No rail reading at the point: the plant ran its rail working value.
        out.add(f"plant:{resolved.rail_effect}")
    if qualification.reason is not None:
        out.add(qualification.reason)
    return out


__all__ = [
    "LEARNED_TRIMS",
    "NEUTRAL_TRIM_PCT",
    "candidate_resets",
    "check_contract_mode",
    "check_run_inputs",
    "learned_state",
    "plant_unresolved",
    "refuse_recorded_sources",
]
