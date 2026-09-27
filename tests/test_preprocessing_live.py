"""Tests for the live (streaming) feature pipeline (Milestone B)."""

from __future__ import annotations

import math
import pickle
from datetime import UTC, datetime, timedelta
from pathlib import Path

import numpy as np
import pandas as pd
import pytest

from src.config import CanaryConfig, OperatingModeSpec
from src.decode.bindings import Bindings, load_bindings
from src.preprocessing.feature_pipeline import build_feature_frame
from src.preprocessing.live import LiveFeatureSource, build_prepared_live_tick
from src.storage.parquet_writer import write_records_to_parquet
from src.telemetry import TelemetryRecord
from tests.configs import example_config


@pytest.fixture
def config() -> CanaryConfig:
    return example_config(Path("canary.example.yaml"))


# ---------------------------------------------------------------------------
# observe + tick basics
# ---------------------------------------------------------------------------


def test_tick_returns_none_before_any_observation(config: CanaryConfig) -> None:
    src = LiveFeatureSource(config=config)
    assert src.tick(datetime(2026, 5, 13, 12, 0, tzinfo=UTC)) is None


def test_prepared_tick_is_pickle_safe_and_preserves_canonical_result(
    config: CanaryConfig,
) -> None:
    """D01.4: the process payload round-trip preserves the feature result."""
    src = LiveFeatureSource(config=config)
    now = datetime(2026, 5, 13, 12, 0, tzinfo=UTC)
    for channel, value, unit in (
        ("RPM", 750.0, "rpm"),
        ("SPEED", 0.0, "km/h"),
        ("THROTTLE_POS", 10.0, "%"),
        ("COOLANT_TEMP", 85.0, "C"),
    ):
        src.observe(TelemetryRecord.obd(now.timestamp(), channel, value, unit))
    prepared = src.prepare_tick(now)
    assert prepared is not None

    expected = build_prepared_live_tick(prepared)
    restored_prepared = pickle.loads(pickle.dumps(prepared))
    actual = pickle.loads(pickle.dumps(build_prepared_live_tick(restored_prepared)))

    pd.testing.assert_series_equal(
        pd.Series(actual.operating_point), pd.Series(expected.operating_point)
    )
    assert actual.segmenter_fell_back is expected.segmenter_fell_back
    assert actual.feature_row is not None
    assert expected.feature_row is not None
    pd.testing.assert_frame_equal(actual.feature_row, expected.feature_row)


def test_obd_observation_appears_in_emitted_row(config: CanaryConfig) -> None:
    src = LiveFeatureSource(config=config)
    now = datetime(2026, 5, 13, 12, 0, tzinfo=UTC)
    src.observe(TelemetryRecord.obd(now.timestamp(), "RPM", 700.0, "rpm"))
    row = src.tick(now)
    assert row is not None
    assert row["obd:RPM"].iloc[0] == 700.0


def test_obd_record_with_non_numeric_value_is_skipped(config: CanaryConfig) -> None:
    """A VIN string shouldn't crash or contaminate the wide row."""
    src = LiveFeatureSource(config=config)
    src.observe(TelemetryRecord.obd(1.0, "VIN", "JN8AR05Y...", ""))
    # VIN didn't go in, so there's nothing in the current-values dict yet.
    assert src.tick(datetime(2026, 5, 13, 12, 0, tzinfo=UTC)) is None


def test_latest_value_wins_per_channel(config: CanaryConfig) -> None:
    src = LiveFeatureSource(config=config)
    now = datetime(2026, 5, 13, 12, 0, tzinfo=UTC)
    src.observe(TelemetryRecord.obd(now.timestamp() - 1, "RPM", 700.0, "rpm"))
    src.observe(TelemetryRecord.obd(now.timestamp(), "RPM", 1500.0, "rpm"))
    row = src.tick(now)
    assert row is not None
    assert row["obd:RPM"].iloc[0] == 1500.0


def test_gps_observation_appears_in_emitted_row(config: CanaryConfig) -> None:
    """2a.1 — gps has its own explicit routing arm, not the generic catch-all."""
    src = LiveFeatureSource(config=config)
    now = datetime(2026, 5, 13, 12, 0, tzinfo=UTC)
    src.observe(TelemetryRecord.gps(now.timestamp(), "speed", 42.0, "km/h"))
    row = src.tick(now)
    assert row is not None
    assert row["gps:speed"].iloc[0] == 42.0


def test_imu_observation_appears_in_emitted_row(config: CanaryConfig) -> None:
    """2a.1 — imu has its own explicit routing arm, not the generic catch-all."""
    src = LiveFeatureSource(config=config)
    now = datetime(2026, 5, 13, 12, 0, tzinfo=UTC)
    src.observe(TelemetryRecord.imu(now.timestamp(), "ACCEL_X", 0.02, "g"))
    row = src.tick(now)
    assert row is not None
    assert row["imu:ACCEL_X"].iloc[0] == pytest.approx(0.02)


def test_multiple_channels_coexist(config: CanaryConfig) -> None:
    src = LiveFeatureSource(config=config)
    now = datetime(2026, 5, 13, 12, 0, tzinfo=UTC)
    src.observe(TelemetryRecord.obd(now.timestamp(), "RPM", 700.0, "rpm"))
    src.observe(TelemetryRecord.obd(now.timestamp(), "SPEED", 25.0, "km/h"))
    row = src.tick(now)
    assert row is not None
    assert row["obd:RPM"].iloc[0] == 700.0
    assert row["obd:SPEED"].iloc[0] == 25.0


# ---------------------------------------------------------------------------
# CAN decode via bindings
# ---------------------------------------------------------------------------


def test_can_record_decodes_via_bindings(config: CanaryConfig) -> None:
    bindings = load_bindings(Path("data/bindings.yaml"))
    src = LiveFeatureSource(config=config, bindings=bindings)
    now = datetime(2026, 5, 13, 12, 0, tzinfo=UTC)
    # 0x551 byte 0 carries COOLANT_TEMP raw with offset −40.
    # Drop a frame with that byte = 0x69 (105 dec). decode_raw extracts 105.
    src.observe(TelemetryRecord.can(now.timestamp(), 0x551, bytes([0x69, 0, 0, 0, 0, 0, 0, 0])))
    row = src.tick(now)
    assert row is not None
    # decoded:* uses the raw integer (105), matching decode_can_rows policy
    assert row["decoded:ECM_COOLANT_TEMP"].iloc[0] == 105.0


def test_can_record_with_no_matching_binding_is_silently_skipped(
    config: CanaryConfig,
) -> None:
    bindings = load_bindings(Path("data/bindings.yaml"))
    src = LiveFeatureSource(config=config, bindings=bindings)
    # An ID no binding knows about
    src.observe(TelemetryRecord.can(1.0, 0xABC, bytes(8)))
    assert src.tick(datetime(2026, 5, 13, 12, 0, tzinfo=UTC)) is None


