"""Pair two sample streams by acquisition time, each sample used at most once.

This is the matching core behind :mod:`src.analysis.compare`. It exists as
its own module because the defect it replaces was *structural*: the previous
``align_pairs`` averaged each side onto a one-second grid and called the
inner-joined bins "pairs". Two samples per second whose values disagreed by
100 at every acquired instant averaged to the same bin means and came back
as ``r2=1.0, slope=1.0, stable`` (roadmap task C01, 2026-09-06).

The contract here is the opposite of that:

* a pair is two **real observations**, identified by their row indices;
* each observation appears in **at most one** pair — no ASOF join that hands
  one sparse sample to every dense neighbour and calls the result ``n_pairs``;
* the two observations were acquired within
  :attr:`PairingPolicy.max_separation` of each other, measured on the
  *acquisition* clock rather than a logical round stamp;
* ties are resolved by a stated rule, so the same inputs always give the
  same pairs.

Matching is greedy in acquisition order over the *reference* side: each
reference sample, earliest first, takes its nearest not-yet-used partner
within tolerance. That is not a globally optimal assignment and does not try
to be — it is simple to state, deterministic, and near-linear
(``O((n + m) log(n + m))`` for the sorts, then a union-find sweep).

Timestamp provenance matters as much as the algorithm. A stamp that is really
a scheduling round's start time cannot support a 50 ms claim, and a frame
with no metadata at all cannot support a sub-second one. Those refusals live
here too (:func:`check_clock_support`), so no caller can pair on a clock that
cannot carry the tolerance it asked for.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Literal

import numpy as np
import pandas as pd

#: How a row's ``timestamp`` came to be:
#:
#: * ``acquired`` — the persisted per-reading arrival instant
#:   (``metadata_json.acquired_at``); the true measurement clock.
#: * ``stored`` — the row's stored stamp, which for a non-round capture is
#:   the decode-time clock read and approximates arrival. Broadcast ``can:``
#:   rows are always this.
#: * ``round`` — the stored stamp is a logical capture-round stamp (the
#:   session's manifest says ``round_timestamps``) and no ``acquired_at`` was
#:   persisted, so the true arrival is unknown to within a round period.
#: * ``unknown`` — the frame carries no ``metadata_json`` column at all, so
#:   nothing about the stamp's provenance can be inferred.
TimestampBasis = Literal["acquired", "stored", "round", "unknown"]

#: Weakest-first. A side mixing bases reports its weakest, so a tolerance is
#: never justified by the best-stamped subset of a series.
_BASIS_RANK: dict[str, int] = {"acquired": 0, "stored": 1, "round": 2, "unknown": 3}

#: The tightest tolerance an ``unknown`` basis may claim. Without metadata
#: the stamp could be a round stamp, and rounds are of order one second.
UNKNOWN_BASIS_FLOOR_S = 1.0

#: Which side of a comparison is the reference — the one whose samples are
#: walked in order, each taking its nearest unused partner. ``auto`` picks
#: the sparser side (fewer samples; ``a`` on a tie), so a 1 Hz PID against a
#: 100 Hz broadcast pairs every PID reading once rather than every frame.
ReferenceSide = Literal["a", "b", "auto"]


class ComparisonRefusedError(ValueError):
    """The data cannot support a comparison, as distinct from showing none.

    Raised rather than returned because the refusal cases all produce a
    *plausible-looking* number if you compute one anyway.
    """


class InputNotCapturedError(ComparisonRefusedError):
    """One side has no observations at all: an unavailable input, not a
    clock or coverage problem. ``spec`` names the side (R05.3 — callers
    branch on this type, never on the message)."""

    def __init__(self, message: str, *, spec: str) -> None:
        super().__init__(message)
        self.spec = spec


@dataclass(frozen=True)
class PairingPolicy:
    """How two streams may be paired. Required at every comparison call site.

    There is deliberately no default: a policy is a claim about how far apart
    two "simultaneous" observations may be, and that claim belongs to the
    caller and the question, not to the library.
    """

    #: Largest acquisition-time separation two observations may have and
    #: still count as one pair. Inclusive: exactly this far apart pairs.
    max_separation: pd.Timedelta
    reference: ReferenceSide = "auto"
    #: When a reference sample sees candidates before and after it at exactly
    #: the same distance, which wins. Among equal-time duplicates the lower
    #: row index wins, always.
    tie_break: Literal["earlier", "later"] = "earlier"

    def __post_init__(self) -> None:
        if not isinstance(self.max_separation, pd.Timedelta):
            raise TypeError(
                "PairingPolicy.max_separation must be a pandas Timedelta — use "
                'PairingPolicy.within("250ms") to build one from a string'
            )
        if self.max_separation < pd.Timedelta(0):
            raise ValueError("PairingPolicy.max_separation must not be negative")
        if self.reference not in ("a", "b", "auto"):
            raise ValueError(f"PairingPolicy.reference must be a/b/auto, got {self.reference!r}")
        if self.tie_break not in ("earlier", "later"):
            raise ValueError(
                f"PairingPolicy.tie_break must be earlier/later, got {self.tie_break!r}"
            )

    @classmethod
    def within(
        cls,
        max_separation: str | pd.Timedelta,
        *,
        reference: ReferenceSide = "auto",
        tie_break: Literal["earlier", "later"] = "earlier",
    ) -> PairingPolicy:
        return cls(
            max_separation=pd.Timedelta(max_separation),
            reference=reference,
            tie_break=tie_break,
        )

    @property
    def max_separation_s(self) -> float:
        return float(self.max_separation.total_seconds())

    def describe(self) -> str:
        return (
            f"within {self.max_separation_s * 1000.0:g} ms, reference={self.reference}, "
            f"ties={self.tie_break}"
        )

    def as_dict(self) -> dict[str, object]:
        return {
            "max_separation_s": self.max_separation_s,
            "reference": self.reference,
            "tie_break": self.tie_break,
        }


@dataclass(frozen=True)
class MatchedSamples:
    """The outcome of :func:`match_ordered`: index pairs, never values.

    ``index_a[i]`` and ``index_b[i]`` are positions into the *input* arrays,
    so every pair points back at two identifiable observations. Both index
    arrays contain no repeats.
    """

    index_a: np.ndarray
    index_b: np.ndarray
    #: ``|t_a - t_b|`` per pair, seconds.
    separation_s: np.ndarray
    unmatched_a: int
    unmatched_b: int
    reference: Literal["a", "b"]

    @property
    def n_pairs(self) -> int:
        return int(len(self.index_a))


def to_seconds(timestamps: pd.Series | pd.DatetimeIndex | np.ndarray) -> np.ndarray:
    """Timestamps as float Unix seconds, the unit the matcher works in.

    This is the one timestamp-to-seconds conversion in the tree
    (``tests/test_epoch_seconds_conversion.py`` refuses others). It pins the
    unit to nanoseconds before the integer view: pandas 3 session frames
    carry ``datetime64[us]``, and ``astype("int64") / 1e9`` on those reads
    every interval 1000x short — the behavior evaluator's 8 s persistence
    window once spanned hours of rows that way (B04.3, 2026-09-22). ``NaT``
    comes back as ``NaN``, never as the int64 sentinel.
    """
    stamps = pd.to_datetime(pd.Index(timestamps), utc=True, errors="coerce")
    # ``.values`` on a tz-aware index is the UTC datetime64 array. Whole
    # seconds and the sub-second remainder are converted separately so the
    # fraction survives exactly — a stamp exactly ``max_separation`` away
    # must compare as exactly that, not a rounding error past it.
    nanos = np.asarray(stamps.values).astype("datetime64[ns]").astype(np.int64)
    whole = (nanos // 1_000_000_000).astype(np.float64)
    fraction = (nanos % 1_000_000_000).astype(np.float64) / 1e9
    seconds = np.asarray(whole + fraction, dtype=np.float64)
    seconds[np.asarray(stamps.isna())] = np.nan
    return seconds


def weakest_basis(bases: pd.Series | list[str]) -> TimestampBasis:
    """The basis a side must be judged on: its weakest row."""
    seen = {str(basis) for basis in bases}
    if not seen:
        return "unknown"
    worst = max(seen, key=lambda basis: _BASIS_RANK.get(basis, _BASIS_RANK["unknown"]))
    return worst if worst in _BASIS_RANK else "unknown"  # type: ignore[return-value]


def round_period_s(times_s: np.ndarray) -> float | None:
    """Median gap between distinct stamps — the round period a ``round`` side implies."""
    distinct = np.unique(times_s)
    if len(distinct) < 2:
        return None
    return float(np.median(np.diff(distinct)))


def basis_floor_s(basis: TimestampBasis, times_s: np.ndarray) -> float:
    """The tightest tolerance this basis can honestly support, in seconds.

    * ``acquired`` and ``stored``: no floor. Both are wall-clock reads at
      arrival (``stored`` trails it by decode latency, unmeasured but of
      millisecond order, and is flagged in the result for that reason).
    * ``round``: the round period, estimated from the data as the median gap
      between distinct stamps. Inside one round the true arrival is unknown.
    * ``unknown``: :data:`UNKNOWN_BASIS_FLOOR_S`.
    """
    if basis in ("acquired", "stored"):
        return 0.0
    if basis == "round":
        period = round_period_s(times_s)
        return UNKNOWN_BASIS_FLOOR_S if period is None else max(period, 0.0)
    return UNKNOWN_BASIS_FLOOR_S


def check_clock_support(
    policy: PairingPolicy,
    *,
    basis_a: TimestampBasis,
    times_a: np.ndarray,
    basis_b: TimestampBasis,
    times_b: np.ndarray,
    label_a: str = "a",
    label_b: str = "b",
) -> None:
    """Refuse a tolerance tighter than either side's clock can carry."""
    for label, basis, times in ((label_a, basis_a, times_a), (label_b, basis_b, times_b)):
        floor = basis_floor_s(basis, times)
        if policy.max_separation_s < floor:
            raise ComparisonRefusedError(
                f"{label}: timestamp basis is {basis!r}, which cannot support a "
                f"{policy.max_separation_s * 1000.0:g} ms pairing tolerance (clock floor "
                f"{floor * 1000.0:g} ms). "
                + (
                    "The stored stamps are logical round stamps with no persisted "
                    "acquired_at, so within one round the true arrival order is unknown."
                    if basis == "round"
                    else "The frame carries no metadata_json, so nothing is known about how "
                    "its stamps were produced; sub-second claims need provenance."
                )
            )


