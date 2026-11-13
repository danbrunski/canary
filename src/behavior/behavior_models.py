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

import hashlib
import json
import logging
from collections.abc import Collection, Sequence
from dataclasses import dataclass
from enum import StrEnum
from pathlib import Path
from typing import Annotated, Literal

import yaml
from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    PrivateAttr,
    field_validator,
    model_validator,
)

from src.operating import (
    ENGINE_RUNNING_MIN_RPM,
    FUEL_STATUS_STATES,
)
from src.paths import DATA_DIR
from src.telemetry import source_of
from src.trainability import source_exclusion_reason
from src.yaml_load import load_yaml

log = logging.getLogger(__name__)

#: Current ``behavior_models.yaml`` document version (B01.6).
#:
#: * ``1`` — legacy artifacts. Models carry at most rate-of-change
#:   ``conditions``; closed-loop, engine-state and cold-start requirements
#:   lived only in the refit recipe and were never enforced at serve time.
#: * ``2`` — every refit-written model persists a typed
#:   :class:`ServingEligibility` contract that fitting, held-out scoring,
#:   batch decoding, live decoding and replay all evaluate through
#:   ``src.behavior.behavior_eligibility``.
#:
#: Both load. A model without an ``eligibility`` contract (every schema-1
#: model, and any hand-written one) still evaluates with its legacy gates so
#: the deviation features existing IF/sequence artifacts consume keep their
#: meaning, but :func:`serving_status` reports it ``diagnostic_only`` with a
#: named reason — it is never silently promoted to a trusted alert source.
CURRENT_SCHEMA_VERSION = 2
SUPPORTED_SCHEMA_VERSIONS: frozenset[int] = frozenset({1, 2})

#: There is deliberately no module-level default path (P02.12). The one that
#: stood here named the repo-level ``data/behavior_models.yaml``, which no
#: vehicle profile has: the artifact lives at the profile-scoped
#: ``config.daemon.behavior_models``. Trainers, replay and the dashboard fell
#: back to it, and trainer frames carried no ``deviation:*`` columns while the
#: daemon's did. Read the path from the (scoped) config.


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
    — see ``src.behavior.behavior_decoder``. ``std_scaled`` opts a directional
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
    min_running_rpm: float = Field(default=ENGINE_RUNNING_MIN_RPM, gt=0)


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
    from ``src.behavior.session_context``, which records the coolant
    reading at an engine start it actually saw (an RPM transition from
    stopped to running, or a run-time clock no older than
    ``max_start_runtime_s``). A mid-drive daemon start, a replay that begins
    mid-drive, or a reconnect that cannot place the start has no evidence
    and refuses — an unknown initial temperature is never read as cold.
    """

    rpm_channel: str = "obd:RPM"
    runtime_channel: str = "obd:RUN_TIME"
    coolant_channel: str = "obd:COOLANT_TEMP"
    min_running_rpm: float = Field(default=ENGINE_RUNNING_MIN_RPM, gt=0)
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


class LocalRangeCondition(_Strict):
    """A steady-state limit on a channel's *local* range (B07.6, B01 schema).

    A persisted rate gate reads ``<channel>__roc``, the net change over the
    30 s feature horizon, so an up-and-back excursion inside the horizon
    passes it: on 2026-09-23 two rpm blips (725 -> 938 -> 750 in ~10 s) were
    graded as warm-idle deviations through a 10 rpm/s gate. This condition
    bounds ``max - min`` of ``channel`` over the trailing ``window_seconds``
    (rows with ``t - window < t_row <= t``). The value is serving context,
    built by :mod:`src.behavior.local_range` on the batch and the live
    paths alike, and refuses when the window is not fully observed.
    """

    channel: str = Field(min_length=1)
    window_seconds: float = Field(gt=0)
    max_range: float = Field(gt=0)
    #: Where the limit came from (a measurement, a definition, a decision).
    basis: str = Field(min_length=1)


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

    Evaluated by ``src.behavior.behavior_eligibility`` — the single evaluator
    used for fitting rows, held-out scoring, vectorized and scalar decoding
    and replay. Rate-of-change limits remain on the model's ``conditions``
    list (unchanged API field) and are part of the same evaluation.
    """

    engine_state: EngineStateRequirement
    fuel_control: FuelControlRequirement | None = None
    cold_start: ColdStartRequirement | None = None
    required_context: list[RequiredContext] = Field(default_factory=list)
    #: Operating modes whose rows the model never speaks for (B05.2): a
    #: closed-loop total-trim surface excludes ``warmup``, where the ECM's
    #: cold enrichment and inhibited trim learning make the same trim value a
    #: different quantity. A row with no mode label is refused, not assumed
    #: to be outside the excluded set.
    excluded_operating_modes: list[str] = Field(default_factory=list)
    #: B07.6 — local-range steady limits (see :class:`LocalRangeCondition`).
    local_ranges: list[LocalRangeCondition] = Field(default_factory=list)
    training_selection: TrainingSelection = Field(default_factory=TrainingSelection)


class BandExcessPersistence(_Strict):
    """B05.3 — event persistence by accumulated excess beyond the warning band.

    The daemon's default gate counts band exceedances (M of N ticks) and is
    blind to how far past the band a residual sits. A smoothed bank-balance
    signal drifts just past its band for 10-25 s during ordinary closed-loop
    control (and while warmup trims wash out of its 30 s mean), which latches
    3-of-5 every time: 2.86 events/h on the 2026-09-23 city test drives, with
    runs of 17-18 warning ticks on both folds. A real bank asymmetry is a
    sustained shift, and a shift of the declared size lands well past the band.

    The rule is Page's two-sided CUSUM on the excess over the warning
    half-width ``w`` at each *evaluated* tick::

        high = max(0, high + r - w)      low = max(0, low - r - w)

    sustained while ``max(high, low)`` is strictly above the fitted limit
    (output units x evaluated ticks). A refused tick leaves both sums
    untouched: no measurement is not a clean one. A gap longer than
    ``reset_gap_seconds`` between two evaluated ticks restarts them, so
    evidence from before the gap never completes a later event. Declared
    here is the form; the limit is fitted on the training fold only
    (:class:`FittedBandExcessPersistence`) and applied identically by the
    daemon (``PersistenceGate.observe_band_excess``) and the B02 evaluator.
    """

    kind: Literal["band_excess"] = "band_excess"
    reset_gap_seconds: float = Field(gt=0)
    #: Why this gap (a measured memory, a signal's smoothing window ...).
    basis: str = Field(min_length=1)


