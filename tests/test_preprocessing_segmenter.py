"""Tests for operating-mode segmentation."""

from __future__ import annotations

from pathlib import Path

import pandas as pd

from src.config import CanaryConfig, OperatingModeSpec
from src.preprocessing.segmenter import (
    MODE_COLUMN,
    UNKNOWN_MODE,
    SegmenterConfig,
    default_segmenter_config,
    mode_for_row,
    segment_operating_modes,
)

REPO_ROOT = Path(__file__).resolve().parents[1]
EXAMPLE_YAML = REPO_ROOT / "canary.example.yaml"


def _modes() -> dict[str, OperatingModeSpec]:
    return {
        "idle": OperatingModeSpec(rpm_max=900, speed_max=0),
        "city": OperatingModeSpec(speed_max=50),
        "highway": OperatingModeSpec(speed_min=80),
        "wot": OperatingModeSpec(throttle_min=80),
    }


def test_segment_operating_modes_labels_rows_by_thresholds() -> None:
    frame = pd.DataFrame(
        [
            {"obd:RPM": 900.0, "obd:SPEED": 0.0, "obd:THROTTLE_POS": 5.0},
            {"obd:RPM": 1500.0, "obd:SPEED": 35.0, "obd:THROTTLE_POS": 20.0},
            {"obd:RPM": 2200.0, "obd:SPEED": 90.0, "obd:THROTTLE_POS": 30.0},
            {"obd:RPM": 3200.0, "obd:SPEED": 65.0, "obd:THROTTLE_POS": 82.0},
            {"obd:RPM": 1000.0, "obd:SPEED": 65.0, "obd:THROTTLE_POS": 20.0},
        ]
    )

    segmented = segment_operating_modes(frame, _modes())

    assert list(segmented[MODE_COLUMN]) == ["idle", "city", "highway", "wot", UNKNOWN_MODE]


def test_segment_operating_modes_uses_configured_mode_order_for_precedence() -> None:
    frame = pd.DataFrame([{"obd:RPM": 2000.0, "obd:SPEED": 35.0, "obd:THROTTLE_POS": 90.0}])

    segmented = segment_operating_modes(frame, _modes())

    assert segmented.loc[0, MODE_COLUMN] == "city"


def test_segment_operating_modes_treats_min_and_max_bounds_as_inclusive() -> None:
    modes = {
        "band": OperatingModeSpec(
            rpm_min=1000,
            rpm_max=2000,
            speed_min=10,
            speed_max=20,
            throttle_min=30,
            throttle_max=40,
        )
    }
    frame = pd.DataFrame(
        [
            {"obd:RPM": 1000.0, "obd:SPEED": 10.0, "obd:THROTTLE_POS": 30.0},
            {"obd:RPM": 2000.0, "obd:SPEED": 20.0, "obd:THROTTLE_POS": 40.0},
            {"obd:RPM": 999.9, "obd:SPEED": 10.0, "obd:THROTTLE_POS": 30.0},
            {"obd:RPM": 1000.0, "obd:SPEED": 20.1, "obd:THROTTLE_POS": 30.0},
            {"obd:RPM": 1000.0, "obd:SPEED": 10.0, "obd:THROTTLE_POS": 40.1},
        ]
    )

    segmented = segment_operating_modes(frame, modes)

    assert list(segmented[MODE_COLUMN]) == [
        "band",
        "band",
        UNKNOWN_MODE,
        UNKNOWN_MODE,
        UNKNOWN_MODE,
    ]


def test_segment_operating_modes_supports_custom_mode_names() -> None:
    frame = pd.DataFrame([{"obd:RPM": 1800.0, "obd:SPEED": 20.0, "obd:THROTTLE_POS": 25.0}])
    modes = {
        "technical_trail": OperatingModeSpec(speed_max=30, throttle_max=40),
    }

    segmented = segment_operating_modes(frame, modes)

    assert segmented.loc[0, MODE_COLUMN] == "technical_trail"


