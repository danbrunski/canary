"""Tests for the end-of-drive digest sidecar (Task 5.5, eval-longitudinal-and-dtc.md)."""

from __future__ import annotations

import json
from datetime import UTC, datetime
from pathlib import Path

import pytest

from src.alerts.event_logger import AnomalyEventLogger
from src.alerts.events import AnomalyEvent
from src.alerts.notifier import format_digest_message
from src.analysis.digest import (
    SessionDigest,
    _fingerprint_top_movers,
    _top_behavior_deviations,
    compute_digest,
    get_or_compute_digest,
    write_digest,
)
from src.analysis.reference_epoch import build_reference_epoch, write_reference_epoch
from src.config import CanaryConfig
from src.decode.dtc_catalog import build_dtc_report
from src.session import DTC_SIDECAR_FILENAME
from src.storage.session_products import DIGEST_FILENAME, read_digest
from src.vehicle.vehicle_ledger import VehicleEvent, VehicleLedgerStore
from tests.builders import RICH_SESSION_END, digest_config, write_rich_session

REPO_ROOT = Path(__file__).resolve().parents[1]
EXAMPLE_YAML = REPO_ROOT / "canary.example.yaml"

SESSION_START = datetime(2026, 5, 20, 18, 0, 0, tzinfo=UTC)


def _session(tmp_path: Path) -> Path:
    return write_rich_session(tmp_path, name="2026-05-20T180000")


def test_compute_digest_raises_without_telemetry(tmp_path: Path) -> None:
    session = tmp_path / "2026-05-20T180000"
    session.mkdir()
    with pytest.raises(FileNotFoundError):
        compute_digest(session, digest_config(tmp_path), now=RICH_SESSION_END)


def test_compute_digest_duration_and_mode_seconds(tmp_path: Path) -> None:
    session = _session(tmp_path)
    digest = compute_digest(session, digest_config(tmp_path), now=RICH_SESSION_END)
    assert digest.session_id == session.name
    assert digest.duration_seconds > 0
    assert sum(digest.mode_seconds.values()) == pytest.approx(digest.duration_seconds)


