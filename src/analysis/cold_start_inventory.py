"""What cold-start evidence the stored corpus actually holds (B07.2).

The warmup models (``coolant_warmup``, ``cold_start_idle_rpm_vs_coolant``,
``cat_warmup``) are only meaningful on an engine start that began cold, and
the 2026-09-21 review found the corpus count behind their band widths was
last taken in July against a different directory. This module answers the
inventory question directly, per **engine run** rather than per directory:

* A start counts only when it was *observed* — an RPM transition from stopped
  to running, or a run-time clock young enough to place the start. That rule
  is :class:`~src.preprocessing.session_context.EngineStartTracker`, the same
  implementation serving uses, not a copy of it: a session whose recording
  began mid-drive has an **unknown** initial temperature, never a cold one.
* Warm restarts and mid-session fragments are reported as their own
  classifications rather than dropped, so "this drive contained no eligible
  start" stays distinguishable from "this drive never carried the channels".
* Every absence is named: a session with no polled rows, a session missing
  the contract's channels, and a session whose engine never ran are three
  different answers.
* Sessions are grouped into independent physical drives by parsed start time
  (:func:`~src.analysis.behavior_validation.group_drives`), never by
  directory name — two directories from one outing are one drive's worth of
  evidence, not two.

Captures (``_``-prefixed directories) are counted separately: they are real
physical drives and real cold starts, but ``preprocessing.loader.
discover_sessions`` excludes them from training by design, so a count that
pools them would overstate what a refit could use.
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping, Sequence
from dataclasses import asdict, dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any

import numpy as np
import pandas as pd

from src.dataframe_utils import numeric_values
from src.operating import ENGINE_RUNNING_MIN_RPM
from src.preprocessing.session_context import EngineStartTracker, StartContextSpec
from src.signals.pairing import to_seconds

#: The serving contract's channels, thresholds and cold ceiling. Defaults
#: match ``ColdStartRequirement`` and the warmup recipes' 40 °C ceiling; a
#: vehicle whose profile differs passes its own.
DEFAULT_RPM_CHANNEL = "obd:RPM"
DEFAULT_RUNTIME_CHANNEL = "obd:RUN_TIME"
DEFAULT_COOLANT_CHANNEL = "obd:COOLANT_TEMP"
DEFAULT_MIN_RUNNING_RPM = ENGINE_RUNNING_MIN_RPM
DEFAULT_MAX_START_RUNTIME_S = 60.0
DEFAULT_MAX_INITIAL_COOLANT_C = 40.0

#: Context channels an added warmup axis would need, and the warmup window
#: they are measured over (start of the run until coolant first reaches
#: ``warm_coolant_c``).
IAT_CHANNEL = "obd:INTAKE_TEMP"
AMBIENT_CHANNEL = "obd:AMBIENT_AIR_TEMP"
LOAD_CHANNEL = "obd:ENGINE_LOAD"
SPEED_CHANNEL = "obd:SPEED"
CATALYST_CHANNEL = "obd:CATALYST_TEMP_B1S1"
DEFAULT_WARM_COOLANT_C = 70.0

INVENTORY_CHANNELS: tuple[str, ...] = (
    DEFAULT_RPM_CHANNEL,
    DEFAULT_RUNTIME_CHANNEL,
    DEFAULT_COOLANT_CHANNEL,
    IAT_CHANNEL,
    AMBIENT_CHANNEL,
    LOAD_CHANNEL,
    SPEED_CHANNEL,
    CATALYST_CHANNEL,
)


@dataclass(frozen=True)
class InventorySpec:
    """The rule an inventory is taken under, carried with its results."""

    rpm_channel: str = DEFAULT_RPM_CHANNEL
    runtime_channel: str = DEFAULT_RUNTIME_CHANNEL
    coolant_channel: str = DEFAULT_COOLANT_CHANNEL
    min_running_rpm: float = DEFAULT_MIN_RUNNING_RPM
    max_start_runtime_s: float = DEFAULT_MAX_START_RUNTIME_S
    max_initial_coolant_c: float = DEFAULT_MAX_INITIAL_COOLANT_C
    warm_coolant_c: float = DEFAULT_WARM_COOLANT_C
    #: Minimum stationary time before the start for "the vehicle was parked".
    stationary_window_s: float = 60.0

    def tracker_spec(self) -> StartContextSpec:
        return StartContextSpec(
            rpm_channel=self.rpm_channel,
            runtime_channel=self.runtime_channel,
            coolant_channel=self.coolant_channel,
            min_running_rpm=self.min_running_rpm,
            max_start_runtime_s=self.max_start_runtime_s,
        )


@dataclass
class EngineRun:
    """One engine run inside a recording, and what it is evidence of."""

    started_at: str | None
    ended_at: str | None
    duration_s: float
    #: ``cold_start`` / ``warm_restart`` / ``unknown_initial_temperature``.
    classification: str
    start_observed: bool
    initial_coolant_c: float | None
    initial_iat_c: float | None
    ambient_c: float | None
    #: Run-time clock never went backwards and never gapped by more than the
    #: sample interval it advances at (training-selection provenance).
    runtime_continuous: bool | None
    #: Speed was zero throughout ``stationary_window_s`` before the start;
    #: ``None`` when no speed channel covered that window.
    stationary_before_start: bool | None
    moving_seconds_in_warmup: float | None
    warmup_seconds: float
    load_spread: float | None
    iat_spread: float | None
    catalyst_rows: int

    def as_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass
class SessionInventory:
    """One recording's cold-start evidence, or the named reason there is none."""

    session: str
    path: str
    is_capture: bool
    started_at: str | None
    rows: int
    missing_channels: list[str]
    runs: list[EngineRun] = field(default_factory=list)
    reason: str | None = None
    sidecar_cold_start: bool | None = None
    sidecar_classified_at: str | None = None
    sidecar_mode_labels_stale: bool | None = None

    @property
    def cold_starts(self) -> list[EngineRun]:
        return [run for run in self.runs if run.classification == "cold_start"]

    def as_dict(self) -> dict[str, Any]:
        return {
            "session": self.session,
            "path": self.path,
            "is_capture": self.is_capture,
            "started_at": self.started_at,
            "rows": self.rows,
            "missing_channels": list(self.missing_channels),
            "reason": self.reason,
            "sidecar_cold_start": self.sidecar_cold_start,
            "sidecar_classified_at": self.sidecar_classified_at,
            "sidecar_mode_labels_stale": self.sidecar_mode_labels_stale,
            "runs": [run.as_dict() for run in self.runs],
        }


