"""Dashboard profile editing (operator request 2026-10-10).

Parameters merge field by field over the tracked registry, a changed vehicle
fact needs new evidence, revert puts tracked values back, and a VIN edit can
neither break nor silently redirect the next start's profile resolution.
"""

from __future__ import annotations

import json
import threading
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest
import yaml
from fastapi.testclient import TestClient

from src.collection import bus_budget, tuning_profile, tx_gate, vin_read
from src.collection.can_obd_collector import run_can_obd_loop
from src.collection.obd_scheduler import CanObdSpec, DidSessionSpec
from src.collection.obd_scheduler.spec import _bus_budget_requests_per_second
from src.config import CanaryConfig, CanBusConfig, ProfilesConfig, VehicleParameters
from src.control.audit import ControlAuditLogger
from src.control.rom_backup import VehicleReading
from src.dashboard.api import create_app
from src.dashboard.routes.control import system as system_routes
from src.vehicle.profiles import (
    PARAMETER_FIELDS,
    ActiveVinMismatchError,
    DuplicateVinError,
    EvidenceRequiredError,
    NoTrackedProfileError,
    ProfileEditError,
    ProfileResolution,
    VehicleInfoProbe,
    VehicleProfile,
    load_registry,
    parameter_schema,
    pending_restart,
    profile_overrides,
    record_resolution,
    resolve_active_profile,
    resolve_profile_for_tooling,
    revert_profile,
    save_registry,
    scope_config,
    update_profile,
)
from src.vehicle.vin import InvalidVinError, inspect_vin, validate_vin
from src.yaml_load import load_yaml
from tests.capture_round_fakes import FakeClock, ScriptedEcu, install_clock, isotp_frames

REPO_ROOT = Path(__file__).resolve().parents[1]
#: The textbook check-digit example (49 CFR 565): position 9 is ``X``.
NA_VIN = "1M8GDM9AXKP042788"
#: Same VIN with a wrong check digit.
NA_VIN_BAD_CHECK = "1M8GDM9A1KP042788"
#: A European VIN whose position 9 is not a check digit.
EU_VIN = "WVWZZZ1JZXW000001"
OTHER_NA_VIN = "1HGCM82633A004352"

MASS_EVIDENCE = "scale ticket 2026-10-10, CAT scale, full fuel, no occupants"


@pytest.fixture(autouse=True)
def _no_resolution() -> Iterator[None]:
    record_resolution(None)
    yield
    record_resolution(None)


def _registry(tmp_path: Path) -> ProfilesConfig:
    return ProfilesConfig(
        registry_path=tmp_path / "profiles.yaml",
        registry_overlay_path=tmp_path / "state" / "profiles.yaml",
    )


def _tracked_params(**overrides: Any) -> VehicleParameters:
    base: dict[str, Any] = {
        "mass_kg": 2184.4,
        "mass_evidence": "itemised estimate",
        "usual_payload_kg": 127.0,
        "final_drive": 4.08,
        "gearbox_ratios": (4.368, 2.518, 1.743, 1.283, 1.0, 0.769),
        "tire_loaded_circumference_m": 2.559,
        "driveline_evidence": "published ratios; GPS cruise",
    }
    return VehicleParameters(**{**base, **overrides})


def _write_tracked(registry: ProfilesConfig, *profiles: VehicleProfile) -> None:
    registry.registry_path.write_text(
        yaml.safe_dump({"schema_version": 1, "profiles": [p.to_dict() for p in profiles]})
    )


def _xterra(**params: Any) -> VehicleProfile:
    return VehicleProfile(
        slug="xterra",
        vin="",
        name="Xterra",
        make="Nissan",
        model="Xterra",
        year=2008,
        parameters=_tracked_params(**params),
    )


def _overlay(registry: ProfilesConfig) -> list[dict[str, Any]]:
    return list(load_yaml(registry.registry_overlay_path.read_text())["profiles"])


# ---------------------------------------------------------------------------
# Merge depth
# ---------------------------------------------------------------------------


def test_a_parameter_edit_stores_only_that_unit_in_the_overlay(tmp_path: Path) -> None:
    registry = _registry(tmp_path)
    _write_tracked(registry, _xterra())
    tracked_before = registry.registry_path.read_text()
    update_profile(
        registry,
        "xterra",
        {"parameters": {"mass_kg": 2230.0, "mass_evidence": MASS_EVIDENCE}},
    )
    assert registry.registry_path.read_text() == tracked_before
    assert _overlay(registry) == [
        {"slug": "xterra", "parameters": {"mass_kg": 2230.0, "mass_evidence": MASS_EVIDENCE}}
    ]


