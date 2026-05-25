"""Tests for the TCN autoencoder sequence-detector backbone."""

from __future__ import annotations

from dataclasses import dataclass

import pytest

torch = pytest.importorskip("torch")

from src.models.tcn import (  # noqa: E402 — after importorskip guard
    TCNAutoencoder,
    TCNAutoencoderConfigLike,
    build_tcn_autoencoder,
    receptive_field,
)


def _model(
    *,
    input_size: int = 3,
    sequence_length: int = 8,
    channels: int = 8,
    kernel_size: int = 2,
    num_levels: int = 2,
    latent_dim: int = 4,
    dropout: float = 0.0,
    use_delta_decay: bool = True,
) -> TCNAutoencoder:
    model = TCNAutoencoder(
        input_size=input_size,
        sequence_length=sequence_length,
        channels=channels,
        kernel_size=kernel_size,
        num_levels=num_levels,
        latent_dim=latent_dim,
        dropout=dropout,
        use_delta_decay=use_delta_decay,
    )
    model.eval()
    return model


def _window(batch: int, seq: int, feats: int) -> tuple[torch.Tensor, torch.Tensor, torch.Tensor]:
    value = torch.randn(batch, seq, feats)
    mask = torch.ones(batch, seq, feats)
    delta = torch.zeros(batch, seq, feats)
    return value, mask, delta


def test_forward_preserves_window_shape() -> None:
    model = _model()
    value, mask, delta = _window(4, 8, 3)
    out = model.forward(value, mask, delta)
    assert tuple(out.shape) == (4, 8, 3)


def test_receptive_field_formula() -> None:
    # 1 + 2·(k-1)·(2^L - 1): k=2,L=2 -> 1 + 2·1·3 = 7; k=3,L=4 -> 1 + 4·15 = 61.
    assert receptive_field(kernel_size=2, num_levels=2) == 7
    assert receptive_field(kernel_size=3, num_levels=4) == 61
    assert _model().architecture.receptive_field == 7


def test_architecture_round_trips() -> None:
    arch = _model(use_delta_decay=True).architecture.to_dict()
    assert arch["input_layout"] == ["value", "mask", "delta"]
    assert arch["use_delta_decay"] is True
    assert arch["receptive_field"] == 7
    assert arch["channels"] == 8


def test_encoder_is_causal() -> None:
    """The dilated stack must be causal: perturbing the last input timestep
    leaves every earlier output untouched (only the last output moves)."""
    model = _model(channels=8, sequence_length=8)
    x = torch.randn(1, 8, 8)  # (B, channels, T) — straight into the conv stack
    y1 = model.encoder(x)
    x2 = x.clone()
    x2[:, :, -1] += 5.0
    y2 = model.encoder(x2)
    assert torch.allclose(y1[:, :, :-1], y2[:, :, :-1], atol=1e-6)
    assert not torch.allclose(y1[:, :, -1], y2[:, :, -1])


def test_delta_decay_freshness_is_one_at_zero_and_decays_with_lag() -> None:
    model = _model(use_delta_decay=True, input_size=3)
    assert model.delta_decay is not None
    zero = torch.zeros(2, 3, 8)
    # Zero-init weights ⇒ freshness 1 everywhere regardless of Δt at init.
    assert torch.allclose(model.delta_decay(zero), torch.ones_like(zero))
    # With a positive per-channel decay rate, larger Δt ⇒ smaller freshness.
    with torch.no_grad():
        model.delta_decay.weight.fill_(0.1)
    fresh = model.delta_decay(torch.full((1, 3, 4), 25.0))
    assert torch.all(fresh < 1.0)
    fresher = model.delta_decay(torch.full((1, 3, 4), 1.0))
    assert torch.all(fresher > fresh)


def test_reconstruction_error_reductions() -> None:
    model = _model()
    value, mask, delta = _window(4, 8, 3)

    def err(reduction: str) -> torch.Tensor:
        return model.reconstruction_error(value, mask, delta, reduction=reduction)  # type: ignore[arg-type]

    assert tuple(err("none").shape) == (4, 8, 3)
    assert tuple(err("sample").shape) == (4,)
    assert tuple(err("timestep").shape) == (4, 8)
    assert tuple(err("channel").shape) == (4, 3)
    assert err("global").ndim == 0


def test_masked_error_ignores_unobserved_cells() -> None:
    """A fully-unobserved window contributes no error; partial masks divide by
    the observed count, so a fabricated fill never inflates the score."""
    model = _model()
    value, _mask, delta = _window(2, 8, 3)
    zero_mask = torch.zeros(2, 8, 3)
    with torch.no_grad():
        assert float(model.reconstruction_error(value, zero_mask, delta, reduction="global")) == 0.0
        sample = model.reconstruction_error(value, zero_mask, delta, reduction="sample")
    assert torch.all(sample == 0.0)


def test_eval_forward_is_deterministic() -> None:
    model = _model(dropout=0.5)  # dropout present but disabled by eval()
    value, mask, delta = _window(2, 8, 3)
    assert torch.allclose(model.forward(value, mask, delta), model.forward(value, mask, delta))


def test_mask_only_variant_drops_delta_channels() -> None:
    model = _model(use_delta_decay=False, input_size=3, channels=8)
    assert model.delta_decay is None
    # encoder input projection sees 2F channels (value ‖ mask), not 3F.
    assert model.input_projection.in_channels == 6
    value, mask, delta = _window(1, 8, 3)
    assert tuple(model.forward(value, mask, delta).shape) == (1, 8, 3)


@pytest.mark.parametrize(
    "kwargs",
    [
        {"input_size": 0},
        {"kernel_size": 1},
        {"num_levels": 0},
        {"latent_dim": 0},
        {"channels": 0},
        {"dropout": 1.0},
    ],
)
def test_invalid_architecture_rejected(kwargs: dict[str, int | float]) -> None:
    with pytest.raises(ValueError):
        _model(**kwargs)  # type: ignore[arg-type]


def test_forward_rejects_mismatched_inputs() -> None:
    model = _model(input_size=3)
    value, mask, delta = _window(1, 8, 3)
    with pytest.raises(ValueError):
        model.forward(value[:, :, :2], mask, delta)  # wrong feature count
    with pytest.raises(ValueError):
        model.forward(value[0], mask[0], delta[0])  # not 3-D


def test_build_from_config() -> None:
    @dataclass(frozen=True)
    class _Cfg:
        enabled: bool = True
        sequence_length: int = 8
        channels: int = 8
        kernel_size: int = 2
        num_levels: int = 2
        latent_dim: int = 4
        dropout: float = 0.0
        use_delta_decay: bool = True

    cfg: TCNAutoencoderConfigLike = _Cfg()
    model = build_tcn_autoencoder(cfg, input_size=3, dtype="float32")
    value, mask, delta = _window(1, 8, 3)
    assert tuple(model.forward(value, mask, delta).shape) == (1, 8, 3)