def test_can_record_with_short_payload_is_silently_skipped(config: CanaryConfig) -> None:
    # Synthetic single binding at bytes 2-3, deliberately NOT real
    # data/bindings.yaml. The original fixture used 0x1F9 + a 1-byte
    # payload on the premise "0x1F9 only binds RPM at bytes 2-3" — which a
    # later byte-0 binding (COOLING_FAN_1F9_0, 0x1F9:0) silently
    # invalidated (a 1-byte payload then DID decode something). Decoupling
    # from the live bindings file keeps this testing the decode-too-short
    # path deterministically, immune to future bindings.yaml changes.
    bindings = Bindings.model_validate(
        {
            "schema_version": 1,
            "signals": [
                {
                    "name": "TEST_NEEDS_BYTES_2_3",
                    "module": "TEST",
                    "can_id": 0x123,
                    "byte_start": 2,
                    "byte_length": 2,
                    "confidence": "high",
                }
            ],
        }
    )
    src = LiveFeatureSource(config=config, bindings=bindings)
    # 1-byte payload can't satisfy a bytes-2-3 binding → nothing decodes.
    src.observe(TelemetryRecord.can(1.0, 0x123, b"\x00"))
    assert src.tick(datetime(2026, 5, 13, 12, 0, tzinfo=UTC)) is None


def test_a_sentinel_word_never_becomes_a_live_feature_value(config: CanaryConfig) -> None:
    """A04.6: the live source consumes FrameDecoder's rows, so a declared
    sentinel reaches the dashboard stream as a ``_SENTINEL`` state and never
    sets the ``decoded:`` number the models read."""
    bindings = Bindings.model_validate(
        {
            "schema_version": 1,
            "signals": [
                {
                    "name": "TYRE_A",
                    "module": "TEST",
                    "can_id": 0x385,
                    "byte_start": 2,
                    "byte_length": 1,
                    "confidence": "hypothesized",
                    "sentinel_raw": 0,
                    "sentinel_meaning": "wheel sensor not transmitting",
                }
            ],
        }
    )
    src = LiveFeatureSource(config=config, bindings=bindings)
    now = datetime(2026, 5, 13, 12, 0, tzinfo=UTC)
    asleep = TelemetryRecord.can(now.timestamp(), 0x385, bytes(8))
    assert [(r.channel, r.value) for r in src.decode_for_telemetry(asleep)] == [
        ("TYRE_A_SENTINEL", "wheel sensor not transmitting")
    ]
    src.observe(asleep)
    assert src.tick(now) is None  # nothing numeric arrived
    later = now + timedelta(seconds=1)
    src.observe(TelemetryRecord.can(later.timestamp(), 0x385, bytes([0, 0, 148, 0, 0, 0, 0, 0])))
    row = src.tick(later)
    assert row is not None
    assert row["decoded:TYRE_A"].iloc[0] == 148.0
    assert "decoded:TYRE_A_SENTINEL" not in row.columns


# ---------------------------------------------------------------------------
# Rolling features actually compute
# ---------------------------------------------------------------------------


def test_rolling_mean_aggregates_observed_history(config: CanaryConfig) -> None:
    """After several ticks, the __mean column should reflect the average."""
    src = LiveFeatureSource(config=config)
    base = datetime(2026, 5, 13, 12, 0, tzinfo=UTC)
    values = [700.0, 720.0, 740.0, 760.0, 780.0]
    for i, value in enumerate(values):
        now = base + timedelta(seconds=i)
        src.observe(TelemetryRecord.obd(now.timestamp(), "RPM", value, "rpm"))
        row = src.tick(now)
        assert row is not None
    # window is 30 s, every tick is 1 s → all values are in-window → mean of all 5
    assert row["obd:RPM__mean"].iloc[0] == pytest.approx(np.mean(values))


def test_rolling_std_is_nan_on_single_sample(config: CanaryConfig) -> None:
    src = LiveFeatureSource(config=config)
    now = datetime(2026, 5, 13, 12, 0, tzinfo=UTC)
    src.observe(TelemetryRecord.obd(now.timestamp(), "RPM", 700.0, "rpm"))
    row = src.tick(now)
    assert row is not None
    assert math.isnan(row["obd:RPM__std"].iloc[0])


def test_rolling_window_drops_old_observations(config: CanaryConfig) -> None:
    """A value far enough in the past must roll out of the mean."""
    src = LiveFeatureSource(config=config)
    first = datetime(2026, 5, 13, 12, 0, tzinfo=UTC)
    src.observe(TelemetryRecord.obd(first.timestamp(), "RPM", 6000.0, "rpm"))  # spike
    src.tick(first)
    # 60 seconds later, push 700.0 — the 6000 should be outside the 30 s window
    base = datetime(2026, 5, 13, 12, 1, tzinfo=UTC)
    src.observe(TelemetryRecord.obd(base.timestamp(), "RPM", 700.0, "rpm"))
    row = src.tick(base)
    assert row is not None
    # spike is outside the rolling 30s window now
    assert row["obd:RPM__mean"].iloc[0] == pytest.approx(700.0, abs=0.5)


def test_live_and_offline_obd_feature_rows_share_contract(
    tmp_path: Path,
    config: CanaryConfig,
) -> None:
    session = tmp_path / "2026-05-13T120000"
    base = datetime(2026, 5, 13, 12, 0, tzinfo=UTC)
    records: list[TelemetryRecord] = []
    for index, rpm in enumerate([700.0, 720.0, 740.0, 760.0, 780.0]):
        ts = base + timedelta(seconds=index)
        records.append(TelemetryRecord(timestamp=ts, source="obd", channel="RPM", value=rpm))
        records.append(
            TelemetryRecord(timestamp=ts, source="obd", channel="SPEED", value=float(index))
        )
    write_records_to_parquet(records, session)
    offline = build_feature_frame(
        [session],
        config,
        bindings_path=tmp_path / "no-bindings.yaml",
        behavior_models_path=tmp_path / "no-behavior.yaml",
        dbc_dir=tmp_path / "no-dbc",
    )

    source = LiveFeatureSource(config=config)
    live_row = None
    for record in records:
        source.observe(record)
        if record.channel == "SPEED":
            live_row = source.tick(record.timestamp)
    assert live_row is not None

    expected_columns = {
        "timestamp",
        "session",
        "operating_mode",
        "obd:RPM",
        "obd:SPEED",
        "obd:RPM__mean",
        "obd:SPEED__mean",
        "obd:RPM__std",
        "obd:SPEED__std",
        "obd:RPM__roc",
        "obd:SPEED__roc",
    }
    assert expected_columns <= set(offline.columns)
    assert expected_columns <= set(live_row.columns)
    last_offline = offline.iloc[-1]
    last_live = live_row.iloc[0]
    for column in expected_columns - {"timestamp", "session", "operating_mode"}:
        assert last_live[column] == pytest.approx(last_offline[column], nan_ok=True)
    assert last_live["operating_mode"] == last_offline["operating_mode"]


