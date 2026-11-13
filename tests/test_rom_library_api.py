"""W08.3: library routes — upload, list, describe, download, label, delete; tables; compare."""

from __future__ import annotations

from collections.abc import Iterator
from pathlib import Path

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from src.config import CanaryConfig
from src.dashboard.routes.rom_library import register_rom_library_routes
from tests import repo_guard
from tests.configs import isolated_config
from tests.rom_fixtures import (
    PROFILE,
    encrypted_backup_lookalike,
    osiris_container,
    synthetic_image,
)

AUTH = {"Authorization": "Bearer secret"}
OCTET = {**AUTH, "Content-Type": "application/octet-stream"}


def _config(tmp_path: Path) -> CanaryConfig:
    # The profile directory is read-only input (the real Xterra map); every
    # writable path is under tmp_path.
    return isolated_config(tmp_path, daemon={"afr_calibration": PROFILE / "afr_calibration.yaml"})


@pytest.fixture
def config(tmp_path: Path) -> CanaryConfig:
    return _config(tmp_path)


@pytest.fixture
def client(config: CanaryConfig) -> Iterator[TestClient]:
    app = FastAPI()
    register_rom_library_routes(app, config=config, resolved_control_token="secret")
    with TestClient(app) as test_client:
        yield test_client


def _upload(client: TestClient, data: bytes, filename: str = "image.bin"):  # type: ignore[no-untyped-def]
    return client.post(
        "/control/rom/library", params={"filename": filename}, content=data, headers=OCTET
    )


def test_every_route_requires_the_token(client: TestClient) -> None:
    sha = "0" * 64
    for method, path in [
        ("post", "/control/rom/library?filename=x.bin"),
        ("get", "/control/rom/library"),
        ("get", f"/control/rom/library/{sha}"),
        ("get", f"/control/rom/library/{sha}/download"),
        ("get", f"/control/rom/library/{sha}/tables"),
        ("get", f"/control/rom/library/{sha}/trace"),
        ("patch", f"/control/rom/library/{sha}"),
        ("delete", f"/control/rom/library/{sha}"),
        ("get", f"/control/rom/compare?a={sha}&b={sha}"),
        ("get", "/control/rom/backup"),
        ("post", "/control/rom/backup"),
        ("post", "/control/rom/backup/cancel"),
    ]:
        assert getattr(client, method)(path).status_code == 401, (method, path)


def test_upload_plain_then_osiris_keeps_one_entry_and_adds_the_source(
    client: TestClient, config: CanaryConfig
) -> None:
    image = synthetic_image()
    first = _upload(client, image, "backup.bin")
    assert first.status_code == 200, first.text
    entry = first.json()
    assert entry["created"] is True
    assert entry["source"] == "upload_plain" and entry["sources"] == ["upload_plain"]
    assert entry["filename"] == "backup.bin"
    assert entry["calibration_id"] == "1ZS44A00000"
    assert entry["checksum"]["ok"] is True
    assert entry["revision_match"]["status"] == "unrecognised"
    assert entry["size"] == len(image)
    assert entry["container_sha256"] is None and entry["read_meta"] is None
    sha = entry["sha256"]
    library = config.daemon.rom_library_dir
    assert (library / f"{sha}.bin").read_bytes() == image
    assert (library / f"{sha}.json").exists()

    second = _upload(client, osiris_container(image), "tune.OsirisROM")
    assert second.status_code == 200, second.text
    again = second.json()
    assert again["created"] is False and again["sha256"] == sha
    assert again["sources"] == ["upload_plain", "upload_osirisrom"]
    assert again["container_sha256"] is not None
    assert len(list(library.glob("*.bin"))) == 1


def test_encrypted_backup_is_refused_by_name(client: TestClient, config: CanaryConfig) -> None:
    response = _upload(client, encrypted_backup_lookalike(), "23710 - ZS44A.bin")
    assert response.status_code == 422
    assert "UpRev encrypted ROM backup" in response.json()["detail"]
    assert not config.daemon.rom_library_dir.exists() or not any(
        config.daemon.rom_library_dir.iterdir()
    )


