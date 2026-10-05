"""E01.3: memory persists, is queried exactly, recurs, and stays out of replays."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest

from src.evidence import (
    Conditions,
    Evidence,
    Intervention,
    Investigation,
    MemoryLedger,
    MemoryQuery,
    MemoryReferenceError,
    ReplayIsolationError,
    Retraction,
    SourceRef,
    VehicleIdentity,
    memory_ledger_for,
    replay_memory_path,
)
from src.evidence import ledger as ledger_module
from tests.configs import isolated_config, scoped_test_config
from tests.test_evidence_memory import T0, _conditions, _evidence, _operator, _vehicle


def _ledger(tmp_path: Path) -> MemoryLedger:
    return MemoryLedger(tmp_path / "memory.jsonl", vehicle="truck")


def _occurrence(session: str, start: datetime, **overrides: Any) -> Evidence:
    """One evidence record over ``session`` starting at ``start`` (one minute)."""
    fields: dict[str, Any] = {
        "valid_from": start,
        "valid_to": start + timedelta(minutes=1),
        "sources": (
            SourceRef(
                session=session,
                window_start=start,
                window_end=start + timedelta(minutes=1),
                channels=("obd:RPM", "can:ECM_RPM"),
                n_samples=60,
            ),
        ),
    }
    fields.update(overrides)
    return _evidence(**fields)


def _flash(at: datetime) -> Intervention:
    return Intervention(
        vehicle=VehicleIdentity(
            profile="truck", vin=None, program_id=None, calibration="ZS44A-20261004"
        ),
        provenance=_operator(),
        conditions=Conditions(regime="all conditions", vehicle_state="ignition_only"),
        valid_from=at,
        intervention_kind="calibration",
        title="flash ZS44A-20261004",
        source="tune_revision",
        source_ref="ZS44A-20261004",
        source_digest="d" * 64,
        affects=("*",),
        effective_precision="instant",
        image_sha256=None,
    )


# -- persistence -----------------------------------------------------------------


def test_restart_preserves_records_and_their_derived_state(tmp_path: Path) -> None:
    ledger = _ledger(tmp_path)
    first = ledger.append(_evidence(), now=T0 + timedelta(hours=1))
    second = ledger.append(_evidence(input_hashes={"pairs": "b" * 64}), now=T0 + timedelta(hours=2))
    before = ledger.entries()
    ledger.reload()  # this process forgets its parsed index, as a restart would
    after = MemoryLedger(ledger.path, vehicle="truck").entries()
    assert after == before
    assert [e.record.id for e in after] == [first.id, second.id]
    assert not after[0].current and after[1].current


def test_another_instance_and_another_writer_are_seen_without_a_full_reread(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Appends are bounded: only bytes written since the last read are parsed."""
    ledger = _ledger(tmp_path)
    for minute in range(20):
        ledger.append(_occurrence("2026-09-20T180000", T0 + timedelta(minutes=minute)))
    parsed: list[bytes] = []
    original = ledger_module._Index.ingest_line

    def counting(self: Any, raw: bytes) -> None:
        parsed.append(raw)
        original(self, raw)

    monkeypatch.setattr(ledger_module._Index, "ingest_line", counting)
    ledger.append(_occurrence("2026-09-20T180000", T0 + timedelta(minutes=30)))
    assert parsed == []  # its own append is indexed directly
    other = MemoryLedger(ledger.path, vehicle="truck")
    assert len(other.entries()) == 21
    assert parsed == []  # the shared index already holds everything
    # A line written by another process: only it is parsed.
    stored = other.entries()[0].record
    foreign = _occurrence("2026-09-21T090000", T0 + timedelta(days=1))
    written = MemoryLedger(tmp_path / "elsewhere.jsonl", vehicle="truck").append(
        foreign, now=T0 + timedelta(days=400)
    )
    with ledger.path.open("a") as handle:
        handle.write((tmp_path / "elsewhere.jsonl").read_text())
    assert len([line for line in parsed if line.strip()]) == 0
    assert ledger.entry(written.id).record == written
    assert len([line for line in parsed if line.strip()]) == 1
    assert ledger.entry(stored.id).record == stored


def test_a_torn_tail_does_not_swallow_the_next_append(tmp_path: Path) -> None:
    ledger = _ledger(tmp_path)
    kept = ledger.append(_evidence(), now=T0)
    with ledger.path.open("a") as handle:
        handle.write('{"schema": "canary-memory/1", "kind": "evid')  # a crash mid-write
    after = ledger.append(_evidence(subject="other subject"), now=T0 + timedelta(hours=1))
    ledger.reload()
    ids = {e.record.id for e in MemoryLedger(ledger.path, vehicle="truck").entries()}
    assert ids == {kept.id, after.id}


