"""Session context a behavior model may require but no single row carries (B01.5).

A warmup model is only meaningful on an engine start that began cold. The
initial coolant temperature is a property of the *start*, not of the current
row, so it has to be carried forward as context — and it has to come from
recorded evidence, never be invented:

* **Evidence of a start** is either an observed transition from a stopped
  engine (a fresh RPM below the running threshold) to a running one, or a
  run-time clock (``RUN_TIME``) no older than ``max_start_runtime_s`` on the
  first running row. The coolant reading taken at that start becomes the
  initial temperature.
* **No evidence** — a daemon that starts mid-drive, a replay that begins
  mid-drive, a reconnect that cannot place the start — leaves the initial
  temperature unknown. Unknown is reported as unknown; it is never read as
  cold, and a restart cannot manufacture it.
* **Boundaries.** A new session, a replay entry/exit (``LiveFeatureSource.
  reset``), an engine stop, or a run-time clock that goes backwards (the
  engine restarted, possibly during a reconnect gap) all end the previous
  start's context.

:class:`EngineStartTracker` is the single implementation: the live source
feeds it one tick at a time (so its memory outlives the ring buffer), and
:func:`append_start_context` folds it over each recorded session in time
order for the batch/refit path. Both see the same rows — values are NaN when
the resampler (batch) or the bounded live snapshot marked them stale.

The emitted ``context:*`` columns are serving context only: the rolling
feature builder skips them and they are never trainable features.
"""

from __future__ import annotations

import math
from collections.abc import Iterable
from dataclasses import dataclass

import numpy as np
import pandas as pd

from src.decode.behavior_models import BehaviorModels, ColdStartRequirement
from src.preprocessing.features import CONTEXT_COLUMN_PREFIX
from src.preprocessing.resampler import SESSION_COLUMN, TIMESTAMP_COLUMN

#: Prefix of serving-context columns. Excluded from rolling features (see
#: ``features._value_columns``) and from every trainable feature selection.
CONTEXT_PREFIX = CONTEXT_COLUMN_PREFIX


@dataclass(frozen=True)
class StartContextSpec:
    """The channels and thresholds that define one start-context tracker."""

    rpm_channel: str
    runtime_channel: str
    coolant_channel: str
    min_running_rpm: float
    max_start_runtime_s: float

    @classmethod
    def from_requirement(cls, requirement: ColdStartRequirement) -> StartContextSpec:
        return cls(
            rpm_channel=requirement.rpm_channel,
            runtime_channel=requirement.runtime_channel,
            coolant_channel=requirement.coolant_channel,
            min_running_rpm=float(requirement.min_running_rpm),
            max_start_runtime_s=float(requirement.max_start_runtime_s),
        )

    @property
    def key(self) -> str:
        return (
            f"{self.rpm_channel}>={self.min_running_rpm:g},"
            f"{self.runtime_channel}<={self.max_start_runtime_s:g},{self.coolant_channel}"
        )

    @property
    def observed_column(self) -> str:
        """1.0 when the current engine run's start was observed with a
        coolant reading, else 0.0."""
        return f"{CONTEXT_PREFIX}ENGINE_START_OBSERVED[{self.key}]"

    @property
    def start_coolant_column(self) -> str:
        """Coolant at the observed start; NaN when unknown."""
        return f"{CONTEXT_PREFIX}START_COOLANT[{self.key}]"


def start_context_columns(requirement: ColdStartRequirement) -> tuple[str, str]:
    """``(observed_column, start_coolant_column)`` for a requirement."""
    spec = StartContextSpec.from_requirement(requirement)
    return spec.observed_column, spec.start_coolant_column


def start_context_specs(models: BehaviorModels | None) -> list[StartContextSpec]:
    """Distinct trackers the loaded models need, in first-seen order."""
    if models is None:
        return []
    specs: list[StartContextSpec] = []
    for model in models.models:
        eligibility = model.eligibility
        if eligibility is None or eligibility.cold_start is None:
            continue
        spec = StartContextSpec.from_requirement(eligibility.cold_start)
        if spec not in specs:
            specs.append(spec)
    return specs


def _finite(value: object) -> float | None:
    if value is None:
        return None
    try:
        out = float(value)  # type: ignore[arg-type]  # except handles non-numeric
    except (TypeError, ValueError):
        return None
    return out if math.isfinite(out) else None


