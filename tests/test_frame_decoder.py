"""FrameDecoder — decoded-channel persistence for the storage sink."""

from __future__ import annotations

import asyncio
import json
from pathlib import Path

import pytest

from src.decode.bindings import Bindings, Signal
from src.decode.frame_decoder import FrameDecoder
from src.telemetry import TelemetryRecord

# 0x1F9 bytes 2-3 big-endian is the confirmed ECM_RPM binding (8 counts/rpm);
# mirror its shape so the test exercises the real target of this feature.
_RPM_SIGNAL = Signal(
    name="ECM_RPM",
    module="ECM",
    can_id=0x1F9,
    byte_start=2,
    byte_length=2,
    byte_order="big",
    scale=0.125,
    unit="rpm",
    confidence="confirmed",
)
_HYPOTHESIZED_SIGNAL = Signal(
    name="ECM_MYSTERY",
    module="ECM",
    can_id=0x1F9,
    byte_start=0,
    byte_length=1,
    confidence="hypothesized",
)


def _bindings(*signals: Signal) -> Bindings:
    return Bindings(schema_version=1, signals=list(signals))


def _rpm_frame(raw_rpm_counts: int) -> TelemetryRecord:
    payload = bytes([0x20, 0x00]) + raw_rpm_counts.to_bytes(2, "big") + bytes([0, 0, 0x5B, 0])
    return TelemetryRecord.can(1_753_800_000.0, 0x1F9, payload)


def test_decodes_bound_signal_to_raw_value() -> None:
    decoder = FrameDecoder.from_bindings(_bindings(_RPM_SIGNAL))
    (row,) = decoder.decode(_rpm_frame(26875))  # 26875 / 8 = 3359.375 rpm

    assert row.source == "decoded"
    assert row.channel == "ECM_RPM"
    assert row.value == 26875.0  # RAW counts — decode_can_rows semantics
    assert row.unit == "rpm"
    assert row.metadata == {"can_id": 0x1F9, "binding_confidence": "confirmed"}


def test_confidence_filter_drops_unlisted_bindings() -> None:
    decoder = FrameDecoder.from_bindings(
        _bindings(_RPM_SIGNAL, _HYPOTHESIZED_SIGNAL),
        confidence_levels=("confirmed",),
    )
    rows = decoder.decode(_rpm_frame(8000))
    assert [r.channel for r in rows] == ["ECM_RPM"]
    assert decoder.signal_count == 1


def test_short_payload_omits_signal_instead_of_nan() -> None:
    decoder = FrameDecoder.from_bindings(_bindings(_RPM_SIGNAL))
    short = TelemetryRecord.can(1_753_800_000.0, 0x1F9, b"\x20\x00\x68")
    assert decoder.decode(short) == ()


def test_non_can_and_unbound_records_pass_through_empty() -> None:
    decoder = FrameDecoder.from_bindings(_bindings(_RPM_SIGNAL))
    assert decoder.decode(TelemetryRecord.obd(1.0, "RPM", 800.0, "rpm")) == ()
    assert decoder.decode(TelemetryRecord.can(1.0, 0x2DE, b"\x00" * 8)) == ()


def test_from_path_returns_none_when_bindings_absent(tmp_path: Path) -> None:
    assert FrameDecoder.from_path(tmp_path / "nope.yaml") is None


def test_matches_decode_can_rows_semantics() -> None:
    """Stored rows must be indistinguishable from the batch decode-at-read
    path, or `decode_can_rows`' skip-existing guard would leave mixed
    conventions in the corpus."""
    import pandas as pd

    from src.preprocessing.decode import decode_can_rows

    record = _rpm_frame(26875)
    long_frame = pd.DataFrame(
        {
            "timestamp": [record.timestamp],
            "source": ["can"],
            "channel": [record.channel],
            "payload": [record.payload],
            "session": ["s"],
        }
    )
    batch = decode_can_rows(long_frame, _bindings(_RPM_SIGNAL))
    batch_row = batch[batch["source"] == "decoded"].iloc[0]

    (stored,) = FrameDecoder.from_bindings(_bindings(_RPM_SIGNAL)).decode(record)
    assert stored.channel == batch_row["channel"]
    assert stored.value == batch_row["value_number"]
    assert stored.unit == batch_row["unit"]
    assert stored.metadata == json.loads(batch_row["metadata_json"])


def test_decode_can_rows_skips_signals_already_stored() -> None:
    """A backfilled/ingest-decoded session must not get duplicate rows."""
    import pandas as pd

    from src.preprocessing.decode import decode_can_rows

    record = _rpm_frame(26875)
    long_frame = pd.DataFrame(
        {
            "timestamp": [record.timestamp, record.timestamp],
            "source": ["can", "decoded"],
            "channel": [record.channel, "ECM_RPM"],
            "payload": [record.payload, None],
            "session": ["s", "s"],
        }
    )
    out = decode_can_rows(long_frame, _bindings(_RPM_SIGNAL))
    assert len(out) == len(long_frame)  # nothing appended — stored row wins


def test_storage_sink_persists_decoded_rows_interleaved() -> None:
    """run_storage_sink with a decoder writes raw + decoded, in order."""
    from src.daemon.drain import run_storage_sink

    written: list[TelemetryRecord] = []

    class _Writer:
        """Sync writer subset; `ensure_async_writer` wraps it in the sink."""

        def write_many(self, records: list[TelemetryRecord]) -> list[Path]:
            written.extend(records)
            return []

        def flush_due(self) -> bool:
            return False

        def flush(self) -> list[Path]:
            return []

        def close(self) -> list[Path]:
            return []

    decoder = FrameDecoder.from_bindings(_bindings(_RPM_SIGNAL))

    async def _run() -> None:
        queue: asyncio.Queue[TelemetryRecord] = asyncio.Queue()
        stop = asyncio.Event()
        queue.put_nowait(_rpm_frame(26875))
        queue.put_nowait(TelemetryRecord.obd(1_753_800_000.5, "RPM", 3360.0, "rpm"))
        task = asyncio.create_task(
            run_storage_sink(
                queue,
                _Writer(),  # type: ignore[arg-type] — protocol subset is what the sink uses
                stop,
                poll_interval=0.01,
                decode=decoder.decode,
            )
        )
        await queue.join()
        stop.set()
        await asyncio.wait_for(task, timeout=5)

    asyncio.run(_run())

    assert [(r.source, r.channel) for r in written] == [
        ("can", "0x1F9"),
        ("decoded", "ECM_RPM"),
        ("obd", "RPM"),
    ]
    decoded = written[1]
    assert decoded.value == 26875.0
    assert decoded.timestamp == written[0].timestamp  # chronological interleave


@pytest.mark.parametrize("levels", [(), None])
def test_from_bindings_confidence_edge_cases(levels: tuple[str, ...] | None) -> None:
    decoder = FrameDecoder.from_bindings(
        _bindings(_RPM_SIGNAL, _HYPOTHESIZED_SIGNAL), confidence_levels=levels
    )
    # None → everything; empty tuple → nothing (runtime treats empty config
    # as "disabled" before construction, but the class itself is literal).
    assert decoder.signal_count == (2 if levels is None else 0)
