"""Write tool: record a vehicle maintenance/intervention ledger entry (Task 3.3).

Mirrors ``record_feedback``'s shape — a thin POST to the daemon's control
API, audit-logged so "Claude logged an oil change" is distinguishable from
an operator-driven entry via the dashboard or ``scripts/vehicle_log.py``.
"""

from __future__ import annotations

from typing import Any

from src.mcp_server.audit import MCPAuditLogger
from src.mcp_server.client import DaemonClient
from src.mcp_server.write_tools._shared import audited_post
from src.vehicle.vehicle_ledger import PhysicalInterventionKind, VehicleEventKind


# The ``kind`` and ``intervention`` literals are the ledger's own
# (``src.vehicle.vehicle_ledger``), so FastMCP's schema for them is the
# daemon's accepted set rather than a restated copy. The docstring is the
# tool description a client reads.
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
    event_id: str | None = None,
    tires: dict[str, Any] | None = None,
    intervention: PhysicalInterventionKind | None = None,
) -> dict[str, Any]:
    """Log a maintenance/mod/repair/observation to the vehicle ledger (3.3).

    ``occurred_at`` is an ISO date (``YYYY-MM-DD``). Set
    ``corpus_boundary=True`` when the event changes what "normal" looks
    like (a mod or repair) so training can optionally start fresh from
    it (``--since-boundary``).

    Pass ``event_id`` (from `list_vehicle_events`) to CORRECT an existing
    entry: the correction replaces it on disk, and the original is gone.
    Re-state the whole event, not just the changed field. Removing an
    entry outright is ``DELETE /control/vehicle-ledger/{id}``.

    ``tires`` records a tire fact the tire-wear trend reads:
    ``{"action": "tread_measured", "tread_depth_mm": 9.5}`` (a gauge
    reading on the driven axle, the baseline for estimated tread depth),
    or ``{"action": "rotated" | "replaced" | "pressure_changed"}``.

    ``intervention`` classifies an event that changed the vehicle or its
    instruments: ``"hardware"``, ``"fuel"`` or ``"instrumentation"``
    (E01.4 investigation memory). A correction re-states it too, or the
    classification is erased with the rest of the old entry.
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
    if event_id is not None:
        body["event_id"] = event_id
    if tires is not None:
        body["tires"] = tires
    if intervention is not None:
        body["intervention"] = intervention
    return await audited_post(
        client, audit, tool="record_vehicle_event", path="/control/vehicle-ledger", body=body
    )


__all__ = ["record_vehicle_event"]
