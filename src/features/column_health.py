"""Per-column health: the one definition of finite, constant and all-NaN that
detector feature selection and the persisted feature spec both read.
"""

from __future__ import annotations

from typing import Any

import numpy as np
import pandas as pd


def is_constant_column(series: pd.Series) -> bool:
    """True when every finite value in ``series`` is identical (or none exist).

    Same definition as ``health_of_column``'s ``constant`` flag so selection and
    the manifest's health report can never disagree."""
    return bool(health_of_column(series)["constant"])


def health_of_column(series: pd.Series) -> dict[str, Any]:
    values = pd.to_numeric(series, errors="coerce").to_numpy(dtype=np.float64, copy=False)
    finite_mask = np.isfinite(values)
    finite_values = values[finite_mask]
    non_null_count = int(series.notna().sum())
    finite_count = int(finite_mask.sum())
    unique_finite_count = int(pd.Series(finite_values).nunique(dropna=True))
    return {
        "dtype": str(series.dtype),
        "non_null_count": non_null_count,
        "finite_count": finite_count,
        "nan_count": int(series.isna().sum()),
        "non_finite_count": non_null_count - finite_count,
        "unique_finite_count": unique_finite_count,
        "constant": finite_count > 0 and unique_finite_count <= 1,
    }
