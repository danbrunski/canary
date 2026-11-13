"""Signed broadcast bindings decode two's-complement on every path.

The bindings schema had no ``signed`` flag, so the Xterra's torque bytes
(0x231 bytes 0/3, 0x29E byte 0: N·m = 3 x signed byte + 150) read 254 for -2
and every decode wrapped at 150 N·m, mid light cruise (F01.14). The flag now
lives on the shared :class:`~src.decode.slices.ByteSlice`, and each path that
slices a binding — the live frame decoder (``decode_raw``), the session decode
on read (the vectorized column), the SQL ``telemetry`` view, the broadcast
correlator and the DBC emit — must agree value for value.
"""

from __future__ import annotations

from collections.abc import Mapping
from pathlib import Path
from typing import Any, cast

import cantools
import duckdb
import numpy as np
import pandas as pd
import pyarrow as pa
import pyarrow.parquet as pq
import pytest
from cantools.database.can import Database as CanDatabase

from src.analysis.broadcast_correlate import ByteProbe
from src.decode import dids, slices
from src.decode.bindings import (
    Bindings,
    Signal,
    apply_scaling,
    decode_raw,
    is_trainable_binding,
    load_bindings,
)
from src.preprocessing.decode import _raw_column, decode_sentinel_mask, decode_signal_column
from src.storage.telemetry_query import view_sql
from tools.emit_dbc import build_database


def _signal(**overrides: object) -> Signal:
    fields: dict[str, object] = {
        "name": "PROBE",
        "module": "ECM",
        "can_id": 0x231,
        "byte_start": 0,
        "byte_length": 1,
        "signed": True,
        "confidence": "high",
    }
    fields.update(overrides)
    return Signal.model_validate(fields)


def _subclasses(cls: type) -> set[type]:
    found = set(cls.__subclasses__())
    for sub in list(found):
        found |= _subclasses(sub)
    return found


def test_every_byte_slice_model_declares_signed() -> None:
    """``ByteSlice.raw_from`` reads ``self.signed``; a model without the field
    would fail on its first decode, not at load."""
    models = _subclasses(slices.ByteSlice)
    assert {Signal, dids.DidEntry, dids.MemoryChannel} <= models
    for model in models:
        fields = getattr(model, "model_fields", {})
        assert "signed" in fields, f"{model.__name__} lacks the `signed` field"
        assert fields["signed"].default is False, model.__name__


def test_a_signed_binding_decodes_twos_complement() -> None:
    torque = _signal(scale=3.0, offset=150.0, unit="Nm")
    assert decode_raw(torque, bytes([0xFE])) == -2
    assert apply_scaling(torque, -2) == pytest.approx(144.0)
    assert decode_raw(torque.model_copy(update={"signed": False}), bytes([0xFE])) == 254


def test_a_signed_sentinel_is_stated_in_the_signed_range() -> None:
    _signal(sentinel_raw=-128, sentinel_meaning="no torque")
    with pytest.raises(ValueError, match="signed slice"):
        _signal(sentinel_raw=0x80, sentinel_meaning="no torque")


def _payloads(seed: int, count: int = 3_000) -> list[object]:
    rng = np.random.default_rng(seed)
    out: list[object] = []
    for _ in range(count):
        kind = rng.integers(0, 10)
        if kind == 0:
            out.append(None)
        elif kind == 1:
            out.append(b"")
        else:
            length = int(rng.choice([1, 2, 3, 5, 8, 8, 8, 12]))
            raw = bytes(rng.integers(0, 256, size=length, dtype=np.uint8))
            out.append(bytearray(raw) if kind == 2 else raw)
    return out


def test_the_vectorized_signed_slice_matches_the_scalar_rule() -> None:
    """Every width up to 8 bytes, both orders, with the top bit set and clear."""
    payloads = _payloads(20261112)
    column = pd.Series(payloads, dtype=object)
    for order in ("big", "little"):
        for width in (1, 2, 3, 4, 6, 8):
            for start in (0, 1, 3):
                signal = _signal(byte_start=start, byte_length=width, byte_order=order)
                got = _raw_column(column, signal)
                negative = 0
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
                        negative += expected < 0
                assert negative > 0  # the sign was exercised


def _session(tmp_path: Path, frames: list[tuple[int, bytes]]) -> Path:
    """One parquet chunk of CAN rows, in the stored telemetry layout."""
    session = tmp_path / "2026-11-12T120000"
    session.mkdir()
    n = len(frames)
    table = pa.table(
        {
            "timestamp": pa.array(
                pd.date_range("2026-11-12T12:00:00", periods=n, freq="10ms", tz="UTC")
            ),
            "source": ["can"] * n,
            "channel": [f"0x{can_id:03X}" for can_id, _ in frames],
            "value_number": pa.nulls(n, pa.float64()),
            "value_text": pa.nulls(n, pa.string()),
            "value_bool": pa.nulls(n, pa.bool_()),
            "unit": [""] * n,
            "payload": pa.array([data for _, data in frames], pa.binary()),
            "metadata_json": ["{}"] * n,
        }
    )
    path = session / "telemetry-00000.parquet"
    pq.write_table(table, path)  # type: ignore[no-untyped-call]
    return path


