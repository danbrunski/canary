"""Sensor committees — cross-check redundant sensors of the *same* quantity.

A committee groups independent sensors that measure one physical quantity (e.g.
vehicle speed from the OBD wheel-speed PID and GPS speed-over-ground). In a
healthy system they keep a known relationship; a sustained **departure** from
it means a sensor is faulting (VSS failure, GPS multipath, a loose IMU mount).

This is deliberately distinct from the physical-relationship residuals in
:mod:`src.signals.derived` (bank-balance, AFR asymmetry, TPS-vs-%): those
compare *different* quantities whose expected relationship indicates an
**engine/mechanical** fault while every sensor is working. A committee answers
"do my redundant sensors agree?"; a physical residual answers "is the machine
behaving?". They are not migrated into each other.

Committees are **detectors, not features** (H04.1; H00 operator decisions 1
and 2, 2026-09-23). Every committee has an external-sensor member, and external
sensors never train or score the Isolation Forest, the sequence detector or the
behavior models. So:

* **No committee channel trains.** :func:`committee_channel_reason` names every
  channel this module produces (the ``derived:<NAME>`` spread residuals and the
  member transforms below); :func:`src.trainability.source_exclusion_reason`
  refuses them by name. There is no per-committee ``trainable`` switch.
* **They flag instead.** :class:`CommitteeEvaluator` checks each committee's
  *relation* — the expected ratio/offset between members, measured per vehicle
  and loaded from ``data/<profile>/committees.yaml`` with its evidence — on
  every tick, and reports a sustained departure beyond the measured tolerance.
  A committee with no entry in that file is *not configured* (named), never
  run with defaults: a known offset such as this truck's ECU speed reading
  ~8 % under GPS would otherwise fire on every drive.
* **The spread residual stays** for the session fingerprint
  (:func:`append_committee_residuals`), in engineering units.

Design rules (``docs/sensor-committees.md``):

* **N members.** The residual is the spread (max − min) across present members.
* **Gates are row preconditions** and fail closed: a missing gate channel means
  no verdict, and the verdict says which gate.
* **Transforms live upstream.** A member that needs a rate or unit conversion
  is precomputed as its own ``derived:`` column
  (:func:`append_committee_member_transforms`); a committee only ever compares
  columns that already exist.
* **Every channel a committee reads has a declared producer**
  (:data:`CHANNEL_PRODUCERS`, H04.4). A member nothing delivers would make its
  relation compare nothing, silently — found 2026-10-03, when the IMU member
  was named ``imu:ACCEL_LON``, a column the decimation drops before any
  committee sees it. A committee naming an undeclared channel refuses at
  import (:class:`CommitteeDeclarationError`); one whose member is declared
  ``pending`` stays declared, but ``committees.yaml`` cannot configure a
  relation, reference or condition on it.
"""

from __future__ import annotations

import math
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING, Any, Literal

import pandas as pd
from pydantic import BaseModel, ConfigDict, Field, model_validator

from src.timefmt import format_epoch
from src.yaml_load import load_yaml

if TYPE_CHECKING:
    from src.config import CanaryConfig
    from src.decode.bindings import Bindings

DERIVED_PREFIX = "derived:"

#: Per-vehicle committee relations, in the profile data directory.
COMMITTEES_FILENAME = "committees.yaml"

#: Standard gravity, for m/s² → g.
STANDARD_GRAVITY = 9.80665


@dataclass(frozen=True)
class MemberGate:
    """A precondition on a channel for a committee to score a given row.

    A missing gate channel fails closed: if we can't confirm the fix is good,
    we don't assert a divergence.
    """

    channel: str
    min_value: float | None = None
    max_value: float | None = None
    #: ``|value| <= max_abs`` — e.g. "not accelerating hard" either way.
    max_abs: float | None = None

    def mask(self, frame: pd.DataFrame) -> pd.Series:
        if self.channel not in frame.columns:
            return pd.Series(False, index=frame.index)
        values = pd.to_numeric(frame[self.channel], errors="coerce")
        mask = values.notna()
        if self.min_value is not None:
            mask &= values >= self.min_value
        if self.max_value is not None:
            mask &= values <= self.max_value
        if self.max_abs is not None:
            mask &= values.abs() <= self.max_abs
        return mask

    def failure(self, point: Mapping[str, Any]) -> str | None:
        """Why this gate fails on one row, or ``None`` when it holds."""
        value = _finite(point.get(self.channel))
        if value is None:
            return f"{self.channel} has no value"
        if self.min_value is not None and value < self.min_value:
            return f"{self.channel} {value:g} < {self.min_value:g}"
        if self.max_value is not None and value > self.max_value:
            return f"{self.channel} {value:g} > {self.max_value:g}"
        if self.max_abs is not None and abs(value) > self.max_abs:
            return f"|{self.channel}| {abs(value):g} > {self.max_abs:g}"
        return None


@dataclass(frozen=True)
class SensorCommittee:
    """Redundant sensors of one quantity.

    No ``trainable`` flag (removed by H04.1): no committee channel ever trains.
    """

    name: str  # -> derived:<name>
    quantity: str  # human label, e.g. "vehicle speed (km/h)"
    members: tuple[str, ...]  # wide columns measuring the same quantity
    gates: tuple[MemberGate, ...] = ()  # ALL must hold for a row to score
    min_members: int = 2  # need at least this many present to cross-check

    @property
    def output(self) -> str:
        return f"{DERIVED_PREFIX}{self.name}"


#: The ECU's vehicle speed (the Mode 01 PID, or the ABS broadcast copy when the
#: capture profile does not poll the PID — ``tuning`` does not) and GPS
#: speed-over-ground measure the same quantity. Cross-checked only on a
#: trustworthy fix (matching the capture-time gate in ``gps_collector``).
#: Both ECU members inherit the same ~8 % under-read against GPS
#: (``data/bindings.yaml`` 0x6F7 notes), which is profile data, not a fault.
SPEED_COMMITTEE = SensorCommittee(
    name="SPEED_SOURCE_DIVERGENCE",
    quantity="vehicle speed (km/h)",
    members=("obd:SPEED", "decoded:WHEEL_SPEED_6F7_A", "gps:speed"),
    gates=(
        MemberGate("gps:num_satellites", min_value=7),
        MemberGate("gps:hdop", max_value=2.0),
    ),
)