def test_segment_operating_modes_marks_missing_required_values_unknown() -> None:
    frame = pd.DataFrame(
        [
            {"obd:RPM": None, "obd:SPEED": 0.0, "obd:THROTTLE_POS": 5.0},
            {"obd:RPM": 800.0, "obd:SPEED": None, "obd:THROTTLE_POS": 5.0},
        ]
    )

    segmented = segment_operating_modes(frame, {"idle": _modes()["idle"]})

    assert list(segmented[MODE_COLUMN]) == [UNKNOWN_MODE, UNKNOWN_MODE]


def test_segment_operating_modes_ignores_empty_mode_specs() -> None:
    frame = pd.DataFrame([{"obd:RPM": 850.0, "obd:SPEED": 0.0, "obd:THROTTLE_POS": 5.0}])

    segmented = segment_operating_modes(frame, {"empty": OperatingModeSpec()})

    assert segmented.loc[0, MODE_COLUMN] == UNKNOWN_MODE


def test_segment_operating_modes_allows_explicit_signal_columns() -> None:
    frame = pd.DataFrame([{"rpm": 850.0, "speed_kmh": 0.0, "throttle_pct": 8.0}])
    config = SegmenterConfig(
        signal_columns={
            "rpm": "rpm",
            "speed": "speed_kmh",
            "throttle": "throttle_pct",
        },
        unknown_mode="unclassified",
        output_column="mode",
    )

    segmented = segment_operating_modes(frame, _modes(), config=config)

    assert segmented.loc[0, "mode"] == "idle"


def test_default_segmenter_config_detects_lowercase_columns() -> None:
    frame = pd.DataFrame(columns=["obd:rpm", "obd:speed", "obd:throttle_pos"])

    config = default_segmenter_config(frame)

    # Each signal keeps the (ordered) tuple of present candidate columns.
    assert config.signal_columns == {
        "rpm": ("obd:rpm",),
        "speed": ("obd:speed",),
        "throttle": ("obd:throttle_pos",),
    }


def test_mode_for_row_returns_first_matching_mode() -> None:
    row = pd.Series({"rpm": 850.0, "speed": 0.0, "throttle": 5.0})
    config = SegmenterConfig(
        signal_columns={"rpm": "rpm", "speed": "speed", "throttle": "throttle"}
    )

    assert mode_for_row(row, _modes(), config=config) == "idle"


def test_segment_operating_modes_uses_coolant_temp_to_split_warmup_from_idle() -> None:
    """Adding ``coolant_temp_max`` to a mode pulls cold-start data into its own
    bucket so the behavior models don't have to cover both regimes with one
    surface (the May 2026 R²=0.41 coolant_warmup mess).
    """
    modes = {
        "warmup": OperatingModeSpec(speed_max=0, coolant_temp_max=70),
        "idle": OperatingModeSpec(rpm_max=900, speed_max=0, coolant_temp_min=70),
    }
    frame = pd.DataFrame(
        [
            # Cold idle (cold coolant) — should go to warmup
            {"obd:RPM": 800.0, "obd:SPEED": 0.0, "obd:COOLANT_TEMP": 30.0},
            # Warm idle (warm coolant) — should go to idle
            {"obd:RPM": 800.0, "obd:SPEED": 0.0, "obd:COOLANT_TEMP": 85.0},
            # Boundary at coolant_temp_max=70 — inclusive, so 70.0 matches warmup
            {"obd:RPM": 800.0, "obd:SPEED": 0.0, "obd:COOLANT_TEMP": 70.0},
        ]
    )
    segmented = segment_operating_modes(frame, modes)
    assert list(segmented[MODE_COLUMN]) == ["warmup", "idle", "warmup"]


def test_segment_operating_modes_marks_warmup_unknown_when_coolant_temp_missing() -> None:
    """If a mode requires coolant_temp but it's not in the frame, that mode is
    simply skipped — rows fall through to the next matching rule or unknown.
    """
    modes = {
        "warmup": OperatingModeSpec(speed_max=0, coolant_temp_max=70),
        "idle": OperatingModeSpec(rpm_max=900, speed_max=0),
    }
    frame = pd.DataFrame(
        [
            {"obd:RPM": 800.0, "obd:SPEED": 0.0},
        ]
    )
    segmented = segment_operating_modes(frame, modes)
    # warmup requires coolant_temp, which isn't in the frame, so warmup is
    # skipped; idle still matches.
    assert list(segmented[MODE_COLUMN]) == ["idle"]


