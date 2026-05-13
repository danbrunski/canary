"""Configuration models for CANary.

Loads `canary.yaml` and validates it. Unknown keys raise rather than being
silently dropped, so typos in config don't become silent drift at runtime.
"""

from __future__ import annotations

from pathlib import Path
from typing import Self

import yaml
from pydantic import BaseModel, ConfigDict, Field


class _Strict(BaseModel):
    model_config = ConfigDict(extra="forbid")


class VehicleConfig(_Strict):
    make: str
    model: str
    year: int
    vin: str = ""


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
    isolation_forest_artifact: str = "isolation_forest/model.joblib"
    lstm_artifact: str = "lstm_autoencoder/model.pt"
    model_poll_seconds: float = 30.0
    broadcaster_queue_size: int = 100


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
