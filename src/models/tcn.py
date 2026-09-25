"""Temporal Convolutional Network autoencoder for multi-rate telemetry windows.

A selectable alternative to the GRU-D :class:`~src.models.lstm_autoencoder.LSTMAutoencoder`
that fills the *same* sequence-detector slot (see
:mod:`src.models.sequence_protocol`). It reconstructs a ``(B, T, F)`` value
window with stacked **dilated causal** Conv1d blocks (Bai et al., *An Empirical
Evaluation of Generic Convolutional and Recurrent Networks for Sequence
Modeling*), trained normal-only so reconstruction error flags anomalies — the
identical anomaly principle as the LSTM-AE, so the two are interchangeable.

Missing-data handling (the reason GRU-D exists) is kept but moved off the
recurrent cell: this module consumes the same ``(value, mask, delta)`` the
sequence buffer emits and concatenates, per channel, **mask** (observed/stale)
and a bounded **delta-decay** ``exp(-relu(w·Δt + b))`` (the GRU-D input-decay
gate, reused as a static *freshness* input channel) onto the value channels. So
the conv stack sees "this cell is stale and how stale" without an unbounded raw
Δt destabilizing it — and every op stays standard Conv/exp/relu, which keeps the
graph export-clean for a possible future NPU offload (a literal Conv1d maps to a
degenerate-height Conv2d; the GRU-D timestep loop does not).

Imports stay lazy so non-ML tooling can import ``src.models`` without Torch.
"""

from __future__ import annotations

import importlib
from dataclasses import dataclass
from typing import Any, Protocol

from src.models.sequence_module import (
    MaskedReconstructionMixin,
    ReconstructionReduction,
    module_base,
)


class TCNAutoencoderConfigLike(Protocol):
    """Subset of a future ``src.config.TCNAutoencoderConfig`` the model needs."""

    enabled: bool
    sequence_length: int
    channels: int
    kernel_size: int
    num_levels: int
    latent_dim: int
    dropout: float
    use_delta_decay: bool


class MissingTCNDependencyError(ImportError):
    """Raised when Torch is unavailable in the current environment."""


@dataclass(frozen=True)
class TCNAutoencoderArchitecture:
    """JSON-safe architecture metadata for manifests.

    ``input_layout`` documents the serve contract: the encoder consumes a value
    tensor plus an observed-mask and a Δt tensor, all ``(B, T, F)`` in feature
    order — identical to the GRU-D contract so the shared
    :class:`~src.models.inference.SequenceInferenceEngine` drives either model.
    ``receptive_field`` is the number of input timesteps the last encoder output
    depends on; a window shorter than this can't be fully contextualized.
    """

    input_size: int
    sequence_length: int
    channels: int
    kernel_size: int
    num_levels: int
    latent_dim: int
    dropout: float
    use_delta_decay: bool
    receptive_field: int
    input_layout: tuple[str, ...] = ("value", "mask", "delta")

    def to_dict(self) -> dict[str, Any]:
        return {
            "input_size": self.input_size,
            "sequence_length": self.sequence_length,
            "channels": self.channels,
            "kernel_size": self.kernel_size,
            "num_levels": self.num_levels,
            "latent_dim": self.latent_dim,
            "dropout": self.dropout,
            "use_delta_decay": self.use_delta_decay,
            "receptive_field": self.receptive_field,
            "input_layout": list(self.input_layout),
        }


def receptive_field(*, kernel_size: int, num_levels: int) -> int:
    """Timesteps the last output depends on for a 2-conv-per-block dilated TCN.

    Dilations double per level (``2**i``); each of the ``num_levels`` blocks has
    two conv layers, each contributing ``(k-1)·dilation``. The geometric sum of
    dilations is ``2**num_levels - 1``, so RF = ``1 + 2·(k-1)·(2**L - 1)``.
    """
    return 1 + 2 * (kernel_size - 1) * ((1 << num_levels) - 1)


class _Chomp1d(module_base()):  # type: ignore[misc]
    """Trim the trailing ``chomp`` timesteps a same-padding conv adds, making it
    causal: output ``t`` then depends only on inputs ``≤ t``."""

    chomp: int

    def __init__(self, chomp: int) -> None:
        super().__init__()
        self.chomp = chomp

    def forward(self, x: Any) -> Any:
        if self.chomp <= 0:
            return x
        return x[:, :, : -self.chomp].contiguous()


