"""Operating-point designs: where the simulated engine runs (E08.2).

A :class:`SimDesign` is a frozen, hashed table of steady operating points,
each with a weight. Paired runs (:mod:`src.analysis.paired_scenarios`) take
their :class:`~src.analysis.paired_scenarios.OperatingPoint` s from it, and
every one carries ``source: design:<sha256>`` -- a design, not a recording, so
E07.6's ``_refuse_recorded_sources`` passes it unchanged and still refuses a
source that names a session.

Two kinds:

* **drive** (:func:`build_drive_design`): built from **pre-change** drives,
  every one recorded on the base ROM's revision (``tune_revisions.yaml``),
  else ``drive_not_on_base_revision``. Points are the fuel plant's own warm
  steady reads (:func:`src.analysis.fuel_plant_eval.build_samples`,
  ``state == "eligible"`` after the plant's manifold refusals), so
  eligibility is one definition shared with E07.3. Every read is counted
  under its plant state, and every eligible read is either placed or counted
  under why not (``throttle_unpaired``, ``throttle_closed_position_unestimated``,
  ``fill_byte_unpaired``, ``fill_byte:<N02 state:reason>``). Throttle is PID
  0x11 when co-observed, else DID 0x120F through N02's closed-position
  method; a read with no module voltage is placed at a stated voltage and
  labelled ``unmeasured`` (:func:`_battery_v`). The fill byte is
  N02's reconstructed interval
  (:func:`src.analysis.fuel_comp_samples.session_samples` through
  :func:`src.analysis.fuel_comp_restore.points_from_samples`), carried whole:
  never collapsed to a midpoint. The weight is dwell time, the interval to the
  next eligible read capped at the steady rule's window, never a read count.
* **grid** (:func:`build_grid_design`): one point per cell centre of a stated
  rpm x load grid in Fuel Comp's own axis units. Every environmental value is
  stated in the call; nothing defaults (``environment_unstated``). MAF is
  solved through the plant's own manifold model; a cell whose MAP it cannot
  reach is counted (``load_below_model_floor``), never placed at another MAP.

What it makes impossible rather than documented:

* the hash is computed over the canonical content (:meth:`SimDesign.sha256`),
  never supplied, and :func:`load_design` refuses a directory whose content
  does not hash to its name;
* a design is written only outside the checkout (``inside_checkout``, the
  rule :func:`src.analysis.rom_effects.write_candidate` keeps);
* a run on another base image is refused (:func:`require_base`,
  ``design_revision_mismatch``);
* an operating point at a fill byte outside the point's interval is refused
  (``fill_byte_outside_interval``).

Nothing here talks to the ECU: no ``src.control``, daemon, dashboard, MCP,
``can`` or ``serial`` import (a clean-process test asserts it).
"""

from __future__ import annotations

import hashlib
import json
import math
from collections import Counter
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any, Literal

import numpy as np
import pandas as pd
import pyarrow as pa
import pyarrow.parquet as pq
from pydantic import BaseModel, ConfigDict, Field, model_validator

from src.analysis.channels import ChannelSpecError, read_channels
from src.analysis.fuel_comp_inputs import (
    ClosedPositionEstimate,
    FuelCompDeclaration,
    did_raw_counts,
    estimate_closed_position,
    load_catalog,
    load_fuel_comp_declaration,
    throttle_counts_did,
)
from src.analysis.fuel_comp_restore import points_from_samples
from src.analysis.fuel_comp_rom import (
    FuelCompCalibration,
    RevisionImageResolver,
    rom_dirs_from_env,
)
from src.analysis.fuel_comp_samples import SAMPLES_VERSION, session_samples
from src.analysis.fuel_plant import (
    B06Rules,
    FuelPlantProfile,
    FuelPlantRefusalError,
    PlantInputs,
    ResolvedParameters,
    load_b06,
    load_fuel_plant,
    predict,
    resolve_parameters,
)
from src.analysis.fuel_plant_eval import (
    apply_manifold_refusals,
    build_samples,
    check_origin,
    plant_inputs,
)
from src.analysis.lambda_delivery import load_lambda_delivery
from src.analysis.paired_scenarios import OperatingPoint
from src.analysis.rom_map import RomMap, load_rom_map
from src.atomic_write import atomic_write_text
from src.paths import BINDINGS_YAML, DIDS_YAML, REPO_ROOT
from src.signals.afr import load_afr_calibration
from src.signals.derived import FUEL_PROPERTIES
from src.signals.pairing import PairingPolicy, match_ordered, to_seconds
from src.vehicle.tune_revisions import (
    TuneRevision,
    TuneRevisionRegistry,
    load_tune_revisions,
    session_dated_at,
)

#: Bump when what a design means changes.
SCHEMA_VERSION = 1
#: The prefix every point's ``source`` carries: a design, never a recording.
SOURCE_PREFIX = "design:"
DESIGN_JSON = "design.json"
DESIGN_PARQUET = "design.parquet"
#: The plant state of a read that is a candidate point.
ELIGIBLE = "eligible"
#: Placement of an eligible read that became a point.
PLACED = "placed"
THROTTLE_UNPAIRED = "throttle_unpaired"
#: The DID route exists but N02's closed-position method gave no estimate.
THROTTLE_CLOSED_UNESTIMATED = "throttle_closed_position_unestimated"
FILL_UNPAIRED = "fill_byte_unpaired"
MAP_UNRESOLVED = "map_unresolved"
#: A grid cell whose target MAP the manifold model cannot reach (fuel-mv-3's
#: residual-gas floor: the manifold never empties below it).
LOAD_BELOW_MODEL_FLOOR = "load_below_model_floor"
#: A grid cell's solved MAP must equal its target to this relative tolerance;
#: anything further is unreachable, never placed at the wrong MAP.
GRID_MAP_REL_TOLERANCE = 1e-9
#: Where each point's throttle_pct comes from (PID 0x11 when co-observed).
THROTTLE_PID_SOURCE = "pid:0x11"
THROTTLE_DID_SOURCE = "did:0x120F"
#: The pedal-enrichment flag is not logged: stated, and said so.
PEDAL_NOT_LOGGED = "stated_zero:not_logged"
#: The only way a grid's fill-% load becomes airflow, stated by the caller.
LoadBasis = Literal["fill_fraction_as_manifold_ratio"]
_LOAD_BASIS_MEANING = {
    "fill_fraction_as_manifold_ratio": (
        "grid load: the throttle-model fill fraction (Fuel Comp's load axis, % of the "
        "model's ceiling) is taken as MAP / baro, and MAF is the airflow at which the "
        "plant's manifold model gives that MAP. The fill byte has no absolute unit in "
        "code (rom_map.yaml fuel_comp_flow_axis); this mapping is stated, not decoded"
    ),
}
#: Bisection steps solving MAF for a stated MAP (2^-100 of the bracket).
_MAF_BISECTION_STEPS = 100
#: Doublings allowed while bracketing that MAF from 1 g/s.
_MAF_BRACKET_DOUBLINGS = 64

