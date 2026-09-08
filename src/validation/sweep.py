"""Incremental bus-budget comparison under one fixed workload (C10.12).

The task's rule is *compare candidate budgets incrementally under the same
workload; stop increasing when a requested contract fails, identify the
constraint, and fix its source* — explicitly **not** "widen a queue or relax a
threshold until the number looks good". So this module changes exactly one
input between steps (``bus_budget_override``) and re-runs the *same*
:class:`~src.validation.scenarios.ScenarioSpec` otherwise unaltered: same
profile members, same target rate, same duration, same ECU, same queue sizes,
same quality thresholds. A step that fails its contract ends the sweep and
reports the stage that constrained it; it is never retried with a looser
setting.

The result is a bounded ladder, not a search: the caller supplies the
candidate budgets and the sweep walks them in order.
"""

from __future__ import annotations

import logging
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from src.validation.harness import run_scenario_clean
from src.validation.report import build_report
from src.validation.scenarios import ScenarioSpec

log = logging.getLogger(__name__)

__all__ = ["SweepStep", "SweepResult", "spec_at_budget", "summarize_sweep", "run_budget_sweep"]

#: Failure names that describe a broken delivery contract, as opposed to
#: environment notes. Membership here makes a failure *eligible* to stop a
#: sweep; it does not by itself stop one — see the baseline rule below.
_CONTRACT_FAILURES = (
    "rate_shortfall",
    "tuple_skew",
    "persistence_loss",
    "unanswered_input",
    "acquisition_gap",
)

#: **A failure seen with maximum headroom is not caused by pacing.** The first
#: sweep written here stopped at its opening step because ``acquisition_gap``
#: fires at every budget (a capture-start artefact: members keep the previous
#: profile's due time across activation), and ``unanswered_input`` fires
#: whenever the ECU simply does not serve a channel. Attributing either to the
#: rung would have reported "8 req/s is the ceiling" about a condition that has
#: nothing to do with the budget — the same shape as every retracted finding in
#: this project's history.
#:
#: So the sweep first runs the **largest** candidate budget as its baseline and
#: records that run's contract failures. Anchoring on the *lowest* rung instead
#: (the obvious first try, and wrong) would fold a genuine starvation failure
#: into the baseline the moment the ladder started below the workload's demand,
#: and the sweep could then never report a ceiling at all. Maximum headroom is
#: the only rung at which "this failure is not about pacing" is sound.
#:
#: The candidates are then walked in ascending order and a rung stops the sweep
#: only on a contract failure the baseline did not already have.


@dataclass
class SweepStep:
    """One budget's outcome under the fixed workload."""

    budget_req_s: float
    verdict: str | None
    verdict_with_adapter_ledger: str | None
    failures: list[str]
    contract_failures: list[str]
    limiting_stage: str | None
    limiting_evidence: Mapping[str, Any]
    achieved_vs_requested: Mapping[str, Any]
    tuples_complete: int | None
    tuples_attempted: int | None
    tuple_skew_max_s: float | None
    storage_dropped: int | None
    sequence_missing: int | None
    loss_identity_holds: bool | None
    batch_occupancy: Mapping[str, Any] | None
    contract_held: bool
    new_failures: list[str] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return {
            "budget_req_s": self.budget_req_s,
            "contract_held": self.contract_held,
            "new_failures_vs_baseline": self.new_failures,
            "verdict": self.verdict,
            "verdict_with_adapter_ledger": self.verdict_with_adapter_ledger,
            "failures": self.failures,
            "contract_failures": self.contract_failures,
            "limiting_stage": self.limiting_stage,
            "limiting_evidence": dict(self.limiting_evidence),
            "achieved_vs_requested": dict(self.achieved_vs_requested),
            "tuples_complete": self.tuples_complete,
            "tuples_attempted": self.tuples_attempted,
            "tuple_skew_max_s": self.tuple_skew_max_s,
            "storage_dropped": self.storage_dropped,
            "sequence_missing": self.sequence_missing,
            "loss_identity_holds": self.loss_identity_holds,
            "batch_occupancy": None if self.batch_occupancy is None else dict(self.batch_occupancy),
        }


