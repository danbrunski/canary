"""Tests for the LSTM autoencoder architecture."""

from __future__ import annotations

from pathlib import Path

import pytest

from src.config import CanaryConfig
from src.models.lstm_autoencoder import (
    GRUDEncoderCell,
    LSTMAutoencoder,
    MissingLSTMAutoencoderDependencyError,
    build_lstm_autoencoder,
)

REPO_ROOT = Path(__file__).resolve().parents[1]
EXAMPLE_YAML = REPO_ROOT / "canary.example.yaml"


def _torch():
    return pytest.importorskip("torch")


def _config():
    return CanaryConfig.from_yaml(EXAMPLE_YAML).models.lstm_autoencoder


def test_config_exposes_architecture_knobs() -> None:
    config = _config()

    assert config.sequence_length == 30
    assert config.hidden_size == 64
    assert config.num_layers == 1
    assert config.latent_dim == 32
    assert config.dropout == 0.0


def _vmd(torch, batch: int, length: int, features: int):
    """Value/mask/delta triple of fully-observed (mask 1, delta 0) inputs."""
    value = torch.randn(batch, length, features)
    mask = torch.ones(batch, length, features)
    delta = torch.zeros(batch, length, features)
    return value, mask, delta


def test_forward_reconstructs_same_shape() -> None:
    torch = _torch()
    torch.manual_seed(0)
    model = LSTMAutoencoder(
        input_size=5,
        sequence_length=7,
        hidden_size=11,
        num_layers=1,
        latent_dim=3,
    )
    value, mask, delta = _vmd(torch, 4, 7, 5)

    reconstruction = model(value, mask, delta)

    assert tuple(reconstruction.shape) == (4, 7, 5)


def test_encode_decode_shapes() -> None:
    torch = _torch()
    model = LSTMAutoencoder(
        input_size=4,
        sequence_length=6,
        hidden_size=8,
        num_layers=2,
        latent_dim=3,
        dropout=0.25,
    )
    value, mask, delta = _vmd(torch, 2, 6, 4)

    latent = model.encode(value, mask, delta)
    decoded = model.decode(latent)

    assert tuple(latent.shape) == (2, 3)
    assert tuple(decoded.shape) == (2, 6, 4)
    assert model.dropout == pytest.approx(0.25)


def test_gru_d_decay_is_identity_at_delta_zero_and_vanishes_at_large_delta() -> None:
    """γ_x → 1 and γ_h → 1 at δ=0 (default init b=0); both → 0 as δ → ∞.

    At δ=0 the decay is ``exp(−relu(b))`` and the parameters initialize to 0,
    so freshly-observed cells pass through undecayed. As δ grows the relu
    argument grows (non-negative weights drive it), so the decay collapses
    toward 0 — a long-stale value relaxes to the channel mean (0).
    """
    torch = _torch()
    cell = GRUDEncoderCell(input_size=3, hidden_size=5)
    zero_delta = torch.zeros(2, 3)
    assert torch.allclose(cell.input_decay(zero_delta), torch.ones(2, 3))
    assert torch.allclose(cell.hidden_decay(zero_delta), torch.ones(2, 5))

    # Force non-negative decay weights so a large δ pushes the relu argument up.
    with torch.no_grad():
        cell.decay_input_weight.fill_(1.0)
        cell.decay_hidden.weight.fill_(1.0)
        cell.decay_hidden.bias.zero_()
    big_delta = torch.full((2, 3), 1e6)
    with torch.no_grad():
        assert float(cell.input_decay(big_delta).max()) == pytest.approx(0.0)
        assert float(cell.hidden_decay(big_delta).max()) == pytest.approx(0.0)


def test_gru_d_cell_decays_stale_value_toward_mean() -> None:
    """A stale cell's contribution is ``γ_x · x_last``; with the decay weight
    large and δ large, x̂ collapses to 0 (the mean) regardless of x_last."""
    torch = _torch()
    cell = GRUDEncoderCell(input_size=2, hidden_size=4)
    with torch.no_grad():
        cell.decay_input_weight.fill_(1.0)
        cell.decay_input_bias.zero_()
    value = torch.zeros(1, 2)  # current value irrelevant where mask=0
    mask = torch.zeros(1, 2)  # everything stale
    delta = torch.full((1, 2), 1e6)
    last_observed = torch.full((1, 2), 999.0)
    # Reproduce x̂ = m·x + (1−m)·γ_x·x_last with the cell's own decay.
    with torch.no_grad():
        gamma_x = cell.input_decay(delta)
        x_hat = mask * value + (1.0 - mask) * (gamma_x * last_observed)
        assert float(x_hat.abs().max()) == pytest.approx(0.0, abs=1e-4)


