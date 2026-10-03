"""P02.16: each row's target AFR is the map of the revision flashed when it was recorded.

``append_afr_channels`` used to apply the calibration's ``active_revision``
(ZS44A-20260920) to every session, so pre-flash sessions got the wrong map. The
revision is now resolved per row from the vehicle's flash history
(``tune_revisions.yaml``) at the row's own timestamp, by the rule the daemon
uses for the running revision (``TuneRevisionRegistry.at``). The k106 and
2026-09-20 maps differ in one cell, rpm 5,200 x BFS 18.20 ms (11.6148 ->
11.5436 AFR), which is where these tests look.
"""

from __future__ import annotations

import asyncio
import pickle
import shutil
from datetime import UTC, datetime, timedelta
from pathlib import Path

import numpy as np
import pandas as pd
import pytest

from src.collection.replay import ReplayEmissionStats, replay_session
from src.config import CanaryConfig, VehicleParameters
from src.decode.dids import load_repo_did_map
from src.paths import DATA_DIR
from src.preprocessing.channel_units import channel_units_for
from src.preprocessing.feature_pipeline import featurize_wide_frame
from src.preprocessing.live import LiveFeatureSource, build_prepared_live_tick
from src.signals.afr import (
    TARGET_AFR,
    TARGET_MAP_UNRECORDED,
    TUNE_REVISION_UNKNOWN,
    AfrCalibration,
    NoFlashHistory,
    RecordedClockFlashHistory,
    append_afr_channels,
    load_afr_calibration,
    target_afr_by_revision,
)
from src.storage.parquet_writer import write_records_to_parquet
from src.telemetry import TelemetryRecord
from src.vehicle.profiles import (
    active_flash_history,
    resolve_profile_for_tooling,
    scope_config,
)
from src.vehicle.tune_revisions import (
    TuneRevision,
    TuneRevisionRegistry,
    load_tune_revisions,
)
from tests.configs import EXAMPLE_YAML, example_config

XTERRA = DATA_DIR / "nissan-xterra-2008"
CALIBRATION = load_afr_calibration(XTERRA / "afr_calibration.yaml")
REGISTRY = load_tune_revisions(XTERRA / "tune_revisions.yaml")
REPO_DIDS = load_repo_did_map()
REPO_UNITS = channel_units_for(REPO_DIDS)
BFS = "derived:BASE_FUEL_SCHEDULE"
MS_PER_COUNT = 0.00049
#: The 2026-09-20 flash: the final post-program CRC response.
FLASH = datetime(2026, 9, 21, 3, 8, 45, 954035, tzinfo=UTC)
#: The one cell the 2026-09-20 flash changed, and its value on each side.
CELL_RPM, CELL_BFS = 5200.0, 18.20
K106_CELL, REV0920_CELL = 11.614815, 11.543558


def _registry() -> TuneRevisionRegistry:
    assert REGISTRY is not None
    return REGISTRY


def _cell_rows(start: datetime, rows: int, session: str) -> pd.DataFrame:
    return pd.DataFrame(
        {
            "session": session,
            "timestamp": pd.date_range(start, periods=rows, freq="s"),
            "obd:RPM": CELL_RPM,
            BFS: CELL_BFS,
        }
    )


def _straddling_frame() -> pd.DataFrame:
    """Two sessions, one each side of the 2026-09-20 flash, both on the cell."""
    before = _cell_rows(FLASH - timedelta(minutes=10), 5, "2026-09-21T025845")
    after = _cell_rows(FLASH + timedelta(minutes=10), 5, "2026-09-21T031845")
    return pd.concat([before, after], ignore_index=True)


