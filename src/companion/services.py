"""Service helpers for the companion API.

Also home to the backend-agnostic ``/control/*`` handlers shared with
``src/dashboard/routes/control/`` (refactoring.md 1.2) for jobs, training
runs, feedback, model manifests and dataset health: each takes the bits that
differ between the companion (local library-backed) and control (live-daemon)
callers as parameters, with an optional hook for the one caller's extra side
effect (control's audit-event publish). Callers keep their own request-schema
classes and unpack them to primitives before calling in, so this module never
depends on either schema module. The per-session routes both apps serve are
one router instead (:mod:`src.dashboard.routes.stored_sessions`, R03.7).
"""

from __future__ import annotations

import asyncio
import json
import secrets
import shutil
import sys
import tempfile
import zipfile
from collections.abc import Callable, Mapping
from datetime import UTC, date, datetime
from io import BytesIO
from pathlib import Path, PurePosixPath
from typing import Any

import yaml
from fastapi import HTTPException

from src.alerts.feedback import FEEDBACK_LABELS, FeedbackStore
from src.artifact_hash import sha256_file
from src.companion.library import CanaryLibrary, LibrarySession
from src.config import CanaryConfig
from src.control.jobs import JobController
from src.control.sessions import (
    summarize_session,
)
from src.mcp_server.client import DaemonClient, DaemonError
from src.models.training_ledger import TrainingLedger


def companion_config(base_config: CanaryConfig, library: CanaryLibrary) -> CanaryConfig:
    """Return a config pointed at the local companion library."""

    anomalies_dir = library.root / "anomalies"
    control_dir = library.root / "control"
    for path in (anomalies_dir, control_dir):
        path.mkdir(parents=True, exist_ok=True)
    collection = base_config.collection.model_copy(update={"storage_path": library.sessions_dir})
    daemon = base_config.daemon.model_copy(
        update={
            "anomalies_dir": anomalies_dir,
            "models_dir": library.models_dir,
            "jobs_dir": library.jobs_dir,
            "audit_log": control_dir / "audit.jsonl",
            "feedback_log": control_dir / "anomaly_feedback.jsonl",
            "vehicle_ledger": control_dir / "vehicle_ledger.jsonl",
            "training_ledger": library.training_runs_dir / "training_runs.jsonl",
            "tuning_config": control_dir / "tuning.yaml",
            "tuning_audit": control_dir / "tuning_audit.jsonl",
        }
    )
    return base_config.model_copy(update={"collection": collection, "daemon": daemon})


def write_companion_config(config: CanaryConfig, path: Path) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = config.model_dump(mode="json")
    path.write_text(yaml.safe_dump(payload, sort_keys=False))
    return path


def import_session_archive(
    archive_bytes: bytes,
    library: CanaryLibrary,
    *,
    config: CanaryConfig | None = None,
    source_host: str | None = None,
) -> LibrarySession:
    """Safely extract a Pi session zip and import it into the local library."""

    with tempfile.TemporaryDirectory(prefix="session-import-", dir=library.sync_dir) as tmp:
        tmp_path = Path(tmp)
        with zipfile.ZipFile(BytesIO(archive_bytes)) as archive:
            _safe_extract_zip(archive, tmp_path)
        session_dirs = [path for path in tmp_path.iterdir() if path.is_dir()]
        if len(session_dirs) != 1:
            raise ValueError("expected archive to contain exactly one session directory")
        return library.import_session(session_dirs[0], config=config, source_host=source_host)


def _make_job_controller(
    config: CanaryConfig,
    *,
    repo_root: Path,
    config_path: Path,
    python: Path | None,
) -> JobController:
    py = python
    if py is None:
        candidate = repo_root / ".venv" / "bin" / "python"
        py = candidate if candidate.exists() else Path(sys.executable)
    return JobController(
        config=config,
        repo_root=repo_root,
        config_path=config_path,
        jobs_dir=config.daemon.jobs_dir,
        python=py,
        compute_target="laptop",
    )


def _library_session(library: CanaryLibrary, session_id: str) -> LibrarySession:
    session = library.get_session(session_id)
    if session is None:
        raise HTTPException(status_code=404, detail=f"session not found: {session_id}")
    return session


