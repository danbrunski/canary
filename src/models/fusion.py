"""Per-tick anomaly fusion (Phase 7.2).

The Isolation Forest, the LSTM autoencoder, and the behavior-model residuals
each answer a different question about the same instant. Historically they
emitted two independent, never-combined streams (and the residuals weren't an
alert source at all), with IF's fixed ``contamination`` structurally flagging
~1% of *nominal* driving. This module fuses whatever sources are available on
a tick into one **agreement-aware, calibration-relative severity** in
``[0, 1]``.

A source *fires* only when its score crosses its own *calibrated* threshold.
A firing source's contribution is a bounded ``[0.5, 1.0]`` magnitude built
from whatever calibrated scale that detector has:

* **Isolation Forest** — the 7.2 held-out per-mode threshold plus the spread
  of the held-out nominal scores as the scale. (IF's negated
  decision-function threshold is typically a small *negative* number, so a
  threshold-relative ratio would pathologically saturate; the held-out spread
  is the correct, non-degenerate scale.)
* **LSTM autoencoder** — its global reconstruction threshold, a positive
  error, so severity grows from the boundary toward ``2×`` threshold.
* **Behavior residuals** — the graded deviation band's configured severity.

The fused severity is the strongest single contribution plus an
``agreement_boost`` for every *additional* agreeing source, clamped to 1.0.

**6.2 — fusion is a real agreement gate, not OR-with-boost.**
(Operator-approved decision, see ``OVERNIGHT-QUEUE.md``'s "Fusion
semantics".) ``is_anomaly`` requires BOTH ``severity >= severity_threshold``
AND ``agreement >= min_agreement`` (independent-group count, default 2) —
one detector firing alone, however severe, is corroborated by nothing and
never sets the fused verdict; it still emits its own per-source event
through the unrelated per-source emit paths (IF/LSTM/behavior events are
never suppressed by fusion — no double-emit *suppression*, just no
false "agreement" from a single voice). Sibling surfaces sharing a
``group`` still collapse to one independent voice for both the boost and
the gate, same as before.

**A06.5 — detector roles.** A source whose detector is configured
``evidence`` (``models.roles``) still fires, still counts as an independent
voice for ``agreement`` and the boost, and is recorded as such in the fused
event's detail — but it sets no magnitude of its own and cannot carry the
verdict: the fused severity's base is the strongest *alert-role* firing
source, and ``is_anomaly`` additionally requires at least one such source.
Two evidence detectors agreeing is recorded (``agreement`` 2, severity 0),
never alerted. With every role ``alert`` (the default) nothing changes.

Kept narrow and dependency-light (no sklearn / torch / pandas) so the daemon
can fuse without touching the model layers — same discipline as the emitter.
"""

from __future__ import annotations

import time
from collections import deque
from collections.abc import Mapping
from dataclasses import dataclass, replace
from typing import Any

from src.config import DetectorRole, FusionConfig

_EPS = 1e-9
#: Severity for a fired source that has neither a calibrated scale nor a
#: positive threshold to size the exceedance against (a degenerate fallback,
#: not a user knob): "it fired, magnitude unknown — treat as clearly over."
_UNSCALED_FIRED_SEVERITY = 0.75


@dataclass(frozen=True)
class FusionSource:
    """One detector's contribution to the current tick.

    ``fired`` and ``severity`` are computed by the builder functions below so
    :class:`FusionScorer` stays a pure combiner. A source with no calibrated
    threshold is represented with ``fired=False, severity=0.0`` — it is kept
    for evidence/observability but can never inflate the fused severity.

    ``group`` declares shared evidence: sources in the same group (e.g. two
    behavior surfaces grading the same output channel) count as ONE agreeing
    voice for the agreement boost — the boost models *independent*
    corroboration, and sibling surfaces firing on the same residual aren't
    independent. ``None`` ⇒ the source is its own group.
    """

    name: str
    score: float
    threshold: float | None
    fired: bool
    severity: float
    group: str | None = None
    #: A06.5 — ``evidence`` sources vote in agreement but cannot carry the
    #: fused verdict. Resolved by :meth:`FusionScorer.fuse` from its
    #: configured roles; a builder never needs to set it.
    role: DetectorRole = "alert"


