"""Tests for the live (streaming) feature pipeline (Milestone B)."""

from __future__ import annotations

import math
from datetime import UTC, datetime, timedelta
from pathlib import Path

import numpy as np
import pytest

from src.config import CanaryConfig
from src.decode.bindings import load_bindings
from src.preprocessing.live import LiveFeatureSource
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
    src.observe(TelemetryRecord.obd(1.0, "RPM", 700.0, "rpm"))
    row = src.tick(datetime(2026, 5, 13, 12, 0, tzinfo=UTC))
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
    src.observe(TelemetryRecord.obd(1.0, "RPM", 700.0, "rpm"))
    src.observe(TelemetryRecord.obd(2.0, "RPM", 1500.0, "rpm"))
    row = src.tick(datetime(2026, 5, 13, 12, 0, tzinfo=UTC))
    assert row is not None
    assert row["obd:RPM"].iloc[0] == 1500.0


def test_multiple_channels_coexist(config: CanaryConfig) -> None:
    src = LiveFeatureSource(config=config)
    src.observe(TelemetryRecord.obd(1.0, "RPM", 700.0, "rpm"))
    src.observe(TelemetryRecord.obd(1.0, "SPEED", 25.0, "km/h"))
    row = src.tick(datetime(2026, 5, 13, 12, 0, tzinfo=UTC))
    assert row is not None
    assert row["obd:RPM"].iloc[0] == 700.0
    assert row["obd:SPEED"].iloc[0] == 25.0


# ---------------------------------------------------------------------------
# CAN decode via bindings
# ---------------------------------------------------------------------------


def test_can_record_decodes_via_bindings(config: CanaryConfig) -> None:
    bindings = load_bindings(Path("data/bindings.yaml"))
    src = LiveFeatureSource(config=config, bindings=bindings)
    # 0x551 byte 0 carries COOLANT_TEMP raw with offset −40.
    # Drop a frame with that byte = 0x69 (105 dec). decode_raw extracts 105.
    src.observe(TelemetryRecord.can(1.0, 0x551, bytes([0x69, 0, 0, 0, 0, 0, 0, 0])))
    row = src.tick(datetime(2026, 5, 13, 12, 0, tzinfo=UTC))
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
    bindings = load_bindings(Path("data/bindings.yaml"))
    src = LiveFeatureSource(config=config, bindings=bindings)
    # 0x1F9 binds RPM at bytes 2-3 but the payload only has 1 byte
    src.observe(TelemetryRecord.can(1.0, 0x1F9, b"\x00"))
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
        src.observe(TelemetryRecord.obd(float(i + 1), "RPM", value, "rpm"))
        row = src.tick(base + timedelta(seconds=i))
        assert row is not None
    # window is 30 s, every tick is 1 s → all values are in-window → mean of all 5
    assert row["obd:RPM__mean"].iloc[0] == pytest.approx(np.mean(values))


def test_rolling_std_is_nan_on_single_sample(config: CanaryConfig) -> None:
    src = LiveFeatureSource(config=config)
    src.observe(TelemetryRecord.obd(1.0, "RPM", 700.0, "rpm"))
    row = src.tick(datetime(2026, 5, 13, 12, 0, tzinfo=UTC))
    assert row is not None
    assert math.isnan(row["obd:RPM__std"].iloc[0])


def test_rolling_window_drops_old_observations(config: CanaryConfig) -> None:
    """A value far enough in the past must roll out of the mean."""
    src = LiveFeatureSource(config=config)
    src.observe(TelemetryRecord.obd(1.0, "RPM", 6000.0, "rpm"))  # spike
    src.tick(datetime(2026, 5, 13, 12, 0, tzinfo=UTC))
    # 60 seconds later, push 700.0 — the 6000 should be outside the 30 s window
    base = datetime(2026, 5, 13, 12, 1, tzinfo=UTC)
    src.observe(TelemetryRecord.obd(60.0, "RPM", 700.0, "rpm"))
    row = src.tick(base)
    assert row is not None
    # spike is outside the rolling 30s window now
    assert row["obd:RPM__mean"].iloc[0] == pytest.approx(700.0, abs=0.5)


# ---------------------------------------------------------------------------
# Buffer / introspection
# ---------------------------------------------------------------------------


def test_buffer_caps_at_max_rows(config: CanaryConfig) -> None:
    src = LiveFeatureSource(config=config)
    # _max_rows defaults to max(32, window_seconds * resample_hz * 2)
    # = max(32, 30*1*2) = 60 for the example config
    for i in range(200):
        src.observe(TelemetryRecord.obd(float(i), "RPM", float(700 + i), "rpm"))
        src.tick(datetime(2026, 5, 13, 12, 0, tzinfo=UTC) + timedelta(seconds=i))
    assert src.buffer_size <= 60


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
    # Idle: RPM<=900, SPEED<=0 per the example config
    src.observe(TelemetryRecord.obd(1.0, "RPM", 750.0, "rpm"))
    src.observe(TelemetryRecord.obd(1.0, "SPEED", 0.0, "km/h"))
    src.observe(TelemetryRecord.obd(1.0, "THROTTLE_POS", 10.0, "%"))
    row = src.tick(datetime(2026, 5, 13, 12, 0, tzinfo=UTC))
    assert row is not None
    assert row["operating_mode"].iloc[0] == "idle"


def test_segmenter_falls_back_to_all_without_signal_columns(config: CanaryConfig) -> None:
    src = LiveFeatureSource(config=config)
    # Only a decoded:* channel, no obd:RPM/SPEED/THROTTLE_POS
    src.observe(TelemetryRecord.obd(1.0, "COOLANT_TEMP", 80.0, "C"))
    row = src.tick(datetime(2026, 5, 13, 12, 0, tzinfo=UTC))
    assert row is not None
    assert row["operating_mode"].iloc[0] == "all"
