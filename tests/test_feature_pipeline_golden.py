"""R14: the feature pipeline's outputs, selections and spec are pinned.

``src/features/feature_pipeline.py`` (2,701 lines) was split into focused
owner modules (policy, selection, transform, dependencies, perturbation,
resampling, workers, build, spec). A pure move must not change a column, a
dtype, a NaN mask, a selection or a persisted spec field, so this test runs
the real chain over a committed slice of a real drive
(``tests/fixtures/ground_truth/drive-slice``, plus seeded extra channels so the
derived channels and behavior deviations have inputs) and compares what comes
out with ``tests/fixtures/feature_pipeline_golden.json``, captured from the
single-file module before any code moved.

What is compared, through the old facade (``src.features.feature_pipeline``)
so the same file ran against the code before and after the split:

* the full transform, ``last_row_only``, and the no-behavior-model /
  no-DID-catalog variants: column order, dtype, row count, and per column the
  NaN mask (hashed), count, sum, extremes and ends (floats to 1e-9 relative);
* the selections (candidate, trainable, IF versus sequence, the coverage
  floor at three settings) with their named exclusion reasons, the feature
  policy's per-channel reasons and its standing exclusions;
* the persisted feature spec (every field, with machine paths removed), the
  feature health, the behavior-model fingerprints and four reconciliations;
* the worker-count policy over a grid of memory, CPUs, sessions and overrides;
* an offline build (two sessions) and its warm-cache read.

The fixture is regenerated only on purpose, by a change that is meant to move
a number or a column::

    .venv/bin/python -m tests.test_feature_pipeline_golden --write
"""

from __future__ import annotations

import hashlib
import json
import math
import os
import shutil
import sys
from pathlib import Path
from typing import Any
from unittest import mock

import numpy as np
import pandas as pd
import pytest

from src.behavior.behavior_models import load_behavior_models
from src.columns import base_channel
from src.config import CanaryConfig
from src.decode.bindings import load_bindings
from src.decode.dids import load_repo_did_map
from src.features import feature_perturbation, feature_workers
from src.features import feature_pipeline as fp
from src.vehicle.profiles import resolve_profile_for_tooling, scope_config
from tests.configs import EXAMPLE_YAML

REPO = Path(__file__).resolve().parents[1]
SLICE = REPO / "tests" / "fixtures" / "ground_truth" / "drive-slice"
PROFILE_DIR = REPO / "data" / "nissan-xterra-2008"
GOLDEN = Path(__file__).parent / "fixtures" / "feature_pipeline_golden.json"

#: Channels the committed slice did not poll, added (seeded) so the derived
#: channels and the behavior deviations over them are exercised.
_ADDED = {
    "obd:MAF": (18.0, 4.0),
    "obd:SHORT_FUEL_TRIM_1": (0.0, 3.0),
    "obd:SHORT_FUEL_TRIM_2": (0.5, 3.0),
    "obd:LONG_FUEL_TRIM_1": (2.0, 0.5),
    "obd:LONG_FUEL_TRIM_2": (1.5, 0.5),
    "obd:INTAKE_TEMP": (30.0, 2.0),
    "obd:BAROMETRIC_PRESSURE": (80.0, 0.2),
    "obd:TIMING_ADVANCE": (20.0, 5.0),
    "obd:O2_B1S1": (0.45, 0.3),
    "obd:O2_B1S2": (0.6, 0.05),
    "did:ECM_AIR_CHARGE": (40.0, 8.0),
    "did:ECM_AF_SENSOR_V_B1": (3.3, 0.1),
    "did:ECM_AF_SENSOR_V_B2": (3.3, 0.1),
    "derived:FUEL_RATE_LPH": (3.0, 1.0),
    "derived:TRIP_DISTANCE_KM": (12.0, 3.0),
}

RTOL = 1e-9


# ---------------------------------------------------------------------------
# Canonical form
# ---------------------------------------------------------------------------


