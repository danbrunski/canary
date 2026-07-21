"""Smoke tests for the config loader."""

from pathlib import Path

import pytest
from pydantic import ValidationError

from src.config import CanaryConfig

REPO_ROOT = Path(__file__).resolve().parents[1]
EXAMPLE_YAML = REPO_ROOT / "canary.example.yaml"
PI_EXAMPLE_YAML = REPO_ROOT / "canary.pi.example.yaml"


def test_example_yaml_loads():
    config = CanaryConfig.from_yaml(EXAMPLE_YAML)
    assert config.vehicle.make == "Nissan"
    assert config.vehicle.year == 2008
    assert config.collection.obd_poll_rate_hz == 1
    assert config.collection.obd_source == "canable"
    assert config.collection.obd_pids == ("auto",)
    assert config.collection.obd_did_request_id == 0x7E0
    assert config.collection.reconnect_initial_delay_seconds == 10
    assert config.collection.queue_max_size == 10000
    assert "idle" in config.preprocessing.operating_modes
    assert config.preprocessing.operating_modes["idle"].rpm_max == 900
    assert config.models.isolation_forest.enabled is True
    assert config.models.isolation_forest.random_state == 42
    assert config.models.isolation_forest.per_mode_contamination == {}
    assert config.dashboard.port == 8000
    assert config.daemon.jobs_dir == Path("data/control/jobs")
    assert config.daemon.control_token_env == "CANARY_CONTROL_TOKEN"
    # 9.1 (operator-approved decision): GRU-D LSTM is the shipped default.
    assert config.models.sequence_detector == "lstm"
    # 6.1: M-of-N persistence + 6.2: agreement-gated fusion.
    assert config.alerts.min_consecutive_ticks == 3
    assert config.alerts.persistence_window_ticks == 5
    assert config.models.fusion.min_agreement == 2
    # Task 3.2 (online-training-strategy.md): hard pre-made decision,
    # scheduled retrain ships disabled.
    assert config.online_training.enabled is False


def test_online_training_config_defaults():
    """Task 3.2: `online_training.enabled` defaults false — enabling the
    scheduler against the real corpus is an explicit operator action, never
    an unattended default."""
    from src.config import OnlineTrainingConfig

    cfg = OnlineTrainingConfig()
    assert cfg.enabled is False
    assert cfg.interval_seconds > 0
    assert cfg.window_days > 0
    assert cfg.poll_interval_seconds > 0


def test_sequence_detector_default_is_lstm():
    """9.1 (operator-approved decision, see OVERNIGHT-QUEUE.md "Sequence
    backbone default"): GRU-D LSTM is the shipped default sequence_detector
    backbone even with no config override; TCN is documented as the
    NPU-export fallback, not an equal — no further bake-off gates this."""
    from src.config import ModelsConfig

    assert ModelsConfig.model_fields["sequence_detector"].default == "lstm"


def test_pi_example_yaml_loads():
    config = CanaryConfig.from_yaml(PI_EXAMPLE_YAML)
    assert config.hardware.can.interface == "socketcan"
    assert config.hardware.can.channel == "can0"
    assert config.collection.storage_path == Path("/data/canary/sessions")
    assert config.daemon.anomalies_dir == Path("/data/canary/anomalies")
    assert config.daemon.models_dir == Path("models")
    assert config.daemon.jobs_dir == Path("/data/canary/control/jobs")
    assert config.models.lstm_autoencoder.enabled is False
    assert config.dashboard.host == "0.0.0.0"
    assert config.daemon.control_token_env == "CANARY_CONTROL_TOKEN"


def test_unknown_top_level_key_rejected(tmp_path: Path):
    bad = tmp_path / "bad.yaml"
    bad.write_text(EXAMPLE_YAML.read_text() + "\nbogus_section: 1\n")
    with pytest.raises(ValidationError) as exc_info:
        CanaryConfig.from_yaml(bad)
    assert "bogus_section" in str(exc_info.value)


def test_unknown_nested_key_rejected(tmp_path: Path):
    bad = tmp_path / "bad.yaml"
    bad.write_text(
        EXAMPLE_YAML.read_text().replace(
            "  port: 8000",
            "  port: 8000\n  bogus_field: hello",
        )
    )
    with pytest.raises(ValidationError) as exc_info:
        CanaryConfig.from_yaml(bad)
    assert "bogus_field" in str(exc_info.value)


def test_user_extensible_operating_modes(tmp_path: Path):
    custom = tmp_path / "custom.yaml"
    custom.write_text(
        EXAMPLE_YAML.read_text().replace(
            "    highway:\n      speed_min: 80",
            "    highway:\n      speed_min: 80\n    offroad:\n      speed_max: 30\n",
        )
    )
    config = CanaryConfig.from_yaml(custom)
    assert "offroad" in config.preprocessing.operating_modes
    assert config.preprocessing.operating_modes["offroad"].speed_max == 30
