"""The offline IMU frame on the ``value_number`` chain, part by part (H02.4).

``loader.resample_and_segment`` (the trip-end digest, fingerprints, session
classification, ``scripts/autotag.py``) reads a session part by part (P02.1)
and decimates ``imu:ACCEL_LON``/``imu:ACCEL_LAT`` per tick
(``loader._decimate_imu_long``). No session records those two channels: the
LSM6DSOX records raw body-frame ``imu:ACCEL_X/Y/Z``, and the frame that turns
them into ``ACCEL_LON/LAT`` is resolved per session into ``imu_frame.json``
(:mod:`src.preprocessing.imu_session`). This module rebuilds that frame's
*linear* family inside the reader, so the offline segmenter's accel-gated modes
and the offline auto-tags see what the live path (``imu_live.LiveImuFrame``)
feeds the live segmenter.

**Exact, not approximate.** The whole-session resolution
(``imu_frame.resolve_frame``) has four steps. :class:`LinearAccelStream`
repeats each one on the rows it has, carrying state between parts:

1. **Inputs at each IMU instant.** It aligns speed and course to the IMU
   instants (the newest earlier reading, no older than
   ``imu_session.INPUT_MAX_AGE_SECONDS``), carrying the last reading of
   each across parts.
2. **The quiescent mask.** Rolling windows ``[i − window + 1, i +
   lookahead]`` over IMU rows. It keeps ``window − 1`` rows of history, and
   holds the last ``lookahead`` rows back until the next part supplies their
   look-ahead. This is the one step that is not bit-for-bit: pandas'
   rolling std accumulates rounding along the series, so a window's std
   differs by up to ~6.4e-10 (measured) depending on where the series
   started. The mask is a threshold on that std, so it can differ only where
   a window's std sits within that of 1.5 km/h (or 3 deg). Every value below
   is then bit-identical.
3. **The gravity low-pass.** An EWM with ``adjust=False``
   (``imu_frame.GRAVITY_LOW_PASS_ALPHA``), whose whole state is the last
   estimate, carried across parts bit for bit. The samples before the first
   quiescent instant subtract that instant's raw sample (the whole-session
   back-fill). The sidecar records it (``linear_accel.initial_gravity_body_g``),
   so no part waits for a later one.
4. **Session-wide yaw and sign**, read from the sidecar.

The stream verifies its inputs against the sidecar: the first quiescent
instant and its sample, and the IMU sample count and span. A mismatch means
the sidecar does not describe these rows. It raises
:class:`ImuFrameStaleError` rather than emit a frame from another session's
fit.

**Ticks stay whole.** ``_decimate_imu_long`` aggregates per tick, and places
each aggregate at the tick's first and last row over *every* channel. So a
tick's rows are emitted together, ``ACCEL_LON/LAT`` included. The ticks that
still hold unresolved IMU rows are held back: about ``lookahead`` samples,
~1.5 s. Later rows, which carry no IMU sample yet, pass on at once. A part
can therefore arrive after a later one, which ``derived.reduce_long_frames``
allows (it sorts its cells). Memory is one part, plus the held ticks, plus
``window + lookahead`` IMU rows.

**What is applied, and why not** (:class:`ImuFramePlan`, from the sidecar
before any row is read; :meth:`ImuFramePlan.use` after, from the reduced
channels):

- ``applied``: a resolved sidecar with the low-pass's initial state;
- ``no_imu_rows``: the session has no raw ``imu:ACCEL_X/Y/Z``. This covers
  every session before the HAT; there is nothing to resolve;
- ``unresolved``: raw IMU rows but no ``imu_frame.json``. The frame is
  **not** resolved on the fly. That needs the whole-session quiescent search
  and yaw fit (``imu_session.resolve_imu_frame``), which is not bounded by
  part size. The Pi's trip-end digest and classification run this chain in a
  memory-capped child (P02.1). So the accel channels stay absent, and the
  reason says to run ``scripts/resolve_imu_frame.py``;
- ``refused``: the sidecar records why the frame did not resolve (its
  ``kind``), so there is nothing to apply;
- ``stale``: a resolved sidecar written before the initial state was
  recorded (2026-09-26). Re-running ``scripts/resolve_imu_frame.py`` records
  it: a re-resolve keeps the recorded speed source
  (``imu_session.resolve_session_imu_frame``), so the reference is unchanged
  and no ``--replace`` is needed, even on a session whose ``0x2D1`` frames the
  old resolver could not decode.

``loader.resample_and_segment`` puts each session's :class:`ImuFrameUse` on its
result (``attrs[IMU_FRAME_ATTR]``), so an absent ``imu:ACCEL_LON_MAX`` says
which kind of absent it is.

**Parity with live.** Live resolves incrementally (``LiveImuFrame``). Its yaw
is a running fit and its gravity starts at the first quiescent sample
decided so far. So live emits nothing before the first straight manoeuvre's
samples are decided (the look-ahead), while offline uses the session-wide
fit from the first sample. Past that the two agree to the extent their
yaw estimates do. ``tests/test_imu_stream.py`` measures the operating-mode
agreement on a synthetic drive.

``imu:*`` stays non-trainable (H01.1, ``trainability.source_exclusion_reason``).
The training chain (``derived.reduce_session``) does not run through here.
"""

