"""Mode signals built from raw broadcast bytes must land in engineering units.

``decoded:`` channels hold the RAW integer from a binding's byte range by
contract — model features are trained and served on those counts. The
operating-mode segmenter falls back to the same columns when Mode 01 is stale,
and its thresholds are engineering values (rpm 900, coolant 70 degC, speed
1/80 km/h). Before the fix it compared counts to them: ECM_RPM reads 8x rpm,
ECM_COOLANT_TEMP reads degC + 40, WHEEL_SPEED_6F7_C reads 16x km/h. A warm idle
labelled ``unknown``, a cold engine labelled warm, and 60 km/h labelled
``highway``.

These tests start from bytes off the wire, not from numbers typed into a
``decoded:`` column, so a fixture cannot paper over the unit mismatch the way
the old ones did.
"""

from __future__ import annotations

from collections.abc import Mapping
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import cast

import pandas as pd
import pytest

from src.columns import MODE_COLUMN
from src.config import CanaryConfig
from src.dashboard.paths import DEFAULT_BINDINGS_PATH as DAEMON_BINDINGS_PATH
from src.datasets.health import _RANGE_CHANNELS
from src.decode.bindings import Bindings, load_bindings
from src.decode.engineering import DEFAULT_BINDINGS_PATH as DEFAULT_ENGINEERING_BINDINGS_PATH
from src.decode.engineering import (
    UnscaledDecodedChannelError,
    assert_scalable,
    physical_series,
)
from src.features.feature_pipeline import SEGMENTER_SIGNAL_COLUMNS, segment_or_constant
from src.features.live import LiveFeatureSource
from src.preprocessing.segmenter import (
    DEFAULT_SIGNAL_COLUMNS,
    SegmenterConfig,
    segment_operating_modes,
)
from src.telemetry import TelemetryRecord
from tests.configs import example_config

REPO_ROOT = Path(__file__).resolve().parents[1]
BINDINGS_PATH = REPO_ROOT / "data" / "bindings.yaml"
EXAMPLE_YAML = REPO_ROOT / "canary.example.yaml"

#: Real 0x551 frame: byte 0 = 0x82 = 130 raw -> 90 degC.
COOLANT_90C = bytes.fromhex("822d0080000280")
#: Same frame with byte 0 = 0x5A = 90 raw -> 50 degC (cold).
COOLANT_50C = bytes.fromhex("5a2d0080000280")
#: 0x1F9 bytes 2-3 big-endian, 8 counts/rpm: 6000 -> 750 rpm, 16000 -> 2000 rpm.
RPM_750 = bytes([0x00, 0x00, 0x17, 0x70, 0x00, 0x00, 0x00, 0x00])
RPM_2000 = bytes([0x00, 0x00, 0x3E, 0x80, 0x00, 0x00, 0x00, 0x00])
#: 0x6F7 bytes 4-5 big-endian, 16 counts per km/h: 960 -> 60 km/h.
SPEED_0 = bytes(8)
SPEED_60 = bytes([0x00, 0x00, 0x00, 0x00, 0x03, 0xC0, 0x00, 0x00])


@pytest.fixture(scope="module")
def config() -> CanaryConfig:
    return example_config(EXAMPLE_YAML)


@pytest.fixture(scope="module")
def bindings() -> Bindings:
    return load_bindings(BINDINGS_PATH)


def _decoded_row(
    source: LiveFeatureSource, frames: list[tuple[int, bytes]], ts: datetime
) -> dict[str, float]:
    """Raw frames -> ``decode_for_telemetry`` -> one wide row of ``decoded:`` values."""
    row: dict[str, float] = {}
    for can_id, payload in frames:
        record = TelemetryRecord.can(ts.timestamp(), can_id, payload)
        for decoded in source.decode_for_telemetry(record):
            row[f"decoded:{decoded.channel}"] = float(cast(float, decoded.value))
    return row


