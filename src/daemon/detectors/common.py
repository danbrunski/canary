"""What every live detector shares: the tick context, the persistence tail, score
publishing, role stamping and the schema-reconciliation log (R03.9).
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, replace
from datetime import datetime
from typing import Any, Generic, Protocol, TypeVar

from src.alerts.emitter import EmitterCounters
from src.alerts.fusion import (
    FusionSource,
    PersistenceGate,
)
from src.alerts.tuning import AlertTuner
from src.coerce import finite_float
from src.dashboard.broadcast import Broadcaster
from src.models.feature_spec_health import observe_reconciliation
from src.preprocessing.feature_pipeline import (
    FeatureSpecReconciliation,
)
from src.timefmt import format_timestamp

#: The detectors keep the logger name they had inside ``src.daemon.inference``,
#: so log filters and journal greps written against it still match.
log = logging.getLogger("src.daemon.inference")

_Job = TypeVar("_Job")
_Computed = TypeVar("_Computed")


def _persistence_sustained(
    persistence: PersistenceGate,
    key: str,
    firing: bool | None,
    *,
    tuner: AlertTuner | None,
    source: str,
    mode: str | None,
    now: float,
) -> bool:
    """Advance ``key`` through the persistence gate, honouring 6.3's
    debounce-ownership rule: when an operator ``DebouncePolicy`` is active
    for ``(source, mode)``, it fully OWNS that key's persistence
    requirement (applied later, inside the emitter, via
    ``AlertTuner.min_consecutive``) — the built-in gate's own M-of-N is
    bypassed (every trustworthy tick passes straight through) rather than
    stacking a second, redundant requirement on top of the tuner's.

    A missing verdict (``firing=None``, see ``PersistenceGate``'s docstring)
    always goes through the gate itself, bypass or not — there's no tuner
    debounce state to "pass through" for a tick that produced no verdict at
    all, and doing so keeps the gate's own streak correctly frozen (not
    reset to False) for the ticks where the bypass isn't in effect.
    """
    if firing is not None and tuner is not None and tuner.has_debounce_override(source, mode):
        return firing
    return persistence.observe(key, firing, now=now)


def gate_and_collect(
    persistence: PersistenceGate,
    key: str,
    firing: bool | None,
    *,
    tuner: AlertTuner | None,
    source: str,
    mode: str | None,
    now: float,
    counters: EmitterCounters,
) -> bool:
    """Advance ``key`` through the persistence gate and account for a
    tick that fired but didn't survive it (Task 3.1).

    The three per-tick detectors (IF, sequence, behavior) each used to
    open-code the same "was this tick's verdict actually sustained, and if
    it fired-but-not-sustained bump ``suppressed_persistence``" pairing
    around their own :func:`_persistence_sustained` call — this is that
    shared tail. Callers still apply :func:`~src.alerts.fusion.downgrade_unsustained`
    themselves (IF defers it a layer up, in ``IsolationForestDetector.tick``,
    since ``_score_and_emit`` needs the raw ``sustained`` flag to decide
    whether to withhold its own per-source emit too).
    """
    sustained = _persistence_sustained(
        persistence, key, firing, tuner=tuner, source=source, mode=mode, now=now
    )
    if firing and not sustained:
        counters.suppressed_persistence += 1
    return sustained


@dataclass(frozen=True)
class TickContext:
    """Per-tick state every :class:`Detector` needs, computed once by
    ``run_live_inference`` instead of re-derived by each detector (Task 3.1).
    """

    now: datetime
    operating_mode: str | None
    timestamp_str: str | None
    #: Phase 1.5 vehicle-state gate and the R03.12 capture-profile gate —
    #: ``False`` means IF/sequence must not score this tick (neither was
    #: trained on a non-running regime or a non-default profile); the
    #: behavior overlay ignores this and keeps its own per-model gating.
    scoring_allowed: bool
    #: The R03.12 capture-profile gate alone. Behavior models are not
    #: vehicle-state gated, but their deviations stop under a non-default
    #: profile too (operator decision 2026-09-25, R03.12b: tuning drives
    #: atypically on purpose, WOT pulls).
    capture_profile_allows: bool = True


class _PreparedDetector(Generic[_Job, _Computed]):
    """``tick`` for a detector split into prepare (loop) → compute (worker) →
    apply (loop) (C08.4). IF and the sequence detector carried identical
    copies of this method (R03.9)."""

    def prepare(self, row: Any, ctx: TickContext) -> _Job | None:
        raise NotImplementedError

    @staticmethod
    def compute(job: _Job) -> _Computed:
        raise NotImplementedError

    def apply(self, computed: _Computed, ctx: TickContext) -> list[FusionSource]:
        raise NotImplementedError

    def tick(self, row: Any, ctx: TickContext) -> list[FusionSource]:
        """Synchronous prepare → compute → apply, all on the caller's thread."""
        job = self.prepare(row, ctx)
        if job is None:
            return []
        return self.apply(self.compute(job), ctx)