def test_the_two_maps_differ_in_exactly_the_cell_these_tests_read() -> None:
    k106, rev0920 = CALIBRATION.target_map("ZS44A-k106"), CALIBRATION.target_map("ZS44A-20260920")
    assert k106 is not None and rev0920 is not None
    differ = np.argwhere(np.asarray(k106.afr) != np.asarray(rev0920.afr)).tolist()
    assert differ == [[7, 4]]
    assert (k106.rpm_breakpoints[7], k106.load_breakpoints[4]) == (CELL_RPM, CELL_BFS)
    assert (k106.afr[7][4], rev0920.afr[7][4]) == pytest.approx((K106_CELL, REV0920_CELL))


def test_a_frame_straddling_a_flash_gets_each_sides_own_map() -> None:
    frame = _straddling_frame()
    out = append_afr_channels(
        frame,
        VehicleParameters(),
        CALIBRATION,
        channel_units=REPO_UNITS,
        flash_history=_registry(),
    )
    by_session = out.groupby("session")[TARGET_AFR].unique()
    assert by_session["2026-09-21T025845"].tolist() == pytest.approx([K106_CELL])
    assert by_session["2026-09-21T031845"].tolist() == pytest.approx([REV0920_CELL])
    lookup = target_afr_by_revision(frame, CALIBRATION, _registry(), channel_units=REPO_UNITS)
    assert lookup.revisions_used == ("ZS44A-k106", "ZS44A-20260920")
    assert lookup.refusals == ()


def test_one_session_spanning_the_flash_is_split_at_the_flash_not_its_start() -> None:
    """The registry note says the session resolver assigns by session start;
    the target is per row, so a capture that recorded the flash changes map
    at the flash itself."""
    frame = _cell_rows(FLASH - timedelta(seconds=2), 5, "2026-09-21T030843")
    out = append_afr_channels(
        frame,
        VehicleParameters(),
        CALIBRATION,
        channel_units=REPO_UNITS,
        flash_history=_registry(),
    )
    assert out[TARGET_AFR].tolist() == pytest.approx([K106_CELL] * 2 + [REV0920_CELL] * 3)


def test_the_straddle_goes_through_the_production_featurizer() -> None:
    """End to end: ``featurize_wide_frame`` on the scoped Xterra config reads
    the vehicle's own flash history (``active_flash_history``), from the raw
    0x1208 counts through the DID view to the lookup."""
    config = CanaryConfig.from_yaml(EXAMPLE_YAML)
    scoped = scope_config(config, resolve_profile_for_tooling(config, None))
    frame = _straddling_frame().drop(columns=[BFS])
    frame["did:ECM_AIR_CHARGE"] = CELL_BFS / MS_PER_COUNT
    operating = featurize_wide_frame(
        frame, scoped, behavior_models=None, did_map=REPO_DIDS
    ).operating_frame
    values = operating.groupby("session")[TARGET_AFR].median()
    assert values["2026-09-21T025845"] == pytest.approx(K106_CELL, abs=1e-6)
    assert values["2026-09-21T031845"] == pytest.approx(REV0920_CELL, abs=1e-6)


def test_a_revision_without_a_map_is_refused_by_name_never_given_another() -> None:
    """Rows at a synthetic instant before the first UpRev flash resolve to
    ZS04C-stock, which has no recorded map: no target, named, rather than
    another revision's. (The instant is chosen for its revision; no claim is
    made about any real session.)"""
    stock_instant = datetime(2026, 7, 20, 18, 0, tzinfo=UTC)
    assert _registry().revisions_at(pd.Series([stock_instant])).tolist() == ["ZS04C-stock"]
    frame = pd.concat(
        [
            _cell_rows(stock_instant, 3, "2026-07-20T180000"),
            _cell_rows(FLASH + timedelta(days=1), 2, "2026-09-22T031000"),
        ],
        ignore_index=True,
    )
    lookup = target_afr_by_revision(frame, CALIBRATION, _registry(), channel_units=REPO_UNITS)
    assert lookup.values.iloc[:3].isna().all()
    assert lookup.values.iloc[3:].tolist() == pytest.approx([REV0920_CELL] * 2)
    (refusal,) = lookup.refusals
    assert (refusal.revision, refusal.rows) == ("ZS04C-stock", 3)
    assert refusal.reason.startswith(TARGET_MAP_UNRECORDED)
    assert "ZS04C-stock" in refusal.reason and "ZS44A-k106" in refusal.reason


