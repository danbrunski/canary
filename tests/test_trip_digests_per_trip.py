"""Every trip end in a session is answered, not only the first (D03.63).

``_capture-ltft-gate-2026-11-08T201601`` held two trips. The first ended at
21:02:28Z and got its ``digest.json``; the second left nothing, because
``handle_trip_end_off_loop`` returned as soon as the *session* had a digest and
wrote no refusal. A whole-drive capture makes several trips in one directory
normal. Each trip end now produces a digest filed under that trip, or a
``digest_skipped`` record naming why, and ``digest.json`` /
``digest_skipped.json`` stay the session's newest answer.
"""

from __future__ import annotations

import asyncio
import json
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import Any

import pytest

from src.analysis.digest import (
    REASON_NO_SPEED_SOURCE,
    REASON_OVER_SPAN_BUDGET,
    write_digest_skipped,
)
from src.analysis.gps_tracker import TripEnd
from src.config import CanaryConfig
from src.control import trip_end
from src.control.trip_end import handle_trip_end_off_loop, record_trip_end_unobservable
from src.storage.session_products import (
    DIGEST_FILENAME,
    DIGEST_SKIPPED_FILENAME,
    SESSION_TRIP_KEY,
    TRIP_DIGESTS_DIRNAME,
    list_trip_records,
    read_digest,
    read_digest_skipped,
    read_trip_digest,
    read_trip_digest_skipped,
    trip_index,
    trip_key,
)
from tests.builders import RICH_SESSION_END, digest_config, write_rich_session
from tests.test_trip_end_digest import _budgeted, _RefusingExecutor

# Two trips, an hour apart (record-time epochs).
FIRST_START, FIRST_STOP = 1_779_400_000.0, 1_779_400_300.0
SECOND_START, SECOND_STOP = 1_779_403_600.0, 1_779_403_900.0


def _trip(session: Path, start: float, stop: float, *, observed: bool = True) -> TripEnd:
    return TripEnd(
        observed=observed,
        speed_source="obd:SPEED",
        detail="stationary for 300 s on obd:SPEED",
        session=session,
        sessions=(session,),
        started_epoch=start,
        stopped_epoch=stop if observed else None,
    )


def _session(tmp_path: Path) -> Path:
    return write_rich_session(tmp_path, name="2026-05-20T180000")


def _files(session: Path) -> list[str]:
    directory = session / TRIP_DIGESTS_DIRNAME
    return sorted(p.name for p in directory.glob("*.json")) if directory.is_dir() else []


async def test_two_trip_ends_in_one_session_both_leave_a_digest(tmp_path: Path) -> None:
    session = _session(tmp_path)
    config = digest_config(tmp_path)
    first, second = (
        _trip(session, FIRST_START, FIRST_STOP),
        _trip(session, SECOND_START, SECOND_STOP),
    )

    one = await handle_trip_end_off_loop(session, config, trip=first, now=RICH_SESSION_END)
    two = await handle_trip_end_off_loop(session, config, trip=second, now=RICH_SESSION_END)

    assert one is not None
    assert two is not None, "the second trip end was answered with nothing (D03.63)"
    key_one, key_two = trip_key(one["trip"]), trip_key(two["trip"])
    assert key_one != key_two
    assert _files(session) == sorted([f"{key_one}.json", f"{key_two}.json"])
    # Each trip's own digest is on disk, naming its own trip.
    assert read_trip_digest(session, key_one)["trip"]["started_at"] == one["trip"]["started_at"]  # type: ignore[index]
    assert read_trip_digest(session, key_two)["trip"]["stopped_at"] == two["trip"]["stopped_at"]  # type: ignore[index]
    # The session's own digest is the newest trip's, and nothing is refused.
    top = read_digest(session)
    assert top is not None and top["trip"] == two["trip"]
    assert read_digest_skipped(session) is None
    records = list_trip_records(session)
    assert [r["trip_key"] for r in records] == sorted([key_one, key_two])
    assert {r["status"] for r in records} == {"digest"}


