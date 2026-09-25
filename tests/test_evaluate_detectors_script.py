"""The one-command evaluation CLI (A02.4): drive selection, per-drive
reporting, summaries, and an end-to-end run on fixture sessions."""

from __future__ import annotations

import json
from datetime import UTC, datetime
from pathlib import Path

import numpy as np
import pandas as pd
import pytest

pytest.importorskip("sklearn")

from scripts.evaluate_detectors import (  # noqa: E402
    EVALUATION_SESSION_KINDS,
    SessionScore,
    clean_drive_record,
    main,
    render_markdown,
    summarize_recall,
)
from src.analysis.labelled_events import LabelledEvent  # noqa: E402
from src.control.sessions import recorded_sessions  # noqa: E402
from src.models.evaluation import SessionSpan, group_into_drives  # noqa: E402
from src.storage.parquet_writer import write_records_to_parquet  # noqa: E402
from src.telemetry import TelemetryRecord  # noqa: E402
from tests.builders import IFConfig

EXAMPLE_YAML = Path(__file__).resolve().parents[1] / "canary.example.yaml"


def _write_session(root: Path, name: str, *, start: float, n: int = 120, seed: int = 0) -> Path:
    rng = np.random.default_rng(seed)
    records: list[TelemetryRecord] = []
    for i in range(n):
        t = start + i
        records.append(TelemetryRecord.obd(t, "RPM", float(rng.normal(1500.0, 50.0)), "rpm"))
        records.append(TelemetryRecord.obd(t, "SPEED", float(rng.normal(30.0, 3.0)), "km/h"))
    session = root / name
    write_records_to_parquet(records, session, max_rows=1000)
    return session


def test_discovery_keeps_captures_drops_quarantine_and_orders_by_parsed_time(
    tmp_path: Path,
) -> None:
    """R06.6: the harness asks ``recorded_sessions`` with the tuning kind
    added, so a legacy ``_tuning-*`` recording still bridges drives as its
    local copy did. A directory with no telemetry chunk is not a recording."""
    for name in (
        "2026-09-18T120000",
        "_capture-tuning-2026-09-18T110000",
        "_tuning-2026-09-18T113000",
        "_archive",
        "_replay-2026-09-18T100000",
        "notes",
    ):
        (tmp_path / name).mkdir()
        (tmp_path / name / "telemetry-000000.parquet").write_bytes(b"PAR1")
    (tmp_path / "2026-09-18T130000").mkdir()  # rotated into, nothing flushed
    found = [p.name for p in recorded_sessions([tmp_path], kinds=EVALUATION_SESSION_KINDS)]
    assert found == [
        "_capture-tuning-2026-09-18T110000",
        "_tuning-2026-09-18T113000",
        "2026-09-18T120000",
    ]


def test_recall_excludes_withheld_outcomes_from_its_denominator() -> None:
    outcomes = [
        {"drive": "d1", "kind": "k", "severity": 1.0, "detector": "x", "status": "detected",
         "time_to_detect_seconds": 4.0},
        {"drive": "d1", "kind": "k", "severity": 1.0, "detector": "x", "status": "missed",
         "time_to_detect_seconds": None},
        {"drive": "d2", "kind": "k", "severity": 1.0, "detector": "x", "status": "withheld",
         "time_to_detect_seconds": None},
    ]  # fmt: skip
    block = summarize_recall(outcomes, ["x"], seed=0)["x"]["k"]["1.0"]
    assert block["graded"] == 2 and block["withheld"] == 1
    assert block["recall"]["estimate"] == pytest.approx(0.5)
    assert block["median_time_to_detect_seconds"] == 4.0


