"""Configuration models for CANary.

Loads `canary.yaml` and validates it. Unknown keys raise rather than being
silently dropped, so typos in config don't become silent drift at runtime.
"""

from __future__ import annotations

from pathlib import Path
from typing import Literal, Self

import yaml
from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator


class _Strict(BaseModel):
    model_config = ConfigDict(extra="forbid")


#: Default ``CollectionConfig.obd_priority_channels`` — the OBD channels that
#: keep their configured poll rate when the request plan oversubscribes the
#: bus. Lives here rather than in ``collection.obd_scheduler`` (which imports
#: it) only to keep the config default and the scheduler constant from
#: drifting apart. See the scheduler's ``DEFAULT_PRIORITY_CHANNELS`` re-export
#: for why this channel specifically.
DEFAULT_PRIORITY_CHANNELS: tuple[str, ...] = ("RPM", "THROTTLE_POS")


class VehicleParameters(_Strict):
    """Physical parameters needed for derived trip/fuel/performance channels
    (Task 6.6). All optional with defaults so existing ``canary.yaml`` files
    keep loading; ``VehicleConfig.model_dump()`` carries them into every
    session manifest, so a trip's calculations stay reproducible from the
    captured data alone.
    """

    #: Curb/test mass in kg — used for acceleration-derived power sanity.
    mass_kg: float = 1900.0
    #: ``gasoline`` | ``diesel`` | ``e85`` — selects stoichiometric AFR, fuel
    #: density, and the CO2 emission factor.
    fuel_type: Literal["gasoline", "diesel", "e85"] = "gasoline"
    #: Engine displacement in litres (volumetric-efficiency fuel estimate).
    displacement_l: float = 4.0
    #: Volumetric efficiency (0–1.2) for the speed-density fallback when no
    #: MAF / fuel-rate PID is available.
    volumetric_efficiency: float = 0.85
    #: Driveline loss fraction (0–1) between crank and wheels for the
    #: estimated-power channel.
    drivetrain_loss_fraction: float = 0.15
    #: Brake thermal efficiency (0–1) for the fuel-energy power estimate.
    thermal_efficiency: float = 0.30
    #: Rolling tire diameter in metres (optional; speed cross-checks only).
    tire_diameter_m: float | None = None
    #: Combined gearbox × final-drive ratio per gear, lowest gear first
    #: (Task 4.6, ``derived:GEAR_ESTIMATE``: rpm/speed ratio clustered to the
    #: nearest entry). Empty by default — deliberately not baked in for any
    #: particular vehicle; a profile without a captured ratio table (e.g. no
    #: dyno/logged 1:1 gear runs yet) just never gets the gear channel, same
    #: as any other absent-input case elsewhere in ``derived.py``.
    gear_ratios: tuple[float, ...] = ()


class VehicleConfig(_Strict):
    make: str
    model: str
    year: int
    vin: str = ""
    parameters: VehicleParameters = VehicleParameters()


class CanBusConfig(_Strict):
    """How to construct the python-can `Bus`.

    `interface` is the python-can backend identifier:
      * `slcan`     — serial-attached adapter (Canable in slcan firmware, WSL dev)
      * `socketcan` — kernel SocketCAN netdev (Pi production)
      * `virtual`   — in-memory bus for tests and replay
    `channel` is the device path (`/dev/ttyACM0`) or netdev name (`can0`).
    `bitrate` is ignored by `virtual`.
    """

    interface: str
    channel: str
    bitrate: int


class GpsConfig(_Strict):
    """Serial NMEA GPS receiver for operating-mode detection.

    Optional and defaulted-disabled so configs without a GPS keep loading and
    behave exactly as before. When ``enabled``, the daemon runs a serial
    collector that parses NMEA 0183 (GGA/RMC/VTG) and emits ``gps:*`` telemetry
    records. ``gps:speed`` (km/h, matching ``obd:SPEED``) feeds the operating-
    mode segmenter; position/heading/quality are logged but excluded from model
    features (see ``feature_pipeline.NON_TRAINABLE_GPS_CHANNELS``).
    """

    enabled: bool = False
    #: Where fixes come from. ``serial`` reads NMEA off the receiver directly.
    #: ``gpsd`` reads gpsd's JSON socket instead, leaving the serial port to
    #: gpsd — which is what lets chrony discipline the clock from GPS with no
    #: network (see docs/time-sync.md). Both emit identical ``gps:*`` channels.
    #: Defaults to ``serial`` so existing configs and WSL dev are unaffected.
    source: Literal["serial", "gpsd"] = "serial"
    #: Serial device path (``source: serial`` only). Prefer the stable
    #: ``/dev/serial/by-id/...`` symlink over ``/dev/ttyUSB0``, which renumbers
    #: when USB devices re-enumerate.
    device: str = "/dev/ttyUSB0"
    baudrate: int = 9600
    #: Serial read timeout (seconds). Also bounds how quickly the collector
    #: notices a stop request while waiting for the next NMEA line. Reused as
    #: the socket timeout under ``source: gpsd``.
    read_timeout_seconds: float = 2.0
    #: gpsd endpoint (``source: gpsd`` only). gpsd runs on the same host as the
    #: daemon; there is no reason to point this off-box.
    gpsd_host: str = "127.0.0.1"
    gpsd_port: int = 2947


class SensorCanConfig(_Strict):
    """A second, private CAN interface carrying add-on sensors (not the vehicle bus).

    Disabled by default: a config without the hardware keeps loading and
    behaves exactly as before. When enabled, the daemon listens on
    ``channel`` and decodes the frames catalogued in ``signals`` into
    ``sensor:*`` records. It never transmits. First use: an AEM X-Series
    fuel-pressure gauge (30-0301) — 500 kbit/s, 29-bit IDs, 20 Hz — ahead of
    the supercharger build (``docs/roadmap/supercharger-build.md``).

    The gauge has no internal termination: a bus of just the Pi and the gauge
    needs 120 ohm at each end (the Pi's HAT jumper, and one at the gauge).
    """

    enabled: bool = False
    interface: str = "socketcan"
    channel: str = "can1"
    bitrate: int = 500_000
    signals: Path = Path("data/sensor_can_signals.yaml")


