"""Tests for the Phase 7.2 fusion layer + fused event emission."""

from __future__ import annotations

from pathlib import Path

import pandas as pd

from src.alerts.emitter import AnomalyEventEmitter
from src.alerts.event_logger import AnomalyEventLogger, read_events_dir
from src.alerts.events import AnomalyEvent
from src.alerts.fusion import (
    FusionScorer,
    isolation_forest_source,
    lstm_source,
)
from src.config import FusionConfig

# ---------------------------------------------------------------------------
# Source builders
# ---------------------------------------------------------------------------


def test_if_source_not_fired_below_threshold() -> None:
    src = isolation_forest_source(-0.20, -0.0645, scale=0.05)
    assert not src.fired
    assert src.severity == 0.0


def test_if_source_scale_gradient_no_saturation() -> None:
    # Just over threshold → ~0.5; one scale beyond → ~1.0. The negated
    # decision-function threshold is negative; a threshold-relative ratio
    # would pathologically saturate, the held-out scale must not.
    near = isolation_forest_source(-0.0645 + 1e-6, -0.0645, scale=0.05)
    far = isolation_forest_source(-0.0645 + 0.05, -0.0645, scale=0.05)
    assert near.fired and far.fired
    assert near.severity == abs(near.severity)  # finite
    assert 0.5 <= near.severity < 0.55
    assert far.severity >= 0.99


def test_if_source_without_scale_uses_unscaled_fired() -> None:
    src = isolation_forest_source(0.9, -0.0645, scale=None)
    assert src.fired
    assert src.severity == 0.75  # _UNSCALED_FIRED_SEVERITY


def test_if_source_none_inputs_not_fired() -> None:
    assert not isolation_forest_source(None, -0.06, scale=0.05).fired
    assert not isolation_forest_source(0.1, None, scale=0.05).fired


def test_lstm_source_threshold_relative() -> None:
    assert not lstm_source(0.04, 0.05).fired
    boundary = lstm_source(0.05, 0.05)
    double = lstm_source(0.10, 0.05)
    assert boundary.fired and double.fired
    assert boundary.severity == 0.5
    assert double.severity == 1.0


def test_behavior_source_bands() -> None:
    fs = FusionScorer(FusionConfig())
    assert not fs.behavior_source("behavior:x", "ok").fired
    warn = fs.behavior_source("behavior:x", "warning")
    crit = fs.behavior_source("behavior:x", "critical")
    assert warn.fired and warn.severity == 0.5
    assert crit.fired and crit.severity == 1.0


# ---------------------------------------------------------------------------
# Fusion
# ---------------------------------------------------------------------------


def test_fuse_nothing_firing_is_not_anomaly() -> None:
    fs = FusionScorer(FusionConfig())
    r = fs.fuse(
        [isolation_forest_source(-0.5, -0.0645, scale=0.05)],
        timestamp="t",
        operating_mode="all",
    )
    assert r.severity == 0.0
    assert not r.is_anomaly
    assert r.contributing == ()
    assert r.agreement == 0


def test_fuse_agreement_boost_and_clamp() -> None:
    fs = FusionScorer(FusionConfig(agreement_boost=0.25, severity_threshold=0.5))
    one = fs.fuse([lstm_source(0.05, 0.05)], timestamp="t", operating_mode=None)
    assert one.is_anomaly and one.agreement == 1
    three = fs.fuse(
        [
            lstm_source(0.05, 0.05),  # base 0.5
            isolation_forest_source(0.0, -0.0645, scale=0.05),  # ~1.0
            fs.behavior_source("behavior:coolant", "warning"),  # 0.5
        ],
        timestamp="t",
        operating_mode="all",
    )
    assert three.agreement == 3
    assert three.severity == 1.0  # max(0.5,1.0,0.5)+0.25*2 clamped
    assert set(three.contributing) == {
        "lstm_autoencoder",
        "isolation_forest",
        "behavior:coolant",
    }


