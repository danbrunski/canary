"""R05.1 — the vehicle's tuning context is resolved once, from the live registry.

Before: ``append_flash`` wrote the *operational* flash registry, while the
trip-end digest and the MCP tuning tools read the tracked *source* file beside
the profile data, so a flash recorded from the dashboard never reached their
revision resolution. And ``afr_stoich`` was a literal 14.7 default no caller
overrode, so a diesel profile's workbench AFR was a gasoline AFR.
"""

from __future__ import annotations

from datetime import timedelta
from pathlib import Path
from typing import Any, cast

import pytest
import yaml

from src.analysis.tuning_context import TuningContext
from src.analysis.tuning_digest import compute_tuning_digest
from src.collection import tuning_profile
from src.config import CanaryConfig
from src.control import workbench_jobs
from src.control.calibration import append_flash
from src.mcp_server.tools import tuning_analysis
from src.storage.parquet_writer import write_records_to_parquet
from src.telemetry import TelemetryRecord
from src.vehicle.profiles import (
    active_registry_path,
    operational_registry_path,
    resolve_profile_for_tooling,
    scope_config,
    source_registry_path,
)
from src.vehicle.tune_revisions import TuneRevision, load_tune_revisions
from src.yaml_load import load_yaml

_TRACKED = load_tune_revisions(Path("data/nissan-xterra-2008/tune_revisions.yaml"))
assert _TRACKED is not None
#: Two days after the newest flash in the tracked Xterra registry, so the
#: synthetic flash below is newest. Derived, not pinned: the registry refuses
#: out-of-order revisions and gains real flashes (2026-10-04 broke a fixed date).
T0 = max(r.flashed_at for r in _TRACKED.revisions) + timedelta(days=2)
#: Session directory stamps for captures that start at T0 and an hour later.
STAMP, LATER = (f"{t:%Y-%m-%dT%H%M%S}" for t in (T0, T0 + timedelta(hours=1)))
FLASH = TuneRevision(revision="R05-dashboard-flash", flashed_at=T0 - timedelta(days=1))


def _raw_config(tmp_path: Path, *, fuel_type: str = "gasoline") -> dict[str, Any]:
    raw = load_yaml(Path("canary.example.yaml").read_text())
    raw.setdefault("collection", {})["storage_path"] = str(tmp_path / "sessions")
    # The operational registry lives beside ``jobs_dir``; keep it in tmp.
    raw.setdefault("daemon", {})["jobs_dir"] = str(tmp_path / "control" / "jobs")
    raw.setdefault("vehicle", {}).setdefault("parameters", {})["fuel_type"] = fuel_type
    # The registry entry is the vehicle's facts and wins over the vehicle:
    # block (S03.8), so the fuel goes on a tmp copy of the tracked registry.
    registry = load_yaml(Path("profiles.yaml").read_text())
    for entry in registry["profiles"]:
        entry.setdefault("parameters", {})["fuel_type"] = fuel_type
    registry_path = tmp_path / "profiles.yaml"
    registry_path.write_text(yaml.safe_dump(registry, sort_keys=False))
    profiles = raw.setdefault("profiles", {})
    profiles["registry_path"] = str(registry_path)
    profiles["registry_overlay_path"] = str(tmp_path / "control" / "profiles.yaml")
    return cast(dict[str, Any], raw)


def _scoped(raw: dict[str, Any]) -> CanaryConfig:
    config = CanaryConfig.model_validate(raw)
    return scope_config(config, resolve_profile_for_tooling(config, None))


def _capture(root: Path, name: str, *, rows: int = 6) -> Path:
    records: list[TelemetryRecord] = []
    for i in range(rows):
        ts = T0 + timedelta(seconds=i)
        for channel, value in (
            ("RPM", 3000.0 + 100 * i),
            ("ENGINE_LOAD", 90.0),
            ("FUEL_STATUS", 4.0),
            ("COMMANDED_EQUIV_RATIO", 1.2),
            ("O2_WR_LAMBDA_S1", 0.83),
            ("MAF", 120.0),
        ):
            records.append(
                TelemetryRecord(timestamp=ts, source="obd", channel=channel, value=value)
            )
    out = root / name
    write_records_to_parquet(records, out)
    return out


