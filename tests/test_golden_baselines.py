"""Golden-anchored disposition baseline sidecar (Task 3.7,
online-training-strategy.md)."""

from __future__ import annotations

from pathlib import Path

from src.analysis.training_disposition import ChannelBaseline, GoldenBaselines
from src.models.golden_baselines import (
    golden_baselines_path,
    read_golden_baselines,
    write_golden_baselines,
)


def test_golden_baselines_path_lives_next_to_the_ledger() -> None:
    ledger_path = Path("data/control/training_runs.jsonl")
    path = golden_baselines_path(ledger_path, "isolation_forest")
    assert path == Path("data/control/golden_baselines/isolation_forest.json")


def test_write_then_read_round_trips(tmp_path: Path) -> None:
    baselines = GoldenBaselines(
        baselines=(
            ChannelBaseline(
                channel="obd:RPM",
                operating_mode="idle",
                is_discrete=False,
                median=900.0,
                scaled_mad=12.0,
            ),
            ChannelBaseline(
                channel="can:ENGINE_STATE",
                operating_mode="idle",
                is_discrete=True,
                baseline_values=(0, 1, 2, 3),
            ),
        )
    )
    path = tmp_path / "golden_baselines" / "isolation_forest.json"
    write_golden_baselines(path, baselines)

    restored = read_golden_baselines(path)
    assert restored is not None
    assert restored.get("obd:RPM", "idle") == baselines.get("obd:RPM", "idle")
    assert restored.get("can:ENGINE_STATE", "idle") == baselines.get("can:ENGINE_STATE", "idle")


def test_read_missing_file_returns_none(tmp_path: Path) -> None:
    assert read_golden_baselines(tmp_path / "nope.json") is None


def test_read_corrupt_file_returns_none(tmp_path: Path) -> None:
    path = tmp_path / "bad.json"
    path.write_text("{not valid json")
    assert read_golden_baselines(path) is None


def test_re_declaring_golden_overwrites_not_merges(tmp_path: Path) -> None:
    path = tmp_path / "isolation_forest.json"
    first = GoldenBaselines(
        baselines=(
            ChannelBaseline(
                channel="obd:RPM",
                operating_mode="idle",
                is_discrete=False,
                median=900.0,
                scaled_mad=12.0,
            ),
        )
    )
    write_golden_baselines(path, first)

    second = GoldenBaselines(
        baselines=(
            ChannelBaseline(
                channel="obd:COOLANT_TEMP",
                operating_mode="idle",
                is_discrete=False,
                median=85.0,
                scaled_mad=2.0,
            ),
        )
    )
    write_golden_baselines(path, second)

    restored = read_golden_baselines(path)
    assert restored is not None
    assert restored.get("obd:RPM", "idle") is None  # gone, not merged
    assert restored.get("obd:COOLANT_TEMP", "idle") is not None
