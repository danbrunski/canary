"""What a proposed Fuel Comp change moves besides fuel (N02.6).

Fuel Comp is applied inside BFS (``docs/ecu-firmware-analysis.md``, "how BFS
is formed"), so a cell edit is not confined to fuel quantity. At every logged
sample a proposed cell touches, this predicts:

- the **BFS change**: the Fuel Comp ratio, proposed over current, both read
  by the firmware lookup (:func:`src.decode.ecm_lookup.lookup_2d`) on the
  target revision's axes with the proposal rounded to whole cells, as a flash
  would carry them;
- the **Target AFR X byte** before and after (``X = min(255, BFS x 128 /
  Load Point Scaler)``, :func:`src.analysis.target_phi.x_from_bfs`, the scaler
  read from the image), and whether the shift crosses a Target AFR column
  breakpoint;
- the **commanded phi** before and after, through the target-phi twin's own
  lookup (:func:`src.analysis.target_phi.predict`: the E02 kernel, X clamped
  at the last breakpoint, the enrichment gate as decoded);
- the **timing-trim cell**: the column X reads on the BFS-X axis ``0xCC83``
  (``rom_map.yaml`` ``timing_trim_load_axis``), before and after.

BFS at the sample: DID 0x1208 where it was read within the route's pairing
separation (``measured``); otherwise ``c x K x FC/128 x MAF/rpm`` with ``c``
the median per-sample ratio where both were read (``modelled``,
:func:`bfs_model`); otherwise not predicted, saying why. The BFS a sample
would have on the target revision as it stands is the logged BFS times
``K_now / K_logged x FC_now / FC_applied``.

Every number here is a prediction from a proposal, never a measured outcome.
Nothing here writes or exports a ROM image.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field
from typing import Any

import numpy as np
import pandas as pd

from src.analysis.fuel_comp_fit import SIDE, TableFit, design_of
from src.analysis.fuel_comp_inputs import FuelCompDeclaration
from src.analysis.fuel_comp_rom import FuelCompCalibration
from src.analysis.rom_image import table_values
from src.analysis.rom_map import RomMap
from src.analysis.target_phi import ResolvedTable, TwinDefinition, predict, x_from_bfs
from src.decode.ecm_lookup import lookup_2d, search_axis

#: Bumped whenever a predicted quantity or its source changes meaning.
KNOCK_ON_VERSION = 1


class KnockOnRefusalError(ValueError):
    """The knock-on tables cannot be read; ``reason`` names why."""

    def __init__(self, reason: str, message: str) -> None:
        super().__init__(f"{reason}: {message}")
        self.reason = reason
        self.message = message

    def __reduce__(self) -> tuple[object, ...]:
        # Rebuilt from its constructor's arguments, so it survives a process
        # boundary (pickle); tests/test_exceptions_pickle.py holds every class to it.
        return (type(self), (self.reason, self.message), self.__dict__)


@dataclass(frozen=True)
class KnockOnTables:
    """The target revision's bytes a knock-on prediction reads."""

    fuel_comp: FuelCompCalibration
    target_afr: ResolvedTable
    twin: TwinDefinition
    #: BFS-X columns of the timing trims (``0xCC83``), raw X counts.
    timing_load_axis: tuple[int, ...]
    load_point_scaler: int

    def provenance(self) -> dict[str, Any]:
        return {
            "fuel_comp": self.fuel_comp.provenance(),
            "target_afr": self.target_afr.provenance(),
            # The twin's own standing (E05.1): while these stand, a predicted
            # phi is the gated lookup's word, not the final commanded word.
            "target_afr_blockers": list(self.target_afr.application.blockers),
            "timing_load_axis": list(self.timing_load_axis),
            "load_point_scaler": self.load_point_scaler,
        }


def knock_on_tables(
    image: bytes,
    rom_map: RomMap,
    fuel_comp: FuelCompCalibration,
    target_afr: ResolvedTable,
    twin: TwinDefinition,
    declaration: FuelCompDeclaration,
) -> KnockOnTables:
    """The timing axis and scaler from the same image the Fuel Comp and
    Target AFR tables came from (refused unless their hashes agree)."""
    if target_afr.image_sha256 != fuel_comp.image_sha256:
        raise KnockOnRefusalError(
            "image_mismatch",
            f"Target AFR came from {target_afr.image_sha256[:12]}..., Fuel Comp from "
            f"{fuel_comp.image_sha256[:12]}...: a knock-on reads one image",
        )
    if target_afr.load_point_scaler is None:
        raise KnockOnRefusalError(
            "table_missing", "the Target AFR table carries no Load Point Scaler from the image"
        )
    wanted = declaration.knock_on.timing_trim_load_axis
    entry = next((t for t in table_values(image, rom_map) if t["id"] == wanted), None)
    if entry is None:
        raise KnockOnRefusalError("table_missing", f"rom_map.yaml places no {wanted!r}")
    if entry["confidence"] != "verified":
        raise KnockOnRefusalError(
            "table_unverified", f"{wanted} is {entry['confidence']}, not verified"
        )
    axis = tuple(int(v) for row in entry["raw"] for v in row)
    return KnockOnTables(fuel_comp, target_afr, twin, axis, int(target_afr.load_point_scaler))


