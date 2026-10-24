"""The matching core: real observations, each used once, within tolerance."""

from __future__ import annotations

import numpy as np
import pandas as pd
import pytest

from src.signals.pairing import (
    ComparisonRefusedError,
    MatchedSamples,
    PairingPolicy,
    basis_floor_s,
    check_clock_support,
    match_ordered,
    to_seconds,
    weakest_basis,
)


def _policy(tolerance: str, **kwargs: object) -> PairingPolicy:
    return PairingPolicy.within(tolerance, **kwargs)  # type: ignore[arg-type]


def _assert_valid(
    matched: MatchedSamples, times_a: np.ndarray, times_b: np.ndarray, policy: PairingPolicy
) -> None:
    """Every pair is two identifiable observations within tolerance, none reused."""
    assert len(matched.index_a) == len(matched.index_b) == matched.n_pairs
    assert len(np.unique(matched.index_a)) == matched.n_pairs
    assert len(np.unique(matched.index_b)) == matched.n_pairs
    separations = np.abs(times_a[matched.index_a] - times_b[matched.index_b])
    assert np.all(separations <= policy.max_separation_s)
    assert np.allclose(separations, matched.separation_s)
    assert matched.unmatched_a == len(times_a) - matched.n_pairs
    assert matched.unmatched_b == len(times_b) - matched.n_pairs


def test_exact_simultaneous_samples_pair_one_to_one() -> None:
    times = np.arange(0.0, 100.0, 1.0)
    matched = match_ordered(times, times, _policy("0ms"))
    _assert_valid(matched, times, times, _policy("0ms"))
    assert matched.n_pairs == 100
    assert np.all(matched.separation_s == 0.0)


def test_unequal_rates_pair_every_sparse_sample_once() -> None:
    dense = np.arange(0.0, 300.0, 0.01)  # 100 Hz
    sparse = np.arange(0.0, 300.0, 1.0 / 15.0) + 0.003  # 15 Hz, offset
    policy = _policy("20ms")
    matched = match_ordered(dense, sparse, policy)
    _assert_valid(matched, dense, sparse, policy)
    assert matched.reference == "b"
    assert matched.n_pairs == len(sparse)
    assert matched.unmatched_a == len(dense) - len(sparse)
    assert matched.unmatched_b == 0


def test_tolerance_boundary_is_inclusive_and_exact() -> None:
    times_a = np.arange(0.0, 50.0, 1.0)
    at_tolerance = times_a + 0.25
    assert match_ordered(times_a, at_tolerance, _policy("250ms")).n_pairs == 50
    assert match_ordered(times_a, at_tolerance, _policy("249ms")).n_pairs == 0


def test_no_sample_is_reused_however_many_neighbours_want_it() -> None:
    """Ten dense samples all inside tolerance of one sparse sample: one pair."""
    dense = np.arange(0.0, 1.0, 0.1)
    sparse = np.array([0.45])
    policy = _policy("1s", reference="a")
    matched = match_ordered(dense, sparse, policy)
    _assert_valid(matched, dense, sparse, policy)
    assert matched.n_pairs == 1
    assert matched.unmatched_a == 9


def test_equal_distance_ties_resolve_by_the_stated_rule() -> None:
    # Exactly representable offsets, so the two distances are exactly equal.
    reference = np.array([1.0])
    candidates = np.array([0.5, 1.5])
    earlier = match_ordered(reference, candidates, _policy("500ms", tie_break="earlier"))
    later = match_ordered(reference, candidates, _policy("500ms", tie_break="later"))
    assert candidates[earlier.index_b[0]] == 0.5
    assert candidates[later.index_b[0]] == 1.5


def test_duplicate_timestamps_are_distinct_observations() -> None:
    """Two samples at the same instant are two samples, consumed lowest index first."""
    times_a = np.array([5.0, 5.0])
    times_b = np.array([5.0, 5.0, 5.0])
    matched = match_ordered(times_a, times_b, _policy("0ms"))
    _assert_valid(matched, times_a, times_b, _policy("0ms"))
    assert matched.n_pairs == 2
    assert matched.index_b.tolist() == [0, 1]
    assert matched.unmatched_b == 1


