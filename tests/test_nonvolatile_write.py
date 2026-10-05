"""W07: non-volatile ECU-write permission and live interlocks."""

from __future__ import annotations

import json
from collections.abc import Iterator
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest

from src.config import CanaryConfig, NonvolatileWriteConfig
from src.control.audit import ControlAuditLogger
from src.control.nonvolatile_write import (
    Authorization,
    AuthorizationRefusedError,
    NonvolatileWriteGuard,
    NonvolatileWriteRequest,
    NonvolatileWriteSignals,
    SupplyVoltageReading,
    execute_authorized_blocks,
    sha256_image,
    supply_voltage_from_latest_rows,
)
from src.dashboard.status import VehicleState, VehicleStateName

IMAGE = sha256_image(b"candidate calibration")
OTHER_IMAGE = sha256_image(b"different calibration")
ECU = "ECM-7E0"
ALL_STATES: tuple[VehicleStateName, ...] = (
    "engine_running",
    "ignition_only",
    "engine_off",
    "parked",
    "bench",
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
        battery_voltage=(
            SupplyVoltageReading(battery_voltage, "sensor:SUPPLY_VOLTAGE", 0.1)
            if battery_voltage is not None
            else SupplyVoltageReading.unknown("test: no voltage")
        ),
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
        (lambda: signals(battery_voltage=None), "battery_unknown"),
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


# --- H03.2: the production supply-voltage reader ---------------------------

NOW = datetime(2026, 9, 26, 12, 0, 0, tzinfo=UTC)
MAX_AGE = NonvolatileWriteConfig().max_signal_age_seconds
SENSOR = ("sensor", "SUPPLY_VOLTAGE")
OBD = ("obd", "CONTROL_MODULE_VOLTAGE")


def _row(key: tuple[str, str], value: Any, age_seconds: float) -> dict[str, Any]:
    source, channel = key
    stamp = (NOW - timedelta(seconds=age_seconds)).isoformat()
    return {"source": source, "channel": channel, "value": value, "timestamp": stamp}


def _read(*rows: dict[str, Any]) -> SupplyVoltageReading:
    return supply_voltage_from_latest_rows(rows, now=NOW, max_age_seconds=MAX_AGE)


@pytest.mark.parametrize(
    ("sensor_age", "obd_age", "expected"),
    [
        # (sensor age or None=absent, obd age or None=absent) -> source used
        (0.5, 0.5, "sensor:SUPPLY_VOLTAGE"),  # both fresh: the 12 V sense wins
        (0.5, None, "sensor:SUPPLY_VOLTAGE"),  # sensor fresh, OBD absent
        (0.5, 60.0, "sensor:SUPPLY_VOLTAGE"),  # sensor fresh, OBD stale
        (60.0, 0.5, "obd:CONTROL_MODULE_VOLTAGE"),  # sensor stale: fall back
        (None, 0.5, "obd:CONTROL_MODULE_VOLTAGE"),  # no HAT sense: fall back
        (None, None, None),  # neither
        (60.0, 60.0, None),  # both stale
        (None, 60.0, None),  # sensor absent, OBD stale
        (60.0, None, None),  # sensor stale, OBD absent
    ],
)
def test_supply_voltage_prefers_the_fresh_sensor_then_the_fresh_ecm_reading(
    sensor_age: float | None, obd_age: float | None, expected: str | None
) -> None:
    rows = []
    if sensor_age is not None:
        rows.append(_row(SENSOR, 12.6, sensor_age))
    if obd_age is not None:
        rows.append(_row(OBD, 12.4, obd_age))
    reading = _read(*rows)
    assert reading.source == expected
    if expected is None:
        assert reading.volts is None
        assert reading.reason is not None
        # Each source's failure is named, the way it failed.
        for key, age in ((SENSOR, sensor_age), (OBD, obd_age)):
            name = f"{key[0]}:{key[1]}"
            assert name in reading.reason
            assert ("stale" if age is not None else "no reading") in reading.reason.split(name)[1]
    else:
        assert reading.volts == (12.6 if expected.startswith("sensor") else 12.4)
        assert reading.age_seconds == pytest.approx(0.5)


def test_supply_voltage_takes_each_sources_newest_row_and_ignores_other_channels() -> None:
    reading = _read(
        _row(SENSOR, 11.0, 30.0),
        _row(SENSOR, 12.7, 1.0),
        _row(("obd", "RPM"), 0.0, 0.1),
        _row(("sensor", "OTHER_VOLTAGE"), 14.0, 0.1),
    )
    assert (reading.volts, reading.source) == (12.7, "sensor:SUPPLY_VOLTAGE")


@pytest.mark.parametrize(
    ("value", "age", "named"),
    [
        (float("nan"), 0.5, "nan"),
        ("12.6", 0.5, "no numeric value"),
        (None, 0.5, "no numeric value"),
        (True, 0.5, "no numeric value"),
        (12.6, -30.0, "in the future"),
    ],
)
def test_an_unusable_fresh_looking_value_is_unknown_not_a_pass(
    value: Any, age: float, named: str
) -> None:
    reading = _read(_row(SENSOR, value, age))
    assert reading.volts is None
    assert reading.reason is not None and named in reading.reason


def test_an_undated_row_is_unknown() -> None:
    row = _row(SENSOR, 12.6, 0.5)
    row["timestamp"] = "not a time"
    assert _read(row).volts is None
    assert "no readable timestamp" in (_read(row).reason or "")


def _signals_with(reading: SupplyVoltageReading) -> NonvolatileWriteSignals:
    return replace(signals(), battery_voltage=reading)


@pytest.mark.parametrize(
    ("rows", "code"),
    [
        ((), "battery_unknown"),
        ((_row(SENSOR, 12.6, 60.0), _row(OBD, 12.4, 60.0)), "battery_unknown"),
        ((_row(OBD, 11.2, 0.5),), "battery_low"),
    ],
)
def test_the_guard_refuses_an_unknown_or_low_supply_with_the_readers_reason(
    rows: tuple[dict[str, Any], ...], code: str, audit: ControlAuditLogger, clock: Clock
) -> None:
    reading = _read(*rows)
    with pytest.raises(AuthorizationRefusedError) as caught:
        guard(audit, clock).authorize(request(lambda: _signals_with(reading)))
    assert caught.value.code == code
    if code == "battery_unknown":
        assert "no fresh supply voltage" in caught.value.detail
    entries = [json.loads(line) for line in audit.path.read_text().splitlines()]
    assert entries[-1]["payload"]["signals"]["battery_voltage"] == reading.to_audit_dict()


def test_the_guard_passes_a_fresh_sensor_or_ecm_voltage(
    audit: ControlAuditLogger, clock: Clock
) -> None:
    write_guard = guard(audit, clock)
    for rows in ((_row(SENSOR, 12.6, 0.5),), (_row(SENSOR, 12.6, 60.0), _row(OBD, 12.4, 0.5))):
        reading = _read(*rows)
        assert isinstance(
            write_guard.authorize(request(lambda r=reading: _signals_with(r))), Authorization
        )


def _forged(volts: Any, source: Any, age: Any, reason: Any = None) -> SupplyVoltageReading:
    """A reading that skips ``__post_init__``: what a future bug could hand
    the guard, to prove its own re-check still refuses."""
    reading = object.__new__(SupplyVoltageReading)
    for name, value in (
        ("volts", volts),
        ("source", source),
        ("age_seconds", age),
        ("reason", reason),
    ):
        object.__setattr__(reading, name, value)
    return reading


@pytest.mark.parametrize("age", [None, 20.0, -1.0, float("nan"), float("inf"), float("-inf")])
def test_the_guard_rechecks_age_rather_than_trusting_the_reader(
    age: float | None, audit: ControlAuditLogger, clock: Clock
) -> None:
    """A reading with a value but no fresh age still refuses — NaN included,
    which passes any pair of ``<``/``>`` tests (2026-09-26 verification)."""
    stale = _forged(12.6, "sensor:SUPPLY_VOLTAGE", age)
    with pytest.raises(AuthorizationRefusedError) as caught:
        guard(audit, clock).authorize(request(lambda: _signals_with(stale)))
    assert caught.value.code == "battery_stale"


@pytest.mark.parametrize("volts", [float("nan"), float("inf")])
def test_the_guard_rechecks_a_non_finite_voltage(
    volts: float, audit: ControlAuditLogger, clock: Clock
) -> None:
    forged = _forged(volts, "sensor:SUPPLY_VOLTAGE", 0.1)
    with pytest.raises(AuthorizationRefusedError) as caught:
        guard(audit, clock).authorize(request(lambda: _signals_with(forged)))
    assert caught.value.code == "battery_unknown"


@pytest.mark.parametrize(
    ("volts", "source", "age", "reason", "named"),
    [
        (None, None, None, None, "reason"),
        (None, None, None, "", "reason"),
        (float("nan"), "sensor:SUPPLY_VOLTAGE", 0.1, None, "finite number"),
        (float("inf"), "sensor:SUPPLY_VOLTAGE", 0.1, None, "finite number"),
        (12.6, None, 0.1, None, "source"),
        (12.6, "", 0.1, None, "source"),
        (12.6, "sensor:SUPPLY_VOLTAGE", None, None, "age"),
        (12.6, "sensor:SUPPLY_VOLTAGE", float("nan"), None, "age"),
        (12.6, "sensor:SUPPLY_VOLTAGE", float("inf"), None, "age"),
        (12.6, "sensor:SUPPLY_VOLTAGE", -0.1, None, "age"),
    ],
)
def test_a_half_formed_reading_cannot_be_built(
    volts: Any, source: Any, age: Any, reason: Any, named: str
) -> None:
    with pytest.raises(ValueError, match=named):
        SupplyVoltageReading(volts, source, age, reason)


def test_well_formed_readings_build() -> None:
    assert SupplyVoltageReading(12.6, "sensor:SUPPLY_VOLTAGE", 0.0).volts == 12.6
    assert SupplyVoltageReading.unknown("no fresh supply voltage: test").volts is None


def test_numpy_values_and_pandas_timestamps_read_like_plain_ones() -> None:
    """``read_latest_per_channel`` rows can carry numpy scalars and Timestamps."""
    import numpy as np
    import pandas as pd

    row = {
        "source": "obd",
        "channel": "CONTROL_MODULE_VOLTAGE",
        "value": np.float64(12.4),
        "timestamp": pd.Timestamp(NOW - timedelta(seconds=1.0)),
    }
    reading = _read(row)
    assert (reading.volts, reading.source) == (12.4, "obd:CONTROL_MODULE_VOLTAGE")
    assert _read({**row, "value": np.int64(12)}).volts == 12.0
