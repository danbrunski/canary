"""Registry of selectable sequence-detector backbones.

The daemon's sequence-detector slot is config-selectable
(``models.sequence_detector``). Each backbone registers one
:class:`SequenceBackend` describing how to gate, locate, and load it. Adding a
TCN is then a single registry entry plus widening the config ``Literal`` — no
changes to the loader, serve loop, or fusion.

Loaders import torch lazily (inside ``load_model``) so importing this module
never pulls in the ML stack; availability is probed with ``importlib.util``.

Task 2.1 additionally parks the training-side unification here:
:class:`SequenceAutoencoderManifest` (one manifest dataclass replacing the two
byte-compatible clones ``LSTMAutoencoderManifest``/``TCNAutoencoderManifest``
used to carry) and :class:`SequenceTrainingBackend` (the training/persistence
counterpart to :class:`SequenceBackend`, describing how to build a fresh
model, reconstruct one from a saved ``architecture`` dict, and the one
genuine load-time divergence between backbones — see its docstring). The
same lazy-import discipline applies: ``build_model``/``reconstruct_model``/
``missing_dependency_error_cls`` are function references that import torch
(via the backbone's own module) only when called, never at registry-import
time.
"""

from __future__ import annotations

import importlib.util
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Any

from src.coerce import optional_str as _optional_str
from src.models.sequence_training import EpochLoss

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


def _tcn_enabled(config: CanaryConfig) -> bool:
    return config.models.tcn_autoencoder.enabled


def _tcn_artifact(config: CanaryConfig) -> Path:
    return config.daemon.models_dir / config.daemon.tcn_artifact


def _load_tcn(path: Path) -> TrainedSequenceModel:
    from src.models.tcn_training import load_tcn_autoencoder_model

    return load_tcn_autoencoder_model(path)


LSTM_BACKEND = SequenceBackend(
    selector="lstm",
    model_type="lstm_autoencoder",
    requires=("torch",),
    is_enabled=_lstm_enabled,
    artifact_path=_lstm_artifact,
    load_model=_load_lstm,
)

TCN_BACKEND = SequenceBackend(
    selector="tcn",
    model_type="tcn_autoencoder",
    requires=("torch",),
    is_enabled=_tcn_enabled,
    artifact_path=_tcn_artifact,
    load_model=_load_tcn,
)

#: All registered backbones, keyed by their config selector value. Adding a new
#: backbone is one entry here plus one arm on the ``sequence_detector``
#: config ``Literal``.
SEQUENCE_BACKENDS: dict[str, SequenceBackend] = {
    backend.selector: backend for backend in (LSTM_BACKEND, TCN_BACKEND)
}


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


