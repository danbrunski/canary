"""H01.1 — the source-level trainability gate.

External sensors (``gps``, ``imu``, ``sensor``) never train or score the
anomaly detectors or behavior models (H00 operator decision 1, 2026-09-23).
Enforced by an allowlist of ECU-origin sources in ``src.telemetry``; a
``derived:`` channel follows its declared inputs.
"""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path
from typing import get_args

import numpy as np
import pandas as pd
import pytest

from src.analysis.refit import Surface1DRecipe, Surface2DRecipe
from src.behavior.behavior_models import load_behavior_models
from src.columns import is_flag_column
from src.config import VehicleParameters
from src.decode.dids import load_repo_did_map
from src.features.feature_pipeline import (
    candidate_feature_columns,
    feature_policy_exclusions,
    featurize_wide_frame,
)
from src.preprocessing.channel_units import append_did_views, channel_units_for
from src.signals.afr import append_afr_channels, load_afr_calibration
from src.signals.committees import COMMITTEES, append_committee_residuals
from src.signals.derived import (
    DERIVED_CHANNEL_INPUTS,
    DERIVED_CHANNEL_OPTIONAL_INPUTS,
    compute_anomaly_signals,
    compute_derived_channels,
)
from src.telemetry import (
    INPUT_GATED_SOURCES,
    NON_TRAINABLE_SOURCES,
    TRAINABLE_SOURCES,
    TelemetrySource,
    require_trainability_classified,
)
from src.trainability import SOURCE_NOT_TRAINABLE, source_exclusion_reason
from src.vehicle.tune_revisions import load_tune_revisions
from tests.configs import example_config

REPO = Path(__file__).resolve().parents[1]
TELEMETRY_PY = REPO / "src" / "telemetry.py"
XTERRA = REPO / "data" / "nissan-xterra-2008"
ROWS = 240


def _session_frame(extra: dict[str, np.ndarray] | None = None) -> pd.DataFrame:
    """A 2-minute, 1 Hz wide session with moving engine channels."""
    rng = np.random.default_rng(7)
    t = np.arange(ROWS, dtype=float)
    frame = pd.DataFrame(
        {
            "session": "2026-09-24T120000",
            "timestamp": pd.date_range(
                datetime(2026, 9, 24, 12, tzinfo=UTC), periods=ROWS, freq="s"
            ),
            "obd:RPM": 1500.0 + 400.0 * np.sin(t / 15.0) + rng.normal(0, 5, ROWS),
            "obd:SPEED": 50.0 + 10.0 * np.sin(t / 20.0),
            "obd:THROTTLE_POS": 20.0 + 5.0 * np.sin(t / 10.0),
            "obd:COOLANT_TEMP": 85.0 + rng.normal(0, 0.5, ROWS),
            "obd:MAF": 12.0 + 3.0 * np.sin(t / 15.0),
            "obd:INTAKE_PRESSURE": 50.0 + 10.0 * np.sin(t / 12.0),
            "obd:BAROMETRIC_PRESSURE": 80.0 + rng.normal(0, 0.1, ROWS),
            "obd:INTAKE_TEMP": 30.0 + rng.normal(0, 0.5, ROWS),
            "obd:ENGINE_LOAD": 30.0 + 8.0 * np.sin(t / 15.0),
            "obd:SHORT_FUEL_TRIM_1": rng.normal(0, 2, ROWS),
            "obd:SHORT_FUEL_TRIM_2": rng.normal(0, 2, ROWS),
            "obd:LONG_FUEL_TRIM_1": rng.normal(1, 0.2, ROWS),
            "obd:LONG_FUEL_TRIM_2": rng.normal(1, 0.2, ROWS),
            "obd:O2_B1S1": 0.45 + 0.4 * np.sign(np.sin(t)),
            "obd:O2_B1S2": 0.6 + rng.normal(0, 0.02, ROWS),
            "obd:O2_B2S1": 0.45 + 0.4 * np.sign(np.cos(t)),
            "obd:O2_B2S2": 0.6 + rng.normal(0, 0.02, ROWS),
            "obd:O2_WR_LAMBDA_S1": 1.0 + rng.normal(0, 0.01, ROWS),
            "obd:O2_WR_LAMBDA_S2": 1.0 + rng.normal(0, 0.01, ROWS),
            "obd:ACTUAL_ENGINE_TORQUE": 40.0 + 5.0 * np.sin(t / 15.0),
            "obd:ENGINE_REFERENCE_TORQUE": np.full(ROWS, 400.0),
            "did:ECM_AF_SENSOR_V_B1": 1.5 + rng.normal(0, 0.02, ROWS),
            "did:ECM_AF_SENSOR_V_B2": 1.5 + rng.normal(0, 0.02, ROWS),
            "did:ECM_THROTTLE_POS_SENSOR_1": 0.9 + 0.1 * np.sin(t / 10.0),
        }
    )
    for column, values in (extra or {}).items():
        frame[column] = values
    return frame


