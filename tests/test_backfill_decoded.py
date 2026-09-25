"""tools/backfill_decoded.py — retroactive decoded-channel backfill."""

from __future__ import annotations

from pathlib import Path

import pandas as pd
import pytest

from src.decode.bindings import Bindings, Signal
from src.storage.parquet_writer import ParquetTelemetryWriter
from src.telemetry import TelemetryRecord
from tools import backfill_decoded as tool

_RPM_SIGNAL = Signal(
    name="ECM_RPM",
    module="ECM",
    can_id=0x1F9,
    byte_start=2,
    byte_length=2,
    scale=0.125,
    unit="rpm",
    confidence="confirmed",
)


def _write_session(root: Path, *, with_decoded: bool = False) -> Path:
    from datetime import UTC, datetime

    started = datetime(2026, 7, 28, 16, 46, 57, tzinfo=UTC)
    writer = ParquetTelemetryWriter(root, session_started_at=started, max_rows=100)
    base = started.timestamp()
    for i in range(10):
        counts = 8 * (800 + 100 * i)  # 800..1700 rpm in raw counts
        payload = bytes([0x20, 0x00]) + counts.to_bytes(2, "big") + bytes(4)
        writer.write(TelemetryRecord.can(base + i * 0.01, 0x1F9, payload))
        if with_decoded:
            writer.write(
                TelemetryRecord.decoded(
                    TelemetryRecord.can(base + i * 0.01, 0x1F9, payload).timestamp,
                    "ECM_RPM",
                    float(counts),
                    "rpm",
                )
            )
    writer.close()
    return writer.paths.root


def test_backfill_writes_sidecar_with_raw_values(tmp_path: Path) -> None:
    session_dir = _write_session(tmp_path)
    result = tool.backfill_session(session_dir, [_RPM_SIGNAL], dry_run=False)
    assert "wrote" in result

    sidecar = session_dir / f"{tool.BACKFILL_STEM}.parquet"
    frame = pd.read_parquet(sidecar)
    assert set(frame["channel"]) == {"ECM_RPM"}
    assert len(frame) == 10
    assert frame["source"].eq("decoded").all()
    # RAW counts, not scaled rpm — decode_can_rows semantics.
    assert frame["value_number"].iloc[0] == pytest.approx(8 * 800)
    assert frame["timestamp"].is_monotonic_increasing


def test_dry_run_writes_nothing(tmp_path: Path) -> None:
    session_dir = _write_session(tmp_path)
    result = tool.backfill_session(session_dir, [_RPM_SIGNAL], dry_run=True)
    assert result.startswith("would write 10 rows")
    assert not any(session_dir.glob(f"{tool.BACKFILL_STEM}*.parquet"))


def test_decoded_row_state_distinguishes_backfill_from_ingest(tmp_path: Path) -> None:
    plain = _write_session(tmp_path / "a")
    assert tool._decoded_row_state(plain) == "none"

    tool.backfill_session(plain, [_RPM_SIGNAL], dry_run=False)
    assert tool._decoded_row_state(plain) == "backfill"

    ingest = _write_session(tmp_path / "b", with_decoded=True)
    assert tool._decoded_row_state(ingest) == "ingest"


def test_loader_sees_backfilled_rows_without_double_decode(tmp_path: Path) -> None:
    """`read_session_decoded` must surface exactly the backfilled rows for a
    backfilled signal — the skip-existing guard, end to end."""
    from src.decode.bindings import Bindings
    from src.preprocessing.decode import decode_can_rows
    from src.preprocessing.loader import read_session

    session_dir = _write_session(tmp_path)
    tool.backfill_session(session_dir, [_RPM_SIGNAL], dry_run=False)

    frame = read_session(session_dir)
    out = decode_can_rows(frame, Bindings(schema_version=1, signals=[_RPM_SIGNAL]))
    rpm_rows = out[(out["source"] == "decoded") & (out["channel"] == "ECM_RPM")]
    assert len(rpm_rows) == 10  # stored rows only — nothing recomputed on top


def test_backfill_stores_a_sentinel_as_the_live_decoders_state_row() -> None:
    """A04.6: the backfilled rows are the ones FrameDecoder writes at ingest —
    the sentinel word a ``_SENTINEL`` state row, never a number."""
    from src.decode.frame_decoder import FrameDecoder

    tyre = Signal(
        name="TYRE_A",
        module="BCM",
        can_id=0x385,
        byte_start=2,
        byte_length=1,
        scale=0.25,
        unit="psi",
        confidence="hypothesized",
        sentinel_raw=0,
        sentinel_meaning="wheel sensor not transmitting",
    )
    records = [
        TelemetryRecord.can(1000.0, 0x385, bytes(8)),
        TelemetryRecord.can(1000.1, 0x385, bytes([0, 0, 148, 0, 0, 0, 0, 0])),
    ]
    can_rows = pd.DataFrame(
        {
            "timestamp": [r.timestamp for r in records],
            "source": "can",
            "channel": [r.channel for r in records],
            "payload": [r.payload for r in records],
        }
    )
    out = tool.decode_session_frame(can_rows, [tyre])
    stored = {
        (row.channel, row.value_text if isinstance(row.value_text, str) else row.value_number)
        for row in out.itertuples()
    }
    decoder = FrameDecoder.from_bindings(Bindings(schema_version=1, signals=[tyre]))
    live = {(d.channel, d.value) for r in records for d in decoder.decode(r)}
    assert (
        stored == live == {("TYRE_A_SENTINEL", "wheel sensor not transmitting"), ("TYRE_A", 148.0)}
    )
