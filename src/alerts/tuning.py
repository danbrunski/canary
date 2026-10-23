"""Threshold tuning + alert suppression (Task 6.25).

Once the operator is labelling false positives (6.24), they need a knob to
*act* on them without retraining: raise a per-model / per-operating-mode
threshold, suppress a known-benign pattern, debounce a flappy detector, or
hold a cool-down after an alert. This module is that knob, with the
guardrails Risk R11 demands — every change is **versioned, audited, and
reversible**, and a **before/after false-positive estimate** can be run
against historical anomalies *before* a change goes live so tuning can't
silently blind the detector.

Scope discipline: the tuner only ever *suppresses* an event the detector
already flagged (raise-the-bar / mute), never manufactures one. Lowering a
threshold to catch *more* would mean re-scoring non-anomalous inference
output — that belongs in the model layer, not the alert edge, and is out of
scope here on purpose.

State model: ``AlertTuner`` is consulted by the (single-threaded) emitter
per candidate via :meth:`AlertTuner.decide`; the emitter calls
:meth:`AlertTuner.notify_emitted` after a real emit so cool-down /
hysteresis arm. ``TuningConfig`` itself is pure data — versioned YAML with
an append-only audit log that snapshots the *full* config each change, so
any prior version can be restored verbatim.
"""

from __future__ import annotations

import json
import logging
from collections.abc import Iterable
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import yaml
from pydantic import BaseModel, ConfigDict, Field

from src.atomic_write import atomic_write_text
from src.paths import CONTROL_DIR
from src.yaml_load import load_yaml

log = logging.getLogger(__name__)

__all__ = [
    "DEFAULT_TUNING_AUDIT_PATH",
    "DEFAULT_TUNING_PATH",
    "AlertTuner",
    "DebouncePolicy",
    "SuppressionRule",
    "ThresholdOverride",
    "TuningConfig",
    "TuningDecision",
    "estimate_before_after",
    "estimate_effect",
    "load_tuning_config",
    "load_tuning_version",
    "save_tuning_config",
]

#: Mirrors of ``DaemonConfig.tuning_config`` / ``tuning_audit`` (R01.2: one
#: repo-rooted home, ``src.paths.CONTROL_DIR``).
DEFAULT_TUNING_PATH = CONTROL_DIR / "tuning.yaml"
DEFAULT_TUNING_AUDIT_PATH = CONTROL_DIR / "tuning_audit.jsonl"

#: Wildcard for "any operating mode" / "any source" in a scope match.
ANY = "*"


class _Strict(BaseModel):
    model_config = ConfigDict(extra="forbid", protected_namespaces=())


class ThresholdOverride(_Strict):
    """Raise the effective threshold for a (model, operating-mode) scope.

    Exactly one of ``absolute_threshold`` / ``multiplier`` should be set.
    ``multiplier`` scales the detector's own threshold (e.g. 1.5 = "only
    alert at 1.5× the calibrated bar"); ``absolute_threshold`` pins it. An
    override only applies when the event carries a numeric ``threshold`` and
    ``score`` (the LSTM does; the IF's contamination is a quantile, so IF
    events fall through untouched). Never lowers the bar.
    """

    source: str  # "lstm_autoencoder" | "isolation_forest" | ANY
    operating_mode: str = ANY
    multiplier: float | None = Field(default=None, gt=0)
    absolute_threshold: float | None = None
    enabled: bool = True
    note: str | None = None


class SuppressionRule(_Strict):
    """Mute events matching a known-benign pattern.

    All set conditions must match (AND). ``channel_contains`` matches if any
    of the event's affected channels contains the substring. ``max_score``
    suppresses only low-confidence hits, so a genuinely severe excursion in a
    suppressed mode still gets through.
    """

    name: str
    source: str = ANY
    operating_mode: str = ANY
    channel_contains: str | None = None
    max_score: float | None = None
    enabled: bool = True
    reason: str = ""


class DebouncePolicy(_Strict):
    """Debounce / hysteresis / cool-down for a (source, operating-mode) scope.

    * ``min_consecutive`` — require this many anomalous candidates in a row
      before the first alert (filters single-sample blips).
    * ``cooldown_seconds`` — after an alert, suppress the scope for this long.
    * ``rearm_below`` — hysteresis: after an alert, stay muted until the
      score drops below this, even past the cool-down (stops flapping around
      the threshold).
    """

    source: str = ANY
    operating_mode: str = ANY
    min_consecutive: int = Field(default=1, ge=1)
    cooldown_seconds: float = Field(default=0.0, ge=0)
    rearm_below: float | None = None
    enabled: bool = True


class TuningConfig(_Strict):
    """Versioned tuning state. Bump ``version`` on every change."""

    version: int = 1
    threshold_overrides: list[ThresholdOverride] = Field(default_factory=list)
    suppression_rules: list[SuppressionRule] = Field(default_factory=list)
    debounce_policies: list[DebouncePolicy] = Field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return self.model_dump(mode="json")


# ---------------------------------------------------------------------------
# Persistence + audit (versioned, reversible)
# ---------------------------------------------------------------------------


