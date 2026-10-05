"""Change detection against a frozen reference (E03.3).

A residual stream (measured minus a reference prediction) is turned into
*events* in declared steps, each a required section of :class:`ChangePolicy`
because each was a way an earlier detector lied:

* **Excitation.** The residual means something only inside declared operating
  cells (``excitation.cells`` on one condition channel); a block outside every
  cell is ``uncovered``, counted, never scored. Intervals are calibrated per
  cell ("Mondrian" split conformal).
* **Dependence, in two parts.** Samples are averaged into blocks of at most
  ``dependence.block_seconds`` inside one cell. Calibration then separates
  (a) the *within-drive* lag-1 autocorrelation, measured on drive-demeaned
  block residuals and refused past ``max_within_drive_lag1``, whose AR(1)
  variance inflation deflates the per-block statistics; and (b) a
  *between-drive* variance component per cell (method of moments over the
  calibration drives' mean residuals, floored at
  ``drive_level.between_drive_sd_floor``), which only the drive-level
  detector uses, with drives as its units. A per-drive offset is not
  short-range noise and is not passed off as one.
* **Bias.** A reference whose own calibration fold sits, in any cell, more
  than ``calibration.max_bias_fraction`` of the shift of interest away from
  it is refused: past half the shift, CUSUM accumulates on in-control data.
* **Effective size.** A cell calibrates only with ``min_cell_drives`` drives
  and ``min_cell_blocks`` *effective* blocks (block count deflated by the
  within-drive dependence); when the conformal rank fails on the effective
  count the interval is kept but marked ``coverage_guaranteed: False``.
* **Persistence.** A standardized value is clipped to ``clip_sigma``
  beyond the CUSUM reference value ``k`` before it enters any statistic (a
  clip below ``k`` would blind the detector to the very shift it is built
  for); GLR inputs are clipped at ``clip_sigma`` about zero. The policy
  refuses a ``clip_sigma`` at which one block or one drive could reach a
  CUSUM or GLR limit. Drive-level alarms need two consecutive drives beyond
  ``k`` toward the alarm, and a ramp spans at least two drives.
  Per block, an alarm also needs ``min_consecutive_blocks`` consecutive
  blocks that are each beyond the CUSUM reference value ``k`` in the alarm's
  direction while the statistic is over its limit — a spike on top of an
  accumulated statistic is one such block, not an alarm. A single block
  outside the conformal interval is an *exceedance*, never an event.
* **Deduplication.** Per-block alarms of one detector and direction within
  ``deduplication.merge_seconds`` extend one event; per-block statistics
  restart every drive (``reset_at_drive_boundary`` is ``True`` by schema).
* **Drive level.** Slow change *between* drives is invisible to statistics
  that restart each drive. At :meth:`ChangeMonitor.close_drive` each drive
  becomes one standardized unit (cell means combined with inverse within-drive
  variance weights, the between-drive component treated as shared across
  cells), which feeds a drive-level CUSUM and a ramp GLR (a gradual drift of
  unknown start and slope) over the last ``window_drives`` drives.

**The reference is frozen, and only the reference sets alarm limits.**
:func:`fit_reference` fits on training drives and calibrates on separate
drives; :class:`FrozenReference` is immutable and self-hashed;
:class:`ChangeMonitor` reads limits from it alone and refuses anything else.
Each :class:`BlockVerdict` names its issuing monitor, the reference
fingerprint and every alarm still unresolved, and the adaptive candidate
(``src.models.adaptive_candidate``) can learn a block only with the verdict
the monitor issued for that very block — so it cannot learn ahead of the
monitor, and cannot learn while any alarm stands.

Pure numpy + pydantic (layer 4), small state and O(window) per block.
"""

from __future__ import annotations

import hashlib
import json
import math
from collections import deque
from collections.abc import Iterable, Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any, Literal

import numpy as np
from pydantic import BaseModel, ConfigDict, Field, ValidationInfo, model_validator

__all__ = [
    "CHANGE_POLICY_SCHEMA",
    "AlarmLimits",
    "Block",
    "BlockAccumulator",
    "BlockVerdict",
    "CellLimits",
    "ChangeEvent",
    "ChangeMonitor",
    "ChangePolicy",
    "ConditionCell",
    "DeclaredShift",
    "Deduplication",
    "Dependence",
    "Detectors",
    "DriveLevel",
    "DriveVerdict",
    "Excitation",
    "FrozenReference",
    "IntervalCalibration",
    "NotAFrozenReferenceError",
    "Persistence",
    "ReferenceRefusedError",
    "Sample",
    "blocks_of",
    "calibration_fold_bias",
    "fit_reference",
    "reference_fingerprint",
]

CHANGE_POLICY_SCHEMA: Literal["canary-change-detection/2"] = "canary-change-detection/2"

Direction = Literal["+", "-"]
DetectorName = Literal["cusum", "glr", "drive_cusum", "drive_glr"]


