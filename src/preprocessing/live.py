"""Streaming feature pipeline for live inference (Milestone B).

The offline path (``build_feature_frame``) is a batch transform over a
recorded session's Parquet. Live inference needs the same outputs row-by-row
as records arrive from the collectors. ``LiveFeatureSource`` is the
incremental version:

1. **observe(record)** — update the "current value" dict from one
   ``TelemetryRecord``. OBD records map directly (``obd:RPM`` ← value).
   CAN records get decoded via ``bindings.yaml`` (same byte-extraction
   path as ``decode_can_rows``), producing ``decoded:<SIGNAL>`` entries.
   This is the only side-effect — emitting a wide row is a separate call.

2. **tick(now)** — emit one wide row (current_values as of ``now``),
   append to the rolling buffer, then run the *shared* wide→features
   builder (``feature_pipeline.featurize_wide_frame``: segment →
   deviation → ``compute_features``) over the buffered window and return
   the *last* row. The offline batch path runs the same builder, so the
   two cannot schema-diverge by hand (7.1). ``None`` until enough history
   has accumulated.

3. **feature_columns** — the columns a downstream inference engine will
   see. Stable across ticks once observation has begun.

The ring buffer holds enough rows for both rolling features and the
segmenter's recent-motion lookback: ``max(2 × window_seconds,
idle_transient_lookback_seconds) × resample_hz`` (plus slack). The window
term keeps ``compute_features`` full right after startup; the lookback term
lets serve-time idle_transient vs idle see the preceding drive, matching
training. At 1 Hz with a 30 s window and 180 s lookback that's ~181 rows —
still trivial to recompute each tick. Higher resample rates scale linearly;
revisit if it ever becomes the bottleneck (NFR-01 budget is <50% Pi 5 CPU).

Decode policy intentionally matches the offline path:
``decode_can_rows`` emits the *raw integer* (not scaled), because the
scaler normalizes anyway and many bindings have no pinned scale yet.
"""

from __future__ import annotations

import logging
import math
from collections import deque
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any

import pandas as pd

from src.analysis.derived import (
    IDLE_RPM_FLOOR,
    IDLE_SPEED_CEIL_KPH,
    MAX_INTEGRATION_DT_S,
    fuel_properties,
)
from src.can_ids import parse_can_channel
from src.config import CanaryConfig
from src.decode.accepted_baselines import AcceptedBaselines
from src.decode.behavior_models import BehaviorModels
from src.decode.bindings import Bindings, Signal
from src.decode.dids import (
    DID_SOURCE,
    DecodedDid,
    DidEntry,
    DidMap,
    decode_raw_did_reading,
)
from src.preprocessing.feature_pipeline import featurize_wide_frame
from src.preprocessing.features import FeatureConfig
from src.preprocessing.quality_gate import is_physically_valid
from src.preprocessing.resampler import ResampleConfig, gap_column, is_slow_channel
from src.telemetry import (
    DBC_SOURCE,
    DECODED_SOURCE,
    GPS_SOURCE,
    IMU_SOURCE,
    OBD_SOURCE,
    SOURCE_SEP,
    TelemetryRecord,
    channel_key,
)

log = logging.getLogger(__name__)