@dataclass
class BandExcessAccumulator:
    """Two-sided CUSUM of a residual's excess beyond its warning half-width.

    The arithmetic of ``BandExcessPersistence`` (B05.3,
    ``src.behavior.behavior_models``), in one place for the daemon's gate, the
    offline evaluator and the training-fold fit: ``high`` accumulates
    ``r - w`` and ``low`` accumulates ``-r - w``, each floored at zero. Call
    :meth:`update` only for evaluated ticks; a gap longer than
    ``reset_gap_seconds`` since the previous evaluated tick restarts both sums.
    """

    reset_gap_seconds: float
    high: float = 0.0
    low: float = 0.0
    last_seconds: float | None = None

    @property
    def statistic(self) -> float:
        return max(self.high, self.low)

    def update(self, residual: float, half_width: float, now: float) -> float:
        """Add one evaluated tick; return the accumulated excess after it."""
        if self.last_seconds is not None and now - self.last_seconds > self.reset_gap_seconds:
            self.high = self.low = 0.0
        self.last_seconds = now
        self.high = max(0.0, self.high + residual - half_width)
        self.low = max(0.0, self.low - residual - half_width)
        return self.statistic


class PersistenceGate:
    """Require M-of-N firing ticks (per key) within a wall-clock-bound window.

    The structural fix for the alert-flood class: every detector here scores
    at ~1 Hz, so a single-tick blip (sensor glitch, round-robin sampling
    skew, one bad window) otherwise becomes an event AND a fusion source
    instantly. Real faults the system exists to catch — vacuum leaks, knock
    retard, thermostat failures — persist across ticks; a lone tick is noise.

    **6.1 — M-of-N, not strict N-consecutive.** The old gate reset its streak
    to zero on any single non-firing tick, so a K=3-consecutive gate at 1 Hz
    structurally could never latch on an intermittent fault that fires,
    gaps, fires (a real pattern for e.g. a marginal connector). This gate
    instead keeps the trailing ``window`` (N) observed ticks per key and
    latches once ``required`` (M) of them fired — ``window`` defaults to
    ``required`` when not given, which reproduces the old strict-consecutive
    behavior exactly (M-of-M), so existing single-argument callers are
    unaffected; pass a looser ``window`` (e.g. ``required=3, window=5`` for
    "3-of-5") to tolerate flicker without weakening the M hits still needed
    for flood protection.

    **Wall-clock bound.** ``window_seconds`` (T), when given, additionally
    evicts any entry older than T seconds from *now* — whichever bound (N
    ticks or T seconds) is tighter wins. Without this a data gap (replay
    stop/start, a stalled queue, the daemon catching up after a stall) could
    let stale hits from well before the gap count toward a "fresh" streak
    once ticks resume; the eviction makes that structurally impossible.

    **Missing ticks (``firing=None``).** A tick where the detector produced
    no trustworthy verdict at all — IF's missing-feature blindness, a
    behavior model that's off-mode this tick, a sequence detector whose
    window buffer isn't full yet — is not "the detector looked and didn't
    fire" (a real miss, which correctly displaces a stale hit from the
    window). It's "the detector didn't get to look." The least-surprising
    reading is a genuine no-op: :meth:`observe` leaves the window (and its
    wall-clock bound) completely untouched and returns the unchanged
    sustained state, so a run of missing ticks freezes rather than decays
    the existing streak. This was already the pre-6.1 behavior for IF
    (callers simply skipped calling ``observe`` on a blind tick); 6.1
    extends the same semantics uniformly to every path via one explicit
    ``None`` argument instead of each caller improvising its own skip logic.

    Keys are detector names (``"isolation_forest"``, ``"lstm_autoencoder"``,
    ``"behavior:<model>"``) so each detector latches independently. Call
    :meth:`observe` at most once per key per tick; :meth:`active` reads the
    latched state without advancing it (safe to call any time after the same
    tick's ``observe`` for that key — it does not re-apply the wall-clock
    trim against a *later* "now").
    """

    def __init__(
        self,
        required: int,
        *,
        window: int | None = None,
        window_seconds: float | None = None,
    ) -> None:
        self._required = max(1, int(required))
        self._window = max(self._required, int(window) if window is not None else self._required)
        self._window_seconds = window_seconds
        self._history: dict[str, deque[tuple[float, bool]]] = {}
        #: B05.3 — per-key band-excess accumulators for models that carry
        #: their own fitted persistence (:meth:`observe_band_excess`). Kept
        #: on the gate so :meth:`reset` clears them at a replay boundary too.
        self._excess: dict[str, tuple[BandExcessAccumulator, float]] = {}
        self.suppressed = 0

    @property
    def required(self) -> int:
        return self._required

    def observe(self, key: str, firing: bool | None, *, now: float | None = None) -> bool:
        """Advance ``key``'s M-of-N window with this tick's verdict; return
        whether the detector is sustained (hits within the window ≥
        required).

        ``firing=None`` is a missing-verdict tick (see class docstring): the
        window is left untouched. ``now`` defaults to :func:`time.monotonic`
        so real-time callers (the live daemon) get the wall-clock bound for
        free; callers replaying historical data (the synthetic-fault
        harness) pass the row's own timestamp so the bound reflects data
        time, not however fast the replay loop happens to run.
        """
        # A model hot-reloaded from its own band-excess persistence back to the
        # M-of-N gate must not keep answering ``active`` from the stale sums.
        self._excess.pop(key, None)
        if firing is None:
            return self.active(key)
        clock = now if now is not None else time.monotonic()
        history = self._history.setdefault(key, deque())
        history.append((clock, firing))
        self._trim(history, clock)
        sustained = self._hits(history) >= self._required
        if firing and not sustained:
            self.suppressed += 1
        return sustained

    def active(self, key: str) -> bool:
        """Whether ``key`` is currently latched (read-only, no trimming)."""
        excess = self._excess.get(key)
        if excess is not None:
            accumulator, limit = excess
            return accumulator.statistic > limit
        history = self._history.get(key)
        if not history:
            return False
        return self._hits(history) >= self._required

    def observe_band_excess(
        self,
        key: str,
        residual: float | None,
        half_width: float | None,
        *,
        firing: bool | None,
        limit: float,
        reset_gap_seconds: float,
        now: float,
    ) -> bool:
        """Advance ``key``'s band-excess accumulation (B05.3); return sustained.

        The M-of-N window is not used for this key: a model with a fitted
        ``BandExcessPersistence`` is sustained while its accumulated excess
        is strictly above ``limit``. ``firing=None`` (or no residual / band)
        is a missing verdict and leaves the sums untouched, exactly like
        :meth:`observe`. A changed ``limit`` or gap (a hot-reloaded model)
        restarts the key's accumulation rather than judging old sums by it.
        """
        entry = self._excess.get(key)
        if entry is None or entry[1] != limit or entry[0].reset_gap_seconds != reset_gap_seconds:
            entry = (BandExcessAccumulator(reset_gap_seconds), limit)
            self._excess[key] = entry
        accumulator = entry[0]
        if firing is None or residual is None or half_width is None:
            return accumulator.statistic > limit
        sustained = accumulator.update(residual, half_width, now) > limit
        if firing and not sustained:
            self.suppressed += 1
        return sustained

    def reset(self) -> None:
        """Clear every key's window (Phase 7.1 replay isolation boundary).

        A live drive's in-progress streak must not carry into a replay run
        (and vice versa on the way back out) — called on both
        ``enter_replay_mode`` and ``exit_replay_mode`` so neither direction
        leaks a partial M-of-N streak across the boundary.
        """
        self._history.clear()
        self._excess.clear()
        self.suppressed = 0

    def _trim(self, history: deque[tuple[float, bool]], now: float) -> None:
        while len(history) > self._window:
            history.popleft()
        if self._window_seconds is not None:
            cutoff = now - self._window_seconds
            while history and history[0][0] < cutoff:
                history.popleft()

    @staticmethod
    def _hits(history: deque[tuple[float, bool]]) -> int:
        return sum(1 for _, hit in history if hit)