@dataclass(frozen=True)
class SequenceAutoencoderManifest:
    """JSON-safe metadata for a trained sequence-autoencoder checkpoint.

    One dataclass for both backbones (Task 2.1): ``LSTMAutoencoderManifest``
    and ``TCNAutoencoderManifest`` were byte-compatible clones — every field,
    ``to_dict``, and ``from_dict`` identical — because only ``model_type`` and
    the free-form ``architecture`` block actually vary per backbone, and
    ``architecture`` was already a ``Mapping`` with no fixed shape. The live
    :class:`~src.models.inference.SequenceInferenceEngine` and hot-swap
    ``ModelManager`` read this manifest with no per-backbone branching.
    ``src.models.lstm_training.LSTMAutoencoderManifest`` and
    ``src.models.tcn_training.TCNAutoencoderManifest`` are now aliases of this
    class, kept so existing imports of either name keep working.
    """

    artifact_format_version: int
    model_type: str
    model_version: str
    created_at: str
    training_data_hash: str
    training_start_timestamp: str | None
    training_end_timestamp: str | None
    training_sessions: tuple[str, ...]
    input_rows: int
    input_windows: int
    train_windows: int
    validation_windows: int
    best_epoch: int
    best_train_loss: float
    best_validation_loss: float | None
    stopped_epoch: int
    early_stopped: bool
    architecture: Mapping[str, Any]
    hyperparams: Mapping[str, Any]
    schema: Mapping[str, Any]
    history: tuple[EpochLoss, ...]
    thresholds: Mapping[str, Any] | None = None
    #: Per-channel z-score scaler ({means, stds, columns, std_floor}) fit on
    #: the training corpus and applied at inference. Pre-scaler artifacts
    #: (May 2026 era) have ``None`` here; the load-time guard refuses them
    #: when thresholds are also present, since a calibrated threshold against
    #: raw-magnitude reconstruction errors won't match scaled-input
    #: reconstruction errors at inference.
    scaler: Mapping[str, Any] | None = None
    #: Threshold-calibration provenance (Task 3.1): ``"held_out"`` when
    #: ``calibration_dataset`` was supplied, ``"in_sample"`` when the caller
    #: opted into ``allow_in_sample_calibration=True`` (in-sample thresholds
    #: underestimate cross-session error — the mechanism behind an earlier
    #: FPR≈1.0 flood), or ``None`` when ``calibrate_thresholds=False`` left
    #: the model uncalibrated. Load-time (``ModelManager``) logs a warning
    #: when serving an ``"in_sample"``-stamped artifact.
    calibration: str | None = None
    calibration_sessions: tuple[str, ...] = ()
    calibration_health: Mapping[str, Any] | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "artifact_format_version": self.artifact_format_version,
            "model_type": self.model_type,
            "model_version": self.model_version,
            "created_at": self.created_at,
            "training_data_hash": self.training_data_hash,
            "training_start_timestamp": self.training_start_timestamp,
            "training_end_timestamp": self.training_end_timestamp,
            "training_sessions": list(self.training_sessions),
            "input_rows": self.input_rows,
            "input_windows": self.input_windows,
            "train_windows": self.train_windows,
            "validation_windows": self.validation_windows,
            "best_epoch": self.best_epoch,
            "best_train_loss": self.best_train_loss,
            "best_validation_loss": self.best_validation_loss,
            "stopped_epoch": self.stopped_epoch,
            "early_stopped": self.early_stopped,
            "architecture": dict(self.architecture),
            "hyperparams": dict(self.hyperparams),
            "schema": dict(self.schema),
            "history": [loss.to_dict() for loss in self.history],
            "thresholds": None if self.thresholds is None else dict(self.thresholds),
            "scaler": None if self.scaler is None else dict(self.scaler),
            "calibration": self.calibration,
            "calibration_sessions": list(self.calibration_sessions),
            "calibration_health": (
                None if self.calibration_health is None else dict(self.calibration_health)
            ),
        }

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> SequenceAutoencoderManifest:
        return cls(
            artifact_format_version=int(data["artifact_format_version"]),
            model_type=str(data["model_type"]),
            model_version=str(data["model_version"]),
            created_at=str(data["created_at"]),
            training_data_hash=str(data["training_data_hash"]),
            training_start_timestamp=_optional_str(data.get("training_start_timestamp")),
            training_end_timestamp=_optional_str(data.get("training_end_timestamp")),
            training_sessions=tuple(str(session) for session in data["training_sessions"]),
            input_rows=int(data.get("input_rows", data["input_windows"])),
            input_windows=int(data["input_windows"]),
            train_windows=int(data["train_windows"]),
            validation_windows=int(data["validation_windows"]),
            best_epoch=int(data["best_epoch"]),
            best_train_loss=float(data["best_train_loss"]),
            best_validation_loss=_optional_float(data.get("best_validation_loss")),
            stopped_epoch=int(data["stopped_epoch"]),
            early_stopped=bool(data["early_stopped"]),
            architecture=dict(data["architecture"]),
            hyperparams=dict(data["hyperparams"]),
            schema=dict(data["schema"]),
            history=tuple(EpochLoss.from_dict(item) for item in data["history"]),
            thresholds=None if data.get("thresholds") is None else dict(data["thresholds"]),
            scaler=None if data.get("scaler") is None else dict(data["scaler"]),
            calibration=_optional_str(data.get("calibration")),
            calibration_sessions=tuple(str(x) for x in data.get("calibration_sessions", [])),
            calibration_health=(
                None if data.get("calibration_health") is None else dict(data["calibration_health"])
            ),
        )


def _optional_float(value: Any) -> float | None:
    if value is None:
        return None
    return float(value)


@dataclass(frozen=True)
class SequenceTrainingBackend:
    """Per-backbone training + persistence behavior (Task 2.1 unification).

    The training-side companion to :class:`SequenceBackend` (which only
    covers serving). ``config_attr`` names the ``CanaryConfig.models`` block
    holding this backbone's training knobs (e.g. ``"lstm_autoencoder"``);
    ``build_model``/``reconstruct_model`` genuinely differ per backbone (the
    architecture kwargs — ``hidden_size``/``num_layers`` vs.
    ``channels``/``kernel_size``/``num_levels`` — don't share a shape), so
    they stay backbone-owned closures rather than something to unify away.

    ``tolerated_missing_state_keys`` is the *one* real load-time divergence
    the refactoring doc names: pre-``decay_input_target`` LSTM checkpoints are
    missing that one GRU-D decay key (their decay weights are zero, which
    makes the target term ``(1−γ_x)·x̄`` inert regardless of x̄, so the
    zeros-init reproduces the old behavior bit-for-bit) — LSTM loads with
    ``strict=False`` and tolerates exactly that key, anything else missing or
    unexpected still fails loudly. TCN has no such legacy checkpoint shape, so
    this is empty and its load stays fully strict. This is a genuine
    backbone-specific hook, not something applied to both or silently dropped.

    The doc also named a "calibrated-without-scaler guard" as LSTM-specific;
    reading the current code shows it is *not* — both backbones already carry
    byte-identical guards (added independently, evidently before this task).
    The unification below shares that guard rather than hooking it, and
    ``tests/test_sequence_autoencoder_training.py`` pins that both backbones
    enforce it symmetrically.
    """

    selector: str
    model_type: str
    config_attr: str
    missing_dependency_error_cls: Callable[[], type[Exception]]
    build_model: Callable[..., Any]
    reconstruct_model: Callable[[Mapping[str, Any]], Any]
    tolerated_missing_state_keys: frozenset[str] = frozenset()


