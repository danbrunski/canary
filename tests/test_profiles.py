"""Vehicle-profile resolution, registry I/O, and config scoping."""

from __future__ import annotations

from pathlib import Path

import pytest
import yaml
from fastapi.testclient import TestClient

import src.vehicle.profiles as profiles_mod
from src.collection.vehicle_probe import probe_vehicle_info
from src.config import CanaryConfig, DaemonConfig, ProfilesConfig, VehicleParameters
from src.dashboard.api import create_app
from src.paths import CONTROL_DIR
from src.vehicle.profiles import (
    VehicleInfoProbe,
    VehicleProfile,
    load_registry,
    resolve_active_profile,
    resolve_profile_for_tooling,
    save_registry,
    scope_config,
    slugify,
    update_profile_name,
)
from src.vehicle.vehicle_ledger import VehicleLedgerStore
from src.yaml_load import load_yaml

REPO_ROOT = Path(__file__).resolve().parents[1]
EXAMPLE_YAML = REPO_ROOT / "canary.example.yaml"


def _base_config(tmp_path: Path, *, obd_source: str = "disabled") -> CanaryConfig:
    """Example config with the VIN probe disabled and the registry in tmp."""
    config = CanaryConfig.from_yaml(EXAMPLE_YAML)
    return config.model_copy(
        update={
            "collection": config.collection.model_copy(update={"obd_source": obd_source}),
            "profiles": config.profiles.model_copy(
                update={
                    "registry_path": tmp_path / "profiles.yaml",
                    "registry_overlay_path": tmp_path / "profiles.overlay.yaml",
                }
            ),
        }
    )


def test_slugify_is_filesystem_safe():
    assert slugify("2008 Nissan Xterra!") == "2008-nissan-xterra"
    # Never a reserved leading underscore (excluded from training discovery).
    assert not slugify("__weird__").startswith("_")
    assert slugify("") == "vehicle"


def _registry(tmp_path: Path) -> ProfilesConfig:
    return ProfilesConfig(
        registry_path=tmp_path / "profiles.yaml",
        registry_overlay_path=tmp_path / "state" / "profiles.yaml",
    )


def test_registry_roundtrip(tmp_path: Path):
    registry = _registry(tmp_path)
    assert load_registry(registry) == []  # missing files → empty
    profiles = [
        VehicleProfile(slug="nissan-xterra-2008", vin="VIN123", name="2008 Xterra", year=2008),
        VehicleProfile(slug="vin999", vin="VIN999", name=""),
    ]
    save_registry(registry, profiles)
    loaded = load_registry(registry)
    assert [p.slug for p in loaded] == ["nissan-xterra-2008", "vin999"]
    assert loaded[0].name == "2008 Xterra"
    assert loaded[0].year == 2008


def test_update_profile_name(tmp_path: Path):
    registry = _registry(tmp_path)
    save_registry(registry, [VehicleProfile(slug="vin999", vin="VIN999", name="")])
    updated = update_profile_name(registry, "vin999", "  Daily Driver  ")
    assert updated.name == "Daily Driver"  # trimmed
    assert load_registry(registry)[0].name == "Daily Driver"
    with pytest.raises(KeyError):
        update_profile_name(registry, "missing", "x")


def _write_tracked(registry: ProfilesConfig, *profiles: VehicleProfile) -> None:
    registry.registry_path.write_text(
        yaml.safe_dump({"schema_version": 1, "profiles": [p.to_dict() for p in profiles]})
    )


def test_runtime_writes_never_touch_the_tracked_registry(tmp_path: Path):
    """S03.2: the Pi's rename lands in the overlay; the tracked file is untouched."""
    registry = _registry(tmp_path)
    _write_tracked(registry, VehicleProfile(slug="xterra", vin="", name="Xterra", year=2008))
    tracked_before = registry.registry_path.read_text()
    update_profile_name(registry, "xterra", "The Truck")
    assert registry.registry_path.read_text() == tracked_before
    overlay = load_yaml(registry.registry_overlay_path.read_text())
    assert overlay["profiles"] == [{"slug": "xterra", "name": "The Truck"}]


