"""Configuration models for CANary.

Loads ``canary.yaml`` and validates it. Unknown keys raise rather than being
silently dropped, so typos in config don't become silent drift at runtime.

Compatibility facade (R21). ``src/config.py`` was one 1,740-line module; the
schemas now live in modules that depend one way, down to ``base``
(``tests/test_config_boundaries.py`` holds the map and enforces it):

================== ==========================================================
``base``           ``_Strict``, the broadcast-timeout and priority-channel
                   constants, ``validate_broadcast_stale_timeout``
``vehicle``        ``vehicle:`` -- ``VehicleParameters``, ``VehicleConfig``
``hardware``       ``hardware:`` -- CAN bus, GPS, IMU, ADCs, K-line
``collection``     ``collection:`` -- OBD polling, storage, replay, arming
``preprocessing``  ``preprocessing:`` -- resampling, operating-mode gates
``models``         ``models:`` and ``alerts:`` -- detectors, fusion, roles
``daemon``         ``dashboard:`` and ``daemon:``, the state-path tables
``auxiliary``      the small independent sections (trends ... twin_live)
``root``           ``CanaryConfig``: assembly, ``from_yaml``, the validators
                   that read two sections, the profile-scope private state
``scoping``        ``rebase_state_paths``, ``require_profile_scoped``
================== ==========================================================

This file defines nothing: every name is the owner's object, kept so
``from src.config import CanaryConfig`` and the other 53 names the single file
defined (private ones included) keep working. The package never imports this
file; a monkeypatch of a name used inside the package must target the module
that owns it.
"""

from __future__ import annotations

from src.config.auxiliary import AutoTagConfig as AutoTagConfig
from src.config.auxiliary import CommitteesConfig as CommitteesConfig
from src.config.auxiliary import CompanionConfig as CompanionConfig
from src.config.auxiliary import LLMAssistConfig as LLMAssistConfig
from src.config.auxiliary import NonvolatileWriteConfig as NonvolatileWriteConfig
from src.config.auxiliary import NotificationsConfig as NotificationsConfig
from src.config.auxiliary import OnlineTrainingConfig as OnlineTrainingConfig
from src.config.auxiliary import PlausibilityConfig as PlausibilityConfig
from src.config.auxiliary import ProfilesConfig as ProfilesConfig
from src.config.auxiliary import TrendsConfig as TrendsConfig
from src.config.auxiliary import TuningReadinessConfig as TuningReadinessConfig
from src.config.auxiliary import TwinLiveConfig as TwinLiveConfig
from src.config.base import (
    DEFAULT_BROADCAST_STALE_TIMEOUT_SECONDS as DEFAULT_BROADCAST_STALE_TIMEOUT_SECONDS,
)
from src.config.base import DEFAULT_PRIORITY_CHANNELS as DEFAULT_PRIORITY_CHANNELS
from src.config.base import (
    MIN_BROADCAST_STALE_TIMEOUT_SECONDS as MIN_BROADCAST_STALE_TIMEOUT_SECONDS,
)
from src.config.base import REWRITE_LEAD_SECONDS as REWRITE_LEAD_SECONDS
from src.config.base import _Strict as _Strict
from src.config.base import validate_broadcast_stale_timeout as validate_broadcast_stale_timeout
from src.config.collection import CollectionConfig as CollectionConfig
from src.config.collection import IdleShutdownConfig as IdleShutdownConfig
from src.config.collection import RecordArmingConfig as RecordArmingConfig
from src.config.collection import log as log
from src.config.daemon import DAEMON_STATE_LAYOUT as DAEMON_STATE_LAYOUT
from src.config.daemon import DAEMON_TRACKED_PATHS as DAEMON_TRACKED_PATHS
from src.config.daemon import DaemonConfig as DaemonConfig
from src.config.daemon import DashboardConfig as DashboardConfig
from src.config.hardware import CanBusConfig as CanBusConfig
from src.config.hardware import GpsConfig as GpsConfig
from src.config.hardware import HardwareConfig as HardwareConfig
from src.config.hardware import HatAdcConfig as HatAdcConfig
from src.config.hardware import HatRevisionSetting as HatRevisionSetting
from src.config.hardware import ImuConfig as ImuConfig
from src.config.hardware import KlineConfig as KlineConfig
from src.config.hardware import SensorCanConfig as SensorCanConfig
from src.config.hardware import SupplyVoltageConfig as SupplyVoltageConfig
from src.config.hardware import UsbAdcConfig as UsbAdcConfig
from src.config.models import AlertsConfig as AlertsConfig
from src.config.models import DetectorRole as DetectorRole
from src.config.models import DetectorRolesConfig as DetectorRolesConfig
from src.config.models import FusionConfig as FusionConfig
from src.config.models import IsolationForestConfig as IsolationForestConfig
from src.config.models import LSTMAutoencoderConfig as LSTMAutoencoderConfig
from src.config.models import ModelsConfig as ModelsConfig
from src.config.models import TCNAutoencoderConfig as TCNAutoencoderConfig
from src.config.models import _SequenceTrainingConfig as _SequenceTrainingConfig
from src.config.preprocessing import OperatingModeSpec as OperatingModeSpec
from src.config.preprocessing import PreprocessingConfig as PreprocessingConfig
from src.config.root import CanaryConfig as CanaryConfig
from src.config.scoping import UnscopedConfigError as UnscopedConfigError
from src.config.scoping import _daemon_path_fields as _daemon_path_fields
from src.config.scoping import _mentions_path as _mentions_path
from src.config.scoping import rebase_state_paths as rebase_state_paths
from src.config.scoping import require_profile_scoped as require_profile_scoped
from src.config.vehicle import VehicleConfig as VehicleConfig
from src.config.vehicle import VehicleParameters as VehicleParameters