# ---------------------------------------------------------------------------
# GRU-D mask / Δt parity with the resampler
# ---------------------------------------------------------------------------


def test_live_gap_flag_matches_resampler_for_same_observations(config: CanaryConfig) -> None:
    """The live serve path has no resampler, so it derives each channel's
    ``__gap`` (mask = ¬gap) and Δt from per-channel last-observed timestamps.
    For the same observation history those flags must match what
    ``resample_wide`` would emit, or train and serve disagree on staleness."""
    from src.columns import SESSION_COLUMN, TIMESTAMP_COLUMN, gap_column
    from src.preprocessing.resampler import ResampleConfig, resample_wide

    base = datetime(2026, 5, 13, 12, 0, tzinfo=UTC)
    # RPM is a fast channel observed at t=0 and t=4 only (so it goes stale in
    # between). The resampler's max_gap at 1 Hz is 2 s (interval × factor 2).
    observe_ticks = {0, 4}

    src = LiveFeatureSource(config=config)
    live_gap: list[bool] = []
    for i in range(8):
        if i in observe_ticks:
            src.observe(TelemetryRecord.obd(float((base).timestamp() + i), "RPM", 800.0, "rpm"))
        # A second, always-fresh channel so RPM going stale (1.4) doesn't make
        # the *whole tick* fully-stale — this test is about RPM's own gap
        # flag, not the fully-stale-tick behavior (covered separately).
        src.observe(
            TelemetryRecord.obd((base + timedelta(seconds=i)).timestamp(), "SPEED", 0.0, "km/h")
        )
        row = src.tick(base + timedelta(seconds=i))
        assert row is not None
        live_gap.append(bool(row[gap_column("obd:RPM")].iloc[0]))

    # Offline equivalent: the same two observations through the resampler.
    wide = pd.DataFrame(
        {
            SESSION_COLUMN: ["s"] * len(observe_ticks),
            TIMESTAMP_COLUMN: [base + timedelta(seconds=i) for i in sorted(observe_ticks)],
            "obd:RPM": [800.0] * len(observe_ticks),
        }
    )
    resampled = resample_wide(wide, config=ResampleConfig(hz=config.preprocessing.resample_hz))
    resampler_gap = resampled[gap_column("obd:RPM")].astype(bool).tolist()

    # The resampler's grid spans only [first_obs, last_obs] (t=0..4); the live
    # path ticks on past the last observation (t=5..7). Over the overlapping
    # span the gap pattern is identical: fresh at t=0,1,2 (age ≤ 2 s) and t=4,
    # stale at t=3.
    assert resampler_gap == [False, False, False, True, False]
    assert live_gap[: len(resampler_gap)] == resampler_gap
    # Past the last observation the live path keeps ageing: fresh at t=4,5,6
    # (age ≤ 2), stale again at t=7.
    assert live_gap == [False, False, False, True, False, False, False, True]


def test_live_gap_flags_follow_a_non_default_fast_channels_like_the_resampler(
    config: CanaryConfig, monkeypatch: pytest.MonkeyPatch
) -> None:
    """R04.7: live ``__gap`` flags read ``ResampleConfig.max_gap_seconds_for``,
    so an explicit ``fast_channels`` (coolant fast, RPM therefore slow) flips
    the same cells stale at serve as the resampler does offline. Before, live
    re-derived the rule from the default slow-channel hints and disagreed on
    both channels."""
    import src.preprocessing.live as live_module
    from src.columns import SESSION_COLUMN, TIMESTAMP_COLUMN, gap_column
    from src.preprocessing.resampler import ResampleConfig, resample_wide

    recipe = ResampleConfig(
        hz=1.0,
        max_gap_factor=2.0,
        fast_channels=frozenset({"obd:COOLANT_TEMP"}),
        slow_channel_max_gap_seconds=30.0,
    )
    monkeypatch.setattr(live_module, "resample_config_for", lambda _config: recipe)

    base = datetime(2026, 5, 13, 12, 0, tzinfo=UTC)
    observed = (0, 5)
    src = LiveFeatureSource(config=config)
    live: dict[str, list[bool]] = {"obd:COOLANT_TEMP": [], "obd:RPM": []}
    for i in range(6):
        at = (base + timedelta(seconds=i)).timestamp()
        if i in observed:
            src.observe(TelemetryRecord.obd(at, "COOLANT_TEMP", 80.0 + i, "C"))
            src.observe(TelemetryRecord.obd(at, "RPM", 700.0 + i, "rpm"))
        src.observe(TelemetryRecord.obd(at, "SPEED", 0.0, "km/h"))
        row = src.tick(base + timedelta(seconds=i))
        assert row is not None
        for channel, flags in live.items():
            flags.append(bool(row[gap_column(channel)].iloc[0]))

    wide = pd.DataFrame(
        {
            SESSION_COLUMN: ["s"] * len(observed),
            TIMESTAMP_COLUMN: [base + timedelta(seconds=i) for i in observed],
            "obd:COOLANT_TEMP": [80.0 + i for i in observed],
            "obd:RPM": [700.0 + i for i in observed],
        }
    )
    resampled = resample_wide(wide, config=recipe)
    for channel, flags in live.items():
        assert flags == resampled[gap_column(channel)].astype(bool).tolist(), channel
    # The non-default rule is what is being compared, not a coincidence of
    # the defaults: coolant goes stale past 2 s, RPM (now slow) never does.
    assert live["obd:COOLANT_TEMP"] == [False, False, False, True, True, False]
    assert live["obd:RPM"] == [False] * 6


