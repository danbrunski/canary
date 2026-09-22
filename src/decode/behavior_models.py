"""Behavior-model schema & loader — the expected-behavior surfaces (Task 4.E.1).

A behavior model captures *what the engine should be doing right now* at a
given operating point. The runtime decoder evaluates the surface, the
deviation `actual − expected` becomes a feature for the IsolationForest /
LSTM (Task 4.E.7), and the dashboard's performance panel renders the
actual-vs-expected curve directly (Task 5.12). Rationale for inferring these
from runtime observation rather than reading the factory calibration tables
is in ``docs/behavior-model-inference.md``.

Two model kinds for v1:

* ``surface_1d`` — one input axis, one output. Fit as a polynomial
  (closed-form coefficients) or a sampled lookup table (for LOWESS / any
  non-polynomial fit where we only have ``(x, y)`` knots). Used for
  coolant-warmup curves, idle-stability curves, closed-loop convergence.
* ``surface_2d`` — two input axes, one output. Grid-binned mean per
  ``(axis_a_bin × axis_b_bin)`` cell; each cell carries mean / std / sample
  count so sparse coverage is visible at evaluation time. Used for fuel /
  AFR / VE / ignition surfaces.

Loaded from ``data/behavior_models.yaml`` (sibling to ``bindings.yaml``).
Strict Pydantic: typos in YAML fail at load, not at runtime. Hand-edited
during RE; auto-written by the refit CLI (4.E.8) once it exists.
"""

from __future__ import annotations

import logging
from collections.abc import Collection, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Annotated, Literal

import yaml
from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

log = logging.getLogger(__name__)

#: Current ``behavior_models.yaml`` document version (B01.6).
#:
#: * ``1`` — legacy artifacts. Models carry at most rate-of-change
#:   ``conditions``; closed-loop, engine-state and cold-start requirements
#:   lived only in the refit recipe and were never enforced at serve time.
#: * ``2`` — every refit-written model persists a typed
#:   :class:`ServingEligibility` contract that fitting, held-out scoring,
#:   batch decoding, live decoding and replay all evaluate through
#:   ``src.decode.behavior_eligibility``.
#:
#: Both load. A model without an ``eligibility`` contract (every schema-1
#: model, and any hand-written one) still evaluates with its legacy gates so
#: the deviation features existing IF/sequence artifacts consume keep their
#: meaning, but :func:`serving_status` reports it ``diagnostic_only`` with a
#: named reason — it is never silently promoted to a trusted alert source.
CURRENT_SCHEMA_VERSION = 2
SUPPORTED_SCHEMA_VERSIONS: frozenset[int] = frozenset({1, 2})

#: Default location of the hand-edited / refit-written behavior-model file.
#: Anchored to this file's repo (``src/decode/`` → up 3), not the process
#: cwd: the daemon must find this version-controlled file no matter where it
#: was launched (systemd ``cd``s to the clone, but Docker/other entrypoints
#: must not silently get an empty model set).
DEFAULT_BEHAVIOR_MODELS_PATH = Path(__file__).resolve().parents[2] / "data" / "behavior_models.yaml"


class _Strict(BaseModel):
    # Behavior models pre-date Pydantic's protected-namespace defaults that
    # would warn on `model_*` field names; explicitly turn that off so we can
    # use `model_*` if a future field needs it, like the alerts schema.
    model_config = ConfigDict(extra="forbid", protected_namespaces=())


# ---------------------------------------------------------------------------
# Axes
# ---------------------------------------------------------------------------


class Axis1D(_Strict):
    """One input axis: the channel a 1D surface is parameterized by.

    ``channel`` is the wide-frame column name the runtime sees after
    preprocessing (``obd:RUN_TIME``, ``decoded:ECM_COOLANT_TEMP``, …).
    ``domain`` is the inclusive range over which the fit is valid;
    evaluations outside this range return ``None`` from ``expected_value``.
    """

    name: str
    channel: str
    unit: str = "unknown"
    domain: tuple[float, float]

    @model_validator(mode="after")
    def _check_domain(self) -> Axis1D:
        lo, hi = self.domain
        if not lo < hi:
            raise ValueError(
                f"axis {self.name!r} domain must be (lo, hi) with lo<hi, got {self.domain}"
            )
        return self


