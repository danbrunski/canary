"""Tests for the session catalog (Task 6.1/6.3, eval-longitudinal-and-dtc.md)."""

from __future__ import annotations

from pathlib import Path

from src.analysis.session_classifier import classify_session, write_classification
from src.config import CanaryConfig
from src.storage.catalog import SessionCatalog, rebuild_catalog
from src.storage.parquet_writer import write_records_to_parquet
from src.telemetry import TelemetryRecord
from tests.configs import example_config

REPO_ROOT = Path(__file__).resolve().parents[1]
EXAMPLE_YAML = REPO_ROOT / "canary.example.yaml"


def _config(tmp_path: Path) -> CanaryConfig:
    config = example_config(EXAMPLE_YAML)
    return config.model_copy(
        update={"collection": config.collection.model_copy(update={"storage_path": tmp_path})}
    )


def _write_session(root: Path, *, name: str = "2026-05-20T120000", n: int = 60) -> Path:
    session = root / name
    records: list[TelemetryRecord] = []
    for i in range(n):
        t = 1_779_300_000.0 + i
        records.extend(
            [
                TelemetryRecord.obd(t, "RPM", 800.0, "rpm"),
                TelemetryRecord.obd(t, "SPEED", 0.0, "km/h"),
                TelemetryRecord.obd(t, "COOLANT_TEMP", 85.0, "C"),
            ]
        )
    write_records_to_parquet(records, session, max_rows=10_000)
    return session


# --------------------------------------------------------------------------- #
# 6.1 — schema + store
# --------------------------------------------------------------------------- #


def test_record_session_round_trips_classification_into_the_row(tmp_path: Path) -> None:
    config = _config(tmp_path)
    session = _write_session(tmp_path)
    classification = classify_session(session, config)
    write_classification(session, classification)

    with SessionCatalog(tmp_path / "catalog.db") as catalog:
        row = catalog.record_session(session)

    assert row.session_id == session.name
    assert row.path == str(session)
    assert row.disposition == classification.disposition
    assert row.classifier_version == classification.classifier_version
    assert row.mode_seconds == dict(classification.mode_seconds)
    assert row.event_counts == dict(classification.event_counts)
    assert row.started_at is not None and row.started_at.startswith("2026-05-20T12:00:00")
    assert row.row_count is not None and row.row_count > 0
    assert row.size_bytes is not None and row.size_bytes > 0
    assert row.pinned is False


def test_record_session_is_idempotent_upsert(tmp_path: Path) -> None:
    config = _config(tmp_path)
    session = _write_session(tmp_path)
    classification = classify_session(session, config)
    write_classification(session, classification)

    with SessionCatalog(tmp_path / "catalog.db") as catalog:
        catalog.record_session(session)
        catalog.record_session(session)
        assert catalog.count() == 1


def test_pin_file_reflected_in_pinned_column(tmp_path: Path) -> None:
    session = _write_session(tmp_path)
    (session / ".canary-pin").write_text("{}\n")

    with SessionCatalog(tmp_path / "catalog.db") as catalog:
        row = catalog.record_session(session)

    assert row.pinned is True


def test_get_session_and_list_sessions(tmp_path: Path) -> None:
    session_a = _write_session(tmp_path, name="2026-05-20T120000")
    session_b = _write_session(tmp_path, name="2026-05-21T120000")

    with SessionCatalog(tmp_path / "catalog.db") as catalog:
        catalog.record_session(session_a)
        catalog.record_session(session_b)

        assert catalog.get_session("nonexistent") is None
        assert catalog.get_session(session_a.name) is not None

        sessions = catalog.list_sessions()
        assert [s.session_id for s in sessions] == [session_a.name, session_b.name]


def test_delete_session_drops_the_row(tmp_path: Path) -> None:
    session = _write_session(tmp_path)
    with SessionCatalog(tmp_path / "catalog.db") as catalog:
        catalog.record_session(session)
        assert catalog.count() == 1
        catalog.delete_session(session.name)
        assert catalog.count() == 0
        # A no-op the second time — never raises for an unknown id.
        catalog.delete_session(session.name)


# --------------------------------------------------------------------------- #
# 6.1 — corruption recovery: rebuild from sidecars, never data loss
# --------------------------------------------------------------------------- #


def test_rebuild_after_corruption_matches_sidecars_on_disk(tmp_path: Path) -> None:
    """Sidecars are the source of truth: delete/corrupt the DB, rebuild, and
    the row content must match what's independently readable from disk."""
    config = _config(tmp_path)
    session = _write_session(tmp_path)
    classification = classify_session(session, config)
    write_classification(session, classification)

    db_path = tmp_path / "catalog.db"
    with SessionCatalog(db_path) as catalog:
        catalog.record_session(session)
        original = catalog.get_session(session.name)
    assert original is not None

    # Corrupt the DB file outright (simulates disk corruption / a bad write).
    db_path.write_bytes(b"not a sqlite file at all")

    # Corruption recovery: delete it and rebuild from the sidecars on disk.
    db_path.unlink()
    with SessionCatalog(db_path) as catalog:
        count = rebuild_catalog(catalog, [session])
        assert count == 1
        rebuilt = catalog.get_session(session.name)

    assert rebuilt is not None
    assert rebuilt.disposition == original.disposition
    assert rebuilt.classifier_version == original.classifier_version
    assert rebuilt.mode_seconds == original.mode_seconds
    assert rebuilt.row_count == original.row_count


