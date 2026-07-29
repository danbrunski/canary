"""Tests for tools/import_uprev_log.py — the UpRev CSV importer."""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pandas as pd
import pytest

from src.mcp_server.parquet_query import TelemetryQuery
from tools import import_uprev_log as importer


def _config_yaml(storage_path: Path) -> str:
    return f"""
vehicle:
  make: Nissan
  model: Xterra
  year: 2008
  vin: ""
hardware:
  elm327_port: /dev/ttyUSB0
  can:
    interface: virtual
    channel: vcan0
    bitrate: 500000
collection:
  obd_poll_rate_hz: 1
  storage_path: {storage_path}
  session_retention_days: 30
  storage_warning_mb: 500
preprocessing:
  resample_hz: 1
  rolling_window_seconds: 30
  operating_modes:
    idle:
      rpm_max: 900
      speed_max: 0
models:
  isolation_forest:
    enabled: false
    n_estimators: 100
    contamination: 0.01
  lstm_autoencoder:
    enabled: false
    sequence_length: 60
    latent_dim: 16
    threshold_k: 3.0
dashboard:
  host: 127.0.0.1
  port: 8000
"""


def _write_config(tmp_path: Path) -> tuple[Path, Path]:
    storage = tmp_path / "storage"
    cfg_path = tmp_path / "canary.yaml"
    cfg_path.write_text(_config_yaml(storage))
    return cfg_path, storage


# ---------------------------------------------------------------------------
# parse_column — the unit-normalization logic the whole tool exists for
# ---------------------------------------------------------------------------


def test_parse_column_known_headers_use_canonical_channel_and_unit() -> None:
    assert importer.parse_column("IGN TIMING (BTDC)") == ("TIMING_ADVANCE", "degBTDC", False)
    assert importer.parse_column("ENGINE RPM (rpm)") == ("RPM", "rpm", False)
    assert importer.parse_column("CAL/LD VALUE (%)") == ("ENGINE_LOAD", "%", False)


def test_parse_column_known_temp_column_is_flagged_fahrenheit() -> None:
    """This is the exact bug report: INTAKE AIR TMP has no unit suffix and is
    silently Fahrenheit — the importer must know to convert it, not just
    relabel it "degC" without doing the math."""
    channel, unit, is_fahrenheit = importer.parse_column("INTAKE AIR TMP")
    assert channel == "INTAKE_TEMP"
    assert unit == "degC"
    assert is_fahrenheit is True


def test_parse_column_unknown_temp_column_is_generically_detected() -> None:
    """COOLANT TEMP isn't in KNOWN_COLUMNS, but the TMP/TEMP-with-no-unit-
    suffix heuristic must still catch it — the whole point is not needing to
    enumerate every UpRev column name in advance."""
    channel, unit, is_fahrenheit = importer.parse_column("COOLANT TEMP")
    assert channel == "COOLANT_TEMP"
    assert unit == "degC"
    assert is_fahrenheit is True


def test_parse_column_respects_an_explicit_unit_suffix_even_for_temp() -> None:
    """A column that already states its unit is trusted, not overridden by
    the Fahrenheit heuristic."""
    channel, unit, is_fahrenheit = importer.parse_column("EXHAUST TEMP (degC)")
    assert channel == "EXHAUST_TEMP"
    assert unit == "degC"
    assert is_fahrenheit is False


def test_parse_column_unknown_non_temp_passes_through() -> None:
    channel, unit, is_fahrenheit = importer.parse_column("A/F ADJ-B1 (%)")
    assert channel == "A_F_ADJ_B1"
    assert unit == "%"
    assert is_fahrenheit is False


# ---------------------------------------------------------------------------
# build_records
# ---------------------------------------------------------------------------


def test_build_records_converts_and_anchors_timestamps() -> None:
    frame = pd.DataFrame(
        {
            "Time": [0.0, 500.0],  # ms since log start, as Osiris exports it
            "ENGINE RPM (rpm)": [800.0, 850.0],
            "INTAKE AIR TMP": [140.0, 141.0],
        }
    )
    started_at = datetime(2026, 7, 27, 0, 41, 32, tzinfo=UTC)
    records = importer.build_records(frame, started_at=started_at)

    by_channel = {(r.channel, r.timestamp): r for r in records}
    rpm0 = by_channel[("RPM", started_at)]
    assert rpm0.value == 800.0
    assert rpm0.unit == "rpm"
    intake0 = by_channel[("INTAKE_TEMP", started_at)]
    assert intake0.value == pytest.approx(60.0)  # 140F -> 60C
    assert intake0.unit == "degC"
    # 500 ms after row 0 — not 500 s, which is what reading the column as
    # seconds would produce (and which the gap-rotator would split apart).
    assert ("RPM", started_at + timedelta(milliseconds=500)) in by_channel


