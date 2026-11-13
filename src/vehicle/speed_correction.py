"""The trip path's speed correction, read from the vehicle's measured record (M01.2).

The ratio of the ECM's speed to GPS speed is a measured fact about one truck
(its tires, its gearing, its speedometer), so it is read from that profile's
own ``dyno.yaml`` (``motion_sources.speed``, the entry for
:data:`~src.signals.speed_correction.TRIP_SPEED_CHANNEL`), beside the
evidence that says when and how it was measured. It is deliberately not looked
up through a platform (``vehicle_data_file``): two trucks of one generation on
different tires read differently, and the 2013 Xterra on stock 265s must not
borrow the regeared 2008's ratio. No file, or no entry for the channel, is an
:meth:`~src.signals.speed_correction.SpeedCorrection.unmeasured` correction
that names which of the two it was; a file that exists but is malformed
raises, like ``analysis.dyno_profile.load_dyno_profile``.

The schema of the section belongs to ``src.analysis.dyno_contract.MotionSources``,
which this layer cannot import; ``tests/test_speed_correction.py`` checks that
this reader and that schema return the same ratio for every tracked profile.

Read through a cache keyed on the file's mtime, so the live tick (which asks
once per tick, as it re-reads the fuel constants) pays a ``stat``, and an
edited ``dyno.yaml`` applies on the next tick.
"""

from __future__ import annotations

from functools import lru_cache
from pathlib import Path
from typing import TYPE_CHECKING, Any

from src.signals.speed_correction import TRIP_SPEED_CHANNEL, SpeedCorrection
from src.vehicle.profiles import profile_dir
from src.yaml_load import load_yaml

if TYPE_CHECKING:
    from src.config import CanaryConfig

#: The profile file the ratio is read from; the same file the dyno reads its
#: speed sources from (``src.analysis.dyno_profile.DYNO_FILENAME``).
DYNO_FILENAME = "dyno.yaml"


def trip_speed_correction(config: CanaryConfig) -> SpeedCorrection:
    """The active vehicle's trip speed correction (its profile's ``dyno.yaml``)."""
    return load_trip_speed_correction(profile_dir(config))


def load_trip_speed_correction(profile_data: Path) -> SpeedCorrection:
    """The trip speed correction recorded in ``profile_data``/``dyno.yaml``."""
    path = profile_data / DYNO_FILENAME
    try:
        mtime_ns = path.stat().st_mtime_ns
    except FileNotFoundError:
        return SpeedCorrection.unmeasured(
            f"this vehicle has no {DYNO_FILENAME}, so no measured ratio_to_gps"
        )
    return _read(str(path), mtime_ns)


@lru_cache(maxsize=16)
def _read(path: str, mtime_ns: int) -> SpeedCorrection:
    del mtime_ns  # the cache key: an edited file is a new entry
    raw = load_yaml(Path(path).read_text())
    if not isinstance(raw, dict):
        raise ValueError(f"{path} is not a mapping; a dyno profile needs at least schema_version")
    for entry in _speed_sources(raw, path):
        if entry.get("channel") != TRIP_SPEED_CHANNEL:
            continue
        ratio = entry.get("ratio_to_gps")
        evidence = entry.get("evidence")
        if not isinstance(ratio, int | float) or not isinstance(evidence, str):
            raise ValueError(
                f"{path}: motion_sources.speed entry for {TRIP_SPEED_CHANNEL} needs a numeric "
                "ratio_to_gps and its evidence"
            )
        return SpeedCorrection.measured(float(ratio), f"{DYNO_FILENAME} motion_sources")
    return SpeedCorrection.unmeasured(
        f"{DYNO_FILENAME} records no ratio_to_gps for {TRIP_SPEED_CHANNEL}"
    )


def _speed_sources(raw: dict[str, Any], path: str) -> list[dict[str, Any]]:
    motion = raw.get("motion_sources")
    if motion is None:
        return []
    speed = motion.get("speed", []) if isinstance(motion, dict) else None
    if not isinstance(speed, list) or not all(isinstance(e, dict) for e in speed):
        raise ValueError(f"{path}: motion_sources.speed must be a list of mappings")
    return speed


__all__ = ["DYNO_FILENAME", "load_trip_speed_correction", "trip_speed_correction"]
