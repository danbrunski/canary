"""R23.5 / R23.6: the faster hot paths return what the slow ones returned.

Each test keeps the *old* implementation beside the new one (the ``iterrows``
loops, ``max()`` over the deque) and compares them on data built to hit every
branch. The same comparison was also run at ``HEAD`` against real sessions under
``~/canary-data`` (recorded in the R23 checklist); these are the part that can
run anywhere.
"""

from __future__ import annotations

import pickle
import random
from collections import deque
from dataclasses import FrozenInstanceError, replace
from datetime import UTC, datetime, timedelta, timezone
from pathlib import Path
from typing import Any, cast
from zoneinfo import ZoneInfo

import numpy as np
import pandas as pd
import pytest

from src.can_ids import parse_can_channel
from src.collection import replay
from src.decode.dids import (
    DID_SOURCE,
    FOREIGN_DID_SOURCES,
    DidMap,
    decode_did_rows,
    decode_raw_did_reading,
    did_entries_by_hex,
    load_repo_did_map,
)
from src.storage.broadcast_storage import (
    _RECENT_INTERARRIVALS,
    CanChangeFilter,
    _RecentGaps,
    is_diagnostic_can_id,
)
from src.telemetry import TelemetryRecord

# --- _RecentGaps: a running maximum over a sliding window -----------------


@pytest.mark.parametrize("size", [1, 2, 3, 16])
def test_the_running_maximum_equals_max_over_the_deque_on_random_sequences(size: int) -> None:
    rng = random.Random(size)
    for trial in range(300):
        window: deque[float] = deque(maxlen=size)
        gaps = _RecentGaps(size)
        assert gaps.longest() == 0.0
        # Few distinct values make ties; sorted runs make the maximum leave the
        # window on every step, the case a naive running maximum gets wrong.
        pool = [
            rng.choice([0.01, 0.02, 0.5, 3.0]) if trial % 3 else rng.random() for _ in range(80)
        ]
        if trial % 5 == 0:
            pool.sort(reverse=True)
        elif trial % 5 == 1:
            pool.sort()
        for gap in pool:
            window.append(gap)
            gaps.append(gap)
            assert gaps.longest() == max(window)


def test_the_maximum_leaving_the_window_hands_over_to_the_next_largest() -> None:
    gaps = _RecentGaps(3)
    for gap in (9.0, 1.0, 2.0):
        gaps.append(gap)
    assert gaps.longest() == 9.0
    gaps.append(0.5)  # 9.0 is now four appends old
    assert gaps.longest() == 2.0
    gaps.append(0.1)
    assert gaps.longest() == 2.0
    gaps.append(0.1)  # 2.0 leaves
    assert gaps.longest() == 0.5


def test_the_monotonic_window_stays_bounded() -> None:
    gaps = _RecentGaps(16)
    for i in range(1000):
        gaps.append(float(1000 - i))  # strictly falling: the worst case for the deque
    assert len(gaps._window) <= 16


# --- CanChangeFilter against the pre-R23.6 decision rule ------------------


class _OldFilter(CanChangeFilter):
    """``keep`` as it was: ``max()`` over a 16-deque, the channel parsed for ids."""

    def keep(
        self,
        channel: str,
        payload: bytes | bytearray | None,
        seconds: float,
        *,
        event: bool = False,
        can_id: int | None = None,
    ) -> bool:
        if payload is None or event or is_diagnostic_can_id(channel):
            self.kept += 1
            return True
        data = bytes(payload)
        previous = self._last.get(channel)
        seen = self._seen.get(channel)
        self._seen[channel] = seconds
        recent: Any = self._old_recent.setdefault(channel, deque(maxlen=_RECENT_INTERARRIVALS))
        if seen is not None and seen <= seconds and seconds - seen <= self.stale_timeout_seconds:
            recent.append(seconds - seen)
        interarrival = max(recent) if recent else 0.0
        age = seconds - previous[1] if previous is not None else 0.0
        if (
            previous is None
            or previous[0] != data
            or age >= self._rewrite_after
            or age + interarrival >= self.stale_timeout_seconds
            or seconds < previous[1]
        ):
            self._last[channel] = (data, seconds)
            self.kept += 1
            return True
        self.suppressed += 1
        return False

    def __post_init__(self) -> None:
        super().__post_init__()
        self._old_recent: dict[str, deque[float]] = {}


