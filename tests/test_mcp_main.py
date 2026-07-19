"""Tests for the ``canary-mcp`` CLI argument parser."""

from __future__ import annotations

import pytest

from src.mcp_server.__main__ import _parse_args


def test_defaults_match_on_pi_deployment() -> None:
    """Bare invocation runs the LAN transport (the on-Pi default) at INFO."""
    args = _parse_args([])
    assert args.transport == "streamable-http"
    assert args.log_level == "INFO"


def test_stdio_shortcut_sets_transport() -> None:
    """``--stdio`` is the documented shorthand for ``--transport stdio``."""
    args = _parse_args(["--stdio"])
    assert args.transport == "stdio"


def test_transport_flag_accepts_sse() -> None:
    """Legacy SSE is still selectable for older MCP clients."""
    args = _parse_args(["--transport", "sse"])
    assert args.transport == "sse"


def test_invalid_transport_rejected() -> None:
    """argparse rejects bogus transport names rather than silently downgrading."""
    with pytest.raises(SystemExit):
        _parse_args(["--transport", "websocket"])