def downgrade_unsustained(source: FusionSource, sustained: bool) -> FusionSource:
    """Downgrade a fired-but-not-yet-sustained source to non-firing.

    The downgraded source stays in the evidence list (score visible on the
    fused event) but can't inflate fused severity or count as agreement.
    """
    if source.fired and not sustained:
        return replace(source, fired=False, severity=0.0)
    return source


def _bounded(value: float) -> float:
    return max(0.0, min(1.0, value))


def isolation_forest_source(
    score: float | None,
    threshold: float | None,
    *,
    scale: float | None,
    name: str = "isolation_forest",
) -> FusionSource:
    """IF source. ``scale`` is the spread of the held-out nominal scores
    (``ModeTrainingSummary.score_scale``); severity rises from ``0.5`` at the
    threshold to ``1.0`` one scale beyond it. Falls back to the unscaled
    fired severity when no scale/threshold is available (legacy / uncalibrated
    mode → cannot meaningfully fire)."""
    if score is None or threshold is None:
        return FusionSource(name, float(score or 0.0), threshold, False, 0.0)
    fired = score >= threshold
    if not fired:
        return FusionSource(name, score, threshold, False, 0.0)
    if scale is not None and scale > _EPS:
        severity = 0.5 + 0.5 * _bounded((score - threshold) / scale)
    else:
        severity = _UNSCALED_FIRED_SEVERITY
    return FusionSource(name, score, threshold, True, severity)


