"""Recovering the standalone brain (E04.6): stores reopen after a simulated
restart, a program change invalidates program-specific state and asks for RAM
re-verification, a calibration-only change selects new tables without
declaring RAM moved, transient ECU state is reported unknown, and every
mutable artifact is under ``state_dir``.

Time is injected (``now``) and the flash history is the real profile's, whose
2026-10-04 23:02 flash is a calibration-only change on one program.
"""

from __future__ import annotations

import asyncio
import shutil
from datetime import UTC, datetime
from pathlib import Path

import yaml

from src.analysis.ecu_application import load_application_model, program_sha256
from src.config import CanaryConfig
from src.control.brain_jobs import BrainJobStore
from src.control.brain_state import (
    IDENTITY_FILE,
    brain_dir,
    current_identity,
    reconcile_identity,
    recovery_report,
)
from src.control.rom_library import RomLibrary
from src.evidence import memory_ledger_for
from src.vehicle.profiles import VehicleProfile, scope_config
from src.vehicle.tune_revisions import load_tune_revisions
from src.yaml_load import load_yaml
from tests.configs import isolated_config
from tests.rom_fixtures import synthetic_image
from tests.test_brain_jobs import SESSION, scheduler, scoped_config, submit_twin, write_session
from tests.test_target_phi import PROFILE

BEFORE_FLASH = datetime(2026, 10, 4, 12, 0, tzinfo=UTC)  # ZS44A-20260920 in force
AFTER_FLASH = datetime(2026, 10, 5, 12, 0, tzinfo=UTC)  # ZS44A-20261004 in force
PROGRAM = "nissan-1ZS44A-uprev-osiris"


def copied_profile_config(tmp_path: Path) -> tuple[CanaryConfig, Path]:
    """A config whose profile files are a temporary copy, so a test can change
    the application model's program as a different ECU program would."""
    profile = tmp_path / "profile-data" / "nissan-xterra-2008"
    shutil.copytree(PROFILE, profile, ignore=shutil.ignore_patterns("_candidates", "_prior"))
    base = isolated_config(tmp_path, daemon={"afr_calibration": profile.parent / "afr.yaml"})
    return scope_config(base, VehicleProfile(slug=profile.name, vin="", name="Xterra")), profile


def test_the_brain_directory_is_per_vehicle_state_under_state_dir(tmp_path: Path) -> None:
    from tests.test_state_dir import _config

    config = scope_config(
        _config(state_dir=str(tmp_path)), VehicleProfile(slug="truck", vin="", name="Truck")
    )
    assert brain_dir(config) == tmp_path / "control/truck/brain"
    assert BrainJobStore.for_config(config).path.is_relative_to(tmp_path)


def test_identity_is_recorded_once_and_reopens_unchanged(tmp_path: Path) -> None:
    config = scoped_config(tmp_path)
    first = reconcile_identity(config, now=BEFORE_FLASH)
    assert first["change"] == "first_seen"
    assert first["identity"]["program_id"] == PROGRAM
    assert first["identity"]["calibration"] == "ZS44A-20260920"
    # Simulated restart: nothing but the files on disk carries over.
    again = reconcile_identity(scoped_config(tmp_path), now=BEFORE_FLASH)
    assert again["change"] == "unchanged"
    assert again["program_since"] == first["program_since"]
    lines = (brain_dir(config) / "identity_history.jsonl").read_text().splitlines()
    assert len(lines) == 1


def test_a_calibration_only_flash_selects_new_tables_and_keeps_ram(tmp_path: Path) -> None:
    config = scoped_config(tmp_path)
    first = reconcile_identity(config, now=BEFORE_FLASH)
    report = recovery_report(config, now=AFTER_FLASH, write=True)
    assert report["change"] == "calibration_changed"
    assert report["identity"]["calibration"] == "ZS44A-20261004"
    assert report["identity"]["program_id"] == PROGRAM
    # Predictions are re-based on the new calibration; the program's state is not.
    assert report["predictions"]["valid_since"] == AFTER_FLASH.isoformat()
    assert report["program_specific_state"]["valid_since"] == first["program_since"]
    assert report["program_specific_state"]["valid"] is True
    # A flash rewrites tables, never the program's RAM map.
    assert report["ram_addresses"]["status"] == "unchanged"
    assert report["tables"]["revision"] == "ZS44A-20261004"
    assert report["tables"]["source"] in ("rom_library", "afr_calibration", None)
    assert report["last_change"]["previous"]["calibration"] == "ZS44A-20260920"


