"""Tests for the Phase 7.4 behavior/physics residual first-class detector."""

from __future__ import annotations

from pathlib import Path

from src.alerts.emitter import AnomalyEventEmitter
from src.alerts.event_logger import AnomalyEventLogger, read_events_dir
from src.alerts.events import AnomalyEvent, behavior_cause

# ---------------------------------------------------------------------------
# behavior_cause — the human-readable physical explanation
# ---------------------------------------------------------------------------


def test_behavior_cause_includes_model_channel_and_gap() -> None:
    msg = behavior_cause(
        model="coolant_warmup",
        channel="obd:COOLANT",
        severity_band="critical",
        actual=55.0,
        expected=88.0,
        deviation=-33.0,
    )
    assert "coolant_warmup" in msg
    assert "critical" in msg
    assert "obd:COOLANT" in msg
    assert "55" in msg and "88" in msg
    assert "-33" in msg


def test_behavior_cause_handles_missing_values() -> None:
    msg = behavior_cause(
        model="m",
        channel="c",
        severity_band="warning",
        actual=None,
        expected=None,
        deviation=None,
    )
    assert "n/a" in msg
    assert "Δ" not in msg  # no deviation term when deviation is None


# ---------------------------------------------------------------------------
# AnomalyEvent.from_behavior
# ---------------------------------------------------------------------------


def test_from_behavior_event_shape_and_residual_detail() -> None:
    ev = AnomalyEvent.from_behavior(
        model="timing_advance_vs_rpm",
        kind="surface_1d",
        channel="dbc:TIMING_ADV",
        timestamp="2026-05-13T12:00:00+00:00",
        severity_band="warning",
        score=0.5,
        threshold=0.5,
        actual=8.0,
        expected=22.0,
        deviation=-14.0,
        operating_mode="city",
    )
    assert ev.source == "behavior"
    assert ev.model_version == "behavior/timing_advance_vs_rpm"
    assert ev.score == 0.5
    assert ev.threshold == 0.5
    assert ev.affected_channels == ("dbc:TIMING_ADV",)
    assert ev.operating_mode == "city"
    assert ev.detail["model"] == "timing_advance_vs_rpm"
    assert "timing_advance_vs_rpm" in ev.detail["cause"]
    residuals = ev.detail["behavior_residuals"]
    assert isinstance(residuals, list) and len(residuals) == 1
    r = residuals[0]
    assert r["name"] == "timing_advance_vs_rpm"
    assert r["severity"] == "warning"
    assert r["actual"] == 8.0
    assert r["expected"] == 22.0
    assert r["deviation"] == -14.0
    # JSON round-trips through the schema.
    assert AnomalyEvent.from_dict(ev.to_dict()).source == "behavior"


# ---------------------------------------------------------------------------
# AnomalyEventEmitter.emit_behavior
# ---------------------------------------------------------------------------


def _emit(
    emitter: AnomalyEventEmitter,
    *,
    model: str = "coolant_warmup",
    band: str = "critical",
    ts: str = "2026-05-13T12:00:00+00:00",
) -> AnomalyEvent | None:
    return emitter.emit_behavior(
        model=model,
        kind="surface_1d",
        channel="obd:COOLANT",
        timestamp=ts,
        severity_band=band,
        score=1.0 if band == "critical" else 0.5,
        threshold=0.5,
        actual=55.0,
        expected=88.0,
        deviation=-33.0,
        operating_mode="idle",
    )


def test_emit_behavior_suppresses_ok_band(tmp_path: Path) -> None:
    emitter = AnomalyEventEmitter(AnomalyEventLogger(tmp_path), min_seconds_between_events=0)
    assert _emit(emitter, band="ok") is None
    assert emitter.counters.suppressed_not_anomaly == 1
    assert read_events_dir(tmp_path) == []


def test_emit_behavior_emits_warning_and_critical(tmp_path: Path) -> None:
    emitter = AnomalyEventEmitter(AnomalyEventLogger(tmp_path), min_seconds_between_events=0)
    warn = _emit(emitter, band="warning")
    crit = _emit(emitter, band="critical")
    assert warn is not None and warn.source == "behavior"
    assert crit is not None
    assert emitter.counters.emitted == 2
    assert len(read_events_dir(tmp_path)) == 2


def test_emit_behavior_dedupes_per_model_independently(tmp_path: Path) -> None:
    emitter = AnomalyEventEmitter(AnomalyEventLogger(tmp_path), min_seconds_between_events=1.0)
    first = _emit(emitter, model="coolant_warmup", ts="2026-05-13T12:00:00+00:00")
    assert first is not None
    # Same model 0.5s later → deduped.
    dup = _emit(emitter, model="coolant_warmup", ts="2026-05-13T12:00:00.5+00:00")
    assert dup is None
    assert emitter.counters.suppressed_dedupe == 1
    # A *different* behavior model at the same instant is not cross-suppressed.
    other = _emit(emitter, model="fuel_trim_vs_rpm_load", ts="2026-05-13T12:00:00.5+00:00")
    assert other is not None
    assert emitter.counters.emitted == 2
