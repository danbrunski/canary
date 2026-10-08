"""W02.2-W02.4: the ECU's own calibration id, its registry check, and readiness.

The read policy is data (``access.calibration_id`` in ``data/dids.yaml``), so
the loop tests pin both answers W02.1 can give: a read that needs the Mode-22
session rides a window the scheduler opened anyway, and a read recorded as
``default`` goes out bare at connect.
"""

from __future__ import annotations

import logging
import threading
from pathlib import Path

import pytest
import yaml
from pydantic import ValidationError

from src.collection import tuning_profile
from src.collection.calibration_id import (
    CalibrationIdSchedule,
    CalibrationIdSpec,
    decode_calibration_id,
    resolve_calibration_id_spec,
)
from src.collection.can_obd_collector import run_can_obd_loop
from src.collection.obd_scheduler import CanObdLoopStats, CanObdSpec, DidSessionSpec
from src.config import CanaryConfig
from src.control.calibration import (
    calibration_status,
    memory_verify_summary,
    record_verify_results,
)
from src.control.tuning_readiness import (
    build_tuning_readiness,
    calibration_entry,
    memory_channels_entry,
    ups_soc_entry,
    vehicle_state_entry,
)
from src.dashboard.status import build_status
from src.decode.dids import DidAccess, is_memory_checkable, load_did_map
from tests.capture_round_fakes import FakeClock, ScriptedEcu, install_clock, isotp_frames

REPO = Path(__file__).resolve().parents[1]
CAL_ID = "1ZS44A00000"
SESSION = 0xC0
EXIT = 0x81


# ---------------------------------------------------------------------------
# fixtures
# ---------------------------------------------------------------------------


@pytest.fixture(autouse=True)
def _reset_calibration_state() -> object:
    tuning_profile.reset_ecu_calibration()
    tuning_profile.configure_running_tune_revision(None)
    yield None
    tuning_profile.reset_ecu_calibration()
    tuning_profile.configure_running_tune_revision(None)


@pytest.fixture
def clock(monkeypatch: pytest.MonkeyPatch) -> FakeClock:
    fake = FakeClock()
    install_clock(monkeypatch, fake)
    return fake


class _IdentifyingEcu(ScriptedEcu):
    """A session-gated ECU that also answers ``21 10`` with an ASCII id.

    ``id_session`` says where it answers: ``"session"`` only while a
    non-default session is held (what the 2026-09-20 probe saw), ``"any"``
    in the default session too (the W02.1 answer that would allow a
    connect-time read), ``None`` never.
    """

    def __init__(self, clock: FakeClock, *, id_session: str | None, calibration_id: str) -> None:
        super().__init__(clock, session_gated=True)
        self.id_session = id_session
        self.calibration_id = calibration_id
        #: ``(time, session held?)`` of every ``21 10`` received.
        self.id_requests: list[tuple[float, int | None]] = []

    def send(self, msg, timeout=None):  # type: ignore[no-untyped-def]
        data = bytes(msg.data)
        if len(data) >= 3 and data[1] == 0x21:
            now = self.clock.monotonic()
            self.sent.append((now, msg))
            held = self.session
            self.id_requests.append((now, held))
            answers = self.id_session == "any" or (self.id_session == "session" and held)
            if answers:
                self._queue(
                    isotp_frames(bytes([0x61, data[2]]) + self.calibration_id.encode()), now
                )
            else:
                self._queue([bytes([0x03, 0x7F, 0x21, 0x80, 0, 0, 0, 0])], now)
            return
        super().send(msg, timeout)


def _spec(*, needs_session: bool, retry_seconds: float = 30.0) -> CanObdSpec:
    from src.config import CanBusConfig

    return CanObdSpec(
        can=CanBusConfig(interface="virtual", channel="test", bitrate=500000),
        poll_rate_hz=4.0,
        pids=("RPM", "SPEED"),
        dids=("1101", "1208"),
        batch_max_pids=1,
        priority_channels=(),
        dtc_poll_interval_seconds=0.0,
        service_poll_interval_seconds=0.0,
        request_timeout_seconds=0.2,
        did_session=DidSessionSpec(
            session=SESSION,
            exit_session=EXIT,
            exit_grace_seconds=0.05,
            keepalive_seconds=2.0,
            window_interval_seconds=0.25,
            window_max_seconds=1.0,
            lapse_seconds=5.0,
        ),
        calibration_id=CalibrationIdSpec(
            local_identifier=0x10,
            request_id=0x7E0,
            needs_session=needs_session,
            retry_seconds=retry_seconds,
        ),
    )


