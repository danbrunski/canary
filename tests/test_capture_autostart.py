"""The rule table for daemon-side capture arming (2026-09-22 drive)."""

from __future__ import annotations

from src.control.capture_autostart import CaptureAutostartDecider, CaptureAutostartRule


def _decider(**kwargs: object) -> CaptureAutostartDecider:
    rule = CaptureAutostartRule(profile="tuning", **kwargs)  # type: ignore[arg-type]
    return CaptureAutostartDecider(rule=rule)


def test_warm_up_stays_on_default_until_the_set_point() -> None:
    d = _decider(coolant_min_c=80.0)
    # The 2026-09-22 warm-up, tick by tick: cold, then crossing 80 °C.
    for coolant in (27.0, 64.0, 76.0, 79.0):
        assert (
            d.update(state="engine_running", coolant_temp=coolant, capture_active=False, now=0.0)
            is None
        )
    assert "warming up" in d.reason
    # First qualifying tick confirms, the second starts.
    assert (
        d.update(state="engine_running", coolant_temp=85.0, capture_active=False, now=1.0) is None
    )
    assert (
        d.update(state="engine_running", coolant_temp=85.0, capture_active=False, now=2.0)
        == "start"
    )


def test_an_unknown_coolant_reading_never_satisfies_the_set_point() -> None:
    d = _decider(coolant_min_c=80.0)
    for tick in range(5):
        assert (
            d.update(
                state="engine_running", coolant_temp=None, capture_active=False, now=float(tick)
            )
            is None
        )
    assert d.reason == "coolant temperature unknown"


def test_a_momentary_cold_read_restarts_the_confirmation() -> None:
    d = _decider(coolant_min_c=80.0, start_after_ticks=2)
    assert (
        d.update(state="engine_running", coolant_temp=90.0, capture_active=False, now=0.0) is None
    )
    assert (
        d.update(state="engine_running", coolant_temp=12.0, capture_active=False, now=1.0) is None
    )
    # Not "start" on the next warm tick: the counter went back to zero.
    assert (
        d.update(state="engine_running", coolant_temp=90.0, capture_active=False, now=2.0) is None
    )
    assert (
        d.update(state="engine_running", coolant_temp=90.0, capture_active=False, now=3.0)
        == "start"
    )


def test_the_capture_stops_when_the_engine_does_and_rearms() -> None:
    d = _decider(coolant_min_c=80.0, stop_after_seconds=10.0)
    d.update(state="engine_running", coolant_temp=90.0, capture_active=False, now=0.0)
    assert (
        d.update(state="engine_running", coolant_temp=90.0, capture_active=False, now=1.0)
        == "start"
    )
    assert d.update(state="engine_running", coolant_temp=90.0, capture_active=True, now=2.0) is None
    # Key off: held briefly, so a one-tick misclassification costs nothing.
    assert d.update(state="ignition_only", coolant_temp=90.0, capture_active=True, now=3.0) is None
    assert d.update(state="engine_off", coolant_temp=90.0, capture_active=True, now=12.0) is None
    assert d.update(state="engine_off", coolant_temp=90.0, capture_active=True, now=13.5) == "stop"
    # Re-armed: the next warm running stretch of the same drive captures too.
    d.update(state="engine_running", coolant_temp=90.0, capture_active=False, now=60.0)
    assert (
        d.update(state="engine_running", coolant_temp=90.0, capture_active=False, now=61.0)
        == "start"
    )


def test_a_brief_dropout_does_not_stop_a_running_capture() -> None:
    d = _decider(coolant_min_c=80.0, stop_after_seconds=10.0)
    d.update(state="engine_running", coolant_temp=90.0, capture_active=False, now=0.0)
    d.update(state="engine_running", coolant_temp=90.0, capture_active=False, now=1.0)
    assert d.update(state="no_data", coolant_temp=None, capture_active=True, now=2.0) is None
    assert d.update(state="engine_running", coolant_temp=90.0, capture_active=True, now=5.0) is None
    # The countdown reset with the engine, so 10 s from the dropout is not a stop.
    assert (
        d.update(state="engine_running", coolant_temp=90.0, capture_active=True, now=13.0) is None
    )


def test_no_set_point_starts_as_soon_as_the_engine_runs() -> None:
    d = _decider(coolant_min_c=None, start_after_ticks=1)
    assert (
        d.update(state="engine_running", coolant_temp=None, capture_active=False, now=0.0)
        == "start"
    )