#: Future committees (docs/sensor-committees.md §Roadmap) — declared now so
#: they activate automatically as IMU/baro hardware channels appear, no code
#: change needed then. Until enough members exist,
#: ``append_committee_residuals``'s "fewer than min_members present" skip makes
#: each a no-op, and until its relation is in the profile's
#: ``committees.yaml`` the evaluator reports it ``not_configured``.

#: The IMU's longitudinal-acceleration member: the signed per-tick mean of
#: the resolved ``imu:ACCEL_LON`` (``imu_decimation.ACCEL_LON_MEAN_COLUMN``).
#: Not ``imu:ACCEL_LON`` itself — that is a raw-rate column the decimation
#: replaces before resampling, so no committee ever sees it (H04.4).
IMU_ACCEL_LON_MEAN = "imu:ACCEL_LON_MEAN"

#: IMU aₓ vs. the OBD/GPS speed derivatives — the doc's headline payoff
#: (clean transient longitudinal events) triple-checked against two
#: independent ground truths. The two derivatives are computed by
#: :func:`append_committee_member_transforms` (H04.4).
LONGITUDINAL_ACCEL_COMMITTEE = SensorCommittee(
    name="LON_ACCEL_SOURCE_DIVERGENCE",
    quantity="longitudinal acceleration (g)",
    members=(IMU_ACCEL_LON_MEAN, "derived:OBD_SPEED_ACCEL_G", "derived:GPS_SPEED_ACCEL_G"),
)

#: GPS course-rate vs. IMU gyro-Z — corners out of the horizontal plane
#: (the one component OBD alone can never see).
HEADING_YAW_COMMITTEE = SensorCommittee(
    name="HEADING_RATE_DIVERGENCE",
    quantity="yaw rate (deg/s)",
    members=("derived:GPS_COURSE_RATE", "imu:GYRO_Z"),
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
)

#: GPS altitude vs. barometric altitude — GPS alone is ±10-30 m and noisy;
#: the baro backstop is what makes a usable ``derived:GRADE`` possible.
ALTITUDE_COMMITTEE = SensorCommittee(
    name="ALTITUDE_SOURCE_DIVERGENCE",
    quantity="altitude (m)",
    members=("gps:altitude", "baro:altitude"),
)

