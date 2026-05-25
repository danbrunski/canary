"""GRU-D masking-aware LSTM autoencoder for multi-rate telemetry windows.

canary's inputs arrive at very different rates (broadcast CAN ~1 Hz, round-robin
OBD PIDs every ~25 s, GPS ~1 Hz). Rather than forward-fill and pretend every
cell is real, this model follows Che et al. (*Recurrent Neural Networks for
Multivariate Time Series with Missing Values*, GRU-D): it consumes, per channel
and timestep, a **value**, an observed/stale **mask**, and **Δt** (seconds since
the channel's last real observation), and learns to **decay** stale inputs and
hidden state toward the channel mean (0 in z-scored feature space). The
reconstruction loss is masked so a forward-filled value never contributes.

The encoder is a GRU-D cell run as an explicit timestep loop (PyTorch's
``nn.GRU`` cannot express the decay). The decoder is a plain GRU/LSTM that
reconstructs the F-wide value sequence from the repeated latent. Imports stay
lazy so non-ML tooling can import ``src.models`` without Torch installed.
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
    """JSON-safe architecture metadata for manifests.

    ``input_layout`` documents the GRU-D serve contract: the encoder consumes a
    value tensor plus an observed-mask and a Δt tensor, all ``(B, T, F)`` in
    ``feature order``. ``decoder_type`` records whether the decoder is a GRU or
    LSTM so a checkpoint reconstructs the exact module. The learned per-channel
    input-decay and hidden-decay parameters live in the state dict (they are
    ``nn.Parameter`` / ``nn.Linear`` weights) and round-trip automatically.
    """

    input_size: int
    sequence_length: int
    hidden_size: int
    num_layers: int
    latent_dim: int
    dropout: float
    decoder_type: str = "gru"
    input_layout: tuple[str, ...] = ("value", "mask", "delta")

    def to_dict(self) -> dict[str, Any]:
        return {
            "input_size": self.input_size,
            "sequence_length": self.sequence_length,
            "hidden_size": self.hidden_size,
            "num_layers": self.num_layers,
            "latent_dim": self.latent_dim,
            "dropout": self.dropout,
            "decoder_type": self.decoder_type,
            "input_layout": list(self.input_layout),
        }


def _module_base() -> Any:
    try:
        return importlib.import_module("torch.nn").Module
    except ImportError:
        return object


class GRUDEncoderCell(_module_base()):  # type: ignore[misc]
    """One GRU-D recurrent cell with input + hidden-state decay.

    For per-channel input decay ``γ_x = exp(−relu(w_x ⊙ δ + b_x))`` (diagonal,
    length-F) and hidden decay ``γ_h = exp(−relu(W_h · δ + b_h))`` (projects δ →
    hidden). The decayed input is ``x̂ = m·x + (1−m)·γ_x·x_last`` — stale values
    relax toward 0 (the empirical mean in scaled space). The hidden state is
    decayed before the GRU update: ``ĥ = γ_h ⊙ h``. The GRU update reads
    ``[x̂ ‖ m]`` so the network always knows which cells were observed.
    """

    input_size: int
    hidden_size: int
    gru_cell: Any
    decay_input_weight: Any
    decay_input_bias: Any
    decay_hidden: Any

    def __init__(self, input_size: int, hidden_size: int) -> None:
        torch, nn = _torch_modules()
        super().__init__()
        self.input_size = input_size
        self.hidden_size = hidden_size
        # GRU update consumes [x̂ ‖ mask] → 2F-wide input.
        self.gru_cell = nn.GRUCell(input_size * 2, hidden_size)
        # Diagonal per-channel input decay: γ_x = exp(−relu(w_x ⊙ δ + b_x)).
        # Zero-init both so a freshly-observed cell (δ=0) passes through
        # undecayed (γ_x = exp(−relu(0)) = 1) at the start of training.
        self.decay_input_weight = nn.Parameter(torch.zeros(input_size))
        self.decay_input_bias = nn.Parameter(torch.zeros(input_size))
        # Hidden-state decay projects δ (F) → hidden: γ_h = exp(−relu(W_h·δ+b_h)).
        # Zero-init the bias so γ_h = 1 at δ=0 (no decay when nothing is stale).
        self.decay_hidden = nn.Linear(input_size, hidden_size)
        nn.init.zeros_(self.decay_hidden.bias)

    def input_decay(self, delta: Any) -> Any:
        """γ_x for a ``(B, F)`` Δt slice → ``(B, F)`` in (0, 1]."""
        torch, _nn = _torch_modules()
        return torch.exp(-torch.relu(self.decay_input_weight * delta + self.decay_input_bias))

    def hidden_decay(self, delta: Any) -> Any:
        """γ_h for a ``(B, F)`` Δt slice → ``(B, hidden)`` in (0, 1]."""
        torch, _nn = _torch_modules()
        return torch.exp(-torch.relu(self.decay_hidden(delta)))

    def forward(
        self,
        value: Any,
        mask: Any,
        delta: Any,
        hidden: Any,
        last_observed: Any,
    ) -> tuple[Any, Any]:
        """Advance one timestep.

        Args (all ``(B, F)`` except ``hidden`` which is ``(B, hidden)``):
          ``value``        — z-scored channel values (forward-filled allowed)
          ``mask``         — 1 where freshly observed, 0 where stale
          ``delta``        — seconds since each channel's last observation
          ``hidden``       — previous hidden state
          ``last_observed``— last freshly-observed value per channel

        Returns ``(hidden_next, last_observed_next)``.
        """
        gamma_x = self.input_decay(delta)
        # Decay the last-observed value toward 0 (the mean) for stale channels;
        # keep the real value where observed.
        x_hat = mask * value + (1.0 - mask) * (gamma_x * last_observed)
        gamma_h = self.hidden_decay(delta)
        decayed_hidden = gamma_h * hidden
        gru_input = _cat([x_hat, mask], dim=-1)
        hidden_next = self.gru_cell(gru_input, decayed_hidden)
        # Refresh the last-observed buffer only where this step was observed.
        last_observed_next = mask * value + (1.0 - mask) * last_observed
        return hidden_next, last_observed_next


class LSTMAutoencoder(_module_base()):  # type: ignore[misc]
    """GRU-D encoder -> latent vector -> plain decoder autoencoder.

    ``forward(value, mask, delta)`` returns a reconstruction with the same
    ``(B, T, F)`` shape as ``value``. ``encode`` exposes the latent for
    diagnostics; ``reconstruction_error`` returns **masked** MAE summaries so a
    forward-filled cell never contributes to the score.
    """

    input_size: int
    sequence_length: int
    hidden_size: int
    num_layers: int
    latent_dim: int
    dropout: float
    decoder_type: str
    encoder_cell: Any
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
        decoder_type: str = "gru",
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
        if decoder_type not in {"gru", "lstm"}:
            raise ValueError("decoder_type must be 'gru' or 'lstm'")
        super().__init__()
        self.input_size = input_size
        self.sequence_length = sequence_length
        self.hidden_size = hidden_size
        self.num_layers = num_layers
        self.latent_dim = latent_dim
        # PyTorch ignores single-layer RNN dropout with a warning; make that
        # behavior explicit and warning-free.
        self.dropout = float(dropout if num_layers > 1 else 0.0)
        self.decoder_type = decoder_type

        self.encoder_cell = GRUDEncoderCell(input_size, hidden_size)
        self.to_latent = nn.Linear(hidden_size, latent_dim)
        self.from_latent = nn.Linear(latent_dim, hidden_size)
        decoder_factory = nn.GRU if decoder_type == "gru" else nn.LSTM
        self.decoder = decoder_factory(
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
            decoder_type=self.decoder_type,
        )

    def encode(self, value: Any, mask: Any, delta: Any) -> Any:
        """Encode ``(B, T, F)`` windows into ``(B, latent_dim)`` vectors."""
        self._validate_inputs(value, mask, delta)
        torch, _nn = _torch_modules()
        batch_size = int(value.shape[0])
        hidden = torch.zeros(batch_size, self.hidden_size, dtype=value.dtype, device=value.device)
        last_observed = torch.zeros(
            batch_size, self.input_size, dtype=value.dtype, device=value.device
        )
        steps = int(value.shape[1])
        for t in range(steps):
            hidden, last_observed = self.encoder_cell(
                value[:, t, :],
                mask[:, t, :],
                delta[:, t, :],
                hidden,
                last_observed,
            )
        return self.to_latent(hidden)

    def decode(self, latent: Any, *, sequence_length: int | None = None) -> Any:
        """Decode ``(B, latent_dim)`` vectors into reconstructed value windows."""
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

    def forward(self, value: Any, mask: Any, delta: Any) -> Any:
        """Reconstruct a batch of sequence windows from value/mask/Δt."""
        self._validate_inputs(value, mask, delta)
        sequence_length = int(value.shape[1])
        return self.decode(self.encode(value, mask, delta), sequence_length=sequence_length)

    def reconstruction_error(
        self,
        value: Any,
        mask: Any,
        delta: Any,
        *,
        reduction: ReconstructionReduction = "sample",
    ) -> Any:
        """Return **masked** absolute reconstruction error summaries.

        Only observed cells (``mask == 1``) count; a forward-filled fill never
        contributes. Each reduction is a mask-weighted mean over the reduced
        axes (sum of masked abs error / sum of mask), so the per-window /
        per-channel scale matches the calibrated thresholds.

        Reductions:
          ``none`` -> ``(B, T, F)`` masked abs error (0 where unobserved)
          ``sample`` -> ``(B,)``
          ``timestep`` -> ``(B, T)``
          ``channel`` -> ``(B, F)``
          ``global`` -> scalar
        """
        reconstruction = self.forward(value, mask, delta)
        masked_error = (reconstruction - value).abs() * mask
        if reduction == "none":
            return masked_error
        if reduction == "sample":
            return _masked_mean(masked_error, mask, dim=(1, 2))
        if reduction == "timestep":
            return _masked_mean(masked_error, mask, dim=2)
        if reduction == "channel":
            return _masked_mean(masked_error, mask, dim=1)
        if reduction == "global":
            return _masked_mean(masked_error, mask, dim=(0, 1, 2))
        raise ValueError(f"unknown reconstruction error reduction: {reduction}")

    def _validate_inputs(self, value: Any, mask: Any, delta: Any) -> None:
        for name, tensor in (("value", value), ("mask", mask), ("delta", delta)):
            if tensor.ndim != 3:
                raise ValueError(f"{name} batch must have shape (batch, sequence, features)")
            if int(tensor.shape[-1]) != self.input_size:
                raise ValueError(
                    f"{name} batch has {int(tensor.shape[-1])} features; expected {self.input_size}"
                )
        if value.shape != mask.shape or value.shape != delta.shape:
            raise ValueError("value, mask, and delta must share the same shape")


def build_lstm_autoencoder(
    config: LSTMAutoencoderConfigLike,
    *,
    input_size: int,
    device: str | None = None,
    dtype: str = "float32",
    decoder_type: str = "gru",
) -> LSTMAutoencoder:
    """Instantiate a GRU-D autoencoder from config and move it if requested."""
    torch, _nn = _torch_modules()
    model = LSTMAutoencoder(
        input_size=input_size,
        sequence_length=config.sequence_length,
        hidden_size=config.hidden_size,
        num_layers=config.num_layers,
        latent_dim=config.latent_dim,
        dropout=config.dropout,
        decoder_type=decoder_type,
    )
    move_kwargs: dict[str, Any] = {"dtype": getattr(torch, dtype)}
    if device is not None:
        move_kwargs["device"] = torch.device(device)
    model.to(**move_kwargs)
    return model


def _cat(tensors: list[Any], *, dim: int) -> Any:
    torch, _nn = _torch_modules()
    return torch.cat(tensors, dim=dim)


def _masked_mean(masked_error: Any, mask: Any, *, dim: tuple[int, ...] | int) -> Any:
    """Mask-weighted mean: sum(masked_error) / sum(mask) over ``dim``.

    Guards a zero denominator (a window/channel with no observed cell) so the
    result is 0 rather than NaN — a fully-unobserved slice has no anomaly
    signal to contribute.
    """
    numerator = masked_error.sum(dim=dim)
    denominator = mask.sum(dim=dim)
    safe = denominator.clamp(min=1.0)
    result = numerator / safe
    return result * (denominator > 0)


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
