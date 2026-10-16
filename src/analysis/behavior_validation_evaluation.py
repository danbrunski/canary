"""Behavior-validation row evaluation and event projection (R20).

:func:`evaluate_rows` grades every row of a frame through the single B01
eligibility evaluator, with the denominators kept apart (rows, input-present,
fresh, eligible, evaluated); an ineligible row is never an ``ok``
measurement. Events are the daemon's alert path offline: its
:class:`~src.models.fusion.PersistenceGate` and emitter dedupe
(:class:`EventPolicy`), or a model's own fitted band-excess persistence
(:func:`model_event_times`, B05.3).
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass

import numpy as np
import pandas as pd

from src.analysis.behavior_validation_inputs import (
    AXIS_PAIRING_COLUMN,
    ProfileScope,
    _model_axes,
    axis_pairing_bounds,
    axis_separation_column,
    profile_refusals,
)
from src.analysis.behavior_validation_schema import BehaviorValidationDocument
from src.analysis.splits import DEFAULT_JOIN_GAP_SECONDS
from src.behavior.behavior_decoder import band_half_widths, explain_expected_column, grade_deviation
from src.behavior.behavior_eligibility import (
    OFF_MAP,
    ServingSpec,
    eligibility_reasons,
    serving_spec,
)
from src.behavior.behavior_models import (
    BandExcessPersistence,
    BehaviorModel1D,
    BehaviorModel2D,
    FittedBandExcessPersistence,
)
from src.behavior.session_context import (
    StartContextSpec,
    append_start_context,
    start_context_columns,
)
from src.columns import MODE_COLUMN, SESSION_COLUMN, TIMESTAMP_COLUMN, gap_column
from src.config import CanaryConfig
from src.dataframe_utils import numeric_values
from src.models.fusion import BandExcessAccumulator, PersistenceGate
from src.signals.pairing import to_seconds

# ---------------------------------------------------------------------------
# Row evaluation
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class EventPolicy:
    """The daemon's alert path, offline: persistence gate + emitter dedupe."""

    required: int = 3
    window: int | None = 5
    window_seconds: float | None = 8.0
    dedupe_seconds: float = 30.0
    interval_seconds: float = 1.0
    #: Exposure (the events/h denominator) stops accumulating across a gap
    #: between evaluated rows longer than this: the drive join gap, so rows
    #: that would have been two drives in two directories are two watches in
    #: one directory too (see :func:`_evaluated_hours`).
    exposure_break_seconds: float = DEFAULT_JOIN_GAP_SECONDS

    @classmethod
    def from_config(
        cls, config: CanaryConfig, document: BehaviorValidationDocument | None = None
    ) -> EventPolicy:
        alerts = config.alerts
        return cls(
            required=alerts.min_consecutive_ticks,
            window=alerts.persistence_window_ticks,
            window_seconds=alerts.persistence_window_seconds,
            dedupe_seconds=alerts.min_seconds_between_events,
            interval_seconds=1.0 / config.preprocessing.resample_hz,
            exposure_break_seconds=(
                document.drive_join_gap_seconds
                if document is not None
                else DEFAULT_JOIN_GAP_SECONDS
            ),
        )

    def describe(self) -> str:
        return (
            f"{self.required}-of-{self.window or self.required} ticks within "
            f"{self.window_seconds} s, then {self.dedupe_seconds} s dedupe"
        )


def axis_separation_summary(
    frame: pd.DataFrame, ev: RowEvaluation, model: BehaviorModel1D | BehaviorModel2D
) -> dict[str, dict[str, float | int | None]]:
    """Per axis, the acquisition separation of the graded rows' pairs:
    percentiles of per-row values, never of aggregates."""
    out: dict[str, dict[str, float | int | None]] = {}
    for axis in _model_axes(model):
        column = axis_separation_column(axis)
        if column not in frame.columns:
            continue
        sep = numeric_values(frame, column)[ev.evaluated]
        sep = sep[np.isfinite(sep)]
        out[axis] = {
            "graded": int(sep.size),
            **{
                f"p{q}": (float(np.percentile(sep, q)) if sep.size else None)
                for q in (50, 90, 95, 99)
            },
            "max": float(sep.max()) if sep.size else None,
        }
    return out