def _stream(seed: int, n: int = 6000) -> list[tuple[str, bytes | None, float, bool]]:
    rng = random.Random(seed)
    ids = ["0x1F9", "0x551", "0x7FF", "0x7E8", "0x7DF", "0x18DAF110", "0x18FEF100", "0x4"]
    now = 1_000_000.0
    out = []
    for _ in range(n):
        now += rng.choice([0.01, 0.01, 0.02, 0.5, 4.0, 11.0, -3.0])
        payload = None if rng.random() < 0.02 else bytes([rng.randrange(3)] * rng.randrange(1, 4))
        out.append((rng.choice(ids), payload, now, rng.random() < 0.05))
    return out


def _can_record(
    channel: str, payload: bytes | None, seconds: float, event: bool, *, with_id: bool
) -> TelemetryRecord:
    meta: dict[str, Any] = {}
    if with_id:
        meta = {"can_id": parse_can_channel(channel), "dlc": len(payload or b"")}
    if event:
        meta["frame_kind"] = "error"
    return TelemetryRecord(
        timestamp=datetime.fromtimestamp(seconds, tz=UTC),
        source="can",
        channel=channel,
        payload=payload,
        metadata=meta,
    )


@pytest.mark.parametrize("seed", range(4))
@pytest.mark.parametrize("with_id", [True, False], ids=["can_id in metadata", "channel parsed"])
def test_keep_record_decides_as_the_old_filter_did(seed: int, with_id: bool) -> None:
    new, old = CanChangeFilter(10.0), _OldFilter(10.0)
    for channel, payload, seconds, event in _stream(seed):
        record = _can_record(channel, payload, seconds, event, with_id=with_id)
        assert new.keep_record(record) == old.keep_record(record), (channel, seconds)
    assert (new.kept, new.suppressed) == (old.kept, old.suppressed)
    assert new._last == old._last


def test_a_malformed_can_id_falls_back_to_the_channel_name() -> None:
    """A ``bool`` or a string in ``metadata["can_id"]`` is not an id."""
    for bad in (True, "0x7E8", 3.0, None):
        record = TelemetryRecord(
            timestamp=datetime.fromtimestamp(5.0, tz=UTC),
            source="can",
            channel="0x7E8",
            payload=b"\x01",
            metadata={"can_id": bad},
        )
        flt = CanChangeFilter(10.0)
        assert flt.keep_record(record) and flt.keep_record(record)  # diagnostic: always kept
        assert flt._last == {}


# --- TelemetryRecord ------------------------------------------------------


def _record(**kw: Any) -> TelemetryRecord:
    base: dict[str, Any] = {
        "timestamp": datetime(2026, 1, 2, 3, 4, 5, 678, tzinfo=UTC),
        "source": "can",
        "channel": "0x1F9",
        "payload": b"\x01\x02",
        "metadata": {"can_id": 0x1F9},
    }
    return TelemetryRecord(**{**base, **kw})


def test_a_record_has_no_instance_dict_and_stays_frozen() -> None:
    record = _record()
    assert not hasattr(record, "__dict__")
    # CPython raises TypeError here for a frozen slotted dataclass (its generated
    # ``__setattr__`` calls ``super()`` on the pre-slots class); either way it refuses.
    with pytest.raises((AttributeError, TypeError)):
        record.extra = 1  # type: ignore[attr-defined]
    with pytest.raises(FrozenInstanceError):
        record.channel = "0x2"  # type: ignore[misc]


def test_a_record_pickles_copies_and_replaces_by_field() -> None:
    record = _record(value=1.5, unit="V")
    assert pickle.loads(pickle.dumps(record)) == record
    assert replace(record, channel="0x2A").channel == "0x2A"
    assert replace(record, channel="0x2A").timestamp == record.timestamp


@pytest.mark.parametrize(
    "given",
    [
        datetime(2026, 1, 2, 3, 4, 5, tzinfo=UTC),
        datetime(2026, 1, 2, 3, 4, 5),
        datetime(2026, 1, 2, 3, 4, 5, tzinfo=timezone(timedelta(hours=-7))),
        datetime(2026, 1, 2, 3, 4, 5, tzinfo=timezone(timedelta(0))),
        datetime(2026, 1, 2, 3, 4, 5, tzinfo=ZoneInfo("America/Denver")),
    ],
    ids=["utc", "naive", "offset", "zero-offset", "zoneinfo"],
)
def test_the_timestamp_is_normalised_to_utc_exactly_as_before(given: datetime) -> None:
    expected = given.replace(tzinfo=UTC) if given.tzinfo is None else given.astimezone(UTC)
    record = _record(timestamp=given)
    assert record.timestamp == expected
    assert record.timestamp.utcoffset() == timedelta(0)
    assert record.timestamp.tzinfo is not None
    if given.tzinfo is UTC:
        assert record.timestamp is given  # nothing was rebuilt


# --- decode_did_rows and replay._read_chunk against the iterrows loops ----