class ImuConfig(_Strict):
    """3-axis IMU (accel + gyro) for motion-aware auto-tagging.

    Phase 2a/2b (``docs/roadmap/imu-gps-integration.md``). Optional and
    defaulted-disabled, same "configs without it keep loading and behave
    exactly as before" contract as ``GpsConfig``. Unlike ``GpsConfig`` there
    is no separate ``enabled`` bool: the doc's config sketch folds "no
    hardware" into the same ``source`` Literal that names which chip is
    wired, since which-chip and whether-any-chip are the same decision here.
    ``.enabled`` is provided as a property so call sites can gate the same
    way as ``gps.enabled`` without a Literal-vs-bool special case.
    """

    #: bno055 does gravity removal + orientation fusion on-chip (see
    #: ``src.preprocessing.imu_frame``'s module docstring); mpu6050/icm20948
    #: are raw accel+gyro requiring this codebase's own gravity low-pass.
    #: ``disabled`` (default) — no IMU wired, daemon startup skips it exactly
    #: like ``gps.enabled=False`` skips the GPS collector.
    source: Literal["bno055", "mpu6050", "icm20948", "disabled"] = "disabled"
    i2c_bus: int = 1
    sample_rate_hz: float = 100.0
    #: ``auto`` resolves mount tilt dynamically via the gravity low-pass in
    #: ``imu_frame.py`` (the only correct option per the doc's physics
    #: section — a one-shot calibration is permanently biased by wherever
    #: the mount happened to sit at cal time). A fixed-quaternion string is
    #: accepted for a rigidly pre-surveyed mount but isn't consumed by any
    #: code yet. The BNO055 does this on-chip either way.
    mount_orientation: str = "auto"

    @property
    def enabled(self) -> bool:
        """Mirrors ``GpsConfig.enabled`` — ``False`` only when ``source == "disabled"``."""
        return self.source != "disabled"


class HardwareConfig(_Strict):
    elm327_port: str
    can: CanBusConfig
    gps: GpsConfig = GpsConfig()
    imu: ImuConfig = ImuConfig()
    sensor_can: SensorCanConfig = SensorCanConfig()


class RecordArmingConfig(_Strict):
    """Operator-intent gate deferring full-rate recording until the vehicle
    looks active (Task 8.6, R14 in ``docs/implementation-tasks.md``).

    **Ships disabled** — a hard default, matching the other config-gated
    background features in this file: current continuous-logging behavior
    (Done-Definition v1 #2) is unchanged byte-for-byte until an operator
    opts in. See ``docs/storage-lifecycle-fix.md`` for the arm/disarm
    design (why disarm only happens at a session-rotation boundary, and why
    pre-arm data is dropped rather than buffered).
    """

    enabled: bool = False
    #: Sources whose mere presence proves the vehicle bus is connected and
    #: talking — the deciding factor for recording. GPS/IMU/derived chatter
    #: whenever the Pi has power, so they never count. Set ``[]`` to gate on
    #: ``arm_channels`` values alone (e.g. a vehicle whose bus stays awake
    #: while parked).
    arm_sources: tuple[str, ...] = ("can", "obd")
    #: ``TelemetryRecord.channel`` names (unprefixed — e.g. ``"RPM"``) that
    #: can arm the gate when ``arm_sources`` doesn't already cover it.
    arm_channels: tuple[str, ...] = ("RPM",)
    #: A configured channel's numeric value must exceed this to arm.
    arm_threshold: float = 0.0
    #: Disarm after this many seconds of records with no arm-channel evidence
    #: (key out, adapter unplugged) — otherwise a post-drive parked night with
    #: the Pi on the UPS stays armed and records GPS chatter until the *next*
    #: key-on rotates the session. Generous enough that a stall or a long
    #: light never trips it (an idling engine keeps emitting RPM > 0), and
    #: cranking RPM re-arms instantly regardless. ``null`` restores the v1
    #: rotation-only disarm.
    disarm_after_seconds: float | None = 300.0


class IdleShutdownConfig(_Strict):
    """Escalate a sustained parked state to a system shutdown.

    The software half of ``docs/CANary-HAT-Circuits.md`` §3.7.6: the rig has no
    ignition sense, so the alternative to unplugging it between drives is to
    infer engine-off from bus activity and power down. See
    ``src.daemon.idle_shutdown`` for the inhibits and why each exists.

    **Ships disabled.** A box that powers itself off unattended is not
    something to enable by surprise, and the same hard default applies to
    every other config-gated background feature in this file.

    Not to be confused with ``scripts/ups_shutdown_watch.py``, which is a
    *safety* watchdog for actual input-power loss and runs standalone so it
    survives a hung daemon. This is an idle timeout and lives in the daemon
    because that is where the vehicle-state classifier lives.
    """

    enabled: bool = False
    #: Vehicle states counting as parked/disconnected. ``ignition_only`` is
    #: deliberately absent — KOEO is an operator at the wheel running
    #: diagnostics — and so is ``unknown``: if the classifier could not
    #: decide, neither should this.
    idle_states: tuple[str, ...] = ("engine_off", "no_data")
    #: Continuous seconds in an idle state before powering off. Generous by
    #: default: the cost of waiting is a few minutes of idle draw, the cost
    #: of firing early is a shutdown mid-drive.
    idle_after_seconds: float = 1800.0
    #: Command used to power down. ``systemctl suspend`` is the obvious
    #: alternative, but the Pi 5 has no meaningful suspend state, so
    #: poweroff is the only one that actually saves anything.
    poweroff_command: tuple[str, ...] = ("systemctl", "poweroff")
    #: Honour the operator hold that ``ups_shutdown_watch.py`` reads. A hold
    #: respected by only one of the two shutdown paths is worse than none.
    respect_keepalive: bool = True
    keepalive_file: str = "/run/canary/ups_keepalive"
    #: The ``ups-shutdown`` watchdog's JSON heartbeat. Its ``input_lost``
    #: field is what distinguishes a parked vehicle (input present, shut
    #: down) from a bench Pi on USB-C with the barrel jack out (input lost,
    #: stay up). Read as a file — never GPIO6 directly, which the watchdog
    #: owns and a second ``lgpio`` claimant collides with.
    status_file: str = "/run/canary/ups_status"
    #: Log the decision instead of acting on it. The way to try this on a
    #: live rig without risking a surprise poweroff.
    dry_run: bool = False