@dataclass(frozen=True)
class RowEvaluation:
    """Per-row outcome arrays for one model over one frame."""

    pieces: np.ndarray
    seconds: np.ndarray
    modes: np.ndarray
    present: np.ndarray
    fresh: np.ndarray
    reasons: np.ndarray
    expected: np.ndarray
    actual: np.ndarray
    severity: np.ndarray
    warning_half_width: np.ndarray
    critical_half_width: np.ndarray
    x: np.ndarray
    y: np.ndarray
    #: B04.9 — per-axis bound the rows were paired under (one row per output
    #: read), or ``None`` for rows of the resampled grid.
    axis_pairing_s: Mapping[str, float] | None = None

    @property
    def evaluated(self) -> np.ndarray:
        return np.asarray(pd.isna(self.reasons), dtype=bool)

    @property
    def eligible(self) -> np.ndarray:
        off_map = np.asarray([reason == OFF_MAP for reason in self.reasons], dtype=bool)
        return np.asarray(self.evaluated | off_map, dtype=bool)

    @property
    def residual(self) -> np.ndarray:
        return np.asarray(self.actual - self.expected, dtype=float)

    def subset(self, mask: np.ndarray) -> RowEvaluation:
        return RowEvaluation(
            **{
                name: getattr(self, name)[mask]
                for name in self.__dataclass_fields__
                if name != "axis_pairing_s"
            },
            axis_pairing_s=self.axis_pairing_s,
        )


def _seconds(frame: pd.DataFrame, interval: float) -> np.ndarray:
    """Epoch seconds per row.

    The unit is pinned to nanoseconds before the integer view: session frames
    carry ``datetime64[us]`` (pandas 3), and ``int64 / 1e9`` on microseconds
    compressed every interval 1000x — the persistence window (8 s) and dedupe
    (30 s) then spanned hours of rows and each synthetic window a whole
    drive. Evaluator version 2 fixed that (B04.3).
    """
    if TIMESTAMP_COLUMN in frame.columns:
        return to_seconds(frame[TIMESTAMP_COLUMN])
    return np.arange(len(frame), dtype=float) * interval


def evaluate_rows(
    model: BehaviorModel1D | BehaviorModel2D,
    frame: pd.DataFrame,
    *,
    spec: ServingSpec | None = None,
    interval_seconds: float = 1.0,
    output_offset: float | np.ndarray = 0.0,
    profile_scope: ProfileScope = "default_profile",
) -> RowEvaluation:
    """Evaluate ``model`` on every row of ``frame`` through the B01 evaluator.

    ``spec`` replaces the row-selection contract — used to reproduce a legacy
    artifact (no persisted contract) under its catalog recipe's contract; the
    model's own gates still apply on top. ``output_offset`` adds an explicitly
    synthetic step to the actual output column (and to any input that *is*
    that column), then re-evaluates — derived columns are not recomputed. A
    per-row array (aligned with ``frame``) injects a shaped excursion, e.g. a
    short transient control (B04.3).

    ``profile_scope`` (B05.3; R03.12): by default every row goes through
    :func:`profile_refusals`, so a stamped non-default-profile row is refused
    by name and an *unstamped* frame holding a capture or tuning piece raises
    :class:`UnverifiedProfileError` instead of being graded. Only a historical
    reproduction of numbers an artifact recorded before the rule passes
    ``"every_recording"``.
    """
    if profile_scope == "default_profile":
        profile_refusals(frame)  # an unverified frame is refused before any work
    model_spec = serving_spec(model)
    spec = spec or model_spec
    output = model_spec.output
    work = frame
    cold = spec.eligibility.cold_start if spec.eligibility is not None else None
    if cold is not None and SESSION_COLUMN in work.columns:
        # Same engine-start fold the refit and live source run; a legacy
        # model's frame was built without a contract that asked for it.
        if any(column not in work.columns for column in start_context_columns(cold)):
            work = append_start_context(work, [StartContextSpec.from_requirement(cold)])
    offset = np.asarray(output_offset, dtype=float)
    if offset.ndim > 0 and offset.shape != (len(work),):
        raise ValueError(f"output_offset has {offset.shape[0]} rows, frame has {len(work)}")
    if np.any(offset != 0.0):
        work = work.copy()
        work[output] = pd.to_numeric(work[output], errors="coerce").to_numpy(dtype=float) + offset
    channels = [*model_spec.inputs, output]
    present = np.ones(len(work), dtype=bool)
    fresh = np.ones(len(work), dtype=bool)
    for channel in channels:
        values = numeric_values(work, channel)
        present &= np.isfinite(values)
        gap = gap_column(channel)
        if gap in work.columns:
            fresh &= ~work[gap].fillna(False).astype(bool).to_numpy()
    fresh &= present

    contract_reasons = eligibility_reasons(work, spec, include_actual=True)
    expected, model_reasons = explain_expected_column(work, model)
    reasons = np.where(pd.isna(contract_reasons), model_reasons, contract_reasons).astype(object)
    # B04.9 — a read whose axes had no partner within the bound is refused as
    # that, not as the missing value the absent partner leaves behind.
    pairing_s = axis_pairing_bounds(work, model)
    if pairing_s is not None:
        unpaired = work[AXIS_PAIRING_COLUMN].to_numpy(dtype=object)
        unpaired = np.asarray([None if pd.isna(v) else str(v) for v in unpaired], dtype=object)
        reasons = np.where(pd.isna(unpaired), reasons, unpaired).astype(object)
    if profile_scope == "default_profile":
        # B05.3 — a stamped non-default-profile row is refused by name first.
        profile = profile_refusals(work)
        reasons = np.where(pd.isna(profile), reasons, profile).astype(object)
    actual = numeric_values(work, output)
    evaluated = np.asarray(pd.isna(reasons), dtype=bool) & np.isfinite(expected)
    evaluated &= np.isfinite(actual)
    # A row the contract passed with a finite expected but no finite actual
    # already carries an actual_* reason from ``include_actual``.
    severity = np.full(len(work), None, dtype=object)
    warn = np.full(len(work), np.nan)
    crit = np.full(len(work), np.nan)
    axis_channels = (
        [model.axis.channel]
        if isinstance(model, BehaviorModel1D)
        else [axis.channel for axis in model.axes]
    )
    axis_values = [numeric_values(work, channel) for channel in axis_channels]
    for i in np.flatnonzero(evaluated):
        point = {
            channel: float(values[i])
            for channel, values in zip(axis_channels, axis_values, strict=True)
        }
        severity[i] = grade_deviation(model, point, float(actual[i] - expected[i]))
        w, c = band_half_widths(model, point)
        warn[i] = np.nan if w is None else w
        crit[i] = np.nan if c is None else c
    pieces = (
        work[SESSION_COLUMN].astype(str).to_numpy()
        if SESSION_COLUMN in work.columns
        else np.full(len(work), "", dtype=object)
    )
    modes = (
        work[MODE_COLUMN].astype(str).to_numpy()
        if MODE_COLUMN in work.columns
        else np.full(len(work), "all", dtype=object)
    )
    return RowEvaluation(
        pieces=pieces,
        seconds=_seconds(work, interval_seconds),
        modes=modes,
        present=present,
        fresh=fresh,
        reasons=reasons,
        expected=np.where(evaluated, expected, np.nan),
        actual=actual,
        severity=severity,
        warning_half_width=warn,
        critical_half_width=crit,
        x=axis_values[0],
        y=axis_values[1] if len(axis_values) > 1 else np.full(len(work), np.nan),
        axis_pairing_s=pairing_s,
    )