from __future__ import annotations

import hashlib
import json
import logging
from collections.abc import Collection, Iterator
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Literal

import numpy as np
import pandas as pd

from src.decode.bindings import Bindings
from src.decode.engineering import is_decoded_column, physical_series
from src.decode.on_read import on_read_catalog
from src.decode.slices import sentinel_channel
from src.preprocessing.imu_decimation import ACCEL_LAT_COLUMN, ACCEL_LON_COLUMN
from src.preprocessing.imu_frame import (
    GRAVITY_LOW_PASS_ALPHA,
    _project_horizontal,
    _rotate,
    steady_speed_mask,
    straight_mask,
)
from src.preprocessing.imu_session import (
    INPUT_MAX_AGE_SECONDS,
    RAW_ACCEL_COLUMNS,
    ImuFrameSidecar,
    read_imu_frame_sidecar,
    speed_decode_catalog,
)
from src.telemetry import DECODED_SOURCE, SOURCE_SEP, channel_key

log = logging.getLogger(__name__)

#: Key of ``resample_and_segment``'s result ``attrs``: ``{session: ImuFrameUse
#: .as_dict()}``.
IMU_FRAME_ATTR = "imu_frame"
#: Bump when the stream's output changes for an unchanged sidecar and rows
#: (it is part of :meth:`ImuFramePlan.key`, which versions stored frames).
IMU_STREAM_CODE_VERSION = 1

ImuFrameStatus = Literal["applied", "no_imu_rows", "unresolved", "refused", "stale"]

_PART_COLUMNS = ["session", "timestamp", "source_channel", "value_number"]
_XYZ = ["x", "y", "z"]


class ImuFrameStaleError(ValueError):
    """A session's ``imu_frame.json`` does not describe its IMU rows (another
    sample count or span, or another first quiescent sample): re-resolve it."""


@dataclass(frozen=True)
class ImuFrameUse:
    """What the offline chain did with one session's IMU frame, and why."""

    session: str
    status: ImuFrameStatus
    reason: str
    reference_id: str | None = None

    def as_dict(self) -> dict[str, Any]:
        return {
            "status": self.status,
            "reason": self.reason,
            "reference_id": self.reference_id,
        }