@dataclass(frozen=True)
class BfsModel:
    """``BFS = c x K x FC/128 x MAF/rpm``, or why there is none."""

    c: float | None
    n: int
    p10: float | None
    p90: float | None
    refusal: str | None
    #: The same median over subsets (idle / off idle, each throttle route),
    #: with n, so a constant set mostly by one kind of sample is visible.
    subsets: dict[str, dict[str, float | int | None]] = field(default_factory=dict)

    def as_dict(self) -> dict[str, Any]:
        return {
            "c": self.c,
            "n": self.n,
            "p10_p90": [self.p10, self.p90],
            "refusal": self.refusal,
            "subsets": self.subsets,
        }


def _air_term(frame: pd.DataFrame, rpm_per_count: float) -> np.ndarray:
    """``K x FC_applied / 128 x MAF / rpm`` per row (NaN where any is missing)."""
    rpm = frame["rpm_word"].to_numpy(dtype=float) * rpm_per_count
    with np.errstate(divide="ignore", invalid="ignore"):
        return np.asarray(
            frame["k_logged"].to_numpy(dtype=float)
            * frame["fc_applied"].to_numpy(dtype=float)
            / 128.0
            * frame["maf_gs"].to_numpy(dtype=float)
            / rpm,
            dtype=float,
        )


def bfs_model(prepared: pd.DataFrame, declaration: FuelCompDeclaration) -> BfsModel:
    """The median per-sample ratio ``BFS / (K x FC/128 x MAF/rpm)`` over every
    eligible sample with BFS and MAF both read and a nominal FC_applied
    (never a ratio of sums). A sample refused from the cell fit still counts:
    only its nominal FC enters, and the ratio's p10-p90 is reported."""
    rule = declaration.knock_on.bfs_model
    term = _air_term(prepared, declaration.inputs.rpm.rpm_per_count)
    bfs = prepared["bfs_raw"].to_numpy(dtype=float)
    ok = np.isfinite(term) & np.isfinite(bfs) & (term > 0)
    ratio = bfs[ok] / term[ok]
    rpm = prepared["rpm_word"].to_numpy(dtype=float)[ok] * declaration.inputs.rpm.rpm_per_count
    route = prepared["throttle_route"].astype(str).to_numpy()[ok]
    masks = {
        f"idle (rpm <= {rule.idle_rpm_max:g})": rpm <= rule.idle_rpm_max,
        f"off idle (rpm > {rule.idle_rpm_max:g})": rpm > rule.idle_rpm_max,
        **{f"throttle route {r}": route == r for r in sorted(set(route))},
    }
    subsets: dict[str, dict[str, float | int | None]] = {
        name: {
            "n": int(mask.sum()),
            "median": float(np.median(ratio[mask])) if mask.any() else None,
        }
        for name, mask in masks.items()
    }
    if len(ratio) < rule.min_pairs:
        return BfsModel(
            None,
            int(len(ratio)),
            None,
            None,
            f"bfs_model_refused: {len(ratio)} eligible samples with DID 0x1208, MAF and a "
            f"nominal FC_applied, the model needs {rule.min_pairs}",
            subsets,
        )
    return BfsModel(
        float(np.median(ratio)),
        int(len(ratio)),
        float(np.quantile(ratio, 0.1)),
        float(np.quantile(ratio, 0.9)),
        None,
        subsets,
    )


def _crosses(breakpoints: tuple[int, ...], a: int, b: int) -> bool:
    lo, hi = min(a, b), max(a, b)
    return any(lo < point <= hi for point in breakpoints)


def _phi(tables: KnockOnTables, x: int, word: int, flag: int) -> tuple[float | None, str | None]:
    prediction = predict(tables.target_afr, tables.twin, load_raw=x, rpm_raw=word, flag_raw=flag)
    return prediction.phi, prediction.reason