class FittedBandExcessPersistence(_Strict):
    """A :class:`BandExcessPersistence` with its limit fitted on training drives.

    ``limit`` is the largest accumulated excess the training fold's own
    no-fault rows reached, so no training excursion completes an event; an
    event needs strictly more. A training fold with no excursion beyond the
    band cannot say how large ordinary control gets, and the fit refuses
    rather than persist a zero limit. Fit-derived like ``reference_std``:
    the contract fingerprint covers ``rule``, not the number.
    """

    rule: BandExcessPersistence
    limit: float = Field(gt=0)
    #: Drive groups (B02.3) the limit was fitted on — never a test drive.
    training_drives: list[str] = Field(min_length=1)
    #: Evaluated training rows the accumulation ran over.
    training_rows: int = Field(ge=1)
    #: Separate accumulations (a sum leaving zero) seen on those rows.
    training_excursions: int = Field(ge=1)


# ---------------------------------------------------------------------------
# Validation criteria and persisted evidence (B02)
# ---------------------------------------------------------------------------


class ResolutionFloor(_Strict):
    """The smallest output change this model can resolve, with its basis.

    Named ``NoiseFloor`` until R05.14, which collided with
    :class:`src.analysis.noise_floor.NoiseFloor` — the measured null-run
    spread tuning comparisons gate on, a different quantity with a refusing
    ``conditions`` rule. The old name is gone (R27.12).

    ``basis`` is required because a floor measured under one condition is
    routinely quoted against another (the WOT timing floor is 16x smaller
    than the all-condition spread — see ``noise_floor.yaml``). A sensor LSB
    is a lower bound, not a measured noise; say which one this is.
    """

    value: float = Field(gt=0)
    basis: str = Field(min_length=1)


class SyntheticFault(_Strict):
    """An explicitly synthetic step offset added to the model's actual output.

    Reported as *synthetic sensitivity* on untouched test drives — never as
    real fault recall, which needs labelled faults this corpus does not have.
    """

    magnitude: float = Field(gt=0)
    direction: Literal["positive", "negative"] = "positive"
    duration_seconds: float = Field(gt=0)


class TransientControl(_Strict):
    """An explicitly synthetic *short* excursion the model must not alert on.

    The negative control of a resolution spec (B04.3): an excursion shorter
    than the sustained duration the diagnostic question needs (e.g. a
    momentary knock-control retard) is ordinary engine behavior, so an event
    for it is a resolution failure, not a detection.
    """

    magnitude: float = Field(gt=0)
    direction: Literal["positive", "negative"] = "positive"
    duration_seconds: float = Field(gt=0)


class ResolutionSpec(_Strict):
    """What "useful resolution" means for one model, declared before testing.

    B04.3: the intended sustained change and how long it must last, justified
    by a *measured* noise figure (not an LSB) and the diagnostic question the
    model answers. ``claim`` is what an event may be reported as;
    ``not_identified`` lists what it must never be reported as (an unexpected
    timing change is not proven knock retard). Evaluated by
    ``src.analysis.behavior_validation`` through the real eligibility and
    persistence path: detection delay and covered envelope for the sustained
    synthetic faults, and a detection rate for each transient control.
    """

    #: The sustained change, in output units, the question needs resolved.
    sustained_change: float = Field(gt=0)
    #: How long it must persist to matter.
    min_duration_seconds: float = Field(gt=0)
    #: Longest acceptable delay from the first evaluated row of an injected
    #: window to the emitted event.
    max_detection_delay_seconds: float = Field(gt=0)
    #: Measured short-horizon spread of the output where the model evaluates;
    #: its ``basis`` must say what was measured, on which drives.
    measured_noise: ResolutionFloor
    diagnostic_question: str = Field(min_length=1)
    claim: str = Field(min_length=1)
    not_identified: list[str] = Field(min_length=1)
    transient_controls: list[TransientControl] = Field(min_length=1)
    #: Largest tolerated fraction of clean windows alerting on a transient.
    max_transient_detection: float = Field(default=0.0, ge=0, le=1)

    @model_validator(mode="after")
    def _check_resolution(self) -> ResolutionSpec:
        if self.sustained_change <= self.measured_noise.value:
            raise ValueError(
                f"sustained_change {self.sustained_change} must exceed the measured noise "
                f"{self.measured_noise.value} — a change inside the noise is not resolvable"
            )
        if self.max_detection_delay_seconds > self.min_duration_seconds:
            raise ValueError(
                "max_detection_delay_seconds cannot exceed min_duration_seconds: a change "
                "must be reported while it is still the change being asked about"
            )
        long = [
            c for c in self.transient_controls if c.duration_seconds >= self.min_duration_seconds
        ]
        if long:
            raise ValueError(
                f"transient controls must be shorter than min_duration_seconds "
                f"{self.min_duration_seconds}: {[c.duration_seconds for c in long]}"
            )
        return self