def test_a_nan_sample_is_not_an_observation_live_or_offline(config: CanaryConfig) -> None:
    """R01.3: the live source coerces through ``coerce.finite_float``. A NaN
    value is not a fresh observation, as offline, where the resampler's
    ``_gap_flags`` drops NaN before ageing. The old ``_to_float`` let NaN
    through, so a NaN sample stamped the channel fresh (``__gap`` False) and
    NaN'd its cell, where training held the previous value."""
    from src.columns import SESSION_COLUMN, TIMESTAMP_COLUMN, gap_column
    from src.preprocessing.resampler import resample_config_for, resample_wide

    base = datetime(2026, 5, 13, 12, 0, tzinfo=UTC)
    values = [700.0, None, None, float("nan")]
    src = LiveFeatureSource(config=config)
    live_gap: list[bool] = []
    live_rpm: list[float] = []
    for i, value in enumerate(values):
        at = (base + timedelta(seconds=i)).timestamp()
        if value is not None:
            src.observe(TelemetryRecord.obd(at, "RPM", value, "rpm"))
        src.observe(TelemetryRecord.obd(at, "SPEED", 0.0, "km/h"))
        row = src.tick(base + timedelta(seconds=i))
        assert row is not None
        live_gap.append(bool(row[gap_column("obd:RPM")].iloc[0]))
        live_rpm.append(float(row["obd:RPM"].iloc[0]))

    wide = pd.DataFrame(
        {
            SESSION_COLUMN: ["s", "s"],
            TIMESTAMP_COLUMN: [base, base + timedelta(seconds=3)],
            "obd:RPM": [700.0, float("nan")],
        }
    )
    resampled = resample_wide(wide, config=resample_config_for(config))
    assert live_gap == resampled[gap_column("obd:RPM")].astype(bool).tolist()
    assert live_gap == [False, False, False, True]
    # The NaN did not refresh the channel; it aged out and went NaN on schedule.
    assert live_rpm[:3] == [700.0] * 3 and math.isnan(live_rpm[3])


def test_live_and_on_read_decode_a_frame_to_the_same_record(config: CanaryConfig) -> None:
    """R04.4: the live source publishes ``FrameDecoder.decode``'s records over
    every confidence. Before, it re-implemented the decode and drifted:
    metadata ``confidence`` vs ``binding_confidence``, an int value vs a
    float, and ``""`` vs ``"unknown"`` for the unit. P02.8: nothing stores
    those rows now, and the readers' decode on read must make exactly the
    live record for each on-read signal, or a value shown live and the one a
    reader decodes later could differ."""
    import json

    import pandas as pd

    from src.decode.frame_decoder import FrameDecoder
    from src.preprocessing.decode import decode_on_read, on_read_catalog

    bindings = load_bindings(Path("data/bindings.yaml"))
    live = LiveFeatureSource(config=config, bindings=bindings)
    on_read = on_read_catalog(bindings)
    assert on_read is not None
    everything = FrameDecoder.from_bindings(bindings)
    now = datetime(2026, 5, 13, 12, 0, tzinfo=UTC).timestamp()
    compared = 0
    for can_id in sorted({signal.can_id for signal in bindings.signals}):
        record = TelemetryRecord.can(now, can_id, bytes(range(0x31, 0x39)))
        live_records = live.decode_for_telemetry(record)
        assert live_records == everything.decode(record)
        by_name = {decoded.channel: decoded for decoded in live_records}
        rows = decode_on_read(pd.DataFrame([record.parquet_row()]), on_read)
        for row in rows[rows["source"] == "decoded"].to_dict(orient="records"):
            shown = by_name[row["channel"]]
            assert row["value_number"] == shown.value
            assert row["unit"] == shown.unit
            assert json.loads(row["metadata_json"]) == shown.metadata
            compared += 1
    assert compared > 0
    # A binding whose unit is the placeholder reads as no unit on every path.
    unknown = next(s for s in bindings.signals if s.unit == "unknown")
    (first, *_) = [
        r
        for r in live.decode_for_telemetry(
            TelemetryRecord.can(now, unknown.can_id, bytes(range(0x31, 0x39)))
        )
        if r.channel == unknown.name
    ]
    assert first.unit == "" == unknown.display_unit
    assert first.metadata == {"can_id": unknown.can_id, "binding_confidence": unknown.confidence}


def test_live_channel_age_tracks_seconds_since_observation(config: CanaryConfig) -> None:
    """``channel_age_seconds`` (the GRU-D Δt source) counts seconds since the
    channel's last observation, resetting on every fresh ``observe``."""
    base = datetime(2026, 5, 13, 12, 0, tzinfo=UTC)
    src = LiveFeatureSource(config=config)
    src.observe(TelemetryRecord.obd(base.timestamp(), "RPM", 800.0, "rpm"))
    ages = []
    for i in range(4):
        now = base + timedelta(seconds=i)
        src.tick(now)
        ages.append(src.channel_age_seconds("obd:RPM", now))
    assert ages == [0.0, 1.0, 2.0, 3.0]
    # A fresh observation resets the age to 0.
    later = base + timedelta(seconds=5)
    src.observe(TelemetryRecord.obd(later.timestamp(), "RPM", 810.0, "rpm"))
    src.tick(later)
    assert src.channel_age_seconds("obd:RPM", later) == 0.0


# ---------------------------------------------------------------------------
# Buffer / introspection
# ---------------------------------------------------------------------------


@pytest.mark.slow  # ~37 s: fills the ring buffer to its cap; excluded from inner loop
def test_buffer_caps_at_max_rows(config: CanaryConfig) -> None:
    src = LiveFeatureSource(config=config)
    # _max_rows = max(32, window_seconds*hz*2, idle_transient_lookback_seconds*hz + hz)
    # = max(32, 30*1*2, 180*1 + 1) = 181 for the example config (lookback dominates).
    base = datetime(2026, 5, 13, 12, 0, tzinfo=UTC)
    for i in range(400):
        now = base + timedelta(seconds=i)
        src.observe(TelemetryRecord.obd(now.timestamp(), "RPM", float(700 + i), "rpm"))
        src.tick(now)
    assert src.buffer_size == 181


def test_known_channels_lists_observed_columns(config: CanaryConfig) -> None:
    src = LiveFeatureSource(config=config)
    src.observe(TelemetryRecord.obd(1.0, "RPM", 700.0, "rpm"))
    src.observe(TelemetryRecord.obd(1.0, "SPEED", 25.0, "km/h"))
    assert set(src.known_channels) == {"obd:RPM", "obd:SPEED"}


# ---------------------------------------------------------------------------
# quality gate (Task 1.2) — physically-impossible values never land
# ---------------------------------------------------------------------------


def test_observe_rejects_physically_impossible_value(config: CanaryConfig) -> None:
    src = LiveFeatureSource(config=config)
    now = datetime(2026, 5, 13, 12, 0, tzinfo=UTC)
    # RPM 16383 is the PID 0x0C decode-rollover artifact — see PHYSICAL_LIMITS.
    src.observe(TelemetryRecord.obd(now.timestamp(), "RPM", 16383.0, "rpm"))
    assert "obd:RPM" not in src.known_channels
    assert src.reject_counts == {"obd:RPM": 1}