class Axis2D(_Strict):
    """One input axis of a 2D surface — strictly-increasing bin edges.

    Bin ``i`` covers ``[bin_edges[i], bin_edges[i+1])`` (half-open, right-side
    open) except the final bin which is closed: ``[bin_edges[-2], bin_edges[-1]]``.
    Two consecutive edges with the same value would yield a zero-width bin
    that swallows nothing, so they're rejected.
    """

    name: str
    channel: str
    unit: str = "unknown"
    bin_edges: list[float] = Field(min_length=2)

    @model_validator(mode="after")
    def _check_edges(self) -> Axis2D:
        for prev, curr in zip(self.bin_edges, self.bin_edges[1:], strict=False):
            if not prev < curr:
                raise ValueError(
                    f"axis {self.name!r} bin_edges must be strictly increasing; "
                    f"got {self.bin_edges}"
                )
        return self

    @property
    def bin_count(self) -> int:
        return len(self.bin_edges) - 1


# ---------------------------------------------------------------------------
# Fits
# ---------------------------------------------------------------------------


class _BaseFit(_Strict):
    """Shared bookkeeping for any fit: where it came from + how good it is.

    ``training_data_hash`` is a stable hex digest of the training rows
    actually fed into the fit. Pairs with ``sessions`` for cache invalidation:
    same sessions but the underlying Parquet changed (a session-keying fix,
    a re-decode) → different hash → refit visible.
    """

    sample_count: int = Field(ge=0)
    r_squared: float | None = None  # None for grid fits where R² isn't well-defined
    sessions: list[str] = Field(default_factory=list)
    fitted_at: str  # ISO-8601 UTC; kept as str so the YAML round-trip is verbatim
    training_data_hash: str = ""
    #: Calibration identity shared by every training and held-out session.
    #: ``None`` is retained for vehicles without a revision registry; when a
    #: registry exists the refit CLI refuses mixed/conflicting revisions
    #: before a model can be written.
    tune_revision: str | None = None
    calibration_sessions: list[str] = Field(default_factory=list)
    validation_sample_count: int = Field(default=0, ge=0)
    validation_coverage: float | None = Field(default=None, ge=0, le=1)
    validation_warning_rate: float | None = Field(default=None, ge=0, le=1)
    validation_critical_rate: float | None = Field(default=None, ge=0, le=1)
    validation_residual_median: float | None = None
    validation_residual_mad: float | None = Field(default=None, ge=0)
    validation_r_squared: float | None = None


class PolynomialFit(_BaseFit):
    """1D polynomial fit: ``y = c0 + c1*x + c2*x^2 + …``.

    ``coefficients`` is lowest-order first (numpy.poly1d / numpy.polynomial
    convention). Order is implied by length; we don't separately store it.
    """

    kind: Literal["poly"]
    coefficients: list[float] = Field(min_length=1)


class LookupTableFit(_BaseFit):
    """1D sampled fit (LOWESS, splines, anything non-polynomial).

    Just an ``(x, y)`` table the runtime linearly interpolates between.
    ``x_knots`` must be strictly increasing.
    """

    kind: Literal["lookup_table"]
    x_knots: list[float] = Field(min_length=2)
    y_knots: list[float] = Field(min_length=2)

    @model_validator(mode="after")
    def _check_knots(self) -> LookupTableFit:
        if len(self.x_knots) != len(self.y_knots):
            raise ValueError(
                f"x_knots ({len(self.x_knots)}) and y_knots ({len(self.y_knots)}) must match length"
            )
        for prev, curr in zip(self.x_knots, self.x_knots[1:], strict=False):
            if not prev < curr:
                raise ValueError("x_knots must be strictly increasing")
        return self


class RobustBaselineFit(_BaseFit):
    """Mode-conditioned robust location/spread without a claimed x relationship."""

    kind: Literal["robust_baseline"]
    median: float
    mad: float = Field(ge=0)
    p01: float
    p99: float


Fit1D = Annotated[PolynomialFit | LookupTableFit | RobustBaselineFit, Field(discriminator="kind")]


