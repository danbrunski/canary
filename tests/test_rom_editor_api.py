"""W09.2: the ROM Editor's routes -- preview an edit, simulate it.

Modelled on ``tests/test_rom_library_api.py``: a library in ``tmp_path``, the
synthetic image of E07.6 uploaded through the library's own route. The
simulator needs a profile whose application model describes that image and a
``tune_revisions.yaml`` that records it as a flashed revision, so the tests
copy the Xterra profile into ``tmp_path`` and state both there (the real
profile is read-only input and the real image is not needed).

The child is the workbench's ``run_off_loop`` job ``rom_sim``. Most tests give
it an in-process executor (the ``executor=`` seam); two run a real ``spawn``
child, the only place the pickling trap (refusals that do not survive it) can
show up.
"""

from __future__ import annotations

import functools
import json
import shutil
from collections.abc import Callable, Iterator
from concurrent.futures import Future, ThreadPoolExecutor
from pathlib import Path
from typing import Any

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from src.analysis import rom_editor
from src.analysis.ecu_application import program_sha256
from src.analysis.paired_scenarios import SimKit
from src.analysis.rom_image import ImageFormatError, revision_match, sha256_hex
from src.config import CanaryConfig
from src.control import workbench_jobs
from src.dashboard.routes import rom_editor as rom_editor_routes
from src.dashboard.routes.rom_editor import register_rom_editor_routes
from src.dashboard.routes.rom_library import register_rom_library_routes
from src.vehicle.profiles import operational_dir
from src.vehicle.tune_revisions import load_tune_revisions
from tests import repo_guard
from tests.configs import isolated_config
from tests.rom_fixtures import PROFILE
from tests.test_rom_editor import FC_EDIT
from tests.test_rom_editor import request as sim_request
from tests.test_sim_montecarlo import build_kit_and_image

AUTH = {"Authorization": "Bearer secret"}
OCTET = {**AUTH, "Content-Type": "application/octet-stream"}
REVISION = "synthetic-w09"
REVISIONS_YAML = """\
schema_version: 1
revisions:
  - revision: {revision}
    calibration_id: 1ZS44A00000
    flashed_at: 2026-10-01T00:00:00Z
    image_sha256: {sha}
"""


# -- the synthetic vehicle ----------------------------------------------------------------------


@pytest.fixture(scope="module")
def kit_and_image() -> tuple[SimKit, bytes]:
    return build_kit_and_image()


@pytest.fixture(scope="module")
def profile(tmp_path_factory: pytest.TempPathFactory, kit_and_image: tuple[SimKit, bytes]) -> Path:
    """The Xterra profile, with its application model describing the synthetic
    image's program and its flash history recording the image as a revision."""
    kit, image = kit_and_image
    out = tmp_path_factory.mktemp("profile") / "xterra"
    shutil.copytree(PROFILE, out, ignore=shutil.ignore_patterns("_candidates", "_prior"))
    model = out / "ecu_application_model.yaml"
    real = PROFILE / "ecu_application_model.yaml"
    old = kit.model.program.identity.sha256
    real_sha = next(
        line.split(":", 1)[1].strip()
        for line in real.read_text().splitlines()
        if line.strip().startswith("sha256:")
    )
    assert program_sha256(image, kit.model) == old and old != real_sha
    model.write_text(real.read_text().replace(real_sha, old, 1))
    (out / "tune_revisions.yaml").write_text(
        REVISIONS_YAML.format(revision=REVISION, sha=sha256_hex(image))
    )
    return out


@pytest.fixture
def config(tmp_path: Path, profile: Path) -> CanaryConfig:
    return isolated_config(tmp_path, daemon={"afr_calibration": profile / "afr_calibration.yaml"})


def make_app(config: CanaryConfig) -> FastAPI:
    app = FastAPI()
    register_rom_library_routes(app, config=config, resolved_control_token="secret")
    register_rom_editor_routes(app, config=config, resolved_control_token="secret")
    return app


@pytest.fixture
def client(config: CanaryConfig) -> Iterator[TestClient]:
    with TestClient(make_app(config)) as test_client:
        yield test_client


class CountingExecutor(ThreadPoolExecutor):
    """The in-process stand-in for the spawn child, counting what it was given."""

    def __init__(self) -> None:
        super().__init__(max_workers=1)
        self.submitted = 0

    def submit(self, fn: Any, /, *args: Any, **kwargs: Any) -> Future[Any]:
        self.submitted += 1
        return super().submit(fn, *args, **kwargs)


@pytest.fixture
def in_process(monkeypatch: pytest.MonkeyPatch) -> Iterator[CountingExecutor]:
    """``run_off_loop`` with the ``executor=`` seam filled, as the route calls it.

    The address-space ceiling the real child puts on itself is not applied to
    the pytest process."""
    pool = CountingExecutor()
    real = workbench_jobs.run_off_loop
    monkeypatch.setattr(rom_editor_routes, "run_off_loop", functools.partial(real, executor=pool))
    monkeypatch.setattr(workbench_jobs, "limit_address_space", lambda nbytes: (nbytes, nbytes))
    yield pool
    pool.shutdown(wait=True)