def test_fuse_unthresholded_source_retained_but_inert() -> None:
    fs = FusionScorer(FusionConfig())
    r = fs.fuse(
        [isolation_forest_source(999.0, None, scale=None)],
        timestamp="t",
        operating_mode=None,
    )
    assert not r.is_anomaly
    assert len(r.sources) == 1  # kept for evidence
    assert r.contributing == ()


def test_disabled_fusion_reports_disabled() -> None:
    assert not FusionScorer(FusionConfig(enabled=False)).enabled


# ---------------------------------------------------------------------------
# Fused AnomalyEvent + emitter
# ---------------------------------------------------------------------------


def test_from_fused_event_shape() -> None:
    fs = FusionScorer(FusionConfig())
    fused = fs.fuse(
        [lstm_source(0.10, 0.05), isolation_forest_source(0.0, -0.0645, scale=0.05)],
        timestamp="2026-05-13T12:00:00+00:00",
        operating_mode="city",
    )
    ev = AnomalyEvent.from_fused(fused, model_version="if=v1|lstm=v2", severity_threshold=0.5)
    assert ev.source == "fused"
    assert ev.score == fused.severity
    assert ev.threshold == 0.5
    assert ev.operating_mode == "city"
    assert set(ev.affected_channels) == {"lstm_autoencoder", "isolation_forest"}
    assert ev.detail["agreement"] == 2
    assert "isolation_forest" in ev.detail["sources"]
    assert ev.detail["sources"]["lstm_autoencoder"]["fired"] is True
    # JSON round-trips
    assert AnomalyEvent.from_dict(ev.to_dict()).source == "fused"


def test_emit_fused_suppresses_when_not_anomaly(tmp_path: Path) -> None:
    emitter = AnomalyEventEmitter(AnomalyEventLogger(tmp_path), min_seconds_between_events=0)
    fs = FusionScorer(FusionConfig())
    fused = fs.fuse(
        [isolation_forest_source(-0.5, -0.0645, scale=0.05)],
        timestamp="2026-05-13T12:00:00+00:00",
        operating_mode=None,
    )
    assert emitter.emit_fused(fused, model_version="v", severity_threshold=0.5) is None
    assert emitter.counters.suppressed_not_anomaly == 1
    assert read_events_dir(tmp_path) == []


def test_emit_fused_logs_and_dedupes(tmp_path: Path) -> None:
    emitter = AnomalyEventEmitter(AnomalyEventLogger(tmp_path), min_seconds_between_events=1.0)
    fs = FusionScorer(FusionConfig())

    def _fused(ts: str):
        return fs.fuse([lstm_source(0.2, 0.05)], timestamp=ts, operating_mode="all")

    first = emitter.emit_fused(
        _fused("2026-05-13T12:00:00+00:00"), model_version="m", severity_threshold=0.5
    )
    assert first is not None and first.source == "fused"
    # 0.5s later, same (source, model_version) → deduped
    dup = emitter.emit_fused(
        _fused("2026-05-13T12:00:00.500000+00:00"), model_version="m", severity_threshold=0.5
    )
    assert dup is None
    assert emitter.counters.suppressed_dedupe == 1
    assert emitter.counters.emitted == 1
    assert len(read_events_dir(tmp_path)) == 1


# ---------------------------------------------------------------------------
# Calibrated IF emit path (7.2 decision rule)
# ---------------------------------------------------------------------------


def _if_row(**over: object) -> pd.Series:
    base: dict[str, object] = {
        "isolation_forest_score": 0.2,
        "isolation_forest_label": 1,  # legacy "normal"
        "isolation_forest_missing_features": False,
        "isolation_forest_model_mode": "idle",
        "isolation_forest_calibrated_threshold": 0.1,
        "isolation_forest_calibrated_anomaly": True,
        "operating_mode": "idle",
    }
    base.update(over)
    return pd.Series(base)


def test_emit_if_uses_calibrated_verdict_over_legacy_label(tmp_path: Path) -> None:
    emitter = AnomalyEventEmitter(AnomalyEventLogger(tmp_path), min_seconds_between_events=0)
    # Legacy label says normal (1) but the calibrated verdict says anomaly.
    ev = emitter.emit_isolation_forest_row(
        _if_row(), model_version="if_v1", timestamp="2026-05-13T12:00:00+00:00"
    )
    assert ev is not None
    assert ev.source == "isolation_forest"
    assert ev.threshold == 0.1  # calibrated boundary surfaced on the event
    assert emitter.counters.emitted == 1


