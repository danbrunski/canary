"""Offline "did my change move behavior?" analysis for one stored session.

Answers the question that, before this module existed, had to be answered by
hand with DuckDB over SSH: for a given drive, how did each behavior surface's
actual telemetry compare to what the learned model expects? Runs the *exact*
same decode → segment → derive → evaluate pipeline the daemon uses live and
the refit CLI uses to fit the surfaces
(:func:`src.preprocessing.feature_pipeline.featurize_wide_frame`), so results
here are directly comparable to ``GET /models/behavior/live`` — just computed
over a whole stored session instead of the current live tick.

Severity is graded via the scalar :func:`src.decode.behavior_decoder.deviation`
per row (not the vectorized ``deviation:<name>`` column, which only carries the
raw delta) — that keeps this module's severity math byte-identical to the
live/daemon path by construction, at the cost of a Python-level loop over
rows × surfaces. For a single session (thousands of rows, a handful of
surfaces) that's well under a second; this is an offline analysis tool, not a
hot path.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, cast

import numpy as np
import pandas as pd

from src.config import CanaryConfig
from src.decode.behavior_decoder import deviation as evaluate_deviation
from src.decode.behavior_models import (
    BehaviorModel1D,
    BehaviorModel2D,
    BehaviorModels,
    unknown_operating_modes,
)
from src.decode.bindings import load_bindings
from src.decode.dbc import DEFAULT_DBC_DIR, load_dbc_dir
from src.decode.dids import load_did_map
from src.preprocessing.feature_pipeline import (
    DEFAULT_BINDINGS_PATH,
    DEFAULT_DIDS_PATH,
    _decode_session_to_resampled,
    featurize_wide_frame,
)
from src.preprocessing.resampler import ResampleConfig
from src.preprocessing.segmenter import MODE_COLUMN
from src.telemetry import DECODED_SOURCE, SOURCE_SEP, channel_key


@dataclass(frozen=True)
class SurfaceDeviationSummary:
    """Aggregated actual-vs-expected deviation for one surface over a session."""

    name: str
    kind: str  # "surface_1d" | "surface_2d"
    #: The model's gate, or None if it evaluates in every mode. Lets callers
    #: (the CLI's --mode, the MCP tool) filter surfaces without re-running
    #: the analysis.
    operating_mode: str | None
    #: Rows where the model's gated ``operating_mode`` (if any) was in effect.
    n_observed: int
    #: Rows where the model actually spoke (in-domain, non-empty cell).
    n_in_domain: int
    mean_actual: float | None
    mean_expected: float | None
    mean_deviation: float | None
    warning_fraction: float | None
    critical_fraction: float | None
    #: Per-(rpm,load)-bin mean deviation, surface_2d models only — see
    #: ``_bin_2d_deviations``. Empty for surface_1d.
    cells: list[dict[str, Any]] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "kind": self.kind,
            "operating_mode": self.operating_mode,
            "n_observed": self.n_observed,
            "n_in_domain": self.n_in_domain,
            "mean_actual": self.mean_actual,
            "mean_expected": self.mean_expected,
            "mean_deviation": self.mean_deviation,
            "warning_fraction": self.warning_fraction,
            "critical_fraction": self.critical_fraction,
            "cells": self.cells,
        }


@dataclass(frozen=True)
class SessionDeviationReport:
    """Full-session result: one summary per surface in the loaded model set."""

    session_dir: Path
    n_rows: int
    surfaces: list[SurfaceDeviationSummary]
    #: From ``unknown_operating_modes`` — surfaces gated on a mode this
    #: session's config can't produce, so they silently never spoke. Distinct
    #: from a surface that spoke and matched fine (n_in_domain == 0 with an
    #: empty gated_on_missing_mode entry means "never in range", not "never
    #: gated on").
    gated_on_missing_mode: dict[str, str]

    def to_dict(self, *, mode_filter: str | None = None) -> dict[str, Any]:
        """JSON-safe payload. ``mode_filter`` restricts ``surfaces`` to those
        gated on that ``operating_mode`` — the single implementation the CLI
        (``--mode``) and the MCP tool (``mode=``) both filter through."""
        surfaces = (
            self.surfaces
            if mode_filter is None
            else [s for s in self.surfaces if s.operating_mode == mode_filter]
        )
        return {
            "session_dir": str(self.session_dir),
            "n_rows": self.n_rows,
            "surfaces": [s.to_dict() for s in surfaces],
            "gated_on_missing_mode": self.gated_on_missing_mode,
        }


def _resolve_actual_column(model: BehaviorModel1D | BehaviorModel2D) -> str:
    """Same resolution rule as ``append_deviation_columns``: a qualified
    ``source:channel`` name verbatim, else the legacy ``decoded:<output>``
    for a bare CAN signal name."""
    if SOURCE_SEP in model.output:
        return model.output
    return channel_key(DECODED_SOURCE, model.output)


def _mean(values: list[float]) -> float | None:
    return sum(values) / len(values) if values else None


def _bin_index(value: float, edges: np.ndarray) -> int:
    """Same half-open/closed-last-bin rule as ``surface_2d._bin_index``."""
    return int(np.clip(np.digitize([value], edges)[0] - 1, 0, len(edges) - 2))


def _bin_2d_deviations(
    model: BehaviorModel2D,
    xs: list[float],
    ys: list[float],
    deviations: list[float],
) -> list[dict[str, Any]]:
    """Mean deviation per (x-bin, y-bin) cell the model was actually
    evaluated against — i.e. per rpm×load cell for the fuel/air surfaces.

    Shows *where* in the operating envelope a shift lives (idle vs. load),
    not just a session-wide average that could hide a load-only effect.
    Only cells with at least one observation are returned (no synthetic
    zero-count rows).
    """
    x_axis, y_axis = model.axes
    x_edges = np.asarray(x_axis.bin_edges, dtype=float)
    y_edges = np.asarray(y_axis.bin_edges, dtype=float)
    buckets: dict[tuple[int, int], list[float]] = {}
    for x, y, dev in zip(xs, ys, deviations, strict=True):
        i = _bin_index(x, x_edges)
        j = _bin_index(y, y_edges)
        buckets.setdefault((i, j), []).append(dev)
    cells: list[dict[str, Any]] = []
    for (i, j), devs in sorted(buckets.items()):
        cells.append(
            {
                # float(): plain Python floats, not numpy scalars — numpy>=2.0
                # reprs float64 as "np.float64(0.0)", which leaked into both
                # the CLI table and (harmlessly, since float64 IS-A float, but
                # still ugly) JSON output.
                "x_bin": [float(x_edges[i]), float(x_edges[i + 1])],
                "y_bin": [float(y_edges[j]), float(y_edges[j + 1])],
                "n": len(devs),
                "mean_deviation": _mean(devs),
            }
        )
    return cells


def _summarize_surface(
    model: BehaviorModel1D | BehaviorModel2D,
    records: list[dict[str, Any]],
) -> SurfaceDeviationSummary:
    actual_col = _resolve_actual_column(model)
    mode_gated = model.operating_mode is not None

    actuals: list[float] = []
    expecteds: list[float] = []
    deviations: list[float] = []
    severities: list[str] = []
    xs: list[float] = []
    ys: list[float] = []
    n_observed = 0

    if actual_col:
        for row in records:
            if mode_gated and row.get(MODE_COLUMN) != model.operating_mode:
                continue
            n_observed += 1
            result = evaluate_deviation(model, row.get(actual_col), row)
            if result.expected is None:
                continue
            expecteds.append(result.expected)
            if result.actual is not None:
                actuals.append(result.actual)
            if result.deviation is not None:
                deviations.append(result.deviation)
                severities.append(result.severity)
                if isinstance(model, BehaviorModel2D):
                    x = row.get(model.axes[0].channel)
                    y = row.get(model.axes[1].channel)
                    if x is not None and y is not None:
                        xs.append(float(x))
                        ys.append(float(y))

    warning_fraction = (
        sum(1 for s in severities if s in ("warning", "critical")) / len(severities)
        if severities
        else None
    )
    critical_fraction = (
        sum(1 for s in severities if s == "critical") / len(severities) if severities else None
    )
    cells = (
        _bin_2d_deviations(model, xs, ys, deviations)
        if isinstance(model, BehaviorModel2D) and xs
        else []
    )

    return SurfaceDeviationSummary(
        name=model.name,
        kind=model.kind,
        operating_mode=model.operating_mode,
        n_observed=n_observed,
        n_in_domain=len(expecteds),
        mean_actual=_mean(actuals),
        mean_expected=_mean(expecteds),
        mean_deviation=_mean(deviations),
        warning_fraction=warning_fraction,
        critical_fraction=critical_fraction,
        cells=cells,
    )


def analyze_session(
    session_dir: Path,
    config: CanaryConfig,
    models: BehaviorModels,
    *,
    bindings_path: Path = DEFAULT_BINDINGS_PATH,
    dbc_dir: Path = DEFAULT_DBC_DIR,
    dids_path: Path = DEFAULT_DIDS_PATH,
) -> SessionDeviationReport:
    """Diff one stored session's telemetry against ``models``.

    Runs the identical decode/segment/derive pipeline
    :func:`~src.preprocessing.feature_pipeline.build_feature_frame` uses for
    training (same bindings/dbc/DID decode, same
    ``compute_anomaly_signals``/committee-residual derived channels, same
    operating-mode segmenter) so results are apples-to-apples with what the
    surfaces were fit on — just for one session instead of a whole corpus,
    and evaluated rather than fit.
    """
    bindings = load_bindings(bindings_path) if bindings_path.exists() else None
    dbc_database = load_dbc_dir(dbc_dir)
    did_map = load_did_map(dids_path) if dids_path.exists() else None
    resample_config = ResampleConfig(
        hz=config.preprocessing.resample_hz,
        slow_channel_max_gap_seconds=config.preprocessing.slow_channel_max_gap_seconds,
    )

    resampled = _decode_session_to_resampled(
        Path(session_dir), bindings, dbc_database, did_map, resample_config
    )
    framed = featurize_wide_frame(resampled, config, behavior_models=models).operating_frame
    # Columns are always strings in this pipeline (channel keys like
    # "obd:RPM"); to_dict's Hashable key type is pandas being generic.
    records = (
        cast("list[dict[str, Any]]", framed.to_dict(orient="records")) if not framed.empty else []
    )

    surfaces = [_summarize_surface(model, records) for model in models.models]
    gated = unknown_operating_modes(models, config.preprocessing.operating_modes.keys())

    return SessionDeviationReport(
        session_dir=Path(session_dir),
        n_rows=len(framed) if isinstance(framed, pd.DataFrame) else 0,
        surfaces=surfaces,
        gated_on_missing_mode=gated,
    )


__all__ = [
    "SessionDeviationReport",
    "SurfaceDeviationSummary",
    "analyze_session",
]
