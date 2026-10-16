"""Monte Carlo over the frozen plant's declared uncertainty, paired across two ROMs (E08.3).

One draw is one plant parameter vector and one Fuel Comp load byte per
operating point. Both ROMs run every point of a draw on that same vector and
byte (common random numbers), so common-mode plant error cancels in the
deltas, which are the product; absolute values are a secondary view.

What it makes impossible rather than documented:

* **A distribution is a declared fact, never a default.** Every parameter the
  plant brackets needs a declared form with evidence in the profile's
  ``sim_uncertainty.yaml`` (:class:`UncertaintyDeclaration`). A missing one is
  ``distribution_undeclared``, never silently uniform; a bracket that is not
  the registry's is ``bracket_mismatch``; a name the registry lacks is
  ``parameter_unknown``. The lumped scale ``stoich_air_per_open_ms`` has no
  bracket: it is drawn inside the frozen baseline's bootstrap interval.
* **The fill byte is an interval, drawn whole.** A design point carries the
  reconstructed interval (:class:`DesignPoint`), each whole count equally
  likely (``uniform_integer``); a point that states a byte is refused, so the
  interval cannot be collapsed to a midpoint on the way in.
* **Sampling is reproducible and stated.** ``draws`` and ``seed`` are
  required; the generator is ``numpy.random.Generator(PCG64(seed))``; the
  seed, draw count and the uncertainty file's sha256 are in the provenance.
* **The fast path is the plain path.** The ECU side (BFS, X, Target AFR,
  pulse, trims) does not read a plant parameter, so it is computed once per
  (point, fill byte) through :mod:`src.analysis.paired_scenarios`' own
  per-point helpers; only the plant runs per draw, vectorised over every
  point and both ROMs. Every run re-checks a sample of combinations against
  :func:`~src.analysis.paired_scenarios.run_paired` itself and refuses on any
  difference (``fast_path_diverged``); the tests check the whole result.
* **Support never varies by plant draw.** Support comes from the ECU side and
  the run's gates, never from a drawn parameter; a supported output that is
  not a finite number at some draw is refused (``support_varies_by_draw``).
  It can vary with the fill byte, which is an input: kept per point and draw.
* **The caveats travel with the numbers.** Structural error is not a
  parameter and is not sampled (``structural_error_not_sampled``); the plant's
  qualification reason and every assumption ``run_paired`` states are carried;
  a drawn parameter that moves no output anywhere in the design is named
  (``parameter_inert:<id>``). Traces use a ``_sim-*`` session name.
"""

from __future__ import annotations

import hashlib
import json
import math
from collections import Counter
from collections.abc import Collection, Mapping, Sequence
from dataclasses import dataclass, field, replace
from pathlib import Path
from statistics import NormalDist
from typing import Any, Literal

import numpy as np
import pandas as pd
import yaml
from numpy.random import PCG64, Generator
from numpy.typing import NDArray
from pydantic import BaseModel, ConfigDict, Field, ValidationError, model_validator

from src.analysis.fuel_plant import (
    DELAY_PARAMETER,
    MANIFOLD_MODELS,
    REFUSING_MODELS,
    ResolvedParameters,
    load_b06,
    load_fuel_plant,
    plant_files,
    plant_qualification,
    plant_reads,
    resolve_parameters,
)
from src.analysis.paired_scenarios import (
    _LOAD_BYTE,
    _PLANT_OUTPUTS,
    OUTPUTS,
    PLANT_ASSUMPTION_PREFIX,
    BatterySource,
    FuelFn,
    GateReason,
    MapSource,
    OperatingPoint,
    PairedResult,
    PairedScenario,
    RailSource,
    ReasonScope,
    SimKit,
    _ecu_chain,
    _fuel_side,
    _pair_loop,
    _refuse_recorded_sources,
    _resets,
    _Side,
    _side,
    _with_contract_mode,
    compare_scenario_roms,
    load_kit,
    output_reasons,
    plant_assumption_lines,
    plant_parameters,
    plant_rail_kpa,
    refusing_as_simulation,
    rom_gate,
    run_paired,
    stated_sample,
)
from src.analysis.paired_scenarios import TOOL_VERSION as PAIRED_TOOL_VERSION
from src.analysis.rom_image import sha256_hex
from src.analysis.sim_contract import SimulationRefusalError
from src.analysis.sim_design import MAF_FROM_MANIFOLD_MODEL, SimDesign
from src.analysis.sim_kit import SIM_KIT_FILE, load_sim_kit_declaration
from src.analysis.twin_runner import (
    StatedPulse,
    plant_predict,
    stage_order,
    stated_plant_samples,
    stated_pulse,
)
from src.session import SYNTHETIC_SESSION_PREFIX

#: Bump when what a result means changes. 2: per-output blanking under a
#: scoped gate, and held working and design assumptions named (E08.10).
#: 3: runs a VE(rpm) plant (E08.11): a VE draw moves only the floor bands, the
#: held fit is named, and a model-MAP refusal is refused per draw, not per plant.
TOOL_VERSION = "3"
UNCERTAINTY_FILE = "sim_uncertainty.yaml"
#: The standing caveat: the plant's structural error is not a parameter.
STRUCTURAL_ERROR_NOT_SAMPLED = "structural_error_not_sampled"
#: The lumped identified scale: drawn inside the baseline's measured interval.
LUMPED_SCALE = "stoich_air_per_open_ms"
#: The fill byte every design point carries as an interval.
FILL_BYTE = _LOAD_BYTE
#: How many (point, fill byte) combinations each run re-checks against run_paired.
SELF_CHECK_COMBOS = 8
#: The mode a paired Monte Carlo runs (the only one run_paired computes).
MODE = "conditional_rom_comparison"
#: A design's own assumptions reach the result with this prefix (E08.10).
DESIGN_ASSUMPTION_PREFIX = "design: "
#: The plant parameter a grid design solved its MAF at (fuel_plant's VE).
_VE_PARAMETER = "manifold_pressure"

FloatArray = NDArray[np.float64]
IntArray = NDArray[np.int64]


# -- the uncertainty declaration ----------------------------------------------------------


class _Strict(BaseModel):
    model_config = ConfigDict(extra="forbid")


