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
from collections import deque
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any

import pandas as pd

from src.behavior.accepted_baselines import AcceptedBaselines
from src.behavior.behavior_models import BehaviorModels
from src.behavior.session_context import (
    EngineStartTracker,
    StartContextSpec,
    start_context_specs,
)
from src.can_ids import parse_can_channel
from src.coerce import finite_float
from src.columns import arrived_column, gap_column
from src.config import CanaryConfig, require_profile_scoped
from src.decode.bindings import Bindings
from src.decode.dids import (
    DID_SOURCE,
    DecodedDid,
    DidEntry,
    DidMap,
    decode_raw_did_reading,
)
from src.decode.engineering import physical_value
from src.decode.frame_decoder import FrameDecoder
from src.features.feature_pipeline import FeaturizedFrame, featurize_wide_frame
from src.preprocessing.features import FeatureConfig
from src.preprocessing.imu_live import IMU_FRAME_INPUTS, LiveImuFrame
from src.preprocessing.quality_gate import is_physically_valid
from src.preprocessing.resampler import ResampleConfig, resample_config_for
from src.signals.afr import FlashHistory, RecordedClockFlashHistory
from src.signals.derived import (
    IDLE_RPM_FLOOR,
    IDLE_SPEED_CEIL_KPH,
    MAX_INTEGRATION_DT_S,
    fuel_properties,
)
from src.storage.broadcast_storage import CanChangeFilter, is_broadcast_channel
from src.telemetry import (
    DBC_SOURCE,
    DECODED_SOURCE,
    GPS_SOURCE,
    IMU_SOURCE,
    OBD_SOURCE,
    REPLAY_SOURCE_AT_KEY,
    SOURCE_SEP,
    TelemetryRecord,
    channel_key,
)
from src.vehicle.profiles import active_flash_history

log = logging.getLogger(__name__)


@dataclass(frozen=True)
class PreparedLiveTick:
    """Immutable input to the expensive, pure live feature transform.

    Constructed on the event loop after one telemetry snapshot is appended.
    It captures the exact config/model references for this tick so a hot reload
    cannot make preparation and computation observe different generations.
    """

    now: datetime
    wide: pd.DataFrame
    config: CanaryConfig
    behavior_models: BehaviorModels | None
    feature_config: FeatureConfig
    generation: int
    #: The DID catalog this source decodes ``DID_<hex>`` polls with: the
    #: featurizer judges DID views and the load-axis unit against it (P02.14).
    #: About 68 KB pickled, 0.6 ms a tick across the process boundary.
    did_map: DidMap | None
    #: P02.16 — the flash history ``derived:TARGET_AFR`` resolves each row's
    #: revision from: the vehicle's (``active_flash_history``), read at each
    #: row's *recorded* instant when the rows came from a replay
    #: (:class:`~src.signals.afr.RecordedClockFlashHistory`). No default, so
    #: the worker cannot featurize a replayed tick against today's flash.
    flash_history: FlashHistory


@dataclass(frozen=True)
class BuiltLiveTick:
    """Compact result returned by the feature worker process."""

    feature_row: pd.DataFrame | None
    operating_point: dict[str, Any]
    segmenter_fell_back: bool


