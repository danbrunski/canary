"""The steady-state ECU pieces of a paired ROM comparison (E07.6).

Two fixed points let :mod:`src.analysis.paired_scenarios` compute, per ROM,
what it used to take as a shared stated value or leave unsupported:

* **BFS at its steady fixed point.** The manifest implements ``0x03C62C``'s
  steady state (:func:`src.analysis.air_charge_twin.predict_steady_bfs`): with
  the airflow and fill filters settled and the transient term zero, BFS is
  ``min(0xFFFF, 4 x (Tp x FuelComp >> 9), ceiling << 8)`` from the reported MAF
  word, the rpm word, K and the Fuel Comp lookup at the throttle-model fill
  byte. So a Fuel Comp, K or Fuel Comp axis edit is *computed* through BFS, X,
  the Target AFR lookup and the pulse, instead of conditioning both ROMs on
  one stated BFS. The dynamics the fixed point stands for are declared in the
  contract (the comparison mode's ``base_fuel_schedule`` ``init.absorbs``,
  checked upstream of the manifest node); :class:`SteadyCoverage` treats their
  ``unmodelled`` reasons as covered only where the change does not edit one of
  them.
* **The closed-loop trim at its fixed point.** In closed loop the integrating
  trim settles where the sensed lambda equals the controller's setpoint. With
  the same air, rail and manifold for both ROMs (the comparison's boundaries)
  the same delivered lambda needs the same metered open time, so the trims
  hold ``G x k`` (``Ti - Ts = (G x k) >> 16``): the candidate's total trim is
  ``k_c = k_b x G_b / G_c``. That holds only while the setpoint is the same on
  both sides: it is a function of the A/F feedback target (``0xFFFF46B2``),
  whose relation to the commanded phi is not decoded, so a point whose target
  phi word differs between the ROMs is refused (``closed_loop_setpoint_moved``).
  The trim authority (its clamp) is not decoded either, so every fixed-point
  value carries ``closed_loop_trim_authority_undecoded``; the STFT/LTFT split
  and the settling are not modelled.
"""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass

from src.analysis.air_charge_twin import AirChargeCalibration, predict_steady_bfs
from src.analysis.ecu_application import ApplicationModel
from src.analysis.injector_pulse import TRIM_BIAS, TRIM_UNITY, trim_word
from src.analysis.rom_effects import EffectReport
from src.analysis.sim_contract import SimContract

#: SAE J1979 PID 0x10: the reported MAF word is ``(256 A + B)``, 0.01 g/s per
#: count -- the word ``air_charge_twin`` reads from the payload.
PID_MAF_COUNTS_PER_GPS = 100.0

#: Contract quantities this slice computes per ROM at a steady point; a point
#: may not state (condition on) them.
COMPUTED_HERE: tuple[str, ...] = ("base_fuel_schedule", "target_afr_x")
#: The quantity whose ``init.absorbs`` lists the dynamics its fixed point
#: stands for.
FIXED_POINT_QUANTITY = "base_fuel_schedule"

#: Per-output reasons a closed-loop fixed point carries, and the one a moved
#: setpoint gives.
TRIM_AUTHORITY_UNDECODED = "closed_loop_trim_authority_undecoded"
SETPOINT_MOVED = "closed_loop_setpoint_moved"


@dataclass(frozen=True)
class SteadyBfs:
    """One ROM's steady BFS at one point: the representative word and the
    exact interval the input quantisation leaves."""

    word: int
    lo: int
    hi: int
    fuel_comp: int


def reported_maf_word(maf_gps: float) -> int:
    """The PID 0x10 word the ECU reports for a prescribed airflow.

    The comparison prescribes airflow and takes the ECU's reading as that
    airflow: a MAF transfer error stays lumped in the plant's identified scale,
    as it is in identification (stated in every result's assumptions)."""
    if not maf_gps >= 0:
        raise ValueError(f"airflow {maf_gps} g/s is not a reading")
    return int(round(maf_gps * PID_MAF_COUNTS_PER_GPS))


