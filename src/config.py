"""Configuration models for CANary.

Loads `canary.yaml` and validates it. Unknown keys raise rather than being
silently dropped, so typos in config don't become silent drift at runtime.
"""

from __future__ import annotations

from pathlib import Path
from typing import Literal, Self

import yaml
from pydantic import BaseModel, ConfigDict, Field, model_validator


class _Strict(BaseModel):
    model_config = ConfigDict(extra="forbid")


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


class HardwareConfig(_Strict):
    elm327_port: str
    can: CanBusConfig
    gps: GpsConfig = GpsConfig()


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
    #: ``TelemetryRecord.channel`` names (unprefixed — e.g. ``"RPM"``) that
    #: can arm the gate.
    arm_channels: tuple[str, ...] = ("RPM", "SPEED")
    #: A configured channel's numeric value must exceed this to arm.
    arm_threshold: float = 0.0


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
    obd_did_session_keepalive_seconds: float | None = None
    #: How often a DID window opens. Each window reads every active DID once,
    #: so this is the effective per-DID sample interval.
    obd_did_window_interval_seconds: float = 60.0
    #: Hard cap on one window's duration (it normally closes much sooner,
    #: as soon as every active DID has been attempted once).
    obd_did_window_max_seconds: float = 10.0
    #: Quiet time after a window so the ECM's diagnostic session expires
    #: (S3 timeout) before Mode 01 polling resumes — resuming earlier would
    #: time the first Mode 01 polls out into their 30 s miss backoff.
    obd_did_session_lapse_seconds: float = 5.0
    obd_request_timeout_seconds: float = 1.0
    obd_poll_rate_hz: float
    #: Cadence (s) of the Mode 03/07/0A stored/pending/permanent DTC poll
    #: (Task 1.3) on the Canable-native path. DTCs change on a minutes
    #: scale, not seconds, so this is its own knob rather than a
    #: `PollTier` scaled off `obd_poll_rate_hz`. ``<= 0`` disables it.
    obd_dtc_poll_interval_seconds: float = 300.0
    #: Cadence (s) of the Mode 06 on-board monitor poll (Task 3.1) — misfire
    #: counts + monitor margins, same "own knob, not a `PollTier`" cadence
    #: model as ``obd_dtc_poll_interval_seconds`` above. ``<= 0`` disables it.
    obd_monitor_poll_interval_seconds: float = 300.0
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
    target_flag_rate: float = Field(default=0.02, gt=0.0, le=0.5)
    #: Task 2.2 — a mode with fewer than this many *fit* rows (post-holdout
    #: split) is skipped entirely rather than fitting a full forest on a
    #: handful of rows: no model persisted, listed in the manifest's
    #: ``skipped_modes`` instead.
    min_samples_per_mode: int = Field(default=100, ge=0)


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
    #: defaults to 0.995 (the calibrated-FPR path) rather than ``None`` — the
    #: old ``None`` default silently meant ``mean + k·σ`` was every deploy's
    #: real threshold. Set explicitly to ``null`` in config to opt back into
    #: k·σ.
    threshold_quantile: float | None = Field(default=0.995, gt=0.0, lt=1.0)
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
    learning_rate: float = 0.001
    max_epochs: int = 100
    validation_fraction: float = 0.2
    early_stopping_patience: int = 10
    min_delta: float = 0.0
    random_state: int = 42
    threshold_k: float = 3.0
    #: Same contract as ``LSTMAutoencoderConfig.threshold_quantile``
    #: (Task 3.2: defaults to 0.995).
    threshold_quantile: float | None = Field(default=0.995, gt=0.0, lt=1.0)
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
    #: Session catalog (Task 6.1, eval-longitudinal-and-dtc.md) — SQLite index
    #: over session sidecars. Rebuildable from disk at any time
    #: (``scripts/catalog.py rebuild``); never a second source of truth.
    #: Profile-scoped like the sibling ledger paths.
    catalog_db: Path = Path("data/control/catalog.db")
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

    @classmethod
    def from_yaml(cls, path: Path) -> Self:
        with path.open("r") as f:
            data = yaml.safe_load(f)
        return cls.model_validate(data)
