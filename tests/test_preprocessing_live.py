"""Tests for the live (streaming) feature pipeline (Milestone B)."""

from __future__ import annotations

import math
from datetime import UTC, datetime, timedelta
from pathlib import Path

import numpy as np
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
# Buffer / introspection
# ---------------------------------------------------------------------------


def test_buffer_caps_at_max_rows(config: CanaryConfig) -> None:
    src = LiveFeatureSource(config=config)
    # _max_rows = max(32, window_seconds*hz*2, idle_transient_lookback_seconds*hz + hz)
    # = max(32, 30*1*2, 180*1 + 1) = 181 for the example config (lookback dominates).
    for i in range(400):
        src.observe(TelemetryRecord.obd(float(i), "RPM", float(700 + i), "rpm"))
        src.tick(datetime(2026, 5, 13, 12, 0, tzinfo=UTC) + timedelta(seconds=i))
    assert src.buffer_size <= 181


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
    # warmup vs idle in the example config splits on coolant_temp (cold
    # idle is its own mode, see canary.example.yaml). Warm coolant + idle
    # RPM/speed → idle.
    src.observe(TelemetryRecord.obd(1.0, "RPM", 750.0, "rpm"))
    src.observe(TelemetryRecord.obd(1.0, "SPEED", 0.0, "km/h"))
    src.observe(TelemetryRecord.obd(1.0, "THROTTLE_POS", 10.0, "%"))
    src.observe(TelemetryRecord.obd(1.0, "COOLANT_TEMP", 85.0, "C"))
    row = src.tick(datetime(2026, 5, 13, 12, 0, tzinfo=UTC))
    assert row is not None
    assert row["operating_mode"].iloc[0] == "idle"


def test_segmenter_picks_warmup_when_coolant_temp_is_cold(config: CanaryConfig) -> None:
    """Cold idle (stopped + cold coolant) now lands in its own ``warmup`` bucket
    rather than the steady-state ``idle`` mode, so per-mode behavior fits
    don't conflate the two regimes.
    """
    src = LiveFeatureSource(config=config)
    src.observe(TelemetryRecord.obd(1.0, "RPM", 750.0, "rpm"))
    src.observe(TelemetryRecord.obd(1.0, "SPEED", 0.0, "km/h"))
    src.observe(TelemetryRecord.obd(1.0, "THROTTLE_POS", 10.0, "%"))
    src.observe(TelemetryRecord.obd(1.0, "COOLANT_TEMP", 30.0, "C"))
    row = src.tick(datetime(2026, 5, 13, 12, 0, tzinfo=UTC))
    assert row is not None
    assert row["operating_mode"].iloc[0] == "warmup"


def test_segmenter_falls_back_to_all_without_signal_columns(config: CanaryConfig) -> None:
    src = LiveFeatureSource(config=config)
    # Only a decoded:* channel, no obd:RPM/SPEED/THROTTLE_POS
    src.observe(TelemetryRecord.obd(1.0, "COOLANT_TEMP", 80.0, "C"))
    row = src.tick(datetime(2026, 5, 13, 12, 0, tzinfo=UTC))
    assert row is not None
    assert row["operating_mode"].iloc[0] == "all"
