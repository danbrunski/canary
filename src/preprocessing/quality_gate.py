"""Physical-impossibility reject gate (Task 1.2, online-training-strategy.md).

A channel reading outside its curated :data:`~src.analysis.fsm.PHYSICAL_LIMITS`
(coolant at the sensor-open floor, a decode-rollover RPM, negative MAF, ...)
is a data-quality failure — broken sensor / bad decode / bus glitch — never an
unhealthy-engine state. It must never reach the scaler and must never be
scored as an anomaly.

The gate NaNs the offending value **per cell**, not the whole row — a bad
coolant reading doesn't have to cost the same row's good RPM/MAF/etc. The
NaN then flows through whatever missingness machinery already exists for
that path, so this module adds no new plumbing:

* **Training** — wired into
  ``src.preprocessing.feature_pipeline._decode_session_to_resampled``,
  between ``to_wide`` and ``resample_wide``. A gated-out cell is simply
  absent from the resampler's ``dropna()``-based "last observed" tracking,
  so it gets the resampler's existing bounded ffill + ``__gap`` flag — a
  reject looks exactly like any other gap.
* **Live scoring** — wired into
  ``src.preprocessing.live.LiveFeatureSource._set_value``, the single
  choke point every observed value (OBD, decoded CAN, DBC, DID) passes
  through. A rejected value is simply never stored as a "current value" —
  the channel ages out via the exact same ``_last_observed_at`` /
  ``_gap_flags`` staleness path a channel that stopped transmitting would.

Either way: no whole-row drop, no parallel "reject" code path for
downstream consumers (GRU-D mask, IF missing-channel policy, ``__gap``
flags) to special-case — a reject is a gap, full stop. Per-channel reject
counts are always returned, never silently swallowed.
"""

from __future__ import annotations

import logging
from collections.abc import Mapping
from dataclasses import dataclass, field

import numpy as np
import pandas as pd

from src.analysis.fsm import PHYSICAL_LIMITS, PhysicalLimit

log = logging.getLogger(__name__)

__all__ = ["QualityGateResult", "apply_quality_gate", "is_physically_valid"]


@dataclass(frozen=True)
class QualityGateResult:
    """Per-channel reject counts from one :func:`apply_quality_gate` call."""

    reject_counts: dict[str, int] = field(default_factory=dict)

    @property
    def total_rejected(self) -> int:
        return sum(self.reject_counts.values())


def is_physically_valid(
    channel: str,
    value: float | None,
    *,
    limits: Mapping[str, PhysicalLimit] = PHYSICAL_LIMITS,
) -> bool:
    """True when ``value`` is inside ``channel``'s physical limit.

    A channel absent from ``limits`` (the common case — see
    ``PHYSICAL_LIMITS``'s docstring) is always valid: absence means "no
    reject", never a guessed bound. Used by the live path
    (``LiveFeatureSource._set_value``), one value at a time; the batch path
    uses :func:`apply_quality_gate` (vectorized over a whole frame) instead.
    """
    limit = limits.get(channel)
    if limit is None or value is None:
        return True
    try:
        numeric = float(value)
    except (TypeError, ValueError):
        return True
    if numeric != numeric:  # NaN is not a physical-impossibility reject — it's already missing
        return True
    return limit.min <= numeric <= limit.max


def apply_quality_gate(
    wide: pd.DataFrame,
    *,
    limits: Mapping[str, PhysicalLimit] = PHYSICAL_LIMITS,
) -> tuple[pd.DataFrame, QualityGateResult]:
    """NaN out physically-impossible cells in a wide telemetry frame, per cell.

    Only touches columns present in both ``wide`` and ``limits``; every other
    column (including one with no curated limit) passes through untouched.
    Returns a copy when anything was rejected — the input is never mutated in
    place — else the original frame, so a no-reject call is a cheap no-op.
    """
    out = wide
    counts: dict[str, int] = {}
    for channel, limit in limits.items():
        if channel not in wide.columns:
            continue
        series = pd.to_numeric(wide[channel], errors="coerce")
        bad = series.notna() & ((series < limit.min) | (series > limit.max))
        n = int(bad.sum())
        if n == 0:
            continue
        if out is wide:
            out = wide.copy()
        out.loc[bad, channel] = np.nan
        counts[channel] = n
    if counts:
        log.info(
            "quality gate rejected physically-impossible cells",
            extra={"reject_counts": counts, "total_rejected": sum(counts.values())},
        )
    return out, QualityGateResult(reject_counts=counts)