def test_compute_digest_resamples_session_once(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Classification and fingerprinting share the same reduced frame."""
    import src.analysis.digest as digest_mod

    session = _session(tmp_path)
    calls = 0
    real_resample = digest_mod.resample_and_segment

    def counted(*args: object, **kwargs: object):  # type: ignore[no-untyped-def]
        nonlocal calls
        calls += 1
        return real_resample(*args, **kwargs)  # type: ignore[arg-type]

    monkeypatch.setattr(digest_mod, "resample_and_segment", counted)
    compute_digest(session, digest_config(tmp_path), now=RICH_SESSION_END)

    assert calls == 1


def test_compute_digest_gated_anomaly_counts_by_source_windowed(tmp_path: Path) -> None:
    session = _session(tmp_path)
    config = digest_config(tmp_path)
    logger = AnomalyEventLogger(config.daemon.anomalies_dir)
    # Two events inside the trip window (different sources), one well after
    # the trip ended (must NOT be counted).
    logger.log(
        AnomalyEvent(
            timestamp="2026-05-20T18:01:00+00:00",
            source="fused",
            model_version="v1",
            score=0.9,
        )
    )
    logger.log(
        AnomalyEvent(
            timestamp="2026-05-20T18:01:30+00:00",
            source="behavior",
            model_version="behavior/afr",
            score=1.0,
        )
    )
    logger.log(
        AnomalyEvent(
            timestamp="2026-05-20T19:30:00+00:00",  # outside the window
            source="fused",
            model_version="v1",
            score=0.9,
        )
    )
    logger.close()

    digest = compute_digest(session, config, now=RICH_SESSION_END)
    assert digest.anomaly_counts_by_source == {"fused": 1, "behavior": 1}


def test_compute_digest_separates_evidence_role_events_from_alerts(tmp_path: Path) -> None:
    """A06.5: an evidence-role detector's events are not alerts. The digest
    counts them apart, so "gated anomaly events" means alerts only."""
    session = _session(tmp_path)
    config = digest_config(tmp_path)
    logger = AnomalyEventLogger(config.daemon.anomalies_dir)
    logger.log(
        AnomalyEvent(
            timestamp="2026-05-20T18:01:00+00:00",
            source="fused",
            model_version="v1",
            score=0.9,
        )
    )
    logger.log(
        AnomalyEvent(
            timestamp="2026-05-20T18:01:30+00:00",
            source="lstm_autoencoder",
            model_version="lstm_v1",
            score=5.0,
            role="evidence",
        )
    )
    logger.close()

    digest = compute_digest(session, config, now=RICH_SESSION_END)
    assert digest.anomaly_counts_by_source == {"fused": 1}
    assert digest.evidence_counts_by_source == {"lstm_autoencoder": 1}
    assert digest.schema_version == 2
    payload = digest.to_dict()
    assert payload["evidence_counts_by_source"] == {"lstm_autoencoder": 1}

    _, body = format_digest_message(payload)
    assert "1 gated anomaly event(s)" in body
    assert "by source: fused=1" in body
    assert "1 evidence event(s) (not alerts): lstm_autoencoder=1" in body


def test_capture_prefixed_digest_uses_its_timestamp_for_all_windows(tmp_path: Path) -> None:
    """A capture prefix must not make its anomaly/ledger window start at `now`.

    This is production-shaped rather than a bare parser test: the same digest
    call that serves a completed tuning capture must include early drive
    evidence while excluding later records.
    """
    session = write_rich_session(tmp_path, name="_capture-tuning-did-2026-05-20T180000")
    (session / "capture_profile.json").write_text(
        '{"profile":"tuning-did","started_at":"2026-05-20T18:00:00+00:00",'
        '"profile_snapshot":{"corpus_eligible":false}}\n'
    )
    config = digest_config(tmp_path)
    logger = AnomalyEventLogger(config.daemon.anomalies_dir)
    logger.log(
        AnomalyEvent(
            timestamp="2026-05-20T18:00:02+00:00",
            source="behavior",
            model_version="behavior/rpm",
            score=1.0,
        )
    )
    logger.log(
        AnomalyEvent(
            timestamp="2026-05-20T18:06:00+00:00",
            source="behavior",
            model_version="behavior/rpm",
            score=1.0,
        )
    )
    logger.close()
    ledger = VehicleLedgerStore(config.daemon.vehicle_ledger)
    ledger.append(
        VehicleEvent(
            recorded_at="2026-05-20T18:00:03+00:00",
            occurred_at="2026-05-20",
            kind="observation",
            title="capture began",
        )
    )
    ledger.append(
        VehicleEvent(
            recorded_at="2026-05-20T18:06:00+00:00",
            occurred_at="2026-05-20",
            kind="observation",
            title="later record",
        )
    )

    digest = compute_digest(session, config, now=RICH_SESSION_END)

    assert digest.anomaly_counts_by_source == {"behavior": 1}
    assert [event["title"] for event in digest.new_ledger_events] == ["capture began"]


def test_compute_digest_new_dtcs_from_session_sidecar(tmp_path: Path) -> None:
    session = _session(tmp_path)
    report = build_dtc_report(stored=["P0171"], pending=["P0300"], mil_on=True)
    (session / DTC_SIDECAR_FILENAME).write_text(json.dumps(report.to_dict(), default=str))

    digest = compute_digest(session, digest_config(tmp_path), now=RICH_SESSION_END)
    codes = {entry["code"] for entry in digest.new_dtcs}
    assert codes == {"P0171", "P0300"}


def test_compute_digest_new_dtcs_empty_without_sidecar(tmp_path: Path) -> None:
    session = _session(tmp_path)
    digest = compute_digest(session, digest_config(tmp_path), now=RICH_SESSION_END)
    assert digest.new_dtcs == []


def test_compute_digest_new_ledger_events_windowed(tmp_path: Path) -> None:
    session = _session(tmp_path)
    config = digest_config(tmp_path)
    ledger = VehicleLedgerStore(config.daemon.vehicle_ledger)
    ledger.append(
        VehicleEvent(
            recorded_at="2026-05-20T18:01:00+00:00",
            occurred_at="2026-05-20",
            kind="dtc",
            title="DTC P0171 stored",
        )
    )
    ledger.append(
        VehicleEvent(
            recorded_at="2020-01-01T00:00:00+00:00",  # long before this trip
            occurred_at="2020-01-01",
            kind="maintenance",
            title="oil change",
        )
    )

    digest = compute_digest(session, config, now=RICH_SESSION_END)
    assert len(digest.new_ledger_events) == 1
    assert digest.new_ledger_events[0]["title"] == "DTC P0171 stored"


def test_compute_digest_fingerprint_top_movers_vs_reference(tmp_path: Path) -> None:
    session = _session(tmp_path)
    config = digest_config(tmp_path)
    # A reference epoch where idle_rpm_mean's healthy band is centered well
    # below this session's idle_rpm_mean (~800) -> a clear top mover.
    reference = build_reference_epoch(
        {
            "s1": {"idle_rpm_mean": 650.0},
            "s2": {"idle_rpm_mean": 660.0},
            "s3": {"idle_rpm_mean": 655.0},
        }
    )
    write_reference_epoch(config.daemon.reference_epoch, reference)

    digest = compute_digest(session, config, now=RICH_SESSION_END)
    movers_by_stat = {m["stat"]: m for m in digest.fingerprint_top_movers}
    assert "idle_rpm_mean" in movers_by_stat
    assert movers_by_stat["idle_rpm_mean"]["z_score"] > 0


def test_compute_digest_without_reference_epoch_has_no_top_movers(tmp_path: Path) -> None:
    session = _session(tmp_path)
    digest = compute_digest(session, digest_config(tmp_path), now=RICH_SESSION_END)
    assert digest.fingerprint_top_movers == []


def test_write_digest_is_atomic_tmp_rename(tmp_path: Path) -> None:
    session = _session(tmp_path)
    digest = compute_digest(session, digest_config(tmp_path), now=RICH_SESSION_END)
    path = write_digest(session, digest)
    assert path == session / DIGEST_FILENAME
    assert not (session / f"{DIGEST_FILENAME}.tmp").exists()

    loaded = read_digest(session)
    assert loaded is not None
    assert loaded["session_id"] == session.name
    assert loaded["duration_seconds"] == pytest.approx(digest.duration_seconds)


def test_get_or_compute_digest_computes_once_then_caches(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    session = _session(tmp_path)
    config = digest_config(tmp_path)

    import src.analysis.digest as digest_mod

    calls: list[str] = []
    real_compute = digest_mod.compute_digest

    def _spy(session_dir: Path, cfg: CanaryConfig, **kwargs: object) -> SessionDigest:
        calls.append(session_dir.name)
        return real_compute(session_dir, cfg, **kwargs)  # type: ignore[arg-type]

    monkeypatch.setattr(digest_mod, "compute_digest", _spy)

    first = get_or_compute_digest(session, config, now=RICH_SESSION_END)
    assert first is not None
    assert calls == [session.name]

    second = get_or_compute_digest(session, config, now=RICH_SESSION_END)
    assert second == first
    assert calls == [session.name]  # cached — not recomputed

    third = get_or_compute_digest(session, config, force=True, now=RICH_SESSION_END)
    assert third is not None
    assert calls == [session.name, session.name]


def test_get_or_compute_digest_returns_none_for_missing_session(tmp_path: Path) -> None:
    assert get_or_compute_digest(tmp_path / "nope", digest_config(tmp_path)) is None


def test_get_or_compute_digest_updates_the_catalog_when_wired(tmp_path: Path) -> None:
    from src.storage.catalog import SessionCatalog

    session = _session(tmp_path)
    config = digest_config(tmp_path)
    catalog_db = tmp_path / "catalog.db"

    digest = get_or_compute_digest(session, config, now=RICH_SESSION_END, catalog_db=catalog_db)
    assert digest is not None

    with SessionCatalog(catalog_db) as catalog:
        row = catalog.get_session(session.name)
    assert row is not None
    assert row.digest is not None
    assert row.digest["session_id"] == session.name


def test_get_or_compute_digest_skips_the_catalog_by_default(tmp_path: Path) -> None:
    session = _session(tmp_path)
    config = digest_config(tmp_path)

    assert get_or_compute_digest(session, config, now=RICH_SESSION_END) is not None
    assert not (tmp_path / "catalog.db").exists()


def test_read_digest_returns_none_when_absent(tmp_path: Path) -> None:
    session = tmp_path / "2026-05-20T180000"
    session.mkdir()
    assert read_digest(session) is None


# ---------------------------------------------------------------------------
# Pure ranking helpers
# ---------------------------------------------------------------------------


def test_top_behavior_deviations_ranks_by_abs_value() -> None:
    stats = {
        "behavior_deviation_abs_mean:afr_bank_balance": 0.4,
        "behavior_deviation_abs_mean:rpm_constant_750": -2.0,
        "idle_rpm_mean": 800.0,  # not a behavior-deviation stat -> excluded
    }
    top = _top_behavior_deviations(stats)
    assert [entry["channel"] for entry in top] == ["rpm_constant_750", "afr_bank_balance"]


def test_fingerprint_top_movers_ranks_by_abs_z_score() -> None:
    from src.analysis.reference_epoch import StatSummary

    stats = {"idle_rpm_mean": 900.0, "voltage_at_idle_v": 14.2}
    reference_stats = {
        "idle_rpm_mean": StatSummary(mean=800.0, std=10.0, quantiles={}, n=5),
        "voltage_at_idle_v": StatSummary(mean=14.2, std=0.0, quantiles={}, n=5),
    }
    movers = _fingerprint_top_movers(stats, reference_stats)
    assert movers[0]["stat"] == "idle_rpm_mean"
    assert movers[0]["z_score"] == pytest.approx(10.0)
    # A degenerate reference std (0.0) reports z_score=None rather than
    # dividing by zero, and still shows up (just unranked/last).
    voltage_mover = next(m for m in movers if m["stat"] == "voltage_at_idle_v")
    assert voltage_mover["z_score"] is None
