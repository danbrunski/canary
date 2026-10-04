"""Tests for the vehicle maintenance/intervention ledger (Task 3.1)."""

from __future__ import annotations

from datetime import date
from pathlib import Path

from src.vehicle.vehicle_ledger import VehicleEvent, VehicleLedgerStore


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


def test_a_correction_replaces_the_entry_on_disk(tmp_path: Path) -> None:
    """2026-10-04: a mis-measured tread depth stayed in the file under its
    correction and had to be cut out by hand. A correction now replaces it."""
    path = tmp_path / "ledger.jsonl"
    store = VehicleLedgerStore(path)
    event = store.record(occurred_at=date(2026, 1, 1), kind="observation", title="Rough idle")
    other = store.record(occurred_at=date(2026, 1, 2), kind="maintenance", title="Oil change")
    store.record(
        occurred_at=date(2026, 1, 1),
        kind="observation",
        title="Rough idle — corrected: was actually a vacuum leak",
        event_id=event.id,
    )
    assert 'Rough idle"' not in path.read_text(), "the erroneous version is gone"
    assert len(store.raw_records()) == 2
    assert [r.title for r in store.records()] == [
        "Rough idle — corrected: was actually a vacuum leak",
        "Oil change",
    ]
    assert store.get(other.id) == other


def test_remove_deletes_every_line_for_the_id_and_nothing_else(tmp_path: Path) -> None:
    path = tmp_path / "ledger.jsonl"
    store = VehicleLedgerStore(path)
    wrong = store.record(occurred_at=date(2026, 10, 4), kind="maintenance", title="0.592 in")
    kept = store.record(occurred_at=date(2026, 10, 4), kind="maintenance", title="0.475 in")
    # A file written before replace existed can hold two lines for one id,
    # and a torn line must survive an edit of something else.
    with path.open("a") as handle:
        handle.write(wrong.model_copy(update={"title": "0.59 in"}).model_dump_json() + "\n")
        handle.write('{"torn": \n')
    removed = store.remove(wrong.id)
    assert removed is not None and removed.title == "0.59 in"
    text = path.read_text()
    assert "0.59" not in text
    assert '{"torn": ' in text
    assert store.records() == [kept]
    assert store.remove(wrong.id) is None


def test_replace_and_append_share_one_lock_per_file(tmp_path: Path) -> None:
    """Every store instance on a file serializes on one lock, so a rewrite
    cannot drop a concurrent append (the DTC auto-append builds its own store)."""
    import threading

    path = tmp_path / "ledger.jsonl"
    first = VehicleLedgerStore(path)
    target = first.record(occurred_at=date(2026, 1, 1), kind="observation", title="x")

    def appender() -> None:
        store = VehicleLedgerStore(path)
        for n in range(50):
            store.record(occurred_at=date(2026, 1, 2), kind="dtc", title=f"P{n:04d}")

    thread = threading.Thread(target=appender)
    thread.start()
    for n in range(50):
        first.record(
            occurred_at=date(2026, 1, 1), kind="observation", title=f"x{n}", event_id=target.id
        )
    thread.join()
    titles = [r.title for r in first.records()]
    assert titles.count("x49") == 1
    assert sum(t.startswith("P") for t in titles) == 50


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
