"""One physical-drive definition for every holdout, floor and count.

The 2026-09-23 verifier found drive grouping in three places, only one of
which saw the captures: the retrained LSTM's validation drives shared
physical drives with training, and the retrained IF's idle_transient "4
training drives" were 2. These tests pin the shared definition
(``src.preprocessing.drives``) through each caller that counts or splits drives.
"""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path
from types import SimpleNamespace

import pandas as pd
import pytest

from src.analysis.behavior_validation import group_drives, piece_time_ranges
from src.config import CanaryConfig
from src.features.feature_pipeline import build_feature_frame
from src.models.evaluation import drives_of_sessions, holdout_by_drive
from src.models.evaluation_heldout import record_seen
from src.models.isolation_forest import _drive_count
from src.models.sequence_training import choose_validation_sessions
from src.preprocessing.drives import DRIVE_COLUMN, count_drives, physical_drives, piece_units
from src.storage.parquet_writer import write_records_to_parquet
from src.telemetry import TelemetryRecord
from tests.configs import example_config

REPO_ROOT = Path(__file__).resolve().parents[1]


def _epoch(stamp: str) -> float:
    return datetime.fromisoformat(stamp).replace(tzinfo=UTC).timestamp()


def _record(
    root: Path,
    name: str,
    start: str,
    minutes: float,
    *,
    parked: tuple[float, float] | None = None,
) -> Path:
    """A recording polling OBD every second for ``minutes``. Inside ``parked``
    (minutes from start) only GPS arrives, every 30 s: the Pi logging a
    key-off truck, as ``2026-09-23T032755`` did all night."""
    t0 = _epoch(start)
    records: list[TelemetryRecord] = []
    for i in range(int(minutes * 60)):
        minute = i / 60.0
        t = t0 + i
        if parked is not None and parked[0] < minute < parked[1]:
            if i % 30 == 0:
                records.append(TelemetryRecord.gps(t, "speed", 0.0, "km/h"))
            continue
        records.extend(
            [
                TelemetryRecord.obd(t, "RPM", 780.0 + (i % 4), "rpm"),
                TelemetryRecord.obd(t, "SPEED", float(i % 5), "km/h"),
                TelemetryRecord.obd(t, "THROTTLE_POS", 6.0 + (i % 3), "%"),
            ]
        )
    write_records_to_parquet(records, root / name, max_rows=100_000)
    return root / name


@pytest.fixture
def config() -> CanaryConfig:
    return example_config(REPO_ROOT / "canary.example.yaml")


@pytest.fixture
def corpus(tmp_path: Path) -> SimpleNamespace:
    """Drive 1: two live sessions either side of a 20-minute capture (the
    2026-09-21 evening shape). Drive 2: next day. Drive 3/4: one recording,
    active, parked 2 h with GPS still arriving, active again."""
    root = tmp_path / "sessions"
    return SimpleNamespace(
        before=_record(root, "2026-09-21T194247", "2026-09-21T19:42:47", 8),
        capture=_record(
            root, "_capture-fuel-target-research-2026-09-21T195100", "2026-09-21T19:51:00", 20
        ),
        after=_record(root, "2026-09-21T201200", "2026-09-21T20:12:00", 8),
        other=_record(root, "2026-09-22T163415", "2026-09-22T16:34:15", 8),
        parked=_record(root, "2026-09-23T003917", "2026-09-23T00:39:17", 136, parked=(8, 128)),
    )


def test_a_pause_in_vehicle_activity_splits_one_recording(corpus: SimpleNamespace) -> None:
    """The operator's decision (2026-09-23): a recording with a long stretch of
    no vehicle activity is two drives. GPS kept arriving, so this is an
    activity gap, not a data gap."""
    drives = physical_drives([corpus.parked])
    assert len(drives) == 2
    assert all(d.sessions == (corpus.parked.name,) for d in drives)
    # A directory cannot be split across a holdout: one unit, two drives.
    units = drives_of_sessions([corpus.parked])
    assert [u.session_names for u in units] == [(corpus.parked.name,)]


