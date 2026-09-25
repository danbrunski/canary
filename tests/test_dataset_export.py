"""Tests for the dataset capture + export pipeline (Task 6.20)."""

from __future__ import annotations

import json
import zipfile
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pandas as pd
import pytest

import scripts.export_dataset as cli
from src.config import CanaryConfig
from src.datasets.export import (
    DatasetExportSpec,
    build_dataset,
    config_hash,
    export_dataset,
    mark_training_candidate,
)
from src.storage.parquet_writer import write_records_to_parquet
from src.telemetry import TelemetryRecord
from tests.configs import write_isolated_config


def _make_session(
    storage: Path, session_id: str, *, seconds: int = 20, with_tags: bool = False
) -> Path:
    sdir = storage / session_id
    sdir.mkdir(parents=True)
    base = datetime(2026, 5, 11, 22, 24, 47, tzinfo=UTC)
    records: list[TelemetryRecord] = []
    for i in range(seconds):
        ts = base + timedelta(seconds=i)
        records.append(
            TelemetryRecord(timestamp=ts, source="obd", channel="RPM", value=800 + i, unit="rpm")
        )
        records.append(
            TelemetryRecord(timestamp=ts, source="obd", channel="SPEED", value=float(i), unit="kph")
        )
    write_records_to_parquet(records, sdir)
    if with_tags:
        t0 = base.timestamp()
        (sdir / "tags.jsonl").write_text(
            json.dumps({"t": t0 + 2, "label": "idle"})
            + "\n"
            + json.dumps({"t": t0 + 10, "label": "wot"})
            + "\n"
        )
    return sdir


def _config(tmp_path: Path) -> tuple[CanaryConfig, Path]:
    storage = tmp_path / "sessions"
    storage.mkdir()
    # The CLI tests read tmp_path/canary.yaml, so the config is written too.
    return CanaryConfig.from_yaml(write_isolated_config(tmp_path, storage=storage)), storage


# ---------------------------------------------------------------------------
# build_dataset
# ---------------------------------------------------------------------------


def test_build_dataset_basic_and_manifest(tmp_path: Path) -> None:
    config, storage = _config(tmp_path)
    _make_session(storage, "2026-05-11T222447")
    spec = DatasetExportSpec(sessions=["2026-05-11T222447"], purpose="baseline")
    frame, manifest = build_dataset(spec, storage, config)
    assert not frame.empty
    assert "obd:RPM" in frame.columns and "obd:SPEED" in frame.columns
    assert manifest.row_count == len(frame)
    assert manifest.purpose == "baseline"
    assert manifest.vehicle["model"] == "Xterra"
    assert manifest.config_hash == config_hash(config)
    assert manifest.time_range["start"] is not None


def test_channel_selection_and_missing_warning(tmp_path: Path) -> None:
    config, storage = _config(tmp_path)
    _make_session(storage, "2026-05-11T222447")
    spec = DatasetExportSpec(sessions=["2026-05-11T222447"], channels=["obd:RPM", "obd:GHOST"])
    frame, manifest = build_dataset(spec, storage, config)
    assert "obd:SPEED" not in frame.columns
    assert "obd:RPM" in frame.columns
    assert any("obd:GHOST" in w for w in manifest.quality_warnings)


def test_resample_changes_grid(tmp_path: Path) -> None:
    config, storage = _config(tmp_path)
    _make_session(storage, "2026-05-11T222447", seconds=20)
    raw, _ = build_dataset(DatasetExportSpec(sessions=["2026-05-11T222447"]), storage, config)
    rs, _ = build_dataset(
        DatasetExportSpec(sessions=["2026-05-11T222447"], resample_hz=2.0), storage, config
    )
    assert len(rs) >= len(raw)  # 2 Hz over the same span


def test_tag_include_exclude(tmp_path: Path) -> None:
    config, storage = _config(tmp_path)
    _make_session(storage, "2026-05-11T222447", with_tags=True)
    inc, m_inc = build_dataset(
        DatasetExportSpec(sessions=["2026-05-11T222447"], include_tags=["idle"]), storage, config
    )
    full, _ = build_dataset(DatasetExportSpec(sessions=["2026-05-11T222447"]), storage, config)
    assert 0 < len(inc) < len(full)
    assert "idle" in m_inc.tags
    # include a label that doesn't exist → warned + empty
    _, m_none = build_dataset(
        DatasetExportSpec(sessions=["2026-05-11T222447"], include_tags=["nope"]), storage, config
    )
    assert any("no rows matched include_tags" in w for w in m_none.quality_warnings)