class AxisPairing(_Strict):
    """Grade each output read against axis reads acquired near it (B04.9).

    The evaluation frame is a 1 Hz grid: each channel's last read in a second,
    forward-filled up to ``max_gap``. One grid row can therefore put a timing
    read beside an rpm read 2.6 s away (measured on B04.1's drives,
    2026-10-08), while timing itself moves a median 2–3° over 1.5–2.7 s
    against 2.2° measured noise. A model whose criteria declare this is graded
    one row per output read, each axis read paired once on the acquisition
    clock within its bound (:func:`src.signals.pairing.match_ordered`); a read
    with no partner inside the bound is refused as ``unpaired:<axis>``.
    :func:`src.analysis.behavior_validation.apply_criteria` fails metrics
    graded any other way, so a grid-joined verdict cannot pass.
    """

    #: Axis channel -> the largest acquisition separation, seconds, an axis
    #: read may have from the output read it describes. Every axis of the
    #: model must be named (the evaluator refuses a partial declaration).
    max_separation_s: dict[str, float] = Field(min_length=1)
    #: What justifies each bound: the measured change of the quantities over
    #: that separation, not convenience.
    basis: str = Field(min_length=1)

    @model_validator(mode="after")
    def _check_bounds(self) -> AxisPairing:
        bad = {axis: s for axis, s in self.max_separation_s.items() if not s > 0}
        if bad:
            raise ValueError(f"axis_pairing bounds must be positive seconds: {bad}")
        return self


class ValidationCriteria(_Strict):
    """Per-recipe acceptance criteria, declared in profile data before testing.

    Loaded from the vehicle profile's ``behavior_validation.yaml`` (never
    hard-coded in shared code). ``kind`` selects the criteria family:

    * ``predictive_surface`` — must beat a training-fitted simple baseline
      on untouched drives by ``min_skill_vs_baseline`` (fractional MAE
      reduction) and resolve ``meaningful_deviation``.
    * ``constant_baseline`` — a robust constant location has no predictive
      skill to show; it is judged on its own error (``max_mae``,
      ``max_abs_error_p95``), coverage and sensitivity. No R² floor exists
      for either family.
    """

    kind: Literal["predictive_surface", "constant_baseline"]
    unit: str = Field(min_length=1)
    noise_floor: ResolutionFloor
    #: The smallest deviation, in output units, the operator needs this model
    #: to resolve. Must exceed the noise floor.
    meaningful_deviation: float = Field(gt=0)
    meaningful_deviation_basis: str = Field(min_length=1)
    #: Independent physical drives (grouped, B02.3) in the test fold.
    min_validation_drives: int = Field(default=2, ge=1)
    #: Drives between train and test, available to any data-driven choice
    #: (band acceptance); final test drives never choose anything.
    calibration_drives: int = Field(default=0, ge=0)
    min_eligible_observations: int = Field(default=300, ge=1)
    min_observations_per_drive: int = Field(default=30, ge=1)
    #: Evaluated ÷ eligible on the test fold (off-map rows are not predictions).
    min_coverage: float = Field(default=0.5, gt=0, le=1)
    #: Fraction of the model's trusted operating envelope (populated 2D cells,
    #: or 1D domain deciles) visited by test drives.
    min_envelope_coverage: float = Field(default=0.3, ge=0, le=1)
    baseline: Literal["training_median", "training_mode_median"] = "training_mode_median"
    min_skill_vs_baseline: float | None = Field(default=None, le=1)
    max_mae: float | None = Field(default=None, gt=0)
    max_abs_error_p95: float | None = Field(default=None, gt=0)
    #: Emitted events (after persistence + dedupe) per evaluated hour on the
    #: unmodified test drives — a no-fault control, not a false-positive rate.
    max_control_events_per_hour: float | None = Field(default=None, ge=0)
    #: Evaluated time below which one event is not a rate: the control-event
    #: criterion then reports insufficient evidence instead of failing.
    min_control_hours: float = Field(default=0.5, gt=0)
    synthetic_faults: list[SyntheticFault] = Field(default_factory=list)
    min_synthetic_detection: float = Field(default=0.8, ge=0, le=1)
    #: B04.3 — the model's declared useful resolution (optional; when present
    #: its delay and transient controls become criteria).
    resolution: ResolutionSpec | None = None
    #: B04.9 — grade per output read with axes paired within these bounds;
    #: absent, the model is graded on the resampled grid's rows.
    axis_pairing: AxisPairing | None = None
    notes: str | None = None

    @model_validator(mode="after")
    def _check_family(self) -> ValidationCriteria:
        if self.meaningful_deviation <= self.noise_floor.value:
            raise ValueError(
                f"meaningful_deviation {self.meaningful_deviation} must exceed the noise "
                f"floor {self.noise_floor.value} — a deviation inside the noise is not resolvable"
            )
        if self.kind == "predictive_surface" and self.min_skill_vs_baseline is None:
            raise ValueError("predictive_surface criteria require min_skill_vs_baseline")
        if self.kind == "constant_baseline" and self.max_mae is None:
            raise ValueError("constant_baseline criteria require max_mae")
        if not self.synthetic_faults:
            raise ValueError(
                "declare at least one synthetic fault — sensitivity is a gate, not an option"
            )
        resolution = self.resolution
        if resolution is not None:
            if resolution.sustained_change != self.meaningful_deviation:
                raise ValueError(
                    f"resolution.sustained_change {resolution.sustained_change} must equal "
                    f"meaningful_deviation {self.meaningful_deviation}: one declared target"
                )
            if not any(
                fault.magnitude >= resolution.sustained_change
                and fault.duration_seconds >= resolution.min_duration_seconds
                for fault in self.synthetic_faults
            ):
                raise ValueError(
                    "declare a synthetic fault of at least the sustained change lasting at "
                    "least min_duration_seconds — otherwise the resolution is never tested"
                )
        return self