def _digest(array: np.ndarray) -> str:
    return hashlib.sha256(np.ascontiguousarray(array).tobytes()).hexdigest()[:16]


def _column(series: pd.Series) -> dict[str, Any]:
    nulls = series.isna().to_numpy()
    record: dict[str, Any] = {
        "dtype": str(series.dtype),
        "nan": int(nulls.sum()),
        "nan_mask": _digest(nulls),
    }
    if pd.api.types.is_bool_dtype(series):
        record["true"] = int(series.fillna(False).astype(bool).sum())
    elif pd.api.types.is_numeric_dtype(series):
        values = series.to_numpy(dtype=np.float64, na_value=np.nan)
        finite = values[np.isfinite(values)]
        if len(finite):
            record.update(
                sum=float(finite.sum()),
                min=float(finite.min()),
                max=float(finite.max()),
                first=float(finite[0]),
                last=float(finite[-1]),
            )
    else:
        record["values"] = [str(v) for v in pd.unique(series.astype(str))][:12]
    return record


def canon_frame(frame: pd.DataFrame) -> dict[str, Any]:
    """Order, dtypes, row count and a per-column image that keeps NaN masks."""
    return {
        "rows": int(len(frame)),
        "index": _digest(np.asarray(frame.index.to_numpy(), dtype=np.int64))
        if pd.api.types.is_integer_dtype(frame.index)
        else str(type(frame.index).__name__),
        "columns": [str(c) for c in frame.columns],
        "per_column": {str(c): _column(frame[c]) for c in frame.columns},
    }


def canon_brief(frame: pd.DataFrame) -> dict[str, Any]:
    """A compact image of a whole frame: shape, column order and dtypes
    (digested), the NaN mask (digested) and the sum of every finite value."""
    numeric = frame.select_dtypes(include="number")
    values = numeric.to_numpy(dtype=np.float64, na_value=np.nan)
    columns = "\n".join(f"{c}:{t}" for c, t in zip(frame.columns, frame.dtypes, strict=True))
    return {
        "rows": int(len(frame)),
        "columns": len(frame.columns),
        "layout": hashlib.sha256(columns.encode()).hexdigest()[:16],
        "nan_mask": _digest(frame.isna().to_numpy()),
        "finite_sum": float(values[np.isfinite(values)].sum()),
    }


def canon(value: Any) -> Any:
    """A JSON-able image: sets sorted, paths made repo-relative, NaN a string."""
    if isinstance(value, pd.DataFrame):
        return canon_frame(value)
    if isinstance(value, dict):
        return {str(k): canon(v) for k, v in value.items()}
    if isinstance(value, list | tuple):
        return [canon(v) for v in value]
    if isinstance(value, set | frozenset):
        return sorted(canon(v) for v in value)
    if isinstance(value, float) and math.isnan(value):
        return "NaN"
    if isinstance(value, np.generic):
        return canon(value.item())
    if isinstance(value, Path):
        return _relative(str(value))
    if isinstance(value, str):
        return _relative(value)
    return value


def _relative(text: str) -> str:
    return text.replace(str(REPO) + "/", "").replace(str(REPO), ".")


def _without_machine_paths(spec: dict[str, Any]) -> dict[str, Any]:
    out = json.loads(json.dumps(spec, sort_keys=True, default=str))
    for key in ("afr_calibration", "behavior_models"):
        if key in out:
            out[key] = {"sha256": out[key]["sha256"], "path": _relative(out[key]["path"])}
    return canon(out)  # type: ignore[no-any-return]


# ---------------------------------------------------------------------------
# The calculations
# ---------------------------------------------------------------------------


def _scoped() -> CanaryConfig:
    config = CanaryConfig.from_yaml(EXAMPLE_YAML)
    return scope_config(config, resolve_profile_for_tooling(config, None))