def per_second_view(long: pd.DataFrame, channels: Sequence[str]) -> pd.DataFrame:
    """Last observation of each channel per second — no forward fill.

    A second with no observation of a channel is *unknown* for that channel,
    which is what :class:`EngineStartTracker` expects (the resampler's stale
    cells reach it as NaN too). Forward-filling here would invent a coolant
    reading at a start that was never measured.
    """
    if long.empty:
        return pd.DataFrame(columns=["timestamp", *channels])
    frame = long.loc[:, ["timestamp", "source_channel", "value_number"]].copy()
    frame["timestamp"] = pd.to_datetime(frame["timestamp"], utc=True).dt.floor("1s")
    frame = frame[frame["source_channel"].isin(channels)]
    wide = frame.pivot_table(
        index="timestamp", columns="source_channel", values="value_number", aggfunc="last"
    )
    wide = wide.reindex(columns=list(channels))
    return wide.sort_index().reset_index()


def _finite(values: np.ndarray) -> np.ndarray:
    out: np.ndarray = values[np.isfinite(values)]
    return out


def _spread(values: np.ndarray) -> float | None:
    finite = _finite(values)
    return float(finite.max() - finite.min()) if len(finite) >= 2 else None


def runs_of_session(view: pd.DataFrame, spec: InventorySpec) -> list[EngineRun]:
    """Split a per-second view into engine runs and classify each one."""
    if view.empty:
        return []
    # to_seconds, not astype("int64")/1e9: a parquet-backed frame carries
    # microsecond datetimes in pandas 2.x, and that arithmetic silently
    # returned 1970 timestamps and one-second "runs".
    epoch = np.asarray(to_seconds(pd.to_datetime(view["timestamp"], utc=True)), dtype=float)
    rpm = numeric_values(view, spec.rpm_channel)
    runtime = numeric_values(view, spec.runtime_channel)
    coolant = numeric_values(view, spec.coolant_channel)
    iat = numeric_values(view, IAT_CHANNEL)
    ambient = numeric_values(view, AMBIENT_CHANNEL)
    load = numeric_values(view, LOAD_CHANNEL)
    speed = numeric_values(view, SPEED_CHANNEL)
    catalyst = numeric_values(view, CATALYST_CHANNEL)

    tracker = EngineStartTracker(spec.tracker_spec())
    observed = np.zeros(len(view))
    start_coolant = np.full(len(view), np.nan)
    for i in range(len(view)):
        observed[i], start_coolant[i] = tracker.update(
            rpm[i], runtime[i], coolant[i], float(epoch[i])
        )

    runs: list[EngineRun] = []
    running = False
    start = 0
    last_runtime = np.nan
    continuous = True
    for i in range(len(view) + 1):
        stopped = i == len(view) or (np.isfinite(rpm[i]) and rpm[i] < spec.min_running_rpm)
        restarted = (
            running
            and i < len(view)
            and np.isfinite(runtime[i])
            and np.isfinite(last_runtime)
            and runtime[i] < last_runtime
        )
        if running and (stopped or restarted):
            runs.append(
                _make_run(
                    spec,
                    epoch=epoch,
                    start=start,
                    stop=i,
                    observed=observed,
                    start_coolant=start_coolant,
                    coolant=coolant,
                    iat=iat,
                    ambient=ambient,
                    load=load,
                    speed=speed,
                    catalyst=catalyst,
                    runtime_continuous=continuous and not restarted,
                )
            )
            running = False
        if i == len(view):
            break
        if not running and np.isfinite(rpm[i]) and rpm[i] >= spec.min_running_rpm:
            running = True
            start = i
            continuous = True
            last_runtime = np.nan
        if running and np.isfinite(runtime[i]):
            if np.isfinite(last_runtime) and runtime[i] < last_runtime:
                continuous = False
            last_runtime = runtime[i]
    return runs


