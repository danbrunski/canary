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


class HardwareConfig(_Strict):
    elm327_port: str
    can: CanBusConfig


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
    reconnect_initial_delay_seconds: float = 10.0
    reconnect_max_delay_seconds: float = 60.0
    queue_max_size: int = 10_000
    queue_warning_fraction: float = 0.8


class OperatingModeSpec(_Strict):
    rpm_min: float | None = None
    rpm_max: float | None = None
    speed_min: float | None = None
    speed_max: float | None = None
    throttle_min: float | None = None
    throttle_max: float | None = None


class PreprocessingConfig(_Strict):
    resample_hz: float
    rolling_window_seconds: int
    operating_modes: dict[str, OperatingModeSpec]


class IsolationForestConfig(_Strict):
    enabled: bool
    n_estimators: int
    contamination: float
    per_mode_contamination: dict[str, float] = Field(default_factory=dict)
    random_state: int = 42


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
    isolation_forest_artifact: str = "isolation_forest/model.joblib"
    lstm_artifact: str = "lstm_autoencoder/model.pt"
    model_poll_seconds: float = 30.0
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
