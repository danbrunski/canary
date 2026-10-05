"""A06.5 — the alert budget, enforced where an artifact is published.

The budget is 1 alert per engine-hour (``V1_ALERTS_PER_HOUR_BUDGET``), on
drives the artifact neither trained nor calibrated on (A02's evaluation
drives). It was a figure in reports for months while the sequence detector
alerted ~117 times per engine-hour on the 2026-09-10 drive, live and in
replay: its false-alarm check scored the sessions its thresholds were cut
from, and nothing refused the artifact. This module turns the figure into a
refusal.

Only ``alert``-role detectors (``models.roles``) count. Since the operator's
2026-09-23 decisions (A06.1, A06.2) both the Isolation Forest and the sequence
detector are ``evidence``, so today the check only guards a future flip back
to ``alert``; an evidence-role artifact publishes without it.

**What must be under the budget: the upper bound, not the estimate.** A
detector passes only when the larger of two 97.5 % upper bounds is at most
the budget:

- the drive bootstrap A02's harness already reports
  (``evaluation.bootstrap_ratio_ci``), which carries between-drive variation;
- an exact Poisson bound on the pooled episode count over the pooled
  engine-hours, which carries count scarcity. The bootstrap reads 0 whenever
  every drive was quiet, however little driving that was, and gives no
  interval at all on one drive.

The estimate would pass the A02.11 Isolation Forest (1.2 per engine-hour,
interval 0-3.9) on noise, which is the whole history of this project's
retracted findings. The cost is stated, not hidden: zero alarms need about
3.7 held-out engine-hours before the bound falls under 1 per hour.
"""

from __future__ import annotations

import json
import math
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from src.artifact_hash import sha256_file

#: The incident every refusal names.
ALERT_BUDGET_INCIDENT = (
    "2026-09-10: the sequence detector raised ~117 events per engine-hour live and in "
    "replay against a budget of 1, because its false-alarm check scored the sessions its "
    "thresholds were cut from (A02.11 measured 45.4 per engine-hour as served, held out)"
)

#: Two-sided 95 % interval, matching ``bootstrap_ratio_ci``: the bound is its
#: 97.5th percentile.
ALERT_BUDGET_ALPHA = 0.05

#: The A02 harness's detector names for each published artifact's event
#: source. The served sequence path is ``sequence`` (global or per-family);
#: ``sequence_global`` is the harness's ablation, not what serves.
HARNESS_NAMES: dict[str, tuple[str, ...]] = {
    "isolation_forest": ("isolation_forest",),
    "lstm_autoencoder": ("sequence",),
}
#: The fused verdict can alert only through an alert-role source, so it is
#: held to the budget whenever one is published.
FUSED = "fused"

#: A06.6 — the key, in the A02 report and in every verdict, recording the
#: sequence forward cadence (``models.sequence_forward_every_n_ticks``) the
#: sequence detector was scored at. A report or verdict without it predates the
#: cadence and scored every window (cadence 1).
CADENCE_KEY = "sequence_forward_every_n_ticks"
#: The harness detectors whose verdicts depend on the sequence cadence.
CADENCE_BOUND = frozenset({"sequence", "sequence_global", FUSED})
#: What a cadence refusal names.
CADENCE_INCIDENT = (
    "A06.6: serving forward-passes the sequence detector every n-th tick, so a "
    "persistence-gated alarm rate measured at one cadence says nothing about another"
)


def recorded_cadence(record: Mapping[str, Any] | None) -> int:
    """The sequence cadence ``record`` (an A02 report or a verdict) was scored at;
    1 for one written before the cadence existed (it scored every window)."""
    value = (record or {}).get(CADENCE_KEY)
    return 1 if value is None else int(value)


def poisson_upper_rate(episodes: int, hours: float, *, alpha: float = ALERT_BUDGET_ALPHA) -> float:
    """Exact (Garwood) upper bound on a Poisson rate: the mean ``mu`` with
    ``P(X <= episodes; mu) = alpha / 2``, divided by ``hours``."""
    if hours <= 0:
        raise ValueError("hours must be positive")
    target = alpha / 2.0

    def cdf(mu: float) -> float:
        term = math.exp(-mu)
        total = term
        for k in range(1, episodes + 1):
            term *= mu / k
            total += term
        return total

    low, high = float(episodes), float(episodes) + 10.0 * math.sqrt(episodes + 1.0) + 10.0
    for _ in range(200):
        mid = (low + high) / 2.0
        if cdf(mid) > target:
            low = mid
        else:
            high = mid
    return high / hours


