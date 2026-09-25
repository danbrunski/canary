"""Session-read routes both apps serve: GPS track exports and the chart (R03.7).

The daemon (``/control/sessions/{id}/…`` over its storage root) and the laptop
companion (the same paths over its synced library) carried near-copies of
these three routes, differing only in the root they read and the auth rule
guarding them. One builder now serves both: ``resolve_root`` names the root,
``auth`` is the app's own dependency — the daemon's control token, or the
companion's deliberately different optional bearer (on the laptop, no token
means open).
"""

# No `from __future__ import annotations`: the route signatures below use an
# `Annotated` dependency on a closure, which FastAPI can only resolve from an
# annotation evaluated at definition time.
from collections.abc import Callable
from pathlib import Path
from typing import Annotated, Any

from fastapi import APIRouter, Depends, HTTPException, Query
from fastapi.responses import Response

from src.control.sessions import resolve_session


def build_session_read_router(
    resolve_root: Callable[[], Path], auth: Callable[..., Any]
) -> APIRouter:
    """``GET …/gps.gpx``, ``…/gps.kml`` and ``…/chart`` rooted at ``resolve_root()``."""
    router = APIRouter(dependencies=[Depends(auth)])

    def session_dir(session_id: str) -> Path:
        try:
            return resolve_session(resolve_root(), session_id)
        except FileNotFoundError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc

    SessionDir = Annotated[Path, Depends(session_dir)]  # noqa: N806 — a type alias

    @router.get("/control/sessions/{session_id}/chart")
    def get_session_chart(
        session_id: str,
        channels: str = Query(..., description="Comma-separated source:channel keys"),
        resample_hz: float | None = Query(default=None, gt=0),
    ) -> dict[str, Any]:
        """Task 5.3: per-channel time-series arrays for one stored session —
        ``ChartsPanel``'s "session mode" (load a stored session instead of
        the live stream) instead of hand-written DuckDB over SSH. The
        companion serves the same shape over its synced library."""
        from src.control.session_timeseries import session_time_series_response

        channel_list = [c.strip() for c in channels.split(",") if c.strip()]
        try:
            return session_time_series_response(
                resolve_root(),
                session_id,
                channel_list,
                resample_hz=resample_hz,
            )
        except FileNotFoundError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc

    @router.get("/control/sessions/{session_id}/gps.gpx")
    def get_session_gps_gpx(
        session_id: str,
        directory: SessionDir,
    ) -> Response:
        """Task 5.6a: GPX export of a session's captured GPS track."""
        from src.analysis.gps_export import read_gps_track, to_gpx

        track = read_gps_track(directory)
        return Response(
            content=to_gpx(track, name=session_id),
            media_type="application/gpx+xml",
            headers={"Content-Disposition": f'attachment; filename="{session_id}.gpx"'},
        )

    @router.get("/control/sessions/{session_id}/gps.kml")
    def get_session_gps_kml(
        session_id: str,
        directory: SessionDir,
    ) -> Response:
        """Task 5.6a: KML export of a session's captured GPS track."""
        from src.analysis.gps_export import read_gps_track, to_kml

        track = read_gps_track(directory)
        return Response(
            content=to_kml(track, name=session_id),
            media_type="application/vnd.google-earth.kml+xml",
            headers={"Content-Disposition": f'attachment; filename="{session_id}.kml"'},
        )

    return router


__all__ = ["build_session_read_router"]
