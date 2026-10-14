"""D03.27 — every resample that means "the training grid" uses one recipe.

``analyze_session`` once built its own ``ResampleConfig`` from ``hz`` and the
slow-channel ceiling only, so ``max_gap_factor`` and ``session_gap_seconds``
silently took the dataclass defaults; eight more callers had the same shape,
and the manifest's ``max_gap_seconds`` dropped ``max_gap_factor``. The fix is
at the layer: :func:`src.preprocessing.resampler.resample_config_for` is the
one constructor, and the scan below fails when a new direct
``ResampleConfig(...)`` appears outside a short, reasoned allowlist.

The per-caller tests use a config with every gap rule off its default, so a
caller that drops any of them is visible.
"""

from __future__ import annotations

import ast
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import pandas as pd
import pytest

from src.columns import SESSION_COLUMN, TIMESTAMP_COLUMN
from src.config import CanaryConfig
from src.preprocessing.resampler import ResampleConfig, resample_config_for
from src.storage.parquet_writer import write_records_to_parquet
from src.telemetry import TelemetryRecord
from tests.configs import example_config

REPO_ROOT = Path(__file__).resolve().parents[1]
EXAMPLE_YAML = REPO_ROOT / "canary.example.yaml"

#: Files allowed to construct ``ResampleConfig`` directly, and why. Anything
#: else must go through ``resample_config_for``.
ALLOWED_DIRECT_CONSTRUCTION: dict[str, str] = {
    # The dataclass's home: ``resample_config_for`` itself, and the
    # ``config=None`` default of ``resample_wide`` / ``resample_long``.
    "src/preprocessing/resampler.py": "defines ResampleConfig and resample_config_for",
    # Ad-hoc display grid at a caller-chosen rate for the binned-analysis and
    # session-timeseries tools; no CanaryConfig in scope, and it answers "what
    # did these channels read together", not "what did the model see".
    "src/analysis/binned_analysis.py": "ad-hoc display grid, not the training grid",
}


class _StopError(Exception):
    """Raised by a spy to end a caller once its resample config is seen."""


def _direct_constructions(path: Path) -> list[int]:
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    lines = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        func = node.func
        name = (
            func.id
            if isinstance(func, ast.Name)
            else func.attr
            if isinstance(func, ast.Attribute)
            else None
        )
        if name == "ResampleConfig":
            lines.append(node.lineno)
    return lines


def test_no_direct_resample_config_outside_the_allowlist() -> None:
    offenders = []
    for top in ("src", "scripts", "tools"):
        for path in sorted((REPO_ROOT / top).rglob("*.py")):
            rel = path.relative_to(REPO_ROOT).as_posix()
            if rel in ALLOWED_DIRECT_CONSTRUCTION:
                continue
            offenders.extend(f"{rel}:{line}" for line in _direct_constructions(path))
    assert not offenders, (
        "direct ResampleConfig(...) construction; use "
        "src.preprocessing.resampler.resample_config_for(config) (the training "
        "recipe; hz= for a deliberate rate), or add the file to "
        "ALLOWED_DIRECT_CONSTRUCTION with the reason it is not the training grid "
        f"(D03.27): {offenders}"
    )


def test_allowlist_entries_still_construct_directly() -> None:
    """A stale allowlist entry would quietly exempt a file's future code."""
    for rel in ALLOWED_DIRECT_CONSTRUCTION:
        assert _direct_constructions(REPO_ROOT / rel), f"{rel} no longer needs its exemption"


def test_the_scan_catches_a_direct_construction(tmp_path: Path) -> None:
    probe = tmp_path / "probe.py"
    probe.write_text(
        "from src.preprocessing import resampler\n"
        "a = ResampleConfig(hz=1.0)\n"
        "b = resampler.ResampleConfig(hz=2.0)\n"
        "c = resample_config_for(config)\n",
        encoding="utf-8",
    )
    assert _direct_constructions(probe) == [2, 3]


