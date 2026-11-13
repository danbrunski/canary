"""One handler for every :class:`~src.control.status_error.StatusError` (R29.3).

A control-layer refusal carries the status code and body its route answers with.
Routes used to re-raise each one as ``HTTPException(exc.status, exc.detail)`` in
an ``except`` block of their own; a route that forgot the block answered 500
instead of the named refusal. :func:`register_status_error_handler` makes the
translation the app's, once.

The response is built by FastAPI's own ``http_exception_handler`` from an
``HTTPException`` carrying the same status and detail, so the body
(``{"detail": ...}``), the status, any headers, and the no-body statuses
(204, 304) are what an ``HTTPException`` raised in the route gives, by
construction rather than by a copy of its rules.
"""

from __future__ import annotations

from fastapi import FastAPI, HTTPException, Request
from fastapi.exception_handlers import http_exception_handler
from fastapi.responses import Response

from src.control.status_error import StatusError


async def status_error_handler(request: Request, exc: Exception) -> Response:
    """Answer a ``StatusError`` as the ``HTTPException`` it stands for."""
    if not isinstance(exc, StatusError):  # pragma: no cover - registered for StatusError only
        raise exc
    return await http_exception_handler(
        request, HTTPException(status_code=exc.status, detail=exc.detail)
    )


def register_status_error_handler(app: FastAPI) -> None:
    app.add_exception_handler(StatusError, status_error_handler)


__all__ = ["register_status_error_handler", "status_error_handler"]