def test_a_replaced_file_is_reread_whole(tmp_path: Path) -> None:
    ledger = _ledger(tmp_path)
    ledger.append(_evidence(), now=T0)
    ledger.append(_evidence(subject="second"), now=T0 + timedelta(hours=1))
    lines = ledger.path.read_text().splitlines(keepends=True)
    replacement = tmp_path / "replacement.jsonl"
    replacement.write_text(lines[0])
    replacement.replace(ledger.path)
    assert len(ledger.entries()) == 1


# -- recurrence and retry identity ------------------------------------------------


def test_a_retry_is_the_same_record_and_a_repeated_event_is_a_recurrence(
    tmp_path: Path,
) -> None:
    ledger = _ledger(tmp_path)
    first = ledger.append(_occurrence("2026-09-20T180000", T0), now=T0 + timedelta(hours=1))
    retried = ledger.append(_occurrence("2026-09-20T180000", T0), now=T0 + timedelta(hours=2))
    assert retried.id == first.id  # a retry/replayed append is not a second finding
    later = T0 + timedelta(days=3)
    again = ledger.append(_occurrence("2026-09-23T080000", later), now=later + timedelta(hours=1))
    assert again.supersedes == ()  # the same subject over other data supersedes nothing
    occurrences = ledger.recurrences(again.id)
    assert [e.record.id for e in occurrences] == [first.id, again.id]
    assert all(e.current for e in occurrences)
    assert len(ledger.entries()) == 2


def test_a_recompute_over_the_same_data_is_one_occurrence(tmp_path: Path) -> None:
    ledger = _ledger(tmp_path)
    first = ledger.append(_occurrence("2026-09-20T180000", T0), now=T0 + timedelta(hours=1))
    recomputed = ledger.append(
        _occurrence("2026-09-20T180000", T0, input_hashes={"pairs": "d" * 64}),
        now=T0 + timedelta(hours=2),
    )
    assert recomputed.supersedes == (first.id,)
    assert [e.record.id for e in ledger.recurrences(recomputed.id)] == [recomputed.id]


def test_an_incident_groups_occurrences_across_subjects(tmp_path: Path) -> None:
    ledger = _ledger(tmp_path)
    a = ledger.append(
        _occurrence("2026-09-20T180000", T0, subject="plausibility:maf:a", incident="maf_vs_rpm"),
        now=T0 + timedelta(hours=1),
    )
    b = ledger.append(
        _occurrence(
            "2026-09-22T180000",
            T0 + timedelta(days=2),
            subject="plausibility:maf:b",
            incident="maf_vs_rpm",
        ),
        now=T0 + timedelta(days=2, hours=1),
    )
    ledger.append(
        _occurrence("2026-09-22T180000", T0 + timedelta(days=2)), now=T0 + timedelta(days=3)
    )
    assert [e.record.id for e in ledger.query(incident="maf_vs_rpm")] == [a.id, b.id]
    assert [e.record.id for e in ledger.recurrences(a.id)] == [a.id, b.id]


# -- the two time axes ------------------------------------------------------------


def test_what_preceded_the_flash_is_a_valid_time_question(tmp_path: Path) -> None:
    ledger = _ledger(tmp_path)
    flash_at = T0 + timedelta(days=5)
    before = ledger.append(_occurrence("2026-09-20T180000", T0), now=T0 + timedelta(hours=1))
    flash = ledger.append(_flash(flash_at), now=flash_at + timedelta(hours=1))
    after = ledger.append(
        _occurrence("2026-09-26T180000", flash_at + timedelta(days=1)),
        now=flash_at + timedelta(days=1, hours=1),
    )
    # A late-entered record about the earlier drive still precedes the flash.
    late = ledger.append(
        _occurrence("2026-09-21T180000", T0 + timedelta(days=1), subject="late"),
        now=flash_at + timedelta(days=2),
    )
    preceded = ledger.query(kinds=("evidence",), valid_before=flash.valid_from)
    assert [e.record.id for e in preceded] == [before.id, late.id]
    followed = ledger.query(kinds=("evidence",), valid_after=flash.valid_from)
    assert [e.record.id for e in followed] == [after.id]


def test_what_we_believed_then_survives_a_retraction(tmp_path: Path) -> None:
    ledger = _ledger(tmp_path)
    t1, t2, t3 = T0 + timedelta(days=1), T0 + timedelta(days=2), T0 + timedelta(days=3)
    finding = ledger.append(_evidence(), now=t1)
    ledger.append(
        Retraction(
            vehicle=_vehicle(),
            provenance=_operator(),
            valid_from=t3,
            retracts=(finding.id,),
            reason="means matched by accident: the low byte of a 16-bit counter",
        ),
        now=t3,
    )
    then = ledger.query(kinds=("evidence",), as_of=t2)
    assert [e.record.id for e in then] == [finding.id]
    assert then[0].current and then[0].retracted_by is None
    assert ledger.query(kinds=("evidence",)) == []  # not believed now
    history = ledger.query(kinds=("evidence",), include_superseded=True)
    assert history[0].retracted_by is not None and history[0].superseded_at == t3
    assert ledger.entry(finding.id, as_of=t2).current
    # Before it was recorded, it was not believed at all.
    assert ledger.query(kinds=("evidence",), as_of=T0) == []