def upload(client: TestClient, data: bytes, filename: str = "image.bin") -> str:
    response = client.post(
        "/control/rom/library", params={"filename": filename}, content=data, headers=OCTET
    )
    assert response.status_code == 200, response.text
    sha: str = response.json()["sha256"]
    assert sha == sha256_hex(data)
    return sha


@pytest.fixture
def sha(client: TestClient, kit_and_image: tuple[SimKit, bytes]) -> str:
    """The synthetic image in the library: a recorded revision."""
    _, image = kit_and_image
    sha = upload(client, image, "synthetic.bin")
    entry = client.get(f"/control/rom/library/{sha}", headers=AUTH).json()["entry"]
    assert entry["revision_match"]["status"] == "matched"
    assert entry["revision_match"]["revision"] == REVISION
    return sha


@pytest.fixture
def unrecorded(client: TestClient, kit_and_image: tuple[SimKit, bytes]) -> str:
    """The same program with one calibration byte changed: in no revision."""
    kit, image = kit_and_image
    changed = bytearray(image)
    changed[kit.rom_map.table_address(kit.rom_map.table("k_constant"), 1) + 1] ^= 1
    sha = upload(client, bytes(changed), "tuner-base.bin")
    entry = client.get(f"/control/rom/library/{sha}", headers=AUTH).json()["entry"]
    assert entry["revision_match"]["status"] == "unrecognised"
    return sha


def preview_url(sha: str) -> str:
    return f"/control/rom/library/{sha}/edits/preview"


def simulate_url(sha: str) -> str:
    return f"/control/rom/library/{sha}/simulate"


def preview_body(*edits: dict[str, Any], label: str = "light load 2 % richer") -> dict[str, Any]:
    return {"label": label, "edits": list(edits)}


def detail(response: Any) -> dict[str, Any]:
    body = response.json()["detail"]
    assert isinstance(body, dict), body
    return body


# -- the token ----------------------------------------------------------------------------------


def test_both_routes_require_the_token(client: TestClient, sha: str) -> None:
    for url, body in (
        (preview_url(sha), preview_body(FC_EDIT)),
        (simulate_url(sha), sim_request()),
    ):
        assert client.post(url, json=body).status_code == 401, url
        wrong = client.post(url, json=body, headers={"Authorization": "Bearer nope"})
        assert wrong.status_code == 401, url
        # The token wins over field validation (the dependency raises first) ...
        assert client.post(url).status_code == 401, url
        assert client.post(url, json={"bogus": 1}).status_code == 401, url
        # ... but FastAPI parses the body before it runs dependencies, so a body
        # that is not JSON at all is a 422 even without a token.
        not_json = client.post(
            url, content=b"not json", headers={"Content-Type": "application/json"}
        )
        assert not_json.status_code == 422, url


def test_the_editor_registers_only_after_the_library(config: CanaryConfig) -> None:
    with pytest.raises(RuntimeError, match="register_rom_library_routes first"):
        register_rom_editor_routes(FastAPI(), config=config, resolved_control_token="secret")


# -- typical values (W09.11) --------------------------------------------------------------------

TYPICAL = "/control/rom/editor/typical-values"


def test_typical_values_need_the_token(client: TestClient) -> None:
    assert client.get(TYPICAL).status_code == 401
    assert client.get(TYPICAL, headers={"Authorization": "Bearer nope"}).status_code == 401


def test_typical_values_are_the_profiles_declared_values_with_their_sources(
    client: TestClient, profile: Path, config: CanaryConfig
) -> None:
    """No library entry is needed: they describe the vehicle, not an image. The
    route serves the loader's body whole and writes nothing."""
    before = sorted(p.name for p in config.daemon.rom_library_dir.parent.rglob("*"))
    response = client.get(TYPICAL, headers=AUTH)
    assert response.status_code == 200, response.text
    body = response.json()
    assert body == rom_editor.typical_values(profile)
    assert set(body) == {"profile", "file", "file_sha256", "values", "left_empty"}
    assert body["values"] and all(v["source"].strip() for v in body["values"].values())
    assert set(body["values"]) | set(body["left_empty"]) == set(rom_editor.TYPICAL_FIELDS)
    assert sorted(p.name for p in config.daemon.rom_library_dir.parent.rglob("*")) == before


def _client_for(tmp_path: Path, profile: Path, text: str | None) -> TestClient:
    """The app over a copy of the profile with this typical-values file (None: absent)."""
    copy_of = tmp_path / "vehicle"
    shutil.copytree(profile, copy_of)
    target = copy_of / rom_editor.TYPICAL_VALUES_FILE
    if text is None:
        target.unlink()
    else:
        target.write_text(text)
    config = isolated_config(tmp_path, daemon={"afr_calibration": copy_of / "afr_calibration.yaml"})
    return TestClient(make_app(config))


