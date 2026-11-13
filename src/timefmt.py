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

R24.4 adds the coercion and epoch-formatting helpers that were copied per file
(``_aware`` x8, ``_utc`` x2, ``_iso`` x13). The copies disagreed, so each
behaviour is a separate named function rather than one with a flag that
could be forgotten:

* :func:`as_utc` -- naive is UTC, aware is *converted* to UTC.
* :func:`assume_utc` -- naive is UTC, aware is *left alone* (its own offset
  kept), for sites that only need a comparable aware value.
* :func:`utc_datetime` -- the pandas flavor: any stamp-like value, floored to
  microseconds, returned as a ``datetime``.
* :func:`format_epoch` -- epoch seconds to ISO text, with the suffix and
  precision of the stored artifact named by the caller.
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


def as_utc(moment: datetime) -> datetime:
    """``moment`` in UTC: a naive value is taken as UTC, an aware one is
    converted (``astimezone``), so the same instant always carries ``+00:00``."""
    return moment.replace(tzinfo=UTC) if moment.tzinfo is None else moment.astimezone(UTC)


def assume_utc(moment: datetime) -> datetime:
    """``moment`` with UTC attached when it is naive; an aware value is
    returned unchanged, keeping its own offset.

    Use it where the value is only compared or subtracted. Where the text is
    stored, :func:`as_utc` is the one that makes the offset uniform."""
    return moment if moment.tzinfo is not None else moment.replace(tzinfo=UTC)


def utc_datetime(value: datetime | pd.Timestamp | str) -> datetime:
    """A tz-aware UTC ``datetime`` from a stamp-like value (naive is UTC),
    floored to microseconds.

    Record times are microsecond-resolution; the explicit floor stops pandas
    warning about discarded nanoseconds on every session. Strings go through
    ``pd.Timestamp``, so they accept more forms than :func:`parse_iso_utc`.
    """
    stamp = pd.Timestamp(value)
    stamp = stamp.tz_localize(UTC) if stamp.tzinfo is None else stamp.tz_convert(UTC)
    return stamp.floor("us").to_pydatetime()


def format_epoch(seconds: float, *, zulu: bool = False, nanoseconds: bool = False) -> str:
    """Epoch seconds as ISO-8601 UTC text.

    The default is ``datetime.isoformat`` with a ``+00:00`` offset and
    microsecond resolution. The two flags exist because stored artifacts
    already fix the form and a change would break their readers and hashes:

    * ``zulu`` writes ``Z`` for ``+00:00``;
    * ``nanoseconds`` goes through ``pd.Timestamp(seconds, unit="s")``, which
      keeps a float's nanosecond noise (``...49.999135494``) where the default
      rounds to microseconds (``...49.999135``).
    """
    if nanoseconds:
        text = pd.Timestamp(seconds, unit="s", tz="UTC").isoformat()
    else:
        text = datetime.fromtimestamp(seconds, tz=UTC).isoformat()
    return text.replace("+00:00", "Z") if zulu else text