def test_a_later_laptop_edit_to_another_parameter_still_flows_after_a_dashboard_edit(
    tmp_path: Path,
) -> None:
    registry = _registry(tmp_path)
    _write_tracked(registry, _xterra())
    update_profile(
        registry,
        "xterra",
        {"parameters": {"mass_kg": 2230.0, "mass_evidence": MASS_EVIDENCE}},
    )
    # The laptop commits a new payload and a regeared axle.
    _write_tracked(
        registry,
        _xterra(usual_payload_kg=150.0, final_drive=4.56, driveline_evidence="regeared 4.56"),
    )
    (merged,) = load_registry(registry)
    assert merged.parameters.mass_kg == 2230.0  # the machine's edit stands
    assert merged.parameters.usual_payload_kg == 150.0  # the laptop's flows
    assert merged.parameters.final_drive == 4.56
    assert merged.parameters.driveline_evidence == "regeared 4.56"


def test_evidence_travels_with_its_fact_across_the_merge(tmp_path: Path) -> None:
    """A laptop rewording of mass_evidence must not land beside the machine's mass."""
    registry = _registry(tmp_path)
    _write_tracked(registry, _xterra())
    update_profile(
        registry,
        "xterra",
        {"parameters": {"mass_kg": 2230.0, "mass_evidence": MASS_EVIDENCE}},
    )
    _write_tracked(registry, _xterra(mass_kg=2190.0, mass_evidence="laptop: re-itemised"))
    (merged,) = load_registry(registry)
    assert (merged.parameters.mass_kg, merged.parameters.mass_evidence) == (
        2230.0,
        MASS_EVIDENCE,
    )


def test_a_legacy_whole_parameters_block_still_merges(tmp_path: Path) -> None:
    """An overlay written before field-wise storage carried every parameter."""
    registry = _registry(tmp_path)
    _write_tracked(registry, _xterra())
    registry.registry_overlay_path.parent.mkdir(parents=True)
    registry.registry_overlay_path.write_text(
        yaml.safe_dump(
            {
                "schema_version": 1,
                "profiles": [
                    {
                        "slug": "xterra",
                        "parameters": _tracked_params(usual_payload_kg=90.0).model_dump(),
                    }
                ],
            }
        )
    )
    (merged,) = load_registry(registry)
    assert merged.parameters.usual_payload_kg == 90.0


# ---------------------------------------------------------------------------
# Validation and evidence
# ---------------------------------------------------------------------------


def test_changing_mass_without_new_evidence_is_refused_by_name(tmp_path: Path) -> None:
    registry = _registry(tmp_path)
    _write_tracked(registry, _xterra())
    with pytest.raises(EvidenceRequiredError) as exc:
        update_profile(registry, "xterra", {"parameters": {"mass_kg": 2230.0}})
    assert exc.value.field == "mass_evidence"
    assert "mass_kg" in str(exc.value)
    assert "scale ticket" in str(exc.value)
    assert not registry.registry_overlay_path.exists()


@pytest.mark.parametrize(
    "reused",
    [
        "itemised estimate",
        "itemised estimate ",
        "  itemised   estimate",
        "Itemised Estimate",
        "ITEMISED\testimate\n",
        "   ",
    ],
)
def test_reusing_the_old_evidence_text_is_not_new_evidence(tmp_path: Path, reused: str) -> None:
    """Whitespace and case do not make evidence new (verifier, 2026-10-10)."""
    registry = _registry(tmp_path)
    _write_tracked(registry, _xterra())
    with pytest.raises(EvidenceRequiredError):
        update_profile(
            registry,
            "xterra",
            {"parameters": {"mass_kg": 2230.0, "mass_evidence": reused}},
        )


@pytest.mark.parametrize(
    "change",
    [
        {"final_drive": 4.56},
        {"gearbox_ratios": [4.0, 2.5, 1.7, 1.3, 1.0, 0.8]},
        {"tire_loaded_circumference_m": 2.6},
    ],
)
def test_changing_a_driveline_fact_needs_driveline_evidence(
    tmp_path: Path, change: dict[str, Any]
) -> None:
    registry = _registry(tmp_path)
    _write_tracked(registry, _xterra())
    with pytest.raises(EvidenceRequiredError) as exc:
        update_profile(registry, "xterra", {"parameters": change})
    assert exc.value.field == "driveline_evidence"
    edit = update_profile(
        registry,
        "xterra",
        {"parameters": {**change, "driveline_evidence": "measured 2026-10-10"}},
    )
    assert any(name.startswith("parameters.") for name in edit.changed)


def test_unrelated_parameters_need_no_evidence(tmp_path: Path) -> None:
    registry = _registry(tmp_path)
    _write_tracked(registry, _xterra())
    edit = update_profile(registry, "xterra", {"parameters": {"usual_payload_kg": 95.0}})
    assert edit.changed == ("parameters.usual_payload_kg",)