def test_a_vehicle_that_declares_no_typical_values_is_a_409_by_name(
    tmp_path: Path, profile: Path
) -> None:
    with _client_for(tmp_path, profile, None) as client:
        response = client.get(TYPICAL, headers=AUTH)
    assert response.status_code == 409, response.text
    assert detail(response)["error"] == "typical_values_undeclared"
    assert "sim_typical_values.yaml" in detail(response)["message"]


def test_a_broken_typical_values_file_is_a_409_by_name_not_a_500(
    tmp_path: Path, profile: Path
) -> None:
    unsourced = (profile / rom_editor.TYPICAL_VALUES_FILE).read_text().replace("source:", "src:", 1)
    with _client_for(tmp_path, profile, unsourced) as client:
        response = client.get(TYPICAL, headers=AUTH)
    assert response.status_code == 409, response.text
    assert detail(response)["error"] == "typical_values_invalid"
    assert "values.baro_kpa.source" in detail(response)["message"]


# -- preview ------------------------------------------------------------------------------------


def test_preview_is_the_pure_function_and_adds_nothing_of_its_own(
    client: TestClient, sha: str, kit_and_image: tuple[SimKit, bytes], profile: Path
) -> None:
    _, image = kit_and_image
    response = client.post(preview_url(sha), json=preview_body(FC_EDIT), headers=AUTH)
    assert response.status_code == 200, response.text
    body = response.json()
    direct = rom_editor.preview_edits(
        image,
        revision_match(sha, load_tune_revisions(profile / "tune_revisions.yaml")),
        "light load 2 % richer",
        [FC_EDIT],
        profile,
        sha256=sha,
    )
    assert body == json.loads(json.dumps(direct))
    # Cells old -> new, the reach, the YAML to keep and what the base is.
    assert len(body["provenance"]["cells"]) == 20
    assert {(c["old"], c["new"]) for c in body["provenance"]["cells"]} == {(128, 131)}
    assert body["provenance"]["effect"]["changed_nodes"] == ["fuel_comp_lookup"]
    assert body["spec"]["base"] == {"revision": REVISION, "file": None, "sha256": None}
    assert body["spec_yaml"].startswith("# CANary ROM Editor edit spec")
    assert body["spec_sha256"] and body["candidate_sha256"] != sha
    # E09.14b: which lever, and what blocks a number, in the classifier's own words.
    levers = body["levers"]
    assert levers["schema_version"] == 1
    assert [e["lever"] for e in levers["levers"]] == ["fuel_compensation"]
    assert levers["messages"] == direct["levers"]["messages"] and levers["messages"]
    assert any("closed loop: trim authority undecoded" in m for m in levers["messages"])
    (table,) = body["tables"]
    assert (table["id"], table["slot"]) == ("fuel_compensation", 1)
    assert body["sim_base"] == {
        "ok": True,
        "status": "matched",
        "revision": REVISION,
        "reason": None,
    }


def test_preview_of_an_unrecorded_base_is_a_file_spec_that_cannot_simulate(
    client: TestClient, unrecorded: str
) -> None:
    response = client.post(preview_url(unrecorded), json=preview_body(FC_EDIT), headers=AUTH)
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["spec"]["base"] == {
        "revision": None,
        "file": f"{unrecorded}.bin",
        "sha256": unrecorded,
    }
    assert body["sim_base"]["ok"] is False
    assert body["sim_base"]["reason"] == "base_not_a_recorded_revision"


@pytest.mark.parametrize(
    ("edit", "reason", "kind"),
    [
        (
            {"table": "timing_trim_a", "slot": 1, "all": True, "add_raw": 1},
            "table_not_editable",
            "edit_spec",
        ),
        (
            {"table": "fuel_targets_maps_2_5", "slot": 2, "all": True, "add_raw": 1},
            "slot_not_simulated",
            "edit_spec",
        ),
        (
            {"table": "fuel_compensation", "slot": 1, "cells": [[2, 3]], "set_raw": 128},
            "no_op_edit",
            "edit_spec",
        ),
        (
            {"table": "fuel_compensation", "slot": 1, "all": True, "scale": 1.02},
            "rounding_undeclared",
            "edit_spec",
        ),
        # E08.1's range check is make_candidate's, a plain RomEffectRefusalError:
        # the route catches the base class, not only EditSpecRefusalError.
        (
            {"table": "fuel_compensation", "slot": 1, "cells": [[2, 3]], "set_raw": 300},
            "value_out_of_range",
            "rom_effect",
        ),
    ],
)
def test_preview_refusals_are_a_400_with_their_reason(
    client: TestClient, sha: str, edit: dict[str, Any], reason: str, kind: str
) -> None:
    response = client.post(preview_url(sha), json=preview_body(edit), headers=AUTH)
    assert response.status_code == 400, response.text
    body = detail(response)
    assert body["error"] == "edit_refused"
    assert (body["reason"], body["kind"]) == (reason, kind)
    assert body["message"] and not body["message"].startswith(f"{reason}:")
    assert set(body) == {"error", "kind", "reason", "message"}


