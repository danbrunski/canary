"""Route registration for the companion API."""

from __future__ import annotations

import base64
import binascii
import json
import zipfile
from collections.abc import Callable
from pathlib import Path
from typing import Any

from fastapi import BackgroundTasks, Depends, FastAPI, Header, HTTPException, Query
from fastapi.responses import FileResponse, Response

from src.alerts.feedback import FeedbackStore, annotate_events_with_feedback
from src.companion.library import CanaryLibrary, LibrarySession
from src.companion.schemas import (
    ImportSessionRequest,
    JobSubmitRequest,
    ModelUploadRequest,
    PublishModelRequest,
    SessionMetadataRequest,
    SessionPinRequest,
    SyncSessionRequest,
    TrainingRunApprovalRequest,
)
from src.companion.services import (
    _artifact_path,
    _base_url,
    _dataset_row_to_dict,
    _download_session_export,
    _library_session,
    _library_session_path,
    _library_session_to_control_dict,
    _manifest_version,
    _model_id,
    _require_optional_bearer,
    cancel_job_response,
    dataset_health_response,
    delete_session_response,
    export_session_checksum_response,
    export_session_response,
    feedback_list_response,
    get_job_response,
    get_model_manifest_response,
    get_session_response,
    job_logs_response,
    list_jobs_response,
    pin_session_response,
    run_queued_action,
    sessions_list_response,
    set_training_run_approval_response,
    submit_feedback_response,
    submit_job_response,
    training_runs_response,
    unpin_session_response,
    update_session_metadata_response,
)
from src.config import CanaryConfig
from src.control.jobs import JobController
from src.control.models import MODEL_TYPES, publish_model_artifact
from src.dashboard.readers import (
    read_anomalies_since,
    read_latest_per_channel,
    read_latest_telemetry_rows,
)
from src.dashboard.schemas import FeedbackRequest
from src.models.manifest import compatibility_report, manifest_path_for