def _wide(config: CanaryConfig) -> pd.DataFrame:
    frame = fp.load_resampled_session(SLICE, config)
    rng = np.random.default_rng(7)
    added: dict[str, Any] = {}
    for column, (centre, spread) in _ADDED.items():
        values = rng.normal(centre, spread, len(frame))
        stale = rng.random(len(frame)) < 0.3
        values[rng.random(len(frame)) < 0.05] = np.nan
        added[column] = values.astype(np.float32)
        added[f"{column}__gap"] = stale | np.isnan(values)
    frame = pd.concat([frame, pd.DataFrame(added, index=frame.index)], axis=1)
    frame["obd:FUEL_STATUS"] = np.float32(2.0)
    return frame


#: One channel per exclusion the policy states, so removing a name from a
#: constant (the frame under test need not carry it) moves the pin.
PROBE_CHANNELS = (
    "obd:RPM",
    "obd:RUN_TIME",
    "obd:DISTANCE_SINCE_DTC_CLEAR",
    "obd:DISTANCE_W_MIL",
    "obd:WARMUPS_SINCE_DTC_CLEAR",
    "obd:FUEL_LEVEL",
    "obd:MISFIRE_COUNT_CYL1",
    "obd:MISFIRE_COUNT_CYL8",
    "gps:latitude",
    "gps:longitude",
    "gps:altitude",
    "gps:heading",
    "gps:fix_quality",
    "gps:num_satellites",
    "gps:hdop",
    "gps:speed",
    "imu:accel_x",
    "sensor:ambient",
    "derived:GEAR_ESTIMATE",
    "derived:TRIP_DISTANCE_KM",
    "derived:FUEL_USED_L",
    "derived:CO2_KG",
    "derived:IDLE_TIME_S",
    "derived:AVG_ECONOMY_L_PER_100KM",
    "derived:TARGET_AFR",
    "derived:BASE_FUEL_SCHEDULE",
    "derived:FUEL_RATE_LPH",
    "obd:mem:ECM_ENGINE_SPEED_RAM",
    "decoded:ECM_RPM",
    "decoded:NOT_IN_THE_CATALOG",
    "dbc:EXTERNAL_ONLY",
    "dbc:ECM_RPM",
    "did:ECM_COOLANT_TEMP",
    "did:NOT_A_DID",
    "unclassified:thing",
)


def _probe(policy: fp.FeaturePolicy) -> dict[str, Any]:
    columns: dict[str, Any] = {
        "session": "s",
        "timestamp": pd.date_range("2026-10-01", periods=3, freq="1s", tz="UTC"),
        "operating_mode": "idle",
        "context:engine_start_age": 1.0,
        "obd:RPM__gap": False,
    }
    for channel in PROBE_CHANNELS:
        columns[channel] = 1.0
        columns[f"{channel}__mean"] = 1.0
    frame = pd.DataFrame(columns)
    return {
        "reasons": {b: policy.exclusion_reason(b) for b in PROBE_CHANNELS},
        "exclusions": fp.feature_policy_exclusions(frame, policy),
        "candidates": fp.candidate_feature_columns(frame, policy=policy),
    }


def _dependencies(behavior: Any) -> dict[str, Any]:
    from src.signals.derived import DERIVED_CHANNEL_INPUTS

    cases = [{"obd:MAF"}, {"obd:RPM"}, {"obd:COOLANT_TEMP"}, {"obd:SPEED", "gps:speed"}, set()]
    return {
        "derived_inputs": {
            c: sorted(fp.derived_channel_inputs(c)) for c in sorted(DERIVED_CHANNEL_INPUTS)
        },
        "reached": [sorted(fp.derived_channels_reached(roots)) for roots in cases],
        "ancestors": [
            sorted(fp._derived_ancestors(sorted(fp.derived_channels_reached(roots))))  # noqa: SLF001
            for roots in cases
        ],
        "model_roots": {m.name: sorted(fp.behavior_model_input_roots(m)) for m in behavior.models},
        "column_roots": [
            fp._column_root(c)  # noqa: SLF001
            for c in (
                "obd:MAF__roc",
                "obd:MAF__gap",
                "obd:MAF__arrived",
                "obd:MAF__mean",
                "obd:MAF",
                "deviation:x__std",
            )
        ],
    }


