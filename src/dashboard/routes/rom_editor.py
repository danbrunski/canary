"""The ROM Editor's routes: preview an edit, simulate it (W09.2).

Both sit under the ROM library (``app.state.rom_library``, so they register
after it) and require the control token. Both are bodies of
:mod:`src.analysis.rom_editor`; the route adds a status code and nothing else:

* ``POST /control/rom/library/{sha}/edits/preview`` -- the E08.1 edit spec the
  page's edits make, the candidate's provenance and the edited tables' after
  values. The order of work of ``GET .../tables``, so it runs in the thread
  pool, with no child and no capture deferral.
* ``POST /control/rom/library/{sha}/simulate`` -- a stated grid, paired Monte
  Carlo draws and per-cell aggregation. Heavy, so it is the workbench child's
  job kind ``rom_sim`` (:func:`src.control.workbench_jobs.run_off_loop`):
  refused while a capture is recording, busy while another child runs.

A refusal inside the child is **data**, not an exception: the simulator's
refusal classes take ``(reason, message)`` and do not survive pickling, so
:func:`src.analysis.rom_editor.run_job` returns them as
``{"refused": {"kind", "reason", "message"}}`` and this route answers 400
``sim_refused``. Left to raise, the same refusal would reach here as a broken
pool and answer 503. The preview goes through the same ``run_job`` in the
thread pool, so one converter serves both routes and every class in
:data:`src.analysis.rom_editor.REFUSALS` is a 400 ``edit_refused``, never a 500.

Order of checks on simulate: 404 / 409-no-map, then the base (an unrecorded one
is a 400 before anything is deferred or spawned: it is a fact about the request,
not about the load), then the capture deferral, then the child.

Nothing is persisted and the candidate image never leaves the simulator: what
a caller keeps is the edit spec YAML. Nothing here reaches the ECU.
"""

from __future__ import annotations

from typing import Any

from fastapi import Depends, FastAPI, HTTPException
from pydantic import BaseModel, ConfigDict
from starlette.concurrency import run_in_threadpool

from src.analysis import rom_editor
from src.analysis.rom_editor import SimRequest
from src.config import CanaryConfig
from src.control.rom_library import RomLibrary, RomNotFoundError, valid_sha
from src.control.workbench_jobs import WorkbenchRefusedError, capture_deferral, run_off_loop
from src.dashboard.security import control_auth_dependency

#: The job kind the simulate route runs in the workbench child.
JOB_KIND = "rom_sim"


class PreviewRequest(BaseModel):
    """``POST .../edits/preview``: E08.1 ``Edit`` dicts and the spec's label.

    The edits are validated by ``rom_edit_spec.parse_edit_spec``, not here, so
    every refusal keeps E08.1's reason name."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    label: str
    edits: list[dict[str, Any]]


def _not_found(sha: str) -> HTTPException:
    return HTTPException(404, f"no ROM library entry {sha!r}")


def _map_unavailable(exc: FileNotFoundError) -> HTTPException:
    # The words GET .../tables answers with, so one handler serves both.
    return HTTPException(409, f"ROM map definition unavailable for this vehicle: {exc}")


def _definition_unavailable(exc: FileNotFoundError) -> HTTPException:
    return HTTPException(409, f"ROM Editor definition unavailable for this vehicle: {exc}")


def _unrecorded_message(revision_match: dict[str, Any]) -> str:
    """Why this base cannot be simulated, in the words ``simulate_edits`` uses,
    plus the library's own reason (an unreadable flash history says why)."""
    why = revision_match.get("reason")
    return (
        f"revision_match is {revision_match.get('status')!r}"
        + (f" ({why})" if why else "")
        + ": only a flashed revision in tune_revisions.yaml can be simulated "
        "(the page still previews the edits)"
    )


def _laptop_path(sha: str) -> str:
    """What a deferred simulate carries in ``laptop_path``. The workbench's own
    text tar-streams a session, which this job never reads."""
    return (
        f"GET /control/rom/library/{sha}/download for the image, then run the same edit spec "
        "through the simulator on the laptop (src.analysis.rom_editor.simulate_edits)"
    )