class _TemporalBlock(module_base()):  # type: ignore[misc]
    """Two dilated causal convs + residual (the standard TCN block)."""

    def __init__(
        self,
        in_channels: int,
        out_channels: int,
        *,
        kernel_size: int,
        dilation: int,
        dropout: float,
    ) -> None:
        _torch, nn = _torch_modules()
        super().__init__()
        padding = (kernel_size - 1) * dilation
        self.conv1 = nn.Conv1d(
            in_channels, out_channels, kernel_size, padding=padding, dilation=dilation
        )
        self.chomp1 = _Chomp1d(padding)
        self.relu1 = nn.ReLU()
        self.drop1 = nn.Dropout(dropout)
        self.conv2 = nn.Conv1d(
            out_channels, out_channels, kernel_size, padding=padding, dilation=dilation
        )
        self.chomp2 = _Chomp1d(padding)
        self.relu2 = nn.ReLU()
        self.drop2 = nn.Dropout(dropout)
        # 1x1 projection so the residual add lines up when channel counts differ.
        self.downsample = (
            nn.Conv1d(in_channels, out_channels, 1) if in_channels != out_channels else None
        )
        self.relu = nn.ReLU()

    def forward(self, x: Any) -> Any:
        out = self.drop1(self.relu1(self.chomp1(self.conv1(x))))
        out = self.drop2(self.relu2(self.chomp2(self.conv2(out))))
        residual = x if self.downsample is None else self.downsample(x)
        return self.relu(out + residual)


class _DeltaDecay(module_base()):  # type: ignore[misc]
    """Per-channel freshness ``exp(-relu(w ⊙ Δt + b))`` ∈ (0, 1].

    The GRU-D input-decay gate, reused here as a *static* bounded transform of
    Δt so the conv stack gets a well-scaled staleness channel instead of raw
    seconds. ``w`` is init small-positive, NOT zero: with ``w = b = 0`` the
    pre-activation is identically 0 where ReLU's gradient is 0, so the gate
    could never train and the freshness channel stayed a constant 1.0 —
    silently wasting a third of the encoder's input width. Positive ``w``
    gives stale cells a live gradient while a freshly-observed cell (Δt=0)
    still starts at freshness ``exp(−relu(b)) = 1`` with ``b = 0``.
    """

    weight: Any
    bias: Any

    def __init__(self, input_size: int) -> None:
        torch, nn = _torch_modules()
        super().__init__()
        self.weight = nn.Parameter(torch.empty(input_size).uniform_(0.01, 0.1))
        self.bias = nn.Parameter(torch.zeros(input_size))

    def forward(self, delta_bft: Any) -> Any:
        torch, _nn = _torch_modules()
        # weight/bias are length-F; broadcast over (B, F, T) via (1, F, 1).
        scaled = self.weight.unsqueeze(0).unsqueeze(-1) * delta_bft + self.bias.unsqueeze(
            0
        ).unsqueeze(-1)
        return torch.exp(-torch.relu(scaled))


class TCNAutoencoder(MaskedReconstructionMixin, module_base()):  # type: ignore[misc]
    """Dilated causal TCN encoder -> latent vector -> TCN decoder autoencoder.

    ``forward(value, mask, delta)`` returns a reconstruction with the same
    ``(B, T, F)`` shape as ``value``. The encoder consumes value channels
    concatenated with the mask and (optionally) the delta-decay freshness, so
    ``encoder_input_channels = F · (3 if use_delta_decay else 2)``.
    """

    input_size: int
    sequence_length: int
    channels: int
    kernel_size: int
    num_levels: int
    latent_dim: int
    dropout: float
    use_delta_decay: bool
    delta_decay: Any
    input_projection: Any
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
        channels: int = 64,
        kernel_size: int = 3,
        num_levels: int = 4,
        latent_dim: int = 32,
        dropout: float = 0.0,
        use_delta_decay: bool = True,
    ) -> None:
        _torch, nn = _torch_modules()
        _validate_architecture(
            input_size=input_size,
            sequence_length=sequence_length,
            channels=channels,
            kernel_size=kernel_size,
            num_levels=num_levels,
            latent_dim=latent_dim,
            dropout=dropout,
        )
        super().__init__()
        self.input_size = input_size
        self.sequence_length = sequence_length
        self.channels = channels
        self.kernel_size = kernel_size
        self.num_levels = num_levels
        self.latent_dim = latent_dim
        self.dropout = float(dropout)
        self.use_delta_decay = use_delta_decay

        self.delta_decay = _DeltaDecay(input_size) if use_delta_decay else None
        encoder_in = input_size * (3 if use_delta_decay else 2)
        self.input_projection = nn.Conv1d(encoder_in, channels, 1)
        self.encoder = nn.Sequential(
            *_build_temporal_blocks(
                channels, kernel_size=kernel_size, num_levels=num_levels, dropout=self.dropout
            )
        )
        self.to_latent = nn.Linear(channels, latent_dim)
        self.from_latent = nn.Linear(latent_dim, channels)
        self.decoder = nn.Sequential(
            *_build_temporal_blocks(
                channels, kernel_size=kernel_size, num_levels=num_levels, dropout=self.dropout
            )
        )
        self.output = nn.Conv1d(channels, input_size, 1)

    @property
    def architecture(self) -> TCNAutoencoderArchitecture:
        """Return serializable model architecture metadata."""
        return TCNAutoencoderArchitecture(
            input_size=self.input_size,
            sequence_length=self.sequence_length,
            channels=self.channels,
            kernel_size=self.kernel_size,
            num_levels=self.num_levels,
            latent_dim=self.latent_dim,
            dropout=self.dropout,
            use_delta_decay=self.use_delta_decay,
            receptive_field=receptive_field(
                kernel_size=self.kernel_size, num_levels=self.num_levels
            ),
        )

    def _encoder_input(self, value: Any, mask: Any, delta: Any) -> Any:
        """Assemble the ``(B, encoder_in, T)`` conv input from value/mask/Δt."""
        torch, _nn = _torch_modules()
        value_bft = value.transpose(1, 2)
        mask_bft = mask.transpose(1, 2)
        parts = [value_bft, mask_bft]
        if self.delta_decay is not None:
            parts.append(self.delta_decay(delta.transpose(1, 2)))
        return torch.cat(parts, dim=1)

    def encode(self, value: Any, mask: Any, delta: Any) -> Any:
        """Encode ``(B, T, F)`` windows into ``(B, latent_dim)`` vectors."""
        self._validate_inputs(value, mask, delta)
        projected = self.input_projection(self._encoder_input(value, mask, delta))
        encoded = self.encoder(projected)  # (B, channels, T)
        # Causal convs ⇒ the last timestep sees the full receptive field; use it
        # as the window summary fed to the latent projection.
        last_step = encoded[:, :, -1]
        return self.to_latent(last_step)

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
        seed = self.from_latent(latent)  # (B, channels)
        decoder_input = seed.unsqueeze(-1).repeat(1, 1, length)  # (B, channels, T)
        decoded = self.decoder(decoder_input)
        return self.output(decoded).transpose(1, 2)  # (B, T, F)