class ParameterUncertainty(_Strict):
    """How one plant parameter is drawn, and why that form."""

    distribution: Literal["uniform", "triangular", "truncated_normal"]
    #: Where the bracket comes from: a registry working assumption's bracket,
    #: or (the lumped scale) the frozen baseline's bootstrap interval.
    bracket_from: Literal["working_assumption", "baseline_interval"]
    #: Copied from the registry or the baseline, and checked equal to it.
    bracket: tuple[float, float]
    #: ``triangular`` only.
    mode: float | None = None
    #: ``truncated_normal`` only (truncated at the bracket).
    mean: float | None = None
    sigma: float | None = None
    evidence: str = Field(min_length=1)

    @model_validator(mode="after")
    def _form(self) -> ParameterUncertainty:
        lo, hi = self.bracket
        if not lo < hi:
            raise ValueError(f"bracket {self.bracket} is empty: nothing to draw")
        wants = {
            "uniform": set(),
            "triangular": {"mode"},
            "truncated_normal": {"mean", "sigma"},
        }[self.distribution]
        given = {k for k in ("mode", "mean", "sigma") if getattr(self, k) is not None}
        if given != wants:
            raise ValueError(
                f"{self.distribution} takes {sorted(wants) or 'no shape'}; got {sorted(given)}"
            )
        for name in ("mode", "mean"):
            value = getattr(self, name)
            if value is not None and not lo <= value <= hi:
                raise ValueError(f"{name} {value} is outside the bracket {self.bracket}")
        if self.sigma is not None and not self.sigma > 0:
            raise ValueError("sigma must be positive")
        return self


class FillByteUncertainty(_Strict):
    distribution: Literal["uniform_integer"]
    evidence: str = Field(min_length=1)


class InputUncertainty(_Strict):
    fill_byte: FillByteUncertainty


class UncertaintyDeclaration(_Strict):
    """``data/<vehicle>/sim_uncertainty.yaml``: one plant version's draws."""

    schema_version: Literal[1]
    plant_version: str = Field(min_length=1)
    parameters: dict[str, ParameterUncertainty] = Field(min_length=1)
    inputs: InputUncertainty


@dataclass(frozen=True)
class LoadedUncertainty:
    declaration: UncertaintyDeclaration
    #: sha256 of the file's bytes (provenance).
    sha256: str
    path: str


def load_uncertainty(path: Path) -> LoadedUncertainty:
    text = path.read_bytes()
    declaration = UncertaintyDeclaration.model_validate(yaml.safe_load(text))
    return LoadedUncertainty(declaration, hashlib.sha256(text).hexdigest(), str(path))


def bracketed_parameters(
    kit: SimKit,
) -> dict[str, tuple[Literal["working_assumption", "baseline_interval"], tuple[float, float]]]:
    """Every parameter the kit's plant reads that carries a spread, with where
    it comes from: a working assumption's bracket (B06's for the delay, as
    :func:`~src.analysis.fuel_plant.sensitivity_cases` reads it) or the frozen
    baseline's interval for an identified value."""
    out: dict[
        str, tuple[Literal["working_assumption", "baseline_interval"], tuple[float, float]]
    ] = {}
    baseline = kit.plant.baseline
    for pid in plant_reads(kit.plant.manifold_model):
        p = kit.plant.registry.get(pid)
        if pid == DELAY_PARAMETER:
            lo, hi = kit.delay.bracket
            out[pid] = ("working_assumption", (float(lo), float(hi)))
        elif p.working_assumption is not None:
            lo, hi = p.working_assumption.bracket
            if lo != hi:
                out[pid] = ("working_assumption", (float(lo), float(hi)))
        elif baseline is not None and pid in baseline.intervals:
            lo, hi = baseline.intervals[pid]
            out[pid] = ("baseline_interval", (float(lo), float(hi)))
    return out


def check_uncertainty(declaration: UncertaintyDeclaration, kit: SimKit) -> None:
    """Refuse a declaration that does not describe the kit's plant exactly."""
    if declaration.plant_version != kit.plant.plant_version:
        raise SimulationRefusalError(
            "uncertainty_plant_mismatch",
            f"the declaration is for {declaration.plant_version}; the run's plant is "
            f"{kit.plant.plant_version}",
        )
    registry = {p.id for p in kit.plant.registry.parameters}
    unknown = sorted(set(declaration.parameters) - registry)
    if unknown:
        raise SimulationRefusalError(
            "parameter_unknown", f"plant {kit.plant.plant_version} has no parameters {unknown}"
        )
    spread = bracketed_parameters(kit)
    fixed = sorted(set(declaration.parameters) - set(spread))
    if fixed:
        raise SimulationRefusalError(
            "parameter_not_bracketed",
            f"{fixed} carry no bracket or measured interval in plant "
            f"{kit.plant.plant_version}: a distribution would invent their uncertainty",
        )
    missing = sorted(set(spread) - set(declaration.parameters))
    if missing:
        raise SimulationRefusalError(
            "distribution_undeclared",
            f"{missing} are bracketed in plant {kit.plant.plant_version} with no declared "
            "distribution: a bracket says where, not how likely, and is never silently uniform",
        )
    for pid, entry in sorted(declaration.parameters.items()):
        source, bracket = spread[pid]
        if (entry.bracket_from, tuple(entry.bracket)) != (source, bracket):
            raise SimulationRefusalError(
                "bracket_mismatch",
                f"{pid}: declared {entry.bracket_from} {tuple(entry.bracket)}; plant "
                f"{kit.plant.plant_version} has {source} {bracket}",
            )


def _sample(entry: ParameterUncertainty, rng: Generator, n: int) -> FloatArray:
    lo, hi = entry.bracket
    if entry.distribution == "uniform":
        values = rng.uniform(lo, hi, size=n)
    elif entry.distribution == "triangular":
        assert entry.mode is not None
        values = rng.triangular(lo, entry.mode, hi, size=n)
    else:
        assert entry.mean is not None and entry.sigma is not None
        normal = NormalDist(entry.mean, entry.sigma)
        u = rng.uniform(normal.cdf(lo), normal.cdf(hi), size=n)
        tiny = np.nextafter(0.0, 1.0)
        u = np.clip(u, tiny, 1.0 - np.finfo(float).epsneg)
        values = np.array([normal.inv_cdf(float(p)) for p in u])
    clipped: FloatArray = np.clip(np.asarray(values, dtype=float), lo, hi)
    return clipped


# -- the design --------------------------------------------------------------------------


@dataclass(frozen=True)
class DesignPoint:
    """One operating point of a frozen design, as the Monte Carlo takes it.

    ``point.conditioned`` is empty: the fill byte is the interval
    ``fill_byte_lo..fill_byte_hi`` (N02's reconstruction, carried whole) and
    is drawn per draw. ``weight`` is the point's dwell, for aggregation
    (E08.4). E08.2's ``SimDesign`` adapts its points to this."""

    point: OperatingPoint
    fill_byte_lo: int
    fill_byte_hi: int
    weight: float


