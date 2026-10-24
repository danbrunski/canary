"""Every HTTP/WebSocket route's contract, pinned (R03.4).

The safety net for R03.5/R03.6, which move ~80 route closures out of
``register_control_routes`` into per-domain routers. A moved route that loses
its auth dependency, a query parameter, its declared status code or its
registration entirely still "works" — the app starts and every other route
answers. This pins, per route: method, path, whether it requires auth, the
declared status code, its query-parameter names and whether it takes a body.

Built with every optional component present, for both apps that serve
``/control/*``: the dashboard (``src.dashboard.api.create_app``) and the laptop
companion (``src.companion.api.create_app``), whose optional bearer is a
deliberately different rule and is recorded separately.

Regenerate after an *intended* contract change::

    CANARY_UPDATE_ROUTE_SNAPSHOT=1 .venv/bin/python -m pytest -n0 tests/test_route_table_snapshot.py
"""

from __future__ import annotations

import asyncio
import json
import os
from collections.abc import Callable, Iterator
from pathlib import Path
from typing import Any, cast

from fastapi import FastAPI
from fastapi.dependencies.models import Dependant
from fastapi.routing import APIRoute, APIWebSocketRoute

from src.config import CanaryConfig

SNAPSHOT = Path(__file__).parent / "fixtures" / "route_table_snapshot.json"
EXAMPLE_YAML = Path(__file__).resolve().parents[1] / "canary.example.yaml"


def _calls(dependant: Dependant) -> Iterator[Callable[..., Any]]:
    for sub in dependant.dependencies:
        if sub.call is not None:
            yield sub.call
        yield from _calls(sub)


def _auth_kind(dependant: Dependant) -> str | None:
    """Which auth rule guards the route, from its dependency tree.

    ``control``: :func:`src.dashboard.security.control_auth_dependency` (the
    dashboard's bearer token, loopback-exempt only while no token is
    configured). ``companion``: the companion's optional local bearer.
    """
    kinds = {getattr(call, "canary_auth", None) for call in _calls(dependant)}
    kinds.discard(None)
    if len(kinds) > 1:
        raise AssertionError(f"route carries two auth rules: {sorted(cast(set[str], kinds))}")
    return next(iter(kinds), None)


def _route_rows(app: FastAPI) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    for route in app.routes:
        if isinstance(route, APIRoute):
            dependant = route.dependant
            for method in sorted(route.methods or ()):
                rows.append(
                    {
                        "method": method,
                        "path": route.path,
                        "auth": _auth_kind(dependant),
                        "status_code": route.status_code,
                        "query": sorted(param.name for param in dependant.query_params),
                        "body": dependant.body_params != [],
                    }
                )
        elif isinstance(route, APIWebSocketRoute):
            rows.append(
                {
                    "method": "WEBSOCKET",
                    "path": route.path,
                    "auth": _auth_kind(route.dependant),
                    "status_code": None,
                    "query": sorted(param.name for param in route.dependant.query_params),
                    "body": False,
                }
            )
    return sorted(rows, key=lambda row: (row["path"], row["method"]))


def _config(tmp_path: Path) -> CanaryConfig:
    config = CanaryConfig.from_yaml(EXAMPLE_YAML)
    return config.model_copy(
        update={
            "collection": config.collection.model_copy(
                update={"storage_path": tmp_path / "storage"}
            ),
            "daemon": config.daemon.model_copy(
                update={
                    "anomalies_dir": tmp_path / "anomalies",
                    "catalog_db": tmp_path / "catalog.db",
                    "capture_profiles": tmp_path / "capture_profiles.yaml",
                    "capture_profile_overrides": tmp_path / "capture_profile_overrides.yaml",
                }
            ),
        }
    )


def _dashboard_app(tmp_path: Path) -> FastAPI:
    """Every optional component the route registration accepts, present."""
    from src.alerts.emitter import AnomalyEventEmitter
    from src.alerts.event_logger import AnomalyEventLogger
    from src.control.audit import ControlAuditLogger
    from src.control.capture import CaptureController
    from src.control.replay_control import ReplayController
    from src.daemon.health import HealthMonitor
    from src.dashboard.api import create_app
    from src.storage.async_writer import AsyncParquetTelemetryWriter
    from src.storage.parquet_writer import ParquetTelemetryWriter

    config = _config(tmp_path)
    storage = config.collection.storage_path
    storage.mkdir(parents=True)
    writer = ParquetTelemetryWriter(storage)
    logger = AnomalyEventLogger(tmp_path / "anomalies")
    return create_app(
        config,
        anomalies_dir=tmp_path / "anomalies",
        models_dir=tmp_path / "models",
        emitter=AnomalyEventEmitter(logger),
        capture_controller=CaptureController(lambda: writer.paths.root),
        replay_controller=ReplayController(
            queue=asyncio.Queue(),
            writer=writer,
            anomaly_logger=logger,
            storage_root=storage,
            broadcast_stale_timeout_seconds=10.0,
        ),
        audit_logger=ControlAuditLogger(tmp_path / "audit.jsonl"),
        control_token="secret",
        health_monitor=HealthMonitor(),
        storage_writer=AsyncParquetTelemetryWriter(writer),
        static_dir=tmp_path / "no-static",
    )