def build_prepared_live_tick(prepared: PreparedLiveTick) -> BuiltLiveTick:
    """Run the train-identical transform, returning only its newest rows.

    Module-level and side-effect-free so ``spawn`` can import and execute it
    in a persistent process. The full rolling frames never cross back to the
    daemon; only the two newest rows needed by inference and behavior do.
    """
    # Start evidence outlives the ring buffer: ``prepare_tick`` already wrote
    # the source's tracked ``context:*`` values into ``wide``, so the behavior
    # decoder uses them instead of re-folding this short window (B01.5).
    result: FeaturizedFrame = featurize_wide_frame(
        prepared.wide,
        prepared.config,
        behavior_models=prepared.behavior_models,
        did_map=prepared.did_map,
        feature_config=prepared.feature_config,
        flash_history=prepared.flash_history,
    )
    operating_point = {
        str(key): value for key, value in result.operating_frame.iloc[-1].to_dict().items()
    }
    feature_row = (
        None if result.features.empty else result.features.iloc[[-1]].reset_index(drop=True)
    )
    return BuiltLiveTick(
        feature_row=feature_row,
        operating_point=operating_point,
        segmenter_fell_back=result.segmenter_fell_back,
    )


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
    #: A05 — called with every arrived value (``channel, value, observed_at``)
    #: *before* the quality gate, so a plausibility check sees the raw sample
    #: the gate would drop. ``None`` (the default) costs one attribute test.
    sample_observer: Callable[[str, float, datetime], None] | None = None
    #: B06.4 — called with every record :meth:`observe` receives, before any
    #: decode, so a consumer that pairs reads on their own acquisition clock
    #: (the lambda-delivery readout) sees each read — a fuel-cut sentinel's
    #: text value included — rather than the latest numeric per channel. A
    #: list, so one consumer cannot displace another.
    record_observers: list[Callable[[TelemetryRecord], None]] = field(default_factory=list)
    #: B07.7 — the recording the rows are being written to, read each tick
    #: (the daemon passes its writer's directory). The batch fold ends a start
    #: at every recording, and a writer rotation inside one process (a capture
    #: arming or disarming, a record gap, an engine-run reset) is a recording
    #: boundary the live source would otherwise never see. ``None`` (replay
    #: tools, tests) leaves the trackers to :meth:`reset` alone.
    session_identity: Callable[[], object] | None = None

    _current_values: dict[str, float] = field(default_factory=dict)
    #: Per-channel wall-clock timestamp of the last fresh ``observe()``. GRU-D
    #: serve derives each channel's mask (``age ≤ max_gap``) and Δt (``age``)
    #: from this, mirroring the resampler's ``__gap`` rule with no resampler.
    _last_observed_at: dict[str, datetime] = field(default_factory=dict)
    #: A03.1 — ``now`` of the previous :meth:`prepare_tick` call (emitted or
    #: not). A channel's ``__arrived`` flag is set when its newest accepted
    #: observation was stamped after this: the cell's value was delivered
    #: since the previous tick, the live twin of the resampler's
    #: ``(t - interval, t]`` rule.
    _last_tick_at: datetime | None = field(init=False, default=None)
    #: Per-channel count of values rejected by the quality gate (Task 1.2) —
    #: never silently swallowed, surfaced via :attr:`reject_counts`. Cleared
    #: on :meth:`reset` like every other observation-derived state.
    _reject_counts: dict[str, int] = field(default_factory=dict)
    _buffer: deque[dict[str, Any]] = field(init=False)
    #: P02.16 — per ``_buffer`` row: its stamp (epoch ns) and how far that
    #: stamp is ahead of the instant the data was recorded (``None`` for live
    #: data). Replay rebases rows to wall-clock; this maps them back so the
    #: target map is the revision flashed when the session was recorded.
    _buffer_clock: deque[tuple[int, float | None]] = field(init=False)
    #: Newest replayed record's emit-minus-recorded offset (seconds), from
    #: :data:`~src.telemetry.REPLAY_SOURCE_AT_KEY`; ``None`` until a replayed
    #: record arrives, and again after :meth:`reset` (the replay boundary).
    _replay_shift_s: float | None = field(init=False, default=None)
    #: R04.4 — the storage sink's decoder over every confidence, so the
    #: ``decoded:`` record this source publishes (and observes) is the record
    #: storage writes. ``None`` without bindings.
    _frame_decoder: FrameDecoder | None = field(init=False, default=None)
    _dbc_message_by_id: dict[int, Any] = field(init=False)
    _did_by_hex: dict[str, DidEntry] = field(init=False)
    _feature_config: FeatureConfig = field(init=False)
    _max_rows: int = field(init=False)
    #: Resampler ``max_gap`` in seconds — the staleness cutoff that flips a
    #: channel's mask to 0. Mirrors ``resample_config_for(config).max_gap`` so train and
    #: serve agree on what "stale" means for the same observation history.
    _max_gap_seconds: float = field(init=False)
    #: Slow-channel counterpart to ``_max_gap_seconds`` (1.1/1.2) — mirrors
    #: ``ResampleConfig.slow_channel_max_gap``, generous relative to the fast
    #: threshold since slow sensors (coolant/IAT/baro) legitimately update
    #: far less often.
    _slow_max_gap_seconds: float = field(init=False)
    #: R04.7 — the training recipe's :class:`ResampleConfig`; its
    #: ``max_gap_seconds_for`` is the one fast/slow rule. Memoised per channel
    #: (``_max_gap_by_channel``) because ``_gap_flags`` runs every tick.
    _resample_config: ResampleConfig = field(init=False)
    _max_gap_by_channel: dict[str, float] = field(init=False, default_factory=dict)
    #: P02.10 — the storage writer's change filter over the CAN frames this
    #: source observes, with the same timeout, reset on the same replay/session
    #: boundary as the rest of this source's state.
    _can_filter: CanChangeFilter = field(init=False)
    #: Newest raw CAN frame that decodes to a binding or DBC signal, kept by
    #: the filter or not: ``can_freshness_seconds`` reads it, because freshness
    #: is about the bus, not about what storage keeps.
    _last_broadcast_frame_at: datetime | None = field(init=False, default=None)
    _decodable_can_ids: frozenset[int] = field(init=False, default=frozenset())
    #: Replay/session reset generation. A feature process can finish after a
    #: reset because the loop remains responsive while it works; tagging the
    #: snapshot makes that stale result impossible to apply to the new run.
    _generation: int = field(init=False, default=0)
    #: B01.5 — engine-start evidence per cold-start contract in the loaded
    #: behavior models, keyed by tracker spec. Created lazily (a model
    #: hot-reloaded mid-drive gets a tracker with no evidence, so it cannot
    #: qualify until a start is observed) and cleared on :meth:`reset`.
    _start_trackers: dict[StartContextSpec, EngineStartTracker] = field(
        init=False, default_factory=dict
    )
    #: The recording the trackers' evidence belongs to (B07.7).
    _start_context_recording: object = field(init=False, default=None)
    #: H02.4 — raw ``imu:ACCEL_X/Y/Z`` → per-tick ``imu:ACCEL_LON_MAX``-style
    #: aggregates (what ``LiveAutoTagHook`` and the segmenter's accel modes
    #: read). Idle, and emitting nothing, while no IMU records arrive.
    imu_frame: LiveImuFrame = field(init=False)

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
        # At construction, not first tick: an unscoped config would featurize
        # without the vehicle's afr_calibration (P02.11).
        require_profile_scoped(self.config, reader="LiveFeatureSource")
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
        self._buffer_clock = deque(maxlen=self._max_rows)
        self._feature_config = FeatureConfig(window_seconds=window_s)
        # Mirror the resampler's max_gap (interval × max_gap_factor) used at
        # training time, so a channel reads stale at serve exactly when its
        # ``__gap`` flag would have been set offline.
        resample_config = resample_config_for(self.config)
        self._resample_config = resample_config
        self._can_filter = CanChangeFilter(self.config.collection.broadcast_stale_timeout_seconds)
        self._max_gap_seconds = float(resample_config.max_gap.total_seconds())
        self._slow_max_gap_seconds = float(resample_config.slow_channel_max_gap.total_seconds())
        # The storage sink's decoder (pre-indexed by CAN ID, so decode is
        # O(signals per ID)), over every confidence: the dashboard and the
        # features see every binding, storage persists a filtered subset.
        self._frame_decoder = (
            FrameDecoder.from_bindings(self.bindings) if self.bindings is not None else None
        )
        self.imu_frame = LiveImuFrame(bindings=self.bindings)
        # Same idea for the DBC database: frame_id → message so a per-frame
        # decode is an O(1) lookup, not a scan of every message.
        self._dbc_message_by_id = {}
        if self.dbc_database is not None:
            for msg in self.dbc_database.messages:
                self._dbc_message_by_id[msg.frame_id] = msg
        # DID map indexed by 4-char hex so a raw "DID_1208" reading resolves
        # to its DidEntry in O(1).
        self._decodable_can_ids = frozenset(self._dbc_message_by_id) | frozenset(
            signal.can_id for signal in (self.bindings.signals if self.bindings else ())
        )
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
        ``src.signals.fsm.PHYSICAL_LIMITS``) is never stored — the channel
        simply doesn't get a fresh observation this tick, so it ages out via
        the exact same staleness path (``_last_observed_at`` / ``_gap_flags``)
        a channel that stopped transmitting would. Counted, never swallowed.
        """
        if self.sample_observer is not None:
            self.sample_observer(channel, value, observed_at)
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
        if channel in IMU_FRAME_INPUTS:
            self.imu_frame.observe_input(channel, value, ts)

    def observe(self, record: TelemetryRecord) -> None:
        """Apply one ``TelemetryRecord`` to the running current-values dict.

        Side-effects only. Emitting a wide row is a separate tick.
        """
        for record_observer in self.record_observers:
            record_observer(record)
        recorded_at = record.metadata.get(REPLAY_SOURCE_AT_KEY)
        if isinstance(recorded_at, (int, float)) and not isinstance(recorded_at, bool):
            stamped = record.timestamp
            if stamped.tzinfo is None:
                stamped = stamped.replace(tzinfo=UTC)
            self._replay_shift_s = stamped.timestamp() - float(recorded_at)
        if record.source == "obd":
            # Raw Mode-22 DID poll responses arrive as obd-source records with
            # channel "DID_<hex>" and a hex-string payload value. Decode them
            # into named ``did:<NAME>`` numerics so live inference + the gauge
            # see the engineering value, not an undecodable hex string. Mirror
            # of decode_did_rows on the offline path.
            if self._did_by_hex and record.channel.startswith("DID_"):
                self._apply_did_decode(record)
                return
            value = finite_float(record.value)
            if value is not None:
                self._set_value(channel_key(OBD_SOURCE, record.channel), value, record.timestamp)
            return
        if record.source == "can":
            # Freshness reads the raw stream: every frame that decodes, kept
            # or not, is the bus being alive (P02.10).
            if self._decodes_frame(record):
                ts = record.timestamp
                self._last_broadcast_frame_at = (
                    ts if ts.tzinfo is not None else ts.replace(tzinfo=UTC)
                )
            # The features read the stream storage keeps (P02.10): a frame
            # repeating its id's payload inside the re-write interval is
            # dropped here exactly as the storage writer drops it, so live,
            # stored and replayed features see the same frames.
            if self._can_filter.keep_record(record):
                self._apply_can_decode(record)
            return
        if record.source == "gps":
            value = finite_float(record.value)
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
            value = finite_float(record.value)
            if value is not None:
                self._set_value(channel_key(IMU_SOURCE, record.channel), value, record.timestamp)
                # H02.4 — the frame resolver turns raw axes into the per-tick
                # imu:ACCEL_LON_MAX/... aggregates prepare_tick publishes.
                self.imu_frame.observe_imu(record.channel, value, record.timestamp)
            return
        # Anything else (e.g. a future source pushed directly) gets stored
        # as-is; the pipeline will surface it as a new column.
        value = finite_float(record.value)
        if value is not None:
            self._set_value(channel_key(record.source, record.channel), value, record.timestamp)

    def decode_for_telemetry(self, record: TelemetryRecord) -> tuple[TelemetryRecord, ...]:
        """Return decoded signal rows for a raw CAN record.

        This is the dashboard-facing counterpart to ``observe``'s CAN decode:
        inference wants the values in ``_current_values``; the UI also needs
        named ``decoded:<SIGNAL>`` rows so it can render gauges instead of raw
        arbitration IDs. The rows are :meth:`FrameDecoder.decode`'s — the same
        records the storage sink persists (R04.4) — over every binding.
        """
        if self._frame_decoder is None:
            return ()
        return self._frame_decoder.decode(record)

    def _decodes_frame(self, record: TelemetryRecord) -> bool:
        """Whether a raw CAN frame's id has a binding or a DBC message."""
        if not self._decodable_can_ids:
            return False
        can_id = parse_can_channel(record.channel)
        return can_id is not None and can_id in self._decodable_can_ids

    def _apply_can_decode(self, record: TelemetryRecord) -> None:
        for decoded in self.decode_for_telemetry(record):
            # Raw integer matches decode_can_rows' policy — the scaler
            # normalizes downstream and most bindings have no pinned scale.
            value = finite_float(decoded.value)
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
        return decode_raw_did_reading(
            self._did_by_hex, record.channel, record.value, source=record.source
        )

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
        prepared = self.prepare_tick(now)
        if prepared is None:
            return None
        return self.apply_built_tick(prepared, build_prepared_live_tick(prepared))

    def prepare_tick(self, now: datetime) -> PreparedLiveTick | None:
        """Snapshot loop-owned state and append one row to the ring buffer.

        This is deliberately cheap and contains every mutation that must stay
        serialized with :meth:`observe`. The returned value is self-contained
        and safe to pickle to the persistent feature process.
        """
        now_utc = now if now.tzinfo is not None else now.replace(tzinfo=UTC)
        previous_tick = self._last_tick_at
        # Every tick call closes an arrival window, emitted or not, exactly as
        # the offline grid has a row per interval before fully-stale rows drop.
        self._last_tick_at = now_utc
        # H02.4 — this tick's decimated IMU aggregates, stamped at their
        # newest sample so a tick without IMU samples ages them out.
        imu_values, imu_observed_at = self.imu_frame.take_tick()
        for channel, value in imu_values.items():
            self._set_value(channel, value, imu_observed_at or now_utc)
        if not self._current_values:
            self.latest_operating_point = None
            return None
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
            # A03.1 — per-channel "a sample arrived this tick" flags; the
            # sequence mask of an artifact trained on them reads these.
            **self._arrival_flags(previous_tick, now_utc),
        }
        snapshot.update(self._start_context(snapshot, now_utc))
        self._buffer.append(snapshot)
        self._buffer_clock.append((pd.Timestamp(now_utc).value, self._replay_shift_s))

        wide = pd.DataFrame(list(self._buffer))
        wide["timestamp"] = pd.to_datetime(wide["timestamp"], utc=True)
        return PreparedLiveTick(
            now=now_utc,
            wide=wide,
            config=self.config,
            behavior_models=self.behavior_models,
            feature_config=self._feature_config,
            generation=self._generation,
            did_map=self.did_map,
            flash_history=self._tick_flash_history(),
        )

    def _tick_flash_history(self) -> FlashHistory:
        """The vehicle's flash history, read at each buffered row's recorded
        instant when any row came from a replay (P02.16). Live rows are
        stamped now, so live reads the revision flashed now."""
        history = active_flash_history(self.config)
        if all(shift is None for _stamp, shift in self._buffer_clock):
            return history
        return RecordedClockFlashHistory(
            base=history,
            stamped_ns=tuple(stamp for stamp, _shift in self._buffer_clock),
            shift_s=tuple(0.0 if shift is None else shift for _stamp, shift in self._buffer_clock),
        )

    def apply_built_tick(
        self, prepared: PreparedLiveTick, built: BuiltLiveTick
    ) -> pd.DataFrame | None:
        """Apply a worker result to loop-owned state and return its feature row."""
        if prepared.generation != self._generation:
            log.info(
                "discarding live feature result from generation %d; current generation is %d",
                prepared.generation,
                self._generation,
            )
            return None
        self.last_segmenter_fallback = built.segmenter_fell_back

        # Stash the row the behavior decoder consumes (raw input channels +
        # operating_mode + deviation:*) from the pre-feature operating frame,
        # so the live overlay still updates on ticks where compute_features
        # yields nothing yet (warmup window not full).
        self.latest_operating_point = built.operating_point
        # Session-scoped trip integrals (Task 1.3) — accumulated here, not
        # via a cumsum over ``framed``/``featured`` (those are windows of the
        # ring buffer, not the whole session; see the docstring on
        # ``_trip_distance_km`` above).
        self.latest_derived_channels = self._accumulate_derived_integrals(
            prepared.now, self.latest_operating_point
        )
        return built.feature_row

    def _start_context(self, snapshot: dict[str, Any], now: datetime) -> dict[str, float]:
        """Advance each cold-start tracker by this tick (B01.5).

        Fed the same bounded (stale → NaN) values the batch fold sees on a
        resampled row, so live, replay and batch reach the same evidence.
        """
        self._end_start_context_at_a_new_recording()
        specs = start_context_specs(self.behavior_models)
        live = set(specs)
        for stale_spec in [spec for spec in self._start_trackers if spec not in live]:
            del self._start_trackers[stale_spec]
        out: dict[str, float] = {}
        for spec in specs:
            tracker = self._start_trackers.get(spec)
            if tracker is None:
                tracker = EngineStartTracker(spec)
                self._start_trackers[spec] = tracker
            observed, start_coolant = tracker.update(
                snapshot.get(spec.rpm_channel),
                snapshot.get(spec.runtime_channel),
                snapshot.get(spec.coolant_channel),
                now.timestamp(),
            )
            out[spec.observed_column] = observed
            out[spec.start_coolant_column] = start_coolant
        return out

    def _end_start_context_at_a_new_recording(self) -> None:
        """Forget start evidence when the writer has moved to a new recording.

        Batch cannot tell a daemon restart (whose fresh source has no
        evidence) from a rotation inside one process: both are just a new
        directory. So the start ends at every recording on both paths, the
        conservative side: a warmup model refuses
        ``cold_start_no_start_evidence`` for the rest of that run rather than
        live serving rows the evaluation refuses.
        """
        if self.session_identity is None:
            return
        current = self.session_identity()
        if current is None or current == self._start_context_recording:
            return
        if self._start_context_recording is not None:
            self._start_trackers = {}
        self._start_context_recording = current

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

        # Same candidates as ``compute_derived_channels`` (``derived._series``).
        # ``_first_finite`` scales a ``decoded:`` fallback to engineering units
        # — the broadcast rpm is 8 counts/rpm, and before 2026-09-18 its raw
        # count went straight into the 200 rpm idle gate.
        speed = _first_finite(point, channel_key(OBD_SOURCE, "SPEED"), bindings=self.bindings)
        rpm = _first_finite(
            point,
            channel_key(OBD_SOURCE, "RPM"),
            channel_key(DECODED_SOURCE, "ECM_RPM"),
            bindings=self.bindings,
        )
        fuel_lph = _first_finite(point, "derived:FUEL_RATE_LPH", bindings=self.bindings)

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
        class's ceiling, read from the recipe's
        ``ResampleConfig.max_gap_seconds_for`` — the resampler's own rule,
        explicit ``fast_channels`` included (R04.7).
        """
        flags: dict[str, bool] = {}
        for channel in self._current_values:
            max_gap = self._max_gap_seconds_for(channel)
            observed_at = self._last_observed_at.get(channel)
            if observed_at is None:
                flags[gap_column(channel)] = True
                continue
            age = (now - observed_at).total_seconds()
            flags[gap_column(channel)] = age > max_gap
        return flags

    def _max_gap_seconds_for(self, channel: str) -> float:
        """The recipe's stale ceiling for ``channel``, memoised (pure per channel)."""
        cached = self._max_gap_by_channel.get(channel)
        if cached is None:
            cached = self._resample_config.max_gap_seconds_for(channel)
            self._max_gap_by_channel[channel] = cached
        return cached

    def _arrival_flags(
        self, previous_tick: datetime | None, now: datetime | None = None
    ) -> dict[str, bool]:
        """Per-channel ``<channel>__arrived`` flags (A03.1).

        ``True`` when the channel's newest accepted observation was stamped
        after the previous tick: the value in this row's cell was delivered
        since then. On the first tick after a reset every observed channel has
        arrived. Quality-gate rejects never stamp, so they never arrive — as
        offline, where the gate NaNs them before the resampler sees them.

        A broadcast channel (P02.10) mirrors the resampler's broadcast rule
        instead: it arrived when its newest observation is at most the
        broadcast stale timeout old at ``now``, because this source sees the
        change-only stream storage keeps, where an unchanged frame is not
        repeated.
        """
        flags: dict[str, bool] = {}
        broadcast_timeout = self._resample_config.broadcast_max_gap_seconds
        for channel in self._current_values:
            observed_at = self._last_observed_at.get(channel)
            if observed_at is not None and now is not None and is_broadcast_channel(channel):
                flags[arrived_column(channel)] = (
                    now - observed_at
                ).total_seconds() <= broadcast_timeout
                continue
            flags[arrived_column(channel)] = observed_at is not None and (
                previous_tick is None or observed_at > previous_tick
            )
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

        # The raw frame stamp, not the decoded channels' observation ages:
        # those follow the change-only stream (P02.10), where an unchanged id
        # is observed once per re-write interval.
        frame_age = (
            (now_utc - self._last_broadcast_frame_at).total_seconds()
            if self._last_broadcast_frame_at is not None
            else None
        )
        can_ages = (
            age
            for age in (
                frame_age,
                _freshest_age(f"{DECODED_SOURCE}{SOURCE_SEP}"),
                _freshest_age(f"{DBC_SOURCE}{SOURCE_SEP}"),
            )
            if age is not None
        )
        return {
            "rpm": self._current_values.get(channel_key(OBD_SOURCE, "RPM")),
            # Broadcast rpm in rpm (the stored ``decoded:`` value is raw counts).
            "broadcast_rpm": physical_value(
                self._current_values.get(channel_key(DECODED_SOURCE, "ECM_RPM")),
                channel_key(DECODED_SOURCE, "ECM_RPM"),
                self.bindings,
            ),
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
        self._generation += 1
        self._can_filter.reset()
        self._last_broadcast_frame_at = None
        self._current_values = {}
        self._last_observed_at = {}
        self._last_tick_at = None
        self._reject_counts = {}
        self._buffer = deque(maxlen=self._max_rows)
        self._buffer_clock = deque(maxlen=self._max_rows)
        self._replay_shift_s = None
        self.latest_operating_point = None
        self.last_segmenter_fallback = False
        # Replay entry/exit is a session boundary: no start evidence carries.
        self._start_trackers = {}
        # Nor does a gravity/yaw estimate (H02.4).
        self.imu_frame.reset()
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


__all__ = [
    "BuiltLiveTick",
    "LiveFeatureSource",
    "PreparedLiveTick",
    "build_prepared_live_tick",
]
