"""R01.4: one "numeric column or NaN" helper."""

from __future__ import annotations

import numpy as np
import pandas as pd

from src.dataframe_utils import numeric_column, numeric_values


def test_missing_column_is_frame_length_nan_on_the_frame_index() -> None:
    """The fingerprint copy returned an *empty* series here; the other five
    returned frame-length NaN, which is the one callers can align."""
    frame = pd.DataFrame({"a": [1, 2, 3]}, index=[10, 11, 12])

    out = numeric_column(frame, "missing")

    assert len(out) == 3
    assert list(out.index) == [10, 11, 12]
    assert out.isna().all()
    assert out.dtype == float


def test_present_column_coerces_unparseable_cells_to_nan() -> None:
    frame = pd.DataFrame({"a": [1, "x", None, "2.5"]})

    np.testing.assert_array_equal(numeric_values(frame, "a"), [1.0, np.nan, np.nan, 2.5])


def test_nullable_dtypes_become_plain_float() -> None:
    frame = pd.DataFrame({"a": pd.array([1.0, None], dtype="Float64")})

    out = numeric_values(frame, "a")

    assert out.dtype == float
    assert out[0] == 1.0 and np.isnan(out[1])