@dataclass(frozen=True)
class MonteCarloDesign:
    """A frozen design's identity and its points (E08.2's ``SimDesign``)."""

    sha256: str
    #: The base image the design's drives were recorded on (or the grid's).
    base_image_sha256: str
    points: tuple[DesignPoint, ...]
    #: The plant version the design placed its points with (its MAF and MAP
    #: model); ``None`` for a hand-built design. A run on another is refused.
    plant_version: str | None = None
    #: The design's own stated assumptions (``SimDesign.assumptions``): every
    #: result built on it carries them (E08.10).
    assumptions: tuple[str, ...] = ()
    #: Points whose MAF the design solved through the plant's manifold model
    #: when it was built (a grid): frozen there, so a VE draw cannot move it.
    maf_frozen_points: int = 0

    @classmethod
    def from_sim_design(cls, design: SimDesign) -> MonteCarloDesign:
        """E08.2's frozen design as the Monte Carlo takes it: each point at no
        stated fill byte (its interval is drawn), with its MAP, rail and
        battery sources. A MAP the design took from the plant's manifold
        model is computed by the run's plant per draw (``plant_model``); any
        other MAP source is a measured, held boundary."""
        points = []
        for i, p in enumerate(design.points):
            op = design.operating_point(i, fill_byte=p.fill_byte_lo)
            points.append(
                DesignPoint(
                    replace(
                        op,
                        conditioned={},
                        map_kpa_source=_map_source(i, p.map_kpa_source),
                        rail_kpa_source=_known_source(i, "rail", p.rail_kpa_source, _RAIL_SOURCES),
                        battery_v_source=_known_source(
                            i, "battery", p.battery_v_source, _BATTERY_SOURCES
                        ),
                    ),
                    p.fill_byte_lo,
                    p.fill_byte_hi,
                    p.weight,
                )
            )
        return cls(
            sha256=design.sha256(),
            base_image_sha256=design.base_image_sha256,
            points=tuple(points),
            plant_version=design.plant_version,
            assumptions=tuple(design.assumptions),
            maf_frozen_points=sum(
                p.maf_source.startswith(f"{MAF_FROM_MANIFOLD_MODEL}:") for p in design.points
            ),
        )


#: E08.2's rail and battery source labels, each meaning the same here.
_RAIL_SOURCES: dict[str, RailSource] = {
    "stated": "stated",
    "measured": "measured",
    "working_assumption": "working_assumption",
}
_BATTERY_SOURCES: dict[str, BatterySource] = {
    "stated": "stated",
    "measured": "measured",
    "unmeasured": "unmeasured",
}


def _map_source(index: int, source: str) -> MapSource:
    """A design's MAP source, mapped explicitly: a manifold model's name is
    the plant's model (computed per draw), ``measured`` or a ``read:`` channel
    is a measured boundary, ``stated`` is stated. Anything else is refused,
    so a typo cannot become a held measurement."""
    if source in MANIFOLD_MODELS:
        return "plant_model"
    if source == "measured" or source.startswith("read:"):
        return "measured"
    if source == "stated":
        return "stated"
    raise SimulationRefusalError(
        "map_source_unknown",
        f"point {index}: MAP source {source!r} is neither a manifold model "
        f"{sorted(MANIFOLD_MODELS)}, measured/read:<channel>, nor stated",
    )


def _known_source(index: int, what: str, source: str, known: Mapping[str, Any]) -> Any:
    if source not in known:
        raise SimulationRefusalError(
            f"{what}_source_unknown",
            f"point {index}: {what} source {source!r} is not one of {sorted(known)}",
        )
    return known[source]


def _check_design(design: MonteCarloDesign, base_image_sha256: str) -> None:
    if design.base_image_sha256 != base_image_sha256:
        raise SimulationRefusalError(
            "design_revision_mismatch",
            f"design {design.sha256[:12]} was built on image {design.base_image_sha256[:12]}; "
            f"the run's base is {base_image_sha256[:12]}",
        )
    if not design.points:
        raise SimulationRefusalError("design_empty", f"design {design.sha256[:12]} has no points")
    source = f"design:{design.sha256}"
    for i, dp in enumerate(design.points):
        if dp.point.source != source:
            raise SimulationRefusalError(
                "source_not_design",
                f"point {i} names source {dp.point.source!r}; every point of design "
                f"{design.sha256[:12]} is {source!r}",
            )
        if dp.point.conditioned:
            raise SimulationRefusalError(
                "conditioned_stated",
                f"point {i} states {sorted(dp.point.conditioned)}: the fill byte is drawn from "
                "its interval, never stated (a midpoint is not the interval)",
            )
        lo, hi = dp.fill_byte_lo, dp.fill_byte_hi
        if not (isinstance(lo, int) and isinstance(hi, int) and 0 <= lo <= hi <= 255):
            raise SimulationRefusalError(
                "fill_byte_interval_invalid", f"point {i}: [{lo}, {hi}] is not a byte interval"
            )
        if not (math.isfinite(dp.weight) and dp.weight > 0):
            raise SimulationRefusalError("weight_invalid", f"point {i}: weight {dp.weight}")


# -- draws --------------------------------------------------------------------------------


@dataclass(frozen=True)
class Draws:
    """Every drawn number of one run: shared by both ROMs."""

    #: Drawn values of the varied parameters, (draws,) each.
    parameters: dict[str, FloatArray]
    #: Fill byte per point and draw, (n_points, draws).
    fill_bytes: IntArray
    #: Declared parameters held at their frozen working value.
    held: tuple[str, ...]

    def overrides(self, d: int) -> dict[str, float]:
        return {pid: float(v[d]) for pid, v in self.parameters.items()}


def sample_draws(
    uncertainty: UncertaintyDeclaration,
    design: MonteCarloDesign,
    *,
    draws: int,
    seed: int,
    vary: Collection[str] | None = None,
) -> Draws:
    """The draws, in a fixed order: every declared parameter by sorted id
    (held ones are drawn too, so varying a subset leaves the others' stream
    unchanged), then the fill bytes, (draws, points)."""
    if draws < 1:
        raise SimulationRefusalError("draws_invalid", f"draws must be >= 1, got {draws}")
    declared = sorted(uncertainty.parameters)
    varied = set(declared) if vary is None else set(vary)
    unknown = sorted(varied - set(declared))
    if unknown:
        raise SimulationRefusalError(
            "parameter_unknown", f"{unknown} are not declared, so cannot be varied"
        )
    rng = Generator(PCG64(seed))
    drawn = {pid: _sample(uncertainty.parameters[pid], rng, draws) for pid in declared}
    lo = np.array([dp.fill_byte_lo for dp in design.points], dtype=np.int64)
    hi = np.array([dp.fill_byte_hi for dp in design.points], dtype=np.int64)
    fill = rng.integers(lo, hi, size=(draws, len(design.points)), endpoint=True)
    return Draws(
        parameters={pid: drawn[pid] for pid in declared if pid in varied},
        fill_bytes=np.ascontiguousarray(fill.T, dtype=np.int64),
        held=tuple(pid for pid in declared if pid not in varied),
    )


# -- the result ---------------------------------------------------------------------------