def _sorted_view(times: np.ndarray) -> tuple[np.ndarray, np.ndarray]:
    order = np.argsort(times, kind="stable")
    return times[order], order


def _find(parent: list[int], k: int) -> int:
    """Union-find root with path halving; ``parent[k] == k`` marks a free slot."""
    while parent[k] != k:
        parent[k] = parent[parent[k]]
        k = parent[k]
    return k


def _match_reference_first(
    ref_times: np.ndarray,
    other_times: np.ndarray,
    *,
    max_separation_s: float,
    tie_break: Literal["earlier", "later"],
) -> tuple[list[int], list[int], list[float]]:
    """Greedy in-order matching over sorted inputs; returns sorted positions.

    Two union-find structures answer "nearest free slot at or after ``p``"
    and "nearest free slot at or before ``p``" in amortised near-constant
    time, so consuming a slot never forces a linear rescan.
    """
    n_other = len(other_times)
    # next_free[k] resolves to the smallest free position >= k, or n_other.
    next_free = list(range(n_other + 1))
    # prev_free[k + 1] resolves to the largest free position <= k, or -1 (slot 0).
    prev_free = list(range(n_other + 1))
    insert_at = np.searchsorted(other_times, ref_times, side="left")
    ref_positions: list[int] = []
    other_positions: list[int] = []
    separations: list[float] = []
    for ref_pos, (t_ref, p) in enumerate(zip(ref_times.tolist(), insert_at.tolist(), strict=True)):
        right = _find(next_free, p)
        left = _find(prev_free, p) - 1  # prev_free is offset by one
        right_gap = other_times[right] - t_ref if right < n_other else np.inf
        left_gap = t_ref - other_times[left] if left >= 0 else np.inf
        if left_gap == right_gap and left_gap <= max_separation_s:
            chosen = left if tie_break == "earlier" else right
        elif left_gap < right_gap and left_gap <= max_separation_s:
            chosen = left
        elif right_gap <= max_separation_s:
            chosen = right
        else:
            continue
        ref_positions.append(ref_pos)
        other_positions.append(chosen)
        separations.append(float(min(left_gap, right_gap)))
        next_free[chosen] = chosen + 1
        prev_free[chosen + 1] = chosen
    return ref_positions, other_positions, separations


