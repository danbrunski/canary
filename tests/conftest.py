"""Shared pytest fixtures.

Session-wide guards that keep tests independent of each other.
"""

from __future__ import annotations

import logging
import os
import zlib
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest
from _pytest.mark.expression import Expression

from src.config import CanaryConfig
from tests import repo_guard
from tests.repo_guard import GUARD, RepoTreeGuard

#: Tests measured slower than the push gate can afford (scripts/mark_slow_tests.py
#: writes this from a ``--durations`` run). They get the ``slow`` marker here, so
#: the one-minute push gate and per-push CI skip them; a nightly workflow runs
#: them (operator, 2026-10-04; .github/workflows/nightly-slow.yml).
_SLOW_TESTS_FILE = Path(__file__).with_name("slow_tests.txt")


def _measured_slow_tests() -> frozenset[str]:
    if not _SLOW_TESTS_FILE.exists():
        return frozenset()
    return frozenset(
        line.split()[0]
        for line in _SLOW_TESTS_FILE.read_text().splitlines()
        if line.strip() and not line.startswith("#")
    )


#: ``CANARY_TEST_SHARD=<index>/<count>`` keeps one share of the collected tests,
#: chosen by a stable hash of each test's node id, so CI can split the suite
#: across parallel jobs (operator, 2026-10-04). Unset, nothing is deselected.
SHARD_ENV = "CANARY_TEST_SHARD"


def parse_shard(value: str) -> tuple[int, int]:
    """``"i/n"`` -> ``(i, n)``; refuses anything but ``0 <= i < n``."""
    try:
        index_text, count_text = value.split("/")
        index, count = int(index_text), int(count_text)
    except ValueError as exc:
        raise pytest.UsageError(f"{SHARD_ENV}={value!r}: expected '<index>/<count>'") from exc
    if count < 1 or not 0 <= index < count:
        raise pytest.UsageError(f"{SHARD_ENV}={value!r}: need 0 <= index < count")
    return index, count


def in_shard(nodeid: str, index: int, count: int) -> bool:
    return zlib.crc32(nodeid.encode()) % count == index


def pytest_collection_modifyitems(config: pytest.Config, items: list[pytest.Item]) -> None:
    slow = _measured_slow_tests()
    for item in items:
        if item.nodeid in slow:
            item.add_marker(pytest.mark.slow)
    shard = os.environ.get(SHARD_ENV)
    if shard:
        index, count = parse_shard(shard)
        keep = [item for item in items if in_shard(item.nodeid, index, count)]
        dropped = [item for item in items if not in_shard(item.nodeid, index, count)]
        if dropped:
            config.hook.pytest_deselected(items=dropped)
        items[:] = keep


#: ``-n auto`` for a run that selects the ``ml_training`` tier spawns at most
#: this many workers (T01.18). One bake-off test alone peaks at 1.76 GB RSS
#: (``test_sequence_bakeoff.py::test_main_writes_json``, ``/usr/bin/time -v``,
#: 2026-10-05), so twelve torch workers on this 15 GB laptop exhausted RAM and
#: all 8 GB of swap beside another session's suite on 2026-10-04: the kernel's
#: OOM killer took two ~1 GB torch workers (journal, 15:25:33 and 15:26:14
#: MDT) and xdist reported "worker 'gw8' crashed". Four is what the tier has
#: been run at by hand (R07: 122 passed at ``-n 4``). An explicit ``-n N`` or
#: ``PYTEST_XDIST_AUTO_NUM_WORKERS`` still wins.
ML_TRAINING_MAX_WORKERS = 4


def selects_ml_training_tier(markexpr: str) -> bool:
    """True when ``-m`` keeps ``ml_training`` tests but drops unmarked ones.

    That is the torch tier run on its own (``-m ml_training``, ``-m "ml_training
    or slow"``), where every worker trains. A run that keeps everything
    (``-m ""``) spreads the few torch tests among thousands of light ones.
    """
    if not markexpr.strip():
        return False

    def only_ml_training(name: str, /, **_: str | int | bool | None) -> bool:
        return name == "ml_training"

    def unmarked(name: str, /, **_: str | int | bool | None) -> bool:
        return False

    expression = Expression.compile(markexpr)
    return expression.evaluate(only_ml_training) and not expression.evaluate(unmarked)


def pytest_xdist_auto_num_workers(config: pytest.Config) -> int | None:
    if os.environ.get("PYTEST_XDIST_AUTO_NUM_WORKERS"):
        return None
    if not selects_ml_training_tier(config.option.markexpr):
        return None
    return min(ML_TRAINING_MAX_WORKERS, len(os.sched_getaffinity(0)))


_REPO_BASELINE = pytest.StashKey[repo_guard.RunBaseline]()
#: Refusals the xdist workers reported that no test was charged with.
_WORKER_REFUSALS: list[str] = []


