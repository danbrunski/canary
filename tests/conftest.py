"""Shared pytest fixtures.

Session-wide guards that keep tests independent of each other.
"""

from __future__ import annotations

import logging
from collections.abc import Iterator

import pytest


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