def test_only_fault_labels_take_episodes_out_of_the_false_alarms() -> None:
    stamps = pd.Series(pd.date_range("2026-09-18T12:00:00Z", periods=60, freq="1s"))
    frame = pd.DataFrame({"timestamp": stamps, "obd:RPM": 800.0})
    alerting = np.zeros(60, dtype=bool)
    alerting[5:8] = True  # inside the label
    alerting[40:43] = True  # outside it
    score = SessionScore(stamps, np.full(60, True, dtype=object), np.zeros(60), alerting)
    span = SessionSpan("s", stamps.iloc[0].to_pydatetime(), stamps.iloc[-1].to_pydatetime())
    (drive,) = group_into_drives([span])
    label = LabelledEvent(
        id="altitude",
        kind="novelty",
        title="t",
        start=datetime(2026, 9, 18, 12, 0, 0, tzinfo=UTC),
        end=datetime(2026, 9, 18, 12, 0, 20, tzinfo=UTC),
        end_basis="measured",
        provenance="test",
    )
    record = clean_drive_record(drive, {"s": frame}, {"s": {"x": score}}, ["x"], [label])
    block = record.detectors["x"]
    assert block["episodes"] == 2
    assert block["labelled_episodes"] == {"novelty": 1}
    # An altitude alert is explained, not excused: still a false alarm.
    assert block["false_alarm_episodes"] == 2
    assert record.labels == ["altitude"]

    fault = label.model_copy(update={"id": "leak", "kind": "fault"})
    record = clean_drive_record(drive, {"s": frame}, {"s": {"x": score}}, ["x"], [fault])
    assert record.detectors["x"]["false_alarm_episodes"] == 1


def _main_args(root: Path, artifact: Path, tmp_path: Path) -> list[str]:
    return [
        "--config",
        str(EXAMPLE_YAML),
        "--profile",
        "nissan-xterra-2008",
        "--sessions-root",
        str(root),
        "--if-model",
        str(artifact),
        "--no-sequence",
        "--baselines",
        "robust_z",
        "--faults",
        "bank_imbalance",
        "--injections-per-drive",
        "1",
        "--lead-seconds",
        "10",
        "--window-seconds",
        "30",
        "--grace-seconds",
        "5",
        "--labels",
        str(tmp_path / "no-labels.yaml"),
        "--tune-revisions",
        str(tmp_path / "no-revisions.yaml"),
        "--cache-dir",
        str(tmp_path / "cache"),
        "--workers",
        "1",
    ]


def test_main_scores_only_unseen_drives_and_names_every_skip(tmp_path: Path) -> None:
    from src.config import CanaryConfig
    from src.models.isolation_forest import default_manifest_path, fit_isolation_forest
    from src.preprocessing.feature_pipeline import featurize_wide_frame, load_resampled_session

    config = CanaryConfig.from_yaml(EXAMPLE_YAML)
    root = tmp_path / "sessions"
    seen = _write_session(root, "2026-05-12T000000", start=1_778_544_000.0, seed=1)
    _write_session(root, "2026-05-13T000000", start=1_778_630_400.0, seed=2)
    features = featurize_wide_frame(load_resampled_session(seen, config), config).features
    trained = fit_isolation_forest(
        features,
        IFConfig(n_estimators=40),
        feature_columns=["obd:RPM", "obd:SPEED"],
        fitted_at="2026-05-12T00:00:00Z",
    )
    artifact = tmp_path / "models" / "model.joblib"
    trained.save(artifact, default_manifest_path(artifact))
    out = tmp_path / "report.json"

    rc = main([*_main_args(root, artifact, tmp_path), "--out", str(out)])

    assert rc == 0
    report = json.loads(out.read_text())
    assert [d["sessions"] for d in report["drives"]] == [["2026-05-13T000000"]]
    (skip,) = report["skipped_drives"]
    assert "seen by 2026-05-12T000000 (isolation_forest training)" in skip["reason"]
    assert "min_agreement" in report["withheld_detectors"]["fused"]
    assert report["artifacts"]["isolation_forest"]["sha256"]
    # The fixture has no fuel-trim channels, so the fault cannot be injected —
    # and the report says so rather than scoring a miss.
    assert report["injections"] == []
    assert any("no role channel" in s["reason"] for s in report["skipped_injections"])
    # No IF calibration sessions to set a baseline threshold on: no baseline.
    assert report["detectors"] == ["isolation_forest"]
    # A04.1's known answer runs on every in-era drive, and — with no role
    # channel to shift in this fixture — says it rests on the control alone.
    shift = report["known_answers"]["single_quantity_shift"]
    (run,) = shift["per_drive"]
    assert run["drive"] == report["drives"][0]["drive"]
    verdict = run["detectors"]["isolation_forest"]
    assert verdict["outcomes"] == []
    assert verdict["control"]["quantity"] == "_control_all_features"
    assert verdict["verdict"] in ("withheld", "invalid")
    assert (tmp_path / "report.md").read_text().startswith("# Detector evaluation")
    assert list((tmp_path / "cache").glob("*-resampled.pkl"))

    # A05's checks join on the same rows when asked for (A02.5).
    out2 = tmp_path / "with-plausibility.json"
    argv = [*_main_args(root, artifact, tmp_path), "--out", str(out2)]
    argv[argv.index("robust_z")] = "plausibility"
    assert main(argv) == 0
    report2 = json.loads(out2.read_text())
    assert "plausibility" in report2["detectors"]
    assert "plausibility" in report2["known_answers"]["single_quantity_shift"]["not_applicable"]
    (drive,) = report2["drives"]
    assert drive["detectors"]["plausibility"]["scored_rows"] > 0
    assert list((tmp_path / "cache" / "plausibility").glob("*.parquet"))