def test_a_later_laptop_edit_to_parameters_still_reaches_the_pi(tmp_path: Path):
    """The overlay stores only what this machine changed, so the tracked file's
    other fields keep flowing (operator decision 2026-10-04)."""
    registry = _registry(tmp_path)
    _write_tracked(registry, VehicleProfile(slug="xterra", vin="", name="Xterra", year=2008))
    update_profile_name(registry, "xterra", "The Truck")
    _write_tracked(  # the laptop commits a weighed mass
        registry,
        VehicleProfile(
            slug="xterra",
            vin="",
            name="Xterra",
            year=2008,
            parameters=VehicleParameters(mass_kg=2200.0),
        ),
    )
    (merged,) = load_registry(registry)
    assert merged.name == "The Truck"
    assert merged.parameters.mass_kg == 2200.0


def test_read_vin_noop_when_not_canable(tmp_path: Path):
    from src.collection.vehicle_probe import read_vin

    config = _base_config(tmp_path, obd_source="disabled")
    assert read_vin(config) is None  # never opens a bus


def test_read_vin_noop_on_virtual_bus(tmp_path: Path):
    from src.collection.vehicle_probe import read_vin

    config = _base_config(tmp_path, obd_source="canable")
    config = config.model_copy(
        update={
            "hardware": config.hardware.model_copy(
                update={"can": config.hardware.can.model_copy(update={"interface": "virtual"})}
            )
        }
    )
    assert read_vin(config) is None


def test_probe_vehicle_info_noop_when_not_canable(tmp_path: Path):
    from src.collection.vehicle_probe import probe_vehicle_info

    config = _base_config(tmp_path, obd_source="disabled")
    probe = probe_vehicle_info(config)
    assert probe == VehicleInfoProbe(vin=None, calibration_id=None, cvn=None)


def test_probe_vehicle_info_reads_vin_calibration_id_and_cvn(tmp_path: Path, monkeypatch):
    """Task 3.4 — one bus connection answers all three Mode 09 InfoTypes."""
    from src.collection.isotp import ObdResponse

    class _FakeClient:
        def __init__(self, _bus, *, timeout_seconds: float, on_frame: object = None) -> None:
            del timeout_seconds, on_frame

        def query_mode_09(self, info_type: int) -> ObdResponse | None:
            payloads = {
                0x02: b"1" + b"5N1AN08U08C123456",  # leading count byte + 17-char VIN
                0x04: b"CAL0001         ",
                0x06: bytes.fromhex("DEADBEEF"),
            }
            payload = payloads.get(info_type)
            return None if payload is None else ObdResponse(9, info_type, payload, 0x7E8)

    class _FakeBus:
        def shutdown(self) -> None:
            pass

    config = _base_config(tmp_path, obd_source="canable")
    monkeypatch.setattr("src.collection.can_bus.bus_factory", lambda _can: _FakeBus())
    monkeypatch.setattr("src.collection.can_obd_client.CanObdClient", _FakeClient)

    from src.collection.vehicle_probe import probe_vehicle_info

    probe = probe_vehicle_info(config)
    assert probe.vin == "5N1AN08U08C123456"
    assert probe.calibration_id == "CAL0001"
    assert probe.cvn == "DEADBEEF"