def _run(clock: FakeClock, bus: ScriptedEcu, spec: CanObdSpec, seconds: float) -> CanObdLoopStats:
    return run_can_obd_loop(
        bus,  # type: ignore[arg-type]
        spec,
        on_frame=lambda _m: None,
        on_reading=lambda _r: None,
        stop_event=threading.Event(),
        deadline_monotonic=clock.monotonic() + seconds,
    )


def _config(tmp_path: Path, registry: str | None) -> CanaryConfig:
    storage = tmp_path / "storage"
    storage.mkdir()
    profile = tmp_path / "profile"
    profile.mkdir()
    if registry is not None:
        (profile / "tune_revisions.yaml").write_text(registry)
    raw = {
        "vehicle": {"make": "Nissan", "model": "Xterra", "year": 2008, "vin": ""},
        "hardware": {
            "elm327_port": "/dev/null",
            "can": {"interface": "virtual", "channel": "vcan0", "bitrate": 500000},
        },
        "collection": {
            "obd_poll_rate_hz": 1,
            "storage_path": str(storage),
            "session_retention_days": 30,
            "storage_warning_mb": 500,
        },
        "preprocessing": {
            "resample_hz": 1,
            "rolling_window_seconds": 30,
            "operating_modes": {"idle": {"rpm_max": 900, "speed_max": 0}},
        },
        "models": {
            "isolation_forest": {"enabled": True, "n_estimators": 10, "contamination": 0.01},
            "lstm_autoencoder": {
                "enabled": False,
                "sequence_length": 60,
                "latent_dim": 16,
                "threshold_k": 3.0,
            },
        },
        "dashboard": {"host": "127.0.0.1", "port": 8000},
        "daemon": {
            "afr_calibration": str(profile / "afr_calibration.yaml"),
            "jobs_dir": str(tmp_path / "control" / "jobs"),
        },
    }
    return CanaryConfig.model_validate(raw)


def _registry(calibration_id: str | None) -> str:
    line = f"    calibration_id: {calibration_id}\n" if calibration_id else ""
    return (
        "schema_version: 1\nrevisions:\n"
        "  - revision: ZS44A-20260920\n"
        f"{line}"
        "    flashed_at: 2026-09-21T03:08:45Z\n"
    )


# ---------------------------------------------------------------------------
# W02.1 as data: the session requirement lives in dids.yaml
# ---------------------------------------------------------------------------


def test_committed_catalog_records_the_measured_session_requirement() -> None:
    """W02.1 was measured 2026-09-22 KOEO: ``21 10`` outside 0xC0 draws NRC 0x80.

    So the calibration id rides the Mode-22 window (``extended``), never a
    connect-time read. This pins the measured answer; a change here needs a
    new probe, not a guess.
    """
    access = load_did_map(REPO / "data" / "dids.yaml").access
    assert access.calibration_id is not None
    assert access.calibration_id.local_identifier == 0x10
    assert access.calibration_id.session == "extended"
    assert "7F 21 80" in access.calibration_id.evidence
    spec = resolve_calibration_id_spec(access)
    assert spec is not None and spec.needs_session is True
    assert spec.request_id == access.ecu_request_id


def test_a_session_requirement_without_a_session_does_not_load() -> None:
    with pytest.raises(ValidationError, match="needs a diagnostic session"):
        DidAccess(
            ecu_request_id=0x7E0,
            ecu_response_id=0x7E8,
            calibration_id={"local_identifier": 0x10, "session": "extended", "evidence": "x"},
        )
    access = DidAccess(
        ecu_request_id=0x7E0,
        ecu_response_id=0x7E8,
        calibration_id={"local_identifier": 0x10, "session": "default", "evidence": "probe"},
    )
    spec = resolve_calibration_id_spec(access)
    assert spec is not None and spec.needs_session is False
    assert resolve_calibration_id_spec(DidAccess(ecu_request_id=1, ecu_response_id=9)) is None


