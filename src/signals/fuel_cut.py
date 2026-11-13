"""Decel fuel cut, as the ECM reports it (P02.15, M01.4).

Its own module because both :mod:`src.signals.derived` (the fuel rate is zero
in fuel cut, M01.4) and :mod:`src.signals.afr` (the target map has no value
then, P02.15) need it, and ``afr`` imports ``derived``.
"""

from __future__ import annotations

import pandas as pd

#: J1979 PID 0x44, commanded equivalence ratio. Exactly zero is the ECM
#: commanding no fuel at all: decel fuel cut (``AfrCalibration.commanded_afr_series``
#: reads it the same way), whatever the vehicle's phi/lambda convention.
COMMANDED_EQUIV_RATIO_COLUMN = "obd:COMMANDED_EQUIV_RATIO"


def fuel_cut_rows(frame: pd.DataFrame) -> pd.Series:
    """Rows on which the ECM reports decel fuel cut: its commanded
    equivalence ratio (PID 0x44) reads exactly zero (P02.15).

    A sentinel is a state, not a number: the ECM has no target then (UpRev
    displays its TARGET AFR as 30.00), so the target map has nothing to say,
    and the injectors are off, so no fuel flows (M01.4). PID 0x03
    ``FUEL_STATUS`` cannot stand in. Its fuel-cut code (0x04) is "open loop
    for load *or* decel fuel cut" by J1979. On the Xterra's two default
    drives it reads 0x04 on 154 rows that also carry phi: 114 at phi 0, 35
    near stoich, 3 under enrichment (phi 1.15-1.16), so it would withhold real
    targets. Phi 0 agrees with it the other way (114 of 117 phi-0 rows read
    0x04). A row with no commanded ratio is not in fuel cut as far as this
    knows: absent, the state is unknown, not "cut".
    """
    if COMMANDED_EQUIV_RATIO_COLUMN not in frame:
        return pd.Series(False, index=frame.index, dtype=bool)
    ratio = pd.to_numeric(frame[COMMANDED_EQUIV_RATIO_COLUMN], errors="coerce")
    return ratio.eq(0.0).fillna(False).astype(bool)


__all__ = ["COMMANDED_EQUIV_RATIO_COLUMN", "fuel_cut_rows"]
