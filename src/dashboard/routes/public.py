"""The public (no-token) read routes, registered as one set (R29.7).

``register_public_routes`` was one 383-line function holding seventeen closures.
The routes now live by concern, each module with its own registrar:

- :mod:`~src.dashboard.routes.status_routes`: identity, ``/status``, the config
  echo, recent anomalies and the telemetry seed;
- :mod:`~src.dashboard.routes.catalog_reads`: behavior surfaces, bindings, the
  DID catalog, test profiles, OBD and CAN coverage, cached DTCs and monitors;
- :mod:`~src.dashboard.routes.analysis_reads`: binned analysis, LLM-assist cards
  and trip summaries.

This stays the entry point ``create_app`` and the bare-app tests call; each
registrar takes the app and the one :class:`~src.dashboard.app_context.AppContext` (R29.6)
and reads the daemon object it needs as ``ctx.<name>``. The route table is the one
it was (``tests/fixtures/route_table_snapshot.json``).
"""

from __future__ import annotations

from fastapi import FastAPI

from src.dashboard.app_context import AppContext
from src.dashboard.routes.analysis_reads import register_analysis_read_routes
from src.dashboard.routes.catalog_reads import register_catalog_read_routes
from src.dashboard.routes.status_routes import register_status_routes


def register_public_routes(app: FastAPI, ctx: AppContext) -> None:
    register_status_routes(app, ctx)
    register_catalog_read_routes(app, ctx)
    register_analysis_read_routes(app, ctx)