def test_build_records_reads_time_as_milliseconds_not_seconds() -> None:
    """A real 62 ms/row Osiris log must import as a ~5 min session, not 3.6 days."""
    rows = 1000
    frame = pd.DataFrame(
        {
            "Time": [float(i * 62) for i in range(rows)],
            "ENGINE RPM (rpm)": [800.0] * rows,
        }
    )
    started_at = datetime(2026, 7, 29, 11, 20, 29, tzinfo=UTC)
    records = importer.build_records(frame, started_at=started_at)

    span = max(r.timestamp for r in records) - min(r.timestamp for r in records)
    assert span == timedelta(milliseconds=62 * (rows - 1))
    assert span < timedelta(minutes=2)


def test_build_records_skips_nan_cells_but_keeps_the_row() -> None:
    frame = pd.DataFrame(
        {
            "Time": [0.0, 1000.0],
            "ENGINE RPM (rpm)": [800.0, 900.0],
            "CAL/LD VALUE (%)": [25.0, None],
        }
    )
    started_at = datetime(2026, 7, 27, tzinfo=UTC)
    records = importer.build_records(frame, started_at=started_at)

    row_1_at = started_at + timedelta(seconds=1)
    channels_at_row_1 = {r.channel for r in records if r.timestamp == row_1_at}
    assert channels_at_row_1 == {"RPM"}  # ENGINE_LOAD was NaN — absent, not zero


def test_build_records_drops_rows_with_no_time() -> None:
    frame = pd.DataFrame({"Time": [0.0, None], "ENGINE RPM (rpm)": [800.0, 900.0]})
    records = importer.build_records(frame, started_at=datetime(2026, 7, 27, tzinfo=UTC))
    assert len(records) == 1


def test_build_records_requires_a_time_column() -> None:
    frame = pd.DataFrame({"ENGINE RPM (rpm)": [800.0]})
    with pytest.raises(SystemExit, match="Time"):
        importer.build_records(frame, started_at=datetime(2026, 7, 27, tzinfo=UTC))


# ---------------------------------------------------------------------------
# filename_time_hint
# ---------------------------------------------------------------------------


def test_filename_time_hint_parses_uprev_naming_convention() -> None:
    hint = importer.filename_time_hint("UpRev_Log7-26-2026_5-41-32_PM.csv")
    assert hint is not None
    assert "2026-07-26" in hint
    assert "17:41:32" in hint


def test_filename_time_hint_none_for_an_unrelated_name() -> None:
    assert importer.filename_time_hint("my_log.csv") is None


# ---------------------------------------------------------------------------
# main() — end to end
# ---------------------------------------------------------------------------


def test_main_requires_timezone_aware_started_at(tmp_path: Path) -> None:
    cfg_path, _storage = _write_config(tmp_path)
    csv = tmp_path / "log.csv"
    csv.write_text("Time,ENGINE RPM (rpm)\n0,800\n")
    with pytest.raises(SystemExit, match="timezone-aware"):
        importer.main(
            ["--csv", str(csv), "--started-at", "2026-07-26T17:41:32", "--config", str(cfg_path)]
        )


def test_main_rejects_a_missing_csv(tmp_path: Path) -> None:
    cfg_path, _storage = _write_config(tmp_path)
    rc = importer.main(
        [
            "--csv",
            str(tmp_path / "does-not-exist.csv"),
            "--started-at",
            "2026-07-26T17:41:32-07:00",
            "--config",
            str(cfg_path),
        ]
    )
    assert rc == 1


def test_main_rejects_a_started_at_collision_cleanly(tmp_path: Path) -> None:
    """Re-running with the same --started-at (to the second) must not crash
    with a raw FileExistsError traceback — surfaced against real UpRev logs,
    where re-importing after fixing a typo'd --label is a plausible retry."""
    cfg_path, _storage = _write_config(tmp_path)
    csv = tmp_path / "log.csv"
    csv.write_text("Time,ENGINE RPM (rpm)\n0,800\n")
    argv = [
        "--csv",
        str(csv),
        "--started-at",
        "2026-07-26T17:41:32-07:00",
        "--config",
        str(cfg_path),
    ]

    assert importer.main(argv) == 0
    assert importer.main(argv) == 1