def _is_xdist_worker(config: pytest.Config) -> bool:
    return hasattr(config, "workerinput")


def pytest_configure(config: pytest.Config) -> None:
    GUARD.install()


def pytest_sessionstart(session: pytest.Session) -> None:
    if not _is_xdist_worker(session.config):
        session.config.stash[_REPO_BASELINE] = repo_guard.baseline()


@pytest.hookimpl(hookwrapper=True)
def pytest_runtest_protocol(item: pytest.Item, nextitem: pytest.Item | None) -> Iterator[None]:
    GUARD.current_test = item.nodeid
    try:
        yield
    finally:
        GUARD.current_test = None


@pytest.hookimpl(optionalhook=True)
def pytest_testnodedown(node: Any, error: Any) -> None:
    """xdist: collect a worker's uncharged refusals (see ``pytest_sessionfinish``)."""
    _WORKER_REFUSALS.extend(getattr(node, "workeroutput", {}).get("repo_guard_refusals", []))


def pytest_sessionfinish(session: pytest.Session, exitstatus: int) -> None:
    """Fail the run on a repo-tree write no single test was charged with.

    - Refusals outside a test phase (collection, a module fixture's teardown
      after the last test's check). A worker cannot fail the run itself, so
      it hands them to the controller through ``workeroutput``.
    - On the controller, files under ``data/``/``models/`` that changed during
      the run and can only have been changed by it (a child process or a
      native writer the audit hook cannot see). Changes that may be another
      session's are listed as warnings; see ``repo_guard._attributable``.
    """
    refusals = [
        f"refused ({nodeid or 'outside any test'}): {what}"
        for nodeid, refused in GUARD.violations.items()
        for what in refused
    ]
    if _is_xdist_worker(session.config):
        session.config.workeroutput["repo_guard_refusals"] = refusals  # type: ignore[attr-defined]
        return
    reporter = session.config.pluginmanager.get_plugin("terminalreporter")
    problems = refusals + _WORKER_REFUSALS
    for nodeid, allowed in GUARD.allowed.items():
        if reporter is not None:
            reason = GUARD.allowance_for(nodeid)
            for what in allowed:
                reporter.write_line(f"R07.2 allowance [{reason}] {nodeid}: {what}")
    start = session.config.stash.get(_REPO_BASELINE, None)
    warnings: list[str] = []
    if start is not None:
        failures, warnings = repo_guard.classify_changes(start)
        problems += [f"changed during the run: {change}" for change in failures]
    if reporter is not None and warnings:
        reporter.write_sep("-", "R07.2: changed during the run, possibly by another session")
        for change in warnings:
            reporter.write_line(change)
    if not problems:
        return
    if reporter is not None:
        reporter.write_sep("=", "R07.2: the test run wrote into the repo tree", red=True)
        for problem in problems:
            reporter.write_line(problem)
    if session.exitstatus == pytest.ExitCode.OK:
        session.exitstatus = pytest.ExitCode.TESTS_FAILED


@pytest.fixture(autouse=True)
def _restore_root_logger_state() -> Iterator[None]:
    """Snapshot and restore the root logger after every test.

    ``src.log.configure_logging`` mutates the root logger globally (level +
    handlers) with no teardown of its own — that is correct for real daemon /
    CLI startup, but several tests exercise script ``main()`` entry points
    in-process, so a permissive ``INFO`` root level would otherwise leak into
    whatever test runs next. Under ``pytest-xdist`` the victim varies by
    shard, which made it surface as a flaky CI-only failure in
    ``test_model_manager.py`` (a ``caplog.at_level`` block silently capturing a
    ``model swapped`` line emitted by setup *before* the block).

    Restoring here at the source keeps that class of pollution from ever
    crossing a test boundary, rather than papering over one victim.
    """
    root = logging.getLogger()
    level = root.level
    handlers = list(root.handlers)
    try:
        yield
    finally:
        root.setLevel(level)
        root.handlers[:] = handlers


@pytest.fixture(autouse=True)
def _refuse_writes_into_the_repo_tree(request: pytest.FixtureRequest) -> Iterator[None]:
    """No test may write into ``<repo>/data`` or ``<repo>/models`` (R07.2).

    The audit hook installed in :func:`pytest_configure` refuses the write as
    it happens; this fails the test that attempted it, even when the code under
    test caught the ``PermissionError``. The real training ledger
    (``data/control/training_runs.jsonl``, 4,252 test-grown rows by 2026-09-20)
    is one case of it. Point the path at ``tmp_path``: ``tests/configs.py``
    ``isolated_config`` for daemon configs, ``retrain_argv`` for
    ``scripts/retrain.py``.
    """
    yield
    refused = GUARD.violations.pop(request.node.nodeid, None)
    if refused:
        pytest.fail(
            "test wrote into the repo tree (R07.2):\n  " + "\n  ".join(refused),
            pytrace=False,
        )