def test_markdown_renders_a_minimal_report() -> None:
    report = {
        "generated_at": "2026-09-22T00:00:00Z",
        "detectors": ["x"],
        "artifacts": {},
        "persistence": {},
        "budget_per_engine_hour": 1.0,
        "sequence_parity": None,
        "training_eras": [],
        "drives": [],
        "false_alarms": {
            "x": {
                "false_alarms_per_engine_hour": {"estimate": None, "note": "withheld"},
                "withheld_share": None,
                "strata": {"baro": {}},
            }
        },
        "recall": {},
        "vus_pr": {},
        "events": [{"id": "e", "kind": "novelty", "start": "a", "end": "b", "status": "skipped"}],
        "skipped_drives": [{"drive": "d", "sessions": ["s"], "reason": "skipped: seen"}],
    }
    text = render_markdown(report)
    assert "| x | withheld |" in text
    assert "- d (s): skipped: seen" in text
    assert "## Not loaded" not in text


def test_a_baseline_session_that_did_not_load_is_named_not_counted() -> None:
    """A02.11 verifier: the report named a threshold session it never loaded.
    What the baselines rest on and what failed to load are separate lines."""
    report = {
        "generated_at": "2026-09-23T00:00:00Z",
        "detectors": [],
        "artifacts": {},
        "persistence": {},
        "budget_per_engine_hour": 1.0,
        "sequence_parity": None,
        "training_eras": [],
        "drives": [],
        "false_alarms": {},
        "recall": {},
        "vus_pr": {},
        "events": [],
        "skipped_drives": [],
        "baseline_threshold_sessions": ["a"],
        "baseline_threshold_sessions_not_loaded": ["b"],
        "load_errors": {"b": "not decoded yet (--cache-only)"},
    }
    text = render_markdown(report)
    assert "Baseline threshold sessions: ['a'] (requested but not loaded: ['b'])" in text
    assert "## Not loaded" in text
    assert "- b: not decoded yet (--cache-only)" in text


def test_each_injection_is_recorded_once_with_every_detectors_outcome() -> None:
    from scripts.evaluate_detectors import compact_injections

    injection = {"kind": "k", "severity": 1.0, "perturbed": ["obd:MAF"]}
    base = {"drive": "d", "session": "s", "kind": "k", "severity": 1.0, "rep": 0}
    compact = compact_injections(
        [
            {**base, "detector": "a", "status": "detected", "time_to_detect_seconds": 3.0,
             "injection": injection},
            {**base, "detector": "b", "status": "missed", "time_to_detect_seconds": None,
             "injection": injection},
        ]
    )  # fmt: skip
    assert len(compact) == 1
    assert compact[0]["perturbed"] == ["obd:MAF"]
    assert compact[0]["outcomes"] == {
        "a": {"status": "detected", "time_to_detect_seconds": 3.0},
        "b": {"status": "missed", "time_to_detect_seconds": None},
    }