def _library_flashes(tmp_path: Path) -> tuple[CanaryConfig, dict[str, str]]:
    """A copied profile whose flash history gains four revisions after the real
    ones: ``A`` (a program image held only in the W08 library, not listed in
    the model), ``B`` (A with one calibration byte changed), ``C`` (A with one
    program byte changed) and ``D`` (an image hash nobody holds).

    The synthetic image stands in for the program's bytes, so the copied
    model's program identity is set to A's hash over the model's own spans —
    the fixture's ground truth, not a simulated change. Nothing else in the
    model is edited, and none of the images is listed in it."""
    config, profile = copied_profile_config(tmp_path)
    model_path = profile / "ecu_application_model.yaml"
    model_yaml = load_yaml(model_path.read_text())
    application, _sha = load_application_model(profile)
    a = bytearray(synthetic_image())
    b = bytearray(a)
    b[0x9000] ^= 0xFF  # calibration block (0x8000-0x1FFFF), outside the program spans
    c = bytearray(a)
    c[0x30000] ^= 0xFF  # inside the program spans
    images = {"A": bytes(a), "B": bytes(b), "C": bytes(c)}
    model_yaml["program"]["identity"]["sha256"] = program_sha256(images["A"], application)
    model_path.write_text(yaml.safe_dump(model_yaml, sort_keys=False))
    library = RomLibrary(
        config.daemon.rom_library_dir,
        profile_dir=profile,
        registry=lambda: load_tune_revisions(profile / "tune_revisions.yaml"),
    )
    shas = {
        name: library.add_image(image, source="upload_plain")[0]["sha256"]
        for name, image in images.items()
    }
    shas["D"] = "d" * 64
    registry_path = profile / "tune_revisions.yaml"
    registry = load_yaml(registry_path.read_text())
    # Dated in 2099 so a synthetic revision sorts after every real flash: the
    # registry refuses an out-of-order list, and a date just past the newest real
    # flash broke these fixtures when ZS44A-20261110 was recorded.
    for day, name in enumerate("ABCD", start=1):
        registry["revisions"].append(
            {
                "revision": f"TEST-{name}",
                "calibration_id": "1ZS44A00000",
                "flashed_at": f"2099-11-0{day}T00:00:00Z",
                "image_sha256": shas[name],
            }
        )
    registry_path.write_text(yaml.safe_dump(registry, sort_keys=False))
    return config, shas


def _on(day: int) -> datetime:
    return datetime(2099, 11, day, 12, 0, tzinfo=UTC)


def test_a_library_image_of_the_same_program_is_a_calibration_change(tmp_path: Path) -> None:
    config, shas = _library_flashes(tmp_path)
    first = recovery_report(config, now=_on(1), write=True)
    assert first["identity"]["program_status"] == "known"  # hashed, not listed
    assert first["identity"]["program_id"] == PROGRAM
    report = recovery_report(config, now=_on(2), write=True)
    assert report["change"] == "calibration_changed"
    assert report["identity"]["image_sha256"] == shas["B"]
    assert report["identity"]["program_id"] == PROGRAM
    assert report["ram_addresses"]["status"] == "unchanged"
    assert report["program_specific_state"]["valid"] is True
    assert (
        report["program_specific_state"]["valid_since"]
        == first["program_specific_state"]["valid_since"]
    )
    assert report["predictions"]["valid_since"] == _on(2).isoformat()


def test_a_library_image_of_another_program_is_a_program_change(tmp_path: Path) -> None:
    config, _shas = _library_flashes(tmp_path)
    recovery_report(config, now=_on(2), write=True)
    report = recovery_report(config, now=_on(3), write=True)
    assert report["change"] == "program_changed"
    assert report["identity"]["program_status"] == "other_program"
    assert report["identity"]["program_id"] is None
    assert report["program_specific_state"]["valid"] is False
    assert report["program_specific_state"]["valid_since"] == _on(3).isoformat()
    assert report["ram_addresses"]["status"] == "reverify"
    assert "verify_memory_channels" in report["ram_addresses"]["detail"]