def test_as_of_sees_the_belief_a_recompute_later_replaced(tmp_path: Path) -> None:
    ledger = _ledger(tmp_path)
    old = ledger.append(_evidence(), now=T0 + timedelta(hours=1))
    new = ledger.append(_evidence(input_hashes={"pairs": "e" * 64}), now=T0 + timedelta(hours=3))
    assert [e.record.id for e in ledger.query(as_of=T0 + timedelta(hours=2))] == [old.id]
    assert [e.record.id for e in ledger.query()] == [new.id]


# -- exact filters and text -------------------------------------------------------


def test_exact_filters_by_conditions_identity_session_and_record_time(tmp_path: Path) -> None:
    ledger = _ledger(tmp_path)
    wot = ledger.append(_occurrence("2026-09-20T180000", T0), now=T0 + timedelta(hours=1))
    idle = ledger.append(
        _occurrence(
            "2026-09-20T180000",
            T0 + timedelta(minutes=10),
            subject="idle",
            conditions=Conditions(regime="idle: rpm<900", vehicle_state="engine_running"),
            vehicle=VehicleIdentity(
                profile="truck", vin=None, program_id="ZS04C", calibration=None
            ),
        ),
        now=T0 + timedelta(hours=2),
    )
    investigation = ledger.append(
        Investigation(
            vehicle=_vehicle(),
            provenance=_operator(),
            conditions=_conditions(),
            valid_from=T0,
            title="Lean at WOT on bank two",
            question="is the pump drooping under load",
            observations=(wot.id,),
        ),
        now=T0 + timedelta(hours=3),
    )

    def ids(**filters: Any) -> list[str]:
        return [e.record.id for e in ledger.query(MemoryQuery(**filters))]

    assert ids(regime=_conditions().regime) == [wot.id, investigation.id]
    assert ids(regime="idle: rpm<900") == [idle.id]
    assert ids(regime="idle") == []  # exact, not fuzzy
    assert ids(calibration=None) == [idle.id]  # unknown is a value you can ask for
    assert ids(calibration="ZS44A") == [wot.id, investigation.id]
    assert ids(program_id="ZS04C") == [idle.id]
    assert ids(kinds=("investigation",)) == [investigation.id]
    assert ids(session="2026-09-20T180000") == [wot.id, idle.id]
    assert ids(recorded_after=T0 + timedelta(hours=2)) == [idle.id, investigation.id]
    assert ids(recorded_before=T0 + timedelta(hours=1)) == [wot.id]
    assert ids(text="PUMP drooping") == [investigation.id]
    assert ids(text="pump nothing-like-this") == []
    assert ids(text="rpm<900") == [idle.id]
    assert ids(limit=1) == [wot.id]
    with pytest.raises(ValueError, match="unknown record kind"):
        MemoryQuery(kinds=("finding",))
    with pytest.raises(ValueError, match="timezone-aware"):
        MemoryQuery(as_of=datetime(2026, 9, 20))


def test_queries_never_return_another_vehicles_records(tmp_path: Path) -> None:
    ledger = _ledger(tmp_path)
    mine = ledger.append(_evidence(), now=T0)
    other = MemoryLedger(ledger.path, vehicle="other")
    other.append(_evidence(vehicle=_vehicle("other")), now=T0 + timedelta(hours=1))
    assert [e.record.id for e in ledger.query()] == [mine.id]


# -- replay isolation ---------------------------------------------------------------


def test_replay_output_cannot_enter_live_memory(tmp_path: Path) -> None:
    config = scoped_test_config(isolated_config(tmp_path))
    live = memory_ledger_for(config)
    replay = memory_ledger_for(config, replay_id="2026-10-04T120000")
    assert replay.path == replay_memory_path(live.path, "2026-10-04T120000")
    assert replay.path.parent.name == "_replay"
    assert replay.path.is_relative_to(live.path.parent)

    kept = live.append(_evidence(vehicle=_vehicle("test-vehicle")), now=T0)
    snapshot = live.path.read_bytes()
    replayed = _occurrence(
        "_replay-2026-10-04T120000", T0, vehicle=_vehicle("test-vehicle"), subject="replayed"
    )
    with pytest.raises(ReplayIsolationError, match="replay"):
        live.append(replayed, now=T0 + timedelta(hours=1))
    stored = replay.append(replayed, now=T0 + timedelta(hours=1))
    assert live.path.read_bytes() == snapshot  # the live file is untouched
    assert [e.record.id for e in live.entries()] == [kept.id]
    assert [e.record.id for e in replay.entries()] == [stored.id]
    with pytest.raises(ValueError, match="not a replay id"):
        replay_memory_path(live.path, "../escape")


