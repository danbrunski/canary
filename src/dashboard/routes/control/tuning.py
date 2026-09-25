"""Alert-tuning configuration: read, update, estimate and revert.

Part of ``/control/*``; every route here requires the control token
(router-level, R03.6).
"""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, HTTPException, Query

from src.alerts.tuning import (
    AlertTuner,
    TuningConfig,
    estimate_before_after,
    load_tuning_version,
    save_tuning_config,
)
from src.dashboard.readers import read_anomalies_since
from src.dashboard.routes.control.context import ControlContext, _require
from src.dashboard.schemas import (
    TuningRevertRequest,
    TuningUpdateRequest,
)


def build_router(ctx: ControlContext) -> APIRouter:
    router = ctx.router()
    alert_tuner = ctx.alert_tuner
    anomalies_dir = ctx.anomalies_dir
    config = ctx.config
    publish_control_event = ctx.publish_control_event

    def _tuner() -> AlertTuner:
        return _require(alert_tuner, "tuning")

    def _ensure_baseline_audited(tuner: AlertTuner) -> None:
        """Snapshot the pre-change config so the first change is revertible.

        The in-memory default (version 1) is never written until the operator
        makes a change; without this, you couldn't undo your *first* tuning
        edit. Idempotent — only writes if that version isn't already in the
        audit log.
        """
        current = tuner.config
        if load_tuning_version(current.version, config.daemon.tuning_audit) is None:
            save_tuning_config(
                current,
                config.daemon.tuning_config,
                audit_path=config.daemon.tuning_audit,
                change="baseline snapshot",
                by="system",
            )

    @router.get("/control/tuning")
    def get_tuning() -> dict[str, Any]:
        return {"tuning": _tuner().config.to_dict()}

    @router.post("/control/tuning")
    def post_tuning(
        request: TuningUpdateRequest,
    ) -> dict[str, Any]:
        tuner = _tuner()
        try:
            proposed = TuningConfig.model_validate(request.config)
        except ValueError as exc:
            raise HTTPException(status_code=422, detail=f"invalid tuning config: {exc}") from exc
        _ensure_baseline_audited(tuner)
        # Monotonic version bump regardless of what the client sent — the
        # version is the audit handle, not client-controlled.
        proposed = proposed.model_copy(update={"version": tuner.config.version + 1})
        save_tuning_config(
            proposed,
            config.daemon.tuning_config,
            audit_path=config.daemon.tuning_audit,
            change=request.change,
            by=request.by,
        )
        # Mutate the live tuner in place so the change takes effect on the
        # very next inference tick — no daemon restart.
        tuner.config = proposed
        publish_control_event(
            "tuning.update", "ok", {"version": proposed.version, "change": request.change}
        )
        return {"tuning": proposed.to_dict()}

    @router.post("/control/tuning/estimate")
    def post_tuning_estimate(
        request: TuningUpdateRequest,
        since: str | None = Query(default=None),
    ) -> dict[str, Any]:
        """Before/after FP estimate of a proposed config over recent
        anomalies — run *before* committing (Risk R11)."""
        tuner = _tuner()
        try:
            proposed = TuningConfig.model_validate(request.config)
        except ValueError as exc:
            raise HTTPException(status_code=422, detail=f"invalid tuning config: {exc}") from exc
        try:
            events = read_anomalies_since(anomalies_dir, since=since)
        except (ValueError, TypeError) as exc:
            raise HTTPException(status_code=400, detail=f"bad `since`: {exc}") from exc
        return estimate_before_after(events, tuner.config, proposed)

    @router.post("/control/tuning/revert")
    def post_tuning_revert(
        request: TuningRevertRequest,
    ) -> dict[str, Any]:
        tuner = _tuner()
        snapshot = load_tuning_version(request.version, config.daemon.tuning_audit)
        if snapshot is None:
            raise HTTPException(
                status_code=404, detail=f"no tuning version {request.version} in audit log"
            )
        # Reverting is itself a new, audited version (never rewinds the log).
        reverted = snapshot.model_copy(update={"version": tuner.config.version + 1})
        save_tuning_config(
            reverted,
            config.daemon.tuning_config,
            audit_path=config.daemon.tuning_audit,
            change=f"revert to v{request.version}",
            by=request.by,
        )
        tuner.config = reverted
        publish_control_event(
            "tuning.revert", "ok", {"to_version": request.version, "new_version": reverted.version}
        )
        return {"tuning": reverted.to_dict()}

    return router