def _fallback_decisions(
    config: CanaryConfig,
    did_map: Any,
    bindings: Any,
    behavior: Any,
    frame: pd.DataFrame,
    clean: fp.FeaturizedFrame,
) -> dict[str, Any]:
    """Whether ``featurize_perturbed_frame`` took the complete transform, for the
    inputs that decide it, and that either way it equals the complete result."""
    stamps = pd.to_datetime(frame["timestamp"], utc=True)
    rows = (stamps >= stamps.iloc[120]) & (stamps <= stamps.iloc[300])

    def faulted(column: str, scale: float, shift: float = 0.0) -> pd.DataFrame:
        out = frame.copy()
        out.loc[rows.to_numpy(), column] = out.loc[rows.to_numpy(), column] * scale + shift
        return out

    shifted = frame.copy()
    shifted["timestamp"] = shifted["timestamp"] + pd.Timedelta(seconds=1)
    cases = {
        "unchanged": frame.copy(),
        "mode_column_present": frame.assign(operating_mode="idle"),
        "extra_column": frame.assign(**{"obd:ZZZ": 1.0}),
        "reordered_columns": frame[list(reversed(frame.columns))],
        "row_structure_changed": shifted,
        "airflow_fault": faulted("obd:MAF", 1.3, 0.7),
        "coolant_small": faulted("obd:COOLANT_TEMP", 1.0, 0.5),
        "coolant_large": faulted("obd:COOLANT_TEMP", 0.0, -40.0),
        "rpm_large": faulted("obd:RPM", 0.0),
        "stored_derived": faulted("derived:FUEL_RATE_LPH", 2.0),
        "undeclared_input": faulted("obd:O2_B1S1", 2.0),
    }
    out: dict[str, Any] = {}
    for name, wide in cases.items():
        with mock.patch.object(
            feature_perturbation,
            "featurize_wide_frame",
            wraps=feature_perturbation.featurize_wide_frame,  # type: ignore[attr-defined]
        ) as whole:
            got = fp.featurize_perturbed_frame(
                clean,
                frame,
                wide,
                config,
                behavior_models=behavior,
                did_map=did_map,
                bindings=bindings,
            )
        expected = fp.featurize_wide_frame(
            wide, config, behavior_models=behavior, did_map=did_map, bindings=bindings
        ).features
        pd.testing.assert_frame_equal(got, expected, check_exact=True)
        out[name] = {"full_transform": bool(whole.called), "features": canon_brief(got)}
    return out


def _coverage_boundary() -> dict[str, Any]:
    """Columns valued on exactly the floor's share of rows: the floor is
    ``valued < min_coverage``, so these are kept, and one row fewer is not."""
    n = 10
    valued = {"half": 5, "under": 4, "over": 6, "all": 10}
    frame = pd.DataFrame(
        {
            "session": ["s"] * n,
            "timestamp": pd.date_range("2026-10-01", periods=n, freq="1s", tz="UTC"),
            "operating_mode": ["idle"] * n,
            **{
                f"obd:{name.upper()}": [float(i) if i < count else np.nan for i in range(n)]
                for name, count in valued.items()
            },
        }
    )
    policy = fp.resolve_feature_policy()
    result: dict[str, Any] = {}
    for floor in (0.5, 0.6, 0.4):
        selected_if = fp.isolation_forest_feature_selection(
            frame, min_coverage=floor, policy=policy
        )
        selected_seq = fp.sequence_feature_selection(frame, min_coverage=floor, policy=policy)
        result[str(floor)] = {
            "if": [selected_if.columns, selected_if.coverage_record()],
            "sequence": [selected_seq.columns, selected_seq.coverage_record()],
        }
    result["rows"] = len(fp.detector_coverage_rows(frame))
    return result