@pytest.mark.parametrize(
    ("change", "field"),
    [
        ({"final_drive": -1.0, "driveline_evidence": "x"}, "parameters.final_drive"),
        ({"fuel_type": "kerosene"}, "parameters.fuel_type"),
        ({"warp_factor": 9}, "parameters.warp_factor"),
        ({"usual_payload_kg": -5.0}, "parameters.usual_payload_kg"),
    ],
)
def test_out_of_range_parameters_are_refused_naming_the_field(
    tmp_path: Path, change: dict[str, Any], field: str
) -> None:
    registry = _registry(tmp_path)
    _write_tracked(registry, _xterra())
    with pytest.raises(ProfileEditError) as exc:
        update_profile(registry, "xterra", {"parameters": change})
    assert exc.value.field == field
    assert field in str(exc.value)


def test_identity_fields_are_validated(tmp_path: Path) -> None:
    registry = _registry(tmp_path)
    _write_tracked(registry, _xterra())
    with pytest.raises(ProfileEditError, match="year"):
        update_profile(registry, "xterra", {"year": 1700})
    edit = update_profile(registry, "xterra", {"make": " Nissan ", "model": "Xterra Pro-4X"})
    assert edit.profile.model == "Xterra Pro-4X"
    assert edit.changed == ("model",)


def test_every_vehicle_parameter_has_a_display_entry() -> None:
    assert set(PARAMETER_FIELDS) == set(VehicleParameters.model_fields)
    schema = {row["name"]: row for row in parameter_schema()}
    assert schema["mass_kg"]["unit"] == "kg"
    assert schema["mass_kg"]["evidence_field"] == "mass_evidence"
    assert schema["fuel_type"]["choices"] == ["gasoline", "diesel", "e85"]


# ---------------------------------------------------------------------------
# Revert
# ---------------------------------------------------------------------------


def test_revert_one_parameter_restores_its_unit(tmp_path: Path) -> None:
    registry = _registry(tmp_path)
    _write_tracked(registry, _xterra())
    update_profile(
        registry,
        "xterra",
        {
            "name": "The Truck",
            "parameters": {
                "mass_kg": 2230.0,
                "mass_evidence": MASS_EVIDENCE,
                "usual_payload_kg": 95.0,
            },
        },
    )
    assert set(profile_overrides(registry)["xterra"]["fields"]) == {
        "name",
        "parameters.mass_kg",
        "parameters.mass_evidence",
        "parameters.usual_payload_kg",
    }
    edit = revert_profile(registry, "xterra", ["parameters.mass_kg"])
    assert set(edit.changed) == {"parameters.mass_kg", "parameters.mass_evidence"}
    (merged,) = load_registry(registry)
    assert merged.parameters.mass_kg == 2184.4
    assert merged.parameters.mass_evidence == "itemised estimate"
    assert merged.parameters.usual_payload_kg == 95.0
    assert merged.name == "The Truck"


def test_revert_all_keeps_what_the_machine_read_off_the_vehicle(tmp_path: Path) -> None:
    registry = _registry(tmp_path)
    _write_tracked(registry, _xterra())
    profiles = load_registry(registry)
    profiles[0].calibration_id = "CAL-1"
    save_registry(registry, profiles)
    update_profile(
        registry,
        "xterra",
        {"name": "The Truck", "vin": NA_VIN, "parameters": {"usual_payload_kg": 95.0}},
    )
    revert_profile(registry, "xterra")
    (merged,) = load_registry(registry)
    assert merged.name == "Xterra"
    assert merged.parameters.usual_payload_kg == 127.0
    assert (merged.vin, merged.calibration_id) == (NA_VIN, "CAL-1")
    assert set(profile_overrides(registry)["xterra"]["fields"]) == {"vin", "calibration_id"}


def test_revert_refuses_the_vin_and_a_machine_only_profile(tmp_path: Path) -> None:
    registry = _registry(tmp_path)
    _write_tracked(registry, _xterra())
    with pytest.raises(ProfileEditError, match="cannot be reverted"):
        revert_profile(registry, "xterra", ["vin"])
    profiles = load_registry(registry)
    profiles.append(VehicleProfile(slug="vin999", vin=OTHER_NA_VIN))
    save_registry(registry, profiles)
    with pytest.raises(NoTrackedProfileError, match="only on this machine"):
        revert_profile(registry, "vin999")


# ---------------------------------------------------------------------------
# VIN validation
# ---------------------------------------------------------------------------


def test_check_digit_passes_on_a_valid_vin() -> None:
    check = validate_vin(NA_VIN.lower())
    assert (check.vin, check.check_digit, check.warning) == (NA_VIN, "pass", None)


def test_a_north_american_check_digit_failure_is_refused() -> None:
    check, _problem = inspect_vin(NA_VIN_BAD_CHECK)
    assert check is not None and check.check_digit == "fail"
    with pytest.raises(InvalidVinError, match="check digit"):
        validate_vin(NA_VIN_BAD_CHECK)


