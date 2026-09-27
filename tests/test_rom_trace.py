"""W05 — the ECM's own coordinates on a ROM table, and per-cell residency.

The mapping from a trace channel onto a table's breakpoints is a measured
constraint, so it is a required state in ``rom_viewer.yaml``: these tests pin
that an unverified axis cannot carry a scale, that nothing places a cell on
one, and that the residency endpoint answers every "could not" with a named
reason instead of an empty grid.
"""

from __future__ import annotations

import json
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime, timedelta
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest
import yaml
from fastapi import FastAPI
from fastapi.testclient import TestClient
from pydantic import ValidationError

from src.analysis.rom_trace import (
    MAX_DWELL_S,
    AxisTrace,
    TableInfo,
    TableTrace,
    cell_residency,
    load_viewer_definition,
    mapping_refusal,
    nearest_index,
    request_channel,
)
from src.config import CanaryConfig
from src.control import workbench_jobs
from src.dashboard.routes import workbench as workbench_routes
from src.storage.capture_manifest import CAPTURE_MANIFEST_FILENAME
from src.storage.parquet_writer import write_records_to_parquet
from src.telemetry import TelemetryRecord

BINDINGS = Path("data/bindings.yaml")
DIDS = Path("data/dids.yaml")
PROFILE = Path("data/nissan-xterra-2008")
T0 = datetime(2026, 9, 21, 12, 0, 0, tzinfo=UTC)
X = "mem:ECM_FUEL_TARGET_MAP_X_TRACE"
Y = "mem:ECM_ENGINE_SPEED_RAM"
X_AXIS = [8.0, 12.0, 16.0]
Y_AXIS = [1200.0, 2000.0, 2800.0]
AUTH = {"Authorization": "Bearer secret"}


def _verified_trace() -> TableTrace:
    """A synthetic *verified* mapping — the shipped one is not (W05.1)."""
    return TableTrace(
        x=AxisTrace(
            channel=X, axis_mapping="verified", scale=1.0, offset=0.0, evidence="synthetic"
        ),
        y=AxisTrace(
            channel=Y, axis_mapping="verified", scale=1.0, offset=0.0, evidence="synthetic"
        ),
    )


def _table(trace: TableTrace | None) -> TableInfo:
    return TableInfo(
        kind="fuel_targets",
        title="Target AFR",
        x_label="Base Fuel Schedule",
        x_unit="ms",
        y_label="Rotations Per Minute",
        y_unit="rpm",
        unit="AFR",
        trace=trace,
    )


CALIBRATION = SimpleNamespace(
    target_map=lambda revision: (
        SimpleNamespace(load_breakpoints=X_AXIS, rpm_breakpoints=Y_AXIS)
        if revision == "REV-A"
        else None
    )
)


def _write(
    root: Path, name: str, points: list[tuple[float, float]], *, step_s: float = 0.125
) -> Path:
    """A session whose trace channels read ``(x, y)`` at ``step_s`` intervals."""
    records: list[TelemetryRecord] = []
    for i, (x, y) in enumerate(points):
        ts = T0 + timedelta(seconds=i * step_s)
        records.append(TelemetryRecord(timestamp=ts, source="obd", channel=X, value=x))
        records.append(
            TelemetryRecord(
                timestamp=ts + timedelta(milliseconds=40), source="obd", channel=Y, value=y
            )
        )
    out = root / name
    write_records_to_parquet(records, out)
    return out


# --------------------------------------------------------------------------- schema


def test_an_unverified_axis_cannot_carry_a_scale() -> None:
    with pytest.raises(ValidationError, match="carries no scale"):
        AxisTrace(channel=X, axis_mapping="unverified", scale=0.25, offset=0.0, evidence="guess")


def test_a_verified_axis_must_state_its_scale() -> None:
    with pytest.raises(ValidationError, match="nonzero scale"):
        AxisTrace(channel=X, axis_mapping="verified", evidence="settled")


def test_to_axis_refuses_an_unverified_mapping() -> None:
    axis = AxisTrace(channel=X, axis_mapping="unverified", evidence="not settled")
    with pytest.raises(ValueError, match="unverified"):
        axis.to_axis(40.0)


def test_shipped_profile_maps_the_fuel_target_trace_but_refuses_its_x_axis() -> None:
    """W05.1 as shipped: Y (rpm) is settled, the X trace's relation to BFS is not."""
    table = load_viewer_definition(PROFILE).table("fuel_targets")
    assert table is not None and table.trace is not None
    assert table.trace.x.channel == X and table.trace.x.axis_mapping == "unverified"
    assert table.trace.x.scale is None
    assert table.trace.y.channel == Y and table.trace.y.axis_mapping == "verified"
    assert table.trace.unavailable_note and "rom-trace" in table.trace.unavailable_note
    refusal = mapping_refusal(table.trace)
    assert refusal is not None and refusal["reason"] == "axis_mapping_unverified"
    assert X in refusal["message"]


