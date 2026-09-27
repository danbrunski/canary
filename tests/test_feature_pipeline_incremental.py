"""P02.4: featurizing an injected frame incrementally equals featurizing it whole.

``featurize_perturbed_frame`` reuses the clean featurized frame and recomputes
only what the changed columns reach. Parity is the acceptance, so every test
here compares against :func:`featurize_wide_frame` with ``check_exact``.
"""

from __future__ import annotations

from pathlib import Path
from unittest import mock

import numpy as np
import pandas as pd
import pytest

from src.analysis.committees import committee_channel_reason
from src.columns import TIMESTAMP_COLUMN
from src.config import CanaryConfig
from src.decode.behavior_models import load_behavior_models
from src.decode.bindings import load_bindings
from src.preprocessing import feature_pipeline
from src.preprocessing.anomaly_injection import (
    REALISTIC_FAULTS,
    inject_realistic_fault,
    load_fault_channel_roles,
)
from src.preprocessing.feature_pipeline import (
    _append_derived_channels,
    _column_root,
    _derived_ancestors,
    behavior_model_input_roots,
    derived_channels_reached,
    featurize_perturbed_frame,
    featurize_wide_frame,
    load_resampled_session,
)
from tests.configs import example_config

REPO = Path(__file__).resolve().parents[1]
SLICE = REPO / "tests" / "fixtures" / "ground_truth" / "drive-slice"
PROFILE_DIR = REPO / "data" / "nissan-xterra-2008"

#: Channels the committed slice did not poll, added so every realistic fault,
#: the derived channels they feed and the behavior deviations over them are
#: exercised (the slice has airflow, load and coolant only).
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
    # Collector-stored derived rows, as real sessions carry them: one the
    # featurizer recomputes over (fuel rate), one it never computes (the trip
    # integral), so a stored value must not be taken for an unchanged input.
    "derived:FUEL_RATE_LPH": (3.0, 1.0),
    "derived:TRIP_DISTANCE_KM": (12.0, 3.0),
}


@pytest.fixture(scope="module")
def config() -> CanaryConfig:
    return example_config(REPO / "canary.example.yaml")


@pytest.fixture(scope="module")
def inputs(config: CanaryConfig) -> dict[str, object]:
    frame = load_resampled_session(SLICE, config)
    rng = np.random.default_rng(7)
    added = {}
    for column, (centre, spread) in _ADDED.items():
        values = rng.normal(centre, spread, len(frame))
        stale = rng.random(len(frame)) < 0.3
        values[rng.random(len(frame)) < 0.05] = np.nan
        added[column] = values.astype(np.float32)
        added[f"{column}__gap"] = stale | np.isnan(values)
    frame = pd.concat([frame, pd.DataFrame(added, index=frame.index)], axis=1)
    # Closed-loop fuel status on every row, so trim-side faults apply.
    frame["obd:FUEL_STATUS"] = np.float32(2.0)
    return {
        "frame": frame,
        "behavior": load_behavior_models(PROFILE_DIR / "behavior_models.yaml"),
        "bindings": load_bindings(REPO / "data" / "bindings.yaml"),
        "roles": load_fault_channel_roles(PROFILE_DIR / "fault_injection.yaml"),
    }


def _full(frame: pd.DataFrame, config: CanaryConfig, inputs: dict[str, object]):
    return featurize_wide_frame(
        frame,
        config,
        bindings=inputs["bindings"],  # type: ignore[arg-type]
        behavior_models=inputs["behavior"],  # type: ignore[arg-type]
    )


def _incremental(clean, frame: pd.DataFrame, config: CanaryConfig, inputs: dict[str, object]):
    return featurize_perturbed_frame(
        clean,
        inputs["frame"],  # type: ignore[arg-type]
        frame,
        config,
        bindings=inputs["bindings"],  # type: ignore[arg-type]
        behavior_models=inputs["behavior"],  # type: ignore[arg-type]
    )