def test_rebuild_catalog_skips_a_bad_session_without_aborting(tmp_path: Path) -> None:
    good = _write_session(tmp_path, name="2026-05-20T120000")
    bad = tmp_path / "2026-05-21T120000"  # no telemetry parquet at all
    bad.mkdir()

    with SessionCatalog(tmp_path / "catalog.db") as catalog:
        count = rebuild_catalog(catalog, [good, bad])

    # `bad` has no parquet, so `_parquet_row_count`/size are harmless (None/0);
    # record_session never raises for it, so both still get indexed.
    assert count == 2


# --------------------------------------------------------------------------- #
# 6.3 — incremental update hooks
# --------------------------------------------------------------------------- #


def test_classify_one_session_updates_the_catalog_when_wired(tmp_path: Path) -> None:
    from src.control.classification import classify_one_session

    config = _config(tmp_path)
    session = _write_session(tmp_path)
    catalog_db = tmp_path / "catalog.db"

    classify_one_session(config, session.name, catalog_db=catalog_db)

    with SessionCatalog(catalog_db) as catalog:
        row = catalog.get_session(session.name)
    assert row is not None
    assert row.fingerprint  # the fingerprint hook ran and got indexed too


def test_classify_one_session_skips_the_catalog_by_default(tmp_path: Path) -> None:
    """No ``catalog_db`` given (the default) touches no catalog file at all —
    the low-level function is opt-in, never a hidden global-config coupling."""
    from src.control.classification import classify_one_session

    config = _config(tmp_path)
    session = _write_session(tmp_path)

    classify_one_session(config, session.name)

    assert not (tmp_path / "catalog.db").exists()


def test_pin_and_unpin_update_the_catalog_when_wired(tmp_path: Path) -> None:
    from src.control.sessions import pin_session, unpin_session

    session = _write_session(tmp_path)
    catalog_db = tmp_path / "catalog.db"

    pin_session(session, catalog_db=catalog_db)
    with SessionCatalog(catalog_db) as catalog:
        assert catalog.get_session(session.name).pinned is True  # type: ignore[union-attr]

    unpin_session(session, catalog_db=catalog_db)
    with SessionCatalog(catalog_db) as catalog:
        assert catalog.get_session(session.name).pinned is False  # type: ignore[union-attr]


def test_dtc_state_updates_the_catalog_when_wired(tmp_path: Path) -> None:
    from src.control.dtc_state import DtcState
    from src.telemetry import TelemetryRecord

    catalog_db = tmp_path / "catalog.db"
    session = tmp_path / "2026-05-20T120000"
    session.mkdir()
    state = DtcState(catalog_db=catalog_db)
    state.observe(
        TelemetryRecord.obd(0.0, "DTC_STORED", "P0300", ""),
        session_id=session.name,
        session_dir=session,
    )

    with SessionCatalog(catalog_db) as catalog:
        row = catalog.get_session(session.name)
    assert row is not None
    assert row.dtc is not None
    assert [c["code"] for c in row.dtc["stored"]] == ["P0300"]


def test_retention_deletion_removes_the_catalog_row(tmp_path: Path) -> None:
    from datetime import UTC, datetime

    from src.storage.retention import delete_expired_sessions

    session = tmp_path / "2000-01-01T000000"
    session.mkdir()
    (session / "telemetry-00000.parquet").write_bytes(b"junk")
    catalog_db = tmp_path / "catalog.db"
    with SessionCatalog(catalog_db) as catalog:
        catalog.record_session(session)
        assert catalog.count() == 1

    deleted = delete_expired_sessions(
        tmp_path,
        retention_days=1,
        now=datetime(2026, 1, 1, tzinfo=UTC),
        catalog_db=catalog_db,
    )
    assert deleted == [session]

    with SessionCatalog(catalog_db) as catalog:
        assert catalog.count() == 0


def test_retention_without_catalog_db_never_touches_a_catalog_file(tmp_path: Path) -> None:
    from datetime import UTC, datetime

    from src.storage.retention import delete_expired_sessions

    session = tmp_path / "2000-01-01T000000"
    session.mkdir()
    (session / "telemetry-00000.parquet").write_bytes(b"junk")

    deleted = delete_expired_sessions(
        tmp_path, retention_days=1, now=datetime(2026, 1, 1, tzinfo=UTC)
    )
    assert deleted == [session]
    assert not (tmp_path / "catalog.db").exists()


def test_a_capture_row_has_started_at(tmp_path: Path) -> None:
    """R01.1: the catalog's own regex knew ``_replay-``/``_tuning-`` but not
    ``_capture-``, so a digested or pinned capture's row had ``started_at =
    NULL`` and fell out of every date-bounded catalog read."""
    session = _write_session(tmp_path, name="_capture-fuel-map-2026-09-20T101500")

    with SessionCatalog(tmp_path / "catalog.db") as catalog:
        row = catalog.record_session(session)

    assert row.started_at == "2026-09-20T10:15:00+00:00"


def test_a_non_object_sidecar_reads_as_absent(tmp_path: Path) -> None:
    """R01.5: valid JSON that is not an object used to raise out of
    ``dict(json.loads(...))`` and abort the whole row."""
    session = _write_session(tmp_path)
    (session / "dtc.json").write_text("[1, 2]\n")
    (session / "digest.json").write_text('"not an object"\n')

    with SessionCatalog(tmp_path / "catalog.db") as catalog:
        row = catalog.record_session(session)

    assert row.dtc is None
    assert row.digest is None