def test_a_non_north_american_check_digit_failure_only_warns() -> None:
    check = validate_vin(EU_VIN)
    assert check.check_digit == "fail"
    assert check.warning is not None and "outside North America" in check.warning


@pytest.mark.parametrize(
    ("text", "rule"),
    [
        ("1M8GDM9AXKP04278", "17 characters"),
        ("1M8GDM9AXKP04278O", "I, O or Q"),
        ("1M8GDM9AXKP0427-8", "letters and digits"),
    ],
)
def test_malformed_vins_are_refused_by_rule(text: str, rule: str) -> None:
    with pytest.raises(InvalidVinError, match=rule):
        validate_vin(text)


def test_a_vin_already_on_another_profile_is_refused_by_name(tmp_path: Path) -> None:
    registry = _registry(tmp_path)
    save_registry(
        registry,
        [
            VehicleProfile(slug="work-truck", vin=NA_VIN, name="Work Truck"),
            VehicleProfile(slug="xterra", vin=""),
        ],
    )
    with pytest.raises(DuplicateVinError, match="work-truck.*Work Truck"):
        update_profile(registry, "xterra", {"vin": NA_VIN})


def test_a_bad_vin_edit_is_refused_and_writes_nothing(tmp_path: Path) -> None:
    registry = _registry(tmp_path)
    _write_tracked(registry, _xterra())
    with pytest.raises(ProfileEditError, match="check digit"):
        update_profile(registry, "xterra", {"vin": NA_VIN_BAD_CHECK})
    assert not registry.registry_overlay_path.exists()
    edit = update_profile(registry, "xterra", {"vin": EU_VIN})
    assert edit.warnings and "outside North America" in edit.warnings[0]


# ---------------------------------------------------------------------------
# VIN edits and the next start's resolution
# ---------------------------------------------------------------------------


def _config(tmp_path: Path) -> CanaryConfig:
    config = CanaryConfig.from_yaml(REPO_ROOT / "canary.example.yaml")
    return config.model_copy(
        update={
            "collection": config.collection.model_copy(update={"obd_source": "canable"}),
            "profiles": _registry(tmp_path),
        }
    )


def _probe(vin: str | None):  # type: ignore[no-untyped-def]
    def probe(_config: CanaryConfig) -> VehicleInfoProbe:
        return VehicleInfoProbe(vin=vin, calibration_id=None, cvn=None)

    return probe


def test_the_active_vin_cannot_move_off_the_vehicle_it_was_read_from(tmp_path: Path) -> None:
    config = _config(tmp_path)
    save_registry(config.profiles, [VehicleProfile(slug="truck", vin=NA_VIN, name="Truck")])
    active = resolve_active_profile(config, probe=_probe(NA_VIN))
    assert active.slug == "truck"
    with pytest.raises(ActiveVinMismatchError, match=f"read VIN {NA_VIN}"):
        update_profile(config.profiles, "truck", {"vin": OTHER_NA_VIN}, active_slug="truck")
    with pytest.raises(ActiveVinMismatchError):
        update_profile(config.profiles, "truck", {"vin": ""}, active_slug="truck")
    # Nothing changed, so the next start resolves the same profile and creates none.
    again = resolve_active_profile(config, probe=_probe(NA_VIN))
    assert again.slug == "truck"
    assert [p.slug for p in load_registry(config.profiles)] == ["truck"]


def test_a_vin_typed_while_none_was_read_selects_the_profile_at_the_next_start(
    tmp_path: Path,
) -> None:
    config = _config(tmp_path)
    active = resolve_active_profile(config, probe=_probe(None))
    slug = active.slug
    edit = update_profile(config.profiles, slug, {"vin": NA_VIN}, active_slug=slug)
    assert edit.vin_resolution is not None and "only if the vehicle reports" in edit.vin_resolution
    # This process keeps running on the profile it resolved; only a start re-resolves.
    assert resolve_active_profile(config, probe=_probe(NA_VIN)).slug == slug
    assert [p.slug for p in load_registry(config.profiles)] == [slug]


def test_a_typed_vin_the_vehicle_does_not_report_registers_that_vehicle_at_the_next_start(
    tmp_path: Path,
) -> None:
    """What the UI warns about: the typed VIN keys resolution, so a vehicle
    reporting another VIN gets its own (new) profile and this one is left."""
    config = _config(tmp_path)
    slug = resolve_active_profile(config, probe=_probe(None)).slug
    update_profile(config.profiles, slug, {"vin": NA_VIN}, active_slug=slug)
    nxt = resolve_active_profile(config, probe=_probe(OTHER_NA_VIN))
    assert nxt.slug != slug
    assert {p.slug for p in load_registry(config.profiles)} == {slug, nxt.slug}