@dataclass(frozen=True)
class DetectorBudget:
    """One alert-role detector against the budget on the evaluation drives."""

    detector: str
    passed: bool
    reason: str
    episodes: int | None = None
    engine_hours: float | None = None
    drives: int = 0
    estimate: float | None = None
    bootstrap_upper: float | None = None
    poisson_upper: float | None = None
    upper: float | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "detector": self.detector,
            "passed": self.passed,
            "reason": self.reason,
            "false_alarm_episodes": self.episodes,
            "engine_hours": self.engine_hours,
            "drives": self.drives,
            "estimate_per_engine_hour": self.estimate,
            "bootstrap_upper_per_engine_hour": self.bootstrap_upper,
            "poisson_upper_per_engine_hour": self.poisson_upper,
            "upper_per_engine_hour": self.upper,
        }


@dataclass(frozen=True)
class AlertBudgetCheck:
    """The publish-time verdict. ``refused`` is what the publisher acts on."""

    budget: float
    #: The published event sources held to the budget (alert role).
    alert_sources: tuple[str, ...]
    detectors: tuple[DetectorBudget, ...] = ()
    #: Stated reason for publishing over a failed check, or ``None``.
    override_reason: str | None = None
    note: str = ""
    report: str | None = None
    extra: Mapping[str, Any] = field(default_factory=dict)
    #: A06.6 — the publishing config's sequence cadence the check held the
    #: report to; recorded in the verdict the daemon reads.
    forward_every_n_ticks: int = 1

    @property
    def required(self) -> bool:
        return bool(self.alert_sources)

    @property
    def violations(self) -> tuple[str, ...]:
        return tuple(f"{d.detector}: {d.reason}" for d in self.detectors if not d.passed)

    @property
    def refused(self) -> bool:
        return bool(self.violations) and self.override_reason is None

    def message(self) -> str:
        """The refusal text: every violation, the budget and the incident."""
        return (
            f"alert budget ({self.budget:g} alert per engine-hour, upper bound, held-out "
            f"drives) not met by alert-role detector(s): {'; '.join(self.violations)}. "
            f"Refused because of {ALERT_BUDGET_INCIDENT}. Keep the detector's role "
            "`evidence` (models.roles), evaluate on more held-out driving, or publish "
            "anyway with --accept-alert-budget '<reason>'."
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "budget_per_engine_hour": self.budget,
            "bound": "max(drive-bootstrap, exact Poisson) 97.5% upper bound",
            "required": self.required,
            "alert_sources": list(self.alert_sources),
            "detectors": [d.to_dict() for d in self.detectors],
            "violations": list(self.violations),
            "refused": self.refused,
            "override_reason": self.override_reason,
            "incident": ALERT_BUDGET_INCIDENT,
            "note": self.note,
            "report": self.report,
            CADENCE_KEY: self.forward_every_n_ticks,
            **dict(self.extra),
        }


def alert_role_sources(
    roles_by_source: Mapping[str, str], published_sources: Sequence[str]
) -> tuple[str, ...]:
    """The published sources whose configured role is ``alert``. A source the
    role table does not name alerts (behavior, DTC, ...: always ``alert``)."""
    return tuple(s for s in published_sources if roles_by_source.get(s, "alert") == "alert")