def test_default_segmenter_config_picks_up_coolant_temp_column() -> None:
    frame = pd.DataFrame(columns=["obd:RPM", "obd:SPEED", "obd:THROTTLE_POS", "obd:COOLANT_TEMP"])
    config = default_segmenter_config(frame)
    assert config.signal_columns["coolant_temp"] == ("obd:COOLANT_TEMP",)


def test_default_segmenter_config_prefers_obd_speed_with_gps_fallback() -> None:
    """When both speed sources are present, OBD (reliable VSS) leads, GPS trails."""
    frame = pd.DataFrame(columns=["obd:RPM", "obd:SPEED", "gps:speed"])
    config = default_segmenter_config(frame)
    assert config.signal_columns["speed"] == ("obd:SPEED", "gps:speed")


def test_segment_coalesces_obd_speed_over_gps_per_row() -> None:
    """OBD speed wins where present; rows without OBD fall back to GPS."""
    frame = pd.DataFrame(
        [
            # OBD present reads highway; a jittery GPS disagrees → OBD wins.
            {"obd:RPM": 2200.0, "obd:SPEED": 95.0, "gps:speed": 5.0},
            # OBD speed missing this row (NaN) → coalesce falls back to GPS city speed.
            {"obd:RPM": 1500.0, "obd:SPEED": float("nan"), "gps:speed": 35.0},
        ]
    )
    modes = {
        "city": OperatingModeSpec(speed_max=50),
        "highway": OperatingModeSpec(speed_min=80),
    }
    segmented = segment_operating_modes(frame, modes)
    assert list(segmented[MODE_COLUMN]) == ["highway", "city"]


def test_segment_uses_gps_speed_when_obd_speed_absent() -> None:
    """Pure-GPS frame (no OBD speed column at all) still segments by speed."""
    frame = pd.DataFrame(
        [
            {"obd:RPM": 2200.0, "gps:speed": 95.0},
            {"obd:RPM": 1500.0, "gps:speed": 30.0},
        ]
    )
    modes = {
        "city": OperatingModeSpec(speed_max=50),
        "highway": OperatingModeSpec(speed_min=80),
    }
    segmented = segment_operating_modes(frame, modes)
    assert list(segmented[MODE_COLUMN]) == ["highway", "city"]


# ---------------------------------------------------------------------------
# idle_transient (recent-motion sub-mode)
# ---------------------------------------------------------------------------


def _idle_split_modes() -> dict[str, OperatingModeSpec]:
    """Warm-idle band split into transient (recent motion) vs standing."""
    return {
        "idle_transient": OperatingModeSpec(
            rpm_max=900, speed_max=0, coolant_temp_min=70, recent_speed_min=8
        ),
        "idle": OperatingModeSpec(rpm_max=900, speed_max=0, coolant_temp_min=70),
        "city": OperatingModeSpec(speed_max=50),
    }


def _drive_then_stop_frame(drive_s: int, stop_s: int, *, session: str = "A") -> pd.DataFrame:
    t0 = pd.Timestamp("2026-05-24T19:00:00Z")
    rows = []
    for i in range(drive_s):
        rows.append(
            {
                "session": session,
                "timestamp": t0 + pd.Timedelta(seconds=i),
                "obd:RPM": 1500.0,
                "obd:SPEED": 40.0,
                "obd:COOLANT_TEMP": 90.0,
            }
        )
    for i in range(drive_s, drive_s + stop_s):
        rows.append(
            {
                "session": session,
                "timestamp": t0 + pd.Timedelta(seconds=i),
                "obd:RPM": 750.0,
                "obd:SPEED": 0.0,
                "obd:COOLANT_TEMP": 90.0,
            }
        )
    return pd.DataFrame(rows)


