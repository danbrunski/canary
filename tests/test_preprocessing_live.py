"""Tests for the live (streaming) feature pipeline (Milestone B)."""

from __future__ import annotations

import math
from datetime import UTC, datetime, timedelta
from pathlib import Path

import numpy as np
import pandas as pd
import pytest

from src.config import CanaryConfig
from src.decode.bindings import Bindings, load_bindings
from src.preprocessing.feature_pipeline import build_feature_frame
from src.preprocessing.live import LiveFeatureSource
from src.storage.parquet_writer import write_records_to_parquet
from src.telemetry import TelemetryRecord


@pytest.fixture
def config() -> CanaryConfig:
    return CanaryConfig.from_yaml(Path("canary.example.yaml"))


# ---------------------------------------------------------------------------
# observe + tick basics
# ---------------------------------------------------------------------------


def test_tick_returns_none_before_any_observation(config: CanaryConfig) -> None:
    src = LiveFeatureSource(config=config)
    assert src.tick(datetime(2026, 5, 13, 12, 0, tzinfo=UTC)) is None


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
    from src.preprocessing.resampler import (
        SESSION_COLUMN,
        TIMESTAMP_COLUMN,
        ResampleConfig,
        gap_column,
        resample_wide,
    )

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
        "coolant_temp": None,
        "run_time": None,
        "obd_freshness_seconds": None,
        "can_freshness_seconds": None,
    }
