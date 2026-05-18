"""Shared DataFrame helpers.

`_require_columns` was copy-pasted into five preprocessing/model modules
verbatim, plus a sixth (anomaly_injection) that had drifted to a different
KeyError message format. One definition now; the canonical message wins.
"""

from __future__ import annotations

from collections.abc import Iterable

import pandas as pd


def require_columns(frame: pd.DataFrame, columns: Iterable[str]) -> None:
    """Raise ``KeyError`` if any of ``columns`` is absent from ``frame``."""
    missing = set(columns) - set(frame.columns)
    if missing:
        raise KeyError(f"missing required columns: {', '.join(sorted(missing))}")
