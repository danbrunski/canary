"""Tests for threshold tuning + alert suppression (Task 6.25)."""

from __future__ import annotations

from pathlib import Path

from fastapi.testclient import TestClient

from src.alerts.emitter import AnomalyEventEmitter
from src.alerts.event_logger import AnomalyEventLogger
from src.alerts.tuning import (
    AlertTuner,
    DebouncePolicy,
    SuppressionRule,
    ThresholdOverride,
    TuningConfig,
    estimate_before_after,
    load_tuning_config,
    load_tuning_version,
    save_tuning_config,
)
from src.config import CanaryConfig
from src.dashboard.api import create_app
from src.models.inference import SequenceInferenceResult
from tests.configs import isolated_config
from tests.fakes import lstm_result


def _event(**over: object) -> dict[str, object]:
    base = {
        "timestamp": "2026-05-16T10:00:00+00:00",
        "source": "lstm_autoencoder",
        "model_version": "lstm_v1",
        "score": 5.0,
        "threshold": 3.0,
        "operating_mode": "idle",
        "affected_channels": ["obd:RPM"],
    }
    base.update(over)
    return base


# ---------------------------------------------------------------------------
# Threshold overrides
# ---------------------------------------------------------------------------


def test_multiplier_override_raises_bar_and_suppresses() -> None:
    cfg = TuningConfig(
        threshold_overrides=[ThresholdOverride(source="lstm_autoencoder", multiplier=2.0)]
    )
    tuner = AlertTuner(cfg)
    # base threshold 3.0 × 2 = 6.0; score 5.0 < 6.0 → suppressed
    d = tuner.decide(_event(score=5.0), now=0.0)
    assert d.emit is False
    assert d.effective_threshold == 6.0
    # score 7.0 ≥ 6.0 → still emits
    assert tuner.decide(_event(score=7.0), now=0.0).emit is True


def test_override_never_lowers_the_bar() -> None:
    tuner = AlertTuner(
        TuningConfig(
            threshold_overrides=[ThresholdOverride(source="lstm_autoencoder", multiplier=0.1)]
        )
    )
    # 3.0 × 0.1 = 0.3 < 3.0 → bar stays at 3.0, score 5 emits
    d = tuner.decide(_event(score=5.0), now=0.0)
    assert d.emit is True
    assert d.effective_threshold == 3.0


def test_override_scope_by_operating_mode() -> None:
    tuner = AlertTuner(
        TuningConfig(
            threshold_overrides=[
                ThresholdOverride(
                    source="lstm_autoencoder", operating_mode="highway", absolute_threshold=99.0
                )
            ]
        )
    )
    assert tuner.decide(_event(operating_mode="idle"), now=0.0).emit is True
    assert tuner.decide(_event(operating_mode="highway"), now=0.0).emit is False


# ---------------------------------------------------------------------------
# Suppression rules
# ---------------------------------------------------------------------------


def test_suppression_rule_channel_and_max_score() -> None:
    cfg = TuningConfig(
        suppression_rules=[
            SuppressionRule(
                name="ac_clutch",
                operating_mode="idle",
                channel_contains="RPM",
                max_score=6.0,
                reason="A/C compressor cycle",
            )
        ]
    )
    tuner = AlertTuner(cfg)
    d = tuner.decide(_event(score=5.0), now=0.0)
    assert d.emit is False and "A/C compressor" in (d.reason or "")
    # A genuinely severe excursion (above max_score) still gets through.
    assert tuner.decide(_event(score=9.0), now=0.0).emit is True
    # Different channel → rule doesn't match.
    assert tuner.decide(_event(affected_channels=["obd:MAF"]), now=0.0).emit is True


# ---------------------------------------------------------------------------
# Debounce / cool-down / hysteresis
# ---------------------------------------------------------------------------


def test_debounce_requires_consecutive() -> None:
    tuner = AlertTuner(TuningConfig(debounce_policies=[DebouncePolicy(min_consecutive=3)]))
    assert tuner.decide(_event(), now=0.0).emit is False  # 1/3
    assert tuner.decide(_event(), now=1.0).emit is False  # 2/3
    assert tuner.decide(_event(), now=2.0).emit is True  # 3/3