def _make_run(
    spec: InventorySpec,
    *,
    epoch: np.ndarray,
    start: int,
    stop: int,
    observed: np.ndarray,
    start_coolant: np.ndarray,
    coolant: np.ndarray,
    iat: np.ndarray,
    ambient: np.ndarray,
    load: np.ndarray,
    speed: np.ndarray,
    catalyst: np.ndarray,
    runtime_continuous: bool,
) -> EngineRun:
    window = slice(start, stop)
    was_observed = bool(np.any(observed[window] > 0))
    initial = _finite(start_coolant[window])
    initial_coolant = float(initial[0]) if len(initial) else None
    if not was_observed or initial_coolant is None:
        classification = "unknown_initial_temperature"
    elif initial_coolant <= spec.max_initial_coolant_c:
        classification = "cold_start"
    else:
        classification = "warm_restart"

    # Warmup window: from the start until coolant first reaches warm.
    warm_mask = np.isfinite(coolant[window]) & (coolant[window] >= spec.warm_coolant_c)
    warm_index = start + int(np.argmax(warm_mask)) if bool(warm_mask.any()) else stop
    warmup = slice(start, max(warm_index, start + 1))
    warmup_seconds = float(epoch[min(warm_index, stop - 1)] - epoch[start])

    # searchsorted, not a whole-array mask: a recording with many engine
    # runs would otherwise cost O(runs x rows) and the corpus scan crawled.
    first = int(np.searchsorted(epoch, epoch[start] - spec.stationary_window_s, side="left"))
    prior_speed = _finite(speed[first:start])
    stationary = bool(np.all(prior_speed == 0.0)) if len(prior_speed) else None
    moving = _finite(speed[warmup])
    iat_window = _finite(iat[warmup])
    ambient_window = _finite(ambient[window])
    return EngineRun(
        started_at=_iso(epoch[start]),
        ended_at=_iso(epoch[stop - 1]),
        duration_s=float(epoch[stop - 1] - epoch[start]),
        classification=classification,
        start_observed=was_observed,
        initial_coolant_c=initial_coolant,
        initial_iat_c=float(iat_window[0]) if len(iat_window) else None,
        ambient_c=float(ambient_window[0]) if len(ambient_window) else None,
        runtime_continuous=runtime_continuous,
        stationary_before_start=stationary,
        moving_seconds_in_warmup=float(np.sum(moving > 0)) if len(moving) else None,
        warmup_seconds=warmup_seconds,
        load_spread=_spread(load[warmup]),
        iat_spread=_spread(iat[warmup]),
        catalyst_rows=int(np.isfinite(catalyst[warmup]).sum()),
    )