def lstm_source(
    score: float | None,
    threshold: float | None,
    *,
    name: str = "lstm_autoencoder",
) -> FusionSource:
    """LSTM source. The global reconstruction threshold is a positive error,
    so a threshold-relative exceedance is well-formed: ``0.5`` at the
    boundary, ``1.0`` at twice the threshold."""
    if score is None or threshold is None:
        return FusionSource(name, float(score or 0.0), threshold, False, 0.0)
    fired = score >= threshold
    if not fired:
        return FusionSource(name, score, threshold, False, 0.0)
    if threshold > _EPS:
        severity = 0.5 + 0.5 * _bounded((score - threshold) / threshold)
    else:
        severity = _UNSCALED_FIRED_SEVERITY
    return FusionSource(name, score, threshold, True, severity)


#: B04.8 / B05.3 — behavior outputs that re-express one measurement. A
#: behavior residual votes in fusion as its output's *measurement family*, not
#: as its output channel: ``derived:AIR_MASS_PER_CYCLE`` and the VE channels
#: are the same MAF/RPM reading divided by different references, and total
#: trim, bank balance and the per-bank trims are the same closed-loop fuel
#: correction. Grouping by output channel let one MAF excursion (or one trim
#: excursion) count as two or three agreeing voices. Keyed by channel prefix
#: over platform names (Mode 01 PIDs and ``derived:`` channels), never a
#: vehicle's DIDs.
BEHAVIOR_MEASUREMENT_FAMILIES: dict[str, tuple[str, ...]] = {
    "airflow": (
        "obd:MAF",
        "derived:AIR_MASS_PER_CYCLE",
        "derived:VE_",
    ),
    "fuel_trim": (
        "obd:SHORT_FUEL_TRIM_",
        "obd:LONG_FUEL_TRIM_",
        "derived:TOTAL_TRIM_",
        "derived:STFT_BANK_BALANCE",
        "derived:LTFT_BANK_BALANCE",
    ),
}


def behavior_fusion_group(channel: str) -> str:
    """The fusion group a behavior residual on ``channel`` votes in.

    Its measurement family when it belongs to one, else the output channel
    itself (sibling surfaces grading one channel still share a voice).
    """
    for family, prefixes in BEHAVIOR_MEASUREMENT_FAMILIES.items():
        if any(channel == prefix or channel.startswith(prefix) for prefix in prefixes):
            return f"behavior:{family}"
    return f"behavior:{channel}"


