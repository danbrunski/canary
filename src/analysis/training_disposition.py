"""Four-tier training-data disposition classifier (Task 1.4,
online-training-strategy.md).

Every candidate training/scoring cell resolves to one of the doc's four
tiers (see "The disposition pipeline"):

| Disposition      | Condition                                                    |
|------------------|---------------------------------------------------------------|
| Reject           | outside FSM *physical* limits / decode failure                 |
| Accept-recenter  | outside this-engine baseline **but** covered by an override    |
| Quarantine       | outside this-engine baseline, no override                      |
| Accept           | within this-engine baseline                                    |

**Ordering** (the doc's "regime-gate-then-disposition ordering", also see
the "Concrete design" pipeline diagram): the *regime gate*
(``src.preprocessing.segmenter`` — is this row even the right operating
mode for the model in question?) and the *data-quality gate*
(``src.preprocessing.quality_gate`` — is this cell physically possible?)
both run **before** this module. :func:`classify_dispositions` takes an
already regime-gated (has an ``operating_mode`` column) and already
quality-gated (physically-impossible cells already NaN'd) frame, plus the
gate's own reject counts (attributed to the Reject tier here, not
re-detected — a cell the gate already NaN'd is indistinguishable from any
other missing cell by the time it reaches this module, by design: Task 1.2
routes it through the same missingness machinery as everything else). Reject
counts come from the quality gate (whole-frame, pre-segmentation) so they
are reported per channel only, never double-counted or split across modes.

"This-engine baseline" (the doc's "three normals", #2) has no persisted
model yet in Phase 1 — that's what a trained detector *becomes*, and
Phase 1 is explicitly "no auto-retrain yet". This module's empirical proxy
is a per-``(channel, operating_mode)`` robust band — ``median ±
baseline_sigma × (1.4826 × MAD)`` — computed directly from the surviving
(non-rejected) values in the frame at hand. Median/MAD (not mean/std, and
not a plain quantile band) deliberately: this same frame is exactly where
the outliers being screened for might live, and a plain top/bottom-quantile
cutoff is itself dragged by them (a handful of gross outliers pull
``quantile(0.99)`` up to meet them, hiding the very contamination the band
exists to catch). Median/MAD's ~50% breakdown point keeps the band anchored
to the bulk of the data regardless. Honest about being a screening
heuristic ("does the four-tier model even hold on real data?", per the
doc's own framing of Phase 1), not a claim that it's *the* baseline a later
retrained model would learn.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from typing import Any

import pandas as pd

from src.decode.accepted_baselines import AcceptedBaselines

#: Rows in a (channel, operating_mode) group below this count can't support
#: a meaningful quantile band — treat every value in that group as Accept
#: rather than manufacture a quarantine flag off a handful of samples.
MIN_BASELINE_SAMPLES = 20

#: Default robust-sigma multiple for the "this-engine baseline" band
#: (``median ± baseline_sigma × scaled_MAD``). 6 is deliberately generous —
#: this is a screening heuristic flagging "persistently outside", not a
#: statistical-process-control chart; a tighter multiple would flood the
#: disposition report with routine variation.
DEFAULT_BASELINE_SIGMA = 6.0

#: 1.4826 makes MAD a consistent estimator of the standard deviation for a
#: normal distribution — the standard scaling factor.
_MAD_TO_SIGMA = 1.4826

_STRUCTURAL_COLUMNS = frozenset({"session", "timestamp", "operating_mode"})


@dataclass(frozen=True)
class ChannelDisposition:
    """One channel's Accept-recenter/Quarantine/Accept tier counts + the
    empirical baseline band it was screened against, for one
    ``(channel, operating_mode)`` group (``operating_mode`` is ``None`` when
    the input frame carries no mode column). Reject counts are NOT part of
    this — they're whole-frame, pre-segmentation, and live on
    :attr:`DispositionSummary.reject_counts` instead, keyed by channel only.
    """

    channel: str
    operating_mode: str | None
    accept_recenter: int
    quarantine: int
    accept: int
    baseline_low: float | None
    baseline_high: float | None

    @property
    def total(self) -> int:
        return self.accept_recenter + self.quarantine + self.accept

    def to_dict(self) -> dict[str, Any]:
        return {
            "channel": self.channel,
            "operating_mode": self.operating_mode,
            "accept_recenter": self.accept_recenter,
            "quarantine": self.quarantine,
            "accept": self.accept,
            "total": self.total,
            "baseline_low": self.baseline_low,
            "baseline_high": self.baseline_high,
        }


@dataclass(frozen=True)
class DispositionSummary:
    """Full disposition breakdown for one screening run — per
    ``(channel, operating_mode)`` group, plus per-channel Reject counts from
    the upstream quality gate, plus corpus-wide totals per tier."""

    groups: tuple[ChannelDisposition, ...] = field(default_factory=tuple)
    reject_counts: dict[str, int] = field(default_factory=dict)

    @property
    def totals(self) -> dict[str, int]:
        totals = {
            "reject": sum(self.reject_counts.values()),
            "accept_recenter": 0,
            "quarantine": 0,
            "accept": 0,
        }
        for group in self.groups:
            totals["accept_recenter"] += group.accept_recenter
            totals["quarantine"] += group.quarantine
            totals["accept"] += group.accept
        return totals

    def to_dict(self) -> dict[str, Any]:
        return {
            "groups": [g.to_dict() for g in self.groups],
            "reject_counts": dict(self.reject_counts),
            "totals": self.totals,
        }


def classify_dispositions(
    gated_frame: pd.DataFrame,
    *,
    reject_counts: Mapping[str, int],
    channels: Sequence[str] | None = None,
    overrides: AcceptedBaselines | None = None,
    operating_mode_column: str = "operating_mode",
    baseline_sigma: float = DEFAULT_BASELINE_SIGMA,
    min_baseline_samples: int = MIN_BASELINE_SAMPLES,
) -> DispositionSummary:
    """Classify every value in ``gated_frame`` into the four-tier table.

    ``gated_frame`` must already have passed the regime gate (an
    ``operating_mode`` column, unless ``operating_mode_column`` is absent
    from the frame entirely, in which case every row is treated as one
    ungrouped baseline) and the quality gate (``apply_quality_gate`` — its
    reject counts are supplied via ``reject_counts``, not recomputed here).

    ``channels`` restricts which columns are screened; defaults to every
    numeric column that isn't structural (session/timestamp/mode).
    """
    has_mode = operating_mode_column in gated_frame.columns
    columns = list(channels) if channels is not None else _numeric_channels(gated_frame)

    groups: list[ChannelDisposition] = []
    for channel in columns:
        if channel not in gated_frame.columns:
            continue
        series = pd.to_numeric(gated_frame[channel], errors="coerce")
        modes: Sequence[Any]
        if has_mode:
            modes = sorted(gated_frame[operating_mode_column].dropna().unique().tolist())
        else:
            modes = [None]
        for mode in modes:
            mask = (
                (gated_frame[operating_mode_column] == mode)
                if has_mode
                else pd.Series(True, index=gated_frame.index)
            )
            values = series[mask].dropna()
            groups.append(
                _classify_one_group(
                    channel=channel,
                    mode=mode,
                    values=values,
                    overrides=overrides,
                    baseline_sigma=baseline_sigma,
                    min_baseline_samples=min_baseline_samples,
                )
            )
    return DispositionSummary(
        groups=tuple(groups),
        reject_counts={str(k): int(v) for k, v in reject_counts.items()},
    )


def _classify_one_group(
    *,
    channel: str,
    mode: Any,
    values: pd.Series,
    overrides: AcceptedBaselines | None,
    baseline_sigma: float,
    min_baseline_samples: int,
) -> ChannelDisposition:
    mode_str = None if mode is None else str(mode)
    if len(values) < min_baseline_samples:
        # Not enough data to establish a baseline — everything present
        # counts as Accept rather than manufacturing a quarantine flag off
        # a handful of samples (see MIN_BASELINE_SAMPLES).
        return ChannelDisposition(
            channel=channel,
            operating_mode=mode_str,
            accept_recenter=0,
            quarantine=0,
            accept=int(len(values)),
            baseline_low=None,
            baseline_high=None,
        )

    median = float(values.median())
    scaled_mad = _MAD_TO_SIGMA * float((values - median).abs().median())
    if scaled_mad <= 0:
        # Quantization-flat data (every surviving value identical, or a
        # single-LSB stair-step) — same degenerate case DeviationConfig's
        # sigma_floor guards against. Nothing legitimately varies, so any
        # different value is unambiguously outside, without a divide.
        outside = values != median
        low = high = median
    else:
        low = median - baseline_sigma * scaled_mad
        high = median + baseline_sigma * scaled_mad
        outside = (values < low) | (values > high)
    n_outside = int(outside.sum())
    n_inside = int(len(values) - n_outside)

    override = overrides.active_override_for(channel, mode_str) if overrides is not None else None
    accept_recenter = n_outside if override is not None else 0
    quarantine = 0 if override is not None else n_outside

    return ChannelDisposition(
        channel=channel,
        operating_mode=mode_str,
        accept_recenter=accept_recenter,
        quarantine=quarantine,
        accept=n_inside,
        baseline_low=low,
        baseline_high=high,
    )


def _numeric_channels(frame: pd.DataFrame) -> list[str]:
    return [
        str(col)
        for col in frame.columns
        if str(col) not in _STRUCTURAL_COLUMNS
        and not str(col).endswith("__gap")
        and pd.api.types.is_numeric_dtype(frame[col])
    ]


__all__ = [
    "DEFAULT_BASELINE_SIGMA",
    "MIN_BASELINE_SAMPLES",
    "ChannelDisposition",
    "DispositionSummary",
    "classify_dispositions",
]
