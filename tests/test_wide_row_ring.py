"""R27.3: the live ring buffer stores its rows by column, and the frame it
builds is exactly the ``pd.DataFrame(list_of_dicts)`` it replaced.

``LiveFeatureSource.prepare_tick`` used to build ``pd.DataFrame(list(deque))``
over ~181 row dicts on the event loop every tick (8-12 ms on the laptop at
197-308 real columns). :class:`~src.features.live.WideRowRing` keeps a float
block, a bool block and the few other values per row, and
:meth:`~src.features.live.WideRows.frame` builds the frame from them in the
feature worker. The contract is equality with the dict path, not "close": the
same columns in the same order, the same index, the same dtypes (a flag only
some held rows carry is ``object`` with NaN, a value is float64 with NaN), and
the same cells, compared with ``check_exact``. Each test builds both from the
same rows: a realistic drive through the source with a channel first seen
mid-buffer and eviction past ``maxlen``, start-context columns that vanish on a
model reload, and random row sequences.
"""

from __future__ import annotations

import math
import pickle
from collections import deque
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import numpy as np
import pandas as pd
import pytest

from src.behavior.behavior_models import (
    BehaviorModels,
    ColdStartRequirement,
    EngineStateRequirement,
    ServingEligibility,
    load_behavior_models,
)
from src.config import CanaryConfig
from src.decode.bindings import load_bindings
from src.features.live import LiveFeatureSource, WideRowRing
from src.telemetry import TelemetryRecord
from tests.configs import example_config

T0 = datetime(2026, 11, 12, 12, 0, tzinfo=UTC)
BEHAVIOR_MODELS = Path("data/nissan-xterra-2008/behavior_models.yaml")
#: 0x1F9 bytes 2-3 big-endian, 8 counts/rpm: a ``decoded:`` broadcast channel.
RPM_1500 = bytes([0x00, 0x00, 0x2E, 0xE0, 0x00, 0x00, 0x00, 0x00])


def _reference(rows: list[dict[str, Any]]) -> pd.DataFrame:
    """The pre-R27.3 build, verbatim."""
    return pd.DataFrame(rows)


def assert_same_frame(actual: pd.DataFrame, expected: pd.DataFrame) -> None:
    """Bit-equal, same dtypes, same column and index types."""
    pd.testing.assert_frame_equal(
        actual,
        expected,
        check_exact=True,
        check_dtype=True,
        check_index_type=True,
        check_column_type=True,
    )
    assert actual.dtypes.equals(expected.dtypes), (actual.dtypes, expected.dtypes)
    assert list(actual.columns) == list(expected.columns)
    assert type(actual.index) is type(expected.index)


class _ShadowedRing(WideRowRing):
    """The source's ring, also keeping the old ``deque`` of row dicts."""

    def __init__(self, capacity: int) -> None:
        super().__init__(capacity)
        self.dicts: deque[dict[str, Any]] = deque(maxlen=capacity)

    def append(self, row: dict[str, Any]) -> None:
        super().append(row)
        self.dicts.append(row)


def _models_with_a_cold_start_contract() -> BehaviorModels:
    """The shipped models, the first given a cold-start contract so every row
    carries the ``context:`` start columns (B01.5)."""
    shipped = load_behavior_models(BEHAVIOR_MODELS)
    first = shipped.models[0]
    eligibility = ServingEligibility(
        engine_state=EngineStateRequirement(state="engine_running"),
        cold_start=ColdStartRequirement(max_initial_coolant_c=40.0),
    )
    return shipped.model_copy(
        update={
            "models": [
                first.model_copy(update={"eligibility": eligibility}),
                *shipped.models[1:],
            ]
        }
    )


@pytest.fixture
def config() -> CanaryConfig:
    return example_config(Path("canary.example.yaml"))


#: The tick at which ``obd:MAF`` is first observed, mid-buffer.
MAF_FROM = 40
#: GPS appears later still, so two growth points are in the buffer at once.
GPS_FROM = 70
#: ``obd:COOLANT_TEMP`` stops answering here: NaN values, gap flags True.
COOLANT_STOPS = 120


