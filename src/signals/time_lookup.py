"""Contextual time lookup: which read does an instant take context from?

Four analyses used to carry a private ``_as_of`` and each meant something
different by it. This module names the three questions they were asking, so a
call site states which one:

* :func:`previous_sample_indices` -- the **latest read at or before** the
  instant. Nothing from the future. Use it for "what was the value *then*".
* :func:`next_sample_indices` -- the **first read strictly after** the
  instant; the other side of a bracket.
* :func:`nearest_sample_indices` -- the **closest read either side**,
  including a future one. Use it only where a slow environmental channel may
  be reused across many anchors and direction does not matter.
* :func:`bracketing_sample_indices` -- the previous and the next read as two
  *distinct* reads, each bounded by its own age.

This is *contextual* lookup: one read may serve any number of instants. It is
not pairing. Comparing two signals takes one-to-one pairs from
:func:`src.signals.pairing.match_ordered` (through
:func:`src.analysis.compare.compare_channels`); nothing here may stand in for
that, because a reused read inflates the pair count and hands one slow sample
to every dense neighbour.

Contract common to all of them
------------------------------

* ``times`` and ``at`` are float seconds on one clock. ``times`` must be
  sorted ascending (equal stamps allowed); ``at`` may be in any order.
  Neither is checked: every caller already sorts its reads and a stricter
  refusal would be a behaviour change, not a refactor.
* ``max_age_s`` is required and keyword-only, in seconds, and **inclusive**:
  a read exactly ``max_age_s`` away is accepted. There is deliberately no
  default and no direction-changing option.
* Results are integer indices into ``times`` (``np.intp``), with
  :data:`MISSING` where no read qualifies: no read in that direction, a read
  older than ``max_age_s``, an empty ``times``, or a NaN instant. (An
  infinite instant has an infinite age, so it qualifies only under an
  infinite ``max_age_s``.) The caller takes values, provenance and
  NaN-versus-``None`` from the index; this module decides nothing about
  domain validity.
* Equal-stamped reads: the previous operation takes the **last** of them (the
  latest in array order); the next operation takes the first read after all
  of them; the nearest operation resolves an exact hit to the **first** equal
  stamp and an exact midpoint tie to the **earlier** sample.
"""

from __future__ import annotations

import numpy as np

#: The index returned where no read qualifies.
MISSING = -1


def previous_sample_indices(times: np.ndarray, at: np.ndarray, *, max_age_s: float) -> np.ndarray:
    """Index of the latest read at or before each instant, ``MISSING`` when
    there is none or it is more than ``max_age_s`` old."""
    out = np.full(len(at), MISSING, dtype=np.intp)
    if not len(times) or not len(at):
        return out
    index = np.searchsorted(times, at, side="right") - 1
    ok = index >= 0
    age = np.full(len(at), np.inf)
    age[ok] = at[ok] - times[index[ok]]
    take = ok & (age <= max_age_s)
    out[take] = index[take]
    return out


def next_sample_indices(times: np.ndarray, at: np.ndarray, *, max_age_s: float) -> np.ndarray:
    """Index of the first read strictly after each instant, ``MISSING`` when
    there is none or it is more than ``max_age_s`` ahead."""
    out = np.full(len(at), MISSING, dtype=np.intp)
    if not len(times) or not len(at):
        return out
    index = np.searchsorted(times, at, side="right")
    ok = index < len(times)
    gap = np.full(len(at), np.inf)
    gap[ok] = times[index[ok]] - at[ok]
    take = ok & (gap <= max_age_s)
    out[take] = index[take]
    return out


def bracketing_sample_indices(
    times: np.ndarray, at: np.ndarray, *, max_age_s: float
) -> tuple[np.ndarray, np.ndarray]:
    """``(previous, next)`` indices for each instant, each bounded by
    ``max_age_s`` independently.

    The two are always distinct reads: a read stamped exactly at the instant
    is the previous one, and the bracket still needs a later read. Returning
    the same read on both sides would let a change between it and the next
    read (a map switch, a mode change) pass as a stable bracket. One side may
    be ``MISSING`` while the other is not.
    """
    return (
        previous_sample_indices(times, at, max_age_s=max_age_s),
        next_sample_indices(times, at, max_age_s=max_age_s),
    )


def nearest_sample_indices(times: np.ndarray, at: np.ndarray, *, max_age_s: float) -> np.ndarray:
    """Index of the closest read to each instant, before or after it,
    ``MISSING`` when even the closest is more than ``max_age_s`` away.

    A future read can win, so this answers "what is around then", never "what
    was known then". An exact midpoint tie takes the earlier read.
    """
    out = np.full(len(at), MISSING, dtype=np.intp)
    if not len(times):
        return out
    i = np.searchsorted(times, at)
    lo, hi = np.clip(i - 1, 0, len(times) - 1), np.clip(i, 0, len(times) - 1)
    d_lo, d_hi = np.abs(at - times[lo]), np.abs(times[hi] - at)
    nearest = np.where(d_lo <= d_hi, lo, hi)
    ok = np.minimum(d_lo, d_hi) <= max_age_s
    out[ok] = nearest[ok]
    return out


def values_or_nan(values: np.ndarray, index: np.ndarray) -> np.ndarray:
    """``values[index]`` as floats, NaN where ``index`` is ``MISSING``.

    For callers whose domain marks a missing context read as NaN; a caller
    that wants ``None`` (or any other marker) reads the index itself.
    """
    out = np.full(len(index), np.nan)
    found = index != MISSING
    out[found] = values[index[found]]
    return out
