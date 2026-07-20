"""Ad-hoc binned-analysis read tool (Task 5.1)."""

from __future__ import annotations

from typing import Any

from src.mcp_server.client import DaemonClient


async def get_binned_analysis(
    client: DaemonClient,
    *,
    value: str,
    x: str,
    y: str | None = None,
    x_bins: int = 12,
    y_bins: int | None = None,
    agg: str = "mean",
    session: str | None = None,
    since: str | None = None,
    until: str | None = None,
) -> dict[str, Any]:
    """Bin ``value`` (a ``source:channel`` key) by ``x`` (and ``y`` if given)
    over the requested scope — "trims by rpm×load" without hand-written SQL
    or a refit. ``agg`` is one of ``mean``/``median``/``p95``/``count``.

    Equivalent to ``GET /analysis/binned`` — see
    :func:`src.analysis.binned_analysis.compute_binned_analysis` for the
    response shape (``available: false`` with a ``reason`` when the scope
    has no data for the requested channels together).
    """
    params: dict[str, Any] = {
        "value": value,
        "x": x,
        "x_bins": x_bins,
        "agg": agg,
    }
    if y is not None:
        params["y"] = y
    if y_bins is not None:
        params["y_bins"] = y_bins
    if session is not None:
        params["session"] = session
    if since is not None:
        params["since"] = since
    if until is not None:
        params["until"] = until
    return await client.get_json("/analysis/binned", params=params)


__all__ = ["get_binned_analysis"]