def test_giving_another_profile_the_read_vin_is_a_duplicate_while_the_active_holds_it(
    tmp_path: Path,
) -> None:
    config = _config(tmp_path)
    save_registry(
        config.profiles,
        [VehicleProfile(slug="truck", vin=NA_VIN), VehicleProfile(slug="spare", vin="")],
    )
    resolve_active_profile(config, probe=_probe(NA_VIN))
    with pytest.raises(DuplicateVinError):
        update_profile(config.profiles, "spare", {"vin": NA_VIN}, active_slug="truck")


def test_a_start_without_a_vin_runs_on_the_registry_parameters(tmp_path: Path) -> None:
    """The fallback profile is the registry's entry, not the vehicle: block's
    defaults, so a dashboard edit reaches a key-off start too."""
    config = _config(tmp_path)
    _write_tracked(
        config.profiles,
        VehicleProfile(
            slug="nissan-xterra-2008",
            vin="",
            name="Xterra",
            make="Nissan",
            model="Xterra",
            year=2008,
            parameters=_tracked_params(),
        ),
    )
    update_profile(
        config.profiles,
        "nissan-xterra-2008",
        {"parameters": {"mass_kg": 2230.0, "mass_evidence": MASS_EVIDENCE}},
    )
    profile = resolve_active_profile(config, probe=_probe(None))
    scoped = scope_config(config, profile)
    assert scoped.vehicle.parameters.mass_kg == 2230.0
    assert scoped.vehicle.parameters.final_drive == 4.08
    assert resolve_profile_for_tooling(config, None).parameters.mass_kg == 2230.0


def test_pending_restart_names_what_the_daemon_does_not_run_yet(tmp_path: Path) -> None:
    config = _config(tmp_path)
    _write_tracked(config.profiles, _xterra())
    (profile,) = load_registry(config.profiles)
    scoped = scope_config(config, profile)
    assert pending_restart(scoped.vehicle, profile) == []
    edit = update_profile(
        config.profiles,
        "xterra",
        {"parameters": {"mass_kg": 2230.0, "mass_evidence": MASS_EVIDENCE}},
    )
    assert pending_restart(scoped.vehicle, edit.profile) == [
        "parameters.mass_kg",
        "parameters.mass_evidence",
    ]


# ---------------------------------------------------------------------------
# HTTP
# ---------------------------------------------------------------------------


def _api(tmp_path: Path) -> tuple[TestClient, CanaryConfig, Path]:
    config = _config(tmp_path)
    _write_tracked(config.profiles, _xterra())
    (profile,) = load_registry(config.profiles)
    scoped = scope_config(config, profile)
    scoped = scoped.model_copy(
        update={
            "collection": scoped.collection.model_copy(
                update={"storage_path": tmp_path / "data" / "xterra"}
            )
        }
    )
    audit = tmp_path / "audit.jsonl"
    app = create_app(
        scoped,
        anomalies_dir=tmp_path / "anomalies",
        control_token="secret",
        audit_logger=ControlAuditLogger(audit),
    )
    return TestClient(app), scoped, audit


AUTH = {"Authorization": "Bearer secret"}


def _audit(path: Path) -> list[dict[str, Any]]:
    if not path.exists():
        return []
    return [json.loads(line) for line in path.read_text().splitlines()]


def test_get_profiles_reports_overrides_units_and_what_is_in_use(tmp_path: Path) -> None:
    client, _config_, _audit_path = _api(tmp_path)
    body = client.get("/control/profiles", headers=AUTH).json()
    assert body["active_slug"] == "xterra"
    assert body["in_use"]["parameters"]["mass_kg"] == 2184.4
    assert body["pending_restart"] == []
    assert body["profiles"][0]["tracked"] is True
    assert body["profiles"][0]["overrides"] == []
    assert {row["name"] for row in body["parameter_fields"]} == set(PARAMETER_FIELDS)


def test_patch_saves_audits_and_says_a_restart_is_needed(tmp_path: Path) -> None:
    client, config, audit = _api(tmp_path)
    resp = client.patch(
        "/control/profiles/xterra",
        json={"parameters": {"mass_kg": 2230.0, "mass_evidence": MASS_EVIDENCE}},
        headers=AUTH,
    )
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["restart_required"] is True
    assert body["pending_restart"] == ["parameters.mass_kg", "parameters.mass_evidence"]
    assert "Restart the daemon" in body["applies"]
    assert [e["action"] for e in _audit(audit)] == ["profile.update"]
    listing = client.get("/control/profiles", headers=AUTH).json()
    assert listing["profiles"][0]["overrides"] == [
        "parameters.mass_kg",
        "parameters.mass_evidence",
    ]
    # The running config is untouched: the daemon still runs the old mass.
    assert listing["in_use"]["parameters"]["mass_kg"] == 2184.4
    assert config.vehicle.parameters.mass_kg == 2184.4