class GridCell(_Strict):
    """One cell of a 2D grid-binned-mean fit.

    ``count == 0`` is the "empty cell" — operating point never observed
    during the fit. Evaluating a 2D surface at an empty cell returns
    ``None`` so the runtime can distinguish "no expectation" from
    "expectation of 0".
    """

    mean: float
    std: float = Field(ge=0)
    count: int = Field(ge=0)
    session_count: int = Field(default=1, ge=0)
    dwell_seconds: float = Field(default=0.0, ge=0)


class GridBinnedMeanFit(_BaseFit):
    """2D grid-binned mean fit. Row-major, ``axes[0]`` is the outer index.

    Length must equal ``axes[0].bin_count * axes[1].bin_count``. The
    enclosing ``BehaviorModel`` enforces that — it knows both axes; this
    object only sees the flat list.
    """

    kind: Literal["grid_binned_mean"]
    cells: list[GridCell] = Field(min_length=1)


Fit2D = Annotated[GridBinnedMeanFit, Field(discriminator="kind")]


# ---------------------------------------------------------------------------
# Deviation bands
# ---------------------------------------------------------------------------


class DeviationConfig(_Strict):
    """When does the runtime treat ``actual − expected`` as an anomaly?

    ``absolute`` — fixed thresholds on ``|actual − expected|`` in output units.
    ``sigma`` — multiples of the local std (so per-cell std for 2D grids;
    1D fits use ``reference_std``, which the fitters auto-fill from the
    residual std when not declared). ``warning_band`` < ``critical_band``
    is required; either can be ``None`` to suppress that band.

    9.4 — ``sigma`` was named ``relative`` through the 2026-07 review, which
    invited misreading it as "percent of expected" (it isn't; it's a
    z-score against the local std). ``relative`` still loads — the
    before-validator below aliases it to ``sigma`` with a deprecation log —
    so shipped configs/refit artifacts using the old name keep working
    unchanged; write new ones as ``sigma``.

    ``sigma_floor`` (sigma kind, and directional kind when ``std_scaled``)
    is a lower bound on the local std, in output units. A cell fit on a
    quantization-flat stretch of data can carry a std below the sensor's
    LSB (the May 2026 IAT surface shipped a 0.13° cell against a 0.5°/LSB
    PID — one LSB flicker fired warning) or exactly 0.0 (which silently
    disabled alerting in that cell). Set it to roughly the output's
    quantization step; both failure modes go away.

    ``directional`` (2.1, online-training-strategy.md Phase 2) — a known,
    *accepted* fault is not a mute, it's a recentering with a one-sided
    band (see the doc's "Directional overrides — the schema gap" section).
    ``tolerance`` is the ± wander around the center that never alerts
    regardless of direction; past that, only ``worse_direction`` (the sign
    of ``actual − center`` that means "the fault is worsening") is graded
    against ``warning_band``/``critical_band`` (read directly off the
    center, same ``score >= band`` convention as absolute/sigma); the
    opposite direction is ``better_direction: ignore`` — the only value
    today — and never alerts no matter how far it goes, which is what stops
    improvement/measurement noise from generating false alarms. The
    center is ``accepted_center`` when set; when it's ``None`` (the usual
    case for a ``behavior_model``-targeted override, whose center is the
    surface's own fitted expectation, not a fixed number) the caller
    supplies the delta already recentered by the model's ``expected_value``
    — see ``src.decode.behavior_decoder``. ``std_scaled`` opts a directional
    band into the same local-std scaling ``sigma`` uses (and then requires
    the same positive ``sigma_floor``); the default (``False``) grades in
    plain output units, matching ``accepted_baselines.yaml``'s
    ``DirectionalBand`` (which has no std concept at all).
    """

    kind: Literal["absolute", "sigma", "directional"]
    warning_band: float | None = Field(default=None, ge=0)
    critical_band: float | None = Field(default=None, ge=0)
    reference_std: float | None = None  # for kind=sigma on 1D fits (auto-filled at fit time)
    sigma_floor: float | None = Field(default=None, ge=0)
    #: directional kind only (2.1) — recenter point; ``None`` means "use the
    #: model's own fitted expected value" (behavior_model-targeted override);
    #: a fixed number is a raw-channel override, which has no fitted surface
    #: to read a center from.
    accepted_center: float | None = None
    #: directional kind only — ± wander around the center that never alerts.
    tolerance: float | None = Field(default=None, ge=0)
    #: directional kind only — which sign of (actual − center) is "worsening".
    worse_direction: Literal["positive", "negative"] | None = None
    #: directional kind only — the other direction; "ignore" (the only value
    #: today) is what stops improvement/noise from ever alerting.
    better_direction: Literal["ignore"] = "ignore"
    #: directional kind only — scale the band by the local std like ``sigma``
    #: does, instead of grading in plain output units.
    std_scaled: bool = False

    @field_validator("kind", mode="before")
    @classmethod
    def _alias_relative(cls, value: object) -> object:
        # 9.4 — deprecated alias for "sigma"; kept accepted (not just
        # documented) so existing behavior_models.yaml artifacts and Pi
        # deploys don't need a coordinated rewrite to keep loading.
        if value == "relative":
            log.warning(
                "DeviationConfig kind='relative' is deprecated — it means "
                "sigma-scaled (z-score), not percent-of-expected; rename to "
                "kind='sigma' in the source YAML/refit recipe."
            )
            return "sigma"
        return value

    @model_validator(mode="after")
    def _check_bands(self) -> DeviationConfig:
        if self.warning_band is None and self.critical_band is None:
            raise ValueError("at least one of warning_band / critical_band must be set")
        if (
            self.warning_band is not None
            and self.critical_band is not None
            and self.warning_band >= self.critical_band
        ):
            raise ValueError("warning_band must be < critical_band")
        return self

    @model_validator(mode="after")
    def _check_sigma_floor(self) -> DeviationConfig:
        # 4.2 — a sigma band with no floor (or a floor of 0) is a live
        # hair-trigger: a cell/fit fit on quantization-flat data can carry a
        # std below the sensor's LSB (or exactly 0.0), and the May 2026 IAT
        # surface shipped exactly that. Schema-enforce it instead of relying
        # on every recipe author remembering the convention. 2.1 — a
        # directional band opted into the same std scaling
        # (``std_scaled=True``) is exactly as exposed to the same
        # degenerate-std failure, so mirror the rule for it too.
        needs_floor = self.kind == "sigma" or (self.kind == "directional" and self.std_scaled)
        if needs_floor and not (self.sigma_floor is not None and self.sigma_floor > 0):
            raise ValueError(
                "kind='sigma' (or a std_scaled directional band) requires a positive "
                "sigma_floor — a cell/fit std can collapse to (near) zero on "
                "quantization-flat data and hair-trigger without one; set it to "
                "roughly the output's sensor LSB"
            )
        return self

    @model_validator(mode="after")
    def _check_directional_fields(self) -> DeviationConfig:
        # 2.1 — the directional fields are meaningless without tolerance +
        # worse_direction; require both explicitly rather than silently
        # grading against a `None` that would compare unequal to everything.
        if self.kind != "directional":
            return self
        if self.tolerance is None:
            raise ValueError("kind='directional' requires tolerance")
        if self.worse_direction is None:
            raise ValueError("kind='directional' requires worse_direction")
        bands = [b for b in (self.warning_band, self.critical_band) if b is not None]
        if bands and self.tolerance > min(bands):
            raise ValueError(
                "tolerance must not exceed the smallest configured band — a wider "
                "wander zone would swallow the band it's supposed to sit inside"
            )
        return self


