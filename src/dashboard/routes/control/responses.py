"""Shared ``/control/*`` response bodies (R09.8).

Both the daemon's control API and the companion app register the same job,
training-run, model-manifest and dataset-health routes against different
backing stores; each body takes that difference as a parameter. They lived in
``src.companion.services``, which made the dashboard import the companion
while the companion imported the dashboard's session routers. Here, the
companion imports the dashboard and nothing imports back.
"""

from __future__ import annotations

import json
from collections.abc import Callable
from datetime import date
from pathlib import Path
from typing import Any

from fastapi import HTTPException

from src.config import CanaryConfig
from src.control.jobs import JobController
from src.control.models import artifact_path
from src.models.training_ledger import TrainingLedger


def http_artifact_path(config: CanaryConfig, model_type: str) -> Path:
    """:func:`src.control.models.artifact_path`, an unknown type as a 400."""
    try:
        return artifact_path(config, model_type)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


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


def dataset_health_response(
    sessions: list[Path],
    config: CanaryConfig,
    *,
    corpus_boundary: date | None = None,
) -> dict[str, Any]:
    from src.datasets.health import analyze_corpus

    return analyze_corpus(sessions, config, corpus_boundary=corpus_boundary).to_dict()


__all__ = [
    "http_artifact_path",
    "get_model_manifest_response",
    "submit_job_response",
    "list_jobs_response",
    "job_logs_response",
    "get_job_response",
    "job_result_response",
    "cancel_job_response",
    "training_runs_response",
    "set_training_run_approval_response",
    "dataset_health_response",
]