@dataclass
class MonteCarloResult:
    """Per point x draw x output: base and candidate (NaN: not computed, see
    the support), with support per point and draw. Always synthetic."""

    label: Literal["simulation"]
    synthetic: Literal[True]
    outputs: tuple[str, ...]
    weights: FloatArray
    fill_bytes: IntArray
    parameters: dict[str, FloatArray]
    base: dict[str, FloatArray]
    candidate: dict[str, FloatArray]
    #: Index into ``reason_sets`` per (point, draw); the empty set is supported.
    support_index: dict[str, NDArray[np.int32]]
    reason_sets: tuple[tuple[str, ...], ...]
    #: ``open_loop`` / ``closed_loop`` / ``closed_loop_setpoint_moved`` per (point, draw).
    loop: NDArray[np.str_]
    outcome: str
    rom_report: dict[str, Any]
    affected_cells: list[dict[str, Any]]
    traced_outputs: dict[str, dict[str, Any]]
    learned_state: dict[str, Any]
    plant: dict[str, Any]
    unresolved: list[str]
    assumptions: list[str]
    caveats: list[str]
    inert_parameters: list[str]
    coverage: dict[str, float]
    provenance: dict[str, Any] = field(default_factory=dict)

    @property
    def n_points(self) -> int:
        return int(self.fill_bytes.shape[0])

    @property
    def draws(self) -> int:
        return int(self.fill_bytes.shape[1])

    def delta(self, output: str) -> FloatArray:
        out: FloatArray = self.candidate[output] - self.base[output]
        return out

    def relative_delta(self, output: str) -> FloatArray:
        base, cand = self.base[output], self.candidate[output]
        with np.errstate(divide="ignore", invalid="ignore"):
            out: FloatArray = np.where(base != 0, cand / base - 1.0, np.nan)
        return out

    def supported(self, output: str) -> NDArray[np.bool_]:
        empty = np.array([not r for r in self.reason_sets], dtype=bool)
        out: NDArray[np.bool_] = empty[self.support_index[output]]
        return out

    def reasons(self, output: str, point: int, draw: int) -> tuple[str, ...]:
        return self.reason_sets[int(self.support_index[output][point, draw])]

    def point_support(self, point: int, output: str) -> list[dict[str, Any]]:
        """The support of one output at one point: one entry per distinct
        reason set over the draws (several only where the fill byte moves it)."""
        ids, counts = np.unique(self.support_index[output][point], return_counts=True)
        return [
            {
                "status": "unsupported" if self.reason_sets[int(i)] else "supported",
                "reasons": list(self.reason_sets[int(i)]),
                "draws": int(n),
            }
            for i, n in zip(ids, counts, strict=True)
        ]

    @property
    def session(self) -> str:
        return f"{SYNTHETIC_SESSION_PREFIX}{self.digest()[:12]}"

    def digest(self) -> str:
        """sha256 over every number and label the result carries."""
        h = hashlib.sha256()
        meta = {
            "provenance": self.provenance,
            "reason_sets": self.reason_sets,
            "caveats": self.caveats,
            "outputs": self.outputs,
        }
        h.update(json.dumps(meta, sort_keys=True, default=str).encode())
        h.update(self.weights.tobytes())
        h.update(self.fill_bytes.tobytes())
        h.update(self.loop.astype("U32").tobytes())
        for pid in sorted(self.parameters):
            h.update(pid.encode())
            h.update(self.parameters[pid].tobytes())
        for name in self.outputs:
            h.update(self.base[name].tobytes())
            h.update(self.candidate[name].tobytes())
            h.update(self.support_index[name].tobytes())
        return h.hexdigest()

    def traces(self) -> pd.DataFrame:
        """Long frame, one row per point x draw x output, under a ``_sim-*``
        session name that no identification, evaluation or memory path accepts."""
        n, d = self.n_points, self.draws
        point = np.repeat(np.arange(n), d)
        draw = np.tile(np.arange(d), n)
        frames = []
        for name in self.outputs:
            base = self.base[name].ravel()
            cand = self.candidate[name].ravel()
            ok = self.supported(name).ravel()
            frames.append(
                pd.DataFrame(
                    {
                        "point": point,
                        "draw": draw,
                        "output": name,
                        "fill_byte": self.fill_bytes.ravel(),
                        "base": base,
                        "candidate": cand,
                        "delta": self.delta(name).ravel(),
                        "relative_delta": self.relative_delta(name).ravel(),
                        "status": np.where(ok, "supported", "unsupported"),
                    }
                )
            )
        frame = pd.concat(frames, ignore_index=True)
        frame.insert(0, "session", self.session)
        frame.attrs["synthetic"] = True
        return frame


# -- running ------------------------------------------------------------------------------


def kit_for_plant(kit: SimKit, profile_dir: Path, plant_version: str | None) -> SimKit:
    """The kit with a named frozen plant version in place of the contract's
    (``None``: the kit as :func:`~src.analysis.paired_scenarios.load_kit`
    loaded it). The contract still decides which versions a scenario may run."""
    if plant_version is None:
        return kit
    plant, _ = load_fuel_plant(profile_dir, plant_version)
    if plant.baseline is None or not plant.baseline.frozen:
        raise SimulationRefusalError(
            "plant_not_frozen", f"{plant_version} has no frozen baseline parameter set"
        )
    return replace(
        kit,
        plant=plant,
        delay=load_b06(profile_dir, plant.delay),
        plant_file=f"{profile_dir.name}/{plant_files(profile_dir)[plant_version].name}",
    )


@dataclass(frozen=True)
class LoadedSimKit:
    """The simulator's kit (E08.11): the contract's kit on the plant
    ``sim_kit.yaml`` declares, with that plant's uncertainty declaration."""

    kit: SimKit
    uncertainty: LoadedUncertainty
    #: sha256 of ``sim_kit.yaml`` (provenance).
    declaration_sha256: str


def load_sim_kit(profile_dir: Path, plant_version: str | None = None) -> LoadedSimKit:
    """The one loader of the ROM-edit simulator's kit.

    The plant is the one ``<profile>/sim_kit.yaml`` declares, never the
    contract's default (which E07.4/E07.6 run); ``plant_version`` names
    another the declaration lists (e.g. ``fuel-mv-1`` to re-run a run recorded
    on it). The uncertainty declaration is that version's file, checked
    against the plant here (:func:`check_uncertainty`), so a kit and a
    declaration for different plants cannot leave this function together. The
    contract still decides which versions a scenario may run."""
    loaded = load_sim_kit_declaration(profile_dir)
    declared = loaded.declaration
    version = declared.plant_version if plant_version is None else plant_version
    if version not in declared.uncertainty:
        raise SimulationRefusalError(
            "uncertainty_undeclared",
            f"{SIM_KIT_FILE} declares uncertainty for {sorted(declared.uncertainty)}, not "
            f"{version}",
        )
    kit = kit_for_plant(load_kit(profile_dir), profile_dir, version)
    name = declared.uncertainty[version]
    try:
        uncertainty = load_uncertainty(Path(profile_dir) / name)
    except FileNotFoundError as exc:
        raise SimulationRefusalError(
            "uncertainty_missing", f"{Path(profile_dir).name} has no {name}"
        ) from exc
    except ValidationError as exc:
        raise SimulationRefusalError(
            "uncertainty_invalid", f"{name}: {exc.error_count()} error(s)"
        ) from exc
    check_uncertainty(uncertainty.declaration, kit)
    return LoadedSimKit(kit, uncertainty, loaded.sha256)


