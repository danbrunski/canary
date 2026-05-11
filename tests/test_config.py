"""Smoke tests for the config loader."""

from pathlib import Path

import pytest
from pydantic import ValidationError

from src.config import CanaryConfig

REPO_ROOT = Path(__file__).resolve().parents[1]
EXAMPLE_YAML = REPO_ROOT / "canary.example.yaml"


def test_example_yaml_loads():
    config = CanaryConfig.from_yaml(EXAMPLE_YAML)
    assert config.vehicle.make == "Nissan"
    assert config.vehicle.year == 2008
    assert config.collection.obd_poll_rate_hz == 1
    assert config.collection.reconnect_initial_delay_seconds == 10
    assert config.collection.queue_max_size == 10000
    assert "idle" in config.preprocessing.operating_modes
    assert config.preprocessing.operating_modes["idle"].rpm_max == 900
    assert config.models.isolation_forest.enabled is True
    assert config.dashboard.port == 8000


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
            "    wot:\n      throttle_min: 80",
            "    wot:\n      throttle_min: 80\n    offroad:\n      speed_max: 30\n",
        )
    )
    config = CanaryConfig.from_yaml(custom)
    assert "offroad" in config.preprocessing.operating_modes
    assert config.preprocessing.operating_modes["offroad"].speed_max == 30
