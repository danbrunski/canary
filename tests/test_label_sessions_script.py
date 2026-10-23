"""Tests for the fingerprint hook in ``scripts/label_sessions.py`` (Task 4.2,
eval-longitudinal-and-dtc.md). Also covers the Task 4.6 trend-alert hook that
rides the same post-classify pass.
"""

from __future__ import annotations

import json
from pathlib import Path

import yaml

import scripts.label_sessions as label_sessions_cli
from src.analysis.reference_epoch import (  # noqa: E402
    ReferenceEpoch,
    StatSummary,
    write_reference_epoch,
)
from src.storage.parquet_writer import write_records_to_parquet  # noqa: E402
from src.storage.session_products import (
    CLASSIFICATION_FILENAME,  # noqa: E402
    FINGERPRINT_FILENAME,  # noqa: E402
)
from src.telemetry import TelemetryRecord  # noqa: E402
from src.yaml_load import load_yaml

REPO_ROOT = Path(__file__).resolve().parents[1]
EXAMPLE_YAML = REPO_ROOT / "canary.example.yaml"


def _write_session(root: Path, *, name: str = "2026-05-20T120000", n: int = 60) -> Path:
    session = root / name
    records: list[TelemetryRecord] = []
    for i in range(n):
        t = 1_779_300_000.0 + i
        records.extend(
            [
                TelemetryRecord.obd(t, "RPM", 800.0, "rpm"),
                TelemetryRecord.obd(t, "SPEED", 0.0, "km/h"),
                TelemetryRecord.obd(t, "THROTTLE_POS", 5.0, "%"),
                TelemetryRecord.obd(t, "COOLANT_TEMP", 85.0, "C"),
            ]
        )
    write_records_to_parquet(records, session, max_rows=10_000)
    return session


def test_main_writes_classification_and_fingerprint(tmp_path: Path) -> None:
    session = _write_session(tmp_path)
    rc = label_sessions_cli.main(
        [
            "--sessions-root",
            str(tmp_path),
            "--session",
            session.name,
            "--config",
            str(EXAMPLE_YAML),
        ]
    )
    assert rc == 0
    assert (session / CLASSIFICATION_FILENAME).exists()
    assert (session / FINGERPRINT_FILENAME).exists()


def test_fingerprint_computed_exactly_once_per_session(tmp_path: Path, monkeypatch) -> None:
    """One post-session pass: the fingerprint hook fires once per targeted
    session per invocation, not once per classification pass plus a
    duplicate corpus walk."""
    session = _write_session(tmp_path)
    calls: list[str] = []
    real_compute = label_sessions_cli.compute_fingerprint

    def _spy(session_dir: Path, config: object) -> dict[str, float]:
        calls.append(session_dir.name)
        return real_compute(session_dir, config)  # type: ignore[arg-type]

    monkeypatch.setattr(label_sessions_cli, "compute_fingerprint", _spy)
    rc = label_sessions_cli.main(
        [
            "--sessions-root",
            str(tmp_path),
            "--session",
            session.name,
            "--config",
            str(EXAMPLE_YAML),
        ]
    )
    assert rc == 0
    assert calls == [session.name]


def test_dry_run_does_not_write_fingerprint(tmp_path: Path) -> None:
    session = _write_session(tmp_path)
    rc = label_sessions_cli.main(
        [
            "--sessions-root",
            str(tmp_path),
            "--session",
            session.name,
            "--config",
            str(EXAMPLE_YAML),
            "--dry-run",
        ]
    )
    assert rc == 0
    assert not (session / CLASSIFICATION_FILENAME).exists()
    assert not (session / FINGERPRINT_FILENAME).exists()


# --------------------------------------------------------------------------- #
# Task 4.6 — trend alerting rides the same post-session pass
# --------------------------------------------------------------------------- #


def _config_path_with_alarm_epoch(tmp_path: Path) -> Path:
    """Profiles-disabled config (so paths land exactly where written, not
    under a profile subdir) with a reference epoch whose ``idle_rpm_mean``
    is far enough from ``_write_session``'s flat 800 rpm that a single
    session already trips the default CUSUM alarm."""
    data = load_yaml(EXAMPLE_YAML.read_text())
    data["profiles"] = {"enabled": False}
    # The trend-alert hook (unlike --sessions-root) walks
    # ``config.collection.storage_path`` when no override is given — match
    # it to where ``_write_session`` actually put the session.
    data["collection"]["storage_path"] = str(tmp_path)
    reference_epoch = tmp_path / "reference_epoch.json"
    data.setdefault("daemon", {})["reference_epoch"] = str(reference_epoch)
    data["daemon"]["anomalies_dir"] = str(tmp_path / "anomalies")
    data["daemon"]["trend_alert_state"] = str(tmp_path / "trend_alert_state.json")
    # The trend pass opens the session catalog; keep it off data/control (R07.2).
    data["daemon"]["catalog_db"] = str(tmp_path / "catalog.db")
    epoch = ReferenceEpoch(
        frozen_at="2026-05-01T00:00:00+00:00",
        sessions=("s0",),
        stats={
            "idle_rpm_mean": StatSummary(
                mean=750.0, std=5.0, quantiles={"p10": 745.0, "p90": 755.0}, n=5
            )
        },
    )
    write_reference_epoch(reference_epoch, epoch)
    config_path = tmp_path / "canary.yaml"
    config_path.write_text(yaml.dump(data))
    return config_path


def test_main_emits_trend_alert_for_a_session_in_alarm(tmp_path: Path) -> None:
    session = _write_session(tmp_path)
    config_path = _config_path_with_alarm_epoch(tmp_path)
    rc = label_sessions_cli.main(
        ["--sessions-root", str(tmp_path), "--session", session.name, "--config", str(config_path)]
    )
    assert rc == 0

    anomalies_dir = tmp_path / "anomalies"
    log_files = list(anomalies_dir.glob("anomalies-*.jsonl"))
    assert len(log_files) == 1
    lines = [json.loads(line) for line in log_files[0].read_text().splitlines() if line]
    assert len(lines) == 1
    assert lines[0]["source"] == "trend"
    assert lines[0]["detail"]["stat"] == "idle_rpm_mean"
