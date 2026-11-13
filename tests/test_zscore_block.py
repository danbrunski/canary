"""R27.4: ``ZScoreScaler.transform`` in dtype blocks is bit-identical to the
per-column loop it replaced.

The reference below is that loop, verbatim. Acceptance is the *bytes* of every
value (``tobytes``), never ``allclose``: the block path must compute
``(x - mean) / denom`` per element in the dtypes pandas did — float32 stays
float32 against a Python-float mean, an ``np.float64`` mean promotes it, an
object column keeps each element's own arithmetic — and must never multiply by
a precomputed reciprocal.
"""

from __future__ import annotations

from typing import Any

import numpy as np
import pandas as pd
import pytest

from src.columns import MODE_COLUMN, SESSION_COLUMN, TIMESTAMP_COLUMN
from src.preprocessing.normalization import SchemaMismatchError, ZScoreScaler


def _reference_transform(scaler: ZScoreScaler, df: pd.DataFrame) -> pd.DataFrame:
    """The pre-R27.4 ``transform``: one pandas expression per fitted column."""
    missing = [col for col in scaler.columns if col not in df.columns]
    if missing:
        raise SchemaMismatchError(f"input DataFrame missing fitted columns: {missing}")
    out = df.copy()
    for col in scaler.columns:
        out[col] = (df[col] - scaler.means[col]) / scaler.denominator(col)
    return out


def _element_bytes(value: Any) -> tuple[str, bytes | str]:
    if isinstance(value, (float, np.floating, np.integer, np.bool_, bool, int)):
        return type(value).__name__, np.asarray(value).tobytes()
    return type(value).__name__, repr(value)


def _assert_bit_identical(actual: pd.DataFrame, expected: pd.DataFrame) -> None:
    assert list(actual.columns) == list(expected.columns)
    assert actual.columns.dtype == expected.columns.dtype
    assert actual.index.equals(expected.index)
    assert actual.index.dtype == expected.index.dtype
    for pos in range(expected.shape[1]):
        got = actual.iloc[:, pos]
        want = expected.iloc[:, pos]
        label = expected.columns[pos]
        assert got.dtype == want.dtype, label
        if isinstance(want.dtype, np.dtype) and want.dtype.kind == "O":
            assert [_element_bytes(v) for v in got] == [_element_bytes(v) for v in want], label
        elif isinstance(want.dtype, np.dtype) and want.dtype.kind in "biufcmM":
            assert got.to_numpy().tobytes() == want.to_numpy().tobytes(), label
        else:
            assert got.equals(want), label


def _realistic_frame(rows: int = 240, seed: int = 7) -> tuple[pd.DataFrame, list[str]]:
    """A feature frame shaped like the served one: mostly float32 features with
    dropouts and saturated values, a few float64/int/bool features, and
    structural/passthrough columns interleaved among them."""
    rng = np.random.default_rng(seed)
    data: dict[str, Any] = {
        SESSION_COLUMN: ["2026-09-22T233855"] * rows,
        TIMESTAMP_COLUMN: pd.date_range("2026-09-22T23:38:55", periods=rows, freq="s", tz="UTC"),
    }
    fitted: list[str] = []
    for i in range(60):
        name = f"obd:F32_{i}"
        values = (rng.normal(1000.0 * (i + 1), 37.0 * (i + 1), rows)).astype(np.float32)
        values[rng.random(rows) < 0.15] = np.nan
        data[name] = values
        fitted.append(name)
        if i % 9 == 0:
            data[f"{name}__gap"] = rng.random(rows) < 0.1  # passthrough flag between features
    data["dbc:CONST_F32"] = np.full(rows, 13.5, dtype=np.float32)  # zero std
    data["dbc:SAT_F32"] = np.where(rng.random(rows) < 0.05, np.inf, 1.0).astype(np.float32)
    data[MODE_COLUMN] = rng.choice(["idle", "city", "highway", "unknown"], rows)
    data["obd:F64"] = rng.normal(0.25, 1e-3, rows)
    data["obd:INT64"] = rng.integers(-5000, 5000, rows)
    data["obd:INT8"] = rng.integers(-100, 100, rows).astype(np.int8)
    data["obd:BOOL"] = rng.random(rows) < 0.5
    data["derived:ALL_NAN_F32"] = np.full(rows, np.nan, dtype=np.float32)
    data["passthrough:F32"] = rng.normal(0.0, 1.0, rows).astype(np.float32)
    fitted += [
        "dbc:CONST_F32",
        "dbc:SAT_F32",
        "obd:F64",
        "obd:INT64",
        "obd:INT8",
        "obd:BOOL",
        "derived:ALL_NAN_F32",
    ]
    frame = pd.DataFrame(data)
    frame.index = pd.Index(rng.permutation(np.arange(10_000, 10_000 + rows)))
    return frame, fitted