def test_idle_transient_after_recent_motion() -> None:
    """A stop within the lookback of driving is idle_transient, not idle."""
    frame = _drive_then_stop_frame(drive_s=5, stop_s=6)
    out = segment_operating_modes(frame, _idle_split_modes(), recent_motion_window_seconds=30)
    modes = list(out[MODE_COLUMN])
    assert modes[:5] == ["city"] * 5
    assert modes[5:] == ["idle_transient"] * 6


def test_idle_standing_from_rest_is_idle() -> None:
    """Stopped from the start (no prior motion) is steady idle, never transient."""
    frame = _drive_then_stop_frame(drive_s=0, stop_s=8)
    out = segment_operating_modes(frame, _idle_split_modes(), recent_motion_window_seconds=30)
    assert list(out[MODE_COLUMN]) == ["idle"] * 8


def test_idle_transient_decays_to_idle_past_lookback() -> None:
    """Once the last motion falls out of the window, a long stop becomes idle."""
    frame = _drive_then_stop_frame(drive_s=3, stop_s=20)
    out = segment_operating_modes(frame, _idle_split_modes(), recent_motion_window_seconds=8)
    modes = list(out[MODE_COLUMN])
    assert modes[:3] == ["city"] * 3
    # Motion (last at second 2) stays in an 8 s trailing window briefly, then the
    # stop decays to standing idle.
    assert "idle_transient" in modes
    assert modes[-1] == "idle"


def test_recent_speed_does_not_bleed_across_sessions() -> None:
    """Motion in one session must not make another session's idle transient."""
    moved = _drive_then_stop_frame(drive_s=4, stop_s=3, session="A")
    rested = _drive_then_stop_frame(drive_s=0, stop_s=7, session="B")
    frame = pd.concat([moved, rested], ignore_index=True)
    out = segment_operating_modes(frame, _idle_split_modes(), recent_motion_window_seconds=60)
    assert set(out[out.session == "B"][MODE_COLUMN]) == {"idle"}


def test_recent_speed_is_ephemeral_not_a_column() -> None:
    """The derived recent_speed must never leak into the output frame."""
    frame = _drive_then_stop_frame(drive_s=3, stop_s=3)
    out = segment_operating_modes(frame, _idle_split_modes(), recent_motion_window_seconds=30)
    assert "recent_speed" not in out.columns


def test_idle_transient_skipped_without_timestamp() -> None:
    """No timestamp column → recent_speed can't be derived; warm stops are idle."""
    frame = pd.DataFrame(
        [
            {"obd:RPM": 750.0, "obd:SPEED": 0.0, "obd:COOLANT_TEMP": 90.0},
            {"obd:RPM": 750.0, "obd:SPEED": 0.0, "obd:COOLANT_TEMP": 90.0},
        ]
    )
    out = segment_operating_modes(frame, _idle_split_modes())
    assert list(out[MODE_COLUMN]) == ["idle", "idle"]


def _warmup_modes() -> dict[str, OperatingModeSpec]:
    return {
        "warmup": OperatingModeSpec(coolant_temp_max=60),
        "idle": OperatingModeSpec(rpm_max=1200, speed_max=0),
    }


def test_stale_coolant_falls_to_unknown_instead_of_warmup() -> None:
    """1.3 regression: a gap-flagged (frozen/forward-filled) coolant reading
    must not confidently drive the warmup/idle split. A cold-stuck sensor
    reading e.g. 40C forever would otherwise misclassify a fully warm engine
    as perpetually warming up."""
    frame = pd.DataFrame(
        [
            {
                "obd:RPM": 800.0,
                "obd:SPEED": 0.0,
                "obd:COOLANT_TEMP": 40.0,
                "obd:COOLANT_TEMP__gap": False,
            },
            {
                "obd:RPM": 800.0,
                "obd:SPEED": 0.0,
                "obd:COOLANT_TEMP": 40.0,
                "obd:COOLANT_TEMP__gap": True,
            },
        ]
    )

    out = segment_operating_modes(frame, _warmup_modes())

    # Row 0: fresh reading, genuinely cold -> warmup.
    assert out.loc[0, MODE_COLUMN] == "warmup"
    # Row 1: same raw value, but flagged stale -> treated as absent, not a
    # confident warmup classification. Falls through to idle (coolant not
    # required there) rather than unknown, since idle only gates on rpm/speed.
    assert out.loc[1, MODE_COLUMN] == "idle"