@dataclass(frozen=True)
class ImuFramePlan:
    """Whether a session's sidecar can be applied, decided before reading rows."""

    session: str
    status: ImuFrameStatus
    reason: str
    sidecar: ImuFrameSidecar | None = None
    #: The catalog narrowed to the sidecar's ``decoded:`` speed source, when it
    #: steers by one (``imu_session.speed_decode_catalog``): decoded from the
    #: raw frames, whatever the binding's confidence, never through the
    #: on-read set alone.
    speed_catalog: Bindings | None = None

    @property
    def applies(self) -> bool:
        return self.status == "applied"

    def read_catalog(self, on_read: Bindings | None) -> tuple[Bindings | None, frozenset[str]]:
        """The catalog to read the session's ``decoded:`` rows with, and the
        channels it adds that only the stream reads (dropped after it, so the
        frame gains nothing but ``ACCEL_LON/LAT``)."""
        if not self.applies or self.speed_catalog is None:
            return on_read, frozenset()
        present = {signal.name for signal in on_read.signals} if on_read is not None else set()
        extra = [s for s in self.speed_catalog.signals if s.name not in present]
        if not extra:
            return on_read, frozenset()
        base = on_read if on_read is not None else self.speed_catalog
        signals = [*(on_read.signals if on_read is not None else []), *extra]
        added = {channel_key(DECODED_SOURCE, s.name) for s in extra}
        added |= {channel_key(DECODED_SOURCE, sentinel_channel(s.name)) for s in extra}
        return base.model_copy(update={"signals": signals}), frozenset(added)

    def key(self) -> str:
        """Identity of what the stream adds to the rows: ``"none"`` when it
        adds nothing, else a hash of every sidecar value it reads. A stored
        frame built under another key is stale (``derived_store``)."""
        if not self.applies or self.sidecar is None:
            return "none"
        inputs, linear = self.sidecar.inputs, self.sidecar.linear_accel
        assert inputs is not None and linear is not None
        speed = [] if self.speed_catalog is None else self.speed_catalog.signals
        payload = {
            "stream_code_version": IMU_STREAM_CODE_VERSION,
            "inputs": inputs.model_dump(mode="json"),
            "linear_accel": linear.model_dump(mode="json"),
            "speed_decode": [signal.model_dump(mode="json") for signal in speed],
        }
        text = json.dumps(payload, sort_keys=True)
        return hashlib.sha256(text.encode()).hexdigest()[:16]

    def use(self, channels: Collection[str]) -> ImuFrameUse:
        """The outcome, given the channels the session's reduced frame holds."""
        reference = (
            self.sidecar.specific_force.reference_id
            if self.sidecar is not None and self.sidecar.specific_force is not None
            else None
        )
        if self.applies:
            return ImuFrameUse(self.session, "applied", self.reason, reference)
        if not all(column in channels for column in RAW_ACCEL_COLUMNS):
            return ImuFrameUse(
                self.session,
                "no_imu_rows",
                f"no raw {'/'.join(RAW_ACCEL_COLUMNS)} rows: nothing to resolve",
            )
        return ImuFrameUse(self.session, self.status, self.reason, reference)


def plan_imu_frame(session_dir: Path) -> ImuFramePlan:
    """Read ``session_dir``'s ``imu_frame.json`` and decide what the stream does.

    A malformed sidecar raises, as :func:`~src.preprocessing.imu_session.
    read_imu_frame_sidecar` does: a frame of record that cannot be read is
    not the same as none.
    """
    session_dir = Path(session_dir)
    session = session_dir.name
    sidecar = read_imu_frame_sidecar(session_dir)
    if sidecar is None:
        return ImuFramePlan(
            session,
            "unresolved",
            "no imu_frame.json: the session's IMU frame was never resolved, so "
            "imu:ACCEL_LON/LAT (and the accel-gated modes and tags) are absent; run "
            f"scripts/resolve_imu_frame.py {session}",
        )
    if sidecar.status != "resolved" or sidecar.linear_accel is None:
        refusal = sidecar.refusal
        cause = f"{refusal.kind}: {refusal.reason}" if refusal is not None else "unresolved"
        return ImuFramePlan(session, "refused", f"imu_frame.json records a refusal ({cause})")
    linear = sidecar.linear_accel
    speed_source = sidecar.inputs.speed_source if sidecar.inputs is not None else None
    if linear.initial_gravity_body_g is None or linear.first_quiescent_sample is None:
        return ImuFramePlan(
            session,
            "stale",
            "imu_frame.json predates the recorded low-pass initial state (2026-09-26), so "
            "the frame cannot be rebuilt part by part; re-run scripts/resolve_imu_frame.py "
            f"{session} (it keeps the recorded speed source {speed_source}, so the "
            "reference is unchanged: no --replace needed)",
        )
    speed_catalog = None
    if speed_source is not None and is_decoded_column(speed_source):
        name = speed_source.split(SOURCE_SEP, 1)[1]
        catalog = speed_decode_catalog()
        signals = [s for s in catalog.signals if s.name == name] if catalog is not None else []
        if not signals:
            return ImuFramePlan(
                session,
                "stale",
                f"imu_frame.json steers by {speed_source}, which the bindings catalog no "
                f"longer has; re-run scripts/resolve_imu_frame.py {session}",
            )
        assert catalog is not None
        speed_catalog = catalog.model_copy(update={"signals": signals})
    return ImuFramePlan(
        session,
        "applied",
        f"imu:ACCEL_LON/LAT rebuilt from imu_frame.json (yaw {linear.yaw_offset_rad:.4f} rad, "
        f"sign {'flipped' if linear.sign_flipped else 'kept'}, speed {speed_source})",
        sidecar,
        speed_catalog,
    )


