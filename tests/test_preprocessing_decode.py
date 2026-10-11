"""Tests for decoding CAN payloads into named signal columns."""

from __future__ import annotations

import json
from pathlib import Path

import numpy as np
import pandas as pd
import pytest

from src.decode.bindings import Bindings, Signal, load_bindings
from src.preprocessing.decode import (
    DECODED_SOURCE,
    decode_can_rows,
    decoded_channel,
    read_session_decoded,
)
from src.preprocessing.loader import to_wide
from tests.configs import example_config

# ---------------------------------------------------------------------------
# Fixtures: synthetic long-format telemetry frames
# ---------------------------------------------------------------------------


def _can_long_frame(rows: list[tuple[float, int, bytes]]) -> pd.DataFrame:
    """Build a long-format frame shaped like loader.read_session output, CAN-only.

    rows = list of (epoch_seconds, can_id, payload_bytes).
    """
    records = []
    for ts, can_id, payload in rows:
        channel = f"0x{can_id:X}"
        records.append(
            {
                "timestamp": pd.Timestamp(ts, unit="s", tz="UTC"),
                "source": "can",
                "channel": channel,
                "value_number": np.nan,
                "value_text": None,
                "value_bool": None,
                "unit": "",
                "payload": payload,
                "metadata_json": json.dumps({"can_id": can_id, "dlc": len(payload)}),
                "session": "synth",
                "session_path": "/tmp/synth",
                "source_channel": f"can:{channel}",
                "value": np.nan,
            }
        )
    return pd.DataFrame(records)


def _bindings(*signals: Signal) -> Bindings:
    return Bindings(schema_version=1, signals=list(signals))


def _sig(**kw) -> Signal:
    defaults = dict(
        name="TEST_SIG",
        module="ECM",
        can_id=0x551,
        byte_start=0,
        byte_length=1,
        confidence="high",
    )
    defaults.update(kw)
    return Signal(**defaults)  # type: ignore[arg-type]


# ---------------------------------------------------------------------------
# decoded_channel
# ---------------------------------------------------------------------------


def test_decoded_channel_naming():
    assert decoded_channel("ECM_COOLANT_TEMP") == "decoded:ECM_COOLANT_TEMP"


# ---------------------------------------------------------------------------
# decode_can_rows — basic decoding
# ---------------------------------------------------------------------------


def test_decode_single_byte_signal():
    frame = _can_long_frame(
        [
            (1.0, 0x551, bytes([42, 0, 0])),
            (2.0, 0x551, bytes([99, 0, 0])),
        ]
    )
    out = decode_can_rows(
        frame, _bindings(_sig(name="COOLANT", can_id=0x551, byte_start=0, byte_length=1))
    )
    decoded = out[out["source"] == DECODED_SOURCE]
    assert len(decoded) == 2
    assert decoded["channel"].unique().tolist() == ["COOLANT"]
    assert sorted(decoded["value"].tolist()) == [42.0, 99.0]
    assert decoded["source_channel"].iloc[0] == "decoded:COOLANT"


def test_decode_two_byte_big_endian():
    frame = _can_long_frame([(1.0, 0x23D, bytes([0xFF, 0x12, 0x34, 0xFF, 0, 0, 0, 0]))])
    out = decode_can_rows(
        frame,
        _bindings(_sig(name="REV", can_id=0x23D, byte_start=1, byte_length=2, byte_order="big")),
    )
    decoded = out[out["source"] == DECODED_SOURCE]
    assert decoded["value"].iloc[0] == float(0x1234)


def test_decode_two_byte_little_endian():
    frame = _can_long_frame([(1.0, 0x23D, bytes([0xFF, 0x34, 0x12, 0xFF, 0, 0, 0, 0]))])
    out = decode_can_rows(
        frame,
        _bindings(_sig(name="REV", can_id=0x23D, byte_start=1, byte_length=2, byte_order="little")),
    )
    decoded = out[out["source"] == DECODED_SOURCE]
    assert decoded["value"].iloc[0] == float(0x1234)


def test_decode_multi_byte_accumulator():
    """A 5-byte field like the odometer accumulator decodes as one big int."""
    payload = bytes([0, 0, 0x01, 0x02, 0x03, 0x04, 0x05, 0])
    frame = _can_long_frame([(1.0, 0x385, payload)])
    out = decode_can_rows(
        frame, _bindings(_sig(name="ODO", can_id=0x385, byte_start=2, byte_length=5))
    )
    decoded = out[out["source"] == DECODED_SOURCE]
    expected = float(int.from_bytes(bytes([0x01, 0x02, 0x03, 0x04, 0x05]), "big"))
    assert decoded["value"].iloc[0] == expected


