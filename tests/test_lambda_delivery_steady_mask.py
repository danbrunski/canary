"""R05.12 — the steady-target rule, vectorized, against the walk it replaced.

``delivery_samples`` decided "steady target" with a per-row backward walk in
pure Python, O(n x dwell). It is now a vectorized function; this keeps the
original walk as the reference and compares the two on randomized traces
with cuts, NaNs, duplicate stamps and gaps.
"""

from __future__ import annotations

import numpy as np
import numpy.typing as npt
import pytest

from src.analysis.lambda_delivery import _seconds_since_cut, _steady_target_mask


def _walk(
    times: npt.NDArray[np.float64],
    target: npt.NDArray[np.float64],
    cut: npt.NDArray[np.bool_],
    *,
    dwell_s: float,
    max_gap_s: float,
    tolerance: float,
) -> tuple[npt.NDArray[np.bool_], npt.NDArray[np.float64]]:
    """The pre-R05.12 loop, verbatim in substance."""
    n = len(times)
    steady = np.zeros(n, dtype=bool)
    since_cut = np.full(n, np.inf)
    last_cut = -np.inf
    for i in range(n):
        if cut[i]:
            last_cut = times[i]
        since_cut[i] = times[i] - last_cut
        if not np.isfinite(target[i]):
            continue
        start = times[i] - dwell_s
        ok = True
        reached = times[i] <= start
        j = i
        while j > 0 and ok and not reached:
            if times[j] - times[j - 1] > max_gap_s:
                ok = False
                break
            j -= 1
            if cut[j] or not np.isfinite(target[j]):
                ok = False
                break
            if abs(target[j] - target[i]) > tolerance:
                ok = False
                break
            reached = times[j] <= start
        steady[i] = ok and reached
    return steady, since_cut


@pytest.mark.parametrize("seed", range(12))
def test_vectorized_mask_matches_the_walk(seed: int) -> None:
    rng = np.random.default_rng(seed)
    n = int(rng.integers(1, 400))
    steps = rng.choice([0.0, 0.05, 0.1, 0.2, 0.9], size=n, p=[0.05, 0.4, 0.4, 0.1, 0.05])
    times = 1_758_000_000.0 + np.cumsum(steps)
    levels = rng.choice([0.80, 0.85, 1.0, 1.001, 1.004], size=n)
    target = np.where(rng.random(n) < 0.7, np.repeat(levels[:: max(1, n // 20)], 20)[:n], levels)
    target[rng.random(n) < 0.03] = np.nan
    cut = rng.random(n) < 0.02
    kwargs = {
        "dwell_s": float(rng.choice([0.0, 0.3, 0.5, 1.0])),
        "max_gap_s": float(rng.choice([0.15, 0.25, 0.5])),
        "tolerance": float(rng.choice([0.0, 0.002, 0.005])),
    }
    expected_steady, expected_since = _walk(times, target, cut, **kwargs)
    assert np.array_equal(_steady_target_mask(times, target, cut, **kwargs), expected_steady)
    assert np.array_equal(_seconds_since_cut(times, cut), expected_since)


def test_empty_trace() -> None:
    empty = np.array([], dtype=float)
    mask = _steady_target_mask(
        empty, empty, np.array([], dtype=bool), dwell_s=0.5, max_gap_s=0.25, tolerance=0.0
    )
    assert mask.size == 0
