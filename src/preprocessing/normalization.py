"""Z-score normalization for the Phase 2 preprocessing pipeline.

A `ZScoreScaler` is fit on a clean training subset (caller's responsibility
to pre-filter — e.g. by operating-mode label or by excluding anomaly-injected
windows), then applied at inference time. Persisting the scaler's parameters
into the model manifest is what makes a saved model self-contained — without
the same (μ, σ) per channel, the model's learned thresholds are meaningless.

Design notes:
  * Schema is explicit: the scaler remembers exactly which columns it was
    fit on. Transform raises on missing columns; non-fitted columns pass
    through unchanged (so downstream code can keep session/timestamp/mode
    columns alongside the scaled features).
  * Constant channels (σ < `std_floor`) are treated as σ=1 — transform
    reduces to centering. Matches sklearn's behavior and avoids
    divide-by-zero amplification of floating-point noise.
  * `to_dict` / `from_dict` give a YAML/JSON-safe round-trip for the model
    manifest. No pickle dependency.
"""

from __future__ import annotations

import copy
from collections.abc import Iterable
from dataclasses import dataclass
from typing import Any, cast

import numpy as np
import pandas as pd

from src.columns import MODE_COLUMN, SESSION_COLUMN, TIMESTAMP_COLUMN, is_flag_column
from src.dataframe_utils import require_columns as _require_columns
from src.trainability import source_exclusion_reason

_DEFAULT_STD_FLOOR = 1e-9


class SchemaMismatchError(ValueError):
    """Raised when a DataFrame doesn't satisfy a fitted scaler's schema."""