def test_short_payload_yields_nan():
    """If a frame's payload is shorter than the binding's byte range, NaN."""
    frame = _can_long_frame(
        [
            (1.0, 0x551, bytes([42])),  # only 1 byte
            (2.0, 0x551, bytes([10, 20, 30])),  # enough
        ]
    )
    out = decode_can_rows(
        frame, _bindings(_sig(name="SIG", can_id=0x551, byte_start=1, byte_length=2))
    )
    decoded = out[out["source"] == DECODED_SOURCE].sort_values("timestamp")
    assert np.isnan(decoded["value"].iloc[0])  # short payload
    assert decoded["value"].iloc[1] == float(int.from_bytes(bytes([20, 30]), "big"))


# ---------------------------------------------------------------------------
# Multiple bindings on one ID / multiple IDs
# ---------------------------------------------------------------------------


def test_multiple_bindings_on_same_can_id():
    frame = _can_long_frame([(1.0, 0x233, bytes([0x10, 0x20, 0, 0x40, 0, 0, 0, 0]))])
    out = decode_can_rows(
        frame,
        _bindings(
            _sig(name="MAP", can_id=0x233, byte_start=1, byte_length=1),
            _sig(name="LOAD", can_id=0x233, byte_start=3, byte_length=1),
        ),
    )
    decoded = out[out["source"] == DECODED_SOURCE]
    by_name = decoded.set_index("channel")["value"]
    assert by_name["MAP"] == 0x20
    assert by_name["LOAD"] == 0x40


def test_bindings_on_unrelated_ids_ignored():
    frame = _can_long_frame(
        [
            (1.0, 0x551, bytes([42, 0])),
            (1.1, 0x999, bytes([1, 2, 3])),  # no binding for 0x999
        ]
    )
    out = decode_can_rows(
        frame, _bindings(_sig(name="COOLANT", can_id=0x551, byte_start=0, byte_length=1))
    )
    decoded = out[out["source"] == DECODED_SOURCE]
    # Only the 0x551 frame produced a decoded row
    assert len(decoded) == 1
    assert decoded["channel"].iloc[0] == "COOLANT"


# ---------------------------------------------------------------------------
# Confidence filtering
# ---------------------------------------------------------------------------


def test_confidence_filter_includes_only_listed_levels():
    frame = _can_long_frame([(1.0, 0x551, bytes([42, 0]))])
    bindings = _bindings(
        _sig(name="HIGH_SIG", can_id=0x551, byte_start=0, byte_length=1, confidence="high"),
        _sig(name="HYPO_SIG", can_id=0x551, byte_start=1, byte_length=1, confidence="hypothesized"),
    )
    out = decode_can_rows(frame, bindings, confidence_levels={"high"})
    decoded = out[out["source"] == DECODED_SOURCE]
    assert decoded["channel"].unique().tolist() == ["HIGH_SIG"]


def test_confidence_filter_none_includes_all():
    frame = _can_long_frame([(1.0, 0x551, bytes([42, 7]))])
    bindings = _bindings(
        _sig(name="HIGH_SIG", can_id=0x551, byte_start=0, byte_length=1, confidence="high"),
        _sig(name="HYPO_SIG", can_id=0x551, byte_start=1, byte_length=1, confidence="hypothesized"),
    )
    out = decode_can_rows(frame, bindings, confidence_levels=None)
    decoded = out[out["source"] == DECODED_SOURCE]
    assert set(decoded["channel"].tolist()) == {"HIGH_SIG", "HYPO_SIG"}


def test_confidence_filter_empty_result_returns_input_unchanged():
    frame = _can_long_frame([(1.0, 0x551, bytes([42, 0]))])
    bindings = _bindings(_sig(name="X", can_id=0x551, confidence="hypothesized"))
    out = decode_can_rows(frame, bindings, confidence_levels={"confirmed"})
    # No binding matched the filter → frame returned as-is
    assert out.equals(frame)


# ---------------------------------------------------------------------------
# Edge cases
# ---------------------------------------------------------------------------


