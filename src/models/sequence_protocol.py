"""Backbone-agnostic contract for the sequence-autoencoder detector slot.

The daemon serves exactly one sequence detector at a time (the GRU-D LSTM
today; a TCN later). Both implementations satisfy these two structural
protocols, so the serve path (:class:`~src.models.inference.SequenceInferenceEngine`),
the hot-swap loader, and the backend registry stay implementation-agnostic — a
new backbone is a registry entry, not a new serve path.

This file deliberately imports nothing heavy (no torch): it is a typing
contract, importable from the daemon and config layers without pulling in the
ML stack.
"""

from __future__ import annotations

from typing import Any, Protocol


class SequenceAutoencoder(Protocol):
    """An ``nn.Module`` that reconstructs a ``(B, T, F)`` value window.

    ``forward``'s three inputs are the GRU-D serve contract: the z-scored value
    window, the observed-mask window (``1`` where a channel was freshly
    observed), and the Δt window (seconds since each channel was last observed).
    A plain dense autoencoder ignores ``mask``/``delta``; a TCN may consume them
    as extra channels — the contract only fixes the call shape so the engine can
    drive any backbone identically. ``to``/``eval`` are the ``nn.Module`` device
    + inference-mode hooks the engine calls at construction.
    """

    def forward(self, value: Any, mask: Any, delta: Any) -> Any: ...

    def reconstruction_error(
        self, value: Any, mask: Any, delta: Any, *, reduction: Any = ...
    ) -> Any:
        """Masked absolute reconstruction error (``reduction="none"``: ``(B, T,
        F)``, 0 where unobserved) -- what calibration and the false-positive
        benchmark score (R04.12; both backbones get it from
        :class:`~src.models.sequence_module.MaskedReconstructionMixin`)."""
        ...

    def to(self, *args: Any, **kwargs: Any) -> Any: ...

    def eval(self) -> Any: ...


class TrainedSequenceModel(Protocol):
    """A loaded sequence-detector bundle the inference engine serves.

    Satisfied by ``TrainedSequenceAutoencoder`` (both backbones).
    The engine reads ``model`` to reconstruct, ``feature_columns`` to align live
    rows, and ``manifest`` for the scaler, calibrated thresholds, window length,
    and version — ``manifest`` stays typed as ``Any`` to avoid binding the
    contract to one trainer's manifest subclass. Members are read-only
    properties so a ``frozen`` dataclass bundle (and covariant ``model``
    subtypes) satisfy the protocol.
    """

    @property
    def model(self) -> SequenceAutoencoder: ...

    @property
    def manifest(self) -> Any: ...

    @property
    def feature_columns(self) -> tuple[str, ...]: ...


__all__ = ["SequenceAutoencoder", "TrainedSequenceModel"]
