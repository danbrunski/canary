"""Per-session IMU frame resolution and its sidecar (H02.4).

The offline caller of :mod:`src.preprocessing.imu_frame`. For one session it
turns raw body-frame ``imu:ACCEL_X/Y/Z`` (g) into two families, on the IMU's
own sample instants:

* ``imu:ACCEL_LON`` / ``imu:ACCEL_LAT`` — *linear* acceleration, gravity
  removed by the quiescent low-pass (:func:`~src.preprocessing.imu_frame.
  resolve_frame`). What the auto-tagger and the segmenter's accel modes read,
  through ``imu_decimation``'s per-tick MAX/RMS.
* ``imu:SF_LON`` / ``imu:SF_LAT`` / ``imu:SF_VERT`` — *specific force* rotated
  into the vehicle frame with gravity **kept** (:func:`~src.preprocessing.
  imu_frame.resolve_specific_force`): ``SF_LON = a + g·sin θ``, the dyno's
  input (H07). The low-pass removes the grade with gravity, which is why this
  family does not come from it.

Axes are ISO 8855 (``LON`` forward, ``LAT`` left, ``VERT`` up), units g.

**The pitch reference is recorded, not corrected.** ``SF_*`` use one
constant rotation per session whose "up" is the mean specific force over the
session's quiescent rows (steady speed and heading). Whatever grade the
vehicle sat on there stays in ``SF_LON`` as a constant pitch offset
``μ ≈ −θr``. The sidecar records which windows formed the reference, the
body-frame gravity vector each saw, the rotation and a ``reference_id``, and
states ``pitch_offset_removed: false``. The dyno measures ``μ`` from a
direction pair in the same session (``src.analysis.dyno.
estimate_pitch_offset``) and removes it (``correct_specific_force``); nothing
here subtracts one, so there is exactly one correction and it is the dyno's.
``SF_*`` are recomputed from the recorded rotation
(:func:`specific_force_from_sidecar`), and :func:`write_imu_frame_sidecar`
refuses to replace a resolved frame with a different one unless asked, so a
pitch offset measured against this reference cannot silently meet ``SF_*``
from another.

**Readers get ``ACCEL_LON/LAT`` from the sidecar, part by part.** The
``value_number`` chain (``loader.resample_and_segment``: the digest,
classification, ``scripts/autotag.py``) rebuilds the linear family from the
recorded yaw, sign and low-pass initial state
(``LinearAccelFrame.initial_gravity_body_g``) with
``src.preprocessing.imu_stream``, bounded by part size, and equal to this
module's frame (``tests/test_imu_stream.py``). A session with raw IMU rows
and no sidecar gets none, and the chain names it ``unresolved``.

**Refusals name their cause** (:class:`ImuFrameRefusal`): no telemetry, IMU
rows missing, no speed source, no heading, no quiescent period, a reference
that is not gravity, an unresolved yaw or sign. A refusal is written to the
sidecar too, so "never resolved" (no file) and "resolved to nothing" differ.

**Speed** is the best the session has (``imu_frame.SPEED_SOURCES``): the
``0x2D1`` broadcast, then Mode 01, then GPS. The ``tuning`` capture profile
does not poll ``obd:SPEED``.
"""

from __future__ import annotations

import hashlib
import json
import math
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Literal

import numpy as np
import pandas as pd
from pydantic import BaseModel, ConfigDict, model_validator

from src.atomic_write import atomic_write_text
from src.decode.bindings import Bindings
from src.decode.engineering import is_decoded_column, repo_bindings
from src.preprocessing.imu_decimation import ACCEL_LAT_COLUMN, ACCEL_LON_COLUMN
from src.preprocessing.imu_frame import (
    GRAVITY_MAGNITUDE_RANGE_G,
    HEADING_COLUMN,
    QUIESCENT_LOOKAHEAD_SECONDS,
    SPEED_SOURCES,
    apply_rotation,
    longitudinal_sign_votes,
    quiescent_mask,
    resolve_frame,
    resolve_specific_force,
    select_speed,
    straight_mask,
)
from src.storage.sidecar import read_json_object
from src.telemetry import CAN_SOURCE, IMU_SOURCE, SOURCE_SEP, channel_key

IMU_FRAME_FILENAME = "imu_frame.json"
IMU_FRAME_SCHEMA_VERSION = 1
#: Bump when the resolution changes for unchanged inputs.
IMU_FRAME_CODE_VERSION = 1