class CollectionConfig(_Strict):
    obd_source: Literal["canable", "elm327", "disabled"] = "canable"
    obd_pids: tuple[str, ...] = ("auto",)
    obd_dids: tuple[str, ...] = ()
    obd_did_request_id: int = 0x7E0
    #: Mode-22 gating session (UDS 0x10 sub-function). ``"auto"`` (default)
    #: reads it from data/dids.yaml's access block; ``null`` forces bare DID
    #: polling (ECUs that answer 0x22 in the default session); an int
    #: overrides the map. When a session gates Mode 22 it is mutually
    #: exclusive with Mode 01 on the bus, so DIDs are polled in short
    #: scheduled windows (see the obd_did_window_* knobs below).
    obd_did_session: int | Literal["auto"] | None = "auto"
    #: ``null`` → keep-alive cadence from dids.yaml access block (or 2.0 s).
    #: PIDs per Mode-01 request. SAE J1979 allows six; the collector narrows
    #: 6 -> 4 -> 2 -> off on repeated failures, so a vehicle that refuses
    #: batching degrades on its own rather than needing this tuned by hand.
    #: Batched PIDs share one request *and one timestamp*, which is what makes
    #: a capture read as synchronous rows instead of interleaved samples.
    obd_batch_max_pids: int = Field(default=6, ge=1, le=6)
    #: Maximum DIDs per Mode-22 request. Independent from Mode 01 capability;
    #: 1 is off. Only DIDs with provenance-backed full response widths batch.
    obd_did_batch_max_dids: int = Field(default=1, ge=1, le=3)
    obd_did_session_keepalive_seconds: float | None = None
    #: How often a DID window opens. Each window reads every active DID once,
    #: so this is the effective per-DID sample interval.
    obd_did_window_interval_seconds: float = 60.0
    #: Start-to-start period of one Mode 01 + Mode 22 cycle (C04.3). When
    #: set, windows open at ``anchor + k * cycle`` and the interval above is
    #: ignored; ``null`` (default) keeps the end-to-start interval policy.
    #: A capture profile's ``did_cycle_seconds`` overrides either.
    obd_did_cycle_seconds: float | None = None
    #: Hard cap on one window's duration (it normally closes much sooner,
    #: as soon as every active DID has been attempted once).
    obd_did_window_max_seconds: float = 10.0
    #: Quiet time after a window so the ECM's diagnostic session expires
    #: (S3 timeout) before Mode 01 polling resumes — resuming earlier would
    #: time the first Mode 01 polls out into their 30 s miss backoff.
    obd_did_session_lapse_seconds: float = 5.0
    obd_request_timeout_seconds: float = 1.0
    obd_poll_rate_hz: float
    #: Channels that hold their tier rate when the request plan asks for more
    #: than the bus can serve; everything else degrades around them. Defaults
    #: to the signals ``preprocessing.operating_modes`` gates on — starve
    #: those and the segmenter can't classify a row at all. Empty disables the
    #: priority lane (uniform degradation, the pre-1.11 behavior).
    obd_priority_channels: tuple[str, ...] = DEFAULT_PRIORITY_CHANNELS
    #: Maximum cadence for exploratory support probes. Discovery still obeys
    #: the global bus budget, but it cannot consume every otherwise-free slot
    #: while confirmed collection work is sparse.
    obd_discovery_budget_requests_per_second: float = Field(default=32.0, gt=0)
    #: Cadence (s) of the Mode 03/07/0A stored/pending/permanent DTC poll
    #: (Task 1.3) on the Canable-native path. DTCs change on a minutes
    #: scale, not seconds, so this is its own knob rather than a
    #: `PollTier` scaled off `obd_poll_rate_hz`. ``<= 0`` disables it.
    obd_dtc_poll_interval_seconds: float = 300.0
    #: Cadence (s) of the Mode 06 on-board monitor poll (Task 3.1) — misfire
    #: counts + monitor margins, same "own knob, not a `PollTier`" cadence
    #: model as ``obd_dtc_poll_interval_seconds`` above. ``<= 0`` disables it.
    obd_monitor_poll_interval_seconds: float = 300.0
    #: Mode 06 monitor IDs to poll. ``"auto"`` walks the standard supported-MID
    #: bitmaps once when the collector connects; an explicit tuple skips
    #: discovery for ECUs with a known fixed set.
    obd_monitor_mids: Literal["auto"] | tuple[int, ...] = "auto"
    storage_path: Path
    session_retention_days: int
    storage_warning_mb: int
    #: Byte-bounded ring buffer. When set, the daemon's retention sweeper
    #: evicts oldest unpinned sessions until total session bytes fall under
    #: this cap (pinned + active dirs are never evicted). ``None`` disables
    #: the cap (age-only retention) — the safe default for configs predating
    #: Phase 8.2.
    storage_max_mb: int | None = None
    reconnect_initial_delay_seconds: float = 10.0
    reconnect_max_delay_seconds: float = 60.0
    queue_max_size: int = 10_000
    queue_warning_fraction: float = 0.8
    #: Bindings confidence levels whose decoded signals are persisted to
    #: session Parquet alongside the raw CAN frames (source ``decoded``,
    #: RAW integer values — same semantics as the training path's
    #: decode-at-read, see ``src/decode/frame_decoder.py``). Storage is
    #: forever, so the default persists only ground-truth-validated
    #: bindings; widen to e.g. ``["confirmed", "high"]`` deliberately.
    #: Empty disables decoded persistence (raw frames are always kept).
    persist_decoded_confidence: tuple[str, ...] = ("confirmed",)
    #: Simulation backend: when set, the daemon replays this recorded
    #: session (dir path or id under storage) onto the telemetry queue
    #: *instead of* the real CAN/OBD collectors — no hardware needed, the
    #: dashboard shows previously-collected signals. Empty = normal capture.
    replay_session: str | None = None
    replay_speed: float = 1.0
    replay_loop: bool = True
    #: Age in days at which ``_replay-*`` session directories are evicted by
    #: the retention sweeper. Independent from ``session_retention_days``
    #: because replay output is test data that the operator may want to
    #: keep visible briefly but not forever. Set to a large value to keep
    #: replays around indefinitely; the ``_`` prefix already excludes them
    #: from training discovery and from the live-session listing.
    replay_retention_days: int = 7
    #: Operator-intent recording gate (Task 8.6). Defaults off.
    record_arming: RecordArmingConfig = RecordArmingConfig()
    idle_shutdown: IdleShutdownConfig = IdleShutdownConfig()


class OperatingModeSpec(_Strict):
    rpm_min: float | None = None
    rpm_max: float | None = None
    speed_min: float | None = None
    speed_max: float | None = None
    throttle_min: float | None = None
    throttle_max: float | None = None
    #: Coolant-temperature gates (°C). Lets the segmenter pull "cold engine
    #: warming up" out of the same RPM/speed band as warm steady-state idle —
    #: without this the behavior models fit one surface to both regimes and
    #: fire on every cold-start cycle. See ``warmup`` in
    #: ``canary.example.yaml`` for a representative threshold; the gating
    #: signal is ``obd:COOLANT_TEMP`` (degC).
    coolant_temp_min: float | None = None
    coolant_temp_max: float | None = None
    #: Recent-motion gates (km/h) on a derived ``recent_speed`` signal — the
    #: trailing max ground speed over ``idle_transient_lookback_seconds``. Lets
    #: the segmenter pull a *transient stop* (stopped now, but driving moments
    #: ago — a stop light) out of the same warm RPM/zero-speed band as a steady
    #: standing idle (driveway / pre-drive). Without it both collapse to one
    #: ``idle`` model that can't tell "stopped mid-drive" from "never moved".
    #: ``recent_speed`` is computed per session, so it never bleeds across
    #: sessions, and is ephemeral — it gates segmentation only, never a feature.
    recent_speed_min: float | None = None
    recent_speed_max: float | None = None
    #: Longitudinal/lateral accel gates (g) on the decimated
    #: ``imu:ACCEL_LON_MAX``/``imu:ACCEL_LAT_MAX`` channels (Phase 2a.3/2a.7,
    #: docs/roadmap/imu-gps-integration.md) — lets ``launch``/``hard_brake``/
    #: ``cornering`` modes gate on a transient IMU peak the same way other
    #: modes gate on RPM/speed/throttle. Inert (never required, never
    #: matched) until a real session actually has these columns — no IMU
    #: hardware exists yet, so every stock mode spec leaves these ``None``.
    accel_lon_min: float | None = None
    accel_lon_max: float | None = None
    accel_lat_min: float | None = None
    accel_lat_max: float | None = None