#: Barometric pressure vs. the OBD baro PID (0x33) vs. MAP-at-idle — cross
#: checks the pressure channel the grade payoff and load-vs-MAP models
#: both lean on.
BARO_PRESSURE_COMMITTEE = SensorCommittee(
    name="BARO_PRESSURE_DIVERGENCE",
    quantity="barometric pressure (kPa)",
    members=("baro:pressure", "obd:BAROMETRIC_PRESSURE", "derived:MAP_AT_IDLE_KPA"),
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

_BY_NAME: dict[str, SensorCommittee] = {c.name: c for c in COMMITTEES}


# ---------------------------------------------------------------------------
# Member transforms (H04.4) — upstream, so committees only compare columns
# ---------------------------------------------------------------------------

#: ECU-side longitudinal acceleration, in g, from the ECU's own speed. Stays
#: **ECU-relative** (it inherits the ECU speed's under-read): the committee
#: relation's ``ratio`` carries the vehicle's ECU/ground factor, exactly as the
#: speed committee's does, so this transform needs no profile data.
OBD_SPEED_ACCEL_G = f"{DERIVED_PREFIX}OBD_SPEED_ACCEL_G"
#: Ground-truth longitudinal acceleration, in g, from GPS speed-over-ground.
GPS_SPEED_ACCEL_G = f"{DERIVED_PREFIX}GPS_SPEED_ACCEL_G"

#: The ECU speed an acceleration is differentiated from, best first: the
#: ~100 Hz 0x2D1 broadcast (0.1 km/h/count), the 0x6F7 ABS copy (1/16 km/h),
#: and last the 1 km/h-quantised, ~0.7 Hz Mode 01 PID, whose steps make a poor
#: derivative. The first with any value in the frame is used for the whole
#: frame — never mixed per row, since two sources differ in lag.
OBD_ACCEL_SPEED_SOURCES: tuple[str, ...] = (
    "decoded:VEHICLE_SPEED_2D1",
    "decoded:WHEEL_SPEED_6F7_A",
    "obd:SPEED",
)
GPS_ACCEL_SPEED_SOURCE = "gps:speed"

#: Span of the trailing difference, s. GPS speed arrives at 1 Hz; a 1 s
#: difference of it is dominated by Doppler jitter, and a longer span lags a
#: real transient. The rate is Δv over the actual elapsed time between the two
#: rows, so an irregular grid still gives the right units.
ACCEL_SPAN_SECONDS = 2.0
#: A difference across a hole longer than this is not a rate; NaN instead.
ACCEL_MAX_SPAN_SECONDS = 5.0

#: Every member transform this module computes, with its inputs (best first).
MEMBER_TRANSFORMS: dict[str, tuple[str, ...]] = {
    OBD_SPEED_ACCEL_G: OBD_ACCEL_SPEED_SOURCES,
    GPS_SPEED_ACCEL_G: (GPS_ACCEL_SPEED_SOURCE,),
}


def speed_accel_g(
    speed_kmh: pd.Series,
    timestamps: pd.Series,
    *,
    span_seconds: float = ACCEL_SPAN_SECONDS,
    max_span_seconds: float = ACCEL_MAX_SPAN_SECONDS,
) -> pd.Series:
    """Longitudinal acceleration (g) from a speed series (km/h), row by row.

    Trailing difference against the newest row at least ``span_seconds``
    older, divided by the real elapsed time. NaN on the first rows, across a
    hole longer than ``max_span_seconds``, and wherever either speed is NaN.
    """
    times = pd.to_datetime(timestamps, utc=True)
    if not len(times):
        return pd.Series(dtype=float, index=speed_kmh.index)
    seconds = (times - times.iloc[0]).dt.total_seconds().to_numpy(dtype=float)
    values = pd.to_numeric(speed_kmh, errors="coerce").to_numpy(dtype=float)
    out = [math.nan] * len(values)
    start = 0
    for i in range(len(values)):
        # Advance to the newest earlier row still >= span_seconds back.
        while start + 1 < i and seconds[i] - seconds[start + 1] >= span_seconds:
            start += 1
        if start >= i:
            continue
        elapsed = seconds[i] - seconds[start]
        if elapsed < span_seconds or elapsed > max_span_seconds:
            continue
        delta = values[i] - values[start]
        if math.isnan(delta):
            continue
        out[i] = (delta / 3.6) / elapsed / STANDARD_GRAVITY
    return pd.Series(out, index=speed_kmh.index, dtype=float)


def append_committee_member_transforms(wide: pd.DataFrame, *, copy: bool = True) -> pd.DataFrame:
    """Append the ``derived:`` member columns committees compare (H04.4).

    Needs a ``timestamp`` column; without one (a single synthetic row) there
    is no rate to take and the frame is returned unchanged. ``decoded:``
    inputs are read through :func:`src.decode.engineering.physical_series`
    (raw counts are not km/h). An input with no value anywhere in the frame is
    skipped, so a session without it cleanly omits the transform.
    ``copy=False`` writes into ``wide`` (a frame the caller already owns).
    """
    if wide.empty or "timestamp" not in wide.columns:
        return wide
    out = wide
    for output, sources in MEMBER_TRANSFORMS.items():
        for source in sources:
            if source not in out.columns:
                continue
            speed = _physical(out[source], source)
            if speed.notna().any():
                if out is wide and copy:
                    out = wide.copy()
                out[output] = speed_accel_g(speed, out["timestamp"])
                break
    return out


# ---------------------------------------------------------------------------
# Producers (H04.4) — no committee names a channel nothing delivers
# ---------------------------------------------------------------------------

ProducerKind = Literal[
    "obd_pid",
    "can_binding",
    "gps_collector",
    "imu_collector",
    "imu_tick",
    "member_transform",
    "pending",
]


@dataclass(frozen=True)
class ChannelProducer:
    """What delivers a committee channel to the tick, live and offline.

    ``kind`` says which code path, so a test can hold each claim to its
    producer (``tests/test_committee_producers.py``): a Mode 01 PID name in
    ``pid_decoders.PID_DECODERS``, a ``data/bindings.yaml`` signal, a channel
    the GPS or IMU collector records, a per-tick IMU aggregate in
    ``imu_decimation.IMU_TICK_COLUMNS`` that ``imu_live.LiveImuFrame`` also
    emits, or one of :data:`MEMBER_TRANSFORMS`. ``pending`` is the honest
    answer for a member whose producer does not exist yet; ``by`` then names
    what will build it, and the profile cannot configure a relation on it.
    """

    kind: ProducerKind
    by: str
    #: What an absent value means, appended to a ``member_missing`` verdict so
    #: "no value" names its cause.
    absent: str = ""

    @property
    def delivered(self) -> bool:
        return self.kind != "pending"


#: The caller did not say why: :class:`CommitteeEvaluator.producer_status`
#: names the actual step (live ``LiveImuFrame``, offline the session's
#: ``imu_frame`` status) when the caller passes it.
_IMU_FRAME_ABSENT = "IMU frame status not reported by the caller"
#: Neutral: a missing Mode 01 value does not say which of these it was.
_NO_MODE01 = "no Mode 01 response this window: not polled, passive, or ECM silent"

#: Every channel any declared committee reads — members and gates. Checked at
#: import (:func:`check_committee_producers`).
CHANNEL_PRODUCERS: dict[str, ChannelProducer] = {
    "obd:SPEED": ChannelProducer("obd_pid", "Mode 01 PID 0x0D", _NO_MODE01),
    "obd:BAROMETRIC_PRESSURE": ChannelProducer("obd_pid", "Mode 01 PID 0x33", _NO_MODE01),
    "decoded:WHEEL_SPEED_6F7_A": ChannelProducer(
        "can_binding", "0x6F7 ABS broadcast (data/bindings.yaml)", "no 0x6F7 frame"
    ),
    "gps:speed": ChannelProducer("gps_collector", "GNSS speed over ground", "no fix"),
    "gps:altitude": ChannelProducer("gps_collector", "GNSS altitude", "no fix"),
    "gps:num_satellites": ChannelProducer("gps_collector", "GNSS fix report", "no fix"),
    "gps:hdop": ChannelProducer("gps_collector", "GNSS fix report", "no fix"),
    # The raw body-frame Z rate is recorded, but it is not the vehicle's yaw
    # rate until a frame rotates it (a tilted mount mixes roll and pitch rate
    # in): pending, so no relation can treat it as one.
    "imu:GYRO_Z": ChannelProducer(
        "pending", "a frame-rotated yaw-rate channel (raw imu:GYRO_Z is body-frame)"
    ),
    IMU_ACCEL_LON_MEAN: ChannelProducer(
        "imu_tick",
        "imu_decimation (offline, value_number chain) and imu_live.LiveImuFrame (live)",
        _IMU_FRAME_ABSENT,
    ),
    OBD_SPEED_ACCEL_G: ChannelProducer(
        "member_transform",
        "append_committee_member_transforms",
        f"none of {', '.join(OBD_ACCEL_SPEED_SOURCES)} over the last {ACCEL_SPAN_SECONDS:g} s",
    ),
    GPS_SPEED_ACCEL_G: ChannelProducer(
        "member_transform",
        "append_committee_member_transforms",
        f"no {GPS_ACCEL_SPEED_SOURCE} over the last {ACCEL_SPAN_SECONDS:g} s",
    ),
    "derived:GPS_COURSE_RATE": ChannelProducer(
        "pending", "nothing yet (docs/sensor-committees.md roadmap: yaw-rate committee)"
    ),
    "derived:IMU_STILLNESS_SPEED_PROXY": ChannelProducer(
        "pending", "nothing yet (docs/sensor-committees.md roadmap: stationary committee)"
    ),
    "baro:altitude": ChannelProducer("pending", "the HAT barometer (H03.6, part dead)"),
    "baro:pressure": ChannelProducer("pending", "the HAT barometer (H03.6, part dead)"),
    "derived:MAP_AT_IDLE_KPA": ChannelProducer(
        "pending", "nothing yet (docs/sensor-committees.md roadmap: baro committee)"
    ),
    "derived:BARO_ALT_RATE": ChannelProducer(
        "pending", "nothing yet; needs the HAT barometer (H03.6)"
    ),
    "derived:GPS_VERTICAL_SPEED": ChannelProducer(
        "pending", "nothing yet (docs/sensor-committees.md roadmap: grade committee)"
    ),
    "derived:IMU_VERTICAL_ACCEL_INTEGRAL": ChannelProducer(
        "pending", "nothing yet (docs/sensor-committees.md roadmap: grade committee)"
    ),
}


class CommitteeDeclarationError(ValueError):
    """A committee reads a channel with no declared producer."""


def check_committee_producers(
    committees: Sequence[SensorCommittee] = COMMITTEES,
    producers: Mapping[str, ChannelProducer] = CHANNEL_PRODUCERS,
) -> None:
    """Refuse, by committee and channel, any member or gate without a producer.

    Run at import on :data:`COMMITTEES` and by :class:`CommitteeEvaluator` on
    the committees it is given, so a committee that names a channel nothing
    delivers cannot load. A ``member_transform`` must be one this module
    computes.
    """
    problems: list[str] = []
    for committee in committees:
        channels = [*committee.members, *(gate.channel for gate in committee.gates)]
        for channel in channels:
            producer = producers.get(channel)
            if producer is None:
                problems.append(
                    f"{committee.name} reads {channel}, which has no declared producer "
                    "(add it to committees.CHANNEL_PRODUCERS, or kind='pending' naming "
                    "what will build it)"
                )
            elif producer.kind == "member_transform" and channel not in MEMBER_TRANSFORMS:
                problems.append(
                    f"{committee.name} reads {channel}, declared a member transform, but "
                    "MEMBER_TRANSFORMS does not compute it"
                )
    if problems:
        raise CommitteeDeclarationError("; ".join(problems))


def undelivered_reason(channel: str) -> str | None:
    """Why a profile may not configure ``channel``, or ``None`` when it may."""
    producer = CHANNEL_PRODUCERS.get(channel)
    if producer is None:
        return f"{channel} has no declared producer (committees.CHANNEL_PRODUCERS)"
    if not producer.delivered:
        return f"{channel} is not delivered by anything yet ({producer.by})"
    return None


check_committee_producers()


# ---------------------------------------------------------------------------
# Spread residuals (fingerprint)
# ---------------------------------------------------------------------------


def append_committee_residuals(
    wide: pd.DataFrame,
    committees: Sequence[SensorCommittee] = COMMITTEES,
) -> pd.DataFrame:
    """Append member transforms, then ``derived:<NAME>`` spread columns.

    A committee is skipped entirely when fewer than ``min_members`` of its
    members are columns in the frame. Otherwise the residual is the per-row
    spread (engineering units) across present members, NaN where the gates
    don't hold or too few members reported — so a session lacking the inputs
    cleanly omits the signal. None of these columns ever trains
    (:func:`committee_channel_reason`); they exist for the fingerprint and for
    :class:`CommitteeEvaluator`.
    """
    if wide.empty:
        return wide.copy()
    out = append_committee_member_transforms(wide.copy(), copy=False)
    for committee in committees:
        present = [m for m in committee.members if m in out.columns]
        if len(present) < committee.min_members:
            continue
        values = pd.concat(
            [_physical(out[m], m) for m in present],
            axis=1,
            keys=present,
        )
        scorable = values.notna().sum(axis=1) >= committee.min_members
        for gate in committee.gates:
            scorable &= gate.mask(out)
        spread = values.max(axis=1) - values.min(axis=1)
        out[committee.output] = spread.where(scorable).astype(float)
    return out


def committee_channel_reason(channel: str) -> str | None:
    """Why ``channel`` is a committee channel (so never a feature), or ``None``.

    Covers every committee's ``derived:<NAME>`` spread and every member
    transform. H00 operator decision 1: external sensors take part in
    detection only through committees, so no committee channel trains — even
    :data:`OBD_SPEED_ACCEL_G`, which reads only ECU data, is a committee
    member rather than a vetted feature.
    """
    for committee in COMMITTEES:
        if channel == committee.output:
            return (
                f"{channel} is the {committee.name} committee's spread over "
                f"{', '.join(committee.members)}; committees are detectors, not features "
                "(H04.1, H00 decision 1)"
            )
    if channel in MEMBER_TRANSFORMS:
        return (
            f"{channel} is a committee member transform of "
            f"{', '.join(MEMBER_TRANSFORMS[channel])}; committees are detectors, not features "
            "(H04.1, H00 decision 1)"
        )
    return None


def non_trainable_committee_outputs(
    committees: Sequence[SensorCommittee] = COMMITTEES,
) -> frozenset[str]:
    """Every committee channel: all are kept out of the trained feature set.

    Kept for ``feature_policy.candidate_feature_columns``; the source gate
    (:func:`committee_channel_reason` via :mod:`src.trainability`) refuses the
    same channels on every other path.
    """
    return frozenset(c.output for c in committees) | frozenset(MEMBER_TRANSFORMS)


# ---------------------------------------------------------------------------
# Relations (profile data) — H04.1
# ---------------------------------------------------------------------------


class _Strict(BaseModel):
    model_config = ConfigDict(extra="forbid")


class RelationSpec(_Strict):
    """``member ≈ ratio × reference + offset`` on this vehicle, with evidence."""

    member: str
    ratio: float = Field(gt=0)
    offset: float = 0.0
    #: Where the numbers were measured (sessions, method, n) — required.
    evidence: str = Field(min_length=1)


class ConditionSpec(_Strict):
    """A condition the tolerance was measured under. Outside it: no verdict."""

    channel: str
    min: float | None = None
    max: float | None = None
    max_abs: float | None = Field(default=None, gt=0)
    reason: str = Field(min_length=1)

    @model_validator(mode="after")
    def _bounded(self) -> ConditionSpec:
        if self.min is None and self.max is None and self.max_abs is None:
            raise ValueError(f"condition on {self.channel} states no bound")
        return self

    def gate(self) -> MemberGate:
        return MemberGate(
            self.channel, min_value=self.min, max_value=self.max, max_abs=self.max_abs
        )


class ToleranceSpec(_Strict):
    """How far a member may depart from its relation, and for how long.

    ``conditions`` is required (an explicit empty list is allowed): a
    tolerance measured while cruising says nothing about a hard launch, and
    the rows it was not measured on must get no verdict rather than a
    confident one.
    """

    #: ``relative``: ``(member − expected) / expected``; ``absolute``: in the
    #: member's units.
    kind: Literal["relative", "absolute"]
    max_departure: float = Field(gt=0)
    #: A departure must persist this long (wall time between the first and
    #: latest departed verdict) before it is an event.
    sustain_seconds: float = Field(gt=0)
    #: ...and over at least this many departed verdicts.
    min_verdicts: int = Field(ge=1)
    #: Longest gap between two departed verdicts that still counts as one run.
    #: Verdicts are routinely missing for a tick or two (a member polled below
    #: the tick rate did not arrive, a condition blinked); a longer hole means
    #: the disagreement was not continuous, and the run starts again.
    max_verdict_gap_seconds: float = Field(gt=0)
    conditions: list[ConditionSpec]
    evidence: str = Field(min_length=1)


class CommitteeSpec(_Strict):
    reference: str
    relations: list[RelationSpec] = Field(min_length=1)
    tolerance: ToleranceSpec


class CommitteeProfile(_Strict):
    """A vehicle's ``committees.yaml``."""

    schema_version: Literal[1]
    committees: dict[str, CommitteeSpec]

    @model_validator(mode="after")
    def _known(self) -> CommitteeProfile:
        for name, spec in self.committees.items():
            committee = _BY_NAME.get(name)
            if committee is None:
                raise ValueError(
                    f"committees.yaml names {name!r}, which is not a declared committee "
                    f"({sorted(_BY_NAME)})"
                )
            columns = [spec.reference, *(r.member for r in spec.relations)]
            foreign = [c for c in columns if c not in committee.members]
            if foreign:
                raise ValueError(f"{name}: {foreign} are not members of {committee.members}")
            if len(set(columns)) != len(columns):
                raise ValueError(f"{name}: reference and relation members must be distinct")
            # H04.4: a relation or condition on a channel nothing delivers
            # would compare nothing, silently.
            read = [*columns, *(c.channel for c in spec.tolerance.conditions)]
            undelivered = [r for r in map(undelivered_reason, read) if r is not None]
            if undelivered:
                raise ValueError(f"{name}: {'; '.join(undelivered)}")
        return self


def committees_path(config: CanaryConfig) -> Path:
    """The active profile's ``committees.yaml`` (beside ``plausibility.yaml``)."""
    return config.daemon.afr_calibration.parent / COMMITTEES_FILENAME


def load_committee_profile(path: Path) -> CommitteeProfile:
    """Parse and validate a ``committees.yaml``. Raises on a bad file."""
    return CommitteeProfile.model_validate(load_yaml(path.read_text()))


def resolve_committee_profile(config: CanaryConfig) -> tuple[CommitteeProfile | None, str]:
    """``(profile, path)``; ``None`` when the vehicle has no ``committees.yaml``.

    A *missing* file is not an error — every committee then reports
    ``not_configured``. A malformed one raises.
    """
    path = committees_path(config)
    if not path.exists():
        return None, str(path)
    return load_committee_profile(path), str(path)


# ---------------------------------------------------------------------------
# Verdicts and events — H04.2
# ---------------------------------------------------------------------------

CommitteeState = Literal["agree", "disagree", "no_verdict"]

#: Why a committee reached no verdict on a tick. ``not_configured``: no relation
#: in the profile. ``member_missing``/``reference_missing``: no value.
#: ``stale``: a value, but it did not arrive this tick (a forward-filled
#: reading is not a same-instant pair). ``gate``: an instrument-quality gate
#: failed (marginal GPS fix). ``condition``: outside the conditions the
#: tolerance was measured under (too slow, accelerating hard).
NO_VERDICT_REASONS = (
    "not_configured",
    "member_missing",
    "reference_missing",
    "stale",
    "gate",
    "condition",
)


@dataclass(frozen=True)
class RelationVerdict:
    member: str
    reference: str
    state: CommitteeState
    reason: str | None = None
    detail: str | None = None
    member_value: float | None = None
    reference_value: float | None = None
    expected: float | None = None
    departure: float | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "member": self.member,
            "reference": self.reference,
            "state": self.state,
            "reason": self.reason,
            "detail": self.detail,
            "member_value": self.member_value,
            "reference_value": self.reference_value,
            "expected": self.expected,
            "departure": self.departure,
        }