class EngineStartTracker:
    """Stateful start-evidence fold shared by the live and batch paths."""

    def __init__(self, spec: StartContextSpec) -> None:
        self.spec = spec
        self.reset()

    def reset(self) -> None:
        """Forget everything: the next start must be observed afresh."""
        #: A fresh stopped-engine reading has been seen since the last run,
        #: so the next running row is an observed start.
        self._saw_stopped = False
        self._running = False
        self._evidence = False
        self._start_coolant: float | None = None
        self._last_runtime: float | None = None
        self._start_time: float | None = None

    def _begin_run(
        self, runtime: float | None, observed_transition: bool, now: float | None
    ) -> None:
        self._running = True
        self._start_coolant = None
        self._start_time = now
        self._evidence = observed_transition or (
            runtime is not None and runtime <= self.spec.max_start_runtime_s
        )

    def _within_start_window(self, runtime: float | None, now: float | None) -> bool:
        if runtime is not None:
            return runtime <= self.spec.max_start_runtime_s
        if now is None or self._start_time is None:
            return False
        return now - self._start_time <= self.spec.max_start_runtime_s

    def update(
        self, rpm: object, runtime: object, coolant: object, now: float | None = None
    ) -> tuple[float, float]:
        """Advance by one row; return ``(observed, start_coolant)``.

        ``now`` is the row's epoch seconds. It bounds how long after the
        start a first coolant reading may still stand in for the initial
        temperature when no run-time clock is available.
        """
        rpm_v = _finite(rpm)
        runtime_v = _finite(runtime)
        coolant_v = _finite(coolant)
        if rpm_v is not None and rpm_v < self.spec.min_running_rpm:
            # Engine stopped (or KOEO): this run is over, and the next
            # running row is a start we actually watched happen.
            self._running = False
            self._evidence = False
            self._start_coolant = None
            self._saw_stopped = True
            self._last_runtime = None
        elif rpm_v is not None:
            restarted = (
                self._running
                and runtime_v is not None
                and self._last_runtime is not None
                and runtime_v < self._last_runtime
            )
            if restarted:
                # The run-time clock went backwards: a restart we did not see
                # the stop of (a reconnect gap). Evidence only if the new
                # clock itself places the start.
                self._begin_run(runtime_v, observed_transition=False, now=now)
            elif not self._running:
                self._begin_run(runtime_v, observed_transition=self._saw_stopped, now=now)
            elif (
                not self._evidence
                and self._last_runtime is None
                and runtime_v is not None
                and runtime_v <= self.spec.max_start_runtime_s
            ):
                # RPM and the run-time clock are separate polls: the first
                # running rows often precede the first RUN_TIME answer by a
                # few seconds. That first reading still places the start
                # (2026-09-18 cold start: RPM from 13:45:31, RUN_TIME=20 s at
                # 13:45:36) — recorded evidence, not an assumption.
                self._evidence = True
            self._saw_stopped = False
        # rpm unknown (missing/stale): keep the current run's state; the
        # engine-state gate refuses such rows on its own.
        if (
            self._running
            and self._evidence
            and self._start_coolant is None
            and coolant_v is not None
            and self._within_start_window(runtime_v, now)
        ):
            self._start_coolant = coolant_v
        if runtime_v is not None and self._running:
            self._last_runtime = runtime_v
        if self._running and self._evidence and self._start_coolant is not None:
            return 1.0, self._start_coolant
        return 0.0, float("nan")


def _column_values(frame: pd.DataFrame, column: str) -> np.ndarray:
    if column not in frame.columns:
        return np.full(len(frame), np.nan)
    return pd.to_numeric(frame[column], errors="coerce").to_numpy(dtype=float)


def append_start_context(frame: pd.DataFrame, specs: Iterable[StartContextSpec]) -> pd.DataFrame:
    """Batch fold of :class:`EngineStartTracker` over each session in time order.

    Rows keep their input order and index. A frame without a session column
    is treated as one recording; without a timestamp column, row order is
    the time order. The tracker is reset at every session boundary.
    """
    specs = list(specs)
    if not specs or frame.empty:
        out = frame.copy()
        for spec in specs:
            out[spec.observed_column] = np.zeros(len(frame))
            out[spec.start_coolant_column] = np.full(len(frame), np.nan)
        return out
    positions = np.arange(len(frame))
    if SESSION_COLUMN in frame.columns:
        groups = [
            np.asarray(idx) for idx in frame.groupby(SESSION_COLUMN, sort=False).indices.values()
        ]
    else:
        groups = [positions]
    if TIMESTAMP_COLUMN in frame.columns:
        stamps = pd.to_datetime(frame[TIMESTAMP_COLUMN], utc=True)
        timestamps: np.ndarray | None = stamps.to_numpy(dtype="datetime64[ns]")
        epoch_seconds: np.ndarray | None = (
            stamps.dt.tz_convert(None).astype("int64").to_numpy() / 1e9
        )
    else:
        timestamps = None
        epoch_seconds = None
    out = frame.copy()
    for spec in specs:
        rpm = _column_values(frame, spec.rpm_channel)
        runtime = _column_values(frame, spec.runtime_channel)
        coolant = _column_values(frame, spec.coolant_channel)
        observed = np.zeros(len(frame))
        start_coolant = np.full(len(frame), np.nan)
        tracker = EngineStartTracker(spec)
        for group in groups:
            ordered = (
                group[np.argsort(timestamps[group], kind="stable")]
                if timestamps is not None
                else group
            )
            tracker.reset()
            for pos in ordered:
                observed[pos], start_coolant[pos] = tracker.update(
                    rpm[pos],
                    runtime[pos],
                    coolant[pos],
                    None if epoch_seconds is None else float(epoch_seconds[pos]),
                )
        out[spec.observed_column] = observed
        out[spec.start_coolant_column] = start_coolant
    return out


__all__ = [
    "CONTEXT_PREFIX",
    "EngineStartTracker",
    "StartContextSpec",
    "append_start_context",
    "start_context_columns",
    "start_context_specs",
]
