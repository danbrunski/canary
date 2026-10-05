"""Find the dyno's pulls in a stored session (H07.7).

:func:`~src.analysis.dyno.compute_power` takes :class:`~src.analysis.dyno.PowerPull`
windows; nothing turned a session into them. This does, from the session alone:

1. **Where.** A sustained rpm rise (:func:`src.analysis.pulls.detect_pulls`,
   the pull report's detector) on the vehicle's first listed rpm source. A
   window whose slope collapsed mid-pull (a lift or a shift) is refused
   ``lift_or_shift`` here rather than handed on.
2. **What.** :func:`~src.analysis.dyno_inputs.read_motion_inputs` builds the
   window on one time base (speed scaled to GPS, IMU specific force, rpm,
   demand) or refuses it by name; :meth:`DynoInputs.motion` refuses a window
   without specific force.
3. **Which gear.** The median overall ratio the window implies
   (``rpm · 2πr / (60 · v)``) against the driveline's gears; nearer than
   :data:`~src.analysis.dyno.POWER_GEAR_TOLERANCE` to one of them, or refused
   ``gear_unresolved``. :func:`compute_power` checks every sample again.
4. **Which way.** The GPS course over the window, circular mean.
   :func:`label_headings` groups pulls whose courses are within
   ``tolerance_deg`` into one direction label, which is what
   ``compute_power`` pairs.

Every window found is returned, with its pull or its refusal: an empty list
means no rpm rise was found, never that every candidate was quietly dropped.
"""

from __future__ import annotations

import math
from collections.abc import Sequence
from dataclasses import dataclass, replace
from pathlib import Path

import numpy as np
import pandas as pd

from src.analysis.channels import parse_spec, read_channels
from src.analysis.dyno import (
    POWER_GEAR_TOLERANCE,
    Driveline,
    DynoRefusal,
    MotionSources,
    PowerPull,
    RefusalKind,
)
from src.analysis.dyno_inputs import DynoInputs, read_motion_inputs
from src.analysis.pulls import Pull, RpmSeries, detect_pulls
from src.decode.bindings import Bindings
from src.storage.broadcast_storage import session_can_storage

#: The GPS course channel the direction comes from.
GPS_COURSE_CHANNEL = "gps:heading"


@dataclass(frozen=True)
class PullDetection:
    """:func:`detect_pulls`'s parameters; the defaults are the pull report's."""

    grid_ms: int = 100
    min_drdt: float = 120.0
    min_duration: float = 1.5
    min_span: float = 500.0
    band: int = 250


@dataclass(frozen=True)
class FoundPull:
    """One rpm rise found in the session, and what became of it.

    Exactly one of ``pull`` and ``refusal`` is set. ``pull.heading`` is a
    placeholder until :func:`label_headings` names the direction.
    """

    start: pd.Timestamp
    end: pd.Timestamp
    rpm_start: float
    rpm_end: float
    course_deg: float | None
    pull: PowerPull | None
    refusal: DynoRefusal | None