def steady_bfs(
    cal: AirChargeCalibration, *, maf_gps: float, rpm_word: int, load_byte: int
) -> SteadyBfs:
    """BFS at the steady fixed point for one image (refusals are
    :class:`~src.analysis.air_charge_twin.AirChargeRefusalError`)."""
    pred = predict_steady_bfs(
        cal, maf_word=reported_maf_word(maf_gps), rpm_word=rpm_word, load_byte=load_byte
    )
    assert pred.fuel_comp_exact, "a stated load byte gives one Fuel Comp value"
    return SteadyBfs(word=pred.mid, lo=pred.lo, hi=pred.hi, fuel_comp=pred.fuel_comp_lo)


# -- what the fixed point covers ---------------------------------------------------------


@dataclass(frozen=True)
class SteadyCoverage:
    """Which ``compare_roms`` reasons a steady comparison computes itself.

    ``unmodelled:<node>`` is covered for a node the contract says BFS's fixed
    point absorbs; ``observed_not_predicted:<node>`` for the manifest node of
    a quantity computed here (X is computed from the computed BFS). Neither is
    covered when the change edits that node itself: an edited filter or
    transient is a different dynamic, and whether its fixed point moves is not
    claimed here."""

    absorbed: frozenset[str]
    computed_nodes: frozenset[str]
    changed_nodes: frozenset[str]

    def covers(self, reason: str) -> bool:
        kind, _, node = reason.partition(":")
        if not node or node in self.changed_nodes:
            return False
        if kind == "unmodelled":
            return node in self.absorbed
        if kind == "observed_not_predicted":
            return node in self.computed_nodes
        return False

    def uncovered(self, reasons: Iterable[str]) -> list[str]:
        return [r for r in reasons if not self.covers(r)]


def steady_coverage(
    contract: SimContract, mode_id: str, changed_nodes: Iterable[str]
) -> SteadyCoverage:
    """The coverage a steady comparison in ``mode_id`` has, read from the
    contract (never restated here)."""
    mode = contract.mode(mode_id)
    init = mode.roles[FIXED_POINT_QUANTITY].init
    absorbed = frozenset(init.absorbs) if init is not None else frozenset()
    computed = frozenset(
        node for q in COMPUTED_HERE if (node := contract.quantity(q).manifest_node) is not None
    )
    return SteadyCoverage(absorbed, computed, frozenset(changed_nodes))


#: The contract quantity (and manifest node) every point of a steady comparison
#: must state: BFS's Fuel Comp lookup reads this fill byte, and a point without
#: it gives ``conditioning_missing:<quantity>``.
FILL_BYTE = "fuel_comp_load_byte"
#: Roles in which a mode computes a quantity itself, so a scenario that states
#: it is conditioning the run on a value the ROM could move.
_CONDITIONABLE_ROLES = ("predicted_output", "inferred_state")


def conditionable_node(contract: SimContract, mode_id: str, quantity: str) -> str | None:
    """The manifest node of ``quantity`` when a scenario of ``mode_id`` may
    state it (a computed ECU quantity on the manifest), else ``None``. The one
    rule ``run_paired`` refuses on (``conditioning_not_allowed``) and
    ``rom_levers`` reads its caveat from."""
    node = contract.quantity(quantity).manifest_node
    if node is None or contract.mode(mode_id).role(quantity) not in _CONDITIONABLE_ROLES:
        return None
    return node


def conditionable_quantities(contract: SimContract, mode_id: str) -> dict[str, str]:
    """Every quantity a scenario of ``mode_id`` may state, with its manifest
    node. Quantities this slice computes itself (:data:`COMPUTED_HERE`) are not
    among them: they cannot be stated at all."""
    roles = contract.mode(mode_id).roles
    found: dict[str, str] = {}
    for q in contract.quantities:
        if q.id in roles and q.id not in COMPUTED_HERE:
            node = conditionable_node(contract, mode_id, q.id)
            if node is not None:
                found[q.id] = node
    return found


#: The application-model outputs a steady comparison computes (the ECU side),
#: and the paired runner's output each one is. The run, the Monte Carlo fast
#: path and ``rom_levers`` all read these names from here.
BFS_OUTPUT = "base_fuel_schedule_word"
TWIN_OUTPUT = "target_equiv_ratio"
ECU_RUNNER_OUTPUTS: dict[str, str] = {
    "base_fuel_schedule": BFS_OUTPUT,
    "target_phi": TWIN_OUTPUT,
}