def _external_sensor_channels() -> dict[str, np.ndarray]:
    rng = np.random.default_rng(11)
    t = np.arange(ROWS, dtype=float)
    return {
        "gps:speed": 54.0 + 10.0 * np.sin(t / 20.0) + rng.normal(0, 0.3, ROWS),
        "gps:num_satellites": np.full(ROWS, 10.0),
        "gps:hdop": np.full(ROWS, 0.9),
        "gps:altitude": 1600.0 + rng.normal(0, 3, ROWS),
        "imu:ACCEL_X": rng.normal(0, 0.05, ROWS),
        "imu:ACCEL_LON": rng.normal(0, 0.05, ROWS),
        "imu:GYRO_Z": rng.normal(0, 1.0, ROWS),
        "sensor:FUEL_PRESSURE": 380.0 + rng.normal(0, 2, ROWS),
        "baro:pressure": 80.0 + rng.normal(0, 0.1, ROWS),
    }


# ---------------------------------------------------------------------------
# The telemetry.py classification
# ---------------------------------------------------------------------------


def test_every_telemetry_source_is_classified_exactly_once() -> None:
    sources = set(get_args(TelemetrySource))
    assert TRAINABLE_SOURCES == {"obd", "decoded"}
    assert TRAINABLE_SOURCES | INPUT_GATED_SOURCES | set(NON_TRAINABLE_SOURCES) == sources
    for external in ("gps", "imu", "sensor"):
        assert "H00 operator decision 1" in NON_TRAINABLE_SOURCES[external]  # type: ignore[index]


def test_a_source_added_without_a_classification_fails_at_import() -> None:
    """Add a source to the Literal (and to the replay classification, so the
    older import-time checks pass) and nothing else: importing must refuse."""
    source = TELEMETRY_PY.read_text()
    patches = [
        ('"sensor", "kline"\n]', '"sensor", "kline", "hat_adc"\n]'),
        ("    KLINE_SOURCE,\n}", '    KLINE_SOURCE,\n    "hat_adc",\n}'),
        ("SENSOR_SOURCE, KLINE_SOURCE}", 'SENSOR_SOURCE, KLINE_SOURCE, "hat_adc"}'),
    ]
    for old, new in patches:
        assert source.count(old) == 1, f"telemetry.py no longer contains {old!r}"
        source = source.replace(old, new)
    namespace: dict[str, object] = {"__name__": "telemetry_with_unclassified_source"}
    with pytest.raises(TypeError, match=r"\['hat_adc'\] have no trainability classification"):
        exec(compile(source, str(TELEMETRY_PY), "exec"), namespace)  # noqa: S102


def test_the_classification_check_refuses_overlap_and_strays() -> None:
    sources = ("obd", "gps", "derived")
    require_trainability_classified(sources, {"obd"}, {"derived"}, {"gps"})
    with pytest.raises(TypeError, match="more than once"):
        require_trainability_classified(sources, {"obd", "gps"}, {"derived"}, {"gps"})
    with pytest.raises(TypeError, match="non-sources"):
        require_trainability_classified(sources, {"obd", "did"}, {"derived"}, {"gps"})


# ---------------------------------------------------------------------------
# The gate itself
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "column",
    [
        "gps:speed",
        "gps:speed__roc",
        "imu:ACCEL_X__std",
        "sensor:FUEL_PRESSURE",
        "kline:ECM:1206",
        "kline:ECM:MEM_ECM_TARGET_EQUIV_RATIO",
        "uprev:TIMING_ADVANCE",
        "baro:pressure",
        "derived:SPEED_SOURCE_DIVERGENCE",
        "derived:NOT_DECLARED_ANYWHERE",
        "unprefixed",
    ],
)
def test_refused_columns_name_the_rule(column: str) -> None:
    reason = source_exclusion_reason(column)
    assert reason is not None and reason.startswith(f"{SOURCE_NOT_TRAINABLE}: ")


