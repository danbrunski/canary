"""Tests for structured logging."""

import json
import logging
import sys

from src.log import JSONFormatter, configure_logging


def _record(**overrides):
    base = dict(
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
    configure_logging("INFO")
    first = list(logging.getLogger().handlers)
    configure_logging("DEBUG")
    second = list(logging.getLogger().handlers)
    assert len(second) == 1
    assert first[0] is not second[0]
    assert logging.getLogger().level == logging.DEBUG
