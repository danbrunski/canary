"""``canary-mcp`` console entry point.

::

    canary-mcp --stdio           # for ssh / local Claude Code
    canary-mcp                   # streamable-http on $CANARY_MCP_HOST:$CANARY_MCP_PORT
    canary-mcp --transport sse   # legacy SSE
"""

from __future__ import annotations

import argparse
import logging
import sys
from collections.abc import Sequence

from src.log import install_token_redaction
from src.mcp_server.config import DEFAULT_BIND_PORT, load_from_env
from src.mcp_server.server import build_server


def _parse_args(argv: Sequence[str] | None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(prog="canary-mcp", description=__doc__)
    parser.add_argument(
        "--transport",
        choices=("stdio", "sse", "streamable-http"),
        default="streamable-http",
        help="MCP transport (default: streamable-http for LAN clients).",
    )
    parser.add_argument(
        "--stdio",
        action="store_const",
        const="stdio",
        dest="transport",
        help="Shorthand for --transport stdio.",
    )
    parser.add_argument(
        "--log-level",
        default="INFO",
        choices=("DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"),
    )
    return parser.parse_args(argv)


def main(argv: Sequence[str] | None = None) -> int:
    """Console-script entry point. Returns a Unix exit code."""
    args = _parse_args(argv)
    logging.basicConfig(
        level=args.log_level,
        format="%(asctime)s %(levelname)s %(name)s %(message)s",
    )
    # D03.71: no request path may carry a credential into the journal.
    install_token_redaction()
    log = logging.getLogger("canary-mcp")
    cfg = load_from_env()
    server = build_server(cfg)
    if args.transport == "stdio":
        log.info("starting canary-mcp on stdio (daemon=%s)", cfg.daemon_url)
    else:
        log.info(
            "starting canary-mcp transport=%s host=%s port=%s daemon=%s",
            args.transport,
            cfg.bind_host,
            cfg.bind_port or DEFAULT_BIND_PORT,
            cfg.daemon_url,
        )
    try:
        server.run(transport=args.transport)
    except KeyboardInterrupt:
        log.info("canary-mcp shutting down")
        return 0
    return 0


if __name__ == "__main__":  # pragma: no cover — entry point
    sys.exit(main())
