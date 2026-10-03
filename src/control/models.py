"""Model artifact publishing helpers for the control API."""

from __future__ import annotations

import base64
import binascii
import json
from pathlib import Path
from typing import Any

from src.config import CanaryConfig
from src.models.manifest import manifest_path_for

MODEL_TYPES = {"isolation_forest", "lstm_autoencoder", "tcn_autoencoder"}


def publish_model_artifact(
    config: CanaryConfig,
    *,
    model_type: str,
    artifact_b64: str,
    manifest: dict[str, Any],
) -> dict[str, Any]:
    """Write a model artifact + manifest to the daemon's watched path."""
    path = artifact_path(config, model_type)
    manifest_path = manifest_path_for(path)
    try:
        artifact_bytes = base64.b64decode(artifact_b64, validate=True)
    except binascii.Error as exc:
        raise ValueError(f"invalid artifact_b64: {exc}") from exc

    path.parent.mkdir(parents=True, exist_ok=True)
    tmp_artifact = path.with_suffix(path.suffix + ".tmp")
    tmp_manifest = manifest_path.with_suffix(manifest_path.suffix + ".tmp")
    tmp_artifact.write_bytes(artifact_bytes)
    tmp_manifest.write_text(json.dumps(manifest, indent=2, sort_keys=True) + "\n")
    tmp_artifact.replace(path)
    tmp_manifest.replace(manifest_path)

    return {
        "model_type": model_type,
        "artifact_path": str(path),
        "manifest_path": str(manifest_path),
        "artifact_bytes": len(artifact_bytes),
    }


def artifact_path(config: CanaryConfig, model_type: str) -> Path:
    """The watched path a ``model_type``'s artifact is served from.

    The one copy (R09.8). The companion kept its own, which knew the TCN
    backbone; this one did not, so a TCN could be read but never published.
    """
    if model_type == "isolation_forest":
        return config.daemon.models_dir / config.daemon.isolation_forest_artifact
    if model_type == "lstm_autoencoder":
        return config.daemon.models_dir / config.daemon.lstm_artifact
    if model_type == "tcn_autoencoder":
        return config.daemon.models_dir / config.daemon.tcn_artifact
    raise ValueError(f"unsupported model type: {model_type}")


__all__ = ["MODEL_TYPES", "artifact_path", "publish_model_artifact"]