def register_companion_routes(
    app: FastAPI,
    *,
    lib: CanaryLibrary,
    local_config: CanaryConfig,
    jobs: JobController,
    config_path: Path,
    pi_host: str | None,
    pi_token: str | None,
    resolved_control_token: str | None,
    import_session_archive: Callable[..., LibrarySession],
    _request_json: Callable[..., dict[str, Any]],
    _request_bytes: Callable[..., bytes],
    _download_session_export: Callable[..., bytes] = _download_session_export,
) -> None:
    def require_local_auth(authorization: str | None = Header(default=None)) -> None:
        _require_optional_bearer(resolved_control_token, authorization)

    def default_pi_host(host: str | None) -> str:
        selected = host or pi_host
        if not selected:
            raise HTTPException(status_code=400, detail="pi_host is required")
        return _base_url(selected)

    def default_pi_token(token: str | None) -> str | None:
        return token or pi_token

    @app.get("/companion/status")
    def companion_status() -> dict[str, Any]:
        sessions = lib.list_sessions()
        return {
            "mode": "local_library",
            "library_root": str(lib.root),
            "sessions": len(sessions),
            "models": len(lib.list_model_artifacts()),
            "datasets": len(lib.list_datasets()),
            "queued_actions": len(lib.list_actions(status="queued")),
            "config_path": str(config_path),
        }

    @app.post("/companion/sessions/import", status_code=201)
    def import_session_archive_endpoint(
        request: ImportSessionRequest,
        _auth: None = Depends(require_local_auth),
    ) -> dict[str, Any]:
        try:
            archive = base64.b64decode(request.archive_b64, validate=True)
            session = import_session_archive(
                archive,
                lib,
                config=local_config if request.index_with_config else None,
                source_host=request.source_host,
            )
        except (binascii.Error, ValueError, OSError, zipfile.BadZipFile) as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        return {"session": _library_session_to_control_dict(session)}

    @app.post("/companion/sync/sessions/{session_id}", status_code=201)
    def sync_session_from_pi(
        session_id: str,
        request: SyncSessionRequest,
        _auth: None = Depends(require_local_auth),
    ) -> dict[str, Any]:
        host = default_pi_host(request.pi_host)
        token = default_pi_token(request.token)
        if request.pin:
            _request_json(
                host,
                "POST",
                f"/control/sessions/{session_id}/pin",
                token=token,
                body={"reason": request.pin_reason, "metadata": {}},
            )

        def _do_sync() -> dict[str, Any]:
            expected_sha256 = None
            if request.verify_checksum:
                checksum = _request_json(
                    host,
                    "GET",
                    f"/control/sessions/{session_id}/export/checksum",
                    token=token,
                )
                expected_sha256 = checksum.get("sha256")
            archive = _download_session_export(
                host,
                session_id,
                token=token,
                dest_dir=lib.sync_dir,
                expected_sha256=expected_sha256,
            )
            session = import_session_archive(
                archive,
                lib,
                config=local_config if request.index_with_config else None,
                source_host=host,
            )
            return {
                "session": _library_session_to_control_dict(session),
                "sha256": expected_sha256,
            }

        # 6.34: queued/retried like model publish (`run_queued_action`) — a
        # dropped mid-transfer connection leaves the partial download on
        # disk (`_download_session_export`'s `.part` file) and a `failed`
        # row in `/companion/actions`; calling this same endpoint again
        # resumes instead of restarting from byte 0.
        try:
            return run_queued_action(
                lib,
                "sync_session",
                {"session_id": session_id, "host": host},
                _do_sync,
            )
        except (ValueError, OSError, zipfile.BadZipFile, RuntimeError) as exc:
            raise HTTPException(status_code=502, detail=str(exc)) from exc

    def _refresh_and_serialize(path: Path) -> dict[str, Any]:
        # Companion's "library index refresh": re-import re-reads the just
        # written pin/metadata state off disk into the sqlite-backed index
        # before serializing, which control's stateless summarize doesn't do.
        refreshed = lib.import_session(path, config=local_config, copy=False)
        return _library_session_to_control_dict(refreshed)

    @app.get("/control/sessions")
    def list_local_sessions(_auth: None = Depends(require_local_auth)) -> dict[str, Any]:
        return sessions_list_response(
            [_library_session_to_control_dict(s) for s in lib.list_sessions()]
        )

    @app.get("/control/sessions/{session_id}/export")
    def export_local_session(
        session_id: str,
        background_tasks: BackgroundTasks,
        _auth: None = Depends(require_local_auth),
    ) -> FileResponse:
        return export_session_response(
            session_id,
            background_tasks,
            resolve=lambda sid: _library_session_path(lib, sid),
            tmp_prefix="canary-local-",
        )

    @app.get("/control/sessions/{session_id}/export/checksum")
    def export_local_session_checksum(
        session_id: str,
        _auth: None = Depends(require_local_auth),
    ) -> dict[str, Any]:
        """6.34: the server-computed sha256 companion's own sync checks its
        download against — registered locally too so this Pi-side endpoint
        has a parity fixture to test against without a real Pi."""
        return export_session_checksum_response(
            session_id, resolve=lambda sid: _library_session_path(lib, sid)
        )

    @app.get("/control/sessions/{session_id}")
    def get_local_session(
        session_id: str,
        _auth: None = Depends(require_local_auth),
    ) -> dict[str, Any]:
        return get_session_response(
            session_id,
            resolve_and_serialize=lambda sid: _library_session_to_control_dict(
                _library_session(lib, sid)
            ),
        )

    @app.post("/control/sessions/{session_id}/metadata")
    def update_local_session_metadata(
        session_id: str,
        request: SessionMetadataRequest,
        _auth: None = Depends(require_local_auth),
    ) -> dict[str, Any]:
        return update_session_metadata_response(
            session_id,
            request.metadata,
            merge=request.merge,
            resolve=lambda sid: _library_session_path(lib, sid),
            finalize=_refresh_and_serialize,
        )

    @app.post("/control/sessions/{session_id}/pin")
    def pin_local_session(
        session_id: str,
        request: SessionPinRequest,
        _auth: None = Depends(require_local_auth),
    ) -> dict[str, Any]:
        return pin_session_response(
            session_id,
            reason=request.reason,
            extra_metadata=request.metadata,
            resolve=lambda sid: _library_session_path(lib, sid),
            serialize=_refresh_and_serialize,
        )

    @app.post("/control/sessions/{session_id}/unpin")
    def unpin_local_session(
        session_id: str,
        _auth: None = Depends(require_local_auth),
    ) -> dict[str, Any]:
        return unpin_session_response(
            session_id,
            resolve=lambda sid: _library_session_path(lib, sid),
            serialize=_refresh_and_serialize,
        )

    @app.delete("/control/sessions/{session_id}")
    def delete_local_session(
        session_id: str,
        force: bool = Query(default=False),
        _auth: None = Depends(require_local_auth),
    ) -> dict[str, Any]:
        return delete_session_response(
            session_id,
            force=force,
            resolve=lambda sid: _library_session_path(lib, sid),
            after_delete=lambda sid: lib.remove_session(sid, delete_files=False),
        )

    @app.get("/control/sessions/{session_id}/gps.gpx")
    def get_local_session_gps_gpx(
        session_id: str,
        _auth: None = Depends(require_local_auth),
    ) -> Response:
        """Task 5.6a: GPX export of a synced session's captured GPS track."""
        from src.analysis.gps_export import read_gps_track, to_gpx
        from src.control.sessions import resolve_session

        try:
            session_dir = resolve_session(lib.sessions_dir, session_id)
        except FileNotFoundError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc
        track = read_gps_track(session_dir)
        return Response(
            content=to_gpx(track, name=session_id),
            media_type="application/gpx+xml",
            headers={"Content-Disposition": f'attachment; filename="{session_id}.gpx"'},
        )

    @app.get("/control/sessions/{session_id}/gps.kml")
    def get_local_session_gps_kml(
        session_id: str,
        _auth: None = Depends(require_local_auth),
    ) -> Response:
        """Task 5.6a: KML export of a synced session's captured GPS track."""
        from src.analysis.gps_export import read_gps_track, to_kml
        from src.control.sessions import resolve_session

        try:
            session_dir = resolve_session(lib.sessions_dir, session_id)
        except FileNotFoundError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc
        track = read_gps_track(session_dir)
        return Response(
            content=to_kml(track, name=session_id),
            media_type="application/vnd.google-earth.kml+xml",
            headers={"Content-Disposition": f'attachment; filename="{session_id}.kml"'},
        )

    @app.get("/control/sessions/{session_id}/chart")
    def get_local_session_chart(
        session_id: str,
        channels: str = Query(..., description="Comma-separated source:channel keys"),
        resample_hz: float | None = Query(default=None, gt=0),
        _auth: None = Depends(require_local_auth),
    ) -> dict[str, Any]:
        # Task 5.3: the "chart" this module's own docstring
        # (src/companion/library.py: "browse / chart / train / publish")
        # promised but never had an endpoint for — per-channel time-series
        # arrays for a synced session, same shape as the daemon's
        # /control/sessions/{id}/chart, just rooted at the local library.
        from src.control.session_timeseries import session_time_series_response

        channel_list = [c.strip() for c in channels.split(",") if c.strip()]
        try:
            return session_time_series_response(
                lib.sessions_dir,
                session_id,
                channel_list,
                resample_hz=resample_hz,
            )
        except FileNotFoundError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc

    @app.get("/telemetry/latest")
    def get_local_telemetry_latest(
        n: int = Query(default=100, ge=1, le=10_000),
        per_channel: bool = Query(default=False),
    ) -> dict[str, Any]:
        rows = (
            read_latest_per_channel(local_config.collection.storage_path)
            if per_channel
            else read_latest_telemetry_rows(local_config.collection.storage_path, n=n)
        )
        return {"rows": rows, "count": len(rows)}

    @app.get("/anomalies")
    def get_local_anomalies(since: str | None = Query(default=None)) -> dict[str, Any]:
        try:
            events = read_anomalies_since(local_config.daemon.anomalies_dir, since=since)
        except (ValueError, TypeError) as exc:
            raise HTTPException(status_code=400, detail=f"bad `since`: {exc}") from exc
        events = annotate_events_with_feedback(
            events,
            FeedbackStore(local_config.daemon.feedback_log),
        )
        return {"events": events, "count": len(events)}

    @app.get("/control/feedback")
    def get_local_feedback(_auth: None = Depends(require_local_auth)) -> dict[str, Any]:
        return feedback_list_response(local_config.daemon.feedback_log)

    @app.post("/control/feedback", status_code=201)
    def submit_local_feedback(
        request: FeedbackRequest,
        _auth: None = Depends(require_local_auth),
    ) -> dict[str, Any]:
        """6.35 — offline false-positive labeling: the same verdict flow the
        live dashboard's AnomalyPanel already posts, now answered locally so
        it works without the Pi."""
        return submit_feedback_response(
            local_config.daemon.feedback_log,
            event=request.event,
            label=request.label,
            reason=request.reason,
            driving_context=request.driving_context,
            labeled_by=request.labeled_by,
        )

    @app.post("/control/jobs", status_code=201)
    def start_local_job(
        request: JobSubmitRequest,
        _auth: None = Depends(require_local_auth),
    ) -> dict[str, Any]:
        return submit_job_response(jobs, request.kind, request.params)

    @app.get("/control/jobs")
    def list_local_jobs(_auth: None = Depends(require_local_auth)) -> dict[str, Any]:
        return list_jobs_response(jobs)

    @app.get("/control/jobs/{job_id}/logs")
    def local_job_logs(
        job_id: str,
        _auth: None = Depends(require_local_auth),
    ) -> dict[str, Any]:
        return job_logs_response(jobs, job_id)

    @app.get("/control/jobs/{job_id}")
    def get_local_job(
        job_id: str,
        _auth: None = Depends(require_local_auth),
    ) -> dict[str, Any]:
        return get_job_response(jobs, job_id)

    @app.post("/control/jobs/{job_id}/cancel")
    def cancel_local_job(
        job_id: str,
        _auth: None = Depends(require_local_auth),
    ) -> dict[str, Any]:
        return cancel_job_response(jobs, job_id)

    @app.get("/control/training-runs")
    def local_training_runs(_auth: None = Depends(require_local_auth)) -> dict[str, Any]:
        return training_runs_response(
            local_config.daemon.training_ledger,
            on_run=lambda run: lib.record_training_run(
                run, compute_target=run.get("compute_target") or "laptop"
            ),
        )

    @app.post("/control/training-runs/{run_id}/approve")
    def approve_local_training_run(
        run_id: str,
        request: TrainingRunApprovalRequest,
        _auth: None = Depends(require_local_auth),
    ) -> dict[str, Any]:
        return set_training_run_approval_response(
            local_config.daemon.training_ledger,
            run_id,
            approved=request.approved,
            notes=request.notes,
        )

    @app.get("/control/datasets")
    def local_datasets(_auth: None = Depends(require_local_auth)) -> dict[str, Any]:
        datasets = [_dataset_row_to_dict(row) for row in lib.list_datasets()]
        return {"datasets": datasets, "count": len(datasets)}

    @app.get("/control/datasets/health")
    def local_dataset_health(_auth: None = Depends(require_local_auth)) -> dict[str, Any]:
        sessions = [
            Path(session.path) for session in lib.list_sessions() if Path(session.path).is_dir()
        ]
        return dataset_health_response(sessions, local_config)

    @app.get("/companion/models")
    def companion_models(_auth: None = Depends(require_local_auth)) -> dict[str, Any]:
        models = lib.list_model_artifacts()
        return {"models": models, "count": len(models)}

    @app.post("/control/models/{model_type}/artifact")
    def upload_local_model_artifact(
        model_type: str,
        request: ModelUploadRequest,
        _auth: None = Depends(require_local_auth),
    ) -> dict[str, Any]:
        try:
            published = publish_model_artifact(
                local_config,
                model_type=model_type,
                artifact_b64=request.artifact_b64,
                manifest=request.manifest,
            )
        except (ValueError, OSError) as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        artifact = Path(published["artifact_path"])
        lib.add_model_artifact(
            _model_id(model_type, request.manifest),
            model_type=model_type,
            artifact_path=artifact,
            manifest=request.manifest,
            version=_manifest_version(request.manifest),
            copy=False,
        )
        return {"model": published}

    @app.get("/control/models/{model_type}/manifest")
    def get_local_model_manifest(
        model_type: str,
        _auth: None = Depends(require_local_auth),
    ) -> dict[str, Any]:
        """6.37: the manifest for whatever's currently active for
        ``model_type``, so a publisher (companion or another Pi) can build a
        compatibility report before uploading. Registered here too (not
        Pi-only) so the same endpoint has a local fixture to test against."""
        return get_model_manifest_response(
            model_type,
            resolve_manifest_path=lambda mt: manifest_path_for(_artifact_path(local_config, mt)),
        )

    @app.post("/companion/models/{model_type}/publish")
    def publish_model_to_pi(
        model_type: str,
        request: PublishModelRequest,
        _auth: None = Depends(require_local_auth),
    ) -> dict[str, Any]:
        if model_type not in MODEL_TYPES:
            raise HTTPException(status_code=400, detail=f"unsupported model type: {model_type}")
        host = default_pi_host(request.pi_host)
        token = default_pi_token(request.token)
        artifact_path = (
            Path(request.artifact_path)
            if request.artifact_path
            else _artifact_path(local_config, model_type)
        )
        manifest_path = (
            Path(request.manifest_path)
            if request.manifest_path
            else manifest_path_for(artifact_path)
        )

        def _do_publish() -> dict[str, Any]:
            manifest = json.loads(manifest_path.read_text())
            # 6.37: compatibility report against whatever the target already
            # has active *before* pushing bytes — a schema mismatch is
            # visible to the operator up front instead of surfacing only as
            # a live rejection on the Pi.
            try:
                running = _request_json(
                    host, "GET", f"/control/models/{model_type}/manifest", token=token
                )
                running_manifest = running.get("manifest")
            except RuntimeError:
                running_manifest = None  # target unreachable/no manifest yet
            report = compatibility_report(
                manifest if isinstance(manifest, dict) else {}, running_manifest
            )
            artifact_b64 = base64.b64encode(artifact_path.read_bytes()).decode("ascii")
            result = _request_json(
                host,
                "POST",
                f"/control/models/{model_type}/artifact",
                token=token,
                body={"artifact_b64": artifact_b64, "manifest": manifest},
            )
            model_id = request.model_id or _model_id(model_type, manifest)
            lib.add_model_artifact(
                model_id,
                model_type=model_type,
                artifact_path=artifact_path,
                manifest=manifest if isinstance(manifest, dict) else {},
                version=_manifest_version(manifest if isinstance(manifest, dict) else {}),
                copy=False,
            )
            # Accepted/active confirmation: the Pi only round-trips a manifest
            # once `publish_model_artifact` wrote it, and `/status`'s
            # `active_model_version` only reflects it once the model manager
            # has actually hot-swapped in. Neither is per-model-type today
            # (one daemon-wide "last swap"), so this is the best available
            # signal, not a per-type guarantee.
            confirmed_manifest = _request_json(
                host, "GET", f"/control/models/{model_type}/manifest", token=token
            ).get("manifest", {})
            accepted = isinstance(confirmed_manifest, dict) and confirmed_manifest.get(
                "model_version"
            ) == manifest.get("model_version")
            status = "accepted" if accepted else "uploaded"
            try:
                daemon_status = _request_json(host, "GET", "/status", token=token)
            except RuntimeError:
                daemon_status = {}
            active_version = (daemon_status.get("health") or {}).get("active_model_version")
            if accepted and active_version == manifest.get("model_version"):
                status = "active"
            lib.record_publish(model_id, target=host, status=status, detail=json.dumps(result))
            return {
                "model_id": model_id,
                "target": host,
                "result": result,
                "status": status,
                "compatibility": report,
            }

        # Queue/retry semantics (6.37): re-posting the same publish request
        # after a failure (network drop, Pi rebooting) just re-reads the
        # still-on-disk local artifact and retries the upload — nothing here
        # is one-shot/consumed on failure.
        try:
            publish = run_queued_action(
                lib,
                "publish_model",
                {"model_type": model_type, "host": host},
                _do_publish,
            )
        except (OSError, json.JSONDecodeError, RuntimeError) as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        return {"publish": publish}

    @app.get("/companion/actions")
    def queued_actions(_auth: None = Depends(require_local_auth)) -> dict[str, Any]:
        actions = lib.list_actions()
        return {"actions": actions, "count": len(actions)}

    @app.post("/companion/actions/{action_id}/retry")
    def retry_queued_action(
        action_id: int,
        _auth: None = Depends(require_local_auth),
    ) -> dict[str, Any]:
        """Re-run a `failed` `queued_actions` row (6.34/6.37 retry semantics).

        Re-dispatches through the *same* route handlers used for the
        original request — `sync_session_from_pi`/`publish_model_to_pi` are
        themselves idempotent-on-retry (resumable download, re-read local
        artifact), so this just re-supplies the original params rather than
        reimplementing the retry.
        """
        action = lib.get_action(action_id)
        if action is None:
            raise HTTPException(status_code=404, detail=f"action not found: {action_id}")
        params = json.loads(action["params"] or "{}")
        kind = action["kind"]
        if kind == "sync_session":
            return sync_session_from_pi(
                params["session_id"], SyncSessionRequest(pi_host=params.get("host"))
            )
        if kind == "publish_model":
            return publish_model_to_pi(
                params["model_type"], PublishModelRequest(pi_host=params.get("host"))
            )
        raise HTTPException(status_code=400, detail=f"action kind not retryable: {kind}")

    @app.get("/companion/sync/state")
    def companion_sync_state(_auth: None = Depends(require_local_auth)) -> dict[str, Any]:
        sessions = lib.list_sessions()
        actions = lib.list_actions()
        queued = [action for action in actions if action.get("status") == "queued"]
        return {
            "source_hosts": sorted({s.source_host for s in sessions if s.source_host}),
            "queued_actions": queued,
            "actions": actions,
            "publish_history": lib.publish_history(),
        }


__all__ = ["register_companion_routes"]
