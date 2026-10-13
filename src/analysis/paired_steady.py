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
from src.analysis.injector_pulse import TRIM_BIAS, TRIM_UNITY, trim_word
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


# -- the closed-loop trim fixed point ---------------------------------------------------


def trim_k(stft_pct: float, ltft_pct: float) -> int:
    """``k = STFT + LTFT + w2E8C - 0xC800`` from Mode 01 percents (``w2E8C``
    neutral), as :func:`~src.analysis.injector_pulse.predict_pulse` forms it."""
    words = trim_word([stft_pct, ltft_pct])
    return int(words[0] + words[1] + TRIM_UNITY - TRIM_BIAS)


def total_trim_pct(k: float) -> float:
    """The total trim a ``k`` word applies, percent (``0x6400`` = 0 %)."""
    return (k / TRIM_UNITY - 1.0) * 100.0


def closed_loop_k(g_base: int, k_base: int, g_candidate: int) -> float:
    """The candidate's ``k`` at the closed-loop fixed point: the same metered
    open time as the base, ``G_c x k_c = G_b x k_b``."""
    if g_candidate <= 0:
        raise ValueError(f"G {g_candidate} meters nothing")
    return g_base * k_base / g_candidate


__all__ = [
    "COMPUTED_HERE",
    "FIXED_POINT_QUANTITY",
    "PID_MAF_COUNTS_PER_GPS",
    "SETPOINT_MOVED",
    "TRIM_AUTHORITY_UNDECODED",
    "SteadyBfs",
    "SteadyCoverage",
    "closed_loop_k",
    "reported_maf_word",
    "steady_bfs",
    "steady_coverage",
    "total_trim_pct",
    "trim_k",
]
