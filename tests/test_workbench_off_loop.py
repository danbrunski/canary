"""R29.8: the routes that read a session's files do it off the event loop.

``get_session_frame``, ``get_cell_residency``, ``post_session_classification``
and ``post_replay_start`` are ``async def``; each used to call
``resolve_session`` / ``select_sessions`` (which opens every parquet chunk
footer) or ``derive_vehicle_state_from_storage`` straight on the loop, so one
slow disk stalled every other request. The rule: plain file work in an
``async def`` handler goes through ``asyncio.to_thread``
(:func:`~src.control.workbench_jobs.prepare_single_session_job`, or the call
itself for the replay guard); a whole-session read goes to the bounded child.

Each test makes the file read slow (a ``time.sleep``, which blocks whatever
thread it runs on) and keeps a ticker coroutine running on the same loop. A
read on the loop freezes the ticker for the whole sleep.
"""

from __future__ import annotations

import asyncio
import threading
import time
from collections.abc import Callable
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import httpx
import pytest

from src.alerts.event_logger import AnomalyEventLogger
from src.collection import tuning_profile
from src.config import CanaryConfig
from src.control import replay_control, workbench_jobs
from src.dashboard.api import create_app
from src.dashboard.routes import workbench as workbench_routes
from src.dashboard.routes.control import replay as replay_routes
from src.dashboard.status import VehicleState
from src.storage.broadcast_storage import UnfaithfulReplayError
from src.storage.parquet_writer import ParquetTelemetryWriter
from src.telemetry import TelemetryRecord
from tests.configs import isolated_config

TOKEN = "secret"
AUTH = {"Authorization": f"Bearer {TOKEN}"}
SESSION = "2026-09-18T120000"
SLOW = 0.4
#: A ticker that slept 10 ms and woke more than this late was held by the loop's
#: other work. Half the blocking read, far above scheduler noise.
MAX_GAP = SLOW / 2


@pytest.fixture
def config(tmp_path: Path) -> CanaryConfig:
    config = isolated_config(tmp_path, "example")
    (config.collection.storage_path / SESSION).mkdir(parents=True)
    return config


class _Replay:
    """The slice of ``ReplayController`` the start route touches before the guard."""

    def status(self) -> Any:
        return SimpleNamespace(running=False, session_id=None)


async def _longest_stall(request: Callable[[httpx.AsyncClient], Any], app: Any) -> float:
    """Run ``request`` against ``app`` while a ticker measures the loop's stalls."""
    worst = 0.0
    stop = asyncio.Event()

    async def tick() -> None:
        nonlocal worst
        while not stop.is_set():
            before = time.perf_counter()
            await asyncio.sleep(0.01)
            worst = max(worst, time.perf_counter() - before - 0.01)

    async with httpx.AsyncClient(
        base_url="http://daemon", headers=AUTH, transport=httpx.ASGITransport(app=app)
    ) as client:
        ticker = asyncio.create_task(tick())
        await asyncio.sleep(0.05)
        await request(client)
        stop.set()
        await ticker
    return worst


def _app(config: CanaryConfig, tmp_path: Path, **kwargs: Any) -> Any:
    return create_app(config, anomalies_dir=tmp_path / "an", control_token=TOKEN, **kwargs)


def _slow(then: Callable[..., Any]) -> Callable[..., Any]:
    def slow(*args: Any, **kwargs: Any) -> Any:
        time.sleep(SLOW)
        return then(*args, **kwargs)

    return slow


