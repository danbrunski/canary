"""R11: previous, nearest and bracketing lookup are separate, explicit questions.

``LEGACY_*`` below are the pre-extraction bodies of the private ``_as_of``
families (``fuel_comp_samples``, ``fuel_comp_inputs``, ``engine_dynamics``,
``fuel_plant_eval``, ``target_phi``), copied verbatim, so the shared
primitive is checked against what the callers did, anchor by anchor -- never
by an aggregate.
"""

from __future__ import annotations

import ast
from pathlib import Path

import numpy as np
import pytest

from src.signals.pairing import PairingPolicy, match_ordered
from src.signals.time_lookup import (
    MISSING,
    bracketing_sample_indices,
    nearest_sample_indices,
    next_sample_indices,
    previous_sample_indices,
    values_or_nan,
)

SRC = Path(__file__).resolve().parents[1] / "src"


def legacy_previous(times, values, at, max_age):  # fuel_comp_samples / fuel_comp_inputs
    out = np.full(len(at), np.nan)
    if not len(times) or not len(at):
        return out
    index = np.searchsorted(times, at, side="right") - 1
    ok = index >= 0
    age = np.full(len(at), np.inf)
    age[ok] = at[ok] - times[index[ok]]
    take = ok & (age <= max_age)
    out[take] = values[index[take]]
    return out


def legacy_next_within(times, values, at, max_age):  # fuel_comp_samples._next_within
    out = np.full(len(at), np.nan)
    if not len(times) or not len(at):
        return out
    index = np.searchsorted(times, at, side="right")
    ok = index < len(times)
    gap = np.full(len(at), np.inf)
    gap[ok] = times[index[ok]] - at[ok]
    take = ok & (gap <= max_age)
    out[take] = values[index[take]]
    return out


def legacy_nearest(t, v, at, max_age_s):  # engine_dynamics / fuel_plant_eval
    out = np.full(len(at), np.nan)
    if not len(t):
        return out
    i = np.searchsorted(t, at)
    lo, hi = np.clip(i - 1, 0, len(t) - 1), np.clip(i, 0, len(t) - 1)
    d_lo, d_hi = np.abs(at - t[lo]), np.abs(t[hi] - at)
    j = np.where(d_lo <= d_hi, lo, hi)
    ok = np.minimum(d_lo, d_hi) <= max_age_s
    out[ok] = v[j[ok]]
    return out


def legacy_bracket(times, t, max_age):  # target_phi._as_of
    if not len(times):
        return None, None
    before = int(np.searchsorted(times, t, side="right")) - 1
    after = before + 1
    prior = before if before >= 0 and t - times[before] <= max_age else None
    nxt = after if after < len(times) and times[after] - t <= max_age else None
    return prior, nxt


def fixtures():
    """Sorted stamps on a 1/8 s grid (duplicates, exact hits, exact midpoint
    ties) and anchors on a 1/16 s grid, plus non-finite and out-of-range ones."""
    rng = np.random.default_rng(11)
    for n in (0, 1, 2, 7, 40):
        times = np.cumsum(rng.integers(0, 5, size=n) * 0.125) + 10.0
        values = rng.normal(size=n).round(3)
        span = float(times[-1] - 10.0) if n else 0.0
        at = np.concatenate(
            [
                np.arange(9.0, 11.5 + span, 0.0625),
                times,
                [np.nan, -np.inf, np.inf, 1e9],
            ]
        )
        yield times, values, at


AGES = (0.0, 0.0625, 0.125, 0.5, 1.0, np.inf)


def test_each_operation_reproduces_its_legacy_family_per_anchor() -> None:
    for times, values, at in fixtures():
        for age in AGES:
            prev = previous_sample_indices(times, at, max_age_s=age)
            nxt = next_sample_indices(times, at, max_age_s=age)
            near = nearest_sample_indices(times, at, max_age_s=age)
            assert prev.dtype == nxt.dtype == near.dtype == np.intp
            np.testing.assert_array_equal(
                values_or_nan(values, prev), legacy_previous(times, values, at, age)
            )
            np.testing.assert_array_equal(
                values_or_nan(values, nxt), legacy_next_within(times, values, at, age)
            )
            np.testing.assert_array_equal(
                values_or_nan(values, near), legacy_nearest(times, values, at, age)
            )
            bracket = bracketing_sample_indices(times, at, max_age_s=age)
            for k, t in enumerate(at):
                expected = legacy_bracket(times, float(t), age)
                got = tuple(None if i[k] == MISSING else int(i[k]) for i in bracket)
                assert got == expected, (t, age)


