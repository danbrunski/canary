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

from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from typing import Any

import pandas as pd

from src.preprocessing.resampler import (
    GAP_SUFFIX,
    SESSION_COLUMN,
    TIMESTAMP_COLUMN,
)
from src.preprocessing.segmenter import MODE_COLUMN

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
            series = df[col].astype("float64")
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
        session/timestamp/mode alongside the scaled features.
        """
        missing = [col for col in self.columns if col not in df.columns]
        if missing:
            raise SchemaMismatchError(f"input DataFrame missing fitted columns: {missing}")

        out = df.copy()
        for col in self.columns:
            std = self.stds[col]
            denom = std if std >= self.std_floor else 1.0
            out[col] = (df[col] - self.means[col]) / denom
        return out

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
        if col.endswith(GAP_SUFFIX):
            continue
        if not pd.api.types.is_numeric_dtype(df[col]):
            continue
        cols.append(col)
    return cols


def _require_columns(frame: pd.DataFrame, columns: Sequence[str]) -> None:
    missing = set(columns) - set(frame.columns)
    if missing:
        raise KeyError(f"missing required columns: {', '.join(sorted(missing))}")
