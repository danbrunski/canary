"""E03.4 on the live tick: ``propose`` runs on the inference tick from a ranking
loaded off the tick, on the newest fresh readings the record path kept.

The drives and the ranking are ``tests/test_hypothesis_tracking.py``'s labelled
synthetic world (drive B logs fuel pressure through an enrichment pull); the
records are that session's rows, replayed into the observer with the wall
clock pinned to the data clock.
"""

from __future__ import annotations

import inspect
import shutil
import threading
from concurrent.futures import Executor, Future
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest

from src.analysis.hypothesis_tracking import rank_investigation
from src.analysis.measurement_plan import CaptureState
from src.daemon.inference_metrics import InferenceTimingRecorder
from src.daemon.proposals_live import (
    STAGE_REFRESH,
    STAGE_TICK,
    ConfigPlanner,
    LiveProposals,
    PlannerState,
)
from src.daemon.twin_live import _InlineExecutor
from src.telemetry import TelemetryRecord
from src.vehicle.profiles import VehicleProfile, scope_config
from tests.configs import isolated_config
from tests.test_hypothesis_tracking import BUDGET, PRESSURE, World, _rows
from tests.test_hypothesis_tracking import world as world  # noqa: F401 - fixture

NO_CAPTURE = CaptureState(active=False, profile="default")


class Clock:
    def __init__(self) -> None:
        self.now = 0.0

    def __call__(self) -> float:
        return self.now


class StaticPlanner:
    def __init__(self, state: PlannerState) -> None:
        self.state = state
        self.calls = 0

    def refresh(self, previous: PlannerState | None) -> PlannerState:
        self.calls += 1
        return self.state


def _state(world: World) -> PlannerState:
    """What :class:`ConfigPlanner` loads, built from the shared world."""
    investigation = world.opened
    record = world.ledger.entry(investigation).record
    ranking = rank_investigation(
        world.ledger,
        world.vehicle.model,
        world.plan,
        [*record.observations, *world.drive_a],  # type: ignore[union-attr]
    ).ranking
    return PlannerState(
        "proposing",
        plan=world.plan,
        plan_sha256="sha",
        model=world.vehicle.model,
        catalog=world.catalog,
        default_budget=BUDGET,
        investigation=investigation,
        ranking=ranking,
    )


def _record(t: float, spec: str, value: float) -> TelemetryRecord:
    source, _sep, channel = spec.partition(":")
    return TelemetryRecord(
        timestamp=datetime.fromtimestamp(t, tz=UTC),
        source=source,  # type: ignore[arg-type]
        channel=channel,
        value=value,
        metadata={"acquired_at": t},
    )


def _drive(
    live: LiveProposals,
    clock: Clock,
    rows: list[tuple[float, str, float]],
    *,
    capture: CaptureState = NO_CAPTURE,
    vehicle_state: str = "engine_running",
) -> list[dict[str, Any]]:
    """Every row through the observer, a tick each data second; the blocks."""
    blocks: list[dict[str, Any]] = []
    next_tick: float | None = None
    for t, spec, value in rows:
        if next_tick is None:
            next_tick = t
        while t > next_tick:
            clock.now = next_tick
            live.tick(vehicle_state=vehicle_state, capture=capture)
            blocks.append(live.snapshot())
            next_tick += 1.0
        live.observe(_record(t, spec, value))
    return blocks


def _live(world: World, **kw: Any) -> tuple[LiveProposals, Clock]:
    clock = Clock()
    live = LiveProposals(
        planner=StaticPlanner(_state(world)),
        recorder=kw.pop("recorder", None),
        executor=_InlineExecutor(),
        clock=clock,
        **kw,
    )
    return live, clock


def _pressure(block: dict[str, Any]) -> list[dict[str, Any]]:
    return [p for p in block["proposals"] if p["measurement"] == PRESSURE]


def test_the_tick_proposes_the_tuning_capture_only_inside_enrichment(world: World) -> None:
    live, clock = _live(world)
    blocks = _drive(live, clock, _rows(world.session_b))
    assert {b["result"] for b in blocks} == {"proposing"}
    kinds = [p["kind"] for b in blocks for p in _pressure(b)]
    assert "open_capture" in kinds
    opened = [p for b in blocks for p in _pressure(b) if p["kind"] == "open_capture"]
    assert {(p["profile"], p["region"], p["delivery"]) for p in opened} == {
        ("tuning", "enrichment", "brain")
    }
    deferred = {p["reason"] for b in blocks for p in _pressure(b) if p["kind"] == "defer"}
    assert "region_not_entered" in deferred
    # Each entry into the region is logged once, not once per tick.
    log = blocks[-1]["opened"]
    assert log and len(log) < len(opened)
    assert {e["profile"] for e in log} == {"tuning"}
    # Physical actions are after-drive review items; nothing reaches the driver.
    reviews = [p for p in blocks[-1]["proposals"] if p["kind"] == "review"]
    assert reviews and {p["delivery"] for p in reviews} == {"after_drive"}
    assert blocks[-1]["delivers_to_driver"] is False
    assert blocks[-1]["writes"].startswith("nothing")