def _scaler(frame: pd.DataFrame, fitted: list[str], **kwargs: Any) -> ZScoreScaler:
    fit = ZScoreScaler.fit(frame.drop(columns=["derived:ALL_NAN_F32"]), columns=fitted[:-1])
    means = {**fit.means, "derived:ALL_NAN_F32": 0.0}
    stds = {**fit.stds, "derived:ALL_NAN_F32": 1.0}
    stds["obd:F32_3"] = 1e-12  # below the floor: centred, denominator 1.0
    return ZScoreScaler(means=means, stds=stds, columns=tuple(fitted), **kwargs)


def test_batch_frame_is_bit_identical_to_the_per_column_loop() -> None:
    frame, fitted = _realistic_frame()
    scaler = _scaler(frame, fitted)
    assert scaler.denominator("dbc:CONST_F32") == 1.0
    assert scaler.denominator("obd:F32_3") == 1.0
    original = frame.copy()

    out = scaler.transform(frame)

    _assert_bit_identical(out, _reference_transform(scaler, frame))
    _assert_bit_identical(frame, original)  # input untouched
    assert out["obd:F32_0"].dtype == np.float32
    assert out["obd:INT8"].dtype == np.float64
    assert out["obd:BOOL"].dtype == np.float64


def test_one_row_frames_are_bit_identical_typed_and_object() -> None:
    """Given a Series or Mapping row, ``IsolationForestInferenceEngine`` builds
    it as ``row.to_frame().T`` -- every column object dtype, holding
    ``np.float32`` scalars. (The daemon passes a typed one-row frame, R27.13.)"""
    frame, fitted = _realistic_frame()
    scaler = _scaler(frame, fitted)
    for position in (0, 17, 239):
        typed = frame.iloc[[position]]
        _assert_bit_identical(scaler.transform(typed), _reference_transform(scaler, typed))
        objects = frame.iloc[position].to_frame().T
        assert {dtype.kind for dtype in objects.dtypes} == {"O"}
        _assert_bit_identical(scaler.transform(objects), _reference_transform(scaler, objects))


def test_numpy_scalar_stats_promote_like_the_loop() -> None:
    """pandas hands an ``np.float64`` mean to numpy as a Python float, so a
    float32 column stays float32; the block must make the same conversion
    (``_operand``) or it would promote to float64."""
    frame, fitted = _realistic_frame(rows=50)
    base = _scaler(frame, fitted)
    strong = ZScoreScaler(
        means={k: (np.float64(v) if i % 2 else v) for i, (k, v) in enumerate(base.means.items())},
        stds={k: (np.float64(v) if i % 3 else v) for i, (k, v) in enumerate(base.stds.items())},
        columns=base.columns,
    )
    expected = _reference_transform(strong, frame)
    assert {str(dtype) for dtype in expected[list(fitted)].dtypes} == {"float32", "float64"}
    assert expected["obd:F32_0"].dtype == np.float32
    _assert_bit_identical(strong.transform(frame), expected)
    _assert_bit_identical(
        strong.transform(frame.iloc[3].to_frame().T),
        _reference_transform(strong, frame.iloc[3].to_frame().T),
    )


def test_columns_the_blocks_cannot_take_keep_the_series_expression() -> None:
    """A pandas extension dtype and an object column holding ``None`` (which
    pandas masks to NaN) fall back to the per-column expression itself."""
    frame, fitted = _realistic_frame(rows=40)
    scaler = _scaler(frame, fitted)
    odd = frame.copy()
    odd["obd:F64"] = odd["obd:F64"].where(odd.index % 3 != 0).astype("Float64")
    obj = odd["obd:F32_1"].astype(object)
    obj.iloc[[2, 5]] = None
    odd["obd:F32_1"] = obj
    _assert_bit_identical(scaler.transform(odd), _reference_transform(scaler, odd))