Regime = Literal["closed_loop", "enrichment", "lean_target", "unknown"]


class SimDesignRefusalError(ValueError):
    """A design cannot be built, written, loaded or used; ``reason`` names why."""

    def __init__(self, reason: str, message: str) -> None:
        super().__init__(f"{reason}: {message}")
        self.reason = reason


class _Strict(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class DesignPoint(_Strict):
    """One steady operating point. Values only; no recording is named as a source.

    ``session`` and ``t`` say which read a drive point was taken from (design
    provenance, never an ``OperatingPoint.source``); a grid point has its
    ``cell_row``/``cell_col`` instead."""

    index: int = Field(ge=0)
    session: str | None = None
    t: float | None = None
    cell_row: int | None = None
    cell_col: int | None = None
    rpm: float
    maf_gps: float
    maf_source: str = Field(min_length=1)
    baro_kpa: float
    iat_c: float
    coolant_c: float
    battery_v: float
    #: ``measured``: the module voltage within the plant's battery_max_age_s;
    #: ``unmeasured``: none was, and ``battery_v`` is the design's stated
    #: ``battery_v_when_unmeasured`` (only the absolute pulse depends on it);
    #: ``stated``: a grid's.
    battery_v_source: Literal["measured", "unmeasured", "stated"]
    rail_kpa: float
    rail_kpa_source: Literal["measured", "working_assumption", "stated"]
    map_kpa: float
    map_kpa_source: str = Field(min_length=1)
    #: A contract boundary only: nothing in the paired slice computes from it.
    #: The DID route's counts interval maps to ``[throttle_pct_lo, _hi]`` and
    #: ``throttle_pct`` is its centre; a PID read is one value.
    throttle_pct: float
    throttle_pct_lo: float
    throttle_pct_hi: float
    throttle_pct_source: str = Field(min_length=1)
    pedal_flag: int
    pedal_flag_source: str = Field(min_length=1)
    #: Fuel Comp's load byte ``0xFFFF118A`` as a whole-count interval.
    fill_byte_lo: int = Field(ge=0, le=255)
    fill_byte_hi: int = Field(ge=0, le=255)
    fill_byte_source: str = Field(min_length=1)
    #: Dwell seconds (drive) or 1 per cell (grid): :attr:`SimDesign.weight_basis`.
    weight: float = Field(gt=0)
    regime: Regime

    @model_validator(mode="after")
    def _finite(self) -> DesignPoint:
        numbers = {
            k: getattr(self, k)
            for k in (
                "rpm",
                "maf_gps",
                "baro_kpa",
                "iat_c",
                "coolant_c",
                "battery_v",
                "rail_kpa",
                "map_kpa",
                "throttle_pct",
                "throttle_pct_lo",
                "throttle_pct_hi",
                "weight",
            )
        }
        bad = sorted(k for k, v in numbers.items() if not math.isfinite(v))
        if bad:
            raise ValueError(f"non-finite point values {bad}")
        if self.fill_byte_lo > self.fill_byte_hi:
            raise ValueError("fill_byte_lo exceeds fill_byte_hi")
        if not self.throttle_pct_lo <= self.throttle_pct <= self.throttle_pct_hi:
            raise ValueError("throttle_pct lies outside its interval")
        return self


class SessionCheck(_Strict):
    """One drive's revision check and its counts."""

    session: str
    #: When its data starts (``tune_revisions.session_dated_at``).
    dated_at: datetime
    revision_at_start: str
    #: Every revision in force at one of its pulse reads.
    revisions_in_reads: list[str]
    reads: int = Field(ge=0)
    plant_states: dict[str, int]
    placement: dict[str, int]
    #: This drive's eligible dwell and the part of it placed, seconds.
    eligible_dwell_s: float
    placed_weight: float
    #: N02's closed-position estimate for the DID throttle route
    #: (``ClosedPositionEstimate.as_dict``), or ``None`` without DID reads.
    throttle_closed_position: dict[str, Any] | None = None


class GridEnvironment(_Strict):
    baro_kpa: float
    iat_c: float
    coolant_c: float
    battery_v: float
    rail_kpa: float
    rail_kpa_source: Literal["working_assumption", "stated"]
    throttle_pct: float
    pedal_flag: int


class GridSpec(_Strict):
    rpm_edges: list[float]
    #: In ``load_unit``, Fuel Comp's column axis's own decode.
    load_edges: list[float]
    load_axis: str
    load_unit: str
    load_basis: LoadBasis
    environment: GridEnvironment


class SimDesign(_Strict):
    """A frozen point design. :meth:`sha256` is computed, never stored here."""

    schema_version: Literal[1]
    kind: Literal["drive", "grid"]
    vehicle_profile: str
    base_revision: str
    base_image_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    plant_version: str
    plant_file_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    plant_baseline_sha256: str
    manifold_model: str
    weight_basis: Literal["dwell_s", "grid_uniform"]
    #: Drive: the steady rule's window (the dwell cap), seconds.
    dwell_cap_s: float | None = None
    #: N02's ``SAMPLES_VERSION`` the fill bytes were reconstructed under.
    fill_samples_version: int | None = None
    #: Drive: the battery voltage stated for reads with no module-voltage read
    #: (``battery_v_source: unmeasured``); ``None`` when none needed it.
    battery_v_when_unmeasured: float | None = None
    sessions: list[SessionCheck] = Field(default_factory=list)
    grid: GridSpec | None = None
    reads: int = Field(ge=0)
    #: Every read by its plant state (``fuel_plant_eval.STATES``).
    plant_states: dict[str, int]
    #: Every eligible read (drive) or grid cell: ``placed`` or why not.
    placement: dict[str, int]
    #: Dwell of the eligible reads, total and of those not placed, by reason.
    eligible_dwell_s: float
    unplaced_dwell_s: dict[str, float]
    assumptions: list[str]
    points: list[DesignPoint]

    @model_validator(mode="after")
    def _consistent(self) -> SimDesign:
        if [p.index for p in self.points] != list(range(len(self.points))):
            raise ValueError("points must be indexed 0..n-1 in order")
        if sum(self.plant_states.values()) != self.reads:
            raise ValueError("plant_states must sum to reads")
        if self.placement.get(PLACED, 0) != len(self.points):
            raise ValueError("placement['placed'] must equal the number of points")
        if self.kind == "drive":
            if not self.sessions or self.grid is not None or self.weight_basis != "dwell_s":
                raise ValueError("a drive design has sessions, no grid, and dwell weights")
            if sum(self.placement.values()) != self.plant_states.get(ELIGIBLE, 0):
                raise ValueError("placement must account for every eligible read")
        elif self.sessions or self.grid is None or self.weight_basis != "grid_uniform":
            raise ValueError("a grid design has a grid, no sessions, and uniform weights")
        return self

    def canonical_json(self) -> str:
        """The content the hash is over: sorted keys, no whitespace, no NaN."""
        return json.dumps(
            self.model_dump(mode="json"), sort_keys=True, separators=(",", ":"), allow_nan=False
        )

    def sha256(self) -> str:
        return hashlib.sha256(self.canonical_json().encode()).hexdigest()

    @property
    def source(self) -> str:
        return f"{SOURCE_PREFIX}{self.sha256()}"

    def operating_points(self, fill_bytes: Sequence[int]) -> list[OperatingPoint]:
        """Every point at one stated fill byte each (E08.3 draws them).

        A byte outside its point's interval is refused
        (``fill_byte_outside_interval``): the interval is the point."""
        if len(fill_bytes) != len(self.points):
            raise SimDesignRefusalError(
                "fill_bytes_mismatch",
                f"{len(fill_bytes)} fill bytes for {len(self.points)} points",
            )
        source = self.source
        return [
            _operating_point(p, b, source) for p, b in zip(self.points, fill_bytes, strict=True)
        ]

    def operating_point(self, index: int, *, fill_byte: int) -> OperatingPoint:
        return _operating_point(self.points[index], fill_byte, self.source)


def _operating_point(point: DesignPoint, fill_byte: int, source: str) -> OperatingPoint:
    if isinstance(fill_byte, bool) or int(fill_byte) != fill_byte:
        raise SimDesignRefusalError(
            "fill_byte_outside_interval", f"point {point.index}: {fill_byte!r} is not a count"
        )
    if not point.fill_byte_lo <= fill_byte <= point.fill_byte_hi:
        raise SimDesignRefusalError(
            "fill_byte_outside_interval",
            f"point {point.index}: byte {fill_byte} outside "
            f"[{point.fill_byte_lo}, {point.fill_byte_hi}]",
        )
    return OperatingPoint(
        rpm=point.rpm,
        maf_gps=point.maf_gps,
        baro_kpa=point.baro_kpa,
        iat_c=point.iat_c,
        coolant_c=point.coolant_c,
        battery_v=point.battery_v,
        rail_kpa=point.rail_kpa,
        map_kpa=point.map_kpa,
        throttle_pct=point.throttle_pct,
        pedal_flag=point.pedal_flag,
        conditioned={"fuel_comp_load_byte": float(fill_byte)},
        source=source,
    )


def require_base(design: SimDesign, base_image_sha256: str) -> None:
    """Refuse a design built on another base image than the run's
    (``design_revision_mismatch``): its points are that revision's driving."""
    if design.base_image_sha256 != base_image_sha256:
        raise SimDesignRefusalError(
            "design_revision_mismatch",
            f"design {design.sha256()[:12]} was built on {design.base_revision} "
            f"({design.base_image_sha256[:12]}...), the run's base image is "
            f"{base_image_sha256[:12]}...",
        )


# -- shared --------------------------------------------------------------------------------


def _base(registry: TuneRevisionRegistry, base_revision: str) -> TuneRevision:
    found = next((r for r in registry.revisions if r.revision == base_revision), None)
    if found is None:
        raise SimDesignRefusalError(
            "base_revision_unknown", f"tune_revisions.yaml records no {base_revision!r}"
        )
    if found.image_sha256 is None:
        raise SimDesignRefusalError(
            "base_image_unknown",
            f"{base_revision} has no image_sha256 in tune_revisions.yaml: a design names the "
            "image its points belong to",
        )
    return found


def _registry(profile_dir: Path, registry: TuneRevisionRegistry | None) -> TuneRevisionRegistry:
    if registry is not None:
        return registry
    loaded = load_tune_revisions(profile_dir / "tune_revisions.yaml")
    if loaded is None:
        raise SimDesignRefusalError("no_tune_registry", f"{profile_dir} has no tune_revisions.yaml")
    return loaded


def _plant(
    profile_dir: Path, plant_version: str | None
) -> tuple[FuelPlantProfile, str, B06Rules, ResolvedParameters]:
    plant, plant_sha = load_fuel_plant(profile_dir, plant_version)
    delay = load_b06(profile_dir, plant.delay)
    return plant, plant_sha, delay, resolve_parameters(plant, delay)


def _inside(path: Path, root: Path) -> bool:
    return path == root or root in path.parents


# -- drive designs ---------------------------------------------------------------------------


def _pair_index(anchor: np.ndarray, times: np.ndarray, tolerance_s: float) -> np.ndarray:
    """One-to-one, anchor-referenced pairing: per anchor, the partner's index or -1."""
    out = np.full(len(anchor), -1, dtype=np.intp)
    if not len(anchor) or not len(times):
        return out
    policy = PairingPolicy.within(pd.Timedelta(seconds=tolerance_s), reference="a")
    matched = match_ordered(anchor, times, policy)
    out[matched.index_a] = matched.index_b
    return out


def _dwell(t: np.ndarray, cap_s: float) -> np.ndarray:
    """Seconds to the next read, capped; the last read gets the cap."""
    order = np.argsort(t, kind="stable")
    ts = t[order]
    gaps = np.append(np.diff(ts), cap_s)
    out = np.empty(len(t))
    out[order] = np.minimum(gaps, cap_s)
    return out


def _regime(value: object) -> Regime:
    text = str(value)
    if text in ("closed_loop", "enrichment", "lean_target"):
        return text  # type: ignore[return-value]
    return "unknown"


def _session_check(
    path: Path, frame: pd.DataFrame, registry: TuneRevisionRegistry, base_revision: str
) -> tuple[datetime, str, list[str]]:
    dated = session_dated_at(path)
    if dated is None:
        raise SimDesignRefusalError(
            "session_undated", f"{path.name}: neither its data nor its name gives a start time"
        )
    at_start = registry.at(dated)
    start_name = at_start.revision if at_start is not None else "before_first_recorded_flash"
    stamps = pd.Series(pd.to_datetime(frame["t"].to_numpy(dtype=float), unit="s", utc=True))
    in_reads = sorted(
        {
            str(r) if r is not None else "before_first_recorded_flash"
            for r in registry.revisions_at(stamps)
        }
    )
    wrong = sorted({start_name, *in_reads} - {base_revision})
    if wrong:
        raise SimDesignRefusalError(
            "drive_not_on_base_revision",
            f"{path.name} (data from {dated.isoformat()}) ran on {wrong}, not {base_revision}: "
            "a design is built from pre-change drives on the base revision only",
        )
    return dated, start_name, in_reads


def _read_series(
    path: Path, spec: str, bindings_path: Path, dids_path: Path
) -> tuple[np.ndarray, np.ndarray]:
    """One spec on the acquisition clock, sorted and finite; empty when the
    session never captured it (or the spec does not resolve there)."""
    try:
        long = read_channels(
            path, [spec], bindings_path=bindings_path, dids_path=dids_path, acquired_time=True
        )
    except (ChannelSpecError, KeyError, ValueError):
        return np.array([], dtype=float), np.array([], dtype=float)
    rows = long[long["spec"] == spec].sort_values("timestamp", kind="stable")
    times = np.asarray(to_seconds(rows["timestamp"]), dtype=float)
    values = pd.to_numeric(rows["value"], errors="coerce").to_numpy(dtype=float)
    keep = np.isfinite(times) & np.isfinite(values)
    return times[keep], values[keep]


def _pid_percent(counts: int, declaration: FuelCompDeclaration) -> float:
    """PID 0x11's own handler on corrected counts (``A = clamp_u8(max(0, w) x
    numerator / denominator)``, truncating; percent ``A x 100 / full_scale``),
    with the declaration's constants: the forward of the interval
    :func:`~src.analysis.fuel_comp_inputs.throttle_counts_pid` inverts."""
    spec = declaration.inputs.throttle_pid
    byte = min(spec.full_scale, max(0, int(counts)) * spec.numerator // spec.denominator)
    return byte * 100.0 / spec.full_scale


@dataclass(frozen=True)
class _ThrottleReads:
    """A session's throttle reads, both routes, as N02 reads them."""

    pid_t: np.ndarray
    pid_pct: np.ndarray
    did_t: np.ndarray
    #: Per DID read: the PID-0x11-equivalent percent interval of its corrected
    #: counts, or empty when N02's closed position has no estimate.
    did_pct: list[tuple[float, float]]
    closed_position: ClosedPositionEstimate | None


def _throttle_reads(
    path: Path, declaration: FuelCompDeclaration, bindings_path: Path, dids_path: Path
) -> _ThrottleReads:
    """PID 0x11 reads, and DID 0x120F reads corrected by N02's own closed-
    position method (:func:`~src.analysis.fuel_comp_inputs.
    estimate_closed_position` on the rpm and the first captured coolant
    source, then :func:`~src.analysis.fuel_comp_inputs.throttle_counts_did`)."""
    inputs = declaration.inputs
    pid_t, pid_v = _read_series(path, inputs.throttle_pid.channel, bindings_path, dids_path)
    did_t, did_v = _read_series(path, inputs.throttle_did.channel, bindings_path, dids_path)
    if not len(did_t):
        return _ThrottleReads(pid_t, pid_v, did_t, [], None)
    raw = did_raw_counts(did_v, inputs.throttle_did.channel, load_catalog(dids_path))
    rpm_t, rpm_v = _read_series(path, inputs.rpm.channel, bindings_path, dids_path)
    cool_t, cool_v = np.array([], dtype=float), np.array([], dtype=float)
    for spec in declaration.samples.gates.coolant.sources:
        cool_t, cool_v = _read_series(path, spec, bindings_path, dids_path)
        if len(cool_t):
            break
    estimate = estimate_closed_position(did_t, raw, rpm_t, rpm_v, cool_t, cool_v, declaration)
    if estimate.interval is None:
        return _ThrottleReads(pid_t, pid_v, did_t, [], estimate)
    counts_max = inputs.throttle_did.counts_max
    pct = []
    for r in raw:
        counts = throttle_counts_did(int(r), estimate.interval, counts_max)
        pct.append((_pid_percent(counts.lo, declaration), _pid_percent(counts.hi, declaration)))
    return _ThrottleReads(pid_t, pid_v, did_t, pct, estimate)


def _battery_v(
    measured: float, when_unmeasured: float | None, session: str, t: float
) -> tuple[float, Literal["measured", "unmeasured"]]:
    """The one place a missing module voltage is decided (operator,
    2026-10-15): the point is placed at the design's stated
    ``battery_v_when_unmeasured``, labelled ``unmeasured`` (lambda, fuel and
    their deltas do not depend on it: the ECM adds its dead time and the plant
    subtracts the same one; only the absolute pulse does). With no stated
    value it is refused (``battery_v_unstated``), never defaulted."""
    if math.isfinite(measured):
        return measured, "measured"
    if when_unmeasured is None:
        raise SimDesignRefusalError(
            "battery_v_unstated",
            f"{session} at t={t:.3f} has no module-voltage read within the plant's "
            "battery_max_age_s: state battery_v_when_unmeasured (no default)",
        )
    return float(when_unmeasured), "unmeasured"


def build_drive_design(
    sessions: Sequence[Path],
    profile_dir: Path,
    base_revision: str,
    *,
    fuel_type: str,
    resolver: RevisionImageResolver | Callable[[str], FuelCompCalibration] | None = None,
    rom_dirs: Sequence[Path] = (),
    plant_version: str | None = None,
    registry: TuneRevisionRegistry | None = None,
    battery_v_when_unmeasured: float | None = None,
    bindings_path: Path = BINDINGS_YAML,
    dids_path: Path = DIDS_YAML,
) -> SimDesign:
    """A design from pre-change drives, every one on ``base_revision``.

    ``battery_v_when_unmeasured`` is the voltage a read with no module-voltage
    read is placed at (:func:`_battery_v`); it has no default, and a drive
    that needs it without it is refused (``battery_v_unstated``).

    ``resolver`` maps a revision to its Fuel Comp calibration for N02's fill
    byte (default: a :class:`RevisionImageResolver` over ``rom_dirs``, else
    ``$CANARY_ROM_DIRS``); without the image every point is unplaced under
    N02's own ``rom_image:*`` reason. ``fuel_type`` is the vehicle's (N02's
    open-loop route reads its stoichiometric AFR; nothing defaults it).
    Sessions are ordered by when their data starts, never by name."""
    if fuel_type not in FUEL_PROPERTIES:
        raise SimDesignRefusalError(
            "fuel_type_unknown", f"{fuel_type!r} is not one of {sorted(FUEL_PROPERTIES)}"
        )
    paths = [Path(s) for s in sessions]
    if not paths:
        raise SimDesignRefusalError("no_sessions", "a drive design needs at least one drive")
    names = [p.name for p in paths]
    dupes = sorted(n for n, c in Counter(names).items() if c > 1)
    if dupes:
        raise SimDesignRefusalError("duplicate_session", f"{dupes} given more than once")
    missing = sorted(p.name for p in paths if not p.is_dir())
    if missing:
        raise SimDesignRefusalError("session_missing", f"no session directory for {missing}")
    reg = _registry(profile_dir, registry)
    base = _base(reg, base_revision)
    assert base.image_sha256 is not None
    plant, plant_sha, delay, params = _plant(profile_dir, plant_version)
    try:
        check_origin(names, plant)
    except FuelPlantRefusalError as exc:
        raise SimDesignRefusalError(exc.reason, str(exc)) from exc
    declaration = load_fuel_comp_declaration(profile_dir)
    if resolver is None:
        resolver = RevisionImageResolver(
            reg,
            load_rom_map(profile_dir),
            declaration.rom,
            rom_dirs=list(rom_dirs) or rom_dirs_from_env(),
            slot=declaration.program.map_slot,
        )
    providers = profile_dir / "tuning_providers.yaml"
    delivery = load_lambda_delivery(
        profile_dir / "lambda_delivery.yaml",
        dids_path=dids_path,
        providers_path=providers if providers.exists() else None,
    )
    afr = load_afr_calibration(profile_dir / "afr_calibration.yaml")
    rule = plant.steady
    rail_working = params.values["rail_pressure"]
    battery_used = False

    built: list[tuple[datetime, str, SessionCheck, list[dict[str, Any]], dict[str, float]]] = []
    for path in paths:
        frame = build_samples(path, plant, delay)
        if len(frame) and "state" in frame:
            frame = apply_manifold_refusals(frame, plant, params)
        n = len(frame)
        states = (
            frame["state"].astype(str).value_counts().to_dict() if n and "state" in frame else {}
        )
        dated, at_start, in_reads = _session_check(path, frame, reg, base_revision)
        placement: Counter[str] = Counter()
        unplaced_dwell: dict[str, float] = {}
        rows: list[dict[str, Any]] = []
        eligible_dwell = 0.0
        closed_position: dict[str, Any] | None = None
        eligible = (frame["state"] == ELIGIBLE).to_numpy() if n and "state" in frame else None
        if eligible is not None and eligible.any():
            t_all = frame["t"].to_numpy(dtype=float)
            throttle = _throttle_reads(path, declaration, bindings_path, dids_path)
            if throttle.closed_position is not None:
                closed_position = throttle.closed_position.as_dict()
            pid_i = _pair_index(t_all, throttle.pid_t, rule.pair_tolerance_s)
            did_i = _pair_index(t_all, throttle.did_t, rule.pair_tolerance_s)

            samples = session_samples(
                path,
                declaration=declaration,
                delivery=delivery,
                afr_calibration=afr,
                afr_stoich=FUEL_PROPERTIES[fuel_type].afr_stoich,
                registry=reg,
                resolver=resolver,
                bindings_path=bindings_path,
                dids_path=dids_path,
            )
            closed = samples.frame[samples.frame["route"] == "closed_loop"].reset_index(drop=True)
            fills = points_from_samples(closed)
            fill_t = np.array([f.t for f in fills], dtype=float)
            fill_i = _pair_index(t_all, fill_t, rule.pair_tolerance_s)
            images: list[object] = closed["image_sha256"].tolist() if len(closed) else []
            fill_revisions: list[object] = closed["revision"].tolist() if len(closed) else []

            idx = np.flatnonzero(eligible)
            sub = frame.iloc[idx]
            dwell = _dwell(sub["t"].to_numpy(dtype=float), rule.window_s)
            eligible_dwell = math.fsum(dwell.tolist())
            plant_out = predict(plant_inputs(sub), params, plant.geometry)
            maps = np.asarray(plant_out.map_kpa, dtype=float)
            for k, i in enumerate(idx.tolist()):
                row = frame.iloc[i]
                why: str | None = None
                throttle_value: tuple[float, float, float, str] | None = None
                if pid_i[i] >= 0:
                    pct = float(throttle.pid_pct[pid_i[i]])
                    throttle_value = (pct, pct, pct, THROTTLE_PID_SOURCE)
                elif did_i[i] >= 0 and throttle.did_pct:
                    lo, hi = throttle.did_pct[did_i[i]]
                    throttle_value = ((lo + hi) / 2.0, lo, hi, THROTTLE_DID_SOURCE)
                if throttle_value is None:
                    why = THROTTLE_CLOSED_UNESTIMATED if did_i[i] >= 0 else THROTTLE_UNPAIRED
                elif fill_i[i] < 0:
                    why = FILL_UNPAIRED
                elif fills[fill_i[i]].reason is not None:
                    why = f"fill_byte:{fills[fill_i[i]].reason}"
                elif not math.isfinite(float(maps[k])):
                    why = MAP_UNRESOLVED
                if why is not None or throttle_value is None:
                    assert why is not None
                    placement[why] += 1
                    unplaced_dwell[why] = unplaced_dwell.get(why, 0.0) + float(dwell[k])
                    continue
                fill = fills[fill_i[i]]
                j = int(fill_i[i])
                if images[j] != base.image_sha256 or fill_revisions[j] != base_revision:
                    raise SimDesignRefusalError(
                        "fill_image_mismatch",
                        f"{path.name} at t={fill.t:.3f}: the fill byte was reconstructed on "
                        f"{fill_revisions[j]} ({str(images[j])[:12]}...), not {base_revision}'s "
                        f"image {base.image_sha256[:12]}...",
                    )
                assert fill.byte_lo is not None and fill.byte_hi is not None
                battery, battery_source = _battery_v(
                    float(row["vb_v"]), battery_v_when_unmeasured, path.name, float(row["t"])
                )
                battery_used |= battery_source == "unmeasured"
                measured = bool(row["rail_measured"])
                placement[PLACED] += 1
                rows.append(
                    {
                        "session": path.name,
                        "t": float(row["t"]),
                        "rpm": float(row["rpm"]),
                        "maf_gps": float(row["maf_gps"]),
                        "maf_source": f"read:{plant.specs.maf}",
                        "baro_kpa": float(row["baro_kpa"]),
                        "iat_c": float(row["iat_c"]),
                        "coolant_c": float(row["coolant_c"]),
                        "battery_v": battery,
                        "battery_v_source": battery_source,
                        "rail_kpa": float(row["rail_kpa"]) if measured else float(rail_working),
                        "rail_kpa_source": "measured" if measured else "working_assumption",
                        "map_kpa": float(maps[k]),
                        "map_kpa_source": params.manifold_model,
                        "throttle_pct": throttle_value[0],
                        "throttle_pct_lo": throttle_value[1],
                        "throttle_pct_hi": throttle_value[2],
                        "throttle_pct_source": throttle_value[3],
                        "pedal_flag": 0,
                        "pedal_flag_source": PEDAL_NOT_LOGGED,
                        "fill_byte_lo": int(fill.byte_lo),
                        "fill_byte_hi": int(fill.byte_hi),
                        "fill_byte_source": fill.route,
                        "weight": float(dwell[k]),
                        "regime": _regime(row["regime"]),
                    }
                )
        check = SessionCheck(
            session=path.name,
            dated_at=dated,
            revision_at_start=at_start,
            revisions_in_reads=in_reads,
            reads=n,
            plant_states={str(k): int(v) for k, v in sorted(states.items())},
            placement=dict(sorted(placement.items())),
            eligible_dwell_s=eligible_dwell,
            placed_weight=math.fsum(r["weight"] for r in rows),
            throttle_closed_position=closed_position,
        )
        built.append((dated, path.name, check, rows, unplaced_dwell))

    built.sort(key=lambda b: (b[0], b[1]))
    points: list[DesignPoint] = []
    plant_states: Counter[str] = Counter()
    placement_all: Counter[str] = Counter()
    unplaced: dict[str, float] = {}
    for _, _, check, rows, unplaced_dwell in built:
        plant_states.update(check.plant_states)
        placement_all.update(check.placement)
        for why, seconds in unplaced_dwell.items():
            unplaced[why] = unplaced.get(why, 0.0) + seconds
        for values in rows:
            points.append(DesignPoint(index=len(points), **values))
    weight = math.fsum(p.weight for p in points)
    assumptions = [
        "points are the fuel plant's warm steady reads (fuel_plant_eval.build_samples, "
        "state eligible after the plant's manifold refusals): one eligibility with E07.3",
        f"weight = dwell: seconds to the next eligible read of the same session, capped at "
        f"the steady window {rule.window_s:g} s; a session's last eligible read gets the cap",
        f"map_kpa from the plant's manifold model ({params.manifold_model}) at the read: no "
        "MAP sensor exists",
        f"rail_kpa is {plant.specs.rail_pressure} where measured, else the plant's working "
        f"value {rail_working:g} kPa ({params.rail_effect})",
        f"throttle_pct is PID 0x11 ({declaration.inputs.throttle_pid.channel}) paired "
        f"one-to-one within {rule.pair_tolerance_s:g} s, else DID 0x120F "
        f"({declaration.inputs.throttle_did.channel}) less N02's estimated closed position, "
        "through PID 0x11's own scaling (the counts interval carried as throttle_pct_lo/hi); "
        "a contract boundary only, nothing in the paired slice computes from it",
        "battery_v is the module voltage within the plant's battery_max_age_s; without one "
        "the point is placed at the stated battery_v_when_unmeasured, labelled unmeasured "
        "(only the absolute injector pulse depends on it)",
        "pedal_flag is stated 0: the pedal-enrichment flag byte is not logged",
        "the fill byte is N02's reconstructed closed-loop-route interval (fuel_comp_samples."
        f"session_samples, SAMPLES_VERSION {SAMPLES_VERSION}) paired one-to-one within "
        f"{rule.pair_tolerance_s:g} s, carried whole",
        "regime is the plant's label from the bracketed commanded phi (closed_loop, "
        "enrichment, lean_target, unknown)",
    ]
    return SimDesign(
        schema_version=1,
        kind="drive",
        vehicle_profile=profile_dir.name,
        base_revision=base_revision,
        base_image_sha256=base.image_sha256,
        plant_version=plant.plant_version,
        plant_file_sha256=plant_sha,
        plant_baseline_sha256=params.baseline_sha256,
        manifold_model=params.manifold_model,
        weight_basis="dwell_s",
        dwell_cap_s=rule.window_s,
        fill_samples_version=SAMPLES_VERSION,
        battery_v_when_unmeasured=battery_v_when_unmeasured if battery_used else None,
        sessions=[b[2] for b in built],
        reads=sum(plant_states.values()),
        plant_states=dict(sorted(plant_states.items())),
        placement=dict(sorted(placement_all.items())),
        eligible_dwell_s=weight + math.fsum(unplaced.values()),
        unplaced_dwell_s=dict(sorted(unplaced.items())),
        assumptions=assumptions,
        points=points,
    )


# -- grid designs ----------------------------------------------------------------------------


def _fuel_comp_load_axis(rom_map: RomMap, profile_dir: Path) -> tuple[str, str, float, float]:
    """Fuel Comp's column (load) axis: its id, decode unit, scale and offset."""
    declaration = load_fuel_comp_declaration(profile_dir)
    kind = declaration.rom.fuel_comp_kind
    tables = [t for t in rom_map.tables if t.kind == kind]
    if not tables or tables[0].axes.cols is None:
        raise SimDesignRefusalError(
            "load_axis_unknown", f"rom_map.yaml has no {kind!r} table with a column axis"
        )
    axis = rom_map.table(tables[0].axes.cols)
    decode = axis.decode
    if decode is None or decode.kind != "linear" or decode.scale is None:
        raise SimDesignRefusalError(
            "load_axis_undecoded", f"{axis.id} has no verified linear decode"
        )
    if axis.confidence != "verified" or decode.confidence != "verified":
        raise SimDesignRefusalError("load_axis_unverified", f"{axis.id} is not verified")
    return axis.id, decode.unit, float(decode.scale), float(decode.offset)


def _edges(name: str, values: Sequence[float]) -> list[float]:
    edges = [float(v) for v in values]
    if len(edges) < 2 or not all(math.isfinite(e) for e in edges):
        raise SimDesignRefusalError("grid_edges_invalid", f"{name}: at least two finite edges")
    if any(b <= a for a, b in zip(edges, edges[1:], strict=False)):
        raise SimDesignRefusalError("grid_edges_invalid", f"{name}: edges must strictly increase")
    return edges


def _maf_for_map(
    target_kpa: np.ndarray,
    rpm: np.ndarray,
    env: GridEnvironment,
    plant: FuelPlantProfile,
    params: ResolvedParameters,
) -> tuple[np.ndarray, np.ndarray, np.ndarray]:
    """The MAF at which the plant's own manifold model gives ``target_kpa``,
    by bisection on :func:`~src.analysis.fuel_plant.predict` itself (MAP rises
    with airflow in every form; a refused MAP counts as above). Returns MAF,
    the plant's MAP there, and its refusal per cell (``""`` = none)."""
    n = len(target_kpa)

    def plant_map(maf: np.ndarray) -> tuple[np.ndarray, np.ndarray]:
        out = predict(
            PlantInputs(
                pulse_ms=np.full(n, 2.0),
                ecu_dead_time_ms=np.zeros(n),
                rpm=rpm,
                maf_gps=maf,
                baro_kpa=np.full(n, env.baro_kpa),
                iat_c=np.full(n, env.iat_c),
                rail_kpa=np.full(n, env.rail_kpa),
            ),
            params,
            plant.geometry,
        )
        refusal = (
            out.manifold_refusal
            if out.manifold_refusal is not None
            else np.full(n, "", dtype="<U32")
        )
        return np.asarray(out.map_kpa, dtype=float), refusal

    def at_or_above(maf: np.ndarray) -> np.ndarray:
        m, _ = plant_map(maf)
        reached: np.ndarray = ~np.isfinite(m) | (m >= target_kpa)
        return reached

    lo = np.zeros(n)
    hi = np.ones(n)
    for _ in range(_MAF_BRACKET_DOUBLINGS):
        short = ~at_or_above(hi)
        if not short.any():
            break
        hi = np.where(short, hi * 2.0, hi)
    for _ in range(_MAF_BISECTION_STEPS):
        mid = (lo + hi) / 2.0
        up = at_or_above(mid)
        hi = np.where(up, mid, hi)
        lo = np.where(up, lo, mid)
    maf = (lo + hi) / 2.0
    m, refusal = plant_map(maf)
    return maf, m, refusal


def _grid_map_mismatch(map_kpa: float, target_kpa: float) -> str | None:
    """``None`` when the solved MAP is the cell's target (to
    :data:`GRID_MAP_REL_TOLERANCE`), else why not: above it, the model's floor
    (fuel-mv-3's residual gas keeps the manifold from emptying below
    ``exhaust_ratio / r_c^n`` x baro), so no airflow gives the target."""
    if not math.isfinite(map_kpa):
        return MAP_UNRESOLVED
    if abs(map_kpa - target_kpa) <= GRID_MAP_REL_TOLERANCE * target_kpa:
        return None
    return LOAD_BELOW_MODEL_FLOOR if map_kpa > target_kpa else MAP_UNRESOLVED


def build_grid_design(
    rpm_edges: Sequence[float],
    load_edges: Sequence[float],
    *,
    profile_dir: Path,
    base_revision: str,
    load_basis: LoadBasis | None = None,
    baro_kpa: float | None = None,
    iat_c: float | None = None,
    coolant_c: float | None = None,
    battery_v: float | None = None,
    rail_kpa: float | Literal["working_assumption"] | None = None,
    throttle_pct: float | None = None,
    pedal_flag: int | None = None,
    plant_version: str | None = None,
    registry: TuneRevisionRegistry | None = None,
) -> SimDesign:
    """One point per cell centre of a stated rpm x load grid.

    ``load_edges`` are in Fuel Comp's column axis's own decode units (the
    throttle-model fill, % of the model's ceiling, from ``rom_map.yaml``).
    Every environmental value is stated; none defaults (``environment_unstated``
    names what is missing): the operator's home is ~80 kPa and trips reach
    63 kPa, so no baro is assumed. ``rail_kpa="working_assumption"`` runs the
    plant's working rail value and says so. ``load_basis`` states how fill
    becomes airflow (:data:`LoadBasis`; no default, ``load_basis_unstated``).
    The fill byte at a centre is carried as the whole counts either side of
    it, never rounded."""
    stated: dict[str, object] = {
        "baro_kpa": baro_kpa,
        "iat_c": iat_c,
        "coolant_c": coolant_c,
        "battery_v": battery_v,
        "rail_kpa": rail_kpa,
        "throttle_pct": throttle_pct,
        "pedal_flag": pedal_flag,
    }
    unstated = sorted(k for k, v in stated.items() if v is None)
    if unstated:
        raise SimDesignRefusalError(
            "environment_unstated",
            f"a grid states every value it runs at; missing {unstated} (no default baro or "
            "IAT: home is ~80 kPa and trips reach 63 kPa)",
        )
    if load_basis is None:
        raise SimDesignRefusalError(
            "load_basis_unstated",
            f"state how a fill-% load becomes airflow: one of {sorted(_LOAD_BASIS_MEANING)}",
        )
    if load_basis not in _LOAD_BASIS_MEANING:
        raise SimDesignRefusalError("load_basis_unknown", f"{load_basis!r}")
    assert baro_kpa is not None and iat_c is not None and coolant_c is not None
    assert battery_v is not None and throttle_pct is not None and pedal_flag is not None
    rpm_e = _edges("rpm_edges", rpm_edges)
    load_e = _edges("load_edges", load_edges)
    reg = _registry(profile_dir, registry)
    base = _base(reg, base_revision)
    assert base.image_sha256 is not None
    plant, plant_sha, _, params = _plant(profile_dir, plant_version)
    rom_map = load_rom_map(profile_dir)
    axis_id, unit, scale, offset = _fuel_comp_load_axis(rom_map, profile_dir)
    if unit != "%":
        raise SimDesignRefusalError(
            "load_axis_not_percent", f"{axis_id} decodes to {unit!r}, not % of the model ceiling"
        )
    if load_e[0] < 0.0 or load_e[-1] > 100.0:
        raise SimDesignRefusalError(
            "load_outside_model",
            f"load edges {load_e[0]:g}..{load_e[-1]:g} %: the fill model spans 0-100 %",
        )
    if rail_kpa == "working_assumption":
        rail_value, rail_source = float(params.values["rail_pressure"]), "working_assumption"
    else:
        rail_value, rail_source = float(rail_kpa), "stated"  # type: ignore[arg-type]
    env = GridEnvironment(
        baro_kpa=float(baro_kpa),
        iat_c=float(iat_c),
        coolant_c=float(coolant_c),
        battery_v=float(battery_v),
        rail_kpa=rail_value,
        rail_kpa_source=rail_source,  # type: ignore[arg-type]
        throttle_pct=float(throttle_pct),
        pedal_flag=int(pedal_flag),
    )
    cells = [
        (i, j, (rpm_e[i] + rpm_e[i + 1]) / 2.0, (load_e[j] + load_e[j + 1]) / 2.0)
        for i in range(len(rpm_e) - 1)
        for j in range(len(load_e) - 1)
    ]
    rpm = np.array([c[2] for c in cells])
    fraction = np.array([c[3] for c in cells]) / 100.0
    targets = fraction * env.baro_kpa
    maf, maps, refusal = _maf_for_map(targets, rpm, env, plant, params)
    placement: Counter[str] = Counter()
    points: list[DesignPoint] = []
    for k, (i, j, rpm_c, load_c) in enumerate(cells):
        why = str(refusal[k]) or _grid_map_mismatch(float(maps[k]), float(targets[k]))
        if why:
            placement[why] += 1
            continue
        raw = (load_c - offset) / scale
        lo_b, hi_b = math.floor(raw + 1e-9), math.ceil(raw - 1e-9)
        if hi_b < lo_b:
            lo_b = hi_b = round(raw)
        placement[PLACED] += 1
        points.append(
            DesignPoint(
                index=len(points),
                cell_row=i,
                cell_col=j,
                rpm=float(rpm_c),
                maf_gps=float(maf[k]),
                maf_source=f"manifold_model_inverse:{params.manifold_model}:{load_basis}",
                baro_kpa=env.baro_kpa,
                iat_c=env.iat_c,
                coolant_c=env.coolant_c,
                battery_v=env.battery_v,
                battery_v_source="stated",
                rail_kpa=env.rail_kpa,
                rail_kpa_source=env.rail_kpa_source,
                map_kpa=float(maps[k]),
                map_kpa_source=params.manifold_model,
                throttle_pct=env.throttle_pct,
                throttle_pct_lo=env.throttle_pct,
                throttle_pct_hi=env.throttle_pct,
                throttle_pct_source="stated",
                pedal_flag=env.pedal_flag,
                pedal_flag_source="stated",
                fill_byte_lo=int(lo_b),
                fill_byte_hi=int(hi_b),
                fill_byte_source=f"grid_cell_centre:{axis_id}",
                weight=1.0,
                regime="unknown",
            )
        )
    return SimDesign(
        schema_version=1,
        kind="grid",
        vehicle_profile=profile_dir.name,
        base_revision=base_revision,
        base_image_sha256=base.image_sha256,
        plant_version=plant.plant_version,
        plant_file_sha256=plant_sha,
        plant_baseline_sha256=params.baseline_sha256,
        manifold_model=params.manifold_model,
        weight_basis="grid_uniform",
        grid=GridSpec(
            rpm_edges=rpm_e,
            load_edges=load_e,
            load_axis=axis_id,
            load_unit=unit,
            load_basis=load_basis,
            environment=env,
        ),
        reads=0,
        plant_states={},
        placement=dict(sorted(placement.items())),
        eligible_dwell_s=0.0,
        unplaced_dwell_s={},
        assumptions=[
            _LOAD_BASIS_MEANING[load_basis],
            "every environmental value, the throttle and the pedal flag are stated in the call",
            "regime unknown: a grid point has no read; the run labels it from its own target",
            "the fill byte at a cell centre is the whole counts either side of it",
        ],
        points=points,
    )


# -- storage ---------------------------------------------------------------------------------

_POINT_SCHEMA = pa.schema(
    [
        ("index", pa.int64()),
        ("session", pa.string()),
        ("t", pa.float64()),
        ("cell_row", pa.int64()),
        ("cell_col", pa.int64()),
        ("rpm", pa.float64()),
        ("maf_gps", pa.float64()),
        ("maf_source", pa.string()),
        ("baro_kpa", pa.float64()),
        ("iat_c", pa.float64()),
        ("coolant_c", pa.float64()),
        ("battery_v", pa.float64()),
        ("battery_v_source", pa.string()),
        ("rail_kpa", pa.float64()),
        ("rail_kpa_source", pa.string()),
        ("map_kpa", pa.float64()),
        ("map_kpa_source", pa.string()),
        ("throttle_pct", pa.float64()),
        ("throttle_pct_lo", pa.float64()),
        ("throttle_pct_hi", pa.float64()),
        ("throttle_pct_source", pa.string()),
        ("pedal_flag", pa.int64()),
        ("pedal_flag_source", pa.string()),
        ("fill_byte_lo", pa.int64()),
        ("fill_byte_hi", pa.int64()),
        ("fill_byte_source", pa.string()),
        ("weight", pa.float64()),
        ("regime", pa.string()),
    ]
)
assert [f.name for f in _POINT_SCHEMA] == list(DesignPoint.model_fields)


def write_design(design: SimDesign, root: Path) -> Path:
    """Store ``design`` as ``<root>/<sha256>/design.parquet`` (the points) and
    ``design.json`` (everything else, and the hash). ``root`` is a runtime
    directory (scratch, or under the state dir later): refused inside the
    checkout (``inside_checkout``), as candidates are."""
    resolved = Path(root).resolve()
    if _inside(resolved, REPO_ROOT) or _inside(REPO_ROOT, resolved):
        raise SimDesignRefusalError(
            "inside_checkout", f"{resolved} is in the checkout; designs stay out of Git"
        )
    sha = design.sha256()
    directory = resolved / sha
    directory.mkdir(parents=True, exist_ok=True)
    columns = {f.name: [getattr(p, f.name) for p in design.points] for f in _POINT_SCHEMA}
    table = pa.table(columns, schema=_POINT_SCHEMA)
    tmp = directory / f"{DESIGN_PARQUET}.tmp"
    pq.write_table(table, tmp)  # type: ignore[no-untyped-call, unused-ignore]
    tmp.replace(directory / DESIGN_PARQUET)
    meta = design.model_dump(mode="json", exclude={"points"})
    record = {"sha256": sha, "points_file": DESIGN_PARQUET, "n_points": len(design.points), **meta}
    atomic_write_text(directory / DESIGN_JSON, json.dumps(record, indent=2, sort_keys=True) + "\n")
    return directory


def load_design(directory: Path) -> SimDesign:
    """A written design, refused unless its content hashes to its recorded
    sha256 and its directory name (``design_hash_mismatch``)."""
    directory = Path(directory)
    meta_path, points_path = directory / DESIGN_JSON, directory / DESIGN_PARQUET
    if not meta_path.is_file() or not points_path.is_file():
        raise SimDesignRefusalError(
            "design_missing", f"{directory} holds no {DESIGN_JSON} and {DESIGN_PARQUET}"
        )
    meta: dict[str, Any] = json.loads(meta_path.read_text())
    recorded = meta.pop("sha256", None)
    meta.pop("points_file", None)
    n_points = meta.pop("n_points", None)
    rows = pq.read_table(points_path).to_pylist()  # type: ignore[no-untyped-call, unused-ignore]
    if n_points != len(rows):
        raise SimDesignRefusalError(
            "design_hash_mismatch",
            f"{DESIGN_JSON} names {n_points} points, the table has {len(rows)}",
        )
    design = SimDesign.model_validate({**meta, "points": rows})
    sha = design.sha256()
    if sha != recorded or sha != directory.name:
        raise SimDesignRefusalError(
            "design_hash_mismatch",
            f"{directory} content hashes to {sha}; recorded {recorded}, directory {directory.name}",
        )
    return design


def summary(design: SimDesign) -> dict[str, Any]:
    """Counts a caller prints: points, weight, per-state and placement counts."""
    weights = [p.weight for p in design.points]
    by_regime: dict[str, float] = {}
    for p in design.points:
        by_regime[p.regime] = by_regime.get(p.regime, 0.0) + p.weight
    return {
        "sha256": design.sha256(),
        "kind": design.kind,
        "base_revision": design.base_revision,
        "points": len(design.points),
        "weight": math.fsum(weights),
        "weight_basis": design.weight_basis,
        "eligible_dwell_s": design.eligible_dwell_s,
        "reads": design.reads,
        "plant_states": design.plant_states,
        "placement": design.placement,
        "unplaced_dwell_s": design.unplaced_dwell_s,
        "weight_by_regime": dict(sorted(by_regime.items())),
        "points_by_throttle_source": dict(
            sorted(Counter(p.throttle_pct_source for p in design.points).items())
        ),
        "points_by_battery_source": dict(
            sorted(Counter(p.battery_v_source for p in design.points).items())
        ),
        "battery_unmeasured_points": sum(p.battery_v_source == "unmeasured" for p in design.points),
        "battery_v_when_unmeasured": design.battery_v_when_unmeasured,
        "fill_byte_width": (
            {
                "max": max(p.fill_byte_hi - p.fill_byte_lo for p in design.points),
                "median": float(
                    np.median([p.fill_byte_hi - p.fill_byte_lo for p in design.points])
                ),
            }
            if design.points
            else None
        ),
    }


__all__ = [
    "SCHEMA_VERSION",
    "SOURCE_PREFIX",
    "DesignPoint",
    "GridEnvironment",
    "GridSpec",
    "SessionCheck",
    "SimDesign",
    "SimDesignRefusalError",
    "build_drive_design",
    "build_grid_design",
    "load_design",
    "require_base",
    "summary",
    "write_design",
]
