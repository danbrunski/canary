"""Write tool: record a vehicle maintenance/intervention ledger entry (Task 3.3).

Mirrors ``record_feedback``'s shape — a thin POST to the daemon's control
API, audit-logged so "Claude logged an oil change" is distinguishable from
an operator-driven entry via the dashboard or ``scripts/vehicle_log.py``.
"""

from __future__ import annotations

from typing import Any, Literal

from src.mcp_server.audit import MCPAuditLogger
from src.mcp_server.client import DaemonClient
from src.mcp_server.write_tools._shared import audited_post

VehicleEventKind = Literal["maintenance", "mod", "repair", "observation", "dtc"]


async def record_vehicle_event(
    client: DaemonClient,
    audit: MCPAuditLogger,
    *,
    occurred_at: str,
    kind: VehicleEventKind,
    title: str,
    details: str = "",
    odometer_km: float | None = None,
    affected_channels: list[str] | None = None,
    corpus_boundary: bool = False,
) -> dict[str, Any]:
    """Append one entry to the vehicle ledger.

    Parameters
    ----------
    occurred_at:
        ISO date (``YYYY-MM-DD``) the event actually happened.
    kind:
        ``"maintenance"``, ``"mod"``, ``"repair"``, ``"observation"``, or
        ``"dtc"``.
    title / details:
        Short summary + free-text context.
    odometer_km:
        Odometer reading at the time, if known.
    affected_channels:
        Channels this event is expected to change the behavior of (e.g.
        ``["obd:COOLANT_TEMP"]``).
    corpus_boundary:
        Whether "normal" changes at this event — a mod or repair that
        shifts expected sensor behavior should bisect the training corpus
        (see ``--since-boundary`` on ``scripts/retrain.py`` /
        ``scripts/refit_behavior_models.py``).
    """
    body: dict[str, Any] = {
        "occurred_at": occurred_at,
        "kind": kind,
        "title": title,
        "details": details,
        "odometer_km": odometer_km,
        "affected_channels": affected_channels or [],
        "corpus_boundary": corpus_boundary,
    }
    return await audited_post(
        client, audit, tool="record_vehicle_event", path="/control/vehicle-ledger", body=body
    )


__all__ = ["record_vehicle_event", "VehicleEventKind"]