class _ScriptedEcu:
    """A CAN bus with one scripted engine ECU behind it (S01.4).

    ``answers`` maps a Mode 09 InfoType to its response: ``bytes`` is the
    payload after ``49 <infotype>`` (ISO-TP segmented when it doesn't fit a
    single frame, with consecutive frames released by the tester's flow
    control, as a real ECU does), an ``int`` is an NRC answered as
    ``7F 09 <nrc>``, and an absent key is silence. ``sent`` records every
    request frame, flow control excluded.
    """

    def __init__(self, answers: dict[int, bytes | int]) -> None:
        self.answers = answers
        self.sent: list[bytes] = []
        self._rx: list[bytes] = []
        self._pending_cf: list[bytes] = []

    def send(self, msg: object) -> None:
        data = bytes(msg.data)  # type: ignore[attr-defined]
        if data[0] >> 4 == 0x3:  # flow control: release the consecutive frames
            self._rx.extend(self._pending_cf)
            self._pending_cf = []
            return
        self.sent.append(data)
        if data[1] != 0x09:
            return
        answer = self.answers.get(data[2])
        if answer is None:
            return
        if isinstance(answer, int):
            self._rx.append(bytes([0x03, 0x7F, 0x09, answer, 0, 0, 0, 0]))
            return
        body = bytes([0x49, data[2]]) + answer
        if len(body) <= 7:
            self._rx.append(bytes([len(body)]) + body.ljust(7, b"\x00"))
            return
        self._rx.append(bytes([0x10 | (len(body) >> 8), len(body) & 0xFF]) + body[:6])
        rest = body[6:]
        for seq, start in enumerate(range(0, len(rest), 7), start=1):
            chunk = rest[start : start + 7]
            self._pending_cf.append(bytes([0x20 | (seq & 0xF)]) + chunk.ljust(7, b"\x00"))

    def recv(self, timeout: float = 0.0) -> object | None:
        import time

        import can

        if self._rx:
            return can.Message(arbitration_id=0x7E8, data=self._rx.pop(0), is_extended_id=False)
        time.sleep(min(timeout, 0.005))
        return None

    def shutdown(self) -> None:
        pass

    def mode_09_requests(self) -> list[int]:
        return [frame[2] for frame in self.sent if frame[1] == 0x09]


_VIN = b"5N1AN08U08C123456"
_ANSWERING_ECU: dict[int, bytes | int] = {
    0x02: b"\x01" + _VIN,
    0x04: b"\x01" + b"CAL0001".ljust(16, b"\x00"),
    0x06: b"\x01" + bytes.fromhex("0B4B7C60"),
}


def _probe_with(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, ecu: _ScriptedEcu, *, timeout: float = 0.3
) -> tuple[VehicleInfoProbe, float]:
    import time

    config = _base_config(tmp_path, obd_source="canable")
    config = config.model_copy(
        update={
            "profiles": config.profiles.model_copy(update={"vin_probe_timeout_seconds": timeout})
        }
    )
    monkeypatch.setattr("src.collection.can_bus.bus_factory", lambda _can: ecu)
    started = time.monotonic()
    probe = probe_vehicle_info(config)
    return probe, time.monotonic() - started


