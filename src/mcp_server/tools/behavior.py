"""Behavior-model state read tools."""

from __future__ import annotations

from pathlib import Path
from typing import Any

from src.analysis.session_deviations import analyze_session, compare_session_deviations
from src.config import CanaryConfig
from src.decode.behavior_models import load_behavior_models
from src.mcp_server.client import DaemonClient
from src.profiles import resolve_profile_for_tooling, scope_config
from src.storage.sessions import resolve_session


async def get_behavior_live(
    client: DaemonClient, *, include_series: bool = False
) -> dict[str, Any]:
    """Live ``you-are-here`` overlay for the behavior surfaces.

    Returns per-model ``expected`` / ``actual`` / ``deviation`` / ``severity``
    for the most recent tick. ``severity`` is ``null`` on a row that was not
    scored, and ``not_scored`` names which kind (``off_map``,
    ``actual_stale``, ``operating_mode_excluded``, ``operating_mode_mismatch``,
    ...); never read a ``null`` severity as healthy. ``include_series=True`` also returns each
    model's rolling trail (~4 min at 1 Hz) — the default is ``False`` because
    with enough registered surfaces the full trail can exceed the MCP
    tool-result size cap; pass ``True`` only when you actually need the
    trajectory (e.g. plotting a Δ sparkline), not for a one-shot status check.
    Equivalent to ``GET /models/behavior/live?series=...`` — see
    :mod:`src.dashboard.behavior_live` for the schema.
    """
    return await client.get_json("/models/behavior/live", params={"series": include_series})


async def get_behavior_models(client: DaemonClient) -> dict[str, Any]:
    """Static behavior surfaces (the learned "normal" maps).

    1D/2D surfaces over operating-mode-segmented data. Use this with
    ``get_behavior_live`` to compare "where the engine should be" with
    "where it is right now."
    """
    return await client.get_json("/models/behavior")


def get_behavior_deviations(
    config_path: Path,
    *,
    session: str,
    mode: str | None = None,
    against: str | None = None,
) -> dict[str, Any]:
    """Diff a *stored* session against the learned behavior surfaces — the
    "did my change move behavior?" tool, run locally (no daemon HTTP call,
    pandas/numpy only, like ``query_telemetry``).

    Unlike ``get_behavior_live`` (the current live tick), this replays a
    whole recorded drive through the same decode/segment/evaluate pipeline
    used for training, and reports per-surface aggregates: how many rows the
    model was gated into (``n_observed``), how many it actually spoke for
    (``n_in_domain``), mean actual/expected/deviation, warning/critical
    fraction, and — for the fuel/air ``surface_2d`` recipes — a per-(rpm,load)
    -bin breakdown so a shift's *location* in the operating envelope is
    visible, not just a session-wide average. Use this instead of ad-hoc
    DuckDB over SSH after a mod, a retrain, or any change you want to verify
    actually moved the numbers.

    ``mode`` restricts the returned ``surfaces`` to those gated on that
    ``operating_mode`` (surfaces with no gate are excluded when a mode filter
    is set, matching ``scripts/analyze_session.py --mode``).

    ``against`` (Task 5.4): when given, ``session`` is compared directly
    against this second session id instead of graded standalone — a
    before-mod/after-mod A/B of per-binned-cell deltas (``b - a``) over the
    cells both drives visited, and ``matched_deviation_delta`` — their mean,
    never a whole-session mean, which would move with the drives' operating
    mix (R05.6) — matching ``scripts/analyze_session.py --against``.

    Returns ``{"ok": False, "error": ...}`` on a bad session id or a missing
    behavior_models.yaml rather than raising, matching ``query_telemetry``'s
    validation-error convention.
    """
    config = CanaryConfig.from_yaml(config_path)
    profile = resolve_profile_for_tooling(config, None)
    scoped = scope_config(config, profile)

    try:
        session_dir = resolve_session(scoped.collection.storage_path, session)
        against_dir = (
            resolve_session(scoped.collection.storage_path, against)
            if against is not None
            else None
        )
    except FileNotFoundError as exc:
        return {"ok": False, "error": str(exc)}

    behavior_models_path = scoped.daemon.behavior_models
    if not behavior_models_path.exists():
        return {"ok": False, "error": f"no behavior_models.yaml found ({behavior_models_path})"}
    models = load_behavior_models(behavior_models_path)

    if against_dir is not None:
        comparison = compare_session_deviations(session_dir, against_dir, scoped, models)
        return {"ok": True, **comparison.to_dict(mode_filter=mode)}

    report = analyze_session(session_dir, scoped, models)
    return {"ok": True, **report.to_dict(mode_filter=mode)}


__all__ = ["get_behavior_live", "get_behavior_models", "get_behavior_deviations"]