def _worker_grid() -> dict[str, int]:
    grid: dict[str, int] = {}
    real_cpu, real_memory = os.cpu_count, feature_workers.available_memory_bytes  # noqa: SLF001
    try:
        for memory in (2 * 1024**3, 64 * 1024**3, None):
            for cpus in (1, 4, 12):
                for sessions in (1, 2, 3, 8):
                    for explicit in (None, 3):
                        os.cpu_count = lambda cpus=cpus: cpus  # type: ignore[misc]
                        feature_workers.available_memory_bytes = lambda memory=memory: memory  # type: ignore[misc]  # noqa: SLF001
                        grid[f"{memory}|{cpus}|{sessions}|{explicit}"] = (
                            feature_workers._resolve_worker_count([SLICE] * sessions, explicit)  # noqa: SLF001
                        )
    finally:
        os.cpu_count = real_cpu
        feature_workers.available_memory_bytes = real_memory  # noqa: SLF001
    return grid


def compute(tmp: Path) -> dict[str, Any]:
    """Everything pinned, from the code under test (through the old facade)."""
    config = _scoped()
    did_map = load_repo_did_map()
    bindings = load_bindings(REPO / "data" / "bindings.yaml")
    behavior = load_behavior_models(PROFILE_DIR / "behavior_models.yaml")
    frame = _wide(config)
    out: dict[str, Any] = {"input": canon_frame(frame)}

    full = fp.featurize_wide_frame(
        frame, config, behavior_models=behavior, did_map=did_map, bindings=bindings
    )
    out["full_features"] = canon_frame(full.features)
    out["full_operating"] = canon_frame(full.operating_frame)
    out["full_fell_back"] = full.segmenter_fell_back
    bare = fp.featurize_wide_frame(
        frame, config, behavior_models=None, did_map=None, bindings=bindings
    )
    out["no_models_no_dids_features"] = canon_frame(bare.features)
    tail = frame.iloc[-120:].reset_index(drop=True)
    last = fp.featurize_wide_frame(
        tail,
        config,
        behavior_models=behavior,
        did_map=did_map,
        bindings=bindings,
        last_row_only=True,
    )
    out["last_row_features"] = canon_frame(last.features)
    out["last_row_operating"] = canon_frame(last.operating_frame)
    no_input = frame.drop(columns=[c for c in frame.columns if c.startswith("obd:RPM")])
    segmented, fell_back = fp.segment_or_constant(no_input, config)
    out["segment_fallback"] = [canon_frame(segmented), fell_back]
    single = config.model_copy(deep=True)
    single.preprocessing.single_operating_mode = True
    constant, constant_fell_back = fp.segment_or_constant(frame, single)
    out["segment_single_mode"] = [canon_frame(constant), constant_fell_back]
    out["segmenter_input_columns"] = list(fp.segmenter_input_columns())

    policy = fp.resolve_feature_policy(config)
    features = full.features
    bases = sorted({base_channel(str(c)) for c in features.columns})
    out["policy_reasons"] = {b: policy.exclusion_reason(b) for b in bases}
    out["policy_standing"] = policy.standing_exclusions()
    out["policy_probe"] = _probe(policy)
    out["policy_exclusions"] = fp.feature_policy_exclusions(features, policy)
    out["candidates"] = fp.candidate_feature_columns(features, policy=policy)
    trainable = fp.trainable_feature_columns(features, policy=policy)
    out["trainable"] = trainable
    for floor in (0.0, 0.5, 0.9):
        selected_if = fp.isolation_forest_feature_selection(
            features, min_coverage=floor, policy=policy
        )
        selected_seq = fp.sequence_feature_selection(features, min_coverage=floor, policy=policy)
        out[f"floor_{floor}"] = {
            "if": [selected_if.columns, selected_if.coverage_record()],
            "sequence": [selected_seq.columns, selected_seq.coverage_record()],
        }
    out["grid_density"] = fp.detector_grid_density(features, trainable)
    out["coverage"] = fp.detector_coverage(features, trainable)

    columns = out["floor_0.5"]["sequence"][0]
    spec = fp.build_feature_spec(
        features,
        columns,
        config=config,
        sequence_length=60,
        sequence_stride=5,
        policy=policy,
        behavior_models=behavior,
    )
    out["spec"] = _without_machine_paths(spec)
    out["spec_minimal"] = _without_machine_paths(fp.build_feature_spec(features, columns[:7]))
    out["health"] = canon(fp.summarize_feature_health(features, columns))
    live = features.drop(columns=[columns[1], columns[2]]).iloc[-3:].copy()
    live["gps:extra"] = 1.0
    live[columns[3]] = live[columns[3]].astype("object")
    reconciliations = {}
    for name, kwargs in (
        ("default", {}),
        ("no_coerce", {"coerce_numeric": False}),
        ("fill", {"fill_missing": -1.0}),
        ("structural", {"structural_columns": ["timestamp"]}),
    ):
        aligned, report = fp.reconcile_feature_frame_to_spec(live, spec, **kwargs)
        reconciliations[name] = [canon_frame(aligned), canon(report.to_dict())]
    out["reconcile"] = reconciliations

    out["dependencies"] = _dependencies(behavior)
    out["fallback_decisions"] = _fallback_decisions(
        config, did_map, bindings, behavior, frame, full
    )
    out["coverage_boundary"] = _coverage_boundary()
    out["worker_grid"] = _worker_grid()

    sessions = []
    for i in range(2):
        session = tmp / f"2026-08-0{i + 1}T120000"
        shutil.copytree(SLICE, session)
        sessions.append(session)
    cache = tmp / "cache"
    cold = fp.build_feature_frame(sessions, config, cache_dir=cache, max_workers=1)
    warm = fp.build_feature_frame(sessions, config, cache_dir=cache, max_workers=1)
    pd.testing.assert_frame_equal(cold, warm, check_exact=True)
    out["build"] = canon_frame(cold)
    out["cache_files"] = sorted(p.suffix for p in cache.iterdir())
    out["cache_version"] = fp.FEATURE_FRAME_CACHE_VERSION
    return out