def check_alert_budget(
    report: Mapping[str, Any] | None,
    *,
    roles_by_source: Mapping[str, str],
    published_sources: Sequence[str],
    budget: float | None = None,
    override_reason: str | None = None,
    missing_report_reason: str | None = None,
    expected_artifacts: Mapping[str, str | None] | None = None,
    report_path: str | None = None,
    forward_every_n_ticks: int = 1,
) -> AlertBudgetCheck:
    """Hold each alert-role published detector to ``budget`` on ``report``.

    ``report`` is ``scripts/evaluate_detectors.py``'s JSON (A02's harness)
    over the evaluation drives, or ``None`` with ``missing_report_reason``
    saying why none exists — which fails every alert-role detector: an
    unmeasured detector has not shown the budget. ``expected_artifacts`` maps
    the harness's artifact labels (``isolation_forest`` / ``sequence``) to the
    sha256 the publisher is about to serve; a report on other bytes fails.
    ``forward_every_n_ticks`` is the publishing config's sequence cadence
    (A06.6): the sequence and fused detectors fail on a report scored at
    another one.
    """
    if override_reason is not None and not override_reason.strip():
        raise ValueError("an alert-budget override needs a stated reason")
    if budget is None:
        # Imported here: ``false_positive_benchmark`` pulls in torch, and the
        # daemon reads this module's verdict helpers at model load.
        from src.models.false_positive_benchmark import V1_ALERTS_PER_HOUR_BUDGET

        budget = V1_ALERTS_PER_HOUR_BUDGET
    alert = alert_role_sources(roles_by_source, published_sources)
    if not alert:
        return AlertBudgetCheck(
            budget=budget,
            alert_sources=(),
            override_reason=override_reason,
            note="every published detector is evidence-role (models.roles): "
            "no alerts to hold to the budget",
            report=report_path,
            forward_every_n_ticks=forward_every_n_ticks,
        )
    names = [name for source in alert for name in HARNESS_NAMES.get(source, (source,))]
    if report is not None and FUSED in (report.get("detectors") or ()):
        names.append(FUSED)
    if report is None:
        reason = missing_report_reason or "no A02 evaluation report"
        return AlertBudgetCheck(
            budget=budget,
            alert_sources=alert,
            detectors=tuple(
                DetectorBudget(name, passed=False, reason=f"not measured: {reason}")
                for name in names
            ),
            override_reason=override_reason,
            report=report_path,
            forward_every_n_ticks=forward_every_n_ticks,
        )
    stale = _artifact_mismatches(report, expected_artifacts or {})
    scored_at = recorded_cadence(report)
    detectors = tuple(
        _cadence_refusal(name, scored_at, forward_every_n_ticks)
        or _detector_budget(report, name, budget=budget, stale=stale)
        for name in names
    )
    return AlertBudgetCheck(
        budget=budget,
        alert_sources=alert,
        detectors=detectors,
        override_reason=override_reason,
        report=report_path,
        forward_every_n_ticks=forward_every_n_ticks,
    )


def _cadence_refusal(name: str, scored_at: int, serving_at: int) -> DetectorBudget | None:
    """A failed budget for a cadence-bound detector scored at another cadence."""
    if name not in CADENCE_BOUND or scored_at == serving_at:
        return None
    return DetectorBudget(
        name,
        passed=False,
        reason=f"the evaluation scored the sequence detector every {scored_at} tick(s); the "
        f"publishing config serves it every {serving_at} (models.sequence_forward_every_n_"
        f"ticks). Re-run the evaluation with the publishing config. Refused because of "
        f"{CADENCE_INCIDENT}",
    )


def _artifact_mismatches(
    report: Mapping[str, Any], expected: Mapping[str, str | None]
) -> list[str]:
    scored = report.get("artifacts") or {}
    out: list[str] = []
    for label, digest in expected.items():
        if digest is None:
            continue
        got = (scored.get(label) or {}).get("sha256")
        if got != digest:
            out.append(f"{label} scored {str(got)[:12]}…, publishing {digest[:12]}…")
    return out


def _detector_budget(
    report: Mapping[str, Any], name: str, *, budget: float, stale: Sequence[str]
) -> DetectorBudget:
    if stale:
        return DetectorBudget(
            name, passed=False, reason="the evaluation scored other bytes: " + "; ".join(stale)
        )
    if name not in (report.get("detectors") or ()):
        withheld = (report.get("withheld_detectors") or {}).get(name)
        return DetectorBudget(
            name,
            passed=False,
            reason="not measured: the evaluation did not score it"
            + (f" ({withheld})" if withheld else ""),
        )
    counted_as = (report.get("roles") or {}).get(name, "alert")
    if counted_as != "alert":
        return DetectorBudget(
            name,
            passed=False,
            reason=f"the evaluation counted it as {counted_as!r}, so its alarms were not "
            "counted; re-run it with the publishing config",
        )
    drives = [
        d for d in report.get("drives") or () if not d.get("cross_era") and name in d["detectors"]
    ]
    with_time = [d for d in drives if float(d.get("engine_hours") or 0.0) > 0]
    hours = sum(float(d["engine_hours"]) for d in with_time)
    if hours <= 0:
        return DetectorBudget(
            name,
            passed=False,
            reason=f"not measured: no held-out engine time scored ({len(drives)} drive(s))",
            drives=len(drives),
        )
    episodes = int(sum(int(d["detectors"][name]["false_alarm_episodes"]) for d in with_time))
    summary = ((report.get("false_alarms") or {}).get(name) or {}).get(
        "false_alarms_per_engine_hour"
    ) or {}
    bootstrap = summary.get("high")
    poisson = poisson_upper_rate(episodes, hours)
    upper = max(poisson, float(bootstrap)) if bootstrap is not None else poisson
    estimate = episodes / hours
    passed = upper <= budget
    reason = (
        f"{episodes} false alarm(s) in {hours:.2f} engine-h over {len(with_time)} drive(s): "
        f"{estimate:.2f}/h, upper bound {upper:.2f}/h "
        f"{'within' if passed else 'over'} {budget:g}/h"
    )
    if not passed and episodes == 0:
        reason += (
            f" (zero alarms need {poisson_upper_rate(0, 1.0) / budget:.1f} engine-h "
            "to bound the rate under the budget)"
        )
    return DetectorBudget(
        name,
        passed=passed,
        reason=reason,
        episodes=episodes,
        engine_hours=hours,
        drives=len(with_time),
        estimate=estimate,
        bootstrap_upper=None if bootstrap is None else float(bootstrap),
        poisson_upper=poisson,
        upper=upper,
    )