def test_the_first_two_flashes_read_the_uprev_base_map_one_map_two_names() -> None:
    """ZS44A-osiris-initial and ZS44A-k105 carry the UpRev base map (only K
    was edited before k106, operator 2026-10-03), recorded once through a
    YAML anchor. On the cell the k106 flash changed most (row 7 col 3) the
    base reads 11.267, k106 11.760."""
    initial = CALIBRATION.target_map("ZS44A-osiris-initial")
    k105 = CALIBRATION.target_map("ZS44A-k105")
    k106 = CALIBRATION.target_map("ZS44A-k106")
    assert initial is not None and k105 is not None and k106 is not None
    assert k105.afr == initial.afr and k105.raw_values == initial.raw_values
    assert initial.provenance is not None and k105.provenance is not None
    assert initial.provenance.rom_sha256 == k105.provenance.rom_sha256
    assert (initial.afr[7][3], k106.afr[7][3]) == pytest.approx((11.267066, 11.76))
    instants = [
        datetime(2026, 7, 28, 12, tzinfo=UTC),  # osiris-initial
        datetime(2026, 8, 2, 12, tzinfo=UTC),  # k105
        datetime(2026, 8, 4, 12, tzinfo=UTC),  # k106
    ]
    frame = pd.concat(
        [_cell_rows(at, 1, f"s{i}") for i, at in enumerate(instants)], ignore_index=True
    ).assign(**{"obd:RPM": 5200.0, BFS: 16.85})
    lookup = target_afr_by_revision(frame, CALIBRATION, _registry(), channel_units=REPO_UNITS)
    assert lookup.revisions_used == ("ZS44A-osiris-initial", "ZS44A-k105", "ZS44A-k106")
    assert lookup.values.tolist() == pytest.approx([11.267066, 11.267066, 11.76])


def test_the_active_revision_is_never_a_fallback() -> None:
    """A calibration whose only map is its ``active_revision`` gives rows on
    another revision nothing."""
    only = CALIBRATION.model_copy(
        update={
            "active_revision": "ZS44A-20260920",
            "target_maps": [CALIBRATION.target_maps[1]],
        }
    )
    assert isinstance(only, AfrCalibration)
    frame = _straddling_frame()
    out = append_afr_channels(
        frame, VehicleParameters(), only, channel_units=REPO_UNITS, flash_history=_registry()
    )
    assert out[TARGET_AFR].iloc[:5].isna().all()
    assert out[TARGET_AFR].iloc[5:].tolist() == pytest.approx([REV0920_CELL] * 5)


def test_rows_with_no_revision_are_refused_and_say_which_kind_of_empty() -> None:
    frame = _straddling_frame()
    late_registry = TuneRevisionRegistry(
        revisions=[TuneRevision(revision="ZS44A-20260920", flashed_at=FLASH)]
    )
    lookup = target_afr_by_revision(frame, CALIBRATION, late_registry, channel_units=REPO_UNITS)
    (before_first_flash,) = lookup.refusals
    assert (before_first_flash.revision, before_first_flash.rows) == (None, 5)
    assert before_first_flash.reason.startswith(TUNE_REVISION_UNKNOWN)

    missing = NoFlashHistory("no tune_revisions.yaml is recorded for this vehicle (x)")
    lookup = target_afr_by_revision(frame, CALIBRATION, missing, channel_units=REPO_UNITS)
    assert lookup.values.isna().all() and lookup.revisions_used == ()
    (none,) = lookup.refusals
    assert none.rows == 10 and none.reason == f"{TUNE_REVISION_UNKNOWN}: {missing.reason}"
    out = append_afr_channels(
        frame, VehicleParameters(), CALIBRATION, channel_units=REPO_UNITS, flash_history=missing
    )
    assert TARGET_AFR not in out

    lookup = target_afr_by_revision(
        frame.drop(columns=["timestamp"]), CALIBRATION, _registry(), channel_units=REPO_UNITS
    )
    (unstamped,) = lookup.refusals
    assert "no 'timestamp' column" in unstamped.reason


