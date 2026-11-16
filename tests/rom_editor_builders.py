"""The ROM editor's HTTP app and its in-process executor (R30.2).

Apart from ``tests/sim_builders.py`` because this one imports the dashboard routes: the
``imports nothing that talks to the ECU`` tests run a script that imports the simulator
builders and then inspect ``sys.modules``, and the routes pull ``can`` in.
"""

from __future__ import annotations

from concurrent.futures import Future, ThreadPoolExecutor
from typing import Any

from fastapi import FastAPI

from src.config import CanaryConfig
from src.dashboard.routes.rom_editor import register_rom_editor_routes
from src.dashboard.routes.rom_library import register_rom_library_routes
from tests.app_context import bare_context

AUTH = {"Authorization": "Bearer secret"}


OCTET = {**AUTH, "Content-Type": "application/octet-stream"}


def make_app(config: CanaryConfig) -> FastAPI:
    app = FastAPI()
    register_rom_library_routes(app, bare_context(config))
    register_rom_editor_routes(app, bare_context(config))
    return app


class CountingExecutor(ThreadPoolExecutor):
    """The in-process stand-in for the spawn child, counting what it was given."""

    def __init__(self) -> None:
        super().__init__(max_workers=1)
        self.submitted = 0

    def submit(self, fn: Any, /, *args: Any, **kwargs: Any) -> Future[Any]:
        self.submitted += 1
        return super().submit(fn, *args, **kwargs)
