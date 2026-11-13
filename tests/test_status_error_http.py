"""R29.3: the HTTP answer to a ``StatusError`` is the one ``HTTPException`` gives.

Eighteen route blocks used to read ``except <refusal> as exc: raise
HTTPException(exc.status, exc.detail)``; ``create_app`` now registers one
handler for ``StatusError`` and the blocks are gone. This file is the proof
that nothing a client sees moved, in two parts.

* :func:`test_each_refusal_route_answers_its_status_and_body` drives every route
  that had such a block (every one of the eighteen sites) with its callee made
  to refuse with a status of its own, and pins ``(status, body)`` literally. It
  was written and run against the tree *before* the blocks were deleted, so the
  literals are what the old blocks produced, not what the new handler does.
* The equivalence tests compare the handler's response with
  ``HTTPException``'s for the same ``(status, detail)``: body, status and
  headers.
"""

from __future__ import annotations

from collections.abc import Callable, Iterator
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest
from fastapi import FastAPI, HTTPException
from fastapi.testclient import TestClient

from src.collection import tuning_profile
from src.config import CanaryConfig
from src.control import brain_jobs, brain_reads, workbench_jobs
from src.control import trip_summary as trip_summary_module
from src.control.brain_jobs import BrainJobRefusedError
from src.control.brain_reads import BrainReadError
from src.control.profile_capture import ProfileCaptureError
from src.control.status_error import StatusError
from src.control.workbench_jobs import WorkbenchRefusedError
from src.dashboard.api import create_app
from src.dashboard.routes import analysis_reads as analysis_reads_routes
from src.dashboard.routes import workbench as workbench_routes
from src.dashboard.routes.control import brain as brain_routes
from src.dashboard.routes.control import classification as classification_routes
from src.dashboard.status_errors import register_status_error_handler
from src.paths import REPO_ROOT
from tests.configs import isolated_config

TOKEN = "secret"
AUTH = {"Authorization": f"Bearer {TOKEN}"}
SESSION = "2026-09-18T120000"


def _body(tag: str) -> dict[str, Any]:
    return {"error": "pinned", "message": f"the pinned refusal at {tag}"}


def _refuses(cls: Callable[[int, dict[str, Any]], Exception], status: int, tag: str) -> Any:
    def refuse(*args: Any, **kwargs: Any) -> Any:
        raise cls(status, _body(tag))

    return refuse


def _async_refuses(cls: Callable[[int, dict[str, Any]], Exception], status: int, tag: str) -> Any:
    async def refuse(*args: Any, **kwargs: Any) -> Any:
        raise cls(status, _body(tag))

    return refuse


def _selection() -> Any:
    return SimpleNamespace(usable=[Path("s")], over_budget=[], as_dict=lambda: {})


@pytest.fixture
def config(tmp_path: Path) -> CanaryConfig:
    config = isolated_config(tmp_path, "example")
    (config.collection.storage_path / SESSION).mkdir(parents=True)
    return config


