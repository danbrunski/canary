"""H07.9 — a windowed read opens only the chunks its window can reach.

A dyno window used to open all ~1,200 chunks of a drive (24.8 s per window
on 2026-09-20T175050); ``read_session(time_range=...)`` skips every chunk
whose footer ``timestamp`` range is disjoint from the window, which is exact
for a row filter on the same column.
"""

from __future__ import annotations

from pathlib import Path

import pandas as pd
import pytest

from src.preprocessing import session_reader
from src.preprocessing.loader import read_session
from src.storage.parquet_writer import write_records_to_parquet
from src.storage.session_files import chunk_time_range, chunks_overlapping
from src.telemetry import TelemetryRecord

T0 = 1_790_000_000.0


def _session(tmp_path: Path) -> Path:
    """Three chunks of 100 one-second rows each: [0,100), [100,200), [200,300) s."""
    records = [TelemetryRecord.obd(T0 + s, "RPM", float(s), "rpm") for s in range(300)]
    session = tmp_path / "2026-10-05T120000"
    write_records_to_parquet(records, session, max_rows=100)
    assert len(list(session.glob("telemetry-*.parquet"))) == 3
    return session


def _at(seconds: float) -> pd.Timestamp:
    return pd.Timestamp(T0 + seconds, unit="s", tz="UTC")


def test_chunk_ranges_come_from_footers(tmp_path: Path) -> None:
    session = _session(tmp_path)
    chunks = sorted(session.glob("telemetry-*.parquet"))
    assert chunk_time_range(chunks[1]) == (_at(100), _at(199))
    assert chunks_overlapping(chunks, _at(150), _at(160)) == [chunks[1]]
    # A window touching a boundary keeps both sides.
    assert chunks_overlapping(chunks, _at(199), _at(200)) == chunks[1:]
    empty = session / "telemetry-99999.parquet"
    empty.write_bytes(b"")
    # Unknown range: cannot be ruled out, so it is kept.
    assert chunk_time_range(empty) is None
    assert empty in chunks_overlapping([*chunks, empty], _at(150), _at(160))


def test_a_windowed_read_opens_only_its_chunks_and_returns_the_same_rows(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    session = _session(tmp_path)
    lo, hi = _at(150), _at(160)
    window = [("timestamp", ">=", lo), ("timestamp", "<=", hi)]
    whole = read_session(session, filters=window, decoded="as_recorded")

    opened: list[Path] = []
    real = session_reader.pd.read_parquet

    def spy(path: Path, *args: object, **kwargs: object) -> pd.DataFrame:
        opened.append(Path(path))
        return real(path, *args, **kwargs)  # type: ignore[arg-type]

    monkeypatch.setattr(session_reader.pd, "read_parquet", spy)
    pruned = read_session(session, filters=window, decoded="as_recorded", time_range=(lo, hi))

    assert len(opened) == 1
    pd.testing.assert_frame_equal(
        whole.reset_index(drop=True), pruned.reset_index(drop=True), check_like=True
    )
    assert len(pruned) == 11


def test_a_window_no_chunk_reaches_is_an_empty_frame(tmp_path: Path) -> None:
    session = _session(tmp_path)
    out = read_session(session, decoded="as_recorded", time_range=(_at(1000), _at(1010)))
    assert out.empty
