"""MCP server — exposes the running CANary daemon to LLM clients.

A separate process from ``src.daemon``: instantiated by ``canary-mcp`` (see
``__main__.py``) and talks to the daemon over its existing HTTP control plane
on localhost. Direct Parquet reads bypass the daemon for ad-hoc SQL.

The public surface is intentionally tiny: the CLI in ``__main__.py`` is the
entry point; tests import ``build_server`` directly from
:mod:`src.mcp_server.server`.
"""