@pytest.fixture(autouse=True)
def _reset_collection_process_state() -> Iterator[None]:
    """Every test starts and ends with collection's process-globals at start-up (R07.4).

    The passive gate, the active capture profile, ECM session claims, the
    bus-budget override, the capture ledger and generation, remembered
    discovery results all live in module globals. Twelve
    files reset a different subset by hand, so what a test saw depended on
    which test its xdist worker ran before it.
    """
    from src.collection.process_state import reset_process_state_for_tests

    reset_process_state_for_tests()
    yield
    reset_process_state_for_tests()


@pytest.fixture
def daemon_config(tmp_path: Path) -> CanaryConfig:
    """A config ``run_daemon`` can start on: detectors off, in-process feature
    build, fast model polling, every writable path under ``tmp_path``.

    The shape of ``tests/test_daemon.py``'s ``config`` fixture, which two other
    files used to import (pulling in all 2,800 lines of it) -- but isolated:
    that fixture left ``trip_state`` pointing at ``data/control`` (R07.2).
    """
    from tests.configs import daemon_test_config

    return daemon_test_config(tmp_path)


@pytest.fixture
def repo_write_guard() -> RepoTreeGuard:
    """The repo-tree write guard, for tests that pin the guard itself."""
    return GUARD


@pytest.fixture
def no_trip_end_digest_child(monkeypatch: pytest.MonkeyPatch) -> list[Path]:
    """Stand in for the trip-end digest ``run_daemon`` computes at shutdown.

    ``run_daemon``'s shutdown fallback digests the active session in a fresh
    ``spawn`` child (a new interpreter importing pandas and the analysis
    stack). Under a parallel run beside other load that child alone outlived
    the 5 s these tests gave the whole shutdown: every ``tests/test_daemon.py``
    failure captured for T01 (2026-09-23, load average 12-20 on 12 cores) was
    ``wait_for(daemon_task)`` expiring inside ``compute_digest_off_loop``. The
    digest and its child are tested where they live
    (``tests/test_trip_end_digest.py``); a test of the daemon's wiring only
    needs to know the hook was called. Returns the sessions it was called with.

    Opt in per module with ``pytestmark = pytest.mark.usefixtures(...)`` —
    every module that starts ``run_daemon`` does.
    """
    from src.daemon import runtime

    calls: list[Path] = []

    async def _record(session_dir: Path, _config: Any, **_kwargs: Any) -> None:
        calls.append(session_dir)

    monkeypatch.setattr(runtime, "handle_trip_end_off_loop", _record)
    return calls


@pytest.fixture
def step_free_wall_clock(monkeypatch: pytest.MonkeyPatch) -> None:
    """Make ``time.time()`` a wall clock that cannot step, for this test only.

    This WSL2 host steps ``CLOCK_REALTIME``. On 2026-09-24, beside one busy
    loop per core, it stepped forward +2.93 s every 32.25 s, five times in
    150 s, and every process on the host saw each step at the same monotonic
    instant. The ``time.time()`` read sat between two ``time.monotonic()`` reads
    less than 0.3 ms apart, so a lost GIL or a descheduled thread does not
    explain the steps. ``src.validation.harness.run_scenario_clean`` rightly
    marks any run with a step invalid and reruns it, three attempts at most.
    A 17-30 s run beside a 32 s step period can lose all three, and
    ``test_fuel_map_persists_tuples_and_known_answers_hold`` then failed
    ``evidence["valid"]`` (T01.9). Its claims are about the writer, the pairing
    and the accounting, not the host clock.

    The replacement is the real wall time at fixture setup, advanced by
    ``time.monotonic()``: one clock for every ``time.time()`` read in the
    process, collector thread included. Records are stamped from
    ``time.time()``; live inference judges their freshness against its own
    ``datetime.now()`` (``src.daemon.inference._utc_now``), so that reads the
    same clock, or a host step would age every channel by 2.93 s. Other
    ``datetime.now()`` reads (session names, metadata stamps, the writer's
    flush timer) still see the host clock; none of them is paired with a row.
    What the harness does with a real step is tested by injecting one
    (``tests/test_sustained_validation.py``).
    """
    import time
    from datetime import UTC, datetime

    from src.daemon import inference

    anchor_wall = time.time()
    anchor_monotonic = time.monotonic()
    monotonic = time.monotonic

    def _step_free_time() -> float:
        return anchor_wall + (monotonic() - anchor_monotonic)

    def _step_free_utc_now() -> datetime:
        return datetime.fromtimestamp(_step_free_time(), UTC)

    monkeypatch.setattr(time, "time", _step_free_time)
    monkeypatch.setattr(inference, "_utc_now", _step_free_utc_now)