# ---------------------------------------------------------------------------
# Models
# ---------------------------------------------------------------------------


class RateOfChangeCondition(_Strict):
    """A persisted serve-time steady-state gate.

    Training only on steady rows is insufficient: without the same condition
    here, the daemon would grade command transitions against a steady-state
    surface and manufacture alerts.  The condition names the already-derived
    ``__roc`` feature explicitly so missing derivative data fails closed.
    """

    kind: Literal["max_abs_rate_of_change"] = "max_abs_rate_of_change"
    channel: str
    max_abs_per_second: float = Field(gt=0)


#: SAE J1979 Mode 01 PID 0x03 fuel-system status (first byte), by the single
#: bit a conforming ECU sets. Standard, not vehicle-specific. Membership is
#: always an explicit allowlist of these codes: a nonzero status is not
#: "closed loop" (0x04 is open loop for load *or* decel fuel cut, 0x10 is
#: closed loop with a feedback fault).
FUEL_STATUS_STATES: dict[int, str] = {
    0x01: "open_loop_insufficient_temperature",
    0x02: "closed_loop",
    0x04: "open_loop_load_or_fuel_cut",
    0x08: "open_loop_system_failure",
    0x10: "closed_loop_feedback_fault",
}

#: Healthy closed-loop feedback only. ``0x10`` is excluded on purpose: a
#: model of normal trims must not learn from, or grade, rows the ECU itself
#: reports as having a faulty feedback loop.
CLOSED_LOOP_FUEL_STATES: tuple[int, ...] = (0x02,)