class PreprocessingConfig(_Strict):
    resample_hz: float
    rolling_window_seconds: int
    operating_modes: dict[str, OperatingModeSpec]
    #: Trailing window (s) for the segmenter's derived ``recent_speed`` signal
    #: (see ``OperatingModeSpec.recent_speed_*``). Sized to outlast a typical
    #: red light so a multi-minute stop stays ``idle_transient`` rather than
    #: decaying into standing ``idle``. The live ring buffer is grown to hold at
    #: least this many seconds so serve-time segmentation matches training.
    idle_transient_lookback_seconds: int = 180
    #: Skip per-mode segmentation entirely and fit/score a single model over a
    #: constant ``operating_mode = "all"``. Use when the corpus covers one
    #: regime, or when the OBD mode signals (RPM/SPEED/THROTTLE) are too sparse
    #: to segment reliably live — that sparsity otherwise produces train/serve
    #: skew where serving emits a mode the trainer never produced. See
    #: ``feature_pipeline.segment_or_constant``.
    single_operating_mode: bool = False
    #: Forward-fill ceiling (s) for slow channels (coolant/IAT/baro —
    #: ``resampler.DEFAULT_SLOW_CHANNEL_HINTS``) during resampling. Generous
    #: relative to fast channels' ``max_gap`` (interval × ``max_gap_factor``)
    #: because slow sensors legitimately update on the order of tens of
    #: seconds; this only catches a channel that's gone genuinely dead.
    #: Beyond the limit the resampled cell goes NaN and its ``__gap`` flag
    #: sets, same treatment fast channels already got.
    slow_channel_max_gap_seconds: float = 120.0
    #: A larger wall-clock discontinuity starts a new logical capture segment,
    #: even if both sides were accidentally written beneath one directory.
    #: Resampling across suspend/disconnect gaps manufactures thousands of
    #: all-stale rows and lets rolling features bridge unrelated drives.
    session_gap_seconds: float = Field(default=300.0, gt=0.0)
    #: Multiple of the resample interval a *fast* channel may go unobserved
    #: before its ffilled cell is NaN'd and its ``__gap`` flag sets.
    #:
    #: This is a claim about the achievable poll cadence, not the configured
    #: one. The default 2.0 (a 2 s ceiling at 1 Hz) assumes fast-tier PIDs
    #: really arrive at ~1 Hz; on a contended bus they do not — the 2026-07-26
    #: drive measured SPEED at 2.13 s and MAF at 4.46 s mean spacing against an
    #: 8 req/s budget carrying ~25 req/s of demand. Every cell past the ceiling
    #: becomes NaN, and because ``TrainedIsolationForest.score`` refuses any row
    #: with a NaN in any trained feature, that made the IF unable to score a
    #: single row (0 of 273 features reached 75% coverage; the Pi reported
    #: ``missing_feature_rate: 1.0``).
    #:
    #: Exposed as config (it was previously a hardcoded ``ResampleConfig``
    #: default) because it is preprocessing-parity-critical: it is recorded in
    #: the model manifest as ``max_gap_seconds`` and read by BOTH the offline
    #: resampler and ``LiveFeatureSource``, so train and serve move together.
    #: Changing it requires a retrain to take effect.
    #:
    #: The default stays at the historical 2.0 deliberately. Raising it was
    #: measured on 2026-07-26T164436 and does NOT rescue the IF: over 40 OBD
    #: channels, mean coverage went 11.7% (factor 2) → 20.9% (5) → 25.7% (10)
    #: → 30.0% (20), with **zero** channels reaching 50% at any setting. The
    #: shortfall is real poll cadence and Mode-01 suspension during DID
    #: windows, not an over-tight ceiling, so a looser ceiling only relabels
    #: stale data as fresh without making per-row scoring viable. Left tunable
    #: because it is a legitimate knob, not because raising it is the fix.
    max_gap_factor: float = 2.0
    #: Phase 5.2 — consecutive rows a candidate operating-mode label must hold
    #: before segmentation actually switches to it. ``1`` (default) is the
    #: legacy immediate-switch behavior. Without dwell, 1 Hz flapping at a
    #: mode boundary (e.g. city/highway right at the speed threshold)
    #: contaminates both models' training rows with a handful of mislabeled
    #: ticks each way. Mirrors ``AlertsConfig.min_consecutive_ticks``'s shape;
    #: see ``segmenter._apply_mode_dwell``.
    mode_dwell_ticks: int = Field(default=1, ge=1)


class IsolationForestConfig(_Strict):
    enabled: bool
    n_estimators: int
    contamination: float
    per_mode_contamination: dict[str, float] = Field(default_factory=dict)
    random_state: int = 42
    #: Phase 7.2 — fraction of each mode's clean training rows, time-ordered
    #: from the tail, held out (not fit on) and used purely to calibrate the
    #: per-mode score threshold. ``0`` disables calibration (legacy
    #: contamination-label decision only).
    calibration_holdout_fraction: float = Field(default=0.2, ge=0.0, lt=1.0)
    #: Target fraction of *nominal* held-out ticks the per-mode threshold is
    #: allowed to flag — the calibrated decision boundary is the
    #: ``1 - target_flag_rate`` quantile of held-out scores. This replaces the
    #: assumption that exactly ``contamination`` of nominal driving is anomalous.
    target_flag_rate: float = Field(default=0.001, gt=0.0, le=0.5)
    #: Task 2.2 — a mode with fewer than this many *fit* rows (post-holdout
    #: split) is skipped entirely rather than fitting a full forest on a
    #: handful of rows: no model persisted, listed in the manifest's
    #: ``skipped_modes`` instead.
    min_samples_per_mode: int = Field(default=100, ge=0)
    #: Parallel tree builders. A small fixed default avoids both the historical
    #: single-core fit and ``-1`` oversubscription on memory-constrained WSL.
    n_jobs: int = 4
    max_dropped_row_fraction: float = Field(default=0.75, ge=0.0, lt=1.0)
    allow_unknown_only: bool = False

    @field_validator("n_jobs")
    @classmethod
    def _nonzero_n_jobs(cls, value: int) -> int:
        if value == 0:
            raise ValueError("n_jobs must not be zero")
        return value