@dataclass(frozen=True)
class CommitteeFlag:
    """A sustained departure: one committee, one member, one episode."""

    committee: str
    quantity: str
    member: str
    reference: str
    relation: RelationSpec
    tolerance_kind: str
    max_departure: float
    sustain_seconds: float
    started_at: float
    detected_at: float
    verdicts: int
    member_value: float
    reference_value: float
    expected: float
    departure: float
    peak_departure: float
    session: str | None = None
    operating_mode: str | None = None

    @property
    def detector(self) -> str:
        return f"committee:{self.committee}"

    @property
    def text(self) -> str:
        """The one-line cause: both members, values, relation and departure."""
        if self.tolerance_kind == "relative":
            departure = f"{self.departure:+.1%}"
            limit = f"{self.max_departure:.0%}"
        else:
            departure = f"{self.departure:+.3g}"
            limit = f"{self.max_departure:.3g}"
        relation = f"{self.relation.ratio:g} x {self.reference}"
        if self.relation.offset:
            relation += f" {self.relation.offset:+g}"
        return (
            f"{self.committee}: {self.member} reads {self.member_value:.4g} where "
            f"{relation} expects {self.expected:.4g} "
            f"({self.reference} {self.reference_value:.4g}); "
            f"departure {departure} beyond {limit} for {self.detected_at - self.started_at:.0f} s"
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "detector": self.detector,
            "committee": self.committee,
            "quantity": self.quantity,
            "member": self.member,
            "reference": self.reference,
            "relation": self.relation.model_dump(mode="json"),
            "tolerance": {
                "kind": self.tolerance_kind,
                "max_departure": self.max_departure,
                "sustain_seconds": self.sustain_seconds,
            },
            "started_at": format_epoch(self.started_at, zulu=True),
            "detected_at": format_epoch(self.detected_at, zulu=True),
            "verdicts": self.verdicts,
            "member_value": self.member_value,
            "reference_value": self.reference_value,
            "expected": self.expected,
            "departure": self.departure,
            "peak_departure": self.peak_departure,
            "session": self.session,
            "operating_mode": self.operating_mode,
        }