def test_zero_std_without_a_floor_divides_by_zero_like_the_loop() -> None:
    frame, fitted = _realistic_frame(rows=30)
    scaler = _scaler(frame, fitted, std_floor=0.0)
    zero = ZScoreScaler(
        means=scaler.means,
        stds={**scaler.stds, "dbc:CONST_F32": 0.0},
        columns=scaler.columns,
        std_floor=0.0,
    )
    assert zero.denominator("dbc:CONST_F32") == 0.0
    _assert_bit_identical(zero.transform(frame), _reference_transform(zero, frame))


def test_duplicate_row_index_and_empty_frame() -> None:
    frame, fitted = _realistic_frame(rows=20)
    scaler = _scaler(frame, fitted)
    duplicated = frame.set_axis(pd.Index([1, 1, 2, 2] * 5), axis=0)
    _assert_bit_identical(scaler.transform(duplicated), _reference_transform(scaler, duplicated))
    empty = frame.iloc[0:0]
    _assert_bit_identical(scaler.transform(empty), _reference_transform(scaler, empty))


def test_attrs_and_flags_survive_like_a_copy() -> None:
    frame, fitted = _realistic_frame(rows=12)
    scaler = _scaler(frame, fitted)
    frame.attrs = {"source": ["replay"]}
    frame = frame.set_flags(allows_duplicate_labels=False)
    out = scaler.transform(frame)
    expected = _reference_transform(scaler, frame)
    assert out.attrs == expected.attrs == {"source": ["replay"]}
    assert out.attrs["source"] is not frame.attrs["source"]
    assert out.flags.allows_duplicate_labels is expected.flags.allows_duplicate_labels is False


def test_transform_fitted_is_the_fitted_slice_of_transform() -> None:
    frame, fitted = _realistic_frame()
    scaler = _scaler(frame, fitted)
    for candidate in (frame, frame.iloc[[9]], frame.iloc[9].to_frame().T):
        expected = _reference_transform(scaler, candidate)[list(scaler.columns)]
        _assert_bit_identical(scaler.transform_fitted(candidate), expected)


def test_missing_fitted_column_still_refuses() -> None:
    frame, fitted = _realistic_frame(rows=10)
    scaler = _scaler(frame, fitted)
    short = frame.drop(columns=["obd:F32_4"])
    with pytest.raises(SchemaMismatchError, match="obd:F32_4"):
        scaler.transform(short)
    with pytest.raises(SchemaMismatchError, match="obd:F32_4"):
        scaler.transform_fitted(short)


def test_transform_fitted_refuses_duplicate_labels() -> None:
    frame, fitted = _realistic_frame(rows=10)
    scaler = _scaler(frame, fitted)
    doubled = pd.concat([frame, frame[["obd:F64"]]], axis=1)
    with pytest.raises(SchemaMismatchError, match="duplicate column labels"):
        scaler.transform_fitted(doubled)


def test_block_plans_are_cached_per_layout_and_stay_bit_identical() -> None:
    """R27.13: the plan (blocks and their operands) is built once per column
    layout. A second call reuses it with the same bytes out, and a different
    dtype layout of the same columns gets its own plan, not the cached one."""
    import pickle

    from src.preprocessing.normalization import _BLOCK_PLANS_ATTR

    frame, fitted = _realistic_frame()
    scaler = _scaler(frame, fitted)
    typed = frame.iloc[[3]]
    first = scaler.transform_fitted(typed)
    plans = dict(scaler.__dict__[_BLOCK_PLANS_ATTR])
    assert len(plans) == 1
    second = scaler.transform_fitted(frame.iloc[[4]])
    assert scaler.__dict__[_BLOCK_PLANS_ATTR] == plans  # same layout, same plan
    _assert_bit_identical(first, _reference_transform(scaler, typed)[list(fitted)])
    _assert_bit_identical(second, _reference_transform(scaler, frame.iloc[[4]])[list(fitted)])

    objects = frame.iloc[3].to_frame().T
    _assert_bit_identical(
        scaler.transform_fitted(objects), _reference_transform(scaler, objects)[list(fitted)]
    )
    widened = typed.astype(
        {column: np.float64 for column in fitted if column.startswith("obd:F32")}
    )
    _assert_bit_identical(
        scaler.transform_fitted(widened), _reference_transform(scaler, widened)[list(fitted)]
    )
    assert len(scaler.__dict__[_BLOCK_PLANS_ATTR]) == 3

    for block in next(iter(plans.values())).blocks:
        if not isinstance(block.key, str):
            assert not block.means.flags.writeable

    # The cache is not state: equality, repr and pickles never see it.
    restored = pickle.loads(pickle.dumps(scaler))
    assert _BLOCK_PLANS_ATTR not in restored.__dict__
    assert restored == scaler
    assert repr(restored) == repr(scaler)
    _assert_bit_identical(restored.transform_fitted(typed), first)