def test_preview_refuses_duplicates_and_an_empty_list_by_name(client: TestClient, sha: str) -> None:
    twice = [
        {"table": "fuel_compensation", "slot": 1, "cells": [[2, 3]], "add_raw": 1},
        {"table": "fuel_compensation", "slot": 1, "cells": [[2, 3]], "add_raw": 2},
    ]
    for edits, reason in ((twice, "duplicate_edit"), ([], "no_edits")):
        response = client.post(preview_url(sha), json={"label": "x", "edits": edits}, headers=AUTH)
        assert response.status_code == 400, response.text
        assert detail(response)["reason"] == reason


@pytest.mark.parametrize(
    ("cls", "kind"), rom_editor.REFUSALS, ids=[k for _, k in rom_editor.REFUSALS]
)
def test_preview_answers_every_refusal_class_as_a_400_not_a_500(
    client: TestClient,
    sha: str,
    monkeypatch: pytest.MonkeyPatch,
    cls: type[ValueError],
    kind: str,
) -> None:
    """The classes take (reason, message) -- except ImageFormatError, a plain
    ValueError with only a message -- and a route that caught only some of them
    answered the rest with a 500. The table is rom_editor.REFUSALS itself."""

    def boom(*args: Any, **kwargs: Any) -> dict[str, Any]:
        raise cls("some message") if cls is ImageFormatError else cls("some_reason", "some message")

    monkeypatch.setattr(rom_editor, "preview_edits", boom)
    response = client.post(preview_url(sha), json=preview_body(FC_EDIT), headers=AUTH)
    assert response.status_code == 400, response.text
    body = detail(response)
    assert body["error"] == "edit_refused" and body["kind"] == kind
    assert body["reason"] == ("image_format" if cls is ImageFormatError else "some_reason")
    assert body["message"] == "some message"
    assert set(body) == {"error", "kind", "reason", "message"}


def test_preview_of_another_program_is_refused_by_name_not_a_500(
    client: TestClient,
) -> None:
    """An image the application model does not describe (a stock calibration,
    another program) is an edit refusal with E08's reason, not a server error."""
    from tests.rom_fixtures import synthetic_image

    other = upload(client, synthetic_image(seed=3), "other.bin")
    response = client.post(preview_url(other), json=preview_body(FC_EDIT), headers=AUTH)
    assert response.status_code == 400, response.text
    assert detail(response)["error"] == "edit_refused"
    assert detail(response)["reason"] in {"wrong_program", "patch_missing"}


def test_preview_body_is_validated_by_pydantic(client: TestClient, sha: str) -> None:
    for body in (
        {"edits": [FC_EDIT]},  # no label
        {"label": "x"},  # no edits
        {"label": "x", "edits": "all of them"},
        {"label": "x", "edits": [FC_EDIT], "slot": 2},  # a field the page does not send
    ):
        assert client.post(preview_url(sha), json=body, headers=AUTH).status_code == 422, body


@pytest.mark.parametrize("url", [preview_url, simulate_url])
def test_an_unknown_or_malformed_sha_is_404(client: TestClient, url: Any) -> None:
    body = preview_body(FC_EDIT) if url is preview_url else sim_request()
    for bad in ("a" * 64, "ABC", "0" * 63, "..%2F..%2Fetc"):
        response = client.post(url(bad), json=body, headers=AUTH)
        assert response.status_code == 404, (bad, response.text)


@pytest.mark.parametrize("url", [preview_url, simulate_url])
def test_a_vehicle_with_no_map_is_409_as_the_tables_route_is(
    tmp_path: Path, profile: Path, kit_and_image: tuple[SimKit, bytes], url: Any
) -> None:
    """The library cannot add an image without a map, so the entry is written
    as the library stores it, under a profile that has none."""
    _, image = kit_and_image
    bare = tmp_path / "bare-profile"
    bare.mkdir()
    shutil.copy(profile / "afr_calibration.yaml", bare / "afr_calibration.yaml")
    config = isolated_config(tmp_path, daemon={"afr_calibration": bare / "afr_calibration.yaml"})
    library = config.daemon.rom_library_dir
    library.mkdir(parents=True)
    digest = sha256_hex(image)
    (library / f"{digest}.bin").write_bytes(image)
    (library / f"{digest}.json").write_text("{}")
    body = preview_body(FC_EDIT) if url is preview_url else sim_request()
    with TestClient(make_app(config)) as client:
        response = client.post(url(digest), json=body, headers=AUTH)
        assert response.status_code == 409, response.text
        assert "ROM map definition unavailable" in response.json()["detail"]
        # Unknown wins over no-map: there is nothing to say a map is missing for.
        assert client.post(url("a" * 64), json=body, headers=AUTH).status_code == 404


