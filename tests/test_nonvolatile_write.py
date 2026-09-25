"""W07: non-volatile ECU-write permission and live interlocks."""

from __future__ import annotations

import json
from collections.abc import Iterator
from dataclasses import replace
from pathlib import Path

import pytest

from src.config import CanaryConfig, NonvolatileWriteConfig
from src.control.audit import ControlAuditLogger
from src.control.nonvolatile_write import (
    Authorization,
    AuthorizationRefusedError,
    NonvolatileWriteGuard,
    NonvolatileWriteRequest,
    NonvolatileWriteSignals,
    execute_authorized_blocks,
    sha256_image,
)
from src.dashboard.status import VehicleState, VehicleStateName

IMAGE = sha256_image(b"candidate calibration")
OTHER_IMAGE = sha256_image(b"different calibration")
ECU = "ECM-7E0"
ALL_STATES: tuple[VehicleStateName, ...] = (
    "engine_running",
    "ignition_only",
    "engine_off",
    "no_data",
    "replay",
    "passive",
    "unknown",
)


class Clock:
    def __init__(self) -> None:
        self.now = 100.0

    def __call__(self) -> float:
        return self.now


def signals(
    state: VehicleStateName = "ignition_only",
    *,
    fresh: bool = True,
    rpm: float | None = 0.0,
    speed: float | None = 0.0,
    speed_fresh: bool = True,
    battery_voltage: float | None = 12.6,
    ups_soc: float | None = 80.0,
    ups_runtime: float | None = 900.0,
) -> NonvolatileWriteSignals:
    return NonvolatileWriteSignals(
        vehicle_state=VehicleState(
            state=state,
            rpm=rpm,
            obd_freshness_seconds=0.2 if fresh else 20.0,
            can_freshness_seconds=0.1,
            replay_id="r1" if state == "replay" else None,
        ),
        vehicle_speed_kph=speed,
        vehicle_speed_freshness_seconds=0.2 if speed_fresh else 20.0,
        battery_voltage=battery_voltage,
        ups_soc_percent=ups_soc,
        ups_runtime_seconds=ups_runtime,
    )


@pytest.fixture
def clock() -> Clock:
    return Clock()


@pytest.fixture
def audit(tmp_path: Path) -> ControlAuditLogger:
    return ControlAuditLogger(tmp_path / "audit.jsonl")


def request(reader=lambda: signals()) -> NonvolatileWriteRequest:  # type: ignore[no-untyped-def]
    return NonvolatileWriteRequest(
        image_sha256=IMAGE,
        ecu=ECU,
        operator_confirmed_image_sha256=IMAGE,
        operator_confirmed_ecu=ECU,
        expected_duration_seconds=300.0,
        read_signals=reader,
    )


def guard(audit: ControlAuditLogger, clock: Clock) -> NonvolatileWriteGuard:
    return NonvolatileWriteGuard(policy=NonvolatileWriteConfig(), audit=audit, monotonic=clock)


@pytest.mark.parametrize("state", ALL_STATES)
@pytest.mark.parametrize("fresh", [True, False])
def test_state_and_freshness_table_is_exhaustive(
    state: VehicleStateName,
    fresh: bool,
    audit: ControlAuditLogger,
    clock: Clock,
) -> None:
    write_guard = guard(audit, clock)
    should_pass = state == "ignition_only" and fresh
    if should_pass:
        authorization = write_guard.authorize(request(lambda: signals(state, fresh=fresh)))
        assert isinstance(authorization, Authorization)
    else:
        with pytest.raises(AuthorizationRefusedError):
            write_guard.authorize(request(lambda: signals(state, fresh=fresh)))


def test_every_vehicle_state_literal_has_a_table_row() -> None:
    from typing import get_args

    assert set(get_args(VehicleStateName)) == set(ALL_STATES)


@pytest.mark.parametrize(
    ("reader", "code"),
    [
        (lambda: signals(speed=None), "speed_unknown"),
        (lambda: signals(speed_fresh=False), "speed_unknown"),
        (lambda: signals(speed=1.0), "vehicle_moving"),
        (lambda: signals(battery_voltage=11.9), "battery_low"),
        (lambda: signals(ups_soc=28.0), "ups_soc_low"),
        (lambda: signals(ups_runtime=419.0), "ups_runtime_short"),
    ],
)
def test_motion_and_supply_refusals_name_their_cause(
    reader,
    code: str,
    audit: ControlAuditLogger,
    clock: Clock,  # type: ignore[no-untyped-def]
) -> None:
    with pytest.raises(AuthorizationRefusedError) as caught:
        guard(audit, clock).authorize(request(reader))
    assert caught.value.code == code


def test_running_and_unknown_are_distinct_refusals(audit: ControlAuditLogger, clock: Clock) -> None:
    write_guard = guard(audit, clock)
    with pytest.raises(AuthorizationRefusedError) as running:
        write_guard.authorize(request(lambda: signals("engine_running", rpm=800.0)))
    with pytest.raises(AuthorizationRefusedError) as unknown:
        write_guard.authorize(request(lambda: signals("unknown")))
    assert running.value.code == "engine_running"
    assert unknown.value.code == "engine_state_not_ignition_only"
    assert "cannot tell" not in running.value.detail