# ---------------------------------------------------------------------------
# Comparison
# ---------------------------------------------------------------------------


def differences(pinned: Any, current: Any, path: str = "") -> list[str]:
    """Every place ``current`` departs from ``pinned`` (floats to RTOL)."""
    if isinstance(pinned, dict):
        if not isinstance(current, dict):
            return [f"{path}: {type(current).__name__}, pinned a mapping"]
        found = [f"{path}.{k}: missing" for k in pinned if k not in current]
        found += [f"{path}.{k}: unpinned" for k in current if k not in pinned]
        for key in pinned:
            if key in current:
                found += differences(pinned[key], current[key], f"{path}.{key}")
        # Insertion order is part of the contract for these mappings.
        if not found and list(pinned) != list(current):
            found.append(f"{path}: key order differs")
        return found
    if isinstance(pinned, list):
        if not isinstance(current, list) or len(pinned) != len(current):
            return [f"{path}: length or type differs"]
        return [
            d
            for i, (a, b) in enumerate(zip(pinned, current, strict=True))
            for d in differences(a, b, f"{path}[{i}]")
        ]
    if isinstance(pinned, float) and isinstance(current, int | float):
        if math.isclose(pinned, float(current), rel_tol=RTOL, abs_tol=1e-12):
            return []
        return [f"{path}: {current!r} != pinned {pinned!r}"]
    return [] if pinned == current else [f"{path}: {current!r} != pinned {pinned!r}"]


@pytest.fixture(scope="module")
def pinned() -> dict[str, Any]:
    return json.loads(GOLDEN.read_text())  # type: ignore[no-any-return]


