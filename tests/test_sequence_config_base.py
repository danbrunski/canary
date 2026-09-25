"""R03.10 — one base for the LSTM/TCN sequence-detector config models.

The two backbones carried 17 identical training fields, so a new knob could
reach one and not the other. Both now inherit ``_SequenceTrainingConfig``;
these pin that the move changed neither model's JSON schema.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from src.config import LSTMAutoencoderConfig, TCNAutoencoderConfig, _SequenceTrainingConfig

SCHEMAS = Path(__file__).parent / "fixtures" / "sequence_config_schemas.json"


def test_both_backbones_share_every_training_field() -> None:
    shared = set(_SequenceTrainingConfig.model_fields)
    assert shared <= set(LSTMAutoencoderConfig.model_fields)
    assert shared <= set(TCNAutoencoderConfig.model_fields)
    assert set(LSTMAutoencoderConfig.model_fields) - shared == {"hidden_size", "num_layers"}
    assert set(TCNAutoencoderConfig.model_fields) - shared == {
        "channels",
        "kernel_size",
        "num_levels",
        "use_delta_decay",
    }


def test_lstm_keeps_its_required_fields() -> None:
    with pytest.raises(ValueError):
        LSTMAutoencoderConfig()  # type: ignore[call-arg]
    required = {name for name, f in LSTMAutoencoderConfig.model_fields.items() if f.is_required()}
    assert required == {"enabled", "sequence_length", "latent_dim", "threshold_k"}
    assert TCNAutoencoderConfig().enabled is False


def test_json_schemas_are_unchanged_by_the_base() -> None:
    """Recorded from the pre-R03.10 classes. Compared as JSON objects: the
    base moves ``hidden_size``/``num_layers`` (LSTM) and the TCN-only fields
    after the shared ones, which changes key order and nothing else."""
    expected = json.loads(SCHEMAS.read_text())
    assert LSTMAutoencoderConfig.model_json_schema() == expected["lstm"]
    assert TCNAutoencoderConfig.model_json_schema() == expected["tcn"]
