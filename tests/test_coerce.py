"""R01.3: one "finite number, or None" rule."""

from __future__ import annotations

import math

import numpy as np
import pandas as pd
import pytest

from src.coerce import finite_float


@pytest.mark.parametrize(
    ("value", "expected"),
    [
        (0, 0.0),
        (1.5, 1.5),
        (np.float32(2.5), 2.5),
        (np.int64(7), 7.0),
        (" 1.5 ", 1.5),
        ("12", 12.0),
    ],
)
def test_finite_values_pass(value: object, expected: float) -> None:
    assert finite_float(value) == expected


@pytest.mark.parametrize(
    "value",
    [
        None,
        math.nan,
        math.inf,
        -math.inf,
        np.nan,
        pd.NA,
        pd.NaT,
        "",
        "x",
        "nan",
        "inf",
        True,
        False,
        object(),
        [1.0],
    ],
)
def test_everything_else_is_none(value: object) -> None:
    """The copies disagreed here: three let +-inf through (``autotag_hook._num``,
    ``replay_window._float_or_none``, ``cold_start._maybe_float``), two let NaN
    through, and only two refused ``bool``."""
    assert finite_float(value) is None


def test_the_old_daemon_export_is_the_same_function() -> None:
    from src.daemon import _finite_or_none

    assert _finite_or_none is finite_float