@dataclass
class _Run:
    """Debounce state of one relation."""

    started_at: float | None = None
    last_at: float | None = None
    verdicts: int = 0
    peak: float = 0.0
    #: An event already fired for this episode; re-armed only by an ``agree``.
    latched: bool = False

    def clear(self) -> None:
        self.started_at = None
        self.last_at = None
        self.verdicts = 0
        self.peak = 0.0


@dataclass
class _CommitteeRuntime:
    committee: SensorCommittee
    spec: CommitteeSpec | None
    runs: dict[str, _Run] = field(default_factory=dict)
    state: CommitteeState = "no_verdict"
    reason: str | None = "not_configured"
    detail: str | None = None
    relations: list[RelationVerdict] = field(default_factory=list)
    counts: dict[str, int] = field(default_factory=dict)
    events: int = 0
    last_event_at: float | None = None


class CommitteeEvaluator:
    """Checks every committee's relation on each tick; flags sustained departures.

    Fed one row at a time (``observe(point, at)``) — the live daemon's
    operating point each tick, or each row of a stored session's resampled
    frame (:func:`evaluate_committee_frame`), so live, replay and offline run
    the same code. ``point`` values must be engineering units (the daemon
    converts ``decoded:`` counts; :func:`evaluate_committee_frame` does too).

    A relation's verdict needs, in order: the committee configured, every gate
    and tolerance condition holding, the reference and the member present and
    arrived this tick (when the row carries ``<channel>__arrived``). Then
    ``departure`` is compared to the tolerance. A run of departed verdicts
    becomes one :class:`CommitteeFlag` once it spans ``sustain_seconds`` over at
    least ``min_verdicts``; an ``agree`` verdict ends the run and re-arms it;
    a no-verdict tick neither extends nor ends it, but a gap longer than
    ``max_verdict_gap_seconds`` between departed verdicts starts the run again.
    """

    def __init__(
        self,
        profile: CommitteeProfile | None,
        *,
        profile_path: str | None = None,
        session: str | None = None,
        committees: Sequence[SensorCommittee] = COMMITTEES,
    ) -> None:
        self.profile = profile
        self.profile_path = profile_path
        self.session = session
        self.mode: str | None = None
        check_committee_producers(committees)
        specs = profile.committees if profile is not None else {}
        self._runtimes = {
            c.name: _CommitteeRuntime(committee=c, spec=specs.get(c.name)) for c in committees
        }
        self._last_at: float | None = None
        self.ticks = 0
        #: Per channel, what its producer currently says about an absent
        #: value (live: ``LiveImuFrame.lon_mean_absence``; offline: the
        #: session's IMU frame status). Read into a ``*_missing`` verdict's
        #: detail and published in :meth:`snapshot`, so "no value" says which
        #: kind of empty it is. Set by the caller before :meth:`observe`.
        self.producer_status: dict[str, str] = {}

    def reset(self, session: str | None) -> None:
        """A new session (or replay start/stop): no run carries across."""
        self.session = session
        self._last_at = None
        for runtime in self._runtimes.values():
            runtime.runs.clear()
            runtime.state = "no_verdict"
            runtime.reason = "not_configured" if runtime.spec is None else None
            runtime.detail = None
            runtime.relations = []

    def observe(self, point: Mapping[str, Any], at: float) -> list[CommitteeFlag]:
        """Evaluate one row at ``at`` (epoch seconds); return new flags."""
        if self._last_at is not None and at <= self._last_at:
            return []  # the same operating point again (no new tick)
        self._last_at = at
        self.ticks += 1
        flags: list[CommitteeFlag] = []
        for runtime in self._runtimes.values():
            flags.extend(self._observe_committee(runtime, point, at))
        return flags

    def _observe_committee(
        self, runtime: _CommitteeRuntime, point: Mapping[str, Any], at: float
    ) -> list[CommitteeFlag]:
        spec = runtime.spec
        committee = runtime.committee
        if spec is None:
            runtime.state, runtime.reason = "no_verdict", "not_configured"
            runtime.detail = (
                f"no {committee.name} relation in {self.profile_path or COMMITTEES_FILENAME}"
            )
            runtime.relations = []
            _count(runtime, "no_verdict:not_configured")
            return []
        verdicts = [self._relation_verdict(committee, spec, rel, point) for rel in spec.relations]
        runtime.relations = verdicts
        flags: list[CommitteeFlag] = []
        for rel, verdict in zip(spec.relations, verdicts, strict=True):
            flag = self._advance(runtime, spec, rel, verdict, at)
            if flag is not None:
                flags.append(flag)
        decided = [v for v in verdicts if v.state != "no_verdict"]
        if any(v.state == "disagree" for v in decided):
            runtime.state, runtime.reason, runtime.detail = "disagree", None, None
        elif decided:
            runtime.state, runtime.reason, runtime.detail = "agree", None, None
        else:
            first = verdicts[0]
            runtime.state, runtime.reason, runtime.detail = "no_verdict", first.reason, first.detail
        _count(
            runtime,
            runtime.state if runtime.state != "no_verdict" else f"no_verdict:{runtime.reason}",
        )
        runtime.events += len(flags)
        if flags:
            runtime.last_event_at = at
        return flags

    def _relation_verdict(
        self,
        committee: SensorCommittee,
        spec: CommitteeSpec,
        relation: RelationSpec,
        point: Mapping[str, Any],
    ) -> RelationVerdict:
        member, reference = relation.member, spec.reference

        def none(reason: str, detail: str) -> RelationVerdict:
            return RelationVerdict(member, reference, "no_verdict", reason=reason, detail=detail)

        reference_value = _finite(point.get(reference))
        if reference_value is None:
            return none("reference_missing", _no_value(reference, self.producer_status))
        member_value = _finite(point.get(member))
        if member_value is None:
            return none("member_missing", _no_value(member, self.producer_status))
        for channel in (reference, member):
            if not _arrived(point, channel):
                return none("stale", f"{channel} did not arrive this tick")
        for gate in committee.gates:
            failure = gate.failure(point)
            if failure is not None:
                return none("gate", failure)
        for condition in spec.tolerance.conditions:
            failure = condition.gate().failure(point)
            if failure is not None:
                return none("condition", f"{failure} ({condition.reason})")
        expected = relation.ratio * reference_value + relation.offset
        if spec.tolerance.kind == "relative":
            if abs(expected) < 1e-9:
                return none(
                    "condition", f"expected {member} is ~0; a relative departure is undefined"
                )
            departure = (member_value - expected) / expected
        else:
            departure = member_value - expected
        state: CommitteeState = (
            "disagree" if abs(departure) > spec.tolerance.max_departure else "agree"
        )
        return RelationVerdict(
            member,
            reference,
            state,
            member_value=member_value,
            reference_value=reference_value,
            expected=expected,
            departure=departure,
        )

    def _advance(
        self,
        runtime: _CommitteeRuntime,
        spec: CommitteeSpec,
        relation: RelationSpec,
        verdict: RelationVerdict,
        at: float,
    ) -> CommitteeFlag | None:
        run = runtime.runs.setdefault(relation.member, _Run())
        tolerance = spec.tolerance
        if verdict.state == "no_verdict":
            if run.last_at is not None and at - run.last_at > tolerance.max_verdict_gap_seconds:
                run.clear()
            return None
        if verdict.state == "agree":
            run.clear()
            run.latched = False
            return None
        assert verdict.departure is not None
        if run.last_at is not None and at - run.last_at > tolerance.max_verdict_gap_seconds:
            run.clear()
        if run.started_at is None:
            run.started_at = at
        run.last_at = at
        run.verdicts += 1
        run.peak = max(run.peak, abs(verdict.departure))
        if run.latched:
            return None
        if at - run.started_at < tolerance.sustain_seconds or run.verdicts < tolerance.min_verdicts:
            return None
        run.latched = True
        assert verdict.member_value is not None
        assert verdict.reference_value is not None
        assert verdict.expected is not None
        return CommitteeFlag(
            committee=runtime.committee.name,
            quantity=runtime.committee.quantity,
            member=relation.member,
            reference=spec.reference,
            relation=relation,
            tolerance_kind=tolerance.kind,
            max_departure=tolerance.max_departure,
            sustain_seconds=tolerance.sustain_seconds,
            started_at=run.started_at,
            detected_at=at,
            verdicts=run.verdicts,
            member_value=verdict.member_value,
            reference_value=verdict.reference_value,
            expected=verdict.expected,
            departure=verdict.departure,
            peak_departure=run.peak,
            session=self.session,
            operating_mode=self.mode,
        )

    def snapshot(self) -> dict[str, Any]:
        """Per-committee state for ``/status`` (``detectors.inference.committees``)."""
        committees: dict[str, Any] = {}
        for name, runtime in self._runtimes.items():
            entry: dict[str, Any] = {
                "state": runtime.state,
                "reason": runtime.reason,
                "detail": runtime.detail,
                "quantity": runtime.committee.quantity,
                "configured": runtime.spec is not None,
                "alerting": any(run.latched for run in runtime.runs.values()),
                "events": runtime.events,
                "last_event_at": format_epoch(runtime.last_event_at, zulu=True)
                if runtime.last_event_at is not None
                else None,
                "counts": dict(runtime.counts),
            }
            if runtime.spec is not None:
                entry["reference"] = runtime.spec.reference
                entry["tolerance"] = {
                    "kind": runtime.spec.tolerance.kind,
                    "max_departure": runtime.spec.tolerance.max_departure,
                    "sustain_seconds": runtime.spec.tolerance.sustain_seconds,
                }
                entry["relations"] = [v.to_dict() for v in runtime.relations]
            committees[name] = entry
        return {
            "profile_path": self.profile_path,
            "session": self.session,
            "ticks": self.ticks,
            "producer_status": dict(self.producer_status),
            "committees": committees,
        }


