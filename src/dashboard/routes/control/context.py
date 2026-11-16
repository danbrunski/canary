"""What the ``/control/*`` routers share beyond the app context (R03.6, R29.6).

Each domain module exposes ``build_router(ctx) -> APIRouter`` over the one
:class:`~src.dashboard.app_context.AppContext` (``create_app`` builds it, every
route module reads it). This module keeps the routers' logger and the
session-directory dependency that owns the ``resolve_session`` → 404 mapping.
"""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Annotated

from fastapi import Depends, Request

from src.dashboard.app_context import AppContext

#: The routes' logger keeps the name it had as one module, so log filters and
#: journal greps written against it still match.
log = logging.getLogger("src.dashboard.routes.control")


def _session_dir(request: Request, session_id: str) -> Path:
    context: AppContext = request.app.state.app_context
    return context.resolve_session(session_id)


#: A route parameter that resolves ``{session_id}`` to its directory, or 404s.
SessionDir = Annotated[Path, Depends(_session_dir)]


__all__ = ["SessionDir", "log"]