def _iso(epoch_seconds: float) -> str:
    return pd.Timestamp(epoch_seconds, unit="s", tz="UTC").isoformat()


def usable_context_runs(
    inventories: Iterable[SessionInventory], min_spread: Mapping[str, float]
) -> list[tuple[str, EngineRun]]:
    """Cold starts whose warmup window actually varied load or IAT.

    The 4.6 rule's "usable" test (``refit.count_usable_cold_start_sessions``)
    applied per engine run: a start that sat at one load and one ambient
    teaches a context axis nothing.
    """
    out: list[tuple[str, EngineRun]] = []
    for inventory in inventories:
        for run in inventory.cold_starts:
            spreads = {LOAD_CHANNEL: run.load_spread, IAT_CHANNEL: run.iat_spread}
            if any(
                spreads.get(channel) is not None and spreads[channel] >= floor  # type: ignore[operator]
                for channel, floor in min_spread.items()
            ):
                out.append((inventory.session, run))
    return out


def group_into_drives(
    inventories: Sequence[SessionInventory], *, join_gap_seconds: float | None = None
) -> list[dict[str, Any]]:
    """Independent physical drives, oldest first, with their cold starts.

    Uses the shared physical-drive definition (:mod:`src.preprocessing.drives`):
    vehicle activity, sibling captures as bridges, and a split at a long pause
    in activity *inside* a recording, so an overnight parked recording no
    longer joins the evening and morning cold starts into one drive (operator,
    2026-09-23). A cold start belongs to the drive whose span holds its start.
    """
    from src.preprocessing.drives import DRIVE_GAP_SECONDS, physical_drives

    gap = DRIVE_GAP_SECONDS if join_gap_seconds is None else join_gap_seconds
    by_session = {inventory.session: inventory for inventory in inventories}
    drives = physical_drives(
        [
            Path(inventory.path).with_name(inventory.session)
            if inventory.path
            else Path(inventory.session)
            for inventory in inventories
        ],
        gap_seconds=gap,
    )

    def _at(value: str | None) -> datetime | None:
        if value is None:
            return None
        stamp = pd.Timestamp(value)
        return (stamp.tz_localize("UTC") if stamp.tzinfo is None else stamp).to_pydatetime()

    out: list[dict[str, Any]] = []
    for drive in drives:
        pieces = [name for name in drive.sessions if name in by_session]
        cold = []
        for piece in pieces:
            for run in by_session[piece].cold_starts:
                begun = _at(run.started_at)
                if begun is None or drive.start <= begun <= drive.end or len(drives) == 1:
                    cold.append({"session": piece, **run.as_dict()})
        out.append(
            {
                "drive_id": drive.drive_id,
                "pieces": pieces,
                "started_at": drive.start.isoformat(),
                "ended_at": drive.end.isoformat(),
                "is_capture": any(by_session[p].is_capture for p in pieces),
                "cold_starts": cold,
            }
        )
    return out


__all__ = [
    "CATALYST_CHANNEL",
    "DEFAULT_MAX_INITIAL_COOLANT_C",
    "IAT_CHANNEL",
    "INVENTORY_CHANNELS",
    "LOAD_CHANNEL",
    "EngineRun",
    "InventorySpec",
    "SessionInventory",
    "group_into_drives",
    "per_second_view",
    "runs_of_session",
    "usable_context_runs",
]