def test_observe_accepts_physically_plausible_value(config: CanaryConfig) -> None:
    src = LiveFeatureSource(config=config)
    now = datetime(2026, 5, 13, 12, 0, tzinfo=UTC)
    src.observe(TelemetryRecord.obd(now.timestamp(), "RPM", 750.0, "rpm"))
    assert "obd:RPM" in src.known_channels
    assert src.reject_counts == {}


def test_reject_counts_accumulate_and_reset_clears_them(config: CanaryConfig) -> None:
    src = LiveFeatureSource(config=config)
    now = datetime(2026, 5, 13, 12, 0, tzinfo=UTC)
    src.observe(TelemetryRecord.obd(now.timestamp(), "COOLANT_TEMP", -40.0, "C"))
    src.observe(TelemetryRecord.obd(now.timestamp(), "COOLANT_TEMP", -40.0, "C"))
    assert src.reject_counts == {"obd:COOLANT_TEMP": 2}
    src.reset()
    assert src.reject_counts == {}


# ---------------------------------------------------------------------------
# operating_mode segmentation lands when inputs are present
# ---------------------------------------------------------------------------


def test_segmenter_runs_when_rpm_speed_throttle_present(config: CanaryConfig) -> None:
    src = LiveFeatureSource(config=config)
    now = datetime(2026, 5, 13, 12, 0, tzinfo=UTC)
    # warmup vs idle in the example config splits on coolant_temp (cold
    # idle is its own mode, see canary.example.yaml). Warm coolant + idle
    # RPM/speed → idle.
    src.observe(TelemetryRecord.obd(now.timestamp(), "RPM", 750.0, "rpm"))
    src.observe(TelemetryRecord.obd(now.timestamp(), "SPEED", 0.0, "km/h"))
    src.observe(TelemetryRecord.obd(now.timestamp(), "THROTTLE_POS", 10.0, "%"))
    src.observe(TelemetryRecord.obd(now.timestamp(), "COOLANT_TEMP", 85.0, "C"))
    row = src.tick(now)
    assert row is not None
    assert row["operating_mode"].iloc[0] == "idle"
    assert not src.last_segmenter_fallback  # 4.7 — real segmentation, not a fallback


def test_segmenter_picks_warmup_when_coolant_temp_is_cold(config: CanaryConfig) -> None:
    """Cold idle (stopped + cold coolant) now lands in its own ``warmup`` bucket
    rather than the steady-state ``idle`` mode, so per-mode behavior fits
    don't conflate the two regimes.
    """
    src = LiveFeatureSource(config=config)
    now = datetime(2026, 5, 13, 12, 0, tzinfo=UTC)
    src.observe(TelemetryRecord.obd(now.timestamp(), "RPM", 750.0, "rpm"))
    src.observe(TelemetryRecord.obd(now.timestamp(), "SPEED", 0.0, "km/h"))
    src.observe(TelemetryRecord.obd(now.timestamp(), "THROTTLE_POS", 10.0, "%"))
    src.observe(TelemetryRecord.obd(now.timestamp(), "COOLANT_TEMP", 30.0, "C"))
    row = src.tick(now)
    assert row is not None
    assert row["operating_mode"].iloc[0] == "warmup"


def test_segmenter_falls_back_to_all_without_signal_columns(config: CanaryConfig) -> None:
    src = LiveFeatureSource(config=config)
    now = datetime(2026, 5, 13, 12, 0, tzinfo=UTC)
    # Only a decoded:* channel, no obd:RPM/SPEED/THROTTLE_POS
    src.observe(TelemetryRecord.obd(now.timestamp(), "COOLANT_TEMP", 80.0, "C"))
    row = src.tick(now)
    assert row is not None
    assert row["operating_mode"].iloc[0] == "all"
    assert src.last_segmenter_fallback  # 4.7


# ---------------------------------------------------------------------------
# 5.2 — mode dwell/hysteresis (live path, must agree with the batch path in
# test_preprocessing_loader.py on the exact same speed sequence)
# ---------------------------------------------------------------------------


def _dwell_modes_live(config: CanaryConfig, dwell_ticks: int) -> CanaryConfig:
    config.preprocessing.operating_modes = {
        "city": OperatingModeSpec(speed_max=50),
        "highway": OperatingModeSpec(speed_min=80),
    }
    config.preprocessing.mode_dwell_ticks = dwell_ticks
    return config


def _tick_speeds_live(config: CanaryConfig, speeds: list[float]) -> list[str]:
    """Drive one observe+tick per speed reading (rpm/throttle/coolant held
    constant so the segmenter's fixed input set is satisfied every tick) and
    collect the emitted ``operating_mode`` per tick, in order."""
    src = LiveFeatureSource(config=config)
    start = datetime(2026, 5, 13, 12, 0, tzinfo=UTC)
    modes: list[str] = []
    for i, speed in enumerate(speeds):
        now = start + timedelta(seconds=i)
        src.observe(TelemetryRecord.obd(now.timestamp(), "RPM", 1500.0, "rpm"))
        src.observe(TelemetryRecord.obd(now.timestamp(), "THROTTLE_POS", 20.0, "%"))
        src.observe(TelemetryRecord.obd(now.timestamp(), "COOLANT_TEMP", 85.0, "C"))
        src.observe(TelemetryRecord.obd(now.timestamp(), "SPEED", speed, "km/h"))
        row = src.tick(now)
        assert row is not None
        modes.append(str(row["operating_mode"].iloc[0]))
    return modes


def test_mode_dwell_absorbs_single_row_spike_live(config: CanaryConfig) -> None:
    """Same scenario/expectation as the batch
    ``test_mode_dwell_absorbs_single_row_spike`` — a one-tick highway spike
    must not switch live serve's label either."""
    modes = _tick_speeds_live(
        _dwell_modes_live(config, dwell_ticks=3), [20, 20, 20, 90, 20, 20, 20]
    )
    assert modes == ["city"] * 7


def test_mode_dwell_switches_after_sustained_transition_live(config: CanaryConfig) -> None:
    """Same scenario/expectation as the batch
    ``test_mode_dwell_switches_after_sustained_transition`` — proves live
    serve and the batch loader agree on exactly when the dwell completes,
    since both run the same ``segment_operating_modes`` dwell state machine."""
    modes = _tick_speeds_live(
        _dwell_modes_live(config, dwell_ticks=3), [20, 20, 20, 90, 90, 90, 90, 90]
    )
    assert modes == ["city", "city", "city", "city", "city", "highway", "highway", "highway"]


# ---------------------------------------------------------------------------
# 1.4 — live parity: bounded ffill + fully-stale ticks return None
# ---------------------------------------------------------------------------