def test_request_channel_names_the_memory_window_that_carries_the_trace() -> None:
    assert request_channel(X, DIDS) == "mem:fuel_axis"
    assert request_channel(Y, DIDS) == "mem:spark"
    assert request_channel("mem:ECM_THROTTLE_MAP_X_TRACE", DIDS) == "mem:ECM_THROTTLE_MAP_X_TRACE"
    assert request_channel("did:ECM_RPM", DIDS) is None


def test_nearest_index_is_none_outside_the_axis() -> None:
    assert nearest_index(X_AXIS, 9.9) == 0
    assert nearest_index(X_AXIS, 10.0) == 0  # tie goes low, like the page
    assert nearest_index(X_AXIS, 15.0) == 2
    assert nearest_index(X_AXIS, 7.9) is None
    assert nearest_index(X_AXIS, 16.1) is None


# --------------------------------------------------------------------------- residency


def _residency(session: Path, trace: TableTrace | None, revision: str | None = "REV-A"):
    return cell_residency(
        session,
        table=_table(trace),
        revision=revision,
        revision_source="capture_manifest",
        calibration=CALIBRATION,
        bindings_path=BINDINGS,
        dids_path=DIDS,
    )


def test_residency_counts_samples_and_dwell_per_cell(tmp_path: Path) -> None:
    points = [(8.2, 1210.0)] * 4 + [(15.5, 2750.0)] * 3 + [(30.0, 2000.0)]
    result = _residency(_write(tmp_path, "2026-09-21T120000", points), _verified_trace())
    assert result["available"] is True
    assert result["counts"] == [[4, 0, 0], [0, 0, 0], [0, 0, 3]]
    assert result["samples"] == 8
    assert result["outside_axes"] == 1  # counted, never dropped silently
    assert result["dwell_s"][0][0] == pytest.approx(0.5)
    assert result["dwell_s"][2][2] == pytest.approx(0.375)
    assert result["assignment"] == "nearest_breakpoint"
    assert result["x_axis"] == X_AXIS and result["y_axis"] == Y_AXIS


def test_residency_dwell_is_capped_across_a_polling_gap(tmp_path: Path) -> None:
    session = _write(tmp_path, "2026-09-21T120000", [(8.0, 1200.0), (12.0, 2000.0)], step_s=30.0)
    result = _residency(session, _verified_trace())
    assert result["dwell_s"][0][0] == pytest.approx(MAX_DWELL_S)


def test_residency_names_a_log_without_trace_channels(tmp_path: Path) -> None:
    records = [TelemetryRecord(timestamp=T0, source="obd", channel="RPM", value=900.0)]
    write_records_to_parquet(records, tmp_path / "2026-09-21T120000")
    result = _residency(tmp_path / "2026-09-21T120000", _verified_trace())
    assert result["available"] is False
    assert result["reason"] == "not_captured"
    assert result["missing"] == [X, Y]
    assert "counts" not in result


def test_residency_refuses_an_unverified_mapping_before_reading(tmp_path: Path) -> None:
    trace = load_viewer_definition(PROFILE).table("fuel_targets").trace  # type: ignore[union-attr]
    result = _residency(tmp_path / "does-not-exist", trace)
    assert (result["available"], result["reason"]) == (False, "axis_mapping_unverified")


def test_residency_names_an_unknown_revision(tmp_path: Path) -> None:
    session = _write(tmp_path, "2026-09-21T120000", [(8.0, 1200.0)])
    result = _residency(session, _verified_trace(), revision=None)
    assert (result["available"], result["reason"]) == (False, "revision_unknown")
    result = _residency(session, _verified_trace(), revision="REV-B")
    assert result["reason"] == "axes_not_recorded"


# --------------------------------------------------------------------------- route


def _profile(tmp_path: Path, *, verified: bool) -> Path:
    profile = tmp_path / "profile"
    profile.mkdir(exist_ok=True)
    viewer = yaml.safe_load((PROFILE / "rom_viewer.yaml").read_text())
    if verified:
        viewer["tables"][0]["trace"] = _verified_trace().model_dump(exclude_none=True)
    (profile / "rom_viewer.yaml").write_text(yaml.safe_dump(viewer))
    afr = [[14.7] * len(X_AXIS) for _ in Y_AXIS]
    (profile / "afr_calibration.yaml").write_text(
        yaml.safe_dump(
            {
                "commanded_equiv_ratio_convention": "phi",
                "target_maps": [
                    {
                        "revision": "REV-A",
                        "rpm_breakpoints": Y_AXIS,
                        "load_breakpoints": X_AXIS,
                        "afr": afr,
                    }
                ],
            }
        )
    )
    return profile


