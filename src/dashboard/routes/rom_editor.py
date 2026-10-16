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
pool and answer 503.

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
from src.analysis.rom_effects import RomEffectRefusalError
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

    def entry_state(sha: str) -> tuple[bytes, dict[str, Any]]:
        """The library image and its ``revision_match``; 404 unknown sha, 409
        when the vehicle has no map (as ``GET .../tables``). Blocking."""
        if not valid_sha(sha):
            raise _not_found(sha)
        try:
            image = library.image(sha)
        except (RomNotFoundError, FileNotFoundError) as exc:
            raise _not_found(sha) from exc
        try:
            library.rom_map()
        except FileNotFoundError as exc:
            raise _map_unavailable(exc) from exc
        return image, library.revision_match(sha)

    def preview(sha: str, body: PreviewRequest) -> dict[str, Any]:
        image, revision_match = entry_state(sha)
        try:
            return rom_editor.preview_edits(
                image,
                revision_match,
                body.label,
                body.edits,
                library.profile_dir,
                sha256=sha,
            )
        except RomEffectRefusalError as exc:
            # EditSpecRefusalError is one, but E08.1's value_out_of_range
            # comes from make_candidate as the base class itself.
            raise HTTPException(
                400,
                {
                    "error": "edit_refused",
                    "kind": rom_editor.refusal_kind(exc),
                    "reason": exc.reason,
                    "message": exc.message,
                },
            ) from exc
        except FileNotFoundError as exc:
            raise _definition_unavailable(exc) from exc

    def job_params(sha: str, body: SimRequest) -> dict[str, Any]:
        """What the child needs, as plain data (``_job_key`` hashes it, so an
        identical click in flight is shared)."""
        _, revision_match = entry_state(sha)
        return {
            "image_path": str(library.root / f"{sha}.bin"),
            "sha256": sha,
            "revision_match": revision_match,
            "profile_dir": str(library.profile_dir),
            "request": body.model_dump(mode="json"),
        }

    @app.post("/control/rom/library/{sha}/edits/preview", dependencies=[Depends(authorize)])
    async def post_rom_edit_preview(sha: str, body: PreviewRequest) -> dict[str, Any]:
        """The spec, its YAML and hash, the candidate's provenance (cells old
        -> new, ranges, reach) and the edited tables' after values."""
        return await run_in_threadpool(preview, sha, body)

    @app.post("/control/rom/library/{sha}/simulate", dependencies=[Depends(authorize)])
    async def post_rom_simulate(sha: str, body: SimRequest) -> dict[str, Any]:
        """The edits on a recorded base, over a stated grid. Every field of the
        body is required: nothing is defaulted (422 names what is missing)."""
        params = await run_in_threadpool(job_params, sha, body)
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
