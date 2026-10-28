"""The ROM Editor's routes: preview an edit, simulate it (W09.2), export it.

Four routes, all under the ROM library's registration (``app.state.rom_library``,
so they register after it) and all requiring the control token. Each is a body of
:mod:`src.analysis.rom_editor`; the route adds a status code and nothing else:

* ``POST /control/rom/library/{sha}/edits/preview`` -- the E08.1 edit spec the
  page's edits make, the candidate's provenance and the edited tables' after
  values. The order of work of ``GET .../tables``, so it runs in the thread
  pool, with no child and no capture deferral.
* ``POST /control/rom/library/{sha}/simulate`` -- a stated grid, paired Monte
  Carlo draws and per-cell aggregation. Heavy, so it is the workbench child's
  job kind ``rom_sim`` (:func:`src.control.workbench_jobs.run_off_loop`):
  refused while a capture is recording, busy while another child runs.
* ``POST /control/rom/library/{sha}/edits/export`` -- the edits applied with
  the checksum words rewritten, stored as a library entry whose source record
  carries the provenance (:func:`src.analysis.rom_editor.export_edits`). It
  downloads through ``GET .../download``, as any entry does. Refused (400
  ``export_refused``) when the calibration is not the map's, the parent's
  checksum fails or an edited table is only inferred; runs in the thread
  pool, like the preview.
* ``GET /control/rom/editor/typical-values`` -- the vehicle's declared typical
  values for the Simulate form's "Fill typical values" button (W09.11), each with
  its source (``?sha=``: the vehicle whose map reads that image); ``409
  typical_values_undeclared`` / ``typical_values_invalid`` when the vehicle
  declares none or the file is broken. A read of one small file: it runs in
  the thread pool and changes no request field (the 23-field ``SimRequest`` has no
  defaults, and the page only fills its form with these).

A refusal inside the child is **data**, not an exception: the simulator's
refusal classes take ``(reason, message)`` and do not survive pickling, so
:func:`src.analysis.rom_editor.run_job` returns them as
``{"refused": {"kind", "reason", "message"}}`` and this route answers 400
``sim_refused``. Left to raise, the same refusal would reach here as a broken
pool and answer 503. The preview goes through the same ``run_job`` in the
thread pool, so one converter serves both routes and every class in
:data:`src.analysis.rom_editor.REFUSALS` is a 400 ``edit_refused``, never a 500.

Every route reads an image with the profile ``RomLibrary.map_for`` picks for
it: the active vehicle on a Pi, the one whose map names its calibration on a
desk or a companion (``daemon.rom_map_lookup``). Map, flash history, simulator
kit and typical values all come from that one profile.

Order of checks on simulate: 404 / 409-no-map, then the base (an unrecorded one
is a 400 before anything is deferred or spawned: it is a fact about the request,
not about the load), then the capture deferral, then the child.

Preview and simulate persist nothing and their candidate never leaves the
simulator: what a caller keeps is the edit spec YAML. Export is the one route
that stores an edited image, and only into the library. Nothing here reaches
the ECU.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from fastapi import Depends, FastAPI, HTTPException
from pydantic import BaseModel, ConfigDict
from starlette.concurrency import run_in_threadpool

from src.analysis import rom_editor
from src.analysis.rom_editor import SimRequest
from src.analysis.rom_map import ROM_MAP_FILE
from src.analysis.sim_contract import SimulationRefusalError
from src.config import CanaryConfig
from src.control.rom_library import RomLibrary, RomMapLookupError, RomNotFoundError, valid_sha
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

    def image_profile(sha: str) -> Path:
        """The profile whose map reads the entry (``RomLibrary.map_for``): its
        map, flash history, simulator kit and typical values are the ones used.
        404 unknown sha, 409 when no profile has a map (as ``GET .../tables``),
        422 when two maps name the calibration. Blocking."""
        if not valid_sha(sha):
            raise _not_found(sha)
        try:
            image = library.image(sha)
        except (RomNotFoundError, FileNotFoundError) as exc:
            raise _not_found(sha) from exc
        try:
            match = library.map_for(image)
        except RomMapLookupError as exc:
            raise HTTPException(422, str(exc)) from exc
        if match.rom_map is None:
            raise _map_unavailable(FileNotFoundError(library.profile_dir / ROM_MAP_FILE))
        return match.profile_dir

    def entry_state(sha: str) -> tuple[Path, dict[str, Any]]:
        """The entry's profile (:func:`image_profile`) and its ``revision_match``
        in that profile's flash history. Blocking."""
        profile = image_profile(sha)
        return profile, library.revision_match(sha, profile)

    def job_params(sha: str, state: tuple[Path, dict[str, Any]], **extra: Any) -> dict[str, Any]:
        """What ``rom_editor.run_job`` needs, as plain data (``_job_key`` hashes
        it, so an identical click in flight is shared)."""
        profile, revision_match = state
        return {
            "image_path": str(library.root / f"{sha}.bin"),
            "sha256": sha,
            "revision_match": revision_match,
            "profile_dir": str(profile),
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

    def export(sha: str, body: PreviewRequest) -> dict[str, Any]:
        """Blocking: the entry checks, the export, the library write."""
        profile, revision_match = entry_state(sha)
        try:
            exported = rom_editor.export_edits(
                library.image(sha),
                revision_match,
                body.label,
                body.edits,
                profile,
                sha256=sha,
            )
        except ValueError as exc:
            refused = rom_editor.export_refusal(exc)
            if refused is None:
                raise
            raise HTTPException(400, {"error": "export_refused", **refused}) from exc
        entry, created = library.add_image(
            exported.image,
            source="rom_editor_export",
            label=body.label,
            export_meta=exported.provenance,
        )
        return {"entry": entry, "created": created, "provenance": exported.provenance}

    def simulate_params(sha: str, body: SimRequest) -> dict[str, Any]:
        """Blocking: the entry checks, the base check, the child's params."""
        state = entry_state(sha)
        revision_match = state[1]
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
        return job_params(sha, state, request=body.model_dump(mode="json"))

    @app.get("/control/rom/editor/typical-values", dependencies=[Depends(authorize)])
    async def get_rom_typical_values(sha: str | None = None) -> dict[str, Any]:
        """The vehicle's typical stated values, each with its source, and the
        fillable fields it leaves empty with the reason. Fills nothing by itself.
        With ``sha``, the vehicle whose map reads that image (the one its
        simulation runs on); without, the active vehicle."""
        profile = (
            library.profile_dir if sha is None else await run_in_threadpool(image_profile, sha)
        )
        try:
            return await run_in_threadpool(rom_editor.typical_values, profile)
        except SimulationRefusalError as exc:
            raise HTTPException(409, {"error": exc.reason, "message": exc.message}) from exc

    @app.post("/control/rom/library/{sha}/edits/preview", dependencies=[Depends(authorize)])
    async def post_rom_edit_preview(sha: str, body: PreviewRequest) -> dict[str, Any]:
        """The spec, its YAML and hash, the candidate's provenance (cells old
        -> new, ranges, reach) and the edited tables' after values."""
        return await run_in_threadpool(preview, sha, body)

    @app.post("/control/rom/library/{sha}/edits/export", dependencies=[Depends(authorize)])
    async def post_rom_edit_export(sha: str, body: PreviewRequest) -> dict[str, Any]:
        """The edited, checksummed image as a new library entry: ``entry`` (its
        ``export_meta`` the provenance), ``created`` (false when the library
        already held these bytes) and ``provenance``."""
        return await run_in_threadpool(export, sha, body)

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