def test_revisions_at_is_the_registry_rule_row_by_row() -> None:
    """The vectorised resolver is ``at`` per instant, the flash boundary and a
    missing stamp included. Naive stamps are UTC, as in ``at``."""
    registry = _registry()
    instants = [
        datetime(1999, 1, 1, tzinfo=UTC),
        datetime(2026, 8, 3, 16, 34, 40, tzinfo=UTC),
        datetime(2026, 8, 3, 18, 45, 21, tzinfo=UTC),
        FLASH - timedelta(microseconds=1),
        FLASH,
        datetime(2026, 10, 3, tzinfo=UTC),
    ]
    stamps = pd.Series([*instants, pd.NaT], index=range(10, 17))
    resolved = registry.revisions_at(stamps)
    expected = [None if (r := registry.at(t)) is None else r.revision for t in instants]
    assert resolved.tolist() == [*expected, None]
    assert list(resolved.index) == list(range(10, 17))
    assert expected[3:5] == ["ZS44A-k106", "ZS44A-20260920"]
    naive = pd.Series(pd.to_datetime(["2026-09-21T03:08:45.954035"]))
    assert registry.revisions_at(naive).tolist() == ["ZS44A-20260920"]


def _live_config(tmp_path: Path, flashed_at: datetime) -> CanaryConfig:
    """The example config reading a copy of the Xterra calibration, beside a
    flash history whose newer revision was flashed at ``flashed_at``."""
    profile = tmp_path / "profile"
    profile.mkdir()
    shutil.copyfile(XTERRA / "afr_calibration.yaml", profile / "afr_calibration.yaml")
    (profile / "tune_revisions.yaml").write_text(
        "schema_version: 1\nrevisions:\n"
        "  - {revision: ZS44A-k106, flashed_at: 2026-08-03T00:00:00Z}\n"
        f"  - {{revision: ZS44A-20260920, flashed_at: {flashed_at.isoformat()}}}\n"
    )
    config = example_config()
    daemon = config.daemon.model_copy(
        update={
            "afr_calibration": profile / "afr_calibration.yaml",
            "calibration_dir": tmp_path / "operational",
        }
    )
    return config.model_copy(update={"daemon": daemon})


@pytest.mark.parametrize(("offset", "expected"), [(-60, K106_CELL), (60, REV0920_CELL)])
def test_the_live_tick_uses_the_revision_flashed_now(
    tmp_path: Path, offset: int, expected: float
) -> None:
    """Live rows are stamped "now", so the tick's map is the revision flashed
    at that moment, through the same featurizer and flash history."""
    config = _live_config(tmp_path, FLASH)
    source = LiveFeatureSource(config=config, did_map=REPO_DIDS)
    now = FLASH + timedelta(seconds=offset)
    source.observe(TelemetryRecord.obd(now.timestamp(), "RPM", CELL_RPM, "rpm"))
    raw = round(CELL_BFS / MS_PER_COUNT)
    source.observe(
        TelemetryRecord(timestamp=now, source="obd", channel="DID_1208", value=f"{raw:04x}")
    )
    prepared = source.prepare_tick(now)
    assert prepared is not None
    point = build_prepared_live_tick(pickle.loads(pickle.dumps(prepared))).operating_point
    assert point[TARGET_AFR] == pytest.approx(expected, abs=1e-3)


