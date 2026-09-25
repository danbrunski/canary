"""Shared DataFrame helpers.

`_require_columns` was copy-pasted into five preprocessing/model modules
verbatim, plus a sixth (anomaly_injection) that had drifted to a different
KeyError message format. One definition now; the canonical message wins.

`numeric_column` (R01.4): "this column as floats, or all-NaN when absent" had
six copies. Five returned frame-length NaN for a missing column; the
fingerprint one returned an *empty* series, so a caller could not tell "no
such channel" from "a frame with no rows" by length alone.
"""

from __future__ import annotations

from collections.abc import Iterable

import numpy as np
import pandas as pd


def require_columns(frame: pd.DataFrame, columns: Iterable[str]) -> None:
    """Raise ``KeyError`` if any of ``columns`` is absent from ``frame``."""
    missing = set(columns) - set(frame.columns)
    if missing:
        raise KeyError(f"missing required columns: {', '.join(sorted(missing))}")


def numeric_column(frame: pd.DataFrame, name: str) -> pd.Series:
    """``frame[name]`` coerced to float (unparseable cells NaN), on ``frame``'s
    index; all-NaN of frame length when the column is absent."""
    if name not in frame.columns:
        return pd.Series(np.nan, index=frame.index, dtype=float, name=name)
    return pd.to_numeric(frame[name], errors="coerce").astype(float)


def numeric_values(frame: pd.DataFrame, name: str) -> np.ndarray:
    """:func:`numeric_column` as a float ``ndarray``."""
    return numeric_column(frame, name).to_numpy(dtype=float)
