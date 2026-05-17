#!/usr/bin/env python3
"""Run CANary's local companion API on a laptop/WSL host."""

from __future__ import annotations

import argparse
import os
from collections.abc import Sequence
from pathlib import Path

import uvicorn

from src.companion.api import create_app
from src.companion.library import DEFAULT_LIBRARY_ROOT, CanaryLibrary
from src.config import CanaryConfig
from src.log import configure_logging


def parse_args(argv: Sequence[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        prog="canary_companion",
        description="Start the laptop-side CANary companion API.",
    )
    parser.add_argument(
        "--config",
        type=Path,
        default=Path(os.environ.get("CANARY_CONFIG", "canary.yaml")),
        help="Base CANary config. Hardware fields are kept for schema compatibility.",
    )
    parser.add_argument(
        "--library-root",
        type=Path,
        default=DEFAULT_LIBRARY_ROOT,
        help="Local companion library root. Default: ~/.canary/library.",
    )
    parser.add_argument(
        "--host",
        default="127.0.0.1",
        help="Bind address for the local companion API. Default: 127.0.0.1.",
    )
    parser.add_argument(
        "--port",
        type=int,
        default=8765,
        help="Bind port for the local companion API. Default: 8765.",
    )
    parser.add_argument(
        "--pi-host",
        default=os.environ.get("CANARY_HOST") or os.environ.get("CANARY_URL"),
        help="Default Pi base URL for sync/publish, e.g. http://canary.local:8000.",
    )
    parser.add_argument(
        "--pi-token",
        default=os.environ.get("CANARY_TOKEN") or os.environ.get("CANARY_CONTROL_TOKEN"),
        help="Default Pi control token for sync/publish.",
    )
    parser.add_argument(
        "--token",
        default=os.environ.get("CANARY_COMPANION_TOKEN"),
        help=("Optional local bearer token. If omitted, loopback companion routes are unlocked."),
    )
    parser.add_argument(
        "--no-static",
        action="store_true",
        help="Serve only the API, not the built dashboard bundle.",
    )
    parser.add_argument(
        "--dev-cors",
        action="store_true",
        help="Allow the Vite dev server on localhost:5173 to call this API.",
    )
    parser.add_argument(
        "--allow-origin",
        action="append",
        default=[],
        help=(
            "Browser origin allowed to call the companion API. Defaults to the "
            "--pi-host origin when --pi-host is set."
        ),
    )
    parser.add_argument(
        "--log-level",
        default="INFO",
        choices=["DEBUG", "INFO", "WARNING", "ERROR"],
    )
    return parser.parse_args(argv)


def main(argv: Sequence[str] | None = None) -> int:
    args = parse_args(argv)
    configure_logging(args.log_level)
    config = CanaryConfig.from_yaml(args.config)
    library = CanaryLibrary(args.library_root.expanduser())
    cors_origins = [*_origin(args.pi_host), *args.allow_origin]
    app_kwargs = {
        "library": library,
        "pi_host": args.pi_host,
        "pi_token": args.pi_token,
        "control_token": args.token,
        "enable_dev_cors": args.dev_cors,
        "cors_origins": cors_origins,
    }
    if args.no_static:
        app_kwargs["static_dir"] = None
    app = create_app(config, **app_kwargs)
    uvicorn.run(app, host=args.host, port=args.port)
    return 0


def _origin(url: str | None) -> list[str]:
    if not url:
        return []
    base = url.rstrip("/")
    if not base.startswith(("http://", "https://")):
        base = f"http://{base}"
    return [base]


if __name__ == "__main__":
    raise SystemExit(main())