def fuel_status_name(code: float) -> str:
    """Stable name for a fuel-status value (``invalid(<n>)`` if not J1979)."""
    as_int = int(code)
    if float(as_int) == float(code) and as_int in FUEL_STATUS_STATES:
        return FUEL_STATUS_STATES[as_int]
    return f"invalid({code:g})"


class EngineStateRequirement(_Strict):
    """Which engine state a model may be served in (B01.3).

    ``engine_running`` requires a fresh ``rpm_channel`` at or above
    ``min_running_rpm`` — missing or stale RPM refuses with a named reason
    rather than guessing. ``any`` also admits key-on/engine-off rows; it is
    the correct choice for relationships that do not depend on combustion
    (redundant TPS tracks legitimately calibrate best from KOEO sweeps), so
    engine-running is never a global default.
    """

    state: Literal["engine_running", "any"]
    rpm_channel: str = "obd:RPM"
    min_running_rpm: float = Field(default=400.0, gt=0)


class FuelControlRequirement(_Strict):
    """Explicit fuel-status membership required on the served row."""

    channel: str = "obd:FUEL_STATUS"
    allowed_states: list[int] = Field(min_length=1)

    @field_validator("allowed_states")
    @classmethod
    def _known_states(cls, value: list[int]) -> list[int]:
        unknown = [code for code in value if code not in FUEL_STATUS_STATES]
        if unknown:
            raise ValueError(
                f"allowed_states must be J1979 fuel-status codes "
                f"{sorted(FUEL_STATUS_STATES)}; got unknown {unknown}"
            )
        return sorted(set(value))


class ColdStartRequirement(_Strict):
    """Served rows must belong to an *observed* engine start that began cold.

    The initial temperature is session context, not a row value: it comes
    from ``src.preprocessing.session_context``, which records the coolant
    reading at an engine start it actually saw (an RPM transition from
    stopped to running, or a run-time clock no older than
    ``max_start_runtime_s``). A mid-drive daemon start, a replay that begins
    mid-drive, or a reconnect that cannot place the start has no evidence
    and refuses — an unknown initial temperature is never read as cold.
    """

    rpm_channel: str = "obd:RPM"
    runtime_channel: str = "obd:RUN_TIME"
    coolant_channel: str = "obd:COOLANT_TEMP"
    min_running_rpm: float = Field(default=400.0, gt=0)
    max_start_runtime_s: float = Field(default=60.0, gt=0)
    max_initial_coolant_c: float


class RequiredContext(_Strict):
    """A measured quantity the model's meaning depends on.

    At least one channel in ``any_of`` must be present and fresh on the
    served row. Exists so a derived output cannot silently switch to a
    substituted constant (VE falls back to 25 °C when IAT is absent).
    """

    any_of: list[str] = Field(min_length=1)
    note: str | None = None


class TrainingSelection(_Strict):
    """Corpus rules applied only when *choosing training data* (B01.3).

    Provenance, never evaluated at serve time: whether a whole recording
    carried a detected event, or had an unbroken run-time clock, says which
    drives were good evidence, not whether the current row is eligible.
    Per-row serving rules live on :class:`ServingEligibility`.
    """

    require_session_events: list[str] = Field(default_factory=list)
    require_contiguous_runtime: bool = False
    min_sessions: int = Field(default=1, ge=1)


class ServingEligibility(_Strict):
    """The typed per-observation serving contract (B01.3, schema 2).

    Evaluated by ``src.decode.behavior_eligibility`` — the single evaluator
    used for fitting rows, held-out scoring, vectorized and scalar decoding
    and replay. Rate-of-change limits remain on the model's ``conditions``
    list (unchanged API field) and are part of the same evaluation.
    """

    engine_state: EngineStateRequirement
    fuel_control: FuelControlRequirement | None = None
    cold_start: ColdStartRequirement | None = None
    required_context: list[RequiredContext] = Field(default_factory=list)
    training_selection: TrainingSelection = Field(default_factory=TrainingSelection)


