"""Preflight checks for a headless Pi control session."""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pandas as pd

from src.config import CanaryConfig
from src.preprocessing.loader import PARQUET_PATTERN, discover_sessions
from src.storage.monitor import free_space_mb


def build_preflight(
    config: CanaryConfig,
    *,
    control_token_configured: bool,
    current_session: Path | None,
) -> dict[str, Any]:
    checks = [
        _check_storage(config),
        _check_session_writable(current_session),
        _check_control_token(control_token_configured),
        _check_can_interface(config),
        _check_models(config),
        _check_latest_telemetry(config),
    ]
    ok = all(check["ok"] for check in checks if check["severity"] == "error")
    return {
        "ok": ok,
        "checked_at": datetime.now(UTC).isoformat(),
        "checks": checks,
    }


def _check_storage(config: CanaryConfig) -> dict[str, Any]:
    try:
        free_mb = free_space_mb(config.collection.storage_path)
    except OSError as exc:
        return _check("storage", False, "error", str(exc))
    ok = free_mb >= config.collection.storage_warning_mb
    return _check(
        "storage",
        ok,
        "error",
        f"{free_mb:.1f} MiB free",
        free_mb=round(free_mb, 1),
        warning_mb=config.collection.storage_warning_mb,
        path=str(config.collection.storage_path),
    )


def _check_session_writable(current_session: Path | None) -> dict[str, Any]:
    if current_session is None:
        return _check("active_session_writable", False, "error", "no active session")
    try:
        current_session.mkdir(parents=True, exist_ok=True)
        probe = current_session / ".canary-write-test"
        probe.write_text("ok")
        probe.unlink(missing_ok=True)
    except OSError as exc:
        return _check("active_session_writable", False, "error", str(exc))
    return _check("active_session_writable", True, "error", "active session is writable")


def _check_control_token(configured: bool) -> dict[str, Any]:
    return _check(
        "control_token",
        configured,
        "error",
        "configured" if configured else "CANARY_CONTROL_TOKEN is not configured",
    )


def _check_can_interface(config: CanaryConfig) -> dict[str, Any]:
    can = config.hardware.can
    if can.interface != "socketcan":
        return _check("can_interface", True, "info", f"{can.interface}:{can.channel}")
    sys_path = Path("/sys/class/net") / can.channel
    if not sys_path.exists():
        return _check("can_interface", False, "error", f"{can.channel} is missing")
    try:
        operstate = (sys_path / "operstate").read_text().strip()
    except OSError as exc:
        return _check("can_interface", False, "error", str(exc))
    return _check(
        "can_interface",
        operstate in {"up", "unknown"},
        "error",
        f"{can.channel} operstate={operstate}",
        interface=can.interface,
        channel=can.channel,
        operstate=operstate,
    )


def _check_models(config: CanaryConfig) -> dict[str, Any]:
    expected: dict[str, Path] = {}
    if config.models.isolation_forest.enabled:
        expected["isolation_forest"] = (
            config.daemon.models_dir / config.daemon.isolation_forest_artifact
        )
    if config.models.lstm_autoencoder.enabled:
        expected["lstm_autoencoder"] = config.daemon.models_dir / config.daemon.lstm_artifact
    missing = [
        name
        for name, artifact in expected.items()
        if not artifact.exists() or not artifact.with_suffix(".manifest.json").exists()
    ]
    return _check(
        "model_artifacts",
        not missing,
        "warning",
        "all enabled model artifacts present" if not missing else f"missing: {', '.join(missing)}",
        missing=missing,
    )


def _check_latest_telemetry(config: CanaryConfig) -> dict[str, Any]:
    sessions = discover_sessions(config.collection.storage_path)
    if not sessions:
        return _check("latest_telemetry", False, "warning", "no telemetry rows found")
    chunks = sorted(sessions[-1].glob(PARQUET_PATTERN))
    if not chunks:
        return _check("latest_telemetry", False, "warning", "no telemetry chunks found")
    try:
        table = pd.read_parquet(chunks[-1]).tail(1)
    except Exception as exc:  # noqa: BLE001 - preflight should report, not crash
        return _check("latest_telemetry", False, "warning", f"latest chunk unreadable: {exc}")
    if table.empty:
        return _check("latest_telemetry", False, "warning", "latest chunk is empty")
    timestamp = str(table.iloc[-1].get("timestamp"))
    return _check("latest_telemetry", True, "warning", "telemetry rows found", timestamp=timestamp)


def _check(name: str, ok: bool, severity: str, detail: str, **extra: Any) -> dict[str, Any]:
    return {"name": name, "ok": ok, "severity": severity, "detail": detail, **extra}


__all__ = ["build_preflight"]