@dataclass
class _Cache:
    """The ECU side per (point, fill byte) combination, and what the plant reads."""

    point: IntArray
    byte: IntArray
    #: Per output: base and candidate values (NaN: None in run_paired).
    base: dict[str, FloatArray]
    candidate: dict[str, FloatArray]
    reason_id: dict[str, NDArray[np.int32]]
    loop: list[str]
    #: Plant inputs per side (0 base, 1 candidate) where the side's fuel chain ran.
    computed: NDArray[np.bool_]
    pulse_ms: FloatArray
    dead_ms: FloatArray


def _scenario(template: PairedScenario, points: Sequence[OperatingPoint]) -> PairedScenario:
    return replace(template, points=tuple(points))


def _with_byte(point: OperatingPoint, byte: int) -> OperatingPoint:
    return replace(point, conditioned={FILL_BYTE: float(byte)})


def _gate(
    kit: SimKit, template: PairedScenario, base_image: bytes, candidate_image: bytes
) -> list[GateReason]:
    """run_paired's run-level gate (``paired_scenarios.rom_gate``), each reason
    with the node it names, on the one quantity a design conditions every
    point on: the fill byte."""
    report = compare_scenario_roms(kit, template, base_image, candidate_image)
    if report.outcome == "unevaluable" and report.reason is not None:
        raise SimulationRefusalError(
            "rom_unevaluable", f"rom:{report.reason}: no point can be computed on either ROM"
        )
    fill = (FILL_BYTE, kit.contract.quantity(FILL_BYTE).manifest_node)
    gate, _, _ = rom_gate(kit, report, template.mode, [fill])
    return gate


def _pulses(
    kit: SimKit,
    side: _Side,
    ecus: Sequence[dict[str, Any]],
    points: Sequence[OperatingPoint],
    stft_pct: float,
) -> StatedPulse | None:
    """One side's pulse stage over every combination at once
    (``twin_runner.stated_pulse``, the chain run_paired runs per point; a
    combination with no phi word takes a placeholder whose words are unused)."""
    return stated_pulse(
        side.ecu,
        [stated_sample(kit, p, p.conditioned[FILL_BYTE]) for p in points],
        bfs=[e["base_fuel_schedule"] if e["target_phi_word"] is not None else 0 for e in ecus],
        phi_q14=[e["target_phi_word"] if e["target_phi_word"] is not None else 0 for e in ecus],
        stft_pct=stft_pct,
        ltft_pct=side.ltft_pct,
    )


def _build_cache(
    kit: SimKit,
    template: PairedScenario,
    design: MonteCarloDesign,
    draws: Draws,
    base_image: bytes,
    candidate_image: bytes,
    gated: Sequence[GateReason],
    learned: dict[str, Any],
) -> tuple[_Cache, IntArray, list[tuple[str, ...]]]:
    """Every (point, fill byte) the draws visit, and the (point, draw) ->
    combination index. Every piece is run_paired's own, none a copy (E09.2c):
    the ECU stages per combination through ``paired_scenarios._ecu_chain``
    (the twin runner's ``stated_ecu``), the pulse stage once per ROM over
    every combination (``twin_runner.stated_pulse``), the loop through
    ``_pair_loop`` and the reasons through ``output_reasons``; the assembled
    result is still re-checked against ``run_paired`` on every run."""
    base_side = _side(kit, base_image, template.base, learned["base"]["ltft_pct"], "base")
    cand_side = _side(
        kit, candidate_image, template.candidate, learned["candidate"]["ltft_pct"], "candidate"
    )
    stfts = (learned["base"]["stft_pct"], learned["candidate"]["stft_pct"])
    qualification = plant_qualification(kit.plant)
    gate = sorted({r for r, _ in gated})
    scope = ReasonScope.of(kit, gated)
    withheld = {name: bool(scope.keep(name, gate)) for name in OUTPUTS}
    plant_withheld = all(withheld[name] for name in _PLANT_OUTPUTS)
    n_points, n_draws = draws.fill_bytes.shape
    combo_of = np.empty((n_points, n_draws), dtype=np.int64)
    at: list[int] = []
    bytes_: list[int] = []
    for i in range(n_points):
        uniq, inverse = np.unique(draws.fill_bytes[i], return_inverse=True)
        combo_of[i] = len(at) + inverse
        at.extend([i] * len(uniq))
        bytes_.extend(int(v) for v in uniq)
    n = len(at)
    points = [_with_byte(design.points[at[c]].point, bytes_[c]) for c in range(n)]
    stages = stage_order(kit.model)
    b_ecus = [_ecu_chain(kit, stages, base_side, p) for p in points]
    c_ecus = [_ecu_chain(kit, stages, cand_side, p) for p in points]
    pulses = (
        _pulses(kit, base_side, b_ecus, points, stfts[0]),
        _pulses(kit, cand_side, c_ecus, points, stfts[1]),
    )
    sides = (base_side, cand_side)

    base = {name: np.full(n, np.nan) for name in OUTPUTS}
    cand = {name: np.full(n, np.nan) for name in OUTPUTS}
    reason_id = {name: np.zeros(n, dtype=np.int32) for name in OUTPUTS}
    reason_sets: list[tuple[str, ...]] = [()]
    interned: dict[tuple[str, ...], int] = {(): 0}
    loops: list[str] = []
    computed = np.zeros((2, n), dtype=bool)
    pulse = np.full((2, n), np.nan)
    dead = np.full((2, n), np.nan)

    def fuel_at(c: int) -> FuelFn:
        def fuel(s: int, ecu: dict[str, Any], hold: tuple[int, int] | None) -> dict[str, Any]:
            return _fuel_side(sides[s], ecu, pulses[s], c, stfts[s], hold)

        return fuel

    for c in range(n):
        point = points[c]

        # _pair_point's loop, with the plant deferred to the per-draw pass.
        pair = _pair_loop(kit, b_ecus[c], c_ecus[c], fuel_at(c))
        b, cd, loop = pair.base, pair.candidate, pair.loop
        loops.append(loop or "open_loop")
        # run_paired's per-output reasons.
        point_reasons = output_reasons(kit, scope, gate, qualification, point, pair)
        for name in OUTPUTS:
            key = tuple(point_reasons[name])
            if key not in interned:
                interned[key] = len(reason_sets)
                reason_sets.append(key)
            reason_id[name][c] = interned[key]
            if name in _PLANT_OUTPUTS:
                continue
            if b[name] is not None:
                base[name][c] = float(b[name])
            if not withheld[name] and cd[name] is not None:
                cand[name][c] = float(cd[name])
        for s, fuel in enumerate((b, cd)):
            if (s == 1 and plant_withheld) or fuel.get("dead_ms") is None:
                continue
            computed[s, c] = True
            pulse[s, c] = fuel["injector_pulse_ms"]
            dead[s, c] = fuel["dead_ms"]
    cache = _Cache(
        point=np.asarray(at, dtype=np.int64),
        byte=np.asarray(bytes_, dtype=np.int64),
        base=base,
        candidate=cand,
        reason_id=reason_id,
        loop=loops,
        computed=computed,
        pulse_ms=pulse,
        dead_ms=dead,
    )
    return cache, combo_of, reason_sets


