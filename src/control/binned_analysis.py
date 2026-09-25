"""Daemon-side I/O for Task 5.1's ad-hoc binned analysis.

:mod:`src.analysis.binned_analysis` is pure pandas/numpy over already-fetched
rows; this module does the I/O — building the shared
:class:`~src.storage.telemetry_query.TelemetryQuery`, scoping it to the
caller's session/date range, and closing it — the same split
:mod:`src.control.trends` keeps against :mod:`src.analysis.trends`.
"""

from __future__ import annotations

from typing import Any

from src.analysis.binned_analysis import DEFAULT_BIN_COUNT, Aggregation, compute_binned_analysis
from src.analysis.channels import rescale_channel_rows
from src.config import CanaryConfig
from src.storage.telemetry_query import TelemetryQuery


def binned_analysis_response(
    config: CanaryConfig,
    *,
    value: str,
    x: str,
    y: str | None = None,
    x_bins: int = DEFAULT_BIN_COUNT,
    y_bins: int | None = None,
    agg: Aggregation = "mean",
    session: str | None = None,
    since: str | None = None,
    until: str | None = None,
    units: str = "stored",
) -> dict[str, Any]:
    """``GET /analysis/binned``'s implementation: fetch the requested
    channels scoped by ``session``/``since``/``until`` and bin ``value`` by
    ``x`` (and ``y`` if given). See
    :func:`src.analysis.binned_analysis.compute_binned_analysis` for the
    response shape.

    ``units="engineering"`` rescales ``decoded:`` channels through
    ``bindings.yaml`` before binning. Those rows hold raw integers by design
    (``decoded:ECM_RPM`` is 8 counts/rpm), so binning them as stored silently
    works in counts — the trap this option closes. ``obd:`` channels are
    already in engineering units and pass through untouched.
    """
    channels = [value, x, *([y] if y else [])]
    query = TelemetryQuery(config.collection.storage_path)
    try:
        rows = query.fetch_channel_rows(channels, session=session, since=since, until=until)
    finally:
        query.close()
    if units == "engineering":
        rows = rescale_channel_rows(rows)
    return compute_binned_analysis(
        rows,
        value_channel=value,
        x_channel=x,
        y_channel=y,
        x_bins=x_bins,
        y_bins=y_bins,
        agg=agg,
        resample_hz=config.preprocessing.resample_hz,
    )


__all__ = ["binned_analysis_response"]
