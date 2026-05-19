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
One detector at its boundary sits at the emit edge; agreement pushes it
decisively over. ``is_anomaly`` is ``severity >= severity_threshold``.

Kept narrow and dependency-light (no sklearn / torch / pandas) so the daemon
can fuse without touching the model layers — same discipline as the emitter.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from src.config import FusionConfig

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
    """

    name: str
    score: float
    threshold: float | None
    fired: bool
    severity: float


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


class FusionScorer:
    """Combine per-tick detector outputs into one calibrated severity."""

    def __init__(self, config: FusionConfig) -> None:
        self._config = config

    @property
    def enabled(self) -> bool:
        return self._config.enabled

    def behavior_source(self, name: str, severity_band: str) -> FusionSource:
        """A behavior-residual source from a graded deviation band.

        The band is the behavior decoder's own calibrated verdict, so its
        configured severity *is* the magnitude — no scale needed. ``"ok"``
        (or anything unrecognized) does not fire.
        """
        if severity_band == "critical":
            sev = self._config.behavior_critical_severity
        elif severity_band == "warning":
            sev = self._config.behavior_warning_severity
        else:
            sev = 0.0
        fired = sev > 0.0
        return FusionSource(name, sev, 0.0 if fired else None, fired, sev if fired else 0.0)

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
        ``is_anomaly=False``.
        """
        present = tuple(sources)
        firing = [s for s in present if s.fired]
        if not firing:
            return FusedResult(0.0, False, operating_mode, timestamp, present, ())
        base = max(s.severity for s in firing)
        boost = self._config.agreement_boost * (len(firing) - 1)
        severity = _bounded(base + boost)
        return FusedResult(
            severity=severity,
            is_anomaly=severity >= self._config.severity_threshold,
            operating_mode=operating_mode,
            timestamp=timestamp,
            sources=present,
            contributing=tuple(s.name for s in firing),
        )


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
                }
                for s in self.sources
            },
        }


__all__ = [
    "FusionScorer",
    "FusionSource",
    "FusedResult",
    "isolation_forest_source",
    "lstm_source",
]
