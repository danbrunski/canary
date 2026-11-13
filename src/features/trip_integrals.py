"""Session-scoped trip integrals for the live feature source (Task 1.3, R27.6).

A live tick's ring buffer evicts old rows continuously, so a ``cumsum`` over
that window (what ``compute_derived_channels`` does for a *complete* offline
session) would reset every time the buffer slides, not just at a session
boundary. :class:`TripIntegrals` instead accumulates incrementally across
ticks, using each tick's real ``dt`` (``MAX_INTEGRATION_DT_S``-capped, as the
batch path caps its per-row dt). A fresh instance is a new session: the live
source replaces it on ``reset`` (the replay-isolation boundary), so it reads
"since this session started" the way the batch cumsum reads "since the
recording started".

Its batch twin is ``compute_derived_channels``' cumulative columns, pinned
equal by ``test_live_trip_integrals_match_batch_compute_derived_channels``.

Pure: no I/O and no clock of its own. The caller supplies the tick's ``now``,
its operating row, the bindings that scale a ``decoded:`` fallback, the speed
correction and the fuel constants, each read per call so a hot-reloaded
config applies on the next tick.

Distance integrates *road* speed: :meth:`TripSample.from_point` applies the
same :class:`~src.signals.speed_correction.SpeedCorrection` the batch path's
``compute_derived_channels`` applies (M01.2). The fuel rate it reads is the
operating row's ``derived:FUEL_RATE_LPH``, which
``compute_instantaneous_channels`` already zeroed in decel fuel cut (M01.4),
so neither correction has a second, live-only implementation.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any

from src.coerce import finite_float
from src.decode.bindings import Bindings
from src.decode.engineering import physical_value
from src.signals.derived import (
    IDLE_RPM_FLOOR,
    IDLE_SPEED_CEIL_KPH,
    MAX_INTEGRATION_DT_S,
    FuelProperties,
)
from src.signals.speed_correction import TRIP_SPEED_CHANNEL, SpeedCorrection
from src.telemetry import DECODED_SOURCE, OBD_SOURCE, channel_key

#: The cumulative columns :meth:`TripIntegrals.channels` reports, keyed as
#: ``compute_derived_channels`` keys them so a consumer can merge the dict
#: straight onto a feature row.
TRIP_DISTANCE_KM = "derived:TRIP_DISTANCE_KM"
FUEL_USED_L = "derived:FUEL_USED_L"
CO2_KG = "derived:CO2_KG"
IDLE_TIME_S = "derived:IDLE_TIME_S"
AVG_ECONOMY_L_PER_100KM = "derived:AVG_ECONOMY_L_PER_100KM"

#: A per-quantity last-accrual marker: when, and at what value, the integrand
#: was last observed.
Marker = tuple[datetime, float]


@dataclass(frozen=True)
class TripSample:
    """One tick's integrands, in engineering units, ``None`` when absent.

    :meth:`from_point` is the one place the operating row is read, with the
    same candidates as ``compute_derived_channels`` (``derived._series``):
    prefer the OBD PID, fall back to the decoded broadcast. ``speed_kph`` is
    road speed, ``speed_correction`` applied.
    """

    speed_kph: float | None
    rpm: float | None
    fuel_lph: float | None

    @classmethod
    def from_point(
        cls,
        point: dict[str, Any],
        bindings: Bindings | None,
        speed_correction: SpeedCorrection,
    ) -> TripSample:
        # ``_first_finite`` scales a ``decoded:`` fallback to engineering
        # units — the broadcast rpm is 8 counts/rpm, and before 2026-09-18
        # its raw count went straight into the 200 rpm idle gate.
        indicated = _first_finite(point, TRIP_SPEED_CHANNEL, bindings=bindings)
        return cls(
            speed_kph=None if indicated is None else speed_correction.road_speed(indicated),
            rpm=_first_finite(
                point,
                channel_key(OBD_SOURCE, "RPM"),
                channel_key(DECODED_SOURCE, "ECM_RPM"),
                bindings=bindings,
            ),
            fuel_lph=_first_finite(point, "derived:FUEL_RATE_LPH", bindings=bindings),
        )


@dataclass
class TripIntegrals:
    """Running trip distance, fuel, CO2 and idle time for one session.

    Each integrand keeps its own last-observed marker: a tick where that input
    is stale/absent leaves the marker alone, so the elapsed time is bridged
    (``MAX_INTEGRATION_DT_S``-capped) at the next fresh sample instead of
    being dropped — the batch integral's valid-sample dt
    (``_cumulative_time_integral``). A single shared last-tick timestamp
    discarded every stale tick's dt and halved the live trip odometer on a
    contended bus.
    """

    trip_distance_km: float = 0.0
    fuel_used_l: float = 0.0
    co2_kg: float = 0.0
    idle_time_s: float = 0.0
    speed_marker: Marker | None = field(default=None)
    fuel_marker: Marker | None = field(default=None)
    idle_marker: Marker | None = field(default=None)

    def accrue(self, now: datetime, sample: TripSample, fuel: FuelProperties) -> dict[str, float]:
        """Advance every integrand ``sample`` carries to ``now``; return :meth:`channels`.

        Same ``MAX_INTEGRATION_DT_S`` cap, same idle gate and same trapezoid
        between consecutive *observed* samples as ``compute_derived_channels``.
        """
        now_utc = now if now.tzinfo is not None else now.replace(tzinfo=UTC)
        if sample.fuel_lph is not None:
            increment, self.fuel_marker = _trapezoid(
                self.fuel_marker, now_utc, sample.fuel_lph / 3600.0
            )
            self.fuel_used_l += increment
            self.co2_kg = self.fuel_used_l * fuel.co2_kg_per_l
        if sample.speed_kph is not None:
            increment, self.speed_marker = _trapezoid(
                self.speed_marker, now_utc, sample.speed_kph / 3600.0
            )
            self.trip_distance_km += increment
        if sample.rpm is not None and sample.speed_kph is not None:
            idling = (
                1.0
                if (sample.rpm >= IDLE_RPM_FLOOR and sample.speed_kph < IDLE_SPEED_CEIL_KPH)
                else 0.0
            )
            increment, self.idle_marker = _trapezoid(self.idle_marker, now_utc, idling)
            self.idle_time_s += increment
        return self.channels()

    def channels(self) -> dict[str, float]:
        """The running totals, keyed as ``compute_derived_channels`` keys them."""
        avg_economy = float("nan")
        if self.trip_distance_km > 0:
            avg_economy = self.fuel_used_l / self.trip_distance_km * 100.0
        return {
            TRIP_DISTANCE_KM: self.trip_distance_km,
            FUEL_USED_L: self.fuel_used_l,
            CO2_KG: self.co2_kg,
            IDLE_TIME_S: self.idle_time_s,
            AVG_ECONOMY_L_PER_100KM: avg_economy,
        }


def _trapezoid(marker: Marker | None, now: datetime, value: float) -> tuple[float, Marker]:
    """Trapezoid increment from ``marker`` to ``(now, value)``, and the new marker."""
    if marker is None:
        return 0.0, (now, value)
    seen_at, seen_value = marker
    dt = min(max((now - seen_at).total_seconds(), 0.0), MAX_INTEGRATION_DT_S)
    return (value + seen_value) / 2.0 * dt, (now, value)


def _first_finite(
    point: dict[str, Any], *keys: str, bindings: Bindings | None = None
) -> float | None:
    """First present, non-NaN value among ``keys`` in a tick's operating row,
    in engineering units.

    Mirrors ``derived._series``'s "prefer OBD, fall back to decoded" lookup,
    but over a single-row dict (``LiveFeatureSource.latest_operating_point``)
    rather than a DataFrame column. A ``decoded:`` key holds raw binding
    counts, so it is scaled through :func:`src.decode.engineering.
    physical_value` (``bindings`` or the repo catalog) — never returned as-is.
    """
    for key in keys:
        if key not in point:
            continue
        value = finite_float(point[key])
        if value is not None:
            return physical_value(value, key, bindings)
    return None


__all__ = ["TripIntegrals", "TripSample"]