def test_decode_calibration_id_strips_padding_and_refuses_binary() -> None:
    assert decode_calibration_id(b"1ZS44A00000\x00\x00\xff") == CAL_ID
    assert decode_calibration_id(b"  1ZS44A00000  ") == CAL_ID
    assert decode_calibration_id(b"\x01\x02\x03") is None
    assert decode_calibration_id(b"\x00\x00") is None
    assert decode_calibration_id(None) is None


def test_schedule_retries_unanswered_and_refreshes_answered() -> None:
    spec = CalibrationIdSpec(local_identifier=0x10, request_id=0x7E0, needs_session=True)
    schedule = CalibrationIdSchedule(spec, now=100.0)
    assert schedule.due(100.0)
    assert schedule.phase_allows("window") and not schedule.phase_allows("mode01")
    schedule.on_result(None, now=100.0)
    assert schedule.next_read_at == pytest.approx(100.0 + spec.retry_seconds)
    schedule.on_result(CAL_ID, now=200.0)
    assert schedule.next_read_at == pytest.approx(200.0 + spec.refresh_seconds)
    bare = CalibrationIdSchedule(
        CalibrationIdSpec(local_identifier=0x10, request_id=0x7E0, needs_session=False), now=0.0
    )
    assert bare.phase_allows("none") and bare.phase_allows("mode01")
    assert not bare.phase_allows("window")


# ---------------------------------------------------------------------------
# W02.2: the poll loop reads it per the data
# ---------------------------------------------------------------------------


def test_session_read_rides_a_window_and_is_cached(clock: FakeClock) -> None:
    bus = _IdentifyingEcu(clock, id_session="session", calibration_id=CAL_ID)
    stats = _run(clock, bus, _spec(needs_session=True), seconds=4.0)

    assert bus.id_requests, "the calibration id was never requested"
    # Every read went out while the ECU held the Mode-22 session: it never
    # opened a session of its own and never probed the default session.
    assert all(held == SESSION for _at, held in bus.id_requests)
    # Answered once, then not again inside the refresh interval.
    assert len(bus.id_requests) == 1
    assert stats.identification_requests_sent == 1
    snap = tuning_profile.calibration_snapshot()
    assert snap["ecu_calibration_id"] == CAL_ID
    assert snap["ecu_calibration_read_at"] is not None


def test_default_session_read_goes_out_at_connect_before_any_window(clock: FakeClock) -> None:
    bus = _IdentifyingEcu(clock, id_session="any", calibration_id=CAL_ID)
    _run(clock, bus, _spec(needs_session=False), seconds=2.0)

    assert len(bus.id_requests) == 1
    at, held = bus.id_requests[0]
    assert held is None
    # Before the first window opens (the connect-time ``10 81`` exit that
    # hands the bus back to the default session may precede it).
    first_entry = min(
        t for t, m in bus.sent if bytes(m.data)[1] == 0x10 and bytes(m.data)[2] == SESSION
    )
    assert at < first_entry
    assert tuning_profile.calibration_snapshot()["ecu_calibration_id"] == CAL_ID


def test_an_unanswered_read_is_retried_not_abandoned(clock: FakeClock) -> None:
    bus = _IdentifyingEcu(clock, id_session=None, calibration_id=CAL_ID)
    stats = _run(clock, bus, _spec(needs_session=True, retry_seconds=1.0), seconds=4.5)

    assert len(bus.id_requests) >= 3
    assert tuning_profile.calibration_snapshot()["ecu_calibration_id"] is None
    # Identification reads are on the wire but are not channel reads.
    assert stats.identification_requests_sent == len(bus.id_requests)


def test_a_spec_without_a_calibration_read_sends_none(clock: FakeClock) -> None:
    from dataclasses import replace

    bus = _IdentifyingEcu(clock, id_session="any", calibration_id=CAL_ID)
    _run(clock, bus, replace(_spec(needs_session=False), calibration_id=None), seconds=2.0)
    assert bus.id_requests == []


# ---------------------------------------------------------------------------
# W02.2 / W02.3: /status keys, null / match / mismatch, and the loud flag
# ---------------------------------------------------------------------------


