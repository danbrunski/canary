"""Four-tier training-data disposition classifier (Task 1.4,
online-training-strategy.md; channel-type-aware screening + golden-anchored
baselines added by the 2026-07-20 review's Tasks 1.6/3.7).

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
is, per ``(channel, operating_mode)`` group, one of two shapes (Task 1.6):

* **Continuous** — a robust band, ``median ± baseline_sigma × (1.4826 ×
  MAD)``, computed from the surviving (non-rejected) values in the frame at
  hand. Median/MAD (not mean/std, and not a plain quantile band)
  deliberately: this same frame is exactly where the outliers being
  screened for might live, and a plain top/bottom-quantile cutoff is itself
  dragged by them. Median/MAD's ~50% breakdown point keeps the band
  anchored to the bulk of the data regardless.
* **Discrete** — engine-state bytes, steering bitmasks, gear numbers: a
  median/MAD band collapses to a single point for these (their whole range
  is a handful of legitimate states), manufacturing a Quarantine flood that
  buries the continuous-channel signal (confirmed on the real corpus —
  see the 1.5 report this fix supersedes). The baseline is instead the
  observed value *set*: membership → Accept, an unseen value may still
  Quarantine. Detection of "discrete" is data-driven — :func:`_is_discrete`
  — never a hand-listed channel roster (a vehicle-specific fact that has no
  business in shared code, per this repo's own convention).

Both shapes are honest about being a screening heuristic ("does the
four-tier model even hold on real data?", per the doc's own framing of
Phase 1), not a claim that it's *the* baseline a later retrained model
would learn — **unless** a golden run has frozen one (Task 3.7): once
``golden_baselines`` is supplied, per-``(channel, operating_mode)`` groups
it covers are screened against *those* frozen statistics instead of
recomputing from the window at hand. This is the fix for the
self-referential-baseline blind spot the 2026-07-20 review identified —
median±MAD (or a discrete value set) computed from the same window being
screened can't catch a fault that dominates that window, because the fault
becomes the new "normal". Window-local remains the fallback: both when no
``golden_baselines`` is given at all, and per-group when a specific
``(channel, operating_mode)`` isn't covered by the frozen set (e.g. a
channel added to the corpus after golden was declared).
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from typing import Any

import pandas as pd

from src.decode.accepted_baselines import AcceptedBaselines
from src.preprocessing.resampler import is_flag_column

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

#: sqrt(pi/2): makes the mean absolute deviation a consistent estimator of
#: the standard deviation for a normal distribution (the MAD = 0 fallback of
#: Iglewicz & Hoaglin's modified z-score).
_MEANAD_TO_SIGMA = 1.2533


def robust_spread(values: pd.Series, median: float) -> float:
    """Scaled MAD, or — when MAD is 0 — the scaled mean absolute deviation.

    A04.3: MAD is 0 whenever more than half the values sit at one level
    (accelerator pedal at rest, a fan off, a trim clamped at 0). A band of
    ``median ± 6 × 0`` then quarantined *every* non-median value: 4.5 % of
    cells, but 71.6 % of the IF's training rows, leaving it only the moments
    when everything sits at its median. The mean absolute deviation is 0 only
    when every value is identical, and then there is nothing to quarantine.
    """
    deviation = (values.astype(float) - median).abs()
    scaled_mad = _MAD_TO_SIGMA * float(deviation.median())
    if scaled_mad > 0:
        return scaled_mad
    return _MEANAD_TO_SIGMA * float(deviation.mean())


_STRUCTURAL_COLUMNS = frozenset({"session", "timestamp", "operating_mode"})

#: A (channel, mode) group with at most this many distinct values is
#: discrete regardless of sample count — real engine-state bytes/bitmasks
#: sit in a handful of legitimate states no matter how long the drive.
DEFAULT_DISCRETE_MAX_UNIQUE_VALUES = 12

#: Above the absolute cap, a group is *still* discrete when every value is
#: integer-valued AND distinct values are a small fraction of the sample
#: count — catches a wider-but-still-enumerable state channel (e.g. a gear
#: number 1-6 sampled for hours) without misclassifying a continuous sensor
#: that merely happens to report whole numbers (RPM, raw ADC counts) and
#: has thousands of genuinely distinct readings.
DEFAULT_DISCRETE_MAX_UNIQUE_FRACTION = 0.05


@dataclass(frozen=True)
class ChannelDisposition:
    """One channel's Accept-recenter/Quarantine/Accept tier counts + the
    empirical baseline it was screened against, for one
    ``(channel, operating_mode)`` group (``operating_mode`` is ``None`` when
    the input frame carries no mode column). Reject counts are NOT part of
    this — they're whole-frame, pre-segmentation, and live on
    :attr:`DispositionSummary.reject_counts` instead, keyed by channel only.

    ``baseline_low``/``baseline_high`` describe the continuous-channel band;
    ``baseline_values`` (Task 1.6) is the discrete channel's observed value
    set instead — exactly one of the two is populated (both ``None`` only
    when the group was too sparse to establish either, see
    ``MIN_BASELINE_SAMPLES``).
    """

    channel: str
    operating_mode: str | None
    accept_recenter: int
    quarantine: int
    accept: int
    baseline_low: float | None
    baseline_high: float | None
    is_discrete: bool = False
    baseline_values: tuple[float, ...] | None = None

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
            "is_discrete": self.is_discrete,
            "baseline_values": (
                list(self.baseline_values) if self.baseline_values is not None else None
            ),
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


@dataclass(frozen=True)
class ChannelBaseline:
    """One frozen ``(channel, operating_mode)`` baseline (Task 3.7) — the
    statistics a golden run's input sessions produced, captured at
    declaration time so a later candidate window is screened against a fixed
    reference instead of its own (self-referential) statistics.

    Exactly one of ``(median, scaled_mad)`` / ``baseline_values`` is
    populated, matching :class:`ChannelDisposition`'s continuous/discrete
    split — ``is_discrete`` says which.
    """

    channel: str
    operating_mode: str | None
    is_discrete: bool
    median: float | None = None
    scaled_mad: float | None = None
    baseline_values: tuple[float, ...] | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "channel": self.channel,
            "operating_mode": self.operating_mode,
            "is_discrete": self.is_discrete,
            "median": self.median,
            "scaled_mad": self.scaled_mad,
            "baseline_values": (
                list(self.baseline_values) if self.baseline_values is not None else None
            ),
        }

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> ChannelBaseline:
        raw_values = data.get("baseline_values")
        return cls(
            channel=str(data["channel"]),
            operating_mode=(
                None if data.get("operating_mode") is None else str(data["operating_mode"])
            ),
            is_discrete=bool(data.get("is_discrete", False)),
            median=(None if data.get("median") is None else float(data["median"])),
            scaled_mad=(None if data.get("scaled_mad") is None else float(data["scaled_mad"])),
            baseline_values=(None if raw_values is None else tuple(raw_values)),
        )


@dataclass(frozen=True)
class GoldenBaselines:
    """Frozen per-``(channel, operating_mode)`` baselines for one golden run
    (Task 3.7). JSON-safe; persisted by ``src.models.golden_baselines``
    alongside the training ledger, re-frozen every time the operator
    re-declares golden for that ``model_type``."""

    baselines: tuple[ChannelBaseline, ...] = field(default_factory=tuple)

    def get(self, channel: str, operating_mode: str | None) -> ChannelBaseline | None:
        """The frozen baseline for this exact ``(channel, operating_mode)``,
        or ``None`` when golden's own input sessions never covered it (the
        caller's documented fallback: screen that group window-local)."""
        return next(
            (
                b
                for b in self.baselines
                if b.channel == channel and b.operating_mode == operating_mode
            ),
            None,
        )

    def to_dict(self) -> dict[str, Any]:
        return {"baselines": [b.to_dict() for b in self.baselines]}

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> GoldenBaselines:
        return cls(baselines=tuple(ChannelBaseline.from_dict(b) for b in data.get("baselines", [])))


