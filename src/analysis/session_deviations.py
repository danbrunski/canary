"""Offline "did my change move behavior?" analysis for one stored session.

Answers the question that, before this module existed, had to be answered by
hand with DuckDB over SSH: for a given drive, how did each behavior surface's
actual telemetry compare to what the learned model expects? Runs the *exact*
same decode → segment → derive → evaluate pipeline the daemon uses live and
the refit CLI uses to fit the surfaces
(:func:`src.features.feature_pipeline.featurize_wide_frame`), so results
here are directly comparable to ``GET /models/behavior/live`` — just computed
over a whole stored session instead of the current live tick.

Severity is graded via the scalar :func:`src.behavior.behavior_decoder.deviation`
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

from src.analysis.noise_floor import FloorRefusal, NoiseFloor, Significance
from src.behavior.behavior_decoder import deviation as evaluate_deviation
from src.behavior.behavior_models import (
    BehaviorModel1D,
    BehaviorModel2D,
    BehaviorModels,
    serving_status,
    unknown_operating_modes,
)
from src.behavior.surface_eval import bin_index
from src.columns import MODE_COLUMN
from src.config import CanaryConfig
from src.decode.bindings import load_bindings
from src.decode.dbc import DEFAULT_DBC_DIR, load_dbc_dir
from src.decode.dids import load_did_map
from src.features.feature_pipeline import (
    DEFAULT_BINDINGS_PATH,
    DEFAULT_DIDS_PATH,
    _decode_session_to_resampled,
    featurize_wide_frame,
    resample_config_for,
)
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
    #: B01.4 — in-mode rows the model refused, counted by named reason
    #: (eligibility condition, ``off_map``, missing/stale actual). A refused
    #: row is not an in-band row; it is excluded from the fractions above.
    refusal_reasons: dict[str, int] = field(default_factory=dict)
    #: B01.6 — the artifact's serving status (``state`` + ``reasons``).
    serving_status: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "kind": self.kind,
            "operating_mode": self.operating_mode,
            "n_observed": self.n_observed,
            "n_in_domain": self.n_in_domain,
            "refusal_reasons": self.refusal_reasons,
            "serving_status": self.serving_status,
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
        i = bin_index(x, x_edges)
        j = bin_index(y, y_edges)
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


#: Equal-width cells a 1D surface's domain is split into for cell-matched
#: comparison. A 1D surface has no fitted bin grid of its own; ten cells over
#: its validity domain are fine enough that a composition shift (a colder or
#: longer warm-up) moves weight *between* cells, not a cell's own mean.
ONE_D_COMPARISON_CELLS = 10


def _bin_1d_deviations(
    model: BehaviorModel1D, xs: list[float], deviations: list[float]
) -> list[dict[str, Any]]:
    """Mean deviation per equal-width cell of a 1D surface's axis domain."""
    lo, hi = model.axis.domain
    edges = np.linspace(lo, hi, ONE_D_COMPARISON_CELLS + 1)
    buckets: dict[int, list[float]] = {}
    for x, dev in zip(xs, deviations, strict=True):
        buckets.setdefault(bin_index(x, edges), []).append(dev)
    return [
        {
            "x_bin": [float(edges[i]), float(edges[i + 1])],
            "y_bin": None,
            "n": len(devs),
            "mean_deviation": _mean(devs),
        }
        for i, devs in sorted(buckets.items())
    ]


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
    refusals: dict[str, int] = {}

    if actual_col:
        for row in records:
            if mode_gated and row.get(MODE_COLUMN) != model.operating_mode:
                continue
            n_observed += 1
            result = evaluate_deviation(model, row.get(actual_col), row)
            if result.reason is not None:
                refusals[result.reason] = refusals.get(result.reason, 0) + 1
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
                else:
                    # A 1D surface's axis value is always in-domain here: the
                    # model returns no expectation outside it.
                    xs.append(float(row[model.axis.channel]))

    warning_fraction = (
        sum(1 for s in severities if s in ("warning", "critical")) / len(severities)
        if severities
        else None
    )
    critical_fraction = (
        sum(1 for s in severities if s == "critical") / len(severities) if severities else None
    )
    if isinstance(model, BehaviorModel2D):
        cells = _bin_2d_deviations(model, xs, ys, deviations) if xs else []
    else:
        cells = _bin_1d_deviations(model, xs, deviations) if xs else []

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
        refusal_reasons=dict(sorted(refusals.items(), key=lambda item: (-item[1], item[0]))),
        serving_status=serving_status(model).to_dict(),
    )


#: A cell contributes to a comparison only with at least this many in-domain
#: rows on *each* side; below it the cell is reported ``sparse`` with no delta.
DEFAULT_MIN_CELL_N = 5


