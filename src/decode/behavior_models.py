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

from collections.abc import Sequence
from pathlib import Path
from typing import Annotated, Literal

import yaml
from pydantic import BaseModel, ConfigDict, Field, model_validator

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


Fit1D = Annotated[PolynomialFit | LookupTableFit, Field(discriminator="kind")]


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
    ``relative`` — multiples of the local std (so per-cell std for 2D grids;
    1D polys must declare an explicit ``reference_std`` because the residual
    band is global). ``warning_band`` < ``critical_band`` is required; either
    can be ``None`` to suppress that band.
    """

    kind: Literal["absolute", "relative"]
    warning_band: float | None = Field(default=None, ge=0)
    critical_band: float | None = Field(default=None, ge=0)
    reference_std: float | None = None  # required for kind=relative on 1D fits

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


# ---------------------------------------------------------------------------
# Models
# ---------------------------------------------------------------------------


class BehaviorModel1D(_Strict):
    """A 1D expected-behavior surface."""

    name: str
    kind: Literal["surface_1d"]
    output: str
    output_unit: str = "unknown"
    axis: Axis1D
    operating_mode: str | None = None  # restrict evaluation to this mode (e.g. "warmup")
    version: int = 1
    fit: Fit1D
    deviation: DeviationConfig | None = None
    notes: str | None = None

    @model_validator(mode="after")
    def _check_deviation(self) -> BehaviorModel1D:
        if (
            self.deviation is not None
            and self.deviation.kind == "relative"
            and self.deviation.reference_std is None
        ):
            raise ValueError(f"{self.name}: relative deviation on a 1D surface needs reference_std")
        return self


class BehaviorModel2D(_Strict):
    """A 2D expected-behavior surface."""

    name: str
    kind: Literal["surface_2d"]
    output: str
    output_unit: str = "unknown"
    axes: tuple[Axis2D, Axis2D]
    operating_mode: str | None = None
    version: int = 1
    fit: Fit2D
    deviation: DeviationConfig | None = None
    notes: str | None = None

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

    def by_name(self, name: str) -> BehaviorModel1D | BehaviorModel2D | None:
        for model in self.models:
            if model.name == name:
                return model
        return None


def load_behavior_models(path: Path) -> BehaviorModels:
    """Load and validate a ``behavior_models.yaml`` file."""
    raw = yaml.safe_load(path.read_text())
    return BehaviorModels.model_validate(raw)


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