@pytest.mark.parametrize(
    ("kind", "severity"),
    [(kind, severity) for kind, fault in REALISTIC_FAULTS.items() for severity in fault.severities],
)
def test_every_realistic_fault_featurizes_incrementally_exactly(
    config: CanaryConfig, inputs: dict[str, object], kind: str, severity: float
) -> None:
    frame: pd.DataFrame = inputs["frame"]  # type: ignore[assignment]
    stamps = pd.to_datetime(frame[TIMESTAMP_COLUMN], utc=True)
    start, end = stamps.iloc[120], stamps.iloc[300]
    clean = _full(frame, config, inputs)
    faulted, injection = inject_realistic_fault(
        frame,
        kind=kind,
        severity=severity,
        start=start,
        end=end,
        roles=inputs["roles"],  # type: ignore[arg-type]
        bindings=inputs["bindings"],  # type: ignore[arg-type]
    )
    assert injection.perturbed, injection.not_perturbed

    expected = _full(faulted, config, inputs).features
    wrapped = mock.patch.object(
        feature_pipeline, "featurize_wide_frame", wraps=feature_pipeline.featurize_wide_frame
    )
    with wrapped as whole:
        got = _incremental(clean, faulted, config, inputs)

    pd.testing.assert_frame_equal(got, expected, check_exact=True)
    # Only a fault that moves a row's operating mode takes the full path;
    # otherwise the "incremental" path would be the full one in disguise.
    moved_mode = not (expected["operating_mode"].equals(clean.features["operating_mode"]))
    assert whole.called is moved_mode


def test_random_channel_perturbations_match_the_full_transform(
    config: CanaryConfig, inputs: dict[str, object]
) -> None:
    frame: pd.DataFrame = inputs["frame"]  # type: ignore[assignment]
    clean = _full(frame, config, inputs)
    numeric = [
        c
        for c in frame.columns
        if frame[c].dtype.kind == "f" and not str(c).endswith(("__gap", "__arrived"))
    ]
    rng = np.random.default_rng(11)
    for _ in range(6):
        picked = list(rng.choice(numeric, size=3, replace=False))
        perturbed = frame.copy()
        rows = rng.random(len(frame)) < 0.4
        for column in picked:
            values = perturbed[column].to_numpy(copy=True)
            values[rows] = values[rows] * np.float32(1.3) + np.float32(0.7)
            perturbed[column] = values
        expected = _full(perturbed, config, inputs).features
        got = _incremental(clean, perturbed, config, inputs)
        pd.testing.assert_frame_equal(got, expected, check_exact=True, obj=str(picked))


def test_an_unchanged_frame_is_the_clean_one_and_a_new_column_is_featurized_whole(
    config: CanaryConfig, inputs: dict[str, object]
) -> None:
    frame: pd.DataFrame = inputs["frame"]  # type: ignore[assignment]
    clean = _full(frame, config, inputs)
    assert _incremental(clean, frame.copy(), config, inputs) is clean.features

    widened = frame.assign(**{"obd:FUEL_PRESSURE": np.float32(300.0)})
    expected = _full(widened, config, inputs).features
    got = _incremental(clean, widened, config, inputs)
    pd.testing.assert_frame_equal(got, expected, check_exact=True)


def test_behavior_model_inputs_are_read_from_the_whole_definition(
    inputs: dict[str, object],
) -> None:
    behavior = inputs["behavior"]
    by_name = {m.name: m for m in behavior.models}  # type: ignore[attr-defined]
    roots = behavior_model_input_roots(by_name["total_trim_vs_rpm_load_b1"])
    # Output, both axes, and the rate-gate channels (``__roc`` folded to its root).
    assert {"derived:TOTAL_TRIM_B1", "obd:RPM", "obd:ENGINE_LOAD"} <= roots
    assert "obd:SHORT_FUEL_TRIM_2" not in roots


# ---------------------------------------------------------------------------
# The declarations the incremental path relies on are complete
# ---------------------------------------------------------------------------

_ROW = ("session", "timestamp", "operating_mode", "drive")


def _scrambled(frame: pd.DataFrame, keep: set[str], seed: int) -> pd.DataFrame:
    """Every float column whose channel is not in ``keep`` replaced with noise
    (gap flags untouched), the row structure kept."""
    rng = np.random.default_rng(seed)
    out = frame.copy()
    for column in frame.columns:
        name = str(column)
        if name in _ROW or _column_root(name) in keep or frame[column].dtype.kind != "f":
            continue
        out[column] = rng.normal(50.0, 30.0, len(frame)).astype(frame[column].dtype)
    return out