@dataclass(frozen=True)
class CommitteeFrameResult:
    """:func:`evaluate_committee_frame`'s answer: flags plus why the rest were silent."""

    flags: list[CommitteeFlag]
    #: Per committee, verdict counts (``agree``, ``disagree``,
    #: ``no_verdict:<reason>``) — so "zero events" says which kind of zero.
    counts: dict[str, dict[str, int]]
    rows: int


def evaluate_committee_frame(
    frame: pd.DataFrame,
    profile: CommitteeProfile | None,
    *,
    bindings: Bindings | None = None,
    session: str | None = None,
    profile_path: str | None = None,
    producer_status: Mapping[str, str] | None = None,
) -> CommitteeFrameResult:
    """Run :class:`CommitteeEvaluator` over a resampled wide frame, row by row.

    The offline twin of the live tick: the frame is the resampler's grid
    (``__arrived`` flags included), member transforms are appended if absent,
    and ``decoded:`` members are converted to engineering units first.
    ``producer_status`` defaults to what the frame itself records: the
    session's IMU frame outcome (``resample_and_segment``'s
    ``attrs["imu_frame"]``), so an absent IMU member says *unresolved* /
    *refused* / *no IMU rows* rather than only "no value".
    """
    work = frame.sort_values("timestamp", kind="stable").reset_index(drop=True)
    if not all(column in work.columns for column in MEMBER_TRANSFORMS):
        work = append_committee_member_transforms(work)
    for column in _decoded_committee_columns(work.columns):
        work[column] = _physical(work[column], column, bindings)
    evaluator = CommitteeEvaluator(profile, profile_path=profile_path, session=session)
    evaluator.producer_status = dict(
        producer_status if producer_status is not None else offline_producer_status(frame, session)
    )
    stamps = pd.to_datetime(work["timestamp"], utc=True)
    flags: list[CommitteeFlag] = []
    mode_column = "operating_mode" if "operating_mode" in work.columns else None
    read = set(committee_channels()) | set(MEMBER_TRANSFORMS)
    if profile is not None:
        read.update(
            condition.channel
            for spec in profile.committees.values()
            for condition in spec.tolerance.conditions
        )
    read.update([f"{channel}__arrived" for channel in read])
    columns = [str(c) for c in work.columns if str(c) in read or c == mode_column]
    records: list[dict[str, Any]] = [
        {str(key): value for key, value in record.items()}
        for record in work[columns].to_dict("records")
    ]
    for index, row in enumerate(records):
        if mode_column is not None:
            mode = row.get(mode_column)
            evaluator.mode = None if mode is None or pd.isna(mode) else str(mode)
        flags.extend(evaluator.observe(row, stamps.iloc[index].timestamp()))
    counts = {name: dict(rt.counts) for name, rt in evaluator._runtimes.items()}
    return CommitteeFrameResult(flags=flags, counts=counts, rows=len(work))