def test_record_times_from_both_axes_are_aware(tmp_path: Path) -> None:
    ledger = _ledger(tmp_path)
    stored = ledger.append(_evidence())
    assert stored.recorded_at is not None and stored.recorded_at.tzinfo is not None
    assert ledger.query(as_of=datetime.now(UTC) + timedelta(seconds=1))[0].record == stored


def test_an_in_place_rewrite_on_the_same_inode_is_reread(tmp_path: Path) -> None:
    """A file rewritten in place, longer or the same length, is not trusted to
    the cached index: the head/tail fingerprint no longer matches."""
    ledger = _ledger(tmp_path)
    first = ledger.append(_evidence(), now=T0)
    ledger.append(_evidence(subject="second"), now=T0 + timedelta(hours=1))
    other = MemoryLedger(tmp_path / "other.jsonl", vehicle="truck")
    replaced = [
        other.append(_evidence(subject=f"replacement {n} with a longer subject"), now=T0)
        for n in range(3)
    ]
    inode = ledger.path.stat().st_ino
    with ledger.path.open("w") as handle:  # same inode, longer content
        handle.write(other.path.read_text())
    assert ledger.path.stat().st_ino == inode
    assert [e.record.id for e in ledger.entries()] == [r.id for r in replaced]
    assert first.id not in {e.record.id for e in ledger.entries()}
    # Same length, different bytes: still caught.
    text = ledger.path.read_text()
    with ledger.path.open("w") as handle:
        handle.write(text.replace("replacement 2", "replacement 9"))
    subjects = [getattr(e.record, "subject", None) for e in ledger.entries()]
    assert subjects[2] == "replacement 9 with a longer subject"


_RACER = """
import sys, time
from pathlib import Path
sys.path.insert(0, {root!r})
from src.evidence import MemoryLedger, ledger as lm
from tests.test_evidence_memory import _evidence
ledger = MemoryLedger(Path({path!r}), vehicle="truck")
ledger.entries()  # each racer starts from the same (soon stale) index
original = lm.MemoryLedger._write
def slow(self, index, stored):
    time.sleep(0.5)
    original(self, index, stored)
lm.MemoryLedger._write = slow
print(ledger.append(_evidence(input_hashes={{"pairs": sys.argv[1] * 64}})).id)
"""


def test_two_processes_recomputing_at_once_leave_one_current_record(tmp_path: Path) -> None:
    import subprocess
    import sys

    from src.paths import REPO_ROOT

    ledger = _ledger(tmp_path)
    base = ledger.append(_evidence())
    assert isinstance(base, Evidence)
    code = _RACER.format(root=str(REPO_ROOT), path=str(ledger.path))
    racers = [
        subprocess.Popen(
            [sys.executable, "-c", code, char],
            cwd=REPO_ROOT,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
        )
        for char in "cd"
    ]
    outputs = [racer.communicate(timeout=120) for racer in racers]
    assert all(racer.returncode == 0 for racer in racers), outputs
    current = ledger.query(subject=base.subject)
    assert len(current) == 1
    history = ledger.query(subject=base.subject, include_superseded=True)
    assert len(history) == 3  # the base and both recomputes, chained, nothing lost


def test_a_retraction_cannot_be_retracted(tmp_path: Path) -> None:
    """Withdrawing a retraction is refused; restating the finding is the way
    back, and as_of keeps the belief at each time."""
    ledger = _ledger(tmp_path)
    t1, t2, t3, t4 = (T0 + timedelta(days=n) for n in (1, 2, 3, 4))
    finding = ledger.append(_evidence(), now=t1)
    retraction = ledger.append(
        Retraction(
            vehicle=_vehicle(),
            provenance=_operator(),
            valid_from=t2,
            retracts=(finding.id,),
            reason="window included a fuel cut",
        ),
        now=t2,
    )
    with pytest.raises(MemoryReferenceError, match="cannot be retracted"):
        ledger.append(
            Retraction(
                vehicle=_vehicle(),
                provenance=_operator(),
                valid_from=t3,
                retracts=(retraction.id,),
                reason="the retraction was wrong",
            ),
            now=t3,
        )
    restated = ledger.append(_evidence(), now=t4)
    assert restated.id != finding.id
    assert [e.record.id for e in ledger.query(kinds=("evidence",), as_of=t1)] == [finding.id]
    assert ledger.query(kinds=("evidence",), as_of=t3) == []
    assert [e.record.id for e in ledger.query(kinds=("evidence",))] == [restated.id]
    assert ledger.entry(finding.id).retracted_by == retraction.id