def test_only_an_image_nobody_holds_is_an_unknown_program(tmp_path: Path) -> None:
    config, _shas = _library_flashes(tmp_path)
    recovery_report(config, now=_on(1), write=True)
    report = recovery_report(config, now=_on(4), write=True)
    assert report["change"] == "program_unknown"
    assert report["identity"]["program_status"] == "unverified"
    assert report["ram_addresses"]["status"] == "unverified"
    # Reopening under the same unknown image records nothing new.
    again = recovery_report(config, now=_on(4), write=True)
    assert again["change"] == "unchanged"


def test_an_unrecorded_image_is_an_unknown_program_not_a_moved_one(tmp_path: Path) -> None:
    config = scoped_config(tmp_path)
    reconcile_identity(config, now=BEFORE_FLASH)
    # k105 (2026-07-31) has no recorded image: its program cannot be checked.
    report = recovery_report(config, now=datetime(2026, 8, 1, tzinfo=UTC), write=True)
    assert report["change"] == "program_unknown"
    assert report["identity"]["program_status"] == "no_image"
    assert report["identity"]["program_id"] is None
    assert report["program_specific_state"]["valid"] is False
    assert report["ram_addresses"]["status"] == "unverified"


def test_a_reader_never_records_a_change(tmp_path: Path) -> None:
    config = scoped_config(tmp_path)
    reconcile_identity(config, now=BEFORE_FLASH)
    stored = (brain_dir(config) / IDENTITY_FILE).read_text()
    report = recovery_report(config, now=AFTER_FLASH, write=False)
    assert report["change"] == "calibration_changed"
    assert (brain_dir(config) / IDENTITY_FILE).read_text() == stored


def test_a_recovered_brain_retrieves_the_same_evidence_and_says_transient_is_unknown(
    tmp_path: Path,
) -> None:
    config = scoped_config(tmp_path)
    write_session(config)
    submit_twin(config)
    done = asyncio.run(scheduler(config).step())
    assert done is not None and done.result is not None
    ids = done.result["evidence"]
    before = [e.record for e in memory_ledger_for(config).entries()]

    # Restart: new config object, new ledger and store instances, new scheduler.
    reopened = scoped_config(tmp_path)
    restarted = scheduler(reopened)
    assert restarted.recover() == []
    after = [e.record for e in memory_ledger_for(reopened).entries()]
    assert after == before
    assert [memory_ledger_for(reopened).resolve_evidence(i).record.id for i in ids] == ids
    report = recovery_report(reopened, process_started_at=AFTER_FLASH)
    assert report["transient"]["state"] == "unknown"
    assert report["transient"]["reason"] == "not_reconstructible"
    assert report["memory"]["records"] == len(before)
    job = BrainJobStore.for_config(reopened).get(done.job_id)
    assert job is not None and job.state == "completed"
    # The job's result is still the evaluation of that session.
    result = BrainJobStore.for_config(reopened).read_result(done.job_id)
    assert result is not None and result["session"] == SESSION


def test_current_identity_states_unknown_rather_than_guessing(tmp_path: Path) -> None:
    config = scoped_config(tmp_path)
    before_any = current_identity(config, now=datetime(1999, 1, 1, tzinfo=UTC))
    assert before_any.calibration is None and before_any.program_status == "no_revision"


def test_an_image_the_model_never_checked_is_unverified(tmp_path: Path) -> None:
    config, profile = copied_profile_config(tmp_path)
    model_path = profile / "ecu_application_model.yaml"
    model = load_yaml(model_path.read_text())
    model["program"]["images"] = [i for i in model["program"]["images"] if i["role"] == "decoded"]
    model_path.write_text(yaml.safe_dump(model, sort_keys=False))
    identity = current_identity(config, now=AFTER_FLASH)
    assert identity.program_status == "unverified" and identity.program_id is None
    assert identity.calibration == "ZS44A-20261004"