def _companion_app(tmp_path: Path) -> FastAPI:
    """The dashboard app with companion mode on (2026-10-16: the standalone
    companion API is gone; ``/companion/*`` is registered on the daemon when
    ``companion.enabled``, under the daemon's own control-token rule)."""
    from src.config import CanaryConfig
    from src.dashboard.api import create_app

    raw = _config(tmp_path).model_dump(mode="json")
    raw["companion"] = {
        "enabled": True,
        "library_root": str(tmp_path / "library"),
        "pi_host": "http://127.0.0.1:1",
        "discover": False,
    }
    config = CanaryConfig.model_validate(raw)
    config.collection.storage_path.mkdir(parents=True, exist_ok=True)
    return create_app(
        config,
        anomalies_dir=tmp_path / "anomalies",
        control_token="secret",
        static_dir=tmp_path / "no-static",
    )


def _companion_rows(tmp_path: Path) -> list[dict[str, Any]]:
    """The routes companion mode adds, and only those."""
    base = {(row["method"], row["path"]) for row in _route_rows(_dashboard_app(tmp_path / "base"))}
    rows = _route_rows(_companion_app(tmp_path / "companion"))
    return [row for row in rows if (row["method"], row["path"]) not in base]


def _snapshot(tmp_path: Path) -> dict[str, list[dict[str, Any]]]:
    return {
        "dashboard": _route_rows(_dashboard_app(tmp_path / "dashboard")),
        "companion": _companion_rows(tmp_path),
    }


def test_companion_mode_adds_only_companion_routes_all_under_the_control_rule(
    tmp_path: Path,
) -> None:
    rows = _companion_rows(tmp_path)
    assert rows, "companion mode registered nothing"
    assert all(row["path"].startswith("/companion/") for row in rows), rows
    public = {(r["method"], r["path"]) for r in rows if r["auth"] is None}
    assert public == {("GET", "/companion/status")}
    assert {r["auth"] for r in rows if r["auth"] is not None} == {"control"}


def test_route_table_matches_snapshot(tmp_path: Path) -> None:
    table = _snapshot(tmp_path)
    if os.environ.get("CANARY_UPDATE_ROUTE_SNAPSHOT"):
        SNAPSHOT.write_text(json.dumps(table, indent=2, sort_keys=True) + "\n")
    expected = json.loads(SNAPSHOT.read_text())
    for app_name in ("dashboard", "companion"):
        got = {(r["method"], r["path"]): r for r in table[app_name]}
        want = {(r["method"], r["path"]): r for r in expected[app_name]}
        assert sorted(got) == sorted(want), f"{app_name}: routes added or dropped"
        for key, row in want.items():
            assert got[key] == row, f"{app_name} {key}: route contract changed"


def test_every_dashboard_control_route_requires_auth(tmp_path: Path) -> None:
    """``/control/*`` and ``/ws/control`` are the control plane; only the
    ``public.py`` routes are open."""
    rows = _route_rows(_dashboard_app(tmp_path))
    open_control = [
        (row["method"], row["path"])
        for row in rows
        if (row["path"].startswith("/control") or row["path"] == "/ws/control")
        and row["auth"] != "control"
    ]
    assert open_control == []


def test_dashboard_has_one_auth_rule(tmp_path: Path) -> None:
    """Every guarded dashboard route uses the shared dependency, never a copy."""
    rows = _route_rows(_dashboard_app(tmp_path))
    assert {row["auth"] for row in rows} == {"control", None}


def test_components_do_not_change_the_route_table(tmp_path: Path) -> None:
    """Routes register unconditionally: a bare app has the same table, so
    the snapshot above is not an artefact of which components were wired."""
    from src.dashboard.api import create_app

    config = _config(tmp_path / "bare")
    config.collection.storage_path.mkdir(parents=True)
    bare = create_app(
        config, anomalies_dir=tmp_path / "bare" / "anomalies", static_dir=tmp_path / "none"
    )
    assert _route_rows(bare) == _route_rows(_dashboard_app(tmp_path / "full"))


def test_no_route_is_shadowed_by_an_earlier_one(tmp_path: Path) -> None:
    """The snapshot is order-free, but Starlette matches in registration
    order: moving routes into routers can put ``/x/{id}`` ahead of ``/x/y``.
    Every route must be the first match for its own path (params filled with
    a placeholder) and method."""
    import re

    from starlette.routing import Match

    for app in (_dashboard_app(tmp_path / "d"), _companion_app(tmp_path / "c")):
        routes = [r for r in app.routes if isinstance(r, APIRoute | APIWebSocketRoute)]
        for route in routes:
            sample = re.sub(r"\{[^}]+\}", "zz-sample", route.path)
            methods = sorted(route.methods or ()) if isinstance(route, APIRoute) else [None]
            for method in methods:
                scope: dict[str, Any] = {
                    "type": "http" if method is not None else "websocket",
                    "path": sample,
                    "method": method or "GET",
                    "root_path": "",
                }
                first = next(
                    candidate for candidate in routes if candidate.matches(scope)[0] is Match.FULL
                )
                assert first is route, (
                    f"{method} {route.path} is shadowed by {getattr(first, 'path', first)}"
                )
