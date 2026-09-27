"""What the storage writer needs from the objects the collectors hand it (R09.2).

The writer is at layer 1 (``tests/test_layering.py``) and the capture profile
and record-arming gate it is given are ``collection`` types (layer 2). It types
against these Protocols instead of importing them: each names only the members
``parquet_writer`` / ``async_writer`` actually use.
``src.collection.capture_profiles.CaptureProfile`` and
``src.collection.record_arming.RecordArmingGate`` satisfy them structurally.
"""

from __future__ import annotations

from typing import Any, Protocol

from src.telemetry import TelemetryRecord


class StorageProfile(Protocol):
    """The capture profile a capture session is written for."""

    @property
    def name(self) -> str: ...

    @property
    def storage_prefix(self) -> str: ...

    @property
    def store_every_frame(self) -> bool: ...

    def model_dump(self, *, mode: str) -> dict[str, Any]: ...


class RecordGate(Protocol):
    """The operator-intent recording gate (Task 8.6)."""

    def observe(self, record: TelemetryRecord) -> bool: ...

    def reset_for_new_session(self) -> None: ...


__all__ = ["RecordGate", "StorageProfile"]