def iter_framed_numeric_parts(
    session_dir: Path,
    *,
    align_ns: int,
    target_rows: int | None = None,
    on_read: Bindings | None | Literal["current"] = "current",
    plan: ImuFramePlan | None = None,
) -> Iterator[pd.DataFrame]:
    """``loader.iter_session_numeric_parts`` with the session's
    ``imu:ACCEL_LON/LAT`` rows added, whole ticks at a time: the one way the
    ``value_number`` chain reads a session (``derived.reduce_numeric_session``).

    Reads with ``on_read`` (``"current"`` loads the on-read catalog) widened by
    the sidecar's ``decoded:`` speed source (:meth:`ImuFramePlan.read_catalog`),
    whose rows it drops once the stream has read them. The read and the
    stream are one call so neither can be done without the other. ``plan``
    defaults to :func:`plan_imu_frame`; the parts are the reader's own when it
    does not apply.
    """
    from src.preprocessing.loader import SESSION_PART_ROWS, iter_session_numeric_parts

    plan = plan_imu_frame(session_dir) if plan is None else plan
    catalog = on_read_catalog() if on_read == "current" else on_read
    read_with, stream_only = plan.read_catalog(catalog)
    parts = iter_session_numeric_parts(
        session_dir,
        target_rows=target_rows or SESSION_PART_ROWS,
        align_ns=align_ns,
        on_read=read_with,
    )
    if not plan.applies:
        yield from parts
        return
    stream = LinearAccelStream(plan, align_ns=align_ns, drop=stream_only)
    for part in parts:
        yield from stream.feed(part)
    yield from stream.finish()