# ---------------------------------------------------------------------------
# Events
# ---------------------------------------------------------------------------


def _verdicts(ev: RowEvaluation) -> np.ndarray:
    """Per-row detector verdict: ``None`` (no measurement), True/False."""
    out = np.full(len(ev.reasons), None, dtype=object)
    evaluated = ev.evaluated
    out[evaluated] = [sev in ("warning", "critical") for sev in ev.severity[evaluated]]
    return out


def count_events(
    verdicts: np.ndarray,
    seconds: np.ndarray,
    groups: np.ndarray,
    policy: EventPolicy,
) -> int:
    """Events emitted by the daemon's persistence gate + dedupe, per group.

    ``groups`` resets the gate (one per drive or synthetic window); refused
    rows are missing verdicts that freeze the window, exactly as live.
    """
    return len(event_times(verdicts, seconds, groups, policy))


def event_times(
    verdicts: np.ndarray,
    seconds: np.ndarray,
    groups: np.ndarray,
    policy: EventPolicy,
) -> list[tuple[object, float]]:
    """``(group, emit time)`` for every event :func:`count_events` counts."""
    events: list[tuple[object, float]] = []
    # One sort instead of a mask per group: synthetic windows number in the
    # thousands on a month of drives. Within a group rows are time-ordered,
    # ties keeping row order (lexsort is stable).
    codes, uniques = pd.factorize(pd.Series(groups, dtype=object), sort=False)
    ordered = np.lexsort((seconds, codes))
    boundaries = np.flatnonzero(np.diff(codes[ordered])) + 1
    for order in np.split(ordered, boundaries):
        if not order.size:
            continue
        code = codes[order[0]]
        group = uniques[code] if code >= 0 else groups[order[0]]
        gate = PersistenceGate(
            policy.required, window=policy.window, window_seconds=policy.window_seconds
        )
        last_emit: float | None = None
        for i in order:
            verdict = verdicts[i]
            now = float(seconds[i]) if np.isfinite(seconds[i]) else 0.0
            sustained = gate.observe("behavior", verdict, now=now)
            if verdict is not True or not sustained:
                continue
            if last_emit is not None and now - last_emit < policy.dedupe_seconds:
                continue
            last_emit = now
            events.append((group, now))
    return events