class FoldMetrics(_Strict):
    """Denominators and physical-unit accuracy on one set of drives (B02.5)."""

    drives: int = Field(ge=0)
    rows: int = Field(ge=0)
    input_present: int = Field(ge=0)
    fresh: int = Field(ge=0)
    eligible: int = Field(ge=0)
    evaluated: int = Field(ge=0)
    coverage: float | None = None
    refusals: dict[str, int] = Field(default_factory=dict)
    mae: float | None = None
    bias: float | None = None
    abs_error_p50: float | None = None
    abs_error_p90: float | None = None
    abs_error_p95: float | None = None
    r_squared: float | None = None
    warning_half_width_median: float | None = None
    critical_half_width_median: float | None = None
    warning_row_rate: float | None = None
    critical_row_rate: float | None = None
    baseline_mae: float | None = None
    skill_vs_baseline: float | None = None
    #: Time the model was actually grading (evaluated rows × tick interval):
    #: the denominator of ``events_per_hour``.
    evaluated_hours: float = Field(default=0.0, ge=0)
    events: int = Field(default=0, ge=0)
    events_per_hour: float | None = None
    envelope_coverage: float | None = None
    #: Drive-level uncertainty: min/max per-drive MAE, and a drive-resampled
    #: bootstrap interval once there are enough drives to resample.
    drive_mae_min: float | None = None
    drive_mae_max: float | None = None
    mae_drive_bootstrap_90: list[float] | None = None
    #: B04.9 — the per-axis acquisition bound the rows were paired under, one
    #: row per output read; ``None``: rows of the resampled grid, whose
    #: forward-filled join leaves the separation unbounded below ``max_gap``.
    axis_pairing_s: dict[str, float] | None = None


class SyntheticSensitivity(_Strict):
    """Detection of one explicitly synthetic fault case (never real recall)."""

    synthetic: Literal[True] = True
    magnitude: float
    direction: Literal["positive", "negative"]
    duration_seconds: float
    #: Windows with at least one evaluated row.
    windows: int = Field(ge=0)
    #: Same windows without the injection — the no-fault control.
    control_detected: int = Field(default=0, ge=0)
    #: Trials: windows whose control raised no event. A window that alerts
    #: anyway cannot show the injection was seen.
    clean_windows: int = Field(default=0, ge=0)
    detected: int = Field(ge=0)
    #: ``detected / clean_windows``; ``None`` when no clean window exists.
    detection_fraction: float | None = None
    #: Seconds from each detected window's first evaluated row (the first
    #: moment the model could see the change) to its first emitted event.
    detection_delay_seconds_median: float | None = None
    detection_delay_seconds_max: float | None = None
    #: Fraction of the trusted envelope the clean windows' evaluated rows
    #: visited — the envelope this sensitivity was actually shown on.
    covered_envelope: float | None = None


class TransientControlResult(_Strict):
    """Events raised by one synthetic transient control (B04.3); should be none."""

    synthetic: Literal[True] = True
    magnitude: float
    direction: Literal["positive", "negative"]
    duration_seconds: float
    windows: int = Field(ge=0)
    clean_windows: int = Field(default=0, ge=0)
    detected: int = Field(ge=0)
    detection_fraction: float | None = None


class DriveRecord(_Strict):
    drive_id: str
    pieces: list[str] = Field(min_length=1)
    started_at: str | None = None
    ended_at: str | None = None


class DriveSplitRecord(_Strict):
    """Grouped folds, by independent physical drive, ordered by parsed time."""

    policy: str
    join_gap_seconds: float = Field(ge=0)
    train: list[DriveRecord] = Field(default_factory=list)
    calibration: list[DriveRecord] = Field(default_factory=list)
    test: list[DriveRecord] = Field(default_factory=list)
    #: Drives that could not serve as evidence (too few eligible rows) — in
    #: no fold, listed so the denominator of "independent drives" is visible.
    excluded: list[DriveRecord] = Field(default_factory=list)


class ValidationEvidence(_Strict):
    """The immutable record a publication route reads to decide trust (B02.7).

    Written once, by the fit that produced the model; carry-over and reload
    never rewrite it. ``contract_fingerprint`` pins the serving contract the
    evidence was gathered under — a model whose contract no longer matches
    (hand edit, changed recipe) is not covered by this evidence.
    """

    evaluator_version: int = Field(ge=1)
    evaluated_at: str
    verdict: Literal["passed", "failed", "insufficient_evidence"]
    failures: list[str] = Field(default_factory=list)
    criteria: ValidationCriteria | None = None
    criteria_source: str | None = None
    contract_fingerprint: str
    decoder_identity: dict[str, str] = Field(default_factory=dict)
    mode_taxonomy: list[str] = Field(default_factory=list)
    tune_revision: str | None = None
    split: DriveSplitRecord
    baseline: dict[str, float] = Field(default_factory=dict)
    calibration: FoldMetrics | None = None
    test: FoldMetrics | None = None
    per_drive: dict[str, FoldMetrics] = Field(default_factory=dict)
    per_regime: dict[str, FoldMetrics] = Field(default_factory=dict)
    sensitivity: list[SyntheticSensitivity] = Field(default_factory=list)
    transient_controls: list[TransientControlResult] = Field(default_factory=list)
    provenance: dict[str, list[str]] = Field(default_factory=dict)


# ---------------------------------------------------------------------------
# Permitted-use policy (B03.2 / B03.3)
# ---------------------------------------------------------------------------