class LinearAccelStream:
    """``imu_frame.resolve_frame``'s linear family, one part at a time.

    :meth:`feed` each ``value_number`` part in time order, then :meth:`finish`.
    Each yields zero or more frames of whole ticks.
    """

    def __init__(
        self,
        plan: ImuFramePlan,
        *,
        align_ns: int,
        bindings: Bindings | None = None,
        drop: Collection[str] = (),
    ) -> None:
        sidecar = plan.sidecar
        if not plan.applies or sidecar is None:
            raise ValueError(f"{plan.session}: the IMU frame does not apply ({plan.reason})")
        inputs, linear = sidecar.inputs, sidecar.linear_accel
        assert inputs is not None and linear is not None
        assert linear.initial_gravity_body_g is not None
        assert linear.first_quiescent_sample is not None
        if inputs.speed_source is None or inputs.heading_source is None:
            raise ValueError(f"{plan.session}: a resolved imu frame names no speed or heading")
        self.session = plan.session
        self._window = int(inputs.quiescent_window_samples)
        self._lookahead = int(inputs.quiescent_lookahead_samples)
        self._speed_column = inputs.speed_source
        self._heading_column = inputs.heading_source
        self._yaw = float(linear.yaw_offset_rad)
        self._flipped = bool(linear.sign_flipped)
        self._initial = np.asarray(linear.initial_gravity_body_g, dtype=np.float64)
        self._first_quiescent = pd.Timestamp(linear.first_quiescent_sample)
        self._expected = (
            int(inputs.imu_samples),
            pd.Timestamp(inputs.first_sample),
            pd.Timestamp(inputs.last_sample),
        )
        self._align_ns = int(align_ns)
        self._bindings = bindings
        self._drop = frozenset(drop)
        self._tolerance = pd.Timedelta(seconds=INPUT_MAX_AGE_SECONDS)
        #: The parts' timestamp dtype: emitted ACCEL rows take it, so adding
        #: them never changes the frame's resolution (the resampled grid
        #: inherits it). Lossless: their instants came from that column.
        self._stamp_dtype: Any = None
        # IMU rows not yet dropped: ``_history`` resolved rows (the rolling
        # windows' reach back), then the unresolved ones.
        self._ts = pd.DatetimeIndex([], tz="UTC").as_unit("ns")
        self._acc = np.empty((0, 3), dtype=np.float64)
        self._speed = np.empty(0, dtype=np.float64)
        self._heading = np.empty(0, dtype=np.float64)
        self._history = 0
        self._last_input: dict[str, pd.DataFrame] = {}
        self._state: np.ndarray | None = None
        # Rows of ticks still holding unresolved IMU rows, and resolved
        # ACCEL_LON/LAT rows not yet emitted.
        self._held: pd.DataFrame | None = None
        self._resolved: list[pd.DataFrame] = []
        self._count = 0
        self._first_ts: pd.Timestamp | None = None
        self._last_ts: pd.Timestamp | None = None
        #: Most rows held back at once (all channels): the memory bound's
        #: measure, surfaced for tests.
        self.peak_held_rows = 0

    # -- public ---------------------------------------------------------------

    def feed(self, part: pd.DataFrame) -> Iterator[pd.DataFrame]:
        part = part[_PART_COLUMNS]
        self._append(part)
        if self._drop:
            part = part.loc[~part["source_channel"].isin(self._drop)]
        self._resolve(final=False)
        yield from self._emit(part, final=False)

    def finish(self) -> Iterator[pd.DataFrame]:
        self._resolve(final=True)
        expected_n, expected_first, expected_last = self._expected
        seen = (self._count, self._first_ts, self._last_ts)
        if seen != (expected_n, expected_first, expected_last):
            raise ImuFrameStaleError(
                f"{self.session}: imu_frame.json was resolved from {expected_n} IMU samples "
                f"{expected_first}..{expected_last}, the session has {self._count} "
                f"{self._first_ts}..{self._last_ts}; re-run scripts/resolve_imu_frame.py "
                "(--replace if the reference changes)"
            )
        if self._state is None:
            raise ImuFrameStaleError(
                f"{self.session}: imu_frame.json names a first quiescent sample at "
                f"{self._first_quiescent}, and these rows have no quiescent sample"
            )
        yield from self._emit(None, final=True)

    # -- inputs ---------------------------------------------------------------

    def _append(self, part: pd.DataFrame) -> None:
        if self._stamp_dtype is None or not part.empty:
            self._stamp_dtype = part["timestamp"].dtype
        axes = [_clean(part, column) for column in RAW_ACCEL_COLUMNS]
        accel = pd.concat(axes, axis=1, join="inner").dropna()
        accel.columns = pd.Index(_XYZ)
        accel = accel.sort_index(kind="stable")
        base = pd.DatetimeIndex(accel.index)
        speed = self._aligned(part, self._speed_column, base)
        heading = self._aligned(part, self._heading_column, base)
        if base.empty:
            return
        self._ts = pd.DatetimeIndex(self._ts.append(base.as_unit("ns")))
        self._acc = np.vstack([self._acc, accel[_XYZ].to_numpy(dtype=np.float64)])
        self._speed = np.concatenate([self._speed, speed])
        self._heading = np.concatenate([self._heading, heading])
        self._count += len(base)
        if self._first_ts is None:
            self._first_ts = pd.Timestamp(base[0])
        self._last_ts = pd.Timestamp(base[-1])

    def _aligned(self, part: pd.DataFrame, column: str, base: pd.DatetimeIndex) -> np.ndarray:
        """``column`` at each ``base`` instant: the newest reading no older than
        ``INPUT_MAX_AGE_SECONDS``, from this part or the last one before it
        (``imu_session._aligned`` over the whole session)."""
        raw = _clean(part, column)
        readings = pd.DataFrame(
            {
                "timestamp": pd.DatetimeIndex(raw.index).as_unit("ns"),
                "value": physical_series(raw, column, self._bindings).to_numpy(dtype=float),
            }
        )
        carried = self._last_input.get(column)
        if carried is not None:
            readings = pd.concat([carried, readings], ignore_index=True)
        if not readings.empty:
            self._last_input[column] = readings.iloc[-1:].reset_index(drop=True)
        if base.empty:
            return np.empty(0, dtype=np.float64)
        left = pd.DataFrame({"timestamp": base.as_unit("ns")})
        if readings.empty:
            return np.full(len(base), np.nan)
        merged = pd.merge_asof(
            left, readings, on="timestamp", direction="backward", tolerance=self._tolerance
        )
        return np.asarray(merged["value"].to_numpy(dtype=float))

    # -- resolution -----------------------------------------------------------

    def _resolve(self, *, final: bool) -> None:
        n = len(self._acc)
        start = self._history
        stop = n if final else n - self._lookahead
        if stop <= start:
            return
        speed = pd.Series(self._speed)
        heading = pd.Series(self._heading)
        # A resolved frame had a heading (its yaw came from straight rows), so
        # the whole-session mask took quiescent_mask's speed-and-course branch.
        steady = steady_speed_mask(speed, window=self._window, lookahead=self._lookahead)
        straight = straight_mask(heading, window=self._window, lookahead=self._lookahead)
        quiescent = (steady & straight).to_numpy(dtype=bool)[start:stop]
        accel = self._acc[start:stop]
        gravity = self._gravity(accel, quiescent, self._ts[start:stop])
        linear = accel - gravity
        h1, h2 = _project_horizontal(linear, gravity)
        lon, lat = _rotate(pd.Series(h1), pd.Series(h2), self._yaw)
        if self._flipped:
            lon, lat = -lon, -lat
        stamps = self._ts[start:stop]
        for column, values in ((ACCEL_LON_COLUMN, lon), (ACCEL_LAT_COLUMN, lat)):
            self._resolved.append(
                pd.DataFrame(
                    {
                        "session": self.session,
                        "timestamp": stamps,
                        "source_channel": column,
                        "value_number": values.to_numpy(dtype=np.float64),
                    }
                )
            )
        keep_from = max(0, stop - (self._window - 1))
        self._ts = self._ts[keep_from:]
        self._acc = self._acc[keep_from:]
        self._speed = self._speed[keep_from:]
        self._heading = self._heading[keep_from:]
        self._history = stop - keep_from

    def _gravity(
        self, accel: np.ndarray, quiescent: np.ndarray, stamps: pd.DatetimeIndex
    ) -> np.ndarray:
        """``imu_frame.estimate_gravity`` for these rows, from the carried state."""
        seed = self._state if self._state is not None else self._initial
        if not quiescent.any():
            return np.tile(seed, (len(accel), 1))
        rows = pd.DataFrame(accel[quiescent], columns=_XYZ)
        if self._state is None:
            first = int(np.flatnonzero(quiescent)[0])
            self._check_first_quiescent(pd.Timestamp(stamps[first]), accel[first])
            smoothed = rows.ewm(alpha=GRAVITY_LOW_PASS_ALPHA, adjust=False).mean().to_numpy()
        else:
            seeded = pd.concat([pd.DataFrame([self._state], columns=_XYZ), rows], ignore_index=True)
            smoothed = seeded.ewm(alpha=GRAVITY_LOW_PASS_ALPHA, adjust=False).mean().to_numpy()[1:]
        self._state = smoothed[-1].copy()
        seen = np.cumsum(quiescent)
        out = np.tile(seed, (len(accel), 1))
        after = seen > 0
        out[after] = smoothed[seen[after] - 1]
        return out

    def _check_first_quiescent(self, stamp: pd.Timestamp, sample: np.ndarray) -> None:
        if stamp != self._first_quiescent or not np.array_equal(sample, self._initial):
            raise ImuFrameStaleError(
                f"{self.session}: imu_frame.json's low-pass starts at {self._first_quiescent} "
                f"with {self._initial.tolist()}, these rows' first quiescent sample is {stamp} "
                f"with {sample.tolist()}; re-run scripts/resolve_imu_frame.py"
            )

    # -- emission -------------------------------------------------------------

    def _emit(self, part: pd.DataFrame | None, *, final: bool) -> Iterator[pd.DataFrame]:
        pieces = [frame for frame in (self._held, part) if frame is not None and not frame.empty]
        rows = pd.concat(pieces, ignore_index=True) if pieces else None
        accel = pd.concat(self._resolved, ignore_index=True) if self._resolved else None
        self._held, self._resolved = None, []
        unresolved = len(self._ts) - self._history
        if not final and unresolved > 0:
            first = self._tick(pd.Series(self._ts[self._history : self._history + 1]))[0]
            last = self._tick(pd.Series(self._ts[-1:]))[0]
            if rows is not None:
                ticks = self._tick(rows["timestamp"])
                hold = (ticks >= first) & (ticks <= last)
                if hold.any():
                    self._held = rows.loc[hold].reset_index(drop=True)
                    rows = rows.loc[~hold]
            if accel is not None:
                later = self._tick(accel["timestamp"]) >= first
                if later.any():
                    self._resolved = [accel.loc[later].reset_index(drop=True)]
                    accel = accel.loc[~later]
        held = 0 if self._held is None else len(self._held)
        self.peak_held_rows = max(self.peak_held_rows, held)
        if accel is not None and self._stamp_dtype is not None:
            target = rows["timestamp"].dtype if rows is not None else self._stamp_dtype
            accel["timestamp"] = accel["timestamp"].astype(target)
        out = [frame for frame in (rows, accel) if frame is not None and not frame.empty]
        if not out:
            return
        frame = pd.concat(out, ignore_index=True) if len(out) > 1 else out[0]
        yield frame.sort_values("timestamp", kind="stable").reset_index(drop=True)

    def _tick(self, stamps: pd.Series) -> np.ndarray:
        index = pd.DatetimeIndex(pd.to_datetime(stamps, utc=True))
        ns = index.as_unit("ns").tz_localize(None).to_numpy().view(np.int64)
        ticks: np.ndarray = ns // self._align_ns * self._align_ns
        return ticks


def _clean(part: pd.DataFrame, column: str) -> pd.Series:
    """One channel's non-null values in ``part`` indexed by time, the last of
    duplicate instants kept (``imu_session._clean``)."""
    rows = part.loc[part["source_channel"] == column, ["timestamp", "value_number"]]
    rows = rows.loc[rows["value_number"].notna()]
    rows = rows.drop_duplicates("timestamp", keep="last")
    index = pd.DatetimeIndex(pd.to_datetime(rows["timestamp"], utc=True))
    series = pd.Series(rows["value_number"].to_numpy(dtype=np.float64), index=index)
    return series.sort_index(kind="stable")


__all__ = [
    "IMU_FRAME_ATTR",
    "IMU_STREAM_CODE_VERSION",
    "ImuFramePlan",
    "ImuFrameStaleError",
    "ImuFrameStatus",
    "ImuFrameUse",
    "LinearAccelStream",
    "iter_framed_numeric_parts",
    "plan_imu_frame",
]