def test_evidence_role_detectors_are_not_held_to_the_alert_budget() -> None:
    """A06.5: an evidence-role detector still reports its episodes per drive,
    but only alert-role detectors count against the budget."""
    from scripts.evaluate_detectors import DriveRecord, summarize_false_alarms
    from src.config import CanaryConfig
    from src.models.evaluation_detectors import detector_roles

    config = CanaryConfig.from_yaml(EXAMPLE_YAML)
    config.models.roles.isolation_forest = "alert"  # one of each role
    config.models.roles.sequence = "evidence"
    roles = detector_roles(config, ["isolation_forest", "sequence", "sequence_global", "fused"])
    assert roles == {
        "isolation_forest": "alert",
        "sequence": "evidence",
        "sequence_global": "evidence",
        "fused": "alert",
    }

    stamps = pd.Series(pd.date_range("2026-09-18T12:00:00Z", periods=2, freq="1s"))
    span = SessionSpan("s", stamps.iloc[0].to_pydatetime(), stamps.iloc[-1].to_pydatetime())
    (drive,) = group_into_drives([span])
    block = {"rows": 10, "withheld_rows": 0, "false_alarm_episodes": 4}
    record = DriveRecord(drive, ["s"], rows=10, hours=2.0)
    record.detectors = {"isolation_forest": dict(block), "sequence": dict(block)}
    summary = summarize_false_alarms(
        [record], ["isolation_forest", "sequence"], seed=0, roles=roles
    )
    assert summary["isolation_forest"]["false_alarms_per_engine_hour"]["estimate"] == 2.0
    assert summary["sequence"]["role"] == "evidence"
    assert summary["sequence"]["false_alarms_per_engine_hour"]["estimate"] is None


# ---------------------------------------------------------------------------
# R06.5: the regression compare folded in from the retired grid, and rows
# featurized with the behavior models (the grid's blind spot)
# ---------------------------------------------------------------------------


def _unseen_drive_fixture(tmp_path: Path) -> tuple[Path, Path]:
    """One training session (not scored) and one unseen session under
    ``sessions/``; returns ``(sessions root, IF artifact)``."""
    from src.config import CanaryConfig
    from src.models.isolation_forest import default_manifest_path, fit_isolation_forest
    from src.preprocessing.feature_pipeline import featurize_wide_frame, load_resampled_session

    config = CanaryConfig.from_yaml(EXAMPLE_YAML)
    training = _write_session(tmp_path / "train", "2026-05-12T000000", start=1_778_544_000.0)
    root = tmp_path / "sessions"
    _write_session(root, "2026-05-13T000000", start=1_778_630_400.0, seed=2)
    trained = fit_isolation_forest(
        featurize_wide_frame(load_resampled_session(training, config), config).features,
        IFConfig(n_estimators=40),
        feature_columns=["obd:RPM", "obd:SPEED"],
        fitted_at="2026-05-12T00:00:00Z",
    )
    artifact = tmp_path / "models" / "model.joblib"
    trained.save(artifact, default_manifest_path(artifact))
    return root, artifact


