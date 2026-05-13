"""Tests for the optional DBC decode hook (Task 1.8)."""

from __future__ import annotations

from pathlib import Path

import cantools
import pandas as pd
import pytest

from src.decode.dbc import (
    DBC_SOURCE,
    dbc_channel,
    decode_can_rows_dbc,
    load_dbc_database,
    load_dbc_dir,
)

# ---------------------------------------------------------------------------
# Fixture: a tiny in-memory DBC describing two messages
# ---------------------------------------------------------------------------


DBC_TEXT = """VERSION ""

NS_ :

BS_:

BU_:

BO_ 1361 ECM_1F9: 8 ECM
 SG_ RPM : 16|16@0+ (0.125,0) [0|8000] "rpm" Vector__XXX

BO_ 1361 ECM_DUMMY: 8 ECM
 SG_ RPM : 16|16@0+ (0.125,0) [0|8000] "rpm" Vector__XXX

BO_ 1361 ECM_551: 8 ECM
 SG_ COOLANT_TEMP : 0|8@1+ (1,-40) [-40|215] "degC" Vector__XXX
"""

# The minimal valid DBC: two distinct messages by frame ID
SINGLE_DBC = """VERSION ""

NS_ :

BS_:

BU_:

BO_ 1361 ECM_1F9: 8 ECM
 SG_ RPM : 16|16@0+ (0.125,0) [0|8000] "rpm" Vector__XXX
"""

COOLANT_DBC = """VERSION ""

NS_ :

BS_:

BU_:

BO_ 1361 ECM_551: 8 ECM
 SG_ COOLANT_TEMP : 0|8@1+ (1,-40) [-40|215] "degC" Vector__XXX
"""


@pytest.fixture
def dbc_db():
    return cantools.database.load_string(SINGLE_DBC, database_format="dbc")


# ---------------------------------------------------------------------------
# Loaders
# ---------------------------------------------------------------------------


def test_load_dbc_database_returns_known_messages(tmp_path: Path) -> None:
    path = tmp_path / "x.dbc"
    path.write_text(SINGLE_DBC)
    db = load_dbc_database(path)
    assert [m.name for m in db.messages] == ["ECM_1F9"]


def test_load_dbc_database_missing_raises(tmp_path: Path) -> None:
    with pytest.raises(FileNotFoundError):
        load_dbc_database(tmp_path / "nope.dbc")


def test_load_dbc_dir_returns_none_when_empty(tmp_path: Path) -> None:
    assert load_dbc_dir(tmp_path) is None


def test_load_dbc_dir_returns_none_when_missing(tmp_path: Path) -> None:
    assert load_dbc_dir(tmp_path / "missing") is None


def test_load_dbc_dir_merges_multiple_files(tmp_path: Path) -> None:
    # Two files, two different frame IDs — cantools will refuse identical IDs
    # via add_dbc_file; use distinct messages.
    a = tmp_path / "a.dbc"
    a.write_text(SINGLE_DBC)  # frame_id 1361 (0x551 is also 1361 — collision)
    # Use a different ID for the second file
    b_text = SINGLE_DBC.replace("1361", "1785").replace("ECM_1F9", "ECM_551")
    b = tmp_path / "b.dbc"
    b.write_text(b_text)
    db = load_dbc_dir(tmp_path)
    assert db is not None
    names = sorted(m.name for m in db.messages)
    assert names == ["ECM_1F9", "ECM_551"]


# ---------------------------------------------------------------------------
# decode_can_rows_dbc
# ---------------------------------------------------------------------------


def test_dbc_channel_format() -> None:
    assert dbc_channel("RPM") == "dbc:RPM"


def _long_can_row(channel: str, payload: bytes, ts: float = 1.0) -> dict:
    return {
        "timestamp": pd.to_datetime(ts, unit="s", utc=True),
        "source": "can",
        "channel": channel,
        "value_number": None,
        "value_text": None,
        "value_bool": None,
        "unit": "",
        "payload": payload,
        "metadata_json": None,
        "session": "s1",
        "session_path": "/tmp/s1",
        "source_channel": f"can:{channel}",
        "value": None,
    }


def test_decode_can_rows_dbc_appends_signal_rows(dbc_db) -> None:
    # DBC big-endian / Motorola bit numbering is fiddly; round-trip
    # through cantools.encode so the test doesn't have to encode the
    # signal layout itself.
    message = dbc_db.get_message_by_frame_id(0x551)
    payload = message.encode({"RPM": 700.0}, padding=True)
    long = pd.DataFrame([_long_can_row("551", payload)])
    out = decode_can_rows_dbc(long, dbc_db)
    rows = out[out["source"] == DBC_SOURCE]
    assert len(rows) == 1
    assert rows.iloc[0]["channel"] == "RPM"
    assert rows.iloc[0]["value_number"] == pytest.approx(700.0, abs=0.5)


def test_decode_can_rows_dbc_skips_unknown_can_ids(dbc_db) -> None:
    long = pd.DataFrame([_long_can_row("999", bytes(8))])
    out = decode_can_rows_dbc(long, dbc_db)
    assert (out["source"] == DBC_SOURCE).sum() == 0
    # original rows are preserved
    assert len(out) == 1


def test_decode_can_rows_dbc_handles_empty_frame() -> None:
    db = cantools.database.load_string(SINGLE_DBC, database_format="dbc")
    out = decode_can_rows_dbc(pd.DataFrame(), db)
    assert out.empty


def test_decode_can_rows_dbc_handles_too_short_payload(dbc_db) -> None:
    # 0x551 expects 8 bytes; give 4 — cantools should reject and we skip.
    long = pd.DataFrame([_long_can_row("551", b"\x00\x00\x00\x00")])
    out = decode_can_rows_dbc(long, dbc_db)
    assert (out["source"] == DBC_SOURCE).sum() == 0
    # original frame still there
    assert len(out) == 1


def test_decode_can_rows_dbc_passes_through_when_no_can_rows(dbc_db) -> None:
    long = pd.DataFrame(
        [
            {
                "timestamp": pd.to_datetime(1.0, unit="s", utc=True),
                "source": "obd",
                "channel": "RPM",
                "value_number": 700.0,
                "payload": None,
            }
        ]
    )
    out = decode_can_rows_dbc(long, dbc_db)
    # untouched
    assert len(out) == 1
    assert (out["source"] == DBC_SOURCE).sum() == 0


def test_decode_can_rows_dbc_preserves_unknown_frames_alongside_decoded(dbc_db) -> None:
    rows = [
        _long_can_row("551", bytes([0, 0, 0x15, 0xE0, 0, 0, 0, 0]), ts=1.0),
        _long_can_row("999", bytes(8), ts=2.0),  # unknown — pass through
    ]
    long = pd.DataFrame(rows)
    out = decode_can_rows_dbc(long, dbc_db)
    by_source = out["source"].value_counts().to_dict()
    assert by_source["can"] == 2  # both originals preserved
    assert by_source[DBC_SOURCE] == 1  # one decoded record added
