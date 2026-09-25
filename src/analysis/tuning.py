"""Tuning analysis with the methodology gates enforced in code.

The 2026-08 campaign retracted three findings in three days, each because an
ad-hoc script re-derived analysis without a gate the project already knew
about. Those gates lived in prose (``docs/roadmap/engine-tuning-support.md``),
so every new script had to remember them, and none did:

* **Closed-loop dilution.** In closed loop the ECU corrects to target
  regardless of what fuelling was changed, so pooling those samples pushes any
  measured effect toward zero *by construction*. A K step of +0.95 % read as
  "unchanged" because 92 % of the samples were at stoich.
* **Band-composition bias.** Delivery error varies with rpm, so pooling across
  two datasets with different rpm coverage compares coverage, not fuelling.
  This alone manufactured a "K responds 1:1" result.
* **DFCO masquerading as enrichment.** ``FUEL_STATUS == 4`` means "open loop",
  which covers *both* power enrichment and decel fuel cut. A bare ``fs == 4``
  filter over the July corpus is 88 % overrun.
* **Gridding on load.** ``CAL/LD`` is derived from BFS, so conditioning on it
  while fuelling moves partly conditions on the thing that moved — and it is
  not even stable at fixed fuel (it shifted 1.43 points at identical airflow
  across a same-tune null run).

Every function here refuses the unsafe form rather than trusting the caller to
remember, and reports which gate dropped how many samples so a diluted result
is visible instead of silent.

See ``docs/roadmap/archive/tuning-analysis-toolkit.md`` Phase 2.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Literal, NoReturn

import numpy as np
import pandas as pd

from src.analysis.afr import AfrCalibration
from src.analysis.channels import read_aligned, read_channels
from src.analysis.lambda_delivery import residual_pct
from src.analysis.noise_floor import FloorRefusal, NoiseFloor
from src.analysis.tune_revisions import TuneRevisionRegistry, resolve_session_revision
from src.control.capture_manifest import read_capture_manifest
from src.decode.surface_eval import bin_indices
from src.paths import BINDINGS_YAML, DIDS_YAML

#: Axes that must never grid a cross-flash comparison. ``CAL/LD`` is computed
#: from BFS, so it moves when fuelling moves; see the module docstring.
FORBIDDEN_AXES: frozenset[str] = frozenset(
    {"ENGINE_LOAD", "CAL/LD", "CAL/LD VALUE", "CALCULATED_LOAD", "ABSOLUTE_LOAD", "LOAD"}
)

# rpm band edges for band-averaging are a vehicle fact (the top edge is the
# engine's redline): every caller passes ``rpm_bands`` from the profile's
# ``tuning_providers.yaml`` ``mode01_wot`` (R05.13). There is no default.

#: `FUEL_STATUS` bit for "open loop". Covers enrichment *and* decel fuel cut,
#: which is why it is never used alone.
OPEN_LOOP = 4.0


class GateError(ValueError):
    """A caller asked for an analysis a gate refuses to perform."""


class InputsNotCapturedError(GateError):
    """The session never held what the analysis needs — not a gate verdict.

    Structured so a caller can tell the kinds apart without parsing the
    message (R05.11): ``kind`` is ``"not_captured"`` when named inputs are
    empty in the session, ``"not_co_observed"`` when every input is present
    but no two overlapped in time; ``missing`` names the empty inputs.
    """

    def __init__(
        self,
        message: str,
        *,
        kind: Literal["not_captured", "not_co_observed"],
        missing: tuple[str, ...] = (),
    ) -> None:
        super().__init__(message)
        self.kind = kind
        self.missing = missing


@dataclass
class GateReport:
    """How many samples each gate removed, and why."""

    started: int = 0
    filtered: dict[str, int] = field(default_factory=dict)
    remaining: int = 0

    def drop(self, gate: str, before: int, after: int) -> None:
        if before != after:
            self.filtered[gate] = self.filtered.get(gate, 0) + (before - after)

    def merge(self, other: GateReport) -> None:
        """Fold another report in — for pooling sessions gated separately."""
        self.started += other.started
        self.remaining += other.remaining
        for gate, count in other.filtered.items():
            self.filtered[gate] = self.filtered.get(gate, 0) + count

    def as_dict(self) -> dict[str, Any]:
        return {
            "started": self.started,
            "filtered": dict(self.filtered),
            "remaining": self.remaining,
        }


@dataclass
class DeliveryError:
    """Measured-vs-commanded AFR, band-averaged, with its gate report."""

    bands: pd.DataFrame
    band_averaged_pct: float
    pooled_pct: float
    n: int
    gates: dict[str, Any]
    rpm_window: tuple[float, float]
    measured_spec: str
    #: Calibration in force when the session was recorded, when a registry
    #: resolves it. Comparing delivery error across a flash boundary without
    #: this is comparing two different engines.
    tune_revision: str | None = None
    tune_revision_source: str = "unknown"

    def as_dict(self) -> dict[str, Any]:
        return {
            "tune_revision": self.tune_revision,
            "tune_revision_source": self.tune_revision_source,
            "band_averaged_pct": self.band_averaged_pct,
            "pooled_pct": self.pooled_pct,
            "n": self.n,
            "rpm_window": list(self.rpm_window),
            "measured_spec": self.measured_spec,
            "gates": self.gates,
            "bands": self.bands.to_dict(orient="records"),
        }


def _assert_axis_allowed(axis: str, *, unsafe: bool) -> None:
    bare = axis.partition(":")[2] or axis
    if (
        bare.upper().replace("_", "/") in {a.replace("_", "/") for a in FORBIDDEN_AXES}
        and not unsafe
    ):
        raise GateError(
            f"{axis!r} is not a valid comparison axis: calculated load is derived from BFS, "
            "so conditioning on it while fuelling moves partly conditions on what moved. "
            "It is not stable at fixed fuel either — it shifted 1.43 points at identical "
            "airflow across the 2026-08-04 same-tune null run. Grid on rpm x MAF (real "
            "airflow) or rpm x BFS (the actual table cell) instead, or pass "
            "unsafe_axes=True if you have a reason."
        )


def _refuse_missing_inputs(
    session: Path | pd.DataFrame,
    specs: list[str],
    *,
    bindings_path: Path,
    dids_path: Path,
    calibration: AfrCalibration | None = None,
    afr_stoich: float,
    mode01_exclusion_note: str | None = None,
) -> NoReturn:
    """Say *why* there is nothing to analyse instead of reporting zero bands.

    An empty aligned frame has two causes that demand opposite responses, and
    reporting ``started: 0`` for both made them indistinguishable:

    * the gates filtered everything — a real answer about the drive, meaning
      go and capture the regime you care about;
    * the inputs were never captured — not an answer at all, and no amount of
      driving will change it.

    Measured 2026-08-06: a clean 25-minute all-DID capture, 18k samples per
    channel at 14.93 Hz, returned ``started: 0``. That reads as "the gates
    rejected everything". In fact the ``tuning-did`` profile disables Mode 01
    (on that vehicle Mode 01 and Mode 22 are mutually exclusive), and
    commanded AFR existed only as a Mode 01 PID. The question was unanswerable
    before the truck moved.

    Raises :class:`InputsNotCapturedError`. Why a vehicle's all-DID captures
    lack Mode 01 is a vehicle fact, so it is ``mode01_exclusion_note`` from
    the profile's ``tuning_providers.yaml``, never a sentence in shared code.
    """
    missing: list[str] = []
    unresolvable: list[str] = []
    for spec in specs:
        try:
            rows = read_channels(
                session,
                [spec],
                bindings_path=bindings_path,
                dids_path=dids_path,
                calibration=calibration,
                afr_stoich=afr_stoich,
            )
        except Exception as exc:  # unresolvable spec is itself a missing input
            missing.append(spec)
            unresolvable.append(f"{spec} ({type(exc).__name__}: {exc})")
            continue
        if rows.empty:
            missing.append(spec)
            unresolvable.append(spec)

    detail = ""
    if isinstance(session, Path):
        manifest = read_capture_manifest(session)
        if manifest is not None and manifest.profile_snapshot.get("mode01_enabled") is False:
            detail = (
                f" This session was captured with profile {manifest.profile!r}, which "
                "disables Mode 01, so its Mode 01 channels are absent by construction, "
                "not by chance."
            )
            if mode01_exclusion_note:
                detail += f" {mode01_exclusion_note.strip()}"

    raise InputsNotCapturedError(
        "this session cannot answer delivery error — its inputs were never "
        "captured, which is not the same as the gates rejecting every sample. "
        + (
            "Empty in this session: " + ", ".join(unresolvable)
            if missing
            else "No two required channels overlapped in time."
        )
        + detail
        + " Use a Mode 01 capture for commanded-vs-measured fuelling, or feed a"
        " semantic frame to delivery_error_from_semantic_frame() once a Mode 22"
        " provider for commanded AFR exists.",
        kind="not_captured" if missing else "not_co_observed",
        missing=tuple(missing),
    )


def _refuse_non_mixture_measurement(
    session: Path | pd.DataFrame, measured_spec: str, *, dids_path: Path
) -> None:
    """Refuse a measured channel that is not a mixture reading.

    This function compares the measured channel against a commanded AFR
    as-is, so it must BE lambda or AFR. The A/F sensor's raw voltage is not:
    on 2026-09-21 `did:ECM_AF_SENSOR_V_B1` was passed here and ~1.5 V was
    compared against a 12.6 AFR target, returning a confident -87 % instead
    of an error. Voltage needs the calibrated transfer, which is what the
    semantic path does (`build_tuning_frame` with a `voltage_afr` provider,
    then :func:`delivery_error_from_semantic_frame`).
    """
    from src.analysis.channels import parse_spec

    spec = parse_spec(measured_spec)
    if spec.source != "did":
        return
    from src.decode import dids as dids_decode

    entries = {e.name: e for e in dids_decode.load_did_map(dids_path).dids}
    entry = entries.get(spec.name)
    if entry is not None and entry.unit in ("V", "mV"):
        raise GateError(
            f"{measured_spec} is a sensor voltage ({entry.unit}), not a mixture reading; "
            "delivery_error compares it against commanded AFR as-is. Use the semantic "
            "path, which applies the calibrated voltage-to-AFR transfer: build_tuning_frame "
            "with a voltage_afr provider, then delivery_error_from_semantic_frame."
        )


#: Raw PID 0x44. Read raw rather than as the derived ``obd:COMMANDED_AFR``
#: so a fuel-cut sample (ratio 0.0) survives the read and can be *counted* by
#: the ``fuel_cut`` gate, instead of vanishing inside the alignment.
EQUIV_RATIO_SPEC = "obd:COMMANDED_EQUIV_RATIO"


def delivery_semantic_frame(
    session: Path | pd.DataFrame,
    *,
    calibration: AfrCalibration,
    measured_spec: str = "obd:O2_WR_LAMBDA_S1",
    measured_is_lambda: bool = True,
    afr_stoich: float,
    min_load: float | None,
    grid: str = "1s",
    cell_agg: str = "last",
    bindings_path: Path = BINDINGS_YAML,
    dids_path: Path = DIDS_YAML,
    mode01_exclusion_note: str | None = None,
) -> pd.DataFrame:
    """The Mode-01 delivery inputs of one session as semantic roles.

    What :func:`delivery_error` feeds :func:`delivery_error_from_semantic_frame`,
    exposed so several sessions can be pooled into one band average (the
    workbench's Analysis page) with the same gates. Raises :class:`GateError`
    naming the absent channels when the inputs were never captured.

    ``fuel_cut`` marks cells whose commanded equivalence ratio is zero: decel
    fuel cut, where the ECU commands no target at all. Those rows carry no
    commanded AFR, and the semantic gate removes them *by name* — converting
    them used to raise ``equivalence ratio must be positive, got 0.0`` and
    cost four sessions on 2026-09-21.
    """
    specs = ["obd:RPM", "obd:FUEL_STATUS", EQUIV_RATIO_SPEC, measured_spec]
    _refuse_non_mixture_measurement(session, measured_spec, dids_path=dids_path)
    if min_load is not None:
        specs.append("obd:ENGINE_LOAD")

    frame = read_aligned(
        session,
        specs,
        grid=grid,
        require_all=True,
        cell_agg=cell_agg,  # type: ignore[arg-type]
        calibration=calibration,
        afr_stoich=afr_stoich,
        bindings_path=bindings_path,
        dids_path=dids_path,
    )
    if frame.empty:
        _refuse_missing_inputs(
            session,
            specs,
            bindings_path=bindings_path,
            dids_path=dids_path,
            calibration=calibration,
            afr_stoich=afr_stoich,
            mode01_exclusion_note=mode01_exclusion_note,
        )
    measured = frame[measured_spec].astype(float)
    ratio = frame[EQUIV_RATIO_SPEC].astype(float)
    semantic = pd.DataFrame(
        {
            "timestamp": frame["timestamp"],
            "rpm": frame["obd:RPM"],
            "measured_afr": measured * afr_stoich if measured_is_lambda else measured,
            "commanded_afr": calibration.commanded_afr_series(ratio, afr_stoich=afr_stoich),
            "fueling_active": frame["obd:FUEL_STATUS"].astype(float).eq(OPEN_LOOP),
            "fuel_cut": ratio.le(0.0),
        }
    )
    if min_load is not None:
        semantic["load"] = frame["obd:ENGINE_LOAD"]
    return semantic


def delivery_error(
    session: Path | pd.DataFrame,
    *,
    calibration: AfrCalibration,
    measured_spec: str = "obd:O2_WR_LAMBDA_S1",
    measured_is_lambda: bool = True,
    afr_stoich: float,
    rpm_bands: tuple[float, ...],
    min_load: float | None,
    grid: str = "1s",
    cell_agg: str = "last",
    revisions: TuneRevisionRegistry | None = None,
    bindings_path: Path = BINDINGS_YAML,
    dids_path: Path = DIDS_YAML,
    mode01_exclusion_note: str | None = None,
) -> DeliveryError:
    """Delivery error at enrichment, with every gate applied.

    ``measured_spec`` is a parameter on purpose. The ECM's own λ (PID 0x24)
    and the A/F sensor voltage path disagree by ~3.8 % at WOT (2026-08-05,
    commit 78592f4) and nobody knows which is right, so this function must not
    quietly pick a side — the caller names the source and the answer carries it.

    Gates, in order:

    1. same-instant pairing (nothing stale-joined; :func:`read_aligned`)
    2. open loop **and** an enrichment target strictly richer than stoich, so
       decel fuel cut is excluded by construction rather than by threshold
    3. optional load floor, for WOT-only questions
    4. band-averaged over rpm, so band composition cannot masquerade as an effect
    """
    semantic = delivery_semantic_frame(
        session,
        calibration=calibration,
        measured_spec=measured_spec,
        measured_is_lambda=measured_is_lambda,
        afr_stoich=afr_stoich,
        min_load=min_load,
        grid=grid,
        cell_agg=cell_agg,
        bindings_path=bindings_path,
        dids_path=dids_path,
        mode01_exclusion_note=mode01_exclusion_note,
    )

    revision = None
    revision_source = "unknown"
    if isinstance(session, Path):
        revision, revision_source, _conflict = resolve_session_revision(session, revisions)
    return delivery_error_from_semantic_frame(
        semantic,
        measured_spec=measured_spec,
        afr_stoich=afr_stoich,
        rpm_bands=rpm_bands,
        min_load=min_load,
        grid=grid,
        fueling_gate_name="closed_loop",
        tune_revision=revision,
        tune_revision_source=revision_source,
    )


def delivery_error_from_semantic_frame(
    frame: pd.DataFrame,
    *,
    measured_spec: str,
    afr_stoich: float,
    rpm_bands: tuple[float, ...],
    min_load: float | None = None,
    grid: str = "1s",
    fueling_gate_name: str = "fueling_inactive",
    tune_revision: str | None = None,
    tune_revision_source: str = "semantic_frame",
) -> DeliveryError:
    """Apply delivery gates to semantic roles instead of transport names.

    Required columns are ``timestamp``, ``rpm``, ``measured_afr``,
    ``commanded_afr``, and boolean ``fueling_active``.  An optional boolean
    ``fuel_cut`` column removes decel-fuel-cut cells (no commanded target) as
    the named ``fuel_cut`` gate.  An optional
    ``measured_afr_censored`` column removes transfer-boundary samples before
    they can become a false mixture error.  ``load`` is optional and may only
    be used when an independently validated load provider exists.

    This is the all-DID adapter boundary: callers resolve/transform roles
    before this function, so an A/F voltage can never be mislabeled as AFR by
    a transport-specific shortcut.
    """
    values, report = gate_delivery_samples(
        frame,
        afr_stoich=afr_stoich,
        rpm_bands=rpm_bands,
        min_load=min_load,
        grid=grid,
        fueling_gate_name=fueling_gate_name,
    )
    return delivery_error_from_gated(
        values,
        report,
        measured_spec=measured_spec,
        rpm_bands=rpm_bands,
        tune_revision=tune_revision,
        tune_revision_source=tune_revision_source,
    )


def gate_delivery_samples(
    frame: pd.DataFrame,
    *,
    afr_stoich: float,
    rpm_bands: tuple[float, ...],
    min_load: float | None = None,
    grid: str = "1s",
    fueling_gate_name: str = "fueling_inactive",
) -> tuple[pd.DataFrame, GateReport]:
    """The gate half of :func:`delivery_error_from_semantic_frame`.

    Returns the surviving samples and the report of what each gate removed.
    Separate so several sessions can be gated *each on its own clock* and then
    band-averaged once (:func:`delivery_error_from_gated`): concatenating raw
    frames first would let the grid grouping merge two sessions' samples that
    happen to share a wall-clock cell.
    """
    required = {"timestamp", "rpm", "measured_afr", "commanded_afr", "fueling_active"}
    missing = sorted(required - set(frame.columns))
    if missing:
        raise GateError("semantic delivery frame is missing roles: " + ", ".join(missing))
    if min_load is not None and "load" not in frame:
        raise GateError(
            "delivery load gate requested but no validated semantic load provider exists"
        )

    values = frame.copy()
    values["timestamp"] = pd.to_datetime(values["timestamp"], utc=True)
    # A row exists only when every required role was independently observed in
    # the same grid cell. Grouping does not forward-fill across cells.
    columns = ["rpm", "measured_afr", "commanded_afr", "fueling_active"]
    if min_load is not None:
        columns.append("load")
    if "measured_afr_censored" in values:
        columns.append("measured_afr_censored")
    # A fuel-cut cell has no commanded target by definition, so with an
    # explicit `fuel_cut` role it is kept long enough to be counted and
    # removed by name rather than disappearing as a missing value.
    has_cut = "fuel_cut" in values
    if has_cut:
        columns.append("fuel_cut")
    observed = ["rpm", "measured_afr", "fueling_active"]
    if not has_cut:
        observed.append("commanded_afr")
    values = (
        values.loc[:, ["timestamp", *columns]]
        .sort_values("timestamp", kind="stable")
        .groupby(pd.Grouper(key="timestamp", freq=grid), sort=True)
        .last()
        .dropna(subset=observed)
        .reset_index()
    )
    report = GateReport(started=len(values))

    if has_cut:
        before = len(values)
        values = values[~values["fuel_cut"].fillna(False).astype(bool)]
        report.drop("fuel_cut", before, len(values))
        before = len(values)
        values = values.dropna(subset=["commanded_afr"])
        report.drop("no_commanded_target", before, len(values))

    before = len(values)
    values = values[values["fueling_active"].astype(bool)]
    report.drop(fueling_gate_name, before, len(values))

    # Enrichment, not overrun. A semantic fueling-active role is deliberately
    # separate from the mixture target: a future DID decoder can identify fuel
    # cut without carrying Mode-01's overloaded `FUEL_STATUS == 4` convention.
    before = len(values)
    values = values[values["commanded_afr"].astype(float) < afr_stoich]
    report.drop("not_enrichment", before, len(values))

    if min_load is not None:
        before = len(values)
        values = values[values["load"].astype(float) > min_load]
        report.drop("below_min_load", before, len(values))

    if "measured_afr_censored" in values:
        before = len(values)
        values = values[~values["measured_afr_censored"].astype(bool)]
        report.drop("censored_measured_afr", before, len(values))

    before = len(values)
    values = values[(values["rpm"] >= rpm_bands[0]) & (values["rpm"] <= rpm_bands[-1])]
    report.drop("outside_rpm_bands", before, len(values))
    report.remaining = len(values)
    return values, report


def delivery_error_from_gated(
    values: pd.DataFrame,
    report: GateReport,
    *,
    measured_spec: str,
    rpm_bands: tuple[float, ...],
    tune_revision: str | None = None,
    tune_revision_source: str = "semantic_frame",
) -> DeliveryError:
    """Band-average already-gated samples (one session or a pool of them)."""
    if values.empty:
        return DeliveryError(
            bands=pd.DataFrame(columns=["rpm_band", "n", "commanded", "measured", "error_pct"]),
            band_averaged_pct=float("nan"),
            pooled_pct=float("nan"),
            n=0,
            gates=report.as_dict(),
            rpm_window=(float("nan"), float("nan")),
            measured_spec=measured_spec,
            tune_revision=tune_revision,
            tune_revision_source=tune_revision_source,
        )

    # The one delivery residual (B06.4): the same definition, sign and
    # refusal of phi as the lambda-delivery relationship, so analysis and any
    # serving path cannot drift apart. Positive = leaner than commanded.
    values = values.assign(
        error_pct=residual_pct(
            values["measured_afr"].astype(float),
            values["commanded_afr"].astype(float),
            quantity="afr",
        )
    )
    binned = values.groupby(pd.cut(values["rpm"], list(rpm_bands)), observed=True)
    bands = binned.agg(
        n=("error_pct", "size"),
        commanded=("commanded_afr", "mean"),
        measured=("measured_afr", "mean"),
        error_pct=("error_pct", "mean"),
    ).reset_index(names="rpm_band")
    bands["rpm_band"] = bands["rpm_band"].astype(str)

    return DeliveryError(
        bands=bands,
        band_averaged_pct=float(bands["error_pct"].mean()),
        pooled_pct=float(values["error_pct"].mean()),
        n=int(len(values)),
        gates=report.as_dict(),
        rpm_window=(float(values["rpm"].min()), float(values["rpm"].max())),
        measured_spec=measured_spec,
        tune_revision=tune_revision,
        tune_revision_source=tune_revision_source,
    )


@dataclass
class SessionComparison:
    """Per-cell deltas between two sessions on a matched grid."""

    cells: pd.DataFrame
    mean_delta: float
    n_cells: int
    axes: tuple[str, ...]
    value: str
    #: Calibrations in force for each side. Equal revisions mean this is a
    #: null run (the spread IS the noise floor); different ones mean the
    #: delta includes whatever the flash changed.
    tune_revisions: tuple[str | None, str | None] = (None, None)
    tune_revision_sources: tuple[str, str] = ("unknown", "unknown")
    tune_revision_conflicts: tuple[bool, bool] = (False, False)
    #: Regime the quoted floor was measured in. A floor is only valid for its
    #: own conditions — the same null pair gives 0.48 deg of spark spread at
    #: WOT and 7.7 deg across all conditions — so this travels with the sigma
    #: figures rather than being assumed to match.
    floor_conditions: str | None = None
    #: The regime the caller declared this comparison was made under.
    comparison_conditions: str | None = None
    #: Why the cells carry no sigma, when a floor was asked for and refused
    #: (:class:`~src.analysis.noise_floor.FloorRefusal`), else ``None``.
    floor_refusal: dict[str, Any] | None = None

    def as_dict(self) -> dict[str, Any]:
        return {
            "tune_revisions": list(self.tune_revisions),
            "tune_revision_sources": list(self.tune_revision_sources),
            "tune_revision_conflicts": list(self.tune_revision_conflicts),
            "floor_conditions": self.floor_conditions,
            "comparison_conditions": self.comparison_conditions,
            "floor_refusal": self.floor_refusal,
            "same_revision": (
                self.tune_revisions[0] == self.tune_revisions[1]
                if self.tune_revisions[0] is not None and self.tune_revisions[1] is not None
                else None
            ),
            "value": self.value,
            "axes": list(self.axes),
            "mean_delta": self.mean_delta,
            "n_cells": self.n_cells,
            "cells": self.cells.to_dict(orient="records"),
        }


def _cell_labels(values: pd.Series, edges: Sequence[float]) -> pd.Series:
    """Each value's cell under the one bin rule (:func:`src.decode.surface_eval.bin_indices`).

    Labels read ``[lo, hi)`` (the last cell ``[lo, hi]``). Values outside the
    edges get no cell. R05.10: this used right-closed ``pd.cut``, under which
    the minimum of auto-computed ``linspace`` edges fell in no cell and every
    sample at the lowest operating point was silently dropped.
    """
    edge_array = np.asarray(edges, dtype=float)
    numeric = pd.to_numeric(values, errors="coerce").to_numpy(dtype=float)
    inside = np.isfinite(numeric) & (numeric >= edge_array[0]) & (numeric <= edge_array[-1])
    index = bin_indices(np.where(inside, numeric, edge_array[0]), edge_array)
    last = len(edge_array) - 2
    names = [
        str(pd.Interval(edge_array[i], edge_array[i + 1], closed="both" if i == last else "left"))
        for i in range(last + 1)
    ]
    labels = [names[i] if ok else None for i, ok in zip(index, inside, strict=True)]
    return pd.Series(labels, index=values.index, dtype=object)


def compare_sessions(
    a: Path | pd.DataFrame,
    b: Path | pd.DataFrame,
    *,
    value: str,
    axes: tuple[str, ...] = ("obd:RPM", "obd:MAF"),
    bins: tuple[tuple[float, ...], ...] | None = None,
    min_cell_samples: int = 4,
    unsafe_axes: bool = False,
    grid: str = "1s",
    calibration: AfrCalibration | None = None,
    afr_stoich: float,
    bindings_path: Path = BINDINGS_YAML,
    dids_path: Path = DIDS_YAML,
    noise_floor: NoiseFloor | None = None,
    floor_channel: str | None = None,
    conditions: str | None = None,
    revisions: TuneRevisionRegistry | None = None,
) -> SessionComparison:
    """A-vs-B on a matched grid, refusing load as an axis.

    ``noise_floor`` (with ``floor_channel``) gates the deltas through
    :meth:`~src.analysis.noise_floor.NoiseFloor.significance`, which needs the
    regime this comparison was made under as ``conditions``. The floor carries
    the regime it was measured in, and that regime is part of the measurement
    — the same null pair gives 0.48 deg of spark spread at WOT and 7.7 deg
    across all conditions, so quoting one against the other understates noise
    by ~16x. A mismatch is refused and reported in ``floor_refusal``; without
    any floor the sigma columns read ``unavailable`` rather than an invented
    significance. (A bare float floor is no longer accepted: it has no regime.)
    """
    for axis in axes:
        _assert_axis_allowed(axis, unsafe=unsafe_axes)
    if isinstance(noise_floor, NoiseFloor) and floor_channel is None:
        raise GateError(
            "a NoiseFloor was given without floor_channel — name the channel whose "
            "spread should gate these deltas. Picking one silently would also pick "
            "the regime it was measured in, and a floor is only valid for its own."
        )
    if noise_floor is not None and conditions is None:
        raise GateError(
            "a NoiseFloor was given without conditions — state the regime this "
            "comparison was made under. The floor is refused unless it was measured "
            "under the same one (R05.2); a WOT floor quoted against idle cells "
            "understates noise ~16x."
        )

    specs = [value, *axes]
    read = dict(
        grid=grid,
        require_all=True,
        calibration=calibration,
        afr_stoich=afr_stoich,
        bindings_path=bindings_path,
        dids_path=dids_path,
    )
    frame_a = read_aligned(a, specs, **read)  # type: ignore[arg-type]
    frame_b = read_aligned(b, specs, **read)  # type: ignore[arg-type]

    for name, frame in (("a", frame_a), ("b", frame_b)):
        if frame.empty:
            raise GateError(
                f"session {name} has no rows where every one of {specs} is fresh in the "
                "same cell — either a channel is absent from that capture, or the two "
                "were never sampled together. Check that before widening the grid."
            )

    if bins is None:
        merged = pd.concat([frame_a, frame_b], ignore_index=True)
        edges: list[tuple[float, ...]] = []
        for axis in axes:
            lo, hi = float(merged[axis].min()), float(merged[axis].max())
            if not np.isfinite(lo) or not np.isfinite(hi) or hi <= lo:
                raise GateError(
                    f"axis {axis!r} has no usable spread across these sessions "
                    f"(min={lo}, max={hi}), so it cannot bin anything. Pick an axis that "
                    "varies, or pass explicit bins."
                )
            edges.append(tuple(np.linspace(lo, hi, 7)))
        bins = tuple(edges)

    def cell_frame(frame: pd.DataFrame) -> pd.DataFrame:
        keys = [
            _cell_labels(frame[axis], edges).rename(axis)
            for axis, edges in zip(axes, bins, strict=True)
        ]
        return frame.groupby(keys, observed=True).agg(n=(value, "size"), mean=(value, "mean"))

    cells_a, cells_b = cell_frame(frame_a), cell_frame(frame_b)
    joined = cells_a.join(cells_b, how="inner", lsuffix="_a", rsuffix="_b")
    joined = joined[(joined["n_a"] >= min_cell_samples) & (joined["n_b"] >= min_cell_samples)]
    joined = joined.assign(delta=joined["mean_b"] - joined["mean_a"])

    floor_conditions: str | None = None
    floor_refusal: dict[str, Any] | None = None
    joined["delta_sigma"] = np.nan
    joined["distinguishable"] = pd.NA
    if noise_floor is not None and floor_channel is not None and conditions is not None:
        entry = noise_floor.channels.get(floor_channel)
        floor_conditions = entry.conditions if entry is not None else None
        verdict = noise_floor.significance(floor_channel, joined["delta"], conditions=conditions)
        if isinstance(verdict, FloorRefusal):
            floor_refusal = verdict.as_dict()
        else:
            joined["delta_sigma"] = verdict.delta_sigma
            joined["distinguishable"] = verdict.distinguishable

    def _revision(session: Path | pd.DataFrame) -> tuple[str | None, str, bool]:
        if not isinstance(session, Path):
            return None, "unknown", False
        return resolve_session_revision(session, revisions)

    revision_a = _revision(a)
    revision_b = _revision(b)

    return SessionComparison(
        tune_revisions=(revision_a[0], revision_b[0]),
        tune_revision_sources=(revision_a[1], revision_b[1]),
        tune_revision_conflicts=(revision_a[2], revision_b[2]),
        floor_conditions=floor_conditions,
        comparison_conditions=conditions,
        floor_refusal=floor_refusal,
        cells=joined.reset_index(),
        mean_delta=float(joined["delta"].mean()) if len(joined) else float("nan"),
        n_cells=int(len(joined)),
        axes=tuple(axes),
        value=value,
    )


__all__ = [
    "FORBIDDEN_AXES",
    "DeliveryError",
    "GateError",
    "InputsNotCapturedError",
    "SessionComparison",
    "compare_sessions",
    "EQUIV_RATIO_SPEC",
    "GateReport",
    "delivery_error",
    "delivery_error_from_gated",
    "delivery_error_from_semantic_frame",
    "gate_delivery_samples",
    "delivery_semantic_frame",
]
