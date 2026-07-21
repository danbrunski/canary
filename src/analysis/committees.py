"""Sensor committees — cross-check redundant sensors of the *same* quantity.

A committee groups independent sensors that measure one physical quantity (e.g.
vehicle speed from the OBD wheel-speed PID and GPS speed-over-ground). In a
healthy system they agree; the per-tick **divergence** (their spread) is the
committee's residual — a ``derived:<NAME>`` signal that flags a *faulting
sensor* (VSS failure, GPS multipath, a drifting transducer).

This is deliberately distinct from the physical-relationship residuals in
:mod:`src.analysis.derived` (bank-balance, AFR asymmetry, TPS-vs-%): those
compare *different* quantities whose expected relationship indicates an
**engine/mechanical** fault while every sensor is working. A committee answers
"do my redundant sensors agree?"; a physical residual answers "is the machine
behaving?". They are not migrated into each other.

Design choices (see ``docs/sensor-committees.md``):

* **Residual-as-feature.** Divergence is an ordinary ``derived:`` column the
  IF/LSTM consume — no separate fusion source. A committee marked
  ``trainable=False`` is still *computed* (and available for inspection /
  dashboards) but kept out of the trained feature set via
  :func:`non_trainable_committee_outputs` (e.g. while a member's data is still
  pre-gate noisy).
* **N members.** The residual is the spread (max − min) across present members,
  so 2-member pairs and 3-member groups (baro / GPS / IMU vertical speed, etc.)
  use the same path.
* **Gates are row preconditions.** A committee only scores a row when every
  gate holds — e.g. cross-check OBD↔GPS speed *only* when the GPS fix is
  trustworthy, since a marginal-fix divergence is meaningless.
* **Transforms live upstream.** A member that needs a rate or unit conversion
  is precomputed as its own ``derived:`` column first; a committee only ever
  compares columns that already exist.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass

import pandas as pd

DERIVED_PREFIX = "derived:"


@dataclass(frozen=True)
class MemberGate:
    """A precondition on a channel for a committee to score a given row.

    A missing gate channel fails closed: if we can't confirm the fix is good,
    we don't assert a divergence.
    """

    channel: str
    min_value: float | None = None
    max_value: float | None = None

    def mask(self, frame: pd.DataFrame) -> pd.Series:
        if self.channel not in frame.columns:
            return pd.Series(False, index=frame.index)
        values = pd.to_numeric(frame[self.channel], errors="coerce")
        mask = values.notna()
        if self.min_value is not None:
            mask &= values >= self.min_value
        if self.max_value is not None:
            mask &= values <= self.max_value
        return mask


@dataclass(frozen=True)
class SensorCommittee:
    """Redundant sensors of one quantity; emits a divergence residual."""

    name: str  # -> derived:<name>
    quantity: str  # human label, e.g. "vehicle speed (km/h)"
    members: tuple[str, ...]  # wide columns measuring the same quantity
    gates: tuple[MemberGate, ...] = ()  # ALL must hold for a row to score
    min_members: int = 2  # need at least this many present to cross-check
    trainable: bool = True  # False -> computed but excluded from trained features

    @property
    def output(self) -> str:
        return f"{DERIVED_PREFIX}{self.name}"


#: The OBD wheel-speed PID and GPS speed-over-ground measure the same quantity.
#: Cross-checked only on a trustworthy GPS fix (matching the capture-time gate
#: in ``gps_collector``). ``trainable=False`` for now: the stored corpus predates
#: the GPS quality-gate, so its divergence is mostly GPS noise — wire it in, but
#: keep it out of the trained set until quality-gated data accumulates.
SPEED_COMMITTEE = SensorCommittee(
    name="SPEED_SOURCE_DIVERGENCE",
    quantity="vehicle speed (km/h)",
    members=("obd:SPEED", "gps:speed"),
    gates=(
        MemberGate("gps:num_satellites", min_value=7),
        MemberGate("gps:hdop", max_value=2.0),
    ),
    trainable=False,
)

#: Future committees (docs/sensor-committees.md §Roadmap) — declared now so
#: they activate automatically as IMU/baro hardware channels appear (Phase
#: 2b), no code change needed then. Every member column is either IMU/baro
#: (no hardware yet) or a derived rate/rate-of-climb channel nothing computes
#: yet, so ``append_committee_residuals``'s "fewer than min_members present"
#: skip makes each of these a true no-op today — the same gating the landed
#: SPEED_COMMITTEE already relies on, just with zero members present instead
#: of one. All ``trainable=False`` (2a.8 pre-made decision): compute-but-
#: exclude until real, quality-gated data accumulates, same reasoning as
#: SPEED_COMMITTEE's own ``trainable=False``.

#: IMU aₓ vs. the OBD/GPS speed derivatives — the doc's headline payoff
#: (clean transient longitudinal events) triple-checked against two
#: independent ground truths.
LONGITUDINAL_ACCEL_COMMITTEE = SensorCommittee(
    name="LON_ACCEL_SOURCE_DIVERGENCE",
    quantity="longitudinal acceleration (g)",
    members=("imu:ACCEL_LON", "derived:OBD_SPEED_ACCEL_G", "derived:GPS_SPEED_ACCEL_G"),
    trainable=False,
)

#: GPS course-rate vs. IMU gyro-Z — corners out of the horizontal plane
#: (the one component OBD alone can never see).
HEADING_YAW_COMMITTEE = SensorCommittee(
    name="HEADING_RATE_DIVERGENCE",
    quantity="yaw rate (deg/s)",
    members=("derived:GPS_COURSE_RATE", "imu:GYRO_Z"),
    trainable=False,
)

#: OBD≈0 & GPS≈0 & IMU-still — a bus-independent "actually stopped" check
#: that also hardens ``idle_transient`` (a VSS fault reading a false 0
#: wouldn't fool GPS+IMU too).
#: min_members=3 (not the usual 2) is deliberate: OBD↔GPS alone is already
#: SPEED_COMMITTEE's job. This one only means anything as a genuine
#: three-way stationary confirmation, so it must stay a true no-op (not
#: silently start scoring on OBD+GPS alone) until the IMU stillness member
#: exists too.
STATIONARY_CONFIRM_COMMITTEE = SensorCommittee(
    name="STATIONARY_CONFIRM_DIVERGENCE",
    quantity="stationary confidence (speed proxy, km/h)",
    members=("obd:SPEED", "gps:speed", "derived:IMU_STILLNESS_SPEED_PROXY"),
    min_members=3,
    trainable=False,
)

#: GPS altitude vs. barometric altitude — GPS alone is ±10-30 m and noisy;
#: the baro backstop is what makes a usable ``derived:GRADE`` possible.
ALTITUDE_COMMITTEE = SensorCommittee(
    name="ALTITUDE_SOURCE_DIVERGENCE",
    quantity="altitude (m)",
    members=("gps:altitude", "baro:altitude"),
    trainable=False,
)

#: Barometric pressure vs. the OBD baro PID (0x33) vs. MAP-at-idle — cross
#: checks the pressure channel the grade payoff and load-vs-MAP models
#: both lean on.
BARO_PRESSURE_COMMITTEE = SensorCommittee(
    name="BARO_PRESSURE_DIVERGENCE",
    quantity="barometric pressure (kPa)",
    members=("baro:pressure", "obd:BAROMETRIC_PRESSURE", "derived:MAP_AT_IDLE_KPA"),
    trainable=False,
)

#: Baro altitude-rate vs. GPS vertical speed vs. IMU's integrated vertical
#: accel — the direct ``derived:GRADE`` de-confounding channel (payoff #2).
GRADE_COMMITTEE = SensorCommittee(
    name="VERTICAL_SPEED_DIVERGENCE",
    quantity="vertical speed (m/s)",
    members=(
        "derived:BARO_ALT_RATE",
        "derived:GPS_VERTICAL_SPEED",
        "derived:IMU_VERTICAL_ACCEL_INTEGRAL",
    ),
    trainable=False,
)

#: Active committees. Append baro/IMU committees here as that hardware lands —
#: each is one declaration (altitude, yaw-rate, longitudinal accel, ...).
COMMITTEES: tuple[SensorCommittee, ...] = (
    SPEED_COMMITTEE,
    LONGITUDINAL_ACCEL_COMMITTEE,
    HEADING_YAW_COMMITTEE,
    STATIONARY_CONFIRM_COMMITTEE,
    ALTITUDE_COMMITTEE,
    BARO_PRESSURE_COMMITTEE,
    GRADE_COMMITTEE,
)


def append_committee_residuals(
    wide: pd.DataFrame,
    committees: Sequence[SensorCommittee] = COMMITTEES,
) -> pd.DataFrame:
    """Append ``derived:<NAME>`` divergence columns for each scorable committee.

    A committee is skipped entirely when fewer than ``min_members`` of its
    members are columns in the frame. Otherwise the residual is the per-row
    spread across present members, NaN where the gates don't hold or too few
    members reported — so a session lacking the inputs cleanly omits the signal.
    """
    if wide.empty:
        return wide.copy()
    out = wide.copy()
    for committee in committees:
        present = [m for m in committee.members if m in out.columns]
        if len(present) < committee.min_members:
            continue
        values = pd.concat(
            [pd.to_numeric(out[m], errors="coerce") for m in present],
            axis=1,
            keys=present,
        )
        scorable = values.notna().sum(axis=1) >= committee.min_members
        for gate in committee.gates:
            scorable &= gate.mask(out)
        spread = values.max(axis=1) - values.min(axis=1)
        out[committee.output] = spread.where(scorable).astype(float)
    return out


def non_trainable_committee_outputs(
    committees: Sequence[SensorCommittee] = COMMITTEES,
) -> frozenset[str]:
    """``derived:<NAME>`` outputs to exclude from the trained feature set."""
    return frozenset(c.output for c in committees if not c.trainable)
