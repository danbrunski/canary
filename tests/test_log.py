"""Tests for structured logging."""

import json
import logging
import sys
from collections.abc import Iterator
from typing import Any

import pytest
import uvicorn

from src.log import (
    PATH_LOGGERS,
    JSONFormatter,
    TokenRedactionFilter,
    configure_logging,
    install_token_redaction,
    redact_tokens,
)


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


# --- D03.71: the control token must not reach the log ----------------------

# An obvious fake; never a real credential.
FAKE = "abc123-not-a-real-token"


@pytest.fixture
def restore_logging() -> Iterator[None]:
    """configure_logging and uvicorn.Config both rewrite global logger state."""
    names = ["", *PATH_LOGGERS]
    saved = {
        n: (
            list(logging.getLogger(n).handlers),
            list(logging.getLogger(n).filters),
            logging.getLogger(n).level,
            logging.getLogger(n).propagate,
        )
        for n in names
    }
    yield
    for n, (handlers, filters, level, propagate) in saved.items():
        lg = logging.getLogger(n)
        lg.handlers[:] = handlers
        lg.filters[:] = filters
        lg.setLevel(level)
        lg.propagate = propagate


def _access_record(path: str) -> logging.LogRecord:
    """The shape uvicorn's HTTP access log emits: path in ``record.args``."""
    return _record(
        name="uvicorn.access",
        msg='%s - "%s %s HTTP/%s" %d',
        args=("10.0.0.2:51000", "GET", path, "1.1", 101),
    )


def test_access_record_with_token_comes_out_redacted():
    record = _access_record(f"/ws/control?token={FAKE}")
    assert TokenRedactionFilter().filter(record) is True
    assert FAKE not in record.getMessage()
    assert "/ws/control?token=<redacted>" in record.getMessage()
    assert FAKE not in JSONFormatter().format(record)


@pytest.mark.parametrize(
    ("path", "expected"),
    [
        (f"/ws/control?token={FAKE}&x=1", "/ws/control?token=<redacted>&x=1"),
        (f"/a?x=1&token={FAKE}", "/a?x=1&token=<redacted>"),
        (f"/a?access_token={FAKE}", "/a?access_token=<redacted>"),
        (f"/a?TOKEN={FAKE}", "/a?TOKEN=<redacted>"),
        (f"/a?token={FAKE}&token={FAKE}", "/a?token=<redacted>&token=<redacted>"),
    ],
)
def test_token_forms_are_redacted(path: str, expected: str) -> None:
    assert redact_tokens(path) == expected


@pytest.mark.parametrize(
    "path",
    [
        "/status",
        "/telemetry/latest?limit=10",
        "/anomalies?since=2026-11-12T00:00:00Z",
        "/control/tokens",  # a path segment, not a query parameter
        "/a?tokenizer=bpe",
    ],
)
def test_paths_without_a_token_are_unchanged(path: str) -> None:
    record = _access_record(path)
    before = record.getMessage()
    TokenRedactionFilter().filter(record)
    assert record.getMessage() == before
    assert path in record.getMessage()


def test_dict_args_and_message_are_redacted():
    record = _record(msg=f"GET /x?token={FAKE} %(who)s", args=({"who": f"?token={FAKE}"},))
    TokenRedactionFilter().filter(record)
    assert FAKE not in record.getMessage()


def test_install_is_idempotent(restore_logging: None) -> None:
    install_token_redaction()
    install_token_redaction()
    for name in PATH_LOGGERS:
        filters = [
            f for f in logging.getLogger(name).filters if isinstance(f, TokenRedactionFilter)
        ]
        assert len(filters) == 1, name


def test_configure_logging_installs_the_filter_that_uvicorn_keeps(
    restore_logging: None, capsys: pytest.CaptureFixture[str]
) -> None:
    """The daemon's own logging setup, then a real ``uvicorn.Config`` (which
    runs uvicorn's dictConfig over its loggers): a token in the request path
    must not appear in anything those loggers write."""
    configure_logging("INFO")
    uvicorn.Config(lambda scope, receive, send: None, log_level="info")

    access = logging.getLogger("uvicorn.access")
    error = logging.getLogger("uvicorn.error")
    # HTTP request line, and the WebSocket upgrade lines uvicorn writes on the
    # ``uvicorn.error`` logger.
    access.info('%s - "%s %s HTTP/%s" %d', "10.0.0.2:51000", "GET", f"/x?token={FAKE}", "1.1", 200)
    error.info('%s - "WebSocket %s" [accepted]', "10.0.0.2:51000", f"/ws/control?token={FAKE}")
    error.info("connection open")
    out = capsys.readouterr()
    written = out.out + out.err
    assert "/ws/control?token=<redacted>" in written
    assert "/x?token=<redacted>" in written
    assert "connection open" in written
    assert FAKE not in written
    # The access line is uvicorn's own, still readable: its AccessFormatter
    # unpacks ``record.args`` into five fields, and a filter that cleared them
    # dropped the line and printed a traceback whose "Message:" line made the
    # assertions above pass (verifier, 2026-11-12).
    assert "Logging error" not in out.err
    assert '10.0.0.2:51000 - "GET /x?token=<redacted> HTTP/1.1" 200' in out.out


def test_a_real_uvicorn_server_logs_the_request_line_with_the_token_redacted(
    restore_logging: None, capfd: pytest.CaptureFixture[str]
) -> None:
    """End to end: the daemon's logging setup, a real uvicorn server on a free
    port, one request carrying a token and one without. Both access lines are
    written; neither the token nor a logging error is."""
    import asyncio
    import socket
    import urllib.request

    async def app(scope: dict[str, Any], receive: Any, send: Any) -> None:
        if scope["type"] != "http":
            return
        await send({"type": "http.response.start", "status": 200, "headers": []})
        await send({"type": "http.response.body", "body": b"ok"})

    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        port = probe.getsockname()[1]
    configure_logging("INFO")
    server = uvicorn.Server(uvicorn.Config(app, host="127.0.0.1", port=port, log_level="info"))

    def fetch(url: str) -> bytes:
        with urllib.request.urlopen(url, timeout=5) as response:
            body: bytes = response.read()
        return body

    async def run() -> None:
        serving = asyncio.create_task(server.serve())
        while not server.started:
            await asyncio.sleep(0.01)
        for path in (f"/x?token={FAKE}", "/plain"):
            await asyncio.to_thread(fetch, f"http://127.0.0.1:{port}{path}")
        server.should_exit = True
        await serving

    asyncio.run(run())
    out = capfd.readouterr()
    assert FAKE not in out.out + out.err
    assert "Logging error" not in out.err
    assert '"GET /x?token=<redacted> HTTP/1.1" 200' in out.out
    assert '"GET /plain HTTP/1.1" 200' in out.out


def test_root_handler_redacts_what_propagates_to_it(
    restore_logging: None, capsys: pytest.CaptureFixture[str]
) -> None:
    configure_logging("INFO")
    logging.getLogger("some.other.logger").info("GET /x?token=%s", FAKE)
    out = capsys.readouterr().out
    assert "token=<redacted>" in out
    assert FAKE not in out