def register_rom_editor_routes(
    app: FastAPI,
    *,
    config: CanaryConfig,
    resolved_control_token: str | None,
) -> None:
    """Register the preview and simulate routes on ``app``.

    ``register_rom_library_routes`` must have run: the images are its."""
    library: RomLibrary | None = getattr(app.state, "rom_library", None)
    if library is None:
        raise RuntimeError(
            "register_rom_editor_routes needs app.state.rom_library: "
            "call register_rom_library_routes first"
        )
    authorize = control_auth_dependency(resolved_control_token)

    def entry_state(sha: str) -> dict[str, Any]:
        """The library entry's ``revision_match``; 404 unknown sha, 409 when the
        vehicle has no map (as ``GET .../tables``). Blocking."""
        if not valid_sha(sha):
            raise _not_found(sha)
        try:
            library.image(sha)
        except (RomNotFoundError, FileNotFoundError) as exc:
            raise _not_found(sha) from exc
        try:
            library.rom_map()
        except FileNotFoundError as exc:
            raise _map_unavailable(exc) from exc
        return library.revision_match(sha)

    def job_params(sha: str, revision_match: dict[str, Any], **extra: Any) -> dict[str, Any]:
        """What ``rom_editor.run_job`` needs, as plain data (``_job_key`` hashes
        it, so an identical click in flight is shared)."""
        return {
            "image_path": str(library.root / f"{sha}.bin"),
            "sha256": sha,
            "revision_match": revision_match,
            "profile_dir": str(library.profile_dir),
            **extra,
        }

    def preview(sha: str, body: PreviewRequest) -> dict[str, Any]:
        params = job_params(sha, entry_state(sha), label=body.label, edits=body.edits)
        try:
            result = rom_editor.run_job("rom_preview", params)
        except FileNotFoundError as exc:
            raise _definition_unavailable(exc) from exc
        refused = result.get("refused")
        if refused is not None:
            raise HTTPException(400, {"error": "edit_refused", **refused})
        return result

    def simulate_params(sha: str, body: SimRequest) -> dict[str, Any]:
        """Blocking: the entry checks, the base check, the child's params."""
        revision_match = entry_state(sha)
        base = rom_editor.sim_base(revision_match)
        if not base["ok"]:
            raise HTTPException(
                400,
                {
                    "error": "sim_refused",
                    "kind": "simulation",
                    "reason": base["reason"],
                    "message": _unrecorded_message(revision_match),
                },
            )
        return job_params(sha, revision_match, request=body.model_dump(mode="json"))

    @app.post("/control/rom/library/{sha}/edits/preview", dependencies=[Depends(authorize)])
    async def post_rom_edit_preview(sha: str, body: PreviewRequest) -> dict[str, Any]:
        """The spec, its YAML and hash, the candidate's provenance (cells old
        -> new, ranges, reach) and the edited tables' after values."""
        return await run_in_threadpool(preview, sha, body)

    @app.post("/control/rom/library/{sha}/simulate", dependencies=[Depends(authorize)])
    async def post_rom_simulate(sha: str, body: SimRequest) -> dict[str, Any]:
        """The edits on a recorded base, over a stated grid. Every field of the
        body is required: nothing is defaulted (422 names what is missing)."""
        params = await run_in_threadpool(simulate_params, sha, body)
        try:
            try:
                capture_deferral(config)
            except WorkbenchRefusedError as exc:
                raise WorkbenchRefusedError(
                    exc.status, {**exc.detail, "laptop_path": _laptop_path(sha)}
                ) from exc
            result = await run_off_loop(JOB_KIND, params, config)
        except WorkbenchRefusedError as exc:
            raise HTTPException(exc.status, exc.detail) from exc
        except FileNotFoundError as exc:
            raise _definition_unavailable(exc) from exc
        refused = result.get("refused")
        if refused is not None:
            raise HTTPException(400, {"error": "sim_refused", **refused})
        return result


__all__ = ["JOB_KIND", "PreviewRequest", "register_rom_editor_routes"]
