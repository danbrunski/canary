"""Structured logging for CANary.

Single-line JSON records on stdout. Reserved LogRecord fields are emitted under
canonical names (`timestamp`, `level`, `logger`, `message`); anything passed via
`logger.info(..., extra={...})` flows through verbatim.
"""

from __future__ import annotations

import json
import logging
import re
import sys
from collections.abc import Mapping
from datetime import UTC, datetime
from typing import Any

_BASELINE_ATTRS = frozenset(logging.LogRecord("", 0, "", 0, "", None, None).__dict__.keys()) | {
    "message",
    "asctime",
}


class JSONFormatter(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        payload: dict[str, Any] = {
            "timestamp": datetime.fromtimestamp(record.created, tz=UTC).isoformat(),
            "level": record.levelname,
            "logger": record.name,
            "message": record.getMessage(),
        }
        if record.exc_info:
            payload["exception"] = self.formatException(record.exc_info)
        for key, value in record.__dict__.items():
            if key not in _BASELINE_ATTRS:
                payload[key] = value
        return json.dumps(payload, default=str)


def configure_logging(level: str = "INFO") -> None:
    """Install the JSON formatter on the root logger.

    Idempotent: existing handlers are replaced, not appended to. Also installs
    the token redaction (D03.71) on the root handler and on uvicorn's loggers.
    """
    root = logging.getLogger()
    root.setLevel(level.upper())
    root.handlers.clear()
    handler = logging.StreamHandler(sys.stdout)
    handler.setFormatter(JSONFormatter())
    handler.addFilter(TokenRedactionFilter())
    root.addHandler(handler)
    install_token_redaction()


# D03.71: the control token rides ``/ws/control?token=...`` (browsers cannot set
# headers on a WebSocket handshake), and uvicorn logs the request line, so the
# daemon's write credential used to sit in ``journalctl -u canary`` on every
# dashboard connect. The redaction lives on the loggers that print request
# paths, so no client -- present or future -- can put a token in a URL and
# have it logged.
_TOKEN_QUERY = re.compile(r"(?<!\w)(\w*token)=[^&\s\"'#]+", re.IGNORECASE)
REDACTED = "<redacted>"

#: Every logger that prints a request path. ``uvicorn.access`` carries the HTTP
#: request line; ``uvicorn.error`` carries ``WebSocket /path?query [accepted]``;
#: ``websockets.*`` print the handshake path at DEBUG. A filter on a logger sees
#: only records created on that exact logger, hence the explicit list.
PATH_LOGGERS = (
    "uvicorn",
    "uvicorn.access",
    "uvicorn.error",
    "websockets.server",
    "websockets.client",
    "websockets.protocol",
)


def redact_tokens(text: str) -> str:
    """``token=abc`` / ``access_token=abc`` -> ``token=<redacted>``, other text untouched."""
    return _TOKEN_QUERY.sub(lambda m: f"{m.group(1)}={REDACTED}", text)


class TokenRedactionFilter(logging.Filter):
    """Redact ``token=`` query values from what a record will print.

    The formatted message decides whether a record holds a token at all. One
    that does is redacted *in place*: the format string and each string
    argument, keeping ``record.args`` the same shape. uvicorn's
    ``AccessFormatter`` unpacks ``record.args`` into five fields, so a record
    rewritten with no arguments lost its access line and printed a "Logging
    error" traceback instead (verifier, 2026-11-12). Only when the key and the
    value are split across the format string and its arguments
    (``"?token=%s" % value``) is the record rewritten to the redacted text with
    no arguments. Records with no token are left untouched.
    """

    def filter(self, record: logging.LogRecord) -> bool:
        try:
            message = record.getMessage()
        except Exception:  # a malformed record is the handler's problem, not ours
            return True
        if redact_tokens(message) == message:
            return True
        if isinstance(record.msg, str):
            record.msg = redact_tokens(record.msg)
        if isinstance(record.args, tuple):
            record.args = tuple(_redact_arg(arg) for arg in record.args)
        elif isinstance(record.args, Mapping):
            record.args = {key: _redact_arg(arg) for key, arg in record.args.items()}
        try:
            in_place = record.getMessage()
        except Exception:  # redacting "token=%s" left an argument with no slot
            in_place = None
        if in_place is None or redact_tokens(in_place) != in_place:
            record.msg = redact_tokens(message)
            record.args = ()
        return True


def _redact_arg(arg: object) -> object:
    return redact_tokens(arg) if isinstance(arg, str) else arg


def install_token_redaction() -> None:
    """Attach :class:`TokenRedactionFilter` to every path-printing logger.

    Idempotent. uvicorn's own ``dictConfig`` (run when a ``uvicorn.Config`` is
    built) replaces handlers but keeps logger filters, so installing before or
    after it both work. The root handler gets the filter too, in
    :func:`configure_logging`, for anything that propagates there.
    """
    for name in PATH_LOGGERS:
        target = logging.getLogger(name)
        if not any(isinstance(f, TokenRedactionFilter) for f in target.filters):
            target.addFilter(TokenRedactionFilter())