def _stale_obd_frame(rows: list[tuple[str, dict[str, float]]]) -> pd.DataFrame:
    """Wide frame whose Mode 01 columns exist but are NaN (stale) on every row."""
    base = datetime(2026, 9, 18, 12, 0, tzinfo=UTC)
    records = []
    for i, (session, decoded) in enumerate(rows):
        records.append(
            {
                "session": session,
                "timestamp": pd.Timestamp(base + timedelta(seconds=i)),
                "obd:RPM": float("nan"),
                "obd:SPEED": float("nan"),
                "obd:THROTTLE_POS": float("nan"),
                "obd:COOLANT_TEMP": float("nan"),
                **decoded,
            }
        )
    return pd.DataFrame(records)


def test_decode_for_telemetry_keeps_raw_counts(config: CanaryConfig, bindings: Bindings) -> None:
    """The contract the fallback must respect: decoded rows stay raw."""
    source = LiveFeatureSource(config=config, bindings=bindings)
    ts = datetime(2026, 9, 18, 12, 0, tzinfo=UTC)
    row = _decoded_row(source, [(0x551, COOLANT_90C), (0x1F9, RPM_750), (0x6F7, SPEED_60)], ts)

    assert row["decoded:ECM_COOLANT_TEMP"] == 130.0
    assert row["decoded:ECM_RPM"] == 6000.0
    assert row["decoded:WHEEL_SPEED_6F7_C"] == 960.0


def test_raw_bytes_segment_to_idle_warmup_and_city_with_mode01_stale(
    config: CanaryConfig, bindings: Bindings
) -> None:
    """Warm idle / cold idle / 60 km/h — each from bytes, Mode 01 stale.

    Pre-fix labels were ``unknown`` (6000 "rpm" matches no idle band),
    ``unknown`` (130 "degC" is not cold, 6000 is not idle) and ``highway``
    (960 "km/h" clears 80).
    """
    source = LiveFeatureSource(config=config, bindings=bindings)
    ts = datetime(2026, 9, 18, 12, 0, tzinfo=UTC)
    frame = _stale_obd_frame(
        [
            (
                "warm-idle",
                _decoded_row(
                    source, [(0x551, COOLANT_90C), (0x1F9, RPM_750), (0x6F7, SPEED_0)], ts
                ),
            ),
            (
                "cold-idle",
                _decoded_row(
                    source, [(0x551, COOLANT_50C), (0x1F9, RPM_750), (0x6F7, SPEED_0)], ts
                ),
            ),
            (
                "city",
                _decoded_row(
                    source, [(0x551, COOLANT_90C), (0x1F9, RPM_2000), (0x6F7, SPEED_60)], ts
                ),
            ),
        ]
    )

    segmented, fell_back = segment_or_constant(frame, config)

    assert not fell_back
    assert list(segmented[MODE_COLUMN]) == ["idle", "warmup", "city"]


def test_segmenter_output_keeps_decoded_columns_raw(
    config: CanaryConfig, bindings: Bindings
) -> None:
    """Scaling is for the mode rules only — the feature columns stay counts."""
    source = LiveFeatureSource(config=config, bindings=bindings)
    ts = datetime(2026, 9, 18, 12, 0, tzinfo=UTC)
    frame = _stale_obd_frame(
        [
            (
                "s",
                _decoded_row(
                    source, [(0x551, COOLANT_90C), (0x1F9, RPM_750), (0x6F7, SPEED_0)], ts
                ),
            )
        ]
    )

    segmented, _ = segment_or_constant(frame, config)

    assert segmented["decoded:ECM_RPM"].iloc[0] == 6000.0
    assert segmented["decoded:ECM_COOLANT_TEMP"].iloc[0] == 130.0