def _drive_and_compare(source: LiveFeatureSource, ticks: int) -> dict[int, pd.DataFrame]:
    """Drive ``source`` tick by tick, asserting the frame against the dict path
    over the same rows on every tick near a growth or eviction point and every
    fourth tick between; return the reference frames by tick."""
    capacity = source._max_rows
    ring = _ShadowedRing(capacity)
    source._buffer = ring
    edges = [MAF_FROM, GPS_FROM, COOLANT_STOPS, capacity]
    edges += [MAF_FROM + capacity, GPS_FROM + capacity, ticks - 1]
    references: dict[int, pd.DataFrame] = {}
    for second in range(ticks):
        at = T0 + timedelta(seconds=second)
        stamp = at.timestamp()
        rpm = 0.0 if second < 3 else 900.0 + 7.0 * (second % 50)
        source.observe(TelemetryRecord.obd(stamp, "RPM", rpm, "rpm"))
        source.observe(TelemetryRecord.obd(stamp, "SPEED", float(second % 90), "km/h"))
        source.observe(TelemetryRecord.obd(stamp, "RUN_TIME", float(max(second - 3, 0)), "s"))
        if second < COOLANT_STOPS:
            source.observe(TelemetryRecord.obd(stamp, "COOLANT_TEMP", 20.0 + second / 4, "C"))
        if second >= MAF_FROM and second % 3:  # MAF arrives on two ticks in three
            source.observe(TelemetryRecord.obd(stamp, "MAF", 3.0 + second / 10, "g/s"))
        if second >= GPS_FROM:
            source.observe(TelemetryRecord.gps(stamp, "speed", float(second % 90), "km/h"))
        source.observe(TelemetryRecord.can(stamp, 0x1F9, RPM_1500))
        prepared = source.prepare_tick(at)
        assert prepared is not None
        near_edge = any(abs(second - edge) <= 2 for edge in edges)
        if second % 4 and not near_edge:
            continue
        expected = _reference(list(ring.dicts))
        expected["timestamp"] = pd.to_datetime(expected["timestamp"], utc=True)
        assert_same_frame(prepared.wide, expected)
        if near_edge:
            # What the worker reads: the tick after a pickle round trip.
            assert_same_frame(pickle.loads(pickle.dumps(prepared)).wide, expected)
        references[second] = expected
    return references


def test_live_source_frame_matches_the_dict_path_through_growth_and_eviction(
    config: CanaryConfig,
) -> None:
    source = LiveFeatureSource(
        config=config,
        bindings=load_bindings(Path("data/bindings.yaml")),
        behavior_models=_models_with_a_cold_start_contract(),
    )
    capacity = source._max_rows
    ticks = MAF_FROM + capacity + 20
    references = _drive_and_compare(source, ticks)

    # The cases the comparison must have covered, read off the dict path:
    maf_gap = "obd:MAF__gap"
    last = references[ticks - 1]
    assert len(last) == capacity, "the buffer evicted past maxlen"
    # A flag first seen mid-buffer is object (True/False/NaN) while older rows
    # lack it, sorted after every key of those rows...
    mid = references[MAF_FROM + 2]
    assert mid[maf_gap].dtype == object and mid[maf_gap].isna().any()
    assert list(mid.columns).index("obd:MAF") > list(mid.columns).index("obd:SPEED__arrived")
    # ...and bool, among its own family, once those rows are evicted.
    assert last[maf_gap].dtype == bool
    assert list(last.columns).index("obd:MAF") < list(last.columns).index("obd:RPM__gap")
    # A value first seen mid-buffer is float64 with NaN in the older rows.
    assert mid["obd:MAF"].dtype == np.float64 and mid["obd:MAF"].isna().any()
    # A channel gone quiet: NaN values, gap flags True, still bool.
    assert last["obd:COOLANT_TEMP"].isna().iloc[-1]
    assert last["obd:COOLANT_TEMP__gap"].dtype == bool
    assert bool(last["obd:COOLANT_TEMP__gap"].iloc[-1])
    # Start-context floats and the non-block values went through too.
    assert any(str(c).startswith("context:") for c in last.columns)
    assert str(last["timestamp"].dtype).startswith("datetime64")
    assert last["session"].iloc[0] == "live"


