"""Per-cell Fuel Comp proposal on the target revision's table and axes (N02.5).

Input: N02.3's samples (:mod:`src.analysis.fuel_comp_samples`) with N02.4's
droop columns (:func:`src.analysis.fuel_comp_droop.apply_droop`). For each
eligible sample:

- ``FC_applied`` is the firmware's lookup (:func:`src.decode.ecm_lookup.
  lookup_2d`: top-down search, truncating Q16 interpolation) on the table and
  axes **in force when it was logged**, at the sample's nominal rpm word and
  load byte (throttle and purge at their intervals' lower middle, idle air at
  its declared nominal, rebuilt through :func:`src.decode.fuel_comp_load.
  reconstruct` on that revision's curves);
- the **required factor** is ``FC_applied x (1 + remainder) x K_logged /
  K_now`` in raw cell units (128 = 1.0): the cell value that would have
  delivered the fuel the engine needed, once the pump's droop is taken out;
- its **design row** is the bilinear weights of the same operating point on
  the **target** axes (the byte rebuilt on the target's curves), as
  ``(sample, cell, weight)`` triplets -- a sparse design matrix that is exact
  for a linear interpolator.

The 256 cells are then fitted by least squares on those rows
(:func:`fit_table`). A cell with too little evidence keeps its current value
and the fit moves only cells that pass every declared refusal (minimum
weighted pairs, minimum drives, each drive contributing a minimum weight);
each fitted cell is held toward its current value by ``prior_pairs`` (a
ridge), so cells that two samples always share stay determined. Uncertainty
is a drive-level bootstrap. Every cell ends ``proposed`` (factor, CI, n,
drives), ``insufficient`` (n, drives, the refusals it failed) or
``not_covered``.

Samples are refused before the fit, each by name (:data:`REFUSALS`): no
droop separation, a cell that idle air, purge, throttle or the open-loop
delay bracket move across rows or columns (on the logged axes or the
target's), or a required factor that cannot be formed. Throttle from the
unvalidated DID route is a switch, not a refusal: off by default, and
:func:`fit_both_ways` reports the cell outcome with and without it, flagging
every cell those samples touch.

:func:`validate` fits on all drives but one (``fit.drive_unit``: sessions
of one outing are one drive) and predicts the held-out drive's required
factor, for every drive in turn, under four models on the same samples and
folds: the current table, one global factor, MAF g/s bins, and the fitted
cells. A paired drive bootstrap decides which beats which; a tie is never a
win. The error is called MAF-shaped only when the bins beat the global
factor, cell-shaped only when the cells do, and a uniform offset (K / MAF
scale) when the global factor beats the current table and neither shaped
model beats it.

This is a proposal computed from logged error, not a measured outcome.
Nothing here writes or exports a ROM image.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from typing import Any

import numpy as np
import pandas as pd

from src.analysis.fuel_comp_inputs import FitRules, FuelCompDeclaration, load_interval
from src.analysis.fuel_comp_rom import FuelCompCalibration
from src.decode.ecm_lookup import AxisHit, lookup_2d, search_axis
from src.decode.fuel_comp_load import Interval, columns_read, reconstruct, spans_one_cell

#: Bumped whenever a refusal, the required factor or a cell status changes.
FIT_VERSION = 1

CELLS = 256
SIDE = 16
CELL_STATUSES = ("proposed", "insufficient", "not_covered")

#: Sample refusal prefixes, in the order they are tested.
REFUSALS = (
    "rom_image_unresolved",
    "inputs_missing",
    "droop_none",
    "cell_ambiguous",
    "target_cell_ambiguous",
    "required_factor_missing",
)

#: Columns of :func:`prepare_fit_samples`' frame, in order.
PREPARED_COLUMNS = (
    "session",
    "drive",
    "route",
    "timestamp",
    "revision",
    "image_sha256",
    "commanded_source",
    "throttle_route",
    "unvalidated_route",
    "error_pct",
    "droop_kind",
    "droop_basis",
    "droop_psi",
    "droop_error_pct",
    "remainder_pct",
    "maf_gs",
    "bfs_raw",
    "rpm_word",
    "byte_logged",
    "byte_target",
    "fc_applied",
    "fc_applied_lo",
    "fc_applied_hi",
    "k_logged",
    "k_now",
    "required_raw",
    "fc_now_firmware",
    "row_index",
    "row_fraction",
    "col_index",
    "col_fraction",
    "fit_refusal",
)


class FitRefusalError(ValueError):
    """The fit cannot run at all (e.g. the target revision's image is missing)."""


def _num(value: Any) -> float:
    """A stored value as a float, NaN for anything that is not a number."""
    try:
        return float(value)
    except (TypeError, ValueError):
        return float("nan")


def _flag(row: Mapping[Any, Any], key: str) -> bool:
    value = row.get(key)
    if isinstance(value, str):
        return value.strip().lower() == "true"
    return bool(value) if value is not None and not pd.isna(value) else False


def _int(row: Mapping[Any, Any], key: str) -> int | None:
    value = _num(row.get(key))
    return None if not np.isfinite(value) else int(value)


def _fraction(hit: AxisHit) -> float:
    return float(hit.numerator) / float(hit.span) if hit.numerator else 0.0


def _rows_one_pair(breakpoints: Sequence[int], words: Interval) -> bool:
    first, _ = columns_read(breakpoints, words.lo)
    _, last = columns_read(breakpoints, words.hi)
    return last - first <= 1


def _culprits(row: Mapping[Any, Any]) -> str:
    names = [
        name
        for name, key in (
            ("idle_air", "idle_air_moves_cell"),
            ("purge", "purge_moves_cell"),
            ("throttle", "throttle_moves_cell"),
            ("rpm", "rpm_moves_cell"),
        )
        if _flag(row, key)
    ]
    return "+".join(names) if names else "inputs_jointly"


def prepare_fit_samples(
    samples: pd.DataFrame,
    calibrations: Mapping[str, FuelCompCalibration],
    target: FuelCompCalibration,
    declaration: FuelCompDeclaration,
) -> pd.DataFrame:
    """Every eligible sample with its required factor and target design row,
    or the refusal that keeps it out of the fit (:data:`PREPARED_COLUMNS`).

    ``samples`` carries :data:`src.analysis.fuel_comp_samples.COLUMNS` and
    :data:`src.analysis.fuel_comp_droop.DROOP_COLUMNS`. Rows not ``eligible``
    are not returned.
    """
    idle = declaration.inputs.idle_air
    rows: list[dict[str, Any]] = []
    eligible = samples[samples["state"].astype(str) == "eligible"]
    for record in eligible.to_dict("records"):
        out: dict[str, Any] = {name: np.nan for name in PREPARED_COLUMNS}
        validated = _flag(record, "throttle_validated")
        out.update(
            session=record["session"],
            drive=record.get("drive") if record.get("drive") is not None else record["session"],
            route=record["route"],
            timestamp=record["timestamp"],
            revision=record.get("revision"),
            image_sha256=record.get("image_sha256"),
            commanded_source=record.get("commanded_source"),
            throttle_route=record.get("throttle_route"),
            unvalidated_route=not validated,
            error_pct=_num(record.get("error_pct")),
            droop_kind=record.get("droop_kind", "none"),
            droop_basis=record.get("droop_basis"),
            droop_psi=_num(record.get("droop_psi")),
            droop_error_pct=_num(record.get("droop_error_pct")),
            remainder_pct=_num(record.get("remainder_pct")),
            maf_gs=_num(record.get("maf_gs")),
            bfs_raw=_num(record.get("bfs_raw")),
            k_now=target.k,
            fit_refusal=None,
        )
        rows.append(out)
        calibration = calibrations.get(str(record.get("revision")))
        if calibration is None:
            out["fit_refusal"] = f"rom_image_unresolved: {record.get('revision')}"
            continue
        out["k_logged"] = calibration.k
        values = {
            key: _int(record, key)
            for key in (
                "rpm_word_lo",
                "rpm_word_hi",
                "throttle_counts_lo",
                "throttle_counts_hi",
                "purge_area_lo",
                "purge_area_hi",
                "byte_lo",
                "byte_hi",
            )
        }
        missing = [key for key, value in values.items() if value is None]
        if missing:
            out["fit_refusal"] = "inputs_missing: " + ",".join(missing)
            continue
        words = Interval(values["rpm_word_lo"] or 0, values["rpm_word_hi"] or 0)
        throttle = Interval(values["throttle_counts_lo"] or 0, values["throttle_counts_hi"] or 0)
        purge = Interval(values["purge_area_lo"] or 0, values["purge_area_hi"] or 0)
        word = (words.lo + words.hi) // 2
        out["rpm_word"] = word
        # The nominal operating point and FC_applied, for every eligible
        # sample with inputs: a refused sample still informs the BFS model
        # (N02.6), where only the nominal FC enters.
        nominal: dict[str, int] = {}
        for name, cal in (("logged", calibration), ("target", target)):
            nominal[name] = reconstruct(
                throttle_counts=throttle.mid,
                rpm_word=word,
                idle_air=idle.nominal,
                purge_area=purge.mid,
                curves=cal.curves,
            ).byte
        out["byte_logged"], out["byte_target"] = nominal["logged"], nominal["target"]
        applied = lookup_2d(
            calibration.fuel_comp,
            calibration.rpm_breakpoints,
            calibration.load_axis_raw,
            word,
            nominal["logged"],
        ).value
        corners = [
            lookup_2d(
                calibration.fuel_comp, calibration.rpm_breakpoints, calibration.load_axis_raw, w, b
            ).value
            for w in (words.lo, words.hi)
            for b in (values["byte_lo"] or 0, values["byte_hi"] or 0)
        ]
        out["fc_applied"] = applied
        out["fc_applied_lo"], out["fc_applied_hi"] = min(corners), max(corners)
        if out["droop_kind"] not in ("measured", "modelled"):
            out["fit_refusal"] = f"droop_none: {record.get('droop_basis')}"
            continue
        if _flag(record, "cell_ambiguous"):
            out["fit_refusal"] = f"cell_ambiguous: {_culprits(record)}"
            continue
        target_interval = load_interval(
            throttle_counts=throttle,
            rpm_words=[words.lo, words.hi],
            idle_air=idle.interval,
            purge=purge,
            calibration=target,
        )
        if not spans_one_cell(target.load_axis_raw, target_interval.byte):
            out["fit_refusal"] = "target_cell_ambiguous: load columns"
            continue
        if not _rows_one_pair(target.rpm_breakpoints, words):
            out["fit_refusal"] = "target_cell_ambiguous: rpm rows"
            continue
        row_hit = search_axis(target.rpm_breakpoints, word)
        col_hit = search_axis(target.load_axis_raw, nominal["target"])
        assert row_hit is not None and col_hit is not None
        out["fc_now_firmware"] = lookup_2d(
            target.fuel_comp, target.rpm_breakpoints, target.load_axis_raw, word, nominal["target"]
        ).value
        out["row_index"], out["row_fraction"] = row_hit.index, _fraction(row_hit)
        out["col_index"], out["col_fraction"] = col_hit.index, _fraction(col_hit)
        remainder = out["remainder_pct"]
        if remainder is None or not np.isfinite(remainder):
            out["fit_refusal"] = "required_factor_missing: no remainder"
            continue
        out["required_raw"] = applied * (1.0 + remainder / 100.0) * calibration.k / target.k
    frame = pd.DataFrame(rows, columns=list(PREPARED_COLUMNS))
    refusal = frame["fit_refusal"].astype(object)
    frame["fit_refusal"] = refusal.where(refusal.notna(), None)
    return frame


# -- the sparse design --------------------------------------------------------------


@dataclass(frozen=True)
class Design:
    """``(sample, cell, weight)`` triplets: the bilinear weights of each
    sample on the target table (cell = 16 x row + column)."""

    sample: np.ndarray
    cell: np.ndarray
    weight: np.ndarray
    n_samples: int

    def predict(self, cells: np.ndarray) -> np.ndarray:
        """Each sample's interpolated value of a 256-cell table."""
        out = np.zeros(self.n_samples)
        np.add.at(out, self.sample, self.weight * cells[self.cell])
        return out

    def cell_weight(self, mask: np.ndarray | None = None) -> np.ndarray:
        """Summed weight per cell over the samples in ``mask`` (default all)."""
        take = np.ones(len(self.sample), dtype=bool) if mask is None else mask[self.sample]
        out = np.zeros(CELLS)
        np.add.at(out, self.cell[take], self.weight[take])
        return out


def design_of(prepared: pd.DataFrame) -> Design:
    """The bilinear weights of each row of ``prepared`` on the target axes
    (zero weights dropped, so a clamped or on-breakpoint sample has fewer)."""
    r = prepared["row_index"].to_numpy(dtype=int)
    fr = prepared["row_fraction"].to_numpy(dtype=float)
    c = prepared["col_index"].to_numpy(dtype=int)
    fc = prepared["col_fraction"].to_numpy(dtype=float)
    samples, cells, weights = [], [], []
    for dr, dc in ((0, 0), (0, 1), (1, 0), (1, 1)):
        w = (fr if dr else 1.0 - fr) * (fc if dc else 1.0 - fc)
        keep = w > 0
        samples.append(np.flatnonzero(keep))
        cells.append((r[keep] + dr) * SIDE + (c[keep] + dc))
        weights.append(w[keep])
    return Design(
        np.concatenate(samples).astype(int),
        np.concatenate(cells).astype(int),
        np.concatenate(weights),
        len(prepared),
    )


# -- cell support and the solve ---------------------------------------------------------


def _support(
    design: Design, drives: np.ndarray, rules: FitRules, mask: np.ndarray
) -> tuple[np.ndarray, np.ndarray, list[list[str]], np.ndarray]:
    """Per cell: summed weight, samples touching it, drives counted, and
    whether it passes the refusals (over the samples in ``mask``)."""
    weight = design.cell_weight(mask)
    take = mask[design.sample]
    n = np.zeros(CELLS, dtype=int)
    np.add.at(n, design.cell[take], 1)
    counted: list[list[str]] = [[] for _ in range(CELLS)]
    for drive in sorted(set(drives[mask])):
        mine = mask & (drives == drive)
        per = design.cell_weight(mine)
        for j in np.flatnonzero(per >= rules.min_drive_weight):
            counted[j].append(str(drive))
    passes = (weight >= rules.min_weighted_pairs) & np.array(
        [len(d) >= rules.min_drives for d in counted]
    )
    return weight, n, counted, passes


def _normal_parts(
    design: Design,
    y: np.ndarray,
    current: np.ndarray,
    free: np.ndarray,
    groups: np.ndarray,
    group_names: Sequence[Any],
) -> tuple[dict[Any, np.ndarray], dict[Any, np.ndarray], dict[Any, np.ndarray]]:
    """Per group: ``A^T A``, ``A^T y'`` over the free cells, and the group's
    weight per free cell (``y'`` = ``y`` minus the fixed cells' share)."""
    index = {int(cell): k for k, cell in enumerate(free)}
    k = len(free)
    fixed = np.ones(CELLS, dtype=bool)
    fixed[free] = False
    fixed_part = np.zeros(design.n_samples)
    sel = fixed[design.cell]
    np.add.at(fixed_part, design.sample[sel], design.weight[sel] * current[design.cell[sel]])
    residual = y - fixed_part
    dense = np.zeros((design.n_samples, k))
    for s, cell, w in zip(design.sample, design.cell, design.weight, strict=True):
        col = index.get(int(cell))
        if col is not None:
            dense[s, col] += w
    normals, rhs, weights = {}, {}, {}
    for name in group_names:
        rows = groups == name
        a = dense[rows]
        normals[name] = a.T @ a
        rhs[name] = a.T @ residual[rows]
        weights[name] = a.sum(axis=0)
    return normals, rhs, weights


def _solve(
    normals: Mapping[Any, np.ndarray],
    rhs: Mapping[Any, np.ndarray],
    counts: Mapping[Any, float],
    prior: np.ndarray,
    ridge: float,
) -> np.ndarray:
    k = len(prior)
    n = np.eye(k) * ridge
    r = prior * ridge
    for name, count in counts.items():
        if count:
            n = n + count * normals[name]
            r = r + count * rhs[name]
    return np.asarray(np.linalg.lstsq(n, r, rcond=None)[0], dtype=float)


def _fit_cells(
    design: Design,
    y: np.ndarray,
    current: np.ndarray,
    free: np.ndarray,
    mask: np.ndarray,
    ridge: float,
) -> np.ndarray:
    """The 256-cell table: least squares on the free cells over the samples
    in ``mask``, every other cell at its current value."""
    table = current.astype(float).copy()
    if not len(free):
        return table
    groups = np.where(mask, "in", "out")
    normals, rhs, _ = _normal_parts(design, y, current, free, groups, ["in"])
    table[free] = _solve(normals, rhs, {"in": 1.0}, current[free].astype(float), ridge)
    return table


@dataclass
class CellResult:
    """One cell's outcome."""

    row: int
    col: int
    status: str
    current_raw: int
    n: int
    weighted_pairs: float
    drives: list[str]
    refusals: list[str] = field(default_factory=list)
    fitted_raw: float | None = None
    proposed_raw: int | None = None
    ci_raw: tuple[float, float] | None = None
    bootstrap_support: float | None = None
    routes: dict[str, int] = field(default_factory=dict)
    unvalidated_weight_share: float = 0.0

    @property
    def factor(self) -> float | None:
        return None if self.fitted_raw is None else self.fitted_raw / 128.0

    @property
    def change_pct(self) -> float | None:
        if self.fitted_raw is None:
            return None
        return 100.0 * (self.fitted_raw / self.current_raw - 1.0)

    @property
    def change_resolved(self) -> bool | None:
        """Whether the bootstrap interval excludes the current value."""
        if self.ci_raw is None:
            return None
        lo, hi = self.ci_raw
        return not lo <= self.current_raw <= hi

    def as_dict(self) -> dict[str, Any]:
        return {
            "row": self.row,
            "col": self.col,
            "status": self.status,
            "current_raw": self.current_raw,
            "n": self.n,
            "weighted_pairs": round(self.weighted_pairs, 3),
            "drives": list(self.drives),
            "refusals": list(self.refusals),
            "fitted_raw": self.fitted_raw,
            "factor": self.factor,
            "proposed_raw": self.proposed_raw,
            "change_pct": self.change_pct,
            "ci_raw": None if self.ci_raw is None else list(self.ci_raw),
            "ci_factor": None if self.ci_raw is None else [v / 128.0 for v in self.ci_raw],
            "change_resolved": self.change_resolved,
            "bootstrap_support": self.bootstrap_support,
            "routes": dict(self.routes),
            "unvalidated_route_weight_share": round(self.unvalidated_weight_share, 4),
            "unvalidated_route": self.unvalidated_weight_share > 0,
        }


@dataclass
class TableFit:
    """Every cell's outcome for one configuration of the fit."""

    include_unvalidated: bool
    cells: list[CellResult]
    n_samples: int
    drives: list[str]
    refusal_counts: dict[str, int]
    excluded_unvalidated: int

    def status_counts(self) -> dict[str, int]:
        counts = {s: 0 for s in CELL_STATUSES}
        for cell in self.cells:
            counts[cell.status] += 1
        return counts

    def proposed_table(self) -> np.ndarray:
        """The 256 cells as integers a flash would carry: proposed cells
        rounded, every other cell at its current value."""
        out = np.array([c.current_raw for c in self.cells], dtype=int)
        for k, cell in enumerate(self.cells):
            if cell.proposed_raw is not None:
                out[k] = cell.proposed_raw
        return out

    def as_dict(self) -> dict[str, Any]:
        return {
            "include_unvalidated_throttle_route": self.include_unvalidated,
            "n_samples": self.n_samples,
            "drives": list(self.drives),
            "excluded_unvalidated_route_samples": self.excluded_unvalidated,
            "status_counts": self.status_counts(),
            "sample_refusals": dict(self.refusal_counts),
            "cells": [c.as_dict() for c in self.cells],
        }


def _usable(prepared: pd.DataFrame, include_unvalidated: bool) -> np.ndarray:
    ok = np.array(prepared["fit_refusal"].isna().to_numpy(), dtype=bool)
    if not include_unvalidated:
        ok = ok & ~np.asarray(prepared["unvalidated_route"].astype(bool).to_numpy(), dtype=bool)
    return ok


def design_vs_firmware(prepared: pd.DataFrame, target: FuelCompCalibration) -> dict[str, Any]:
    """How far the real-valued bilinear design (what the fit solves on) is
    from the firmware's truncating Q16 lookup (what the ECM reads), on the
    current table at every usable sample: raw counts, design minus firmware."""
    usable = prepared[prepared["fit_refusal"].isna()].reset_index(drop=True)
    if usable.empty:
        return {"n": 0}
    current = np.array([v for row in target.fuel_comp for v in row], dtype=float)
    diff = design_of(usable).predict(current) - usable["fc_now_firmware"].to_numpy(dtype=float)
    return {
        "n": int(len(diff)),
        "min_raw": float(diff.min()),
        "max_raw": float(diff.max()),
        "median_raw": float(np.median(diff)),
        "note": (
            "The fit solves on real-valued bilinear weights; the ECM truncates each "
            "interpolation toward the lower cell (0x026F10). The knock-on reads the "
            "rounded proposal through the firmware lookup; the fit and the held-out "
            "validation do not."
        ),
    }


def refusal_counts(prepared: pd.DataFrame) -> dict[str, int]:
    """Prepared samples per refusal (``fit_refusal`` text), and ``usable``."""
    reasons = prepared["fit_refusal"].fillna("usable").astype(str)
    return {str(k): int(v) for k, v in reasons.value_counts().sort_index().items()}


def fit_table(
    prepared: pd.DataFrame,
    target: FuelCompCalibration,
    rules: FitRules,
    *,
    include_unvalidated: bool,
) -> TableFit:
    """The 256 cells from the usable samples, with a drive-level bootstrap."""
    usable = _usable(prepared, include_unvalidated)
    subset = prepared[usable].reset_index(drop=True)
    current = np.array([v for row in target.fuel_comp for v in row], dtype=float)
    cells = [
        CellResult(
            row=k // SIDE,
            col=k % SIDE,
            status="not_covered",
            current_raw=int(current[k]),
            n=0,
            weighted_pairs=0.0,
            drives=[],
        )
        for k in range(CELLS)
    ]
    excluded = (
        int((prepared["fit_refusal"].isna() & prepared["unvalidated_route"].astype(bool)).sum())
        if not include_unvalidated
        else 0
    )
    fit = TableFit(
        include_unvalidated=include_unvalidated,
        cells=cells,
        n_samples=int(len(subset)),
        drives=sorted(set(subset["drive"].astype(str))),
        refusal_counts=refusal_counts(prepared),
        excluded_unvalidated=excluded,
    )
    if subset.empty:
        return fit
    design = design_of(subset)
    drives = subset["drive"].astype(str).to_numpy()
    y = subset["required_raw"].to_numpy(dtype=float)
    everyone = np.ones(len(subset), dtype=bool)
    weight, n, counted, passes = _support(design, drives, rules, everyone)
    unvalidated = subset["unvalidated_route"].astype(bool).to_numpy()
    unvalidated_weight = design.cell_weight(unvalidated)
    routes = subset["route"].astype(str).to_numpy()
    for k in range(CELLS):
        cell = cells[k]
        if weight[k] <= 0:
            continue
        touching = np.unique(design.sample[design.cell == k])
        cell.n = int(n[k])
        cell.weighted_pairs = float(weight[k])
        cell.drives = counted[k]
        cell.routes = {
            str(r): int(c)
            for r, c in zip(*np.unique(routes[touching], return_counts=True), strict=True)
        }
        cell.unvalidated_weight_share = float(unvalidated_weight[k] / weight[k])
        if passes[k]:
            cell.status = "proposed"
            continue
        cell.status = "insufficient"
        if weight[k] < rules.min_weighted_pairs:
            cell.refusals.append(
                f"min_weighted_pairs: {weight[k]:.2f} < {rules.min_weighted_pairs:g}"
            )
        if len(counted[k]) < rules.min_drives:
            cell.refusals.append(
                f"min_drives: {len(counted[k])} drives with >= {rules.min_drive_weight:g} "
                f"weighted pairs < {rules.min_drives}"
            )
    free = np.flatnonzero(passes)
    if not len(free):
        return fit
    names = sorted(set(drives))
    normals, rhs, drive_weight = _normal_parts(design, y, current, free, drives, names)
    ridge = rules.prior_pairs
    prior = current[free]
    fitted = _solve(normals, rhs, {d: 1.0 for d in names}, prior, ridge)
    rng = np.random.default_rng(rules.bootstrap.seed)
    replicates = np.full((rules.bootstrap.resamples, len(free)), np.nan)
    for b in range(rules.bootstrap.resamples):
        picks = rng.integers(0, len(names), len(names))
        counts = np.bincount(picks, minlength=len(names)).astype(float)
        count_of = {name: counts[i] for i, name in enumerate(names)}
        solution = _solve(normals, rhs, count_of, prior, ridge)
        have = sum(count_of[name] * drive_weight[name] for name in names)
        solution[have < rules.min_drive_weight] = np.nan
        replicates[b] = solution
    lo_pct, hi_pct = rules.bootstrap.interval_pct
    for k, cell_index in enumerate(free):
        cell = cells[int(cell_index)]
        cell.fitted_raw = float(fitted[k])
        cell.proposed_raw = int(np.clip(np.rint(fitted[k]), 0, 255))
        column = replicates[:, k]
        column = column[np.isfinite(column)]
        cell.bootstrap_support = float(len(column) / rules.bootstrap.resamples)
        if len(column):
            cell.ci_raw = (
                float(np.percentile(column, lo_pct)),
                float(np.percentile(column, hi_pct)),
            )
    return fit


def fit_both_ways(
    prepared: pd.DataFrame, target: FuelCompCalibration, rules: FitRules
) -> dict[str, TableFit]:
    """The declared configuration and the other one, by name."""
    return {
        "did_route_excluded": fit_table(prepared, target, rules, include_unvalidated=False),
        "did_route_included": fit_table(prepared, target, rules, include_unvalidated=True),
    }


# -- held-out validation and the MAF diagnostic -------------------------------------------


def _maf_bins(maf: np.ndarray, edges: Sequence[float]) -> np.ndarray:
    """Bin index per sample (``-1`` outside the edges or unpaired)."""
    out = np.full(len(maf), -1, dtype=int)
    ok = np.isfinite(maf)
    index = np.searchsorted(np.asarray(edges, dtype=float), maf[ok], side="right") - 1
    inside = (index >= 0) & (index < len(edges) - 1)
    out[np.flatnonzero(ok)[inside]] = index[inside]
    return out


def _maf_factors(
    baseline: np.ndarray,
    y: np.ndarray,
    bins: np.ndarray,
    drives: np.ndarray,
    mask: np.ndarray,
    rules: FitRules,
) -> np.ndarray:
    """Per MAF bin, the least-squares multiplier on the current table's
    prediction over the training samples, held toward 1 by the same prior
    (one weighted pair at full cell scale); 1 where a bin fails the same
    refusals as a cell (minimum pairs, minimum drives)."""
    n_bins = len(rules.maf_bins_gs) - 1
    factors = np.ones(n_bins)
    ridge = rules.prior_pairs * 128.0**2
    for b in range(n_bins):
        take = mask & (bins == b)
        if take.sum() < rules.min_weighted_pairs:
            continue
        per_drive = pd.Series(drives[take]).value_counts().to_numpy(dtype=float)
        if int((per_drive >= rules.min_drive_weight).sum()) < rules.min_drives:
            continue
        a = baseline[take]
        factors[b] = float((a @ y[take] + ridge) / (a @ a + ridge))
    return factors


def _global_factor(baseline: np.ndarray, y: np.ndarray, mask: np.ndarray, rules: FitRules) -> float:
    """One least-squares multiplier on the current table over the training
    samples, with the bins' prior: a uniform offset (K or a MAF scale)."""
    ridge = rules.prior_pairs * 128.0**2
    a = baseline[mask]
    return float((a @ y[mask] + ridge) / (a @ a + ridge))


#: The held-out models, in the order they are reported.
MODELS = ("current_table", "global_factor", "maf_bins", "fitted_cells")

VERDICT_IMPROVEMENT = "improvement"
VERDICT_NONE = "no improvement"
DIAGNOSTIC_MAF = "the error is MAF-shaped; Fuel Comp is the wrong table"
DIAGNOSTIC_CELLS = "the error is cell-shaped: Fuel Comp cells explain more than a global factor"
DIAGNOSTIC_UNIFORM = "uniform offset (K / MAF scale)"
DIAGNOSTIC_NEITHER = "neither a global factor, MAF bins nor Fuel Comp cells explain held-out error"


@dataclass
class Validation:
    """Held-out error under every model, and the comparisons between them."""

    include_unvalidated: bool
    folds: int
    n_compared: int
    n_without_maf: int
    #: Per model: held-out RMS (%), mean signed residual (%), per-drive
    #: better / worse / same against the current table, and its size per fold.
    models: dict[str, dict[str, Any]]
    #: ``"a<b"`` -> share of drive resamples in which a's MSE is below b's.
    comparisons: dict[str, float]
    folds_with_a_proposal: int
    verdict: str
    diagnostic: str
    per_drive: list[dict[str, Any]]

    def rms(self, model: str) -> float | None:
        value = self.models.get(model, {}).get("rms_pct")
        return None if value is None else float(value)

    @property
    def rms_current_pct(self) -> float | None:
        return self.rms("current_table")

    @property
    def rms_cells_pct(self) -> float | None:
        return self.rms("fitted_cells")

    @property
    def rms_maf_bins_pct(self) -> float | None:
        return self.rms("maf_bins")

    @property
    def rms_global_pct(self) -> float | None:
        return self.rms("global_factor")

    def as_dict(self) -> dict[str, Any]:
        return {
            "include_unvalidated_throttle_route": self.include_unvalidated,
            "folds": self.folds,
            "folds_with_a_proposal": self.folds_with_a_proposal,
            "n_compared": self.n_compared,
            "n_held_out_without_maf": self.n_without_maf,
            "models": self.models,
            "comparisons": self.comparisons,
            "verdict": self.verdict,
            "diagnostic": self.diagnostic,
            "per_drive": self.per_drive,
        }


def _rms(values: np.ndarray) -> float | None:
    return float(np.sqrt(np.mean(values * values))) if len(values) else None


def _beats(share: float, rules: FitRules) -> bool:
    return share >= rules.model_comparison.confidence


def validate(
    prepared: pd.DataFrame,
    target: FuelCompCalibration,
    rules: FitRules,
    *,
    include_unvalidated: bool,
) -> Validation:
    """Leave one drive out (``fit.drive_unit``): fit each model on the other
    drives and predict the held-out drive's required factor, every drive in
    turn, on the same samples and folds:

    - ``current_table``: the target table as it stands;
    - ``global_factor``: the current table times one fitted multiplier -- a
      uniform offset, what K or a MAF scale would correct;
    - ``maf_bins``: the current table times a multiplier per MAF bin;
    - ``fitted_cells``: the cells passing the refusals refitted.

    Residual: ``100 x (required / predicted - 1)`` over held-out samples with
    a paired MAF read. Models are compared by a paired drive bootstrap of
    their pooled MSE (``fit.model_comparison``); a difference the drives do
    not resolve is a tie, never a win.

    Verdict (``rms_and_drive_majority``): improvement when the cells beat the
    current table in the bootstrap and on more held-out drives than not.
    Diagnostic: MAF-shaped only when the bins beat the global factor (and the
    cells); cell-shaped only when the cells beat the global factor (and the
    bins); a uniform offset when the global factor beats the current table
    and neither shaped model beats it; otherwise neither.
    """
    usable = _usable(prepared, include_unvalidated)
    subset = prepared[usable].reset_index(drop=True)
    names = sorted(set(subset["drive"].astype(str)))
    if len(names) < 2:
        why = f"not evaluable: {len(names)} drives with usable samples; leave-one-drive-out needs 2"
        return Validation(
            include_unvalidated, 0, 0, 0, {}, {}, 0, f"{VERDICT_NONE} ({why})", why, []
        )
    design = design_of(subset)
    drives = subset["drive"].astype(str).to_numpy()
    y = subset["required_raw"].to_numpy(dtype=float)
    current = np.array([v for row in target.fuel_comp for v in row], dtype=float)
    baseline = design.predict(current)
    maf = subset["maf_gs"].to_numpy(dtype=float)
    bins = _maf_bins(maf, rules.maf_bins_gs)
    predicted = {m: np.full(len(subset), np.nan) for m in MODELS}
    predicted["current_table"] = baseline.copy()
    sizes: dict[str, list[int]] = {"global_factor": [], "maf_bins": [], "fitted_cells": []}
    with_proposal = 0
    for held in names:
        test = drives == held
        train = ~test
        _, _, _, passes = _support(design, drives, rules, train)
        free = np.flatnonzero(passes)
        with_proposal += int(len(free) > 0)
        sizes["fitted_cells"].append(int(len(free)))
        table = _fit_cells(design, y, current, free, train, rules.prior_pairs)
        predicted["fitted_cells"][test] = design.predict(table)[test]
        factors = _maf_factors(baseline, y, bins, drives, train, rules)
        sizes["maf_bins"].append(int((factors != 1.0).sum()))
        scale = np.where(bins >= 0, factors[np.clip(bins, 0, None)], 1.0)
        predicted["maf_bins"][test] = (baseline * scale)[test]
        sizes["global_factor"].append(1)
        predicted["global_factor"][test] = (baseline * _global_factor(baseline, y, train, rules))[
            test
        ]
    compared = np.isfinite(maf)
    residual = {m: 100.0 * (y / predicted[m] - 1.0) for m in MODELS}
    held_drives = [n for n in names if (compared & (drives == n)).any()]
    if not held_drives:
        why = "not evaluable: no held-out sample has a paired MAF read"
        return Validation(
            include_unvalidated,
            len(names),
            0,
            int((~compared).sum()),
            {},
            {},
            with_proposal,
            f"{VERDICT_NONE} ({why})",
            why,
            [],
        )
    # Per drive: summed squared residual and count, for pooled MSE resamples.
    sq = {
        m: np.array([np.sum(residual[m][compared & (drives == n)] ** 2) for n in held_drives])
        for m in MODELS
    }
    count = np.array([int((compared & (drives == n)).sum()) for n in held_drives], dtype=float)
    per_drive_mse = {m: sq[m] / count for m in MODELS}
    rng = np.random.default_rng(rules.model_comparison.seed)
    picks = rng.integers(0, len(held_drives), (rules.model_comparison.resamples, len(held_drives)))
    pooled = {m: sq[m][picks].sum(axis=1) / count[picks].sum(axis=1) for m in MODELS}
    comparisons: dict[str, float] = {}
    for a in MODELS:
        for b in MODELS:
            if a != b:
                comparisons[f"{a}<{b}"] = float(np.mean(pooled[a] < pooled[b]))
    models: dict[str, dict[str, Any]] = {}
    for m in MODELS:
        values = residual[m][compared]
        better = int(np.sum(per_drive_mse[m] < per_drive_mse["current_table"] - 1e-12))
        worse = int(np.sum(per_drive_mse[m] > per_drive_mse["current_table"] + 1e-12))
        models[m] = {
            "rms_pct": _rms(values),
            "mean_signed_residual_pct": float(np.mean(values)),
            "drives_better_than_current": better,
            "drives_worse_than_current": worse,
            "drives_same_as_current": int(len(held_drives) - better - worse),
            "parameters_per_fold": (
                None
                if m == "current_table"
                else {
                    "min": min(sizes[m]),
                    "median": float(np.median(sizes[m])),
                    "max": max(sizes[m]),
                }
            ),
            "parameter_kind": {
                "current_table": "none",
                "global_factor": "one multiplier",
                "maf_bins": "active MAF bins (multiplier != 1)",
                "fitted_cells": "free cells (passing the refusals)",
            }[m],
        }

    def beats(a: str, b: str) -> bool:
        return _beats(comparisons[f"{a}<{b}"], rules)

    cells = models["fitted_cells"]
    better = beats("fitted_cells", "current_table") and (
        cells["drives_better_than_current"] > cells["drives_worse_than_current"]
    )
    verdict = VERDICT_IMPROVEMENT if better else VERDICT_NONE
    if not with_proposal:
        verdict = f"{VERDICT_NONE} (no cell passed the refusals in any fold)"
    if beats("maf_bins", "global_factor") and not beats("fitted_cells", "maf_bins"):
        diagnostic = DIAGNOSTIC_MAF
    elif beats("fitted_cells", "global_factor") and not beats("maf_bins", "fitted_cells"):
        diagnostic = DIAGNOSTIC_CELLS
    elif beats("global_factor", "current_table"):
        diagnostic = DIAGNOSTIC_UNIFORM
    else:
        diagnostic = DIAGNOSTIC_NEITHER
    per_drive = [
        {
            "drive": n,
            "n": int(count[i]),
            **{f"rms_{m}_pct": float(np.sqrt(per_drive_mse[m][i])) for m in MODELS},
        }
        for i, n in enumerate(held_drives)
    ]
    return Validation(
        include_unvalidated=include_unvalidated,
        folds=len(names),
        n_compared=int(compared.sum()),
        n_without_maf=int((~compared).sum()),
        models=models,
        comparisons=comparisons,
        folds_with_a_proposal=with_proposal,
        verdict=verdict,
        diagnostic=diagnostic,
        per_drive=per_drive,
    )


__all__ = [
    "CELLS",
    "CELL_STATUSES",
    "DIAGNOSTIC_CELLS",
    "DIAGNOSTIC_MAF",
    "DIAGNOSTIC_NEITHER",
    "DIAGNOSTIC_UNIFORM",
    "MODELS",
    "FIT_VERSION",
    "PREPARED_COLUMNS",
    "REFUSALS",
    "VERDICT_IMPROVEMENT",
    "VERDICT_NONE",
    "CellResult",
    "Design",
    "FitRefusalError",
    "TableFit",
    "Validation",
    "design_of",
    "design_vs_firmware",
    "fit_both_ways",
    "fit_table",
    "prepare_fit_samples",
    "refusal_counts",
    "validate",
]
