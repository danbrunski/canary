"""``scripts/evaluate_behavior_models.py`` (B02.1) and the refit CLI's B02.4
provenance checks."""

from __future__ import annotations

import json
import sys
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import numpy as np
import pandas as pd
import pytest
import yaml

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
import evaluate_behavior_models as eval_cli  # noqa: E402
import refit_behavior_models as refit_cli  # noqa: E402

from src.analysis.behavior_validation import session_content_identity  # noqa: E402
from src.analysis.refit import Surface2DRecipe, fit_recipe  # noqa: E402
from src.analysis.tune_revisions import TuneRevision, TuneRevisionRegistry  # noqa: E402
from src.decode.behavior_models import (  # noqa: E402
    BehaviorModels,
    DeviationConfig,
    NoiseFloor,
    SyntheticFault,
    ValidationCriteria,
    dump_behavior_models,
)
from src.preprocessing.resampler import SESSION_COLUMN, TIMESTAMP_COLUMN  # noqa: E402
from src.preprocessing.segmenter import MODE_COLUMN  # noqa: E402

T0 = datetime(2026, 8, 1, 12, 0, tzinfo=UTC)
NAMES = [f"2026-08-0{day + 1}T120000" for day in range(6)]


def _drive(name: str, day: int, n: int = 400) -> pd.DataFrame:
    rng = np.random.default_rng(day)
    rpm = rng.uniform(800, 4000, n)
    load = rng.uniform(10, 90, n)
    return pd.DataFrame(
        {
            TIMESTAMP_COLUMN: pd.date_range(
                T0 + timedelta(days=day), periods=n, freq="1s", tz="UTC"
            ),
            SESSION_COLUMN: name,
            MODE_COLUMN: "city",
            "obd:RPM": rpm,
            "obd:ENGINE_LOAD": load,
            "obd:TIMING_ADVANCE": 10.0 + 0.004 * rpm - 0.1 * load + rng.normal(0.0, 0.4, n),
        }
    )


