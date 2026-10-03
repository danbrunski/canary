"""Runtime control of the active sequence-detector backbone (Phase 9.6).

The daemon serves one sequence detector at a time (GRU-D LSTM or TCN). This
controller surfaces which backbone is live and performs an **in-process swap**:
it builds the target backbone's :class:`~src.models.manager.ModelManager`, points
``components.sequence_model_type`` at it, and drops the previous backbone's
manager so its model leaves RAM. The next inference tick reads the new manager
via ``components.model_managers.get(components.sequence_model_type)`` — IF and
the fusion layer are untouched, and the published anomaly source stays the
stable ``lstm_autoencoder`` vocabulary.

The swap is **not persisted**: ``models.sequence_detector`` in ``canary.yaml``
remains the durable default restored on restart. The control is for live
experimentation (e.g. A/B-ing backbones during a drive); set the durable
default in config.
"""

from __future__ import annotations

import threading
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Any, Protocol

from src.models.manager import manager_for_artifact
from src.models.manifest import manifest_path_for
from src.models.sequence_registry import SEQUENCE_BACKENDS
from src.storage.sidecar import read_json_object

if TYPE_CHECKING:
    from src.config import CanaryConfig
    from src.models.manager import ModelManager


class SequenceComponents(Protocol):
    """The members of the daemon's component record this controller reads and
    swaps (R09.8): ``control`` sits below ``daemon``, so it names what it uses
    instead of importing ``DaemonComponents``."""

    config: CanaryConfig
    model_managers: dict[str, ModelManager[Any]]
    sequence_model_type: str | None


class SequenceDetectorUnavailableError(RuntimeError):
    """The requested backbone can't be activated (e.g. torch is absent)."""


@dataclass(frozen=True)
class SequenceDetectorStatus:
    """Which sequence backbone is live + what's available to switch to."""

    #: Durable default from ``models.sequence_detector`` (config).
    selector: str
    #: ``model_type`` of the live backbone, or ``None`` when the slot is off.
    active: str | None
    #: Loaded model_version of the active backbone, or ``None`` (no artifact).
    active_version: str | None
    #: Per-backbone availability: ``model_type -> {selector, artifact_present,
    #: version, active}``.
    available: dict[str, dict[str, Any]]

    def to_dict(self) -> dict[str, Any]:
        return {
            "selector": self.selector,
            "active": self.active,
            "active_version": self.active_version,
            "available": self.available,
        }


class SequenceDetectorController:
    """Read + live-swap the active sequence-detector backbone."""

    def __init__(self, components: SequenceComponents) -> None:
        self._components = components
        self._lock = threading.Lock()

    def status(self) -> SequenceDetectorStatus:
        comp = self._components
        active = comp.sequence_model_type
        active_manager = comp.model_managers.get(active) if active is not None else None
        active_version: str | None = None
        if active_manager is not None and active_manager.current is not None:
            active_version = active_manager.current.manifest.model_version
        available: dict[str, dict[str, Any]] = {}
        for backend in SEQUENCE_BACKENDS.values():
            artifact = backend.artifact_path(comp.config)
            present = artifact.exists()
            available[backend.model_type] = {
                "selector": backend.selector,
                "artifact_present": present,
                "version": _manifest_version(artifact) if present else None,
                "active": backend.model_type == active,
            }
        return SequenceDetectorStatus(
            selector=comp.config.models.sequence_detector,
            active=active,
            active_version=active_version,
            available=available,
        )

    def switch(self, backbone: str) -> SequenceDetectorStatus:
        """Activate ``backbone`` (a selector value, e.g. ``"tcn"``) in-process.

        Builds the target manager (loading its artifact if present), swaps it
        into the sequence slot, and drops the previous backbone's manager.
        Raises ``ValueError`` for an unknown backbone and
        ``SequenceDetectorUnavailableError`` when its requirements (torch) are
        missing. Idempotent: re-selecting the live backbone just reloads it.
        """
        backend = SEQUENCE_BACKENDS.get(backbone)
        if backend is None:
            known = sorted(SEQUENCE_BACKENDS)
            raise ValueError(f"unknown sequence backbone {backbone!r}; expected one of {known}")
        missing = backend.missing_requirements()
        if missing:
            raise SequenceDetectorUnavailableError(
                f"cannot activate {backend.model_type}: missing {', '.join(missing)}"
            )
        with self._lock:
            comp = self._components
            # Build off the lock-protected section's critical bits only after the
            # (slow) artifact load so a concurrent switch can't interleave the
            # manager-dict mutations.
            manager = manager_for_artifact(
                backend.artifact_path(comp.config),
                backend.load_model,
                model_type=backend.model_type,
            )
            previous = comp.sequence_model_type
            comp.model_managers[backend.model_type] = manager
            if previous is not None and previous != backend.model_type:
                comp.model_managers.pop(previous, None)
            comp.sequence_model_type = backend.model_type
        return self.status()


def _manifest_version(artifact_path: Path) -> str | None:
    data = read_json_object(manifest_path_for(artifact_path))
    if data is None:
        return None
    version = data.get("model_version")
    return str(version) if version else None


__all__ = [
    "SequenceDetectorController",
    "SequenceDetectorStatus",
    "SequenceDetectorUnavailableError",
]
