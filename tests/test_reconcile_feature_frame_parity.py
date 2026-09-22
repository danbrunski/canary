"""Built-once reconciliation must equal the insert-and-mutate form it replaced.

`reconcile_feature_frame_to_spec` aligns every live row to a model's persisted
feature spec before scoring. It used to copy the row and then insert each fill
and re-assign each coerced column in place. On a one-row ~330-column frame that
was ~650 block-manager writes (``iset`` / ``_iset_split_block`` /
``blocks.delete``) per tick, and they were the whole cost. D01.10 finishes every
column first and builds the frame once.

The output is the model input, so "equivalent" is not good enough and neither
is `allclose`. The reference below is the verbatim pre-change code, and outputs
must match **exactly**: column order and dtypes, index, NaN positions, and the
reconciliation report. One ULP of drift is a parity change and should fail here
first.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from typing import Any

import numpy as np
import pandas as pd
import pytest

from src.preprocessing.feature_pipeline import (
    FeatureSpecReconciliation,
    _resolve_structural_columns,
    candidate_feature_columns,
    reconcile_feature_frame_to_spec,
)
from src.preprocessing.segmenter import MODE_COLUMN


def _reference_reconcile(
    frame: pd.DataFrame,
    schema: Mapping[str, Any],
    *,
    structural_columns: Sequence[str] | None = None,
    fill_missing: float = float("nan"),
    coerce_numeric: bool = True,
) -> tuple[pd.DataFrame, FeatureSpecReconciliation]:
    """Verbatim insert-and-mutate implementation, as it stood before D01.10."""
    feature_columns = tuple(str(column) for column in schema.get("feature_columns", ()))
    if not feature_columns:
        raise ValueError("feature schema has no feature_columns")

    out = frame.copy()
    present = {str(column) for column in out.columns}
    missing = tuple(column for column in feature_columns if column not in present)
    for column in missing:
        out[column] = fill_missing

    dtype_spec = schema.get("feature_dtypes", {})
    coerced: list[str] = []
    if coerce_numeric:
        for column in feature_columns:
            before = str(out[column].dtype)
            out[column] = pd.to_numeric(out[column], errors="coerce")
            after = str(out[column].dtype)
            expected_dtype = (
                str(dtype_spec.get(column, "")) if isinstance(dtype_spec, Mapping) else ""
            )
            if before != after or (expected_dtype and after != expected_dtype):
                coerced.append(column)

    extras = tuple(
        column for column in candidate_feature_columns(out) if column not in feature_columns
    )
    structural = _resolve_structural_columns(out, schema, structural_columns)
    output_columns = tuple(dict.fromkeys([*structural, *feature_columns]))
    aligned = out.loc[:, [column for column in output_columns if column in out.columns]].copy()
    report = FeatureSpecReconciliation(
        row_count=int(len(aligned)),
        expected_feature_columns=feature_columns,
        missing_feature_columns=missing,
        extra_feature_columns=extras,
        filled_feature_columns=missing,
        coerced_feature_columns=tuple(coerced),
        output_columns=tuple(str(column) for column in aligned.columns),
    )
    return aligned, report


def _assert_identical(
    expected: tuple[pd.DataFrame, FeatureSpecReconciliation],
    actual: tuple[pd.DataFrame, FeatureSpecReconciliation],
) -> None:
    want, want_report = expected
    got, got_report = actual
    assert list(got.columns) == list(want.columns), "column order changed"
    assert got.columns.dtype == want.columns.dtype
    assert got.index.equals(want.index) and got.index.dtype == want.index.dtype
    for column in want.columns:
        w, g = want[column], got[column]
        assert w.dtype == g.dtype, f"{column}: dtype {w.dtype} != {g.dtype}"
        if pd.api.types.is_numeric_dtype(w) and not pd.api.types.is_bool_dtype(w):
            assert np.array_equal(w.to_numpy(), g.to_numpy(), equal_nan=True), column
        else:
            assert w.equals(g), column
    pd.testing.assert_frame_equal(got, want, check_exact=True)
    assert got_report == want_report


def _live_like_frame(n_rows: int, n_features: int, *, seed: int = 0) -> pd.DataFrame:
    """A featurized-row-shaped frame with the awkward cases in it: structural
    columns, a string-typed numeric, an int column, NaN holes, gap flags, and a
    channel the schema does not expect."""
    rng = np.random.default_rng(seed)
    data: dict[str, object] = {
        "timestamp": pd.date_range("2026-09-21T12:00:00Z", periods=n_rows, freq="1s"),
        "session": ["live"] * n_rows,
        MODE_COLUMN: ["idle"] * n_rows,
    }
    for c in range(n_features):
        values = rng.random(n_rows) * 100.0
        if c % 7 == 1:
            values[::2] = np.nan
        data[f"obd:CHAN{c}"] = values
        data[f"obd:CHAN{c}__mean"] = values * 0.5
        data[f"obd:CHAN{c}__gap"] = np.zeros(n_rows, dtype=np.int8)
    data["obd:AS_TEXT"] = [f"{800 + i}.5" for i in range(n_rows)]
    data["obd:AS_INT"] = np.arange(n_rows, dtype=np.int64)
    data["obd:UNEXPECTED"] = rng.random(n_rows)
    return pd.DataFrame(data)


def _schema(frame: pd.DataFrame, *, n_missing: int) -> dict[str, Any]:
    present = [c for c in frame.columns if c.startswith("obd:") and not c.endswith("__gap")]
    expected = [c for c in present if c != "obd:UNEXPECTED"]
    expected += [f"obd:ABSENT{i}" for i in range(n_missing)]
    return {
        "feature_columns": expected,
        "feature_dtypes": dict.fromkeys(expected, "float64"),
        "mode_column": MODE_COLUMN,
        "timestamp_column": "timestamp",
        "session_column": "session",
    }


KWARGS: list[dict[str, Any]] = [
    {},
    {"coerce_numeric": False},
    {"structural_columns": ["timestamp"]},
    {"structural_columns": []},
    {"fill_missing": 0.0},
]


@pytest.mark.parametrize("kwargs", KWARGS, ids=lambda k: ",".join(k) or "default")
@pytest.mark.parametrize("shape", [(1, 160), (1, 3), (25, 12), (0, 4)])
def test_matches_insert_and_mutate_exactly(shape: tuple[int, int], kwargs: dict[str, Any]) -> None:
    frame = _live_like_frame(*shape)
    schema = _schema(frame, n_missing=6)
    _assert_identical(
        _reference_reconcile(frame, schema, **kwargs),
        reconcile_feature_frame_to_spec(frame, schema, **kwargs),
    )


def test_matches_on_a_non_default_index() -> None:
    """Scoring hands over a slice of a larger frame; its index must survive."""
    frame = _live_like_frame(10, 5).iloc[[7, 3, 3, 9]]
    schema = _schema(frame, n_missing=2)
    _assert_identical(
        _reference_reconcile(frame, schema), reconcile_feature_frame_to_spec(frame, schema)
    )


def test_extras_are_judged_against_the_filled_frame() -> None:
    """A filled ``decoded:`` column still de-duplicates its ``dbc:`` twin.

    ``candidate_feature_columns`` drops ``dbc:X`` when ``decoded:X`` is present,
    and the old code judged extras *after* inserting fills. So a ``dbc:`` twin of
    an expected-but-missing ``decoded:`` feature was never reported as extra.
    Building from finished columns must keep that.
    """
    frame = pd.DataFrame(
        {
            "timestamp": pd.date_range("2026-09-21T12:00:00Z", periods=1, freq="1s"),
            "obd:RPM": [800.0],
            "dbc:NOT_A_BOUND_SIGNAL": [3.0],
        }
    )
    schema = {"feature_columns": ["obd:RPM", "decoded:NOT_A_BOUND_SIGNAL"]}
    expected = _reference_reconcile(frame, schema)
    actual = reconcile_feature_frame_to_spec(frame, schema)
    _assert_identical(expected, actual)
    assert "dbc:NOT_A_BOUND_SIGNAL" not in actual[1].extra_feature_columns


def test_output_does_not_alias_the_input() -> None:
    frame = _live_like_frame(1, 4)
    schema = _schema(frame, n_missing=1)
    aligned, _ = reconcile_feature_frame_to_spec(frame, schema)
    aligned.loc[aligned.index[0], "obd:CHAN0"] = -1.0
    assert frame["obd:CHAN0"].iloc[0] != -1.0


def test_filled_columns_do_not_share_storage() -> None:
    """Every missing key is built from one shared fill Series; the output
    columns must still be independent of each other."""
    frame = _live_like_frame(2, 3)
    schema = _schema(frame, n_missing=3)
    aligned, _ = reconcile_feature_frame_to_spec(frame, schema)
    aligned.loc[aligned.index[0], "obd:ABSENT0"] = 5.0
    assert np.isnan(aligned["obd:ABSENT1"].iloc[0])
    assert np.isnan(aligned["obd:ABSENT2"].iloc[0])


def test_duplicate_columns_are_refused() -> None:
    frame = pd.DataFrame([[1.0, 2.0]], columns=["obd:RPM", "obd:RPM"])
    with pytest.raises(ValueError, match="duplicate columns: obd:RPM"):
        reconcile_feature_frame_to_spec(frame, {"feature_columns": ["obd:RPM"]})