def test_patch_refusals_name_the_field(tmp_path: Path) -> None:
    client, _config_, audit = _api(tmp_path)
    resp = client.patch(
        "/control/profiles/xterra", json={"parameters": {"mass_kg": 2230.0}}, headers=AUTH
    )
    assert resp.status_code == 422
    assert "mass_evidence" in resp.json()["detail"]
    resp = client.patch("/control/profiles/xterra", json={"vin": NA_VIN_BAD_CHECK}, headers=AUTH)
    assert resp.status_code == 422
    assert "check digit" in resp.json()["detail"]
    assert client.patch("/control/profiles/nope", json={}, headers=AUTH).status_code == 404
    assert _audit(audit) == []


def test_revert_route_puts_the_tracked_value_back(tmp_path: Path) -> None:
    client, _config_, audit = _api(tmp_path)
    client.patch(
        "/control/profiles/xterra", json={"parameters": {"usual_payload_kg": 95.0}}, headers=AUTH
    )
    resp = client.post("/control/profiles/xterra/revert", json={}, headers=AUTH)
    assert resp.status_code == 200, resp.text
    assert resp.json()["profile"]["parameters"]["usual_payload_kg"] == 127.0
    assert resp.json()["restart_required"] is False
    assert [e["action"] for e in _audit(audit)] == ["profile.update", "profile.revert"]


# ---------------------------------------------------------------------------
# Reading the VIN through the collector
# ---------------------------------------------------------------------------


def _live(monkeypatch: pytest.MonkeyPatch, state: str) -> None:
    def reading(*_args: Any, **_kwargs: Any) -> VehicleReading:
        return VehicleReading(state=state, rpm=0.0, broadcast_rpm=None, obd_freshness_seconds=0.5)

    monkeypatch.setattr(system_routes, "vehicle_reading", reading)


def _serve_once(payload: bytes | None, nrc: int | None = None) -> threading.Thread:
    """Stand in for the collector loop: take the next ticket and answer it."""

    def serve() -> None:
        for _ in range(500):
            ticket = vin_read.take()
            if ticket is not None:
                ticket.fulfil(vin_read.outcome_from_reply(payload, nrc))
                return
            threading.Event().wait(0.01)

    thread = threading.Thread(target=serve, daemon=True)
    thread.start()
    return thread


def _vin_payload(vin: str) -> bytes:
    return bytes([0x01]) + vin.encode()


