"""Tests for ``src.control.trends`` (Tasks 4.5/4.6, eval-longitudinal-and-dtc.md):
the corpus trend summary (read side) and trend alerting (write side).
"""

from __future__ import annotations

import json
from pathlib import Path

from src.alerts.emitter import AnomalyEventEmitter
from src.alerts.event_logger import AnomalyEventLogger
from src.analysis.fingerprint import write_fingerprint
from src.analysis.reference_epoch import ReferenceEpoch, StatSummary, write_reference_epoch
from src.config import CanaryConfig
from src.control.trends import corpus_trend_summary, evaluate_and_alert_trends
from tests.configs import READ_ONLY_INPUTS, isolated_config

REPO_ROOT = Path(__file__).resolve().parents[1]
EXAMPLE_YAML = REPO_ROOT / "canary.example.yaml"


def _config(tmp_path: Path) -> CanaryConfig:
    # Every writable path under tmp_path, the session catalog included: the
    # summary opens ``daemon.catalog_db``, which defaulted to the repo's real
    # data/control/catalog.db (R07.2).
    return isolated_config(
        tmp_path,
        "example",
        keep=READ_ONLY_INPUTS,
        storage=tmp_path / "storage",
        daemon={
            "reference_epoch": tmp_path / "reference_epoch.json",
            "trend_alert_state": tmp_path / "trend_alert_state.json",
            "anomalies_dir": tmp_path / "anomalies",
        },
    )


def _summary(mean: float, std: float) -> StatSummary:
    return StatSummary(
        mean=mean,
        std=std,
        quantiles={"p10": mean - std, "p50": mean, "p90": mean + std},
        n=5,
    )


def _write_epoch(config: CanaryConfig, stats: dict[str, StatSummary]) -> None:
    epoch = ReferenceEpoch(
        frozen_at="2026-06-01T00:00:00+00:00",
        sessions=("s0", "s1", "s2", "s3", "s4"),
        stats=stats,
        notes="test epoch",
    )
    write_reference_epoch(config.daemon.reference_epoch, epoch)


def _write_session_fingerprint(
    config: CanaryConfig, session_id: str, stats: dict[str, float]
) -> Path:
    session_dir = config.collection.storage_path / session_id
    session_dir.mkdir(parents=True, exist_ok=True)
    write_fingerprint(session_dir, stats)
    return session_dir


# --------------------------------------------------------------------------- #
# 4.5 — corpus_trend_summary
# --------------------------------------------------------------------------- #


def test_summary_unavailable_without_a_frozen_reference_epoch(tmp_path: Path) -> None:
    config = _config(tmp_path)
    summary = corpus_trend_summary(config)
    assert summary["available"] is False
    assert "reason" in summary
    assert summary["stats"] == {}


def test_summary_reports_latest_value_and_band(tmp_path: Path) -> None:
    config = _config(tmp_path)
    _write_epoch(config, {"idle_rpm_mean": _summary(800.0, 10.0)})
    _write_session_fingerprint(config, "2026-06-01T000000", {"idle_rpm_mean": 800.0})
    _write_session_fingerprint(config, "2026-06-01T000001", {"idle_rpm_mean": 810.0})

    summary = corpus_trend_summary(config)
    assert summary["available"] is True
    assert summary["reference_sessions"] == 5
    stat = summary["stats"]["idle_rpm_mean"]
    assert stat["latest_session"] == "2026-06-01T000001"
    assert stat["latest_value"] == 810.0
    assert stat["reference_band"] == [790.0, 810.0]
    assert stat["n"] == 2


def test_summary_ignores_sessions_missing_fingerprints(tmp_path: Path) -> None:
    config = _config(tmp_path)
    _write_epoch(config, {"idle_rpm_mean": _summary(800.0, 10.0)})
    _write_session_fingerprint(config, "2026-06-01T000000", {"idle_rpm_mean": 800.0})
    (config.collection.storage_path / "2026-06-01T000001").mkdir(parents=True)  # no fingerprint

    summary = corpus_trend_summary(config)
    stat = summary["stats"]["idle_rpm_mean"]
    assert stat["n"] == 1


# --------------------------------------------------------------------------- #
# Task 6.4(c) — reads fingerprints from the catalog when it's populated
# --------------------------------------------------------------------------- #


def test_summary_reads_the_catalog_instead_of_the_sidecar_once_populated(tmp_path: Path) -> None:
    """Proof the catalog is actually consulted (not just a no-op path): the
    catalog row carries a DIFFERENT value than the on-disk sidecar, and the
    summary reflects the catalog's value."""
    import dataclasses

    from src.storage.catalog import SessionCatalog

    config = _config(tmp_path)
    config = config.model_copy(
        update={"daemon": config.daemon.model_copy(update={"catalog_db": tmp_path / "catalog.db"})}
    )
    _write_epoch(config, {"idle_rpm_mean": _summary(800.0, 10.0)})
    session_dir = _write_session_fingerprint(config, "2026-06-01T000000", {"idle_rpm_mean": 800.0})

    with SessionCatalog(config.daemon.catalog_db) as catalog:
        catalog.record_session(session_dir)
        # Overwrite just the fingerprint column with a distinguishable value —
        # if the summary reflects THIS, the catalog path was taken.
        row = catalog.get_session(session_dir.name)
        assert row is not None
        catalog._upsert(dataclasses.replace(row, fingerprint={"idle_rpm_mean": 950.0}))

    summary = corpus_trend_summary(config)
    assert summary["stats"]["idle_rpm_mean"]["latest_value"] == 950.0