def test_tick_returns_none_once_every_channel_is_fully_stale(config: CanaryConfig) -> None:
    """Mirrors ``_drop_fully_stale_rows``: once every gap-tracked channel's
    age exceeds its ffill limit, the tick is a synthesized fill, not real
    telemetry, and detectors must not score it."""
    src = LiveFeatureSource(config=config)
    first = datetime(2026, 5, 13, 12, 0, tzinfo=UTC)
    src.observe(TelemetryRecord.obd(first.timestamp(), "RPM", 800.0, "rpm"))
    assert src.tick(first) is not None

    # max_gap at 1 Hz (factor 2.0) is 2s — well past that with no further
    # observation, RPM (the only channel) is fully stale.
    later = first + timedelta(seconds=30)
    assert src.tick(later) is None
    assert src.latest_operating_point is None


def test_tick_nans_a_single_stale_channel_but_keeps_the_row(config: CanaryConfig) -> None:
    """Bounded ffill (1.1) applied live: one channel going stale NaNs just
    that cell rather than either freezing its old value forever or (since a
    second channel stays fresh) killing the whole tick."""
    src = LiveFeatureSource(config=config)
    first = datetime(2026, 5, 13, 12, 0, tzinfo=UTC)
    src.observe(TelemetryRecord.obd(first.timestamp(), "RPM", 800.0, "rpm"))
    src.tick(first)

    later = first + timedelta(seconds=30)
    # SPEED stays fresh every tick; RPM goes stale from having no update.
    src.observe(TelemetryRecord.obd(later.timestamp(), "SPEED", 0.0, "km/h"))
    row = src.tick(later)

    assert row is not None
    assert pd.isna(row["obd:RPM"].iloc[0])
    assert bool(row["obd:RPM__gap"].iloc[0]) is True
    assert row["obd:SPEED"].iloc[0] == 0.0
    assert bool(row["obd:SPEED__gap"].iloc[0]) is False


def test_tick_bounds_slow_channel_ffill_with_its_own_ceiling(config: CanaryConfig) -> None:
    """A slow channel (coolant) gets the generous slow ceiling, not the fast
    one — it survives longer without a fresh reading before going NaN."""
    src = LiveFeatureSource(config=config)
    first = datetime(2026, 5, 13, 12, 0, tzinfo=UTC)
    src.observe(TelemetryRecord.obd(first.timestamp(), "COOLANT_TEMP", 80.0, "C"))
    src.tick(first)

    # Past the fast max_gap (2s) but well inside the default 120s slow
    # ceiling — a second, always-fresh fast channel keeps the tick alive.
    later = first + timedelta(seconds=30)
    src.observe(TelemetryRecord.obd(later.timestamp(), "RPM", 800.0, "rpm"))
    row = src.tick(later)

    assert row is not None
    assert row["obd:COOLANT_TEMP"].iloc[0] == 80.0
    assert bool(row["obd:COOLANT_TEMP__gap"].iloc[0]) is False


# ---------------------------------------------------------------------------
# 1.5 — vehicle_state_inputs (cheap in-memory classify_vehicle_state feed)
# ---------------------------------------------------------------------------


def test_vehicle_state_inputs_reports_rpm_and_obd_freshness(config: CanaryConfig) -> None:
    src = LiveFeatureSource(config=config)
    now = datetime(2026, 5, 13, 12, 0, tzinfo=UTC)
    src.observe(TelemetryRecord.obd(now.timestamp(), "RPM", 1500.0, "rpm"))
    src.observe(TelemetryRecord.obd(now.timestamp(), "COOLANT_TEMP", 90.0, "C"))

    later = now + timedelta(seconds=3)
    inputs = src.vehicle_state_inputs(later)

    assert inputs["rpm"] == 1500.0
    assert inputs["coolant_temp"] == 90.0
    assert inputs["run_time"] is None
    assert inputs["obd_freshness_seconds"] == pytest.approx(3.0)
    assert inputs["can_freshness_seconds"] is None


def test_vehicle_state_inputs_tracks_can_via_decoded_and_dbc_channels(
    config: CanaryConfig,
) -> None:
    bindings = load_bindings(Path("data/bindings.yaml"))
    src = LiveFeatureSource(config=config, bindings=bindings)
    now = datetime(2026, 5, 13, 12, 0, tzinfo=UTC)
    src.observe(TelemetryRecord.can(now.timestamp(), 0x551, bytes([0x69, 0, 0, 0, 0, 0, 0, 0])))

    later = now + timedelta(seconds=2)
    inputs = src.vehicle_state_inputs(later)

    assert inputs["obd_freshness_seconds"] is None
    assert inputs["can_freshness_seconds"] == pytest.approx(2.0)


def test_vehicle_state_inputs_empty_before_any_observation(config: CanaryConfig) -> None:
    src = LiveFeatureSource(config=config)
    inputs = src.vehicle_state_inputs(datetime(2026, 5, 13, 12, 0, tzinfo=UTC))
    assert inputs == {
        "rpm": None,
        "broadcast_rpm": None,
        "coolant_temp": None,
        "run_time": None,
        "obd_freshness_seconds": None,
        "can_freshness_seconds": None,
    }


# ---------------------------------------------------------------------------
# reset (Phase 7.1 — replay in-memory isolation)
# ---------------------------------------------------------------------------


def test_reset_clears_current_values_and_buffer(config: CanaryConfig) -> None:
    """A live drive's observed state must not leak into a replay run (and
    vice versa on the way back out) — reset() is called on both boundaries."""
    src = LiveFeatureSource(config=config)
    now = datetime(2026, 5, 13, 12, 0, tzinfo=UTC)
    src.observe(TelemetryRecord.obd(now.timestamp(), "RPM", 1500.0, "rpm"))
    src.tick(now)
    assert src.known_channels
    assert src.buffer_size > 0
    assert src.latest_operating_point is not None

    src.reset()

    assert src.known_channels == ()
    assert src.buffer_size == 0
    assert src.latest_operating_point is None
    assert src.tick(now) is None  # nothing observed since reset
    assert src.vehicle_state_inputs(now) == {
        "rpm": None,
        "broadcast_rpm": None,
        "coolant_temp": None,
        "run_time": None,
        "obd_freshness_seconds": None,
        "can_freshness_seconds": None,
    }


def test_reset_discards_feature_result_built_from_previous_generation(
    config: CanaryConfig,
) -> None:
    """D01.4: an in-flight child result cannot cross a replay reset."""
    src = LiveFeatureSource(config=config)
    now = datetime(2026, 5, 13, 12, 0, tzinfo=UTC)
    src.observe(TelemetryRecord.obd(now.timestamp(), "RPM", 1500.0, "rpm"))
    prepared = src.prepare_tick(now)
    assert prepared is not None
    built = build_prepared_live_tick(prepared)

    src.reset()

    assert src.apply_built_tick(prepared, built) is None
    assert src.latest_operating_point is None
    assert src.latest_derived_channels is None
    assert src.last_segmenter_fallback is False


