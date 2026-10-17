"""Request schemas for the companion API."""

from __future__ import annotations

from typing import Any

from pydantic import BaseModel, ConfigDict, Field


class _ApiRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")


class ImportSessionRequest(_ApiRequest):
    archive_b64: str
    source_host: str | None = None
    index_with_config: bool = True


class SelectPiRequest(_ApiRequest):
    """``POST /companion/pi``: the daemon to link to, as a host or a base URL."""

    base_url: str


class SyncSessionRequest(_ApiRequest):
    pi_host: str | None = None
    token: str | None = None
    pin: bool = False
    pin_reason: str | None = "synced to companion library"
    index_with_config: bool = True
    verify_checksum: bool = True


class JobSubmitRequest(_ApiRequest):
    kind: str
    params: dict[str, Any] = Field(default_factory=dict)


class SessionMetadataRequest(_ApiRequest):
    metadata: dict[str, Any] = Field(default_factory=dict)
    merge: bool = True


class SessionPinRequest(_ApiRequest):
    reason: str | None = None
    metadata: dict[str, Any] = Field(default_factory=dict)


class ModelUploadRequest(_ApiRequest):
    artifact_b64: str
    manifest: dict[str, Any]


class PublishModelRequest(_ApiRequest):
    pi_host: str | None = None
    token: str | None = None
    artifact_path: str | None = None
    manifest_path: str | None = None
    model_id: str | None = None


__all__ = [
    "ImportSessionRequest",
    "JobSubmitRequest",
    "ModelUploadRequest",
    "PublishModelRequest",
    "SessionMetadataRequest",
    "SelectPiRequest",
    "SessionPinRequest",
    "SyncSessionRequest",
]
