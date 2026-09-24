"""Shared pytest fixtures.

Session-wide guards that keep tests independent of each other.
"""

from __future__ import annotations

import logging
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest

from src.jsonl_store import JsonlStore

#: Tests measured slower than the push gate can afford (scripts/mark_slow_tests.py
#: writes this from a ``--durations`` run). They get the ``slow`` marker here, so
#: the one-minute push gate skips them; bare ``pytest`` and GitHub CI still run
#: them on every push (operator, 2026-09-24).
_SLOW_TESTS_FILE = Path(__file__).with_name("slow_tests.txt")


def _measured_slow_tests() -> frozenset[str]:
    if not _SLOW_TESTS_FILE.exists():
        return frozenset()
    return frozenset(
        line.split()[0]
        for line in _SLOW_TESTS_FILE.read_text().splitlines()
        if line.strip() and not line.startswith("#")
    )


def pytest_collection_modifyitems(items: list[pytest.Item]) -> None:
    slow = _measured_slow_tests()
    for item in items:
        if item.nodeid in slow:
            item.add_marker(pytest.mark.slow)


#: ``src.models.training_ledger.DEFAULT_TRAINING_LEDGER_PATH``, spelled out:
#: importing it here would pull ``src.models`` (and torch) into every xdist
#: worker. ``test_conftest_ledger_guard_matches_the_default_path`` pins the two.
_REAL_TRAINING_LEDGER = (
    Path(__file__).resolve().parents[1] / "data" / "control" / "training_runs.jsonl"
).resolve()


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
def _refuse_writes_to_the_real_training_ledger(monkeypatch: pytest.MonkeyPatch) -> None:
    """No test may append to the operator's ``data/control/training_runs.jsonl``.

    ``scripts/retrain.py`` defaults ``--ledger`` to that file and ``--if-out`` /
    ``--sequence-out`` to the deployed model directories. Tests that ran
    ``retrain.main`` with stubbed trainers and no ``--ledger`` therefore
    re-ingested the *deployed* manifests on every run: by 2026-09-20 the ledger
    held 4,252 lines for 40 real runs (419 MB), 1,174 of them carrying a
    ``detector_benchmark_report_path`` inside a pytest tmp dir, and — latest
    line per run wins — each one reset the real run's publish/approval state.

    ``retrain._ingest_ledger`` now refuses a manifest it did not produce. This
    is the second lock: any future path to the real file fails the test that
    took it, instead of relying on every test remembering ``--no-ledger``.
    """
    real_append = JsonlStore._append_record

    def _guarded(self: Any, record: Any, **kwargs: Any) -> Any:
        if Path(self.path).resolve() == _REAL_TRAINING_LEDGER:
            raise AssertionError(
                f"test attempted to append to the real training ledger "
                f"{_REAL_TRAINING_LEDGER}; pass --ledger <tmp_path> / --no-ledger"
            )
        return real_append(self, record, **kwargs)

    monkeypatch.setattr(JsonlStore, "_append_record", _guarded)


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