class PermittedUse(StrEnum):
    """What a loaded behavior artifact may be used for (B03.2).

    One small vocabulary shared by the loader, preprocessing and the daemon:

    * ``display`` — the live overlay, session analysis, dashboard and MCP
      read it. Always retained: a demoted surface stays inspectable.
    * ``learned_feature`` — its ``deviation:<name>`` column may be *selected*
      as a feature by a new IF training run
      (``feature_policy.FeaturePolicy``). Withholding it never stops the
      column being produced, so an existing detector trained on it keeps its
      input (the 2026-09 IF-blind incident was a silently deleted column).
    * ``alert`` — standalone ``BehaviorDetector`` events and fusion votes.
      Granted only by passed held-out evidence (:func:`serving_status`);
      a policy entry can withdraw it, never grant it.
    """

    DISPLAY = "display"
    LEARNED_FEATURE = "learned_feature"
    ALERT = "alert"


#: Every use an artifact could have before evidence and policy subtract.
ALL_PERMITTED_USES: frozenset[PermittedUse] = frozenset(PermittedUse)

#: Profile-data file holding :class:`BehaviorPolicy`, next to the artifact.
BEHAVIOR_POLICY_FILENAME = "behavior_policy.yaml"

Lifecycle = Literal["validated", "unvalidated", "exploratory", "demoted", "quarantined"]


class LifecyclePolicyEntry(_Strict):
    """One operator/review decision restricting a model's permitted uses.

    Lives in profile data (``behavior_policy.yaml``), never in the artifact,
    so a refit, a partial-refit carry-over, a hot reload or restoring an old
    artifact cannot drop it (B03.3):

    * ``demoted`` / ``exploratory`` apply to the model *name* — any artifact
      of that name, including a future refit, until the entry is removed
      (``lift_when`` says by what).
    * ``quarantined`` applies to one *artifact* (``artifact_fingerprint``,
      see :func:`artifact_fingerprint`): reloading or carrying over that
      artifact stays quarantined; a new fit is a different artifact and must
      earn trust through B02 evidence like any other.
    """

    model: str = Field(min_length=1)
    lifecycle: Literal["exploratory", "demoted", "quarantined"]
    permitted_uses: list[PermittedUse]
    artifact_fingerprint: str | None = None
    reason: str = Field(min_length=1)
    decided: str = Field(min_length=1)
    evidence: str = Field(min_length=1)
    lift_when: str | None = None

    @model_validator(mode="after")
    def _restrict_only(self) -> LifecyclePolicyEntry:
        uses = set(self.permitted_uses)
        if PermittedUse.ALERT in uses:
            raise ValueError(
                f"{self.model}: a policy entry cannot grant 'alert' — trust comes only "
                "from passed held-out evidence; policy can only withdraw uses"
            )
        if PermittedUse.DISPLAY not in uses:
            raise ValueError(
                f"{self.model}: 'display' must stay permitted — a demoted or quarantined "
                "surface stays available for inspection"
            )
        if self.lifecycle == "quarantined" and not self.artifact_fingerprint:
            raise ValueError(
                f"{self.model}: a quarantine pins one artifact; give artifact_fingerprint"
            )
        if self.lifecycle != "quarantined" and self.artifact_fingerprint is not None:
            raise ValueError(
                f"{self.model}: {self.lifecycle} applies to the model name; "
                "artifact_fingerprint is only for quarantine"
            )
        return self

    def to_dict(self) -> dict[str, object]:
        return self.model_dump(mode="json", exclude_none=True)


class BehaviorPolicy(_Strict):
    """``behavior_policy.yaml`` — the per-vehicle permitted-use decisions."""

    schema_version: Literal[1] = 1
    entries: list[LifecyclePolicyEntry] = Field(default_factory=list)

    @model_validator(mode="after")
    def _unique(self) -> BehaviorPolicy:
        seen: set[tuple[str, str | None]] = set()
        for entry in self.entries:
            key = (entry.model, entry.artifact_fingerprint)
            if key in seen:
                raise ValueError(f"duplicate behavior policy entry for {key}")
            seen.add(key)
        return self

    def entry_for(self, model: BehaviorModel1D | BehaviorModel2D) -> LifecyclePolicyEntry | None:
        """The entry governing ``model``: an artifact quarantine, else a name-wide one."""
        named = [entry for entry in self.entries if entry.model == model.name]
        if not named:
            return None
        pinned = [entry for entry in named if entry.artifact_fingerprint is not None]
        if pinned:
            identity = artifact_fingerprint(model)
            for entry in pinned:
                if entry.artifact_fingerprint == identity:
                    return entry
        for entry in named:
            if entry.artifact_fingerprint is None:
                return entry
        return None


def _model_channels(model: BehaviorModel1D | BehaviorModel2D, axes: tuple[str, ...]) -> list[str]:
    """Every telemetry channel a model's contract reads: axes, output, rate
    gates and every eligibility channel (the set
    ``behavior_eligibility.required_channel_groups`` plans collection from)."""
    channels = [*axes, model.output, *(condition.channel for condition in model.conditions)]
    eligibility = model.eligibility
    if eligibility is not None:
        channels.append(eligibility.engine_state.rpm_channel)
        if eligibility.fuel_control is not None:
            channels.append(eligibility.fuel_control.channel)
        if eligibility.cold_start is not None:
            channels.extend(
                (eligibility.cold_start.rpm_channel, eligibility.cold_start.coolant_channel)
            )
        channels.extend(c for ctx in eligibility.required_context for c in ctx.any_of)
        channels.extend(condition.channel for condition in eligibility.local_ranges)
    return channels


