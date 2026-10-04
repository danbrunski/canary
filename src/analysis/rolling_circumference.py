"""The driven tyres' rolling circumference, measured from one drive (tyre wear).

Engine speed and GPS ground speed on steady cruise fix the distance the driven
tyres roll per revolution, given the gearbox and final-drive ratios:

    C = v / (rpm / (gear · final_drive))

Same-instant pairs only (:func:`~src.analysis.compare.align_pairs`), so no
average stands in for a measurement (AGENTS.md). A pair counts when the five
GPS samples centred on it (at most :data:`STEADY_SPAN_S` apart) hold speed and
rpm/speed within :data:`STEADY_TOLERANCE`, above :data:`MIN_SPEED_KPH`, and its
rpm/speed lies within :data:`GEAR_TOLERANCE` of one gear: a clutch-in coast or
a shift never qualifies. The drive's value is the median of those pairs.

Measured on the Xterra (H07.11, D03.35): one drive's median has a standard
error of 0.03-0.10 %, but drives scatter from one another by more: 0.15 % (σ)
over 18 September drives on the laptop, 0.43 % over the 9 drives the Pi first
measured (2026-10-04). One millimetre of tread is 2π mm of circumference, 0.25 %
on a 2.56 m tyre, so one drive resolves wear to about ±0.6-1.7 mm, and the
trend is read from many. Absolute tread depth needs a gauge reading as the
baseline (:mod:`src.control.tyres`); this module measures circumference only.

Each drive's result is its own sidecar, ``rolling_circumference.json``: the
digest writes it, trip end measures it alone when the digest is refused, and
``scripts/backfill_rolling_circumference.py`` covers stored sessions.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Any

import numpy as np
import pandas as pd

from src.analysis.compare import ComparisonRefusedError, align_pairs
from src.signals.pairing import PairingPolicy
from src.storage.session_products import ROLLING_CIRCUMFERENCE_FILENAME
from src.storage.sidecar import write_json_sidecar

if TYPE_CHECKING:
    from src.config import CanaryConfig

log = logging.getLogger(__name__)

GPS_SPEED_CHANNEL = "gps:speed"
#: Same-instant tolerance between an rpm sample and a GPS speed fix.
PAIRING = PairingPolicy.within("60ms")
#: Steady cruise: speed and rpm/speed each within 1 % across five GPS fixes.
STEADY_TOLERANCE = 0.01
STEADY_SPAN_S = 6.0
#: Below this, GPS speed's own resolution is a larger share of the reading.
MIN_SPEED_KPH = 40.0
#: An rpm/speed further than this from every gear is a slip, a coast or a shift.
GEAR_TOLERANCE = 0.05
#: Fewer steady pairs than this and the drive gives no value, by name.
MIN_PAIRS = 50
#: Gear-choice prior when the profile has no circumference: a typical road
#: tyre. Only picks the gear, never enters the measurement.
DEFAULT_PRIOR_M = 2.6

#: Bumped when the method changes, so values from different methods are never
#: trended together (:mod:`src.control.tyres` groups by it).
METHOD_VERSION = 1


@dataclass(frozen=True)
class RollingCircumference:
    """One drive's measurement, or why there is none."""

    available: bool
    method_version: int
    rpm_channel: str
    #: Median of the steady pairs' circumference, metres.
    median_m: float | None = None
    n_pairs: int = 0
    #: Standard error of the median, % of it (1.2533 σ / √n).
    se_pct: float | None = None
    #: Steady pairs per gear, ``{"5": 812, ...}``.
    gears: dict[str, int] | None = None
    speed_kph: tuple[float, float] | None = None
    #: Machine-readable kind when not available.
    reason: str | None = None
    detail: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "available": self.available,
            "method_version": self.method_version,
            "rpm_channel": self.rpm_channel,
            "median_m": self.median_m,
            "n_pairs": self.n_pairs,
            "se_pct": self.se_pct,
            "gears": self.gears,
            "speed_kph": list(self.speed_kph) if self.speed_kph is not None else None,
            "reason": self.reason,
            "detail": self.detail,
        }


