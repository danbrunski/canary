"""Model artifact publishing helpers for the control API."""

from __future__ import annotations

import base64
import binascii
import json
from pathlib import Path
from typing import Any

from src.config import CanaryConfig

MODEL_TYPES = {"isolation_forest", "lstm_autoencoder"}


def publish_model_artifact(
    config: CanaryConfig,
    *,
    model_type: str,
    artifact_b64: str,
    manifest: dict[str, Any],
) -> dict[str, Any]:
    """Write a model artifact + manifest to the daemon's watched path."""
    artifact_path = _artifact_path(config, model_type)
    manifest_path = artifact_path.with_suffix(".manifest.json")
    try:
        artifact_bytes = base64.b64decode(artifact_b64, validate=True)
    except binascii.Error as exc:
        raise ValueError(f"invalid artifact_b64: {exc}") from exc

    artifact_path.parent.mkdir(parents=True, exist_ok=True)
    tmp_artifact = artifact_path.with_suffix(artifact_path.suffix + ".tmp")
    tmp_manifest = manifest_path.with_suffix(manifest_path.suffix + ".tmp")
    tmp_artifact.write_bytes(artifact_bytes)
    tmp_manifest.write_text(json.dumps(manifest, indent=2, sort_keys=True) + "\n")
    tmp_artifact.replace(artifact_path)
    tmp_manifest.replace(manifest_path)

    return {
        "model_type": model_type,
        "artifact_path": str(artifact_path),
        "manifest_path": str(manifest_path),
        "artifact_bytes": len(artifact_bytes),
    }


def _artifact_path(config: CanaryConfig, model_type: str) -> Path:
    if model_type == "isolation_forest":
        return config.daemon.models_dir / config.daemon.isolation_forest_artifact
    if model_type == "lstm_autoencoder":
        return config.daemon.models_dir / config.daemon.lstm_artifact
    raise ValueError(f"unsupported model type: {model_type}")


__all__ = ["MODEL_TYPES", "publish_model_artifact"]