def test_status_calibration_is_null_when_the_ecu_is_unread(tmp_path: Path) -> None:
    config = _config(tmp_path, _registry(CAL_ID))
    payload = build_status(config).to_dict()
    assert payload["ecu_calibration_id"] is None
    assert payload["ecu_calibration_read_at"] is None
    assert payload["calibration_matches_registry"] is None
    assert payload["registry_revision"] == "ZS44A-20260920"
    assert payload["calibration_alert"] is False


def test_status_calibration_is_null_when_the_registry_records_no_id(tmp_path: Path) -> None:
    config = _config(tmp_path, _registry(None))
    tuning_profile.record_ecu_calibration_id(CAL_ID)
    status = calibration_status(config)
    assert status["ecu_calibration_id"] == CAL_ID
    assert status["calibration_matches_registry"] is None


def test_status_calibration_matches(tmp_path: Path) -> None:
    config = _config(tmp_path, _registry(CAL_ID))
    tuning_profile.record_ecu_calibration_id(CAL_ID)
    payload = build_status(config).to_dict()
    assert payload["ecu_calibration_id"] == CAL_ID
    assert payload["ecu_calibration_read_at"] is not None
    assert payload["calibration_matches_registry"] is True
    assert payload["calibration_alert"] is False
    assert payload["tuning_readiness"]["calibration"]["ok"] is True


def test_status_calibration_mismatch_is_flagged(tmp_path: Path) -> None:
    """No string derivation: ``ZS44A-…`` naming a revision is not a match for ``1ZS44B…``."""
    config = _config(tmp_path, _registry(CAL_ID))
    tuning_profile.record_ecu_calibration_id("1ZS44B00000")
    payload = build_status(config).to_dict()
    assert payload["calibration_matches_registry"] is False
    assert payload["calibration_alert"] is True
    assert "1ZS44B00000" in payload["calibration_alert_reason"]
    chip = payload["tuning_readiness"]["calibration"]
    assert chip["ok"] is False and "Calibration page" in chip["fix"]


def test_mismatch_with_the_registry_logs_a_warning_once(caplog: pytest.LogCaptureFixture) -> None:
    tuning_profile.configure_running_tune_revision("ZS44A-20260920", calibration_id=CAL_ID)
    with caplog.at_level(logging.WARNING, logger="src.collection.tuning_profile"):
        tuning_profile.record_ecu_calibration_id("1ZS44B00000")
        tuning_profile.record_ecu_calibration_id("1ZS44B00000")
    warnings = [r for r in caplog.records if "flash history does not contain" in r.getMessage()]
    assert len(warnings) == 1