def test_the_tick_never_displaces_an_operator_capture(world: World) -> None:
    live, clock = _live(world)
    operator = CaptureState(active=True, profile="fuel-pulsation", source="control")
    blocks = _drive(live, clock, _rows(world.session_b), capture=operator)
    proposals = [p for b in blocks for p in b["proposals"]]
    assert not [p for p in proposals if p["kind"] == "open_capture"]
    assert "operator_capture_active" in {
        p["reason"] for p in proposals if p["measurement"] == PRESSURE
    }
    assert blocks[-1]["capture"] == {
        "active": True,
        "profile": "fuel-pulsation",
        "source": "control",
    }
    assert blocks[-1]["opened"] == []


def test_no_capture_is_proposed_unless_the_engine_runs(world: World) -> None:
    live, clock = _live(world)
    for state in ("replay", "passive", "None"):
        blocks = _drive(live, clock, _rows(world.session_b), vehicle_state=state)
        pressure = [p for b in blocks for p in _pressure(b) if p["kind"] != "review"]
        assert pressure and {p["reason"] for p in pressure} == {f"vehicle_state:{state}"}
        assert not [p for b in blocks for p in b["proposals"] if p["kind"] == "open_capture"]


def test_stale_readings_defer_by_name(world: World) -> None:
    """The wall clock runs on past the last read: values age out, never stand
    for now."""
    live, clock = _live(world)
    rows = _rows(world.session_b)
    _drive(live, clock, rows)
    clock.now = rows[-1][0] + 30.0
    live.tick(vehicle_state="engine_running", capture=NO_CAPTURE)
    pressure = _pressure(live.snapshot())
    assert pressure and {p["kind"] for p in pressure} <= {"defer", "review"}
    assert any(p["reason"] in {"region_stale_input", "instrument_absent"} for p in pressure), (
        pressure
    )


class _Stalled(Executor):
    """Accepts work and never runs it: a worker stuck on a slow disk."""

    def __init__(self) -> None:
        self.submitted = 0

    def submit(self, fn: Any, /, *args: Any, **kwargs: Any) -> Future[Any]:
        self.submitted += 1
        return Future()


def test_a_stalled_worker_never_holds_the_tick() -> None:
    executor = _Stalled()
    clock = [0.0]
    live = LiveProposals(
        planner=StaticPlanner(PlannerState("proposing")),
        recorder=None,
        executor=executor,
        refresh_s=1.0,
        monotonic=lambda: clock[0],
    )
    for step in range(20):
        clock[0] = float(step)
        live.tick(vehicle_state="engine_running", capture=NO_CAPTURE)
    block = live.snapshot()
    assert executor.submitted == 1
    assert block["result"] == "starting" and block["busy_ticks"] == 19
    assert block["proposals"] == []


def test_refreshes_run_on_the_worker_at_the_refresh_interval(world: World) -> None:
    planner = StaticPlanner(_state(world))
    clock = [0.0]
    live = LiveProposals(
        planner=planner,
        recorder=None,
        executor=_InlineExecutor(),
        refresh_s=60.0,
        monotonic=lambda: clock[0],
    )
    for step in range(125):
        clock[0] = float(step)
        live.tick(vehicle_state="engine_off", capture=NO_CAPTURE)
    assert planner.calls == 3
    assert live.snapshot()["refreshes"] == 3


def test_the_observer_keeps_only_the_plans_channels(world: World) -> None:
    live, _clock = _live(world)
    live.tick(vehicle_state="engine_off", capture=NO_CAPTURE)
    watched = set(live.snapshot()["watched"])
    assert {"obd:MAF", "obd:FUEL_STATUS", "obd:ENGINE_LOAD", "obd:RPM"} <= watched
    assert "sensor:FUEL_PRESSURE" in watched
    live.observe(_record(1.0, "obd:COOLANT_TEMP", 90.0))
    live.observe(_record(1.0, "obd:maf", 12.0))  # Mode 01 names fold case
    live.observe(_record(1.0, "obd:RPM", "n/a"))  # type: ignore[arg-type]
    assert set(live._latest) == {"obd:MAF"}