def find_power_pulls(
    session_dir: Path,
    *,
    sources: MotionSources,
    driveline: Driveline,
    demand_channel: str | None,
    bindings: Bindings | None = None,
    detection: PullDetection | None = None,
    wot_threshold: float | None = None,
    channels: pd.DataFrame | None = None,
) -> list[FoundPull]:
    """Every rpm rise in ``session_dir``, as a :class:`PowerPull` or a refusal.

    ``wot_threshold`` (the WOT criterion's ``threshold_value``, in the demand
    channel's unit) refuses a window whose operator demand never reaches it,
    ``not_wot``, before its motion is read: each read opens every chunk of the
    session (~12-25 s on a laptop), and most rpm rises in a drive are part
    throttle. A necessary condition only: :func:`compute_power` still checks
    every sample above the rpm floor. Without a demand channel, or without a
    threshold, nothing is pre-gated.

    ``channels`` is a :func:`read_channels` frame (``acquired_time=True``)
    already holding the rpm, course and demand specs: a caller reading the
    session once for several purposes passes it, and nothing here reads the
    whole session again (:func:`session_reads` names the specs).
    """
    if not sources.rpm:
        raise ValueError("motion_sources lists no rpm channel; pulls are found from rpm")
    rpm_channel = sources.rpm[0]
    if channels is None:
        wanted = session_reads(sources, demand_channel if wot_threshold is not None else None)
        channels = read_channels(session_dir, wanted, acquired_time=True)
    series = _rpm_series(session_dir, rpm_channel, channels)
    if series is None:
        return []
    demand = (
        _value_series(channels, demand_channel)
        if demand_channel is not None and wot_threshold is not None
        else None
    )
    params = detection or PullDetection()
    windows = detect_pulls(
        series,
        grid_ms=params.grid_ms,
        min_drdt=params.min_drdt,
        min_duration=params.min_duration,
        min_span=params.min_span,
        band=params.band,
    )
    course = _value_series(channels, GPS_COURSE_CHANNEL)
    found: list[FoundPull] = []
    for window in windows:
        start = series.t0 + pd.Timedelta(seconds=window.start_s)
        end = series.t0 + pd.Timedelta(seconds=window.end_s)
        pull, refusal = _resolve(
            session_dir,
            window,
            start,
            end,
            sources=sources,
            driveline=driveline,
            demand_channel=demand_channel,
            bindings=bindings,
            demand=demand,
            wot_threshold=wot_threshold,
        )
        found.append(
            FoundPull(
                start=start,
                end=end,
                rpm_start=window.rpm_start,
                rpm_end=window.rpm_end,
                course_deg=_circular_mean(course, start, end),
                pull=pull,
                refusal=refusal,
            )
        )
    return found


def label_headings(found: Sequence[FoundPull], *, tolerance_deg: float = 30.0) -> list[PowerPull]:
    """The found pulls with a direction label each, ready for ``compute_power``.

    Courses within ``tolerance_deg`` of a group's first member share its label
    (``course-<deg>``, rounded to 5°). A pull without a GPS course is labelled
    ``course-unknown``, and ``compute_power`` treats that as its own direction.
    Refused windows are left out; they are in ``found`` with their reasons.
    """
    groups: list[float] = []
    out: list[PowerPull] = []
    for item in found:
        if item.pull is None:
            continue
        if item.course_deg is None:
            out.append(replace(item.pull, heading="course-unknown"))
            continue
        for anchor in groups:
            if _angle_between(anchor, item.course_deg) <= tolerance_deg:
                label = anchor
                break
        else:
            groups.append(item.course_deg)
            label = item.course_deg
        out.append(replace(item.pull, heading=f"course-{int(5 * round(label / 5)) % 360:03d}"))
    return out


def _resolve(
    session_dir: Path,
    window: Pull,
    start: pd.Timestamp,
    end: pd.Timestamp,
    *,
    sources: MotionSources,
    driveline: Driveline,
    demand_channel: str | None,
    bindings: Bindings | None,
    demand: pd.Series | None = None,
    wot_threshold: float | None = None,
) -> tuple[PowerPull | None, DynoRefusal | None]:
    if window.dip:
        return _refused(
            "lift_or_shift",
            f"the rpm slope collapsed mid-pull (below 40 % of its median) between "
            f"{start.isoformat()} and {end.isoformat()}: a lift or a shift",
        )
    if demand is not None and wot_threshold is not None:
        inside = demand[(demand.index >= start) & (demand.index <= end)].dropna()
        if inside.empty:
            return _refused(
                "inputs_missing",
                f"{demand_channel} has no reading between {start.isoformat()} and "
                f"{end.isoformat()}, so nothing shows the throttle was wide open",
            )
        peak = float(inside.max())
        if peak < wot_threshold:
            return _refused(
                "not_wot",
                f"operator demand ({demand_channel}) peaks at {peak:.3g} in this window, "
                f"below the WOT criterion's {wot_threshold:g}: a part-throttle rise",
            )
    inputs = read_motion_inputs(
        session_dir,
        start=start,
        end=end,
        sources=sources,
        bindings=bindings,
        demand_channel=demand_channel,
    )
    if isinstance(inputs, DynoRefusal):
        return None, inputs
    motion = inputs.motion()
    if isinstance(motion, DynoRefusal):
        return None, motion
    gear = _gear(inputs, driveline)
    if isinstance(gear, DynoRefusal):
        return None, gear
    return PowerPull(heading="unlabelled", gear=gear, samples=motion, demand=inputs.demand), None


