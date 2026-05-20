"""Tests for the derived ``vehicle_state`` block in ``GET /status``.

The classifier rule table is documented in ``classify_vehicle_state``;
these tests pin the truth table so a future tweak to ``engine_off`` vs.
``ignition_only`` thresholds can't silently regress the live-vs-replay
disambiguation the agent docs rely on.
"""

from __future__ import annotations

from src.dashboard.status import classify_vehicle_state


def _common_telemetry(**overrides: object) -> dict[str, object]:
    base: dict[str, object] = {
        "rpm": None,
        "coolant_temp": None,
        "run_time": None,
        "obd_freshness_seconds": None,
        "can_freshness_seconds": None,
        "replay_id": None,
    }
    base.update(overrides)
    return base


def test_replay_overrides_everything() -> None:
    """Replay precedence holds even when OBD/CAN are fresh and RPM > running."""
    state = classify_vehicle_state(
        **_common_telemetry(
            rpm=800.0,
            coolant_temp=85.0,
            run_time=120.0,
            obd_freshness_seconds=0.5,
            can_freshness_seconds=0.1,
            replay_id="2026-05-20T180000",
        )
    )
    assert state.state == "replay"
    assert state.replay_id == "2026-05-20T180000"
    # raw telemetry fields still surfaced for diagnostics
    assert state.rpm == 800.0


def test_no_data_when_both_buses_stale() -> None:
    state = classify_vehicle_state(
        **_common_telemetry(
            obd_freshness_seconds=120.0,
            can_freshness_seconds=120.0,
        )
    )
    assert state.state == "no_data"


def test_no_data_when_neither_observed() -> None:
    state = classify_vehicle_state(**_common_telemetry())
    assert state.state == "no_data"


def test_engine_running_at_idle_rpm() -> None:
    state = classify_vehicle_state(
        **_common_telemetry(
            rpm=760.0,
            coolant_temp=82.0,
            run_time=240.0,
            obd_freshness_seconds=0.3,
            can_freshness_seconds=0.1,
        )
    )
    assert state.state == "engine_running"


def test_engine_running_at_cruise_rpm() -> None:
    state = classify_vehicle_state(
        **_common_telemetry(
            rpm=2200.0,
            coolant_temp=88.0,
            run_time=1200.0,
            obd_freshness_seconds=0.4,
            can_freshness_seconds=0.1,
        )
    )
    assert state.state == "engine_running"


def test_ignition_only_when_obd_fresh_but_rpm_zero() -> None:
    """KOEO (key on, engine off): ECM responds to OBD with RPM=0."""
    state = classify_vehicle_state(
        **_common_telemetry(
            rpm=0.0,
            coolant_temp=25.0,
            run_time=0.0,
            obd_freshness_seconds=0.5,
            can_freshness_seconds=0.2,
        )
    )
    assert state.state == "ignition_only"


def test_engine_off_when_can_fresh_but_obd_stale() -> None:
    """Plugged into the bus, but ECM isn't responding — typical with key out."""
    state = classify_vehicle_state(
        **_common_telemetry(
            obd_freshness_seconds=45.0,
            can_freshness_seconds=0.1,
        )
    )
    assert state.state == "engine_off"


def test_unknown_when_obd_fresh_and_rpm_in_no_mans_land() -> None:
    """OBD fresh, RPM > 0 but below the running threshold — flag it as unknown
    so the caller doesn't silently classify a noisy reading as ``engine_off``.
    """
    state = classify_vehicle_state(
        **_common_telemetry(
            rpm=120.0,  # below running threshold but not zero
            obd_freshness_seconds=0.5,
            can_freshness_seconds=0.1,
        )
    )
    assert state.state == "unknown"
    assert state.rpm == 120.0


def test_classifier_returns_raw_signals_alongside_state() -> None:
    """Diagnostic fields are always populated so the agent can second-guess."""
    state = classify_vehicle_state(
        **_common_telemetry(
            rpm=760.0,
            coolant_temp=82.0,
            run_time=180.0,
            obd_freshness_seconds=0.4,
            can_freshness_seconds=0.1,
        )
    )
    assert state.rpm == 760.0
    assert state.coolant_temp == 82.0
    assert state.run_time == 180.0
    assert state.obd_freshness_seconds == 0.4
    assert state.can_freshness_seconds == 0.1


def test_to_dict_rounds_floats_for_stable_json() -> None:
    state = classify_vehicle_state(
        **_common_telemetry(
            rpm=760.4444,
            coolant_temp=82.999,
            obd_freshness_seconds=0.345678,
            can_freshness_seconds=0.123456,
        )
    )
    payload = state.to_dict()
    assert payload["rpm"] == 760.44
    assert payload["coolant_temp"] == 83.0
    assert payload["obd_freshness_seconds"] == 0.35
    assert payload["can_freshness_seconds"] == 0.12
