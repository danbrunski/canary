"""LSTM autoencoder architecture for sequence telemetry windows.

The model consumes batches shaped ``(B, T, F)`` from
``SequenceWindowDataset`` and reconstructs the same shape. Imports are kept
lazy so non-ML tooling can still import ``src.models`` without Torch installed.
"""

from __future__ import annotations

import importlib
from dataclasses import dataclass
from typing import Any, Literal, Protocol

ReconstructionReduction = Literal["none", "sample", "timestep", "channel", "global"]


class LSTMAutoencoderConfigLike(Protocol):
    """Subset of ``src.config.LSTMAutoencoderConfig`` needed by the model."""

    enabled: bool
    sequence_length: int
    hidden_size: int
    num_layers: int
    latent_dim: int
    dropout: float


class MissingLSTMAutoencoderDependencyError(ImportError):
    """Raised when Torch is unavailable in the current environment."""


@dataclass(frozen=True)
class LSTMAutoencoderArchitecture:
    """JSON-safe architecture metadata for future manifests."""

    input_size: int
    sequence_length: int
    hidden_size: int
    num_layers: int
    latent_dim: int
    dropout: float

    def to_dict(self) -> dict[str, int | float]:
        return {
            "input_size": self.input_size,
            "sequence_length": self.sequence_length,
            "hidden_size": self.hidden_size,
            "num_layers": self.num_layers,
            "latent_dim": self.latent_dim,
            "dropout": self.dropout,
        }


def _module_base() -> Any:
    try:
        return importlib.import_module("torch.nn").Module
    except ImportError:
        return object


class LSTMAutoencoder(_module_base()):  # type: ignore[misc]
    """Encoder LSTM -> latent vector -> decoder LSTM autoencoder.

    ``forward`` returns a reconstruction with the same ``(B, T, F)`` shape as
    the input. ``encode`` exposes the latent representation for diagnostics,
    and ``reconstruction_error`` provides MAE summaries used by later
    threshold-calibration and inference tasks.
    """

    input_size: int
    sequence_length: int
    hidden_size: int
    num_layers: int
    latent_dim: int
    dropout: float
    encoder: Any
    to_latent: Any
    from_latent: Any
    decoder: Any
    output: Any

    def __init__(
        self,
        *,
        input_size: int,
        sequence_length: int,
        hidden_size: int,
        num_layers: int,
        latent_dim: int,
        dropout: float = 0.0,
    ) -> None:
        _torch, nn = _torch_modules()
        _validate_architecture(
            input_size=input_size,
            sequence_length=sequence_length,
            hidden_size=hidden_size,
            num_layers=num_layers,
            latent_dim=latent_dim,
            dropout=dropout,
        )
        super().__init__()
        self.input_size = input_size
        self.sequence_length = sequence_length
        self.hidden_size = hidden_size
        self.num_layers = num_layers
        self.latent_dim = latent_dim
        # PyTorch ignores single-layer LSTM dropout with a warning; make that
        # behavior explicit and warning-free.
        self.dropout = float(dropout if num_layers > 1 else 0.0)

        self.encoder = nn.LSTM(
            input_size=input_size,
            hidden_size=hidden_size,
            num_layers=num_layers,
            batch_first=True,
            dropout=self.dropout,
        )
        self.to_latent = nn.Linear(hidden_size, latent_dim)
        self.from_latent = nn.Linear(latent_dim, hidden_size)
        self.decoder = nn.LSTM(
            input_size=hidden_size,
            hidden_size=hidden_size,
            num_layers=num_layers,
            batch_first=True,
            dropout=self.dropout,
        )
        self.output = nn.Linear(hidden_size, input_size)

    @property
    def architecture(self) -> LSTMAutoencoderArchitecture:
        """Return serializable model architecture metadata."""
        return LSTMAutoencoderArchitecture(
            input_size=self.input_size,
            sequence_length=self.sequence_length,
            hidden_size=self.hidden_size,
            num_layers=self.num_layers,
            latent_dim=self.latent_dim,
            dropout=self.dropout,
        )

    def encode(self, batch: Any) -> Any:
        """Encode ``(B, T, F)`` windows into ``(B, latent_dim)`` vectors."""
        self._validate_input_batch(batch)
        _encoded, (hidden, _cell) = self.encoder(batch)
        return self.to_latent(hidden[-1])

    def decode(self, latent: Any, *, sequence_length: int | None = None) -> Any:
        """Decode ``(B, latent_dim)`` vectors into reconstructed windows."""
        length = self.sequence_length if sequence_length is None else sequence_length
        if length <= 0:
            raise ValueError("sequence_length must be positive")
        if latent.ndim != 2:
            raise ValueError("latent tensor must have shape (batch, latent_dim)")
        if int(latent.shape[-1]) != self.latent_dim:
            raise ValueError(
                f"latent tensor has {int(latent.shape[-1])} features; expected {self.latent_dim}"
            )

        decoded_seed = self.from_latent(latent)
        decoder_input = decoded_seed.unsqueeze(1).repeat(1, length, 1)
        decoded, _state = self.decoder(decoder_input)
        return self.output(decoded)

    def forward(self, batch: Any) -> Any:
        """Reconstruct a batch of sequence windows."""
        self._validate_input_batch(batch)
        sequence_length = int(batch.shape[1])
        return self.decode(self.encode(batch), sequence_length=sequence_length)

    def reconstruction_error(
        self,
        batch: Any,
        *,
        reduction: ReconstructionReduction = "sample",
    ) -> Any:
        """Return absolute reconstruction error summaries.

        Reductions:
          ``none`` -> ``(B, T, F)``
          ``sample`` -> ``(B,)``
          ``timestep`` -> ``(B, T)``
          ``channel`` -> ``(B, F)``
          ``global`` -> scalar
        """
        errors = (self.forward(batch) - batch).abs()
        if reduction == "none":
            return errors
        if reduction == "sample":
            return errors.mean(dim=(1, 2))
        if reduction == "timestep":
            return errors.mean(dim=2)
        if reduction == "channel":
            return errors.mean(dim=1)
        if reduction == "global":
            return errors.mean()
        raise ValueError(f"unknown reconstruction error reduction: {reduction}")

    def _validate_input_batch(self, batch: Any) -> None:
        if batch.ndim != 3:
            raise ValueError("input batch must have shape (batch, sequence, features)")
        if int(batch.shape[-1]) != self.input_size:
            raise ValueError(
                f"input batch has {int(batch.shape[-1])} features; expected {self.input_size}"
            )