@dataclass(frozen=True)
class _PointArrays:
    rpm: FloatArray
    maf_gps: FloatArray
    baro_kpa: FloatArray
    iat_c: FloatArray
    #: What the plant reads: NaN where no rail reading (its working value applies).
    rail_kpa: FloatArray
    map_kpa: FloatArray
    #: Where the plant's own manifold model computes MAP.
    map_from_model: NDArray[np.bool_]

    @classmethod
    def of(cls, design: MonteCarloDesign) -> _PointArrays:
        def col(name: str) -> FloatArray:
            return np.array([float(getattr(dp.point, name)) for dp in design.points])

        return cls(
            rpm=col("rpm"),
            maf_gps=col("maf_gps"),
            baro_kpa=col("baro_kpa"),
            iat_c=col("iat_c"),
            rail_kpa=np.array([plant_rail_kpa(dp.point) for dp in design.points]),
            map_kpa=col("map_kpa"),
            map_from_model=np.array(
                [dp.point.map_kpa_source == "plant_model" for dp in design.points], dtype=bool
            ),
        )


def _plant(
    kit: SimKit, cache: _Cache, points: _PointArrays, combos: IntArray, params: ResolvedParameters
) -> dict[str, FloatArray]:
    """The plant at ``combos`` (any shape) on one parameter vector, both ROMs
    in one vectorised call per MAP route (stated, or the plant's own model):
    ``{"<output>": (2, *shape)}``, NaN where a side's fuel chain did not run."""
    flat = combos.ravel()
    sides, where = np.nonzero(cache.computed[:, flat])
    idx = flat[where]
    at = cache.point[idx]
    result = {name: np.full((2, flat.size), np.nan) for name in _PLANT_OUTPUTS}
    model = points.map_from_model[at]
    for group in (~model, model):
        if not group.any():
            continue
        g_at, g_idx, g_sides = at[group], idx[group], sides[group]
        # The plant stage on the predicted pulses, in the replay's columns.
        out = plant_predict(
            stated_plant_samples(
                pulse_ms=cache.pulse_ms[g_sides, g_idx],
                ecu_dead_time_ms=cache.dead_ms[g_sides, g_idx],
                rpm=points.rpm[g_at],
                maf_gps=points.maf_gps[g_at],
                baro_kpa=points.baro_kpa[g_at],
                iat_c=points.iat_c[g_at],
                rail_kpa=points.rail_kpa[g_at],
            ),
            params,
            kit.plant,
            map_kpa=None if group is model else points.map_kpa[g_at],
        )
        result["delivered_lambda"][g_sides, where[group]] = out.lambda_delivered
        result["fuel_per_event_g"][g_sides, where[group]] = out.fuel_g
    return {name: v.reshape((2, *combos.shape)) for name, v in result.items()}


def _refuse_manifold_refusal(
    kit: SimKit, points: _PointArrays, params: ResolvedParameters, draw: int, sampled: Draws
) -> None:
    """The backstop for a plant whose manifold model refuses MAP per sample
    (fuel-mv-2/3): MAP at a ``plant_model`` point is recomputed at each draw,
    so a refusal there (above ambient, outside the VE fit) would make the
    point's support depend on the draw. Refused at the first such draw, by
    name (``manifold_refusal_varies_by_draw``). Designs place a point only
    where the frozen plant gives a MAP, and drawing the VE floor at warm idle
    (charge fraction ~0.15) cannot reach ambient, so it is not expected to fire."""
    if kit.plant.manifold_model not in REFUSING_MODELS or not points.map_from_model.any():
        return
    at = np.flatnonzero(points.map_from_model)
    out = plant_predict(
        stated_plant_samples(
            pulse_ms=np.full(at.size, 2.0),
            ecu_dead_time_ms=np.zeros(at.size),
            rpm=points.rpm[at],
            maf_gps=points.maf_gps[at],
            baro_kpa=points.baro_kpa[at],
            iat_c=points.iat_c[at],
            rail_kpa=points.rail_kpa[at],
        ),
        params,
        kit.plant,
    )
    assert out.manifold_refusal is not None
    refused = np.flatnonzero(out.manifold_refusal != "")
    if refused.size:
        k = int(refused[0])
        raise SimulationRefusalError(
            "manifold_refusal_varies_by_draw",
            f"{kit.plant.manifold_model} refuses MAP at point {int(at[k])} "
            f"({out.manifold_refusal[k]}, {refused.size} of {at.size} model-MAP points) at draw "
            f"{draw} ({sampled.overrides(draw)}): with MAP computed per draw a point's support "
            "would depend on the draw",
        )


def _same(a: float | None, b: float) -> bool:
    if a is None:
        return bool(np.isnan(b))
    return float(a) == b or (math.isnan(float(a)) and math.isnan(b))


def _check_pairs(cache: _Cache, combo_of: IntArray) -> list[tuple[int, int]]:
    """The (point, draw) pairs re-run through run_paired: one combination per
    loop kind first (each kind states its own assumptions), then one per
    distinct support signature, then any, up to ``SELF_CHECK_COMBOS``, each at
    the first draw that visits it; and the last draw at the first point."""
    picked: list[int] = [cache.loop.index(kind) for kind in sorted(set(cache.loop))]
    signature: dict[tuple[Any, ...], int] = {}
    for c in range(len(cache.point)):
        key = (cache.loop[c], *(int(cache.reason_id[o][c]) for o in OUTPUTS))
        signature.setdefault(key, c)
    for c in [*sorted(signature.values()), *range(len(cache.point))]:
        if len(picked) >= SELF_CHECK_COMBOS:
            break
        if c not in picked:
            picked.append(c)
    pairs = set()
    for c in picked:
        i = int(cache.point[c])
        pairs.add((i, int(np.flatnonzero(combo_of[i] == c)[0])))
    pairs.add((0, combo_of.shape[1] - 1))
    return sorted(pairs)