def _config(tmp_path: Path, *, verified: bool) -> CanaryConfig:
    config = CanaryConfig.from_yaml(Path("canary.example.yaml"))
    profile = _profile(tmp_path, verified=verified)
    storage = tmp_path / "storage"
    storage.mkdir(exist_ok=True)
    return config.model_copy(
        update={
            "collection": config.collection.model_copy(update={"storage_path": storage}),
            "daemon": config.daemon.model_copy(
                update={
                    "afr_calibration": profile / "afr_calibration.yaml",
                    "jobs_dir": tmp_path / "control" / "jobs",
                }
            ),
        }
    )


def _client(config: CanaryConfig, monkeypatch: pytest.MonkeyPatch) -> TestClient:
    app = FastAPI()
    workbench_routes.register_workbench_routes(
        app,
        config=config,
        bindings_path=BINDINGS,
        dids_path=DIDS,
        resolved_control_token="secret",
    )
    monkeypatch.setattr(workbench_jobs, "_spawn_pool", lambda: ThreadPoolExecutor(max_workers=1))
    monkeypatch.setattr(workbench_jobs, "limit_address_space", lambda nbytes: (nbytes, nbytes))
    return TestClient(app)


def _capture_session(storage: Path, points: list[tuple[float, float]]) -> str:
    name = "_capture-rom-trace-2026-09-21T120000"
    session = _write(storage, name, points)
    manifest: dict[str, Any] = {
        "profile": "rom-trace",
        "started_at": T0.isoformat(),
        "profile_snapshot": {},
        "tune_revision": "REV-A",
    }
    (session / CAPTURE_MANIFEST_FILENAME).write_text(json.dumps(manifest))
    return name


def test_cell_residency_route_runs_through_the_child(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    config = _config(tmp_path, verified=True)
    name = _capture_session(config.collection.storage_path, [(12.0, 2000.0)] * 5)
    with _client(config, monkeypatch) as client:
        assert (
            client.get(f"/control/sessions/{name}/cell-residency?table=fuel_targets").status_code
            == 401
        )
        response = client.get(
            f"/control/sessions/{name}/cell-residency?table=fuel_targets", headers=AUTH
        )
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["available"] is True
    assert body["revision"] == "REV-A" and body["revision_source"] == "capture_manifest"
    assert body["counts"][1][1] == 5


def test_cell_residency_route_refuses_unverified_mapping_without_a_child(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    config = _config(tmp_path, verified=False)
    name = _capture_session(config.collection.storage_path, [(12.0, 2000.0)])

    async def no_child(*args: object, **kwargs: object) -> dict[str, Any]:
        raise AssertionError("an unverified mapping must not spawn a whole-session read")

    monkeypatch.setattr(workbench_routes, "run_off_loop", no_child)
    with _client(config, monkeypatch) as client:
        response = client.get(
            f"/control/sessions/{name}/cell-residency?table=fuel_targets", headers=AUTH
        )
        missing = client.get(
            f"/control/sessions/{name}/cell-residency?table=ignition", headers=AUTH
        )
    assert response.status_code == 200
    assert response.json()["reason"] == "axis_mapping_unverified"
    assert missing.status_code == 404


def test_cell_residency_route_defers_while_a_capture_records(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    config = _config(tmp_path, verified=True)
    name = _capture_session(config.collection.storage_path, [(12.0, 2000.0)])
    monkeypatch.setattr(workbench_jobs.tuning_profile, "is_active", lambda: True)
    monkeypatch.setattr(
        workbench_jobs.tuning_profile,
        "active_profile",
        lambda: type("P", (), {"name": "tuning"})(),
    )
    with _client(config, monkeypatch) as client:
        response = client.get(
            f"/control/sessions/{name}/cell-residency?table=fuel_targets", headers=AUTH
        )
    assert response.status_code == 409
    assert response.json()["detail"]["error"] == "analysis_deferred"


def test_cell_residency_route_names_a_log_without_trace_channels(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    config = _config(tmp_path, verified=True)
    storage = config.collection.storage_path
    name = "_capture-tuning-2026-09-21T120000"
    records = [TelemetryRecord(timestamp=T0, source="obd", channel="RPM", value=900.0)]
    write_records_to_parquet(records, storage / name)
    manifest = {"profile": "tuning", "started_at": T0.isoformat(), "profile_snapshot": {}}
    manifest["tune_revision"] = "REV-A"
    (storage / name / CAPTURE_MANIFEST_FILENAME).write_text(json.dumps(manifest))
    with _client(config, monkeypatch) as client:
        response = client.get(
            f"/control/sessions/{name}/cell-residency?table=fuel_targets", headers=AUTH
        )
    assert response.status_code == 200, response.text
    body = response.json()
    assert (body["available"], body["reason"]) == (False, "not_captured")
    assert body["missing"] == [X, Y]
    assert "counts" not in body