def build_lstm_autoencoder(
    config: LSTMAutoencoderConfigLike,
    *,
    input_size: int,
    device: str | None = None,
    dtype: str = "float32",
) -> LSTMAutoencoder:
    """Instantiate an LSTM autoencoder from config and move it if requested."""
    torch, _nn = _torch_modules()
    model = LSTMAutoencoder(
        input_size=input_size,
        sequence_length=config.sequence_length,
        hidden_size=config.hidden_size,
        num_layers=config.num_layers,
        latent_dim=config.latent_dim,
        dropout=config.dropout,
    )
    move_kwargs: dict[str, Any] = {"dtype": getattr(torch, dtype)}
    if device is not None:
        move_kwargs["device"] = torch.device(device)
    model.to(**move_kwargs)
    return model


def _validate_architecture(
    *,
    input_size: int,
    sequence_length: int,
    hidden_size: int,
    num_layers: int,
    latent_dim: int,
    dropout: float,
) -> None:
    if input_size <= 0:
        raise ValueError("input_size must be positive")
    if sequence_length <= 0:
        raise ValueError("sequence_length must be positive")
    if hidden_size <= 0:
        raise ValueError("hidden_size must be positive")
    if num_layers <= 0:
        raise ValueError("num_layers must be positive")
    if latent_dim <= 0:
        raise ValueError("latent_dim must be positive")
    if dropout < 0 or dropout >= 1:
        raise ValueError("dropout must be >= 0 and < 1")


def _torch_modules() -> tuple[Any, Any]:
    try:
        torch = importlib.import_module("torch")
        nn = importlib.import_module("torch.nn")
    except ImportError as exc:
        raise MissingLSTMAutoencoderDependencyError(
            "torch is required for LSTMAutoencoder; run scripts/install_deps.py "
            "or install the 'ml' optional dependency"
        ) from exc
    return torch, nn
