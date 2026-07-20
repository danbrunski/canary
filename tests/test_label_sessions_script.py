"""Tests for the fingerprint hook in ``scripts/label_sessions.py`` (Task 4.2,
eval-longitudinal-and-dtc.md).
"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
import label_sessions as label_sessions_cli  # noqa: E402

from src.analysis.fingerprint import FINGERPRINT_FILENAME  # noqa: E402
from src.analysis.session_classifier import CLASSIFICATION_FILENAME  # noqa: E402
from src.storage.parquet_writer import write_records_to_parquet  # noqa: E402
from src.telemetry import TelemetryRecord  # noqa: E402

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