def build_tcn_autoencoder(
    config: TCNAutoencoderConfigLike,
    *,
    input_size: int,
    device: str | None = None,
    dtype: str = "float32",
) -> TCNAutoencoder:
    """Instantiate a TCN autoencoder from config and move it if requested."""
    torch, _nn = _torch_modules()
    model = TCNAutoencoder(
        input_size=input_size,
        sequence_length=config.sequence_length,
        channels=config.channels,
        kernel_size=config.kernel_size,
        num_levels=config.num_levels,
        latent_dim=config.latent_dim,
        dropout=config.dropout,
        use_delta_decay=config.use_delta_decay,
    )
    move_kwargs: dict[str, Any] = {"dtype": getattr(torch, dtype)}
    if device is not None:
        move_kwargs["device"] = torch.device(device)
    model.to(**move_kwargs)
    return model


def _build_temporal_blocks(
    channels: int, *, kernel_size: int, num_levels: int, dropout: float
) -> list[Any]:
    """Stack of ``num_levels`` dilated causal blocks, dilation doubling per level."""
    return [
        _TemporalBlock(
            channels, channels, kernel_size=kernel_size, dilation=2**level, dropout=dropout
        )
        for level in range(num_levels)
    ]


def _validate_architecture(
    *,
    input_size: int,
    sequence_length: int,
    channels: int,
    kernel_size: int,
    num_levels: int,
    latent_dim: int,
    dropout: float,
) -> None:
    if input_size <= 0:
        raise ValueError("input_size must be positive")
    if sequence_length <= 0:
        raise ValueError("sequence_length must be positive")
    if channels <= 0:
        raise ValueError("channels must be positive")
    if kernel_size < 2:
        raise ValueError("kernel_size must be >= 2 (kernel 1 has no temporal receptive field)")
    if num_levels <= 0:
        raise ValueError("num_levels must be positive")
    if latent_dim <= 0:
        raise ValueError("latent_dim must be positive")
    if dropout < 0 or dropout >= 1:
        raise ValueError("dropout must be >= 0 and < 1")


def _torch_modules() -> tuple[Any, Any]:
    try:
        torch = importlib.import_module("torch")
        nn = importlib.import_module("torch.nn")
    except ImportError as exc:
        raise MissingTCNDependencyError(
            "torch is required for TCNAutoencoder; run scripts/install_deps.py "
            "or install the 'ml' optional dependency"
        ) from exc
    return torch, nn


__all__ = [
    "MissingTCNDependencyError",
    "ReconstructionReduction",
    "TCNAutoencoder",
    "TCNAutoencoderArchitecture",
    "TCNAutoencoderConfigLike",
    "build_tcn_autoencoder",
    "receptive_field",
]