# -- simulate -----------------------------------------------------------------------------------


def test_simulate_runs_the_child_and_returns_the_cells(
    client: TestClient,
    sha: str,
    in_process: CountingExecutor,
    profile: Path,
) -> None:
    response = client.post(simulate_url(sha), json=sim_request(), headers=AUTH)
    assert response.status_code == 200, response.text
    assert in_process.submitted == 1
    body = response.json()
    assert "refused" not in body
    # The route adds nothing: the child's own output, through JSON.
    params = {
        "image_path": str(client.app.state.rom_library.root / f"{sha}.bin"),  # type: ignore[attr-defined]
        "sha256": sha,
        "revision_match": client.app.state.rom_library.revision_match(sha),  # type: ignore[attr-defined]
        "profile_dir": str(profile),
        "request": sim_request(),
    }
    assert body == json.loads(json.dumps(rom_editor.run_job("rom_sim", params)))
    # A 2 x 3 grid of points on Fuel Comp's 16 x 16 table: every cell present,
    # visited or not (a cell nobody visited is not an unchanged one).
    cells = body["cells"]
    assert cells["schema_version"] == 1 and cells["grid"] == "fuel_comp"
    assert body["design"]["points"] == 6
    assert "assumptions" not in body["design"]  # the design's are in body["assumptions"], E08.10
    assert any(a.startswith("design: ") for a in body["assumptions"])
    assert len(cells["cells"]) == 16 * 16
    assert sum(1 for cell in cells["cells"] if cell["pairs"]) == 6
    assert body["provenance"]["seed"] == 20261015 and body["provenance"]["draws"] == 6
    assert next(iter(body)) in ("tool", "caveats") and body["caveats"]
    assert body["spec_yaml"].startswith("# CANary ROM Editor edit spec")
    assert body["spec_sha256"]
    # The K edit reaches the pulse: some cell changed.
    assert any(
        o["status"] == "changed" for cell in cells["cells"] for o in cell["outputs"].values()
    )


