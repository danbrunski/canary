"""The one behavior-model eligibility evaluator (B01.4).

Every consumer of a behavior model asks the same question — *may this model
speak for this row, and if not, why not?* — and before this module each
answered it differently: the refit filtered on RPM, fuel status and a
session's first coolant reading, while the runtime decoder only knew the
operating mode and rate gates. A model fitted on closed-loop rows then graded
open-loop enrichment at serve time.

Here the question has one answer, used by fitting rows
(``src.analysis.refit.fit_recipe``), held-out scoring, vectorized decoding
(``append_deviation_columns``), scalar live decoding (the daemon's
``_observe_behavior``), session analysis and replay. The same array code
serves a whole frame or a single operating point, so batch and live cannot
drift apart.

A refusal is a stable ``code[:detail]`` string; the first unmet condition in
this order wins:

1. ``operating_mode_mismatch``, ``operating_mode_excluded:<mode>``,
   ``operating_mode_unknown``
2. engine state — ``engine_state_stale:<ch>``, ``engine_state_unknown:<ch>``,
   ``engine_not_running``
3. fuel control — ``fuel_status_stale:<ch>``, ``fuel_status_missing:<ch>``,
   ``fuel_status_excluded:<state name>``
4. cold start — ``cold_start_context_unavailable``,
   ``cold_start_no_start_evidence``, ``cold_start_initial_coolant_above_limit``
5. required context — ``context_stale:<a|b>``, ``context_missing:<a|b>``
6. inputs — ``input_stale:<ch>``, ``input_missing:<ch>``
7. rate gates — ``rate_unavailable:<__roc column>``, ``rate_stale:<ch>``,
   ``rate_insufficient_history:<ch>``, ``rate_exceeded:<ch>``

after which the surface itself may be ``off_map``, and a deviation may lack
its actual value (``actual_stale:<ch>`` / ``actual_missing:<ch>``). A
legacy model (no :class:`ServingEligibility`) is evaluated with the legacy
gates only (1, 6, 7) so the ``deviation:*`` features existing detectors were
trained on keep their meaning; its lack of a contract is reported through
``serving_status`` instead.

Staleness: a channel is *stale* when its ``<channel>__gap`` flag is set (the
resampler offline, the bounded live snapshot online) and *missing* when it is
absent or non-finite without one. A frame with no gap column for a channel
cannot report staleness for it — missing values then read as missing.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import Protocol

import numpy as np
import pandas as pd

from src.decode.behavior_models import (
    BehaviorModel1D,
    BehaviorModel2D,
    RateOfChangeCondition,
    ServingEligibility,
    fuel_status_name,
)
from src.preprocessing.features import ROC_SUFFIX
from src.preprocessing.resampler import gap_column
from src.preprocessing.segmenter import MODE_COLUMN
from src.preprocessing.session_context import start_context_columns
from src.telemetry import DECODED_SOURCE, SOURCE_SEP, channel_key

OPERATING_MODE_MISMATCH = "operating_mode_mismatch"
OPERATING_MODE_EXCLUDED = "operating_mode_excluded"
OPERATING_MODE_UNKNOWN = "operating_mode_unknown"
ENGINE_STATE_STALE = "engine_state_stale"
ENGINE_STATE_UNKNOWN = "engine_state_unknown"
ENGINE_NOT_RUNNING = "engine_not_running"
FUEL_STATUS_STALE = "fuel_status_stale"
FUEL_STATUS_MISSING = "fuel_status_missing"
FUEL_STATUS_EXCLUDED = "fuel_status_excluded"
COLD_START_CONTEXT_UNAVAILABLE = "cold_start_context_unavailable"
COLD_START_NO_EVIDENCE = "cold_start_no_start_evidence"
COLD_START_NOT_COLD = "cold_start_initial_coolant_above_limit"
CONTEXT_STALE = "context_stale"
CONTEXT_MISSING = "context_missing"
INPUT_STALE = "input_stale"
INPUT_MISSING = "input_missing"
RATE_UNAVAILABLE = "rate_unavailable"
RATE_STALE = "rate_stale"
RATE_INSUFFICIENT_HISTORY = "rate_insufficient_history"
RATE_EXCEEDED = "rate_exceeded"
OFF_MAP = "off_map"
ACTUAL_STALE = "actual_stale"
ACTUAL_MISSING = "actual_missing"


def eligible_mask(reasons: np.ndarray) -> np.ndarray:
    """Boolean mask of rows with no refusal (``None`` entries)."""
    return np.asarray(pd.isna(reasons), dtype=bool)


def reason_code(reason: str) -> str:
    """The code part of a ``code:detail`` refusal (for aggregation)."""
    return reason.split(":", 1)[0]


@dataclass(frozen=True)
class ServingSpec:
    """Everything the evaluator reads, independent of whether a model exists yet.

    A fitted model yields one through :func:`serving_spec`; the refit builds
    one from a recipe *before* fitting so its training rows pass the very
    contract it then persists.
    """

    operating_mode: str | None
    conditions: tuple[RateOfChangeCondition, ...]
    eligibility: ServingEligibility | None
    inputs: tuple[str, ...]
    output: str


def resolve_output_column(output: str) -> str:
    """Actual-value column for a model output: a qualified ``source:channel``
    verbatim, else the legacy ``decoded:<NAME>`` for a bare CAN signal."""
    return output if SOURCE_SEP in output else channel_key(DECODED_SOURCE, output)


def serving_spec(model: BehaviorModel1D | BehaviorModel2D) -> ServingSpec:
    inputs = (
        (model.axis.channel,)
        if isinstance(model, BehaviorModel1D)
        else tuple(axis.channel for axis in model.axes)
    )
    return ServingSpec(
        operating_mode=model.operating_mode,
        conditions=tuple(model.conditions),
        eligibility=model.eligibility,
        inputs=inputs,
        output=resolve_output_column(model.output),
    )


def required_channel_groups(
    spec: ServingSpec | BehaviorModel1D | BehaviorModel2D,
) -> list[tuple[str, ...]]:
    """Base telemetry channels the contract needs, as any-of groups.

    For collection planning (capture-profile warnings): every axis, the
    output, the rate-gate base channels and every eligibility channel.
    """
    resolved = spec if isinstance(spec, ServingSpec) else serving_spec(spec)
    groups: list[tuple[str, ...]] = [(channel,) for channel in resolved.inputs]
    groups.append((resolved.output,))
    groups.extend(
        (condition.channel.removesuffix(ROC_SUFFIX),) for condition in resolved.conditions
    )
    eligibility = resolved.eligibility
    if eligibility is not None:
        if eligibility.engine_state.state == "engine_running":
            groups.append((eligibility.engine_state.rpm_channel,))
        if eligibility.fuel_control is not None:
            groups.append((eligibility.fuel_control.channel,))
        if eligibility.cold_start is not None:
            cold = eligibility.cold_start
            groups.extend([(cold.rpm_channel,), (cold.coolant_channel,)])
        groups.extend(tuple(ctx.any_of) for ctx in eligibility.required_context)
    unique: list[tuple[str, ...]] = []
    for group in groups:
        if group not in unique:
            unique.append(group)
    return unique


# ---------------------------------------------------------------------------
# Column access shared by the frame and single-point paths
# ---------------------------------------------------------------------------


class _Columns(Protocol):
    size: int

    def has(self, name: str) -> bool: ...

    def floats(self, name: str) -> np.ndarray: ...

    def flags(self, name: str) -> np.ndarray: ...

    def equals(self, name: str, value: object) -> np.ndarray: ...


class _FrameColumns:
    def __init__(self, frame: pd.DataFrame) -> None:
        self._frame = frame
        self.size = len(frame)

    def has(self, name: str) -> bool:
        return name in self._frame.columns

    def floats(self, name: str) -> np.ndarray:
        if name not in self._frame.columns:
            return np.full(self.size, np.nan)
        return pd.to_numeric(self._frame[name], errors="coerce").to_numpy(dtype=float)

    def flags(self, name: str) -> np.ndarray:
        """Boolean flag column; a missing (NaN/None) flag reads ``True``."""
        column = self._frame[name]
        if column.dtype == bool:
            return column.to_numpy()
        present = column.notna().to_numpy()
        values = np.ones(self.size, dtype=bool)
        values[present] = column[present].astype(bool).to_numpy()
        return values

    def equals(self, name: str, value: object) -> np.ndarray:
        return (self._frame[name].astype(object) == value).to_numpy(dtype=bool)


def _scalar_float(value: object) -> float:
    if value is None or isinstance(value, str):
        return float("nan")
    try:
        return float(value)  # type: ignore[arg-type]  # except handles non-numeric
    except (TypeError, ValueError):
        return float("nan")


def point_flag(point: Mapping[str, object], name: str) -> bool:
    """A ``__gap``-style flag on a single point: present-but-unset (NaN or
    ``None``) reads ``True`` (stale), an absent key reads ``False``."""
    if name not in point:
        return False
    flag = point[name]
    if flag is None or (isinstance(flag, float) and flag != flag):
        return True
    return bool(flag)


class _PointColumns:
    size = 1

    def __init__(self, point: Mapping[str, object]) -> None:
        self._point = point

    def has(self, name: str) -> bool:
        return name in self._point

    def floats(self, name: str) -> np.ndarray:
        return np.array([_scalar_float(self._point.get(name))], dtype=float)

    def flags(self, name: str) -> np.ndarray:
        return np.array([point_flag(self._point, name)], dtype=bool)

    def equals(self, name: str, value: object) -> np.ndarray:
        return np.array([self._point.get(name) == value], dtype=bool)


def _stale_mask(cols: _Columns, channel: str) -> np.ndarray:
    gap = gap_column(channel)
    if not cols.has(gap):
        return np.zeros(cols.size, dtype=bool)
    return cols.flags(gap)


def _freshness(cols: _Columns, channel: str) -> tuple[np.ndarray, np.ndarray, np.ndarray]:
    """``(values, stale, missing)`` — stale and missing are disjoint."""
    values = cols.floats(channel)
    stale = _stale_mask(cols, channel)
    missing = ~np.isfinite(values) & ~stale
    return values, stale, missing


class _Refusals:
    """First-failure-wins reason array."""

    def __init__(self, size: int) -> None:
        self.reasons = np.full(size, None, dtype=object)

    def open(self) -> np.ndarray:
        return eligible_mask(self.reasons)

    def assign(self, mask: np.ndarray, reason: str) -> None:
        target = mask & self.open()
        if target.any():
            self.reasons[target] = reason


def _evaluate(cols: _Columns, spec: ServingSpec, *, include_actual: bool) -> np.ndarray:
    refusals = _Refusals(cols.size)

    # 1. operating mode
    if spec.operating_mode is not None:
        if cols.has(MODE_COLUMN):
            refusals.assign(~cols.equals(MODE_COLUMN, spec.operating_mode), OPERATING_MODE_MISMATCH)
        else:
            refusals.assign(np.ones(cols.size, dtype=bool), OPERATING_MODE_MISMATCH)

    eligibility = spec.eligibility
    if eligibility is not None:
        # 1b. excluded operating modes (B05.2) — an unlabelled row is refused,
        # never assumed to be outside the excluded set.
        if eligibility.excluded_operating_modes:
            if cols.has(MODE_COLUMN):
                for mode in eligibility.excluded_operating_modes:
                    refusals.assign(
                        cols.equals(MODE_COLUMN, mode), f"{OPERATING_MODE_EXCLUDED}:{mode}"
                    )
            else:
                refusals.assign(np.ones(cols.size, dtype=bool), OPERATING_MODE_UNKNOWN)

        # 2. engine state
        engine = eligibility.engine_state
        if engine.state == "engine_running":
            rpm, stale, missing = _freshness(cols, engine.rpm_channel)
            refusals.assign(stale, f"{ENGINE_STATE_STALE}:{engine.rpm_channel}")
            refusals.assign(missing, f"{ENGINE_STATE_UNKNOWN}:{engine.rpm_channel}")
            with np.errstate(invalid="ignore"):
                refusals.assign(rpm < engine.min_running_rpm, ENGINE_NOT_RUNNING)

        # 3. fuel control — explicit membership only
        fuel = eligibility.fuel_control
        if fuel is not None:
            status, stale, missing = _freshness(cols, fuel.channel)
            refusals.assign(stale, f"{FUEL_STATUS_STALE}:{fuel.channel}")
            refusals.assign(missing, f"{FUEL_STATUS_MISSING}:{fuel.channel}")
            finite = np.isfinite(status)
            excluded = finite & ~np.isin(status, np.asarray(fuel.allowed_states, dtype=float))
            open_rows = excluded & refusals.open()
            for code in np.unique(status[open_rows]):
                refusals.assign(
                    open_rows & (status == code),
                    f"{FUEL_STATUS_EXCLUDED}:{fuel_status_name(float(code))}",
                )

        # 4. cold-start session context
        cold = eligibility.cold_start
        if cold is not None:
            observed_col, coolant_col = start_context_columns(cold)
            if not (cols.has(observed_col) and cols.has(coolant_col)):
                refusals.assign(np.ones(cols.size, dtype=bool), COLD_START_CONTEXT_UNAVAILABLE)
            else:
                observed = cols.floats(observed_col)
                start_coolant = cols.floats(coolant_col)
                no_evidence = ~(observed == 1.0) | ~np.isfinite(start_coolant)
                refusals.assign(no_evidence, COLD_START_NO_EVIDENCE)
                with np.errstate(invalid="ignore"):
                    refusals.assign(start_coolant > cold.max_initial_coolant_c, COLD_START_NOT_COLD)

        # 5. required measured context
        for context in eligibility.required_context:
            fresh = np.zeros(cols.size, dtype=bool)
            any_stale = np.zeros(cols.size, dtype=bool)
            for channel in context.any_of:
                values, stale, _ = _freshness(cols, channel)
                fresh |= np.isfinite(values) & ~stale
                any_stale |= stale
            label = "|".join(context.any_of)
            refusals.assign(~fresh & any_stale, f"{CONTEXT_STALE}:{label}")
            refusals.assign(~fresh, f"{CONTEXT_MISSING}:{label}")

    # 6. surface inputs
    for channel in spec.inputs:
        _, stale, missing = _freshness(cols, channel)
        refusals.assign(stale, f"{INPUT_STALE}:{channel}")
        refusals.assign(missing, f"{INPUT_MISSING}:{channel}")

    # 7. persisted rate gates (derivatives computed before decoding, B01.2)
    for condition in spec.conditions:
        base = condition.channel.removesuffix(ROC_SUFFIX)
        if not cols.has(condition.channel):
            refusals.assign(
                np.ones(cols.size, dtype=bool), f"{RATE_UNAVAILABLE}:{condition.channel}"
            )
            continue
        rate = cols.floats(condition.channel)
        undefined = ~np.isfinite(rate)
        refusals.assign(undefined & _stale_mask(cols, base), f"{RATE_STALE}:{base}")
        refusals.assign(undefined, f"{RATE_INSUFFICIENT_HISTORY}:{base}")
        with np.errstate(invalid="ignore"):
            refusals.assign(np.abs(rate) > condition.max_abs_per_second, f"{RATE_EXCEEDED}:{base}")

    if include_actual:
        _, stale, missing = _freshness(cols, spec.output)
        refusals.assign(stale, f"{ACTUAL_STALE}:{spec.output}")
        refusals.assign(missing, f"{ACTUAL_MISSING}:{spec.output}")
    return refusals.reasons


def eligibility_reasons(
    frame: pd.DataFrame,
    spec: ServingSpec | BehaviorModel1D | BehaviorModel2D,
    *,
    include_actual: bool = False,
) -> np.ndarray:
    """Per-row refusal (``None`` = eligible) for a whole frame."""
    resolved = spec if isinstance(spec, ServingSpec) else serving_spec(spec)
    return _evaluate(_FrameColumns(frame), resolved, include_actual=include_actual)


def point_eligibility(
    point: Mapping[str, object],
    spec: ServingSpec | BehaviorModel1D | BehaviorModel2D,
    *,
    include_actual: bool = False,
) -> str | None:
    """Refusal for a single operating point (``None`` = eligible)."""
    resolved = spec if isinstance(spec, ServingSpec) else serving_spec(spec)
    reason = _evaluate(_PointColumns(point), resolved, include_actual=include_actual)[0]
    return None if reason is None else str(reason)


def summarize_reasons(reasons: Sequence[object] | np.ndarray, *, limit: int = 3) -> str:
    """``"code:detail (n), ..."`` for the most frequent refusals."""
    counts: dict[str, int] = {}
    for reason in reasons:
        if reason is None:
            continue
        counts[str(reason)] = counts.get(str(reason), 0) + 1
    ranked = sorted(counts.items(), key=lambda item: (-item[1], item[0]))[:limit]
    return ", ".join(f"{reason} ({count})" for reason, count in ranked)


__all__ = [
    "ACTUAL_MISSING",
    "ACTUAL_STALE",
    "OFF_MAP",
    "ServingSpec",
    "eligibility_reasons",
    "eligible_mask",
    "point_eligibility",
    "point_flag",
    "reason_code",
    "required_channel_groups",
    "resolve_output_column",
    "serving_spec",
    "summarize_reasons",
]