class AlertsConfig(_Strict):
    """Emission policy shared by every detector (Phase 7.5; M-of-N 6.1).

    All defaulted so configs predating it keep loading.
    """

    #: A detector (IF / sequence / each behavior surface) must be over its
    #: threshold for at least this many ticks WITHIN the trailing
    #: ``persistence_window_ticks`` (M-of-N, 6.1) before it emits an event or
    #: counts as a firing fusion source. Real faults persist; a lone tick is
    #: the historical flood. 1 = legacy fire-on-first-tick behavior. Named
    #: ``min_consecutive_ticks`` from its pre-6.1 strict-N-consecutive
    #: shape; kept for config compatibility even though it's now the M in
    #: M-of-N (``persistence_window_ticks`` defaults to this same value,
    #: which reproduces the old strict-consecutive gate exactly).
    min_consecutive_ticks: int = Field(default=3, ge=1)
    #: N — the trailing window (in ticks) the M hits above are counted
    #: within. Must be >= min_consecutive_ticks. Default 5 against the
    #: default M=3 gives the roadmap's "3-of-5" shape out of the box —
    #: tolerates one fire-gap-fire flicker per window without weakening the
    #: 3 hits still required (flood protection unchanged). ``None`` falls
    #: back to ``min_consecutive_ticks`` itself (strict-consecutive, the
    #: exact pre-6.1 gate) for anyone who explicitly wants that shape back.
    persistence_window_ticks: int | None = Field(default=5, ge=1)
    #: T — wall-clock bound (seconds) on the same window: an entry older
    #: than this many seconds from "now" is evicted regardless of tick
    #: count, so a long data/replay gap can't let stale hits from before the
    #: gap count toward a streak once ticks resume. ``None`` disables the
    #: time bound (tick-count-only, the pre-6.1 shape).
    persistence_window_seconds: float | None = Field(default=8.0, gt=0.0)
    #: Per-(source, model) refractory window between emitted events. The old
    #: 1.0 s default was a no-op at the 1 Hz tick — a sustained deviation
    #: emitted ~3600 events/hour per source.
    min_seconds_between_events: float = Field(default=30.0, ge=0.0)

    @model_validator(mode="after")
    def _check_window(self) -> AlertsConfig:
        if (
            self.persistence_window_ticks is not None
            and self.persistence_window_ticks < self.min_consecutive_ticks
        ):
            raise ValueError(
                "persistence_window_ticks must be >= min_consecutive_ticks "
                f"({self.persistence_window_ticks} < {self.min_consecutive_ticks})"
            )
        return self


class FusionConfig(_Strict):
    """Phase 7.2 — per-tick fusion of IF + LSTM + behavior residuals into one
    agreement-aware calibrated severity. All defaulted so configs predating
    7.2 keep loading with fusion on.
    """

    enabled: bool = True
    #: Fused severity in [0, 1] at/above which a fused ``AnomalyEvent`` fires.
    severity_threshold: float = Field(default=0.5, gt=0.0, le=1.0)
    #: Added to the fused severity per *additional* agreeing source beyond the
    #: first (agreement-aware boost), before clamping to 1.0.
    agreement_boost: float = Field(default=0.25, ge=0.0, le=1.0)
    #: 6.2 (operator-approved decision, see OVERNIGHT-QUEUE.md "Fusion
    #: semantics"): the fused event is a real agreement gate, not
    #: OR-with-boost — ``is_anomaly`` requires at least this many
    #: *independent* firing sources (sibling surfaces sharing a ``group``
    #: still count as one), on top of ``severity_threshold``. A single
    #: source at any severity never sets the fused verdict alone; it still
    #: emits its own per-source event through the unrelated per-source path.
    min_agreement: int = Field(default=2, ge=1)
    #: Normalized severity a "warning" / "critical" behavior-deviation band
    #: contributes as a fusion source.
    behavior_warning_severity: float = Field(default=0.5, ge=0.0, le=1.0)
    behavior_critical_severity: float = Field(default=1.0, ge=0.0, le=1.0)


class LSTMAutoencoderConfig(_Strict):
    enabled: bool
    sequence_length: int
    hidden_size: int = 64
    num_layers: int = 1
    latent_dim: int
    dropout: float = 0.0
    batch_size: int = 64
    #: Overlapping training windows are highly redundant. Calibration remains
    #: dense by default so deployed thresholds retain one-tick resolution.
    training_stride: int = Field(default=5, ge=1)
    calibration_stride: int = Field(default=1, ge=1)
    #: ``None`` chooses a conservative platform default (zero on WSL). Worker
    #: processes can duplicate the full pandas-backed dataset.
    dataloader_workers: int | None = Field(default=None, ge=0)
    mixed_precision: bool = True
    compile_model: bool = False
    min_window_observed_fraction: float = Field(default=0.01, gt=0.0, le=1.0)
    min_usable_window_fraction: float = Field(default=0.9, gt=0.0, le=1.0)
    learning_rate: float = 0.001
    max_epochs: int = 100
    validation_fraction: float = 0.2
    early_stopping_patience: int = 10
    min_delta: float = 0.0
    random_state: int = 42
    threshold_k: float
    #: When set (0–1), calibrate thresholds as this quantile of the
    #: reconstruction-error distribution instead of ``mean + k·σ``.
    #: Reconstruction errors are right-skewed and stride-1 windows heavily
    #: autocorrelated, so σ is a poor tail proxy; a quantile targets the
    #: alert budget directly (like the IF's ``target_flag_rate``). Task 3.2:
    #: defaults to 0.999 (the calibrated-FPR path) rather than ``None`` — the
    #: old ``None`` default silently meant ``mean + k·σ`` was every deploy's
    #: real threshold. Set explicitly to ``null`` in config to opt back into
    #: k·σ.
    threshold_quantile: float | None = Field(default=0.999, gt=0.0, lt=1.0)
    #: Feature columns to exclude from anomaly scoring. Each entry matches
    #: itself plus its rolling-derivative siblings (``__mean``, ``__std``,
    #: ``__roc``), so listing ``obd:FUEL_LEVEL`` silences all four. Applied
    #: at *train* time (excluded from ``feature_columns`` in the manifest,
    #: so the model never learns them) **and** at *serve* time (the live
    #: window's mask is forced to 0 at these indices, so a stale artifact
    #: that still includes them can't push the global score). Use for
    #: behavior signals — fuel level, HVAC blower, module voltage — that
    #: drift naturally over a drive without being a health signal.
    suppress_channels: tuple[str, ...] = ()


