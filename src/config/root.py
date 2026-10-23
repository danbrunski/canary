"""The root schema, ``CanaryConfig`` (R21).

Assembles every section and owns what reads more than one of them: the
``state_dir`` rooting of the paths outside ``daemon``, the broadcast timeout
against the session gap, the sequence cadence against the persistence
window, the profile-scope private state and ``from_yaml``. A section schema
never imports this module.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any, Self

from pydantic import PrivateAttr, model_validator

from src.config.auxiliary import (
    AutoTagConfig,
    CommitteesConfig,
    CompanionConfig,
    LLMAssistConfig,
    NonvolatileWriteConfig,
    NotificationsConfig,
    OnlineTrainingConfig,
    PlausibilityConfig,
    ProfilesConfig,
    TrendsConfig,
    TuningReadinessConfig,
    TwinLiveConfig,
)
from src.config.base import _Strict
from src.config.collection import CollectionConfig
from src.config.daemon import DaemonConfig, DashboardConfig
from src.config.hardware import HardwareConfig
from src.config.models import AlertsConfig, ModelsConfig
from src.config.preprocessing import PreprocessingConfig
from src.config.vehicle import VehicleConfig
from src.yaml_load import load_yaml


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
    nonvolatile_write: NonvolatileWriteConfig = NonvolatileWriteConfig()
    tuning_readiness: TuningReadinessConfig = TuningReadinessConfig()
    autotag: AutoTagConfig = AutoTagConfig()
    llm_assist: LLMAssistConfig = LLMAssistConfig()
    plausibility: PlausibilityConfig = PlausibilityConfig()
    committees: CommitteesConfig = CommitteesConfig()
    twin_live: TwinLiveConfig = TwinLiveConfig()
    companion: CompanionConfig = CompanionConfig()

    #: The profile slug :func:`src.vehicle.profiles.scope_config` nested this config's
    #: vehicle paths under; ``None`` for a config as loaded. Private, so no YAML
    #: can claim it: the one writer is ``scope_config``. It survives
    #: ``model_copy`` and pickling (the scoring child gets the parent's object).
    _scoped_profile: str | None = PrivateAttr(default=None)
    #: The ``VehicleProfile`` itself (opaque here: ``profiles`` sits above this
    #: module), so a freshly loaded YAML can be re-scoped exactly as this config
    #: was -- same slug, same registry vehicle parameters
    #: (:func:`src.vehicle.profiles.rescope_like`, the config hot-reload).
    _scope_source: Any = PrivateAttr(default=None)

    @property
    def scoped_profile(self) -> str | None:
        """The profile slug this config is scoped to, or ``None`` (unscoped)."""
        return self._scoped_profile

    @model_validator(mode="after")
    def _root_state_paths(self) -> Self:
        """The non-daemon runtime paths follow ``daemon.state_dir`` too (S03.1)."""
        state = self.daemon.state_dir
        if state is None:
            return self
        if "registry_overlay_path" not in self.profiles.model_fields_set:
            self.profiles = self.profiles.model_copy(
                update={"registry_overlay_path": state / "control" / "profiles.yaml"}
            )
        if "cards_path" not in self.llm_assist.model_fields_set:
            self.llm_assist = self.llm_assist.model_copy(
                update={"cards_path": state / "analyses" / "llm_assist_cards.jsonl"}
            )
        return self

    @model_validator(mode="after")
    def _companion_library(self) -> Self:
        """Companion mode's library follows ``daemon.state_dir``, and its
        ``sessions/`` is the daemon's session store, so the synced drives are
        the ones every session route and panel sees. Done here, not in a
        launcher, so a hand-written ``canary.yaml`` with ``companion.enabled``
        behaves the same."""
        if not self.companion.enabled:
            return self
        root = self.companion.library_root
        if root is None:
            state = self.daemon.state_dir
            root = state / "library" if state is not None else Path.home() / ".canary" / "library"
        root = root.expanduser()
        self.companion = self.companion.model_copy(update={"library_root": root})
        self.collection = self.collection.model_copy(update={"storage_path": root / "sessions"})
        return self

    @model_validator(mode="after")
    def _broadcast_timeout_inside_session_gap(self) -> Self:
        """An unchanged CAN id is re-written only once per broadcast stale
        timeout (P02.10). A timeout at or past ``session_gap_seconds`` would
        let the gap between two stored frames of a quiet, CAN-only session
        reach the resampler's segment split, cutting one recording into
        segments that are not real discontinuities."""
        timeout = self.collection.broadcast_stale_timeout_seconds
        gap = self.preprocessing.session_gap_seconds
        if timeout >= gap:
            raise ValueError(
                f"collection.broadcast_stale_timeout_seconds ({timeout:g} s) must be under "
                f"preprocessing.session_gap_seconds ({gap:g} s): an unchanged CAN id is "
                "re-written only once per timeout (P02.10), so a longer one would split "
                "quiet recordings into segments at the heartbeat gaps"
            )
        return self

    @model_validator(mode="after")
    def _sequence_cadence_can_latch(self) -> Self:
        """A06.6: at a forward cadence of n, the persistence gate sees one
        sequence verdict every n ticks. ``min_consecutive_ticks`` hits then span
        ``(M - 1) * n`` tick periods; at or past ``persistence_window_seconds``
        the oldest hit is evicted before the last arrives, and the detector can
        never latch, whatever it scores."""
        every_n = self.models.sequence_forward_every_n_ticks
        window = self.alerts.persistence_window_seconds
        if every_n == 1 or window is None:
            return self
        tick = 1.0 / max(self.preprocessing.resample_hz, 1.0)
        span = (self.alerts.min_consecutive_ticks - 1) * every_n * tick
        if span >= window:
            raise ValueError(
                f"models.sequence_forward_every_n_ticks ({every_n}) is too slow for the "
                f"persistence gate: {self.alerts.min_consecutive_ticks} sequence verdicts "
                f"span {span:g} s at one forward pass every {every_n * tick:g} s, which is "
                f"not inside alerts.persistence_window_seconds ({window:g} s), so the "
                "sequence detector could never latch (A06.6)"
            )
        return self

    @classmethod
    def from_yaml(cls, path: Path) -> Self:
        with path.open("r") as f:
            data = load_yaml(f)
        return cls.model_validate(data)