def classify_dispositions(
    gated_frame: pd.DataFrame,
    *,
    reject_counts: Mapping[str, int],
    channels: Sequence[str] | None = None,
    overrides: AcceptedBaselines | None = None,
    operating_mode_column: str = "operating_mode",
    baseline_sigma: float = DEFAULT_BASELINE_SIGMA,
    min_baseline_samples: int = MIN_BASELINE_SAMPLES,
    golden_baselines: GoldenBaselines | None = None,
    discrete_max_unique_values: int = DEFAULT_DISCRETE_MAX_UNIQUE_VALUES,
    discrete_max_unique_fraction: float = DEFAULT_DISCRETE_MAX_UNIQUE_FRACTION,
) -> DispositionSummary:
    """Classify every value in ``gated_frame`` into the four-tier table.

    ``gated_frame`` must already have passed the regime gate (an
    ``operating_mode`` column, unless ``operating_mode_column`` is absent
    from the frame entirely, in which case every row is treated as one
    ungrouped baseline) and the quality gate (``apply_quality_gate`` — its
    reject counts are supplied via ``reject_counts``, not recomputed here).

    ``channels`` restricts which columns are screened; defaults to every
    numeric column that isn't structural (session/timestamp/mode).

    ``golden_baselines`` (Task 3.7), when given, anchors each covered group
    to its frozen golden statistics instead of this window's own — see the
    module docstring's "golden-anchored" section. Uncovered groups (no
    golden run declared, or this specific ``(channel, mode)`` wasn't in
    golden's input) fall back to window-local, unchanged from Phase 1.
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
            mode_str = None if mode is None else str(mode)
            golden_baseline = (
                golden_baselines.get(channel, mode_str) if golden_baselines is not None else None
            )
            groups.append(
                _classify_one_group(
                    channel=channel,
                    mode=mode,
                    values=values,
                    overrides=overrides,
                    baseline_sigma=baseline_sigma,
                    min_baseline_samples=min_baseline_samples,
                    golden_baseline=golden_baseline,
                    discrete_max_unique_values=discrete_max_unique_values,
                    discrete_max_unique_fraction=discrete_max_unique_fraction,
                )
            )
    return DispositionSummary(
        groups=tuple(groups),
        reject_counts={str(k): int(v) for k, v in reject_counts.items()},
    )


def compute_quarantine_mask(
    gated_frame: pd.DataFrame,
    *,
    channels: Sequence[str] | None = None,
    overrides: AcceptedBaselines | None = None,
    operating_mode_column: str = "operating_mode",
    baseline_sigma: float = DEFAULT_BASELINE_SIGMA,
    min_baseline_samples: int = MIN_BASELINE_SAMPLES,
    golden_baselines: GoldenBaselines | None = None,
    discrete_max_unique_values: int = DEFAULT_DISCRETE_MAX_UNIQUE_VALUES,
    discrete_max_unique_fraction: float = DEFAULT_DISCRETE_MAX_UNIQUE_FRACTION,
) -> pd.DataFrame:
    """Cell-level companion to :func:`classify_dispositions` (Task 3.6).

    Same screening (shares :func:`_screen_group` with ``classify_dispositions``
    so the two can never disagree on what's Quarantine), but returns a
    boolean frame — same index as ``gated_frame``, one column per screened
    channel present in it — ``True`` exactly where that cell is Quarantine
    tier (outside its baseline, no covering override). Reject-tier cells are
    already NaN by the time this runs and never read back ``True`` here
    (``dropna()`` excludes them from the screen, same as ``classify_dispositions``).

    ``scripts/retrain.py`` consumes this to NaN those cells out of the
    training matrix (``src.preprocessing.quality_gate.apply_quarantine_gate``
    — the same cell-level, never-a-row-drop mechanism the Reject tier
    already uses).
    """
    has_mode = operating_mode_column in gated_frame.columns
    columns = [c for c in (channels if channels is not None else _numeric_channels(gated_frame))]
    present = [c for c in columns if c in gated_frame.columns]
    mask = pd.DataFrame(False, index=gated_frame.index, columns=present)
    for channel in present:
        series = pd.to_numeric(gated_frame[channel], errors="coerce")
        modes: Sequence[Any]
        if has_mode:
            modes = sorted(gated_frame[operating_mode_column].dropna().unique().tolist())
        else:
            modes = [None]
        for mode in modes:
            row_mask = (
                (gated_frame[operating_mode_column] == mode)
                if has_mode
                else pd.Series(True, index=gated_frame.index)
            )
            values = series[row_mask].dropna()
            mode_str = None if mode is None else str(mode)
            golden_baseline = (
                golden_baselines.get(channel, mode_str) if golden_baselines is not None else None
            )
            screen = _screen_group(
                channel=channel,
                mode=mode,
                values=values,
                overrides=overrides,
                baseline_sigma=baseline_sigma,
                min_baseline_samples=min_baseline_samples,
                golden_baseline=golden_baseline,
                discrete_max_unique_values=discrete_max_unique_values,
                discrete_max_unique_fraction=discrete_max_unique_fraction,
            )
            if screen.override_covers:
                continue  # Accept-recenter, not Quarantine
            quarantine_index = screen.outside[screen.outside].index
            if len(quarantine_index):
                mask.loc[quarantine_index, channel] = True
    return mask


def compute_golden_baselines(
    gated_frame: pd.DataFrame,
    *,
    channels: Sequence[str] | None = None,
    operating_mode_column: str = "operating_mode",
    min_baseline_samples: int = MIN_BASELINE_SAMPLES,
    discrete_max_unique_values: int = DEFAULT_DISCRETE_MAX_UNIQUE_VALUES,
    discrete_max_unique_fraction: float = DEFAULT_DISCRETE_MAX_UNIQUE_FRACTION,
) -> GoldenBaselines:
    """Freeze per-``(channel, operating_mode)`` baseline statistics from a
    golden run's input sessions (Task 3.7) — median/MAD for continuous
    channels, the observed value *set* for discrete ones (Task 1.6) — for
    :func:`classify_dispositions`/:func:`compute_quarantine_mask` to screen
    future candidate windows against instead of recomputing from the window
    itself. This is the fix for the self-referential-baseline blind spot the
    2026-07-20 review identified: a slow drift can't out-vote its own
    median while it's sitting inside the very window being screened.

    A group below ``min_baseline_samples`` is simply omitted (not frozen at
    all) rather than freezing a degenerate baseline off a handful of
    samples — ``GoldenBaselines.get`` returning ``None`` for it means
    callers fall back to window-local screening for that group, same as
    "no golden declared".
    """
    has_mode = operating_mode_column in gated_frame.columns
    columns = list(channels) if channels is not None else _numeric_channels(gated_frame)

    baselines: list[ChannelBaseline] = []
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
            row_mask = (
                (gated_frame[operating_mode_column] == mode)
                if has_mode
                else pd.Series(True, index=gated_frame.index)
            )
            values = series[row_mask].dropna()
            mode_str = None if mode is None else str(mode)
            if len(values) < min_baseline_samples:
                continue
            if _is_discrete(
                values,
                max_unique_values=discrete_max_unique_values,
                max_unique_fraction=discrete_max_unique_fraction,
            ):
                baselines.append(
                    ChannelBaseline(
                        channel=channel,
                        operating_mode=mode_str,
                        is_discrete=True,
                        baseline_values=tuple(sorted(values.unique().tolist())),
                    )
                )
            else:
                median = float(values.median())
                scaled_mad = robust_spread(values, median)
                baselines.append(
                    ChannelBaseline(
                        channel=channel,
                        operating_mode=mode_str,
                        is_discrete=False,
                        median=median,
                        scaled_mad=scaled_mad,
                    )
                )
    return GoldenBaselines(baselines=tuple(baselines))


@dataclass(frozen=True)
class _GroupScreen:
    """Internal: one ``(channel, mode)`` group's screening result, shared by
    ``classify_dispositions`` (aggregates to counts) and
    ``compute_quarantine_mask`` (keeps the per-row boolean)."""

    outside: pd.Series
    override_covers: bool
    is_discrete: bool
    baseline_low: float | None
    baseline_high: float | None
    baseline_values: tuple[float, ...] | None


def _screen_group(
    *,
    channel: str,
    mode: Any,
    values: pd.Series,
    overrides: AcceptedBaselines | None,
    baseline_sigma: float,
    min_baseline_samples: int,
    golden_baseline: ChannelBaseline | None,
    discrete_max_unique_values: int,
    discrete_max_unique_fraction: float,
) -> _GroupScreen:
    mode_str = None if mode is None else str(mode)
    override = overrides.active_override_for(channel, mode_str) if overrides is not None else None

    if golden_baseline is not None:
        # Golden-anchored (Task 3.7) — screen against the frozen reference,
        # never this window's own statistics, regardless of how many rows
        # this window happens to have for the group.
        if golden_baseline.is_discrete:
            allowed = frozenset(golden_baseline.baseline_values or ())
            outside = ~values.isin(allowed)
            low = min(allowed) if allowed else None
            high = max(allowed) if allowed else None
            return _GroupScreen(
                outside=outside,
                override_covers=override is not None,
                is_discrete=True,
                baseline_low=low,
                baseline_high=high,
                baseline_values=tuple(sorted(allowed)) if allowed else None,
            )
        median = golden_baseline.median
        scaled_mad = golden_baseline.scaled_mad
        if median is None or scaled_mad is None:
            # A golden baseline that was somehow frozen without stats (data
            # corruption / hand-edited sidecar) — fail open (nothing
            # screened) rather than crash; not a case real code produces.
            return _GroupScreen(
                outside=pd.Series(False, index=values.index),
                override_covers=override is not None,
                is_discrete=False,
                baseline_low=None,
                baseline_high=None,
                baseline_values=None,
            )
        if scaled_mad <= 0:
            # A04.3: a frozen zero spread cannot say what legitimately varies
            # (sidecars frozen before ``robust_spread`` recorded MAD = 0 for
            # any channel sitting at one value more than half the time).
            # Exempt the group rather than quarantine every non-median value.
            outside = pd.Series(False, index=values.index)
            low = high = None
        else:
            low = median - baseline_sigma * scaled_mad
            high = median + baseline_sigma * scaled_mad
            outside = (values < low) | (values > high)
        return _GroupScreen(
            outside=outside,
            override_covers=override is not None,
            is_discrete=False,
            baseline_low=low,
            baseline_high=high,
            baseline_values=None,
        )

    # Window-local (Phase 1 default; also the documented fallback when a
    # golden run exists but doesn't cover this exact group).
    if len(values) < min_baseline_samples:
        # Not enough data to establish a baseline — everything present
        # counts as Accept rather than manufacturing a quarantine flag off
        # a handful of samples (see MIN_BASELINE_SAMPLES).
        return _GroupScreen(
            outside=pd.Series(False, index=values.index),
            override_covers=False,
            is_discrete=False,
            baseline_low=None,
            baseline_high=None,
            baseline_values=None,
        )

    if _is_discrete(
        values,
        max_unique_values=discrete_max_unique_values,
        max_unique_fraction=discrete_max_unique_fraction,
    ):
        allowed = frozenset(values.unique().tolist())
        outside = ~values.isin(allowed)
        return _GroupScreen(
            outside=outside,
            override_covers=override is not None,
            is_discrete=True,
            baseline_low=min(allowed) if allowed else None,
            baseline_high=max(allowed) if allowed else None,
            baseline_values=tuple(sorted(allowed)) if allowed else None,
        )

    median = float(values.median())
    scaled_mad = robust_spread(values, median)
    if scaled_mad <= 0:
        # Every surviving value identical: nothing is outside the band.
        outside = pd.Series(False, index=values.index)
        low = high = median
    else:
        low = median - baseline_sigma * scaled_mad
        high = median + baseline_sigma * scaled_mad
        outside = (values < low) | (values > high)
    return _GroupScreen(
        outside=outside,
        override_covers=override is not None,
        is_discrete=False,
        baseline_low=low,
        baseline_high=high,
        baseline_values=None,
    )


def _classify_one_group(
    *,
    channel: str,
    mode: Any,
    values: pd.Series,
    overrides: AcceptedBaselines | None,
    baseline_sigma: float,
    min_baseline_samples: int,
    golden_baseline: ChannelBaseline | None = None,
    discrete_max_unique_values: int = DEFAULT_DISCRETE_MAX_UNIQUE_VALUES,
    discrete_max_unique_fraction: float = DEFAULT_DISCRETE_MAX_UNIQUE_FRACTION,
) -> ChannelDisposition:
    mode_str = None if mode is None else str(mode)
    screen = _screen_group(
        channel=channel,
        mode=mode,
        values=values,
        overrides=overrides,
        baseline_sigma=baseline_sigma,
        min_baseline_samples=min_baseline_samples,
        golden_baseline=golden_baseline,
        discrete_max_unique_values=discrete_max_unique_values,
        discrete_max_unique_fraction=discrete_max_unique_fraction,
    )
    n_outside = int(screen.outside.sum())
    n_inside = int(len(values) - n_outside)
    accept_recenter = n_outside if screen.override_covers else 0
    quarantine = 0 if screen.override_covers else n_outside

    return ChannelDisposition(
        channel=channel,
        operating_mode=mode_str,
        accept_recenter=accept_recenter,
        quarantine=quarantine,
        accept=n_inside,
        baseline_low=screen.baseline_low,
        baseline_high=screen.baseline_high,
        is_discrete=screen.is_discrete,
        baseline_values=screen.baseline_values,
    )


def _is_discrete(
    values: pd.Series,
    *,
    max_unique_values: int,
    max_unique_fraction: float,
) -> bool:
    """Data-driven discrete/bitmask detection (Task 1.6) — never a
    hand-listed channel roster.

    A group is discrete when either:

    * it has at most ``max_unique_values`` distinct values, full stop (a
      handful of legitimate engine-state/bitmask codes, regardless of how
      many samples were taken); or
    * every value is integer-valued AND distinct values are a small
      fraction of the sample count (``max_unique_fraction``) — a wider but
      still-enumerable state channel, without misclassifying a continuous
      integer-rounded sensor (RPM, raw ADC counts) that legitimately has
      thousands of distinct readings.
    """
    n = len(values)
    if n == 0:
        return False
    n_unique = int(values.nunique())
    if n_unique <= max_unique_values:
        return True
    is_integer_valued = bool(((values.astype(float) % 1) == 0).all())
    return is_integer_valued and (n_unique / n) <= max_unique_fraction


def _numeric_channels(frame: pd.DataFrame) -> list[str]:
    return [
        str(col)
        for col in frame.columns
        if str(col) not in _STRUCTURAL_COLUMNS
        and not is_flag_column(col)
        and pd.api.types.is_numeric_dtype(frame[col])
    ]


__all__ = [
    "DEFAULT_BASELINE_SIGMA",
    "DEFAULT_DISCRETE_MAX_UNIQUE_FRACTION",
    "DEFAULT_DISCRETE_MAX_UNIQUE_VALUES",
    "MIN_BASELINE_SAMPLES",
    "ChannelBaseline",
    "ChannelDisposition",
    "DispositionSummary",
    "GoldenBaselines",
    "classify_dispositions",
    "compute_golden_baselines",
    "compute_quarantine_mask",
]