@dataclass(frozen=True)
class SurfaceComparison:
    """One surface's before/after delta between two sessions (Task 5.4) —
    the mod-effect question asked directly, session-to-session, instead of
    each session graded independently against the model.

    R05.6: there is no whole-session mean delta. A session-wide mean of
    actual, expected or deviation depends on *where* each drive spent its
    time — two drives with identical per-cell behaviour and a different rpm
    mix report different means (the retracted "K responds 1:1" shape). The
    surface-level figure is ``matched_deviation_delta``: the unweighted mean
    of per-cell deltas over the cells both sessions visited at least
    ``min_n`` times, so composition cannot enter it.
    """

    name: str
    kind: str
    operating_mode: str | None
    n_observed_a: int
    n_observed_b: int
    n_in_domain_a: int
    n_in_domain_b: int
    min_n: int
    #: Cells with >= ``min_n`` rows on both sides.
    n_matched_cells: int
    #: Mean over matched cells of ``b - a`` per-cell mean deviation; ``None``
    #: when no cell matched (the two drives shared no operating point).
    matched_deviation_delta: float | None
    warning_fraction_a: float | None
    warning_fraction_b: float | None
    critical_fraction_a: float | None
    critical_fraction_b: float | None
    #: Per-cell delta on the surface's own grid (2D), or on
    #: :data:`ONE_D_COMPARISON_CELLS` equal cells of the domain (1D).
    cells: list[dict[str, Any]] = field(default_factory=list)
    #: The noise-floor verdict when one was requested
    #: (:meth:`~src.analysis.noise_floor.NoiseFloor.significance`): ``None``
    #: when not requested, a refusal naming both regimes when the floor does
    #: not apply.
    significance: dict[str, Any] | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "kind": self.kind,
            "operating_mode": self.operating_mode,
            "n_observed_a": self.n_observed_a,
            "n_observed_b": self.n_observed_b,
            "n_in_domain_a": self.n_in_domain_a,
            "n_in_domain_b": self.n_in_domain_b,
            "min_n": self.min_n,
            "n_matched_cells": self.n_matched_cells,
            "matched_deviation_delta": self.matched_deviation_delta,
            "warning_fraction_a": self.warning_fraction_a,
            "warning_fraction_b": self.warning_fraction_b,
            "critical_fraction_a": self.critical_fraction_a,
            "critical_fraction_b": self.critical_fraction_b,
            "cells": self.cells,
            "significance": self.significance,
        }


@dataclass(frozen=True)
class SessionComparisonReport:
    """Full A/B result: one :class:`SurfaceComparison` per surface both
    sessions were evaluated against (Task 5.4)."""

    session_a: Path
    session_b: Path
    surfaces: list[SurfaceComparison]

    def to_dict(self, *, mode_filter: str | None = None) -> dict[str, Any]:
        surfaces = (
            self.surfaces
            if mode_filter is None
            else [s for s in self.surfaces if s.operating_mode == mode_filter]
        )
        return {
            "session_a": str(self.session_a),
            "session_b": str(self.session_b),
            "surfaces": [s.to_dict() for s in surfaces],
        }


def _cell_key(cell: dict[str, Any]) -> tuple[tuple[float, ...], tuple[float, ...] | None]:
    y_bin = cell["y_bin"]
    return tuple(cell["x_bin"]), None if y_bin is None else tuple(y_bin)


def _compare_surface(
    a: SurfaceDeviationSummary,
    b: SurfaceDeviationSummary,
    *,
    min_n: int,
    noise_floor: NoiseFloor | None,
    conditions: str | None,
) -> SurfaceComparison:
    cells_a = {_cell_key(c): c for c in a.cells}
    cells_b = {_cell_key(c): c for c in b.cells}
    cells: list[dict[str, Any]] = []
    for key in sorted(set(cells_a) | set(cells_b), key=lambda k: (k[0], k[1] or ())):
        ca, cb = cells_a.get(key), cells_b.get(key)
        n_a = ca["n"] if ca else 0
        n_b = cb["n"] if cb else 0
        dev_a = ca["mean_deviation"] if ca else None
        dev_b = cb["mean_deviation"] if cb else None
        delta = (
            dev_b - dev_a
            if n_a >= min_n and n_b >= min_n and dev_a is not None and dev_b is not None
            else None
        )
        cells.append(
            {
                "x_bin": list(key[0]),
                "y_bin": None if key[1] is None else list(key[1]),
                "n_a": n_a,
                "n_b": n_b,
                "mean_deviation_a": dev_a,
                "mean_deviation_b": dev_b,
                "sparse": delta is None and n_a > 0 and n_b > 0,
                "delta": delta,
                "delta_sigma": None,
            }
        )
    deltas = [c["delta"] for c in cells if c["delta"] is not None]
    verdict: Significance | FloorRefusal | None = None
    if noise_floor is not None and conditions is not None and deltas:
        # The surface's floor is keyed by the surface name, in the same
        # units as its deviation.
        verdict = noise_floor.significance(a.name, pd.Series(deltas), conditions=conditions)
        if isinstance(verdict, Significance):
            matched_cells = [c for c in cells if c["delta"] is not None]
            for cell, sigma in zip(matched_cells, verdict.delta_sigma, strict=True):
                cell["delta_sigma"] = float(sigma)
    return SurfaceComparison(
        name=a.name,
        kind=a.kind,
        operating_mode=a.operating_mode,
        n_observed_a=a.n_observed,
        n_observed_b=b.n_observed,
        n_in_domain_a=a.n_in_domain,
        n_in_domain_b=b.n_in_domain,
        min_n=min_n,
        n_matched_cells=len(deltas),
        matched_deviation_delta=_mean(deltas),
        warning_fraction_a=a.warning_fraction,
        warning_fraction_b=b.warning_fraction,
        critical_fraction_a=a.critical_fraction,
        critical_fraction_b=b.critical_fraction,
        cells=cells,
        significance=None if verdict is None else verdict.as_dict(),
    )