def test_stale_coolant_falls_to_unknown_when_no_fallback_mode_matches() -> None:
    frame = pd.DataFrame(
        [
            {"obd:RPM": 5000.0, "obd:COOLANT_TEMP": 40.0, "obd:COOLANT_TEMP__gap": True},
        ]
    )

    out = segment_operating_modes(frame, _warmup_modes())

    assert out.loc[0, MODE_COLUMN] == UNKNOWN_MODE


def test_fresh_coolant_without_gap_column_still_segments() -> None:
    """No __gap column present at all (pre-resampler frame) -> treated as
    always-fresh, not always-stale — gap_masked only fires when a resampler
    actually produced the flag."""
    frame = pd.DataFrame([{"obd:RPM": 800.0, "obd:COOLANT_TEMP": 40.0}])

    out = segment_operating_modes(frame, _warmup_modes())

    assert out.loc[0, MODE_COLUMN] == "warmup"


# ---------------------------------------------------------------------------
# 5.1 — mode-taxonomy fall-throughs closed (real canary.example.yaml config)
# ---------------------------------------------------------------------------


def test_warm_fast_idle_no_longer_falls_through_to_city() -> None:
    """Regression: ``city`` used to constrain only ``speed_max``, so a warm,
    >900 rpm standing idle (A/C-loaded / fast idle) matched no rpm-gated mode
    and fell through into ``city`` on speed alone. ``city`` now also requires
    ``speed_min``, and ``idle_high`` covers the >900-1200 rpm standing band.
    """
    config = CanaryConfig.from_yaml(EXAMPLE_YAML)
    frame = pd.DataFrame([{"obd:RPM": 1000.0, "obd:SPEED": 0.0, "obd:COOLANT_TEMP": 90.0}])

    segmented = segment_operating_modes(frame, config.preprocessing.operating_modes)

    assert segmented.loc[0, MODE_COLUMN] != "city"
    assert segmented.loc[0, MODE_COLUMN] == "idle_high"


def test_50_to_80_kmh_band_classified_as_city() -> None:
    """Regression: 50-80 km/h once fell through to ``unknown`` (city capped at
    50, highway floored at 80). It briefly had its own ``suburban`` band, but
    50-80 reads as "city moving faster" on real drives, so ``city`` now spans
    1-80 and scores the band with the existing city model."""
    config = CanaryConfig.from_yaml(EXAMPLE_YAML)
    frame = pd.DataFrame([{"obd:RPM": 1800.0, "obd:SPEED": 65.0, "obd:COOLANT_TEMP": 90.0}])

    segmented = segment_operating_modes(frame, config.preprocessing.operating_modes)

    assert segmented.loc[0, MODE_COLUMN] not in {UNKNOWN_MODE, "highway"}
    assert segmented.loc[0, MODE_COLUMN] == "city"


def test_low_speed_creep_no_longer_falls_through_to_unknown() -> None:
    """Regression (5.1 follow-up): 5.1's ``city.speed_min: 5`` closed the
    >900 rpm standing-idle fall-through, but the idle bands are strict
    ``speed_max: 0`` (no tolerance band) — so it opened a *new* 0-5 km/h
    dead zone. A running engine creeping at low-but-nonzero speed (parking
    lot, stop-and-go traffic) matched no mode at all. speed_min: 1 closes
    it without reopening the original fall-through."""
    config = CanaryConfig.from_yaml(EXAMPLE_YAML)
    frame = pd.DataFrame([{"obd:RPM": 1500.0, "obd:SPEED": 3.0, "obd:COOLANT_TEMP": 90.0}])

    segmented = segment_operating_modes(frame, config.preprocessing.operating_modes)

    assert segmented.loc[0, MODE_COLUMN] != UNKNOWN_MODE
    assert segmented.loc[0, MODE_COLUMN] == "city"