def test_has_debounce_override() -> None:
    """6.3 — the daemon's PersistenceGate wiring uses this public accessor
    (instead of reaching for the private ``_policy_for``) to decide whether
    a tuner debounce override fully owns a (source, mode) key's persistence
    requirement."""
    tuner = AlertTuner(
        TuningConfig(
            debounce_policies=[DebouncePolicy(source="isolation_forest", operating_mode="idle")]
        )
    )
    assert tuner.has_debounce_override("isolation_forest", "idle") is True
    assert tuner.has_debounce_override("isolation_forest", "city") is False
    assert tuner.has_debounce_override("lstm_autoencoder", "idle") is False
    assert AlertTuner(TuningConfig()).has_debounce_override("isolation_forest", "idle") is False


def test_cooldown_and_hysteresis() -> None:
    tuner = AlertTuner(
        TuningConfig(debounce_policies=[DebouncePolicy(cooldown_seconds=10.0, rearm_below=2.0)])
    )
    first = tuner.decide(_event(score=5.0), now=100.0)
    assert first.emit is True
    tuner.notify_emitted(_event(score=5.0), now=100.0)
    # Within cool-down → muted
    assert tuner.decide(_event(score=5.0), now=105.0).emit is False
    # Past cool-down but score still high → hysteresis keeps it muted
    assert tuner.decide(_event(score=5.0), now=200.0).emit is False
    # Score drops below rearm → re-armed, next high score emits
    assert tuner.decide(_event(score=1.0), now=210.0).emit is False
    assert tuner.decide(_event(score=5.0), now=220.0).emit is True


# ---------------------------------------------------------------------------
# Estimate (before/after — Risk R11)
# ---------------------------------------------------------------------------


def test_estimate_before_after() -> None:
    events = [_event(score=s, operating_mode="idle") for s in (4.0, 5.0, 8.0)]
    old = TuningConfig()
    new = TuningConfig(
        suppression_rules=[SuppressionRule(name="idle_mute", operating_mode="idle", max_score=6.0)]
    )
    result = estimate_before_after(events, old, new)
    assert result["before"]["would_suppress"] == 0
    assert result["after"]["would_suppress"] == 2  # the 4.0 and 5.0
    assert result["newly_suppressed"] == 2


# ---------------------------------------------------------------------------
# Persistence + versioned/reversible audit
# ---------------------------------------------------------------------------


def test_save_load_and_revert(tmp_path: Path) -> None:
    path = tmp_path / "tuning.yaml"
    audit = tmp_path / "tuning_audit.jsonl"
    v1 = TuningConfig(version=1)
    save_tuning_config(v1, path, audit_path=audit, change="init", by="dan")
    v2 = TuningConfig(
        version=2,
        suppression_rules=[SuppressionRule(name="r", operating_mode="idle")],
    )
    save_tuning_config(v2, path, audit_path=audit, change="add rule", by="dan")
    assert load_tuning_config(path).version == 2
    # Any prior version reconstructable verbatim from the audit snapshot.
    restored = load_tuning_version(1, audit)
    assert restored is not None and restored.suppression_rules == []
    assert load_tuning_version(99, audit) is None


# ---------------------------------------------------------------------------
# Emitter integration
# ---------------------------------------------------------------------------


def _lstm(score: float) -> SequenceInferenceResult:
    """A flagged window scoring ``score`` on ``obd:RPM`` (threshold 3.0)."""
    return lstm_result(
        timestamp="2026-05-16T10:00:00+00:00",
        session="2026-05-16T100000",
        window_start_timestamp="2026-05-16T09:59:50+00:00",
        window_end_timestamp="2026-05-16T10:00:00+00:00",
        global_score=score,
        channel_scores={"obd:RPM": score},
    )


def test_emitter_applies_tuner(tmp_path: Path) -> None:
    logger = AnomalyEventLogger(tmp_path / "anomalies")
    tuner = AlertTuner(
        TuningConfig(
            threshold_overrides=[ThresholdOverride(source="lstm_autoencoder", multiplier=2.0)]
        )
    )
    emitter = AnomalyEventEmitter(logger, min_seconds_between_events=0, tuner=tuner)
    # threshold 3 × 2 = 6; score 5 → suppressed by tuning
    assert emitter.emit_lstm(_lstm(5.0)) is None
    assert emitter.counters.suppressed_tuning == 1
    assert emitter.counters.emitted == 0
    # score 7 ≥ 6 → emitted
    assert emitter.emit_lstm(_lstm(7.0)) is not None
    assert emitter.counters.emitted == 1
    assert emitter.counters.to_dict()["suppressed_tuning"] == 1