def test_live_tick_labels_idle_from_broadcast_when_mode01_goes_stale(
    config: CanaryConfig, bindings: Bindings
) -> None:
    """The real serve path: Mode 01 seen once, then only broadcast frames."""
    source = LiveFeatureSource(config=config, bindings=bindings)
    t0 = datetime(2026, 9, 18, 12, 0, tzinfo=UTC)
    for channel, value, unit in (
        ("RPM", 750.0, "rpm"),
        ("SPEED", 0.0, "km/h"),
        ("THROTTLE_POS", 12.0, "%"),
        ("COOLANT_TEMP", 90.0, "degC"),
    ):
        source.observe(TelemetryRecord.obd(t0.timestamp(), channel, value, unit))
    # Past the slow-channel ceiling so even obd:COOLANT_TEMP reads stale.
    later = t0 + timedelta(seconds=config.preprocessing.slow_channel_max_gap_seconds + 10)
    for can_id, payload in ((0x551, COOLANT_90C), (0x1F9, RPM_750), (0x6F7, SPEED_0)):
        source.observe(TelemetryRecord.can(later.timestamp(), can_id, payload))

    source.tick(later)

    point = source.latest_operating_point
    assert point is not None
    assert point[MODE_COLUMN] == "idle"


def test_live_vehicle_state_broadcast_rpm_is_engineering(
    config: CanaryConfig, bindings: Bindings
) -> None:
    source = LiveFeatureSource(config=config, bindings=bindings)
    now = datetime(2026, 9, 18, 12, 0, tzinfo=UTC)
    source.observe(TelemetryRecord.can(now.timestamp(), 0x1F9, RPM_750))

    assert source.vehicle_state_inputs(now)["broadcast_rpm"] == pytest.approx(750.0)


def test_live_idle_integrator_uses_broadcast_rpm_in_rpm(
    config: CanaryConfig, bindings: Bindings
) -> None:
    """Idle time accrues on a warm idle seen only through broadcast rpm.

    Pre-fix the raw count went straight into the 200 rpm idle floor, so a
    cranking 100 rpm (800 counts) read as idling.
    """
    source = LiveFeatureSource(config=config, bindings=bindings)
    t0 = datetime(2026, 9, 18, 12, 0, tzinfo=UTC)
    point = {"obd:SPEED": 0.0, "decoded:ECM_RPM": 6000.0}
    source._accumulate_derived_integrals(t0, point)
    out = source._accumulate_derived_integrals(t0 + timedelta(seconds=4), point)
    assert out["derived:IDLE_TIME_S"] == pytest.approx(4.0)

    # 800 counts = 100 rpm: below the 200 rpm idle floor once scaled.
    source.reset()
    stalled = {"obd:SPEED": 0.0, "decoded:ECM_RPM": 800.0}
    source._accumulate_derived_integrals(t0, stalled)
    out = source._accumulate_derived_integrals(t0 + timedelta(seconds=4), stalled)
    assert out["derived:IDLE_TIME_S"] == pytest.approx(0.0)


# ---------------------------------------------------------------------------
# Make the unsafe form unwritable
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "table",
    [DEFAULT_SIGNAL_COLUMNS, SEGMENTER_SIGNAL_COLUMNS, _RANGE_CHANNELS],
    ids=["default", "training", "dataset-health"],
)
def test_every_decoded_threshold_candidate_has_pinned_scaling(
    table: Mapping[str, str | tuple[str, ...]], bindings: Bindings
) -> None:
    """Every candidate table that feeds an engineering threshold or report.

    ``decoded:VEHICLE_SPEED`` — a name no binding ever produced — sat in three
    of these lists for a year and silently did nothing. ``assert_scalable`` is
    the refusal the segmenter now runs on its own table at construction; this
    runs it over the declared tables, so a dead or unpinned name cannot be
    added to any of them without a red test.
    """
    columns = [
        column
        for candidates in table.values()
        for column in ((candidates,) if isinstance(candidates, str) else candidates)
    ]
    assert_scalable(columns, bindings)
    for column in columns:
        if column.startswith("decoded:"):
            assert physical_series(pd.Series([100.0]), column, bindings).notna().all(), column