def test_probe_silent_ecu_costs_one_request_and_one_timeout(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    """S01.4: an ECU asleep at key-off answers nothing. The calibration ID and
    CVN come from the same module, so asking them too cost two more 2 s
    timeouts on every key-off boot (6 s on the 2026-09-22 cold trace)."""
    ecu = _ScriptedEcu({})
    with caplog.at_level("INFO", logger="src.collection.vehicle_probe"):
        probe, elapsed = _probe_with(tmp_path, monkeypatch, ecu, timeout=0.3)
    assert probe == VehicleInfoProbe(vin=None, calibration_id=None, cvn=None)
    assert ecu.mode_09_requests() == [0x02]
    assert elapsed < 2 * 0.3
    assert "skipped" in caplog.text


def test_probe_answering_ecu_reads_all_three(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    ecu = _ScriptedEcu(dict(_ANSWERING_ECU))
    probe, _ = _probe_with(tmp_path, monkeypatch, ecu)
    assert ecu.mode_09_requests() == [0x02, 0x04, 0x06]
    assert probe.vin == _VIN.decode()
    assert probe.calibration_id == "CAL0001"
    assert probe.cvn == "0B4B7C60"


def test_probe_vin_nrc_still_asks_the_others(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A refusal is an answer: the module is awake, so each InfoType stays
    independent (pre-2008 ECUs may refuse the VIN and still report a CVN)."""
    ecu = _ScriptedEcu({**_ANSWERING_ECU, 0x02: 0x12})
    probe, _ = _probe_with(tmp_path, monkeypatch, ecu)
    assert ecu.mode_09_requests() == [0x02, 0x04, 0x06]
    assert probe.vin is None
    assert probe.calibration_id == "CAL0001"
    assert probe.cvn == "0B4B7C60"


def test_probe_implausible_vin_still_asks_the_others(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    ecu = _ScriptedEcu({**_ANSWERING_ECU, 0x02: b"\x01NOTAVIN"})
    probe, _ = _probe_with(tmp_path, monkeypatch, ecu)
    assert ecu.mode_09_requests() == [0x02, 0x04, 0x06]
    assert probe.vin is None
    assert probe.cvn == "0B4B7C60"


def test_probe_partial_vin_answer_still_asks_the_others(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A first frame whose consecutive frames never arrive times out like
    silence at the client, but the module did answer: not skipped."""

    class _DropsConsecutiveFrames(_ScriptedEcu):
        def send(self, msg: object) -> None:
            super().send(msg)
            if bytes(msg.data)[0] >> 4 == 0x3:  # type: ignore[attr-defined]
                self._rx = [frame for frame in self._rx if frame[0] >> 4 != 0x2]

    ecu = _DropsConsecutiveFrames(dict(_ANSWERING_ECU))
    probe, _ = _probe_with(tmp_path, monkeypatch, ecu)
    assert ecu.mode_09_requests() == [0x02, 0x04, 0x06]
    assert probe.vin is None
    assert probe.cvn == "0B4B7C60"


def test_resolve_falls_back_to_default_and_registers(tmp_path: Path):
    config = _base_config(tmp_path)  # probe disabled → VIN is None
    profile = resolve_active_profile(config, probe=probe_vehicle_info)
    # Default profile derived from the vehicle: block, with a sensible name.
    assert profile.slug == slugify(
        f"{config.vehicle.make}-{config.vehicle.model}-{config.vehicle.year}"
    )
    assert profile.name  # default gets the "year make model" label
    # Persisted so the dashboard can list/rename it.
    registered = load_registry(config.profiles)
    assert any(p.slug == profile.slug for p in registered)


def _must_not_probe(_config: CanaryConfig) -> VehicleInfoProbe:
    """A probe for paths that must never open a bus: an override or disabled profiles.

    R09.5 made the probe a required argument of ``resolve_active_profile``; the
    old code patched ``probe_vehicle_info``, and passing the real probe here
    (a no-op with OBD disabled) would not notice a call.
    """
    pytest.fail("resolve_active_profile probed the bus on a path that must not")


def test_resolve_override_returns_registry_entry(tmp_path: Path):
    config = _base_config(tmp_path)
    save_registry(
        config.profiles,
        [VehicleProfile(slug="work-truck", vin="VINWORK", name="Work Truck")],
    )
    profile = resolve_active_profile(config, probe=_must_not_probe, override_slug="work-truck")
    assert profile.name == "Work Truck"


def test_resolve_override_unknown_slug_synthesizes(tmp_path: Path):
    config = _base_config(tmp_path)
    profile = resolve_active_profile(config, probe=_must_not_probe, override_slug="brand-new")
    assert profile.slug == "brand-new"
    assert any(p.slug == "brand-new" for p in load_registry(config.profiles))


def test_resolve_disabled_does_not_write(tmp_path: Path):
    config = _base_config(tmp_path)
    config = config.model_copy(
        update={"profiles": config.profiles.model_copy(update={"enabled": False})}
    )
    resolve_active_profile(config, probe=_must_not_probe)
    assert not config.profiles.registry_path.exists()


def _base_config_with_ledger(tmp_path: Path) -> CanaryConfig:
    config = _base_config(tmp_path)
    return config.model_copy(
        update={
            "daemon": config.daemon.model_copy(
                update={"vehicle_ledger": tmp_path / "vehicle_ledger.jsonl"}
            )
        }
    )


def test_resolve_persists_calibration_id_and_cvn_on_first_probe(tmp_path: Path):
    """Task 3.4 — a registered profile's Mode 09 calibration ID/CVN land in
    the registry alongside its VIN."""
    config = _base_config_with_ledger(tmp_path)
    save_registry(
        config.profiles,
        [VehicleProfile(slug="work-truck", vin="VINWORK", name="Work Truck")],
    )
    probe = VehicleInfoProbe(vin="VINWORK", calibration_id="CAL0001", cvn="CVN0001")
    profile = resolve_active_profile(config, probe=lambda _config: probe)
    assert profile.calibration_id == "CAL0001"
    assert profile.cvn == "CVN0001"
    reloaded = {p.slug: p for p in load_registry(config.profiles)}
    assert reloaded["work-truck"].calibration_id == "CAL0001"
    # First-ever read: a baseline, not a reflash — no ledger entry.
    assert (
        VehicleLedgerStore(
            profiles_mod._scope_file(config.daemon.vehicle_ledger, "work-truck")
        ).records()
        == []
    )


def test_resolve_flags_a_changed_calibration_id_as_a_corpus_boundary(tmp_path: Path):
    """Task 3.4 — a calibration ID that changed vs. the previously-stored
    one is an ECU reflash: a `corpus_boundary` vehicle-ledger event."""
    config = _base_config_with_ledger(tmp_path)
    save_registry(
        config.profiles,
        [
            VehicleProfile(
                slug="work-truck",
                vin="VINWORK",
                name="Work Truck",
                calibration_id="CAL0001",
                cvn="CVN0001",
            )
        ],
    )
    probe = VehicleInfoProbe(vin="VINWORK", calibration_id="CAL0002", cvn="CVN0002")
    profile = resolve_active_profile(config, probe=lambda _config: probe)
    assert profile.calibration_id == "CAL0002"

    ledger = VehicleLedgerStore(
        profiles_mod._scope_file(config.daemon.vehicle_ledger, "work-truck")
    )
    records = ledger.records()
    assert len(records) == 1
    assert records[0].corpus_boundary is True
    assert records[0].kind == "mod"
    assert "CAL0001" in records[0].title
    assert "CAL0002" in records[0].title


def test_resolve_unchanged_calibration_id_does_not_flag_a_boundary(tmp_path: Path):
    config = _base_config_with_ledger(tmp_path)
    save_registry(
        config.profiles,
        [
            VehicleProfile(
                slug="work-truck", vin="VINWORK", name="Work Truck", calibration_id="CAL0001"
            )
        ],
    )
    probe = VehicleInfoProbe(vin="VINWORK", calibration_id="CAL0001", cvn=None)
    resolve_active_profile(config, probe=lambda _config: probe)
    ledger = VehicleLedgerStore(
        profiles_mod._scope_file(config.daemon.vehicle_ledger, "work-truck")
    )
    assert ledger.records() == []


def test_resolve_for_tooling_never_probes(tmp_path: Path):
    # canable + non-virtual would normally probe; tooling must not open a bus.
    config = _base_config(tmp_path, obd_source="canable")
    profile = resolve_profile_for_tooling(config, None)
    assert profile.slug  # returns the default profile without touching the bus


def test_scope_config_nests_every_vehicle_path(tmp_path: Path):
    config = _base_config(tmp_path)
    config = config.model_copy(
        update={
            "collection": config.collection.model_copy(
                update={"storage_path": Path("/data/canary")}
            ),
            "daemon": config.daemon.model_copy(
                update={
                    "models_dir": Path("models"),
                    "anomalies_dir": Path("data/anomalies"),
                    "training_ledger": Path("data/control/training_runs.jsonl"),
                    "behavior_models": Path("data/behavior_models.yaml"),
                }
            ),
        }
    )
    profile = VehicleProfile(slug="xterra", vin="VIN42", name="X", make="Nissan", year=2008)
    scoped = scope_config(config, profile)

    # Directory fields gain the slug as a trailing component.
    assert scoped.collection.storage_path == Path("/data/canary/xterra")
    assert scoped.daemon.models_dir == Path("models/xterra")
    assert scoped.daemon.anomalies_dir == Path("data/anomalies/xterra")
    # File fields gain the slug as a parent of the filename.
    assert scoped.daemon.training_ledger == Path("data/control/xterra/training_runs.jsonl")
    assert scoped.daemon.behavior_models == Path("data/xterra/behavior_models.yaml")
    # Vehicle ledger (Task 3.2) scopes the same way as feedback_log.
    assert scoped.daemon.vehicle_ledger == CONTROL_DIR / "xterra" / "vehicle_ledger.jsonl"
    # Reference epoch (Task 4.3) scopes the same way.
    assert scoped.daemon.reference_epoch == CONTROL_DIR / "xterra" / "reference_epoch.json"
    # Trend-alert idempotency marker (Task 4.6) scopes the same way — a stat
    # name is generic (e.g. "idle_rpm_mean"), so without scoping a session id
    # recorded for one vehicle could suppress a legitimate alert for another.
    assert scoped.daemon.trend_alert_state == CONTROL_DIR / "xterra" / "trend_alert_state.json"
    # R05.15: calibration state is per vehicle, not one directory in jobs/.
    assert scoped.daemon.calibration_dir == CONTROL_DIR / "calibration" / "xterra"
    # Slug is recoverable as the storage path's final component.
    assert scoped.collection.storage_path.name == "xterra"
    # The vehicle block reflects the profile (VIN for reproducible manifests).
    assert scoped.vehicle.vin == "VIN42"
    assert scoped.vehicle.make == "Nissan"
    # The registry path itself is NOT scoped (it indexes all vehicles).
    assert scoped.profiles.registry_path == config.profiles.registry_path


def test_scope_config_covers_every_daemon_path_field(tmp_path: Path):
    """Every ``Path``-typed field on ``DaemonConfig`` must come out with the
    slug inserted as a path component. Exhaustive by construction (iterates
    ``DaemonConfig.model_fields`` rather than naming fields), so a future
    field added to ``DaemonConfig`` but forgotten in ``scope_config`` fails
    this test immediately instead of silently shipping unscoped (as
    ``trend_alert_state`` did)."""
    config = _base_config(tmp_path)
    profile = VehicleProfile(slug="xterra", vin="VIN42", name="X")
    scoped = scope_config(config, profile)

    # ``state_dir`` is the machine's state root, shared by every vehicle: the
    # per-vehicle paths are derived from it and scoped (S03.1).
    path_fields = [
        name
        for name, f in DaemonConfig.model_fields.items()
        if (f.annotation is Path or f.annotation == Path | None) and name != "state_dir"
    ]
    assert path_fields  # sanity: DaemonConfig still has Path fields to check
    for name in path_fields:
        original = getattr(config.daemon, name)
        result = getattr(scoped.daemon, name)
        assert result is not None, f"DaemonConfig.{name} left unresolved by scope_config"
        assert "xterra" in result.parts, (
            f"DaemonConfig.{name} was not profile-scoped by scope_config "
            f"(original={original!r}, scoped={result!r})"
        )


# ---------------------------------------------------------------------------
# Control endpoints
# ---------------------------------------------------------------------------


def _api(tmp_path: Path) -> tuple[TestClient, CanaryConfig]:
    config = _base_config(tmp_path)
    # The active slug is the scoped storage path's final component.
    storage = tmp_path / "data" / "nissan-xterra-2008"
    storage.mkdir(parents=True)
    config = config.model_copy(
        update={"collection": config.collection.model_copy(update={"storage_path": storage})}
    )
    save_registry(
        config.profiles,
        [
            VehicleProfile(slug="nissan-xterra-2008", vin="VINX", name=""),
            VehicleProfile(slug="work-truck", vin="VINW", name="Work Truck"),
        ],
    )
    app = create_app(config, anomalies_dir=tmp_path / "anomalies", control_token="secret")
    return TestClient(app), config


def _auth() -> dict[str, str]:
    return {"Authorization": "Bearer secret"}


def test_get_profiles_requires_auth_and_marks_active(tmp_path: Path):
    client, _config = _api(tmp_path)
    assert client.get("/control/profiles").status_code == 401
    body = client.get("/control/profiles", headers=_auth()).json()
    assert body["active_slug"] == "nissan-xterra-2008"
    assert body["count"] == 2
    assert {p["slug"] for p in body["profiles"]} == {"nissan-xterra-2008", "work-truck"}


def test_post_profile_name_persists(tmp_path: Path):
    client, config = _api(tmp_path)
    resp = client.post(
        "/control/profiles/nissan-xterra-2008/name",
        json={"name": "2008 Xterra"},
        headers=_auth(),
    )
    assert resp.status_code == 200
    assert resp.json()["profile"]["name"] == "2008 Xterra"
    persisted = {p.slug: p.name for p in load_registry(config.profiles)}
    assert persisted["nissan-xterra-2008"] == "2008 Xterra"


def test_post_profile_name_unknown_slug_404(tmp_path: Path):
    client, _config = _api(tmp_path)
    resp = client.post("/control/profiles/nope/name", json={"name": "x"}, headers=_auth())
    assert resp.status_code == 404