def test_a_consumed_neighbour_does_not_hide_a_further_free_one() -> None:
    """The second reference sample must still find the earlier free candidate."""
    reference = np.array([0.0, 0.1])
    candidates = np.array([-0.9, 0.5])
    policy = _policy("1s", reference="a")
    matched = match_ordered(reference, candidates, policy)
    _assert_valid(matched, reference, candidates, policy)
    # r=0.0 takes 0.5 (nearer); r=0.1 then takes -0.9, which is 1.0 s away: in.
    assert matched.n_pairs == 2
    assert sorted(matched.index_b.tolist()) == [0, 1]


def test_non_overlapping_series_produce_no_pairs() -> None:
    early = np.arange(0.0, 100.0, 1.0)
    late = early + 3600.0
    matched = match_ordered(early, late, _policy("1s"))
    assert matched.n_pairs == 0
    assert matched.unmatched_a == matched.unmatched_b == 100


def test_unsorted_input_returns_indices_into_the_input_as_given() -> None:
    rng = np.random.default_rng(1)
    times_a = rng.permutation(np.arange(0.0, 200.0, 1.0))
    times_b = rng.permutation(np.arange(0.0, 200.0, 1.0) + 0.01)
    policy = _policy("50ms")
    matched = match_ordered(times_a, times_b, policy)
    _assert_valid(matched, times_a, times_b, policy)
    assert matched.n_pairs == 200


def test_auto_reference_is_the_sparser_side() -> None:
    assert match_ordered(np.arange(10.0), np.arange(5.0), _policy("1s")).reference == "b"
    assert match_ordered(np.arange(5.0), np.arange(10.0), _policy("1s")).reference == "a"
    assert match_ordered(np.arange(5.0), np.arange(5.0), _policy("1s")).reference == "a"


def test_policy_validates_its_inputs() -> None:
    with pytest.raises(TypeError, match="Timedelta"):
        PairingPolicy(max_separation="1s")  # type: ignore[arg-type]
    with pytest.raises(ValueError, match="negative"):
        PairingPolicy.within("-1s")
    with pytest.raises(ValueError, match="reference"):
        PairingPolicy.within("1s", reference="c")  # type: ignore[arg-type]
    assert PairingPolicy.within("250ms").as_dict() == {
        "max_separation_s": 0.25,
        "reference": "auto",
        "tie_break": "earlier",
    }


def test_clock_floor_refuses_what_the_basis_cannot_carry() -> None:
    times = np.arange(0.0, 60.0, 1.0)
    assert basis_floor_s("acquired", times) == 0.0
    assert basis_floor_s("stored", times) == 0.0
    assert basis_floor_s("round", times) == pytest.approx(1.0)
    assert basis_floor_s("unknown", times) == 1.0

    check_clock_support(
        _policy("1s"), basis_a="round", times_a=times, basis_b="stored", times_b=times
    )
    with pytest.raises(ComparisonRefusedError, match="round"):
        check_clock_support(
            _policy("500ms"), basis_a="round", times_a=times, basis_b="stored", times_b=times
        )
    with pytest.raises(ComparisonRefusedError, match="metadata_json"):
        check_clock_support(
            _policy("999ms"), basis_a="stored", times_a=times, basis_b="unknown", times_b=times
        )


def test_a_side_is_judged_on_its_weakest_row() -> None:
    assert weakest_basis(["acquired", "acquired"]) == "acquired"
    assert weakest_basis(["acquired", "stored"]) == "stored"
    assert weakest_basis(["stored", "round"]) == "round"
    assert weakest_basis([]) == "unknown"
    assert weakest_basis(pd.Series(["acquired", "garbage"])) == "unknown"


def test_to_seconds_keeps_the_sub_second_fraction_exact() -> None:
    base = pd.Timestamp("2026-08-06T12:00:00Z")
    stamps = pd.DatetimeIndex(
        [base, base + pd.Timedelta(milliseconds=250), base + pd.Timedelta(seconds=1)]
    )
    seconds = to_seconds(stamps)
    assert seconds[1] - seconds[0] == 0.25
    assert seconds[2] - seconds[0] == 1.0