def predict_samples(
    prepared: pd.DataFrame,
    fit: TableFit,
    tables: KnockOnTables,
    model: BfsModel,
    declaration: FuelCompDeclaration,
) -> pd.DataFrame:
    """One row per usable sample of ``fit``'s configuration that a changed
    cell touches: BFS, X, commanded phi and timing column before and after."""
    usable = prepared[prepared["fit_refusal"].isna()]
    if not fit.include_unvalidated:
        usable = usable[~usable["unvalidated_route"].astype(bool)]
    usable = usable.reset_index(drop=True)
    columns = [
        "session",
        "route",
        "timestamp",
        "cells",
        "bfs_kind",
        "bfs_reason",
        "fc_now",
        "fc_proposed",
        "bfs_ratio",
        "bfs_now",
        "bfs_proposed",
        "x_now",
        "x_proposed",
        "x_shift",
        "crosses_target_afr_breakpoint",
        "phi_now",
        "phi_proposed",
        "phi_change",
        "phi_reason",
        "timing_col_now",
        "timing_col_proposed",
        "crosses_timing_breakpoint",
    ]
    if usable.empty:
        return pd.DataFrame(columns=columns)
    current = np.array([c.current_raw for c in fit.cells], dtype=int)
    proposed = fit.proposed_table()
    changed = proposed != current
    design = design_of(usable)
    touched: dict[int, list[int]] = {}
    for s, cell in zip(design.sample, design.cell, strict=True):
        if changed[cell]:
            touched.setdefault(int(s), []).append(int(cell))
    if not touched:
        return pd.DataFrame(columns=columns)
    target = tables.fuel_comp
    now_table = current.reshape(SIDE, SIDE).tolist()
    new_table = proposed.reshape(SIDE, SIDE).tolist()
    flag = declaration.knock_on.pedal_flag_assumed
    term = _air_term(usable, declaration.inputs.rpm.rpm_per_count)
    timing_axis = tables.timing_load_axis
    afr_columns = tables.target_afr.column_breakpoints
    rows: list[dict[str, Any]] = []
    for s in sorted(touched):
        record = usable.iloc[s]
        word, byte = int(record["rpm_word"]), int(record["byte_target"])
        fc_now = lookup_2d(now_table, target.rpm_breakpoints, target.load_axis_raw, word, byte)
        fc_new = lookup_2d(new_table, target.rpm_breakpoints, target.load_axis_raw, word, byte)
        row: dict[str, Any] = {name: np.nan for name in columns}
        row.update(
            session=record["session"],
            route=record["route"],
            timestamp=record["timestamp"],
            cells=",".join(f"{c // SIDE}:{c % SIDE}" for c in sorted(set(touched[s]))),
            fc_now=fc_now.value,
            fc_proposed=fc_new.value,
            bfs_ratio=fc_new.value / fc_now.value,
            bfs_kind="none",
            bfs_reason=None,
        )
        rows.append(row)
        logged = float(record["bfs_raw"])
        if np.isfinite(logged):
            row["bfs_kind"] = "measured"
        elif model.c is not None and np.isfinite(term[s]):
            logged = model.c * float(term[s])
            row["bfs_kind"] = "modelled"
        else:
            row["bfs_reason"] = (
                "BFS (DID 0x1208) not paired and " + (model.refusal or "MAF not paired")
                if model.c is None
                else "BFS (DID 0x1208) and MAF not paired"
            )
            continue
        bfs_now = (
            logged
            * float(record["k_now"])
            / float(record["k_logged"])
            * fc_now.value
            / float(record["fc_applied"])
        )
        bfs_new = bfs_now * fc_new.value / fc_now.value
        x_now = x_from_bfs(int(bfs_now), tables.load_point_scaler, tables.twin)
        x_new = x_from_bfs(int(bfs_new), tables.load_point_scaler, tables.twin)
        phi_now, reason_now = _phi(tables, x_now, word, flag)
        phi_new, reason_new = _phi(tables, x_new, word, flag)
        t_now = search_axis(timing_axis, x_now)
        t_new = search_axis(timing_axis, x_new)
        assert t_now is not None and t_new is not None
        row.update(
            bfs_now=bfs_now,
            bfs_proposed=bfs_new,
            x_now=x_now,
            x_proposed=x_new,
            x_shift=x_new - x_now,
            crosses_target_afr_breakpoint=_crosses(afr_columns, x_now, x_new),
            phi_now=phi_now,
            phi_proposed=phi_new,
            phi_change=None if phi_now is None or phi_new is None else phi_new - phi_now,
            phi_reason=reason_now or reason_new,
            timing_col_now=t_now.position,
            timing_col_proposed=t_new.position,
            crosses_timing_breakpoint=_crosses(timing_axis, x_now, x_new),
        )
    return pd.DataFrame(rows, columns=columns)