@pytest.fixture
def client(
    config: CanaryConfig, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> Iterator[TestClient]:
    monkeypatch.setattr(tuning_profile, "is_active", lambda: False)
    with TestClient(create_app(config, anomalies_dir=tmp_path / "an", control_token=TOKEN)) as c:
        yield c


Patch = tuple[Any, str, Any]


def _wb(status: int, tag: str) -> Any:
    return _refuses(WorkbenchRefusedError, status, tag)


def _awb(status: int, tag: str) -> Any:
    return _async_refuses(WorkbenchRefusedError, status, tag)


def _no_op(*args: Any, **kwargs: Any) -> None:
    return None


#: ``(site, request, patches, expected status, expected tag)``. The tag names which
#: refusal the patched callee raised, so a response proves the right block answered.
CASES: list[tuple[str, tuple[str, str], list[Patch], int, str]] = [
    (
        "workbench.run (run_off_loop refuses)",
        ("GET", "/analysis/delivery-error?sessions=s"),
        [
            (workbench_routes, "select_sessions", lambda *a, **k: _selection()),
            (workbench_routes, "capture_deferral", _no_op),
            (workbench_routes, "run_off_loop", _awb(409, "run")),
        ],
        409,
        "run",
    ),
    (
        "workbench.pooled (capture_deferral refuses)",
        ("GET", "/analysis/delivery-error?sessions=s"),
        [(workbench_routes, "capture_deferral", _wb(418, "pooled"))],
        418,
        "pooled",
    ),
    (
        "workbench.frame (capture_deferral refuses)",
        ("GET", f"/control/sessions/{SESSION}/frame?specs=obd:RPM"),
        [(workbench_routes, "capture_deferral", _wb(419, "frame"))],
        419,
        "frame",
    ),
    (
        "workbench.cell_residency deferral",
        ("GET", f"/control/sessions/{SESSION}/cell-residency?table=fuel_targets"),
        [
            (
                workbench_routes,
                "load_viewer_definition",
                lambda *a, **k: SimpleNamespace(table=lambda name: SimpleNamespace(trace=None)),
            ),
            (workbench_routes, "capture_deferral", _wb(420, "residency-deferral")),
        ],
        420,
        "residency-deferral",
    ),
    (
        "workbench.cell_residency selection",
        ("GET", f"/control/sessions/{SESSION}/cell-residency?table=fuel_targets"),
        [
            (
                workbench_routes,
                "load_viewer_definition",
                lambda *a, **k: SimpleNamespace(table=lambda name: SimpleNamespace(trace=None)),
            ),
            (workbench_routes, "capture_deferral", _no_op),
            (workbench_routes, "mapping_refusal", lambda trace: None),
            (workbench_routes, "select_sessions", _wb(421, "residency-selection")),
        ],
        421,
        "residency-selection",
    ),
    (
        "workbench.dyno (dyno_preflight refuses)",
        ("GET", "/analysis/dyno?sessions=s"),
        [
            (workbench_routes, "capture_deferral", _no_op),
            (workbench_routes, "select_sessions", lambda *a, **k: _selection()),
            (workbench_routes, "dyno_preflight", _wb(422, "dyno")),
        ],
        422,
        "dyno",
    ),
    (
        "classification (capture_deferral refuses)",
        ("POST", f"/control/sessions/{SESSION}/classification"),
        [(classification_routes, "capture_deferral", _wb(423, "classify-deferral"))],
        423,
        "classify-deferral",
    ),
    (
        "classification (run_off_loop refuses)",
        ("POST", f"/control/sessions/{SESSION}/classification"),
        [
            (classification_routes, "capture_deferral", _no_op),
            (classification_routes, "select_sessions", lambda *a, **k: _selection()),
            (classification_routes, "run_off_loop", _awb(424, "classify-run")),
        ],
        424,
        "classify-run",
    ),
    (
        "public /analysis/binned (capture_deferral refuses)",
        ("GET", "/analysis/binned?value=obd:RPM&x=obd:RPM"),
        [(analysis_reads_routes, "capture_deferral", _wb(425, "binned"))],
        425,
        "binned",
    ),
    (
        "public trip-summary (trip_summary_off_loop refuses)",
        ("GET", f"/sessions/{SESSION}/trip-summary"),
        [(trip_summary_module, "trip_summary_off_loop", _awb(426, "trip-summary"))],
        426,
        "trip-summary",
    ),
    (
        "brain read",
        ("GET", "/control/brain/jobs"),
        [(brain_reads, "brain_jobs", _refuses(BrainReadError, 427, "brain-read"))],
        427,
        "brain-read",
    ),
    (
        "brain job submit",
        ("POST", "/control/brain/jobs"),
        [(brain_routes, "canonical_params", _refuses(BrainJobRefusedError, 428, "brain-job"))],
        428,
        "brain-job",
    ),
]


@pytest.mark.parametrize("case", CASES, ids=[c[0] for c in CASES])
def test_each_refusal_route_answers_its_status_and_body(
    client: TestClient, monkeypatch: pytest.MonkeyPatch, case: Any
) -> None:
    _site, (method, path), patches, status, tag = case
    for owner, name, replacement in patches:
        monkeypatch.setattr(owner, name, replacement)
    kwargs: dict[str, Any] = {"headers": AUTH}
    if method == "POST" and path.endswith("/brain/jobs"):
        kwargs["json"] = {"kind": "twin_session_evaluation", "params": {}}
    response = client.request(method, path, **kwargs)
    assert response.status_code == status, response.text
    assert response.json() == {"detail": _body(tag)}
    assert response.headers["content-type"] == "application/json"


# -- the real refusals, by the callee's own rules -------------------------------


def _recording(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(tuning_profile, "is_active", lambda: True)
    monkeypatch.setattr(
        tuning_profile, "active_profile", lambda: type("P", (), {"name": "tuning"})()
    )


def test_a_capture_defers_the_whole_session_routes_with_the_workbenchs_body(
    client: TestClient, config: CanaryConfig, monkeypatch: pytest.MonkeyPatch
) -> None:
    _recording(monkeypatch)
    expected = {
        "error": "analysis_deferred",
        "reason": "capture_active",
        "profile": "tuning",
        "message": "analysis is deferred while 'tuning' is recording",
        "laptop_path": workbench_jobs.laptop_path(config, [SESSION]),
    }
    for method, path in (
        ("GET", f"/control/sessions/{SESSION}/frame?specs=obd:RPM"),
        ("POST", f"/control/sessions/{SESSION}/classification"),
        ("GET", f"/sessions/{SESSION}/trip-summary"),
    ):
        response = client.request(method, path, headers=AUTH)
        assert response.status_code == 409, (path, response.text)
        assert response.json() == {"detail": expected}, path


def test_the_capture_session_routes_answer_their_refusals(client: TestClient) -> None:
    """The five ``ProfileCaptureError`` blocks; a string detail stays a string."""
    start = client.post("/control/capture", json={"profile": "fuel-map"}, headers=AUTH)
    assert (start.status_code, start.json()) == (
        503,
        {"detail": "capture storage writer is not available"},
    )
    pause = client.post("/control/capture/pause", json={"paused": True}, headers=AUTH)
    assert (pause.status_code, pause.json()) == (409, {"detail": "no capture is active"})
    quality = client.get("/control/capture/quality", headers=AUTH)
    assert (quality.status_code, quality.json()) == (
        503,
        {"detail": "capture quality is not available"},
    )
    stop = client.post("/control/capture/stop", headers=AUTH)
    assert (stop.status_code, stop.json()) == (
        503,
        {"detail": "capture storage writer is not available"},
    )
    arm = client.post(
        "/control/capture/autostart",
        json={"profile": "no-such-profile", "coolant_min_c": 60},
        headers=AUTH,
    )
    assert (arm.status_code, arm.json()) == (404, {"detail": "unknown profile: no-such-profile"})


def test_the_brain_routes_answer_their_real_refusals(client: TestClient) -> None:
    """Unscoped memory is the brain reads' 503; an unknown job kind is the submit's 400."""
    unscoped = client.get("/control/brain/jobs/nope", headers=AUTH)
    assert unscoped.status_code == 503
    assert set(unscoped.json()) == {"detail"}
    assert set(unscoped.json()["detail"]) == {"error", "message"}
    assert unscoped.json()["detail"]["error"] == "unscoped"
    unknown = client.post(
        "/control/brain/jobs", json={"kind": "no_such_kind", "params": {}}, headers=AUTH
    )
    assert (unknown.status_code, unknown.json()) == (
        400,
        {"detail": {"error": "unknown_kind", "message": "unknown brain job 'no_such_kind'"}},
    )


def test_the_refusal_modules_are_the_ones_the_routes_patch() -> None:
    """A guard on this file: the patched names exist where the routes read them."""
    assert brain_jobs.BrainJobRefusedError is BrainJobRefusedError
    assert brain_reads.BrainReadError is BrainReadError


# -- the handler against HTTPException ----------------------------------------


#: ``(status, detail)`` pairs a refusal carries today: dict bodies, a sentence,
#: and the statuses that take no body.
DETAILS: list[tuple[int, Any]] = [
    (409, {"error": "analysis_deferred", "message": "deferred", "laptop_path": "ssh x"}),
    (404, {"error": "unknown_job", "message": "no brain job 'x'"}),
    (503, {"error": "unscoped", "message": "scope the config"}),
    (400, {"error": "unknown_kind", "message": "nested", "extra": {"a": [1, 2, {"b": None}]}}),
    (409, "no capture is active"),
    (500, "a capture failed to stop"),
    (418, {"message": "a status of its own"}),
    (204, "no body is allowed"),
    (304, {"message": "no body is allowed"}),
]


def _toy_app() -> FastAPI:
    app = FastAPI()
    register_status_error_handler(app)

    @app.get("/status-error/{index}")
    def status_error(index: int) -> None:
        status, detail = DETAILS[index]
        raise ProfileCaptureError(status, detail)

    @app.get("/http-exception/{index}")
    def http_exception(index: int) -> None:
        status, detail = DETAILS[index]
        raise HTTPException(status_code=status, detail=detail)

    @app.get("/async-status-error")
    async def async_status_error() -> None:
        raise WorkbenchRefusedError(409, {"error": "x", "message": "from an async route"})

    return app


@pytest.mark.parametrize(
    "index", range(len(DETAILS)), ids=[f"{s}-{type(d).__name__}" for s, d in DETAILS]
)
def test_the_handler_answers_what_an_http_exception_answers(index: int) -> None:
    client = TestClient(_toy_app())
    refusal = client.get(f"/status-error/{index}")
    expected = client.get(f"/http-exception/{index}")
    assert refusal.status_code == expected.status_code == DETAILS[index][0]
    assert refusal.content == expected.content
    assert refusal.headers == expected.headers


def test_an_async_route_answers_its_refusal_too() -> None:
    response = TestClient(_toy_app()).get("/async-status-error")
    assert (response.status_code, response.json()) == (
        409,
        {"detail": {"error": "x", "message": "from an async route"}},
    )


def test_a_route_that_never_caught_the_refusal_answers_it_instead_of_500() -> None:
    """The defect R29.3 closes: without the app's handler this route's refusal is a 500."""

    def app(*, handled: bool) -> FastAPI:
        bare = FastAPI()

        @bare.get("/forgot")
        def forgot() -> None:
            raise BrainReadError(404, {"error": "unknown_job", "message": "no brain job 'x'"})

        if handled:
            register_status_error_handler(bare)
        return bare

    unhandled = TestClient(app(handled=False), raise_server_exceptions=False)
    assert unhandled.get("/forgot").status_code == 500
    response = TestClient(app(handled=True)).get("/forgot")
    assert (response.status_code, response.json()) == (
        404,
        {"detail": {"error": "unknown_job", "message": "no brain job 'x'"}},
    )


def test_create_app_registers_the_handler_for_every_subclass(
    config: CanaryConfig, tmp_path: Path
) -> None:
    app = create_app(config, anomalies_dir=tmp_path / "an", control_token=TOKEN)
    assert StatusError in app.exception_handlers
    for cls in (BrainJobRefusedError, BrainReadError, ProfileCaptureError, WorkbenchRefusedError):
        assert issubclass(cls, StatusError)


# -- a registrar must not depend on its caller for the handler ------------------

ROUTES = REPO_ROOT / "src" / "dashboard" / "routes"
#: What raises a ``StatusError`` for a route to answer.
RAISERS = (
    "capture_deferral",
    "run_off_loop",
    "select_sessions",
    "dyno_preflight",
    "trip_summary_off_loop",
    "WorkbenchRefusedError",
    "BrainJobRefusedError",
    "BrainReadError",
    "brain_reads",
    "BrainJobStore",
    "ProfileCaptureSession",
    "ProfileCaptureError",
    "set_tuning_mode",
    "TuningStorageSwitchError",
)


def test_every_route_module_that_can_raise_a_refusal_installs_the_handler() -> None:
    """Eight tests built a bare ``FastAPI()`` with one registrar and saw a refusal
    escape as an unhandled error once the per-route ``except`` blocks were gone. A
    registrar that mounts a route able to raise one installs the handler itself
    (idempotent), so no app builder has to remember it. ``control/`` is one
    registrar (``control/__init__.py``) over many routers."""
    for path in sorted(ROUTES.rglob("*.py")):
        text = path.read_text(encoding="utf-8")
        if not any(name in text for name in RAISERS):
            continue
        owner = ROUTES / "control" / "__init__.py" if path.parent.name == "control" else path
        assert "register_status_error_handler(app)" in owner.read_text(encoding="utf-8"), (
            f"{path.relative_to(ROUTES)} can raise a StatusError but "
            f"{owner.relative_to(ROUTES)} does not install the handler"
        )


def test_a_bare_app_with_the_workbench_routes_answers_a_refusal(
    config: CanaryConfig, monkeypatch: pytest.MonkeyPatch
) -> None:
    app = FastAPI()
    workbench_routes.register_workbench_routes(
        app,
        config=config,
        bindings_path=Path("data/bindings.yaml"),
        dids_path=Path("data/dids.yaml"),
        resolved_control_token=TOKEN,
    )
    monkeypatch.setattr(workbench_routes, "capture_deferral", _wb(409, "bare-app"))
    response = TestClient(app).get("/analysis/trim-surface?sessions=s", headers=AUTH)
    assert (response.status_code, response.json()) == (409, {"detail": _body("bare-app")})
