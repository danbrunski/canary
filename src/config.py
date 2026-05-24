"""Configuration models for CANary.

Loads `canary.yaml` and validates it. Unknown keys raise rather than being
silently dropped, so typos in config don't become silent drift at runtime.
"""

from __future__ import annotations

from pathlib import Path
from typing import Literal, Self

import yaml
from pydantic import BaseModel, ConfigDict, Field


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
    #: Serial device path. Prefer the stable ``/dev/serial/by-id/...`` symlink
    #: over ``/dev/ttyUSB0``, which renumbers when USB devices re-enumerate.
    device: str = "/dev/ttyUSB0"
    baudrate: int = 9600
    #: Serial read timeout (seconds). Also bounds how quickly the collector
    #: notices a stop request while waiting for the next NMEA line.
    read_timeout_seconds: float = 2.0


class HardwareConfig(_Strict):
    elm327_port: str
    can: CanBusConfig
    gps: GpsConfig = GpsConfig()


class CollectionConfig(_Strict):
    obd_source: Literal["canable", "elm327", "disabled"] = "canable"
    obd_pids: tuple[str, ...] = ("auto",)
    obd_dids: tuple[str, ...] = ()
    obd_did_request_id: int = 0x7E0
    obd_request_timeout_seconds: float = 1.0
    obd_poll_rate_hz: float
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


class PreprocessingConfig(_Strict):
    resample_hz: float
    rolling_window_seconds: int
    operating_modes: dict[str, OperatingModeSpec]
    #: Skip per-mode segmentation entirely and fit/score a single model over a
    #: constant ``operating_mode = "all"``. Use when the corpus covers one
    #: regime, or when the OBD mode signals (RPM/SPEED/THROTTLE) are too sparse
    #: to segment reliably live — that sparsity otherwise produces train/serve
    #: skew where serving emits a mode the trainer never produced. See
    #: ``feature_pipeline.segment_or_constant``.
    single_operating_mode: bool = False


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


class ModelsConfig(_Strict):
    isolation_forest: IsolationForestConfig
    lstm_autoencoder: LSTMAutoencoderConfig
    fusion: FusionConfig = Field(default_factory=FusionConfig)


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
    training_ledger: Path = Path("data/control/training_runs.jsonl")
    tuning_config: Path = Path("data/control/tuning.yaml")
    tuning_audit: Path = Path("data/control/tuning_audit.jsonl")
    #: Persisted GPS trip odometer (cumulative distance + moving time). Survives
    #: daemon restarts so the System page shows lifetime "miles / time driven
    #: with the unit plugged in" rather than per-process totals.
    trip_state: Path = Path("data/control/trip_odometer.json")
    isolation_forest_artifact: str = "isolation_forest/model.joblib"
    lstm_artifact: str = "lstm_autoencoder/model.pt"
    model_poll_seconds: float = 30.0
    retention_sweep_seconds: float = 3600.0
    broadcaster_queue_size: int = 100
    control_token_env: str = "CANARY_CONTROL_TOKEN"


class CanaryConfig(_Strict):
    vehicle: VehicleConfig
    hardware: HardwareConfig
    collection: CollectionConfig
    preprocessing: PreprocessingConfig
    models: ModelsConfig
    dashboard: DashboardConfig
    daemon: DaemonConfig = DaemonConfig()

    @classmethod
    def from_yaml(cls, path: Path) -> Self:
        with path.open("r") as f:
            data = yaml.safe_load(f)
        return cls.model_validate(data)