def test_main_writes_a_quarantined_session_with_manifest(tmp_path: Path) -> None:
    cfg_path, storage = _write_config(tmp_path)
    csv = tmp_path / "UpRev_Log7-26-2026_5-41-32_PM.csv"
    csv.write_text(
        "Time,IGN TIMING (BTDC),ENGINE RPM (rpm),CAL/LD VALUE (%),INTAKE AIR TMP,COOLANT TEMP\n"
        "0.0,17.0,800,25.0,140.0,190.0\n"
        "0.5,17.5,850,26.0,141.0,190.0\n"
    )

    rc = importer.main(
        [
            "--csv",
            str(csv),
            "--started-at",
            "2026-07-26T17:41:32-07:00",
            "--label",
            "post-flash-validation",
            "--config",
            str(cfg_path),
        ]
    )
    assert rc == 0

    uprev_root = storage / "_uprev"
    session_dirs = list(uprev_root.iterdir())
    assert len(session_dirs) == 1
    session_dir = session_dirs[0]

    manifest = json.loads((session_dir / "manifest.json").read_text())
    assert manifest["source"] == "uprev"
    assert manifest["label"] == "post-flash-validation"
    assert manifest["original_filename"] == csv.name
    assert manifest["row_count"] == 10  # 5 channels x 2 rows
    assert manifest["channels"] == [
        "COOLANT_TEMP",
        "ENGINE_LOAD",
        "INTAKE_TEMP",
        "RPM",
        "TIMING_ADVANCE",
    ]

    parquet_files = list(session_dir.glob("telemetry-*.parquet"))
    assert len(parquet_files) == 1
    df = pd.read_parquet(parquet_files[0])
    assert set(df["source"]) == {"uprev"}
    intake = df[df["channel"] == "INTAKE_TEMP"]
    assert intake["value_number"].tolist() == pytest.approx([60.0, 60.555556])
    assert set(intake["unit"]) == {"degC"}


def test_main_default_label_is_the_csv_stem(tmp_path: Path) -> None:
    cfg_path, storage = _write_config(tmp_path)
    csv = tmp_path / "some_drive.csv"
    csv.write_text("Time,ENGINE RPM (rpm)\n0,800\n")

    rc = importer.main(
        ["--csv", str(csv), "--started-at", "2026-07-26T17:41:32-07:00", "--config", str(cfg_path)]
    )
    assert rc == 0
    session_dir = next((storage / "_uprev").iterdir())
    manifest = json.loads((session_dir / "manifest.json").read_text())
    assert manifest["label"] == "some_drive"


# ---------------------------------------------------------------------------
# Integration: the actual point — reachable via query_telemetry afterward
# ---------------------------------------------------------------------------


def test_imported_session_is_queryable_via_telemetry_query(tmp_path: Path) -> None:
    cfg_path, storage = _write_config(tmp_path)
    csv = tmp_path / "log.csv"
    csv.write_text("Time,ENGINE RPM (rpm),COOLANT TEMP\n0.0,800,190.0\n0.5,850,190.0\n")
    rc = importer.main(
        ["--csv", str(csv), "--started-at", "2026-07-26T17:41:32-07:00", "--config", str(cfg_path)]
    )
    assert rc == 0
    session_id = next((storage / "_uprev").iterdir()).name

    query = TelemetryQuery(storage)
    try:
        # Default (unscoped) sweep must NOT pick it up — same quarantine as
        # `_archive`/`_replay-*`, so it never contaminates a corpus-wide query.
        unscoped = query.run("SELECT COUNT(*) AS n FROM telemetry")
        assert unscoped[0]["n"] == 0

        # An explicit session= name is the documented escape hatch.
        rows = query.run(
            "SELECT channel, value_number, unit FROM telemetry "
            "WHERE channel = 'COOLANT_TEMP' ORDER BY timestamp",
            session=session_id,
        )
        assert len(rows) == 2
        assert rows[0] == {
            "channel": "COOLANT_TEMP",
            "value_number": pytest.approx(87.777778),
            "unit": "degC",
        }
    finally:
        query.close()