@pytest.fixture(scope="module")
def current(tmp_path_factory: pytest.TempPathFactory) -> dict[str, Any]:
    # Through JSON, as the pin is, so a tuple/list or int/float distinction
    # cannot make an identical value look different.
    computed = compute(tmp_path_factory.mktemp("feature-golden"))
    return json.loads(json.dumps(canon(computed), default=str))  # type: ignore[no-any-return]


#: Every pinned section, each compared by its own test.
SECTIONS = [
    "input",
    "full_features",
    "full_operating",
    "full_fell_back",
    "no_models_no_dids_features",
    "last_row_features",
    "last_row_operating",
    "segment_fallback",
    "segment_single_mode",
    "segmenter_input_columns",
    "policy_reasons",
    "policy_standing",
    "policy_probe",
    "policy_exclusions",
    "candidates",
    "trainable",
    "floor_0.0",
    "floor_0.5",
    "floor_0.9",
    "grid_density",
    "coverage",
    "spec",
    "spec_minimal",
    "health",
    "reconcile",
    "dependencies",
    "fallback_decisions",
    "coverage_boundary",
    "worker_grid",
    "build",
    "cache_files",
    "cache_version",
]


def test_the_pin_covers_every_section(pinned: dict[str, Any], current: dict[str, Any]) -> None:
    assert list(pinned) == list(current)
    assert sorted(SECTIONS) == sorted(pinned), "a pinned section no test compares"
    assert len(pinned["full_features"]["columns"]) > 300
    assert pinned["last_row_features"]["rows"] == 1
    assert len(pinned["worker_grid"]) == 72
    assert pinned["spec"]["feature_columns"], "the spec pins the feature columns"
    assert pinned["floor_0.5"]["if"][1]["columns"], "the coverage floor excluded something"


@pytest.mark.parametrize("section", SECTIONS)
def test_every_section_matches_its_pin(
    section: str, pinned: dict[str, Any], current: dict[str, Any]
) -> None:
    assert differences(pinned[section], current[section], section) == []


def test_the_comparison_fails_on_a_real_change(pinned: dict[str, Any]) -> None:
    """A pin that cannot fail is not a pin: move a number, a mask, a column's
    place and a selection, and each is reported."""
    changed = json.loads(json.dumps(pinned))
    column = changed["full_features"]["columns"][5]
    changed["full_features"]["per_column"][column]["sum"] *= 1.000001
    changed["full_features"]["per_column"][column]["nan_mask"] = "0" * 16
    changed["full_features"]["columns"][5:7] = changed["full_features"]["columns"][6:4:-1]
    changed["trainable"].pop()
    changed["spec"]["feature_dtypes"][changed["spec"]["feature_columns"][0]] = "float16"
    found = differences(pinned, changed)
    assert any("sum" in d for d in found)
    assert any("nan_mask" in d for d in found)
    assert any("columns[5]" in d for d in found)
    assert any("trainable" in d for d in found)
    assert any("float16" in d for d in found)


def test_the_comparison_tolerates_only_float_noise(pinned: dict[str, Any]) -> None:
    near = json.loads(json.dumps(pinned))
    column = near["full_features"]["columns"][5]
    record = near["full_features"]["per_column"][column]
    if "sum" in record:
        record["sum"] = record["sum"] * (1 + RTOL / 10)
    assert differences(pinned, near) == []


def main() -> None:  # pragma: no cover - regenerating the pin
    import tempfile

    if "--write" not in sys.argv:
        raise SystemExit("pass --write to regenerate tests/fixtures/feature_pipeline_golden.json")
    with tempfile.TemporaryDirectory() as tmp:
        pinned = canon(compute(Path(tmp)))
    # One line per section: a moved number is a one-line diff, not a 1 MB reflow.
    body = ",\n".join(
        f"{json.dumps(key)}:{json.dumps(value, separators=(',', ':'), default=str)}"
        for key, value in pinned.items()
    )
    GOLDEN.write_text("{\n" + body + "\n}\n")
    print(f"wrote {GOLDEN}")


if __name__ == "__main__":  # pragma: no cover
    main()