def ecu_output_gate(
    report: EffectReport, coverage: SteadyCoverage
) -> dict[str, tuple[tuple[str, ...], bool]]:
    """What a steady comparison can say about the two ECU outputs it computes,
    by application-model output id: ``(reasons, evaluable)``.

    One rule, used by the run (``paired_scenarios.twin_gate``), the Monte
    Carlo fast path (through it) and the lever classifier (``rom_levers``), so
    none restates it:

    - **BFS**: its ``compare_roms`` reasons that ``coverage`` does not cover
      (``coverage.uncovered``); evaluable when none is left. An unchanged
      output has no reasons.
    - **The twin** (Target AFR): evaluable when the report says it computes at
      equal inputs, or reaches it with nothing uncovered on its twin path. When
      it is not evaluable, its reasons are the output's own (all of them: the
      run withholds the word, it does not partly cover it).

    An output the report does not trace is absent from the result."""
    gate: dict[str, tuple[tuple[str, ...], bool]] = {}
    bfs = next((o for o in report.outputs if o.output == BFS_OUTPUT), None)
    if bfs is not None:
        rest = tuple(coverage.uncovered(bfs.reasons)) if bfs.status != "unchanged" else ()
        gate[BFS_OUTPUT] = (rest, not rest)
    twin = next((o for o in report.outputs if o.output == TWIN_OUTPUT), None)
    if twin is not None:
        node = twin.twin_node
        evaluable = bool(
            node
            and (
                node.get("evaluable_at_equal_inputs")
                or (node.get("reached") and not coverage.uncovered(node.get("reasons", [])))
            )
        )
        held = twin.status != "unchanged" and not evaluable
        gate[TWIN_OUTPUT] = (tuple(twin.reasons) if held else (), evaluable)
    return gate


# -- the closed-loop trim fixed point ---------------------------------------------------


def trim_k(stft_pct: float, ltft_pct: float) -> int:
    """``k = STFT + LTFT + w2E8C - 0xC800`` from Mode 01 percents (``w2E8C``
    neutral), as :func:`~src.analysis.injector_pulse.predict_pulse` forms it."""
    words = trim_word([stft_pct, ltft_pct])
    return int(words[0] + words[1] + TRIM_UNITY - TRIM_BIAS)


def total_trim_pct(k: float) -> float:
    """The total trim a ``k`` word applies, percent (``0x6400`` = 0 %)."""
    return (k / TRIM_UNITY - 1.0) * 100.0


def closed_loop_trim_pending(model: ApplicationModel) -> list[str]:
    """Closed-loop overrides the vehicle has not qualified: the manifest's own
    statement that the trim's authority (its clamp, enable and step) is not
    decoded. A closed-loop fixed point carries
    :data:`TRIM_AUTHORITY_UNDECODED` while this is non-empty.

    The coupling is deliberate and coarse: the three E05.3 closed-loop
    overrides (``closed_loop_phi_one``, ``closed_loop_curve_term``,
    ``closed_loop_feedback_stft``) are read together, so the reason lifts only
    when *every* ``regime: closed_loop`` override is ``vehicle``-qualified
    (E09.10). The run and ``rom_levers`` both call this; neither restates the
    rule. Today it is always non-empty."""
    return [
        o.id for o in model.overrides if o.regime == "closed_loop" and o.qualification != "vehicle"
    ]


def closed_loop_k(g_base: int, k_base: int, g_candidate: int) -> float:
    """The candidate's ``k`` at the closed-loop fixed point: the same metered
    open time as the base, ``G_c x k_c = G_b x k_b``."""
    if g_candidate <= 0:
        raise ValueError(f"G {g_candidate} meters nothing")
    return g_base * k_base / g_candidate


__all__ = [
    "BFS_OUTPUT",
    "COMPUTED_HERE",
    "ECU_RUNNER_OUTPUTS",
    "FILL_BYTE",
    "FIXED_POINT_QUANTITY",
    "PID_MAF_COUNTS_PER_GPS",
    "SETPOINT_MOVED",
    "TRIM_AUTHORITY_UNDECODED",
    "TWIN_OUTPUT",
    "SteadyBfs",
    "SteadyCoverage",
    "closed_loop_k",
    "closed_loop_trim_pending",
    "conditionable_node",
    "conditionable_quantities",
    "ecu_output_gate",
    "reported_maf_word",
    "steady_bfs",
    "steady_coverage",
    "total_trim_pct",
    "trim_k",
]