def _paired_at(
    kit: SimKit,
    template: PairedScenario,
    design: MonteCarloDesign,
    base_image: bytes,
    candidate_image: bytes,
    draws: Draws,
    pairs: Sequence[tuple[int, int]],
) -> dict[tuple[int, int], tuple[PairedResult, int]]:
    """run_paired itself at each pair: one run per draw, at that draw's
    parameters, over that draw's points at their drawn fill bytes."""
    out: dict[tuple[int, int], tuple[PairedResult, int]] = {}
    for d in sorted({d for _, d in pairs}):
        idx = [i for i, dd in pairs if dd == d]
        scenario = _scenario(
            template,
            [_with_byte(design.points[i].point, int(draws.fill_bytes[i, d])) for i in idx],
        )
        overrides = draws.overrides(d)
        paired = run_paired(
            kit, scenario, base_image, candidate_image, plant_overrides=overrides or None
        )
        for k, i in enumerate(idx):
            out[(i, d)] = (paired, k)
    return out


def _verify_assembled(
    checks: Mapping[tuple[int, int], tuple[PairedResult, int]],
    base: Mapping[str, FloatArray],
    cand: Mapping[str, FloatArray],
    support: Mapping[str, NDArray[np.int32]],
    reason_sets: Sequence[tuple[str, ...]],
) -> None:
    """The assembled result at each checked (point, draw) is run_paired's:
    base, candidate, reasons and the lambda relative delta, exactly."""
    diverged: list[str] = []
    for (i, d), (paired, k) in sorted(checks.items()):
        outputs = paired.points[k]["outputs"]
        for name in OUTPUTS:
            o = outputs[name]
            if not (_same(o["base"], base[name][i, d]) and _same(o["candidate"], cand[name][i, d])):
                diverged.append(f"{name}@({i},{d}): {o['base']}/{o['candidate']}")
            if tuple(o["reasons"]) != reason_sets[int(support[name][i, d])]:
                diverged.append(f"{name}@({i},{d}): reasons {o['reasons']}")
        lam = outputs["delivered_lambda"]
        if "relative_delta" in lam:
            mine = cand["delivered_lambda"][i, d] / base["delivered_lambda"][i, d] - 1.0
            if mine != lam["relative_delta"]:
                diverged.append(f"relative_delta@({i},{d})")
    if diverged:
        raise SimulationRefusalError(
            "fast_path_diverged",
            f"the assembled result differs from run_paired at {diverged[:5]}",
        )


def _inert(
    kit: SimKit,
    cache: _Cache,
    points: _PointArrays,
    uncertainty: UncertaintyDeclaration,
    varied: Sequence[str],
) -> list[str]:
    """Drawn parameters that move no plant output at any combination across
    their whole bracket (a stated boundary, or a quantity the steady slice
    does not read): their draws carry no information, and the result says so."""
    combos = np.arange(len(cache.point), dtype=np.int64)
    inert = []
    for pid in varied:
        lo, hi = uncertainty.parameters[pid].bracket
        at_lo = _plant(kit, cache, points, combos, plant_parameters(kit, {pid: lo}))
        at_hi = _plant(kit, cache, points, combos, plant_parameters(kit, {pid: hi}))
        if all(np.array_equal(at_lo[o], at_hi[o], equal_nan=True) for o in _PLANT_OUTPUTS):
            inert.append(pid)
    return inert


def _design_assumptions(design: MonteCarloDesign, *, ve_drawn: bool) -> list[str]:
    """The design's own assumptions, and, for points whose MAF the design
    solved through the plant's manifold model, that it is frozen there."""
    out = [f"{DESIGN_ASSUMPTION_PREFIX}{a}" for a in design.assumptions]
    if design.maf_frozen_points:
        out.append(
            f"{DESIGN_ASSUMPTION_PREFIX}the MAF of {design.maf_frozen_points} of "
            f"{len(design.points)} points was solved through the plant's manifold model at the "
            "design's VE when the design was built and is frozen there: this run's VE "
            f"({'drawn' if ve_drawn else 'held'}) moves the plant's MAP and lambda at a point, "
            "never its MAF, so BFS and the pulse rest on the design's VE"
        )
    return out