def test_reset_is_reusable_source_observes_normally_afterward(config: CanaryConfig) -> None:
    src = LiveFeatureSource(config=config)
    now = datetime(2026, 5, 13, 12, 0, tzinfo=UTC)
    src.observe(TelemetryRecord.obd(now.timestamp(), "RPM", 1500.0, "rpm"))
    src.reset()

    later = now + timedelta(seconds=1)
    src.observe(TelemetryRecord.obd(later.timestamp(), "RPM", 800.0, "rpm"))
    row = src.tick(later)
    assert row is not None
    assert row["obd:RPM"].iloc[0] == 800.0


# ---------------------------------------------------------------------------
# Task 1.1/1.3 — performance channels on the live tick
# ---------------------------------------------------------------------------


def test_derived_instantaneous_channels_appear_on_live_tick(config: CanaryConfig) -> None:
    """Task 1.1 — fuel rate / boost-vacuum / estimated power stream on every
    tick's operating point (ready for the daemon to publish + persist),
    while the cumulative trip integrals are NOT part of this row (Task
    1.3 handles those via the session-scoped accumulator instead)."""
    src = LiveFeatureSource(config=config)
    now = datetime(2026, 5, 13, 12, 0, tzinfo=UTC)
    src.observe(TelemetryRecord.obd(now.timestamp(), "RPM", 2000.0, "rpm"))
    src.observe(TelemetryRecord.obd(now.timestamp(), "SPEED", 60.0, "km/h"))
    src.observe(TelemetryRecord.obd(now.timestamp(), "MAF", 12.0, "g/s"))
    src.observe(TelemetryRecord.obd(now.timestamp(), "INTAKE_PRESSURE", 95.0, "kPa"))
    src.observe(TelemetryRecord.obd(now.timestamp(), "BAROMETRIC_PRESSURE", 80.0, "kPa"))
    row = src.tick(now)
    assert row is not None

    point = src.latest_operating_point
    assert point is not None
    assert point["derived:FUEL_RATE_LPH"] > 0
    assert point["derived:BOOST_VACUUM_KPA"] == pytest.approx(95.0 - 80.0)
    assert point["derived:ESTIMATED_POWER_HP"] > 0
    assert "derived:TRIP_DISTANCE_KM" not in point

    # The instantaneous channels also ride the model-feature row, same as
    # every other decoded channel.
    assert "derived:FUEL_RATE_LPH" in row.columns


def test_latest_derived_channels_accumulates_session_scoped_trip_integrals(
    config: CanaryConfig,
) -> None:
    """Task 1.3 — the cumulative integrals grow tick over tick even though
    each ``tick()`` only sees a ring-buffer window, not the whole session."""
    src = LiveFeatureSource(config=config)
    now = datetime(2026, 5, 13, 12, 0, tzinfo=UTC)
    for i in range(5):
        t = now + timedelta(seconds=i)
        src.observe(TelemetryRecord.obd(t.timestamp(), "RPM", 2500.0, "rpm"))
        src.observe(TelemetryRecord.obd(t.timestamp(), "SPEED", 50.0, "km/h"))
        src.observe(TelemetryRecord.obd(t.timestamp(), "MAF", 10.0, "g/s"))
        src.tick(t)

    derived = src.latest_derived_channels
    assert derived is not None
    # 4 whole seconds of dt have accrued (first tick's dt is 0, matching
    # compute_derived_channels' own first-row convention).
    assert derived["derived:TRIP_DISTANCE_KM"] == pytest.approx(50.0 / 3600.0 * 4)
    assert derived["derived:FUEL_USED_L"] > 0
    assert derived["derived:CO2_KG"] > 0
    assert derived["derived:IDLE_TIME_S"] == 0.0  # never stopped
    assert derived["derived:AVG_ECONOMY_L_PER_100KM"] > 0


def test_trip_integrals_bridge_stale_ticks(config: CanaryConfig) -> None:
    """A tick where SPEED is stale/absent must not drop the elapsed time —
    the next fresh sample bridges the gap (capped). The old shared last-tick
    timestamp advanced on stale ticks too, so a contended bus (SPEED fresh
    on only every ~2nd tick) halved the live trip odometer."""
    src = LiveFeatureSource(config=config)
    now = datetime(2026, 5, 13, 12, 0, tzinfo=UTC)
    # SPEED observed at t=0, then nothing until t=10 — well past the fast
    # channel's ffill ceiling, so intermediate ticks see SPEED as stale.
    src.observe(TelemetryRecord.obd(now.timestamp(), "SPEED", 72.0, "km/h"))
    src.tick(now)
    for i in range(1, 10):
        # Keep the tick loop alive with an unrelated fresh channel.
        t = now + timedelta(seconds=i)
        src.observe(TelemetryRecord.obd(t.timestamp(), "COOLANT_TEMP", 90.0, "degC"))
        src.tick(t)
    late = now + timedelta(seconds=10)
    src.observe(TelemetryRecord.obd(late.timestamp(), "SPEED", 72.0, "km/h"))
    src.tick(late)

    derived = src.latest_derived_channels
    assert derived is not None
    # The full 10 s at 72 kph accrues (0.2 km), not just the ticks where
    # SPEED happened to be fresh.
    assert derived["derived:TRIP_DISTANCE_KM"] == pytest.approx(72.0 / 3600.0 * 10.0)


def test_reset_clears_trip_integral_accumulators(config: CanaryConfig) -> None:
    """Task 1.3 — a session boundary (the same ``reset()`` Phase 7.1 uses for
    replay isolation) must zero the trip integrals, not carry a stale total
    into the next session."""
    src = LiveFeatureSource(config=config)
    now = datetime(2026, 5, 13, 12, 0, tzinfo=UTC)
    for i in range(3):
        t = now + timedelta(seconds=i)
        src.observe(TelemetryRecord.obd(t.timestamp(), "SPEED", 80.0, "km/h"))
        src.observe(TelemetryRecord.obd(t.timestamp(), "MAF", 8.0, "g/s"))
        src.tick(t)
    assert src.latest_derived_channels is not None
    assert src.latest_derived_channels["derived:TRIP_DISTANCE_KM"] > 0

    src.reset()
    assert src.latest_derived_channels is None

    later = now + timedelta(minutes=10)
    src.observe(TelemetryRecord.obd(later.timestamp(), "SPEED", 80.0, "km/h"))
    src.observe(TelemetryRecord.obd(later.timestamp(), "MAF", 8.0, "g/s"))
    src.tick(later)
    # First tick of the new session: dt=0 against the just-reset
    # accumulator state, so distance/fuel start over from zero, not from
    # wherever the previous session left off.
    assert src.latest_derived_channels is not None
    assert src.latest_derived_channels["derived:TRIP_DISTANCE_KM"] == 0.0
    assert src.latest_derived_channels["derived:FUEL_USED_L"] == 0.0