def band_excess_event_times(
    ev: RowEvaluation,
    groups: np.ndarray,
    persistence: FittedBandExcessPersistence,
    *,
    dedupe_seconds: float,
) -> list[tuple[object, float]]:
    """``(group, emit time)`` under a model's fitted band-excess persistence (B05.3).

    The daemon's path, offline: per group a fresh :class:`PersistenceGate`
    whose :meth:`~PersistenceGate.observe_band_excess` holds the accumulator,
    an event only on a firing tick that is sustained, then the emitter
    dedupe. Refused rows are missing verdicts that leave the sums untouched.
    """
    events: list[tuple[object, float]] = []
    verdicts = _verdicts(ev)
    residual = ev.residual
    half_width = ev.warning_half_width
    codes, uniques = pd.factorize(pd.Series(groups, dtype=object), sort=False)
    ordered = np.lexsort((ev.seconds, codes))
    boundaries = np.flatnonzero(np.diff(codes[ordered])) + 1
    for order in np.split(ordered, boundaries):
        if not order.size:
            continue
        code = codes[order[0]]
        group = uniques[code] if code >= 0 else groups[order[0]]
        gate = PersistenceGate(1)
        last_emit: float | None = None
        for i in order:
            verdict = verdicts[i]
            now = float(ev.seconds[i]) if np.isfinite(ev.seconds[i]) else 0.0
            width = float(half_width[i]) if np.isfinite(half_width[i]) else None
            sustained = gate.observe_band_excess(
                "behavior",
                float(residual[i]) if verdict is not None else None,
                width,
                firing=verdict,
                limit=persistence.limit,
                reset_gap_seconds=persistence.rule.reset_gap_seconds,
                now=now,
            )
            if verdict is not True or not sustained:
                continue
            if last_emit is not None and now - last_emit < dedupe_seconds:
                continue
            last_emit = now
            events.append((group, now))
    return events


def model_event_times(
    model: BehaviorModel1D | BehaviorModel2D,
    ev: RowEvaluation,
    groups: np.ndarray,
    policy: EventPolicy,
) -> list[tuple[object, float]]:
    """Events ``model`` emits over ``ev``: its own persistence, else the daemon gate.

    The one place the evaluator decides how a model is evented, so held-out
    control rates, synthetic sensitivity and transient controls all count
    what the daemon would emit for *this* model (B05.3).
    """
    if model.event_persistence is not None:
        return band_excess_event_times(
            ev, groups, model.event_persistence, dedupe_seconds=policy.dedupe_seconds
        )
    return event_times(_verdicts(ev), ev.seconds, groups, policy)


#: B05.3 — why a band-excess persistence could not be fitted on a training fold.
PERSISTENCE_NO_TRAINING_EXCURSION = "event_persistence_unfittable:no_training_excursion"


def fit_band_excess_persistence(
    rule: BandExcessPersistence,
    train_ev: RowEvaluation,
    groups: np.ndarray,
) -> FittedBandExcessPersistence | str:
    """Fit ``rule``'s limit on the training fold's own rows (B02.3, B05.3).

    Runs the accumulator over every evaluated training row, per drive group
    and in time order, and takes the largest accumulated excess reached: the
    limit an event must strictly exceed, so no ordinary excursion on the
    training drives completes one. Test drives never enter. Returns a named
    refusal instead of a zero limit when no training row ever left the band
    (nothing says how large ordinary control gets), or when there are no
    evaluated rows at all.
    """
    evaluated = train_ev.evaluated & np.isfinite(train_ev.warning_half_width)
    if not evaluated.any():
        return f"{PERSISTENCE_NO_TRAINING_EXCURSION}: no evaluated training rows"
    largest = 0.0
    excursions = 0
    drives: set[str] = set()
    residual = train_ev.residual
    width = train_ev.warning_half_width
    for group in pd.unique(pd.Series(groups[evaluated], dtype=object)):
        rows = np.flatnonzero(evaluated & (groups == group))
        rows = rows[np.argsort(train_ev.seconds[rows], kind="stable")]
        drives.add(str(group))
        accumulator = BandExcessAccumulator(rule.reset_gap_seconds)
        for i in rows:
            now = float(train_ev.seconds[i])
            last = accumulator.last_seconds
            restarts = last is None or now - last > rule.reset_gap_seconds
            idle = restarts or accumulator.statistic == 0.0
            after = accumulator.update(float(residual[i]), float(width[i]), now)
            if idle and after > 0.0:
                excursions += 1
            largest = max(largest, after)
    if largest <= 0.0:
        return (
            f"{PERSISTENCE_NO_TRAINING_EXCURSION}: {int(evaluated.sum())} evaluated training "
            "rows never left the warning band"
        )
    return FittedBandExcessPersistence(
        rule=rule,
        limit=largest,
        training_drives=sorted(drives),
        training_rows=int(evaluated.sum()),
        training_excursions=excursions,
    )