def test_live_and_the_daemons_running_revision_agree(tmp_path: Path) -> None:
    """``/control/calibration``'s ``running_revision`` is ``registry.at(now)``;
    the featurizer's flash history resolves the same revision for a row
    stamped now."""
    from src.control.calibration import calibration_status

    config = _live_config(tmp_path, datetime.now(UTC) - timedelta(hours=1))
    now = pd.Series([pd.Timestamp.now(tz="UTC")])
    running = calibration_status(config)["running_revision"]
    assert running == "ZS44A-20260920"
    assert active_flash_history(config).revisions_at(now).tolist() == [running]


def test_an_edited_flash_history_is_reread(tmp_path: Path) -> None:
    config = _live_config(tmp_path, FLASH)
    stamp = pd.Series([FLASH + timedelta(seconds=1)])
    assert active_flash_history(config).revisions_at(stamp).tolist() == ["ZS44A-20260920"]
    registry = config.daemon.afr_calibration.parent / "tune_revisions.yaml"
    registry.write_text(
        "schema_version: 1\nrevisions:\n"
        "  - {revision: ZS44A-k106, flashed_at: 2026-08-03T00:00:00Z}\n"
    )
    assert active_flash_history(config).revisions_at(stamp).tolist() == ["ZS44A-k106"]
    registry.unlink()
    history = active_flash_history(config)
    assert isinstance(history, NoFlashHistory)
    assert "no tune_revisions.yaml" in history.reason


# ---------------------------------------------------------------------------
# Runtime replay: the map flashed when the source was recorded, not now
# ---------------------------------------------------------------------------


def _xterra_config(tmp_path: Path) -> CanaryConfig:
    """The example config reading copies of the Xterra calibration and flash
    history (example_config's own paths hold neither)."""
    profile = tmp_path / "xterra"
    profile.mkdir()
    for name in ("afr_calibration.yaml", "tune_revisions.yaml"):
        shutil.copyfile(XTERRA / name, profile / name)
    config = example_config()
    daemon = config.daemon.model_copy(
        update={
            "afr_calibration": profile / "afr_calibration.yaml",
            "calibration_dir": tmp_path / "operational",
        }
    )
    return config.model_copy(update={"daemon": daemon})


def _recorded_session(root: Path, start: datetime) -> Path:
    """Five seconds on the cell that differs between maps, recorded at ``start``."""
    raw = round(CELL_BFS / MS_PER_COUNT)
    records: list[TelemetryRecord] = []
    for k in range(5):
        at = start + timedelta(seconds=k)
        records.append(TelemetryRecord(timestamp=at, source="obd", channel="RPM", value=CELL_RPM))
        records.append(
            TelemetryRecord(timestamp=at, source="obd", channel="DID_1208", value=f"{raw:04x}")
        )
    session = root / start.strftime("%Y-%m-%dT%H%M%S")
    write_records_to_parquet(records, session)
    return session


def _replay_into(source: LiveFeatureSource, session: Path) -> datetime:
    """Replay ``session`` through the real emitter into ``source``; return
    the newest emitted (wall-clock) stamp."""

    async def run() -> list[TelemetryRecord]:
        queue: asyncio.Queue[TelemetryRecord] = asyncio.Queue()
        stop = asyncio.Event()
        task = asyncio.create_task(
            replay_session(
                queue,
                session,
                stop,
                speed=10_000.0,
                loop=False,
                stats=ReplayEmissionStats(),
                broadcast_stale_timeout_seconds=10.0,
            )
        )
        got: list[TelemetryRecord] = []
        while len(got) < 10:
            got.append(await asyncio.wait_for(queue.get(), timeout=5.0))
        stop.set()
        await task
        return got

    records = asyncio.run(run())
    for record in records:
        source.observe(record)
    return max(record.timestamp for record in records)