class TCNAutoencoderConfig(_Strict):
    """Dilated causal TCN autoencoder — the alternate sequence-detector backbone.

    Fully defaulted (and ``enabled: false`` by default) so configs without a
    ``tcn_autoencoder`` block keep loading; selecting it requires both
    ``sequence_detector: tcn`` and ``enabled: true``.
    """

    enabled: bool = False
    sequence_length: int = 30
    channels: int = 64
    kernel_size: int = 3
    num_levels: int = 4
    latent_dim: int = 32
    dropout: float = 0.0
    #: Feed a bounded per-channel Δt decay as extra conv input channels (the
    #: GRU-D freshness gate). When false the TCN sees value+mask only.
    use_delta_decay: bool = True
    batch_size: int = 64
    training_stride: int = Field(default=5, ge=1)
    calibration_stride: int = Field(default=1, ge=1)
    dataloader_workers: int | None = Field(default=None, ge=0)
    mixed_precision: bool = True
    compile_model: bool = False
    min_window_observed_fraction: float = Field(default=0.01, gt=0.0, le=1.0)
    min_usable_window_fraction: float = Field(default=0.9, gt=0.0, le=1.0)
    learning_rate: float = 0.001
    max_epochs: int = 100
    validation_fraction: float = 0.2
    early_stopping_patience: int = 10
    min_delta: float = 0.0
    random_state: int = 42
    threshold_k: float = 3.0
    #: Same contract as ``LSTMAutoencoderConfig.threshold_quantile``
    #: (Task 3.2: defaults to 0.999).
    threshold_quantile: float | None = Field(default=0.999, gt=0.0, lt=1.0)
    #: Feature columns to exclude from anomaly scoring (same contract as
    #: ``LSTMAutoencoderConfig.suppress_channels`` — each entry expands to
    #: its rolling-derivative siblings). Applied at train + serve.
    suppress_channels: tuple[str, ...] = ()


class ModelsConfig(_Strict):
    isolation_forest: IsolationForestConfig
    lstm_autoencoder: LSTMAutoencoderConfig
    tcn_autoencoder: TCNAutoencoderConfig = Field(default_factory=TCNAutoencoderConfig)
    fusion: FusionConfig = Field(default_factory=FusionConfig)
    #: Which backbone fills the single sequence-detector slot. The selected
    #: backend's own ``enabled`` flag still gates whether it loads (so an
    #: existing ``lstm_autoencoder.enabled: false`` keeps the slot off, and
    #: ``tcn_autoencoder`` defaults to disabled). See
    #: ``src/models/sequence_registry.py``. 9.1 (operator-approved decision,
    #: see ``OVERNIGHT-QUEUE.md``): GRU-D LSTM is the shipped default — its
    #: GRU decoder reconstructs step-by-step from the latent, materially
    #: stronger temporal-shape modeling than the TCN decoder's single
    #: broadcast-latent reconstruction. TCN is documented as an NPU-export
    #: fallback, not an equal; no further bake-off gates this default.
    sequence_detector: Literal["lstm", "tcn"] = "lstm"
    #: Score IF + sequence in a child process rather than a worker thread.
    #: C08 moved scoring off the event loop into a thread, which is not the
    #: same as off the CPU: measured on the Pi 2026-09-09, the daemon missed
    #: 48 % of its 1 Hz ticks with 2.5 of 4 cores idle, because one Python
    #: process is GIL-bound to roughly one core and a slow forward pass
    #: starved the collector thread sharing it — collection stopped for ~100 s
    #: at a stretch.
    #:
    #: **Off by default, deliberately.** The child rebuilds its models from
    #: *this config's* artifact paths, so it only sees models that came from
    #: disk — a parent holding an injected or in-memory model (tests, and any
    #: caller that populates ``model_managers`` itself) would have the child
    #: score against something else, or nothing. Deployments where the models
    #: do come from disk opt in; nothing else changes behaviour. A host where
    #: the child cannot start falls back to in-process scoring, so this is a
    #: performance knob, never a correctness one.
    out_of_process_scoring: bool = False
    #: Build the live rolling feature row in one persistent spawned process.
    #: Unlike model scoring, this worker needs no artifact paths and is safe
    #: for every deployment: each request carries the exact config and
    #: behavior-model snapshot for that tick. Enabled by default so pandas'
    #: Python/GIL-heavy transform cannot starve collection or HTTP on the Pi.
    #: Set false for deterministic single-process debugging and unit fixtures.
    out_of_process_feature_build: bool = True


class DashboardConfig(_Strict):
    host: str
    port: int


