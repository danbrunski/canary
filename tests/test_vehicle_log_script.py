"""Tests for ``scripts/vehicle_log.py`` (Task 3.3 CLI)."""

from __future__ import annotations

from pathlib import Path

import pytest

import scripts.vehicle_log as vehicle_log_cli
from src.vehicle.vehicle_ledger import VehicleLedgerStore  # noqa: E402

REPO_ROOT = Path(__file__).resolve().parents[1]
EXAMPLE_YAML = REPO_ROOT / "canary.example.yaml"


def test_add_then_list_round_trips(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    ledger_path = tmp_path / "vehicle_ledger.jsonl"
    rc = vehicle_log_cli.main(
        [
            "--config",
            str(EXAMPLE_YAML),
            "--profile",
            "cli-test-vehicle",
            "--ledger",
            str(ledger_path),
            "add",
            "--occurred",
            "2026-07-19",
            "--kind",
            "mod",
            "--title",
            "Cold air intake",
            "--channel",
            "obd:MAF",
            "--boundary",
        ]
    )
    assert rc == 0
    store = VehicleLedgerStore(ledger_path)
    records = store.records()
    assert len(records) == 1
    assert records[0].title == "Cold air intake"
    assert records[0].corpus_boundary is True
    assert records[0].affected_channels == ["obd:MAF"]

    capsys.readouterr()
    rc = vehicle_log_cli.main(
        [
            "--config",
            str(EXAMPLE_YAML),
            "--profile",
            "cli-test-vehicle",
            "--ledger",
            str(ledger_path),
            "list",
        ]
    )
    assert rc == 0
    out = capsys.readouterr().out
    assert "Cold air intake" in out
    assert "[boundary]" in out


def test_list_on_empty_ledger_prints_placeholder(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    ledger_path = tmp_path / "vehicle_ledger.jsonl"
    rc = vehicle_log_cli.main(["--config", str(EXAMPLE_YAML), "--ledger", str(ledger_path), "list"])
    assert rc == 0
    assert "no vehicle events recorded" in capsys.readouterr().out


def test_add_rejects_bad_date(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    ledger_path = tmp_path / "vehicle_ledger.jsonl"
    rc = vehicle_log_cli.main(
        [
            "--config",
            str(EXAMPLE_YAML),
            "--ledger",
            str(ledger_path),
            "add",
            "--occurred",
            "not-a-date",
            "--kind",
            "observation",
            "--title",
            "x",
        ]
    )
    assert rc == 1
    assert not ledger_path.exists()