def _library_session_path(library: CanaryLibrary, session_id: str) -> Path:
    session = _library_session(library, session_id)
    path = Path(session.path)
    if not path.is_dir():
        raise HTTPException(status_code=404, detail=f"session files missing: {session_id}")
    return path


def _library_session_to_control_dict(session: LibrarySession) -> dict[str, Any]:
    path = Path(session.path)
    if path.is_dir():
        data = summarize_session(path).to_dict()
    else:
        data = {
            "session_id": session.session_id,
            "session_path": session.path,
            "started_at": session.started_at,
            "telemetry_chunks": 0,
            "tag_count": len(session.tags),
            "has_manifest": False,
            "pinned": session.pinned,
            "metadata": {},
            "size_bytes": session.size_bytes or 0,
        }
    data.update(
        {
            "source_host": session.source_host,
            "imported_at": session.imported_at,
            "training_candidate": session.training_candidate,
            "quality_warnings": session.quality_warnings,
            "tags": session.tags,
        }
    )
    return data


def _dataset_row_to_dict(row: Mapping[str, Any]) -> dict[str, Any]:
    payload = dict(row)
    raw_manifest = payload.get("manifest")
    if isinstance(raw_manifest, str):
        try:
            payload["manifest"] = json.loads(raw_manifest)
        except json.JSONDecodeError:
            payload["manifest"] = {}
    elif raw_manifest is None:
        payload["manifest"] = {}
    return payload


def _safe_extract_zip(archive: zipfile.ZipFile, target_root: Path) -> None:
    root = target_root.resolve()
    for item in archive.infolist():
        name = PurePosixPath(item.filename)
        if name.is_absolute() or ".." in name.parts:
            raise ValueError(f"unsafe archive path: {item.filename}")
        if item.is_dir():
            continue
        target = root.joinpath(*name.parts)
        resolved_target = target.resolve()
        if root not in (resolved_target, *resolved_target.parents):
            raise ValueError(f"unsafe archive path: {item.filename}")
        target.parent.mkdir(parents=True, exist_ok=True)
        with archive.open(item) as source, target.open("wb") as dest:
            shutil.copyfileobj(source, dest)


def _require_optional_bearer(expected_token: str | None, authorization: str | None) -> None:
    if not expected_token:
        return
    prefix = "Bearer "
    if not authorization or not authorization.startswith(prefix):
        raise HTTPException(status_code=401, detail="missing bearer token")
    provided = authorization[len(prefix) :]
    if not secrets.compare_digest(provided, expected_token):
        raise HTTPException(status_code=401, detail="invalid bearer token")


def _base_url(host: str) -> str:
    base = host.rstrip("/")
    if not base.startswith(("http://", "https://")):
        return f"http://{base}"
    return base