@dataclass
class SweepResult:
    """The ladder and where it stopped."""

    scenario: str
    budgets_requested: list[float]
    steps: list[SweepStep] = field(default_factory=list)
    #: The first rung that fails *after* a rung that held — a genuine ceiling,
    #: as opposed to a floor below the workload's demand.
    ceiling_at: float | None = None
    constraint: str | None = None
    constraint_failures: list[str] = field(default_factory=list)
    #: Contract failures the lowest rung already had. Present at every rung
    #: and therefore never attributed to the budget.
    baseline_failures: list[str] = field(default_factory=list)
    #: The budget the baseline was taken at (the largest candidate).
    baseline_budget_req_s: float | None = None

    @property
    def holding(self) -> list[float]:
        return [step.budget_req_s for step in self.steps if step.contract_held]

    @property
    def highest_holding(self) -> float | None:
        """The largest budget whose contract held."""
        return max(self.holding) if self.holding else None

    @property
    def lowest_holding(self) -> float | None:
        """The smallest budget whose contract held — the minimum sufficient
        budget for this workload, and the more useful of the two: pacing
        failures appear *below* demand, not above it."""
        return min(self.holding) if self.holding else None

    def to_dict(self) -> dict[str, Any]:
        return {
            "report": "bus_budget_sweep",
            "scenario": self.scenario,
            "budgets_requested": self.budgets_requested,
            "highest_holding_budget_req_s": self.highest_holding,
            "lowest_holding_budget_req_s": self.lowest_holding,
            "ceiling_at_budget_req_s": self.ceiling_at,
            "constraint": self.constraint,
            "constraint_failures": self.constraint_failures,
            "baseline_failures": self.baseline_failures,
            "baseline_budget_req_s": self.baseline_budget_req_s,
            "steps": [step.to_dict() for step in self.steps],
        }


def spec_at_budget(spec: ScenarioSpec, budget: float) -> ScenarioSpec:
    """``spec`` with only ``bus_budget_override`` changed.

    Every other field — members, target rate, duration, ECU, queue sizes,
    thresholds — is carried through unchanged, because a ladder whose rungs
    differ in more than one input measures nothing.
    """
    from dataclasses import replace

    profile = dict(spec.profile)
    profile["bus_budget_override"] = float(budget)
    return replace(spec, profile=profile)


#: Cadence entries the bus budget actually governs. Broadcast frames arrive
#: whether or not anything is polled, so folding them into the worst-channel
#: ratio would dilute exactly the shortfall the sweep is looking for.
_POLLED_KINDS = ("pid", "did")


def _achieved_vs_requested(evidence: Mapping[str, Any]) -> dict[str, Any]:
    cadence = (evidence.get("transport") or {}).get("channel_cadence") or []
    worst_name: str | None = None
    worst_ratio: float | None = None
    measured = 0
    for entry in cadence:
        if not isinstance(entry, Mapping) or entry.get("kind") not in _POLLED_KINDS:
            continue
        requested = entry.get("requested_hz")
        achieved = entry.get("achieved_hz")
        if not requested or achieved is None:
            continue
        measured += 1
        ratio = float(achieved) / float(requested)
        if worst_ratio is None or ratio < worst_ratio:
            worst_ratio, worst_name = ratio, str(entry.get("channel"))
    return {
        "polled_channels_measured": measured,
        "worst_channel": worst_name,
        "worst_achieved_over_requested": None if worst_ratio is None else round(worst_ratio, 4),
    }


def _step_from_report(
    budget: float, report: Mapping[str, Any], baseline: Sequence[str] = ()
) -> SweepStep:
    evidence = report["evidence"]
    head = report["headline"]
    failures = list(head.get("quality_failures") or [])
    contract = [name for name in failures if name in _CONTRACT_FAILURES]
    # Count each name only as many times as it exceeds the baseline, so a
    # rung that gains a *second* acquisition_gap is still a new failure.
    new: list[str] = []
    remaining = list(baseline)
    for name in contract:
        if name in remaining:
            remaining.remove(name)
        else:
            new.append(name)
    return SweepStep(
        budget_req_s=budget,
        verdict=head.get("quality_verdict"),
        verdict_with_adapter_ledger=head.get("quality_verdict_with_adapter_ledger"),
        failures=failures,
        contract_failures=contract,
        limiting_stage=head.get("limiting_stage"),
        limiting_evidence=(evidence.get("limiting_stage") or {}).get("evidence") or {},
        achieved_vs_requested=_achieved_vs_requested(evidence),
        tuples_complete=head.get("tuples_complete"),
        tuples_attempted=head.get("tuples_attempted"),
        tuple_skew_max_s=head.get("tuple_skew_max_s"),
        storage_dropped=head.get("storage_dropped"),
        sequence_missing=head.get("sequence_missing"),
        loss_identity_holds=head.get("loss_identity_holds"),
        batch_occupancy=head.get("batch_occupancy"),
        contract_held=not new,
        new_failures=new,
    )