def _old_decode_did_rows(long_frame: pd.DataFrame, did_map: DidMap) -> pd.DataFrame:
    """``decode_did_rows`` as it was before R23.5 (``iterrows``, one decode per row)."""
    if long_frame.empty or "channel" not in long_frame.columns:
        return long_frame
    raw = long_frame[long_frame["channel"].astype("string").str.startswith("DID_")]
    if "source" in raw.columns:
        raw = raw[~raw["source"].isin(FOREIGN_DID_SOURCES)]
    if raw.empty:
        return long_frame
    by_hex = did_entries_by_hex(did_map)
    if not by_hex:
        return long_frame
    has_session_path = "session_path" in long_frame.columns
    records: list[dict[str, Any]] = []
    for _, row in raw.iterrows():
        hit = decode_raw_did_reading(
            by_hex, str(row["channel"]), row.get("value_text"), source=str(row.get("source", ""))
        )
        if hit is None:
            continue
        entry, numeric = hit.entry, hit.value
        records.append(
            {
                "timestamp": row["timestamp"],
                "source": DID_SOURCE,
                "channel": entry.name,
                "value_number": numeric,
                "value_text": None,
                "value_bool": None,
                "unit": hit.unit,
                "payload": None,
                "metadata_json": None,
                "session": row.get("session"),
                "session_path": row.get("session_path") if has_session_path else None,
                "source_channel": f"{DID_SOURCE}:{entry.name}",
                "value": numeric,
            }
        )
    if not records:
        return long_frame
    decoded = pd.DataFrame(records)
    for col in long_frame.columns:
        if col not in decoded.columns:
            decoded[col] = None
    decoded = decoded[list(long_frame.columns)]
    combined = pd.concat([long_frame, decoded], ignore_index=True)
    return combined.sort_values(["timestamp", "source", "channel"], kind="stable").reset_index(
        drop=True
    )


def _did_frame(seed: int, *, session: bool, session_path: bool) -> pd.DataFrame:
    did_map = load_repo_did_map()
    assert did_map is not None
    hexes = [f"{entry.did:04X}" for entry in did_map.dids[:12]]
    rng = random.Random(seed)
    start = datetime(2026, 5, 1, tzinfo=UTC)
    rows: list[dict[str, Any]] = []
    for _ in range(1500):
        kind = rng.random()
        if kind < 0.7:
            channel = f"DID_{rng.choice(hexes)}"
            text: Any = rng.choice(["0000", "0123", "FFFF", "00112233", "01", "zz", "", None])
            source = "obd"
        elif kind < 0.8:
            channel, text, source = f"DID_{rng.choice(hexes)}", "0123", "kline"
        elif kind < 0.9:
            channel, text, source = "DID_ABCD", "0123", "obd"  # not in the catalog
        else:
            channel, text, source = "RPM", None, "obd"
        rows.append(
            {
                "timestamp": start + timedelta(milliseconds=rng.randrange(400) * 10),
                "source": source,
                "channel": channel,
                "value_number": 1.0 if channel == "RPM" else np.nan,
                "value_text": text,
                "value_bool": None,
                "unit": "",
                "payload": None,
                "metadata_json": None,
            }
        )
    frame = pd.DataFrame(rows)
    frame["timestamp"] = pd.to_datetime(frame["timestamp"], utc=True)
    for col in ("source", "channel", "value_text", "unit", "metadata_json"):
        frame[col] = frame[col].astype("string")
    if session:
        frame["session"] = [None if i % 9 == 0 else f"s{i % 3}" for i in range(len(frame))]
    if session_path:
        frame["session_path"] = "/data/s"
    return frame


@pytest.mark.parametrize("session", [False, True])
@pytest.mark.parametrize("session_path", [False, True])
def test_decode_did_rows_matches_the_iterrows_loop(session: bool, session_path: bool) -> None:
    did_map = load_repo_did_map()
    assert did_map is not None
    for seed in range(3):
        frame = _did_frame(seed, session=session, session_path=session_path)
        before = frame.copy(deep=True)
        got = decode_did_rows(frame, did_map)
        want = _old_decode_did_rows(frame, did_map)
        assert len(want) > len(frame), "the fixture decodes nothing, so it proves nothing"
        pd.testing.assert_frame_equal(got, want, check_exact=True)
        pd.testing.assert_frame_equal(frame, before)  # the input is not mutated


