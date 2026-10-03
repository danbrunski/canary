"""Model artifacts and manifests, training-run history, and dataset health.

Part of ``/control/*``; every route here requires the control token
(router-level, R03.6).
"""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, HTTPException

from src.control.models import publish_model_artifact
from src.dashboard.routes.control.context import ControlContext
from src.dashboard.routes.control.responses import (
    dataset_health_response,
    get_model_manifest_response,
    http_artifact_path,
    set_training_run_approval_response,
    training_runs_response,
)
from src.dashboard.schemas import (
    ModelUploadRequest,
    TrainingRunApprovalRequest,
)
from src.models.manifest import manifest_path_for
from src.preprocessing.loader import discover_sessions


def build_router(ctx: ControlContext) -> APIRouter:
    router = ctx.router()
    config = ctx.config
    publish_control_event = ctx.publish_control_event
    vehicle_ledger_store = ctx.vehicle_ledger_store

    @router.post("/control/models/{model_type}/artifact")
    def post_control_model_artifact(
        model_type: str,
        request: ModelUploadRequest,
    ) -> dict[str, Any]:
        try:
            published = publish_model_artifact(
                config,
                model_type=model_type,
                artifact_b64=request.artifact_b64,
                manifest=request.manifest,
            )
        except (ValueError, OSError) as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        publish_control_event("model.upload", "ok", published)
        return {"model": published}

    @router.get("/control/models/{model_type}/manifest")
    def get_control_model_manifest(
        model_type: str,
    ) -> dict[str, Any]:
        """6.37: the manifest for the artifact currently on this daemon's
        watched model path, so a publisher can build a compatibility report
        before uploading a candidate."""
        return get_model_manifest_response(
            model_type,
            resolve_manifest_path=lambda mt: manifest_path_for(http_artifact_path(config, mt)),
        )

    @router.get("/control/training-runs")
    def get_training_runs() -> dict[str, Any]:
        """Training-run history for the 6.22 dashboard (Task 6.21 ledger).

        Latest record per run (the append-only chain is collapsed); read
        per request so a fresh ``retrain.py`` run shows up without a daemon
        restart. Empty list when nothing's been recorded yet."""
        return training_runs_response(config.daemon.training_ledger)

    @router.post("/control/training-runs/{run_id}/approve")
    def approve_training_run(
        run_id: str,
        request: TrainingRunApprovalRequest,
    ) -> dict[str, Any]:
        """6.36 — persist the operator's approve/unapprove decision for a run."""
        return set_training_run_approval_response(
            config.daemon.training_ledger,
            run_id,
            approved=request.approved,
            notes=request.notes,
        )

    @router.get("/control/datasets/health")
    def get_dataset_health() -> dict[str, Any]:
        """Corpus readiness + per-session coverage (Task 6.23)."""
        storage = config.collection.storage_path
        sessions = discover_sessions(storage) if storage.exists() else []
        boundary_event = (
            vehicle_ledger_store.latest_boundary() if vehicle_ledger_store is not None else None
        )
        return dataset_health_response(
            sessions,
            config,
            corpus_boundary=boundary_event.occurred_at if boundary_event is not None else None,
        )

    return router