def refuse_non_trainable_sources(name: str, channels: Sequence[str]) -> None:
    """H01.1 — refuse a behavior model (recipe or loaded artifact) that reads a
    channel whose source may not feed a detector: an external sensor (``gps:``,
    ``imu:``, ``sensor:``; H00 operator decision 1, 2026-09-23), a ``derived:``
    channel computed from one or with no declared inputs, or an unclassified
    namespace. A bare, unprefixed name is left to the contract's own
    resolution (an output with no namespace is read as ``decoded:``)."""
    refused = {
        channel: reason
        for channel in dict.fromkeys(channels)
        if source_of(channel) is not None
        and (reason := source_exclusion_reason(channel)) is not None
    }
    if refused:
        raise ValueError(
            f"{name}: behavior models may read only trainable sources "
            f"(H00 operator decision 1: external sensors never train or score a "
            f"behavior model; src.trainability): {refused}"
        )


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
    #: B02 held-out validation record; written once by the fit, never rewritten.
    evidence: ValidationEvidence | None = None
    #: B02.7 — named reasons a publication route (carry-over after a decoder,
    #: contract, criteria or mode-taxonomy change) withheld trust without
    #: touching ``evidence``. Any entry makes the model diagnostic-only.
    publication_flags: list[str] = Field(default_factory=list)
    #: B05.3 — this model's own event persistence (``None``: the daemon's
    #: configured M-of-N gate). Applied by the daemon and the evaluator alike.
    event_persistence: FittedBandExcessPersistence | None = None
    #: B03.2 — the profile policy entry governing this artifact, attached by
    #: :func:`load_behavior_models` / :func:`apply_behavior_policy`. Private:
    #: never serialized, and an artifact cannot carry its own policy.
    _policy_entry: LifecyclePolicyEntry | None = PrivateAttr(default=None)

    @model_validator(mode="after")
    def _check_input_sources(self) -> BehaviorModel1D:
        refuse_non_trainable_sources(self.name, _model_channels(self, (self.axis.channel,)))
        return self

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
    evidence: ValidationEvidence | None = None
    publication_flags: list[str] = Field(default_factory=list)
    event_persistence: FittedBandExcessPersistence | None = None
    _policy_entry: LifecyclePolicyEntry | None = PrivateAttr(default=None)
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
    def _check_input_sources(self) -> BehaviorModel2D:
        refuse_non_trainable_sources(
            self.name, _model_channels(self, tuple(axis.channel for axis in self.axes))
        )
        return self

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
    #: B03.2 — the policy :func:`apply_behavior_policy` attached, and its
    #: entries that matched no loaded artifact.
    _policy: BehaviorPolicy | None = PrivateAttr(default=None)
    _policy_unmatched: list[str] = PrivateAttr(default_factory=list)

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
#: Held-out rows were scored but no B02 gate evidence was recorded (a
#: schema-2 artifact written before the B02 gate, or a hand edit).
STATUS_NO_GATE_EVIDENCE = "unvalidated:no_gate_evidence"
#: The evidence was gathered under a different serving contract than the
#: artifact now declares (hand edit, or a contract field changed after fit).
STATUS_EVIDENCE_CONTRACT_MISMATCH = "evidence_contract_mismatch"
#: Prefix of the reason a profile policy entry adds (``policy:demoted`` …).
STATUS_POLICY_PREFIX = "policy:"


def contract_fingerprint_payload(
    *,
    kind: str,
    output: str,
    inputs: Sequence[str],
    operating_mode: str | None,
    conditions: Sequence[RateOfChangeCondition],
    eligibility: ServingEligibility | None,
    deviation: DeviationConfig | None,
    event_persistence: BandExcessPersistence | None = None,
) -> dict[str, object]:
    """The serving-contract fields validation evidence is only valid for.

    Excludes fit-derived values (cells, coefficients, ``reference_std``, a
    fitted persistence limit) and the training-only selection, which is
    provenance rather than contract — so a recipe and the model it produced
    fingerprint identically. ``event_persistence`` (B05.3) is the declared
    rule; it enters the payload only when set, so every contract
    fingerprinted before it keeps its identity and its evidence still matches.
    """
    persistence = (
        {}
        if event_persistence is None
        else {"event_persistence": event_persistence.model_dump(mode="json")}
    )
    return {
        **persistence,
        "kind": kind,
        "output": output,
        "inputs": list(inputs),
        "operating_mode": operating_mode,
        "conditions": [condition.model_dump(mode="json") for condition in conditions],
        "eligibility": (
            None
            if eligibility is None
            else eligibility.model_dump(
                mode="json",
                # An empty ``local_ranges`` (added 2026-09-23) is left out so
                # every contract fingerprinted before it keeps its identity
                # and its evidence still matches.
                exclude=(
                    {"training_selection"}
                    if eligibility.local_ranges
                    else {"training_selection", "local_ranges"}
                ),
            )
        ),
        "deviation": (
            None
            if deviation is None
            else deviation.model_dump(mode="json", exclude={"reference_std"})
        ),
    }


def fingerprint(payload: object) -> str:
    """``sha256:`` of canonical JSON — the one hashing rule for B02 identities."""
    blob = json.dumps(payload, sort_keys=True, separators=(",", ":"), default=str)
    return "sha256:" + hashlib.sha256(blob.encode()).hexdigest()


def contract_fingerprint(model: BehaviorModel1D | BehaviorModel2D) -> str:
    inputs = (
        [model.axis.channel]
        if isinstance(model, BehaviorModel1D)
        else [axis.channel for axis in model.axes]
    )
    return fingerprint(
        contract_fingerprint_payload(
            kind=model.kind,
            output=model.output,
            inputs=inputs,
            operating_mode=model.operating_mode,
            conditions=model.conditions,
            eligibility=model.eligibility,
            deviation=model.deviation,
            event_persistence=(
                None if model.event_persistence is None else model.event_persistence.rule
            ),
        )
    )


def artifact_fingerprint(model: BehaviorModel1D | BehaviorModel2D) -> str:
    """Identity of one fitted artifact, stable across carry-over and reload.

    Covers the name, output and the whole ``fit`` (coefficients/knots/cells,
    training sessions, ``fitted_at``, training-row hash). Excludes ``version``
    and ``publication_flags``, which a carry-over may change without changing
    the fitted surface — so a quarantine pinned to this value follows the
    artifact through partial refits and survives restoring an old file.
    """
    return fingerprint(
        {
            "name": model.name,
            "kind": model.kind,
            "output": model.output,
            "fit": model.fit.model_dump(mode="json"),
        }
    )