def test_an_id_change_within_one_lifetime_warns_and_sets_the_flag(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    config = _config(tmp_path, _registry(None))
    with caplog.at_level(logging.WARNING, logger="src.collection.tuning_profile"):
        tuning_profile.record_ecu_calibration_id(CAL_ID)
        tuning_profile.record_ecu_calibration_id("1ZS44B00000")
    assert any("changed during this daemon lifetime" in r.getMessage() for r in caplog.records)
    payload = build_status(config).to_dict()
    assert payload["ecu_calibration_changed"] is True
    assert payload["calibration_alert"] is True


def test_a_broken_registry_degrades_status_instead_of_failing(tmp_path: Path) -> None:
    config = _config(tmp_path, "revisions: [not, a, registry")
    tuning_profile.record_ecu_calibration_id(CAL_ID)
    payload = build_status(config).to_dict()
    assert payload["ecu_calibration_id"] == CAL_ID
    assert payload["calibration_matches_registry"] is None
    assert payload["calibration_alert"] is True


# ---------------------------------------------------------------------------
# W02.4: tuning_readiness
# ---------------------------------------------------------------------------


def test_readiness_block_has_every_entry_with_the_same_shape(tmp_path: Path) -> None:
    config = _config(tmp_path, _registry(CAL_ID))
    payload = build_status(config, ups_status_path=tmp_path / "missing").to_dict()
    readiness = payload["tuning_readiness"]
    assert set(readiness) == {
        "ups_soc",
        "storage_free_mb",
        "memory_channels",
        "calibration",
        "vehicle_state",
    }
    for entry in readiness.values():
        assert set(entry) == {"ok", "value", "detail", "fix"}
    # No watchdog heartbeat and no ECU read: unknown, never green.
    assert readiness["ups_soc"]["value"] is None and readiness["ups_soc"]["ok"] is None
    assert readiness["calibration"]["value"] is None
    assert readiness["storage_free_mb"]["ok"] is True
    assert readiness["memory_channels"]["value"] is None  # no dids_path wired


def test_ups_floor_is_configurable_and_defaults_to_40(tmp_path: Path) -> None:
    config = _config(tmp_path, None)
    assert config.tuning_readiness.min_ups_soc_percent == 40.0
    heartbeat = tmp_path / "ups_status"
    import json
    import time

    heartbeat.write_text(json.dumps({"ts": time.time(), "soc": 35.0}))
    low = build_status(config, ups_status_path=heartbeat).to_dict()["tuning_readiness"]
    assert low["ups_soc"]["ok"] is False and low["ups_soc"]["value"] == 35.0
    assert low["ups_soc"]["fix"]
    relaxed = config.model_copy(
        update={
            "tuning_readiness": config.tuning_readiness.model_copy(
                update={"min_ups_soc_percent": 30.0}
            )
        }
    )
    ok = build_status(relaxed, ups_status_path=heartbeat).to_dict()["tuning_readiness"]
    assert ok["ups_soc"]["ok"] is True and ok["ups_soc"]["fix"] is None


def test_ups_entry_with_a_stale_heartbeat_is_reported_but_not_judged() -> None:
    entry = ups_soc_entry(
        {"watchdog_running": True, "watchdog_stale": True, "soc": 90}, floor_percent=40
    )
    assert entry["ok"] is None and entry["value"] == 90.0


def test_memory_channel_counts_come_from_the_last_verify(tmp_path: Path) -> None:
    config = _config(tmp_path, None)
    dids = REPO / "data" / "dids.yaml"
    catalog = load_did_map(dids).memory
    # A DID known answer or a declared relation (D03.38), wherever declared.
    checkable = [c.name for c in catalog if is_memory_checkable(c, catalog)]
    assert len(checkable) >= 2, "fixture assumes the catalog has verifiable channels"

    before = memory_verify_summary(config, dids)
    assert before["passed"] == 0 and before["failed"] == 0
    assert before["never_ran"] == len(checkable)
    assert memory_channels_entry(before)["ok"] is None

    record_verify_results(config, {checkable[0]: "pass"})
    record_verify_results(config, {checkable[1]: "fail"})  # merged, not replaced
    after = memory_verify_summary(config, dids)
    assert after["passed"] == 1 and after["failed"] == 1
    assert after["total"] == len(catalog)
    entry = memory_channels_entry(after)
    assert entry["ok"] is False and entry["fix"]

    status = build_status(config, dids_path=dids).to_dict()
    assert status["tuning_readiness"]["memory_channels"]["value"]["failed"] == 1


def test_calibration_and_vehicle_state_entries() -> None:
    assert calibration_entry({"ecu_calibration_id": None})["value"] is None
    unjudged = calibration_entry(
        {"ecu_calibration_id": CAL_ID, "calibration_matches_registry": None}
    )
    assert unjudged["ok"] is None and unjudged["value"] == CAL_ID
    assert vehicle_state_entry("engine_running")["ok"] is True
    assert vehicle_state_entry("engine_off")["ok"] is False
    assert vehicle_state_entry("replay")["ok"] is None
    assert vehicle_state_entry(None)["value"] is None


def test_readiness_builder_is_additive_only(tmp_path: Path) -> None:
    """New keys only: every pre-W02 top-level /status key is still present."""
    config = _config(tmp_path, None)
    payload = build_status(config).to_dict()
    for key in ("storage_path", "free_mb", "vehicle_state", "capture_profile", "tuning_mode"):
        assert key in payload
    readiness = build_tuning_readiness(
        power=None,
        ups_floor_percent=40,
        free_mb=None,
        storage_floor_mb=500,
        memory_summary=None,
        calibration={},
        vehicle_state=None,
    )
    assert all(entry["value"] is None for entry in readiness.values())


def test_example_config_documents_the_readiness_floor() -> None:
    raw = yaml.safe_load((REPO / "canary.example.yaml").read_text())
    assert raw["tuning_readiness"]["min_ups_soc_percent"] == 40.0