def match_ordered(
    times_a: np.ndarray,
    times_b: np.ndarray,
    policy: PairingPolicy,
) -> MatchedSamples:
    """Pair ``times_a`` with ``times_b`` under ``policy``; see the module docstring.

    Inputs are float seconds and need not be sorted; returned indices refer
    to the inputs as given. Result pairs are ordered by reference-side time.
    """
    times_a = np.asarray(times_a, dtype=np.float64)
    times_b = np.asarray(times_b, dtype=np.float64)
    reference: Literal["a", "b"]
    if policy.reference == "auto":
        reference = "a" if len(times_a) <= len(times_b) else "b"
    else:
        reference = policy.reference
    ref_times, ref_order = _sorted_view(times_a if reference == "a" else times_b)
    other_times, other_order = _sorted_view(times_b if reference == "a" else times_a)
    ref_positions, other_positions, separations = _match_reference_first(
        ref_times,
        other_times,
        max_separation_s=policy.max_separation_s,
        tie_break=policy.tie_break,
    )
    ref_index = ref_order[np.asarray(ref_positions, dtype=np.intp)]
    other_index = other_order[np.asarray(other_positions, dtype=np.intp)]
    index_a, index_b = (ref_index, other_index) if reference == "a" else (other_index, ref_index)
    n_pairs = len(ref_positions)
    return MatchedSamples(
        index_a=index_a,
        index_b=index_b,
        separation_s=np.asarray(separations, dtype=np.float64),
        unmatched_a=int(len(times_a) - n_pairs),
        unmatched_b=int(len(times_b) - n_pairs),
        reference=reference,
    )


__all__ = [
    "UNKNOWN_BASIS_FLOOR_S",
    "ComparisonRefusedError",
    "InputNotCapturedError",
    "MatchedSamples",
    "PairingPolicy",
    "ReferenceSide",
    "TimestampBasis",
    "basis_floor_s",
    "check_clock_support",
    "match_ordered",
    "round_period_s",
    "to_seconds",
    "weakest_basis",
]
