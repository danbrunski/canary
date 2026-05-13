#!/usr/bin/env python3
"""Serve the dashboard REST + WebSocket app via uvicorn (Task 5.5).

Bind-host policy lives here, not in ``create_app`` — the app itself doesn't
know where it's mounted, and the same factory is used by tests + production.

The defaults (``host``, ``port``) come from ``canary.yaml``'s ``dashboard:``
block. ``--host`` / ``--port`` flags override. We deliberately *log a
warning* when binding to anything other than loopback / a tight LAN range:
v1 has no auth, and the README is explicit that the Pi should be on a
hotspot / firewalled network. A future v2 will add auth; until then,
loud-on-public-bind is the cheap guard.

Usage:

    .venv/bin/python scripts/serve_dashboard.py
    .venv/bin/python scripts/serve_dashboard.py --host 0.0.0.0 --port 8000
    .venv/bin/python scripts/serve_dashboard.py --anomalies-dir /data/canary/anomalies
"""

from __future__ import annotations

import argparse
import ipaddress
import logging
import sys
from collections.abc import Sequence
from pathlib import Path

import uvicorn

from src.config import CanaryConfig
from src.dashboard.api import create_app
from src.log import configure_logging

log = logging.getLogger(__name__)


def parse_args(argv: Sequence[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        prog="serve_dashboard",
        description=(
            "Run the CANary dashboard REST + WebSocket app via uvicorn. "
            "Defaults from canary.yaml; --host/--port override."
        ),
    )
    parser.add_argument(
        "--config",
        type=Path,
        default=Path("canary.yaml"),
        help="Path to canary.yaml.",
    )
    parser.add_argument(
        "--anomalies-dir",
        type=Path,
        default=Path("data/anomalies"),
        help=(
            "Directory the AnomalyEventLogger writes anomalies-YYYY-MM-DD.jsonl into. "
            "Default data/anomalies."
        ),
    )
    parser.add_argument(
        "--models-dir",
        type=Path,
        default=Path("models"),
        help="Models directory. /status reports loaded versions from manifests here.",
    )
    parser.add_argument(
        "--host",
        default=None,
        help="Bind address. Default: canary.yaml's dashboard.host.",
    )
    parser.add_argument(
        "--port",
        type=int,
        default=None,
        help="Bind port. Default: canary.yaml's dashboard.port.",
    )
    parser.add_argument(
        "--static-dir",
        type=Path,
        default=None,
        help=(
            "Path to the React SPA's built `dist/`. Default: "
            "src/dashboard/static/dist beside the backend. Pass an empty "
            "string to disable static serving entirely (API-only mode)."
        ),
    )
    parser.add_argument(
        "--dev-cors",
        action="store_true",
        help=(
            "Allow CORS from http://localhost:5173 (the Vite dev origin) so "
            "the frontend's `npm run dev` can call the backend directly. "
            "Don't enable in production."
        ),
    )
    parser.add_argument(
        "--log-level",
        default="INFO",
        choices=["DEBUG", "INFO", "WARNING", "ERROR"],
    )
    return parser.parse_args(argv)


def warn_if_public_bind(host: str) -> bool:
    """Log a warning when ``host`` doesn't look loopback / link-local / private.

    v1 has no auth on the dashboard; binding to a public address is the
    foot-gun we explicitly warn about. Returns True if a warning was emitted.
    """
    if host in ("127.0.0.1", "localhost", "::1"):
        return False
    if host == "0.0.0.0":
        log.warning(
            "binding to 0.0.0.0 — dashboard has NO AUTH in v1; ensure firewall / "
            "hotspot-only access. See docs and Task 5.5 for context."
        )
        return True
    try:
        addr = ipaddress.ip_address(host)
    except ValueError:
        # Hostname — can't tell without resolving; warn conservatively.
        log.warning(
            "binding to hostname %r; ensure it resolves to a private network only "
            "(dashboard has no auth in v1).",
            host,
        )
        return True
    if addr.is_loopback or addr.is_link_local or addr.is_private:
        return False
    log.warning("binding to %s which appears public — dashboard has NO AUTH in v1.", host)
    return True


def main(argv: Sequence[str] | None = None) -> int:
    args = parse_args(argv)
    configure_logging(args.log_level)
    config = CanaryConfig.from_yaml(args.config)

    host = args.host or config.dashboard.host
    port = args.port or config.dashboard.port
    warn_if_public_bind(host)

    app = create_app(
        config,
        anomalies_dir=args.anomalies_dir,
        models_dir=args.models_dir,
        static_dir=args.static_dir,
        enable_dev_cors=args.dev_cors,
    )

    log.info(
        "starting dashboard",
        extra={
            "host": host,
            "port": port,
            "anomalies_dir": str(args.anomalies_dir),
            "models_dir": str(args.models_dir),
        },
    )
    uvicorn.run(app, host=host, port=port, log_level=args.log_level.lower())
    return 0


if __name__ == "__main__":
    sys.exit(main())
