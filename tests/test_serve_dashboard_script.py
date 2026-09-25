"""Tests for the dashboard entry-point script (Task 5.5)."""

from __future__ import annotations

import logging
from pathlib import Path

import pytest

from scripts import serve_dashboard

# ---------------------------------------------------------------------------
# warn_if_public_bind
# ---------------------------------------------------------------------------


def test_warn_loopback_silent() -> None:
    assert serve_dashboard.warn_if_public_bind("127.0.0.1") is False
    assert serve_dashboard.warn_if_public_bind("localhost") is False
    assert serve_dashboard.warn_if_public_bind("::1") is False


def test_warn_private_silent() -> None:
    assert serve_dashboard.warn_if_public_bind("192.168.1.5") is False
    assert serve_dashboard.warn_if_public_bind("10.0.0.42") is False
    assert serve_dashboard.warn_if_public_bind("172.16.0.1") is False


def test_warn_link_local_silent() -> None:
    assert serve_dashboard.warn_if_public_bind("169.254.1.1") is False
    assert serve_dashboard.warn_if_public_bind("fe80::1") is False


def test_warn_zero_loud(caplog: pytest.LogCaptureFixture) -> None:
    with caplog.at_level(logging.WARNING, logger="serve_dashboard"):
        warned = serve_dashboard.warn_if_public_bind("0.0.0.0")
    assert warned is True
    assert any("NO AUTH" in r.message for r in caplog.records)


def test_warn_public_ip_loud(caplog: pytest.LogCaptureFixture) -> None:
    with caplog.at_level(logging.WARNING, logger="serve_dashboard"):
        warned = serve_dashboard.warn_if_public_bind("8.8.8.8")
    assert warned is True
    assert any("public" in r.message.lower() for r in caplog.records)


def test_warn_hostname_loud(caplog: pytest.LogCaptureFixture) -> None:
    """We can't resolve at warn-time without a roundtrip; warn conservatively."""
    with caplog.at_level(logging.WARNING, logger="serve_dashboard"):
        warned = serve_dashboard.warn_if_public_bind("dashboard.example.com")
    assert warned is True


# ---------------------------------------------------------------------------
# main()
# ---------------------------------------------------------------------------


def test_main_uses_config_defaults_when_no_flags(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """No --host/--port → values come from canary.yaml."""
    captured: dict[str, object] = {}

    def _fake_uvicorn_run(app: object, **kwargs: object) -> None:
        captured["host"] = kwargs.get("host")
        captured["port"] = kwargs.get("port")

    monkeypatch.setattr(serve_dashboard.uvicorn, "run", _fake_uvicorn_run)

    rc = serve_dashboard.main(["--config", "canary.example.yaml"])
    assert rc == 0
    # canary.example.yaml has dashboard: host: 0.0.0.0, port: 8000 (see file)
    assert captured["host"] == "0.0.0.0"
    assert captured["port"] == 8000


def test_main_cli_flags_override_config(monkeypatch: pytest.MonkeyPatch) -> None:
    captured: dict[str, object] = {}

    def _fake_uvicorn_run(app: object, **kwargs: object) -> None:
        captured["host"] = kwargs.get("host")
        captured["port"] = kwargs.get("port")

    monkeypatch.setattr(serve_dashboard.uvicorn, "run", _fake_uvicorn_run)

    rc = serve_dashboard.main(
        [
            "--config",
            "canary.example.yaml",
            "--host",
            "127.0.0.1",
            "--port",
            "9001",
        ]
    )
    assert rc == 0
    assert captured["host"] == "127.0.0.1"
    assert captured["port"] == 9001


def test_main_creates_app_with_anomalies_dir(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Pass-through of --anomalies-dir to create_app."""
    captured: dict[str, object] = {}

    def _fake_create_app(_config: object, **kwargs: object):  # type: ignore[no-untyped-def]
        captured["anomalies_dir"] = kwargs.get("anomalies_dir")
        captured["models_dir"] = kwargs.get("models_dir")
        return object()  # uvicorn.run is also stubbed below; it just needs *something*

    def _fake_uvicorn_run(*_a: object, **_k: object) -> None:
        pass

    monkeypatch.setattr(serve_dashboard, "create_app", _fake_create_app)
    monkeypatch.setattr(serve_dashboard.uvicorn, "run", _fake_uvicorn_run)

    rc = serve_dashboard.main(
        [
            "--config",
            "canary.example.yaml",
            "--anomalies-dir",
            str(tmp_path / "anomalies"),
            "--models-dir",
            str(tmp_path / "models"),
        ]
    )
    assert rc == 0
    assert captured["anomalies_dir"] == tmp_path / "anomalies"
    assert captured["models_dir"] == tmp_path / "models"