def measure_rolling_circumference(
    session_dir: Path,
    *,
    rpm_channels: list[str],
    gearbox_ratios: tuple[float, ...],
    final_drive: float,
    prior_m: float | None = None,
) -> RollingCircumference:
    """The driven tyres' rolling circumference over ``session_dir``'s steady cruise.

    ``rpm_channels`` are tried in order (the vehicle's ``dyno.yaml``
    ``motion_sources.rpm``, best first); the first that pairs with GPS speed
    is used. ``prior_m`` (the profile's circumference, else
    :data:`DEFAULT_PRIOR_M`) only chooses each pair's gear: wear moves the
    circumference by well under :data:`GEAR_TOLERANCE`, so the prior's own
    error does not reach the result. A tyre of a different size from the prior
    by more than that matches no gear and is refused by name. Never raises for
    missing data: an absent GPS, no steady cruise or too few pairs is returned
    with its ``reason``.
    """
    prior_m = DEFAULT_PRIOR_M if prior_m is None else prior_m
    if not gearbox_ratios or not final_drive:
        return _refused(rpm_channels, "not_configured", "the profile has no gearbox or final drive")
    pairs: pd.DataFrame | None = None
    used = rpm_channels[0] if rpm_channels else ""
    for channel in rpm_channels:
        try:
            aligned = align_pairs(session_dir, channel, GPS_SPEED_CHANNEL, policy=PAIRING)
        except (ComparisonRefusedError, FileNotFoundError, ValueError):
            continue
        if len(aligned.pairs):
            pairs, used = aligned.pairs, channel
            break
    if pairs is None:
        return _refused(
            rpm_channels, "inputs_missing", f"no {GPS_SPEED_CHANNEL} pairs with {rpm_channels}"
        )
    ordered = pairs.sort_values("timestamp_b").reset_index(drop=True)
    speed = ordered["b"].to_numpy(dtype=float)
    rpm = ordered["a"].to_numpy(dtype=float)
    seconds = (
        pd.to_datetime(ordered["timestamp_b"], utc=True) - pd.Timestamp(0, tz="UTC")
    ) / pd.Timedelta(seconds=1)
    t = seconds.to_numpy(dtype=float)
    overall = np.asarray(gearbox_ratios, dtype=float) * final_drive
    circumferences: list[float] = []
    gears: dict[str, int] = {}
    kept_speed: list[float] = []
    for i in range(2, len(ordered) - 2):
        window = slice(i - 2, i + 3)
        if t[i + 2] - t[i - 2] > STEADY_SPAN_S or speed[i] <= MIN_SPEED_KPH:
            continue
        v, r = speed[window], rpm[window]
        if not np.all(v > 0):
            continue
        ratio = r / v
        if v.max() - v.min() > STEADY_TOLERANCE * v.mean():
            continue
        if ratio.max() - ratio.min() > STEADY_TOLERANCE * ratio.mean():
            continue
        # Each gear implies a circumference; the gear is the one nearest the
        # prior. Adjacent gears are 28 %+ apart, so within GEAR_TOLERANCE at
        # most one qualifies; a slip, coast or shift matches none.
        implied = speed[i] * 1000.0 / 60.0 / (rpm[i] / overall)
        errors = np.abs(implied / prior_m - 1.0)
        gear = int(np.argmin(errors))
        if errors[gear] > GEAR_TOLERANCE:
            continue
        circumferences.append(float(implied[gear]))
        gears[str(gear + 1)] = gears.get(str(gear + 1), 0) + 1
        kept_speed.append(float(speed[i]))
    if len(circumferences) < MIN_PAIRS:
        return _refused(
            [used],
            "too_few_steady_pairs",
            f"{len(circumferences)} steady-cruise pairs above {MIN_SPEED_KPH:.0f} km/h "
            f"within {GEAR_TOLERANCE:.0%} of a gear at {prior_m:.3f} m (need {MIN_PAIRS})",
        )
    values = np.asarray(circumferences)
    median = float(np.median(values))
    se = 1.2533 * float(values.std(ddof=1)) / np.sqrt(len(values))
    return RollingCircumference(
        available=True,
        method_version=METHOD_VERSION,
        rpm_channel=used,
        median_m=round(median, 5),
        n_pairs=len(values),
        se_pct=round(float(se / median * 100.0), 4),
        gears=dict(sorted(gears.items())),
        speed_kph=(round(min(kept_speed), 1), round(max(kept_speed), 1)),
    )


def measure_session(session_dir: Path, config: CanaryConfig) -> dict[str, Any]:
    """This drive's rolling circumference as the sidecar stores it, or why not.

    The rpm sources are the vehicle's ``dyno.yaml`` ``motion_sources.rpm``,
    else Mode 01 rpm; the gearbox, final drive and the gear-choice prior are
    the profile's. Never raises but ``MemoryError``: it is a report, and the
    digest and trip end call it beside work that must not fail with it.
    """
    from src.analysis.dyno import DYNO_FILENAME, load_dyno_profile
    from src.vehicle.profiles import profile_dir

    params = config.vehicle.parameters
    try:
        dyno = load_dyno_profile(profile_dir(config) / DYNO_FILENAME)
        sources = dyno.motion_sources if dyno is not None else None
        rpm_channels = list(sources.rpm) if sources is not None and sources.rpm else ["obd:RPM"]
        return measure_rolling_circumference(
            session_dir,
            rpm_channels=rpm_channels,
            gearbox_ratios=tuple(params.gearbox_ratios),
            final_drive=params.final_drive or 0.0,
            prior_m=params.tyre_loaded_circumference_m,
        ).to_dict()
    except MemoryError:
        raise
    except Exception as exc:  # noqa: BLE001 — a report, not a gate
        log.warning("rolling circumference failed for %s", session_dir.name, exc_info=True)
        return {
            "available": False,
            "method_version": METHOD_VERSION,
            "kind": "failed",
            "error": f"{type(exc).__name__}: {exc}",
        }


def write_rolling_circumference(session_dir: Path, block: dict[str, Any]) -> Path:
    """Atomic write of the ``rolling_circumference.json`` sidecar."""
    path = session_dir / ROLLING_CIRCUMFERENCE_FILENAME
    write_json_sidecar(path, block)
    return path


def _refused(channels: list[str], reason: str, detail: str) -> RollingCircumference:
    return RollingCircumference(
        available=False,
        method_version=METHOD_VERSION,
        rpm_channel=channels[0] if channels else "",
        reason=reason,
        detail=detail,
    )


__all__ = [
    "METHOD_VERSION",
    "RollingCircumference",
    "measure_rolling_circumference",
    "measure_session",
    "write_rolling_circumference",
]
