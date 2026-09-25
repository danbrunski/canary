"""Tests for the local CANary library format (Task 6.32)."""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from pathlib import Path

from src.companion.library import SCHEMA_VERSION, CanaryLibrary
from src.storage.parquet_writer import write_records_to_parquet
from src.telemetry import TelemetryRecord
from tests.configs import isolated_config


def _make_session(parent: Path, session_id: str, *, seconds: int = 70) -> Path:
    sdir = parent / session_id
    sdir.mkdir(parents=True)
    base = datetime(2026, 5, 11, 22, 24, 47, tzinfo=UTC)
    recs: list[TelemetryRecord] = []
    for i in range(seconds):
        ts = base + timedelta(seconds=i)
        recs.append(TelemetryRecord(timestamp=ts, source="obd", channel="RPM", value=800 + i))
        recs.append(TelemetryRecord(timestamp=ts, source="obd", channel="SPEED", value=float(i)))
    write_records_to_parquet(recs, sdir)
    (sdir / "manifest.json").write_text(json.dumps({"started_at": base.isoformat()}))
    return sdir


def test_library_layout_and_schema(tmp_path: Path) -> None:
    with CanaryLibrary(tmp_path / "lib") as lib:
        assert lib.sessions_dir.is_dir()
        assert lib.models_dir.is_dir()
        assert lib.datasets_dir.is_dir()
        assert lib.db_path.exists()
        row = lib._conn.execute("SELECT value FROM meta WHERE key='schema_version'").fetchone()
        assert row["value"] == str(SCHEMA_VERSION)


def test_import_session_cheap_path_and_idempotent(tmp_path: Path) -> None:
    src = _make_session(tmp_path / "remote", "2026-05-11T222447")
    lib = CanaryLibrary(tmp_path / "lib")
    s = lib.import_session(src, source_host="pi")
    assert s.session_id == "2026-05-11T222447"
    assert s.source_host == "pi"
    assert s.size_bytes and s.size_bytes > 0
    assert (lib.sessions_dir / "2026-05-11T222447" / "manifest.json").exists()
    # Re-import refreshes, doesn't duplicate.
    lib.import_session(src, source_host="pi2")
    assert len(lib.list_sessions()) == 1
    assert lib.get_session("2026-05-11T222447").source_host == "pi2"  # type: ignore[union-attr]
    lib.close()


def test_import_session_with_config_indexes_channels(tmp_path: Path) -> None:
    storage = tmp_path / "sessions"
    storage.mkdir()
    src = _make_session(storage, "2026-05-11T222447", seconds=70)
    config = isolated_config(tmp_path, storage=storage)
    with CanaryLibrary(tmp_path / "lib") as lib:
        s = lib.import_session(src, config=config)
        assert s.row_count == 70
        assert s.duration_s == 69.0
        channels = {
            r["channel"]
            for r in lib._conn.execute(
                "SELECT channel FROM session_channels WHERE session_id=?", (s.session_id,)
            )
        }
        assert {"obd:RPM", "obd:SPEED"} <= channels
        assert s.quality_warnings == []  # 70s, both key channels


def test_training_runs_upsert(tmp_path: Path) -> None:
    with CanaryLibrary(tmp_path / "lib") as lib:
        rec = {
            "run_id": "lstm_v1",
            "model_type": "lstm_autoencoder",
            "model_version": "lstm_v1",
            "status": "succeeded",
            "created_at": "2026-05-16T10:00:00Z",
            "publish_status": "unpublished",
        }
        lib.record_training_run(rec, compute_target="laptop")
        lib.record_training_run({**rec, "publish_status": "active"})
        runs = lib.list_training_runs()
        assert len(runs) == 1
        assert runs[0]["publish_status"] == "active"


def test_model_artifacts_and_publish_history(tmp_path: Path) -> None:
    artifact = tmp_path / "model.pt"
    artifact.write_bytes(b"weights")
    with CanaryLibrary(tmp_path / "lib") as lib:
        stored = lib.add_model_artifact(
            "lstm_v1",
            model_type="lstm_autoencoder",
            artifact_path=artifact,
            manifest={"model_version": "lstm_v1"},
            version="lstm_v1",
        )
        assert stored.exists()
        assert stored.parent == lib.models_dir / "lstm_v1"
        assert (lib.models_dir / "lstm_v1" / "manifest.json").exists()
        assert lib.list_model_artifacts()[0]["model_id"] == "lstm_v1"
        lib.record_publish("lstm_v1", target="pi@192.168.1.50", status="uploaded")
        lib.record_publish("lstm_v1", target="pi@192.168.1.50", status="active")
        hist = lib.publish_history("lstm_v1")
        assert [h["status"] for h in hist] == ["uploaded", "active"]


def test_datasets_and_queued_actions(tmp_path: Path) -> None:
    with CanaryLibrary(tmp_path / "lib") as lib:
        lib.add_dataset(
            "ds1",
            path=tmp_path / "ds1",
            manifest={"purpose": "baseline", "row_count": 500},
        )
        ds = lib.list_datasets()
        assert ds[0]["name"] == "ds1" and ds[0]["row_count"] == 500

        aid = lib.queue_action("retrain_lstm", {"days": 14})
        assert lib.list_actions(status="queued")[0]["id"] == aid
        lib.update_action(aid, "sent", detail="dispatched to pi")
        assert lib.list_actions(status="queued") == []
        sent = lib.list_actions(status="sent")
        assert sent[0]["detail"] == "dispatched to pi"
        assert json.loads(sent[0]["params"]) == {"days": 14}