def test_previous_takes_nothing_from_the_future_and_nearest_does() -> None:
    times = np.array([10.0, 12.0])
    values = np.array([1.0, 2.0])
    at = np.array([11.5])  # the 12.0 read is nearer, but it has not happened yet
    prev = previous_sample_indices(times, at, max_age_s=5.0)
    near = nearest_sample_indices(times, at, max_age_s=5.0)
    assert values_or_nan(values, prev)[0] == 1.0
    assert values_or_nan(values, near)[0] == 2.0
    # With a tight age the previous question has no answer; nearest still does.
    assert previous_sample_indices(times, at, max_age_s=1.0)[0] == MISSING
    assert nearest_sample_indices(times, at, max_age_s=1.0)[0] == 1


def test_empty_inputs_are_all_missing() -> None:
    empty = np.array([], dtype=float)
    some = np.array([1.0, 2.0])
    for op in (previous_sample_indices, next_sample_indices, nearest_sample_indices):
        assert op(empty, some, max_age_s=1.0).tolist() == [MISSING, MISSING]
        assert op(some, empty, max_age_s=1.0).shape == (0,)
        assert op(empty, empty, max_age_s=1.0).shape == (0,)
    prior, nxt = bracketing_sample_indices(empty, some, max_age_s=1.0)
    assert prior.tolist() == nxt.tolist() == [MISSING, MISSING]


def test_age_limit_is_inclusive() -> None:
    times = np.array([10.0])
    at = np.array([10.5, 10.5 + 1e-9, 9.5, 9.5 - 1e-9])
    assert previous_sample_indices(times, at, max_age_s=0.5).tolist() == [
        0,
        MISSING,
        MISSING,
        MISSING,
    ]
    assert next_sample_indices(times, at, max_age_s=0.5).tolist() == [MISSING, MISSING, 0, MISSING]
    assert nearest_sample_indices(times, at, max_age_s=0.5).tolist() == [0, MISSING, 0, MISSING]


def test_exact_anchor_is_previous_and_the_bracket_still_needs_a_later_read() -> None:
    times = np.array([10.0, 11.0, 12.0])
    at = np.array([11.0])
    assert previous_sample_indices(times, at, max_age_s=0.0)[0] == 1
    assert next_sample_indices(times, at, max_age_s=5.0)[0] == 2  # strictly after
    prior, nxt = bracketing_sample_indices(times, at, max_age_s=5.0)
    assert (prior[0], nxt[0]) == (1, 2)
    assert prior[0] != nxt[0]
    # A final exact-time read has a prior but no next.
    prior, nxt = bracketing_sample_indices(times, np.array([12.0]), max_age_s=5.0)
    assert (prior[0], nxt[0]) == (2, MISSING)


def test_bracket_endpoints_are_bounded_independently() -> None:
    times = np.array([10.0, 20.0])
    prior, nxt = bracketing_sample_indices(times, np.array([11.0, 19.0, 15.0]), max_age_s=2.0)
    assert prior.tolist() == [0, MISSING, MISSING]
    assert nxt.tolist() == [MISSING, 1, MISSING]
    # Before the first and after the last read: one side exists at most.
    prior, nxt = bracketing_sample_indices(times, np.array([9.0, 21.0]), max_age_s=2.0)
    assert prior.tolist() == [MISSING, 1]
    assert nxt.tolist() == [0, MISSING]


def test_nearest_midpoint_tie_takes_the_earlier_read() -> None:
    times = np.array([10.0, 11.0])
    assert nearest_sample_indices(times, np.array([10.5]), max_age_s=1.0)[0] == 0


