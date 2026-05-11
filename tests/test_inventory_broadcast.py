"""Tests for the broadcast-CAN-ID inventory tool."""

from __future__ import annotations

from pathlib import Path

from tools.inventory_broadcast import inventory


def _write_log(path: Path, lines: list[str]) -> None:
    path.write_text("\n".join(lines) + "\n")


def test_inventory_excludes_diagnostic_ids(tmp_path: Path):
    """0x7DF/0x7E0..0x7EF should be filtered as diagnostic, not broadcast."""
    log = tmp_path / "frames.log"
    _write_log(
        log,
        [
            "(1.0) vcan0 7DF#0102030000000000",
            "(1.1) vcan0 7E0#0102030000000000",
            "(1.2) vcan0 7E8#0102030000000000",
            "(1.3) vcan0 180#1122334455667788",
            "(1.4) vcan0 180#1122334455667788",
        ],
    )
    inv = inventory(log)
    assert [r.can_id for r in inv] == [0x180]


def test_inventory_counts_frames_per_id(tmp_path: Path):
    log = tmp_path / "frames.log"
    _write_log(
        log,
        [
            "(1.0) vcan0 180#0000000000000000",
            "(1.1) vcan0 180#0000000000000000",
            "(1.2) vcan0 180#0000000000000000",
            "(1.3) vcan0 280#FF",
        ],
    )
    inv = inventory(log)
    by_id = {r.can_id: r for r in inv}
    assert by_id[0x180].n_frames == 3
    assert by_id[0x280].n_frames == 1


def test_inventory_sorted_by_count_descending(tmp_path: Path):
    log = tmp_path / "frames.log"
    _write_log(
        log,
        [
            "(1.0) vcan0 280#00",
            "(1.1) vcan0 180#00",
            "(1.2) vcan0 180#00",
            "(1.3) vcan0 180#00",
        ],
    )
    inv = inventory(log)
    assert [r.can_id for r in inv] == [0x180, 0x280]


def test_inventory_computes_median_period(tmp_path: Path):
    """20ms spacing → median period 20ms."""
    log = tmp_path / "frames.log"
    _write_log(
        log,
        [f"({1.0 + i * 0.020:.6f}) vcan0 180#00" for i in range(10)],
    )
    inv = inventory(log)
    assert len(inv) == 1
    assert abs(inv[0].period_ms - 20.0) < 0.01


def test_inventory_period_zero_for_single_frame(tmp_path: Path):
    log = tmp_path / "frames.log"
    _write_log(log, ["(1.0) vcan0 180#00"])
    inv = inventory(log)
    assert inv[0].period_ms == 0.0


def test_byte_variability_tracks_unique_values_per_position(tmp_path: Path):
    """Byte 0 varies (3 unique), byte 1 constant (1 unique)."""
    log = tmp_path / "frames.log"
    _write_log(
        log,
        [
            "(1.0) vcan0 180#01FF",
            "(1.1) vcan0 180#02FF",
            "(1.2) vcan0 180#03FF",
        ],
    )
    inv = inventory(log)
    assert inv[0].byte_variability == (3, 1)


def test_sample_frames_capped_at_n_samples(tmp_path: Path):
    log = tmp_path / "frames.log"
    _write_log(log, [f"(1.{i}) vcan0 180#0{i}" for i in range(10)])
    inv = inventory(log, n_samples=3)
    assert len(inv[0].sample_frames) == 3
    # First three should be retained
    assert inv[0].sample_frames[0] == bytes([0x00])
    assert inv[0].sample_frames[2] == bytes([0x02])


def test_first_and_last_seen_timestamps(tmp_path: Path):
    log = tmp_path / "frames.log"
    _write_log(
        log,
        [
            "(10.0) vcan0 180#00",
            "(15.5) vcan0 180#00",
            "(22.3) vcan0 180#00",
        ],
    )
    inv = inventory(log)
    assert inv[0].first_seen == 10.0
    assert inv[0].last_seen == 22.3


def test_dlc_locked_to_first_sighting(tmp_path: Path):
    """Real bus frames are fixed-length per ID; first sighting wins."""
    log = tmp_path / "frames.log"
    _write_log(
        log,
        [
            "(1.0) vcan0 180#1122334455667788",  # 8 bytes
            "(1.1) vcan0 180#1122334455667788",
        ],
    )
    inv = inventory(log)
    assert inv[0].dlc == 8
    assert len(inv[0].byte_variability) == 8


def test_empty_log_returns_empty_inventory(tmp_path: Path):
    log = tmp_path / "frames.log"
    log.write_text("")
    assert inventory(log) == []


def test_only_diagnostic_returns_empty(tmp_path: Path):
    log = tmp_path / "frames.log"
    _write_log(
        log,
        [
            "(1.0) vcan0 7DF#0102030000000000",
            "(1.1) vcan0 7E8#0102030000000000",
        ],
    )
    assert inventory(log) == []


def test_custom_exclude_set(tmp_path: Path):
    """Caller can pass their own exclude_ids."""
    log = tmp_path / "frames.log"
    _write_log(
        log,
        [
            "(1.0) vcan0 180#00",
            "(1.1) vcan0 200#00",
        ],
    )
    inv = inventory(log, exclude_ids={0x180})
    assert [r.can_id for r in inv] == [0x200]
