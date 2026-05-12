"""Tests for the LSTM autoencoder architecture."""

from __future__ import annotations

from pathlib import Path

import pytest

from src.config import CanaryConfig
from src.models.lstm_autoencoder import (
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
    batch = torch.randn(4, 7, 5)

    reconstruction = model(batch)

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
    batch = torch.randn(2, 6, 4)

    latent = model.encode(batch)
    decoded = model.decode(latent)

    assert tuple(latent.shape) == (2, 3)
    assert tuple(decoded.shape) == (2, 6, 4)
    assert model.dropout == pytest.approx(0.25)


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
    assert model.encoder.dropout == 0.0


def test_reconstruction_error_reductions() -> None:
    torch = _torch()
    model = LSTMAutoencoder(
        input_size=3,
        sequence_length=5,
        hidden_size=7,
        num_layers=1,
        latent_dim=2,
    )
    batch = torch.randn(2, 5, 3)

    assert tuple(model.reconstruction_error(batch, reduction="none").shape) == (2, 5, 3)
    assert tuple(model.reconstruction_error(batch, reduction="sample").shape) == (2,)
    assert tuple(model.reconstruction_error(batch, reduction="timestep").shape) == (2, 5)
    assert tuple(model.reconstruction_error(batch, reduction="channel").shape) == (2, 3)
    assert model.reconstruction_error(batch, reduction="global").ndim == 0


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
        model(torch.randn(5, 3))
    with pytest.raises(ValueError, match="features"):
        model(torch.randn(2, 5, 4))


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
