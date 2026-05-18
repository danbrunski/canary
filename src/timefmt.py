"""Shared timestamp formatting.

`_format_timestamp` was copy-pasted verbatim into isolation_forest,
sequence_dataset and lstm_training (and inference, which adds an
empty/None guard on top). One definition; inference keeps its guard as a
thin wrapper.
"""

from __future__ import annotations

from datetime import datetime

import pandas as pd


def format_timestamp(value: datetime | pd.Timestamp) -> str:
    """ISO-8601 in UTC with a ``Z`` suffix (naive input assumed UTC)."""
    timestamp = pd.Timestamp(value)
    if timestamp.tzinfo is None:
        timestamp = timestamp.tz_localize("UTC")
    else:
        timestamp = timestamp.tz_convert("UTC")
    return timestamp.isoformat().replace("+00:00", "Z")