@pytest.fixture
def tuned() -> CanaryConfig:
    base = example_config(EXAMPLE_YAML)
    return base.model_copy(
        update={
            "preprocessing": base.preprocessing.model_copy(
                update={
                    "max_gap_factor": 5.0,
                    "session_gap_seconds": 45.0,
                    "slow_channel_max_gap_seconds": 77.0,
                }
            )
        }
    )


def test_resample_config_for_carries_every_gap_rule(tuned: CanaryConfig) -> None:
    got = resample_config_for(tuned)
    assert got == ResampleConfig(
        hz=tuned.preprocessing.resample_hz,
        max_gap_factor=5.0,
        slow_channel_max_gap_seconds=77.0,
        session_gap_seconds=45.0,
    )
    # A deliberate rate keeps the gap rules: the factor is in intervals.
    at_4hz = resample_config_for(tuned, hz=4.0)
    assert at_4hz.hz == 4.0
    assert at_4hz.max_gap_factor == 5.0
    assert at_4hz.max_gap.total_seconds() == pytest.approx(1.25)
    assert at_4hz.slow_channel_max_gap_seconds == 77.0
    assert at_4hz.session_gap_seconds == 45.0


def test_feature_pipeline_reexports_the_same_function() -> None:
    from src.features import feature_pipeline

    assert feature_pipeline.resample_config_for is resample_config_for


def test_manifest_max_gap_seconds_honours_max_gap_factor(tuned: CanaryConfig) -> None:
    """It recorded ResampleConfig(hz=...).max_gap — the default factor 2.0 —
    while the live source applied the configured factor."""
    from src.features.feature_pipeline import build_feature_spec

    features = pd.DataFrame(
        {
            SESSION_COLUMN: ["s"] * 3,
            TIMESTAMP_COLUMN: pd.date_range("2026-01-01", periods=3, freq="1s", tz="UTC"),
            "operating_mode": ["idle"] * 3,
            "obd:RPM": [800.0, 810.0, 820.0],
        }
    )
    spec = build_feature_spec(features, ["obd:RPM"], config=tuned)
    expected = resample_config_for(tuned).max_gap.total_seconds()
    assert spec["max_gap_seconds"] == pytest.approx(expected)
    assert spec["max_gap_seconds"] == pytest.approx(5.0 / tuned.preprocessing.resample_hz)
    # P02.10: the broadcast stale class travels with the model beside it.
    assert spec["broadcast_max_gap_seconds"] == pytest.approx(
        tuned.collection.broadcast_stale_timeout_seconds
    )
    assert spec["broadcast_max_gap_seconds"] == pytest.approx(
        resample_config_for(tuned).broadcast_max_gap_seconds
    )


def test_live_source_stale_ceilings_match_the_recipe(tuned: CanaryConfig) -> None:
    from src.features.live import LiveFeatureSource

    source = LiveFeatureSource(config=tuned)
    recipe = resample_config_for(tuned)
    assert source._max_gap_seconds == pytest.approx(recipe.max_gap.total_seconds())
    assert source._slow_max_gap_seconds == pytest.approx(77.0)


def test_synthetic_dropout_holds_for_the_recipe_ceiling(tuned: CanaryConfig) -> None:
    from src.models.synthetic_fault_eval import _max_gap_seconds_for

    recipe = resample_config_for(tuned)
    assert _max_gap_seconds_for("obd:RPM", tuned) == pytest.approx(recipe.max_gap.total_seconds())
    assert _max_gap_seconds_for("obd:COOLANT_TEMP", tuned) == pytest.approx(77.0)


def _write_session(root: Path, name: str = "2026-01-01T000000") -> Path:
    session = root / name
    start = datetime(2026, 1, 1, tzinfo=UTC)
    records = []
    for i in range(40):
        t = start + timedelta(seconds=i)
        records.extend(
            [
                TelemetryRecord(t, "obd", "RPM", 900.0 + i, "rpm"),
                TelemetryRecord(t, "obd", "SPEED", 0.0, "km/h"),
                TelemetryRecord(t, "obd", "THROTTLE_POS", 8.0, "%"),
                TelemetryRecord(t, "obd", "COOLANT_TEMP", 85.0, "C"),
            ]
        )
    write_records_to_parquet(records, session, max_rows=100_000)
    return session