def test_live_trip_integrals_match_batch_compute_derived_channels(
    config: CanaryConfig,
) -> None:
    """Task 1.3's load-bearing parity check: the same input data through the
    batch ``compute_derived_channels`` (a full-session cumsum) and the live
    session-scoped accumulator must land on the same final cumulative
    values — not just "both increase", but numerically equal."""
    from src.analysis.derived import compute_derived_channels

    n = 8
    start = datetime(2026, 5, 13, 12, 0, tzinfo=UTC)
    timestamps = [start + timedelta(seconds=i) for i in range(n)]
    # First two rows idle (RPM up, stopped); rest is a steady-ish drive —
    # exercises every cumulative channel (distance, fuel, CO2, idle, avg
    # economy) in one pass.
    rpm_values = [800.0, 800.0, 1500.0, 1800.0, 2000.0, 2200.0, 2400.0, 2500.0]
    speed_values = [0.0, 0.0, 20.0, 35.0, 50.0, 60.0, 70.0, 80.0]
    maf_values = [3.0, 3.0, 8.0, 9.0, 10.0, 11.0, 12.0, 13.0]

    wide = pd.DataFrame(
        {
            "timestamp": pd.to_datetime([t.isoformat() for t in timestamps], utc=True),
            "obd:RPM": rpm_values,
            "obd:SPEED": speed_values,
            "obd:MAF": maf_values,
        }
    )
    batch = compute_derived_channels(wide, config.vehicle.parameters)
    batch_last = batch.iloc[-1]

    src = LiveFeatureSource(config=config)
    for t, rpm, speed, maf in zip(timestamps, rpm_values, speed_values, maf_values, strict=True):
        src.observe(TelemetryRecord.obd(t.timestamp(), "RPM", rpm, "rpm"))
        src.observe(TelemetryRecord.obd(t.timestamp(), "SPEED", speed, "km/h"))
        src.observe(TelemetryRecord.obd(t.timestamp(), "MAF", maf, "g/s"))
        src.tick(t)

    live = src.latest_derived_channels
    assert live is not None
    for channel in (
        "derived:TRIP_DISTANCE_KM",
        "derived:FUEL_USED_L",
        "derived:CO2_KG",
        "derived:IDLE_TIME_S",
        "derived:AVG_ECONOMY_L_PER_100KM",
    ):
        assert live[channel] == pytest.approx(batch_last[channel]), channel


# ---------------------------------------------------------------------------
# A03.1 — "a sample arrived this tick": offline/live parity
# ---------------------------------------------------------------------------


def test_arrival_flags_match_the_resampler_for_the_same_stream(config: CanaryConfig) -> None:
    """The offline resampler and the live source emit the same per-channel
    ``__arrived`` flag for the same input stream: off-grid samples, a channel
    that goes quiet, a slow channel between polls, and a quality-gate reject
    (which arrives on neither path)."""
    from src.columns import SESSION_COLUMN, TIMESTAMP_COLUMN, arrived_column
    from src.preprocessing.quality_gate import apply_quality_gate
    from src.preprocessing.resampler import resample_config_for, resample_wide

    base = datetime(2026, 5, 13, 12, 0, tzinfo=UTC)
    stream: list[tuple[float, str, float]] = []
    for i in range(20):
        stream.append((i, "SPEED", float(i)))  # on-grid, every tick
        if i not in {5, 6, 7}:
            # Off-grid by 0.3 s; i=10 is the PID 0x0C rollover the gate rejects.
            stream.append((i + 0.3, "RPM", 16383.0 if i == 10 else 800.0 + i))
    for t in (0.0, 7.5, 14.2):
        stream.append((t, "COOLANT_TEMP", 85.0 + t))
    stream.sort()

    src = LiveFeatureSource(config=config)
    live: dict[str, list[bool]] = {"RPM": [], "SPEED": [], "COOLANT_TEMP": []}
    pending = list(stream)
    ticks = 20
    for k in range(ticks):
        now = base + timedelta(seconds=k)
        while pending and pending[0][0] <= k:
            t, channel, value = pending.pop(0)
            src.observe(
                TelemetryRecord.obd((base + timedelta(seconds=t)).timestamp(), channel, value, "")
            )
        prepared = src.prepare_tick(now)
        assert prepared is not None
        row = prepared.wide.iloc[-1]
        for channel in live:
            flag = row.get(arrived_column(f"obd:{channel}"), False)
            live[channel].append(bool(flag) if not pd.isna(flag) else False)

    wide = pd.DataFrame(
        [
            {
                SESSION_COLUMN: "s",
                TIMESTAMP_COLUMN: base + timedelta(seconds=t),
                f"obd:{channel}": value,
            }
            for t, channel, value in stream
        ]
    )
    gated, _quality = apply_quality_gate(wide)
    offline = resample_wide(gated, config=resample_config_for(config))
    assert len(offline) >= ticks
    for channel, flags in live.items():
        expected = offline[arrived_column(f"obd:{channel}")].astype(bool).tolist()[:ticks]
        assert flags == expected, channel

    # Spot-check the semantics, not just the agreement.
    assert live["RPM"][:3] == [False, True, True]  # 0.3 s arrives on the 1 s row
    assert live["RPM"][11] is False  # the rejected 10.3 s sample
    assert [k for k, flag in enumerate(live["COOLANT_TEMP"]) if flag] == [0, 8, 15]


def test_arrival_flags_survive_the_live_feature_transform(config: CanaryConfig) -> None:
    """The sequence engine reads ``__arrived`` off the feature row, so the
    shared wide→features transform must carry it through untouched and never
    grow rolling features from it."""
    src = LiveFeatureSource(config=config)
    base = datetime(2026, 5, 13, 12, 0, tzinfo=UTC)
    row = None
    for k in range(3):
        now = base + timedelta(seconds=k)
        src.observe(TelemetryRecord.obd(now.timestamp(), "SPEED", 10.0, "km/h"))
        if k == 0:
            src.observe(TelemetryRecord.obd(now.timestamp(), "RPM", 900.0, "rpm"))
        row = src.tick(now)
    assert row is not None
    assert bool(row["obd:SPEED__arrived"].iloc[0]) is True
    assert bool(row["obd:RPM__arrived"].iloc[0]) is False
    assert not any(str(c).startswith("obd:SPEED__arrived__") for c in row.columns)