ACCEL_X_COLUMN = channel_key(IMU_SOURCE, "ACCEL_X")
ACCEL_Y_COLUMN = channel_key(IMU_SOURCE, "ACCEL_Y")
ACCEL_Z_COLUMN = channel_key(IMU_SOURCE, "ACCEL_Z")
RAW_ACCEL_COLUMNS = (ACCEL_X_COLUMN, ACCEL_Y_COLUMN, ACCEL_Z_COLUMN)
SF_LON_COLUMN = channel_key(IMU_SOURCE, "SF_LON")
SF_LAT_COLUMN = channel_key(IMU_SOURCE, "SF_LAT")
SF_VERT_COLUMN = channel_key(IMU_SOURCE, "SF_VERT")
SPECIFIC_FORCE_COLUMNS = (SF_LON_COLUMN, SF_LAT_COLUMN, SF_VERT_COLUMN)
LINEAR_ACCEL_COLUMNS = (ACCEL_LON_COLUMN, ACCEL_LAT_COLUMN)
FRAME_COLUMNS = (*LINEAR_ACCEL_COLUMNS, *SPECIFIC_FORCE_COLUMNS)
FRAME_UNIT = "g"

#: Quiescence is "steady for this long" (speed std ≤ 1.5 km/h, course std ≤
#: 3 deg: ``imu_frame.quiescent_mask``'s thresholds), converted to samples at
#: the IMU's measured rate: a fixed row count means 0.1 s at 208 Hz.
DEFAULT_QUIESCENT_SECONDS = 2.0
#: A speed or heading older than this at an IMU sample is missing there, not
#: carried: GPS is 1 Hz and Mode 01 speed ~0.7 Hz.
INPUT_MAX_AGE_SECONDS = 3.0
#: Speeds at or below this count as stationary in the reference's record.
STATIONARY_KPH = 1.0

AXES = "ISO 8855: LON forward, LAT left, VERT up (LAT = VERT x LON)"

#: What ``SF_LON`` still carries, stated in every resolved sidecar.
RESIDUAL_PITCH_OFFSET = (
    "SF_LON = a*cos(thr) + sin(theta - thr) in g, for road grade theta and the mean "
    "vehicle pitch thr over the quiescent windows below: a constant offset mu ~ -thr "
    "(plus accelerometer bias) that this frame does NOT remove. Measure it with "
    "src.analysis.dyno.estimate_pitch_offset on a direction pair from this session "
    "(MotionSamples.imu_reference_id = this reference_id) and pass the PitchOffset to "
    "correct_specific_force, which refuses samples from any other reference_id "
    "(frame_mismatch)."
)

RefusalKind = Literal[
    "no_telemetry",
    "imu_rows_missing",
    "no_speed_source",
    "no_heading",
    "no_quiescent_period",
    "gravity_implausible",
    "yaw_unresolved",
    "sign_unresolved",
]


@dataclass(frozen=True)
class ImuFrameRefusal:
    """Why a session's IMU frame did not resolve. ``kind`` is stable."""

    kind: RefusalKind
    reason: str

    def as_dict(self) -> dict[str, str]:
        return {"kind": self.kind, "reason": self.reason}


class ImuFrameOfRecordError(ValueError):
    """Writing would replace a session's resolved frame with a different one.

    A pitch offset measured against the old reference would then meet
    ``SF_*`` from the new one. Pass ``replace=True`` when that is the intent.
    """