def test_emit_if_calibrated_normal_is_suppressed(tmp_path: Path) -> None:
    emitter = AnomalyEventEmitter(AnomalyEventLogger(tmp_path), min_seconds_between_events=0)
    ev = emitter.emit_isolation_forest_row(
        _if_row(isolation_forest_calibrated_anomaly=False, isolation_forest_label=-1),
        model_version="if_v1",
        timestamp="2026-05-13T12:00:00+00:00",
    )
    assert ev is None  # calibrated verdict wins over the legacy -1 label
    assert emitter.counters.suppressed_not_anomaly == 1


def test_emit_if_falls_back_to_label_when_uncalibrated(tmp_path: Path) -> None:
    emitter = AnomalyEventEmitter(AnomalyEventLogger(tmp_path), min_seconds_between_events=0)
    row = _if_row(
        isolation_forest_calibrated_anomaly=pd.NA,
        isolation_forest_calibrated_threshold=float("nan"),
        isolation_forest_label=-1,
    )
    ev = emitter.emit_isolation_forest_row(
        row, model_version="if_v1", timestamp="2026-05-13T12:00:00+00:00"
    )
    assert ev is not None  # legacy label rule
    assert ev.threshold is None  # no calibrated threshold for this mode


# ---------------------------------------------------------------------------
# Phase 7.5: persistence gate + grouped agreement
# ---------------------------------------------------------------------------


def test_persistence_gate_requires_consecutive_ticks() -> None:
    from src.alerts.fusion import PersistenceGate

    gate = PersistenceGate(3)
    assert not gate.observe("lstm", True)
    assert not gate.observe("lstm", True)
    assert gate.observe("lstm", True)  # third consecutive tick latches
    assert gate.active("lstm")
    assert gate.observe("lstm", True)  # stays latched while firing
    # One quiet tick resets the streak entirely.
    assert not gate.observe("lstm", False)
    assert not gate.active("lstm")
    assert not gate.observe("lstm", True)
    assert gate.suppressed == 3  # ticks 1, 2 and the post-reset tick


def test_persistence_gate_keys_are_independent() -> None:
    from src.alerts.fusion import PersistenceGate

    gate = PersistenceGate(2)
    assert not gate.observe("a", True)
    assert not gate.observe("b", True)
    assert gate.observe("a", True)
    assert not gate.active("b")


def test_downgrade_unsustained_keeps_evidence_but_kills_severity() -> None:
    from src.alerts.fusion import downgrade_unsustained

    src = lstm_source(0.10, 0.05)
    assert src.fired
    gated = downgrade_unsustained(src, sustained=False)
    assert not gated.fired and gated.severity == 0.0
    assert gated.score == src.score  # evidence retained
    assert downgrade_unsustained(src, sustained=True) is src


def test_fuse_grouped_siblings_count_as_one_agreeing_voice() -> None:
    """Two surfaces grading the same output channel are shared evidence —
    no agreement boost between them; an independent detector still boosts."""
    fs = FusionScorer(FusionConfig(agreement_boost=0.25, severity_threshold=0.5))
    siblings = [
        fs.behavior_source(
            "behavior:timing_advance_vs_rpm_load", "warning", group="behavior:obd:TIMING_ADVANCE"
        ),
        fs.behavior_source(
            "behavior:timing_advance_vs_rpm_iat", "warning", group="behavior:obd:TIMING_ADVANCE"
        ),
    ]
    fused = fs.fuse(list(siblings), timestamp="t", operating_mode=None)
    assert fused.severity == 0.5  # max(0.5, 0.5) + 0 boost — one group
    assert fused.agreement == 2  # both still listed as contributing evidence

    with_if = fs.fuse(
        [*siblings, lstm_source(0.05, 0.05)],
        timestamp="t",
        operating_mode=None,
    )
    assert with_if.severity == 0.75  # two independent groups → one boost