def test_block_plan_cache_is_bounded() -> None:
    from src.preprocessing.normalization import _BLOCK_PLANS_ATTR, _MAX_BLOCK_PLANS

    frame, fitted = _realistic_frame()
    scaler = _scaler(frame, fitted)
    row = frame.iloc[[0]]
    # Each reordering of the frame's columns is a new layout.
    for shift in range(_MAX_BLOCK_PLANS + 3):
        columns = list(row.columns)
        rotated = row[columns[shift:] + columns[:shift]]
        scaler.transform_fitted(rotated)
        assert len(scaler.__dict__[_BLOCK_PLANS_ATTR]) <= _MAX_BLOCK_PLANS


def test_apply_block_is_float64_rounded_once_whatever_the_shape_or_chunking(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """R27.5: the sequence model's one scaling. Each element is
    ``float32((float64(x) - mean) / denom)`` -- serve's expression -- for a
    1-D row, a 2-D block, a 3-D batch of windows, in place, and when the block
    is cut into chunks; NaN and ±inf stay non-finite, a below-floor std
    centres. Unlike :meth:`transform` it never computes float32 in float32."""
    import pickle

    import src.preprocessing.normalization as normalization

    columns = ("a", "b", "c", "d")
    scaler = ZScoreScaler(
        means={"a": 1234.567, "b": -0.1, "c": 3.0, "d": 98765.4321},
        stds={"a": 17.3, "b": 1e-12, "c": 0.7, "d": 1234.5},
        columns=columns,
    )
    rng = np.random.default_rng(7)
    block = (
        rng.normal(size=(37, 4)) * [20.0, 1.0, 1.0, 2000.0] + [1234.5, 0.0, 3.0, 98765.0]
    ).astype(np.float32)
    block[3, 0] = np.nan
    block[5, 1] = np.inf
    block[6, 2] = -np.inf
    means = np.array([scaler.means[c] for c in columns])
    denoms = np.array([scaler.denominator(c) for c in columns])
    assert denoms[1] == 1.0
    with np.errstate(all="ignore"):
        expected = ((block.astype(np.float64) - means) / denoms).astype(np.float32)
        in_float32 = (block - means.astype(np.float32)) / denoms.astype(np.float32)
    assert expected.tobytes() != in_float32.tobytes()  # the check has teeth

    whole = scaler.apply_block(block, columns)
    assert whole.dtype == np.float32
    assert whole.tobytes() == expected.tobytes()
    assert scaler.apply_block(block[2], list(columns)).tobytes() == expected[2].tobytes()
    windows = np.stack([block[i : i + 5] for i in range(30)])
    batched = scaler.apply_block(windows.astype(np.float64), columns)
    assert batched.tobytes() == np.stack([expected[i : i + 5] for i in range(30)]).tobytes()

    monkeypatch.setattr(normalization, "_APPLY_BLOCK_CHUNK_ELEMENTS", 9)  # two rows a chunk
    in_place = block.copy()
    assert scaler.apply_block(in_place, columns, out=in_place) is in_place
    assert in_place.tobytes() == expected.tobytes()

    with pytest.raises(SchemaMismatchError, match="3 named"):
        scaler.apply_block(block, columns[:3])
    with pytest.raises(ValueError, match="out must be"):
        scaler.apply_block(block, columns, out=block.astype(np.float64))
    # The cached vectors are not state, as with the block plans.
    restored = pickle.loads(pickle.dumps(scaler))
    assert normalization._BLOCK_VECTORS_ATTR not in restored.__dict__
    assert restored == scaler