def _spy_resample_wide(
    monkeypatch: pytest.MonkeyPatch, module: Any, *, stop: bool = False
) -> list[ResampleConfig]:
    seen: list[ResampleConfig] = []
    real = module.resample_wide

    def spy(wide: pd.DataFrame, **kwargs: Any) -> pd.DataFrame:
        seen.append(kwargs["config"])
        if stop:
            raise _StopError
        return real(wide, **kwargs)

    monkeypatch.setattr(module, "resample_wide", spy)
    return seen


def test_dataset_health_uses_the_recipe(
    tuned: CanaryConfig, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from src.datasets import health

    seen = _spy_resample_wide(monkeypatch, health)
    health.analyze_session(_write_session(tmp_path), tuned)
    assert seen == [resample_config_for(tuned)]


def test_dataset_export_uses_the_recipe_at_the_requested_rate(
    tuned: CanaryConfig, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from src.datasets import export

    session = _write_session(tmp_path)
    seen = _spy_resample_wide(monkeypatch, export)
    spec = export.DatasetExportSpec(sessions=[session.name], resample_hz=2.0)
    export.build_dataset(spec, tmp_path, tuned)
    assert seen == [resample_config_for(tuned, hz=2.0)]
    assert seen[0].max_gap_factor == 5.0
    assert seen[0].session_gap_seconds == 45.0


def test_replay_library_uses_the_recipe(
    tuned: CanaryConfig, monkeypatch: pytest.MonkeyPatch
) -> None:
    from src.analysis import replay

    seen = _spy_resample_wide(monkeypatch, replay)
    wide = pd.DataFrame(
        {
            SESSION_COLUMN: ["s"] * 3,
            TIMESTAMP_COLUMN: pd.date_range("2026-01-01", periods=3, freq="1s", tz="UTC"),
            "obd:RPM": [800.0, 810.0, 820.0],
        }
    )
    replay._resample_numeric(wide, ["obd:RPM"], tuned)
    assert seen == [resample_config_for(tuned)]


def test_evaluation_session_loader_uses_the_recipe(
    tuned: CanaryConfig, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from src.features import feature_pipeline
    from src.preprocessing import derived

    # evaluate_detectors (and so retrain's benchmark gate, R06.5) loads
    # sessions with the trainers' own per-session chain, which since P01.1
    # resamples the reduced session in src.preprocessing.derived (once per
    # channel batch).
    seen = _spy_resample_wide(monkeypatch, derived)
    feature_pipeline.load_resampled_session(
        _write_session(tmp_path), tuned, bindings_path=tmp_path / "absent.yaml"
    )
    assert seen and set(seen) == {resample_config_for(tuned)}


def test_disposition_report_uses_the_recipe(
    tuned: CanaryConfig, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from src.features import feature_pipeline

    # The screen scripts/disposition_report.py runs (R06.4: moved to src).
    seen = _spy_resample_wide(monkeypatch, feature_pipeline, stop=True)
    with pytest.raises(_StopError):
        feature_pipeline.disposition_screen_frames(
            [_write_session(tmp_path)], tuned, bindings_path=tmp_path / "absent.yaml"
        )
    assert seen == [resample_config_for(tuned)]


def test_train_anomaly_model_evaluate_uses_the_recipe(
    tuned: CanaryConfig, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import scripts.train_anomaly_model as train_script

    seen = _spy_resample_wide(monkeypatch, train_script, stop=True)
    with pytest.raises(_StopError):
        train_script.evaluate(
            None,  # type: ignore[arg-type]  # never reached: the spy stops first
            _write_session(tmp_path),
            tuned,
            bindings_path=tmp_path / "absent.yaml",
        )
    assert seen == [resample_config_for(tuned)]