def load_tuning_config(path: Path = DEFAULT_TUNING_PATH) -> TuningConfig:
    """Load the active tuning config; empty (no-op) config if absent."""
    if not Path(path).exists():
        return TuningConfig()
    return TuningConfig.model_validate(load_yaml(Path(path).read_text()) or {})


def save_tuning_config(
    config: TuningConfig,
    path: Path = DEFAULT_TUNING_PATH,
    *,
    audit_path: Path = DEFAULT_TUNING_AUDIT_PATH,
    change: str,
    by: str | None = None,
) -> TuningConfig:
    """Atomically write ``config`` and append a full-snapshot audit line.

    The audit line carries the entire post-change config, so
    :func:`load_tuning_version` can reconstruct any prior version verbatim —
    that's what makes a tuning change reversible without a separate backup.
    """
    path = Path(path)
    atomic_write_text(path, yaml.safe_dump(config.to_dict(), sort_keys=False), mkdir=True)

    audit_path = Path(audit_path)
    audit_path.parent.mkdir(parents=True, exist_ok=True)
    with audit_path.open("a") as handle:
        handle.write(
            json.dumps(
                {
                    "at": datetime.now(UTC).isoformat(),
                    "by": by,
                    "change": change,
                    "version": config.version,
                    "config": config.to_dict(),
                },
                sort_keys=True,
            )
            + "\n"
        )
        handle.flush()
    return config


def load_tuning_version(
    version: int, audit_path: Path = DEFAULT_TUNING_AUDIT_PATH
) -> TuningConfig | None:
    """Reconstruct a historical config snapshot by version (for revert)."""
    if not Path(audit_path).exists():
        return None
    found: TuningConfig | None = None
    with Path(audit_path).open() as handle:
        for line in handle:
            line = line.strip()
            if not line:
                continue
            try:
                entry = json.loads(line)
            except json.JSONDecodeError:
                continue
            if int(entry.get("version", -1)) == version:
                found = TuningConfig.model_validate(entry["config"])
    return found


# ---------------------------------------------------------------------------
# Decisioning
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class TuningDecision:
    """Outcome of consulting the tuner for one candidate event."""

    emit: bool
    reason: str | None = None  # why suppressed, when emit is False
    effective_threshold: float | None = None


def _scope_match(rule_source: str, rule_mode: str, source: str, mode: str | None) -> bool:
    src_ok = rule_source in (ANY, source)
    mode_ok = rule_mode == ANY or rule_mode == (mode or "")
    return src_ok and mode_ok


def _event_view(event: Any) -> dict[str, Any]:
    """Normalize an AnomalyEvent (or dict) to the fields the tuner reads."""
    if hasattr(event, "model_dump"):
        dumped: dict[str, Any] = event.model_dump(mode="json")
        return dumped
    return dict(event)


@dataclass
class _ScopeState:
    consecutive: int = 0
    muted_until: float = 0.0
    awaiting_rearm: bool = False


