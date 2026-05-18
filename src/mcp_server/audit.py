"""Audit logging for MCP-driven write actions.

Wraps :class:`src.control.audit.ControlAuditLogger` and stamps every event
with ``actor="mcp"`` so MCP-driven changes are distinguishable from
operator-driven ones in the audit trail.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from src.control.audit import ControlAuditLogger

_ACTOR = "mcp"


class MCPAuditLogger:
    """Thin wrapper that records MCP tool invocations to the audit log."""

    def __init__(self, path: Path) -> None:
        self._inner = ControlAuditLogger(path)

    def record(
        self,
        *,
        tool: str,
        result: str,
        payload: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        """Append one record. ``result`` is ``"ok"`` or a short failure code."""
        body = dict(payload or {})
        body.setdefault("actor", _ACTOR)
        return self._inner.record(action=f"mcp.{tool}", result=result, payload=body)


__all__ = ["MCPAuditLogger"]