def test_unrecognised_sizes_are_refused_by_name(client: TestClient) -> None:
    response = _upload(client, b"x" * 1000)
    assert response.status_code == 422
    assert "1,000 bytes" in response.json()["detail"]


def test_upload_cap_type_and_filename(client: TestClient) -> None:
    assert _upload(client, b"x" * (2 * 1024 * 1024 + 1)).status_code == 413
    assert (
        client.post(
            "/control/rom/library", params={"filename": "a.bin"}, json={}, headers=AUTH
        ).status_code
        == 415
    )
    assert (
        client.post("/control/rom/library", content=b"x", headers=OCTET).status_code == 422
    )  # filename is required
    assert _upload(client, b"").status_code == 422


def test_list_describe_download_label_delete(client: TestClient) -> None:
    first = _upload(client, synthetic_image(seed=1), "one.bin").json()
    second = _upload(client, synthetic_image(seed=2), "two.bin").json()
    listing = client.get("/control/rom/library", headers=AUTH).json()
    assert [e["sha256"] for e in listing["entries"]] == [second["sha256"], first["sha256"]]
    assert listing["count"] == 2

    detail = client.get(f"/control/rom/library/{first['sha256']}", headers=AUTH)
    assert detail.status_code == 200
    body = detail.json()
    assert body["sha256"] == first["sha256"] and body["entry"]["filename"] == "one.bin"
    assert len(body["blocks"]) == 256 and body["map"] is not None
    assert body["checksum"]["ok"] is True

    download = client.get(f"/control/rom/library/{first['sha256']}/download", headers=AUTH)
    assert download.status_code == 200
    assert download.headers["content-type"] == "application/octet-stream"
    assert download.content == synthetic_image(seed=1)
    assert f"one-{first['sha256'][:8]}.bin" in download.headers["content-disposition"]

    patched = client.patch(
        f"/control/rom/library/{first['sha256']}", json={"label": "  stock  base "}, headers=AUTH
    )
    assert patched.status_code == 200 and patched.json()["label"] == "stock base"
    assert (
        client.patch(
            f"/control/rom/library/{first['sha256']}", json={"label": "x" * 200}, headers=AUTH
        ).status_code
        == 422
    )
    assert (
        client.patch(
            f"/control/rom/library/{first['sha256']}",
            json={"label": "a", "filename": "b"},
            headers=AUTH,
        ).status_code
        == 422
    )  # the label is the only editable field

    deleted = client.delete(f"/control/rom/library/{first['sha256']}", headers=AUTH)
    assert deleted.status_code == 200 and deleted.json() == {"deleted": first["sha256"]}
    assert client.get(f"/control/rom/library/{first['sha256']}", headers=AUTH).status_code == 404
    assert client.delete(f"/control/rom/library/{first['sha256']}", headers=AUTH).status_code == 404


def test_unknown_or_malformed_sha_is_404(client: TestClient) -> None:
    assert client.get(f"/control/rom/library/{'a' * 64}", headers=AUTH).status_code == 404
    assert client.get("/control/rom/library/..%2F..%2Fetc", headers=AUTH).status_code == 404
    assert client.get("/control/rom/library/ABC", headers=AUTH).status_code == 404


def test_tables_and_compare_routes(client: TestClient) -> None:
    a = _upload(client, synthetic_image(k=27089), "a.bin").json()["sha256"]
    b = _upload(client, synthetic_image(k=28715), "b.bin").json()["sha256"]
    tables = client.get(f"/control/rom/library/{a}/tables", headers=AUTH).json()
    k1 = next(t for t in tables["tables"] if t["id"] == "k_constant" and t["slot"] == 1)
    assert k1["raw"] == [[27089]] and k1["confidence"] == "verified"
    fuel = next(t for t in tables["tables"] if t["id"] == "fuel_targets" and t["slot"] == 1)
    assert fuel["decoded"][0][0] == pytest.approx(14.7)
    # The slot the page offers edits in is the server's (R25.3): the 2008's
    # fuel_comp_fit.yaml declares map_slot 1, on every table of the response.
    assert {t["sim_slot"] for t in tables["tables"]} == {1}

    compare = client.get("/control/rom/compare", params={"a": a, "b": b}, headers=AUTH)
    assert compare.status_code == 200
    result = compare.json()
    assert result["a"] == a and result["b"] == b
    k_ranges = [r for r in result["ranges"] if any("K (" in lbl for lbl in r["labels"])]
    assert len(k_ranges) == 5
    assert k_ranges[0]["cells"][0]["old"] == 27089
    assert (
        client.get("/control/rom/compare", params={"a": a, "b": "f" * 64}, headers=AUTH).status_code
        == 404
    )