@pytest.mark.parametrize(
    "column",
    [
        "obd:RPM",
        "obd:mem:ECM_TARGET_EQUIV_RATIO",
        "decoded:ECM_RPM__mean",
        "did:ECM_AIR_CHARGE",
        "dbc:VENDOR_ONLY",
        "deviation:coolant_warmup",
        "derived:VE_PERCENT__roc",
        "derived:ESTIMATED_TORQUE_NM",
    ],
)
def test_ecu_origin_columns_pass(column: str) -> None:
    assert source_exclusion_reason(column) is None


def test_a_derived_channel_follows_every_input_including_optional_ones() -> None:
    reason = source_exclusion_reason("derived:SPEED_SOURCE_DIVERGENCE")
    assert reason is not None and "gps:speed" in reason
    # A derived channel of a derived channel resolves recursively.
    assert source_exclusion_reason("derived:CO2_KG") is None
    # Optional inputs are inputs: each must itself be trainable.
    for column in DERIVED_CHANNEL_OPTIONAL_INPUTS["derived:FUEL_RATE_LPH"]:
        assert source_exclusion_reason(column) is None


def test_every_derived_channel_the_pipeline_emits_declares_its_inputs() -> None:
    """The registry covers every ``derived:`` output; an undeclared one would be
    silently non-trainable (safe) but also unexplained, so it is a failure.

    The Xterra's verified load axis (P02.14, ``derived:BASE_FUEL_SCHEDULE``, a
    DID view of 0x1208) is computed here by the pipeline's own step and
    resolved through the real catalog, so the view, ``TARGET_AFR`` and
    ``AFR_ERROR_B*`` are all emitted and checked. Its raw counts are chosen to
    land on the target map's axis (8.76-21.57 ms)."""
    vehicle = VehicleParameters(
        final_drive=3.357,
        gearbox_ratios=(3.79, 2.28, 1.45, 1.0, 0.82),
        tyre_loaded_circumference_m=2.4,
        driveline_evidence="synthetic test vehicle",
    )
    calibration = load_afr_calibration(XTERRA / "afr_calibration.yaml")
    frame = _session_frame(_external_sensor_channels())
    # Committee members nothing computes today, so every committee emits.
    for committee in COMMITTEES:
        for member in committee.members:
            if member not in frame.columns:
                frame[member] = np.linspace(0.0, 1.0, ROWS)
    frame["did:ECM_AIR_CHARGE"] = np.linspace(9.0, 21.0, ROWS) / 0.00049
    frame = append_did_views(frame, did_map=load_repo_did_map())
    # The frame is dated 2026-09-24, on ZS44A-20260920, which has a map.
    registry = load_tune_revisions(XTERRA / "tune_revisions.yaml")
    assert registry is not None
    frame = append_afr_channels(
        frame,
        vehicle,
        calibration,
        channel_units=channel_units_for(load_repo_did_map()),
        flash_history=registry,
    )
    frame = compute_anomaly_signals(frame, vehicle)
    frame = compute_derived_channels(frame, vehicle, calibration=calibration)
    frame = append_committee_residuals(frame)
    emitted = {
        column
        for column in frame.columns
        if column.startswith("derived:") and not is_flag_column(column)
    }
    member_only = {m for c in COMMITTEES for m in c.members if m.startswith("derived:")}
    produced = emitted - member_only
    assert "derived:GEAR_ESTIMATE" in produced
    assert "derived:AFR_ERROR_B1" in produced
    assert "derived:BASE_FUEL_SCHEDULE" in produced
    assert "derived:VERTICAL_SPEED_DIVERGENCE" in produced
    assert sorted(produced - set(DERIVED_CHANNEL_INPUTS)) == []


# ---------------------------------------------------------------------------
# Acceptance: a synthetic session with external sensors yields no feature
# ---------------------------------------------------------------------------