def _gear(inputs: DynoInputs, driveline: Driveline) -> int | DynoRefusal:
    """The gear whose overall ratio the window's median implies, or why not."""
    if inputs.rpm is None:
        return DynoRefusal("find_power_pulls", "inputs_missing", "no rpm on the window's time base")
    moving = inputs.speed_mps > 1.0
    if not moving.any():
        return DynoRefusal("find_power_pulls", "gear_unresolved", "the window never moves")
    implied = (
        inputs.rpm[moving]
        * 2.0
        * math.pi
        * driveline.tyre_radius_m
        / (60.0 * inputs.speed_mps[moving])
    )
    median = float(np.nanmedian(implied))
    ratios = [driveline.overall_ratio(g) for g in range(1, len(driveline.gearbox_ratios) + 1)]
    best = int(np.argmin([abs(median / r - 1.0) for r in ratios]))
    if abs(median / ratios[best] - 1.0) > POWER_GEAR_TOLERANCE:
        return DynoRefusal(
            "find_power_pulls",
            "gear_unresolved",
            f"median overall ratio {median:.3f} is more than "
            f"{POWER_GEAR_TOLERANCE:.0%} from every gear "
            f"({', '.join(f'{r:.3f}' for r in ratios)})",
        )
    return best + 1


def _refused(kind: RefusalKind, detail: str) -> tuple[None, DynoRefusal]:
    return None, DynoRefusal("find_power_pulls", kind, detail)


def session_reads(sources: MotionSources, demand_channel: str | None) -> list[str]:
    """The specs :func:`find_power_pulls` reads from the whole session."""
    if not sources.rpm:
        raise ValueError("motion_sources lists no rpm channel; pulls are found from rpm")
    specs = [sources.rpm[0], GPS_COURSE_CHANNEL]
    if demand_channel is not None:
        specs.append(demand_channel)
    return list(dict.fromkeys(specs))


def _rows(channels: pd.DataFrame, spec: str) -> pd.DataFrame:
    """One spec's rows of a multi-spec :func:`read_channels` frame."""
    if channels.empty or "spec" not in channels.columns:
        return channels.iloc[0:0]
    return channels[channels["spec"] == parse_spec(spec).text]


def _rpm_series(session_dir: Path, rpm_channel: str, channels: pd.DataFrame) -> RpmSeries | None:
    long = _rows(channels, rpm_channel)
    if long.empty:
        return None
    long = long.sort_values("timestamp", kind="stable")
    stamps = pd.to_datetime(long["timestamp"], utc=True)
    t0 = stamps.iloc[0]
    held = (
        rpm_channel.split(":", 1)[0] in ("decoded", "can")
        and session_can_storage(session_dir).any_change_only
    )
    return RpmSeries(
        rpm_channel,
        ((stamps - t0) / pd.Timedelta(seconds=1)).to_numpy(dtype=float),
        long["value"].to_numpy(dtype=float),
        t0,
        held=held,
    )


def _value_series(channels: pd.DataFrame, channel: str) -> pd.Series:
    """One channel of the session read, indexed by acquisition time."""
    long = _rows(channels, channel)
    if long.empty:
        return pd.Series(dtype=float)
    return pd.Series(
        long["value"].to_numpy(dtype=float),
        index=pd.DatetimeIndex(pd.to_datetime(long["timestamp"], utc=True)),
    ).sort_index()


def _circular_mean(course: pd.Series, start: pd.Timestamp, end: pd.Timestamp) -> float | None:
    window = course[(course.index >= start) & (course.index <= end)].dropna()
    if window.empty:
        return None
    radians = np.deg2rad(window.to_numpy(dtype=float))
    mean = math.degrees(math.atan2(np.sin(radians).mean(), np.cos(radians).mean()))
    return mean % 360.0


def _angle_between(a: float, b: float) -> float:
    return abs((a - b + 180.0) % 360.0 - 180.0)


__all__ = ["FoundPull", "PullDetection", "find_power_pulls", "label_headings", "session_reads"]