def test_the_accessors_default_catalog_is_the_one_the_daemon_decodes_with() -> None:
    """The live path scales mode signals with the repo catalog rather than
    shipping ``LiveFeatureSource.bindings`` across the scoring-process boundary
    each tick — sound only while the two are the same file."""
    assert DEFAULT_ENGINEERING_BINDINGS_PATH == DAEMON_BINDINGS_PATH


def test_segmenter_refuses_an_unscaled_decoded_column_in_the_frame(config: CanaryConfig) -> None:
    """A catalog that positions a signal but never pinned its scale.

    Its counts have no engineering value, so the threshold must never see
    them — refused when the column is read, which is also the path that
    catches a stored ``decoded:`` column a caller-supplied ``--bindings``
    cannot explain.
    """
    unscaled = Bindings.model_validate(
        {
            "schema_version": 1,
            "signals": [
                {
                    "name": "MYSTERY_RPM",
                    "module": "ECM",
                    "can_id": 0x123,
                    "byte_start": 0,
                    "byte_length": 2,
                    "confidence": "low",
                }
            ],
        }
    )
    frame = pd.DataFrame({"obd:RPM": [float("nan")], "decoded:MYSTERY_RPM": [6000.0]})
    seg = SegmenterConfig(
        signal_columns={"rpm": ("obd:RPM", "decoded:MYSTERY_RPM")}, bindings=unscaled
    )
    with pytest.raises(UnscaledDecodedChannelError, match="MYSTERY_RPM"):
        segment_operating_modes(frame, config.preprocessing.operating_modes, config=seg)


def test_segmenter_refuses_a_decoded_candidate_with_no_binding() -> None:
    """Declared against the repo catalog, so a dead name fails at construction
    — no frame needed, no session where the primary PID happens to go stale."""
    with pytest.raises(UnscaledDecodedChannelError, match="NOT_A_BINDING"):
        SegmenterConfig(signal_columns={"rpm": ("obd:RPM", "decoded:NOT_A_BINDING")})


def test_segment_or_constant_scales_with_the_catalog_it_is_given(config: CanaryConfig) -> None:
    """``--bindings`` reaches the mode rules: the catalog that decoded the
    counts is the catalog that scales them."""
    halved = Bindings.model_validate(
        {
            "schema_version": 1,
            "signals": [
                {
                    "name": "ECM_RPM",
                    "module": "ECM",
                    "can_id": 0x1F9,
                    "byte_start": 2,
                    "byte_length": 2,
                    "scale": 0.0625,
                    "unit": "rpm",
                    "confidence": "low",
                },
                {
                    "name": "ECM_COOLANT_TEMP",
                    "module": "ECM",
                    "can_id": 0x551,
                    "byte_start": 0,
                    "byte_length": 1,
                    "scale": 1.0,
                    "offset": -40.0,
                    "unit": "degC",
                    "confidence": "low",
                },
                {
                    "name": "WHEEL_SPEED_6F7_C",
                    "module": "ABS",
                    "can_id": 0x6F7,
                    "byte_start": 4,
                    "byte_length": 2,
                    "scale": 0.0625,
                    "unit": "km/h",
                    "confidence": "low",
                },
            ],
        }
    )
    # 6000 counts is 750 rpm under the repo catalog (0.125) and 375 under this
    # one — below the 400 rpm idle floor, so the label has to change.
    frame = _stale_obd_frame(
        [("s", {"decoded:ECM_RPM": 6000.0, "decoded:ECM_COOLANT_TEMP": 130.0})]
    )
    frame["decoded:WHEEL_SPEED_6F7_C"] = 0.0

    assert segment_or_constant(frame, config)[0][MODE_COLUMN].iloc[0] == "idle"
    assert segment_or_constant(frame, config, bindings=halved)[0][MODE_COLUMN].iloc[0] != "idle"