class BehaviorModel1D(_Strict):
    """A 1D expected-behavior surface."""

    name: str
    kind: Literal["surface_1d"]
    output: str
    output_unit: str = "unknown"
    axis: Axis1D
    operating_mode: str | None = None  # restrict evaluation to this mode (e.g. "warmup")
    conditions: list[RateOfChangeCondition] = Field(default_factory=list)
    #: B01.3 serving contract; ``None`` marks a legacy (schema-1 or
    #: hand-written) artifact, which :func:`serving_status` reports as
    #: diagnostic-only.
    eligibility: ServingEligibility | None = None
    version: int = 1
    fit: Fit1D
    deviation: DeviationConfig | None = None
    notes: str | None = None
    diagnostic_capabilities: list[str] = Field(default_factory=list)
    diagnostic_limitations: list[str] = Field(default_factory=list)

    @model_validator(mode="after")
    def _check_deviation(self) -> BehaviorModel1D:
        if self.deviation is None:
            return self
        needs_reference_std = self.deviation.kind == "sigma" or (
            self.deviation.kind == "directional" and self.deviation.std_scaled
        )
        if needs_reference_std and self.deviation.reference_std is None:
            raise ValueError(
                f"{self.name}: sigma (or std_scaled directional) deviation on a 1D "
                "surface needs reference_std"
            )
        return self


class BehaviorModel2D(_Strict):
    """A 2D expected-behavior surface."""

    name: str
    kind: Literal["surface_2d"]
    output: str
    output_unit: str = "unknown"
    axes: tuple[Axis2D, Axis2D]
    operating_mode: str | None = None
    conditions: list[RateOfChangeCondition] = Field(default_factory=list)
    eligibility: ServingEligibility | None = None
    version: int = 1
    fit: Fit2D
    deviation: DeviationConfig | None = None
    notes: str | None = None
    diagnostic_capabilities: list[str] = Field(default_factory=list)
    diagnostic_limitations: list[str] = Field(default_factory=list)
    #: Cells with fewer fit samples than this evaluate to ``None`` (off-map)
    #: instead of grading against a near-meaningless mean/std. Default 1
    #: keeps pre-existing YAML artifacts (which only excluded count==0)
    #: loading with unchanged behavior.
    min_cell_count: int = Field(default=1, ge=1)
    min_cell_sessions: int = Field(default=1, ge=1)
    min_cell_dwell_seconds: float = Field(default=0.0, ge=0)
    #: 4.3 — bumped only when the grid geometry (bin edges) actually
    #: changes, distinct from ``version`` which bumps on every successful
    #: refit. The refit driver persists edges across refits by default so
    #: cross-version drift comparison reads the same operating rectangles;
    #: an explicit re-grid recomputes edges *and* bumps this.
    grid_version: int = Field(default=1, ge=1)

    @model_validator(mode="after")
    def _check_cells(self) -> BehaviorModel2D:
        expected = self.axes[0].bin_count * self.axes[1].bin_count
        if len(self.fit.cells) != expected:
            raise ValueError(
                f"{self.name}: expected {expected} cells "
                f"({self.axes[0].bin_count}×{self.axes[1].bin_count}); got {len(self.fit.cells)}"
            )
        return self


BehaviorModel = Annotated[BehaviorModel1D | BehaviorModel2D, Field(discriminator="kind")]


class BehaviorModels(_Strict):
    """Top-level behavior_models.yaml document."""

    schema_version: int
    models: list[BehaviorModel] = Field(default_factory=list)

    @field_validator("schema_version")
    @classmethod
    def _supported_version(cls, value: int) -> int:
        if value not in SUPPORTED_SCHEMA_VERSIONS:
            raise ValueError(
                f"behavior_models schema_version {value} is not supported "
                f"(supported: {sorted(SUPPORTED_SCHEMA_VERSIONS)})"
            )
        return value

    def by_name(self, name: str) -> BehaviorModel1D | BehaviorModel2D | None:
        for model in self.models:
            if model.name == name:
                return model
        return None