def test_empty_frame_returned_unchanged():
    empty = pd.DataFrame(columns=["timestamp", "source", "channel", "payload", "value"])
    out = decode_can_rows(empty, _bindings(_sig()))
    assert out.equals(empty)


def test_frame_with_no_can_rows_returned_unchanged():
    frame = pd.DataFrame(
        {
            "timestamp": [pd.Timestamp("2026-05-11T15:00:00Z")],
            "source": ["obd"],
            "channel": ["RPM"],
            "payload": [None],
            "value": [752.0],
        }
    )
    out = decode_can_rows(frame, _bindings(_sig()))
    assert out.equals(frame)


def test_no_matching_binding_returns_input_unchanged():
    frame = _can_long_frame([(1.0, 0x100, bytes([1, 2, 3]))])
    out = decode_can_rows(frame, _bindings(_sig(can_id=0x999)))
    assert out.equals(frame)


def test_input_frame_not_mutated():
    frame = _can_long_frame([(1.0, 0x551, bytes([42, 0]))])
    before = frame.copy()
    _ = decode_can_rows(
        frame, _bindings(_sig(name="COOLANT", can_id=0x551, byte_start=0, byte_length=1))
    )
    pd.testing.assert_frame_equal(frame, before)


def test_decoded_rows_preserve_session_metadata():
    frame = _can_long_frame([(1.0, 0x551, bytes([42, 0]))])
    out = decode_can_rows(
        frame, _bindings(_sig(name="COOLANT", can_id=0x551, byte_start=0, byte_length=1))
    )
    decoded = out[out["source"] == DECODED_SOURCE].iloc[0]
    assert decoded["session"] == "synth"
    assert decoded["session_path"] == "/tmp/synth"
    meta = json.loads(decoded["metadata_json"])
    assert meta["can_id"] == 0x551
    assert meta["binding_confidence"] == "high"


def test_decoded_unit_carried_from_binding():
    frame = _can_long_frame([(1.0, 0x551, bytes([100, 0]))])
    out = decode_can_rows(
        frame,
        _bindings(_sig(name="COOLANT", can_id=0x551, byte_start=0, byte_length=1, unit="degC")),
    )
    decoded = out[out["source"] == DECODED_SOURCE].iloc[0]
    assert decoded["unit"] == "degC"


# ---------------------------------------------------------------------------
# Integration with to_wide — the actual point of this module
# ---------------------------------------------------------------------------


def test_decoded_rows_become_wide_columns():
    """The whole reason this module exists: decoded:NAME columns after to_wide."""
    frame = _can_long_frame(
        [
            (1.0, 0x551, bytes([42, 0])),
            (2.0, 0x551, bytes([50, 0])),
            (1.5, 0x233, bytes([0, 0x80, 0, 0, 0, 0, 0, 0])),
        ]
    )
    bindings = _bindings(
        _sig(name="COOLANT", can_id=0x551, byte_start=0, byte_length=1),
        _sig(name="MAP", can_id=0x233, byte_start=1, byte_length=1),
    )
    decoded_long = decode_can_rows(frame, bindings)
    wide = to_wide(decoded_long)
    assert "decoded:COOLANT" in wide.columns
    assert "decoded:MAP" in wide.columns
    # The raw CAN payload columns also exist (object dtype, skipped by features)
    assert "can:0x551" in wide.columns
    # COOLANT values present at their timestamps
    coolant_col = wide["decoded:COOLANT"].dropna()
    assert set(coolant_col.tolist()) == {42.0, 50.0}


def test_decoded_columns_are_numeric():
    """Decoded columns must be numeric so compute_features picks them up."""
    frame = _can_long_frame([(1.0, 0x551, bytes([42, 0])), (2.0, 0x551, bytes([50, 0]))])
    decoded_long = decode_can_rows(
        frame, _bindings(_sig(name="COOLANT", can_id=0x551, byte_start=0, byte_length=1))
    )
    wide = to_wide(decoded_long)
    assert pd.api.types.is_numeric_dtype(wide["decoded:COOLANT"])


# ---------------------------------------------------------------------------
# read_session_decoded convenience
# ---------------------------------------------------------------------------