def test_read_vin_returns_the_vin_with_its_check_and_never_saves_it(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    client, config, audit = _api(tmp_path)
    _live(monkeypatch, "ignition_only")
    _serve_once(_vin_payload(NA_VIN))
    body = client.post("/control/vin/read", headers=AUTH).json()["vin_read"]
    assert body["ok"] is True
    assert body["vin"] == NA_VIN
    assert body["check"]["check_digit"] == "pass"
    assert body["held_by"] is None
    assert load_registry(config.profiles)[0].vin == ""  # confirmed in the form, not here
    assert [(e["action"], e["result"]) for e in _audit(audit)] == [("vin.read", "ok")]


@pytest.mark.parametrize(
    ("setup", "refusal", "words"),
    [
        ("passive", "passive_armed", "passive armed"),
        ("ecm", "ecm_held", "another tool holds the ECM"),
        ("parked", "key_off", "key off"),
        ("bench", "key_off", "no vehicle on the OBD port"),
        ("replay", "replay", "replay"),
        ("paused", "capture_paused", "the capture is paused"),
    ],
)
def test_read_vin_refuses_by_name_before_sending(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    setup: str,
    refusal: str,
    words: str,
) -> None:
    client, _config_, audit = _api(tmp_path)
    _live(monkeypatch, setup if setup in ("parked", "bench", "replay") else "ignition_only")
    if setup == "passive":
        tx_gate.set_passive(True, source="test")
    if setup == "ecm":
        tx_gate.claim_ecm_session("rom_backup")
    if setup == "paused":
        tuning_profile.set_paused(True)
    body = client.post("/control/vin/read", headers=AUTH).json()["vin_read"]
    assert (body["ok"], body["refusal"]) == (False, refusal)
    assert words in body["detail"]
    assert vin_read.take() is None  # nothing was queued for the bus
    assert _audit(audit)[-1]["result"] == refusal


@pytest.mark.parametrize(
    ("payload", "nrc", "refusal"),
    [
        (None, None, "no_answer"),
        (None, 0x12, "refused"),
        (b"\x01SHORT", None, "implausible"),
    ],
)
def test_read_vin_reports_which_kind_of_empty(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    payload: bytes | None,
    nrc: int | None,
    refusal: str,
) -> None:
    client, _config_, _audit_path = _api(tmp_path)
    _live(monkeypatch, "ignition_only")
    _serve_once(payload, nrc)
    body = client.post("/control/vin/read", headers=AUTH).json()["vin_read"]
    assert (body["ok"], body["refusal"]) == (False, refusal)


def test_read_vin_without_a_polling_collector_says_so(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    client, _config_, _audit_path = _api(tmp_path)
    _live(monkeypatch, "ignition_only")
    monkeypatch.setattr(system_routes, "VIN_PICKUP_SECONDS", 0.05)
    body = client.post("/control/vin/read", headers=AUTH).json()["vin_read"]
    assert body["refusal"] == "not_polling"
    assert vin_read.take() is None  # the unclaimed ticket was withdrawn


def test_read_vin_names_the_profile_already_holding_it(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    client, config, _audit_path = _api(tmp_path)
    profiles = load_registry(config.profiles)
    profiles.append(VehicleProfile(slug="work-truck", vin=NA_VIN))
    save_registry(config.profiles, profiles)
    _live(monkeypatch, "engine_running")
    _serve_once(_vin_payload(NA_VIN))
    body = client.post("/control/vin/read", headers=AUTH).json()["vin_read"]
    assert body["held_by"] == "work-truck"


class _VinEcu(ScriptedEcu):
    """Answers Mode 09 InfoType 02 with a multi-frame VIN."""

    def __init__(self, clock: FakeClock, vin: str) -> None:
        super().__init__(clock)
        self.vin = vin
        self.vin_requests = 0

    def send(self, msg, timeout=None):  # type: ignore[no-untyped-def]
        data = bytes(msg.data)
        if len(data) >= 3 and data[1] == 0x09 and data[2] == 0x02:
            now = self.clock.monotonic()
            self.sent.append((now, msg))
            self.vin_requests += 1
            self._queue(isotp_frames(bytes([0x49, 0x02, 0x01]) + self.vin.encode()), now)
            return
        super().send(msg, timeout)


def test_the_collector_loop_reads_the_vin_on_its_own_bus(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    clock = FakeClock()
    install_clock(monkeypatch, clock)
    bus = _VinEcu(clock, NA_VIN)
    spec = CanObdSpec(
        can=CanBusConfig(interface="virtual", channel="test", bitrate=500000),
        poll_rate_hz=4.0,
        pids=("RPM", "SPEED"),
        batch_max_pids=1,
        priority_channels=(),
        dtc_poll_interval_seconds=0.0,
        service_poll_interval_seconds=0.0,
        request_timeout_seconds=0.2,
    )
    ticket = vin_read.submit(0.5)
    run_can_obd_loop(
        bus,  # type: ignore[arg-type]
        spec,
        on_frame=lambda _m: None,
        on_reading=lambda _r: None,
        stop_event=threading.Event(),
        deadline_monotonic=clock.monotonic() + 1.0,
    )
    outcome = ticket.wait(0)
    assert outcome is not None and outcome.vin == NA_VIN
    assert bus.vin_requests == 1
    # Mode 01 polling carried on around it.
    assert bus.mode01_requests()


def _loop_spec(**extra: Any) -> CanObdSpec:
    base: dict[str, Any] = {
        "can": CanBusConfig(interface="virtual", channel="test", bitrate=500000),
        "poll_rate_hz": 4.0,
        "pids": ("RPM", "SPEED"),
        "batch_max_pids": 1,
        "priority_channels": (),
        "dtc_poll_interval_seconds": 0.0,
        "service_poll_interval_seconds": 0.0,
        "request_timeout_seconds": 0.2,
    }
    return CanObdSpec(**{**base, **extra})


def _run_loop(clock: FakeClock, bus: ScriptedEcu, spec: CanObdSpec, seconds: float) -> None:
    run_can_obd_loop(
        bus,  # type: ignore[arg-type]
        spec,
        on_frame=lambda _m: None,
        on_reading=lambda _r: None,
        stop_event=threading.Event(),
        deadline_monotonic=clock.monotonic() + seconds,
    )


def _is_request(msg: Any) -> bool:
    """A tester request (not an ISO-TP flow-control frame)."""
    data = bytes(msg.data)
    return len(data) >= 2 and data[1] != 0x30 and data[0] >> 4 != 0x3


def test_the_vin_request_waits_for_its_own_budget_slot(monkeypatch: pytest.MonkeyPatch) -> None:
    """Verifier 2026-10-10: it went out 5 ms after a Mode 01 request against a
    31 ms slot. It must land at least one slot after the request before it."""
    clock = FakeClock()
    install_clock(monkeypatch, clock)
    bus = _VinEcu(clock, NA_VIN)
    spec = _loop_spec()
    slot = 1.0 / bus_budget.effective(_bus_budget_requests_per_second(spec))

    def submit_after_first_poll(msg: Any) -> None:
        if bytes(msg.data)[1] == 0x01 and not bus.vin_requests:
            vin_read.submit(0.5)

    bus.on_send.append(submit_after_first_poll)
    _run_loop(clock, bus, spec, 1.0)
    requests = [(at, bytes(m.data)) for at, m in bus.sent if _is_request(m)]
    index = next(i for i, (_at, d) in enumerate(requests) if d[1] == 0x09)
    assert index > 0
    gap = requests[index][0] - requests[index - 1][0]
    assert gap >= slot - 1e-9, f"VIN request {gap * 1000:.1f} ms after the last, slot {slot}"


def test_a_paused_capture_leaves_the_ticket_untaken(monkeypatch: pytest.MonkeyPatch) -> None:
    clock = FakeClock()
    install_clock(monkeypatch, clock)
    bus = _VinEcu(clock, NA_VIN)
    tuning_profile.set_paused(True)
    ticket = vin_read.submit(0.5)
    _run_loop(clock, bus, _loop_spec(), 0.5)
    assert ticket.taken is False
    assert bus.vin_requests == 0


class _SessionVinEcu(_VinEcu):
    """Session-gated: records whether the Mode 22 session was held when the
    VIN request arrived, and submits the ticket the moment a window opens."""

    def __init__(self, clock: FakeClock, vin: str) -> None:
        ScriptedEcu.__init__(self, clock, session_gated=True)
        self.vin = vin
        self.vin_requests = 0
        self.session_at_vin: list[int | None] = []
        self.ticket: vin_read.VinReadTicket | None = None
        self.submitted_at: float | None = None

    def send(self, msg, timeout=None):  # type: ignore[no-untyped-def]
        data = bytes(msg.data)
        if len(data) >= 3 and data[1] == 0x09 and data[2] == 0x02:
            self.session_at_vin.append(self.session)
        super().send(msg, timeout)
        if (
            self.ticket is None
            and len(data) >= 3
            and data[1] == 0x10
            and data[2] == 0xC0
            and self.session == 0xC0
        ):
            self.ticket = vin_read.submit(0.5)
            self.submitted_at = self.clock.monotonic()


def test_a_ticket_submitted_inside_a_session_window_waits_for_the_exit(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    clock = FakeClock()
    install_clock(monkeypatch, clock)
    bus = _SessionVinEcu(clock, NA_VIN)
    spec = _loop_spec(
        dids=("1101", "1208"),
        did_session=DidSessionSpec(
            session=0xC0,
            exit_session=0x81,
            exit_grace_seconds=0.05,
            keepalive_seconds=2.0,
            window_interval_seconds=0.25,
            window_max_seconds=1.0,
            lapse_seconds=5.0,
        ),
    )
    _run_loop(clock, bus, spec, 4.0)
    assert bus.ticket is not None and bus.submitted_at is not None, "no window ever opened"
    assert bus.vin_requests == 1
    assert bus.session_at_vin == [None]  # sent in the default session, never inside the window
    vin_at = next(at for at, m in bus.sent if bytes(m.data)[1:3] == b"\x09\x02")
    exits = [at for at, sub in bus.session_changes if sub == 0x81 and at > bus.submitted_at]
    assert exits and exits[0] < vin_at
    outcome = bus.ticket.wait(0)
    assert outcome is not None and outcome.vin == NA_VIN


def test_a_passive_loop_never_takes_the_ticket(monkeypatch: pytest.MonkeyPatch) -> None:
    clock = FakeClock()
    install_clock(monkeypatch, clock)
    bus = _VinEcu(clock, NA_VIN)
    spec = CanObdSpec(
        can=CanBusConfig(interface="virtual", channel="test", bitrate=500000),
        poll_rate_hz=4.0,
        pids=("RPM",),
        batch_max_pids=1,
        priority_channels=(),
        dtc_poll_interval_seconds=0.0,
        service_poll_interval_seconds=0.0,
        request_timeout_seconds=0.2,
    )
    tx_gate.set_passive(True, source="test")
    ticket = vin_read.submit(0.5)
    run_can_obd_loop(
        bus,  # type: ignore[arg-type]
        spec,
        on_frame=lambda _m: None,
        on_reading=lambda _r: None,
        stop_event=threading.Event(),
        deadline_monotonic=clock.monotonic() + 0.5,
    )
    assert ticket.taken is False
    assert bus.vin_requests == 0


def test_resolution_record_is_exposed(tmp_path: Path) -> None:
    record_resolution(ProfileResolution("xterra", "vin_matched", NA_VIN))
    client, _config_, _audit_path = _api(tmp_path)
    body = client.get("/control/profiles", headers=AUTH).json()
    assert body["resolution"] == {"slug": "xterra", "how": "vin_matched", "vin_read": NA_VIN}