def test_decode_did_rows_decodes_each_distinct_reading_once(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import src.decode.dids as dids

    did_map = load_repo_did_map()
    assert did_map is not None
    frame = _did_frame(0, session=False, session_path=False)
    calls: list[tuple[str, object]] = []
    real = dids.decode_raw_did_reading

    def counting(by_hex: Any, channel: str, payload_hex: object, *, source: str) -> Any:
        calls.append((channel, payload_hex))
        return real(by_hex, channel, payload_hex, source=source)

    monkeypatch.setattr(dids, "decode_raw_did_reading", counting)
    decode_did_rows(frame, did_map)
    raw = frame[frame["channel"].str.startswith("DID_") & (frame["source"] != "kline")]
    distinct = {
        (c, t) for c, t in zip(raw["channel"], raw["value_text"], strict=True) if isinstance(t, str)
    }
    assert sorted(map(str, calls)) == sorted(map(str, distinct))
    assert len(calls) < len(raw) / 5


def _old_read_chunk(
    chunk: Path, skip: frozenset[str]
) -> tuple[list[tuple[float, dict[str, Any]]], int]:
    """``_read_chunk`` as it was before R23.5, on the same ``_value``/``_payload``/``_metadata``."""
    df = pd.read_parquet(chunk)
    skipped = 0
    if skip:
        keep = ~df["source"].astype(str).isin(skip)
        skipped = int((~keep).sum())
        df = df[keep]
    df = df.sort_values("timestamp", kind="stable")
    ts = pd.to_datetime(df["timestamp"], utc=True)
    rows = []
    for (_, row), t in zip(df.iterrows(), ts, strict=True):
        unit = row.get("unit")
        rows.append(
            (
                float(t.timestamp()),
                {
                    "source": str(row["source"]),
                    "channel": str(row["channel"]),
                    "value": replay._value(row),
                    "unit": "" if unit is None or pd.isna(unit) else str(unit),
                    "payload": replay._payload(row),
                    "metadata": replay._metadata(row),
                },
            )
        )
    return rows, skipped


def _replay_chunk(path: Path, *, bool_dtype: Any, drop: tuple[str, ...] = ()) -> None:
    rng = random.Random(3)
    start = datetime(2026, 5, 1, tzinfo=UTC)
    rows = []
    for i in range(600):
        kind = i % 6
        rows.append(
            {
                "timestamp": start + timedelta(milliseconds=rng.randrange(300)),
                "source": ["can", "obd", "gps", "imu", "decoded", "obd"][kind],
                "channel": ["0x1F9", "RPM", "lat", "ax", "ECM_RPM", "DID_1208"][kind],
                "value_number": [np.nan, 800.0 + i, 1.5, np.nan, 0.0, np.nan][kind],
                "value_text": [None, None, None, None, None, "0123"][kind],
                "value_bool": [None, None, None, True, False, None][kind],
                "unit": [None, "rpm", "deg", "", "rpm", "unknown"][kind],
                "payload": [b"\x01\x02", None, None, None, None, None][kind],
                "metadata_json": [
                    '{"can_id": 505, "dlc": 2}',
                    None,
                    "not json",
                    "[1, 2]",
                    "",
                    '{"acquired_at": 1.5}',
                ][kind],
            }
        )
    frame = pd.DataFrame(rows)
    frame["value_bool"] = frame["value_bool"].astype(bool_dtype)
    for col in ("source", "channel", "value_text", "unit", "metadata_json"):
        frame[col] = frame[col].astype("string")
    frame.drop(columns=list(drop)).to_parquet(path)


@pytest.mark.parametrize("bool_dtype", ["object", "boolean"])
@pytest.mark.parametrize("skip", [frozenset(), frozenset({"can"}), frozenset({"can", "obd"})])
def test_read_chunk_matches_the_iterrows_loop(
    tmp_path: Path, bool_dtype: str, skip: frozenset[str]
) -> None:
    chunk = tmp_path / "telemetry-00000.parquet"
    _replay_chunk(chunk, bool_dtype=bool_dtype)
    got_rows, got_skipped = replay._read_chunk(chunk, cast(Any, skip))
    want_rows, want_skipped = _old_read_chunk(chunk, skip)
    assert got_skipped == want_skipped and len(got_rows) == len(want_rows) > 0
    for (t_got, f_got), (t_want, f_want) in zip(got_rows, want_rows, strict=True):
        assert t_got == t_want
        assert f_got.keys() == f_want.keys()
        for name in f_got:
            assert type(f_got[name]) is type(f_want[name]), name
            assert f_got[name] == f_want[name], name


def test_read_chunk_without_the_optional_columns_matches_too(tmp_path: Path) -> None:
    chunk = tmp_path / "telemetry-00001.parquet"
    _replay_chunk(
        chunk, bool_dtype="object", drop=("unit", "payload", "metadata_json", "value_bool")
    )
    got, _ = replay._read_chunk(chunk, frozenset())
    want, _ = _old_read_chunk(chunk, frozenset())
    assert got == want