def test_replaying_a_k106_session_gets_the_k106_map_not_todays(tmp_path: Path) -> None:
    """Replay rebases every row to wall-clock. Resolved at the emit stamp, a
    k106 session would get ZS44A-20260920 (flashed now); the live source reads
    the recorded instant off each replayed record instead."""
    config = _xterra_config(tmp_path)
    session = _recorded_session(tmp_path / "sessions", datetime(2026, 8, 4, 16, 44, tzinfo=UTC))
    source = LiveFeatureSource(config=config, did_map=REPO_DIDS)
    now = _replay_into(source, session)
    assert now > datetime(2026, 10, 1, tzinfo=UTC), "replay stamps rows at emit time"
    prepared = source.prepare_tick(now)
    assert prepared is not None
    assert prepared.flash_history.revisions_at(prepared.wide["timestamp"]).tolist() == [
        "ZS44A-k106"
    ]
    assert _registry().revisions_at(prepared.wide["timestamp"]).tolist() == ["ZS44A-20260920"]
    point = build_prepared_live_tick(pickle.loads(pickle.dumps(prepared))).operating_point
    assert point[TARGET_AFR] == pytest.approx(K106_CELL, abs=1e-3)

    # The replay boundary resets the source: live rows read the flash of now.
    source.reset()
    source.observe(TelemetryRecord.obd(now.timestamp(), "RPM", CELL_RPM, "rpm"))
    live = source.prepare_tick(now)
    assert live is not None
    assert live.flash_history.revisions_at(live.wide["timestamp"]).tolist() == ["ZS44A-20260920"]


def test_replaying_a_pre_flash_session_is_refused_by_name(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    """A session recorded on the factory calibration (ZS04C-stock, no map)
    replays with no target and the target_map_unrecorded refusal, not today's
    map."""
    from src.signals import afr

    monkeypatch.setattr(afr, "_logged_refusals", set())
    config = _xterra_config(tmp_path)
    session = _recorded_session(tmp_path / "sessions", datetime(2026, 7, 20, 18, 0, tzinfo=UTC))
    source = LiveFeatureSource(config=config, did_map=REPO_DIDS)
    now = _replay_into(source, session)
    prepared = source.prepare_tick(now)
    assert prepared is not None
    assert prepared.flash_history.revisions_at(prepared.wide["timestamp"]).tolist() == [
        "ZS04C-stock"
    ]
    with caplog.at_level("WARNING", logger="src.signals.afr"):
        point = build_prepared_live_tick(pickle.loads(pickle.dumps(prepared))).operating_point
    assert TARGET_AFR not in point
    assert point["derived:BASE_FUEL_SCHEDULE"] == pytest.approx(CELL_BFS, abs=1e-3)
    refused = [r.getMessage() for r in caplog.records if TARGET_MAP_UNRECORDED in r.getMessage()]
    assert refused and "ZS04C-stock" in refused[0]


def test_the_recorded_clock_maps_each_row_through_its_own_shift() -> None:
    """Rows stamped on the emit clock map back by the shift of the newest row
    stamped at or before them (the first row's for an earlier stamp); a
    missing stamp stays unknown."""
    emit = pd.Timestamp("2026-10-03T12:00:00Z")
    shift_k106 = (emit - pd.Timestamp("2026-08-04T12:00:00Z")).total_seconds()
    shift_stock = (emit - pd.Timestamp("2026-07-20T12:00:00Z")).total_seconds()
    history = RecordedClockFlashHistory(
        base=_registry(),
        stamped_ns=(emit.value, emit.value + 10**9),
        shift_s=(shift_k106, shift_stock + 1.0),
    )
    stamps = pd.Series(
        [emit - pd.Timedelta(seconds=5), emit, emit + pd.Timedelta(seconds=1), pd.NaT]
    )
    assert history.revisions_at(stamps).tolist() == [
        "ZS44A-k106",
        "ZS44A-k106",
        "ZS04C-stock",
        None,
    ]
    with pytest.raises(ValueError, match="equal-length"):
        RecordedClockFlashHistory(base=_registry(), stamped_ns=(1,), shift_s=())