def test_trace_context_serves_the_vehicles_trace_definitions_for_a_library_image(
    client: TestClient,
) -> None:
    """The ROM Editor's live overlay (merged from ROM Maps): the viewer's trace
    definitions and live providers for a plain library image, and the honest
    answer that an image nobody flashed is not the running revision."""
    sha = _upload(client, synthetic_image(k=27089), "a.bin").json()["sha256"]
    response = client.get(f"/control/rom/library/{sha}/trace", headers=AUTH)
    assert response.status_code == 200
    context = response.json()
    assert context["sha256"] == sha
    assert context["map_slot"] == 1
    # M01.3: the ECM's AFR scale, under its name and the deprecated old one.
    assert context["afr_scale"] == pytest.approx(14.7)
    assert context["afr_stoich"] == context["afr_scale"]
    assert context["matches_recorded_revision"] is False
    kinds = {table["kind"]: table for table in context["tables"]}
    assert set(kinds) == {"fuel_targets", "fuel_compensation"}
    trace = kinds["fuel_targets"]["trace"]
    assert trace["x"]["channel"] == "mem:ECM_FUEL_TARGET_MAP_X_TRACE"
    assert trace["x"]["axis_mapping"] == "verified" and trace["x"]["axis_units"] == "raw"
    assert trace["x"]["stored_decode"] is not None and trace["x"]["request_channel"]
    assert trace["out_of_range"]["behaviour"] == "clamp"
    # No trace is mapped to Fuel Compensation; the page says so rather than guess.
    assert kinds["fuel_compensation"]["trace"] is None
    assert kinds["fuel_targets"]["x_unit"] == "ms" and kinds["fuel_targets"]["y_unit"] == "rpm"
    assert context["live"]["rpm"] is not None
    assert "x_axis" not in kinds["fuel_targets"]  # axes are the image's, from /tables


def test_trace_context_unknown_image_is_404(client: TestClient) -> None:
    assert client.get(f"/control/rom/library/{'a' * 64}/trace", headers=AUTH).status_code == 404
    assert client.get("/control/rom/library/ABC/trace", headers=AUTH).status_code == 404


def test_trace_context_says_when_the_image_is_the_running_revision() -> None:
    """``matches_recorded_revision`` is the plain-image hash the flash history
    names the running revision by, not a container hash."""
    from types import SimpleNamespace

    from src.dashboard.routes.rom_maps import library_trace_context

    sha = "c" * 64
    running = SimpleNamespace(revision="ZS44A-20260920", image_sha256=sha)
    other = SimpleNamespace(revision="ZS04C", image_sha256="d" * 64)
    for current, matches in [(running, True), (other, False), (None, False)]:
        registry = SimpleNamespace(at=lambda _when, current=current: current)
        context = library_trace_context(
            PROFILE,
            sha=sha,
            revisions=registry,  # type: ignore[arg-type]
        )
        assert context["matches_recorded_revision"] is matches
        assert context["recorded_revision"] == (current.revision if current else None)
    assert library_trace_context(PROFILE, sha=sha, revisions=None)["recorded_revision"] is None


