"""Tests for the MCP server's env-driven config loader."""

from __future__ import annotations

from pathlib import Path

import pytest

from src.config import DaemonConfig
from src.mcp_server.config import (
    DEFAULT_AUDIT_PATH,
    DEFAULT_BIND_PORT,
    DEFAULT_DAEMON_URL,
    load_from_env,
)
from src.paths import ANOMALIES_DIR


def test_load_from_env_uses_defaults(monkeypatch: pytest.MonkeyPatch) -> None:
    """With nothing set, defaults are sensible and storage stays disabled."""
    for var in (
        "CANARY_DAEMON_URL",
        "CANARY_CONTROL_TOKEN",
        "CANARY_MCP_HOST",
        "CANARY_MCP_PORT",
        "CANARY_MCP_AUDIT_PATH",
        "CANARY_STORAGE_PATH",
        "CANARY_CONFIG",
        "CANARY_SIGNALS_PATH",
        "CANARY_MCP_LOG_FALLBACK",
        "CANARY_MCP_DAEMON_UNIT",
        "CANARY_ANOMALIES_DIR",
        "CANARY_MCP_STATELESS",
    ):
        monkeypatch.delenv(var, raising=False)
    cfg = load_from_env()
    assert cfg.daemon_url == DEFAULT_DAEMON_URL
    assert cfg.bind_port == DEFAULT_BIND_PORT
    assert cfg.daemon_token is None
    assert cfg.storage_path is None
    assert cfg.signals_path is None
    assert cfg.log_fallback_path is None
    assert cfg.daemon_unit == "canary"
    assert cfg.anomalies_dir is None
    assert cfg.auth_required is False
    assert cfg.config_path is None
    assert cfg.mcp_stateless is False


