"""A06.5 — at most 1 alert per engine-hour on held-out drives, as a refusal.

The budget sat in reports while the sequence detector alerted ~117 times per
engine-hour on 2026-09-10; nothing refused the artifact. These tests pin the
publish-time refusal: only alert-role detectors count, the *upper bound* must
be under the budget, and an alert-role detector nobody measured is refused,
not waved through.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pandas as pd
import pytest

from scripts import _cli, evaluate_detectors, retrain
from src.models import training_entry
from src.models.alert_budget import (
    ALERT_BUDGET_INCIDENT,
    check_alert_budget,
    file_sha256,
    poisson_upper_rate,
)
from src.models.retrain_exit_codes import ALERT_BUDGET_REFUSED
from src.models.role_gate import resolve_role

REPO_ROOT = Path(__file__).resolve().parents[1]
EXAMPLE_YAML = REPO_ROOT / "canary.example.yaml"

ALERT = {"isolation_forest": "alert", "lstm_autoencoder": "evidence"}
EVIDENCE = {"isolation_forest": "evidence", "lstm_autoencoder": "evidence"}


def test_the_poisson_bound_reproduces_the_garwood_table() -> None:
    """Known answers (Garwood 1936; chi-square 97.5 % / 2): 0 events -> 3.689,
    1 -> 5.572, 5 -> 11.668."""
    assert poisson_upper_rate(0, 1.0) == pytest.approx(3.6889, abs=1e-4)
    assert poisson_upper_rate(1, 1.0) == pytest.approx(5.5716, abs=1e-4)
    assert poisson_upper_rate(5, 2.0) == pytest.approx(11.6683 / 2.0, abs=1e-4)


def _drive(
    name: str, hours: float, episodes: dict[str, int], *, cross_era: bool = False
) -> dict[str, Any]:
    return {
        "drive": name,
        "cross_era": cross_era,
        "engine_hours": hours,
        "detectors": {
            detector: {"false_alarm_episodes": count, "rows": 100, "withheld_rows": 0}
            for detector, count in episodes.items()
        },
    }


def _report(
    drives: list[dict[str, Any]],
    *,
    bootstrap_high: float | None = None,
    roles: dict[str, str] | None = None,
    detectors: tuple[str, ...] = ("isolation_forest",),
    artifacts: dict[str, str] | None = None,
) -> dict[str, Any]:
    """The fields of ``scripts/evaluate_detectors.py``'s report the gate reads."""
    return {
        "detectors": list(detectors),
        "roles": roles or dict.fromkeys(detectors, "alert"),
        "drives": drives,
        "false_alarms": {
            name: {"false_alarms_per_engine_hour": {"high": bootstrap_high}} for name in detectors
        },
        "artifacts": {label: {"sha256": digest} for label, digest in (artifacts or {}).items()},
    }


def test_evidence_role_detectors_are_never_held_to_the_budget() -> None:
    """Today's configuration: both detectors evidence, so publishing needs no
    measurement at all — the check only guards a future flip to alert."""
    check = check_alert_budget(
        None, roles_by_source=EVIDENCE, published_sources=["isolation_forest", "lstm_autoencoder"]
    )
    assert not check.required
    assert not check.refused
    assert check.violations == ()


def test_one_alarm_in_one_engine_hour_is_refused_on_its_upper_bound() -> None:
    """An estimate of 1.0/h meets the budget on paper; its bound (5.6/h over
    one drive) does not, and one drive gives the bootstrap no interval."""
    report = _report([_drive("d1", 1.0, {"isolation_forest": 1})])

    check = check_alert_budget(
        report, roles_by_source=ALERT, published_sources=["isolation_forest"]
    )

    assert check.refused
    (detector,) = check.detectors
    assert detector.estimate == pytest.approx(1.0)
    assert detector.upper == pytest.approx(5.5716, abs=1e-4)
    assert "upper bound 5.57/h over 1/h" in check.violations[0]
    assert ALERT_BUDGET_INCIDENT in check.message()
    assert "2026-09-10" in check.message()


def test_zero_alarms_over_little_driving_is_refused_and_says_how_much_is_needed() -> None:
    report = _report([_drive("d1", 1.06, {"isolation_forest": 0})], bootstrap_high=0.0)

    check = check_alert_budget(
        report, roles_by_source=ALERT, published_sources=["isolation_forest"]
    )

    assert check.refused
    assert "zero alarms need 3.7 engine-h" in check.violations[0]


