"""Engine Tuning Logs and Analysis routes (W04).

Every route that reads session *data* goes through
:func:`src.control.workbench_jobs.run_off_loop` — the digest's bounded,
single-flight child — and refuses with a named reason while a capture is
recording. The capture browser reads only sidecars and Parquet footers.

Refusals are structured (``detail.error`` names the kind) so the page can say
*which* unavailable it is: ``analysis_deferred`` (a capture is recording),
``analysis_busy``, ``analysis_unavailable`` (the child failed), or a 400 for a
request the gates refuse outright.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any, Literal

from fastapi import Depends, FastAPI, HTTPException, Query

from src.analysis.channels import ChannelSpecError
from src.analysis.dyno_workbench import DynoRequest
from src.analysis.rom_trace import load_viewer_definition, mapping_refusal
from src.analysis.tuning import GateError
from src.analysis.workbench import (
    AIRFLOW_SPEC,
    DEFAULT_MIN_CELL_N,
    FRAME_ROW_CAP,
    REGIMES,
    RPM_SPEC,
    TIMING_SPEC,
)
from src.config import CanaryConfig
from src.control.calibration import profile_dir
from src.control.logs import DEFAULT_LOG_LIMIT, list_logs
from src.control.workbench_jobs import (
    Selection,
    WorkbenchRefusedError,
    capture_deferral,
    dyno_preflight,
    run_off_loop,
    select_sessions,
)
from src.dashboard.security import (
    control_auth_dependency,
)
from src.storage.sessions import resolve_session

DEFAULT_MEASURED = "obd:O2_WR_LAMBDA_S1"


def _split(raw: str | None) -> list[str]:
    return [part.strip() for part in (raw or "").split(",") if part.strip()]


def _optional_float(raw: str | None) -> float | None:
    if raw is None or raw.strip().lower() in ("", "none", "null"):
        return None
    try:
        return float(raw)
    except ValueError as exc:
        raise HTTPException(400, f"not a number: {raw!r}") from exc


def register_workbench_routes(
    app: FastAPI,
    *,
    config: CanaryConfig,
    bindings_path: Path,
    dids_path: Path,
    resolved_control_token: str | None,
) -> None:
    authorize = control_auth_dependency(resolved_control_token)

    async def run(kind: str, params: dict[str, Any], names: list[str]) -> dict[str, Any]:
        try:
            capture_deferral(config, names)
            return await run_off_loop(
                kind, params, config, bindings_path=bindings_path, dids_path=dids_path
            )
        except WorkbenchRefusedError as exc:
            raise HTTPException(exc.status, exc.detail) from exc
        except (GateError, ChannelSpecError) as exc:
            raise HTTPException(400, {"error": "analysis_refused", "message": str(exc)}) from exc

    def pooled(sessions: str | None, revision: str | None) -> Selection:
        try:
            capture_deferral(config, _split(sessions))
            return select_sessions(config, sessions=_split(sessions), revision=revision or None)
        except WorkbenchRefusedError as exc:
            raise HTTPException(exc.status, exc.detail) from exc

    async def run_pooled(kind: str, selection: Selection, params: dict[str, Any]) -> dict[str, Any]:
        params = {
            **params,
            "sessions": [str(p) for p in selection.usable],
            "unavailable": [item.as_dict() for item in selection.over_budget],
        }
        result = await run(kind, params, [p.name for p in selection.usable])
        return {**result, "selection": selection.as_dict()}

    @app.get("/control/logs", dependencies=[Depends(authorize)])
    def get_logs(
        include_sessions: bool = False,
        limit: int = Query(default=DEFAULT_LOG_LIMIT, ge=1, le=1000),
    ) -> dict[str, Any]:
        return list_logs(config, include_sessions=include_sessions, limit=limit)

    @app.get("/control/sessions/{session_id}/frame", dependencies=[Depends(authorize)])
    async def get_session_frame(
        session_id: str,
        specs: str = Query(..., description="Comma-separated channel specs"),
        grid: str = "1s",
        regime: str = "all",
        offset: int = Query(default=0, ge=0),
        max_rows: int = Query(default=FRAME_ROW_CAP, ge=1, le=FRAME_ROW_CAP),
    ) -> dict[str, Any]:
        if regime not in REGIMES:
            raise HTTPException(400, f"regime must be one of {', '.join(REGIMES)}")
        names = _split(specs)
        if not names:
            raise HTTPException(400, "name at least one channel spec")
        try:
            session = resolve_session(config.collection.storage_path, session_id)
        except FileNotFoundError as exc:
            raise HTTPException(404, str(exc)) from exc
        try:
            capture_deferral(config, [session_id])
            selection = select_sessions(config, sessions=[session_id])
        except WorkbenchRefusedError as exc:
            raise HTTPException(exc.status, exc.detail) from exc
        if selection.over_budget:
            raise HTTPException(
                409,
                {
                    "error": "analysis_refused",
                    "reason": "over_budget",
                    "message": selection.over_budget[0].reason,
                },
            )
        params = {
            "session": str(session),
            "specs": names,
            "grid": grid,
            "regime": regime,
            "offset": offset,
            "max_rows": max_rows,
        }
        return await run("frame", params, [session_id])

    @app.get("/control/sessions/{session_id}/cell-residency", dependencies=[Depends(authorize)])
    async def get_cell_residency(
        session_id: str,
        table: str = Query(..., description="ROM table kind, e.g. fuel_targets"),
    ) -> dict[str, Any]:
        """Per-cell sample count and dwell seconds from the ECM's trace channels
        (W05.6). Deferred while a capture records; a log, table or mapping that
        cannot answer returns ``available: false`` with a named ``reason``,
        never an empty grid."""
        try:
            definition = load_viewer_definition(profile_dir(config))
        except FileNotFoundError as exc:
            raise HTTPException(409, "ROM map definitions unavailable for this vehicle") from exc
        info = definition.table(table)
        if info is None:
            raise HTTPException(404, f"no ROM table {table!r} for this vehicle")
        try:
            session = resolve_session(config.collection.storage_path, session_id)
        except FileNotFoundError as exc:
            raise HTTPException(404, str(exc)) from exc
        try:
            capture_deferral(config, [session_id])
        except WorkbenchRefusedError as exc:
            raise HTTPException(exc.status, exc.detail) from exc
        # A profile fact, not a data fact: refuse before spawning a child.
        refusal = mapping_refusal(info.trace)
        if refusal is not None:
            return {
                "session_id": session.name,
                "table": table,
                "available": False,
                **refusal,
            }
        try:
            selection = select_sessions(config, sessions=[session_id])
        except WorkbenchRefusedError as exc:
            raise HTTPException(exc.status, exc.detail) from exc
        if selection.over_budget:
            raise HTTPException(
                409,
                {
                    "error": "analysis_refused",
                    "reason": "over_budget",
                    "message": selection.over_budget[0].reason,
                },
            )
        return await run("cell_residency", {"session": str(session), "table": table}, [session_id])

    @app.get("/analysis/delivery-error", dependencies=[Depends(authorize)])
    async def get_delivery_error(
        sessions: str | None = None,
        revision: str | None = None,
        measured: str = DEFAULT_MEASURED,
        min_load: str | None = None,
    ) -> dict[str, Any]:
        """Delivery error by rpm band. ``measured`` is the measured-AFR source and
        has no silent default choice on the page: the ECM's λ and the A/F voltage
        path (``semantic:measured_afr_b1``) disagree by ~3.8 %."""
        floor = _optional_float(min_load)
        return await run_pooled(
            "delivery_error",
            pooled(sessions, revision),
            {"measured": measured, "min_load": floor},
        )

    @app.get("/analysis/trim-surface", dependencies=[Depends(authorize)])
    async def get_trim_surface(
        sessions: str | None = None,
        revision: str | None = None,
        rpm: str = RPM_SPEC,
        airflow: str = AIRFLOW_SPEC,
        min_n: int = Query(default=DEFAULT_MIN_CELL_N, ge=1),
    ) -> dict[str, Any]:
        return await run_pooled(
            "trim_surface",
            pooled(sessions, revision),
            {"rpm": rpm, "airflow": airflow, "min_n": min_n},
        )

    @app.get("/analysis/timing", dependencies=[Depends(authorize)])
    async def get_timing(
        sessions: str | None = None,
        revision: str | None = None,
        timing: str = TIMING_SPEC,
        rpm: str = RPM_SPEC,
        airflow: str = AIRFLOW_SPEC,
        min_n: int = Query(default=DEFAULT_MIN_CELL_N, ge=1),
    ) -> dict[str, Any]:
        return await run_pooled(
            "timing",
            pooled(sessions, revision),
            {"timing": timing, "rpm": rpm, "airflow": airflow, "min_n": min_n},
        )

    @app.get("/analysis/dyno", dependencies=[Depends(authorize)])
    async def get_dyno(
        sessions: str | None = None,
        revision: str | None = None,
        road: str | None = None,
        payload_kg: float | None = Query(default=None, ge=0.0),
        induction: Literal["na", "boosted"] | None = None,
        ambient_temp_c: float | None = None,
        allow_single_direction: bool = False,
    ) -> dict[str, Any]:
        """Wheel power and torque against rpm per session and gear (H07.9).

        Every rpm rise found comes back as a pull or its refusal; a run whose
        road load or squat is still owed is refused ``coefficients_pending``
        rather than computed from a default. ``road``, ``payload_kg`` and
        ``induction`` are run conditions the operator states; a run without
        them is refused naming each."""
        request = DynoRequest(
            road=(road or "").strip() or None,
            payload_kg=payload_kg,
            induction=induction,
            ambient_temp_c=ambient_temp_c,
            allow_single_direction=allow_single_direction,
        )
        selection = pooled(sessions, revision)
        try:
            dyno_preflight(config, selection)
        except WorkbenchRefusedError as exc:
            raise HTTPException(exc.status, exc.detail) from exc
        return await run_pooled("dyno", selection, {"request": request.as_dict()})

    @app.get("/analysis/pull-comparison", dependencies=[Depends(authorize)])
    async def get_pull_comparison(
        revision_a: str,
        revision_b: str,
        sessions: str | None = None,
        value: str = TIMING_SPEC,
        rpm: str = RPM_SPEC,
        airflow: str = AIRFLOW_SPEC,
        min_n: int = Query(default=DEFAULT_MIN_CELL_N, ge=1),
        floor_channel: str | None = None,
    ) -> dict[str, Any]:
        """Revision A vs B per cell. ``sessions`` narrows each side to the named
        sessions on that revision; without it the newest sessions on each
        revision are pooled (up to the per-request limit, the rest named)."""
        if revision_a == revision_b:
            raise HTTPException(400, "choose two different revisions")
        names = set(_split(sessions))
        sides: dict[str, Selection] = {}
        for side, revision in (("a", revision_a), ("b", revision_b)):
            selection = pooled(None, revision)
            if names:
                selection.usable = [p for p in selection.usable if p.name in names]
            sides[side] = selection
        params = {
            "sessions_a": [str(p) for p in sides["a"].usable],
            "sessions_b": [str(p) for p in sides["b"].usable],
            "unavailable": [item.as_dict() for side in sides.values() for item in side.over_budget],
            "revision_a": revision_a,
            "revision_b": revision_b,
            "value": value,
            "rpm": rpm,
            "airflow": airflow,
            "min_n": min_n,
            "floor_channel": floor_channel or None,
        }
        result = await run(
            "pull_comparison",
            params,
            [p.name for side in sides.values() for p in side.usable],
        )
        return {**result, "selection": {k: v.as_dict() for k, v in sides.items()}}


__all__ = ["register_workbench_routes"]
