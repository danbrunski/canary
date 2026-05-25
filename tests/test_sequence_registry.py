"""Tests for the selectable sequence-detector registry + daemon wiring."""

from __future__ import annotations

from pathlib import Path

from src.config import CanaryConfig
from src.daemon.components import _load_model_managers, _sequence_model_type
from src.models.sequence_registry import (
    LSTM_BACKEND,
    SEQUENCE_BACKENDS,
    TCN_BACKEND,
    SequenceBackend,
    resolve_sequence_backend,
)

REPO_ROOT = Path(__file__).resolve().parents[1]
EXAMPLE_YAML = REPO_ROOT / "canary.example.yaml"


def _config(*, selector: str, lstm: bool, tcn: bool) -> CanaryConfig:
    config = CanaryConfig.from_yaml(EXAMPLE_YAML)
    config.models.isolation_forest.enabled = False  # isolate the sequence slot
    config.models.sequence_detector = selector  # type: ignore[assignment]
    config.models.lstm_autoencoder.enabled = lstm
    config.models.tcn_autoencoder.enabled = tcn
    return config


def test_registry_has_both_backbones() -> None:
    assert set(SEQUENCE_BACKENDS) == {"lstm", "tcn"}
    assert SEQUENCE_BACKENDS["lstm"] is LSTM_BACKEND
    assert SEQUENCE_BACKENDS["tcn"] is TCN_BACKEND
    assert LSTM_BACKEND.model_type == "lstm_autoencoder"
    assert TCN_BACKEND.model_type == "tcn_autoencoder"


def test_resolve_returns_selected_enabled_backend() -> None:
    lstm_cfg = _config(selector="lstm", lstm=True, tcn=False)
    assert resolve_sequence_backend(lstm_cfg) is LSTM_BACKEND

    tcn_cfg = _config(selector="tcn", lstm=False, tcn=True)
    backend = resolve_sequence_backend(tcn_cfg)
    assert backend is TCN_BACKEND
    assert backend.artifact_path(tcn_cfg) == tcn_cfg.daemon.models_dir / "tcn_autoencoder/model.pt"


def test_resolve_none_when_selected_backend_disabled() -> None:
    # Selecting tcn but leaving it disabled keeps the sequence slot empty even
    # though lstm happens to be enabled — the selector picks which backend's
    # flag is consulted.
    config = _config(selector="tcn", lstm=True, tcn=False)
    assert resolve_sequence_backend(config) is None


def test_missing_requirements_reports_absent_modules() -> None:
    present = SequenceBackend(
        selector="x",
        model_type="x",
        requires=("torch",),
        is_enabled=lambda _c: True,
        artifact_path=lambda c: c.daemon.models_dir,
        load_model=lambda _p: object(),  # type: ignore[arg-type,return-value]
    )
    assert present.missing_requirements() == ()

    absent = SequenceBackend(
        selector="x",
        model_type="x",
        requires=("torch", "definitely_not_a_real_module_xyz"),
        is_enabled=lambda _c: True,
        artifact_path=lambda c: c.daemon.models_dir,
        load_model=lambda _p: object(),  # type: ignore[arg-type,return-value]
    )
    assert absent.missing_requirements() == ("definitely_not_a_real_module_xyz",)


def test_daemon_loads_selected_sequence_backend() -> None:
    tcn_cfg = _config(selector="tcn", lstm=False, tcn=True)
    managers = _load_model_managers(tcn_cfg)
    assert "tcn_autoencoder" in managers
    assert "lstm_autoencoder" not in managers
    assert _sequence_model_type(tcn_cfg) == "tcn_autoencoder"
    # No artifact on disk yet → the manager keeps polling, current is None.
    assert managers["tcn_autoencoder"].current is None

    lstm_cfg = _config(selector="lstm", lstm=True, tcn=False)
    assert "lstm_autoencoder" in _load_model_managers(lstm_cfg)
    assert _sequence_model_type(lstm_cfg) == "lstm_autoencoder"


def test_daemon_no_sequence_manager_when_disabled() -> None:
    config = _config(selector="lstm", lstm=False, tcn=False)
    assert _load_model_managers(config) == {}
    assert _sequence_model_type(config) is None