def test_a_quiet_detector_over_enough_held_out_driving_passes() -> None:
    drives = [_drive(f"d{i}", 1.0, {"isolation_forest": 0}) for i in range(5)]
    report = _report(drives, bootstrap_high=0.0)

    check = check_alert_budget(
        report, roles_by_source=ALERT, published_sources=["isolation_forest"]
    )

    assert not check.refused
    (detector,) = check.detectors
    assert detector.passed
    assert detector.upper == pytest.approx(3.6889 / 5.0, abs=1e-4)


def test_between_drive_spread_counts_when_it_is_the_larger_bound() -> None:
    """2 alarms over 20 engine-hours bounds at 0.36/h by count, but the drive
    bootstrap's 1.5/h (one bad drive) decides it."""
    drives = [_drive("bad", 1.0, {"isolation_forest": 2})] + [
        _drive(f"d{i}", 1.0, {"isolation_forest": 0}) for i in range(19)
    ]
    report = _report(drives, bootstrap_high=1.5)

    check = check_alert_budget(
        report, roles_by_source=ALERT, published_sources=["isolation_forest"]
    )

    assert check.refused
    assert check.detectors[0].upper == 1.5


def test_cross_era_drives_are_not_counted() -> None:
    drives = [_drive(f"d{i}", 1.0, {"isolation_forest": 0}) for i in range(5)] + [
        _drive("pre-flash", 3.0, {"isolation_forest": 40}, cross_era=True)
    ]
    check = check_alert_budget(
        _report(drives, bootstrap_high=0.0),
        roles_by_source=ALERT,
        published_sources=["isolation_forest"],
    )
    assert not check.refused
    assert check.detectors[0].episodes == 0


def test_an_unmeasured_alert_detector_is_refused_not_waved_through() -> None:
    check = check_alert_budget(
        None,
        roles_by_source=ALERT,
        published_sources=["isolation_forest"],
        missing_report_reason="no evaluation holdout",
    )
    assert check.refused
    assert check.violations == ("isolation_forest: not measured: no evaluation holdout",)

    no_time = check_alert_budget(
        _report([_drive("d1", 0.0, {"isolation_forest": 0})]),
        roles_by_source=ALERT,
        published_sources=["isolation_forest"],
    )
    assert no_time.refused
    assert "no held-out engine time scored" in no_time.violations[0]


def test_an_evaluation_that_counted_the_detector_as_evidence_is_refused() -> None:
    """The harness drops an evidence detector's alarms from its summary; a
    report run under the old roles says nothing about the alert role."""
    drives = [_drive(f"d{i}", 1.0, {"isolation_forest": 0}) for i in range(5)]
    report = _report(drives, bootstrap_high=0.0, roles={"isolation_forest": "evidence"})

    check = check_alert_budget(
        report, roles_by_source=ALERT, published_sources=["isolation_forest"]
    )

    assert check.refused
    assert "counted it as 'evidence'" in check.violations[0]


def test_a_report_on_other_bytes_is_refused() -> None:
    drives = [_drive(f"d{i}", 1.0, {"isolation_forest": 0}) for i in range(5)]
    report = _report(drives, bootstrap_high=0.0, artifacts={"isolation_forest": "a" * 64})

    check = check_alert_budget(
        report,
        roles_by_source=ALERT,
        published_sources=["isolation_forest"],
        expected_artifacts={"isolation_forest": "b" * 64},
    )

    assert check.refused
    assert "scored other bytes" in check.violations[0]


def test_the_fused_verdict_is_held_to_the_budget_with_an_alert_source() -> None:
    drives = [_drive(f"d{i}", 1.0, {"isolation_forest": 0, "fused": 3}) for i in range(5)]
    report = _report(drives, bootstrap_high=0.0, detectors=("isolation_forest", "fused"))

    check = check_alert_budget(
        report, roles_by_source=ALERT, published_sources=["isolation_forest"]
    )

    assert check.refused
    assert [d.detector for d in check.detectors] == ["isolation_forest", "fused"]
    assert check.violations[0].startswith("fused: ")


def test_an_override_needs_a_reason_and_keeps_the_violation_on_record() -> None:
    report = _report([_drive("d1", 1.0, {"isolation_forest": 1})])
    with pytest.raises(ValueError, match="stated reason"):
        check_alert_budget(
            report,
            roles_by_source=ALERT,
            published_sources=["isolation_forest"],
            override_reason="  ",
        )

    check = check_alert_budget(
        report,
        roles_by_source=ALERT,
        published_sources=["isolation_forest"],
        override_reason="bench fault-injection drive, operator 2026-09-23",
    )
    assert not check.refused
    assert check.violations
    assert check.to_dict()["override_reason"] == "bench fault-injection drive, operator 2026-09-23"
    stripped = check_alert_budget(
        report,
        roles_by_source=ALERT,
        published_sources=["isolation_forest"],
        override_reason="  bench drive\n",
    )
    assert stripped.override_reason == "bench drive"