def test_trace_context_is_409_for_a_vehicle_without_a_viewer_definition(tmp_path: Path) -> None:
    """A vehicle with a ROM map but no ``rom_viewer.yaml`` still edits its
    tables; only the live overlay is unavailable, and it says so."""
    profile = tmp_path / "profile"
    profile.mkdir()
    for name in ["rom_map.yaml", "rom_targets.yaml", "afr_calibration.yaml"]:
        (profile / name).write_text((PROFILE / name).read_text())
    config = isolated_config(tmp_path, daemon={"afr_calibration": profile / "afr_calibration.yaml"})
    app = FastAPI()
    register_rom_library_routes(app, config=config, resolved_control_token="secret")
    with TestClient(app) as client:
        sha = _upload(client, synthetic_image(k=27089), "a.bin").json()["sha256"]
        response = client.get(f"/control/rom/library/{sha}/trace", headers=AUTH)
        assert response.status_code == 409
        assert "rom_viewer.yaml" in response.json()["detail"]


def test_nothing_is_written_under_the_repo(tmp_path: Path) -> None:
    before = repo_guard.snapshot()
    config = _config(tmp_path)
    app = FastAPI()
    register_rom_library_routes(app, config=config, resolved_control_token="secret")
    with TestClient(app) as client:
        sha = _upload(client, synthetic_image(seed=5)).json()["sha256"]
        client.patch(f"/control/rom/library/{sha}", json={"label": "x"}, headers=AUTH)
        client.get("/control/rom/library", headers=AUTH)
    assert config.daemon.rom_library_dir.is_relative_to(tmp_path)
    assert (config.daemon.rom_library_dir / f"{sha}.bin").exists()
    assert repo_guard.diff_snapshots(before, repo_guard.snapshot()) == []


def test_default_library_dir_is_gitignored_and_scoped_per_vehicle() -> None:
    from src.config import CanaryConfig as Config
    from src.vehicle.profiles import VehicleProfile, scope_config

    config = Config.from_yaml(Path(__file__).resolve().parents[1] / "canary.example.yaml")
    assert config.daemon.rom_library_dir.parts[-3:] == ("data", "control", "rom_library")
    scoped = scope_config(config, VehicleProfile(slug="truck", vin="", name="Truck"))
    assert scoped.daemon.rom_library_dir.name == "truck"
    assert scoped.daemon.rom_library_dir.parent == config.daemon.rom_library_dir


def test_a_vehicle_without_a_map_still_keeps_lists_describes_and_compares(
    tmp_path: Path,
) -> None:
    """No map is not no library: a vehicle without one stores and compares
    images (an ECU backup is how it gets the image to map). Only what needs
    the map says so: a plain upload cannot be size-checked, and the table
    view is 409."""
    profile = tmp_path / "profile"
    profile.mkdir()
    config = isolated_config(tmp_path, daemon={"afr_calibration": profile / "afr_calibration.yaml"})
    app = FastAPI()
    register_rom_library_routes(app, config=config, resolved_control_token="secret")
    with TestClient(app) as client:
        plain = _upload(client, synthetic_image())
        assert plain.status_code == 422
        assert plain.json()["detail"].startswith(
            "no ROM map for this vehicle (rom_map.yaml), so a plain file of 1,048,576 bytes "
            "cannot be checked as an image"
        )
        a = _upload(client, osiris_container(synthetic_image(k=27089)), "a.OsirisROM").json()
        b = _upload(client, osiris_container(synthetic_image(k=28715)), "b.OsirisROM").json()
        assert a["source"] == "upload_osirisrom" and a["checksum"] is None
        assert a["map_reason"] == "no ROM map for this vehicle (rom_map.yaml)"
        assert client.get("/control/rom/library", headers=AUTH).json()["count"] == 2
        detail = client.get(f"/control/rom/library/{a['sha256']}", headers=AUTH).json()
        assert detail["map"] is None and len(detail["blocks"]) == 256
        compare = client.get(
            "/control/rom/compare", params={"a": a["sha256"], "b": b["sha256"]}, headers=AUTH
        ).json()
        assert compare["map_applied"] is False and compare["changed_bytes"] > 0
        assert all(r["labels"] == [] for r in compare["ranges"])
        tables = client.get(f"/control/rom/library/{a['sha256']}/tables", headers=AUTH)
        assert tables.status_code == 409
        assert "ROM map definition unavailable" in tables.json()["detail"]
        download = client.get(f"/control/rom/library/{a['sha256']}/download", headers=AUTH)
        assert download.content == synthetic_image(k=27089)