def _build_lstm_model(config: Any, *, input_size: int, device: str, dtype: str) -> Any:
    from src.models.lstm_autoencoder import build_lstm_autoencoder

    return build_lstm_autoencoder(config, input_size=input_size, device=device, dtype=dtype)


def _reconstruct_lstm_model(architecture: Mapping[str, Any]) -> Any:
    from src.models.lstm_autoencoder import LSTMAutoencoder

    return LSTMAutoencoder(
        input_size=int(architecture["input_size"]),
        sequence_length=int(architecture["sequence_length"]),
        hidden_size=int(architecture["hidden_size"]),
        num_layers=int(architecture["num_layers"]),
        latent_dim=int(architecture["latent_dim"]),
        dropout=float(architecture["dropout"]),
        decoder_type=str(architecture.get("decoder_type", "gru")),
    )


def _lstm_missing_dependency_error_cls() -> type[Exception]:
    from src.models.lstm_autoencoder import MissingLSTMAutoencoderDependencyError

    return MissingLSTMAutoencoderDependencyError


def _build_tcn_model(config: Any, *, input_size: int, device: str, dtype: str) -> Any:
    from src.models.tcn import build_tcn_autoencoder

    return build_tcn_autoencoder(config, input_size=input_size, device=device, dtype=dtype)


def _reconstruct_tcn_model(architecture: Mapping[str, Any]) -> Any:
    from src.models.tcn import TCNAutoencoder

    return TCNAutoencoder(
        input_size=int(architecture["input_size"]),
        sequence_length=int(architecture["sequence_length"]),
        channels=int(architecture["channels"]),
        kernel_size=int(architecture["kernel_size"]),
        num_levels=int(architecture["num_levels"]),
        latent_dim=int(architecture["latent_dim"]),
        dropout=float(architecture["dropout"]),
        use_delta_decay=bool(architecture.get("use_delta_decay", True)),
    )


def _tcn_missing_dependency_error_cls() -> type[Exception]:
    from src.models.tcn import MissingTCNDependencyError

    return MissingTCNDependencyError


LSTM_TRAINING_BACKEND = SequenceTrainingBackend(
    selector="lstm",
    model_type="lstm_autoencoder",
    config_attr="lstm_autoencoder",
    missing_dependency_error_cls=_lstm_missing_dependency_error_cls,
    build_model=_build_lstm_model,
    reconstruct_model=_reconstruct_lstm_model,
    tolerated_missing_state_keys=frozenset({"encoder_cell.decay_input_target"}),
)

TCN_TRAINING_BACKEND = SequenceTrainingBackend(
    selector="tcn",
    model_type="tcn_autoencoder",
    config_attr="tcn_autoencoder",
    missing_dependency_error_cls=_tcn_missing_dependency_error_cls,
    build_model=_build_tcn_model,
    reconstruct_model=_reconstruct_tcn_model,
    tolerated_missing_state_keys=frozenset(),
)

#: All registered training backbones, keyed by ``models.sequence_detector``
#: selector value (``scripts/train_sequence.py --backbone``).
SEQUENCE_TRAINING_BACKENDS: dict[str, SequenceTrainingBackend] = {
    backend.selector: backend for backend in (LSTM_TRAINING_BACKEND, TCN_TRAINING_BACKEND)
}

#: Same registry, keyed by manifest ``model_type`` — lets persistence code
#: (``TrainedSequenceAutoencoder.save``) recover the right backend from a
#: manifest alone, without threading a backend argument through every call.
SEQUENCE_TRAINING_BACKENDS_BY_MODEL_TYPE: dict[str, SequenceTrainingBackend] = {
    backend.model_type: backend for backend in (LSTM_TRAINING_BACKEND, TCN_TRAINING_BACKEND)
}


def sequence_training_backend_for_model_type(model_type: str) -> SequenceTrainingBackend:
    """The training backend that produced a manifest's ``model_type``.

    Raises :class:`KeyError` for an unknown ``model_type`` — callers already
    validate ``model_type`` against ``MODEL_TYPE`` before this is reached, so
    that should never happen outside a corrupted/foreign manifest.
    """
    return SEQUENCE_TRAINING_BACKENDS_BY_MODEL_TYPE[model_type]


__all__ = [
    "LSTM_BACKEND",
    "LSTM_TRAINING_BACKEND",
    "SEQUENCE_BACKENDS",
    "SEQUENCE_TRAINING_BACKENDS",
    "SEQUENCE_TRAINING_BACKENDS_BY_MODEL_TYPE",
    "TCN_BACKEND",
    "TCN_TRAINING_BACKEND",
    "SequenceAutoencoderManifest",
    "SequenceBackend",
    "SequenceTrainingBackend",
    "resolve_sequence_backend",
    "sequence_training_backend_for_model_type",
]