def _request_json(
    host: str,
    method: str,
    path: str,
    *,
    token: str | None,
    body: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    """``method path`` on the remote Pi's control API, via `DaemonClient`.

    Kept synchronous (wraps its own event loop) rather than turned into an
    ``async def`` — every caller is a plain (non-async) FastAPI route handler
    that Starlette already runs off the event loop in a worker thread, so
    ``asyncio.run`` here is safe and avoids rippling ``async``/``await``
    through routes.py, api.py, and their tests for a two-call-site client
    swap.
    """

    async def _call() -> Any:
        async with DaemonClient(host, token, timeout=120.0) as client:
            if method == "GET":
                return await client.get_json(path)
            if method == "POST":
                return await client.post_json(path, dict(body) if body is not None else None)
            raise RuntimeError(f"{method} {path} failed: unsupported method")

    try:
        result = asyncio.run(_call())
    except DaemonError as exc:
        raise RuntimeError(f"{method} {path} failed: {exc}") from exc
    if result is None:
        return {}
    if not isinstance(result, dict):
        raise RuntimeError(f"{method} {path} returned non-object JSON")
    return result


def _request_bytes(host: str, method: str, path: str, *, token: str | None) -> bytes:
    """``method path`` on the remote Pi's control API, raw bytes (see `_request_json`)."""

    async def _call() -> bytes:
        if method != "GET":
            raise RuntimeError(f"{method} {path} failed: unsupported method")
        async with DaemonClient(host, token, timeout=300.0) as client:
            return await client.get_bytes(path)

    try:
        return asyncio.run(_call())
    except DaemonError as exc:
        raise RuntimeError(f"{method} {path} failed: {exc}") from exc


def _download_session_export(
    host: str,
    session_id: str,
    *,
    token: str | None,
    dest_dir: Path,
    expected_sha256: str | None = None,
) -> bytes:
    """Resumable, checksum-verified fetch of a Pi session export (6.34).

    Streams into ``dest_dir/{session_id}.zip`` via `DaemonClient.download_file`'s
    Range-based resume: a connection dropped mid-transfer leaves the partial
    file on disk, so calling this again for the same session continues from
    where it left off instead of re-downloading from byte 0 (the
    "retry-after-interrupt" story). If a *complete*, checksum-matching
    download from an earlier attempt is already sitting in ``dest_dir`` (the
    process died after the transfer finished but before import completed),
    skip the network entirely — a byte-for-byte duplicate of what we're
    about to fetch.
    """
    dest_dir.mkdir(parents=True, exist_ok=True)
    dest = dest_dir / f"{session_id}.zip"
    if expected_sha256 is not None and dest.exists() and sha256_file(dest) == expected_sha256:
        data = dest.read_bytes()
        dest.unlink(missing_ok=True)
        return data

    async def _call() -> Path:
        async with DaemonClient(host, token, timeout=300.0) as client:
            return await client.download_file(f"/control/sessions/{session_id}/export", dest)

    try:
        result_path = asyncio.run(_call())
    except DaemonError as exc:
        raise RuntimeError(f"GET /control/sessions/{session_id}/export failed: {exc}") from exc

    digest = sha256_file(result_path)
    if expected_sha256 is not None and digest != expected_sha256:
        result_path.unlink(missing_ok=True)
        raise ValueError(
            f"checksum mismatch for session {session_id} export: "
            f"expected {expected_sha256}, got {digest}"
        )
    data = result_path.read_bytes()
    result_path.unlink(missing_ok=True)
    return data


def run_queued_action(
    library: CanaryLibrary,
    kind: str,
    params: dict[str, Any],
    fn: Callable[[], dict[str, Any]],
) -> dict[str, Any]:
    """Run ``fn`` under library-persisted queue/retry bookkeeping (6.34/6.37).

    Every companion-initiated Pi round trip (session sync, model publish) is
    recorded as a `queued_actions` row before it runs and flipped to
    `done`/`failed` after, so a dropped Wi-Fi link leaves a durable,
    inspectable row in `/companion/actions` (with the error message) instead
    of a silently-lost request — and the *same* call, retried, resumes
    rather than restarting (session downloads via `_download_session_export`'s
    `.part` file; model publish by re-reading the still-on-disk artifact).
    """
    action_id = library.queue_action(kind, params)
    try:
        result = fn()
    except Exception as exc:
        library.update_action(action_id, "failed", detail=str(exc))
        raise
    library.update_action(action_id, "done", detail=json.dumps(result, default=str))
    return {**result, "action_id": action_id}


def _artifact_path(config: CanaryConfig, model_type: str) -> Path:
    if model_type == "isolation_forest":
        return config.daemon.models_dir / config.daemon.isolation_forest_artifact
    if model_type == "lstm_autoencoder":
        return config.daemon.models_dir / config.daemon.lstm_artifact
    if model_type == "tcn_autoencoder":
        return config.daemon.models_dir / config.daemon.tcn_artifact
    raise HTTPException(status_code=400, detail=f"unsupported model type: {model_type}")


def _model_id(model_type: str, manifest: Mapping[str, Any]) -> str:
    for key in ("run_id", "model_version", "version"):
        value = manifest.get(key)
        if value:
            return str(value)
    stamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%S")
    return f"{model_type}-{stamp}"


def _manifest_version(manifest: Mapping[str, Any]) -> str | None:
    for key in ("model_version", "version", "run_id"):
        value = manifest.get(key)
        if value:
            return str(value)
    return None


def _unlink_path(path: Path) -> None:
    path.unlink(missing_ok=True)


# -- shared `/control/*` handlers (refactoring.md 1.2) -----------------------
#
# Both the companion app and the dashboard's control API register the same
# job/training-run/feedback/dataset-health routes against different backing
# stores (companion: `CanaryLibrary`'s sqlite index; control: the live
# daemon's). Each handler below takes that difference as a parameter instead
# of being written twice. The per-session routes (read, metadata, pin, unpin,
# delete, export, checksum) are no longer here: both apps include one router,
# `src.dashboard.routes.stored_sessions` (R03.7).


def get_model_manifest_response(
    model_type: str,
    *,
    resolve_manifest_path: Callable[[str], Path],
) -> dict[str, Any]:
    """Shared body of `GET /control/models/{model_type}/manifest` (6.37).

    Lets a publisher read the *target's* currently-active manifest before
    uploading, so a compatibility report (`src.models.manifest.compatibility_report`)
    can be built client-side. 404s (no manifest written yet — first install)
    are the caller's to interpret, not an error here.
    """
    manifest_path = resolve_manifest_path(model_type)
    if not manifest_path.exists():
        raise HTTPException(
            status_code=404, detail=f"no manifest on file for model type: {model_type}"
        )
    try:
        manifest = json.loads(manifest_path.read_text())
    except (OSError, json.JSONDecodeError) as exc:
        raise HTTPException(status_code=500, detail=f"unreadable manifest: {exc}") from exc
    return {"model_type": model_type, "manifest": manifest}


def submit_job_response(
    jobs: JobController,
    kind: str,
    params: dict[str, Any],
    *,
    on_submit: Callable[[dict[str, Any]], None] | None = None,
) -> dict[str, Any]:
    try:
        job = jobs.submit(kind, params)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    record = job.to_dict()
    if on_submit is not None:
        on_submit(record)
    return {"job": record}


def list_jobs_response(jobs: JobController) -> dict[str, Any]:
    records = [job.to_dict() for job in jobs.list_jobs()]
    return {"jobs": records, "count": len(records)}


def job_logs_response(jobs: JobController, job_id: str) -> dict[str, Any]:
    logs = jobs.logs(job_id)
    if logs is None:
        raise HTTPException(status_code=404, detail=f"job not found: {job_id}")
    return {"job_id": job_id, "log": logs}


def get_job_response(jobs: JobController, job_id: str) -> dict[str, Any]:
    job = jobs.get(job_id)
    if job is None:
        raise HTTPException(status_code=404, detail=f"job not found: {job_id}")
    return {"job": job.to_dict()}


#: Job kinds that write a structured result JSON, and the suffix it lands
#: under (``<jobs_dir>/<job_id>.<suffix>.json``) — see
#: ``JobController._replay_window_command``/``_did_sweep_command``.
_RESULT_SUFFIX_BY_KIND: dict[str, str] = {
    "replay_window": "replay",
    "did_sweep": "did_sweep",
}


def job_result_response(jobs: JobController, job_id: str) -> dict[str, Any]:
    """`GET /control/jobs/{id}/result` (docs/roadmap/archive/dashboard-ux.md Task 4.1).

    Only some job kinds write a structured report (`_RESULT_SUFFIX_BY_KIND`)
    — that's the same `<jobs_dir>/<job_id>.<suffix>.json` path each kind's
    MCP tool resolves on its own (`src/mcp_server/tools/replay.py`,
    `src/mcp_server/tools/did_sweep.py`), so the dashboard can render the
    identical result an agent would get. `result` is `null` while the job
    is still queued/running rather than a 404 — the caller (job's own
    status) is the honest "not ready yet" signal, not a hard error.
    """
    job = jobs.get(job_id)
    if job is None:
        raise HTTPException(status_code=404, detail=f"job not found: {job_id}")
    suffix = _RESULT_SUFFIX_BY_KIND.get(job.kind)
    if suffix is None:
        raise HTTPException(
            status_code=404,
            detail=f"job {job_id} has no result (kind={job.kind!r} does not produce one)",
        )
    result_path = job.log_path.parent / f"{job.job_id}.{suffix}.json"
    if not result_path.exists():
        return {"job": job.to_dict(), "result": None}
    try:
        result = json.loads(result_path.read_text())
    except (OSError, json.JSONDecodeError) as exc:
        raise HTTPException(status_code=500, detail=f"unreadable job result: {exc}") from exc
    return {"job": job.to_dict(), "result": result}


def cancel_job_response(
    jobs: JobController,
    job_id: str,
    *,
    on_cancel: Callable[[dict[str, Any]], None] | None = None,
) -> dict[str, Any]:
    job = jobs.cancel(job_id)
    if job is None:
        raise HTTPException(status_code=404, detail=f"job not found: {job_id}")
    record = job.to_dict()
    if on_cancel is not None:
        on_cancel(record)
    return {"job": record}


def training_runs_response(
    training_ledger_path: Path,
    *,
    on_run: Callable[[dict[str, Any]], None] | None = None,
) -> dict[str, Any]:
    ledger = TrainingLedger(training_ledger_path)
    runs = [r.to_dict() for r in ledger.records()]
    if on_run is not None:
        for run in runs:
            on_run(run)
    return {"runs": runs, "count": len(runs)}


def set_training_run_approval_response(
    training_ledger_path: Path,
    run_id: str,
    *,
    approved: bool,
    notes: str | None = None,
) -> dict[str, Any]:
    """Shared body of `POST /control/training-runs/{id}/approve` (6.36).

    The operator's "this run is good" decision, persisted in the same
    shared JSONL ledger as everything else about the run (compute target,
    metrics, artifacts) rather than a side table — so approval state travels
    with the run record wherever it's read (companion's local ledger, the
    Pi's, `scripts/ledger.py`).
    """
    ledger = TrainingLedger(training_ledger_path)
    record = ledger.set_approved(run_id, approved, notes=notes)
    if record is None:
        raise HTTPException(status_code=404, detail=f"training run not found: {run_id}")
    return {"run": record.to_dict()}


def feedback_list_response(feedback_log_path: Path) -> dict[str, Any]:
    """Shared body of `GET /control/feedback` — offline false-positive review
    (6.35) needs this to work identically against the companion's local
    feedback log as it does against the Pi's."""
    store = FeedbackStore(feedback_log_path)
    records = [fb.to_dict() for fb in store.labels().values()]
    return {"feedback": records, "count": len(records), "summary": store.summary()}


def submit_feedback_response(
    feedback_log_path: Path,
    *,
    event: dict[str, Any],
    label: str,
    reason: str | None = None,
    driving_context: dict[str, Any] | None = None,
    labeled_by: str | None = None,
    on_feedback: Callable[[dict[str, Any]], None] | None = None,
) -> dict[str, Any]:
    """Shared body of `POST /control/feedback` (6.35 offline false-positive
    labeling). ``on_feedback`` is control's seam for its audit-event publish
    (companion has no audit bus to publish to, so it passes none)."""
    if label not in FEEDBACK_LABELS:
        raise HTTPException(
            status_code=422, detail=f"label must be one of {sorted(FEEDBACK_LABELS)}"
        )
    store = FeedbackStore(feedback_log_path)
    fb = store.record(
        event=event,
        label=label,  # type: ignore[arg-type]  # checked above
        reason=reason,
        driving_context=driving_context or {},
        labeled_by=labeled_by,
    )
    record = fb.to_dict()
    if on_feedback is not None:
        on_feedback(record)
    return {"feedback": record}


def dataset_health_response(
    sessions: list[Path],
    config: CanaryConfig,
    *,
    corpus_boundary: date | None = None,
) -> dict[str, Any]:
    from src.datasets.health import analyze_corpus

    return analyze_corpus(sessions, config, corpus_boundary=corpus_boundary).to_dict()


__all__ = [
    "companion_config",
    "import_session_archive",
    "write_companion_config",
    "_artifact_path",
    "_base_url",
    "_dataset_row_to_dict",
    "_download_session_export",
    "_library_session",
    "_library_session_path",
    "_library_session_to_control_dict",
    "_make_job_controller",
    "_manifest_version",
    "_model_id",
    "_request_bytes",
    "_request_json",
    "_require_optional_bearer",
    "_unlink_path",
    "cancel_job_response",
    "dataset_health_response",
    "feedback_list_response",
    "get_job_response",
    "get_model_manifest_response",
    "job_logs_response",
    "list_jobs_response",
    "run_queued_action",
    "set_training_run_approval_response",
    "submit_feedback_response",
    "submit_job_response",
    "training_runs_response",
]