def _setup(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> tuple[Path, list[Path], Path]:
    frame = pd.concat([_drive(name, day) for day, name in enumerate(NAMES)], ignore_index=True)
    criteria = ValidationCriteria(
        kind="predictive_surface",
        unit="deg",
        noise_floor=NoiseFloor(value=0.5, basis="LSB"),
        meaningful_deviation=5.0,
        meaningful_deviation_basis="test",
        min_eligible_observations=100,
        min_envelope_coverage=0.0,
        min_skill_vs_baseline=0.3,
        synthetic_faults=[SyntheticFault(magnitude=6.0, direction="negative", duration_seconds=60)],
    )
    recipe = Surface2DRecipe(
        name="timing_advance_vs_rpm_load",
        output="obd:TIMING_ADVANCE",
        x_column="obd:RPM",
        y_column="obd:ENGINE_LOAD",
        x_edges=(800.0, 1600.0, 2400.0, 3200.0, 4000.0),
        y_edges=(10.0, 30.0, 50.0, 70.0, 90.0),
        min_samples=200,
        min_cell_count=5,
        min_rpm=400.0,
        max_validation_warning_rate=1.0,
        max_validation_critical_rate=1.0,
        deviation=DeviationConfig(
            kind="sigma", warning_band=3.0, critical_band=6.0, sigma_floor=0.5
        ),
        validation=criteria,
    )
    # Fit on the first five drives; the sixth stays independent.
    fitted = fit_recipe(recipe, frame[frame[SESSION_COLUMN] != NAMES[-1]])
    assert fitted.model is not None, fitted.reason
    artifact = tmp_path / "behavior_models.yaml"
    dump_behavior_models(BehaviorModels(schema_version=2, models=[fitted.model]), artifact)
    validation = tmp_path / "behavior_validation.yaml"
    validation.write_text(
        yaml.safe_dump(
            {
                "approved_operating_modes": [
                    "koeo",
                    "warmup",
                    "idle_transient",
                    "idle",
                    "idle_high",
                    "city",
                    "highway",
                ],
                "retired_operating_modes": {"suburban": "city"},
                "recipes": {recipe.name: criteria.model_dump(mode="json")},
            }
        )
    )
    sessions = []
    for name in NAMES:
        session = tmp_path / "corpus" / name
        session.mkdir(parents=True)
        (session / "telemetry-00000.parquet").write_bytes(name.encode())
        sessions.append(session)
    monkeypatch.setattr(eval_cli, "build_evaluation_frame", lambda *_a, **_k: frame)
    return artifact, sessions, validation


def _run(
    tmp_path: Path, artifact: Path, validation: Path, *extra: str
) -> tuple[int, dict[str, Any]]:
    out = tmp_path / "report.json"
    rc = eval_cli.main(
        [
            "--profile",
            "nissan-xterra-2008",
            "--artifact",
            str(artifact),
            "--validation",
            str(validation),
            "--out",
            str(out),
            *extra,
        ]
    )
    return rc, (json.loads(out.read_text()) if rc == 0 else {})


def test_historical_report_reproduces_the_persisted_holdout(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    artifact, sessions, validation = _setup(tmp_path, monkeypatch)
    rc, report = _run(
        tmp_path,
        artifact,
        validation,
        "--config",
        "canary.pi.example.yaml",
        "--mode",
        "historical",
        *map(str, sessions),
    )
    assert rc == 0
    entry = report["models"]["timing_advance_vs_rpm_load"]
    assert entry["historical"]["status"] == "reproduced", entry["historical"]
    assert entry["serving_status"]["state"] == "active"
    inputs = report["inputs"]
    for key in ("config", "artifact", "validation", "bindings", "dids"):
        assert "@sha256:" in inputs[key] or inputs[key].endswith("@missing")
    assert inputs["decode_can"] == "none"
    assert [s["sha256"] for s in inputs["sessions"]] == [
        session_content_identity(s)["sha256"] for s in sessions
    ]
    assert report["selection_rules"] == eval_cli.SELECTION_RULES["historical"]
    assert "3-of-5" in inputs["event_policy"]


def test_corrected_report_needs_the_approved_taxonomy(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    artifact, sessions, validation = _setup(tmp_path, monkeypatch)
    # canary.example.yaml adds IMU event modes the profile has not approved.
    rc, _ = _run(
        tmp_path,
        artifact,
        validation,
        "--config",
        "canary.example.yaml",
        "--mode",
        "corrected",
        *map(str, sessions),
    )
    assert rc == 2
    rc, report = _run(
        tmp_path,
        artifact,
        validation,
        "--config",
        "canary.pi.example.yaml",
        "--mode",
        "corrected",
        *map(str, sessions),
    )
    assert rc == 0
    corrected = report["models"]["timing_advance_vs_rpm_load"]["corrected"]
    # Training and held-out drives are excluded; only the sixth is independent.
    assert corrected["independent_drives"] == [NAMES[-1]]
    assert set(corrected["excluded_training_drives"]) == set(NAMES[:-1])
    metrics = corrected["metrics"]
    assert metrics["evaluated"] > 0 and metrics["drives"] == 1
    assert corrected["baseline_source"].startswith("artifact evidence")
    # One independent drive cannot meet a two-drive criterion.
    assert corrected["would_be_verdict"] == "insufficient_evidence"
    assert any(f.startswith("test_drives=1<2") for f in corrected["failures"])
    assert corrected["synthetic_sensitivity"][0]["synthetic"] is True


def test_manifest_hash_mismatch_refuses(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    artifact, sessions, validation = _setup(tmp_path, monkeypatch)
    manifest = tmp_path / "manifest.yaml"
    manifest.write_text(
        yaml.safe_dump(
            {
                "corpus_root": str(tmp_path / "corpus"),
                "sessions": [
                    {"path": sessions[0].name, "sha256": "sha256:" + "0" * 64},
                ],
            }
        )
    )
    rc, _ = _run(
        tmp_path,
        artifact,
        validation,
        "--config",
        "canary.pi.example.yaml",
        "--mode",
        "historical",
        "--manifest",
        str(manifest),
    )
    assert rc == 2


def test_config_is_required() -> None:
    with pytest.raises(SystemExit):
        eval_cli.parse_args(
            ["--profile", "p", "--artifact", "a", "--mode", "historical", "--out", "o"]
        )


# ---------------------------------------------------------------------------
# refit CLI provenance (B02.4)
# ---------------------------------------------------------------------------


def _registry() -> TuneRevisionRegistry:
    return TuneRevisionRegistry(
        revisions=[
            TuneRevision(revision="stock", flashed_at=datetime(2000, 1, 1, tzinfo=UTC)),
            TuneRevision(revision="flash-1", flashed_at=datetime(2026, 7, 26, 22, 31, tzinfo=UTC)),
        ]
    )


def test_session_crossing_a_flash_needs_a_named_resolution() -> None:
    ranges = {
        "2026-07-26T214348": (
            datetime(2026, 7, 26, 21, 43, 49, tzinfo=UTC),
            datetime(2026, 7, 26, 22, 31, 47, tzinfo=UTC),
        ),
        "2026-07-26T223147": (
            datetime(2026, 7, 26, 22, 31, 47, tzinfo=UTC),
            datetime(2026, 7, 26, 22, 50, 24, tzinfo=UTC),
        ),
    }
    refusals, notes = refit_cli._flash_crossings(ranges, _registry(), None)
    assert refusals == ["2026-07-26T214348 starts on stock and ends on flash-1"]
    assert notes == []
    from src.analysis.behavior_validation import BehaviorValidationDocument

    doc = BehaviorValidationDocument.model_validate(
        {
            "approved_operating_modes": ["idle"],
            "provenance_resolutions": [
                {
                    "session": "2026-07-26T214348",
                    "revision": "stock",
                    "reason": "rotation at the post-flash restart",
                }
            ],
        }
    )
    refusals, notes = refit_cli._flash_crossings(ranges, _registry(), doc)
    assert refusals == []
    assert notes and "resolved to stock" in notes[0]


def test_exclusion_resolution_and_unknown_revision(tmp_path: Path) -> None:
    from src.analysis.behavior_validation import BehaviorValidationDocument

    early = tmp_path / "1999-01-01T000000"
    kept = tmp_path / "2026-08-01T120000"
    doc = BehaviorValidationDocument.model_validate(
        {
            "approved_operating_modes": ["idle"],
            "provenance_resolutions": [
                {"session": early.name, "exclude": True, "reason": "predates the registry"}
            ],
        }
    )
    registry = TuneRevisionRegistry(
        revisions=[TuneRevision(revision="stock", flashed_at=datetime(2000, 1, 1, tzinfo=UTC))]
    )
    with pytest.raises(ValueError, match="unknown"):
        refit_cli._consistent_tune_revision([early, kept], registry, None)
    sessions, notes = refit_cli._apply_exclusions([early, kept], doc)
    assert sessions == [kept] and "predates the registry" in notes[0]
    assert refit_cli._consistent_tune_revision(sessions, registry, doc) == "stock"


def test_sidecars_classified_before_the_mode_label_fix_do_not_gate(tmp_path: Path) -> None:
    def sidecar(name: str, classified_at: str) -> Path:
        session = tmp_path / name
        session.mkdir()
        (session / "classification.json").write_text(
            json.dumps(
                {
                    "schema_version": 1,
                    "classifier_version": "0.2.0",
                    "classified_at": classified_at,
                    "session_id": name,
                    "duration_seconds": 60.0,
                    "summary": {"mode_seconds": {}, "event_counts": {"cold_start": 1}},
                    "mode_segments": [],
                    "events": [],
                    "quality": {},
                    "disposition": "ready",
                }
            )
        )
        return session

    old = sidecar("2026-08-06T034415", "2026-08-07T00:00:00Z")
    new = sidecar("2026-09-20T120000", "2026-09-21T00:00:00Z")
    events, stale = refit_cli._event_sidecars([old, new])
    assert events == {new.name: {"cold_start"}}
    assert len(stale) == 1 and "D03.22" in stale[0]