class DaemonConfig(_Strict):
    """Where the long-running daemon reads / writes its sidecar artifacts.

    All fields are optional so existing ``canary.yaml`` files keep loading
    without edits; the defaults are sensible relative-to-cwd paths for the
    common WSL-dev case. Production deployments override them (the systemd
    unit will pin absolute paths under ``/var/lib/canary``).
    """

    anomalies_dir: Path = Path("data/anomalies")
    models_dir: Path = Path("models")
    jobs_dir: Path = Path("data/control/jobs")
    audit_log: Path = Path("data/control/audit.jsonl")
    feedback_log: Path = Path("data/control/anomaly_feedback.jsonl")
    #: Vehicle maintenance / intervention ledger (Task 3.1,
    #: eval-longitudinal-and-dtc.md) — what physically happened to the
    #: truck, append-only, profile-scoped like the sibling paths below.
    vehicle_ledger: Path = Path("data/control/vehicle_ledger.jsonl")
    #: Frozen reference epoch for fingerprint drift trending (Task 4.3,
    #: eval-longitudinal-and-dtc.md) — per-stat mean/std/quantiles over a
    #: hand-picked set of healthy sessions. Written only by
    #: ``scripts/fingerprint.py freeze``; never auto-updated. Profile-scoped
    #: like the sibling ledger paths.
    reference_epoch: Path = Path("data/control/reference_epoch.json")
    training_ledger: Path = Path("data/control/training_runs.jsonl")
    tuning_config: Path = Path("data/control/tuning.yaml")
    tuning_audit: Path = Path("data/control/tuning_audit.jsonl")
    #: Persisted GPS trip odometer (cumulative distance + moving time). Survives
    #: daemon restarts so the System page shows lifetime "miles / time driven
    #: with the unit plugged in" rather than per-process totals.
    trip_state: Path = Path("data/control/trip_odometer.json")
    #: Task 4.6 idempotency marker — ``{stat: last_alerted_session_id}``. CUSUM
    #: state itself is recomputed fresh from the full fingerprint history on
    #: every evaluation (Task 4.4's design note: cheap at per-session
    #: frequency, avoids incremental-state staleness/corruption); this tiny
    #: file is the ONLY thing persisted, purely so a re-evaluation of the same
    #: already-alerted session never double-emits. Profile-scoped like the
    #: sibling ledger paths.
    trend_alert_state: Path = Path("data/control/trend_alert_state.json")
    #: Behavior-model surfaces (hand-edited / refit-written). A config field
    #: rather than a hardcoded path so it can be scoped per vehicle profile
    #: (see ``src/profiles.py``). Cwd-relative like the sibling paths; the
    #: systemd unit ``cd``s to the clone so the default resolves there.
    behavior_models: Path = Path("data/behavior_models.yaml")
    #: Directional, provenanced accepted-fault overrides (Task 1.3,
    #: online-training-strategy.md) — sibling to ``behavior_models`` above,
    #: same profile-scoping rationale (a second vehicle must never inherit
    #: this one's overrides). Optional: absent file ⇒ zero overrides
    #: (``load_accepted_baselines`` handles that gracefully).
    accepted_baselines: Path = Path("data/accepted_baselines.yaml")
    #: User-defined custom-PID formulas (Task 6.8, wired in by Task 2.6) —
    #: schema documented in ``custom_pids.example.yaml``. Optional: absent
    #: file ⇒ zero custom PIDs polled (``load_custom_pids`` handles that
    #: gracefully), same posture as ``behavior_models`` above.
    custom_pids: Path = Path("data/custom_pids.yaml")
    #: Operator-selectable collection modes, scoped per vehicle.
    capture_profiles: Path = Path("data/capture_profiles.yaml")
    #: Per-vehicle, per-tune AFR voltage transfer and target-table exports.
    afr_calibration: Path = Path("data/afr_calibration.yaml")
    #: Session catalog (Task 6.1, eval-longitudinal-and-dtc.md) — SQLite index
    #: over session sidecars. Rebuildable from disk at any time
    #: (``scripts/catalog.py rebuild``); never a second source of truth.
    #: Profile-scoped like the sibling ledger paths.
    catalog_db: Path = Path("data/control/catalog.db")
    #: Ceiling on the row count a trip-end digest will load (D03.6). Peak
    #: memory for a digest is O(rows x channels) because the wide pivot runs
    #: at raw timestamp resolution *before* the 1 Hz resample: 5,483 MB
    #: measured for a 2,622,370-row session, to produce 1,143 final rows. On
    #: the 8 GB Pi the cliff is bracketed between ~4.5 M rows (a session that
    #: completed) and ~5.9 M rows (the daemon OOM-killed), so 4 M sits just
    #: inside the known-good side. A session over the budget gets a
    #: ``digest_skipped.json`` naming the count and the budget instead of an
    #: attempt; ``0`` disables the check.
    #:
    #: ``None`` (the default) **derives** the budget from the child's
    #: address-space ceiling and the measured per-row peak, rather than
    #: stating a second number that can drift out of step with the first. A
    #: hand-picked 4 M sat against a 4 GiB ceiling that only covers ~2 M rows,
    #: so sessions in between passed the budget, spawned a child and died of
    #: ``MemoryError`` — safe, but it reported a memory anomaly where the
    #: honest answer was "this is simply larger than what fits". One number,
    #: one place to change when the O(rows x channels) cost is fixed.
    digest_row_budget: int | None = Field(default=None, ge=0)
    #: Hard ``RLIMIT_AS`` ceiling, in MiB, applied inside the digest child
    #: process before it touches any data. This is the backstop that holds
    #: even if the row budget is raised or the memory cost regresses: an
    #: over-budget digest then raises ``MemoryError`` *in the child*, which
    #: the parent reports, instead of the kernel picking an OOM victim (twice
    #: that victim was the daemon). ``None`` derives it from physical RAM —
    #: half of total, clamped to [1 GiB, 4 GiB] — which on the 8 GB Pi gives
    #: a 4 GiB child that still fits, beside the daemon's normal 150-650 MB,
    #: inside the unit's ``MemoryMax=6G``; the rlimit is the *catchable* half
    #: of that pair, since a cgroup kill is not an exception anyone can name.
    digest_address_space_mb: int | None = Field(default=None, gt=0)
    isolation_forest_artifact: str = "isolation_forest/model.joblib"
    lstm_artifact: str = "lstm_autoencoder/model.pt"
    tcn_artifact: str = "tcn_autoencoder/model.pt"
    model_poll_seconds: float = 30.0
    retention_sweep_seconds: float = 3600.0
    broadcaster_queue_size: int = 100
    control_token_env: str = "CANARY_CONTROL_TOKEN"


class TrendsConfig(_Strict):
    """Task 4.4 trend-test parameters (``src.analysis.trends``). All defaulted
    to the textbook values so configs predating this task keep loading."""

    #: Tabular-CUSUM slack ``k``, in reference-std units — half the shift
    #: size worth detecting.
    cusum_k: float = Field(default=0.5, ge=0.0)
    #: Tabular-CUSUM decision interval ``h``, in reference-std units — the
    #: alarm threshold. 4-5 sigma is the usual textbook range.
    cusum_h: float = Field(default=5.0, gt=0.0)
    #: Mann-Kendall two-tailed significance level.
    mann_kendall_alpha: float = Field(default=0.05, gt=0.0, lt=1.0)


class NotificationsConfig(_Strict):
    """Push notifications via ntfy (Phase 5, eval-longitudinal-and-dtc.md).

    Ships disabled with no real topic baked in (operator-approved decision,
    see OVERNIGHT-QUEUE.md Q12) — a made-up ``topic`` would either silently
    no-op against ntfy.sh or, worse, land on a stranger's public topic.
    Turning this on for the operator's actual ntfy instance is an explicit
    morning task, not something to flip during unattended work. All fields
    default so configs predating this section keep loading with
    notifications off.
    """

    enabled: bool = False
    #: Base ntfy server URL (self-hosted or the public ``ntfy.sh``).
    ntfy_url: str = "https://ntfy.sh"
    #: ntfy topic to POST to. Blank until the operator picks a real one.
    topic: str = ""
    #: Minimum ``AnomalyEvent.score`` (same larger-is-worse scale each source
    #: already uses) that reaches a push (5.3's critical-alert hook).
    min_severity: float = Field(default=0.8, ge=0.0)
    #: Hard cap on pushes per recording session — a flapping detector must
    #: never turn into a phone full of notifications.
    max_pushes_per_session: int = Field(default=3, ge=0)
    #: End-of-drive digest push (5.5) — independent toggle from the
    #: per-event critical-alert hook above.
    digest_enabled: bool = False


