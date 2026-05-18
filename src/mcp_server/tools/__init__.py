"""Read tools — wrap daemon HTTP endpoints or direct Parquet reads.

Each module exposes plain async functions taking their dependencies as
explicit arguments. ``server.py`` registers them as MCP tools by closing
over a shared :class:`~src.mcp_server.client.DaemonClient` and (for SQL)
:class:`~src.mcp_server.parquet_query.TelemetryQuery`. Keeping the tool
bodies pure makes them trivially unit-testable.
"""