@pytest.mark.parametrize(
    ("confirmed_image", "confirmed_ecu", "code"),
    [
        (OTHER_IMAGE, ECU, "permission_image_mismatch"),
        (IMAGE, "TCM-7E1", "permission_ecu_mismatch"),
    ],
)
def test_permission_must_name_exact_image_and_ecu(
    confirmed_image: str,
    confirmed_ecu: str,
    code: str,
    audit: ControlAuditLogger,
    clock: Clock,
) -> None:
    candidate = request()
    candidate = replace(
        candidate,
        operator_confirmed_image_sha256=confirmed_image,
        operator_confirmed_ecu=confirmed_ecu,
    )
    with pytest.raises(AuthorizationRefusedError) as caught:
        guard(audit, clock).authorize(candidate)
    assert caught.value.code == code


def test_permission_is_single_use_and_bound_to_image_and_ecu(
    audit: ControlAuditLogger, clock: Clock
) -> None:
    write_guard = guard(audit, clock)

    different_image = write_guard.authorize(request())
    with pytest.raises(AuthorizationRefusedError, match="different image"):
        execute_authorized_blocks(
            different_image,
            image_sha256=OTHER_IMAGE,
            ecu=ECU,
            blocks=[],
            write_block=lambda *_: None,
        )

    different_ecu = write_guard.authorize(request())
    with pytest.raises(AuthorizationRefusedError, match="another ECU"):
        execute_authorized_blocks(
            different_ecu,
            image_sha256=IMAGE,
            ecu="TCM-7E1",
            blocks=[],
            write_block=lambda *_: None,
        )

    used = write_guard.authorize(request())
    assert (
        execute_authorized_blocks(
            used,
            image_sha256=IMAGE,
            ecu=ECU,
            blocks=[b"one"],
            write_block=lambda *_: None,
        )
        == 1
    )
    with pytest.raises(AuthorizationRefusedError, match="single-use"):
        execute_authorized_blocks(
            used,
            image_sha256=IMAGE,
            ecu=ECU,
            blocks=[],
            write_block=lambda *_: None,
        )


def test_expired_permission_refuses(audit: ControlAuditLogger, clock: Clock) -> None:
    authorization = guard(audit, clock).authorize(request())
    clock.now += 121.0
    with pytest.raises(AuthorizationRefusedError, match="expired"):
        execute_authorized_blocks(
            authorization,
            image_sha256=IMAGE,
            ecu=ECU,
            blocks=[],
            write_block=lambda *_: None,
        )


def test_rpm_rising_between_blocks_aborts_before_next_write_and_audits(
    audit: ControlAuditLogger, clock: Clock
) -> None:
    readings: Iterator[NonvolatileWriteSignals] = iter(
        [signals(), signals(), signals("engine_running", rpm=750.0)]
    )
    authorization = guard(audit, clock).authorize(request(lambda: next(readings)))
    written: list[int] = []
    with pytest.raises(AuthorizationRefusedError) as caught:
        execute_authorized_blocks(
            authorization,
            image_sha256=IMAGE,
            ecu=ECU,
            blocks=[b"one", b"two"],
            write_block=lambda _authorization, index, _block: written.append(index),
        )
    assert caught.value.code == "engine_running"
    assert written == [0]
    entries = [json.loads(line) for line in audit.path.read_text().splitlines()]
    assert entries[-1]["action"] == "nonvolatile_write.abort"
    assert entries[-1]["payload"]["stage"] == "before_block_1"
    assert entries[-1]["payload"]["signals"]["vehicle_state"]["rpm"] == 750.0


def test_every_authorization_request_is_audited(audit: ControlAuditLogger, clock: Clock) -> None:
    write_guard = guard(audit, clock)
    write_guard.authorize(request())
    with pytest.raises(AuthorizationRefusedError):
        write_guard.authorize(request(lambda: signals(ups_soc=28.0)))
    entries = [json.loads(line) for line in audit.path.read_text().splitlines()]
    assert [entry["result"] for entry in entries] == ["granted", "refused"]
    assert all(entry["payload"]["image_sha256"] == IMAGE for entry in entries)
    assert entries[-1]["payload"]["reason_code"] == "ups_soc_low"


def test_no_http_or_mcp_route_can_mint_authorization(tmp_path: Path) -> None:
    from src.dashboard.api import create_app
    from src.mcp_server import server as mcp_server

    config = CanaryConfig.model_validate(
        {
            "vehicle": {"make": "Test", "model": "Rig", "year": 2026},
            "hardware": {
                "elm327_port": "/dev/null",
                "can": {"interface": "virtual", "channel": "test", "bitrate": 500000},
            },
            "collection": {
                "obd_poll_rate_hz": 1,
                "storage_path": tmp_path / "sessions",
                "session_retention_days": 1,
                "storage_warning_mb": 1,
            },
            "preprocessing": {
                "resample_hz": 1,
                "rolling_window_seconds": 30,
                "operating_modes": {"all": {}},
            },
            "models": {
                "isolation_forest": {
                    "enabled": False,
                    "n_estimators": 10,
                    "contamination": 0.01,
                },
                "lstm_autoencoder": {
                    "enabled": False,
                    "sequence_length": 10,
                    "latent_dim": 4,
                    "threshold_k": 3.0,
                },
            },
            "dashboard": {"host": "127.0.0.1", "port": 8000},
        }
    )
    app = create_app(config, anomalies_dir=tmp_path / "anomalies")
    mutation_routes = {
        route.path
        for route in app.routes
        if getattr(route, "methods", set()) & {"POST", "PUT", "PATCH"}
    }
    assert not any("nonvolatile" in path or "flash" in path for path in mutation_routes)
    assert "authorize_nonvolatile_write" not in Path(mcp_server.__file__).read_text()