def artifact_identity(model: BehaviorModel1D | BehaviorModel2D) -> dict[str, object]:
    """What a status surface shows to say *which* artifact is serving (B03.8)."""
    return {
        "name": model.name,
        "version": model.version,
        "fingerprint": artifact_fingerprint(model),
        "fitted_at": model.fit.fitted_at,
        "training_data_hash": model.fit.training_data_hash or None,
        "tune_revision": model.fit.tune_revision,
        "schema_contract": model.eligibility is not None,
        "grid_version": model.grid_version if isinstance(model, BehaviorModel2D) else None,
    }


def validation_scope(model: BehaviorModel1D | BehaviorModel2D) -> dict[str, object]:
    """Where the artifact's evidence holds, or that there is none (B03.8).

    An artifact with no B02 evidence says so (``verdict: "none"``) instead of
    looking like a clean result; held-out rows without evidence are named.
    """
    evidence = model.evidence
    if evidence is None:
        return {
            "verdict": "none",
            "held_out_rows": model.fit.validation_sample_count,
            "held_out_sessions": list(model.fit.calibration_sessions),
            "tune_revision": model.fit.tune_revision,
            "operating_mode": model.operating_mode,
        }
    return {
        "verdict": evidence.verdict,
        "evaluated_at": evidence.evaluated_at,
        "failures": list(evidence.failures),
        "tune_revision": evidence.tune_revision,
        "operating_mode": model.operating_mode,
        "mode_taxonomy": list(evidence.mode_taxonomy),
        "test_drives": [drive.drive_id for drive in evidence.split.test],
        "calibration_drives": [drive.drive_id for drive in evidence.split.calibration],
        "train_drives": len(evidence.split.train),
        "test_evaluated_rows": evidence.test.evaluated if evidence.test is not None else 0,
        "criteria_source": evidence.criteria_source,
        "contract_matches": evidence.contract_fingerprint == contract_fingerprint(model),
    }


@dataclass(frozen=True)
class ServingStatus:
    """Whether a loaded model may raise trusted alerts, what else it may do, and why.

    ``diagnostic_only`` models still evaluate — the live overlay, session
    analysis and the ``deviation:*`` feature columns existing detectors were
    trained on keep working — but the daemon never emits or fuses their
    alerts. ``lifecycle`` separates *why*: ``unvalidated`` (the artifact
    lacks passing evidence), or a profile policy decision (``exploratory``,
    ``demoted``, ``quarantined``); ``validated`` is the only alerting one.
    ``permitted_uses`` is the typed B03.2 policy the loader, preprocessing
    and daemon all read.
    """

    state: Literal["active", "diagnostic_only"]
    reasons: tuple[str, ...] = ()
    lifecycle: Lifecycle = "validated"
    permitted_uses: frozenset[PermittedUse] = ALL_PERMITTED_USES
    policy: LifecyclePolicyEntry | None = None

    @property
    def alerting(self) -> bool:
        return PermittedUse.ALERT in self.permitted_uses

    def permits(self, use: PermittedUse) -> bool:
        return use in self.permitted_uses

    def to_dict(self) -> dict[str, object]:
        return {
            "state": self.state,
            "reasons": list(self.reasons),
            "lifecycle": self.lifecycle,
            "permitted_uses": sorted(use.value for use in self.permitted_uses),
            "policy": None if self.policy is None else self.policy.to_dict(),
        }


def serving_status(model: BehaviorModel1D | BehaviorModel2D) -> ServingStatus:
    """Classify a loaded artifact (B01.6, B02.7, B03.2) without blessing missing metadata.

    The one trust decision: the daemon's model load and hot reload, the
    behavior detector, the dashboard, MCP and the refit report all call this.
    ``active`` requires the schema-2 eligibility contract, held-out rows, and
    B02 :class:`ValidationEvidence` whose verdict is ``passed`` under this
    model's current contract, with no publication flag, and no profile
    policy entry withdrawing ``alert``. Anything else is ``diagnostic_only``
    with every applicable reason named. Loading never upgrades a model; only
    a refit that writes all of these can, and a policy entry (attached at
    load from profile data) can only subtract uses.
    """
    reasons: list[str] = []
    if model.eligibility is None:
        reasons.append(STATUS_LEGACY_NO_CONTRACT)
    held_out = model.fit.validation_sample_count > 0 and bool(model.fit.calibration_sessions)
    if not held_out:
        reasons.append(STATUS_NO_HELD_OUT_VALIDATION)
    # B02.7 — held-out rows alone are not a pass: the evidence must record a
    # passed gate against declared criteria, under this exact contract.
    evidence = model.evidence
    if evidence is None:
        if held_out:
            reasons.append(STATUS_NO_GATE_EVIDENCE)
    else:
        if evidence.verdict != "passed":
            details = evidence.failures or ["no failure recorded"]
            reasons.extend(f"validation_{evidence.verdict}:{detail}" for detail in details)
        if evidence.contract_fingerprint != contract_fingerprint(model):
            reasons.append(STATUS_EVIDENCE_CONTRACT_MISMATCH)
    reasons.extend(model.publication_flags)
    uses = set(ALL_PERMITTED_USES)
    lifecycle: Lifecycle = "validated"
    if reasons:
        uses.discard(PermittedUse.ALERT)
        lifecycle = "unvalidated"
    entry = model._policy_entry
    if entry is not None:
        # B03.3 — policy is intersected, never unioned: it cannot restore a
        # use the evidence withheld.
        uses &= set(entry.permitted_uses)
        lifecycle = entry.lifecycle
        reasons.insert(0, f"{STATUS_POLICY_PREFIX}{entry.lifecycle}")
    state: Literal["active", "diagnostic_only"] = (
        "active" if PermittedUse.ALERT in uses else "diagnostic_only"
    )
    return ServingStatus(
        state,
        tuple(reasons),
        lifecycle=lifecycle,
        permitted_uses=frozenset(uses),
        policy=entry,
    )