# --- scripts/retrain.py: the refusal where artifacts are published ----------


def _write_span(session: Path, start: str) -> None:
    session.mkdir(parents=True)
    stamps = pd.date_range(start, periods=2, freq=pd.Timedelta(minutes=30), tz="UTC")
    pd.DataFrame({"timestamp": stamps, "value_number": [0.0, 1.0]}).to_parquet(
        session / "telemetry-00000.parquet"
    )


def _alert_config(tmp_path: Path, *, isolation_forest: str = "alert") -> Path:
    text = EXAMPLE_YAML.read_text()
    old = "  roles:\n    isolation_forest: evidence\n    sequence: evidence\n"
    assert old in text
    path = tmp_path / "canary.yaml"
    path.write_text(
        text.replace(
            old, f"  roles:\n    isolation_forest: {isolation_forest}\n    sequence: evidence\n"
        )
    )
    return path


def _setup(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> dict[str, Any]:
    root = tmp_path / "sessions"
    names = ["2026-09-20T120000", "2026-09-21T120000", "2026-09-22T120000", "2026-09-23T120000"]
    for day, name in enumerate(names):
        _write_span(root / name, f"2026-09-{20 + day}T12:00:00")
    sessions = [root / name for name in names]
    monkeypatch.setattr(_cli, "discover_sessions", lambda _root: list(sessions))
    monkeypatch.setattr(retrain, "build_feature_frame", lambda *a, **k: object())
    monkeypatch.setattr(retrain, "_run_benchmark_gate", lambda *a, **k: (None, None))
    trained_into: list[Path] = []

    def _if_train(_sessions: object, _config: object, out_dir: Path, **_k: object) -> None:
        out_dir.mkdir(parents=True, exist_ok=True)
        (out_dir / "model.joblib").write_bytes(b"new-if")
        (out_dir / "model.manifest.json").write_text("{}")
        trained_into.append(out_dir)

    monkeypatch.setattr(training_entry, "train_isolation_forest", _if_train)
    monkeypatch.setattr(training_entry, "train_sequence_detector", lambda *a, **k: None)
    return {"root": root, "sessions": sessions, "trained_into": trained_into}


def _argv(tmp_path: Path, config: Path, setup: dict[str, Any], *extra: str) -> list[str]:
    return [
        "--config",
        str(config),
        "--sessions-root",
        str(setup["root"]),
        "--all",
        "--no-feedback-policy",
        "--no-ledger",
        "--skip-disposition-summary",
        "--skip-quarantine-exclusion",
        "--calibration-sessions",
        str(setup["sessions"][2]),
        "--evaluation-sessions",
        str(setup["sessions"][3]),
        "--if-out",
        str(tmp_path / "models" / "isolation_forest"),
        "--sequence-out",
        str(tmp_path / "models" / "lstm_autoencoder"),
        "--analyses-dir",
        str(tmp_path / "analyses"),
        *extra,
    ]


def _fake_harness(report: dict[str, Any], calls: list[list[str]]) -> Any:
    def _main(argv: list[str]) -> int:
        calls.append(argv)
        args = evaluate_detectors.parse_args(argv)  # the gate's argv is valid harness input
        digest = retrain._file_sha256(args.if_model)
        full = {**report, "artifacts": {"isolation_forest": {"sha256": digest}}}
        args.out.write_text(json.dumps(full))
        return 0

    return _main


def test_retrain_with_evidence_roles_publishes_without_measuring(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    setup = _setup(tmp_path, monkeypatch)
    calls: list[list[str]] = []
    monkeypatch.setattr(retrain, "run_evaluate_detectors", _fake_harness({}, calls))

    rc = retrain.main(_argv(tmp_path, EXAMPLE_YAML, setup))

    assert rc == 0
    assert calls == []
    assert setup["trained_into"] == [tmp_path / "models" / "isolation_forest"]
    assert (tmp_path / "models" / "isolation_forest" / "model.joblib").read_bytes() == b"new-if"
    assert not (tmp_path / "analyses").exists() or not list(
        (tmp_path / "analyses").glob("alert-budget-*")
    )


def test_retrain_refuses_an_alert_role_artifact_over_the_budget(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """The artifact trains into a staging dir the daemon never reads, fails
    the gate, and is set aside; the served directory keeps what it had."""
    setup = _setup(tmp_path, monkeypatch)
    served = tmp_path / "models" / "isolation_forest"
    served.mkdir(parents=True)
    (served / "model.joblib").write_bytes(b"old-if")
    calls: list[list[str]] = []
    flooding = _report([_drive("d4", 1.0, {"isolation_forest": 12})])
    monkeypatch.setattr(retrain, "run_evaluate_detectors", _fake_harness(flooding, calls))

    rc = retrain.main(_argv(tmp_path, _alert_config(tmp_path), setup))

    assert rc == ALERT_BUDGET_REFUSED
    assert setup["trained_into"] == [tmp_path / "models" / "_staging-isolation_forest"]
    assert (served / "model.joblib").read_bytes() == b"old-if"
    (refused,) = (tmp_path / "models").glob("_refused-alert-budget-*-isolation_forest")
    assert (refused / "model.joblib").read_bytes() == b"new-if"
    # The refused artifact keeps its failed verdict; the served one gets none.
    kept = json.loads((refused / "model.joblib.alert-budget.json").read_text())
    assert (kept["accepted"], kept["sha256"]) == (False, file_sha256(refused / "model.joblib"))
    assert not (served / "model.joblib.alert-budget.json").exists()
    assert not (tmp_path / "models" / "_staging-isolation_forest").exists()
    # Scored on the evaluation drive only.
    assert str(setup["sessions"][3]) in calls[0]
    assert str(setup["sessions"][2]) not in calls[0]
    (verdict,) = (tmp_path / "analyses").glob("alert-budget-*-verdict.json")
    record = json.loads(verdict.read_text())
    assert record["refused"] is True
    assert record["incident"] == ALERT_BUDGET_INCIDENT
    err = capsys.readouterr().err
    assert "2026-09-10" in err
    assert "--accept-alert-budget" in err


def test_retrain_publishes_an_alert_role_artifact_within_the_budget(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    setup = _setup(tmp_path, monkeypatch)
    calls: list[list[str]] = []
    quiet = _report(
        [_drive(f"d{i}", 1.0, {"isolation_forest": 0}) for i in range(5)], bootstrap_high=0.0
    )
    monkeypatch.setattr(retrain, "run_evaluate_detectors", _fake_harness(quiet, calls))

    rc = retrain.main(_argv(tmp_path, _alert_config(tmp_path), setup))

    assert rc == 0
    served = tmp_path / "models" / "isolation_forest"
    assert (served / "model.joblib").read_bytes() == b"new-if"
    assert (served / "model.manifest.json").exists()
    assert not (tmp_path / "models" / "_staging-isolation_forest").exists()
    # The verdict travels with the binary, and the daemon honours it.
    verdict = json.loads((served / "model.joblib.alert-budget.json").read_text())
    assert verdict["sha256"] == file_sha256(served / "model.joblib")
    assert verdict["accepted"] is True and verdict["passed"] is True
    assert len(verdict["evaluation_drives"]) == 5
    resolution = resolve_role("isolation_forest", "alert", artifact_path=served / "model.joblib")
    assert resolution.effective == "alert"


def test_retrain_publishes_over_the_budget_only_with_a_stated_reason(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    setup = _setup(tmp_path, monkeypatch)
    config = _alert_config(tmp_path)
    with pytest.raises(SystemExit):
        retrain.parse_args(_argv(tmp_path, config, setup, "--accept-alert-budget", "   "))

    calls: list[list[str]] = []
    flooding = _report([_drive("d4", 1.0, {"isolation_forest": 12})])
    monkeypatch.setattr(retrain, "run_evaluate_detectors", _fake_harness(flooding, calls))
    reason = "fault-injection bench drive, operator 2026-09-23"

    rc = retrain.main(_argv(tmp_path, config, setup, "--accept-alert-budget", reason))

    assert rc == 0
    assert (tmp_path / "models" / "isolation_forest" / "model.joblib").read_bytes() == b"new-if"
    (verdict,) = (tmp_path / "analyses").glob("alert-budget-*-verdict.json")
    record = json.loads(verdict.read_text())
    assert (record["refused"], record["override_reason"]) == (False, reason)
    assert record["violations"]


def test_retrain_refuses_an_alert_role_artifact_with_no_evaluation_drive(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    setup = _setup(tmp_path, monkeypatch)
    calls: list[list[str]] = []
    monkeypatch.setattr(retrain, "run_evaluate_detectors", _fake_harness({}, calls))
    # Nothing spared for evaluation (the auto-pick would otherwise take one).
    monkeypatch.setattr(retrain, "pick_evaluation_holdouts", lambda *a, **k: [])
    argv = [
        a for a in _argv(tmp_path, _alert_config(tmp_path), setup) if a != "--evaluation-sessions"
    ]
    argv.remove(str(setup["sessions"][3]))

    rc = retrain.main(argv)

    assert rc == ALERT_BUDGET_REFUSED
    assert calls == []
    assert not (tmp_path / "models" / "isolation_forest" / "model.joblib").exists()