class FusionScorer:
    """Combine per-tick detector outputs into one calibrated severity."""

    def __init__(
        self, config: FusionConfig, *, roles: Mapping[str, DetectorRole] | None = None
    ) -> None:
        self._config = config
        #: A06.5 — role per source name (``DetectorRolesConfig.by_source``);
        #: an unnamed source (behavior residuals) is ``alert``.
        self._roles: dict[str, DetectorRole] = dict(roles or {})

    @property
    def enabled(self) -> bool:
        return self._config.enabled

    def behavior_source(self, name: str, severity_band: str, *, channel: str) -> FusionSource:
        """A behavior-residual source from a graded deviation band.

        The band is the behavior decoder's own calibrated verdict, so its
        configured severity *is* the magnitude — no scale needed. ``"ok"``
        (or anything unrecognized) does not fire. ``channel`` is the graded
        output channel; the group is derived here
        (:func:`behavior_fusion_group`), so no caller can make two transforms
        of one measurement vote as independent agreement.
        """
        group = behavior_fusion_group(channel)
        if severity_band == "critical":
            sev = self._config.behavior_critical_severity
        elif severity_band == "warning":
            sev = self._config.behavior_warning_severity
        else:
            sev = 0.0
        fired = sev > 0.0
        return FusionSource(
            name, sev, 0.0 if fired else None, fired, sev if fired else 0.0, group=group
        )

    def fuse(
        self,
        sources: list[FusionSource],
        *,
        timestamp: str,
        operating_mode: str | None,
    ) -> FusedResult:
        """Fuse the available sources for one tick.

        Non-firing / unthresholded sources are retained in ``sources`` for
        evidence but never inflate severity. Nothing firing ⇒ severity ``0`` /
        ``is_anomaly=False``. 6.2: ``is_anomaly`` additionally requires
        ``min_agreement`` independent firing sources — see module docstring.
        """
        present = tuple(self._with_role(s) for s in sources)
        firing = [s for s in present if s.fired]
        if not firing:
            return FusedResult(0.0, False, operating_mode, timestamp, present, ())
        # A06.5: only an alert-role source sets the magnitude or can carry
        # the verdict; evidence sources join the agreement count below.
        alerting = [s for s in firing if s.role == "alert"]
        base = max((s.severity for s in alerting), default=0.0)
        # Agreement counts *independent* voices: sources sharing a ``group``
        # (sibling surfaces grading the same output channel) collapse to one
        # before the boost — two timing surfaces over the same excursion are
        # one piece of evidence, not corroboration. The same independent
        # count gates ``is_anomaly`` (6.2) so a lone sibling pair can't pass
        # as "agreement" any more than a single detector can.
        independent_groups = {s.group or s.name for s in firing}
        boost = self._config.agreement_boost * (len(independent_groups) - 1)
        severity = _bounded(base + boost) if alerting else 0.0
        is_anomaly = (
            bool(alerting)
            and severity >= self._config.severity_threshold
            and len(independent_groups) >= self._config.min_agreement
        )
        return FusedResult(
            severity=severity,
            is_anomaly=is_anomaly,
            operating_mode=operating_mode,
            timestamp=timestamp,
            sources=present,
            contributing=tuple(s.name for s in firing),
        )

    def _with_role(self, source: FusionSource) -> FusionSource:
        """Apply the configured role; never promotes an evidence source."""
        if source.role == "evidence" or self._roles.get(source.name, "alert") == "alert":
            return source
        return replace(source, role="evidence")


@dataclass(frozen=True)
class FusedResult:
    """Outcome of fusing one tick's available sources."""

    severity: float
    is_anomaly: bool
    operating_mode: str | None
    timestamp: str
    sources: tuple[FusionSource, ...]
    contributing: tuple[str, ...]

    @property
    def agreement(self) -> int:
        return len(self.contributing)

    def to_detail(self) -> dict[str, Any]:
        """Per-source breakdown carried in the fused ``AnomalyEvent.detail``
        — the seed of 7.2a's richer attribution payload."""
        return {
            "fused_severity": self.severity,
            "agreement": self.agreement,
            "contributing_sources": list(self.contributing),
            "sources": {
                s.name: {
                    "score": s.score,
                    "threshold": s.threshold,
                    "fired": s.fired,
                    "severity": s.severity,
                    "role": s.role,
                }
                for s in self.sources
            },
        }


__all__ = [
    "BandExcessAccumulator",
    "FusionScorer",
    "FusionSource",
    "FusedResult",
    "PersistenceGate",
    "downgrade_unsustained",
    "isolation_forest_source",
    "lstm_source",
]