class _Strict(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


Vector3 = tuple[float, float, float]


class QuiescentWindow(_Strict):
    """One contiguous quiescent run that fed the specific-force reference."""

    start: str
    end: str
    samples: int
    mean_speed_kph: float
    #: Mean specific force over the run, body frame (g).
    gravity_body_g: Vector3
    #: Angle between this run's "up" and the session reference. Differences
    #: here are mostly the grades the runs sat on.
    angle_from_reference_deg: float


class PitchReference(_Strict):
    """The orientation ``SF_*`` are resolved against, and what it leaves in."""

    method: Literal["mean_quiescent_specific_force"] = "mean_quiescent_specific_force"
    #: Mean specific force over every quiescent sample, body frame (g).
    gravity_body_g: Vector3
    gravity_magnitude_g: float
    #: Angle between the reference "up" and the chip's +Z axis (mount tilt as
    #: seen from the chip; includes the reference's mean grade).
    tilt_from_body_z_deg: float
    quiescent_samples: int
    quiescent_seconds: float
    stationary_fraction: float
    windows: list[QuiescentWindow]
    window_angle_spread_deg: float
    #: Always false: the frame never subtracts a pitch estimate.
    pitch_offset_removed: Literal[False] = False
    residual_pitch_offset: str = RESIDUAL_PITCH_OFFSET


class SpecificForceFrame(_Strict):
    channels: tuple[str, str, str] = SPECIFIC_FORCE_COLUMNS
    #: Hash of the rotation: the identity a dyno pitch offset is valid for.
    reference_id: str
    #: Rows LON, LAT, VERT as unit vectors in the chip's body frame.
    rotation_body_to_vehicle: tuple[Vector3, Vector3, Vector3]
    yaw_offset_rad: float
    sign_flipped: bool
    #: ``(agree, disagree)`` rows of ``LON`` against d(speed)/dt.
    sign_votes: tuple[int, int]
    pitch_reference: PitchReference


class LinearAccelFrame(_Strict):
    channels: tuple[str, str] = LINEAR_ACCEL_COLUMNS
    method: Literal["quiescent_low_pass"] = "quiescent_low_pass"
    yaw_offset_rad: float
    sign_flipped: bool
    sign_votes: tuple[int, int]
    #: The low-pass's first state: the raw sample at the first quiescent
    #: instant, which every earlier sample subtracts as gravity
    #: (``imu_frame.estimate_gravity`` back-fills it). With the yaw and sign
    #: above it is all the offline stream (``imu_stream``) needs to rebuild
    #: ``ACCEL_LON/LAT`` part by part; the rest of the low-pass is recomputed
    #: from the rows. ``None`` in a sidecar written before 2026-09-26: the
    #: stream then names it ``stale`` and does not apply it.
    first_quiescent_sample: str | None = None
    initial_gravity_body_g: Vector3 | None = None


class ImuFrameInputs(_Strict):
    imu_samples: int
    imu_rate_hz: float
    first_sample: str
    last_sample: str
    speed_source: str | None
    heading_source: str | None
    quiescent_seconds: float
    quiescent_window_samples: int
    #: Each sample's steadiness is judged this far past it too
    #: (``imu_frame.QUIESCENT_LOOKAHEAD_SECONDS``).
    quiescent_lookahead_seconds: float
    quiescent_lookahead_samples: int


class RefusalRecord(_Strict):
    kind: RefusalKind
    reason: str


class ImuFrameSidecar(_Strict):
    """``imu_frame.json``: one session's frame of record, or why there is none."""

    schema_version: Literal[1] = 1
    code_version: int = IMU_FRAME_CODE_VERSION
    status: Literal["resolved", "refused"]
    resolved_at: str
    axes: str = AXES
    units: Literal["g"] = "g"
    inputs: ImuFrameInputs | None = None
    refusal: RefusalRecord | None = None
    specific_force: SpecificForceFrame | None = None
    linear_accel: LinearAccelFrame | None = None

    @model_validator(mode="after")
    def _status_matches_payload(self) -> ImuFrameSidecar:
        if self.status == "resolved":
            missing = [
                name
                for name in ("inputs", "specific_force", "linear_accel")
                if getattr(self, name) is None
            ]
            if missing or self.refusal is not None:
                raise ValueError(
                    f"a resolved imu frame needs inputs, specific_force and linear_accel "
                    f"and no refusal (missing {missing})"
                )
        elif self.refusal is None or self.specific_force is not None:
            raise ValueError("a refused imu frame carries its refusal and no frame")
        return self


@dataclass(frozen=True)
class ImuSessionFrame:
    """Both families on the IMU's sample instants, plus the sidecar that made them."""

    #: ``timestamp`` plus :data:`FRAME_COLUMNS`, g.
    frame: pd.DataFrame
    sidecar: ImuFrameSidecar

    def rows(self, session: str) -> pd.DataFrame:
        """The families as long telemetry rows (``source`` ``imu``, unit g), in
        :func:`src.preprocessing.loader.read_session`'s core columns: the
        whole-session reference the part-by-part rebuild
        (``src.preprocessing.imu_stream``) is checked against. Readers of a
        stored session go through that rebuild, which never holds the session."""
        melted = self.frame.melt(
            id_vars=["timestamp"],
            value_vars=list(FRAME_COLUMNS),
            var_name="source_channel",
            value_name="value_number",
        ).dropna(subset=["value_number"])
        melted["source"] = IMU_SOURCE
        melted["channel"] = melted["source_channel"].str.split(SOURCE_SEP, n=1).str[1]
        melted["unit"] = FRAME_UNIT
        melted["value"] = melted["value_number"].astype("object")
        melted["session"] = session
        columns = ["timestamp", "source", "channel", "value_number", "unit"]
        columns += ["session", "source_channel", "value"]
        return (
            melted[columns]
            .sort_values(["timestamp", "channel"], kind="stable")
            .reset_index(drop=True)
        )


# ---------------------------------------------------------------------------
# Inputs
# ---------------------------------------------------------------------------


def _read_filters() -> list[list[tuple[str, str, Any]]]:
    """pyarrow DNF filters for exactly the rows the resolution reads."""
    wanted: dict[str, list[str]] = {}
    for column in (*RAW_ACCEL_COLUMNS, *SPEED_SOURCES, HEADING_COLUMN):
        source, channel = column.split(SOURCE_SEP, 1)
        wanted.setdefault(source, []).append(channel)
    return [
        [("source", "==", source), ("channel", "in", names)] for source, names in wanted.items()
    ]


def channels_from_long(long: pd.DataFrame) -> dict[str, pd.Series]:
    """Per-``source:channel`` numeric series indexed by UTC timestamp, from
    long rows (:func:`~src.preprocessing.loader.read_session`'s shape)."""
    if long.empty:
        return {}
    numbers = pd.to_numeric(long["value_number"], errors="coerce")
    timestamps = pd.to_datetime(long["timestamp"], utc=True)
    out: dict[str, pd.Series] = {}
    for column, index in long.groupby("source_channel", sort=False).groups.items():
        series = pd.Series(numbers.loc[index].to_numpy(), index=timestamps.loc[index].to_numpy())
        out[str(column)] = _clean(series)
    return out


def channels_from_wide(
    wide: pd.DataFrame, *, timestamp_column: str = "timestamp"
) -> dict[str, pd.Series]:
    """Per-column numeric series indexed by UTC timestamp, from a wide frame
    (NaN where a channel was not observed)."""
    index = pd.DatetimeIndex(pd.to_datetime(wide[timestamp_column], utc=True))
    out: dict[str, pd.Series] = {}
    for column in wide.columns:
        if column == timestamp_column:
            continue
        values = pd.to_numeric(wide[column], errors="coerce").to_numpy()
        out[str(column)] = _clean(pd.Series(values, index=index))
    return out


def _clean(series: pd.Series) -> pd.Series:
    series = series.dropna()
    series.index = pd.DatetimeIndex(pd.to_datetime(series.index, utc=True))
    series = series[~series.index.duplicated(keep="last")]
    return series.sort_index(kind="stable").astype(float)


def _aligned(series: pd.Series, base: pd.DatetimeIndex) -> pd.Series:
    """``series`` sampled at ``base``: the newest value no older than
    :data:`INPUT_MAX_AGE_SECONDS`, else NaN."""
    left = pd.DataFrame({"timestamp": base})
    right = pd.DataFrame({"timestamp": series.index, "value": series.to_numpy()})
    merged = pd.merge_asof(
        left,
        right,
        on="timestamp",
        direction="backward",
        tolerance=pd.Timedelta(seconds=INPUT_MAX_AGE_SECONDS),
    )
    return pd.Series(merged["value"].to_numpy(dtype=float), index=base)


def _iso(ts: pd.Timestamp) -> str:
    return pd.Timestamp(ts).isoformat()


def _runs(mask: np.ndarray) -> list[tuple[int, int]]:
    """``[start, stop)`` index pairs of contiguous ``True`` runs."""
    padded = np.concatenate([[False], mask, [False]]).astype(np.int8)
    edges = np.flatnonzero(np.diff(padded))
    return [(int(a), int(b)) for a, b in zip(edges[::2], edges[1::2], strict=True)]


def _angle_deg(a: np.ndarray, b: np.ndarray) -> float:
    cosine = float(a @ b) / (float(np.linalg.norm(a)) * float(np.linalg.norm(b)))
    return math.degrees(math.acos(max(-1.0, min(1.0, cosine))))


def _vector(values: np.ndarray) -> Vector3:
    return (float(values[0]), float(values[1]), float(values[2]))


def reference_id(rotation: np.ndarray) -> str:
    """Stable identity of a specific-force rotation (16 hex chars)."""
    payload = json.dumps([[repr(float(v)) for v in row] for row in np.asarray(rotation)])
    return hashlib.sha256(payload.encode()).hexdigest()[:16]


# ---------------------------------------------------------------------------
# Resolution
# ---------------------------------------------------------------------------


def resolve_imu_frame(
    channels: Mapping[str, pd.Series],
    *,
    bindings: Bindings | None = None,
    quiescent_seconds: float = DEFAULT_QUIESCENT_SECONDS,
    speed_source: str | None = None,
    now: datetime | None = None,
) -> ImuSessionFrame | ImuFrameRefusal:
    """Resolve one session's IMU frame from its channels (no I/O).

    ``channels`` maps ``source:channel`` to a numeric series indexed by UTC
    timestamp (:func:`channels_from_long` / :func:`channels_from_wide`). Needs
    ``imu:ACCEL_X/Y/Z`` (g), one of ``imu_frame.SPEED_SOURCES`` and
    ``gps:heading``. Returns both families or the first refusal met, in this
    order: IMU rows, speed, heading, quiescent period, gravity, yaw, sign.

    ``speed_source`` pins the speed to that one column (a frame of record
    being re-resolved, :func:`resolve_session_imu_frame`); ``None`` takes the
    best of ``SPEED_SOURCES``.
    """
    missing_axes = [c for c in RAW_ACCEL_COLUMNS if c not in channels or channels[c].empty]
    if missing_axes:
        return ImuFrameRefusal(
            "imu_rows_missing",
            f"no {', '.join(missing_axes)} rows: the session recorded no raw accelerometer "
            "(hardware.imu disabled, or recorded before the HAT)",
        )
    accel = pd.concat(
        {axis: channels[c] for axis, c in zip("xyz", RAW_ACCEL_COLUMNS, strict=True)},
        axis=1,
        join="inner",
    ).dropna()
    if len(accel) < 2:
        return ImuFrameRefusal(
            "imu_rows_missing",
            f"{len(accel)} instant(s) carry all three accelerometer axes; "
            "frame resolution needs a series",
        )
    base = pd.DatetimeIndex(accel.index)
    seconds = np.diff(base.to_numpy(dtype="datetime64[ns]").astype(np.int64)) / 1e9
    rate_hz = 1.0 / float(np.median(seconds[seconds > 0])) if np.any(seconds > 0) else 0.0
    window = max(2, round(quiescent_seconds * rate_hz))
    lookahead = max(1, round(QUIESCENT_LOOKAHEAD_SECONDS * rate_hz))

    candidates = SPEED_SOURCES if speed_source is None else (speed_source,)
    speed_choice = None
    for column in candidates:
        if column in channels:
            speed_choice = select_speed(pd.DataFrame({column: channels[column]}), bindings=bindings)
            if speed_choice is not None:
                break
    if speed_choice is None:
        return ImuFrameRefusal(
            "no_speed_source",
            f"none of {', '.join(candidates)} has a value (a decoded speed also needs a "
            "pinned scale): quiescence and the longitudinal sign both need a speed",
        )
    speed_series, speed_source = speed_choice
    speed = _aligned(speed_series, base)

    heading_series = channels.get(HEADING_COLUMN)
    if heading_series is None or heading_series.empty:
        return ImuFrameRefusal(
            "no_heading",
            f"no {HEADING_COLUMN}: the mount's yaw comes from GPS course on straight "
            "stretches (imu-gps-integration.md, a recorded decision), and nothing else",
        )
    heading = _aligned(heading_series, base)

    quiescent = quiescent_mask(speed, heading, window=window, lookahead=lookahead)
    quiescent_values = quiescent.to_numpy(dtype=bool)
    if not quiescent_values.any():
        return ImuFrameRefusal(
            "no_quiescent_period",
            f"no {quiescent_seconds:g} s stretch (+{QUIESCENT_LOOKAHEAD_SECONDS:g} s "
            f"look-ahead) with steady speed (std <= 1.5 km/h, from "
            f"{speed_source}) and steady course (std <= 3 deg): no gravity reference",
        )

    straight = straight_mask(heading, window=window, lookahead=lookahead)
    timestamps = pd.Series(base, index=base)
    sf = resolve_specific_force(accel, quiescent, straight, speed, timestamps)
    magnitude = float(np.linalg.norm(sf.gravity_body_g))
    low, high = GRAVITY_MAGNITUDE_RANGE_G
    if not low <= magnitude <= high:
        return ImuFrameRefusal(
            "gravity_implausible",
            f"mean specific force over quiescent rows is {magnitude:.3f} g, outside "
            f"{low}-{high} g: the rows are not raw accelerometer output in g (fused / "
            "gravity-removed, another unit, or a failing part)",
        )
    if sf.yaw_offset_rad is None:
        return ImuFrameRefusal(
            "yaw_unresolved",
            "no straight stretch (steady GPS course) with >= 0.05 g of horizontal "
            "acceleration: the mount's yaw is unobservable until the vehicle accelerates "
            "or brakes in a straight line",
        )
    if sf.rotation is None:
        return ImuFrameRefusal(
            "sign_unresolved",
            f"{speed_source} never changed while the IMU saw longitudinal acceleration: "
            "no evidence which end of the axis is forward",
        )

    wide = pd.DataFrame(
        {
            "timestamp": base,
            ACCEL_X_COLUMN: accel["x"].to_numpy(),
            ACCEL_Y_COLUMN: accel["y"].to_numpy(),
            ACCEL_Z_COLUMN: accel["z"].to_numpy(),
            "speed_kph": speed.to_numpy(),
            HEADING_COLUMN: heading.to_numpy(),
        }
    )
    linear = resolve_frame(
        wide, speed_column="speed_kph", quiescent_window=window, quiescent_lookahead=lookahead
    )
    lin_timestamps = wide["timestamp"]
    if linear.yaw_offset_rad is None:
        return ImuFrameRefusal(
            "yaw_unresolved",
            "the low-pass frame found no straight stretch with >= 0.05 g of horizontal "
            "acceleration about its running gravity estimate",
        )
    lin_votes = longitudinal_sign_votes(linear.linear_accel_lon, wide["speed_kph"], lin_timestamps)
    if lin_votes == (0, 0):
        return ImuFrameRefusal(
            "sign_unresolved",
            f"{speed_source} never changed while ACCEL_LON was non-zero: no evidence "
            "which end of the axis is forward",
        )

    rotated = apply_rotation(accel[["x", "y", "z"]].to_numpy(), sf.rotation)
    frame = pd.DataFrame(
        {
            "timestamp": base,
            ACCEL_LON_COLUMN: linear.linear_accel_lon.to_numpy(dtype=float),
            ACCEL_LAT_COLUMN: linear.linear_accel_lat.to_numpy(dtype=float),
            SF_LON_COLUMN: rotated[:, 0],
            SF_LAT_COLUMN: rotated[:, 1],
            SF_VERT_COLUMN: rotated[:, 2],
        }
    )

    accel_values = accel[["x", "y", "z"]].to_numpy(dtype=np.float64)
    speed_values = speed.to_numpy(dtype=float)
    windows: list[QuiescentWindow] = []
    for start, stop in _runs(quiescent_values):
        mean_g = accel_values[start:stop].mean(axis=0)
        windows.append(
            QuiescentWindow(
                start=_iso(base[start]),
                end=_iso(base[stop - 1]),
                samples=stop - start,
                mean_speed_kph=float(np.nanmean(speed_values[start:stop])),
                gravity_body_g=_vector(mean_g),
                angle_from_reference_deg=_angle_deg(mean_g, sf.gravity_body_g),
            )
        )
    quiescent_speed = speed_values[quiescent_values]
    # The linear family's low-pass starts here (resolve_frame computes the
    # same mask from the same inputs; the stream checks it against the rows).
    first_quiescent = int(np.flatnonzero(quiescent_values)[0])
    reference = PitchReference(
        gravity_body_g=_vector(sf.gravity_body_g),
        gravity_magnitude_g=magnitude,
        tilt_from_body_z_deg=_angle_deg(sf.gravity_body_g, np.array([0.0, 0.0, 1.0])),
        quiescent_samples=int(quiescent_values.sum()),
        quiescent_seconds=float(quiescent_values.sum() / rate_hz) if rate_hz else 0.0,
        stationary_fraction=float(np.mean(quiescent_speed <= STATIONARY_KPH)),
        windows=windows,
        window_angle_spread_deg=max(w.angle_from_reference_deg for w in windows),
    )
    rotation = sf.rotation
    sidecar = ImuFrameSidecar(
        status="resolved",
        resolved_at=(now or datetime.now(UTC)).isoformat(),
        inputs=ImuFrameInputs(
            imu_samples=len(base),
            imu_rate_hz=rate_hz,
            first_sample=_iso(base[0]),
            last_sample=_iso(base[-1]),
            speed_source=speed_source,
            heading_source=HEADING_COLUMN,
            quiescent_seconds=quiescent_seconds,
            quiescent_window_samples=window,
            quiescent_lookahead_seconds=QUIESCENT_LOOKAHEAD_SECONDS,
            quiescent_lookahead_samples=lookahead,
        ),
        specific_force=SpecificForceFrame(
            reference_id=reference_id(rotation),
            rotation_body_to_vehicle=(
                _vector(rotation[0]),
                _vector(rotation[1]),
                _vector(rotation[2]),
            ),
            yaw_offset_rad=sf.yaw_offset_rad,
            sign_flipped=sf.sign_flipped,
            sign_votes=sf.sign_votes,
            pitch_reference=reference,
        ),
        linear_accel=LinearAccelFrame(
            yaw_offset_rad=linear.yaw_offset_rad,
            sign_flipped=linear.sign_flipped,
            sign_votes=lin_votes,
            first_quiescent_sample=_iso(base[first_quiescent]),
            initial_gravity_body_g=_vector(accel_values[first_quiescent]),
        ),
    )
    return ImuSessionFrame(frame=frame, sidecar=sidecar)


def refused_sidecar(refusal: ImuFrameRefusal, *, now: datetime | None = None) -> ImuFrameSidecar:
    """The sidecar recording ``refusal``."""
    return ImuFrameSidecar(
        status="refused",
        resolved_at=(now or datetime.now(UTC)).isoformat(),
        refusal=RefusalRecord(kind=refusal.kind, reason=refusal.reason),
    )


def specific_force_from_sidecar(accel: pd.DataFrame, sidecar: ImuFrameSidecar) -> pd.DataFrame:
    """``imu:SF_*`` for raw ``imu:ACCEL_X/Y/Z`` rows, through the recorded rotation.

    The way to get specific force for a session once it has a sidecar: the
    rotation is the frame of record, so every reader (H07.3) sees the same
    reference a dyno pitch offset was measured against. Refuses a refused
    sidecar, naming its cause.
    """
    if sidecar.specific_force is None:
        refusal = sidecar.refusal
        cause = f"{refusal.kind}: {refusal.reason}" if refusal is not None else "unresolved"
        raise ValueError(f"the session's imu frame was refused ({cause})")
    missing = [c for c in RAW_ACCEL_COLUMNS if c not in accel.columns]
    if missing:
        raise ValueError(f"specific force needs {', '.join(missing)}")
    rotation = np.asarray(sidecar.specific_force.rotation_body_to_vehicle, dtype=np.float64)
    values = accel[list(RAW_ACCEL_COLUMNS)].apply(pd.to_numeric, errors="coerce").to_numpy()
    rotated = apply_rotation(values, rotation)
    return pd.DataFrame(
        {name: rotated[:, i] for i, name in enumerate(SPECIFIC_FORCE_COLUMNS)},
        index=accel.index,
    )


# ---------------------------------------------------------------------------
# Session I/O
# ---------------------------------------------------------------------------


def read_imu_frame_sidecar(session_dir: Path) -> ImuFrameSidecar | None:
    """The session's ``imu_frame.json``, or ``None`` when it was never resolved.

    A present but malformed file raises (``pydantic.ValidationError``) rather
    than reading as absent: a frame of record that cannot be read is not the
    same as none.
    """
    data = read_json_object(Path(session_dir) / IMU_FRAME_FILENAME)
    if data is None:
        return None
    return ImuFrameSidecar.model_validate(data)


def write_imu_frame_sidecar(
    session_dir: Path, sidecar: ImuFrameSidecar, *, replace: bool = False
) -> Path:
    """Write ``imu_frame.json`` atomically.

    Refuses (:class:`ImuFrameOfRecordError`) to replace a *resolved* frame with
    a different reference, or with a refusal, unless ``replace``. Rewriting the
    same reference, or replacing a refusal, needs nothing.
    """
    path = Path(session_dir) / IMU_FRAME_FILENAME
    existing = read_imu_frame_sidecar(session_dir) if path.exists() else None
    if not replace and existing is not None and existing.specific_force is not None:
        old = existing.specific_force.reference_id
        new = sidecar.specific_force.reference_id if sidecar.specific_force else None
        if new != old:
            what = f"reference {new}" if new else f"a refusal ({sidecar.refusal})"
            raise ImuFrameOfRecordError(
                f"{path} holds resolved reference {old}; writing {what} would change the "
                "frame any dyno pitch offset for this session was measured against. "
                "Pass replace=True if that is intended."
            )
    atomic_write_text(path, json.dumps(sidecar.model_dump(mode="json"), indent=2) + "\n")
    return path


def speed_decode_catalog(bindings: Bindings | None = None) -> Bindings | None:
    """The catalog narrowed to the ``decoded:`` speed sources' signals
    (``imu_frame.SPEED_SOURCES``), whatever their confidence; ``None`` when it
    has none of them.

    The loader decodes only ``confirmed`` or ``persist: true`` bindings on
    read (P02.8, ``decode.on_read_catalog``), and ``VEHICLE_SPEED_2D1`` is
    ``high``: read through it, a session has no ``0x2D1`` speed at all and the
    resolver silently steered by Mode 01 or GPS. Every IMU reader decodes
    these signals from the raw frames itself (:func:`read_imu_inputs`, and
    ``imu_stream`` on the ``value_number`` chain), as the driving dyno does
    (``analysis.dyno_inputs.read_motion_inputs``).
    """
    catalog = bindings if bindings is not None else repo_bindings()
    if catalog is None:
        return None
    names = {c.split(SOURCE_SEP, 1)[1] for c in SPEED_SOURCES if is_decoded_column(c)}
    signals = [signal for signal in catalog.signals if signal.name in names]
    return catalog.model_copy(update={"signals": signals}) if signals else None


def read_imu_inputs(session_dir: Path, *, bindings: Bindings | None = None) -> dict[str, pd.Series]:
    """The channels :func:`resolve_imu_frame` reads, from a session directory.

    One filtered :func:`~src.preprocessing.loader.read_session` of the IMU,
    speed and course rows plus the raw frames of the ``decoded:`` speed
    sources, which are decoded here (:func:`speed_decode_catalog`): any stored
    ``decoded`` row is dropped and the frames decoded with the current catalog,
    exactly as a decode on read would. Raises ``FileNotFoundError`` without
    telemetry.
    """
    from src.preprocessing.decode import can_channel_spellings, decode_on_read, on_read_can_ids
    from src.preprocessing.loader import read_session

    catalog = speed_decode_catalog(bindings)
    filters = _read_filters()
    if catalog is not None:
        channels = can_channel_spellings(on_read_can_ids(catalog))
        filters.append([("source", "==", CAN_SOURCE), ("channel", "in", channels)])
    long = read_session(Path(session_dir), filters=filters, decoded="as_recorded")
    long = decode_on_read(long, catalog)
    long = long.loc[long["source"] != CAN_SOURCE].copy()
    long["source_channel"] = long["source"].astype(str) + SOURCE_SEP + long["channel"].astype(str)
    return channels_from_long(long)


def resolve_session_imu_frame(
    session_dir: Path,
    *,
    bindings: Bindings | None = None,
    quiescent_seconds: float = DEFAULT_QUIESCENT_SECONDS,
    write: bool = True,
    replace: bool = False,
    now: datetime | None = None,
) -> ImuSessionFrame | ImuFrameRefusal:
    """Resolve one session and (by default) record the result in its sidecar.

    The production offline caller (``scripts/resolve_imu_frame.py``). A
    refusal is written too. See :func:`write_imu_frame_sidecar` for when an
    existing sidecar blocks the write.

    **A resolved frame of record keeps its speed source** unless ``replace``:
    the speed decides the quiescent set, and so the reference. Sidecars
    written before 2026-09-26 steered by ``obd:SPEED`` or GPS even where the
    session recorded ``0x2D1``, which :func:`read_imu_inputs` could not decode
    then. Re-resolving one pins that source, so it only backfills the
    low-pass's initial state (``imu_stream`` reads it ``stale`` until then)
    with the same ``reference_id``. A session with no sidecar, or with
    ``replace=True``, takes the best source.
    """
    existing = None if replace else read_imu_frame_sidecar(session_dir)
    pinned = (
        existing.inputs.speed_source
        if existing is not None and existing.status == "resolved" and existing.inputs is not None
        else None
    )
    try:
        channels = read_imu_inputs(session_dir, bindings=bindings)
    except FileNotFoundError as exc:
        result: ImuSessionFrame | ImuFrameRefusal = ImuFrameRefusal("no_telemetry", str(exc))
    else:
        result = resolve_imu_frame(
            channels,
            bindings=bindings,
            quiescent_seconds=quiescent_seconds,
            speed_source=pinned,
            now=now,
        )
    if write:
        sidecar = (
            result.sidecar
            if isinstance(result, ImuSessionFrame)
            else refused_sidecar(result, now=now)
        )
        write_imu_frame_sidecar(session_dir, sidecar, replace=replace)
    return result


__all__ = [
    "ACCEL_X_COLUMN",
    "ACCEL_Y_COLUMN",
    "ACCEL_Z_COLUMN",
    "DEFAULT_QUIESCENT_SECONDS",
    "FRAME_COLUMNS",
    "IMU_FRAME_FILENAME",
    "INPUT_MAX_AGE_SECONDS",
    "RESIDUAL_PITCH_OFFSET",
    "SF_LAT_COLUMN",
    "SF_LON_COLUMN",
    "SF_VERT_COLUMN",
    "SPECIFIC_FORCE_COLUMNS",
    "ImuFrameOfRecordError",
    "ImuFrameRefusal",
    "ImuFrameSidecar",
    "ImuSessionFrame",
    "channels_from_long",
    "channels_from_wide",
    "read_imu_frame_sidecar",
    "read_imu_inputs",
    "reference_id",
    "refused_sidecar",
    "resolve_imu_frame",
    "resolve_session_imu_frame",
    "specific_force_from_sidecar",
    "speed_decode_catalog",
    "write_imu_frame_sidecar",
]