async def test_a_trip_end_that_fires_again_is_not_recomputed(tmp_path: Path) -> None:
    """The shutdown fallback naming the trip the edge already digested."""
    session = _session(tmp_path)
    config = digest_config(tmp_path)
    trip = _trip(session, FIRST_START, FIRST_STOP)
    assert await handle_trip_end_off_loop(session, config, trip=trip, now=RICH_SESSION_END)
    again = await handle_trip_end_off_loop(
        session, config, trip=trip, now=RICH_SESSION_END, executor=_RefusingExecutor()
    )
    assert again is None
    assert len(_files(session)) == 1


async def test_a_call_naming_no_trip_does_not_recompute_a_session_that_has_a_digest(
    tmp_path: Path,
) -> None:
    """The route's on-demand read and a fallback with no open trip: the second
    trip's refusal must not make them recompute the first trip's digest."""
    session = _session(tmp_path)
    config = digest_config(tmp_path)
    assert await handle_trip_end_off_loop(
        session, config, trip=_trip(session, FIRST_START, FIRST_STOP), now=RICH_SESSION_END
    )
    tight = _budgeted(config, digest_span_budget_seconds=10)
    assert (
        await handle_trip_end_off_loop(
            session, tight, trip=_trip(session, SECOND_START, SECOND_STOP), now=RICH_SESSION_END
        )
        is None
    )
    assert read_digest(session) is None, "the newest trip was refused"
    assert (
        await handle_trip_end_off_loop(
            session, config, now=RICH_SESSION_END, executor=_RefusingExecutor()
        )
        is None
    )


async def test_a_refused_second_trip_leaves_a_record_naming_why(tmp_path: Path) -> None:
    session = _session(tmp_path)
    config = digest_config(tmp_path)
    first = await handle_trip_end_off_loop(
        session, config, trip=_trip(session, FIRST_START, FIRST_STOP), now=RICH_SESSION_END
    )
    assert first is not None
    tight = _budgeted(config, digest_span_budget_seconds=10)
    second_trip = _trip(session, SECOND_START, SECOND_STOP)
    assert (
        await handle_trip_end_off_loop(session, tight, trip=second_trip, now=RICH_SESSION_END)
        is None
    )

    key_two = trip_key(trip_end.trip_block(second_trip, session))
    refusal = read_trip_digest_skipped(session, key_two)
    assert refusal is not None
    assert refusal["reason"] == REASON_OVER_SPAN_BUDGET
    assert refusal["trip"]["started_at"] == trip_end.trip_block(second_trip, session)["started_at"]
    # Trip one's digest is still its answer, and the top level reports the newest trip.
    assert read_trip_digest(session, trip_key(first["trip"])) is not None
    assert read_digest_skipped(session) == refusal
    assert read_digest(session) is None
    by_status = {r["trip_key"]: r["status"] for r in list_trip_records(session)}
    assert by_status == {trip_key(first["trip"]): "digest", key_two: "skipped"}
    index = trip_index(session)
    assert [line["status"] for line in index] == [by_status[line["trip_key"]] for line in index]
    refused = next(line for line in index if line["status"] == "skipped")
    assert refused["reason"] == REASON_OVER_SPAN_BUDGET


async def test_a_second_trip_ending_while_the_first_digest_runs_is_not_dropped(
    tmp_path: Path,
) -> None:
    """``_IN_FLIGHT`` was keyed by session: trip two awaited trip one's child
    and returned, recording nothing."""
    session = _session(tmp_path)
    config = digest_config(tmp_path)
    with ThreadPoolExecutor(max_workers=2) as pool:
        one, two = await asyncio.gather(
            handle_trip_end_off_loop(
                session,
                config,
                trip=_trip(session, FIRST_START, FIRST_STOP),
                now=RICH_SESSION_END,
                executor=pool,
            ),
            handle_trip_end_off_loop(
                session,
                config,
                trip=_trip(session, SECOND_START, SECOND_STOP),
                now=RICH_SESSION_END,
                executor=pool,
            ),
        )
    assert one is not None and two is not None
    assert len(_files(session)) == 2


