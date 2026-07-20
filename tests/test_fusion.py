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
    # 6.2: a lone source, however severe, never sets the fused verdict by
    # itself — is_anomaly requires min_agreement (default 2) independent
    # firing sources on top of severity_threshold.
    one = fs.fuse([lstm_source(0.05, 0.05)], timestamp="t", operating_mode=None)
    assert one.severity >= 0.5 and not one.is_anomaly and one.agreement == 1
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


def test_fuse_agreement_gate_two_independent_sources_sets_is_anomaly() -> None:
    """6.2 (operator-approved decision): two independent firing sources
    clear min_agreement (default 2), so the fused verdict fires."""
    fs = FusionScorer(FusionConfig(severity_threshold=0.5, min_agreement=2))
    fused = fs.fuse(
        [lstm_source(0.10, 0.05), isolation_forest_source(0.0, -0.0645, scale=0.05)],
        timestamp="t",
        operating_mode=None,
    )
    assert fused.agreement == 2
    assert fused.is_anomaly


def test_fuse_agreement_gate_single_source_never_sets_is_anomaly_but_per_source_still_emits(
    tmp_path: Path,
) -> None:
    """6.2: a lone firing source, however severe, never sets the fused
    verdict by itself — but fusion never suppresses the unrelated
    per-source event stream, so the IF event still emits on its own path."""
    emitter = AnomalyEventEmitter(AnomalyEventLogger(tmp_path), min_seconds_between_events=0)
    fs = FusionScorer(FusionConfig(severity_threshold=0.5, min_agreement=2))

    if_event = emitter.emit_isolation_forest_row(
        _if_row(), model_version="if_v1", timestamp="2026-05-13T12:00:00+00:00"
    )
    assert if_event is not None  # per-source path is untouched by fusion

    fused = fs.fuse(
        [isolation_forest_source(0.2, 0.1, scale=0.05)],
        timestamp="2026-05-13T12:00:00+00:00",
        operating_mode="idle",
    )
    assert fused.severity >= 0.5  # a single source can still reach severity...
    assert fused.agreement == 1
    assert not fused.is_anomaly  # ...but not agreement
    assert emitter.emit_fused(fused, model_version="v", severity_threshold=0.5) is None
    assert emitter.counters.suppressed_not_anomaly == 1  # the fused suppression only


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
        # 6.2: two independent sources so agreement (2) clears min_agreement
        # and the fused event actually emits.
        return fs.fuse(
            [lstm_source(0.2, 0.05), isolation_forest_source(0.0, -0.0645, scale=0.05)],
            timestamp=ts,
            operating_mode="all",
        )

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


def test_fused_dedupe_ignores_model_version_churn_from_hot_swap(tmp_path: Path) -> None:
    """6.4: ``model_version`` on the fused event embeds BOTH backing
    detectors' versions purely for provenance; a hot-swap of either mid-
    drive changes that string, so the dedupe *identity* must not be keyed
    on it — otherwise a swap right inside the refractory window bursts a
    duplicate alert for the same underlying fault."""
    emitter = AnomalyEventEmitter(AnomalyEventLogger(tmp_path), min_seconds_between_events=30.0)
    fs = FusionScorer(FusionConfig())
    sources = [lstm_source(0.2, 0.05), isolation_forest_source(0.0, -0.0645, scale=0.05)]

    first = emitter.emit_fused(
        fs.fuse(sources, timestamp="2026-05-13T12:00:00+00:00", operating_mode="idle"),
        model_version="if=v1|lstm=v1",
        severity_threshold=0.5,
    )
    assert first is not None
    assert first.model_version == "if=v1|lstm=v1"  # provenance kept on the payload

    # A hot-swap 5s later changes the provenance string, well inside the 30s
    # refractory window.
    second = emitter.emit_fused(
        fs.fuse(sources, timestamp="2026-05-13T12:00:05+00:00", operating_mode="idle"),
        model_version="if=v1|lstm=v2",
        severity_threshold=0.5,
    )
    assert second is None  # deduped as the SAME alert, not a burst
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


def test_persistence_gate_m_of_n_catches_intermittent_fire_gap_fire() -> None:
    """6.1: a fire-gap-fire pattern (hits on ticks 1, 3, 5 of a 5-tick span)
    structurally can't sustain a strict 3-consecutive gate but does sustain
    3-of-5 M-of-N — the actual intermittent-fault case 6.1 exists for."""
    from src.alerts.fusion import PersistenceGate

    pattern = [True, False, True, False, True]

    strict = PersistenceGate(3)  # window defaults to required: pre-6.1 shape
    assert not any(strict.observe("k", v, now=float(i)) for i, v in enumerate(pattern))

    m_of_n = PersistenceGate(3, window=5)
    results = [m_of_n.observe("k", v, now=float(i)) for i, v in enumerate(pattern)]
    assert results[-1] is True  # the 3rd hit (tick 5) completes 3-of-5
    assert m_of_n.active("k")


def test_persistence_gate_m_of_n_still_suppresses_isolated_flicker() -> None:
    """Flood protection is preserved: a lone hit among mostly misses in the
    window still doesn't sustain — M-of-N needs the full M hits, not just
    "some" evidence within the window."""
    from src.alerts.fusion import PersistenceGate

    gate = PersistenceGate(3, window=5)
    pattern = [False, False, True, False, False]
    assert not any(gate.observe("k", v, now=float(i)) for i, v in enumerate(pattern))
    assert not gate.active("k")


def test_persistence_gate_wall_clock_bound_evicts_stale_hits_across_a_gap() -> None:
    """6.1: the window is bounded by N ticks OR T seconds, whichever is
    tighter — a long data/replay gap can't let hits from before the gap
    count toward a "fresh" streak once ticks resume, even though the raw
    tick count alone would still fit inside the N-tick window."""
    from src.alerts.fusion import PersistenceGate

    gate = PersistenceGate(3, window=5, window_seconds=10.0)
    assert not gate.observe("k", True, now=0.0)
    assert not gate.observe("k", True, now=1.0)
    # A long real-world gap before the next tick resumes — the two hits
    # above are now outside the 10s wall-clock bound.
    assert not gate.observe("k", True, now=100.0)  # only 1 hit left in-window
    assert not gate.observe("k", True, now=101.0)  # 2 hits
    assert gate.observe("k", True, now=102.0)  # 3rd *post-gap* hit sustains


def test_persistence_gate_missing_tick_freezes_not_resets() -> None:
    """6.1 — ``firing=None`` (feature blindness / off-mode / no verdict yet)
    is a genuine no-op: it leaves the window untouched, so a run of missing
    ticks freezes rather than decays an in-progress streak (contrast with a
    real miss, which does displace a stale hit)."""
    from src.alerts.fusion import PersistenceGate

    gate = PersistenceGate(3, window=5)
    assert not gate.observe("k", True, now=0.0)
    assert not gate.observe("k", True, now=1.0)
    # Missing ticks don't touch the 2-hit state already accumulated.
    assert gate.observe("k", None, now=2.0) is False
    assert gate.observe("k", None, now=3.0) is False
    assert not gate.active("k")
    # The 3rd real hit still completes the M-of-N requirement afterward.
    assert gate.observe("k", True, now=4.0)


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
