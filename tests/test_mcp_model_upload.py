"""Tests for the ``upload_model_artifact`` write tool."""

from __future__ import annotations

import base64
import json
from pathlib import Path
from typing import Any

import httpx
import pytest

from src.mcp_server.audit import MCPAuditLogger
from src.mcp_server.client import DaemonClient, DaemonError
from src.mcp_server.write_tools import models as models_tool


async def _fake_daemon(handler_factory: Any) -> DaemonClient:
    client = DaemonClient("http://daemon")
    await client._client.aclose()
    client._client = httpx.AsyncClient(
        base_url="http://daemon",
        transport=httpx.MockTransport(handler_factory),
    )
    return client


async def test_upload_encodes_artifact_and_audits(tmp_path: Path) -> None:
    """Body has base64 artifact + manifest; audit excludes the giant blob."""
    artifact = tmp_path / "model.joblib"
    artifact.write_bytes(b"binary content")
    captured: dict[str, Any] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured["body"] = json.loads(request.read())
        return httpx.Response(200, json={"model": {"version": 7}})

    client = await _fake_daemon(handler)
    audit = MCPAuditLogger(tmp_path / "audit.jsonl")
    result = await models_tool.upload_model_artifact(
        client,
        audit,
        model_type="isolation_forest",
        artifact_path=artifact,
        manifest={"version": 7, "trained_at": "2026-05-18"},
    )
    await client.aclose()
    assert result == {"model": {"version": 7}}
    sent_b64 = captured["body"]["artifact_b64"]
    assert base64.b64decode(sent_b64) == b"binary content"
    assert captured["body"]["manifest"]["version"] == 7
    entry = json.loads((tmp_path / "audit.jsonl").read_text().splitlines()[0])
    assert entry["action"] == "mcp.upload_model_artifact"
    # The base64 blob must NOT be in the audit payload — keep audit lines small.
    assert "artifact_b64" not in entry["payload"]
    assert entry["payload"]["artifact_size"] == len(b"binary content")


async def test_upload_missing_file_raises_and_audits(tmp_path: Path) -> None:
    """A missing artifact is reported as ``result="error"`` before HTTP."""
    audit = MCPAuditLogger(tmp_path / "audit.jsonl")
    # No client needed — we fail before the HTTP call. Use a stub that would
    # 500 if it ever fired, so the test fails loudly if the order changes.

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(500, json={"detail": "should not be called"})

    client = await _fake_daemon(handler)
    with pytest.raises(FileNotFoundError):
        await models_tool.upload_model_artifact(
            client,
            audit,
            model_type="isolation_forest",
            artifact_path=tmp_path / "absent.joblib",
            manifest={},
        )
    await client.aclose()
    entry = json.loads((tmp_path / "audit.jsonl").read_text().splitlines()[0])
    assert entry["result"] == "error"
    assert "missing artifact" in entry["payload"]["error"]


async def test_upload_oversized_artifact_rejected(tmp_path: Path) -> None:
    """Artifacts above the 200 MB cap are rejected before encoding."""
    audit = MCPAuditLogger(tmp_path / "audit.jsonl")
    artifact = tmp_path / "huge.joblib"
    artifact.write_bytes(b"x")
    # Patch the limit down so we don't need to actually write 200 MB.
    original_limit = models_tool._MAX_ARTIFACT_BYTES
    models_tool._MAX_ARTIFACT_BYTES = 0
    try:
        client = await _fake_daemon(
            lambda r: httpx.Response(500, json={"detail": "should not run"})
        )
        with pytest.raises(ValueError, match="refusing to upload"):
            await models_tool.upload_model_artifact(
                client,
                audit,
                model_type="isolation_forest",
                artifact_path=artifact,
                manifest={},
            )
        await client.aclose()
    finally:
        models_tool._MAX_ARTIFACT_BYTES = original_limit
    entry = json.loads((tmp_path / "audit.jsonl").read_text().splitlines()[0])
    assert entry["result"] == "error"
    assert "too large" in entry["payload"]["error"] or "refusing" in entry["payload"]["error"]


async def test_upload_propagates_daemon_error_and_audits(tmp_path: Path) -> None:
    """A 4xx from the daemon ends up in the audit log + raises DaemonError."""
    artifact = tmp_path / "model.joblib"
    artifact.write_bytes(b"x")
    audit = MCPAuditLogger(tmp_path / "audit.jsonl")

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(400, json={"detail": "bad manifest"})

    client = await _fake_daemon(handler)
    with pytest.raises(DaemonError):
        await models_tool.upload_model_artifact(
            client,
            audit,
            model_type="isolation_forest",
            artifact_path=artifact,
            manifest={"bogus": True},
        )
    await client.aclose()
    entry = json.loads((tmp_path / "audit.jsonl").read_text().splitlines()[0])
    assert entry["result"] == "error"
    assert "bad manifest" in entry["payload"]["error"]
