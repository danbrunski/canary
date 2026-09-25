"""GRU-D masking-aware LSTM autoencoder for multi-rate telemetry windows.

canary's inputs arrive at very different rates (broadcast CAN ~1 Hz, round-robin
OBD PIDs every ~25 s, GPS ~1 Hz). Rather than forward-fill and pretend every
cell is real, this model follows Che et al. (*Recurrent Neural Networks for
Multivariate Time Series with Missing Values*, GRU-D): it consumes, per channel
and timestep, a **value**, an observed/stale **mask**, and **Δt** (seconds since
the channel's last real observation), and learns to **decay** stale inputs and
hidden state toward a trainable per-channel target (init = the channel mean,
0 in z-scored feature space — the paper's trainable-mean variant). The
reconstruction loss is masked (MSE in training, MAE for scoring) so a
forward-filled value never contributes.

The encoder is a GRU-D cell run as an explicit timestep loop (PyTorch's
``nn.GRU`` cannot express the decay). The decoder is a plain GRU/LSTM that
reconstructs the F-wide value sequence from the repeated latent. Imports stay
lazy so non-ML tooling can import ``src.models`` without Torch installed.
"""

from __future__ import annotations

import importlib
from dataclasses import dataclass
from typing import Any, Protocol

from src.models.sequence_module import (
    MaskedReconstructionMixin,
    module_base,
)


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


class GRUDEncoderCell(module_base()):  # type: ignore[misc]
    """One GRU-D recurrent cell with input + hidden-state decay.

    For per-channel input decay ``γ_x = exp(−relu(w_x ⊙ δ + b_x))`` (diagonal,
    length-F) and hidden decay ``γ_h = exp(−relu(W_h · δ + b_h))`` (projects δ →
    hidden). The decayed input is ``x̂ = m·x + (1−m)·(γ_x·x_last + (1−γ_x)·x̄)``
    — stale values relax toward a **trainable per-channel target** ``x̄``
    (init 0 = the global corpus mean in z-space; the GRU-D paper's
    trainable-mean variant). The hidden state is decayed before the GRU
    update: ``ĥ = γ_h ⊙ h``. The GRU update reads ``[x̂ ‖ m]`` so the network
    always knows which cells were observed.
    """

    input_size: int
    hidden_size: int
    gru_cell: Any
    decay_input_weight: Any
    decay_input_bias: Any
    decay_input_target: Any
    decay_hidden: Any

    def __init__(self, input_size: int, hidden_size: int) -> None:
        torch, nn = _torch_modules()
        super().__init__()
        self.input_size = input_size
        self.hidden_size = hidden_size
        # GRU update consumes [x̂ ‖ mask] → 2F-wide input.
        self.gru_cell = nn.GRUCell(input_size * 2, hidden_size)
        # Diagonal per-channel input decay: γ_x = exp(−relu(w_x ⊙ δ + b_x)).
        # w is init small-POSITIVE, not zero: with w = b = 0 the
        # pre-activation is identically 0 where PyTorch's ReLU has zero
        # gradient, so ∂loss/∂w = ∂loss/∂b = 0 for every batch and the gate
        # can never train — decay silently degenerates to permanent
        # hold-last-observed (the models deployed through May 2026 did
        # exactly that). Positive w gives every stale cell (δ > 0) a live
        # gradient while keeping the fresh-cell identity
        # γ_x(δ=0) = exp(−relu(b)) = 1 with b = 0.
        self.decay_input_weight = nn.Parameter(torch.empty(input_size).uniform_(0.01, 0.1))
        self.decay_input_bias = nn.Parameter(torch.zeros(input_size))
        # Decay target x̄: what a long-stale channel relaxes toward.
        # Trainable per channel because engine signals are mode mixtures —
        # the global z-space mean (0) blends idle/city/highway RPM into a
        # value no real gap drifts toward. Init 0 keeps the classic
        # decay-to-corpus-mean behavior at the start of training and exact
        # state-dict compatibility with pre-target checkpoints (whose w = 0
        # makes the target term inert anyway).
        self.decay_input_target = nn.Parameter(torch.zeros(input_size))
        # Hidden-state decay projects δ (F) → hidden: γ_h = exp(−relu(W_h·δ+b_h)).
        # Zero-init the bias so γ_h = 1 at δ=0 (no decay when nothing is stale);
        # the nn.Linear weight keeps its default nonzero init, which is why this
        # half of the decay always trained even when the input gate was stuck.
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
          ``mask``         — 1 where freshly observed, 0 where stale (what
                             "fresh" means is the artifact's
                             ``observation_mask`` rule, A03.1)
          ``delta``        — seconds since each channel's last observation
          ``hidden``       — previous hidden state
          ``last_observed``— last freshly-observed value per channel

        Returns ``(hidden_next, last_observed_next)``.
        """
        gamma_x = self.input_decay(delta)
        # Decay the last-observed value toward the trainable per-channel
        # target for stale channels; keep the real value where observed.
        x_hat = mask * value + (1.0 - mask) * (
            gamma_x * last_observed + (1.0 - gamma_x) * self.decay_input_target
        )
        gamma_h = self.hidden_decay(delta)
        decayed_hidden = gamma_h * hidden
        gru_input = _cat([x_hat, mask], dim=-1)
        hidden_next = self.gru_cell(gru_input, decayed_hidden)
        # Refresh the last-observed buffer only where this step was observed.
        last_observed_next = mask * value + (1.0 - mask) * last_observed
        return hidden_next, last_observed_next


class LSTMAutoencoder(MaskedReconstructionMixin, module_base()):  # type: ignore[misc]
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