class _Strict(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


# ---------------------------------------------------------------------------
# Declared policy


class ConditionCell(_Strict):
    """One operating cell ``[lower, upper)`` on the excitation condition."""

    name: str = Field(min_length=1)
    lower: float
    upper: float

    @model_validator(mode="after")
    def _ordered(self) -> ConditionCell:
        if not self.upper > self.lower:
            raise ValueError(f"cell {self.name!r}: upper must exceed lower")
        return self


class Excitation(_Strict):
    """Where the residual is meaningful, and the cells intervals are kept per."""

    condition: str = Field(min_length=1)
    unit: str = Field(min_length=1)
    cells: tuple[ConditionCell, ...] = Field(min_length=1)
    #: A block with fewer samples than this is dropped (``short_block``).
    min_block_samples: int = Field(ge=2)

    @model_validator(mode="after")
    def _disjoint(self) -> Excitation:
        names = [cell.name for cell in self.cells]
        if len(set(names)) != len(names):
            raise ValueError("excitation cell names must be unique")
        ordered = sorted(self.cells, key=lambda cell: cell.lower)
        for left, right in zip(ordered, ordered[1:], strict=False):
            if right.lower < left.upper:
                raise ValueError(f"cells {left.name!r} and {right.name!r} overlap")
        return self

    def cell_of(self, value: float) -> str | None:
        if not math.isfinite(value):
            return None
        for cell in self.cells:
            if cell.lower <= value < cell.upper:
                return cell.name
        return None


class Dependence(_Strict):
    """Blocks, then a measured within-drive correction (between-drive
    variance is the drive level's, see :class:`DriveLevel`)."""

    block_seconds: float = Field(gt=0.0)
    #: A gap longer than this between samples closes the block.
    max_gap_seconds: float = Field(gt=0.0)
    correction: Literal["ar1_variance_inflation"]
    #: Lag-1 autocorrelation of drive-demeaned calibration block residuals
    #: past which the reference is refused. At 0.5 the AR(1) inflation is
    #: sqrt(3); beyond it the AR(1) approximation is carrying the limit.
    max_within_drive_lag1: float = Field(ge=0.0, le=0.9)


class IntervalCalibration(_Strict):
    """Out-of-sample calibration: a fixed held-out set, never leave-one-out.

    In-sample threshold calibration once drove a sequence detector's false
    positive rate to about one on unseen drives; the fold sizes are capped at
    three drives each (project memory, A02.10).
    """

    miscoverage: float = Field(gt=0.0, le=0.5)
    n_calibration_drives: int = Field(ge=2, le=3)
    n_test_drives: int = Field(ge=1, le=3)
    min_train_drives: int = Field(ge=2)
    #: A cell needs this many *effective* calibration blocks (within-drive
    #: dependence removed) ...
    min_cell_blocks: int = Field(ge=5)
    #: ... and blocks from this many calibration drives.
    min_cell_drives: int = Field(ge=2)
    #: Refuse a reference whose calibration-fold mean residual in any cell
    #: exceeds this fraction of ``detectors.shift_of_interest``.
    max_bias_fraction: float = Field(gt=0.0, le=1.0)


class Detectors(_Strict):
    """CUSUM for the declared shift size; windowed GLR for unknown size/time."""

    #: The shift worth detecting, in the residual's unit. CUSUM's reference
    #: value is half of it, in each cell's standardized units.
    shift_of_interest: float = Field(gt=0.0)
    cusum_h: float = Field(gt=0.0)
    glr_window_blocks: int = Field(ge=2, le=500)
    glr_h: float = Field(gt=0.0)


class DriveLevel(_Strict):
    """Change between drives: one standardized unit per drive."""

    cusum_h: float = Field(gt=0.0)
    #: Limit of the ramp GLR (gradual drift, unknown start and slope).
    glr_h: float = Field(gt=0.0)
    window_drives: int = Field(ge=2, le=200)
    #: Floor under the estimated between-drive sd, in the residual's unit:
    #: three calibration drives cannot rule out a component near zero.
    between_drive_sd_floor: float = Field(gt=0.0)
    #: A drive with fewer evaluated blocks is unevaluable at drive level.
    min_drive_blocks: int = Field(ge=1)


class Persistence(_Strict):
    """How much evidence one alarm needs (see the module docstring)."""

    min_consecutive_blocks: int = Field(ge=1)
    clip_sigma: float = Field(gt=0.0)


class Deduplication(_Strict):
    merge_seconds: float = Field(ge=0.0)
    reset_at_drive_boundary: Literal[True]


class DeclaredShift(_Strict):
    """A synthetic fault injected for sensitivity evaluation — never real.

    ``step``/``ramp`` start ``start_after_seconds`` into each drive;
    ``drive_ramp`` adds ``magnitude`` times the drive's position (1, 2, ...)
    to every sample of each successive drive — a fault developing between
    drives, which per-drive statistics cannot accumulate.
    """

    name: str = Field(min_length=1)
    kind: Literal["step", "ramp", "drive_ramp"]
    magnitude: float
    start_after_seconds: float = Field(ge=0.0)
    ramp_seconds: float = Field(ge=0.0)
    synthetic: Literal[True]

    @model_validator(mode="after")
    def _shape(self) -> DeclaredShift:
        if self.magnitude == 0.0:
            raise ValueError(f"shift {self.name!r} has zero magnitude")
        if self.kind == "ramp" and self.ramp_seconds <= 0.0:
            raise ValueError(f"ramp shift {self.name!r} needs ramp_seconds > 0")
        if self.kind != "ramp" and self.ramp_seconds != 0.0:
            raise ValueError(f"{self.kind} shift {self.name!r} has no ramp")
        if self.kind == "drive_ramp" and self.start_after_seconds != 0.0:
            raise ValueError(f"drive_ramp {self.name!r} applies to whole drives")
        return self

    def offset(self, elapsed_since_onset: float, drive_position: int = 1) -> float:
        if self.kind == "drive_ramp":
            return self.magnitude * drive_position
        if elapsed_since_onset < 0.0:
            return 0.0
        if self.kind == "step":
            return self.magnitude
        return self.magnitude * min(1.0, elapsed_since_onset / self.ramp_seconds)


class ChangePolicy(_Strict):
    """Everything a change detector's verdicts depend on, declared."""

    schema_: Literal["canary-change-detection/2"] = Field(alias="schema")
    residual: str = Field(min_length=1)
    unit: str = Field(min_length=1)
    #: Reference regressors: ``"1"`` (intercept) or an expression the
    #: residual definition evaluates per sample (e.g. ``"1/obd:MAF"``).
    features: tuple[str, ...] = Field(min_length=1)
    excitation: Excitation
    dependence: Dependence
    calibration: IntervalCalibration
    detectors: Detectors
    drive_level: DriveLevel
    persistence: Persistence
    deduplication: Deduplication
    shifts: tuple[DeclaredShift, ...] = Field(min_length=1)

    model_config = ConfigDict(extra="forbid", frozen=True, populate_by_name=True)

    @model_validator(mode="after")
    def _one_unit_is_not_an_event(self) -> ChangePolicy:
        # One unit's CUSUM increment is at most clip_sigma (CUSUM inputs are
        # clipped that far beyond k). GLR inputs are clipped at clip_sigma
        # about zero, so one unit's GLR is at most clip_sigma**2 / 2. Both
        # limits must sit above what a single unit can reach; persistence
        # (consecutive blocks or drives beyond k) is required on top.
        clip = self.persistence.clip_sigma
        limits = {
            "detectors.cusum_h": (clip, self.detectors.cusum_h),
            "detectors.glr_h": (clip * clip / 2.0, self.detectors.glr_h),
            "drive_level.cusum_h": (clip, self.drive_level.cusum_h),
            "drive_level.glr_h": (clip * clip / 2.0, self.drive_level.glr_h),
        }
        for name, (reach, limit) in limits.items():
            if reach >= limit:
                raise ValueError(
                    f"persistence.clip_sigma {clip} lets a single block or drive reach "
                    f"{name} {limit}; a single exceedance must not be able to make an event"
                )
        return self

    def sha256(self) -> str:
        payload = self.model_dump(mode="json", by_alias=True)
        text = json.dumps(payload, sort_keys=True, separators=(",", ":"))
        return hashlib.sha256(text.encode()).hexdigest()


# ---------------------------------------------------------------------------
# Samples and blocks


@dataclass(frozen=True)
class Sample:
    """One paired, gated observation: residual target, regressors, condition."""

    t: float  # unix seconds (acquisition)
    y: float
    x: tuple[float, ...]
    condition: float


@dataclass(frozen=True)
class Block:
    """Samples averaged inside one cell and at most one block length."""

    drive: str
    start: float
    end: float
    cell: str
    n: int
    y: float
    x: tuple[float, ...]
    condition: float


class BlockAccumulator:
    """Streaming block formation; O(1) per sample, one open block of state."""

    def __init__(self, policy: ChangePolicy, drive: str) -> None:
        self._excitation = policy.excitation
        self._dependence = policy.dependence
        self._n_features = len(policy.features)
        self.drive = drive
        self.dropped: dict[str, int] = {"uncovered": 0, "short_block": 0}
        self._cell: str | None = None
        self._reset()

    def _reset(self) -> None:
        self._start = math.nan
        self._last = math.nan
        self._n = 0
        self._sum_y = 0.0
        self._sum_c = 0.0
        self._sum_x = [0.0] * self._n_features

    def _close(self) -> list[Block]:
        if self._n == 0 or self._cell is None:
            self._reset()
            return []
        if self._n < self._excitation.min_block_samples:
            self.dropped["short_block"] += self._n
            self._reset()
            return []
        block = Block(
            drive=self.drive,
            start=self._start,
            end=self._last,
            cell=self._cell,
            n=self._n,
            y=self._sum_y / self._n,
            x=tuple(value / self._n for value in self._sum_x),
            condition=self._sum_c / self._n,
        )
        self._reset()
        return [block]

    def push(self, sample: Sample) -> list[Block]:
        if len(sample.x) != self._n_features:
            raise ValueError(
                f"sample has {len(sample.x)} regressors; policy declares {self._n_features}"
            )
        cell = self._excitation.cell_of(sample.condition)
        if cell is None or not math.isfinite(sample.y) or not all(map(math.isfinite, sample.x)):
            self.dropped["uncovered"] += 1
            return []
        closed: list[Block] = []
        if self._n and (
            cell != self._cell
            or sample.t - self._last > self._dependence.max_gap_seconds
            or sample.t - self._start >= self._dependence.block_seconds
        ):
            closed = self._close()
        if self._n == 0:
            self._start = sample.t
            self._cell = cell
        self._last = sample.t
        self._n += 1
        self._sum_y += sample.y
        self._sum_c += sample.condition
        for i, value in enumerate(sample.x):
            self._sum_x[i] += value
        return closed

    def flush(self) -> list[Block]:
        return self._close()


def blocks_of(
    policy: ChangePolicy, drive: str, samples: Iterable[Sample]
) -> tuple[list[Block], dict[str, int]]:
    """All blocks of one drive's samples (in time order) and the drop counts."""
    acc = BlockAccumulator(policy, drive)
    out: list[Block] = []
    for sample in samples:
        out.extend(acc.push(sample))
    out.extend(acc.flush())
    return out, dict(acc.dropped)


# ---------------------------------------------------------------------------
# The frozen reference


class ReferenceRefusedError(ValueError):
    """The data cannot support a reference under the declared policy."""


class NotAFrozenReferenceError(TypeError):
    """Change detection was handed something other than a frozen reference."""


class CellLimits(_Strict):
    cell: str
    #: RMS of calibration block residuals about zero (bias included).
    scale: float = Field(gt=0.0)
    #: Split-conformal half-width at the policy's miscoverage (raw rank).
    halfwidth: float = Field(gt=0.0)
    #: Whether the conformal rank also holds on the *effective* block count.
    #: ``False``: an interval exists, but its coverage is not guaranteed.
    coverage_guaranteed: bool
    n_blocks: int = Field(ge=1)
    n_effective: float = Field(gt=0.0)
    n_drives: int = Field(ge=1)
    #: Pooled sd of drive-demeaned block residuals.
    within_sd: float = Field(gt=0.0)
    #: Between-drive sd (method of moments, floored).
    between_sd: float = Field(gt=0.0)
    #: Mean of the calibration drives' mean residuals (drives weighted equally).
    bias: float


class AlarmLimits(_Strict):
    """Detector limits, copied from the policy when the reference is frozen."""

    shift_of_interest: float = Field(gt=0.0)
    cusum_h: float = Field(gt=0.0)
    glr_window_blocks: int = Field(ge=2)
    glr_h: float = Field(gt=0.0)
    min_consecutive_blocks: int = Field(ge=1)
    clip_sigma: float = Field(gt=0.0)
    merge_seconds: float = Field(ge=0.0)
    drive_cusum_h: float = Field(gt=0.0)
    drive_glr_h: float = Field(gt=0.0)
    drive_window: int = Field(ge=2)
    min_drive_blocks: int = Field(ge=1)


#: Validation context used once, by the code that mints a fingerprint, to
#: render the content it hashes. Identity-compared: an equal dict is not it.
_SEALING: dict[str, bool] = {"sealing": True}


def reference_fingerprint(payload: dict[str, Any]) -> str:
    body = {k: v for k, v in payload.items() if k != "fingerprint"}
    text = json.dumps(body, sort_keys=True, separators=(",", ":"), default=str)
    return hashlib.sha256(text.encode()).hexdigest()


class FrozenReference(_Strict):
    """A validated reference and its alarm limits, immutable and self-hashed."""

    kind: Literal["frozen_reference"] = "frozen_reference"
    #: Only a held-out fit exists; a candidate becomes a reference through
    #: promotion (E03.6), never by being passed as one.
    lineage: Literal["held_out_fit"] = "held_out_fit"
    name: str = Field(min_length=1)
    residual: str = Field(min_length=1)
    unit: str = Field(min_length=1)
    features: tuple[str, ...] = Field(min_length=1)
    theta: tuple[float, ...] = Field(min_length=1)
    cells: tuple[CellLimits, ...]
    #: Declared cells left uncalibrated, with why.
    uncalibrated_cells: dict[str, str]
    alarm: AlarmLimits
    #: Bias-corrected lag-1 (what the limit and the inflation use) ...
    within_drive_lag1: float
    #: ... and the raw demeaned estimate it was corrected from.
    within_drive_lag1_raw: float
    variance_inflation: float = Field(ge=1.0)
    n_calibration_blocks: int = Field(ge=1)
    policy_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    train_drives: tuple[str, ...] = Field(min_length=1)
    calibration_drives: tuple[str, ...] = Field(min_length=1)
    #: The calibration (tune revision) every fitting drive ran on.
    calibration: str | None
    valid_from: datetime
    valid_to: datetime
    fingerprint: str = Field(pattern=r"^[0-9a-f]{64}$")

    @model_validator(mode="after")
    def _self_hashed(self, info: ValidationInfo) -> FrozenReference:
        if len(self.theta) != len(self.features):
            raise ValueError("theta and features differ in length")
        for stamp in (self.valid_from, self.valid_to):
            if stamp.tzinfo is None:
                raise ValueError("reference times are timezone-aware")
        if info.context is _SEALING:
            return self
        expected = reference_fingerprint(self.model_dump(mode="json"))
        if self.fingerprint != expected:
            raise ValueError(
                "fingerprint does not hash this reference's content: a reference is "
                "frozen when fitted, and an edited copy is a different, unvalidated model"
            )
        return self

    def predict(self, x: Sequence[float]) -> float:
        return float(
            np.dot(np.asarray(self.theta, dtype=np.float64), np.asarray(x, dtype=np.float64))
        )

    def limits(self, cell: str) -> CellLimits | None:
        return next((c for c in self.cells if c.cell == cell), None)


def _conformal_rank(n: float, miscoverage: float) -> int:
    return math.ceil((n + 1.0) * (1.0 - miscoverage))


def calibration_fold_bias(train: Sequence[Block], calibration: Sequence[Block]) -> dict[str, float]:
    """Per cell: the mean of the calibration drives' mean residuals against a
    fit on ``train`` — the quantity the bias gate tests, reported on its own
    so a refusal for another reason (too few effective blocks) does not hide
    whether the reference would also be biased."""
    design = np.asarray([b.x for b in train], dtype=np.float64)
    y = np.asarray([b.y for b in train], dtype=np.float64)
    theta, *_ = np.linalg.lstsq(design, y, rcond=None)
    by_cell: dict[str, dict[str, list[float]]] = {}
    for b in calibration:
        by_cell.setdefault(b.cell, {}).setdefault(b.drive, []).append(
            float(b.y - np.dot(theta, b.x))
        )
    return {
        cell: float(np.mean([np.mean(v) for v in drives.values()]))
        for cell, drives in by_cell.items()
    }


def fit_reference(
    policy: ChangePolicy,
    train: Sequence[Block],
    calibration: Sequence[Block],
    *,
    name: str,
    calibration_revision: str | None,
) -> FrozenReference:
    """Fit on ``train`` blocks, calibrate on ``calibration`` blocks, freeze.

    The two block sets must come from disjoint drives (refused otherwise):
    limits calibrated on the data that fitted the model are in-sample.
    """
    train_drives = sorted({b.drive for b in train})
    cal_drives = sorted({b.drive for b in calibration})
    if set(train_drives) & set(cal_drives):
        raise ReferenceRefusedError(
            f"drives {sorted(set(train_drives) & set(cal_drives))} are in both the training "
            "and calibration folds; calibration must be out of sample"
        )
    if len(train_drives) < policy.calibration.min_train_drives:
        raise ReferenceRefusedError(
            f"{len(train_drives)} training drive(s) with blocks; the policy needs "
            f"{policy.calibration.min_train_drives}"
        )
    if len(cal_drives) < 2:
        raise ReferenceRefusedError(f"{len(cal_drives)} calibration drive(s) with blocks; need 2")
    design = np.asarray([b.x for b in train], dtype=np.float64)
    y = np.asarray([b.y for b in train], dtype=np.float64)
    if np.linalg.matrix_rank(design) < design.shape[1]:
        raise ReferenceRefusedError(
            "the training blocks do not excite every reference regressor "
            f"({list(policy.features)}): the fit is not identified"
        )
    theta, *_ = np.linalg.lstsq(design, y, rcond=None)

    def residual(b: Block) -> float:
        return float(b.y - np.dot(theta, b.x))

    ordered = sorted(calibration, key=lambda b: (b.drive, b.start))
    # Per cell, per drive residuals.
    by_cell: dict[str, dict[str, list[float]]] = {}
    for b in ordered:
        by_cell.setdefault(b.cell, {}).setdefault(b.drive, []).append(residual(b))

    # (a) Within-drive dependence on drive-and-cell-demeaned residuals.
    within_var: dict[str, float] = {}
    for cell_name, drives in by_cell.items():
        sq = sum(float(np.sum((np.asarray(r) - np.mean(r)) ** 2)) for r in drives.values())
        dof = sum(len(r) for r in drives.values()) - len(drives)
        if dof > 0 and sq > 0:
            within_var[cell_name] = sq / dof
    num = den = 0.0
    for drive in cal_drives:
        z = [
            (residual(b) - float(np.mean(by_cell[b.cell][drive]))) / math.sqrt(within_var[b.cell])
            for b in ordered
            if b.drive == drive and b.cell in within_var
        ]
        arr = np.asarray(z, dtype=np.float64)
        den += float(np.sum(arr * arr))
        if len(arr) > 1:
            num += float(np.sum(arr[:-1] * arr[1:]))
    rho_raw = num / den if den > 0 else 0.0
    # Demeaning each drive biases lag-1 downward by about (1 + 3 rho) / n
    # (Marriott & Pope 1954; n blocks per drive). Invert that first-order
    # bias with n the mean blocks per calibration drive, so short drives
    # cannot make dependent residuals look independent.
    # Below eight blocks per drive the first-order correction is itself
    # unreliable (at five it turns a measured 0.09 into 0.73), so refuse.
    n_bar = len(calibration) / len(cal_drives)
    if n_bar < 8.0:
        raise ReferenceRefusedError(
            f"{n_bar:.1f} calibration blocks per drive: too few to estimate within-drive "
            "dependence (the demeaned lag-1 bias correction needs at least 8)"
        )
    rho = min((rho_raw + 1.0 / n_bar) / (1.0 - 3.0 / n_bar), 0.99)
    if rho > policy.dependence.max_within_drive_lag1:
        raise ReferenceRefusedError(
            f"within-drive lag-1 autocorrelation of calibration block residuals is "
            f"{rho:.2f} (bias-corrected from {rho_raw:.2f}), above the declared "
            f"{policy.dependence.max_within_drive_lag1}"
        )
    rho_pos = max(rho, 0.0)
    inflation = math.sqrt((1.0 + rho_pos) / (1.0 - rho_pos))
    deflate = (1.0 - rho_pos) / (1.0 + rho_pos)

    # (b) Per cell: between-drive component, bias, effective size, intervals.
    cells: list[CellLimits] = []
    uncalibrated: dict[str, str] = {}
    floor = policy.drive_level.between_drive_sd_floor
    alpha = policy.calibration.miscoverage
    for cell in policy.excitation.cells:
        drives = by_cell.get(cell.name, {})
        r = np.asarray([v for vals in drives.values() for v in vals], dtype=np.float64)
        n = len(r)
        n_eff = n * deflate
        if len(drives) < policy.calibration.min_cell_drives:
            uncalibrated[cell.name] = (
                f"blocks from {len(drives)} calibration drive(s); the policy needs "
                f"{policy.calibration.min_cell_drives}"
            )
            continue
        if n_eff < policy.calibration.min_cell_blocks or cell.name not in within_var:
            uncalibrated[cell.name] = (
                f"{n} calibration block(s), {n_eff:.1f} effective; the policy needs "
                f"{policy.calibration.min_cell_blocks} effective"
            )
            continue
        rank = _conformal_rank(n, alpha)
        if rank > n:
            uncalibrated[cell.name] = f"{n} blocks are too few for the conformal rank {rank}"
            continue
        means = np.asarray([np.mean(v) for v in drives.values()], dtype=np.float64)
        sizes = np.asarray([len(v) for v in drives.values()], dtype=np.float64)
        w_var = within_var[cell.name]
        tau2 = float(np.var(means, ddof=1)) - float(np.mean(w_var * inflation**2 / sizes))
        between = max(math.sqrt(max(tau2, 0.0)), floor)
        bias = float(np.mean(means))
        cells.append(
            CellLimits(
                cell=cell.name,
                scale=float(np.sqrt(np.mean(r * r))),
                halfwidth=float(np.sort(np.abs(r))[rank - 1]),
                coverage_guaranteed=_conformal_rank(n_eff, alpha) <= math.floor(n_eff),
                n_blocks=n,
                n_effective=n_eff,
                n_drives=len(drives),
                within_sd=math.sqrt(w_var),
                between_sd=between,
                bias=bias,
            )
        )
    if not cells:
        raise ReferenceRefusedError(f"no cell could be calibrated: {uncalibrated}")
    limit = policy.calibration.max_bias_fraction * policy.detectors.shift_of_interest
    biased = {c.cell: round(c.bias, 3) for c in cells if abs(c.bias) > limit}
    if biased:
        raise ReferenceRefusedError(
            f"the reference is biased on its own calibration fold: mean residual {biased} "
            f"{policy.unit} exceeds {policy.calibration.max_bias_fraction} x shift of interest "
            f"({limit:g}); CUSUM would accumulate on in-control data"
        )
    every = [*train, *calibration]
    payload: dict[str, Any] = {
        "kind": "frozen_reference",
        "lineage": "held_out_fit",
        "name": name,
        "residual": policy.residual,
        "unit": policy.unit,
        "features": list(policy.features),
        "theta": [float(v) for v in theta],
        "cells": [c.model_dump(mode="json") for c in cells],
        "uncalibrated_cells": uncalibrated,
        "alarm": AlarmLimits(
            shift_of_interest=policy.detectors.shift_of_interest,
            cusum_h=policy.detectors.cusum_h,
            glr_window_blocks=policy.detectors.glr_window_blocks,
            glr_h=policy.detectors.glr_h,
            min_consecutive_blocks=policy.persistence.min_consecutive_blocks,
            clip_sigma=policy.persistence.clip_sigma,
            merge_seconds=policy.deduplication.merge_seconds,
            drive_cusum_h=policy.drive_level.cusum_h,
            drive_glr_h=policy.drive_level.glr_h,
            drive_window=policy.drive_level.window_drives,
            min_drive_blocks=policy.drive_level.min_drive_blocks,
        ).model_dump(mode="json"),
        "within_drive_lag1": rho,
        "within_drive_lag1_raw": rho_raw,
        "variance_inflation": inflation,
        "n_calibration_blocks": len(calibration),
        "policy_sha256": policy.sha256(),
        "train_drives": train_drives,
        "calibration_drives": cal_drives,
        "calibration": calibration_revision,
        "valid_from": datetime.fromtimestamp(min(b.start for b in every), UTC).isoformat(),
        "valid_to": datetime.fromtimestamp(max(b.end for b in every), UTC).isoformat(),
    }
    unsealed = FrozenReference.model_validate(
        {**payload, "fingerprint": "0" * 64}, context=_SEALING
    )
    payload["fingerprint"] = reference_fingerprint(unsealed.model_dump(mode="json"))
    return FrozenReference.model_validate(payload)


# ---------------------------------------------------------------------------
# Monitoring


@dataclass
class ChangeEvent:
    """One deduplicated alarm episode of one detector in one direction."""

    detector: DetectorName
    direction: Direction
    drive: str
    first_alarm: float
    last_alarm: float
    #: The detector's own estimate of when the change began.
    estimated_onset: float
    n_alarms: int
    peak_statistic: float
    #: Mean residual over what the detector attributes to the change.
    shift_estimate: float
    alarm_id: str = ""


@dataclass(frozen=True)
class BlockVerdict:
    block: Block
    status: Literal["evaluated", "unevaluable"]
    reason: str | None
    residual: float | None
    z: float | None
    #: Inside the cell's conformal interval; ``None`` when unevaluable.
    inside: bool | None
    cusum_pos: float
    cusum_neg: float
    glr: float
    #: ``detector:direction`` alarms (persistence met) raised at this block.
    alarms: tuple[str, ...]
    reference_fingerprint: str
    #: Every alarm id unresolved when this verdict was issued (this block's
    #: own included). A candidate must not learn while any stands.
    open_alarms: tuple[str, ...]
    issuer: ChangeMonitor = field(compare=False, repr=False)


@dataclass(frozen=True)
class DriveVerdict:
    drive: str
    status: Literal["evaluated", "unevaluable"]
    reason: str | None
    n_blocks: int
    #: Combined mean residual and its sd under the reference's components.
    mean_residual: float | None
    sd: float | None
    #: Standardized (unclipped) drive unit.
    u: float | None
    cusum_pos: float
    cusum_neg: float
    glr: float
    alarms: tuple[str, ...]


@dataclass
class _Track:
    over: int = 0


@dataclass
class _DriveState:
    drive: str
    start: float = math.nan
    end: float = math.nan
    s_pos: float = 0.0
    s_neg: float = 0.0
    zero_pos_t: float = math.nan
    zero_neg_t: float = math.nan
    window: deque[tuple[float, float, float]] = field(default_factory=deque)  # (t, z, r)
    tracks: dict[str, _Track] = field(default_factory=dict)
    residuals: dict[str, list[float]] = field(default_factory=dict)


class ChangeMonitor:
    """Per-block CUSUM/GLR, drive-level CUSUM/ramp GLR and conformal checks
    against one frozen reference.

    Built from a :class:`FrozenReference` only, plus the ids of alarms still
    unresolved in memory (``unresolved``). It keeps no copy of any limit and
    offers no way to set one.
    """

    def __init__(self, reference: FrozenReference, *, unresolved: Iterable[str] = ()) -> None:
        if not isinstance(reference, FrozenReference):
            raise NotAFrozenReferenceError(
                "change detection runs against the frozen reference, never against an "
                f"adaptive candidate (got {type(reference).__name__}); a candidate that "
                "learned the change would hide it"
            )
        self._reference = reference
        self._state: _DriveState | None = None
        self._open: set[str] = set(unresolved)
        self._last_verdict: BlockVerdict | None = None
        self.events: list[ChangeEvent] = []
        self.drive_verdicts: list[DriveVerdict] = []
        # Drive-level state, carried across drives.
        self._d_pos = 0.0
        self._d_neg = 0.0
        self._d_zero_pos = math.nan
        self._d_zero_neg = math.nan
        self._d_last_toward: dict[str, bool] = {"+": False, "-": False}
        self._d_window: deque[tuple[float, float, float]] = deque(
            maxlen=reference.alarm.drive_window
        )  # (drive start, clipped u, mean residual)

    @property
    def reference(self) -> FrozenReference:
        return self._reference

    @property
    def open_alarms(self) -> tuple[str, ...]:
        return tuple(sorted(self._open))

    def resolve(self, alarms: Iterable[str]) -> None:
        """Alarms an investigation resolved; they stop holding the candidate."""
        self._open.difference_update(alarms)

    def issued(self, verdict: BlockVerdict) -> bool:
        """Whether ``verdict`` is the verdict this monitor issued last."""
        return verdict is self._last_verdict

    def start_drive(self, drive: str) -> None:
        self._state = _DriveState(drive=drive)
        self._last_verdict = None

    def _clip(self, z: float, k: float) -> float:
        """Bound one unit's contribution to ``clip_sigma`` *beyond* the
        reference value ``k``: a large shift still accumulates (by at most
        ``clip_sigma`` a unit), and no single unit can reach an alarm limit."""
        clip = k + self._reference.alarm.clip_sigma
        return min(max(z, -clip), clip)

    def _persist(self, state: _DriveState, key: str, over: bool) -> bool:
        track = state.tracks.setdefault(key, _Track())
        track.over = track.over + 1 if over else 0
        return track.over >= self._reference.alarm.min_consecutive_blocks

    def _emit(
        self,
        detector: DetectorName,
        direction: Direction,
        drive: str,
        t: float,
        onset: float,
        statistic: float,
        shift: float,
        merge: bool,
    ) -> str:
        if merge:
            for event in reversed(self.events):
                if event.drive != drive:
                    break
                if (
                    event.detector == detector
                    and event.direction == direction
                    and t - event.last_alarm <= self._reference.alarm.merge_seconds
                ):
                    event.last_alarm = t
                    event.n_alarms += 1
                    event.peak_statistic = max(event.peak_statistic, statistic)
                    return event.alarm_id
        alarm_id = (
            f"change:{self._reference.fingerprint[:12]}:{drive}:{detector}:{direction}:{t:.0f}"
        )
        self.events.append(
            ChangeEvent(
                detector=detector,
                direction=direction,
                drive=drive,
                first_alarm=t,
                last_alarm=t,
                estimated_onset=onset,
                n_alarms=1,
                peak_statistic=statistic,
                shift_estimate=shift,
                alarm_id=alarm_id,
            )
        )
        self._open.add(alarm_id)
        return alarm_id

    def _verdict(
        self,
        block: Block,
        status: Literal["evaluated", "unevaluable"],
        reason: str | None,
        residual: float | None,
        z: float | None,
        inside: bool | None,
        cusum_pos: float,
        cusum_neg: float,
        glr: float,
        alarms: tuple[str, ...],
    ) -> BlockVerdict:
        verdict = BlockVerdict(
            block=block,
            status=status,
            reason=reason,
            residual=residual,
            z=z,
            inside=inside,
            cusum_pos=cusum_pos,
            cusum_neg=cusum_neg,
            glr=glr,
            alarms=alarms,
            reference_fingerprint=self._reference.fingerprint,
            open_alarms=self.open_alarms,
            issuer=self,
        )
        self._last_verdict = verdict
        return verdict

    def observe(self, block: Block) -> BlockVerdict:
        state = self._state
        if state is None or state.drive != block.drive:
            raise ValueError("start_drive(<the block's drive>) first: statistics are per drive")
        if math.isnan(state.start):
            state.start = block.start
        state.end = block.end
        ref = self._reference
        limits = ref.limits(block.cell)
        if limits is None:
            reason = ref.uncalibrated_cells.get(block.cell, "cell not declared")
            return self._verdict(
                block, "unevaluable", f"uncovered: {reason}", None, None, None,
                state.s_pos, state.s_neg, 0.0, (),
            )  # fmt: skip
        r = block.y - ref.predict(block.x)
        state.residuals.setdefault(block.cell, []).append(r)
        z_raw = r / limits.scale / ref.variance_inflation
        k = ref.alarm.shift_of_interest / (2.0 * limits.scale * ref.variance_inflation)
        z = self._clip(z_raw, k)
        h = ref.alarm.cusum_h
        if state.s_pos == 0.0:
            state.zero_pos_t = block.start
        if state.s_neg == 0.0:
            state.zero_neg_t = block.start
        state.s_pos = max(0.0, state.s_pos + z - k)
        state.s_neg = max(0.0, state.s_neg - z - k)
        alarms: list[str] = []
        for direction, stat, onset, toward in (
            ("+", state.s_pos, state.zero_pos_t, z),
            ("-", state.s_neg, state.zero_neg_t, -z),
        ):
            # Persistence counts consecutive blocks that are themselves beyond
            # k in this direction while the statistic is over h.
            if self._persist(state, f"cusum{direction}", stat >= h and toward > k):
                window = [w for w in state.window if w[0] >= onset] + [(block.start, z, r)]
                shift = float(np.mean([w[2] for w in window]))
                self._emit(
                    "cusum", direction, state.drive, block.end, onset, stat, shift,  # type: ignore[arg-type]
                    merge=True,
                )  # fmt: skip
                alarms.append(f"cusum:{direction}")
                if direction == "+":
                    state.s_pos = 0.0
                else:
                    state.s_neg = 0.0
                state.tracks[f"cusum{direction}"].over = 0

        # GLR takes the value clipped at clip_sigma about zero, so one block's
        # GLR is at most clip_sigma**2 / 2 (the policy keeps that under glr_h).
        state.window.append((block.start, self._clip(z_raw, 0.0), r))
        while len(state.window) > ref.alarm.glr_window_blocks:
            state.window.popleft()
        glr, onset_idx, total = 0.0, len(state.window) - 1, 0.0
        running = 0.0
        for m, (_, zz, _) in enumerate(reversed(state.window), start=1):
            running += zz
            g = running * running / (2.0 * m)
            if g > glr:
                glr, onset_idx, total = g, len(state.window) - m, running
        direction_glr: Direction = "+" if total >= 0 else "-"
        toward = z if direction_glr == "+" else -z
        if self._persist(state, "glr", glr >= ref.alarm.glr_h and toward > k):
            attributed = list(state.window)[onset_idx:]
            shift = float(np.mean([w[2] for w in attributed]))
            self._emit(
                "glr", direction_glr, state.drive, block.end, attributed[0][0], glr, shift,
                merge=True,
            )  # fmt: skip
            alarms.append(f"glr:{direction_glr}")
            state.window.clear()
            state.tracks["glr"].over = 0
        inside = abs(r) <= limits.halfwidth
        return self._verdict(
            block, "evaluated", None, r, z_raw, inside, state.s_pos, state.s_neg, glr,
            tuple(alarms),
        )  # fmt: skip

    def close_drive(self) -> DriveVerdict:
        """End the drive: one standardized unit into the drive-level detectors."""
        state = self._state
        if state is None:
            raise ValueError("no drive is open")
        self._state = None
        self._last_verdict = None
        ref = self._reference
        n_blocks = sum(len(v) for v in state.residuals.values())

        def verdict(**kw: Any) -> DriveVerdict:
            out = DriveVerdict(
                drive=state.drive,
                n_blocks=n_blocks,
                cusum_pos=self._d_pos,
                cusum_neg=self._d_neg,
                **kw,
            )
            self.drive_verdicts.append(out)
            return out

        if n_blocks < ref.alarm.min_drive_blocks:
            return verdict(
                status="unevaluable",
                reason=(
                    f"{n_blocks} evaluated block(s); the drive level needs "
                    f"{ref.alarm.min_drive_blocks}"
                ),
                mean_residual=None, sd=None, u=None, glr=0.0, alarms=(),
            )  # fmt: skip
        inflation2 = ref.variance_inflation**2
        parts = []
        for cell, values in state.residuals.items():
            limits = ref.limits(cell)
            assert limits is not None
            parts.append(
                (float(np.mean(values)), limits.within_sd**2 * inflation2 / len(values), limits)
            )
        inv = np.asarray([1.0 / p[1] for p in parts])
        weights = inv / inv.sum()
        mean = float(sum(w * p[0] for w, p in zip(weights, parts, strict=True)))
        shared = float(sum(w * p[2].between_sd for w, p in zip(weights, parts, strict=True)))
        within = float(sum(w * w * p[1] for w, p in zip(weights, parts, strict=True)))
        sd = math.sqrt(shared * shared + within)
        u_raw = mean / sd
        k = ref.alarm.shift_of_interest / (2.0 * sd)
        u = self._clip(u_raw, k)
        # Drive-level persistence: an alarm needs this drive and the previous
        # evaluated one each beyond k toward it. A single wild drive followed
        # by in-control drives is a level jump that went away, not a change.
        toward = {"+": u_raw > k, "-": -u_raw > k}
        previous = self._d_last_toward
        self._d_last_toward = toward
        persists = {d: toward[d] and previous[d] for d in ("+", "-")}
        if self._d_pos == 0.0:
            self._d_zero_pos = state.start
        if self._d_neg == 0.0:
            self._d_zero_neg = state.start
        self._d_pos = max(0.0, self._d_pos + u - k)
        self._d_neg = max(0.0, self._d_neg - u - k)
        self._d_window.append((state.start, self._clip(u_raw, 0.0), mean))
        alarms: list[str] = []
        for direction, stat, onset in (
            ("+", self._d_pos, self._d_zero_pos),
            ("-", self._d_neg, self._d_zero_neg),
        ):
            if stat >= ref.alarm.drive_cusum_h and persists[direction]:
                attributed = [w[2] for w in self._d_window if w[0] >= onset]
                self._emit(
                    "drive_cusum", direction, state.drive, state.end, onset, stat,  # type: ignore[arg-type]
                    float(np.mean(attributed)), merge=False,
                )  # fmt: skip
                alarms.append(f"drive_cusum:{direction}")
                if direction == "+":
                    self._d_pos = 0.0
                else:
                    self._d_neg = 0.0
        # Ramp GLR: u_i = beta * (i - j + 1) for i >= j, beta and j unknown.
        window = list(self._d_window)
        glr, onset_idx, sign = 0.0, len(window) - 1, 1.0
        for j in range(len(window)):
            steps = np.arange(1, len(window) - j + 1, dtype=np.float64)
            us = np.asarray([w[1] for w in window[j:]], dtype=np.float64)
            dot = float(steps @ us)
            g = dot * dot / (2.0 * float(steps @ steps))
            if g > glr:
                glr, onset_idx, sign = g, j, math.copysign(1.0, dot)
        # A ramp needs at least two drives: one drive is a level, not a trend,
        # and one drive alone must not alarm.
        ramp_direction: Direction = "+" if sign > 0 else "-"
        if (
            glr >= ref.alarm.drive_glr_h
            and len(window) - onset_idx >= 2
            and persists[ramp_direction]
        ):
            self._emit(
                "drive_glr", ramp_direction, state.drive, state.end, window[onset_idx][0], glr,
                float(window[-1][2]), merge=False,
            )  # fmt: skip
            alarms.append(f"drive_glr:{ramp_direction}")
            self._d_window.clear()
        return verdict(
            status="evaluated", reason=None, mean_residual=mean, sd=sd, u=u_raw, glr=glr,
            alarms=tuple(alarms),
        )  # fmt: skip