#: ``resample_and_segment``'s per-session IMU frame outcome key
#: (``preprocessing.imu_stream.IMU_FRAME_ATTR``; equal by test — this layer
#: cannot import it).
IMU_FRAME_ATTR = "imu_frame"


def offline_producer_status(frame: pd.DataFrame, session: str | None = None) -> dict[str, str]:
    """What a resampled frame records about its producers: the IMU frame's
    status for ``session`` (or the frame's only session)."""
    outcomes = frame.attrs.get(IMU_FRAME_ATTR)
    if not isinstance(outcomes, Mapping) or not outcomes:
        return {}
    if session is None and len(outcomes) == 1:
        session = next(iter(outcomes))
    outcome = outcomes.get(session) if session is not None else None
    if not isinstance(outcome, Mapping):
        return {}
    status, reason = outcome.get("status"), outcome.get("reason")
    if status == "applied":
        text = "offline IMU frame applied; no IMU sample in this tick"
    else:
        text = f"offline IMU frame {status}: {reason}"
    return {IMU_ACCEL_LON_MEAN: text}


def committee_channels() -> frozenset[str]:
    """Every column any committee reads (members, gates) — for the live converter."""
    channels: set[str] = set()
    for committee in COMMITTEES:
        channels.update(committee.members)
        channels.update(g.channel for g in committee.gates)
    return frozenset(channels)


