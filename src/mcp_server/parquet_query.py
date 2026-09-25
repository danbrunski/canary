"""Compatibility shim: :mod:`src.storage.telemetry_query` is the home (R01.8).

``TelemetryQuery`` moved to ``src.storage`` so ``src.control`` can read
telemetry without importing ``src.mcp_server`` (a layering inversion,
enforced by ``tests/test_layering.py``). Import from the new home; this
re-export exists so older callers keep working.
"""

from __future__ import annotations

from src.storage.telemetry_query import QueryValidationError, TelemetryQuery, validate_query

__all__ = ["QueryValidationError", "TelemetryQuery", "validate_query"]