def test_external_sensors_and_their_derived_channels_yield_no_feature() -> None:
    config = example_config(REPO / "canary.example.yaml")
    wide = _session_frame(_external_sensor_channels())
    featurized = featurize_wide_frame(
        wide, config, behavior_models=None, did_map=load_repo_did_map()
    )
    features = featurized.features
    # The derived channel built from gps: is actually computed (for the
    # committee evaluator and fingerprint), but never enters the feature frame.
    assert featurized.operating_frame["derived:SPEED_SOURCE_DIVERGENCE"].notna().any()
    assert "derived:SPEED_SOURCE_DIVERGENCE" not in features.columns
    columns = candidate_feature_columns(features)
    external = ("gps:", "imu:", "sensor:", "baro:", "derived:SPEED_SOURCE_DIVERGENCE")
    assert [c for c in columns if c.startswith(external)] == []
    # ...and the ECU channels still train, derived ones included.
    assert "obd:RPM" in columns
    assert "derived:AIR_MASS_PER_CYCLE" in columns
    assert "derived:FUEL_RATE_LPH__roc" in columns
    # The manifest's exclusion record names the rule for each.
    excluded = feature_policy_exclusions(features)
    for base in ("gps:speed", "imu:ACCEL_X", "sensor:FUEL_PRESSURE", "baro:pressure"):
        assert excluded[base].startswith(SOURCE_NOT_TRAINABLE)
    assert "session" not in excluded and "timestamp" not in excluded


def test_committee_channels_leave_the_trained_feature_list_unchanged() -> None:
    """H04.1 acceptance: committees are detectors, not features. The pipeline
    still computes every committee channel into the operating frame (the
    committee evaluator and the fingerprint read them) but never rolls one
    into the feature frame, so the trained list is exactly what it is without
    them — committees were all non-trainable, so the corpus's lists do not move.
    The source gate refuses them anyway, should one ever reach a feature frame."""
    from src.columns import base_channel
    from src.signals.committees import (
        GPS_SPEED_ACCEL_G,
        OBD_SPEED_ACCEL_G,
        committee_channel_reason,
    )

    config = example_config(REPO / "canary.example.yaml")
    wide = _session_frame(_external_sensor_channels())
    featurized = featurize_wide_frame(
        wide, config, behavior_models=None, did_map=load_repo_did_map()
    )
    committee = ("derived:SPEED_SOURCE_DIVERGENCE", OBD_SPEED_ACCEL_G, GPS_SPEED_ACCEL_G)
    for channel in committee:
        assert featurized.operating_frame[channel].notna().any(), channel
    features = featurized.features
    assert [
        c for c in features.columns if committee_channel_reason(base_channel(str(c))) is not None
    ] == []
    columns = candidate_feature_columns(features)
    rolled = features.assign(
        **{
            f"{channel}{suffix}": 0.1
            for channel in committee
            for suffix in ("", "__mean", "__std", "__roc")
        }
    )
    assert candidate_feature_columns(rolled) == columns
    assert "detectors, not features" in feature_policy_exclusions(rolled)[OBD_SPEED_ACCEL_G]


# ---------------------------------------------------------------------------
# Behavior models: recipes and artifacts
# ---------------------------------------------------------------------------


def test_a_recipe_naming_an_external_sensor_is_refused() -> None:
    with pytest.raises(ValueError, match="H00 operator decision 1"):
        Surface1DRecipe(name="speed_vs_gps", output="obd:SPEED", x_column="gps:speed")
    with pytest.raises(ValueError, match="imu:ACCEL_LON"):
        Surface2DRecipe(
            name="load_vs_rpm_accel",
            output="obd:ENGINE_LOAD",
            x_column="obd:RPM",
            y_column="imu:ACCEL_LON",
        )
    with pytest.raises(ValueError, match="sensor:FUEL_PRESSURE"):
        Surface1DRecipe(
            name="trim_vs_rpm",
            output="obd:SHORT_FUEL_TRIM_1",
            x_column="obd:RPM",
            required_context=(("sensor:FUEL_PRESSURE",),),
        )


def test_a_behavior_artifact_reading_an_external_sensor_is_refused_at_load(
    tmp_path: Path,
) -> None:
    source = XTERRA / "behavior_models.yaml"
    assert load_behavior_models(source).models  # the tracked artifacts load
    text = source.read_text()
    assert "channel: obd:RPM" in text
    poisoned = tmp_path / "behavior_models.yaml"
    poisoned.write_text(text.replace("channel: obd:RPM", "channel: gps:speed", 1))
    with pytest.raises(ValueError, match="gps:speed"):
        load_behavior_models(poisoned)