def test_quality_warning_parse_errors_and_missing_session(tmp_path: Path) -> None:
    config, storage = _config(tmp_path)
    sdir = _make_session(storage, "2026-05-11T222447")
    (sdir / "manifest.json").write_text(json.dumps({"frames_recorded": 10, "parse_errors": 9999}))
    spec = DatasetExportSpec(sessions=["2026-05-11T222447", "2099-01-01T000000"])
    _, manifest = build_dataset(spec, storage, config)
    assert any("parse_errors" in w for w in manifest.quality_warnings)
    assert any("session not found" in w for w in manifest.quality_warnings)


def test_empty_export_is_flagged(tmp_path: Path) -> None:
    config, storage = _config(tmp_path)
    spec = DatasetExportSpec(sessions=["2099-01-01T000000"])
    frame, manifest = build_dataset(spec, storage, config)
    assert frame.empty
    assert any("empty" in w for w in manifest.quality_warnings)


def test_spec_validate() -> None:
    with pytest.raises(ValueError, match="at least one session"):
        DatasetExportSpec(sessions=[]).validate()
    with pytest.raises(ValueError, match="unknown formats"):
        DatasetExportSpec(sessions=["s"], formats=["xml"]).validate()


# ---------------------------------------------------------------------------
# export_dataset (files + zip)
# ---------------------------------------------------------------------------


def test_export_writes_all_formats_and_zip(tmp_path: Path) -> None:
    config, storage = _config(tmp_path)
    _make_session(storage, "2026-05-11T222447")
    spec = DatasetExportSpec(
        sessions=["2026-05-11T222447"],
        formats=["csv", "jsonl", "parquet"],
        purpose="multi",
    )
    result = export_dataset(spec, storage, config, tmp_path / "out", name="ds1")
    names = {f.name for f in result.files}
    assert {"dataset.csv", "dataset.jsonl", "dataset.parquet", "dataset_manifest.json"} <= names
    assert result.zip_path is not None and result.zip_path.exists()
    with zipfile.ZipFile(result.zip_path) as zf:
        assert any(n.endswith("dataset_manifest.json") for n in zf.namelist())
    manifest = json.loads((result.out_dir / "dataset_manifest.json").read_text())
    assert manifest["purpose"] == "multi"
    # CSV round-trips to the same row count.
    assert len(pd.read_csv(result.out_dir / "dataset.csv")) == result.manifest.row_count


def test_export_feature_matrix(tmp_path: Path) -> None:
    config, storage = _config(tmp_path)
    _make_session(storage, "2026-05-11T222447", seconds=30)
    spec = DatasetExportSpec(sessions=["2026-05-11T222447"], formats=["feature_matrix"], zip=False)
    result = export_dataset(spec, storage, config, tmp_path / "out", name="fm")
    fm = result.out_dir / "feature_matrix.parquet"
    assert fm.exists()
    pd.read_parquet(fm)  # readable


# ---------------------------------------------------------------------------
# mark_training_candidate + CLI
# ---------------------------------------------------------------------------


def test_mark_training_candidate(tmp_path: Path) -> None:
    config, storage = _config(tmp_path)
    sdir = _make_session(storage, "2026-05-11T222447")
    meta = mark_training_candidate(sdir, purpose="clean highway")
    assert meta["training_candidate"] is True
    assert meta["training_purpose"] == "clean highway"


def test_cli_mark_and_export(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    config, storage = _config(tmp_path)
    _make_session(storage, "2026-05-11T222447")
    cfg_path = tmp_path / "canary.yaml"  # written by _config

    rc = cli.main(
        ["--config", str(cfg_path), "--mark-candidate", "2026-05-11T222447", "--purpose", "x"]
    )
    assert rc == 0
    assert "training_candidate=True" in capsys.readouterr().out

    rc = cli.main(
        [
            "--config",
            str(cfg_path),
            "--session",
            "2026-05-11T222447",
            "--format",
            "parquet",
            "--no-zip",
            "--out",
            str(tmp_path / "out"),
            "--name",
            "cli_ds",
        ]
    )
    assert rc == 0
    out = capsys.readouterr().out
    assert "DATASET EXPORT" in out
    assert (tmp_path / "out" / "cli_ds" / "dataset.parquet").exists()
