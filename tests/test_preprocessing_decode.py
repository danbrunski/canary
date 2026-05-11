"""Tests for decoding CAN payloads into named signal columns."""

from __future__ import annotations

import json
from pathlib import Path

import numpy as np
import pandas as pd
import pytest

from src.decode.bindings import Bindings, Signal
from src.preprocessing.decode import (
    DECODED_SOURCE,
    decode_can_rows,
    decoded_channel,
    read_session_decoded,
)
from src.preprocessing.loader import to_wide

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