async def test_a_session_digested_before_per_trip_records_keeps_its_first_trip(
    tmp_path: Path,
) -> None:
    """Old sessions hold one ``digest.json`` and nothing else. Writing the
    second trip's must not overwrite the only copy of the first's."""
    session = _session(tmp_path)
    config = digest_config(tmp_path)
    first_trip = _trip(session, FIRST_START, FIRST_STOP)
    first = await handle_trip_end_off_loop(session, config, trip=first_trip, now=RICH_SESSION_END)
    assert first is not None
    for leftover in (session / TRIP_DIGESTS_DIRNAME).glob("*"):
        leftover.unlink()
    (session / TRIP_DIGESTS_DIRNAME).rmdir()
    assert list_trip_records(session)[0]["status"] == "digest", "legacy layout is still listed"
    assert read_trip_digest(session, trip_key(first["trip"])) == first

    second = await handle_trip_end_off_loop(
        session, config, trip=_trip(session, SECOND_START, SECOND_STOP), now=RICH_SESSION_END
    )
    assert second is not None
    assert read_trip_digest(session, trip_key(first["trip"])) == first
    assert len(list_trip_records(session)) == 2


async def test_a_legacy_session_without_a_trip_block_is_the_session_key(tmp_path: Path) -> None:
    session = _session(tmp_path)
    config = digest_config(tmp_path)
    assert await handle_trip_end_off_loop(session, config, now=RICH_SESSION_END) is not None
    (session / DIGEST_FILENAME).write_text(
        json.dumps({**json.loads((session / DIGEST_FILENAME).read_text()), "trip": None})
    )
    for leftover in (session / TRIP_DIGESTS_DIRNAME).glob("*"):
        leftover.unlink()
    assert [r["trip_key"] for r in list_trip_records(session)] == [SESSION_TRIP_KEY]
    assert read_trip_digest(session, SESSION_TRIP_KEY) is not None
    # A trip arriving later is not mistaken for the answered session.
    assert (
        await handle_trip_end_off_loop(
            session, config, trip=_trip(session, SECOND_START, SECOND_STOP), now=RICH_SESSION_END
        )
        is not None
    )
    assert len(list_trip_records(session)) == 2


def test_a_second_unobservable_trip_is_recorded_beside_the_first_trips_answer(
    tmp_path: Path,
) -> None:
    session = _session(tmp_path)
    config: CanaryConfig = digest_config(tmp_path)
    first = _trip(session, FIRST_START, FIRST_STOP, observed=False)
    second = _trip(session, SECOND_START, SECOND_STOP, observed=False)
    one = record_trip_end_unobservable(session, config, "speed lost (1)", trip=first)
    assert one is not None
    # The same trip again changes nothing; the other trip is its own record.
    assert record_trip_end_unobservable(session, config, "again", trip=first) is None
    two = record_trip_end_unobservable(session, config, "speed lost (2)", trip=second)
    assert two is not None and two.reason == REASON_NO_SPEED_SOURCE
    assert len(_files(session)) == 2
    assert read_digest_skipped(session)["detail"] == "speed lost (2)"  # type: ignore[index]


def test_a_trip_less_refusal_is_filed_under_the_session_key(tmp_path: Path) -> None:
    session = _session(tmp_path)
    config = digest_config(tmp_path)
    assert record_trip_end_unobservable(session, config, "blind") is not None
    assert (session / TRIP_DIGESTS_DIRNAME / f"{SESSION_TRIP_KEY}.skipped.json").exists()
    assert (session / DIGEST_SKIPPED_FILENAME).exists()
    # No trip named: the session is the unit, and an answer on disk stands.
    assert record_trip_end_unobservable(session, config, "blind again") is None


def test_writing_a_refusal_never_leaves_the_top_level_claiming_a_digest(tmp_path: Path) -> None:
    """The top level is one trip's answer; a computed one and a refused one are
    exclusive there even when they belong to different trips."""
    session = _session(tmp_path)
    config = digest_config(tmp_path)
    blocks: dict[str, Any] = {"started_at": "2026-05-20T18:00:00Z", "stopped_at": None}
    (session / DIGEST_FILENAME).write_text(json.dumps({"session_id": session.name, "trip": blocks}))
    other = {"started_at": "2026-05-20T19:00:00Z", "stopped_at": None}
    from dataclasses import replace

    skipped = record_trip_end_unobservable(session, config, "x", trip=_trip(session, 1.0, 2.0))
    assert skipped is not None
    write_digest_skipped(session, replace(skipped, trip=other))
    assert read_digest(session) is None
    assert read_trip_digest(session, trip_key(blocks)) == {
        "session_id": session.name,
        "trip": blocks,
    }