#: Named reasons a model is not a trusted alert source (B01.6). Stable
#: strings: the daemon, dashboard and MCP surface them verbatim.
STATUS_LEGACY_NO_CONTRACT = "legacy_artifact:no_serving_eligibility_contract"
STATUS_NO_HELD_OUT_VALIDATION = "unvalidated:no_held_out_evidence"


@dataclass(frozen=True)
class ServingStatus:
    """Whether a loaded model may raise trusted alerts, and why not.

    ``diagnostic_only`` models still evaluate — the live overlay, session
    analysis and the ``deviation:*`` feature columns existing detectors were
    trained on keep working — but the daemon never emits or fuses their
    alerts. B03 extends this with explicit demotion/quarantine policy; the
    reasons here are the ones derivable from the artifact alone.
    """

    state: Literal["active", "diagnostic_only"]
    reasons: tuple[str, ...] = ()

    @property
    def alerting(self) -> bool:
        return self.state == "active"

    def to_dict(self) -> dict[str, object]:
        return {"state": self.state, "reasons": list(self.reasons)}


def serving_status(model: BehaviorModel1D | BehaviorModel2D) -> ServingStatus:
    """Classify a loaded artifact (B01.6) without blessing missing metadata.

    A model lacking the schema-2 eligibility contract, or lacking held-out
    validation evidence, is ``diagnostic_only`` with every applicable reason
    named. Loading never upgrades a model; only a refit that writes both can.
    """
    reasons: list[str] = []
    if model.eligibility is None:
        reasons.append(STATUS_LEGACY_NO_CONTRACT)
    if model.fit.validation_sample_count <= 0 or not model.fit.calibration_sessions:
        reasons.append(STATUS_NO_HELD_OUT_VALIDATION)
    if reasons:
        return ServingStatus("diagnostic_only", tuple(reasons))
    return ServingStatus("active")


def diagnostic_only_models(models: BehaviorModels) -> dict[str, list[str]]:
    """``{model_name: reasons}`` for every model that cannot alert."""
    out: dict[str, list[str]] = {}
    for model in models.models:
        status = serving_status(model)
        if not status.alerting:
            out[model.name] = list(status.reasons)
    return out


def load_behavior_models(path: Path) -> BehaviorModels:
    """Load and validate a ``behavior_models.yaml`` file."""
    raw = yaml.safe_load(path.read_text())
    return BehaviorModels.model_validate(raw)


def unknown_operating_modes(
    models: BehaviorModels,
    configured_modes: Collection[str],
) -> dict[str, str]:
    """Catalog models whose ``operating_mode`` the active segmenter can't emit.

    Returns ``{model_name: operating_mode}`` for every mismatch. A model
    gated on a mode outside the configured vocabulary silently never
    evaluates — the May 2026 warmup surfaces returned ``None`` on every
    live tick because the daemon config lacked the ``warmup`` mode the
    training config defined. Callers (daemon model load/hot-reload, refit
    CLI) should log these loudly; an empty dict means full coverage.
    """
    configured = set(configured_modes)
    return {
        model.name: model.operating_mode
        for model in models.models
        if model.operating_mode is not None and model.operating_mode not in configured
    }


def dump_behavior_models(models: BehaviorModels, path: Path) -> None:
    """Serialize a ``BehaviorModels`` object back to YAML.

    Used by the refit CLI (Task 4.E.8). Keeps the field order stable
    (``yaml.safe_dump`` with ``sort_keys=False``) so diffs stay readable.
    """
    payload = models.model_dump(exclude_none=True)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(yaml.safe_dump(payload, sort_keys=False, default_flow_style=False))


# ---------------------------------------------------------------------------
# Convenience helpers used by the inferers (4.E.2, 4.E.3) when constructing
# models from scratch. Centralized here so the schema invariants live in one
# place and the inferers don't have to know the row-major flattening rule.
# ---------------------------------------------------------------------------


def flatten_grid(rows: Sequence[Sequence[GridCell]]) -> list[GridCell]:
    """Row-major flatten of a 2D cell grid, validating the shape is rectangular."""
    if not rows:
        raise ValueError("grid must have at least one row")
    width = len(rows[0])
    if width == 0:
        raise ValueError("grid rows must have at least one cell")
    out: list[GridCell] = []
    for r, row in enumerate(rows):
        if len(row) != width:
            raise ValueError(f"grid row {r} has width {len(row)}, expected {width}")
        out.extend(row)
    return out