def test_simulate_defers_while_a_capture_records_and_runs_no_child(
    client: TestClient, sha: str, in_process: CountingExecutor, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(workbench_jobs.tuning_profile, "is_active", lambda: True)
    monkeypatch.setattr(
        workbench_jobs.tuning_profile, "active_profile", lambda: type("P", (), {"name": "tuning"})()
    )
    response = client.post(simulate_url(sha), json=sim_request(), headers=AUTH)
    assert response.status_code == 409, response.text
    body = detail(response)
    assert body["error"] == "analysis_deferred" and body["reason"] == "capture_active"
    assert body["profile"] == "tuning"
    assert body["message"] == "analysis is deferred while 'tuning' is recording"
    # The workbench's own text tar-streams a session; this job reads an image.
    assert f"/control/rom/library/{sha}/download" in body["laptop_path"]
    assert "tar -cf" not in body["laptop_path"]
    assert in_process.submitted == 0
    # Preview is the thread pool's cheap read: never deferred.
    assert (
        client.post(preview_url(sha), json=preview_body(FC_EDIT), headers=AUTH).status_code == 200
    )


def test_simulate_is_busy_while_another_child_runs(
    client: TestClient, sha: str, in_process: CountingExecutor, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setitem(workbench_jobs._IN_FLIGHT, "other", object())  # type: ignore[arg-type]
    response = client.post(simulate_url(sha), json=sim_request(), headers=AUTH)
    assert response.status_code == 409, response.text
    assert detail(response)["error"] == "analysis_busy"
    assert detail(response)["reason"] == "child_busy"
    assert in_process.submitted == 0


def test_a_child_that_times_out_is_the_workbenchs_503(
    client: TestClient, sha: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A child that does not finish (here: an instant timeout) is the workbench's
    named 503, passed through."""
    monkeypatch.setattr(workbench_jobs, "limit_address_space", lambda nbytes: (nbytes, nbytes))
    pool = CountingExecutor()
    real = workbench_jobs.run_off_loop
    monkeypatch.setattr(
        rom_editor_routes, "run_off_loop", functools.partial(real, executor=pool, timeout=1e-9)
    )
    response = client.post(simulate_url(sha), json=sim_request(), headers=AUTH)
    assert response.status_code == 503, response.text
    assert detail(response)["error"] == "analysis_unavailable"
    assert detail(response)["reason"] == "child_timed_out"
    pool.shutdown(wait=True)


@pytest.mark.parametrize(
    ("over", "kind", "reason"),
    [
        (
            dict(edits=[{"table": "timing_trim_a", "slot": 1, "all": True, "add_raw": 1}]),
            "edit_spec",
            "table_not_editable",
        ),
        (
            dict(edits=[{"table": "fuel_targets", "slot": 1, "cells": [[0, 0]], "set_raw": 300}]),
            "rom_effect",
            "value_out_of_range",
        ),
        (dict(rpm_edges=[2000.0]), "sim_design", "grid_edges_invalid"),
        (dict(draws=rom_editor.MAX_DRAWS + 1), "simulation", "draws_too_many"),
        (
            dict(
                rpm_edges=[float(400 + 100 * i) for i in range(34)],
                load_edges=[float(i) for i in range(34)],
            ),
            "simulation",
            "grid_too_large",
        ),
    ],
)
def test_a_refusal_in_the_child_is_a_400_with_its_reason_not_a_503(
    client: TestClient,
    sha: str,
    in_process: CountingExecutor,
    over: dict[str, Any],
    kind: str,
    reason: str,
) -> None:
    response = client.post(simulate_url(sha), json=sim_request(**over), headers=AUTH)
    assert response.status_code == 400, response.text
    body = detail(response)
    assert body["error"] == "sim_refused"
    assert (body["kind"], body["reason"]) == (kind, reason)
    assert body["message"] and not body["message"].startswith(f"{reason}:")
    assert set(body) == {"error", "kind", "reason", "message"}


def test_a_revision_recorded_from_the_dashboard_is_simulated_as_the_library_sees_it(
    tmp_path: Path,
    profile: Path,
    kit_and_image: tuple[SimKit, bytes],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The library matches an image against the flash history the daemon reads:
    the tracked registry merged with the operational one a dashboard flash
    record writes. The child must design against the same history, or a base
    the page offers to simulate (matched) is refused as unknown."""
    _, image = kit_and_image
    own = tmp_path / "xterra"
    shutil.copytree(profile, own)
    (own / "tune_revisions.yaml").write_text(
        REVISIONS_YAML.format(revision="ZS04C-stock", sha="a" * 64)
    )
    config = isolated_config(tmp_path, daemon={"afr_calibration": own / "afr_calibration.yaml"})
    operational = operational_dir(config)
    operational.mkdir(parents=True, exist_ok=True)
    (operational / "tune_revisions.yaml").write_text(
        REVISIONS_YAML.format(revision=REVISION, sha=sha256_hex(image))
    )
    monkeypatch.setattr(workbench_jobs, "limit_address_space", lambda nbytes: (nbytes, nbytes))
    pool = CountingExecutor()
    monkeypatch.setattr(
        rom_editor_routes,
        "run_off_loop",
        functools.partial(workbench_jobs.run_off_loop, executor=pool),
    )
    with TestClient(make_app(config)) as client:
        sha = upload(client, image, "synthetic.bin")
        entry = client.get(f"/control/rom/library/{sha}", headers=AUTH).json()["entry"]
        assert entry["revision_match"]["revision"] == REVISION, entry["revision_match"]
        response = client.post(simulate_url(sha), json=sim_request(draws=2), headers=AUTH)
        assert response.status_code == 200, response.text
        assert response.json()["design"]["base_revision"] == REVISION
    pool.shutdown(wait=True)


def test_an_unrecorded_base_is_refused_before_anything_is_deferred_or_spawned(
    client: TestClient,
    unrecorded: str,
    in_process: CountingExecutor,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A fact about the request, not about the load: it is a 400 whether or not a
    capture records, and no child is spawned for it."""
    for recording in (False, True):
        if recording:
            monkeypatch.setattr(workbench_jobs.tuning_profile, "is_active", lambda: True)
            monkeypatch.setattr(
                workbench_jobs.tuning_profile,
                "active_profile",
                lambda: type("P", (), {"name": "tuning"})(),
            )
        response = client.post(simulate_url(unrecorded), json=sim_request(), headers=AUTH)
        assert response.status_code == 400, (recording, response.text)
        body = detail(response)
        assert (body["error"], body["kind"], body["reason"]) == (
            "sim_refused",
            "simulation",
            "base_not_a_recorded_revision",
        )
        assert "'unrecognised'" in body["message"] and "previews" in body["message"]
        assert set(body) == {"error", "kind", "reason", "message"}
    assert in_process.submitted == 0


def test_every_request_field_is_required_and_a_missing_one_is_a_422(
    client: TestClient, sha: str, in_process: CountingExecutor
) -> None:
    full = sim_request()
    for name in rom_editor.SimRequest.model_fields:
        partial = {k: v for k, v in full.items() if k != name}
        response = client.post(simulate_url(sha), json=partial, headers=AUTH)
        assert response.status_code == 422, name
        (error,) = response.json()["detail"]
        assert (error["type"], error["loc"]) == ("missing", ["body", name])
    extra = client.post(simulate_url(sha), json={**full, "slot": 2}, headers=AUTH)
    assert extra.status_code == 422
    assert extra.json()["detail"][0]["type"] == "extra_forbidden"
    wrong = client.post(simulate_url(sha), json={**full, "grid": "fuel_targets"}, headers=AUTH)
    assert wrong.status_code == 422
    assert in_process.submitted == 0


class RealChild:
    """``run_off_loop`` with the real ``spawn`` child (no executor) and a bounded
    wait, so a hung child fails a test in 90 s rather than at the digest's
    timeout. Records each dispatch; ``before`` runs just before the child starts
    (to change the world between the route's checks and the child's)."""

    def __init__(self) -> None:
        self.calls: list[str] = []
        self.before: Callable[[], None] | None = None

    async def __call__(
        self, kind: str, params: dict[str, Any], config: CanaryConfig, **kw: Any
    ) -> dict[str, Any]:
        self.calls.append(kind)
        if self.before is not None:
            self.before()
        return await workbench_jobs.run_off_loop(kind, params, config, timeout=90.0, **kw)


@pytest.fixture
def real_child(monkeypatch: pytest.MonkeyPatch) -> RealChild:
    child = RealChild()
    monkeypatch.setattr(rom_editor_routes, "run_off_loop", child)
    return child


@pytest.mark.usefixtures("real_child")
def test_the_refusal_trap_end_to_end_through_a_real_spawn_child(
    client: TestClient, sha: str
) -> None:
    """The simulator's refusals take (reason, message) and do not pickle. In a
    real ``spawn`` child one raised out of it breaks the pool (checked by letting
    ``simulate_edits`` raise: 503 ``child_failed``, ``BrokenProcessPool``).
    Returned as data it is a 400 with its reason. This is the test that catches a refactor that lets
    them escape: an in-process executor never pickles."""
    for over, kind, reason in (
        (
            dict(edits=[{"table": "timing_trim_a", "slot": 1, "all": True, "add_raw": 1}]),
            "edit_spec",
            "table_not_editable",
        ),
        (dict(draws=rom_editor.MAX_DRAWS + 1), "simulation", "draws_too_many"),
        (dict(rpm_edges=[2000.0]), "sim_design", "grid_edges_invalid"),
    ):
        response = client.post(simulate_url(sha), json=sim_request(**over), headers=AUTH)
        assert response.status_code == 400, (reason, response.status_code, response.text)
        body = detail(response)
        assert (body["error"], body["kind"], body["reason"]) == ("sim_refused", kind, reason)
    assert workbench_jobs._IN_FLIGHT == {}


@pytest.mark.usefixtures("real_child")
def test_a_whole_simulate_crosses_the_real_spawn_boundary(client: TestClient, sha: str) -> None:
    """The result (a plain dict, hundreds of floats) pickles back out of a real child."""
    response = client.post(simulate_url(sha), json=sim_request(draws=2), headers=AUTH)
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["cells"]["schema_version"] == 1 and body["provenance"]["draws"] == 2


def write_operational_history(config: CanaryConfig, image: bytes, *, flashed_at: str) -> None:
    """The dashboard's own flash record (R05.17): the operational registry."""
    operational = operational_dir(config)
    operational.mkdir(parents=True, exist_ok=True)
    text = REVISIONS_YAML.format(revision=REVISION, sha=sha256_hex(image))
    (operational / "tune_revisions.yaml").write_text(
        text.replace("2026-10-01T00:00:00Z", flashed_at)
    )


def test_a_conflicting_flash_history_is_a_400_and_spawns_nothing(
    config: CanaryConfig,
    kit_and_image: tuple[SimKit, bytes],
    real_child: RealChild,
) -> None:
    """One revision name recorded as two different flashes (tracked vs
    operational) is a RegistryConflictError, which does not pickle: in a child
    it was a 503. The library reports that history as ``unknown``, and a base
    that is not a recorded revision is refused before a child is spawned."""
    _, image = kit_and_image
    write_operational_history(config, image, flashed_at="2026-10-02T00:00:00Z")
    with TestClient(make_app(config)) as client:
        sha = upload(client, image, "synthetic.bin")
        match = client.get(f"/control/rom/library/{sha}", headers=AUTH).json()["entry"][
            "revision_match"
        ]
        assert match["status"] == "unknown" and "RegistryConflictError" in match["reason"]
        response = client.post(simulate_url(sha), json=sim_request(), headers=AUTH)
        assert response.status_code == 400, response.text
        body = detail(response)
        assert (body["error"], body["kind"], body["reason"]) == (
            "sim_refused",
            "simulation",
            "base_not_a_recorded_revision",
        )
        assert "flash history unreadable" in body["message"]
        # The preview does not need the history: it still works, by file.
        previewed = client.post(preview_url(sha), json=preview_body(FC_EDIT), headers=AUTH)
        assert previewed.status_code == 200, previewed.text
        assert previewed.json()["sim_base"]["reason"] == "base_not_a_recorded_revision"
    assert real_child.calls == []


def test_a_flash_history_that_breaks_after_the_check_is_a_400_from_the_real_child(
    client: TestClient,
    sha: str,
    config: CanaryConfig,
    kit_and_image: tuple[SimKit, bytes],
    real_child: RealChild,
) -> None:
    """The route matched the image against a readable history; by the time the
    child reads it the operational registry conflicts with the tracked one. The
    child returns that as data (``flash_history_unreadable``): raised, the
    RegistryConflictError would not unpickle and the route would answer 503."""
    _, image = kit_and_image
    real_child.before = lambda: write_operational_history(
        config, image, flashed_at="2026-10-02T00:00:00Z"
    )
    response = client.post(simulate_url(sha), json=sim_request(draws=2), headers=AUTH)
    assert real_child.calls == ["rom_sim"]
    assert response.status_code == 400, response.text
    body = detail(response)
    assert (body["error"], body["kind"], body["reason"]) == (
        "sim_refused",
        "simulation",
        "flash_history_unreadable",
    )
    assert "RegistryConflictError" in body["message"]
    assert workbench_jobs._IN_FLIGHT == {}


def test_a_missing_vehicle_definition_is_a_409_on_both_routes(
    tmp_path: Path,
    profile: Path,
    kit_and_image: tuple[SimKit, bytes],
    real_child: RealChild,
) -> None:
    """The map is there (the library works) but the vehicle has no application
    model: preview and simulate say which definition is missing, as a 409. The
    simulate error is raised in a real child and crosses the process boundary."""
    _, image = kit_and_image
    bare = tmp_path / "no-model"
    shutil.copytree(profile, bare)
    (bare / "ecu_application_model.yaml").unlink()
    config = isolated_config(tmp_path, daemon={"afr_calibration": bare / "afr_calibration.yaml"})
    with TestClient(make_app(config)) as client:
        sha = upload(client, image, "synthetic.bin")
        for url, body in (
            (preview_url(sha), preview_body(FC_EDIT)),
            (simulate_url(sha), sim_request(draws=2)),
        ):
            response = client.post(url, json=body, headers=AUTH)
            assert response.status_code == 409, (url, response.text)
            message = response.json()["detail"]
            assert message.startswith("ROM Editor definition unavailable for this vehicle:")
            assert "ecu_application_model.yaml" in message
    assert real_child.calls == ["rom_sim"]


# -- nothing is persisted, nothing leaves --------------------------------------------------------


def test_nothing_is_persisted_and_the_candidate_is_never_offered(
    client: TestClient,
    sha: str,
    in_process: CountingExecutor,
    config: CanaryConfig,
    kit_and_image: tuple[SimKit, bytes],
    tmp_path: Path,
) -> None:
    _, image = kit_and_image
    library = config.daemon.rom_library_dir

    def tree() -> dict[str, bytes]:
        return {
            str(p.relative_to(tmp_path)): p.read_bytes()
            for p in sorted(tmp_path.rglob("*"))
            if p.is_file()
        }

    before_repo = repo_guard.snapshot()
    before = tree()
    preview = client.post(preview_url(sha), json=preview_body(FC_EDIT), headers=AUTH)
    simulated = client.post(simulate_url(sha), json=sim_request(), headers=AUTH)
    assert preview.status_code == 200 and simulated.status_code == 200
    assert tree() == before
    assert repo_guard.diff_snapshots(before_repo, repo_guard.snapshot()) == []
    # The library holds the one base image, unchanged; the candidate is not in it.
    entries = client.get("/control/rom/library", headers=AUTH).json()["entries"]
    assert [e["sha256"] for e in entries] == [sha]
    assert sorted(p.name for p in library.iterdir()) == [f"{sha}.bin", f"{sha}.json"]
    download = client.get(f"/control/rom/library/{sha}/download", headers=AUTH)
    assert download.content == image
    # Neither response carries the candidate's bytes, only its hash.
    candidate = preview.json()["candidate_sha256"]
    assert candidate != sha and candidate not in {e["sha256"] for e in entries}
    assert len(preview.content) < 500_000 and len(simulated.content) < 5_000_000


def test_the_editor_is_wired_into_the_dashboard_app(tmp_path: Path, profile: Path) -> None:
    """``create_app`` registers it, after the library (which it needs)."""
    from src.dashboard.api import create_app

    config = isolated_config(tmp_path, daemon={"afr_calibration": profile / "afr_calibration.yaml"})
    app = create_app(config, anomalies_dir=tmp_path / "anomalies", control_token="secret")
    paths = {getattr(r, "path", None) for r in app.routes}
    assert "/control/rom/library/{sha}/edits/preview" in paths
    assert "/control/rom/library/{sha}/simulate" in paths
    assert "/control/rom/editor/typical-values" in paths