@dataclass
class LiveFeatureSource:
    """Incremental feature pipeline. One per inference engine, or shared.

    ``session`` is the label attached to every emitted row — analogous to
    the offline pipeline's session-dir name. The session-keyed Parquet
    writer (1.11) rotates separately; this is a logical label for the
    feature stage, used only by the segmenter / feature-window grouping
    so windows reset on a session change.
    """

    config: CanaryConfig
    bindings: Bindings | None = None
    behavior_models: BehaviorModels | None = None
    #: Active accepted-baseline overrides (2.2, online-training-strategy.md
    #: Phase 2) — ``None`` means "no accepted_baselines.yaml for this
    #: profile", identical in effect to an empty document (no override
    #: ever applies). The daemon's behavior-detector loop consults this via
    #: ``AcceptedBaselines.active_override_for`` once per model per tick.
    accepted_baselines: AcceptedBaselines | None = None
    dbc_database: Any | None = None
    did_map: DidMap | None = None
    session: str = "live"

    _current_values: dict[str, float] = field(default_factory=dict)
    #: Per-channel wall-clock timestamp of the last fresh ``observe()``. GRU-D
    #: serve derives each channel's mask (``age ≤ max_gap``) and Δt (``age``)
    #: from this, mirroring the resampler's ``__gap`` rule with no resampler.
    _last_observed_at: dict[str, datetime] = field(default_factory=dict)
    #: Per-channel count of values rejected by the quality gate (Task 1.2) —
    #: never silently swallowed, surfaced via :attr:`reject_counts`. Cleared
    #: on :meth:`reset` like every other observation-derived state.
    _reject_counts: dict[str, int] = field(default_factory=dict)
    _buffer: deque[dict[str, Any]] = field(init=False)
    _bindings_by_id: dict[int, list[Signal]] = field(init=False)
    _dbc_message_by_id: dict[int, Any] = field(init=False)
    _did_by_hex: dict[str, DidEntry] = field(init=False)
    _feature_config: FeatureConfig = field(init=False)
    _max_rows: int = field(init=False)
    #: Resampler ``max_gap`` in seconds — the staleness cutoff that flips a
    #: channel's mask to 0. Mirrors ``ResampleConfig(hz).max_gap`` so train and
    #: serve agree on what "stale" means for the same observation history.
    _max_gap_seconds: float = field(init=False)
    #: Slow-channel counterpart to ``_max_gap_seconds`` (1.1/1.2) — mirrors
    #: ``ResampleConfig.slow_channel_max_gap``, generous relative to the fast
    #: threshold since slow sensors (coolant/IAT/baro) legitimately update
    #: far less often.
    _slow_max_gap_seconds: float = field(init=False)

    #: Last wide-row operating point (raw channels + ``operating_mode`` +
    #: ``deviation:*``) from :meth:`tick`. The daemon reads this to compute
    #: live behavior deviations off the exact row the feature pipeline used,
    #: rather than re-deriving segmentation. ``None`` until the first tick
    #: with observations; reset to ``None`` on an empty tick so a stale point
    #: can't outlive a data gap.
    latest_operating_point: dict[str, Any] | None = field(init=False, default=None)

    #: 4.7 — whether the most recent :meth:`tick` that actually ran the
    #: transform hit ``segment_or_constant``'s runtime fallback (segmenter
    #: input columns missing from the buffer), as opposed to real
    #: segmentation or an explicit ``single_operating_mode`` config. The
    #: daemon feeds this into ``HealthMonitor`` so every mode-gated surface
    #: silently never firing shows up on ``/status`` instead of nowhere.
    #: Retains its last value on a tick with nothing to emit (no new
    #: information that tick), same as ``latest_operating_point``.
    last_segmenter_fallback: bool = field(init=False, default=False)

    #: Session-scoped trip-integral accumulators (Task 1.3). A live tick's
    #: ring-buffer window (``_buffer``) evicts old rows continuously, so a
    #: ``cumsum`` over that window (what ``compute_derived_channels`` does
    #: for a *complete* offline session) would reset every time the buffer
    #: slides, not just at a session boundary. These instead accumulate
    #: incrementally across ticks, using each tick's real wall-clock ``dt``
    #: (mirroring ``compute_derived_channels``'s own ``_MAX_DT_S``-capped
    #: per-row dt) — reset only by :meth:`reset` (the same replay-isolation
    #: boundary Phase 7.1 already uses), so they read "since this session
    #: started" the way the batch cumsum reads "since the recording started".
    _trip_distance_km: float = field(init=False, default=0.0)
    _fuel_used_l: float = field(init=False, default=0.0)
    _co2_kg: float = field(init=False, default=0.0)
    _idle_time_s: float = field(init=False, default=0.0)
    #: Per-quantity last-accrual markers: wall-clock time and value of the
    #: last tick on which each integrand was actually observed. A tick where
    #: the input is stale/absent must NOT advance the marker — the elapsed
    #: time is bridged (``MAX_INTEGRATION_DT_S``-capped) at the next fresh
    #: sample instead of being dropped, mirroring the batch integral's
    #: valid-sample dt (``_cumulative_time_integral``). A single shared
    #: last-tick timestamp discarded every stale tick's dt and halved the
    #: live trip odometer on a contended bus.
    _speed_marker: tuple[datetime, float] | None = field(init=False, default=None)
    _fuel_marker: tuple[datetime, float] | None = field(init=False, default=None)
    _idle_marker: tuple[datetime, float] | None = field(init=False, default=None)
    #: Last tick's session-scoped trip integrals (Task 1.3), keyed the same
    #: as ``compute_derived_channels``'s cumulative columns
    #: (``derived:TRIP_DISTANCE_KM`` etc.) so a downstream consumer can merge
    #: this dict directly onto a feature row. ``None`` until the first tick
    #: that has both a fresh observation and enough inputs to accumulate.
    latest_derived_channels: dict[str, float] | None = field(init=False, default=None)

    def __post_init__(self) -> None:
        hz = max(self.config.preprocessing.resample_hz, 1.0)
        window_s = self.config.preprocessing.rolling_window_seconds
        # Keep a slack factor — pandas' time-window rolling can use anything
        # in the buffer, but we want enough rows to actually fill a window
        # plus a margin so the ROC shift doesn't fall off the end. The buffer
        # must also span the segmenter's recent-motion lookback so serve-time
        # idle_transient vs idle matches training (a multi-minute stop still
        # sees the preceding drive); at 1 Hz that's a few hundred trivial rows.
        recent_motion_s = self.config.preprocessing.idle_transient_lookback_seconds
        self._max_rows = max(32, int(window_s * hz * 2), int(recent_motion_s * hz) + int(hz))
        self._buffer = deque(maxlen=self._max_rows)
        self._feature_config = FeatureConfig(window_seconds=window_s)
        # Mirror the resampler's max_gap (interval × max_gap_factor) used at
        # training time, so a channel reads stale at serve exactly when its
        # ``__gap`` flag would have been set offline.
        resample_config = ResampleConfig(
            hz=self.config.preprocessing.resample_hz,
            max_gap_factor=self.config.preprocessing.max_gap_factor,
            slow_channel_max_gap_seconds=self.config.preprocessing.slow_channel_max_gap_seconds,
        )
        self._max_gap_seconds = float(resample_config.max_gap.total_seconds())
        self._slow_max_gap_seconds = float(resample_config.slow_channel_max_gap.total_seconds())
        # Pre-index bindings by CAN ID so decode is O(signals per ID), not
        # O(all signals) per CAN frame.
        self._bindings_by_id = {}
        if self.bindings is not None:
            for sig in self.bindings.signals:
                self._bindings_by_id.setdefault(sig.can_id, []).append(sig)
        # Same idea for the DBC database: frame_id → message so a per-frame
        # decode is an O(1) lookup, not a scan of every message.
        self._dbc_message_by_id = {}
        if self.dbc_database is not None:
            for msg in self.dbc_database.messages:
                self._dbc_message_by_id[msg.frame_id] = msg
        # DID map indexed by 4-char hex so a raw "DID_1208" reading resolves
        # to its DidEntry in O(1).
        self._did_by_hex = {}
        if self.did_map is not None:
            for entry in self.did_map.dids:
                self._did_by_hex[f"{entry.did:04X}"] = entry

    # ------------------------------------------------------------------
    # observe — update the current-value dict
    # ------------------------------------------------------------------

    def _set_value(self, channel: str, value: float, observed_at: datetime) -> None:
        """Store a channel's current value and stamp its observation time.

        The observation time drives the GRU-D serve mask/Δt: at each tick a
        channel is fresh iff ``now − observed_at ≤ max_gap``. ``observed_at`` is
        normalized to tz-aware UTC so age arithmetic against the tick's ``now``
        never mixes naive/aware datetimes.

        Task 1.2 quality gate: a physically-impossible value (see
        ``src.analysis.fsm.PHYSICAL_LIMITS``) is never stored — the channel
        simply doesn't get a fresh observation this tick, so it ages out via
        the exact same staleness path (``_last_observed_at`` / ``_gap_flags``)
        a channel that stopped transmitting would. Counted, never swallowed.
        """
        if not is_physically_valid(channel, value):
            self._reject_counts[channel] = self._reject_counts.get(channel, 0) + 1
            log.debug(
                "quality gate rejected live value",
                extra={"channel": channel, "value": value},
            )
            return
        self._current_values[channel] = value
        ts = observed_at if observed_at.tzinfo is not None else observed_at.replace(tzinfo=UTC)
        self._last_observed_at[channel] = ts

    def observe(self, record: TelemetryRecord) -> None:
        """Apply one ``TelemetryRecord`` to the running current-values dict.

        Side-effects only. Emitting a wide row is a separate tick.
        """
        if record.source == "obd":
            # Raw Mode-22 DID poll responses arrive as obd-source records with
            # channel "DID_<hex>" and a hex-string payload value. Decode them
            # into named ``did:<NAME>`` numerics so live inference + the gauge
            # see the engineering value, not an undecodable hex string. Mirror
            # of decode_did_rows on the offline path.
            if self._did_by_hex and record.channel.startswith("DID_"):
                self._apply_did_decode(record)
                return
            value = _to_float(record.value)
            if value is not None:
                self._set_value(channel_key(OBD_SOURCE, record.channel), value, record.timestamp)
            return
        if record.source == "can":
            self._apply_can_decode(record)
            return
        if record.source == "gps":
            value = _to_float(record.value)
            if value is not None:
                self._set_value(channel_key(GPS_SOURCE, record.channel), value, record.timestamp)
            return
        if record.source == "imu":
            # No decimation here (2a.3 is a batch/pre-pivot concern — see
            # src.preprocessing.imu_decimation): live serve just tracks the
            # latest raw/frame-resolved value like every other source, same
            # as the "anything else" catch-all did before this source got
            # its own arm. Phase 2b's collector is what actually starts
            # emitting these records; ``ImuConfig.source: disabled`` (2a.2)
            # means none arrive today.
            value = _to_float(record.value)
            if value is not None:
                self._set_value(channel_key(IMU_SOURCE, record.channel), value, record.timestamp)
            return
        # Anything else (e.g. a future source pushed directly) gets stored
        # as-is; the pipeline will surface it as a new column.
        value = _to_float(record.value)
        if value is not None:
            self._set_value(channel_key(record.source, record.channel), value, record.timestamp)

    def decode_for_telemetry(self, record: TelemetryRecord) -> tuple[TelemetryRecord, ...]:
        """Return decoded signal rows for a raw CAN record.

        This is the dashboard-facing counterpart to ``observe``'s CAN decode:
        inference wants the values in ``_current_values``; the UI also needs
        named ``decoded:<SIGNAL>`` rows so it can render gauges instead of raw
        arbitration IDs.
        """
        if record.source != "can" or record.payload is None:
            return ()
        can_id = parse_can_channel(record.channel)
        if can_id is None:
            return ()
        signals = self._bindings_by_id.get(can_id)
        if not signals:
            return ()
        payload = bytes(record.payload)
        decoded: list[TelemetryRecord] = []
        for sig in signals:
            raw = _decode_signal_raw(sig, payload)
            if raw is None:
                continue
            decoded.append(
                TelemetryRecord.decoded(
                    record.timestamp,
                    sig.name,
                    raw,
                    sig.unit if sig.unit != "unknown" else "",
                    metadata={
                        "can_id": can_id,
                        "confidence": sig.confidence,
                    },
                )
            )
        return tuple(decoded)

    def _apply_can_decode(self, record: TelemetryRecord) -> None:
        for decoded in self.decode_for_telemetry(record):
            # Raw integer matches decode_can_rows' policy — the scaler
            # normalizes downstream and most bindings have no pinned scale.
            value = _to_float(decoded.value)
            if value is not None:
                self._set_value(
                    channel_key(DECODED_SOURCE, decoded.channel), value, record.timestamp
                )
        self._apply_dbc_decode(record)

    def _apply_dbc_decode(self, record: TelemetryRecord) -> None:
        """Mirror ``decode_can_rows_dbc``: emit ``dbc:<SIGNAL>`` current values.

        The offline trainer decodes CAN through the DBC database too, so the
        model's feature columns include ``dbc:*``. Without this the live row
        is missing every ``dbc:*`` column and inference fails the schema
        check. Same policy as the offline path: cantools ``decode`` with
        ``decode_choices=False``, numeric signals only (named/enum choices
        skip the float() cast).
        """
        if self.dbc_database is None or record.payload is None:
            return
        can_id = parse_can_channel(record.channel)
        if can_id is None:
            return
        message = self._dbc_message_by_id.get(can_id)
        if message is None:
            return
        try:
            decoded = message.decode(bytes(record.payload), decode_choices=False)
        except Exception:  # noqa: BLE001 — cantools raises a family of errors
            return
        for signal_name, value in decoded.items():
            try:
                numeric = float(value)
            except (TypeError, ValueError):
                continue  # named choice / enum signals — skip in numeric path
            self._set_value(channel_key(DBC_SOURCE, signal_name), numeric, record.timestamp)

    def decode_did_for_telemetry(self, record: TelemetryRecord) -> DecodedDid | None:
        """Decode a raw ``DID_<hex>`` reading, or ``None`` if it isn't one.

        The DID counterpart of :meth:`decode_for_telemetry`: raw Mode-22
        polls ride the bus as ``obd:DID_<hex>`` rows carrying the response
        payload as a hex string, but every consumer — dashboard gauges,
        behavior recipes, the wide frame — speaks ``did:<NAME>`` in
        engineering units. Callers publishing to the live stream turn this
        into that row; ``did`` is a pseudo-source (see
        :data:`src.decode.dids.DID_SOURCE`), so it deliberately never
        becomes a ``TelemetryRecord`` that could reach storage — the raw
        poll stays the persisted truth and preprocessing re-decodes it.
        """
        return decode_raw_did_reading(self._did_by_hex, record.channel, record.value)

    def _apply_did_decode(self, record: TelemetryRecord) -> None:
        """Mirror ``decode_did_rows``: decode a raw ``DID_<hex>`` reading into
        a ``did:<NAME>`` current value."""
        decoded = self.decode_did_for_telemetry(record)
        if decoded is None:
            return
        self._set_value(
            channel_key(DID_SOURCE, decoded.entry.name), decoded.value, record.timestamp
        )

    # ------------------------------------------------------------------
    # tick — emit one feature row
    # ------------------------------------------------------------------

    def tick(self, now: datetime) -> pd.DataFrame | None:
        """Emit one feature row at ``now``.

        Returns a one-row ``DataFrame`` (so callers can index columns by
        name) or ``None`` when there's nothing meaningful to emit — no
        observations yet, or (1.4) every gap-tracked channel is stale past
        its ffill limit, mirroring ``_drop_fully_stale_rows``'s training-side
        rule: a tick with no fresh observation at all is a synthesized fill,
        not real telemetry, and scoring it would train/serve-skew every
        downstream detector. The returned frame's index is reset; row index
        0 is the only row.
        """
        if not self._current_values:
            self.latest_operating_point = None
            return None
        now_utc = now if now.tzinfo is not None else now.replace(tzinfo=UTC)
        gap_flags = self._gap_flags(now_utc)
        if gap_flags and all(gap_flags.values()):
            self.latest_operating_point = None
            return None
        # Snapshot — copy so a later observation can't mutate this tick's row.
        snapshot: dict[str, Any] = {
            "timestamp": pd.Timestamp(now_utc),
            "session": self.session,
            # Bounded ffill (1.1): a channel past its own class's max_gap
            # reads NaN here, same as the resampler NaNs the ffilled cell
            # offline, rather than carrying the stale value forever.
            **self._bounded_values(gap_flags),
            # Per-channel ``__gap`` flags mirror the resampler (1.2 — every
            # channel is now gap-tracked, fast and slow) so the GRU-D serve
            # path derives mask (¬gap) and Δt (age) identically to training.
            **gap_flags,
        }
        self._buffer.append(snapshot)

        wide = pd.DataFrame(list(self._buffer))
        wide["timestamp"] = pd.to_datetime(wide["timestamp"], utc=True)

        # Identical transform to the offline batch path — same
        # segment_or_constant → append_deviation_columns → compute_features —
        # via the one shared builder so the two can never schema-diverge by
        # hand (7.1). The segmenter falls back to a constant "all" mode when
        # the operating-mode signal columns aren't present yet, matching the
        # offline pipeline's behavior on no-OBD captures.
        result = featurize_wide_frame(
            wide,
            self.config,
            behavior_models=self.behavior_models,
            feature_config=self._feature_config,
        )
        self.last_segmenter_fallback = result.segmenter_fell_back

        # Stash the row the behavior decoder consumes (raw input channels +
        # operating_mode + deviation:*) from the pre-feature operating frame,
        # so the live overlay still updates on ticks where compute_features
        # yields nothing yet (warmup window not full).
        self.latest_operating_point = {
            str(k): v for k, v in result.operating_frame.iloc[-1].to_dict().items()
        }
        # Session-scoped trip integrals (Task 1.3) — accumulated here, not
        # via a cumsum over ``framed``/``featured`` (those are windows of the
        # ring buffer, not the whole session; see the docstring on
        # ``_trip_distance_km`` above).
        self.latest_derived_channels = self._accumulate_derived_integrals(
            now_utc, self.latest_operating_point
        )

        featured = result.features
        if featured.empty:
            return None
        return featured.iloc[[-1]].reset_index(drop=True)

    def _accumulate_derived_integrals(
        self, now: datetime, point: dict[str, Any]
    ) -> dict[str, float]:
        """Update the session-scoped trip integrals from one tick's operating row.

        Mirrors ``compute_derived_channels``'s cumulative math (same
        ``MAX_INTEGRATION_DT_S`` cap, same idle gate, same trapezoid between
        consecutive *observed* samples) but accumulates incrementally across
        ticks instead of ``cumsum``-ing a frame, so ring-buffer eviction never
        resets a running total mid-session. Each integrand keeps its own
        last-observed marker: a tick where that input is stale/absent leaves
        the marker alone, so the elapsed time is bridged (capped) at the next
        fresh sample instead of being silently dropped — matching the batch
        integral's valid-sample dt.
        """
        now_utc = now if now.tzinfo is not None else now.replace(tzinfo=UTC)

        def _accrue(
            marker: tuple[datetime, float] | None, value: float
        ) -> tuple[float, tuple[datetime, float]]:
            """Trapezoid increment from the marker to ``(now, value)``."""
            if marker is None:
                return 0.0, (now_utc, value)
            seen_at, seen_value = marker
            dt = min(max((now_utc - seen_at).total_seconds(), 0.0), MAX_INTEGRATION_DT_S)
            return (value + seen_value) / 2.0 * dt, (now_utc, value)

        speed = _first_finite(
            point, channel_key(OBD_SOURCE, "SPEED"), channel_key(DECODED_SOURCE, "VEHICLE_SPEED")
        )
        rpm = _first_finite(
            point, channel_key(OBD_SOURCE, "RPM"), channel_key(DECODED_SOURCE, "ECM_RPM")
        )
        fuel_lph = _first_finite(point, "derived:FUEL_RATE_LPH")

        if fuel_lph is not None:
            increment, self._fuel_marker = _accrue(self._fuel_marker, fuel_lph / 3600.0)
            self._fuel_used_l += increment
            fuel = fuel_properties(self.config.vehicle.parameters)
            self._co2_kg = self._fuel_used_l * fuel.co2_kg_per_l
        if speed is not None:
            increment, self._speed_marker = _accrue(self._speed_marker, speed / 3600.0)
            self._trip_distance_km += increment
        if rpm is not None and speed is not None:
            idling = 1.0 if (rpm > IDLE_RPM_FLOOR and speed < IDLE_SPEED_CEIL_KPH) else 0.0
            increment, self._idle_marker = _accrue(self._idle_marker, idling)
            self._idle_time_s += increment

        avg_economy = float("nan")
        if self._trip_distance_km > 0:
            avg_economy = self._fuel_used_l / self._trip_distance_km * 100.0

        return {
            "derived:TRIP_DISTANCE_KM": self._trip_distance_km,
            "derived:FUEL_USED_L": self._fuel_used_l,
            "derived:CO2_KG": self._co2_kg,
            "derived:IDLE_TIME_S": self._idle_time_s,
            "derived:AVG_ECONOMY_L_PER_100KM": avg_economy,
        }

    def _gap_flags(self, now: datetime) -> dict[str, bool]:
        """Per-channel ``<channel>__gap`` flags for the current tick.

        Mirrors ``resampler._gap_flags`` post-1.2: every channel is stale
        (``True``) when the seconds since its last observation exceed its
        class's ceiling — fast channels use ``_max_gap_seconds``, slow
        channels (``is_slow_channel``) their own, more generous
        ``_slow_max_gap_seconds``.
        """
        flags: dict[str, bool] = {}
        for channel in self._current_values:
            max_gap = (
                self._slow_max_gap_seconds if is_slow_channel(channel) else self._max_gap_seconds
            )
            observed_at = self._last_observed_at.get(channel)
            if observed_at is None:
                flags[gap_column(channel)] = True
                continue
            age = (now - observed_at).total_seconds()
            flags[gap_column(channel)] = age > max_gap
        return flags

    def _bounded_values(self, gap_flags: dict[str, bool]) -> dict[str, float]:
        """``_current_values`` with each stale (gap-flagged) channel NaN'd out.

        Mirrors the resampler's bounded ffill (1.1): a channel doesn't stay
        pinned at its last real reading forever, it goes NaN once its age
        exceeds the class-appropriate ceiling — same rows ``_gap_flags``
        already marks.
        """
        return {
            channel: (float("nan") if gap_flags.get(gap_column(channel)) else value)
            for channel, value in self._current_values.items()
        }

    def vehicle_state_inputs(self, now: datetime) -> dict[str, float | None]:
        """Cheap in-memory inputs for ``classify_vehicle_state`` (1.5).

        No Parquet I/O, safe to call every inference tick — unlike
        ``build_status``'s ``_derive_vehicle_state``, which re-reads the
        latest session for the dashboard's polling cadence. Freshness
        mirrors that function's per-source-family logic: OBD freshness is
        the youngest ``obd:*`` observation age; CAN freshness is the
        youngest ``decoded:*``/``dbc:*`` observation age (this source never
        stores a raw ``can:*`` value — every CAN frame that matched a
        binding or DBC message updates one of those instead).
        """
        now_utc = now if now.tzinfo is not None else now.replace(tzinfo=UTC)

        def _freshest_age(prefix: str) -> float | None:
            ages = [
                (now_utc - ts).total_seconds()
                for channel, ts in self._last_observed_at.items()
                if channel.startswith(prefix)
            ]
            return min(ages) if ages else None

        can_ages = (
            age
            for age in (
                _freshest_age(f"{DECODED_SOURCE}{SOURCE_SEP}"),
                _freshest_age(f"{DBC_SOURCE}{SOURCE_SEP}"),
            )
            if age is not None
        )
        return {
            "rpm": self._current_values.get(channel_key(OBD_SOURCE, "RPM")),
            "coolant_temp": self._current_values.get(channel_key(OBD_SOURCE, "COOLANT_TEMP")),
            "run_time": self._current_values.get(channel_key(OBD_SOURCE, "RUN_TIME")),
            "obd_freshness_seconds": _freshest_age(f"{OBD_SOURCE}{SOURCE_SEP}"),
            "can_freshness_seconds": min(can_ages, default=None),
        }

    def channel_age_seconds(self, channel: str, now: datetime) -> float | None:
        """Seconds since ``channel`` was last observed (Δt), or ``None``.

        Used by the GRU-D serve adapter to build the per-cell Δt input. ``now``
        is normalized to tz-aware UTC to match the stored observation stamps.
        """
        observed_at = self._last_observed_at.get(channel)
        if observed_at is None:
            return None
        now_utc = now if now.tzinfo is not None else now.replace(tzinfo=UTC)
        return max((now_utc - observed_at).total_seconds(), 0.0)

    def reset(self) -> None:
        """Clear all observed state (Phase 7.1 replay isolation boundary).

        Called on both entry to and exit from replay mode so a live drive's
        buffer/current-values never bleed into a replay run's inference, and
        a replay's tail state never bleeds back into live once it stops.
        Config/bindings/segmenter setup (immutable per source) are untouched,
        so the source is immediately usable again after this call — it just
        starts from an empty history, same as a freshly constructed one.
        """
        self._current_values = {}
        self._last_observed_at = {}
        self._reject_counts = {}
        self._buffer = deque(maxlen=self._max_rows)
        self.latest_operating_point = None
        self.last_segmenter_fallback = False
        # Trip integrals are session-scoped (Task 1.3) — zero them on the
        # same boundary as everything else so a new session (or a replay
        # run) never inherits the previous session's running totals.
        self._trip_distance_km = 0.0
        self._fuel_used_l = 0.0
        self._co2_kg = 0.0
        self._idle_time_s = 0.0
        self._speed_marker = None
        self._fuel_marker = None
        self._idle_marker = None
        self.latest_derived_channels = None

    # ------------------------------------------------------------------
    # Introspection
    # ------------------------------------------------------------------

    @property
    def buffer_size(self) -> int:
        return len(self._buffer)

    @property
    def known_channels(self) -> tuple[str, ...]:
        """Channels we've observed at least once. Stable enough for telemetry."""
        return tuple(sorted(self._current_values))

    @property
    def reject_counts(self) -> dict[str, int]:
        """Per-channel quality-gate reject counts (Task 1.2) since the last
        :meth:`reset`. A copy — callers can't mutate the running tally."""
        return dict(self._reject_counts)


# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------


def _to_float(value: Any) -> float | None:
    if value is None or isinstance(value, bool):
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _first_finite(point: dict[str, Any], *keys: str) -> float | None:
    """First present, non-NaN value among ``keys`` in a tick's operating row.

    Mirrors ``derived._series``'s "prefer OBD, fall back to decoded" lookup,
    but over a single-row dict (``LiveFeatureSource.latest_operating_point``)
    rather than a DataFrame column.
    """
    for key in keys:
        if key not in point:
            continue
        value = _to_float(point[key])
        if value is not None and not math.isnan(value):
            return value
    return None


def _decode_signal_raw(signal: Signal, payload: bytes) -> int | None:
    end = signal.byte_start + signal.byte_length
    if len(payload) < end:
        return None
    return int.from_bytes(payload[signal.byte_start : end], byteorder=signal.byte_order)


__all__ = ["LiveFeatureSource"]