@pytest.fixture
def no_running_revision_leak(monkeypatch: pytest.MonkeyPatch) -> None:
    """``append_flash`` records the running revision in process state; undo it."""
    monkeypatch.setattr(tuning_profile, "_running_tune_revision", None)
    monkeypatch.setattr(tuning_profile, "_registry_calibration_id", None)


@pytest.mark.usefixtures("no_running_revision_leak")
def test_a_dashboard_flash_resolves_in_the_digest(tmp_path: Path) -> None:
    config = _scoped(_raw_config(tmp_path))
    append_flash(config, FLASH)
    assert active_registry_path(config) == operational_registry_path(config)
    assert active_registry_path(config) != source_registry_path(config)

    session = _capture(config.collection.storage_path, f"_capture-fuel-map-{STAMP}")
    digest = compute_tuning_digest(session, config)
    assert digest is not None
    assert digest.tune_revision == FLASH.revision


@pytest.mark.usefixtures("no_running_revision_leak")
def test_a_dashboard_flash_resolves_in_the_mcp_tuning_tools(tmp_path: Path) -> None:
    raw = _raw_config(tmp_path)
    config_path = tmp_path / "canary.yaml"
    config_path.write_text(yaml.safe_dump(raw))
    config = _scoped(raw)
    append_flash(config, FLASH)

    root = config.collection.storage_path
    a = _capture(root, f"_capture-fuel-map-{STAMP}")
    b = _capture(root, f"_capture-fuel-map-{LATER}")

    delivery = tuning_analysis.get_delivery_error(config_path, session=a.name, min_load=None)
    assert delivery["ok"], delivery
    assert delivery["tune_revision"] == FLASH.revision
    # R05.13: no min_load means the profile's WOT floor, echoed with its source.
    assert (delivery["min_load"], delivery["min_load_source"]) == (85.0, "profile")

    compared = tuning_analysis.compare_tuning_sessions(
        config_path, session=a.name, against=b.name, value="obd:MAF", axes=("obd:RPM",)
    )
    assert compared["ok"], compared
    assert tuple(compared["tune_revisions"]) == (FLASH.revision, FLASH.revision)


def test_stoichiometry_comes_from_the_configured_fuel(tmp_path: Path) -> None:
    diesel = _scoped(_raw_config(tmp_path, fuel_type="diesel"))
    assert TuningContext.from_config(diesel).afr_stoich == 14.5
    gasoline = _scoped(_raw_config(tmp_path))
    assert TuningContext.from_config(gasoline).afr_stoich == 14.7


def test_a_diesel_profiles_workbench_afr_uses_its_stoich(tmp_path: Path) -> None:
    """The dashboard's workbench and the serve path agree for a non-gasoline fuel."""
    from src.analysis.workbench import session_frame

    config = _scoped(_raw_config(tmp_path, fuel_type="diesel"))
    inputs = workbench_jobs.workbench_inputs(
        config, bindings_path=Path("data/bindings.yaml"), dids_path=Path("data/dids.yaml")
    )
    assert inputs.afr_stoich == 14.5
    session = _capture(config.collection.storage_path, STAMP)
    frame = session_frame(session, ["obd:RPM", "obd:COMMANDED_AFR"], inputs=inputs)
    afr = set(frame["values"]["obd:COMMANDED_AFR"])
    ratio = 1.2
    expected = (
        14.5 / ratio
        if inputs.calibration is not None
        and inputs.calibration.commanded_equiv_ratio_convention == "phi"
        else 14.5 * ratio
    )
    assert afr and all(value == pytest.approx(expected) for value in afr)


@pytest.mark.usefixtures("no_running_revision_leak")
def test_a_dashboard_flash_resolves_in_the_behavior_inventory(tmp_path: Path) -> None:
    """The B03.1 inventory records the running revision from the live registry."""
    from src.analysis.behavior_inventory import build_inventory

    config = _scoped(_raw_config(tmp_path))
    append_flash(config, FLASH)
    inventory = build_inventory(config, now=T0)
    assert inventory["calibration"]["current_revision"] == FLASH.revision
    assert inventory["calibration"]["registry_path"] == str(operational_registry_path(config))