def test_duplicate_stamps() -> None:
    times = np.array([10.0, 10.0, 10.0, 11.0])
    at = np.array([10.0, 10.5])
    # Previous: the last of the equal stamps. Next: the first read after all of them.
    assert previous_sample_indices(times, at, max_age_s=1.0).tolist() == [2, 2]
    assert next_sample_indices(times, at, max_age_s=1.0).tolist() == [3, 3]
    # Nearest at an exact hit resolves to the first equal stamp.
    assert nearest_sample_indices(times, np.array([10.0]), max_age_s=0.0)[0] == 0


def test_non_finite_instants_follow_the_legacy_arithmetic() -> None:
    times = np.array([10.0, 11.0])
    at = np.array([np.nan, np.inf, -np.inf])
    for op in (previous_sample_indices, next_sample_indices, nearest_sample_indices):
        # A finite age limit rejects all three; NaN is rejected even without one.
        assert op(times, at, max_age_s=5.0).tolist() == [MISSING] * 3
        assert op(times, at, max_age_s=np.inf)[0] == MISSING


def test_reused_context_reads_are_not_counted_pairs() -> None:
    """One slow read serves every anchor of a lookup; one-to-one pairing of the
    same inputs yields one pair. Lookup must never feed a pair count."""
    slow = np.array([10.0])
    anchors = np.arange(10.0, 10.5, 0.05)
    reused = previous_sample_indices(slow, anchors, max_age_s=1.0)
    assert (reused == 0).sum() == len(anchors)
    matched = match_ordered(anchors, slow, PairingPolicy.within("1s", reference="a"))
    assert len(matched.index_a) == 1


def test_lookup_stays_out_of_scientific_pairing() -> None:
    """Pairing and comparison modules do not import the lookup, and the
    primitive imports nothing but numpy (layer 0, no analysis/preprocessing)."""
    for name in ("signals/pairing.py", "analysis/compare.py"):
        assert "time_lookup" not in (SRC / name).read_text(), name
    tree = ast.parse((SRC / "signals" / "time_lookup.py").read_text())
    imported = {
        alias.name.split(".")[0]
        for node in ast.walk(tree)
        if isinstance(node, ast.Import)
        for alias in node.names
    } | {
        (node.module or "").split(".")[0]
        for node in ast.walk(tree)
        if isinstance(node, ast.ImportFrom)
    }
    assert imported <= {"__future__", "numpy"}


def test_max_age_is_required_and_keyword_only() -> None:
    times = np.array([1.0])
    for op in (
        previous_sample_indices,
        next_sample_indices,
        nearest_sample_indices,
        bracketing_sample_indices,
    ):
        with pytest.raises(TypeError):
            op(times, times)  # type: ignore[call-arg]
        with pytest.raises(TypeError):
            op(times, times, 1.0)  # type: ignore[misc]


def test_domain_wrappers_keep_their_distinct_contracts_on_one_fixture() -> None:
    """The previous-only caller, the nearest caller and the bracket caller give
    different answers for the same reads; that disagreement is the point."""
    from src.analysis import engine_dynamics, fuel_comp_samples, fuel_plant_eval, target_phi

    times = np.array([10.0, 12.0])
    values = np.array([1.0, 2.0])
    at = np.array([11.5])
    prev = fuel_comp_samples._value_at_or_before(fuel_comp_samples._Series(times, values), at, 5.0)
    after = fuel_comp_samples._value_after(fuel_comp_samples._Series(times, values), at, 5.0)
    near = engine_dynamics._nearest_value(times, values, at, 5.0)
    near_eval = fuel_plant_eval._nearest_value(fuel_plant_eval._Series(times, values), at, 5.0)
    assert (prev[0], after[0], near[0], near_eval[0]) == (1.0, 2.0, 2.0, 2.0)

    series = target_phi._Series(times, values, ["acquired", "acquired"])
    assert target_phi._bracket(series, 11.5, 5.0) == (0, 1)
    # Exact-time read is the prior; the next is a distinct later read.
    assert target_phi._bracket(series, 10.0, 5.0) == (0, 1)
    assert target_phi._bracket(series, 12.0, 5.0) == (1, None)
    assert target_phi._bracket(series, 9.0, 0.5) == (None, None)
    empty = target_phi._Series(np.array([]), np.array([]), [])
    assert target_phi._bracket(empty, 1.0, 5.0) == (None, None)