class Detector(Protocol):
    """One per-tick anomaly detector (Task 3.1).

    Implementations own their detector's full observe → per-source emit →
    persistence → fusion-source shape, so ``run_live_inference`` reduces to
    "tick every detector, collect fusion sources, fuse." A detector may
    return zero, one, or several :class:`FusionSource` instances for one
    tick (IF/sequence contribute at most one; behavior contributes one per
    fired model).
    """

    def tick(self, row: Any, ctx: TickContext) -> list[FusionSource]: ...


def _with_effective_role(components: Any, source: FusionSource) -> FusionSource:
    """A06.5 — stamp the detector's *effective* role on its fusion source.

    ``FusionScorer`` holds the configured roles and never promotes an
    ``evidence`` source, so an ``alert``-configured detector serving an
    artifact without an accepted alert-budget verdict is demoted here, from
    ``components.role_gate`` (which re-resolves at a hot-swap), without the
    scorer knowing about artifacts.
    """
    gate = getattr(components, "role_gate", None)
    if gate is None or source.role == "evidence":
        return source
    if gate.get(source.name, "alert") == "evidence":
        return replace(source, role="evidence")
    return source


def _log_schema_reconciliation_once(
    model_type: str,
    model_version: str,
    report: FeatureSpecReconciliation,
    *,
    now: float | None = None,
) -> None:
    """Feed the structural-blindness tracker and log only on verdict changes.

    Every report is folded into :func:`observe_reconciliation` (a column is
    structural only if absent from *every* report over a sustained window —
    the first window after start lacks GPS and slow-tier PIDs, which is
    sparsity, not a retrain signal). Logging is bounded: one WARNING per
    (model_type, version) on first divergence, one line when the structural
    verdict is established, and one per later change to it.
    """
    update = observe_reconciliation(model_type, model_version, report, now=now)
    if update.transition is not None and update.verdict and update.verdict["absent_count"]:
        _log_structural_verdict(model_type, model_version, update.verdict, update.transition)
    elif update.transition is not None:
        log.info(
            "model feature spec is fully satisfiable by the live pipeline",
            extra={"model_type": model_type, "model_version": model_version},
        )
    if not report.has_divergence:
        return
    key = (model_type, model_version)
    if key in _SCHEMA_RECONCILIATION_WARNED:
        return
    _SCHEMA_RECONCILIATION_WARNED.add(key)
    log.warning(
        "live feature schema reconciled to model feature spec",
        extra={
            "model_type": model_type,
            "model_version": model_version,
            "schema_reconciliation": report.to_dict(),
        },
    )


def _log_structural_verdict(
    model_type: str,
    model_version: str,
    verdict: dict[str, Any],
    transition: str,
) -> None:
    """Log a structural-blindness verdict. Not the same event as "schema
    reconciled": an ERROR means the model cannot score a single row until it
    is retrained, and the only prior symptom was a `missing_feature_rate` of
    1.0 with no cause attached (D02.5). For a per-mode model the wording is
    about which mode models are blind (A04.5), from
    :func:`src.models.feature_spec_health.describe_structural_verdict`."""
    from src.models.feature_spec_health import describe_structural_verdict

    extra = {
        "model_type": model_type,
        "model_version": model_version,
        "structural_blindness": verdict,
        "transition": transition,
    }
    level, message = describe_structural_verdict(verdict, transition)
    if level == "error":
        log.error("%s", message, extra=extra)
    else:
        log.warning("%s", message, extra=extra)


_SCHEMA_RECONCILIATION_WARNED: set[tuple[str, str]] = set()


def _publish_score(
    scores: Broadcaster | None,
    *,
    source: str,
    score: Any,
    threshold: Any,
    operating_mode: str | None,
    model_version: str,
    timestamp: Any,
    missing_features: bool,
) -> None:
    """Fan one inference tick's score onto /ws/scores.

    Published every tick — not just anomalies — so the dashboard can show
    each model's live current value (including a normal 0). A non-finite
    score (a missing-feature tick) serializes as ``null`` alongside the
    ``missing_features`` flag rather than a misleading number.
    """
    if scores is None:
        return
    if isinstance(timestamp, str):
        ts_str = timestamp
    else:
        try:
            ts_str = format_timestamp(timestamp)
        except (ValueError, TypeError):
            ts_str = str(timestamp)
    scores.publish(
        {
            "type": "score",
            "source": source,
            "score": finite_float(score),
            "threshold": finite_float(threshold),
            "operating_mode": operating_mode,
            "model_version": model_version,
            "timestamp": ts_str,
            "missing_features": bool(missing_features),
        }
    )