def _write_synthetic_parquet_session(root: Path) -> Path:
    """Write a minimal telemetry-*.parquet session readable by loader.read_session."""
    import pyarrow as pa
    import pyarrow.parquet as pq

    from src.storage.parquet_writer import TELEMETRY_SCHEMA

    session = root / "2026-05-11T120000"
    session.mkdir()
    # Two CAN frames on 0x551 (coolant byte 0)
    rows = [
        {
            "timestamp": pd.Timestamp("2026-05-11T12:00:00Z").to_pydatetime(),
            "source": "can",
            "channel": "0x551",
            "value_number": None,
            "value_text": None,
            "value_bool": None,
            "unit": "",
            "payload": bytes([42, 0]),
            "metadata_json": json.dumps({"can_id": 0x551, "dlc": 2}),
        },
        {
            "timestamp": pd.Timestamp("2026-05-11T12:00:01Z").to_pydatetime(),
            "source": "can",
            "channel": "0x551",
            "value_number": None,
            "value_text": None,
            "value_bool": None,
            "unit": "",
            "payload": bytes([55, 0]),
            "metadata_json": json.dumps({"can_id": 0x551, "dlc": 2}),
        },
    ]
    table = pa.Table.from_pylist(rows, schema=TELEMETRY_SCHEMA)
    pq.write_table(table, session / "telemetry-00000.parquet")
    return session


def test_read_session_decoded_appends_decoded_rows(tmp_path: Path):
    session = _write_synthetic_parquet_session(tmp_path)
    bindings_path = tmp_path / "bindings.yaml"
    bindings_path.write_text(
        """
schema_version: 1
signals:
  - name: ECM_COOLANT_TEMP
    module: ECM
    can_id: 0x551
    byte_start: 0
    byte_length: 1
    scale: 1.0
    offset: -40.0
    unit: degC
    confidence: high
"""
    )
    out = read_session_decoded(session, bindings_path=bindings_path)
    decoded = out[out["source"] == "decoded"]
    assert len(decoded) == 2
    assert decoded["channel"].unique().tolist() == ["ECM_COOLANT_TEMP"]
    # Raw byte values (decode emits raw, not scaled)
    assert sorted(decoded["value"].tolist()) == [42.0, 55.0]


def test_read_session_decoded_falls_back_when_no_bindings_file(tmp_path: Path):
    session = _write_synthetic_parquet_session(tmp_path)
    missing = tmp_path / "nope.yaml"
    out = read_session_decoded(session, bindings_path=missing)
    # No decoded rows — just the plain read_session output
    assert (out["source"] == "decoded").sum() == 0
    assert (out["source"] == "can").sum() == 2


@pytest.mark.slow  # ~18 s: decodes a full real session against on-disk bindings
def test_read_session_decoded_against_real_bindings_file():
    """Smoke against the committed data/bindings.yaml, if present."""
    bindings_path = Path("data/bindings.yaml")
    sessions_root = Path("data/sessions")
    if not bindings_path.exists() or not sessions_root.exists():
        pytest.skip("no data/bindings.yaml or data/sessions in this checkout")
    # Find any session that has parquet chunks (offline-converted ones do).
    from src.preprocessing.loader import discover_sessions

    sessions = discover_sessions(sessions_root)
    if not sessions:
        pytest.skip("no Parquet-converted sessions available")
    out = read_session_decoded(sessions[0], bindings_path=bindings_path)
    # Should run without error and produce some decoded rows for bound IDs.
    # (Not asserting count — depends on which session and what's bound.)
    assert "source" in out.columns


# ---------------------------------------------------------------------------
# R04.14 — one bounded reader across channel-spelling eras
# ---------------------------------------------------------------------------


def _spelled_session(root: Path, spelling: str) -> Path:
    """A session whose raw CAN rows spell id 0x551 as ``spelling``, plus an
    unrelated id and an OBD row."""
    from src.storage.parquet_writer import write_records_to_parquet
    from src.telemetry import TelemetryRecord

    session = root / spelling.replace("0x", "hex-")
    records = []
    for i in range(5):
        t = 1_778_500_000.0 + i
        records.append(TelemetryRecord.can(t, 0x551, bytes([40 + i, 0]), channel=spelling))
        records.append(TelemetryRecord.can(t, 0x6F7, bytes([1, 2, 3])))
        records.append(TelemetryRecord.obd(t, "RPM", 700.0 + i, "rpm"))
    write_records_to_parquet(records, session, max_rows=10_000)
    return session


