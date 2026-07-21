"""Shared timestamp formatting.

`_format_timestamp` was copy-pasted verbatim into isolation_forest,
sequence_dataset and lstm_training (and inference, which adds an
empty/None guard on top). One definition; inference keeps its guard as a
thin wrapper.

`parse_iso_utc` (Task 4.3, refactoring.md) is the parse-side counterpart:
the ``datetime.fromisoformat(ts.replace("Z", "+00:00"))`` + naive-assumed-UTC
idiom was hand-rolled at several call sites (``mcp_server/tools/search.py``,
``dashboard/status.py``, ``analysis/replay_window.py``,
``analysis/digest.py``) with slightly different error-handling wrapped
around the identical core. Callers keep their own policy for a bad
timestamp (raise / return ``None`` / skip) — only the parse+normalize core
is shared.
"""

from __future__ import annotations

from datetime import UTC, datetime

import pandas as pd


def format_timestamp(value: datetime | pd.Timestamp) -> str:
    """ISO-8601 in UTC with a ``Z`` suffix (naive input assumed UTC)."""
    timestamp = pd.Timestamp(value)
    if timestamp.tzinfo is None:
        timestamp = timestamp.tz_localize("UTC")
    else:
        timestamp = timestamp.tz_convert("UTC")
    return timestamp.isoformat().replace("+00:00", "Z")


def parse_iso_utc(value: str) -> datetime:
    """Parse an ISO-8601 timestamp (tolerating a trailing ``Z``) into a
    tz-aware UTC ``datetime``.

    Naive input (no ``Z``, no offset) is assumed UTC, matching
    :func:`format_timestamp`'s output convention. Raises ``ValueError`` on
    anything ``datetime.fromisoformat`` can't parse — callers that want a
    ``None``-on-bad-input or skip-on-bad-input fallback catch it themselves,
    since whether a bad timestamp is fatal differs by call site.
    """
    parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    return parsed.replace(tzinfo=UTC) if parsed.tzinfo is None else parsed.astimezone(UTC)
