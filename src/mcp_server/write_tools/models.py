"""Model artifact upload — publish a trained model to the daemon."""

from __future__ import annotations

import base64
from pathlib import Path
from typing import Any

from src.mcp_server.audit import MCPAuditLogger
from src.mcp_server.client import DaemonClient, DaemonError

#: Cap so an LLM can't request a 10 GB upload by accident. Models on this
#: project's roadmap are <100 MB; 200 MB is comfortable headroom.
_MAX_ARTIFACT_BYTES = 200 * 1024 * 1024


async def upload_model_artifact(
    client: DaemonClient,
    audit: MCPAuditLogger,
    *,
    model_type: str,
    artifact_path: Path,
    manifest: dict[str, Any],
) -> dict[str, Any]:
    """Base64-encode ``artifact_path`` and POST it as a new model artifact.

    ``artifact_path`` is read from the MCP server's local filesystem (the Pi,
    when running there) — typically the location a retrain job just wrote to.
    ``manifest`` is the metadata the daemon stores alongside the binary.

    The daemon validates both before publishing; we don't.
    """
    if not artifact_path.exists():
        audit.record(
            tool="upload_model_artifact",
            result="error",
            payload={"model_type": model_type, "error": f"missing artifact: {artifact_path}"},
        )
        raise FileNotFoundError(f"artifact not found: {artifact_path}")
    size = artifact_path.stat().st_size
    if size > _MAX_ARTIFACT_BYTES:
        audit.record(
            tool="upload_model_artifact",
            result="error",
            payload={"model_type": model_type, "error": f"artifact too large: {size}"},
        )
        raise ValueError(f"artifact size {size} > limit {_MAX_ARTIFACT_BYTES}; refusing to upload")
    artifact_b64 = base64.b64encode(artifact_path.read_bytes()).decode("ascii")
    body: dict[str, Any] = {"artifact_b64": artifact_b64, "manifest": manifest}
    # Don't include the giant base64 blob in the audit payload — log the
    # path + size + manifest summary, which is what review actually wants.
    audit_payload = {
        "model_type": model_type,
        "artifact_path": str(artifact_path),
        "artifact_size": size,
        "manifest": manifest,
    }
    try:
        result = await client.post_json(f"/control/models/{model_type}/artifact", body)
    except DaemonError as exc:
        audit.record(
            tool="upload_model_artifact",
            result="error",
            payload={**audit_payload, "error": str(exc)},
        )
        raise
    audit.record(
        tool="upload_model_artifact",
        result="ok",
        payload={**audit_payload, "response": result},
    )
    return result


__all__ = ["upload_model_artifact"]
