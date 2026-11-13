"""Indicated speed to road speed for the trip path (M01.2).

The trip path integrates the ECM's Mode 01 speed (``obd:SPEED``), and an ECM's
speed is not road speed: tire growth, a regear or a conservative speedometer
scale it. The Xterra's reads 0.9203 x GPS, so an uncorrected trip read 8.0 %
short and its L/100 km 8.7 % high. :class:`SpeedCorrection` is the one place
that ratio is applied. ``compute_instantaneous_channels``,
``compute_derived_channels`` and ``summarize_trip`` (batch) and
``TripSample.from_point`` (live) all take one, so the two paths cannot correct
differently, and the vehicle-layer loader
(:func:`src.vehicle.speed_correction.trip_speed_correction`) reads the ratio
from the profile's measured record.

A vehicle with no measured ratio is not given a default: it integrates the
channel as reported, and :attr:`SpeedCorrection.basis` says so. The trip
summary carries that string as a required field (``speed_basis``), so a
figure never travels without the basis it was computed on.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import overload

import pandas as pd

#: The speed channel the trip integrals read. One name, shared by the batch
#: (``derived._series``) and live (``TripSample.from_point``) readers and by
#: the loader that looks up its ratio, so they cannot read different channels.
TRIP_SPEED_CHANNEL = "obd:SPEED"

#: The bounds a measured ``ratio_to_gps`` may take, the same as the dyno's
#: ``SpeedSource`` (``src.analysis.dyno_contract``): outside them the number is
#: a unit or a wrong-channel error, not a scale.
MIN_RATIO_TO_GPS = 0.5
MAX_RATIO_TO_GPS = 1.5


@dataclass(frozen=True)
class SpeedCorrection:
    """How the trip path turns :data:`TRIP_SPEED_CHANNEL` into road speed.

    ``ratio_to_gps`` is indicated / true, measured on this vehicle (road speed
    = indicated / ratio); ``None`` means no ratio was measured and the channel
    is integrated as reported. ``source`` says where the ratio came from, or
    why there is none. Build one with :meth:`measured` or :meth:`unmeasured`.
    """

    ratio_to_gps: float | None
    source: str

    def __post_init__(self) -> None:
        if not self.source.strip():
            raise ValueError("a speed correction must say where its ratio came from")
        ratio = self.ratio_to_gps
        if ratio is not None and not MIN_RATIO_TO_GPS < ratio < MAX_RATIO_TO_GPS:
            raise ValueError(
                f"ratio_to_gps {ratio!r} for {TRIP_SPEED_CHANNEL} is outside "
                f"({MIN_RATIO_TO_GPS}, {MAX_RATIO_TO_GPS}): a unit or channel error, not a scale"
            )

    @classmethod
    def measured(cls, ratio_to_gps: float, source: str) -> SpeedCorrection:
        return cls(ratio_to_gps=float(ratio_to_gps), source=source)

    @classmethod
    def unmeasured(cls, reason: str) -> SpeedCorrection:
        return cls(ratio_to_gps=None, source=reason)

    @overload
    def road_speed(self, indicated: float) -> float: ...

    @overload
    def road_speed(self, indicated: pd.Series) -> pd.Series: ...

    def road_speed(self, indicated: float | pd.Series) -> float | pd.Series:
        """Road speed for an indicated reading (or series), same units."""
        if self.ratio_to_gps is None:
            return indicated
        return indicated / self.ratio_to_gps

    @property
    def basis(self) -> str:
        """One line naming the speed a trip figure was computed on."""
        if self.ratio_to_gps is None:
            return f"{TRIP_SPEED_CHANNEL} as reported, uncorrected: {self.source}"
        return (
            f"{TRIP_SPEED_CHANNEL} / {self.ratio_to_gps:g} "
            f"(measured ratio to GPS speed, {self.source})"
        )


__all__ = [
    "MAX_RATIO_TO_GPS",
    "MIN_RATIO_TO_GPS",
    "TRIP_SPEED_CHANNEL",
    "SpeedCorrection",
]