@pytest.mark.parametrize("spelling", ["0x551", "551", "0x0551", "0X551", "0551"])
def test_read_session_decoded_subset_reads_every_channel_spelling(
    tmp_path: Path, spelling: str
) -> None:
    from src.plausibility.offline import session_long
    from src.preprocessing.decode import read_session_decoded_subset

    bindings = _bindings(_sig(name="COOLANT", can_id=0x551), _sig(name="OTHER", can_id=0x6F7))
    session = _spelled_session(tmp_path, spelling)

    long, subset = read_session_decoded_subset(session, bindings=bindings, decoded={"COOLANT"})
    assert subset is not None and [s.name for s in subset.signals] == ["COOLANT"]
    can = long[long["source"] == "can"]
    # Only the wanted id's raw frames, under whatever spelling the era used.
    assert can["channel"].astype(str).unique().tolist() == [spelling]
    assert len(can) == 5
    assert (long["source"] == "obd").sum() == 5

    decoded = session_long(session, bindings=bindings, did_map=None, decoded={"COOLANT"})
    rows = decoded[decoded["source"] == "decoded"]
    assert rows["channel"].unique().tolist() == ["COOLANT"]
    assert rows["value_number"].tolist() == [40.0, 41.0, 42.0, 43.0, 44.0]
    assert "can" not in set(decoded["source"])


@pytest.mark.parametrize("spelling", ["0x551", "551", "0x0551", "0551"])
def test_both_bounded_readers_decode_an_old_spelling_session_alike(
    tmp_path: Path, spelling: str
) -> None:
    """R04.14: the plausibility reader and the behavior-validation evaluation
    frame both read through ``read_session_decoded_subset``, so an era that
    spelled the id without ``0x`` decodes in both. ``build_evaluation_frame``
    pushed ``0x{id:X}`` down itself and got no ``decoded:`` values from it."""
    from src.analysis.behavior_validation import build_evaluation_frame
    from src.behavior.behavior_models import BehaviorModels
    from src.plausibility.offline import session_long

    bindings_path = tmp_path / "bindings.yaml"
    bindings_path.write_text(
        """
schema_version: 1
signals:
  - name: ECM_COOLANT_TEMP
    module: ECM
    can_id: 0x551
    byte_start: 0
    byte_length: 1
    confidence: high
  - name: OTHER
    module: ECM
    can_id: 0x6F7
    byte_start: 0
    byte_length: 1
    confidence: high
"""
    )
    session = _spelled_session(tmp_path, spelling)
    wanted = {"ECM_COOLANT_TEMP"}

    plausibility = session_long(
        session, bindings=load_bindings(bindings_path), did_map=None, decoded=wanted
    )
    decoded = plausibility[plausibility["source"] == "decoded"]
    assert decoded["channel"].unique().tolist() == ["ECM_COOLANT_TEMP"]

    frame = build_evaluation_frame(
        [session],
        example_config(Path("canary.example.yaml")),
        BehaviorModels(schema_version=1),
        bindings_path=bindings_path,
        dids_path=tmp_path / "absent-dids.yaml",
    )
    column = "decoded:ECM_COOLANT_TEMP"
    assert column in frame.columns
    assert "decoded:OTHER" not in frame.columns
    assert frame[column].dropna().tolist() == decoded["value_number"].tolist()


def test_read_session_decoded_subset_without_wanted_signals_reads_no_can(tmp_path: Path) -> None:
    from src.preprocessing.decode import read_session_decoded_subset

    session = _spelled_session(tmp_path, "0x551")
    long, subset = read_session_decoded_subset(session, bindings=None, decoded={"COOLANT"})
    assert subset is None
    assert "can" not in set(long["source"])


# ---------------------------------------------------------------------------
# R04.4 — one byte-slice rule
# ---------------------------------------------------------------------------


def test_decode_signal_column_matches_decode_raw() -> None:
    from src.decode.bindings import decode_raw
    from src.preprocessing.decode import decode_signal_column

    signal = _sig(byte_start=1, byte_length=2, byte_order="little")
    payloads = pd.Series([bytes([0, 1, 2]), bytearray([9, 255, 1]), bytes([0, 1]), None])
    out = decode_signal_column(payloads, signal)
    assert out.dtype == np.float64
    assert out[0] == decode_raw(signal, bytes([0, 1, 2])) == 0x0201
    assert out[1] == 0x01FF
    assert np.isnan(out[2]) and np.isnan(out[3])


# ---------------------------------------------------------------------------
# A04.6 — a sentinel is a state, not a number (live/storage vs batch parity)
# ---------------------------------------------------------------------------