@refusing_as_simulation
def run_monte_carlo(
    kit: SimKit,
    template: PairedScenario,
    design: MonteCarloDesign,
    base_image: bytes,
    candidate_image: bytes,
    uncertainty: LoadedUncertainty,
    *,
    draws: int,
    seed: int,
    vary: Collection[str] | None = None,
) -> MonteCarloResult:
    """Both ROMs over every design point and draw.

    ``template`` is the paired scenario minus its points (ROM identities,
    plant identity, learned state, candidate events); the points come from
    ``design``. ``vary`` names the declared parameters to draw (default all);
    the rest are held at their frozen working values and named in the
    provenance and caveats."""
    if template.points:
        raise SimulationRefusalError(
            "template_has_points", "the points come from the design, never the template"
        )
    if template.mode != MODE:
        raise SimulationRefusalError(
            "not_a_paired_mode", f"a paired Monte Carlo runs {MODE}, not {template.mode}"
        )
    check_uncertainty(uncertainty.declaration, kit)
    _check_design(design, template.base.image_sha256)
    if design.plant_version is not None and design.plant_version != kit.plant.plant_version:
        raise SimulationRefusalError(
            "design_plant_mismatch",
            f"design {design.sha256[:12]} was built on plant {design.plant_version}; the run's "
            f"is {kit.plant.plant_version}",
        )
    for label, image, identity in (
        ("base", base_image, template.base),
        ("candidate", candidate_image, template.candidate),
    ):
        if sha256_hex(image) != identity.image_sha256:
            raise SimulationRefusalError(
                "image_hash_mismatch", f"{label} bytes are not image {identity.image_sha256}"
            )
    sampled = sample_draws(uncertainty.declaration, design, draws=draws, seed=seed, vary=vary)
    # The contract's and run_paired's per-point refusals, on every point.
    full = _scenario(template, [_with_byte(dp.point, dp.fill_byte_lo) for dp in design.points])
    _refuse_recorded_sources(full)
    _with_contract_mode(kit.contract, full, kit.contract_sha256)
    if (template.plant.version, template.plant.hardware_configuration) != (
        kit.plant.plant_version,
        kit.plant.hardware_configuration,
    ):
        raise SimulationRefusalError(
            "plant_mismatch",
            f"the scenario names plant {template.plant.version}; the kit's is "
            f"{kit.plant.plant_version}",
        )
    resets = _resets(kit.contract, template.candidate_events)
    base_ltft = template.learned.ltft_pct
    learned = {
        "base": {"stft_pct": template.learned.stft_pct, "ltft_pct": base_ltft},
        "candidate": {
            "stft_pct": 0.0 if "stft_b1" in resets else template.learned.stft_pct,
            "ltft_pct": 0.0 if "ltft_b1" in resets else base_ltft,
        },
    }
    gated = _gate(kit, template, base_image, candidate_image)
    gate = sorted({r for r, _ in gated})
    scope = ReasonScope.of(kit, gated)
    cache, combo_of, reason_sets = _build_cache(
        kit, template, design, sampled, base_image, candidate_image, gated, learned
    )
    points = _PointArrays.of(design)

    n_points, n_draws = combo_of.shape
    base = {name: np.empty((n_points, n_draws)) for name in OUTPUTS}
    cand = {name: np.empty((n_points, n_draws)) for name in OUTPUTS}
    for name in OUTPUTS:
        if name not in _PLANT_OUTPUTS:
            base[name][:] = cache.base[name][combo_of]
            cand[name][:] = cache.candidate[name][combo_of]
    for d in range(n_draws):
        params = plant_parameters(kit, sampled.overrides(d))
        _refuse_manifold_refusal(kit, points, params, d, sampled)
        plant = _plant(kit, cache, points, combo_of[:, d], params)
        for name in _PLANT_OUTPUTS:
            base[name][:, d] = plant[name][0]
            cand[name][:, d] = plant[name][1]
    for name in _PLANT_OUTPUTS:
        if scope.keep(name, gate):
            # A gate that reaches this output withholds the candidate, as run_paired.
            cand[name][:] = np.nan
    support = {name: cache.reason_id[name][combo_of] for name in OUTPUTS}
    # The assembled arrays themselves, checked against run_paired.
    checks = _paired_at(
        kit, template, design, base_image, candidate_image, sampled, _check_pairs(cache, combo_of)
    )
    _verify_assembled(checks, base, cand, support, reason_sets)
    empty = np.array([not r for r in reason_sets], dtype=bool)
    for name in OUTPUTS:
        ok = empty[support[name]]
        bad = ok & ~(np.isfinite(base[name]) & np.isfinite(cand[name]))
        if bad.any():
            i, d = (int(v) for v in np.argwhere(bad)[0])
            raise SimulationRefusalError(
                "support_varies_by_draw",
                f"{name} is supported at point {i} but not a number at draw {d} "
                f"({sampled.overrides(d)}): support must not depend on a plant draw",
            )

    first = next(iter(checks.values()))[0]
    qualification = plant_qualification(kit.plant)
    varied = sorted(sampled.parameters)
    assumptions: list[str] = []
    for paired, _ in checks.values():
        assumptions.extend(
            a
            for a in paired.assumptions
            if a not in assumptions and not a.startswith(PLANT_ASSUMPTION_PREFIX)
        )
    # The plant's working assumptions over the whole design (a checked subset
    # may miss a point that reads one), then the design's own (E08.10).
    assumptions.extend(
        plant_assumption_lines(kit, [dp.point for dp in design.points], drawn=varied)
    )
    assumptions.extend(_design_assumptions(design, ve_drawn=_VE_PARAMETER in varied))
    inert = _inert(kit, cache, points, uncertainty.declaration, varied)
    caveats = [STRUCTURAL_ERROR_NOT_SAMPLED]
    if not qualification.qualified:
        assert qualification.reason is not None
        caveats.append(qualification.reason)
    caveats.append("parameter_uncertainty_only")
    caveats.extend(f"parameter_held:{pid}" for pid in sampled.held)
    caveats.extend(f"parameter_inert:{pid}" for pid in inert)
    caveats.extend(assumptions)
    used = {int(v) for name in OUTPUTS for v in np.unique(support[name])}
    unresolved = set(gate)
    for k in used:
        unresolved.update(reason_sets[k])
    resolved = resolve_parameters(kit.plant, kit.delay)
    unresolved.update(f"plant:{e}" for e in resolved.effects)
    if any(dp.point.rail_kpa_source == "working_assumption" for dp in design.points):
        unresolved.add(f"plant:{resolved.rail_effect}")
    if qualification.reason is not None:
        unresolved.add(qualification.reason)
    provenance = {
        "tool": "sim_montecarlo",
        "tool_version": TOOL_VERSION,
        "paired_tool_version": PAIRED_TOOL_VERSION,
        "mode": template.mode,
        "base_image_sha256": template.base.image_sha256,
        "candidate_image_sha256": template.candidate.image_sha256,
        "candidate_events": list(template.candidate_events),
        "design_sha256": design.sha256,
        "uncertainty_sha256": uncertainty.sha256,
        "plant_version": kit.plant.plant_version,
        "baseline_sha256": qualification.baseline_sha256,
        "contract_sha256": kit.contract_sha256,
        "application_model_sha256": kit.model_sha256,
        "seed": seed,
        "draws": n_draws,
        "rng": "numpy.random.Generator(PCG64(seed)): declared parameters by sorted id, "
        "then fill bytes (draws x points)",
        "varied": varied,
        "held": list(sampled.held),
        "combinations": int(len(cache.point)),
        "self_checked_pairs": [list(p) for p in sorted(checks)],
        "point_sources": {
            name: dict(sorted(Counter(getattr(dp.point, f) for dp in design.points).items()))
            for name, f in (
                ("map_kpa", "map_kpa_source"),
                ("rail_kpa", "rail_kpa_source"),
                ("battery_v", "battery_v_source"),
            )
        },
    }
    n = n_points * n_draws
    return MonteCarloResult(
        label="simulation",
        synthetic=True,
        outputs=OUTPUTS,
        weights=np.array([dp.weight for dp in design.points], dtype=float),
        fill_bytes=sampled.fill_bytes,
        parameters=sampled.parameters,
        base=base,
        candidate=cand,
        support_index=support,
        reason_sets=tuple(reason_sets),
        loop=np.asarray(cache.loop, dtype=str)[combo_of],
        outcome=first.outcome,
        rom_report=first.rom_report,
        affected_cells=first.affected_cells,
        traced_outputs=first.traced_outputs,
        learned_state=first.learned_state,
        plant=first.plant,
        unresolved=sorted(unresolved),
        assumptions=assumptions,
        caveats=caveats,
        inert_parameters=inert,
        coverage={name: float(empty[support[name]].sum()) / n for name in OUTPUTS},
        provenance=provenance,
    )


__all__ = [
    "FILL_BYTE",
    "LUMPED_SCALE",
    "STRUCTURAL_ERROR_NOT_SAMPLED",
    "TOOL_VERSION",
    "UNCERTAINTY_FILE",
    "DESIGN_ASSUMPTION_PREFIX",
    "DesignPoint",
    "Draws",
    "LoadedUncertainty",
    "MonteCarloDesign",
    "MonteCarloResult",
    "ParameterUncertainty",
    "UncertaintyDeclaration",
    "bracketed_parameters",
    "check_uncertainty",
    "LoadedSimKit",
    "kit_for_plant",
    "load_sim_kit",
    "load_uncertainty",
    "run_monte_carlo",
    "sample_draws",
]