@dataclass(frozen=True)
class ZScoreScaler:
    """Per-channel z-score normalizer with manifest-friendly serialization."""

    means: dict[str, float]
    stds: dict[str, float]
    columns: tuple[str, ...]
    std_floor: float = _DEFAULT_STD_FLOOR

    def __post_init__(self) -> None:
        if self.std_floor < 0:
            raise ValueError("std_floor must be non-negative")
        missing_means = set(self.columns) - set(self.means)
        missing_stds = set(self.columns) - set(self.stds)
        if missing_means:
            raise ValueError(f"means missing for columns: {sorted(missing_means)}")
        if missing_stds:
            raise ValueError(f"stds missing for columns: {sorted(missing_stds)}")

    @classmethod
    def fit(
        cls,
        df: pd.DataFrame,
        *,
        columns: Iterable[str] | None = None,
        std_floor: float = _DEFAULT_STD_FLOOR,
    ) -> ZScoreScaler:
        """Fit per-column means and stds on `df`.

        When `columns` is omitted we use every numeric column that isn't
        structural (session/timestamp/mode) or a gap flag — matching the
        column selection in `features.compute_features` so a typical
        pipeline can do `compute_features → ZScoreScaler.fit` without
        thinking about column names.
        """
        cols = _resolve_columns(df, columns)
        means: dict[str, float] = {}
        stds: dict[str, float] = {}
        for col in cols:
            # Reduce in float64 even when the feature column is float32:
            # μ/σ accumulated over a long training set lose precision in
            # float32, and these scalars are persisted in the model manifest
            # and divide every transformed value. The upcast is a transient
            # per-column copy, freed each iteration.
            series = df[col].astype("float64").replace([np.inf, -np.inf], np.nan)
            mean_val = float(series.mean(skipna=True))
            # ddof=0 (population std). The classic z-score assumes the
            # training set represents the full distribution; ddof=1 would
            # only matter at tiny sample sizes where a scaler is already
            # untrustworthy.
            std_val = float(series.std(skipna=True, ddof=0))
            # NaN can leak in when an entire column is NaN. Surface that
            # as a clean error rather than poisoning the manifest.
            if pd.isna(mean_val) or pd.isna(std_val):
                raise ValueError(f"column {col!r} has no non-NaN values; cannot fit scaler")
            means[col] = mean_val
            stds[col] = std_val
        return cls(
            means=means,
            stds=stds,
            columns=tuple(cols),
            std_floor=std_floor,
        )

    def transform(self, df: pd.DataFrame) -> pd.DataFrame:
        """Apply z-score to every fitted column; pass non-fitted columns through.

        Raises `SchemaMismatchError` when any fitted column is absent from
        `df`. Extra columns in `df` are preserved — useful for keeping
        session/timestamp/mode alongside the scaled features. Columns keep
        their order, the index is ``df``'s, and each column's values and dtype
        are bit-identical to ``(df[col] - mean) / denom`` (R27.4: computed in
        dtype blocks, see :meth:`_scaled_pieces`).
        """
        self._require_fitted_columns(df)
        fitted = set(self.columns)
        labels = df.columns.tolist()  # iterating a str Index directly costs ~0.4 µs a label
        scaled_at = [pos for pos, label in enumerate(labels) if label in fitted]
        if not scaled_at:
            return df.copy()
        kept_at = [pos for pos, label in enumerate(labels) if label not in fitted]
        pieces = self._scaled_pieces(df, scaled_at)
        if kept_at:
            pieces.append(df.iloc[:, kept_at].set_axis(kept_at, axis=1))
        combined = _in_position_order(pieces, list(range(len(df.columns))))
        out = combined.set_axis(df.columns, axis=1)
        # What ``df.copy()`` carried besides the data.
        if df.attrs:
            out.attrs = copy.deepcopy(df.attrs)
        if not df.flags.allows_duplicate_labels:
            out = out.set_flags(allows_duplicate_labels=False)
        return out

    def transform_fitted(self, df: pd.DataFrame) -> pd.DataFrame:
        """Only the fitted columns of ``df``, z-scored, in :attr:`columns` order.

        The same values as ``transform(df)[list(self.columns)]`` without
        copying the columns ``transform`` passes through -- for a scorer that
        reads the scaled features and keeps the unscaled frame itself
        (``TrainedIsolationForest.score``). Refuses a frame with duplicate
        column labels, where "the fitted column" names more than one.
        """
        self._require_fitted_columns(df)
        if not df.columns.is_unique:
            duplicated = sorted({str(label) for label in df.columns[df.columns.duplicated()]})
            raise SchemaMismatchError(f"input DataFrame has duplicate column labels: {duplicated}")
        if not self.columns:
            return pd.DataFrame(index=df.index)
        positions = [int(pos) for pos in df.columns.get_indexer(pd.Index(self.columns))]
        combined = _in_position_order(self._scaled_pieces(df, positions), positions)
        return combined.set_axis(list(self.columns), axis=1)

    def _require_fitted_columns(self, df: pd.DataFrame) -> None:
        missing = [col for col in self.columns if col not in df.columns]
        if missing:
            raise SchemaMismatchError(f"input DataFrame missing fitted columns: {missing}")

    def _scaled_pieces(self, df: pd.DataFrame, positions: list[int]) -> list[pd.DataFrame]:
        """z-score the columns of ``df`` at ``positions``, one block per dtype (R27.4).

        Each returned frame is labelled by the *positions* it holds, on
        ``df``'s index. Every element is computed as ``(x - mean) / denom`` --
        the per-column pandas expression this replaced, in the same dtypes, so
        the result is bit-identical to it (``tests/test_zscore_block.py``):

        * a numpy numeric column is computed in the dtypes numpy's own
          promotion picks for ``column - mean`` and then ``/ denom`` -- float32
          stays float32 against a Python-float mean, ints and bools become
          float64. Columns sharing those dtypes share one 2-D block, which is
          what replaces ~230 µs of pandas overhead per column;
        * an object column (the daemon's one-row ``to_frame().T`` frame) is
          one object block, so each element keeps Python's own arithmetic for
          its type (an ``np.float32`` stays float32);
        * anything else (pandas extension dtypes, an object column holding
          ``None``) takes the per-column Series expression itself.

        The reciprocal is never multiplied in its place: ``x * (1 / d)`` is
        not ``x / d`` to the last bit.
        """
        dtypes = df.dtypes.tolist()
        labels = df.columns.tolist()
        blocks: dict[_BlockKey, list[int]] = {}
        by_column: list[int] = []
        for pos in positions:
            key = self._block_key(labels[pos], dtypes[pos])
            if key is None:
                by_column.append(pos)
            else:
                blocks.setdefault(key, []).append(pos)

        pieces: list[pd.DataFrame] = []
        for key, group in blocks.items():
            names = [labels[pos] for pos in group]
            means = [_operand(self.means[name]) for name in names]
            denoms = [_operand(self.denominator(name)) for name in names]
            if isinstance(key, str):
                piece = _object_piece(df, group, means, denoms)
                if piece is None:
                    by_column.extend(group)
                    continue
            else:
                first, second = key
                values = df.iloc[:, group].to_numpy(dtype=first)
                with np.errstate(all="ignore"):
                    diff = values - np.array(means, dtype=first)
                    if second != first:
                        diff = diff.astype(second)
                    result = diff / np.array(denoms, dtype=second)
                piece = pd.DataFrame(result, index=df.index, columns=group, copy=False)
            pieces.append(piece)
        if by_column:
            series = [
                (df.iloc[:, pos] - self.means[labels[pos]]) / self.denominator(labels[pos])
                for pos in by_column
            ]
            pieces.append(pd.concat(series, axis=1).set_axis(by_column, axis=1))
        return pieces

    def _block_key(self, label: Any, dtype: Any) -> _BlockKey | None:
        """Which block column ``label`` of ``dtype`` joins, or ``None`` for the
        per-column path. A numeric block is keyed by the two result dtypes
        numpy promotes ``column - mean`` and ``... / denom`` to, for the
        operands pandas actually hands numpy (:func:`_operand`)."""
        if not isinstance(dtype, np.dtype):
            return None
        if dtype.kind == "O":
            return _OBJECT_BLOCK
        if dtype.kind not in "biuf":
            return None
        first = np.result_type(dtype, _operand(self.means[label]))
        second = np.result_type(first, _operand(self.denominator(label)))
        return (first, second)

    def denominator(self, column: str) -> float:
        """The divisor for ``column``: its std, or 1.0 when the std is below
        ``std_floor`` (a constant channel is centred, never blown up).

        The one statement of the rule (R04.15); :meth:`transform`,
        :meth:`vectors` and every scorer read it from here.
        """
        std = self.stds[column]
        return std if std >= self.std_floor else 1.0

    def vectors(
        self, columns: Iterable[str]
    ) -> tuple[np.ndarray[Any, np.dtype[np.float64]], np.ndarray[Any, np.dtype[np.float64]]]:
        """``(means, denominators)`` as float64 arrays in ``columns`` order.

        For callers that z-score a numpy block -- serve's per-tick window, the
        batched scorer, the IF fit -- so they apply the same denominator rule
        as :meth:`transform` instead of re-deriving it.
        """
        ordered = list(columns)
        means = np.array([self.means[column] for column in ordered], dtype=np.float64)
        denominators = np.array([self.denominator(column) for column in ordered], dtype=np.float64)
        return means, denominators

    def fit_transform(
        self,
        df: pd.DataFrame,
        *,
        columns: Iterable[str] | None = None,
    ) -> tuple[ZScoreScaler, pd.DataFrame]:
        """Convenience: fit a new scaler on `df` and return (scaler, scaled_df).

        Note this is a classmethod-like constructor that also transforms;
        unlike sklearn's `fit_transform`, it returns the fitted scaler
        explicitly so the caller can persist it. Implemented at the
        instance level only for type clarity — call it from a fresh
        scaler or use the staticmethod `ZScoreScaler.fit(...)` + `.transform(...)`.
        """
        del self  # this is logically a classmethod; instance is unused
        fitted = ZScoreScaler.fit(df, columns=columns)
        return fitted, fitted.transform(df)

    def to_dict(self) -> dict[str, Any]:
        """Serialize to a JSON/YAML-safe dict for the model manifest."""
        return {
            "means": dict(self.means),
            "stds": dict(self.stds),
            "columns": list(self.columns),
            "std_floor": self.std_floor,
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> ZScoreScaler:
        """Load a scaler previously emitted by `to_dict`."""
        return cls(
            means=dict(data["means"]),
            stds=dict(data["stds"]),
            columns=tuple(data["columns"]),
            std_floor=float(data.get("std_floor", _DEFAULT_STD_FLOOR)),
        )


_OBJECT_BLOCK = "object"
# A numeric block's (``x - mean`` dtype, ``/ denom`` dtype), or _OBJECT_BLOCK.
_BlockKey = tuple[np.dtype[Any], np.dtype[Any]] | str


def _operand(value: Any) -> Any:
    """A stored mean/denominator as pandas passes it to numpy: a numpy
    integer or floating scalar becomes the Python scalar, so it promotes
    weakly (pandas ``maybe_prepare_scalar_for_op``; float32 minus an
    ``np.float64`` mean stays float32 in a Series)."""
    if isinstance(value, np.integer):
        return int(value)
    if isinstance(value, np.floating):
        return float(value)
    return value


def _object_piece(
    df: pd.DataFrame, positions: list[int], means: list[Any], denoms: list[Any]
) -> pd.DataFrame | None:
    """One object block: numpy applies ``(x - mean) / denom`` element by
    element with the same operands, exactly as pandas does for an object
    Series. ``None`` when an element refuses the arithmetic (``None``
    itself): pandas then masks the nulls, so the caller takes that path."""
    values = df.iloc[:, positions].to_numpy(dtype=object)
    mean_row = np.empty(len(means), dtype=object)
    mean_row[:] = means
    denom_row = np.empty(len(denoms), dtype=object)
    denom_row[:] = denoms
    try:
        with np.errstate(all="ignore"):
            result = (values - mean_row) / denom_row
    except TypeError:
        return None
    return pd.DataFrame(result, index=df.index, columns=positions, dtype=object, copy=False)


def _in_position_order(pieces: list[pd.DataFrame], order: list[int]) -> pd.DataFrame:
    """Concatenate position-labelled ``pieces`` and arrange them in ``order``."""
    combined = pieces[0] if len(pieces) == 1 else pd.concat(pieces, axis=1)
    current = cast(list[int], combined.columns.tolist())
    if current != order:
        where = {pos: index for index, pos in enumerate(current)}
        combined = combined.iloc[:, [where[pos] for pos in order]]
    return combined


def _resolve_columns(
    df: pd.DataFrame,
    override: Iterable[str] | None,
) -> list[str]:
    """Select numeric feature columns, mirroring features.py's policy."""
    if override is not None:
        cols = list(override)
        _require_columns(df, cols)
        for col in cols:
            if not pd.api.types.is_numeric_dtype(df[col]):
                raise TypeError(f"column {col!r} is not numeric; cannot fit z-score scaler")
        return cols

    skip_exact = {SESSION_COLUMN, TIMESTAMP_COLUMN, MODE_COLUMN}
    cols = []
    for raw in df.columns:
        col = str(raw)
        if col in skip_exact:
            continue
        if is_flag_column(col):
            continue
        if not pd.api.types.is_numeric_dtype(df[col]):
            continue
        # H01.1 source gate: discovery never picks an external sensor.
        if source_exclusion_reason(col) is not None:
            continue
        cols.append(col)
    return cols