def test_summary_falls_back_to_sidecars_when_catalog_never_built(tmp_path: Path) -> None:
    """An unset/never-built catalog is a pure no-op — same result as before
    6.4(c), so a deployment that hasn't run ``scripts/catalog.py rebuild``
    yet sees no behavior change."""
    config = _config(tmp_path)
    config = config.model_copy(
        update={"daemon": config.daemon.model_copy(update={"catalog_db": tmp_path / "catalog.db"})}
    )
    _write_epoch(config, {"idle_rpm_mean": _summary(800.0, 10.0)})
    _write_session_fingerprint(config, "2026-06-01T000000", {"idle_rpm_mean": 800.0})

    assert not config.daemon.catalog_db.exists()
    summary = corpus_trend_summary(config)
    assert summary["stats"]["idle_rpm_mean"]["latest_value"] == 800.0


# --------------------------------------------------------------------------- #
# 4.6 — evaluate_and_alert_trends
# --------------------------------------------------------------------------- #

#: Deterministic sustained-shift scenario (matches the hand-verified CUSUM
#: example in test_trends.py): ref mean=800/std=10, four sessions each at
#: 820.0 (z=2.0/tick, k=0.5, h=5.0 default) alarms at the 4th (index 3):
#: Sh sequence 1.5, 3.0, 4.5, 6.0.
_ALARM_SESSIONS = [
    "2026-06-01T000000",
    "2026-06-01T000001",
    "2026-06-01T000002",
    "2026-06-01T000003",
]


def _seed_alarm_scenario(config: CanaryConfig) -> None:
    _write_epoch(
        config,
        {
            "idle_rpm_mean": _summary(800.0, 10.0),
            "stationary_stat": _summary(5.0, 1.0),
        },
    )
    for session_id in _ALARM_SESSIONS:
        _write_session_fingerprint(
            config, session_id, {"idle_rpm_mean": 820.0, "stationary_stat": 5.0}
        )


def test_no_reference_epoch_emits_nothing(tmp_path: Path) -> None:
    config = _config(tmp_path)
    _write_session_fingerprint(config, "2026-06-01T000000", {"idle_rpm_mean": 999.0})
    assert evaluate_and_alert_trends(config, "2026-06-01T000000") == []


def test_stationary_stat_never_emits(tmp_path: Path) -> None:
    config = _config(tmp_path)
    _seed_alarm_scenario(config)
    events = evaluate_and_alert_trends(config, _ALARM_SESSIONS[-1])
    assert all(event.detail["stat"] != "stationary_stat" for event in events)


def test_sustained_alarm_emits_exactly_one_trend_event(tmp_path: Path) -> None:
    config = _config(tmp_path)
    _seed_alarm_scenario(config)

    events = evaluate_and_alert_trends(config, _ALARM_SESSIONS[-1])
    assert len(events) == 1
    event = events[0]
    assert event.source == "trend"
    assert event.session == _ALARM_SESSIONS[-1]
    assert event.detail["stat"] == "idle_rpm_mean"
    assert event.detail["direction"] == "upper"

    # Actually persisted to disk (standalone-emitter fallback), not just returned.
    log_files = list(config.daemon.anomalies_dir.glob("anomalies-*.jsonl"))
    assert len(log_files) == 1
    lines = [json.loads(line) for line in log_files[0].read_text().splitlines() if line]
    assert len(lines) == 1
    assert lines[0]["source"] == "trend"


def test_reevaluating_the_same_session_does_not_double_emit(tmp_path: Path) -> None:
    config = _config(tmp_path)
    _seed_alarm_scenario(config)

    first = evaluate_and_alert_trends(config, _ALARM_SESSIONS[-1])
    assert len(first) == 1

    second = evaluate_and_alert_trends(config, _ALARM_SESSIONS[-1])
    assert second == []

    # Still only one line on disk after the re-evaluation.
    log_files = list(config.daemon.anomalies_dir.glob("anomalies-*.jsonl"))
    lines = [line for line in log_files[0].read_text().splitlines() if line]
    assert len(lines) == 1


def test_a_new_session_still_in_alarm_gets_its_own_fresh_event(tmp_path: Path) -> None:
    config = _config(tmp_path)
    _seed_alarm_scenario(config)
    first = evaluate_and_alert_trends(config, _ALARM_SESSIONS[-1])
    assert len(first) == 1

    next_session = "2026-06-01T000004"
    _write_session_fingerprint(
        config, next_session, {"idle_rpm_mean": 820.0, "stationary_stat": 5.0}
    )
    second = evaluate_and_alert_trends(config, next_session)
    assert len(second) == 1
    assert second[0].session == next_session


def test_evaluate_and_alert_trends_uses_the_given_emitter_when_provided(tmp_path: Path) -> None:
    config = _config(tmp_path)
    _seed_alarm_scenario(config)

    other_dir = tmp_path / "other-anomalies"
    other_dir.mkdir()
    emitter = AnomalyEventEmitter(AnomalyEventLogger(other_dir), min_seconds_between_events=0)

    events = evaluate_and_alert_trends(config, _ALARM_SESSIONS[-1], emitter=emitter)
    assert len(events) == 1
    # Went through the CALLER's emitter/logger, not a standalone one over
    # config.daemon.anomalies_dir.
    assert not config.daemon.anomalies_dir.exists()
    assert list(other_dir.glob("anomalies-*.jsonl"))


def test_alert_state_marker_persists_last_alerted_session_per_stat(tmp_path: Path) -> None:
    config = _config(tmp_path)
    _seed_alarm_scenario(config)
    evaluate_and_alert_trends(config, _ALARM_SESSIONS[-1])

    state = json.loads(config.daemon.trend_alert_state.read_text())
    assert state == {"idle_rpm_mean": _ALARM_SESSIONS[-1]}