# ---------------------------------------------------------------------------
# The verdict that travels with the artifact (operator decision 2026-09-23)
# ---------------------------------------------------------------------------

#: The verdict file sits beside the binary it judges: ``model.pt`` ->
#: ``model.pt.alert-budget.json``. Models reach the Pi by ``scp`` and
#: ``data/analyses`` never does, so the daemon can only trust a verdict that
#: travels in the same directory and names the binary's sha256.
ALERT_BUDGET_VERDICT_SUFFIX = ".alert-budget.json"
ALERT_BUDGET_VERDICT_SCHEMA = 1


def verdict_path_for(artifact_path: Path) -> Path:
    """``<artifact>.alert-budget.json`` beside ``artifact_path``."""
    return artifact_path.with_name(artifact_path.name + ALERT_BUDGET_VERDICT_SUFFIX)


#: Old name, re-exported for its importers (``role_gate``, ``scripts/retrain``);
#: the one definition is :func:`src.artifact_hash.sha256_file` (R01.6).
file_sha256 = sha256_file


def evaluation_drives(report: Mapping[str, Any] | None) -> list[dict[str, Any]]:
    """The drives a verdict rests on (cross-era drives are never counted)."""
    return [
        {
            "drive": d.get("drive"),
            "engine_hours": d.get("engine_hours"),
            "sessions": d.get("sessions"),
        }
        for d in (report or {}).get("drives") or ()
        if not d.get("cross_era")
    ]


def verdict_record(
    check: AlertBudgetCheck,
    *,
    source: str,
    artifact_path: Path,
    sha256: str,
    evaluated_at: str | None = None,
) -> dict[str, Any]:
    """The per-artifact verdict ``scripts/retrain.py`` writes beside a gated
    artifact. ``accepted`` is what the daemon reads: the budget was met, or
    the publisher overrode it with a stated reason (kept in
    ``override_reason``). ``passed`` is the measurement alone. The drives it
    rests on come from ``check.extra["evaluation_drives"]``."""
    return {
        "schema_version": ALERT_BUDGET_VERDICT_SCHEMA,
        "source": source,
        "artifact": artifact_path.name,
        "sha256": sha256,
        "evaluated_at": evaluated_at or datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "accepted": check.required and not check.refused,
        "passed": check.required and not check.violations,
        "evaluation_drives": [],
        **check.to_dict(),
    }


def write_verdict(artifact_path: Path, record: Mapping[str, Any]) -> Path:
    path = verdict_path_for(artifact_path)
    path.write_text(json.dumps(dict(record), indent=2, sort_keys=True) + "\n")
    return path


__all__ = [
    "ALERT_BUDGET_ALPHA",
    "ALERT_BUDGET_VERDICT_SUFFIX",
    "ALERT_BUDGET_INCIDENT",
    "CADENCE_BOUND",
    "CADENCE_INCIDENT",
    "CADENCE_KEY",
    "FUSED",
    "HARNESS_NAMES",
    "AlertBudgetCheck",
    "DetectorBudget",
    "alert_role_sources",
    "check_alert_budget",
    "evaluation_drives",
    "file_sha256",
    "poisson_upper_rate",
    "recorded_cadence",
    "verdict_path_for",
    "verdict_record",
    "write_verdict",
]
