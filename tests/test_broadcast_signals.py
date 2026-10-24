"""Tests for the byte-vs-tag differential analyzer."""

from __future__ import annotations

from pathlib import Path

from src.session import TagWindow
from tools.broadcast_signals import (
    ByteStats,
    collect_byte_stats,
    rank_differentials,
)


def _write_log(path: Path, lines: list[str]) -> None:
    path.write_text("\n".join(lines) + "\n")


# ---------------------------------------------------------------------------
# collect_byte_stats
# ---------------------------------------------------------------------------


def test_collect_byte_stats_groups_by_window(tmp_path: Path) -> None:
    """Frames should be assigned to the window covering their timestamp."""
    log = tmp_path / "frames.log"
    _write_log(
        log,
        [
            "(1.0) vcan0 180#0A00",
            "(1.5) vcan0 180#0B00",
            "(5.0) vcan0 180#5000",  # in second window
            "(5.5) vcan0 180#5200",
        ],
    )
    windows = [
        TagWindow(label="idle", start=0.5, end=2.0),
        TagWindow(label="revved", start=4.0, end=6.0),
    ]
    stats = collect_byte_stats(log, windows)
    byte0 = stats[(0x180, 0)]
    assert byte0["idle"].mean == (0x0A + 0x0B) / 2
    assert byte0["revved"].mean == (0x50 + 0x52) / 2


def test_collect_skips_frames_in_gaps(tmp_path: Path) -> None:
    """Frames between windows should be ignored."""
    log = tmp_path / "frames.log"
    _write_log(
        log,
        [
            "(1.0) vcan0 180#01",
            "(2.5) vcan0 180#99",  # gap between windows
            "(5.0) vcan0 180#05",
        ],
    )
    windows = [
        TagWindow(label="a", start=0.5, end=2.0),
        TagWindow(label="b", start=4.0, end=6.0),
    ]
    stats = collect_byte_stats(log, windows)
    assert stats[(0x180, 0)]["a"].n == 1
    assert stats[(0x180, 0)]["b"].n == 1
    # The gap frame shouldn't appear anywhere
    assert sum(s.n for s in stats[(0x180, 0)].values()) == 2


def test_collect_excludes_diagnostic_ids(tmp_path: Path) -> None:
    """Diagnostic IDs are skipped by default."""
    log = tmp_path / "frames.log"
    _write_log(
        log,
        [
            "(1.0) vcan0 7E8#0162110105A30000",
            "(1.0) vcan0 180#42",
        ],
    )
    windows = [TagWindow(label="w", start=0.5, end=2.0)]
    stats = collect_byte_stats(log, windows)
    assert (0x180, 0) in stats
    assert not any(can_id == 0x7E8 for can_id, _ in stats)


def test_collect_computes_min_max_and_unique(tmp_path: Path) -> None:
    log = tmp_path / "frames.log"
    _write_log(
        log,
        [
            "(1.0) vcan0 180#10",
            "(1.1) vcan0 180#20",
            "(1.2) vcan0 180#30",
            "(1.3) vcan0 180#20",  # duplicate
        ],
    )
    windows = [TagWindow(label="w", start=0.5, end=2.0)]
    stats = collect_byte_stats(log, windows)
    s = stats[(0x180, 0)]["w"]
    assert s.min_val == 0x10
    assert s.max_val == 0x30
    assert s.n_unique == 3
    assert s.n == 4


def test_collect_locks_dlc_per_id(tmp_path: Path) -> None:
    """DLC is locked to first sighting — never grows across frames."""
    log = tmp_path / "frames.log"
    _write_log(
        log,
        [
            "(1.0) vcan0 180#1122",  # DLC=2
            "(1.1) vcan0 180#33445566",  # longer; only first 2 bytes count
        ],
    )
    windows = [TagWindow(label="w", start=0.5, end=2.0)]
    stats = collect_byte_stats(log, windows)
    # Only bytes 0 and 1 should appear, even though frame 2 had 4 bytes
    assert (0x180, 0) in stats
    assert (0x180, 1) in stats
    assert (0x180, 2) not in stats