def compare_session_deviations(
    session_a_dir: Path,
    session_b_dir: Path,
    config: CanaryConfig,
    models: BehaviorModels,
    *,
    min_n: int = DEFAULT_MIN_CELL_N,
    noise_floor: NoiseFloor | None = None,
    conditions: str | None = None,
    bindings_path: Path = DEFAULT_BINDINGS_PATH,
    dbc_dir: Path = DEFAULT_DBC_DIR,
    dids_path: Path = DEFAULT_DIDS_PATH,
) -> SessionComparisonReport:
    """A/B session comparison (Task 5.4): run :func:`analyze_session`
    independently for ``session_a_dir`` (before) and ``session_b_dir``
    (after) against the identical ``models``, then diff the two reports
    cell-by-cell — "did the mod move behavior?" answered session-to-session
    instead of each session graded independently against the model.

    Only cells with ``min_n`` rows on both sides are compared, and the
    surface-level figure is built from those cells alone (see
    :class:`SurfaceComparison`). ``noise_floor`` with ``conditions`` quotes
    each matched delta in sigma of the floor measured for that surface, or
    records the refusal. Renamed from ``compare_sessions`` (R05.6): that name
    belongs to :func:`src.analysis.tuning.compare_sessions`, which gates.
    """
    report_a = analyze_session(
        session_a_dir,
        config,
        models,
        bindings_path=bindings_path,
        dbc_dir=dbc_dir,
        dids_path=dids_path,
    )
    report_b = analyze_session(
        session_b_dir,
        config,
        models,
        bindings_path=bindings_path,
        dbc_dir=dbc_dir,
        dids_path=dids_path,
    )
    by_name_b = {s.name: s for s in report_b.surfaces}
    surfaces = [
        _compare_surface(
            sa, by_name_b[sa.name], min_n=min_n, noise_floor=noise_floor, conditions=conditions
        )
        for sa in report_a.surfaces
        if sa.name in by_name_b
    ]
    return SessionComparisonReport(
        session_a=report_a.session_dir, session_b=report_b.session_dir, surfaces=surfaces
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
    :func:`~src.features.feature_pipeline.build_feature_frame` uses for
    training (same bindings/dbc/DID decode, same
    ``compute_anomaly_signals``/committee-residual derived channels, same
    operating-mode segmenter) so results are apples-to-apples with what the
    surfaces were fit on — just for one session instead of a whole corpus,
    and evaluated rather than fit.
    """
    bindings = load_bindings(bindings_path) if bindings_path.exists() else None
    dbc_database = load_dbc_dir(dbc_dir)
    did_map = load_did_map(dids_path) if dids_path.exists() else None
    # The one batch resample recipe training uses. A hand-built ResampleConfig
    # here dropped ``max_gap_factor``/``session_gap_seconds``, so a config that
    # set either resampled this analysis differently from the surfaces' fit.
    resample_config = resample_config_for(config)

    resampled = _decode_session_to_resampled(
        Path(session_dir), bindings, dbc_database, did_map, resample_config
    )
    framed = featurize_wide_frame(
        resampled, config, behavior_models=models, did_map=did_map
    ).operating_frame
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
    "SessionComparisonReport",
    "SessionDeviationReport",
    "SurfaceComparison",
    "SurfaceDeviationSummary",
    "DEFAULT_MIN_CELL_N",
    "ONE_D_COMPARISON_CELLS",
    "analyze_session",
    "compare_session_deviations",
]
