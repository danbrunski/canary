"""Registry of selectable sequence-detector backbones.

The daemon's sequence-detector slot is config-selectable
(``models.sequence_detector``). Each backbone registers one
:class:`SequenceBackend` describing how to gate, locate, and load it. Adding a
TCN is then a single registry entry plus widening the config ``Literal`` — no
changes to the loader, serve loop, or fusion.

Loaders import torch lazily (inside ``load_model``) so importing this module
never pulls in the ML stack; availability is probed with ``importlib.util``.
"""

from __future__ import annotations

import importlib.util
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from src.config import CanaryConfig
    from src.models.sequence_protocol import TrainedSequenceModel


@dataclass(frozen=True)
class SequenceBackend:
    """One selectable sequence-detector implementation.

    ``selector`` is the ``models.sequence_detector`` value; ``model_type`` is
    the manifest type *and* the model-manager dict key (kept stable so the
    published anomaly ``source`` and dashboard vocab don't move). ``requires``
    lists import names that must be present (e.g. ``torch``) for the backend to
    load — absent ones are reported and the backend is skipped, not crashed.
    """

    selector: str
    model_type: str
    requires: tuple[str, ...]
    is_enabled: Callable[[CanaryConfig], bool]
    artifact_path: Callable[[CanaryConfig], Path]
    load_model: Callable[[Path], TrainedSequenceModel]

    def missing_requirements(self) -> tuple[str, ...]:
        return tuple(name for name in self.requires if importlib.util.find_spec(name) is None)


def _lstm_enabled(config: CanaryConfig) -> bool:
    return config.models.lstm_autoencoder.enabled


def _lstm_artifact(config: CanaryConfig) -> Path:
    return config.daemon.models_dir / config.daemon.lstm_artifact


def _load_lstm(path: Path) -> TrainedSequenceModel:
    from src.models.lstm_training import load_lstm_autoencoder_model

    return load_lstm_autoencoder_model(path)


LSTM_BACKEND = SequenceBackend(
    selector="lstm",
    model_type="lstm_autoencoder",
    requires=("torch",),
    is_enabled=_lstm_enabled,
    artifact_path=_lstm_artifact,
    load_model=_load_lstm,
)

#: All registered backbones, keyed by their config selector value. A TCN adds
#: one entry here (and one arm to the ``sequence_detector`` config ``Literal``).
SEQUENCE_BACKENDS: dict[str, SequenceBackend] = {LSTM_BACKEND.selector: LSTM_BACKEND}


def resolve_sequence_backend(config: CanaryConfig) -> SequenceBackend | None:
    """The backend the config selects, or ``None`` when the slot is disabled.

    ``None`` is returned both when the selected backend's own ``enabled`` flag
    is off and when the selector names no registered backend (shouldn't happen
    given the config ``Literal``, but keeps callers total).
    """
    backend = SEQUENCE_BACKENDS.get(config.models.sequence_detector)
    if backend is None or not backend.is_enabled(config):
        return None
    return backend


__all__ = [
    "LSTM_BACKEND",
    "SEQUENCE_BACKENDS",
    "SequenceBackend",
    "resolve_sequence_backend",
]