def _span(values: pd.Series) -> list[float] | None:
    finite = pd.to_numeric(values, errors="coerce").dropna()
    return None if finite.empty else [float(finite.min()), float(finite.max())]


def per_cell(predictions: pd.DataFrame, fit: TableFit) -> list[dict[str, Any]]:
    """For each proposed cell whose rounded value changes: the samples it
    touches and what moves there."""
    out: list[dict[str, Any]] = []
    for cell in fit.cells:
        if cell.proposed_raw is None or cell.proposed_raw == cell.current_raw:
            continue
        key = f"{cell.row}:{cell.col}"
        hit = [key in str(cells).split(",") for cells in predictions["cells"]]
        mine = predictions[np.asarray(hit, dtype=bool)] if len(predictions) else predictions
        placed = mine[mine["bfs_kind"] != "none"] if len(mine) else mine
        out.append(
            {
                "cell": [cell.row, cell.col],
                "current_raw": cell.current_raw,
                "proposed_raw": cell.proposed_raw,
                "samples_touched": int(len(mine)),
                "bfs_measured": int((mine["bfs_kind"] == "measured").sum()) if len(mine) else 0,
                "bfs_modelled": int((mine["bfs_kind"] == "modelled").sum()) if len(mine) else 0,
                "bfs_not_predicted": int((mine["bfs_kind"] == "none").sum()) if len(mine) else 0,
                "bfs_ratio": _span(mine["bfs_ratio"]) if len(mine) else None,
                "x_shift": _span(placed["x_shift"]) if len(placed) else None,
                "crosses_target_afr_breakpoint": int(
                    placed["crosses_target_afr_breakpoint"].astype(bool).sum()
                )
                if len(placed)
                else 0,
                "phi_change": _span(placed["phi_change"]) if len(placed) else None,
                "crosses_timing_breakpoint": int(
                    placed["crosses_timing_breakpoint"].astype(bool).sum()
                )
                if len(placed)
                else 0,
            }
        )
    return out


def knock_on(
    prepared: pd.DataFrame,
    fit: TableFit,
    tables: KnockOnTables,
    declaration: FuelCompDeclaration,
) -> dict[str, Any]:
    """The whole knock-on block: the BFS model, per-sample predictions and
    the per-cell table, with what an empty table means."""
    model = bfs_model(prepared, declaration)
    predictions = predict_samples(prepared, fit, tables, model, declaration)
    cells = per_cell(predictions, fit)
    changed = sum(
        1 for c in fit.cells if c.proposed_raw is not None and c.proposed_raw != c.current_raw
    )
    if not changed:
        empty: str | None = (
            "no proposed cell changes its rounded value"
            if fit.status_counts()["proposed"]
            else "no cell was proposed"
        )
    elif predictions.empty:
        empty = "no usable sample touches a changed cell"
    else:
        empty = None
    return {
        "knock_on_version": KNOCK_ON_VERSION,
        "configuration": (
            "did_route_included" if fit.include_unvalidated else "did_route_excluded"
        ),
        "bfs_model": model.as_dict(),
        "tables": tables.provenance(),
        "pedal_flag_assumed": declaration.knock_on.pedal_flag_assumed,
        "limitations": list(declaration.knock_on.limitations),
        "empty": empty,
        "cells": cells,
        "predictions": predictions,
    }


def summarise_predictions(predictions: pd.DataFrame) -> Mapping[str, Any]:
    """Counts over every predicted sample, for the report header."""
    if predictions.empty:
        return {"samples": 0}
    placed = predictions[predictions["bfs_kind"] != "none"]
    return {
        "samples": int(len(predictions)),
        "bfs_measured": int((predictions["bfs_kind"] == "measured").sum()),
        "bfs_modelled": int((predictions["bfs_kind"] == "modelled").sum()),
        "bfs_not_predicted": int((predictions["bfs_kind"] == "none").sum()),
        "crossing_target_afr_breakpoint": int(
            placed["crosses_target_afr_breakpoint"].astype(bool).sum()
        ),
        "crossing_timing_breakpoint": int(placed["crosses_timing_breakpoint"].astype(bool).sum()),
    }


__all__ = [
    "KNOCK_ON_VERSION",
    "BfsModel",
    "KnockOnRefusalError",
    "KnockOnTables",
    "bfs_model",
    "knock_on",
    "knock_on_tables",
    "per_cell",
    "predict_samples",
    "summarise_predictions",
]
