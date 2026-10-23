"""Each xdist worker sizes its native thread pools to its share of the cores
(tests/conftest.py ``_limit_native_threads``)."""

from __future__ import annotations

import os

import pytest

from tests.conftest import NATIVE_THREAD_VARIABLES, worker_thread_budget


@pytest.mark.parametrize(
    ("cores", "workers", "budget"),
    [(8, 8, 1), (8, 4, 2), (12, 4, 3), (16, 8, 2), (4, 8, 1), (8, 0, 8)],
)
def test_the_budget_is_each_workers_share_and_never_zero(
    cores: int, workers: int, budget: int
) -> None:
    assert worker_thread_budget(cores, workers) == budget


def _in_a_worker() -> int:
    workers = os.environ.get("PYTEST_XDIST_WORKER_COUNT")
    if workers is None:
        pytest.skip("a serial run keeps every core; the budget applies to xdist workers")
    return int(workers)


def test_a_worker_sets_every_variable_before_the_run() -> None:
    """torch sizes its pool from ``OMP_NUM_THREADS`` when the pool first
    starts, so a variable set before the first test covers it. (Asking torch
    itself would read whatever an earlier test on this worker set.)"""
    _in_a_worker()
    for name in NATIVE_THREAD_VARIABLES:
        assert int(os.environ[name]) >= 1, name


def test_the_blas_pool_was_sized_before_numpy_loaded() -> None:
    """OpenBLAS reads its variable once, as it loads, so this fails if anything
    conftest imports starts loading numpy before ``pytest_configure``."""
    _in_a_worker()
    threadpoolctl = pytest.importorskip("threadpoolctl")
    import numpy  # noqa: F401 -- loads OpenBLAS

    blas = [p for p in threadpoolctl.threadpool_info() if p["user_api"] == "blas"]
    assert blas
    assert {p["num_threads"] for p in blas} == {int(os.environ["OPENBLAS_NUM_THREADS"])}