class AlertTuner:
    """Apply a :class:`TuningConfig` at the alert edge.

    Stateful across candidates within a scope (debounce/cool-down/hysteresis);
    construct one per emitter. Pure-suppression by contract — see module
    docstring.
    """

    def __init__(self, config: TuningConfig) -> None:
        self.config = config
        self._state: dict[tuple[str, str], _ScopeState] = {}

    # -- threshold + suppression are stateless; debounce/cooldown stateful --

    def _override_threshold(
        self, source: str, mode: str | None, base: float | None
    ) -> float | None:
        """The bar an operator override imposes, or ``None`` if no override
        applies. Deliberately does *not* fall back to the detector's own
        threshold — re-gating on that is the detector's job, not the tuner's,
        and would double-suppress legitimately-anomalous events. Never lowers
        below the detector's bar (suppression-only contract)."""
        result: float | None = None
        for ov in self.config.threshold_overrides:
            if not ov.enabled or not _scope_match(ov.source, ov.operating_mode, source, mode):
                continue
            candidate: float | None = None
            if ov.absolute_threshold is not None:
                candidate = ov.absolute_threshold
            elif ov.multiplier is not None and base is not None:
                candidate = base * ov.multiplier
            if candidate is None:
                continue
            if base is not None:
                candidate = max(candidate, base)
            result = candidate if result is None else max(result, candidate)
        return result

    def _suppression_reason(self, view: dict[str, Any]) -> str | None:
        source = str(view.get("source", ""))
        mode = view.get("operating_mode")
        score = view.get("score")
        channels = [str(c) for c in view.get("affected_channels", [])]
        for rule in self.config.suppression_rules:
            if not rule.enabled or not _scope_match(rule.source, rule.operating_mode, source, mode):
                continue
            if rule.channel_contains is not None and not any(
                rule.channel_contains in c for c in channels
            ):
                continue
            if rule.max_score is not None and score is not None and float(score) > rule.max_score:
                continue
            return rule.reason or rule.name
        return None

    def decide(self, event: Any, *, now: float | None = None) -> TuningDecision:
        """Should this already-flagged candidate actually be emitted?"""
        view = _event_view(event)
        source = str(view.get("source", ""))
        mode = view.get("operating_mode")
        score = view.get("score")
        threshold = view.get("threshold")
        clock = now if now is not None else _now()

        override = self._override_threshold(source, mode, threshold)
        eff = override if override is not None else threshold
        if override is not None and score is not None and float(score) < override:
            return TuningDecision(False, f"below tuned threshold {override:g}", eff)

        suppressed = self._suppression_reason(view)
        if suppressed is not None:
            return TuningDecision(False, f"suppressed: {suppressed}", eff)

        policy = self._policy_for(source, mode)
        if policy is not None:
            key = (source, str(mode or ""))
            st = self._state.setdefault(key, _ScopeState())
            below_rearm = (
                policy.rearm_below is not None
                and score is not None
                and float(score) < policy.rearm_below
            )
            # Cool-down: time-based mute after an alert. A low sample seen
            # during cool-down still clears the hysteresis latch.
            if clock < st.muted_until:
                if st.awaiting_rearm and below_rearm:
                    st.awaiting_rearm = False
                return TuningDecision(False, "muted: cool-down active", eff)
            # Hysteresis: after cool-down, stay muted until a sub-`rearm_below`
            # sample is observed. That re-arming sample is itself suppressed
            # (it's the "all clear", not a new alert).
            if st.awaiting_rearm:
                if below_rearm:
                    st.awaiting_rearm = False
                    return TuningDecision(False, "muted: hysteresis re-armed", eff)
                return TuningDecision(False, "muted: awaiting hysteresis re-arm", eff)
            st.consecutive += 1
            if st.consecutive < policy.min_consecutive:
                return TuningDecision(
                    False,
                    f"debounce: {st.consecutive}/{policy.min_consecutive}",
                    eff,
                )
        return TuningDecision(True, None, eff)

    def notify_emitted(self, event: Any, *, now: float | None = None) -> None:
        """Tell the tuner an event was actually emitted — arms cool-down."""
        view = _event_view(event)
        source = str(view.get("source", ""))
        mode = view.get("operating_mode")
        policy = self._policy_for(source, mode)
        if policy is None:
            return
        clock = now if now is not None else _now()
        st = self._state.setdefault((source, str(mode or "")), _ScopeState())
        st.consecutive = 0
        if policy.cooldown_seconds > 0:
            st.muted_until = clock + policy.cooldown_seconds
        if policy.rearm_below is not None:
            st.awaiting_rearm = True

    def _policy_for(self, source: str, mode: str | None) -> DebouncePolicy | None:
        for policy in self.config.debounce_policies:
            if policy.enabled and _scope_match(policy.source, policy.operating_mode, source, mode):
                return policy
        return None

    def has_debounce_override(self, source: str, mode: str | None) -> bool:
        """6.3 — whether an operator debounce policy is active for
        (source, mode). Used by the daemon's built-in ``PersistenceGate``
        wiring: when true, the tuner's own ``min_consecutive`` fully owns
        that key's persistence requirement, so the gate is bypassed for it
        instead of stacking a second requirement on top."""
        return self._policy_for(source, mode) is not None


def _now() -> float:
    return datetime.now(UTC).timestamp()


# ---------------------------------------------------------------------------
# Before/after estimate (run before a change goes live — Risk R11)
# ---------------------------------------------------------------------------


@dataclass
class EstimateResult:
    evaluated: int = 0
    would_emit: int = 0
    would_suppress: int = 0
    by_reason: dict[str, int] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return {
            "evaluated": self.evaluated,
            "would_emit": self.would_emit,
            "would_suppress": self.would_suppress,
            "by_reason": dict(self.by_reason),
        }


def estimate_effect(events: Iterable[dict[str, Any]], config: TuningConfig) -> EstimateResult:
    """Stateless threshold+suppression pass over historical anomalies.

    Debounce/cool-down are intentionally *not* applied — they depend on live
    ordering/timing; the estimate answers the safety question "how many of
    these past alerts would this config have muted, and why?" so the operator
    sees the blast radius before committing (Risk R11).
    """
    # A fresh tuner with the debounce policies stripped: estimate the
    # rule/threshold effect only, deterministically.
    static = TuningConfig(
        version=config.version,
        threshold_overrides=config.threshold_overrides,
        suppression_rules=config.suppression_rules,
    )
    tuner = AlertTuner(static)
    result = EstimateResult()
    for ev in events:
        result.evaluated += 1
        decision = tuner.decide(ev, now=0.0)
        if decision.emit:
            result.would_emit += 1
        else:
            result.would_suppress += 1
            reason = (decision.reason or "suppressed").split(":")[0].split(" ")[0]
            result.by_reason[reason] = result.by_reason.get(reason, 0) + 1
    return result


def estimate_before_after(
    events: Iterable[dict[str, Any]], old: TuningConfig, new: TuningConfig
) -> dict[str, Any]:
    """Before/after FP estimate for a proposed tuning change."""
    materialized = list(events)
    before = estimate_effect(materialized, old)
    after = estimate_effect(materialized, new)
    return {
        "before": before.to_dict(),
        "after": after.to_dict(),
        "newly_suppressed": after.would_suppress - before.would_suppress,
    }