def test_columns_that_vanish_mid_buffer_and_return(config: CanaryConfig) -> None:
    """A model reload drops the start-context columns from new rows: they stay
    (NaN) while older rows carry them, vanish after eviction, and a key seen
    again lands back in its column."""
    capacity = 6
    ring = WideRowRing(capacity)
    dicts: deque[dict[str, Any]] = deque(maxlen=capacity)
    stamp = pd.Timestamp(T0)

    def row(i: int, *, context: bool, extra: bool = False) -> dict[str, Any]:
        out: dict[str, Any] = {
            "timestamp": stamp + pd.Timedelta(seconds=i),
            "session": "live",
            "obd:RPM": float(800 + i),
        }
        if extra:
            out["obd:MAF"] = 4.0 + i
        out["obd:RPM__gap"] = i % 4 == 0
        if extra:
            out["obd:MAF__gap"] = bool(i % 2)
        if context:
            out["context:observed"] = 1.0
            out["context:flag"] = True
        return out

    plan = [row(i, context=True) for i in range(4)]
    plan += [row(i, context=False, extra=True) for i in range(4, 12)]
    plan += [row(i, context=True) for i in range(12, 20)]
    seen_vanished = seen_partial_flag = False
    for r in plan:
        ring.append(r)
        dicts.append(r)
        expected = _reference(list(dicts))
        assert_same_frame(ring.frame(), expected)
        seen_partial_flag |= "context:flag" in expected and expected["context:flag"].dtype == object
        seen_vanished |= "context:flag" not in expected
    assert seen_partial_flag and seen_vanished
    assert "obd:MAF" not in ring.frame()  # evicted entirely


@pytest.mark.parametrize("seed", range(6))
def test_random_row_sequences_match_the_dict_path(seed: int) -> None:
    """Keys appear, vanish and return at random, with NaNs, over a small ring."""
    rng = np.random.default_rng(seed)
    capacity = int(rng.integers(1, 9))
    ring = WideRowRing(capacity)
    dicts: deque[dict[str, Any]] = deque(maxlen=capacity)
    floats = [f"f{i}" for i in range(6)]
    flags = [f"b{i}" for i in range(6)]
    for i in range(40):
        row: dict[str, Any] = {"timestamp": pd.Timestamp(T0) + pd.Timedelta(seconds=i)}
        keys = [k for k in [*floats, *flags] if rng.random() < 0.7]
        rng.shuffle(keys)
        for key in keys:
            if key.startswith("f"):
                row[key] = math.nan if rng.random() < 0.2 else float(rng.normal())
            else:
                row[key] = bool(rng.random() < 0.5)
        if rng.random() < 0.3:
            row["session"] = "live"
        ring.append(row)
        dicts.append(row)
        assert_same_frame(ring.frame(), _reference(list(dicts)))


def test_other_values_take_pandas_own_inference() -> None:
    """A key that is neither float nor bool (an int, a string) is framed by
    pandas itself, so it gets the dict path's dtype by construction."""
    ring = WideRowRing(3)
    dicts: deque[dict[str, Any]] = deque(maxlen=3)
    for i in range(5):
        r: dict[str, Any] = {"count": i, "label": f"x{i}", "v": float(i)}
        if i >= 3:
            r["late"] = i * 2
        ring.append(r)
        dicts.append(r)
        assert_same_frame(ring.frame(), _reference(list(dicts)))
    assert ring.frame()["count"].dtype == np.int64


def test_empty_ring_frames_like_an_empty_list() -> None:
    assert_same_frame(WideRowRing(4).frame(), _reference([]))


@pytest.mark.parametrize(
    ("first", "then"),
    [(1.5, True), (1.5, 2), (np.float64(1.5), "x"), (True, 0.0), (False, None)],
)
def test_a_value_that_changes_kind_is_refused(first: object, then: object) -> None:
    """The blocks cannot hold it, and the dict path would have changed that
    column's dtype silently: a float column given a bool turns ``object``."""
    ring = WideRowRing(4)
    ring.append({"a": first})
    with pytest.raises(TypeError, match="'a'"):
        ring.append({"a": then})


def test_capacity_must_be_positive() -> None:
    with pytest.raises(ValueError, match="positive"):
        WideRowRing(0)


def test_rings_compare_by_value() -> None:
    a, b = WideRowRing(3), WideRowRing(3)
    assert a == b
    a.append({"x": 1.0})
    assert a != b
    b.append({"x": 1.0})
    assert a == b
    assert WideRowRing(3) != WideRowRing(4)


def test_a_snapshot_is_detached_from_later_appends() -> None:
    """The worker reads ``rows`` while the loop keeps appending."""
    ring = WideRowRing(2)
    ring.append({"x": 1.0, "g": False})
    rows = ring.rows()
    before = rows.frame()
    for i in range(3):
        ring.append({"x": 10.0 + i, "g": True})
    assert_same_frame(rows.frame(), before)