# ---------------------------------------------------------------------------
# API
# ---------------------------------------------------------------------------


def _client(tmp_path: Path) -> tuple[TestClient, AlertTuner, CanaryConfig]:
    storage = tmp_path / "storage"
    (storage / "2026-05-17T120000").mkdir(parents=True)
    config = isolated_config(
        tmp_path,
        storage=storage,
        daemon={
            "tuning_config": tmp_path / "tuning.yaml",
            "tuning_audit": tmp_path / "tuning_audit.jsonl",
        },
    )
    tuner = AlertTuner(TuningConfig())
    app = create_app(
        config,
        anomalies_dir=tmp_path / "anomalies",
        control_token="secret",
        alert_tuner=tuner,
    )
    return TestClient(app), tuner, config


def _auth() -> dict[str, str]:
    return {"Authorization": "Bearer secret"}


def test_tuning_api_get_post_mutates_live_tuner(tmp_path: Path) -> None:
    client, tuner, config = _client(tmp_path)
    assert client.get("/control/tuning").status_code == 401

    got = client.get("/control/tuning", headers=_auth())
    assert got.status_code == 200
    assert got.json()["tuning"]["version"] == 1

    new_cfg = {
        "version": 1,
        "suppression_rules": [{"name": "idle_mute", "operating_mode": "idle"}],
    }
    resp = client.post("/control/tuning", headers=_auth(), json={"config": new_cfg, "by": "dan"})
    assert resp.status_code == 200
    assert resp.json()["tuning"]["version"] == 2  # auto-bumped
    # Live tuner mutated in place — no restart.
    assert tuner.config.version == 2
    assert tuner.config.suppression_rules[0].name == "idle_mute"
    # Persisted + auditable.
    assert load_tuning_config(config.daemon.tuning_config).version == 2


def test_tuning_estimate_and_revert(tmp_path: Path) -> None:
    client, tuner, config = _client(tmp_path)
    # Seed one anomaly so the estimate has something to chew on.
    from src.alerts.event_logger import AnomalyEventLogger as _Logger
    from src.alerts.events import AnomalyEvent

    with _Logger(tmp_path / "anomalies") as lg:
        lg.log(
            AnomalyEvent.model_validate(
                {
                    "timestamp": "2026-05-16T10:00:00+00:00",
                    "source": "lstm_autoencoder",
                    "model_version": "lstm_v1",
                    "score": 4.0,
                    "threshold": 3.0,
                    "operating_mode": "idle",
                }
            )
        )
    proposed = {
        "version": 1,
        "suppression_rules": [{"name": "m", "operating_mode": "idle", "max_score": 6.0}],
    }
    est = client.post("/control/tuning/estimate", headers=_auth(), json={"config": proposed})
    assert est.status_code == 200
    assert est.json()["after"]["would_suppress"] == 1

    # Commit it, then revert to v1.
    client.post("/control/tuning", headers=_auth(), json={"config": proposed})
    assert tuner.config.version == 2
    rev = client.post("/control/tuning/revert", headers=_auth(), json={"version": 1})
    assert rev.status_code == 200
    assert tuner.config.version == 3  # revert is itself a new version
    assert tuner.config.suppression_rules == []
    bad = client.post("/control/tuning/revert", headers=_auth(), json={"version": 88})
    assert bad.status_code == 404


def test_tuning_503_when_unconfigured(tmp_path: Path) -> None:
    storage = tmp_path / "s"
    (storage / "x").mkdir(parents=True)
    config = isolated_config(
        tmp_path,
        storage=storage,
        daemon={"tuning_config": tmp_path / "t.yaml", "tuning_audit": tmp_path / "a.jsonl"},
    )
    app = create_app(config, anomalies_dir=tmp_path / "anomalies", control_token="secret")
    resp = TestClient(app).get("/control/tuning", headers=_auth())
    assert resp.status_code == 503