class OnlineTrainingConfig(_Strict):
    """Config-gated scheduled retrain (Task 3.2, online-training-strategy.md).

    **Ships disabled** — a hard pre-made decision (queue Round-2): enabling
    unattended scheduled retrains against the real corpus is an explicit
    operator action, never an unattended default. When enabled, the daemon's
    ``run_online_training_scheduler`` background task (mirrors
    ``run_retention_sweeper``'s asyncio-task-on-a-cadence shape) submits a
    ``retrain_all`` job through the existing ``JobController`` every
    ``interval_seconds`` — the same subprocess-isolated mechanism the
    dashboard's manual "Retrain" button already uses, so a scheduled retrain
    never runs on the daemon's own event loop or blocks real-time capture.
    The job is windowed to the trailing ``window_days`` of sessions
    (``--since``, Task 3.1) — a genuine sliding window, not a fixed corpus —
    and the scheduler never adds ``--no-calibration-holdout``: a window too
    small to spare a calibration holdout is refused by ``retrain.py`` itself
    (Task 3.1) and logged as a skip, never degraded to in-sample calibration.
    """

    enabled: bool = False
    #: Seconds between scheduled-retrain attempts.
    interval_seconds: float = Field(default=86400.0, gt=0.0)
    #: Rolling-window length in days passed to ``retrain.py --since``.
    window_days: int = Field(default=30, gt=0)
    #: How often the scheduler re-checks a submitted job's status while
    #: waiting for it to finish (a cheap in-memory dict lookup — this is not
    #: additional training, just how promptly the loop notices completion).
    poll_interval_seconds: float = Field(default=5.0, gt=0.0)


class ProfilesConfig(_Strict):
    """Multi-vehicle profile resolution (see ``src/profiles.py``).

    When the Pi is plugged into a vehicle, the daemon reads the VIN
    (OBD-II Mode 09 PID 0x02) at startup and resolves it to a profile slug,
    then scopes every vehicle-specific path under ``<base>/<slug>/`` so two
    vehicles never share a training corpus or model set. All fields default so
    configs predating profiles keep loading; with the probe disabled or no VIN
    available the ``vehicle:`` block is used as the fallback/default profile.
    """

    enabled: bool = True
    #: VIN → profile registry (a daemon-managed *data* file, not canary.yaml).
    #: Not scoped per vehicle — it is the index that maps VIN to slug.
    registry_path: Path = Path("profiles.yaml")
    #: Persist a freshly-seen VIN to the registry automatically (with a blank
    #: display name for the operator to fill in via the dashboard).
    auto_register: bool = True
    #: How long the startup VIN probe waits for the ECU's Mode 09 response.
    vin_probe_timeout_seconds: float = 2.0


class AutoTagConfig(_Strict):
    """Thresholds for the motion-aware auto-tagger (Phase 2a.5/2a.6,
    docs/roadmap/imu-gps-integration.md). One taxonomy, one config block:
    both the offline pass (``scripts/autotag.py``) and the live daemon hook
    evaluate the exact same thresholds, so tuning them (as real drives
    accumulate) never needs a redeploy — same philosophy as the behavior
    models' deviation bands.

    ``live_enabled`` is a **hard pre-made decision** (Round-2
    OVERNIGHT-QUEUE): ships ``False`` until Phase 2b hardware validation,
    independent of whether the offline pass is useful today (it is, for the
    GPS-only ``stopped``/``highway_cruise`` tags against existing sessions).
    """

    live_enabled: bool = False
    #: g — decimated ``imu:ACCEL_LON_MAX`` threshold, disambiguated into
    #: accel vs. brake by ``d(obd:SPEED)/dt``'s sign (IMU + OBD, no GPS).
    hard_accel_g: float = 0.35
    hard_brake_g: float = 0.40
    #: g — decimated ``imu:ACCEL_LAT_MAX`` threshold for ``cornering``.
    cornering_g: float = 0.30
    #: km/h — a launch's starting speed must be at/below this ("near-stop").
    launch_speed_max_kph: float = 5.0
    #: seconds a qualifying hard_accel must sustain from near-stop to count
    #: as ``launch`` rather than an ordinary hard_accel.
    launch_sustain_seconds: float = 1.5
    #: km/h — ``gps:speed`` at/below this counts as ``stopped`` (GPS-only,
    #: bus-independent — works even with no OBD/CAN fix at all).
    stopped_speed_max_kph: float = 3.0
    #: km/h — ``gps:speed`` at/above this, sustained, is a highway_cruise
    #: candidate.
    highway_speed_min_kph: float = 90.0
    #: seconds ``gps:speed`` must hold >= ``highway_speed_min_kph`` before
    #: ``highway_cruise`` tags.
    highway_sustain_seconds: float = 20.0
    #: g² — max variance of lon/lat accel allowed during the sustained-speed
    #: window (keeps a fast, bumpy/swervy stretch from reading as "cruise").
    #: Only applied when IMU accel columns are present; ignored otherwise.
    highway_max_accel_variance_g2: float = 0.02


class LLMAssistConfig(_Strict):
    """Feature flag for the optional LLM-assist tier (Task S.4,
    docs/roadmap/llm-assist-and-compute-roadmap.md). Default ``False``: no
    producer exists yet (the roadmap's Sequencing step 1 is seams-only), and
    nothing on the capture path reads this block — it's read only by the
    dashboard's read-only analysis routes deciding whether to surface
    ``src.analysis.llm_assist`` cards or the disabled/absent state.
    """

    enabled: bool = False
    #: Where a future producer appends cards and the dashboard endpoint
    #: reads them from (``src.analysis.llm_assist.store.LlmAssistCardStore``).
    cards_path: Path = Path("data/analyses/llm_assist_cards.jsonl")


class CanaryConfig(_Strict):
    vehicle: VehicleConfig
    hardware: HardwareConfig
    collection: CollectionConfig
    preprocessing: PreprocessingConfig
    models: ModelsConfig
    dashboard: DashboardConfig
    daemon: DaemonConfig = DaemonConfig()
    alerts: AlertsConfig = AlertsConfig()
    profiles: ProfilesConfig = ProfilesConfig()
    trends: TrendsConfig = TrendsConfig()
    notifications: NotificationsConfig = NotificationsConfig()
    online_training: OnlineTrainingConfig = OnlineTrainingConfig()
    autotag: AutoTagConfig = AutoTagConfig()
    llm_assist: LLMAssistConfig = LLMAssistConfig()

    @classmethod
    def from_yaml(cls, path: Path) -> Self:
        with path.open("r") as f:
            data = yaml.safe_load(f)
        return cls.model_validate(data)
