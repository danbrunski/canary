"""Tests for structured logging."""

import json
import logging
import sys
from typing import Any

from src.log import JSONFormatter, configure_logging


def _record(**overrides: Any) -> logging.LogRecord:
    base: dict[str, Any] = dict(
        name="test",
        level=logging.INFO,
        pathname="x.py",
        lineno=1,
        msg="hello",
        args=None,
        exc_info=None,
    )
    base.update(overrides)
    return logging.LogRecord(**base)


def test_formatter_produces_valid_json_with_canonical_fields():
    record = _record(msg="hello %s", args=("world",))
    payload = json.loads(JSONFormatter().format(record))
    assert payload["level"] == "INFO"
    assert payload["logger"] == "test"
    assert payload["message"] == "hello world"
    assert "timestamp" in payload


def test_formatter_includes_extras():
    record = _record()
    record.custom_field = "value"
    record.numeric_field = 42
    payload = json.loads(JSONFormatter().format(record))
    assert payload["custom_field"] == "value"
    assert payload["numeric_field"] == 42


def test_formatter_includes_exception():
    try:
        raise ValueError("oops")
    except ValueError:
        exc_info = sys.exc_info()
    record = _record(level=logging.ERROR, msg="boom", exc_info=exc_info)
    payload = json.loads(JSONFormatter().format(record))
    assert "ValueError" in payload["exception"]
    assert "oops" in payload["exception"]


def test_configure_logging_replaces_handlers():
    root = logging.getLogger()
    original_level = root.level
    original_handlers = list(root.handlers)
    try:
        configure_logging("INFO")
        first = list(root.handlers)
        configure_logging("DEBUG")
        second = list(root.handlers)
        assert len(second) == 1
        assert first[0] is not second[0]
        assert root.level == logging.DEBUG
    finally:
        # configure_logging mutates the root logger globally with no
        # teardown of its own (that's correct for real startup use) — undo
        # it here so later tests don't inherit a permissive root level,
        # which lets their own caplog.at_level(...) scoping silently pick
        # up log records emitted before it, e.g. test_model_manager.py's
        # test_poll_swap_logs_the_new_manifests_provenance capturing an
        # extra "model swapped" line from a setup call made before its
        # own `with caplog.at_level(...)` block.
        root.setLevel(original_level)
        root.handlers.clear()
        root.handlers.extend(original_handlers)
