"""Tests for ``scripts/fingerprint.py`` (Task 4.2 backfill / Task 4.3 freeze /
Task 4.5 trends, eval-longitudinal-and-dtc.md).
"""

from __future__ import annotations

import json
import re
from pathlib import Path

import numpy as np
import pytest
import yaml

import scripts.fingerprint as fingerprint_cli
from src.analysis.fingerprint import is_stale, write_fingerprint
from src.analysis.reference_epoch import (  # noqa: E402
    DEFAULT_REFERENCE_EPOCH_PATH,
    ReferenceEpoch,
    StatSummary,
    read_reference_epoch,
    write_reference_epoch,
)
from src.analysis.session_classifier import classify_session, write_classification  # noqa: E402
from src.config import CanaryConfig  # noqa: E402
from src.storage.parquet_writer import write_records_to_parquet  # noqa: E402
from src.storage.session_products import FINGERPRINT_FILENAME, read_fingerprint
from src.telemetry import TelemetryRecord  # noqa: E402

REPO_ROOT = Path(__file__).resolve().parents[1]
EXAMPLE_YAML = REPO_ROOT / "canary.example.yaml"


@pytest.fixture(autouse=True)
def _derived_store_in_tmp(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """The CLI's derived-frame store defaults into the repo (R07.2): point it
    at the test's tree. The frames it builds are what these tests read."""
    monkeypatch.setattr(fingerprint_cli, "DEFAULT_DERIVED_DIR", tmp_path / "derived")


def _config() -> CanaryConfig:
    return CanaryConfig.from_yaml(EXAMPLE_YAML)


def _config_path_with_reference_epoch(tmp_path: Path, reference_epoch: Path) -> Path:
    """A ``canary.yaml`` derived from the example config, but pointing
    ``daemon.reference_epoch`` under ``tmp_path`` (the ``trends`` subcommand
    has no ``--out``-style override, unlike ``freeze``). Profiles disabled so
    ``_scoped_config`` doesn't rewrite the path under a profile slug."""
    data = yaml.safe_load(EXAMPLE_YAML.read_text())
    data.setdefault("daemon", {})["reference_epoch"] = str(reference_epoch)
    # ``trends`` opens the session catalog; keep it off data/control (R07.2).
    data["daemon"]["catalog_db"] = str(tmp_path / "catalog.db")
    data["profiles"] = {"enabled": False}
    out = tmp_path / "canary.yaml"
    out.write_text(yaml.dump(data))
    return out


def _log_messages(capsys: pytest.CaptureFixture[str]) -> list[str]:
    """Parse the JSON-structured log lines ``configure_logging`` writes to
    stdout back into their ``message`` strings."""
    out = capsys.readouterr().out
    return [json.loads(line)["message"] for line in out.splitlines() if line.strip()]


def _write_ready_session(root: Path, name: str) -> Path:
    """Idle + city coverage -> ``disposition == "ready"`` from the classifier."""
    session = root / name
    records: list[TelemetryRecord] = []
    n = 120
    for i in range(n):
        t = 1_779_300_000.0 + i
        if i < n // 4 or i >= 3 * n // 4:
            rpm, speed, throttle = 800.0, 0.0, 5.0
        else:
            rpm, speed, throttle = 1900.0, 35.0, 22.0
        records.extend(
            [
                TelemetryRecord.obd(t, "RPM", rpm, "rpm"),
                TelemetryRecord.obd(t, "SPEED", speed, "km/h"),
                TelemetryRecord.obd(t, "THROTTLE_POS", throttle, "%"),
                TelemetryRecord.obd(t, "COOLANT_TEMP", 85.0, "C"),
            ]
        )
    write_records_to_parquet(records, session, max_rows=10_000)
    return session


def _classify_ready(session: Path, config: CanaryConfig) -> None:
    classification = classify_session(session, config)
    assert classification.disposition == "ready"
    write_classification(session, classification)


# --------------------------------------------------------------------------- #
# backfill (Task 4.2)
# --------------------------------------------------------------------------- #


def test_backfill_computes_missing_fingerprint(tmp_path: Path) -> None:
    session = _write_ready_session(tmp_path, "2026-05-20T120000")
    _classify_ready(session, _config())
    assert read_fingerprint(session) is None

    rc = fingerprint_cli.main(
        ["--sessions-root", str(tmp_path), "--config", str(EXAMPLE_YAML), "backfill", "--all"]
    )
    assert rc == 0
    assert (session / FINGERPRINT_FILENAME).exists()
    assert read_fingerprint(session) is not None


def test_backfill_skips_current_without_rebuild(tmp_path: Path, monkeypatch) -> None:
    session = _write_ready_session(tmp_path, "2026-05-20T120000")
    _classify_ready(session, _config())
    fingerprint_cli.main(
        ["--sessions-root", str(tmp_path), "--config", str(EXAMPLE_YAML), "backfill", "--all"]
    )
    assert not is_stale(read_fingerprint(session))

    calls: list[str] = []
    real_compute = fingerprint_cli.compute_fingerprint

    def _spy(session_dir: Path, config: object) -> dict[str, float]:
        calls.append(session_dir.name)
        return real_compute(session_dir, config)  # type: ignore[arg-type]

    monkeypatch.setattr(fingerprint_cli, "compute_fingerprint", _spy)
    rc = fingerprint_cli.main(
        ["--sessions-root", str(tmp_path), "--config", str(EXAMPLE_YAML), "backfill", "--all"]
    )
    assert rc == 0
    assert calls == []  # already current -> skipped, not recomputed


def test_backfill_rebuild_forces_recompute(tmp_path: Path, monkeypatch) -> None:
    session = _write_ready_session(tmp_path, "2026-05-20T120000")
    _classify_ready(session, _config())
    fingerprint_cli.main(
        ["--sessions-root", str(tmp_path), "--config", str(EXAMPLE_YAML), "backfill", "--all"]
    )

    calls: list[str] = []
    real_compute = fingerprint_cli.compute_fingerprint

    def _spy(session_dir: Path, config: object) -> dict[str, float]:
        calls.append(session_dir.name)
        return real_compute(session_dir, config)  # type: ignore[arg-type]

    monkeypatch.setattr(fingerprint_cli, "compute_fingerprint", _spy)
    rc = fingerprint_cli.main(
        [
            "--sessions-root",
            str(tmp_path),
            "--config",
            str(EXAMPLE_YAML),
            "backfill",
            "--all",
            "--rebuild",
        ]
    )
    assert rc == 0
    assert calls == [session.name]


# --------------------------------------------------------------------------- #
# freeze (Task 4.3)
# --------------------------------------------------------------------------- #


def test_freeze_builds_correct_reference_epoch(tmp_path: Path) -> None:
    session_a = _write_ready_session(tmp_path, "2026-05-20T120000")
    session_b = _write_ready_session(tmp_path, "2026-05-20T130000")
    _classify_ready(session_a, _config())
    _classify_ready(session_b, _config())
    write_fingerprint(session_a, {"idle_rpm_mean": 800.0, "only_in_a": 1.0})
    write_fingerprint(session_b, {"idle_rpm_mean": 820.0})

    out_path = tmp_path / "reference_epoch.json"
    rc = fingerprint_cli.main(
        [
            "--sessions-root",
            str(tmp_path),
            "--config",
            str(EXAMPLE_YAML),
            "freeze",
            "--notes",
            "test freeze",
            "--out",
            str(out_path),
        ]
    )
    assert rc == 0
    epoch = read_reference_epoch(out_path)
    assert epoch is not None
    assert epoch.notes == "test freeze"
    assert epoch.sessions == (session_a.name, session_b.name)

    rpm_stat = epoch.stats["idle_rpm_mean"]
    assert rpm_stat.n == 2
    assert rpm_stat.mean == 810.0
    assert rpm_stat.std == np.std([800.0, 820.0])
    assert rpm_stat.quantiles["p50"] == np.quantile([800.0, 820.0], 0.5)

    # A stat only one session carried isn't diluted by the other's absence.
    only_a_stat = epoch.stats["only_in_a"]
    assert only_a_stat.n == 1
    assert only_a_stat.mean == 1.0
    assert only_a_stat.std == 0.0


def test_freeze_excludes_non_ready_and_unfingerprinted_sessions(tmp_path: Path) -> None:
    ready = _write_ready_session(tmp_path, "2026-05-20T120000")
    _classify_ready(ready, _config())
    write_fingerprint(ready, {"idle_rpm_mean": 800.0})

    # Classified but never fingerprinted -> excluded.
    unfingerprinted = _write_ready_session(tmp_path, "2026-05-20T130000")
    _classify_ready(unfingerprinted, _config())

    # Fingerprinted but never classified ("ready") -> excluded.
    unclassified = _write_ready_session(tmp_path, "2026-05-20T140000")
    write_fingerprint(unclassified, {"idle_rpm_mean": 900.0})

    out_path = tmp_path / "reference_epoch.json"
    rc = fingerprint_cli.main(
        [
            "--sessions-root",
            str(tmp_path),
            "--config",
            str(EXAMPLE_YAML),
            "freeze",
            "--out",
            str(out_path),
        ]
    )
    assert rc == 0
    epoch = read_reference_epoch(out_path)
    assert epoch is not None
    assert epoch.sessions == (ready.name,)


def test_freeze_fails_with_no_eligible_sessions(tmp_path: Path) -> None:
    out_path = tmp_path / "reference_epoch.json"
    rc = fingerprint_cli.main(
        [
            "--sessions-root",
            str(tmp_path),
            "--config",
            str(EXAMPLE_YAML),
            "freeze",
            "--out",
            str(out_path),
        ]
    )
    assert rc == 1
    assert not out_path.exists()


# --------------------------------------------------------------------------- #
# Task 4.3 acceptance: nothing else auto-writes reference_epoch.json
# --------------------------------------------------------------------------- #


def test_only_freeze_calls_write_reference_epoch() -> None:
    """Static check: ``write_reference_epoch`` is called from exactly the
    ``freeze`` CLI path (plus its own definition/tests), never from a
    daemon/live/classification code path that could silently re-freeze."""
    pattern = re.compile(r"write_reference_epoch\(")
    allowed = {
        REPO_ROOT / "src" / "analysis" / "reference_epoch.py",  # definition
        REPO_ROOT / "scripts" / "fingerprint.py",  # the freeze CLI
    }
    offenders: list[str] = []
    for base in (REPO_ROOT / "src", REPO_ROOT / "scripts"):
        for path in base.rglob("*.py"):
            if path in allowed:
                continue
            text = path.read_text()
            if pattern.search(text):
                offenders.append(str(path.relative_to(REPO_ROOT)))
    assert offenders == []


def test_default_reference_epoch_path_matches_config_default() -> None:
    config = _config()
    assert config.daemon.reference_epoch == DEFAULT_REFERENCE_EPOCH_PATH


# --------------------------------------------------------------------------- #
# trends (Task 4.5)
# --------------------------------------------------------------------------- #


def test_trends_unavailable_without_frozen_epoch(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    reference_epoch = tmp_path / "reference_epoch.json"
    config_path = _config_path_with_reference_epoch(tmp_path, reference_epoch)
    rc = fingerprint_cli.main(
        ["--sessions-root", str(tmp_path), "--config", str(config_path), "trends"]
    )
    assert rc == 1
    assert any("no reference epoch" in msg for msg in _log_messages(capsys))


def test_trends_prints_a_row_per_stat(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    reference_epoch = tmp_path / "reference_epoch.json"
    epoch = ReferenceEpoch(
        frozen_at="2026-06-01T00:00:00+00:00",
        sessions=("s0",),
        stats={
            "idle_rpm_mean": StatSummary(
                mean=800.0, std=10.0, quantiles={"p10": 790.0, "p90": 810.0}, n=5
            )
        },
    )
    write_reference_epoch(reference_epoch, epoch)
    session = tmp_path / "2026-06-01T000000"
    session.mkdir()
    write_fingerprint(session, {"idle_rpm_mean": 800.0})

    config_path = _config_path_with_reference_epoch(tmp_path, reference_epoch)
    rc = fingerprint_cli.main(
        ["--sessions-root", str(tmp_path), "--config", str(config_path), "trends"]
    )
    assert rc == 0
    messages = _log_messages(capsys)
    assert any("idle_rpm_mean" in msg for msg in messages)
