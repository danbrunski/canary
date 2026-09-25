"""R04.6: the A02 evaluation scores default-profile rows only.

Operator decision 2026-09-24: tuning logs do not represent the default
profile, so they are not in the anomaly detectors -- neither trained,
calibrated, scored live (R03.12) nor evaluated (this file). A capture still
bridges its drive (``drives_of_sessions``), so held-out stays honest, but its
rows reach no metric, and a drive with nothing else is reported as skipped.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path

import numpy as np
import pytest

pytest.importorskip("sklearn")

from scripts.evaluate_detectors import main  # noqa: E402
from src.control.capture_manifest import CAPTURE_MANIFEST_FILENAME  # noqa: E402
from src.models.evaluation import default_profile_exclusion  # noqa: E402
from src.storage.parquet_writer import write_records_to_parquet  # noqa: E402
from src.telemetry import TelemetryRecord  # noqa: E402

EXAMPLE_YAML = Path(__file__).resolve().parents[1] / "canary.example.yaml"
DAY = 1_778_630_400.0  # 2026-05-13T00:00:00Z


@dataclass(frozen=True)
class _IFConfig:
    enabled: bool = True
    n_estimators: int = 40
    contamination: float = 0.02
    per_mode_contamination: dict[str, float] = field(default_factory=dict)
    random_state: int = 7
    calibration_holdout_fraction: float = 0.2
    target_flag_rate: float = 0.05


def _write(root: Path, name: str, *, start: float, n: int, rpm: float, seed: int) -> Path:
    rng = np.random.default_rng(seed)
    records: list[TelemetryRecord] = []
    for i in range(n):
        t = start + i
        records.append(TelemetryRecord.obd(t, "RPM", float(rng.normal(rpm, 50.0)), "rpm"))
        records.append(TelemetryRecord.obd(t, "SPEED", float(rng.normal(30.0, 3.0)), "km/h"))
    session = root / name
    write_records_to_parquet(records, session, max_rows=1000)
    return session


def _capture_manifest(session: Path, *, profile: str, corpus_eligible: bool) -> None:
    (session / CAPTURE_MANIFEST_FILENAME).write_text(
        json.dumps(
            {
                "profile": profile,
                "started_at": "2026-05-13T00:01:00Z",
                "profile_snapshot": {"name": profile, "corpus_eligible": corpus_eligible},
            }
        )
    )


def test_default_profile_exclusion_names_each_kind(tmp_path: Path) -> None:
    default = tmp_path / "2026-05-13T000000"
    default.mkdir()
    tuning = tmp_path / "_tuning-2026-05-13T000000"
    tuning.mkdir()
    capture = tmp_path / "_capture-tuning-2026-05-13T000100"
    capture.mkdir()
    _capture_manifest(capture, profile="tuning", corpus_eligible=False)
    eligible_capture = tmp_path / "_capture-default-2026-05-13T000200"
    eligible_capture.mkdir()
    _capture_manifest(eligible_capture, profile="default", corpus_eligible=True)

    assert default_profile_exclusion(default) is None
    assert default_profile_exclusion(eligible_capture) is None
    assert "tuning session" in (default_profile_exclusion(tuning) or "")
    assert "'tuning'" in (default_profile_exclusion(capture) or "")


def test_tuning_capture_rows_reach_no_metric(tmp_path: Path) -> None:
    from scripts.detector_benchmark import load_resampled_session
    from src.config import CanaryConfig
    from src.models.isolation_forest import default_manifest_path, fit_isolation_forest
    from src.preprocessing.feature_pipeline import featurize_wide_frame

    config = CanaryConfig.from_yaml(EXAMPLE_YAML)
    training = _write(tmp_path / "training", "2026-05-01T000000", start=DAY - 12 * 86_400,
                      n=120, rpm=1500.0, seed=1)  # fmt: skip
    trained = fit_isolation_forest(
        featurize_wide_frame(load_resampled_session(training, config), config).features,
        _IFConfig(),
        feature_columns=["obd:RPM", "obd:SPEED"],
        fitted_at="2026-05-12T00:00:00Z",
    )
    artifact = tmp_path / "models" / "model.joblib"
    trained.save(artifact, default_manifest_path(artifact))

    root = tmp_path / "sessions"
    default = _write(root, "2026-05-13T000000", start=DAY, n=120, rpm=1500.0, seed=2)
    # The sibling capture of the same drive, overlapping it in time; its rows
    # would be wildly anomalous if anything scored them.
    capture = _write(root, "_capture-tuning-2026-05-13T000030", start=DAY + 30, n=120,
                     rpm=6000.0, seed=3)  # fmt: skip
    _capture_manifest(capture, profile="tuning", corpus_eligible=False)
    # A drive made of a tuning session alone.
    _write(root, "_tuning-2026-05-14T000000", start=DAY + 86_400, n=120, rpm=6000.0, seed=4)

    out = tmp_path / "report.json"
    rc = main(
        [
            "--config", str(EXAMPLE_YAML),
            "--profile", "nissan-xterra-2008",
            "--sessions-root", str(root),
            "--if-model", str(artifact),
            "--no-sequence",
            "--baselines", "robust_z",
            "--faults", "bank_imbalance",
            "--injections-per-drive", "1",
            "--lead-seconds", "10",
            "--window-seconds", "30",
            "--grace-seconds", "5",
            "--labels", str(tmp_path / "no-labels.yaml"),
            "--tune-revisions", str(tmp_path / "no-revisions.yaml"),
            "--cache-dir", str(tmp_path / "cache"),
            "--workers", "1",
            "--out", str(out),
        ]
    )  # fmt: skip

    assert rc == 0
    report = json.loads(out.read_text())
    (drive,) = report["drives"]
    # The capture bridged the drive but was not scored, and is named as such
    # (not as a session that failed to load).
    assert drive["sessions"] == [default.name]
    assert drive["missing_sessions"] == []
    assert set(drive["excluded_sessions"]) == {capture.name}
    default_rows = len(load_resampled_session(default, config))
    assert drive["rows"] == default_rows
    assert drive["detectors"]["isolation_forest"]["rows"] == default_rows
    # Nothing downstream saw the capture either.
    for run in report["known_answers"]["single_quantity_shift"]["per_drive"]:
        assert run["session"] == default.name
    assert all(entry.get("session") != capture.name for entry in report["injections"])
    skipped = {entry["drive"]: entry for entry in report["skipped_drives"]}
    (tuning_only,) = [e for e in skipped.values() if "_tuning-2026-05-14T000000" in e["sessions"]]
    assert tuning_only["reason"].startswith("skipped: no default-profile rows")
