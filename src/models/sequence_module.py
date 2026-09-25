"""What the LSTM and TCN autoencoder modules share, torch-free (R04.12).

Both backbones reconstruct a ``(B, T, F)`` value window from value / mask / Δt
through their own ``encode`` / ``decode``; everything around that -- input
validation, ``forward`` and the masked reconstruction-error reductions the
calibration and the benchmark read -- is one implementation here. The mixin
holds no parameters, so a backbone's ``state_dict`` keys are unchanged and
checkpoints saved before the mixin load after it.

Nothing here imports torch at module level: :func:`module_base` resolves
``torch.nn.Module`` lazily and falls back to ``object`` so non-ML tooling can
import the backbone modules without Torch.
"""

from __future__ import annotations

import importlib
from typing import Any, Literal

ReconstructionReduction = Literal["none", "sample", "timestep", "channel", "global"]


def module_base() -> Any:
    """``torch.nn.Module`` when Torch is installed, else ``object``."""
    try:
        return importlib.import_module("torch.nn").Module
    except ImportError:
        return object


def masked_mean(masked_error: Any, mask: Any, *, dim: tuple[int, ...] | int) -> Any:
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


class MaskedReconstructionMixin:
    """``forward`` and masked ``reconstruction_error`` for a sequence autoencoder.

    Mix in *before* the module base (``class X(MaskedReconstructionMixin,
    module_base())``). The backbone supplies ``input_size`` and its own
    ``encode(value, mask, delta)`` / ``decode(latent, sequence_length=...)``.
    """

    input_size: int

    def encode(self, value: Any, mask: Any, delta: Any) -> Any:
        raise NotImplementedError

    def decode(self, latent: Any, *, sequence_length: int | None = None) -> Any:
        raise NotImplementedError

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
            return masked_mean(masked_error, mask, dim=(1, 2))
        if reduction == "timestep":
            return masked_mean(masked_error, mask, dim=2)
        if reduction == "channel":
            return masked_mean(masked_error, mask, dim=1)
        if reduction == "global":
            return masked_mean(masked_error, mask, dim=(0, 1, 2))
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


__all__ = [
    "MaskedReconstructionMixin",
    "ReconstructionReduction",
    "masked_mean",
    "module_base",
]