def test_baseline_compare_exits_3_on_a_regression_and_names_incomparable_baselines(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    root, artifact = _unseen_drive_fixture(tmp_path)
    first = tmp_path / "first.json"
    assert main([*_main_args(root, artifact, tmp_path), "--out", str(first)]) == 0
    report = json.loads(first.read_text())
    assert report["protocol"]["name"] == "evaluate_detectors"
    assert report["protocol"]["faults"] == ["bank_imbalance"]

    # The same drives, the same protocol: compared, nothing regressed.
    again = tmp_path / "again.json"
    argv = [*_main_args(root, artifact, tmp_path), "--baseline", str(first)]
    assert main([*argv, "--out", str(again)]) == 0
    assert json.loads(again.read_text())["regressions"]["comparable"] is True
    assert "no regressions beyond tolerance" in again.with_suffix(".md").read_text()

    # A baseline that graded a cell this run cannot grade: exit 3, named.
    report["recall"] = {
        "isolation_forest": {"bank_imbalance": {"0.1": {"graded": 2, "detected": 2}}}
    }
    tampered = tmp_path / "tampered.json"
    tampered.write_text(json.dumps(report))
    regressed = tmp_path / "regressed.json"
    argv = [*_main_args(root, artifact, tmp_path), "--baseline", str(tampered)]
    assert main([*argv, "--out", str(regressed)]) == 3
    assert "isolation_forest bank_imbalance @0.1: graded in the baseline" in capsys.readouterr().out

    # A retired-grid report is incomparable: reported, not a failure.
    grid = tmp_path / "grid.json"
    grid.write_text(json.dumps({"table": [], "cells": []}))
    out = tmp_path / "vs-grid.json"
    argv = [*_main_args(root, artifact, tmp_path), "--baseline", str(grid)]
    assert main([*argv, "--out", str(out)]) == 0
    regressions = json.loads(out.read_text())["regressions"]
    assert regressions["comparable"] is False
    assert "retired detector_benchmark grid" in regressions["reason"]

    unreadable = tmp_path / "missing.json"
    argv = [*_main_args(root, artifact, tmp_path), "--baseline", str(unreadable)]
    assert main([*argv, "--out", str(tmp_path / "x.json")]) == 2


def _behavior_models_file(path: Path) -> Path:
    import yaml

    path.write_text(
        yaml.safe_dump(
            {
                "schema_version": 1,
                "models": [
                    {
                        "name": "speed_vs_rpm",
                        "kind": "surface_2d",
                        "output": "obd:SPEED",
                        "axes": [
                            {"name": "rpm", "channel": "obd:RPM", "bin_edges": [0.0, 3000.0]},
                            {"name": "rpm2", "channel": "obd:RPM", "bin_edges": [0.0, 3000.0]},
                        ],
                        "fit": {
                            "kind": "grid_binned_mean",
                            "cells": [{"mean": 30.0, "std": 3.0, "count": 50}],
                            "sample_count": 50,
                            "fitted_at": "2026-01-01T00:00:00Z",
                        },
                    }
                ],
            }
        )
    )
    return path


def test_rows_are_featurized_with_the_behavior_models(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The retired grid featurized with no behavior models, so an IF trained
    on ``deviation:*`` columns could not be scored as served. Every
    featurization here carries the profile's (or ``--behavior-models``) set,
    and the report says which file."""
    import scripts.evaluate_detectors as harness
    from src.preprocessing import feature_pipeline

    root, artifact = _unseen_drive_fixture(tmp_path)
    models_path = _behavior_models_file(tmp_path / "behavior_models.yaml")
    seen: list[object] = []

    def spy(*args: object, **kwargs: object) -> object:
        seen.append(kwargs.get("behavior_models"))
        return feature_pipeline.featurize_wide_frame(*args, **kwargs)  # type: ignore[arg-type]

    monkeypatch.setattr(harness, "featurize_wide_frame", spy)
    out = tmp_path / "report.json"
    argv = [*_main_args(root, artifact, tmp_path), "--behavior-models", str(models_path)]
    assert main([*argv, "--out", str(out)]) == 0
    assert seen and all(models is not None for models in seen)
    record = json.loads(out.read_text())["behavior_models"]
    assert record["path"] == str(models_path) and record["loaded"] is True
    assert record["sha256"]
    # This IF consumes no deviation column: nothing to trace.
    assert record["provenance"] == {}


def test_an_if_whose_deviation_columns_cannot_be_produced_is_refused(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """Scoring it with those columns empty would report a detector the daemon
    does not serve."""
    import scripts.evaluate_detectors as harness

    root, artifact = _unseen_drive_fixture(tmp_path)
    monkeypatch.setattr(
        harness,
        "behavior_fingerprint_provenance",
        lambda *_a, **_k: {
            "speed_vs_rpm": {"status": "absent", "reason": "no behavior model 'speed_vs_rpm'"}
        },
    )
    argv = [*_main_args(root, artifact, tmp_path), "--behavior-models", str(tmp_path / "none")]
    assert main([*argv, "--out", str(tmp_path / "r.json")]) == 2
    assert "no behavior model 'speed_vs_rpm'" in capsys.readouterr().err
    assert not (tmp_path / "r.json").exists()