async def run_budget_sweep(
    spec: ScenarioSpec,
    budgets: Sequence[float],
    *,
    label: str,
    sessions_root: Path,
    scratch: Path,
    enable_inference: bool = True,
) -> SweepResult:
    """Walk ``budgets`` in order under one workload, stopping at the first
    failed contract. Returns every step that ran, including the failing one —
    the failure is evidence, not an error."""
    candidates = [float(b) for b in budgets]
    result = SweepResult(scenario=spec.name, budgets_requested=candidates)

    async def _report(budget: float) -> Mapping[str, Any]:
        artifacts = await run_scenario_clean(
            spec_at_budget(spec, budget),
            label=f"{label}-{budget:g}",
            sessions_root=sessions_root,
            scratch=scratch / f"budget-{budget:g}",
            enable_inference=enable_inference,
        )
        return build_report(artifacts)

    # Baseline: the most headroom any rung will get. Its failures are, by
    # construction, not about pacing. Its report is kept so the rung appears in
    # the ladder without being run twice.
    headroom = max(candidates)
    headroom_report = await _report(headroom)
    baseline = _step_from_report(headroom, headroom_report).contract_failures
    result.baseline_failures = list(baseline)
    result.baseline_budget_req_s = headroom
    log.info(
        "baseline at %.4g req/s (max headroom): %s",
        headroom,
        ", ".join(baseline) or "no contract failures",
    )

    # Every rung is run. The ladder is explicit and bounded, and stopping at
    # the first failure would report the *floor* (a budget below the
    # workload's demand) as though it were the ceiling.
    seen_holding = False
    for budget in sorted(candidates):
        report = headroom_report if budget == headroom else await _report(budget)
        step = _step_from_report(budget, report, baseline)
        result.steps.append(step)
        log.info(
            "budget %.4g req/s: contract %s (new: %s); limiting=%s",
            budget,
            "held" if step.contract_held else "FAILED",
            ", ".join(step.new_failures) or "-",
            step.limiting_stage,
        )
        if step.contract_held:
            seen_holding = True
        elif seen_holding and result.ceiling_at is None:
            # A failure that appears only after the contract has already held
            # at a lower budget is the thing C10.12 is looking for.
            result.ceiling_at = budget
            result.constraint = step.limiting_stage
            result.constraint_failures = step.new_failures
    return result


def summarize_sweep(result: SweepResult) -> str:
    """A markdown ladder. States the constraint, never a "safe" new default —
    raising a deployed rate needs hardware evidence (C09.16, C10.10)."""
    lines = [
        f"### Budget sweep — `{result.scenario}` (synthetic bus)",
        "",
        "| budget req/s | contract | verdict (adapter ledger) | limiting stage | "
        "worst achieved/requested | tuples | storage drops | named failures |",
        "|---:|---|---|---|---:|---|---:|---|",
    ]
    for step in result.steps:
        tuples = (
            "—"
            if step.tuples_attempted in (None, 0)
            else f"{step.tuples_complete}/{step.tuples_attempted}"
        )
        worst = step.achieved_vs_requested.get("worst_achieved_over_requested")
        lines.append(
            f"| {step.budget_req_s:g} | {'held' if step.contract_held else '**failed**'} "
            f"| {step.verdict_with_adapter_ledger or '—'} | {step.limiting_stage or '—'} "
            f"| {'—' if worst is None else f'{worst:.3f}'} | {tuples} "
            f"| {step.storage_dropped if step.storage_dropped is not None else '—'} "
            f"| {', '.join(step.failures) or 'none'} |"
        )
    lines.append("")
    if result.baseline_failures:
        lines.append(
            f"Baseline at {result.baseline_budget_req_s:g} req/s (the most headroom any rung "
            f"gets, so these are not about pacing): `{', '.join(result.baseline_failures)}`."
        )
        lines.append("")
    if not result.holding:
        lines.append(
            "**No rung held its contract.** Every candidate budget gained a failure the "
            "baseline did not have, so the ladder is entirely below this workload's demand — "
            "it found a floor, not a ceiling."
        )
    elif result.ceiling_at is not None:
        lines.append(
            f"Held from **{result.lowest_holding:g} req/s** up, then failed at "
            f"**{result.ceiling_at:g} req/s** (`{', '.join(result.constraint_failures)}`), "
            f"constrained by **{result.constraint}**. That is a ceiling: the contract had "
            "already held at a lower budget. No queue was widened and no threshold relaxed "
            "to get past it."
        )
    else:
        lines.append(
            f"Every rung from **{result.lowest_holding:g} req/s** to "
            f"{result.highest_holding:g} req/s held its contract, and no rung failed after "
            "one that held. **The ladder found no ceiling — it ran out of rungs, which is "
            "not the same thing.** Raising the budget past the workload's own request "
            "demand cannot fail, so a ceiling for this workload would have to be sought by "
            "raising demand, which would no longer be the same workload."
        )
    lines.append("")
    lines.append(
        "Synthetic bus, workstation. These bound the daemon's software path "
        "at each budget; they establish no rate on the vehicle and license no "
        "change to a deployed default (C10.10)."
    )
    return "\n".join(lines)