def test_holdout_units_follow_physical_drives(corpus: SimpleNamespace) -> None:
    pool = [corpus.before, corpus.after, corpus.other]
    assert [d.session_names for d in drives_of_sessions(pool)] == [
        (corpus.before.name, corpus.after.name),
        (corpus.other.name,),
    ]
    held = holdout_by_drive(pool, evaluation=[corpus.after])
    assert held.evaluation == (corpus.before, corpus.after)
    assert held.training == (corpus.other,)


def test_frames_carry_physical_drives_and_every_count_reads_them(
    corpus: SimpleNamespace, config: CanaryConfig
) -> None:
    frame = build_feature_frame(
        [corpus.before, corpus.after, corpus.other, corpus.parked], config, max_workers=1
    )
    assert DRIVE_COLUMN in frame.columns
    by_session = frame.groupby("session")[DRIVE_COLUMN].agg(lambda s: set(s.dropna()))
    assert by_session[corpus.before.name] == by_session[corpus.after.name]
    assert len(by_session[corpus.parked.name]) == 2

    # The IF's per-mode floor counts physical drives: the capture makes
    # before + after one drive, the parked recording is two.
    def rows(*names: str) -> pd.DataFrame:
        return frame[frame["session"].isin(names)]

    assert (
        _drive_count(
            rows(corpus.before.name, corpus.after.name),
            timestamp_column="timestamp",
            session_column="session",
        )
        == 1
    )
    assert (
        _drive_count(
            rows(corpus.parked.name), timestamp_column="timestamp", session_column="session"
        )
        == 2
    )
    assert count_drives(frame) == 4

    # The frame alone (no directories) would have split before/after: the
    # capture is not in it. The stamped label is what keeps them together.
    unstamped = frame.drop(columns=[DRIVE_COLUMN])
    assert (
        count_drives(rows(corpus.before.name, corpus.after.name).drop(columns=[DRIVE_COLUMN])) == 2
    )
    # ... and its parked GPS rows would have kept the pause inside one drive.
    assert count_drives(unstamped) == 4

    units = piece_units(frame)
    assert (corpus.before.name, corpus.after.name) in units


def test_validation_never_shares_a_physical_drive_with_training(
    corpus: SimpleNamespace, config: CanaryConfig
) -> None:
    frame = build_feature_frame(
        [corpus.before, corpus.after, corpus.other, corpus.parked], config, max_workers=1
    )
    pair = {corpus.before.name, corpus.after.name}
    for seed in range(8):
        choice = choose_validation_sessions(
            frame, sequence_length=6, validation_fraction=0.3, random_state=seed
        )
        held = set(choice.sessions)
        assert not held or not (pair & held) or pair <= held, (seed, sorted(held))
        for drive in choice.record["drives"]:
            names = set(drive)
            assert not (names & pair) or pair <= names


def test_behavior_validation_groups_by_the_same_physical_drives(
    corpus: SimpleNamespace, config: CanaryConfig
) -> None:
    frame = build_feature_frame(
        [corpus.before, corpus.after, corpus.other, corpus.parked], config, max_workers=1
    )
    drives = group_drives(piece_time_ranges(frame), join_gap_seconds=600.0)
    pieces = [set(d.pieces) for d in drives]
    assert {corpus.before.name, corpus.after.name} in pieces
    assert {corpus.other.name} in pieces
    # The parked recording is one piece, so it is one fold unit even though it
    # holds two drives; the evaluator's exposure breaks at the pause.
    assert {corpus.parked.name} in pieces
    assert len(drives) == 3


def test_seen_sessions_are_recorded_by_directory() -> None:
    """Manifests name ``#segment`` pieces; the harness skips drives by
    directory, so a drive the detector saw part of must still be skipped."""
    manifest = SimpleNamespace(
        training_sessions=["2026-09-21T143331#segment-001", "2026-09-21T142126"],
        calibration_sessions=["2026-09-21T225511#segment-002"],
    )
    seen: dict[str, dict[str, str]] = {}
    record_seen(seen, "sequence", manifest)
    assert seen == {
        "2026-09-21T143331": {"sequence": "training"},
        "2026-09-21T142126": {"sequence": "training"},
        "2026-09-21T225511": {"sequence": "calibration"},
    }