_ASLEEP = "wheel sensor not transmitting"


def _sentinel_bindings(*, with_sentinel: bool = True) -> Bindings:
    extra = {"sentinel_raw": 0, "sentinel_meaning": _ASLEEP} if with_sentinel else {}
    return _bindings(
        _sig(name="TIRE_A", can_id=0x385, byte_start=2, scale=0.25, unit="psi", **extra),
        _sig(name="NEIGHBOUR", can_id=0x385, byte_start=3),
    )


def _sentinel_frames() -> list[tuple[float, int, bytes]]:
    # byte 2 asleep (0), awake (148), asleep, awake; byte 3 is always a number,
    # including 0, which is a value for a binding with no sentinel.
    return [
        (1000.0, 0x385, bytes([0, 0, 0, 7, 0, 0, 0, 0])),
        (1000.1, 0x385, bytes([0, 0, 148, 0, 0, 0, 0, 0])),
        (1000.2, 0x385, bytes([0, 0, 0, 9, 0, 0, 0, 0])),
        (1000.3, 0x385, bytes([0, 0, 150, 11, 0, 0, 0, 0])),
    ]


def _rows_as_tuples(frame: pd.DataFrame) -> set[tuple[float, str, object]]:
    decoded = frame[frame["source"] == "decoded"]
    out: set[tuple[float, str, object]] = set()
    for row in decoded.itertuples():
        value = row.value_text if isinstance(row.value_text, str) else float(row.value_number)
        out.add((round(pd.Timestamp(row.timestamp).timestamp(), 3), str(row.channel), value))
    return out


def test_a_sentinel_decodes_to_the_same_state_rows_on_the_live_and_batch_paths() -> None:
    from src.decode.frame_decoder import FrameDecoder
    from src.telemetry import TelemetryRecord

    bindings = _sentinel_bindings()
    rows = _sentinel_frames()
    decoder = FrameDecoder.from_bindings(bindings)
    live = {
        (round(r.timestamp.timestamp(), 3), r.channel, r.value)
        for ts, can_id, payload in rows
        for r in decoder.decode(TelemetryRecord.can(ts, can_id, payload))
    }
    batch = _rows_as_tuples(decode_can_rows(_can_long_frame(rows), bindings))
    assert live == batch
    assert {r for r in live if r[1] == "TIRE_A_SENTINEL"} == {
        (1000.0, "TIRE_A_SENTINEL", _ASLEEP),
        (1000.2, "TIRE_A_SENTINEL", _ASLEEP),
    }
    # The sentinel word never becomes a number, anywhere.
    assert {r[2] for r in live if r[1] == "TIRE_A"} == {148.0, 150.0}
    # Zero is still a value for a binding that declares no sentinel.
    assert (1000.1, "NEIGHBOUR", 0.0) in live


def test_non_sentinel_values_decode_exactly_as_without_the_declaration() -> None:
    frame = _can_long_frame(_sentinel_frames())
    with_sentinel = decode_can_rows(frame, _sentinel_bindings())
    without = decode_can_rows(frame, _sentinel_bindings(with_sentinel=False))
    numeric = ~with_sentinel["channel"].str.endswith("_SENTINEL")
    kept = with_sentinel[numeric & (with_sentinel["source"] == "decoded")].reset_index(drop=True)
    plain = without[without["source"] == "decoded"]
    plain = plain[~((plain["channel"] == "TIRE_A") & (plain["value_number"] == 0))]
    # ``value`` coalesces text and numbers once state rows are present (the
    # loader's long frame is object-valued anyway); the values are identical.
    pd.testing.assert_frame_equal(kept, plain.reset_index(drop=True), check_dtype=False)
    assert kept["value"].astype(float).tolist() == plain["value"].tolist()


def test_a_sentinel_column_is_a_text_state_and_the_number_column_skips_it() -> None:
    wide = to_wide(decode_can_rows(_can_long_frame(_sentinel_frames()), _sentinel_bindings()))
    number = wide[decoded_channel("TIRE_A")]
    state = wide[decoded_channel("TIRE_A_SENTINEL")]
    assert pd.api.types.is_numeric_dtype(number)
    assert not pd.api.types.is_numeric_dtype(state)
    assert number.dropna().tolist() == [148.0, 150.0]
    assert state.dropna().tolist() == [_ASLEEP, _ASLEEP]