@pytest.mark.parametrize("trip", [None, {}, {"started_at": None}, {"started_at": 5}])
def test_a_record_naming_no_start_is_the_session_key(trip: Any) -> None:
    assert trip_key(trip) == SESSION_TRIP_KEY


async def test_the_daemons_unobservable_hook_files_the_refusal_under_its_trip(
    tmp_path: Path,
) -> None:
    from src.daemon import runtime

    session = _session(tmp_path)
    config = digest_config(tmp_path)
    for start, stop in ((FIRST_START, FIRST_STOP), (SECOND_START, SECOND_STOP)):
        runtime._run_trip_unobservable_hook(
            session, "speed lost", config, _trip(session, start, stop, observed=False)
        )
    await asyncio.gather(*runtime._TRIP_END_TASKS)
    assert len(_files(session)) == 2


# --- the top level follows trip order, not write order -------------------------


def _block(started: str) -> dict[str, Any]:
    return {"started_at": started, "stopped_at": None, "end_session": "s", "sessions": ["s"]}


def _stub_digest(session: Path, trip: dict[str, Any] | None) -> Any:
    from src.analysis.digest import SessionDigest

    return SessionDigest(
        schema_version=2,
        computed_at="2026-05-20T20:00:00Z",
        session_id=session.name,
        duration_seconds=1.0,
        mode_seconds={},
        anomaly_counts_by_source={},
        top_behavior_deviations=[],
        fingerprint_top_movers=[],
        new_dtcs=[],
        new_ledger_events=[],
        trip=trip,
    )


def _stub_skip(session: Path, trip: dict[str, Any] | None) -> Any:
    from src.analysis.digest import SKIP_SCHEMA_VERSION, DigestSkipped

    return DigestSkipped(
        schema_version=SKIP_SCHEMA_VERSION,
        session_id=session.name,
        reason=REASON_OVER_SPAN_BUDGET,
        detail="d",
        recorded_at="now",
        row_count=None,
        row_budget=None,
        trip=trip,
    )


OLDER, NEWER = _block("2026-05-20T18:00:00Z"), _block("2026-05-20T19:00:00Z")


def test_an_older_trips_digest_landing_late_does_not_displace_a_newer_refusal(
    tmp_path: Path,
) -> None:
    """The probe: trip two refused, then trip one's digest finishes (two
    children can run at once). The top level still says trip two was refused."""
    from src.analysis.digest import clear_digest_skipped, persist_digest

    session = _session(tmp_path)
    write_digest_skipped(session, _stub_skip(session, NEWER))
    persist_digest(session, _stub_digest(session, OLDER))
    clear_digest_skipped(session, trip_key(OLDER))  # what persist_digest used to do

    assert read_digest(session) is None
    refusal = read_digest_skipped(session)
    assert refusal is not None and refusal["trip"] == NEWER
    assert read_trip_digest(session, trip_key(OLDER)) is not None
    assert read_trip_digest_skipped(session, trip_key(NEWER)) is not None
    assert {r["status"] for r in list_trip_records(session)} == {"digest", "skipped"}


def test_an_older_trips_refusal_landing_late_does_not_displace_a_newer_digest(
    tmp_path: Path,
) -> None:
    from src.analysis.digest import persist_digest

    session = _session(tmp_path)
    persist_digest(session, _stub_digest(session, NEWER))
    write_digest_skipped(session, _stub_skip(session, OLDER))

    top = read_digest(session)
    assert top is not None and top["trip"] == NEWER
    assert read_digest_skipped(session) is None, "the top level must not claim both"
    older = read_trip_digest_skipped(session, trip_key(OLDER))
    assert older is not None and older["trip"] == OLDER


def test_the_newer_trip_wins_whichever_order_the_two_are_written(tmp_path: Path) -> None:
    from src.analysis.digest import persist_digest

    for first, second in ((OLDER, NEWER), (NEWER, OLDER)):
        session = write_rich_session(
            tmp_path, name=f"2026-05-2{'0' if first is OLDER else '1'}T180000"
        )
        persist_digest(session, _stub_digest(session, first))
        persist_digest(session, _stub_digest(session, second))
        top = read_digest(session)
        assert top is not None and top["trip"] == NEWER


