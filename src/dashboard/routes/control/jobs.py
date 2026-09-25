"""Background jobs (``/control/jobs``).

Part of ``/control/*``; every route here requires the control token
(router-level, R03.6).
"""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter

from src.companion.services import (
    cancel_job_response,
    get_job_response,
    job_logs_response,
    job_result_response,
    list_jobs_response,
    submit_job_response,
)
from src.dashboard.routes.control.context import ControlContext
from src.dashboard.schemas import (
    JobSubmitRequest,
)


def build_router(ctx: ControlContext) -> APIRouter:
    router = ctx.router()
    _jobs = ctx.require_job_controller
    publish_control_event = ctx.publish_control_event

    @router.post("/control/jobs", status_code=201)
    def post_control_job(
        request: JobSubmitRequest,
    ) -> dict[str, Any]:
        return submit_job_response(
            _jobs(),
            request.kind,
            request.params,
            on_submit=lambda record: publish_control_event("job.submit", "ok", record),
        )

    @router.get("/control/jobs")
    def get_control_jobs() -> dict[str, Any]:
        return list_jobs_response(_jobs())

    @router.get("/control/jobs/{job_id}/logs")
    def get_control_job_logs(
        job_id: str,
    ) -> dict[str, Any]:
        return job_logs_response(_jobs(), job_id)

    @router.get("/control/jobs/{job_id}/result")
    def get_control_job_result(
        job_id: str,
    ) -> dict[str, Any]:
        return job_result_response(_jobs(), job_id)

    @router.get("/control/jobs/{job_id}")
    def get_control_job(
        job_id: str,
    ) -> dict[str, Any]:
        return get_job_response(_jobs(), job_id)

    @router.post("/control/jobs/{job_id}/cancel")
    def post_control_job_cancel(
        job_id: str,
    ) -> dict[str, Any]:
        return cancel_job_response(
            _jobs(),
            job_id,
            on_cancel=lambda record: publish_control_event("job.cancel", "ok", record),
        )

    return router