def test_status_and_the_brain_route_carry_the_published_block(world: World, tmp_path: Path) -> None:
    """``/status`` ``detectors.inference.proposals`` and
    ``/control/brain/status`` ``live_proposals`` are one block; MCP passes the
    route through."""
    import asyncio

    from fastapi.testclient import TestClient

    from src.dashboard.api import create_app
    from src.mcp_server.tools import brain as brain_tool
    from tests.test_brain_routes import AUTH, TOKEN, _mcp_client

    recorder = InferenceTimingRecorder()
    live, clock = _live(world, recorder=recorder)
    _drive(live, clock, _rows(world.session_b)[:4000])
    timing = recorder.snapshot().to_dict()
    assert {STAGE_TICK, STAGE_REFRESH} <= set(timing["stages"])
    block = timing["proposals"]
    assert block == live.snapshot()
    config = scope_config(
        isolated_config(tmp_path, keep=("afr_calibration",)),
        VehicleProfile(slug="nissan-xterra-2008", vin="", name="Xterra"),
    )
    app = create_app(
        config, anomalies_dir=tmp_path / "an", control_token=TOKEN, inference_timing=recorder
    )
    response = TestClient(app).get("/control/brain/status", headers=AUTH)
    assert response.status_code == 200, response.text
    served = response.json()["live_proposals"]
    assert served == block
    via_mcp = asyncio.run(brain_tool.get_brain_status(_mcp_client(app)))
    assert via_mcp["live_proposals"] == served


def test_without_a_pipeline_the_route_says_so(tmp_path: Path) -> None:
    from fastapi.testclient import TestClient

    from src.dashboard.api import create_app
    from tests.test_brain_routes import AUTH, TOKEN

    config = scope_config(
        isolated_config(tmp_path, keep=("afr_calibration",)),
        VehicleProfile(slug="nissan-xterra-2008", vin="", name="Xterra"),
    )
    app = create_app(config, anomalies_dir=tmp_path / "an", control_token=TOKEN)
    served = TestClient(app).get("/control/brain/status", headers=AUTH).json()
    assert served["live_proposals"]["result"] == "not_running"
    assert served["live_proposals"]["proposals"] == []


# -- the real loader -----------------------------------------------------------------


def _scoped(tmp_path: Path) -> Any:
    return scope_config(
        isolated_config(tmp_path, keep=("afr_calibration", "capture_profiles")),
        VehicleProfile(slug="nissan-xterra-2008", vin="", name="Xterra"),
    )


def test_the_config_planner_ranks_the_open_investigation_from_the_ledger(
    world: World, tmp_path: Path
) -> None:
    config = _scoped(tmp_path)
    ledger_path = config.daemon.memory_ledger
    ledger_path.parent.mkdir(parents=True, exist_ok=True)
    shutil.copyfile(world.ledger.path, ledger_path)
    planner = ConfigPlanner(config)
    state = planner.refresh(None)
    assert state.result == "proposing", state.detail
    assert state.ranking is not None and state.ranking.method == "structural"
    assert state.ranking.order[0] == PRESSURE
    assert state.catalog is not None and state.catalog.by_name("tuning") is not None
    # An unchanged ledger is not re-ranked; the plan and model are not re-read.
    again = planner.refresh(state)
    assert again.ranked_at == state.ranked_at and again.ranking is state.ranking
    assert again.plan is state.plan


def test_the_config_planner_names_each_kind_of_nothing(tmp_path: Path) -> None:
    unscoped = isolated_config(tmp_path / "u")
    assert ConfigPlanner(unscoped).refresh(None).result == "unscoped"
    empty = ConfigPlanner(_scoped(tmp_path / "e")).refresh(None)
    assert empty.result == "no_open_investigation" and empty.ranking is None
    elsewhere = isolated_config(tmp_path / "n")
    bare = scope_config(elsewhere, VehicleProfile(slug="bare", vin="", name="Bare"))
    state = ConfigPlanner(bare).refresh(None)
    assert state.result == "not_declared" and "fault_hypotheses.yaml" in state.detail


def test_proposals_are_wired_into_the_inference_pipeline() -> None:
    from src.daemon import inference

    source = inspect.getsource(inference.LiveInferencePipeline)
    assert "build_live_proposals(components, source, recorder)" in source
    assert "self._proposals.tick_live(" in source
    assert "self._proposals.close()" in source


def test_the_tick_does_not_wait_for_a_slow_load(world: World) -> None:
    """A real worker thread blocked in its load: ticks return at once."""
    release = threading.Event()

    class Slow:
        def refresh(self, previous: PlannerState | None) -> PlannerState:
            release.wait(10.0)
            return _state(world)

    live = LiveProposals(planner=Slow(), recorder=None)
    try:
        import time

        started = time.perf_counter()
        for _ in range(5):
            live.tick(vehicle_state="engine_running", capture=NO_CAPTURE)
        assert time.perf_counter() - started < 0.5
        assert live.snapshot()["result"] == "starting"
    finally:
        release.set()
        live.close()


@pytest.mark.parametrize("bad", [float("nan"), float("inf")])
def test_a_non_finite_value_is_never_a_reading(world: World, bad: float) -> None:
    live, _clock = _live(world)
    live.tick(vehicle_state="engine_off", capture=NO_CAPTURE)
    live.observe(_record(1.0, "obd:MAF", bad))
    assert live._latest == {}