def test_every_derived_channel_reads_only_its_declared_inputs(
    config: CanaryConfig, inputs: dict[str, object]
) -> None:
    """``DERIVED_CHANNEL_INPUTS`` is what the incremental path recomputes a
    derived channel from: scrambling everything else must leave it alone."""
    frame: pd.DataFrame = inputs["frame"]  # type: ignore[assignment]
    framed = _full(frame, config, inputs).operating_frame
    wide = framed.loc[:, list(frame.columns) + ["operating_mode"]]
    clean = _append_derived_channels(wide, config)
    from src.analysis.derived import DERIVED_CHANNEL_INPUTS

    derived = sorted(
        str(c)
        for c in clean.columns
        if c not in wide.columns
        and c in DERIVED_CHANNEL_INPUTS
        # Committees are detectors, never features, and never recomputed
        # incrementally (their member transforms are not declared inputs).
        and committee_channel_reason(str(c)) is None
    )
    checked = 0
    for index, column in enumerate(derived):
        keep = set(_derived_ancestors({column}))
        moved = _append_derived_channels(_scrambled(wide, keep, index), config)
        for name in (column, f"{column}__gap"):
            if name in clean.columns:
                pd.testing.assert_series_equal(moved[name], clean[name], check_exact=True, obj=name)
        checked += 1
    assert checked >= 10


def test_every_behavior_model_reads_only_the_channels_it_names(
    config: CanaryConfig, inputs: dict[str, object]
) -> None:
    from src.decode.behavior_decoder import append_deviation_columns

    frame: pd.DataFrame = inputs["frame"]  # type: ignore[assignment]
    behavior = inputs["behavior"]
    framed = _full(frame, config, inputs).operating_frame
    before = framed.loc[
        :,
        [
            c
            for c in framed.columns
            if not str(c).startswith(("deviation:", "context:"))
            and not str(c).endswith(("__mean", "__std", "__roc"))
        ],
    ]
    for index, model in enumerate(behavior.models):  # type: ignore[attr-defined]
        one = behavior.model_copy(update={"models": [model]})  # type: ignore[attr-defined]
        clean = append_deviation_columns(before, one, rate_window_seconds=60)
        keep = set(behavior_model_input_roots(model))
        moved = append_deviation_columns(
            _scrambled(before, keep, index), one, rate_window_seconds=60
        )
        column = f"deviation:{model.name}"
        pd.testing.assert_series_equal(moved[column], clean[column], check_exact=True)
        pd.testing.assert_series_equal(
            moved[f"{column}__gap"], clean[f"{column}__gap"], check_exact=True
        )


def test_derived_reach_is_transitive() -> None:
    reached = derived_channels_reached({"obd:MAF"})
    # Air mass reads MAF, and every VE reads air mass.
    assert {"derived:AIR_MASS_PER_CYCLE", "derived:VE_AMBIENT_PERCENT"} <= reached
    assert "derived:STFT_BANK_BALANCE" not in reached
    assert derived_channels_reached({"obd:NOT_A_CHANNEL"}) == frozenset()


# ---------------------------------------------------------------------------
# P02.5: the decode catalogs are loaded once per process, by content
# ---------------------------------------------------------------------------


def test_decode_catalogs_load_once_per_process_and_reload_on_an_edit(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    calls: list[Path] = []
    real = feature_pipeline.load_dbc_dir

    def counting(path: Path) -> object:
        calls.append(path)
        return real(path)

    monkeypatch.setattr(feature_pipeline, "load_dbc_dir", counting)
    monkeypatch.setattr(feature_pipeline, "_DECODE_INPUTS_MEMO", {})
    bindings = tmp_path / "bindings.yaml"
    bindings.write_bytes((REPO / "data" / "bindings.yaml").read_bytes())

    first = feature_pipeline.default_decode_inputs(bindings)
    assert feature_pipeline.default_decode_inputs(bindings) is first
    assert len(calls) == 1
    # An edited catalog is a different catalog: reloaded, never served stale.
    bindings.write_bytes(bindings.read_bytes() + b"\n# edited\n")
    assert feature_pipeline.default_decode_inputs(bindings) is not first
    assert len(calls) == 2