def physical_committee_point(point: Mapping[str, Any], bindings: Bindings | None) -> dict[str, Any]:
    """``point`` with every ``decoded:`` committee member in engineering units.

    A signal the catalog cannot scale reads as missing, never as raw counts.
    """
    from src.decode.engineering import UnscaledDecodedChannelError, physical_value

    out = dict(point)
    for column in _decoded_committee_columns(point.keys()):
        value = _finite(point.get(column))
        if value is None:
            continue
        try:
            out[column] = physical_value(value, column, bindings)
        except (UnscaledDecodedChannelError, KeyError, ValueError):
            out[column] = None
    return out


# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------


def _decoded_committee_columns(columns: Iterable[Any]) -> list[str]:
    wanted = {c for c in committee_channels() if c.startswith("decoded:")}
    return [str(c) for c in columns if str(c) in wanted]


def _physical(values: pd.Series, column: str, bindings: Bindings | None = None) -> pd.Series:
    if not column.startswith("decoded:"):
        return pd.to_numeric(values, errors="coerce").astype(float)
    from src.decode.engineering import physical_series

    return physical_series(values, column, bindings)


def _no_value(channel: str, status: Mapping[str, str] | None = None) -> str:
    """``"<channel> has no value"``, with why: the producer's reported status
    when the caller passed one, else the declared ``absent`` text."""
    reported = (status or {}).get(channel)
    producer = CHANNEL_PRODUCERS.get(channel)
    cause = reported or (producer.absent if producer is not None else "")
    return f"{channel} has no value ({cause})" if cause else f"{channel} has no value"


def _finite(value: Any) -> float | None:
    if value is None or isinstance(value, bool):
        return None
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    return number if math.isfinite(number) else None


def _arrived(point: Mapping[str, Any], channel: str) -> bool:
    """``False`` only when the row carries ``<channel>__arrived`` and it is false.

    A row without the flag (a ``derived:`` member, a synthetic frame) has no
    arrival information; its value is taken as current.
    """
    flag = point.get(f"{channel}__arrived")
    if flag is None:
        return True
    try:
        if pd.isna(flag):
            return True
    except (TypeError, ValueError):
        pass
    return bool(flag)


def _count(runtime: _CommitteeRuntime, key: str) -> None:
    runtime.counts[key] = runtime.counts.get(key, 0) + 1


__all__ = [
    "ACCEL_SPAN_SECONDS",
    "COMMITTEES",
    "COMMITTEES_FILENAME",
    "GPS_SPEED_ACCEL_G",
    "MEMBER_TRANSFORMS",
    "NO_VERDICT_REASONS",
    "OBD_SPEED_ACCEL_G",
    "SPEED_COMMITTEE",
    "CHANNEL_PRODUCERS",
    "IMU_ACCEL_LON_MEAN",
    "ChannelProducer",
    "CommitteeDeclarationError",
    "CommitteeEvaluator",
    "CommitteeFlag",
    "CommitteeFrameResult",
    "CommitteeProfile",
    "MemberGate",
    "SensorCommittee",
    "append_committee_member_transforms",
    "append_committee_residuals",
    "check_committee_producers",
    "committee_channel_reason",
    "committees_path",
    "evaluate_committee_frame",
    "load_committee_profile",
    "non_trainable_committee_outputs",
    "offline_producer_status",
    "physical_committee_point",
    "resolve_committee_profile",
    "speed_accel_g",
    "undelivered_reason",
]
