"""Long telemetry rows to a wide frame, and the numeric coercion around it (R13.2).

Row-shape transforms with no reader and no policy: :func:`to_wide` pivots a
long frame (the shape ``session_reader.read_session`` returns) to one column
per ``source:channel``; :func:`coerce_numeric_columns` is its numeric-cast
step, split out so the bounded session loader applies the identical rule part
by part. They depend only on the frame contracts, never on a reader, a cache
or the IMU services, which is what let the ``loader`` ↔ ``derived`` cycle go.
"""

from __future__ import annotations

from collections.abc import Iterable, Sequence

import pandas as pd

from src.columns import MODE_COLUMN, SESSION_COLUMN, TIMESTAMP_COLUMN, is_flag_column
from src.preprocessing.session_contracts import INDEX_COLUMNS, TelemetryValueKind
from src.storage.broadcast_storage import FRAME_ATTR


def to_wide(
    telemetry: pd.DataFrame,
    *,
    value: TelemetryValueKind = "value",
    index_columns: Sequence[str] = INDEX_COLUMNS,
    coerce_numeric: bool = True,
) -> pd.DataFrame:
    """Pivot long telemetry rows to one column per `source:channel`.

    The `value` column is `object`-dtype (it coalesces numeric / text / bool
    values per row), so the pivot output is `object` too — which means the
    downstream resampler and feature stages would treat even genuinely
    numeric channels (OBD PIDs, decoded CAN signals) as non-numeric and skip
    them. With `coerce_numeric=True` (the default), each value column whose
    non-null entries are all numbers is cast to a numeric dtype after the
    pivot. Channels carrying strings (VIN) or bools are left untouched.
    """
    if telemetry.empty:
        return pd.DataFrame(columns=[*index_columns])

    required = {*index_columns, "source_channel", value}
    missing = required - set(telemetry.columns)
    if missing:
        raise KeyError(f"missing required columns: {', '.join(sorted(missing))}")

    pivoted = telemetry.pivot_table(
        index=list(index_columns),
        columns="source_channel",
        values=value,
        aggfunc="last",
        dropna=False,
    )
    pivoted.columns = [str(column) for column in pivoted.columns]
    wide = pivoted.reset_index().sort_values(list(index_columns), kind="stable")

    if coerce_numeric:
        coerce_numeric_columns(wide, index_columns=index_columns)
    # P02.10: the resampler refuses a broadcast class shorter than the timeout
    # the rows were stored with; the pivot drops ``attrs``, so carry it.
    if FRAME_ATTR in telemetry.attrs:
        wide.attrs[FRAME_ATTR] = telemetry.attrs[FRAME_ATTR]
    return wide


def non_numeric_columns(
    wide: pd.DataFrame, *, index_columns: Sequence[str] = INDEX_COLUMNS
) -> set[str]:
    """Object columns :func:`coerce_numeric_columns` would leave un-coerced:
    some non-null entry is not a number (VIN text, a bool channel)."""
    index_set = set(index_columns)
    out: set[str] = set()
    for column in wide.columns:
        if column in index_set or pd.api.types.is_numeric_dtype(wide[column]):
            continue
        col = wide[column]
        converted = pd.to_numeric(col, errors="coerce")
        if not bool((col.notna() == converted.notna()).all()):
            out.add(str(column))
    return out


def coerce_numeric_columns(
    wide: pd.DataFrame,
    *,
    index_columns: Sequence[str] = INDEX_COLUMNS,
    keep_object: Iterable[str] = (),
) -> pd.DataFrame:
    """Cast every all-numeric value column of ``wide`` to float32, in place.

    :func:`to_wide`'s ``coerce_numeric`` step, split out so the bounded
    session loader applies the identical rule part by part. ``keep_object``
    names columns to leave as they are: a column that is all-numeric in one
    part but carries text elsewhere in the session stays ``object``, as the
    whole-session pivot leaves it. Returns ``wide``.
    """
    index_set = set(index_columns)
    held = set(keep_object)
    for column in wide.columns:
        if column in index_set or column in held:
            continue
        col = wide[column]
        if pd.api.types.is_numeric_dtype(col):
            # Already numeric (rare on the object-valued read_session
            # path; happens when to_wide is called with a numeric
            # `value` kind). Narrow float64 → float32; leave int / bool
            # channels untouched so their semantics don't change.
            if col.dtype == "float64":
                wide[column] = col.astype("float32")
            continue
        converted = pd.to_numeric(col, errors="coerce")
        # Only keep the conversion if it didn't drop any real values —
        # i.e. every non-null original entry was numeric. A string-bearing
        # channel (VIN) or a bool channel stays as-is.
        original_notna = col.notna()
        if bool((original_notna == converted.notna()).all()):
            # Store as float32: telemetry is integer-origin sensor data
            # (≤16-bit raw range, simple scale/offset) — exactly
            # representable in a 24-bit mantissa, so this is lossless for
            # the values that matter. This is the dominant pre-resample
            # memory cost (the dense pivot); halving it is what actually
            # keeps full-corpus feature-building off the OOM line.
            # Scaler μ/σ still reduce in float64 (see normalization.py).
            wide[column] = converted.astype("float32")
    return wide


def numeric_value_columns(frame: pd.DataFrame) -> list[str]:
    """Numeric channel columns, excluding structural columns and gap flags.

    Canonical selection shared by every batch preprocessing path that walks
    from a wide/resampled/segmented frame to a feature-column list
    (sequence windowing, session classification). Two independent copies of
    this had drifted apart — one excluded ``__gap`` flag columns, the other
    didn't — which let boolean gap flags leak into session-classifier
    quality stats as if they were real feature channels.
    """
    skip = {SESSION_COLUMN, TIMESTAMP_COLUMN, MODE_COLUMN}
    return [
        str(column)
        for column in frame.columns
        if str(column) not in skip
        and not is_flag_column(column)
        and pd.api.types.is_numeric_dtype(frame[column])
    ]