def diagnostic_only_models(models: BehaviorModels) -> dict[str, list[str]]:
    """``{model_name: reasons}`` for every model that cannot alert."""
    out: dict[str, list[str]] = {}
    for model in models.models:
        status = serving_status(model)
        if not status.alerting:
            out[model.name] = list(status.reasons)
    return out


class GoverningFileConflictError(ValueError):
    """A copy of a profile file beside an artifact disagrees with the tracked one."""


def _governing_profile_dir(artifact_dir: Path) -> Path | None:
    """The tracked profile directory that governs an artifact in ``artifact_dir``.

    Inside the checkout's ``data/`` it is the directory above the first
    ``_``-prefixed component: ``data/<slug>/_candidates/<run>/`` and
    ``data/<slug>/_prior/<stamp>/`` are governed by ``data/<slug>/``, exactly
    as the published artifact is (B03.7: a candidate loaded in place used to
    get the nonexistent policy beside it and silently shed every demotion and
    quarantine). Elsewhere it is the tracked profile named like the artifact
    directory (a machine copy, S03.3), or ``None`` (a scratch artifact).
    """
    if artifact_dir.is_relative_to(DATA_DIR):
        parts = artifact_dir.relative_to(DATA_DIR).parts
        for index, part in enumerate(parts):
            if part.startswith("_"):
                return DATA_DIR.joinpath(*parts[:index]) if index else None
        return artifact_dir
    tracked = DATA_DIR / artifact_dir.name
    return tracked if tracked.is_dir() else None


def governing_profile_file(behavior_models_path: Path, filename: str) -> Path:
    """The tracked profile file (policy, validation document) that governs the
    artifact at ``behavior_models_path``.

    The tracked profile's file whenever the artifact has a tracked profile
    (:func:`_governing_profile_dir`): the published artifact in ``data/<slug>/``,
    a candidate or prior copy nested under it in ``_``-prefixed directories,
    or a machine copy under ``state_dir/models/<slug>/`` (S03.3, operator
    decision 2026-10-04: trained artifacts are Pi-owned, and the files that
    govern them are laptop-authored facts in the checkout). A copy of the file
    beside such an artifact is never preferred: identical, it is ignored;
    different, it is refused (:class:`GoverningFileConflictError`), because a
    stale copy would silently re-grant what the profile has since withdrawn.
    An artifact with no tracked profile (a scratch or test artifact) is
    governed by the file beside it.
    """
    artifact_dir = behavior_models_path.parent
    beside = artifact_dir / filename
    profile = _governing_profile_dir(artifact_dir)
    if profile is None:
        return beside
    tracked = profile / filename
    if beside != tracked and beside.exists():
        if not tracked.exists() or beside.read_bytes() != tracked.read_bytes():
            raise GoverningFileConflictError(
                f"{beside} differs from the tracked profile's {tracked}, which governs "
                f"{behavior_models_path}; delete the copy (or update the tracked file) "
                "so one file decides the artifact's permitted uses"
            )
    return tracked


def behavior_policy_path(behavior_models_path: Path) -> Path:
    """The profile policy file governing the artifact at ``behavior_models_path``."""
    return governing_profile_file(behavior_models_path, BEHAVIOR_POLICY_FILENAME)


def load_behavior_policy(path: Path) -> BehaviorPolicy:
    """Load ``behavior_policy.yaml``; a missing file is an empty policy."""
    if not path.exists():
        return BehaviorPolicy()
    raw = load_yaml(path.read_text()) or {}
    return BehaviorPolicy.model_validate(raw)


def apply_behavior_policy(models: BehaviorModels, policy: BehaviorPolicy) -> list[str]:
    """Attach ``policy`` to every model of ``models``; return unmatched entries.

    An unmatched quarantine means its artifact was replaced (a new fit must
    earn trust through evidence) — named so a status surface can show it.
    """
    matched: set[tuple[str, str | None]] = set()
    for model in models.models:
        entry = policy.entry_for(model)
        model._policy_entry = entry
        if entry is not None:
            matched.add((entry.model, entry.artifact_fingerprint))
    # An empty policy leaves the document exactly as validated (private
    # attributes take part in pydantic equality).
    models._policy = policy if policy.entries else None
    unmatched = [
        f"{entry.model}:{entry.lifecycle}"
        + (f"@{entry.artifact_fingerprint}" if entry.artifact_fingerprint else "")
        for entry in policy.entries
        if (entry.model, entry.artifact_fingerprint) not in matched
    ]
    models._policy_unmatched = unmatched
    return unmatched


def policy_unmatched_entries(models: BehaviorModels) -> list[str]:
    """Policy entries that governed no artifact of ``models`` at load (B03.8)."""
    return list(models._policy_unmatched)


def load_behavior_models(path: Path) -> BehaviorModels:
    """Load and validate a ``behavior_models.yaml`` file, with its profile policy.

    The sibling ``behavior_policy.yaml`` (B03.2) is applied here — the one
    loader every consumer (daemon load, hot reload, dashboard, MCP, replay,
    refit, feature pipeline) goes through — so no caller can read an
    artifact without the demotions that govern it.
    """
    raw = load_yaml(path.read_text())
    models = BehaviorModels.model_validate(raw)
    unmatched = apply_behavior_policy(models, load_behavior_policy(behavior_policy_path(path)))
    if unmatched:
        log.info(
            "behavior policy entries match no loaded artifact (replaced or retired)",
            extra={"entries": unmatched},
        )
    return models


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
