"""Tests for the vehicle maintenance/intervention ledger (Task 3.1)."""

from __future__ import annotations

from datetime import date
from pathlib import Path

from src.analysis.vehicle_ledger import VehicleEvent, VehicleLedgerStore


def test_store_starts_empty(tmp_path: Path) -> None:
    store = VehicleLedgerStore(tmp_path / "ledger.jsonl")
    assert store.records() == []
    assert store.raw_records() == []
    assert store.latest_boundary() is None


def test_record_append_and_list_round_trip(tmp_path: Path) -> None:
    store = VehicleLedgerStore(tmp_path / "ledger.jsonl")
    store.record(
        occurred_at=date(2026, 1, 1),
        kind="maintenance",
        title="Oil change",
        odometer_km=123456.0,
    )
    store.record(
        occurred_at=date(2026, 2, 1),
        kind="mod",
        title="Cold air intake",
        affected_channels=["obd:MAF"],
        corpus_boundary=True,
    )
    records = store.records()
    assert len(records) == 2
    # Ordered by occurred_at, oldest first.
    assert [r.title for r in records] == ["Oil change", "Cold air intake"]
    assert records[1].corpus_boundary is True
    assert records[1].affected_channels == ["obd:MAF"]


def test_correction_appends_new_line_but_collapses_by_id(tmp_path: Path) -> None:
    """A relabel/correction is just another append with the same id —
    JSONL append-only semantics: no rewrite of prior entries."""
    path = tmp_path / "ledger.jsonl"
    store = VehicleLedgerStore(path)
    event = store.record(occurred_at=date(2026, 1, 1), kind="observation", title="Rough idle")
    store.record(
        occurred_at=date(2026, 1, 1),
        kind="observation",
        title="Rough idle — corrected: was actually a vacuum leak",
        event_id=event.id,
    )
    # Two lines on disk (no rewrite of the first).
    lines = path.read_text().strip().splitlines()
    assert len(lines) == 2
    # Full chain preserved.
    raw = store.raw_records()
    assert len(raw) == 2
    assert raw[0].title == "Rough idle"
    # Latest-by-id collapses to the correction.
    records = store.records()
    assert len(records) == 1
    assert records[0].title.startswith("Rough idle — corrected")
    assert store.get(event.id) is not None
    assert store.get(event.id).title.startswith("Rough idle — corrected")  # type: ignore[union-attr]


def test_get_unknown_id_returns_none(tmp_path: Path) -> None:
    store = VehicleLedgerStore(tmp_path / "ledger.jsonl")
    assert store.get("nonexistent") is None


def test_latest_boundary_ignores_non_boundary_events(tmp_path: Path) -> None:
    store = VehicleLedgerStore(tmp_path / "ledger.jsonl")
    store.record(occurred_at=date(2026, 1, 1), kind="maintenance", title="Oil change")
    boundary = store.record(
        occurred_at=date(2026, 3, 1),
        kind="repair",
        title="Head gasket replaced",
        corpus_boundary=True,
    )
    store.record(occurred_at=date(2026, 4, 1), kind="observation", title="Sounds fine now")
    latest = store.latest_boundary()
    assert latest is not None
    assert latest.id == boundary.id


def test_latest_boundary_picks_most_recent_occurrence(tmp_path: Path) -> None:
    store = VehicleLedgerStore(tmp_path / "ledger.jsonl")
    store.record(occurred_at=date(2026, 1, 1), kind="mod", title="Intake", corpus_boundary=True)
    second = store.record(
        occurred_at=date(2026, 5, 1), kind="repair", title="Turbo swap", corpus_boundary=True
    )
    latest = store.latest_boundary()
    assert latest is not None
    assert latest.id == second.id


def test_vehicle_event_to_dict_is_json_safe(tmp_path: Path) -> None:
    event = VehicleEvent(occurred_at=date(2026, 1, 1), kind="dtc", title="P0420 stored")
    payload = event.to_dict()
    assert payload["occurred_at"] == "2026-01-01"
    assert payload["kind"] == "dtc"
    assert VehicleEvent.from_dict(payload).id == event.id
