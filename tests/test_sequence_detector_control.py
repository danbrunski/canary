"""Tests for the live sequence-detector backbone switch (Phase 9.6)."""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest
from fastapi.testclient import TestClient

from src.config import CanaryConfig
from src.control.sequence_detector import (
    SequenceDetectorController,
    SequenceDetectorUnavailableError,
)
from src.dashboard.api import create_app

REPO_ROOT = Path(__file__).resolve().parents[1]
EXAMPLE_YAML = REPO_ROOT / "canary.example.yaml"


def _components(tmp_path: Path, *, active: str = "lstm_autoencoder") -> Any:
    """Minimal stand-in with the three attributes the controller touches."""
    config = CanaryConfig.from_yaml(EXAMPLE_YAML)
    config.daemon.models_dir = tmp_path / "models"
    # Managers expose ``.current`` (None = nothing loaded), matching ModelManager.
    managers: dict[str, object] = {
        "isolation_forest": SimpleNamespace(current=None),
        active: SimpleNamespace(current=None),
    }
    return SimpleNamespace(config=config, model_managers=managers, sequence_model_type=active)


def test_status_reports_selector_active_and_availability(tmp_path: Path) -> None:
    comp = _components(tmp_path)
    status = SequenceDetectorController(comp).status()
    assert status.selector == "lstm"
    assert status.active == "lstm_autoencoder"
    assert set(status.available) == {"lstm_autoencoder", "tcn_autoencoder"}
    assert status.available["lstm_autoencoder"]["active"] is True
    assert status.available["tcn_autoencoder"]["active"] is False
    # No artifacts on disk in the tmp models dir.
    assert status.available["tcn_autoencoder"]["artifact_present"] is False


def test_switch_swaps_active_backbone_and_frees_the_old_manager(tmp_path: Path) -> None:
    # The real switch loads the target backbone, which requires torch.
    pytest.importorskip("torch")
    comp = _components(tmp_path, active="lstm_autoencoder")
    controller = SequenceDetectorController(comp)

    status = controller.switch("tcn")

    assert comp.sequence_model_type == "tcn_autoencoder"
    assert "tcn_autoencoder" in comp.model_managers
    # Previous backbone's manager is dropped so its model leaves RAM; IF stays.
    assert "lstm_autoencoder" not in comp.model_managers
    assert "isolation_forest" in comp.model_managers
    assert status.active == "tcn_autoencoder"
    # No artifact on disk yet → the manager polls, current is None.
    assert comp.model_managers["tcn_autoencoder"].current is None


def test_switch_unknown_backbone_raises(tmp_path: Path) -> None:
    controller = SequenceDetectorController(_components(tmp_path))
    with pytest.raises(ValueError, match="unknown sequence backbone"):
        controller.switch("bogus")


def test_switch_unavailable_when_requirements_missing(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import src.control.sequence_detector as mod
    from src.models.sequence_registry import SequenceBackend

    fake = SequenceBackend(
        selector="tcn",
        model_type="tcn_autoencoder",
        requires=("a_module_that_is_not_installed_xyz",),
        is_enabled=lambda _c: True,
        artifact_path=lambda c: c.daemon.models_dir / "tcn_autoencoder/model.pt",
        load_model=lambda _p: object(),  # type: ignore[arg-type,return-value]
    )
    monkeypatch.setattr(mod, "SEQUENCE_BACKENDS", {"tcn": fake})
    controller = SequenceDetectorController(_components(tmp_path))
    with pytest.raises(SequenceDetectorUnavailableError, match="missing"):
        controller.switch("tcn")


# ---------------------------------------------------------------------------
# Endpoints


def _client(tmp_path: Path) -> TestClient:
    comp = _components(tmp_path)
    controller = SequenceDetectorController(comp)
    config = comp.config
    app = create_app(
        config,
        anomalies_dir=tmp_path / "anomalies",
        sequence_detector_controller=controller,
        control_token="secret",
    )
    return TestClient(app)


def _auth() -> dict[str, str]:
    return {"Authorization": "Bearer secret"}


def test_status_endpoint_includes_sequence_detector_block(tmp_path: Path) -> None:
    client = _client(tmp_path)
    block = client.get("/status").json()["sequence_detector"]
    assert block["selector"] == "lstm"
    assert block["active"] == "lstm_autoencoder"


def test_post_switches_active_backbone(tmp_path: Path) -> None:
    # The real switch loads the target backbone, which requires torch.
    pytest.importorskip("torch")
    client = _client(tmp_path)
    response = client.post("/control/sequence-detector", json={"backbone": "tcn"}, headers=_auth())
    assert response.status_code == 200
    assert response.json()["sequence_detector"]["active"] == "tcn_autoencoder"
    # The change is visible on the next /status read.
    assert client.get("/status").json()["sequence_detector"]["active"] == "tcn_autoencoder"


def test_post_requires_auth(tmp_path: Path) -> None:
    client = _client(tmp_path)
    assert client.post("/control/sequence-detector", json={"backbone": "tcn"}).status_code == 401


def test_post_rejects_unknown_backbone(tmp_path: Path) -> None:
    client = _client(tmp_path)
    response = client.post(
        "/control/sequence-detector", json={"backbone": "bogus"}, headers=_auth()
    )
    assert response.status_code == 422  # rejected by the request schema's Literal