@pytest.fixture(autouse=True)
def no_capture(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(tuning_profile, "is_active", lambda: False)


def _refusing_selection(*args: Any, **kwargs: Any) -> Any:
    """What ``select_sessions`` answers for an over-budget session, after the sleep."""
    over = SimpleNamespace(reason="over the row budget")
    return SimpleNamespace(usable=[], over_budget=[over], as_dict=lambda: {})


async def test_the_frame_route_reads_the_session_off_the_loop(
    config: CanaryConfig, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(workbench_jobs, "select_sessions", _slow(_refusing_selection))
    responses: list[httpx.Response] = []

    async def request(client: httpx.AsyncClient) -> None:
        responses.append(
            await client.get(f"/control/sessions/{SESSION}/frame", params={"specs": "obd:RPM"})
        )

    stall = await _longest_stall(request, _app(config, tmp_path))
    assert responses[0].status_code == 409
    assert responses[0].json()["detail"]["reason"] == "over_budget"
    assert stall < MAX_GAP, f"the loop stalled {stall:.2f}s inside the frame route"


async def test_the_cell_residency_route_reads_the_session_off_the_loop(
    config: CanaryConfig, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(
        workbench_routes,
        "load_viewer_definition",
        _slow(lambda *a, **k: SimpleNamespace(table=lambda name: SimpleNamespace(trace=None))),
    )
    monkeypatch.setattr(workbench_routes, "mapping_refusal", lambda trace: None)
    monkeypatch.setattr(workbench_jobs, "select_sessions", _slow(_refusing_selection))
    responses: list[httpx.Response] = []

    async def request(client: httpx.AsyncClient) -> None:
        responses.append(
            await client.get(
                f"/control/sessions/{SESSION}/cell-residency", params={"table": "fuel_targets"}
            )
        )

    stall = await _longest_stall(request, _app(config, tmp_path))
    assert responses[0].status_code == 409
    assert stall < MAX_GAP, f"the loop stalled {stall:.2f}s inside the cell-residency route"


async def test_the_classification_route_reads_the_session_off_the_loop(
    config: CanaryConfig, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(workbench_jobs, "select_sessions", _slow(_refusing_selection))
    responses: list[httpx.Response] = []

    async def request(client: httpx.AsyncClient) -> None:
        responses.append(await client.post(f"/control/sessions/{SESSION}/classification"))

    stall = await _longest_stall(request, _app(config, tmp_path))
    assert responses[0].status_code == 409
    assert stall < MAX_GAP, f"the loop stalled {stall:.2f}s inside the classification route"


async def test_the_replay_start_guard_reads_the_state_off_the_loop(
    config: CanaryConfig, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    running = VehicleState(state="engine_running")
    monkeypatch.setattr(
        replay_routes, "derive_vehicle_state_from_storage", _slow(lambda *a, **k: running)
    )
    responses: list[httpx.Response] = []

    async def request(client: httpx.AsyncClient) -> None:
        responses.append(await client.post("/control/replay", json={"session_id": SESSION}))

    app = _app(config, tmp_path, replay_controller=_Replay())
    stall = await _longest_stall(request, app)
    assert responses[0].status_code == 409
    assert "engine_running" in responses[0].json()["detail"]
    assert stall < MAX_GAP, f"the loop stalled {stall:.2f}s inside the replay start route"


async def test_replay_start_reads_the_source_session_off_the_loop(
    config: CanaryConfig, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """``ReplayController._start_locked`` checks the source directory, its storage
    metadata (``refuse_unfaithful_replay``) and its capture manifest; those reads
    are off the loop, and the unfaithful refusal still answers 422."""

    def unfaithful(*args: Any, **kwargs: Any) -> None:
        raise UnfaithfulReplayError("the pinned unfaithful replay")

    monkeypatch.setattr(replay_control, "refuse_unfaithful_replay", _slow(unfaithful))
    monkeypatch.setattr(
        replay_routes,
        "derive_vehicle_state_from_storage",
        lambda *a, **k: VehicleState(state="ignition_only"),
    )
    controller = replay_control.ReplayController(
        queue=asyncio.Queue[TelemetryRecord](),
        writer=ParquetTelemetryWriter(config.collection.storage_path),
        anomaly_logger=AnomalyEventLogger(tmp_path / "anomalies"),
        storage_root=config.collection.storage_path,
        broadcast_stale_timeout_seconds=10.0,
    )
    responses: list[httpx.Response] = []

    async def request(client: httpx.AsyncClient) -> None:
        responses.append(await client.post("/control/replay", json={"session_id": SESSION}))

    stall = await _longest_stall(request, _app(config, tmp_path, replay_controller=controller))
    assert responses[0].status_code == 422
    assert responses[0].json()["detail"] == "the pinned unfaithful replay"
    assert stall < MAX_GAP, f"the loop stalled {stall:.2f}s reading the replay source"


async def test_prepare_single_session_job_runs_its_reads_on_a_worker_thread(
    config: CanaryConfig, monkeypatch: pytest.MonkeyPatch
) -> None:
    seen: list[int] = []

    def selection(*args: Any, **kwargs: Any) -> Any:
        seen.append(threading.get_ident())
        return SimpleNamespace(usable=[Path("s")], over_budget=[], as_dict=lambda: {})

    monkeypatch.setattr(workbench_jobs, "select_sessions", selection)
    prepared = await workbench_jobs.prepare_single_session_job(config, SESSION)
    assert prepared.session.name == SESSION and prepared.answer is None
    assert seen and seen[0] != threading.get_ident()


async def test_prepare_single_session_job_asks_the_profile_before_the_budget(
    config: CanaryConfig, monkeypatch: pytest.MonkeyPatch
) -> None:
    def never(*args: Any, **kwargs: Any) -> Any:
        raise AssertionError("the budget is read after a profile refusal")

    monkeypatch.setattr(workbench_jobs, "select_sessions", never)
    prepared = await workbench_jobs.prepare_single_session_job(
        config, SESSION, profile_refusal=lambda session: {"available": False}
    )
    assert prepared.answer == {"available": False}


async def test_run_off_loop_defers_while_a_capture_records_without_the_route_asking(
    config: CanaryConfig, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(tuning_profile, "is_active", lambda: True)
    monkeypatch.setattr(
        tuning_profile, "active_profile", lambda: type("P", (), {"name": "tuning"})()
    )
    with pytest.raises(workbench_jobs.WorkbenchRefusedError) as refused:
        await workbench_jobs.run_off_loop(
            "classify", {"session_id": SESSION}, config, sessions=[SESSION]
        )
    assert refused.value.status == 409
    assert refused.value.detail["reason"] == "capture_active"
    assert refused.value.detail["laptop_path"] == workbench_jobs.laptop_path(config, [SESSION])