# ---------------------------------------------------------------------------
# rank_differentials
# ---------------------------------------------------------------------------


def test_rank_differentials_sorts_by_delta_descending():
    """Bigger spread → higher rank."""
    stats = {
        (0x180, 0): {
            "a": ByteStats(n=10, mean=10.0, min_val=10, max_val=10, n_unique=1),
            "b": ByteStats(n=10, mean=100.0, min_val=100, max_val=100, n_unique=1),
        },
        (0x200, 0): {
            "a": ByteStats(n=10, mean=10.0, min_val=10, max_val=10, n_unique=1),
            "b": ByteStats(n=10, mean=30.0, min_val=30, max_val=30, n_unique=1),
        },
    }
    diffs = rank_differentials(stats, min_delta=1.0)
    assert [d.can_id for d in diffs] == [0x180, 0x200]
    assert diffs[0].delta == 90.0
    assert diffs[1].delta == 20.0


def test_rank_differentials_filters_below_min_delta():
    """Bytes whose mean barely moves between windows shouldn't surface."""
    stats = {
        (0x180, 0): {
            "a": ByteStats(n=10, mean=10.0, min_val=10, max_val=10, n_unique=1),
            "b": ByteStats(n=10, mean=12.0, min_val=12, max_val=12, n_unique=1),
        },
    }
    assert rank_differentials(stats, min_delta=5.0) == []


def test_rank_differentials_requires_min_windows():
    """A byte seen in only one window can't be differentiated."""
    stats = {
        (0x180, 0): {
            "a": ByteStats(n=10, mean=10.0, min_val=10, max_val=10, n_unique=1),
        },
    }
    assert rank_differentials(stats, min_windows=2) == []


def test_rolling_counter_is_not_flagged_as_signal():
    """A byte that cycles 0..255 in both windows has matching means → no flag."""
    stats = {
        (0x180, 7): {
            "idle": ByteStats(n=1000, mean=127.5, min_val=0, max_val=255, n_unique=256),
            "revved": ByteStats(n=1000, mean=127.5, min_val=0, max_val=255, n_unique=256),
        },
    }
    diffs = rank_differentials(stats, min_delta=5.0)
    # Identical means → delta 0 → filtered
    assert diffs == []


def test_signal_byte_is_flagged_even_with_overlap():
    """A byte where idle stays low and revved spikes high should be flagged."""
    stats = {
        (0x180, 2): {
            "idle": ByteStats(n=100, mean=15.0, min_val=10, max_val=20, n_unique=11),
            "revved": ByteStats(n=100, mean=120.0, min_val=110, max_val=130, n_unique=21),
        },
    }
    diffs = rank_differentials(stats, min_delta=5.0)
    assert len(diffs) == 1
    assert diffs[0].delta == 105.0


# ---------------------------------------------------------------------------
# End-to-end synthetic
# ---------------------------------------------------------------------------


def test_end_to_end_finds_planted_signal(tmp_path: Path) -> None:
    """Two windows: byte 0 of 0x180 sits at 10 in 'idle' and at 100 in 'revved'.
    The differential should rank that byte at the top."""
    log = tmp_path / "frames.log"
    lines = []
    # 50 idle frames at value 10
    for i in range(50):
        lines.append(f"({1.0 + i * 0.01:.6f}) vcan0 180#0A")
    # 50 revved frames at value 100 (0x64)
    for i in range(50):
        lines.append(f"({5.0 + i * 0.01:.6f}) vcan0 180#64")
    _write_log(log, lines)
    windows = [
        TagWindow(label="idle", start=0.5, end=2.0),
        TagWindow(label="revved", start=4.5, end=6.0),
    ]
    stats = collect_byte_stats(log, windows)
    diffs = rank_differentials(stats, min_delta=5.0)
    assert len(diffs) == 1
    assert diffs[0].can_id == 0x180
    assert diffs[0].byte_index == 0
    assert abs(diffs[0].delta - 90.0) < 0.001