def test_high_rpm_standing_still_does_not_fall_through_to_city() -> None:
    """The low-speed-creep fix above must not reopen the original 5.1 bug:
    a warm, >900 rpm, exactly-stopped row (idle_high's own upper rpm bound
    exceeded — no rpm-gated mode matches) still must not fall through to
    ``city`` now that speed_min dropped from 5 to 1."""
    config = CanaryConfig.from_yaml(EXAMPLE_YAML)
    frame = pd.DataFrame([{"obd:RPM": 1500.0, "obd:SPEED": 0.0, "obd:COOLANT_TEMP": 90.0}])

    segmented = segment_operating_modes(frame, config.preprocessing.operating_modes)

    assert segmented.loc[0, MODE_COLUMN] != "city"


# ---------------------------------------------------------------------------
# Phase 2a.7 — accel_lon/accel_lat mode gates
# ---------------------------------------------------------------------------


def test_accel_lon_and_accel_lat_gates_match_when_columns_present() -> None:
    # imu:ACCEL_LON_MAX/ACCEL_LAT_MAX are decimated as ``.abs().max()``
    # (imu_decimation.decimate_imu_wide) and so are always non-negative;
    # hard_brake gates on a positive accel_lon_min, matching the shipped
    # canary.example.yaml spec and AutoTagConfig.hard_brake_g's sign
    # convention.
    modes = {
        "hard_brake": OperatingModeSpec(accel_lon_min=0.4),
        "cornering": OperatingModeSpec(accel_lat_min=0.3),
    }
    frame = pd.DataFrame(
        [
            {"imu:ACCEL_LON_MAX": 0.6, "imu:ACCEL_LAT_MAX": 0.1},
            {"imu:ACCEL_LON_MAX": 0.1, "imu:ACCEL_LAT_MAX": 0.5},
            {"imu:ACCEL_LON_MAX": 0.1, "imu:ACCEL_LAT_MAX": 0.1},
        ]
    )

    segmented = segment_operating_modes(frame, modes)

    assert list(segmented[MODE_COLUMN]) == ["hard_brake", "cornering", UNKNOWN_MODE]


def test_launch_hard_brake_cornering_specs_are_inert_without_imu_columns() -> None:
    """2a.7 acceptance: the shipped example specs must never fire on a
    real, IMU-less session — the accel_lon/accel_lat candidate columns are
    entirely absent, so segment_operating_modes treats them the same as any
    other mode's missing required signal and skips the spec outright."""
    config = CanaryConfig.from_yaml(EXAMPLE_YAML)
    frame = pd.DataFrame(
        [
            # Rows deliberately shaped to "look like" a launch/hard-brake/
            # cornering row on every OBD-visible axis — proves the IMU
            # gate, not some other threshold, is what's silent.
            {
                "obd:RPM": 4500.0,
                "obd:SPEED": 2.0,
                "obd:THROTTLE_POS": 95.0,
                "obd:COOLANT_TEMP": 90.0,
            },
            {
                "obd:RPM": 2000.0,
                "obd:SPEED": 40.0,
                "obd:THROTTLE_POS": 0.0,
                "obd:COOLANT_TEMP": 90.0,
            },
        ]
    )

    segmented = segment_operating_modes(frame, config.preprocessing.operating_modes)

    assert "launch" not in set(segmented[MODE_COLUMN])
    assert "hard_brake" not in set(segmented[MODE_COLUMN])
    assert "cornering" not in set(segmented[MODE_COLUMN])


def test_required_signals_include_accel_axes_when_spec_gates_on_them() -> None:
    from src.preprocessing.segmenter import _required_signals

    spec = OperatingModeSpec(accel_lon_min=0.3, accel_lat_max=0.5)
    assert "accel_lon" in _required_signals(spec)
    assert "accel_lat" in _required_signals(spec)
