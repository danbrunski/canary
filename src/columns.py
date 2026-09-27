"""Frame-column naming conventions shared by every layer (R09.2).

The names the preprocessing chain gives the columns of a wide telemetry frame:
the session and timestamp keys, the operating-mode label, the resampler's
per-channel bookkeeping flags (``__gap``, ``__arrived``) and the rolling
features' suffixes (``__mean``, ``__std``, ``__roc``). They used to live in
``src.preprocessing.{resampler,segmenter,features}``, so most packages that
imported those modules did so only for a column name. Here they sit at layer 0
(``tests/test_layering.py``), beneath everything that reads a frame.

Pure names and string helpers only: no pandas, no config.
"""

from __future__ import annotations

SESSION_COLUMN = "session"
TIMESTAMP_COLUMN = "timestamp"
GAP_SUFFIX = "__gap"
#: A03.1 — per-cell "a sample arrived this tick" flag. ``True`` at grid row
#: ``t`` when at least one (quality-gated, numeric) sample of the channel has a
#: timestamp in ``(t - interval, t]``: the sample was delivered by the row's own
#: time, since the previous row. ``__gap`` answers a different question ("is
#: the newest sample younger than ``max_gap``?") and stays the dead-sensor
#: signal; the two differ on every forward-filled row inside ``max_gap``.
#: The live source (``LiveFeatureSource``) emits the same flag for the same
#: stream: "the cell's value was delivered after the previous tick".
ARRIVED_SUFFIX = "__arrived"
#: Every per-channel bookkeeping suffix the resampler emits. A column ending in
#: one of these is never a value channel: use :func:`is_flag_column` rather
#: than testing one suffix, so a new flag cannot leak into rolling features,
#: scalers or model schemas at the one call site that forgot it.
FLAG_SUFFIXES: tuple[str, ...] = (GAP_SUFFIX, ARRIVED_SUFFIX)

MODE_COLUMN = "operating_mode"
UNKNOWN_MODE = "unknown"

MEAN_SUFFIX = "__mean"
STD_SUFFIX = "__std"
ROC_SUFFIX = "__roc"

#: Every rolling-feature suffix ``src.preprocessing.features`` appends. The one
#: declaration: consumers split a column with :func:`split_rolling` /
#: :func:`base_channel` rather than re-listing the suffixes (R04.3) -- a copy
#: that missed a newly added rolling feature would lose its freshness mask at
#: serve, leak around ``suppress_channels`` and misfile its calibration family,
#: all silently.
ROLLING_SUFFIXES: tuple[str, ...] = (MEAN_SUFFIX, STD_SUFFIX, ROC_SUFFIX)


def gap_column(channel: str) -> str:
    """Return the boolean gap-flag column name for a channel."""
    return f"{channel}{GAP_SUFFIX}"


def arrived_column(channel: str) -> str:
    """Return the boolean "sample arrived this tick" column name (A03.1)."""
    return f"{channel}{ARRIVED_SUFFIX}"


def is_flag_column(column: object) -> bool:
    """``True`` for per-channel bookkeeping columns (``__gap``, ``__arrived``)."""
    return str(column).endswith(FLAG_SUFFIXES)


def split_rolling(column: str) -> tuple[str, str]:
    """``"<base>__roc"`` -> ``("<base>", "__roc")``; a raw column -> ``(column, "")``."""
    for suffix in ROLLING_SUFFIXES:
        if column.endswith(suffix):
            return column[: -len(suffix)], suffix
    return column, ""


def base_channel(column: str) -> str:
    """The channel a rolling feature summarizes (the column itself when raw).

    Also the sequence detector's *family* key: a channel and its rolling
    siblings share one calibration band.
    """
    return split_rolling(column)[0]


def mean_column(channel: str) -> str:
    return f"{channel}{MEAN_SUFFIX}"


def std_column(channel: str) -> str:
    return f"{channel}{STD_SUFFIX}"


def roc_column(channel: str) -> str:
    return f"{channel}{ROC_SUFFIX}"


__all__ = [
    "ARRIVED_SUFFIX",
    "FLAG_SUFFIXES",
    "GAP_SUFFIX",
    "MEAN_SUFFIX",
    "MODE_COLUMN",
    "ROC_SUFFIX",
    "ROLLING_SUFFIXES",
    "SESSION_COLUMN",
    "STD_SUFFIX",
    "TIMESTAMP_COLUMN",
    "UNKNOWN_MODE",
    "arrived_column",
    "base_channel",
    "gap_column",
    "is_flag_column",
    "mean_column",
    "roc_column",
    "split_rolling",
    "std_column",
]