def test_load_from_env_reads_env(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """All documented env vars round-trip into the dataclass."""
    monkeypatch.setenv("CANARY_DAEMON_URL", "http://192.168.1.50:8000/")
    monkeypatch.setenv("CANARY_CONTROL_TOKEN", "secret")
    monkeypatch.setenv("CANARY_MCP_HOST", "0.0.0.0")
    monkeypatch.setenv("CANARY_MCP_PORT", "9001")
    monkeypatch.setenv("CANARY_MCP_AUDIT_PATH", str(tmp_path / "a.jsonl"))
    monkeypatch.setenv("CANARY_STORAGE_PATH", str(tmp_path / "sessions"))
    monkeypatch.delenv("CANARY_CONFIG", raising=False)
    cfg = load_from_env()
    assert cfg.daemon_url == "http://192.168.1.50:8000"  # trailing slash stripped
    assert cfg.daemon_token == "secret"
    assert cfg.bind_host == "0.0.0.0"
    assert cfg.bind_port == 9001
    assert cfg.audit_path == tmp_path / "a.jsonl"
    assert cfg.storage_path == tmp_path / "sessions"
    assert cfg.auth_required is True


def test_storage_path_falls_back_to_config_yaml(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """If CANARY_STORAGE_PATH is unset, parse it out of CANARY_CONFIG's YAML."""
    config_file = tmp_path / "canary.yaml"
    config_file.write_text(
        "collection:\n  storage_path: /var/canary/sessions\n  session_retention_days: 30\n"
    )
    monkeypatch.delenv("CANARY_STORAGE_PATH", raising=False)
    monkeypatch.setenv("CANARY_CONFIG", str(config_file))
    cfg = load_from_env()
    assert cfg.storage_path == Path("/var/canary/sessions")


def test_storage_fallback_tolerates_missing_yaml(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """An unreadable CANARY_CONFIG just disables query_telemetry — no crash."""
    monkeypatch.delenv("CANARY_STORAGE_PATH", raising=False)
    monkeypatch.setenv("CANARY_CONFIG", str(tmp_path / "does-not-exist.yaml"))
    cfg = load_from_env()
    assert cfg.storage_path is None


def test_phase2_paths_round_trip(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """Phase 2 env vars (signals, log fallback, daemon unit) flow through."""
    monkeypatch.setenv("CANARY_SIGNALS_PATH", str(tmp_path / "signals.json"))
    monkeypatch.setenv("CANARY_MCP_LOG_FALLBACK", str(tmp_path / "canary.log"))
    monkeypatch.setenv("CANARY_MCP_DAEMON_UNIT", "canary-prod")
    cfg = load_from_env()
    assert cfg.signals_path == tmp_path / "signals.json"
    assert cfg.log_fallback_path == tmp_path / "canary.log"
    assert cfg.daemon_unit == "canary-prod"


def test_anomalies_dir_explicit_env(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """``CANARY_ANOMALIES_DIR`` wins over any YAML-derived value."""
    monkeypatch.setenv("CANARY_ANOMALIES_DIR", str(tmp_path / "anom"))
    monkeypatch.delenv("CANARY_CONFIG", raising=False)
    cfg = load_from_env()
    assert cfg.anomalies_dir == tmp_path / "anom"


def test_anomalies_dir_from_config_yaml(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """Without an env override, ``alerts.anomalies_dir`` from canary.yaml is used."""
    config_file = tmp_path / "canary.yaml"
    config_file.write_text("alerts:\n  anomalies_dir: /var/canary/anomalies\n")
    monkeypatch.delenv("CANARY_ANOMALIES_DIR", raising=False)
    monkeypatch.setenv("CANARY_CONFIG", str(config_file))
    cfg = load_from_env()
    assert cfg.anomalies_dir == Path("/var/canary/anomalies")


def test_audit_path_defaults_under_install_data_dir(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Without an env override, the audit log sits under the install data dir.

    The previous default (``/var/lib/canary/audit/mcp.jsonl``) didn't match
    ``scripts/install_pi.sh``, which sets up ``/data/canary``. The MCP user
    can't ``mkdir`` under ``/var/lib`` and crash-looped on first start.
    """
    for var in ("CANARY_MCP_AUDIT_PATH", "CANARY_DATA_DIR"):
        monkeypatch.delenv(var, raising=False)
    cfg = load_from_env()
    assert cfg.audit_path == DEFAULT_AUDIT_PATH
    assert str(DEFAULT_AUDIT_PATH).startswith("/data/canary")


def test_audit_path_follows_data_dir_env(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """``CANARY_DATA_DIR`` reroutes the audit log so an operator can relocate the tree."""
    monkeypatch.delenv("CANARY_MCP_AUDIT_PATH", raising=False)
    monkeypatch.setenv("CANARY_DATA_DIR", str(tmp_path))
    cfg = load_from_env()
    assert cfg.audit_path == tmp_path / "audit" / "mcp.jsonl"


def test_audit_path_explicit_env_wins(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """An explicit ``CANARY_MCP_AUDIT_PATH`` overrides the data-dir derivation."""
    monkeypatch.setenv("CANARY_DATA_DIR", str(tmp_path / "data"))
    monkeypatch.setenv("CANARY_MCP_AUDIT_PATH", str(tmp_path / "elsewhere.jsonl"))
    cfg = load_from_env()
    assert cfg.audit_path == tmp_path / "elsewhere.jsonl"


def test_mcp_stateless_defaults_off_and_opts_in_on_1(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("CANARY_MCP_STATELESS", raising=False)
    assert load_from_env().mcp_stateless is False
    monkeypatch.setenv("CANARY_MCP_STATELESS", "1")
    assert load_from_env().mcp_stateless is True
    monkeypatch.setenv("CANARY_MCP_STATELESS", "0")
    assert load_from_env().mcp_stateless is False
    monkeypatch.setenv("CANARY_MCP_STATELESS", "true")  # only "1" opts in, not truthy strings
    assert load_from_env().mcp_stateless is False


def test_config_path_round_trips(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """``CANARY_CONFIG`` is preserved verbatim as ``config_path`` (not just
    parsed for storage_path/anomalies_dir) — get_behavior_deviations needs
    the full typed CanaryConfig, not just these two derived fields."""
    config_file = tmp_path / "canary.yaml"
    config_file.write_text("collection:\n  storage_path: /var/canary/sessions\n")
    monkeypatch.setenv("CANARY_CONFIG", str(config_file))
    cfg = load_from_env()
    assert cfg.config_path == config_file


def test_anomalies_dir_falls_back_to_daemon_default(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """No YAML key → the daemon's default (the checkout's ``data/anomalies``) is used.

    Matches the daemon's own behavior in :mod:`src.config` so the resource
    works against a stock dev tree without extra env wiring.
    """
    config_file = tmp_path / "canary.yaml"
    config_file.write_text("collection:\n  storage_path: /var/canary/sessions\n")
    monkeypatch.delenv("CANARY_ANOMALIES_DIR", raising=False)
    monkeypatch.setenv("CANARY_CONFIG", str(config_file))
    cfg = load_from_env()
    # R01.2: the same repo-rooted default as DaemonConfig.anomalies_dir,
    # never a working-directory-relative ``data/anomalies``.
    assert cfg.anomalies_dir == ANOMALIES_DIR
    assert cfg.anomalies_dir == DaemonConfig().anomalies_dir
    assert cfg.catalog_db == DaemonConfig().catalog_db