def test_the_sql_view_decodes_signed_bindings_as_the_column_decoder_does(
    tmp_path: Path,
) -> None:
    """The ``telemetry`` view's SQL slice, sentinel split included, gives the
    session decoder's values for signed 1-, 2- and 8-byte bindings."""
    signals = [
        _signal(name="S8", sentinel_raw=-128, sentinel_meaning="no torque"),
        _signal(name="S16LE", byte_start=1, byte_length=2, byte_order="little"),
        _signal(name="S64", byte_start=0, byte_length=8),
        _signal(name="U8", byte_start=0, signed=False),
    ]
    catalog = Bindings(schema_version=1, signals=signals)
    rng = np.random.default_rng(7)
    payloads = [bytes(rng.integers(0, 256, size=8, dtype=np.uint8)) for _ in range(400)]
    payloads += [bytes([0x80, 0, 0, 0, 0, 0, 0, 0]), bytes([0xFF] * 8), bytes([0x7F] + [0] * 7)]
    path = _session(tmp_path, [(0x231, p) for p in payloads])

    con = duckdb.connect()
    try:
        con.execute(view_sql([str(path)], catalog))
        rows = con.execute(
            "SELECT channel, value_number, value_text FROM telemetry "
            "WHERE source = 'decoded' ORDER BY timestamp"
        ).df()
    finally:
        con.close()
    column = pd.Series(payloads, dtype=object)
    for signal in signals:
        sql = rows.loc[rows.channel == signal.name, "value_number"].to_numpy()
        expected = decode_signal_column(column, signal)
        np.testing.assert_array_equal(sql, expected[~np.isnan(expected)])
    states = rows.loc[rows.channel == "S8_SENTINEL"]
    mask = decode_sentinel_mask(column, signals[0])
    assert len(states) == int(mask.sum()) >= 1
    assert set(states.value_text) == {"no torque"}
    assert rows.loc[rows.channel == "S64", "value_number"].min() < 0


def test_the_dbc_emits_a_signed_binding_signed(tmp_path: Path) -> None:
    bindings = tmp_path / "bindings.yaml"
    bindings.write_text(
        """\
schema_version: 1
signals:
  - name: TORQUE
    module: ECM
    can_id: 0x231
    byte_start: 0
    byte_length: 1
    signed: true
    scale: 3.0
    offset: 150.0
    unit: Nm
    confidence: high
  - name: NET
    module: ECM
    can_id: 0x231
    byte_start: 5
    byte_length: 2
    byte_order: little
    signed: true
    scale: 0.1
    unit: Nm
    confidence: medium
"""
    )
    db = cast(
        CanDatabase,
        cantools.database.load_string(
            build_database(bindings).as_dbc_string(), database_format="dbc"
        ),
    )
    frame = bytes([0xFE, 0, 0, 0, 0, 0x7A, 0xFC, 0])  # NET = 0xFC7A = -902
    theirs = cast(Mapping[str, Any], db.get_message_by_frame_id(0x231).decode(frame))
    for signal in load_bindings(bindings).signals:
        ours = apply_scaling(signal, decode_raw(signal, frame) or 0)
        assert theirs[signal.name] == pytest.approx(ours)
    assert theirs["TORQUE"] == pytest.approx(144.0)
    assert theirs["NET"] == pytest.approx(-90.2)


def test_the_broadcast_probe_carries_the_binding_sign() -> None:
    signal = _signal(byte_start=5, byte_length=2, byte_order="little")
    probe = ByteProbe.from_signal(signal)
    assert probe.signed
    frame = bytes([0, 0, 0, 0, 0, 0x7A, 0xFC, 0])
    assert probe.raw_from(frame) == decode_raw(signal, frame) == -902
    assert "signed" in probe.display


def test_the_xterra_torque_bytes_are_signed_and_replace_the_unsigned_reads() -> None:
    """The shipped catalog: the torque bytes decode continuously across the old
    wrap, the unsigned names stay for stored data but no longer train."""
    catalog = load_bindings(Path("data/bindings.yaml"))
    by_name = {s.name: s for s in catalog.signals}
    replaced = {
        "ECM_ENGINE_STATE_A": "ECM_TORQUE_ARBITRATED",
        "ECM_ENGINE_STATE_B": "ECM_TORQUE_DRIVER_DEMAND",
        "ECM_ENGINE_STATE_C": "VDC_TORQUE_29E",
        "ECM_THROTTLE_TRANSIENT": "ECM_TORQUE_NET_23D",
    }
    for old, new in replaced.items():
        assert by_name[old].superseded_by == new
        assert not is_trainable_binding(by_name[old])
        assert by_name[new].signed
    for name in ("ECM_TORQUE_ARBITRATED", "ECM_TORQUE_DRIVER_DEMAND", "VDC_TORQUE_29E"):
        torque = by_name[name]
        assert is_trainable_binding(torque)
        # 0xFF -> 0x00 is one count, 147 -> 150 N·m. Read unsigned it was
        # 255 -> 0, a 765 N·m jump in the middle of light cruise.
        frame = bytearray(8)
        frame[torque.byte_start] = 0xFF
        below = apply_scaling(torque, decode_raw(torque, bytes(frame)) or 0)
        frame[torque.byte_start] = 0x00
        above = apply_scaling(torque, decode_raw(torque, bytes(frame)) or 0)
        assert below == pytest.approx(147.0)
        assert above == pytest.approx(150.0)
