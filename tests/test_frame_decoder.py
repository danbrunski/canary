"""FrameDecoder — the live path's per-frame decoder, and the rule the decode on
read shares (P02.8: nothing stores ``decoded`` rows any more)."""

from __future__ import annotations

import asyncio
import json
from pathlib import Path

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


def test_the_live_decoder_decodes_every_binding_whatever_its_confidence() -> None:
    """The live source shows and scores every binding; the confidence filter
    was the storage sink's, and there is no storage sink decode any more."""
    decoder = FrameDecoder.from_bindings(_bindings(_RPM_SIGNAL, _HYPOTHESIZED_SIGNAL))
    rows = decoder.decode(_rpm_frame(8000))
    assert [r.channel for r in rows] == ["ECM_RPM", "ECM_MYSTERY"]
    assert decoder.signal_count == 2


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
    """A live record must be indistinguishable from the batch decode's row,
    or a value the dashboard showed and the one a reader decodes later would
    follow different conventions."""
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


def test_decode_can_rows_replaces_a_stored_row_with_the_recomputed_one() -> None:
    """P02.8: decode always. An old session's stored row (here a stale value,
    as after a binding edit) is replaced by the current decode, never kept
    beside it or preferred to it."""
    import pandas as pd

    from src.preprocessing.decode import decode_can_rows

    record = _rpm_frame(26875)
    long_frame = pd.DataFrame(
        {
            "timestamp": [record.timestamp, record.timestamp],
            "source": ["can", "decoded"],
            "channel": [record.channel, "ECM_RPM"],
            "value_number": [None, 1.0],
            "payload": [record.payload, None],
            "session": ["s", "s"],
        }
    )
    out = decode_can_rows(long_frame, _bindings(_RPM_SIGNAL))
    decoded = out[out["source"] == "decoded"]
    assert len(out) == len(long_frame)
    assert decoded["value_number"].tolist() == [26875.0]


def test_the_storage_sink_never_writes_a_decoded_row() -> None:
    """P02.8: whatever reaches the storage sink's queue, a decoded row is not
    written; raw rows are written in order. (The fanout does not even
    enqueue one: ``tests/test_decoded_rows_never_stored.py``.)"""
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

    frame = _rpm_frame(26875)
    (decoded,) = FrameDecoder.from_bindings(_bindings(_RPM_SIGNAL)).decode(frame)

    async def _run() -> None:
        queue: asyncio.Queue[TelemetryRecord] = asyncio.Queue()
        stop = asyncio.Event()
        queue.put_nowait(frame)
        queue.put_nowait(decoded)
        queue.put_nowait(TelemetryRecord.obd(1_753_800_000.5, "RPM", 3360.0, "rpm"))
        task = asyncio.create_task(
            run_storage_sink(
                queue,
                _Writer(),  # type: ignore[arg-type]  # protocol subset is what the sink uses
                stop,
                poll_interval=0.01,
            )
        )
        await queue.join()
        stop.set()
        await asyncio.wait_for(task, timeout=5)

    asyncio.run(_run())

    assert [(r.source, r.channel) for r in written] == [("can", "0x1F9"), ("obd", "RPM")]


def test_persist_puts_a_binding_in_the_on_read_set_whatever_its_confidence() -> None:
    """`persist` answers "do the session readers need it", not "trustworthy".

    The wheel-speed bindings sit at `high` for a documented reason — Mode 01
    speed is quantised to whole km/h, so the last digit of the 1/16 scale is
    unpinned — and must NOT be promoted to `confirmed` to be read. They are
    still the only fast road-speed source the digest's segmenter can reach.
    Nothing is stored either way (P02.8): this is the set every session is
    decoded with on read.
    """
    from src.decode.bindings import on_read_signals

    bindings = Bindings(
        schema_version=1,
        signals=[
            Signal(
                name="WHEEL_SPEED",
                module="ABS",
                can_id=0x6F7,
                byte_start=0,
                byte_length=2,
                scale=0.0625,
                offset=0.0,
                unit="km/h",
                confidence="high",
                persist=True,
            ),
            Signal(
                name="UNTRUSTED",
                module="ABS",
                can_id=0x6F7,
                byte_start=2,
                byte_length=2,
                confidence="high",
            ),
        ],
    )

    assert [signal.name for signal in on_read_signals(bindings)] == ["WHEEL_SPEED"]
    assert [signal.name for signal in on_read_signals(_bindings(_HYPOTHESIZED_SIGNAL))] == []
    assert [signal.name for signal in on_read_signals(_bindings(_RPM_SIGNAL))] == ["ECM_RPM"]


def test_persist_defaults_off_so_the_on_read_set_stays_opt_in() -> None:
    """A binding joins every session's decoded rows only when confirmed or
    marked: a new one must not change what the readers see by default."""
    signal = Signal(
        name="NEW", module="ECM", can_id=0x123, byte_start=0, byte_length=1, confidence="confirmed"
    )
    assert signal.persist is False


def test_a_sentinel_word_decodes_to_a_state_row_not_a_number() -> None:
    """A04.6: the declared no-measurement word is a ``_SENTINEL`` state row
    carrying the stated meaning; every other value decodes exactly as before."""
    tire = Signal(
        name="TIRE_A",
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
    decoder = FrameDecoder.from_bindings(_bindings(tire))
    (asleep,) = decoder.decode(TelemetryRecord.can(1.0, 0x385, bytes(8)))
    assert (asleep.channel, asleep.value, asleep.unit) == (
        "TIRE_A_SENTINEL",
        "wheel sensor not transmitting",
        "",
    )
    assert asleep.metadata == {"can_id": 0x385, "binding_confidence": "hypothesized"}
    (awake,) = decoder.decode(TelemetryRecord.can(2.0, 0x385, bytes([0, 0, 148, 0])))
    assert (awake.channel, awake.value, awake.unit) == ("TIRE_A", 148.0, "psi")