def test_masked_reconstruction_error_ignores_filled_cells() -> None:
    """A cell with mask=0 must not contribute to reconstruction error, even if
    its (filled) value reconstructs badly."""
    torch = _torch()
    torch.manual_seed(1)
    model = LSTMAutoencoder(
        input_size=2,
        sequence_length=3,
        hidden_size=4,
        num_layers=1,
        latent_dim=2,
    )
    value = torch.randn(1, 3, 2)
    delta = torch.zeros(1, 3, 2)
    all_observed = torch.ones(1, 3, 2)

    # Mask out one cell; the per-cell masked error there must be exactly 0,
    # and the global score must equal the mean over only the observed cells.
    partial = all_observed.clone()
    partial[0, 1, 0] = 0.0
    with torch.no_grad():
        error_partial = model.reconstruction_error(value, partial, delta, reduction="none")
        assert float(error_partial[0, 1, 0]) == pytest.approx(0.0)
        masked_global = float(model.reconstruction_error(value, partial, delta, reduction="global"))
        observed_mean = float(error_partial.sum() / partial.sum())
        assert masked_global == pytest.approx(observed_mean, rel=1e-5)


def test_single_layer_dropout_is_disabled_without_warning() -> None:
    _torch()
    model = LSTMAutoencoder(
        input_size=4,
        sequence_length=6,
        hidden_size=8,
        num_layers=1,
        latent_dim=3,
        dropout=0.25,
    )

    assert model.dropout == 0.0
    assert model.decoder.dropout == 0.0


def test_reconstruction_error_reductions() -> None:
    torch = _torch()
    model = LSTMAutoencoder(
        input_size=3,
        sequence_length=5,
        hidden_size=7,
        num_layers=1,
        latent_dim=2,
    )
    value, mask, delta = _vmd(torch, 2, 5, 3)

    def _shape(reduction: str) -> tuple[int, ...]:
        return tuple(model.reconstruction_error(value, mask, delta, reduction=reduction).shape)

    assert _shape("none") == (2, 5, 3)
    assert _shape("sample") == (2,)
    assert _shape("timestep") == (2, 5)
    assert _shape("channel") == (2, 3)
    assert model.reconstruction_error(value, mask, delta, reduction="global").ndim == 0


def test_build_lstm_autoencoder_from_config() -> None:
    torch = _torch()
    config = _config().model_copy(update={"sequence_length": 4, "hidden_size": 6, "latent_dim": 2})

    model = build_lstm_autoencoder(config, input_size=3, dtype="float64")

    assert model.architecture.to_dict() == {
        "input_size": 3,
        "sequence_length": 4,
        "hidden_size": 6,
        "num_layers": 1,
        "latent_dim": 2,
        "dropout": 0.0,
        "decoder_type": "gru",
        "input_layout": ["value", "mask", "delta"],
    }
    assert next(model.parameters()).dtype == torch.float64


def test_invalid_architecture_raises_value_error() -> None:
    _torch()
    with pytest.raises(ValueError, match="input_size"):
        LSTMAutoencoder(
            input_size=0,
            sequence_length=5,
            hidden_size=8,
            num_layers=1,
            latent_dim=2,
        )
    with pytest.raises(ValueError, match="dropout"):
        LSTMAutoencoder(
            input_size=3,
            sequence_length=5,
            hidden_size=8,
            num_layers=1,
            latent_dim=2,
            dropout=1.0,
        )


def test_input_shape_validation() -> None:
    torch = _torch()
    model = LSTMAutoencoder(
        input_size=3,
        sequence_length=5,
        hidden_size=8,
        num_layers=1,
        latent_dim=2,
    )

    with pytest.raises(ValueError, match="shape"):
        model(torch.randn(5, 3), torch.ones(5, 3), torch.zeros(5, 3))
    with pytest.raises(ValueError, match="features"):
        model(torch.randn(2, 5, 4), torch.ones(2, 5, 4), torch.zeros(2, 5, 4))


def test_missing_torch_dependency_error(monkeypatch: pytest.MonkeyPatch) -> None:
    import src.models.lstm_autoencoder as module

    def fail_torch_modules():
        raise MissingLSTMAutoencoderDependencyError("torch unavailable")

    monkeypatch.setattr(module, "_torch_modules", fail_torch_modules)

    with pytest.raises(MissingLSTMAutoencoderDependencyError, match="torch unavailable"):
        module.LSTMAutoencoder(
            input_size=3,
            sequence_length=5,
            hidden_size=8,
            num_layers=1,
            latent_dim=2,
        )
