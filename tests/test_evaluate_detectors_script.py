"""The one-command evaluation CLI (A02.4): drive selection, per-drive
reporting, summaries, and an end-to-end run on fixture sessions."""

from __future__ import annotations

import json
import os
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

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
from src.decode.dids import load_repo_did_map
from src.models.evaluation import SessionSpan, group_into_drives  # noqa: E402
from src.storage.parquet_writer import write_records_to_parquet  # noqa: E402
from src.storage.sessions import recorded_sessions  # noqa: E402
from src.telemetry import TelemetryRecord  # noqa: E402
from tests.builders import IFConfig
from tests.configs import example_config

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
        "--derived-dir",
        str(tmp_path / "derived"),
        "--workers",
        "1",
    ]


def test_main_scores_only_unseen_drives_and_names_every_skip(tmp_path: Path) -> None:
    from src.features.feature_pipeline import featurize_wide_frame, load_resampled_session
    from src.models.isolation_forest import default_manifest_path, fit_isolation_forest

    config = example_config(EXAMPLE_YAML)
    root = tmp_path / "sessions"
    seen = _write_session(root, "2026-05-12T000000", start=1_778_544_000.0, seed=1)
    _write_session(root, "2026-05-13T000000", start=1_778_630_400.0, seed=2)
    features = featurize_wide_frame(
        load_resampled_session(seen, config),
        config,
        behavior_models=None,
        did_map=load_repo_did_map(),
    ).features
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
    # A06.6: the cadence the sequence detector would be graded at.
    assert report["sequence_forward_every_n_ticks"] == 1
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
    # The session frame went into the derived store (P01.2), not a pickle cache.
    assert list((tmp_path / "derived").glob("*/cells-*.parquet"))
    assert not list((tmp_path / "cache").glob("*-resampled.pkl"))

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
    from src.models.evaluation_detectors import detector_roles

    config = example_config(EXAMPLE_YAML)
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
    from src.features.feature_pipeline import featurize_wide_frame, load_resampled_session
    from src.models.isolation_forest import default_manifest_path, fit_isolation_forest

    config = example_config(EXAMPLE_YAML)
    training = _write_session(tmp_path / "train", "2026-05-12T000000", start=1_778_544_000.0)
    root = tmp_path / "sessions"
    _write_session(root, "2026-05-13T000000", start=1_778_630_400.0, seed=2)
    trained = fit_isolation_forest(
        featurize_wide_frame(
            load_resampled_session(training, config),
            config,
            behavior_models=None,
            did_map=load_repo_did_map(),
        ).features,
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
    from src.features import feature_pipeline

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


# ---------------------------------------------------------------------------
# P02.5: parallel scoring and the score cache leave the report unchanged
# ---------------------------------------------------------------------------


def _report_body(path: Path) -> dict[str, object]:
    report = json.loads(path.read_text())
    return {k: v for k, v in report.items() if k not in ("generated_at", "command")}


def _fit_if(session: Path, artifact: Path, *, seed: int) -> None:
    from src.features.feature_pipeline import featurize_wide_frame, load_resampled_session
    from src.models.isolation_forest import default_manifest_path, fit_isolation_forest

    config = example_config(EXAMPLE_YAML)
    trained = fit_isolation_forest(
        featurize_wide_frame(
            load_resampled_session(session, config),
            config,
            behavior_models=None,
            did_map=load_repo_did_map(),
        ).features,
        IFConfig(n_estimators=40, random_state=seed),
        feature_columns=["obd:RPM", "obd:SPEED"],
        fitted_at="2026-05-12T00:00:00Z",
    )
    trained.save(artifact, default_manifest_path(artifact))


def _entries(cache: Path, component: str) -> set[Path]:
    return set(cache.glob(f"scores/{component}-*/*/*.pkl"))


def _record_scoring_pids(monkeypatch: pytest.MonkeyPatch, harness: Any) -> list[int]:
    """Wrap ``score_drives``; the returned list fills with the pid that scored
    each drive (a pool worker's, or this process's)."""
    pids: list[int] = []
    real = harness.score_drives

    def recording(*args: Any, **kwargs: Any) -> Any:
        results = real(*args, **kwargs)
        pids.extend(result.pid for result in results)
        return results

    monkeypatch.setattr(harness, "score_drives", recording)
    return pids


def test_workers_and_the_score_cache_leave_the_report_unchanged(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """The A02.11 acceptance on fixture drives: ``--workers 1`` and
    ``--workers 2`` write the same report, an unchanged re-run is read back
    from the cache without featurizing anything, and a new IF artifact is
    rescored for the IF alone (the plausibility scores are reused)."""
    import scripts.evaluate_detectors as harness
    from tests.pinned_checkout_harness import CHECKOUT_KEY_INPUTS, pin_in_process

    # T01.23: the score cache keys on the working tree (src/, data/, the
    # config), which other sessions edit during this test. An edit made the
    # re-run rekey (it featurized) or the pool workers refuse (scored here):
    # correct cache behaviour, wrong verdict. The checkout inputs are held at
    # the first run's values, in this process and in the spawned workers; the
    # artifact and frames are still keyed live, so the new IF still rescores.
    pins = tmp_path / "key-pins.json"
    pin_in_process(monkeypatch, pins)

    training = _write_session(tmp_path / "train", "2026-05-12T000000", start=1_778_544_000.0)
    root = tmp_path / "sessions"
    _write_session(root, "2026-05-13T000000", start=1_778_630_400.0, seed=2)
    _write_session(root, "2026-05-14T000000", start=1_778_716_800.0, seed=3)
    artifact = tmp_path / "models" / "model.joblib"
    _fit_if(training, artifact, seed=0)
    base = _main_args(root, artifact, tmp_path)
    base[base.index("robust_z")] = "plausibility"
    # Size the pool by a tiny per-worker peak, so two workers always fit.
    monkeypatch.setattr(harness, "SCORING_WORKER_PEAK_BYTES", 1)

    serial = tmp_path / "serial.json"
    assert main([*base, "--no-score-cache", "--out", str(serial)]) == 0
    assert "(--no-score-cache)" in capsys.readouterr().out
    assert not (tmp_path / "cache" / "scores").exists()
    parallel = tmp_path / "parallel.json"
    workers = base.index("--workers") + 1
    argv = [*base[:workers], "2", *base[workers + 1 :]]
    # The pool really ran: every drive's result came from another process.
    scored_by = _record_scoring_pids(monkeypatch, harness)
    assert main([*argv, "--out", str(parallel)]) == 0
    assert len(scored_by) == 2 and os.getpid() not in scored_by
    assert len(json.loads(parallel.read_text())["drives"]) == 2
    assert _report_body(parallel) == _report_body(serial)
    assert not list((tmp_path / "cache" / "scores" / ".staging").glob("*"))

    # Unchanged re-run: every score read back, no row featurized.
    def refuse(*_a: object, **_k: object) -> object:
        raise AssertionError("an unchanged re-run featurized rows")

    capsys.readouterr()
    with monkeypatch.context() as patched:
        patched.setattr(harness, "featurize_wide_frame", refuse)
        rerun = tmp_path / "rerun.json"
        assert main([*base, "--out", str(rerun)]) == 0
    line = next(x for x in capsys.readouterr().out.splitlines() if x.startswith("score cache:"))
    hits, _, lookups = line.removeprefix("score cache: ").split(" lookups")[0].partition(" of ")
    assert int(hits) == int(lookups) > 0
    assert _report_body(rerun) == _report_body(serial)

    # A new IF artifact: its own scores are new, plausibility's are reused.
    cache = tmp_path / "cache"
    before_if = _entries(cache, "isolation_forest")
    before_plausibility = _entries(cache, "plausibility")
    _fit_if(training, artifact, seed=1)
    changed = tmp_path / "changed.json"
    assert main([*base, "--out", str(changed)]) == 0
    new_if = _entries(cache, "isolation_forest") - before_if
    assert new_if and not {p.parent.parent for p in new_if} & {p.parent.parent for p in before_if}
    assert _entries(cache, "plausibility") == before_plausibility
    report = json.loads(changed.read_text())
    assert (
        report["artifacts"]["isolation_forest"]["sha256"]
        != json.loads(serial.read_text())["artifacts"]["isolation_forest"]["sha256"]
    )
    # Every run above went through the pins.
    assert set(json.loads(pins.read_text())) == set(CHECKOUT_KEY_INPUTS)


def test_score_cache_keys_follow_content_and_entries_round_trip_exactly(
    tmp_path: Path,
) -> None:
    from src.models.evaluation_cache import ScoreCache, code_version, frame_key
    from src.models.evaluation_detectors import DetectorOutput

    frame = pd.DataFrame({"a": [1.0, np.nan, 3.0], "b": ["x", None, "z"]})
    assert frame_key(frame) == frame_key(frame.copy())
    moved = frame.copy()
    moved.loc[2, "a"] = 3.0000001
    assert frame_key(moved) != frame_key(frame)
    assert frame_key(frame.astype({"a": "float32"})) != frame_key(frame)

    extra = tmp_path / "harness.py"
    extra.write_text("x = 1\n")
    first = code_version([extra])
    extra.write_text("x = 2\n")
    assert code_version([extra]) != first

    cache = ScoreCache(tmp_path / "scores")
    verdict = np.array([True, np.True_, None, np.False_], dtype=object)
    output = DetectorOutput(verdict, np.array([0.5, np.nan, 1.0, 2.0]))
    assert cache.get("iforest-abc", "unit") is None
    cache.put("iforest-abc", "unit", {"iforest": output})
    back = cache.get("iforest-abc", "unit")["iforest"]
    # The same element types, not merely equal values: the harness tests
    # ``v is True or v is np.True_`` and must see what a fresh score gives.
    assert [type(v) for v in back.verdict] == [type(v) for v in verdict]
    np.testing.assert_array_equal(back.score, output.score)
    assert cache.counts() == {"iforest": {"hits": 1, "misses": 1}}
    assert ScoreCache(None).get("iforest-abc", "unit") is None


def test_the_scoring_pool_is_capped_by_the_measured_worker_peak(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """``--workers`` is a ceiling: memory (the measured per-worker peak, as if
    every worker peaked at once), cores at the pinned thread count, and the
    drive count each cap it; one worker never spawns a pool."""
    import scripts.evaluate_detectors as harness

    peak = harness.SCORING_WORKER_PEAK_BYTES
    monkeypatch.setattr(harness.os, "cpu_count", lambda: 12)
    room = int(3 * peak / harness.SCORING_MEMORY_FRACTION) + 1
    monkeypatch.setattr(harness, "_available_memory_bytes", lambda: room)
    assert harness.scoring_workers(4, 18) == 3
    assert harness.scoring_workers(4, 2) == 2
    assert harness.scoring_workers(4, 18, threads=6) == 2
    assert harness.scoring_workers(1, 18) == 1
    monkeypatch.setattr(harness, "_available_memory_bytes", lambda: peak // 2)
    assert harness.scoring_workers(4, 18) == 1


@pytest.mark.parametrize("workers", ["1", "2"])
def test_an_input_edited_mid_run_leaves_no_entry_under_either_key(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    workers: str,
) -> None:
    """P02.5: the key is hashed before anything loads; a bindings edit between
    then and scoring must not leave a score under the old key or the new one.
    Scoring workers re-hash before they load and refuse (the parent finishes
    in-process); the parent re-hashes after scoring and commits nothing."""
    import scripts.evaluate_detectors as harness

    training = _write_session(tmp_path / "train", "2026-05-12T000000", start=1_778_544_000.0)
    root = tmp_path / "sessions"
    _write_session(root, "2026-05-13T000000", start=1_778_630_400.0, seed=2)
    _write_session(root, "2026-05-14T000000", start=1_778_716_800.0, seed=3)
    artifact = tmp_path / "models" / "model.joblib"
    _fit_if(training, artifact, seed=0)
    bindings = tmp_path / "bindings.yaml"
    bindings.write_bytes((EXAMPLE_YAML.parent / "data" / "bindings.yaml").read_bytes())
    base = _main_args(root, artifact, tmp_path)
    at = base.index("--workers") + 1
    argv = [*base[:at], workers, *base[at + 1 :], "--bindings", str(bindings)]
    monkeypatch.setattr(harness, "SCORING_WORKER_PEAK_BYTES", 1)

    real = harness.score_drives

    def edit_then_score(*args: Any, **kwargs: Any) -> Any:
        bindings.write_bytes(bindings.read_bytes() + b"\n# edited mid-run\n")
        results = real(*args, **kwargs)
        # A worker that loaded the edited bindings refused; the parent scored.
        assert all(result.pid == os.getpid() for result in results)
        return results

    monkeypatch.setattr(harness, "score_drives", edit_then_score)
    assert main([*argv, "--out", str(tmp_path / "r.json")]) == 0
    assert "key inputs changed during the run: bindings" in capsys.readouterr().err
    scores = tmp_path / "cache" / "scores"
    assert not list(scores.rglob("*.pkl"))
    assert json.loads((tmp_path / "r.json").read_text())["drives"]


def test_the_score_cache_stages_until_commit_and_prunes_least_recently_used(
    tmp_path: Path,
) -> None:
    import time

    from src.models.evaluation_cache import STAGING_DIR, STALE_STAGING_SECONDS, ScoreCache

    root = tmp_path / "scores"
    cache = ScoreCache(root)
    cache.put("a-1", "u1", [1])
    assert not list((root / "a-1").rglob("*.pkl"))  # staged, not in the store
    assert cache.get("a-1", "u1") == [1]  # this run reads its own entries
    cache.discard()
    assert ScoreCache(root).get("a-1", "u1") is None
    cache = ScoreCache(root)
    cache.put("a-1", "u1", [1])
    assert cache.commit() == 1
    assert ScoreCache(root).get("a-1", "u1") == [1]
    assert not (root / STAGING_DIR / cache.run).exists()

    # Twelve ~1 kB entries in a fresh store, oldest first.
    root = tmp_path / "lru"
    now = time.time()
    filler = ScoreCache(root)
    for i in range(12):
        filler.put("b-1", f"u{i:02d}", b"x" * 1000)
    filler.commit()
    paths = {i: next(root.rglob(f"u{i:02d}.pkl")) for i in range(12)}
    for i, path in paths.items():
        os.utime(path, (now - 1000 + i, now - 1000 + i))
    size = paths[0].stat().st_size
    # A hit refreshes an entry's mtime: the oldest entry, once read, is recent.
    assert ScoreCache(root).get("b-1", "u00") is not None
    stale = root / STAGING_DIR / "dead-run"
    stale.mkdir(parents=True)
    os.utime(stale, (now - STALE_STAGING_SECONDS - 1,) * 2)

    kept_since = now - 1000 + 10  # u10, u11 and the u00 just read are this run's
    deleted = ScoreCache(root).prune(max_bytes=4 * size, keep_since=kept_since)
    left = sorted(p.stem for p in root.rglob("*.pkl"))
    # Least recently used went first until the store fit; this run's stayed.
    assert left == ["u00", "u09", "u10", "u11"]
    assert deleted == 8 and not stale.exists()
    # The current run's entries survive even when they alone exceed the cap.
    ScoreCache(root).prune(max_bytes=0, keep_since=now - 1000 + 9)
    assert sorted(p.stem for p in root.rglob("*.pkl")) == left


def test_the_scoring_pool_respects_the_cgroup_and_stays_in_process_on_the_pi(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import scripts.evaluate_detectors as harness

    group = tmp_path / "cgroup" / "system.slice" / "canary.service"
    group.mkdir(parents=True)
    self_cgroup = tmp_path / "self_cgroup"
    self_cgroup.write_text("0::/system.slice/canary.service\n")
    (group / "memory.max").write_text(f"{6 * 1024**3}\n")
    (group / "memory.current").write_text(f"{1024**3}\n")
    headroom = harness._cgroup_memory_headroom(
        cgroup_root=tmp_path / "cgroup", self_cgroup=self_cgroup
    )
    assert headroom == 5 * 1024**3
    (group / "memory.max").write_text("max\n")
    assert (
        harness._cgroup_memory_headroom(cgroup_root=tmp_path / "cgroup", self_cgroup=self_cgroup)
        is None
    )

    peak = harness.SCORING_WORKER_PEAK_BYTES
    monkeypatch.setattr(harness.os, "cpu_count", lambda: 12)
    monkeypatch.setattr(harness, "on_the_pi", lambda: False)
    monkeypatch.setattr(harness, "_available_memory_bytes", lambda: 100 * peak)
    # The cgroup's headroom caps the pool below what MemAvailable would allow.
    room = int(2 * peak / harness.SCORING_MEMORY_FRACTION) + 1
    monkeypatch.setattr(harness, "_cgroup_memory_headroom", lambda: room)
    assert harness.scoring_workers(4, 18) == 2
    monkeypatch.setattr(harness, "_cgroup_memory_headroom", lambda: None)
    assert harness.scoring_workers(4, 18) == 4
    # On the Pi: in-process, whatever memory says, and no refusal.
    monkeypatch.setattr(harness, "on_the_pi", lambda: True)
    assert harness.scoring_workers(4, 18) == 1


def test_zero_drives_because_every_load_failed_is_refused_and_named(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """An empty result says which kind of empty it is (P02.5): a harness run
    on the Pi with a derived store (which refuses to build there) loads no
    session. It exits non-zero naming each failure, never 0 with no drives."""
    import src.preprocessing.derived_store as derived_store

    training = _write_session(tmp_path / "train", "2026-05-12T000000", start=1_778_544_000.0)
    root = tmp_path / "sessions"
    _write_session(root, "2026-05-13T000000", start=1_778_630_400.0, seed=2)
    artifact = tmp_path / "models" / "model.joblib"
    _fit_if(training, artifact, seed=0)
    out = tmp_path / "r.json"
    argv = [*_main_args(root, artifact, tmp_path), "--no-score-cache", "--out", str(out)]
    monkeypatch.setattr(derived_store, "on_the_pi", lambda: True)
    assert main(argv) == 2
    err = capsys.readouterr().err
    assert "no evaluation drive loaded; 1 session load(s) failed" in err
    assert "2026-05-13T000000: " in err and "never built on the Pi" in err
    assert not out.exists()
    # The same drive reduced in memory (what retrain now passes on the Pi).
    at = argv.index("--derived-dir")
    del argv[at : at + 2]
    assert main([*argv, "--no-derived-store"]) == 0
    assert len(json.loads(out.read_text())["drives"]) == 1


def test_the_cgroup_headroom_is_the_tightest_level_up_to_the_root(tmp_path: Path) -> None:
    import scripts.evaluate_detectors as harness

    root = tmp_path / "cgroup"
    leaf = root / "system.slice" / "canary.service"
    leaf.mkdir(parents=True)
    self_cgroup = tmp_path / "self_cgroup"
    self_cgroup.write_text("0::/system.slice/canary.service\n")
    (leaf / "memory.max").write_text("max\n")
    (leaf / "memory.current").write_text("100\n")
    (root / "system.slice" / "memory.max").write_text("1000\n")
    (root / "system.slice" / "memory.current").write_text("400\n")
    # The leaf is unlimited; its parent's ceiling binds it.
    assert harness._cgroup_memory_headroom(cgroup_root=root, self_cgroup=self_cgroup) == 600
    (leaf / "memory.max").write_text("300\n")
    assert harness._cgroup_memory_headroom(cgroup_root=root, self_cgroup=self_cgroup) == 200


def test_the_key_hashes_the_afr_calibration_featurization_reads(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The key hashes the ``afr_calibration`` featurization reads: the
    profile-scoped one (P02.11), never the absent unscoped ``data/`` path."""
    import scripts.evaluate_detectors as harness

    seen: list[Any] = []
    real = harness.key_inputs

    def recording(sources: Any) -> Any:
        seen.append(sources)
        return real(sources)

    monkeypatch.setattr(harness, "key_inputs", recording)
    root = tmp_path / "sessions"
    _write_session(root, "2026-05-13T000000", start=1_778_630_400.0, seed=2)
    argv = _main_args(root, tmp_path / "absent.joblib", tmp_path)
    assert main([*argv, "--plan", "--out", str(tmp_path / "r.json")]) == 0
    from src.config import CanaryConfig
    from src.vehicle.profiles import resolve_profile_for_tooling, scope_config

    base = CanaryConfig.from_yaml(EXAMPLE_YAML)
    afr = scope_config(base, resolve_profile_for_tooling(base, None)).daemon.afr_calibration
    assert afr.is_file(), "the example vehicle ships a calibration"
    assert afr.resolve() in seen[0].extra_data
    assert base.daemon.afr_calibration.resolve() not in seen[0].extra_data