def test_a_tripless_record_never_displaces_a_real_trips_answer(tmp_path: Path) -> None:
    from src.analysis.digest import persist_digest

    session = _session(tmp_path)
    persist_digest(session, _stub_digest(session, OLDER))
    write_digest_skipped(session, _stub_skip(session, None))
    persist_digest(session, _stub_digest(session, None))
    top = read_digest(session)
    assert top is not None and top["trip"] == OLDER
    assert read_digest_skipped(session) is None
    # ...but a real trip replaces a trip-less answer, and a computed whole-session
    # digest replaces a real trip's refusal (raise the budget, ask again).
    other = write_rich_session(tmp_path, name="2026-05-22T180000")
    write_digest_skipped(other, _stub_skip(other, OLDER))
    persist_digest(other, _stub_digest(other, None))
    assert read_digest(other) is not None and read_digest_skipped(other) is None
    third = write_rich_session(tmp_path, name="2026-05-23T180000")
    persist_digest(third, _stub_digest(third, None))
    persist_digest(third, _stub_digest(third, OLDER))
    held = read_digest(third)
    assert held is not None and held["trip"] == OLDER


def test_trips_are_ordered_by_parsed_start_not_by_key_or_spelling(tmp_path: Path) -> None:
    from src.analysis.digest import persist_digest

    session = _session(tmp_path)
    # 20:00+02:00 is 18:00Z: older than 19:00Z, though it sorts after it as text.
    older = _block("2026-05-20T20:00:00+02:00")
    persist_digest(session, _stub_digest(session, NEWER))
    persist_digest(session, _stub_digest(session, older))
    top = read_digest(session)
    assert top is not None and top["trip"] == NEWER
    assert [r["trip"]["started_at"] for r in list_trip_records(session)] == [
        older["started_at"],
        NEWER["started_at"],
    ]
    assert trip_key(_block("2026-05-20T18:00:00Z")) == trip_key(_block("2026-05-20T18:00:00+00:00"))


def test_concurrent_writers_never_leave_the_top_level_claiming_both(tmp_path: Path) -> None:
    """The unobservable refusal writes from a thread while digests land on the
    loop: one lock, so the directory never ends up with both answers."""
    import threading

    from src.analysis.digest import persist_digest

    session = _session(tmp_path)
    starts = [f"2026-05-20T{hour:02d}:00:00Z" for hour in range(10, 22)]

    def writer(index: int, started: str) -> None:
        block = _block(started)
        if index % 2:
            write_digest_skipped(session, _stub_skip(session, block))
        else:
            persist_digest(session, _stub_digest(session, block))

    threads = [threading.Thread(target=writer, args=(i, s)) for i, s in enumerate(starts)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()
    assert not (
        (session / DIGEST_FILENAME).exists() and (session / DIGEST_SKIPPED_FILENAME).exists()
    )
    newest = (read_digest(session) or read_digest_skipped(session) or {})["trip"]
    assert newest["started_at"] == starts[-1]
    assert len(list_trip_records(session)) == len(starts)


async def test_a_refusal_that_removes_the_top_level_digest_reindexes_the_catalog(
    tmp_path: Path,
) -> None:
    from src.storage.catalog import SessionCatalog

    session = _session(tmp_path)
    config = digest_config(tmp_path)
    db = tmp_path / "catalog.db"
    assert await handle_trip_end_off_loop(
        session,
        config,
        trip=_trip(session, FIRST_START, FIRST_STOP),
        now=RICH_SESSION_END,
        catalog_db=db,
    )
    with SessionCatalog(db) as catalog:
        row = catalog.get_session(session.name)
    assert row is not None and row.digest is not None
    tight = _budgeted(config, digest_span_budget_seconds=10)
    await handle_trip_end_off_loop(
        session,
        tight,
        trip=_trip(session, SECOND_START, SECOND_STOP),
        now=RICH_SESSION_END,
        catalog_db=db,
    )
    with SessionCatalog(db) as catalog:
        row = catalog.get_session(session.name)
    assert row is not None
    assert row.digest is None
    assert row.digest_skipped is not None
    assert row.digest_skipped["reason"] == REASON_OVER_SPAN_BUDGET