def test_stored_sentinel_rows_count_as_stored() -> None:
    """A session the sensor never woke in stored only ``_SENTINEL`` rows; the
    batch decode replaces them (P02.8: decode always) and never adds the
    signal beside them."""
    frame = _can_long_frame(_sentinel_frames()[:1])
    stored = decode_can_rows(frame, _sentinel_bindings())
    assert set(stored.loc[stored["source"] == "decoded", "channel"]) == {
        "TIRE_A_SENTINEL",
        "NEIGHBOUR",
    }
    again = decode_can_rows(stored, _sentinel_bindings())
    assert len(again) == len(stored)


def test_decode_signal_column_masks_the_sentinel_and_the_mask_finds_it() -> None:
    from src.preprocessing.decode import decode_sentinel_mask, decode_signal_column

    signal = _sentinel_bindings().signals[0]
    payloads = pd.Series([p for _, _, p in _sentinel_frames()] + [b"\x00"])
    values = decode_signal_column(payloads, signal)
    assert np.isnan(values[[0, 2, 4]]).all() and values[[1, 3]].tolist() == [148.0, 150.0]
    assert decode_sentinel_mask(payloads, signal).tolist() == [True, False, True, False, False]


# ---------------------------------------------------------------------------
# P02.8 — the decode on read is the storage decoder's rule, exactly
# ---------------------------------------------------------------------------


def test_the_vectorized_slice_matches_the_scalar_rule() -> None:
    """``_raw_column`` slices a whole column at once; every value must be the
    one :func:`src.decode.slices.slice_raw` gives, for every order, width and
    offset, with short, empty, missing and ``bytearray`` payloads mixed in."""
    from src.decode.bindings import decode_raw
    from src.preprocessing.decode import _raw_column

    rng = np.random.default_rng(20260926)
    payloads: list[object] = []
    for _ in range(4_000):
        kind = rng.integers(0, 10)
        if kind == 0:
            payloads.append(None)
        elif kind == 1:
            payloads.append(b"")
        else:
            length = int(rng.choice([1, 2, 3, 5, 8, 8, 8, 12]))
            raw = bytes(rng.integers(0, 256, size=length, dtype=np.uint8))
            payloads.append(bytearray(raw) if kind == 2 else raw)
    column = pd.Series(payloads, dtype=object)
    for order in ("big", "little"):
        for width in (1, 2, 3, 4, 6, 8):
            for start in (0, 1, 3):
                signal = _sig(byte_start=start, byte_length=width, byte_order=order)
                got = _raw_column(column, signal)
                for value, payload in zip(got, payloads, strict=True):
                    expected = (
                        decode_raw(signal, bytes(payload))
                        if isinstance(payload, (bytes, bytearray))
                        else None
                    )
                    if expected is None:
                        assert np.isnan(value)
                    else:
                        assert value == float(expected)


def test_decode_on_read_writes_what_the_storage_decoder_wrote(tmp_path: Path) -> None:
    """Rows, values, units, metadata, dtypes and order: each frame's rows follow
    it, in catalog signal order; the sentinel word is a state row; a payload
    too short for a slice yields no row (not a NaN one); stored rows are
    dropped and never read. Both sides go through Parquet, as a session does."""
    from src.decode.frame_decoder import FrameDecoder
    from src.preprocessing.decode import decode_on_read
    from src.storage.parquet_writer import write_records_to_parquet
    from src.telemetry import TelemetryRecord

    def through_parquet(rows: list[TelemetryRecord], name: str) -> pd.DataFrame:
        (path,) = write_records_to_parquet(rows, tmp_path / name)
        return pd.read_parquet(path)

    bindings = _sentinel_bindings()
    records = [TelemetryRecord.can(ts, can_id, raw) for ts, can_id, raw in _sentinel_frames()]
    records.append(TelemetryRecord.can(1000.4, 0x385, bytes([0, 0, 21])))  # too short for byte 3
    stale = TelemetryRecord.decoded(records[1].timestamp, "TIRE_A", 1.0, "psi")
    decoder = FrameDecoder.from_bindings(bindings)
    expected = [row for record in records for row in (record, *decoder.decode(record))]

    stored = through_parquet([*records[:2], stale, *records[2:]], "stored")
    got = decode_on_read(stored, bindings)
    pd.testing.assert_frame_equal(got, through_parquet(expected, "written"), check_exact=True)
    assert decode_on_read(stored, None)["source"].tolist() == ["can"] * len(records)
