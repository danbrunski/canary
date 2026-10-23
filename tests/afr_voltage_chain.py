"""Synthetic voltage transfers that satisfy the B06.7 schema, for tests.

A voltage transfer must say which ECM table it reproduces and come with the
ECM's barometric gain and the declared residual sources
(``src/signals/afr.py``). Tests that build their own curve get a provenance
whose ECM table *is* that curve (reproduction error 0), and the Xterra's real
``baro_gain`` / ``voltage_residual_sources`` from the profile, so the gain
arithmetic under test is the vehicle's.

:data:`SEA_LEVEL_KPA` sits inside the ECM's skip window (gain exactly 1), for
tests about something other than the gain; :func:`baro_columns` adds a fresh
baro reading to a wide frame.
"""

from __future__ import annotations

from functools import cache
from pathlib import Path
from typing import Any

import pandas as pd

from src.columns import arrived_column
from src.yaml_load import load_yaml

PROFILE_AFR = Path("data/nissan-xterra-2008/afr_calibration.yaml")
BARO = "obd:BAROMETRIC_PRESSURE"
#: A PID 0x33 reading the ECM does not correct at (99.52-111.70 kPa): gain 1.
SEA_LEVEL_KPA = 101.0
VOLTS_PER_X2 = 0.0025


@cache
def _profile() -> dict[str, Any]:
    return dict(load_yaml(PROFILE_AFR.read_text()))


def transfer(points: list[dict[str, float]], **extra: Any) -> dict[str, Any]:
    """A ``voltage_transfer`` dict for ``points`` whose provenance excludes what
    the profile's does (the barometric gain and the residual sources)."""
    real = _profile()["voltage_transfer"]["provenance"]
    provenance = {
        "method": "log_cross_reference",
        "source": "synthetic test curve",
        "volts_per_count": VOLTS_PER_X2,
        "excludes": list(real["excludes"]),
        "verified_on": real["verified_on"],
    }
    return {"points": list(points), "provenance": provenance, **extra}


def voltage_chain(points: list[dict[str, float]], **extra: Any) -> dict[str, Any]:
    """Calibration keys for a voltage route on ``points``: transfer, gain, residuals."""
    profile = _profile()
    return {
        "voltage_transfer": transfer(points, **extra),
        "baro_gain": profile["baro_gain"],
        "voltage_residual_sources": profile["voltage_residual_sources"],
    }


def baro_columns(frame: pd.DataFrame, kpa: float = SEA_LEVEL_KPA) -> pd.DataFrame:
    """``frame`` with a baro read arriving on every row (A03.1 flag included)."""
    return frame.assign(**{BARO: kpa, arrived_column(BARO): True})
