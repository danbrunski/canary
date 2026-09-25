"""Tests for ``scripts/refit_behavior_models.py`` (Task 4.E.8 CLI shell)."""

from __future__ import annotations

import json
from datetime import UTC, date, datetime
from pathlib import Path

import numpy as np
import pandas as pd
import pytest

import scripts.refit_behavior_models as refit_cli

# Importable as a module so we can call main() and patch its dependencies.
from scripts import _cli


def _frame(session_name: str = "2026-05-13T120000", seed: int = 0) -> pd.DataFrame:
    """A minimal feature frame that satisfies the coolant_warmup recipe.

    Carries ``operating_mode=warmup`` + the recipe's required columns so
    the now-mode-gated ``coolant_warmup`` actually finds matching rows.
    Includes ``session`` so the event-gating filter (added Phase C) can
    intersect with classifier-loaded session_events.
    """
    rng = np.random.default_rng(seed)
    n = 400
    t = np.linspace(0, 600, n)
    return pd.DataFrame(
        {
            "session": [session_name] * n,
            "obd:RUN_TIME": t,
            "obd:RPM": np.clip(750 + rng.normal(0, 30, n), 600, 6000),
            "obd:COOLANT_TEMP": 25 + 60 * (1 - np.exp(-t / 120)) + rng.normal(0, 0.5, n),
            "obd:ENGINE_LOAD": np.clip(20 + rng.normal(0, 5, n), 0, 100),
            "obd:TIMING_ADVANCE": 8 + 0.005 * 750 + rng.normal(0, 0.5, n),
            "obd:SHORT_FUEL_TRIM_1": rng.normal(0, 2, n),
            "operating_mode": ["warmup"] * n,
        }
    )


# ---------------------------------------------------------------------------
# _cli.filter_sessions_by_age (moved here from a private per-script copy;
# retrain.py and refit_behavior_models.py share this one now — refactoring.md
# Phase 2 task 2.4)
# ---------------------------------------------------------------------------


def test_filter_keeps_recent_sessions_drops_old_ones(tmp_path: Path) -> None:
    now = datetime(2026, 5, 13, tzinfo=UTC)
    recent = tmp_path / "2026-05-10T120000"
    old = tmp_path / "2026-03-01T000000"
    odd = tmp_path / "manual-import"  # non-conforming name — kept
    for p in (recent, old, odd):
        p.mkdir()
    kept = _cli.filter_sessions_by_age([recent, old, odd], days=14, now=now)
    assert recent in kept
    assert old not in kept
    assert odd in kept


def test_filter_disabled_by_days_zero(tmp_path: Path) -> None:
    sessions = [tmp_path / "2020-01-01T000000", tmp_path / "2026-05-13T000000"]
    for s in sessions:
        s.mkdir()
    kept = _cli.filter_sessions_by_age(sessions, days=0, now=datetime.now(UTC))
    assert kept == sessions


# ---------------------------------------------------------------------------
# _cli.filter_sessions_by_boundary (Task 3.5; one copy for refit and retrain, R06.3)
# ---------------------------------------------------------------------------


def test_filter_sessions_by_boundary_keeps_on_and_after(tmp_path: Path) -> None:
    older = tmp_path / "2026-05-01T000000"
    on_boundary = tmp_path / "2026-06-01T000000"
    newer = tmp_path / "2026-06-15T000000"
    odd = tmp_path / "manual-import"  # non-conforming name — kept
    for p in (older, on_boundary, newer, odd):
        p.mkdir()
    kept = _cli.filter_sessions_by_boundary(
        [older, on_boundary, newer, odd], boundary=date(2026, 6, 1)
    )
    assert older not in kept
    assert on_boundary in kept
    assert newer in kept
    assert odd in kept


# ---------------------------------------------------------------------------
# _select_recipes
# ---------------------------------------------------------------------------


def test_select_recipes_default_returns_full_catalog() -> None:
    catalog = refit_cli.default_recipes()
    assert refit_cli._select_recipes(catalog, None) == catalog


def test_select_recipes_filters_by_name() -> None:
    catalog = refit_cli.default_recipes()
    selected = refit_cli._select_recipes(catalog, ["coolant_warmup", "warm_idle_rpm_baseline"])
    assert [r.name for r in selected] == ["coolant_warmup", "warm_idle_rpm_baseline"]


def test_select_recipes_rejects_unknown_name() -> None:
    catalog = refit_cli.default_recipes()
    with pytest.raises(SystemExit, match="unknown recipe"):
        refit_cli._select_recipes(catalog, ["nope"])


def test_behavior_capture_discovery_requires_an_exact_recipe_allowlist(tmp_path: Path) -> None:
    eligible = tmp_path / "_capture-tuning-did-2026-08-07T000000"
    unrelated = tmp_path / "_capture-other-2026-08-07T010000"
    normal = tmp_path / "2026-08-07T020000"
    for session in (eligible, unrelated, normal):
        session.mkdir()
        (session / "telemetry-00000.parquet").write_bytes(b"evidence")
    for session, recipes in (
        (eligible, ["cam_secondary_vs_command_rpm_did"]),
        (unrelated, ["another_recipe"]),
    ):
        (session / "capture_profile.json").write_text(
            json.dumps(
                {
                    "schema_version": 1,
                    "profile": "research",
                    "started_at": "2026-08-07T00:00:00Z",
                    "profile_snapshot": {"behavior_model_recipes": recipes},
                }
            )
        )

    found = refit_cli._discover_behavior_capture_sessions(
        tmp_path, {"cam_secondary_vs_command_rpm_did"}
    )
    assert found == [eligible]


def test_capture_recipe_allowlists_preserve_exact_per_capture_grants(tmp_path: Path) -> None:
    first = tmp_path / "_capture-first"
    second = tmp_path / "_capture-second"
    normal = tmp_path / "2026-08-07T020000"
    for session in (first, second, normal):
        session.mkdir()
    for session, recipes in ((first, ["recipe-a"]), (second, ["recipe-b", "recipe-c"])):
        (session / "capture_profile.json").write_text(
            json.dumps(
                {
                    "schema_version": 1,
                    "profile": "research",
                    "started_at": "2026-08-07T00:00:00Z",
                    "profile_snapshot": {"behavior_model_recipes": recipes},
                }
            )
        )

    assert refit_cli._capture_recipe_allowlists([first, second, normal]) == {
        "_capture-first": {"recipe-a"},
        "_capture-second": {"recipe-b", "recipe-c"},
    }
    assert refit_cli._capture_recipe_allowlists(
        [first, second, normal], explicit_recipe_grants={"manual-recipe"}
    ) == {
        "_capture-first": {"manual-recipe"},
        "_capture-second": {"manual-recipe"},
    }


def test_tune_revision_gate_accepts_one_revision_and_refuses_mixed(tmp_path: Path) -> None:
    from src.analysis.tune_revisions import TuneRevision, TuneRevisionRegistry

    old = tmp_path / "2026-08-06T000000"
    new = tmp_path / "2026-08-07T000000"
    old.mkdir()
    new.mkdir()
    registry = TuneRevisionRegistry(
        revisions=[
            TuneRevision(revision="rom-a", flashed_at=datetime(2026, 8, 1, tzinfo=UTC)),
            TuneRevision(revision="rom-b", flashed_at=datetime(2026, 8, 7, tzinfo=UTC)),
        ]
    )

    assert refit_cli._consistent_tune_revision([old], registry) == "rom-a"
    with pytest.raises(ValueError, match="mixed tune revisions"):
        refit_cli._consistent_tune_revision([old, new], registry)


# ---------------------------------------------------------------------------
# main() — dry run, smoke, write-out
# ---------------------------------------------------------------------------


def _patch_pipeline(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> Path:
    """Stub out session discovery + the feature pipeline so we don't need real Parquet.

    Also writes a synthetic ``classification.json`` carrying a
    ``cold_start`` event so the post-Phase-C event-gated ``coolant_warmup``
    recipe finds the session eligible.
    """
    # Three independent drives (days apart): B02.3 groups directories within
    # the join gap into one physical drive, so minutes-apart names would be one.
    sessions = [tmp_path / "sessions" / f"2026-05-1{day}T120000" for day in range(3)]
    # Mirror what ``label_sessions.py`` would write — minimal but valid.
    import json

    for session in sessions:
        session.mkdir(parents=True)
        (session / "classification.json").write_text(
            json.dumps(
                {
                    "schema_version": 1,
                    "classifier_version": "0.1.0",
                    # After the mode-label fix (f8b7d38): older sidecars are
                    # excluded from event gating until D03.22 relabels them.
                    "classified_at": "2026-09-21T12:00:00Z",
                    "session_id": session.name,
                    "duration_seconds": 600.0,
                    "summary": {
                        "mode_seconds": {"warmup": 600.0},
                        "event_counts": {"cold_start": 1},
                    },
                    "mode_segments": [],
                    "events": [
                        {
                            "timestamp": "2026-05-13T12:00:00Z",
                            "kind": "cold_start",
                            "fields": {},
                        }
                    ],
                    "quality": {},
                    "disposition": "ready",
                }
            )
        )
    monkeypatch.setattr(_cli, "discover_sessions", lambda _root: sessions)
    monkeypatch.setattr(
        refit_cli,
        "build_feature_frame",
        lambda *_a, **_k: pd.concat(
            [_frame(session.name, seed=index) for index, session in enumerate(sessions)],
            ignore_index=True,
        ),
    )
    return sessions[0]


def test_main_dry_run_does_not_write_output(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _patch_pipeline(monkeypatch, tmp_path)
    out = tmp_path / "behavior_models.yaml"
    rc = refit_cli.main(
        [
            "--config",
            "canary.example.yaml",
            "--sessions-root",
            str(tmp_path / "sessions"),
            "--all",
            "--out",
            str(out),
            "--dry-run",
        ]
    )
    assert rc == 0
    assert not out.exists()


def test_main_writes_output_yaml(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    _patch_pipeline(monkeypatch, tmp_path)
    out = tmp_path / "behavior_models.yaml"
    rc = refit_cli.main(
        [
            "--config",
            "canary.example.yaml",
            "--sessions-root",
            str(tmp_path / "sessions"),
            "--all",
            "--out",
            str(out),
            "--recipe",
            "coolant_warmup",
        ]
    )
    assert rc == 0
    assert out.exists()
    report = json.loads(out.with_suffix(out.suffix + ".report.json").read_text())
    assert report["frame_rows"] == 1200
    assert report["recipes"][0]["calibration_sessions"]
    assert "tune_revision" in report["recipes"][0]
    assert "conditions" in report["recipes"][0]
    # B02 — the report carries the held-out evidence and its named verdict.
    # canary.example.yaml adds IMU event modes the profile has not approved,
    # so this evidence cannot pass whatever the numbers say.
    evidence = report["recipes"][0]["evidence"]
    assert evidence["split"]["test"] and evidence["test"]["evaluated"] > 0
    assert evidence["verdict"] != "passed"
    assert "mode_taxonomy:unapproved_mode:launch" in evidence["failures"]
    assert report["recipes"][0]["serving_status"]["state"] == "diagnostic_only"
    # YAML loads cleanly and contains the model
    from src.decode.behavior_models import load_behavior_models

    models = load_behavior_models(out)
    assert [m.name for m in models.models] == ["coolant_warmup"]
    # Report mentions the recipe in fitted block
    captured = capsys.readouterr().out
    assert "coolant_warmup" in captured
    assert "fitted (1)" in captured


def test_main_report_applies_profile_policy_and_preserves_prior(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """B03.3/B03.10 — a fresh fit is governed by the profile policy in the
    report, and the artifact it replaces is kept as a recoverable prior."""
    _patch_pipeline(monkeypatch, tmp_path)
    out = tmp_path / "behavior_models.yaml"
    (tmp_path / "behavior_policy.yaml").write_text(
        "schema_version: 1\n"
        "entries:\n"
        "  - model: coolant_warmup\n"
        "    lifecycle: demoted\n"
        "    permitted_uses: [display]\n"
        "    reason: test demotion\n"
        "    decided: '2026-09-21'\n"
        "    evidence: tests\n"
    )
    argv = [
        "--config",
        "canary.example.yaml",
        "--sessions-root",
        str(tmp_path / "sessions"),
        "--all",
        "--out",
        str(out),
        "--recipe",
        "coolant_warmup",
    ]
    assert refit_cli.main(argv) == 0
    report = json.loads(out.with_suffix(out.suffix + ".report.json").read_text())
    status = report["recipes"][0]["serving_status"]
    assert status["lifecycle"] == "demoted"
    assert status["permitted_uses"] == ["display"]
    assert not (tmp_path / "_prior").exists()  # nothing to preserve on the first write
    first = out.read_bytes()
    assert refit_cli.main(argv) == 0
    priors = sorted((tmp_path / "_prior").glob("*/behavior_models.yaml"))
    assert len(priors) == 1 and priors[0].read_bytes() == first
    assert (priors[0].parent / "behavior_models.yaml.report.json").exists()


def test_main_rejects_unknown_recipe(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    _patch_pipeline(monkeypatch, tmp_path)
    with pytest.raises(SystemExit, match="unknown recipe"):
        refit_cli.main(
            [
                "--config",
                "canary.example.yaml",
                "--sessions-root",
                str(tmp_path / "sessions"),
                "--all",
                "--recipe",
                "nope",
            ]
        )


def test_main_no_sessions_returns_error_code(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(_cli, "discover_sessions", lambda _root: [])
    rc = refit_cli.main(
        [
            "--config",
            "canary.example.yaml",
            "--sessions-root",
            str(tmp_path / "sessions"),
        ]
    )
    assert rc == 1


def _write_config_with_ledger(tmp_path: Path, ledger_path: Path) -> Path:
    import yaml

    data = yaml.safe_load(Path("canary.example.yaml").read_text())
    data["daemon"] = {"vehicle_ledger": str(ledger_path)}
    # Disable profile scoping so `scoped.daemon.vehicle_ledger` in the CLI
    # resolves to exactly `ledger_path` — the test writes/asserts against
    # that path directly rather than re-deriving the profile slug.
    data["profiles"] = {"enabled": False}
    cfg_path = tmp_path / "canary.yaml"
    cfg_path.write_text(yaml.safe_dump(data))
    return cfg_path


def test_since_boundary_excludes_sessions_older_than_boundary(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Task 3.5: a boundary event newer than every discovered session
    filters everything out (the CLI's existing "no sessions" error path)."""
    from src.analysis.vehicle_ledger import VehicleLedgerStore

    session = _patch_pipeline(monkeypatch, tmp_path)  # named 2026-05-13T120000
    VehicleLedgerStore(tmp_path / "vehicle_ledger.jsonl").record(
        occurred_at=date(2026, 6, 1), kind="repair", title="Head gasket", corpus_boundary=True
    )
    cfg_path = _write_config_with_ledger(tmp_path, tmp_path / "vehicle_ledger.jsonl")
    rc = refit_cli.main(
        [
            "--config",
            str(cfg_path),
            "--sessions-root",
            str(session.parent),
            "--all",
            "--since-boundary",
        ]
    )
    assert rc == 1


def test_since_boundary_keeps_sessions_on_or_after_boundary(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from src.analysis.vehicle_ledger import VehicleLedgerStore

    _patch_pipeline(monkeypatch, tmp_path)  # named 2026-05-13T120000
    VehicleLedgerStore(tmp_path / "vehicle_ledger.jsonl").record(
        occurred_at=date(2026, 1, 1), kind="mod", title="Old mod", corpus_boundary=True
    )
    cfg_path = _write_config_with_ledger(tmp_path, tmp_path / "vehicle_ledger.jsonl")
    out = tmp_path / "behavior_models.yaml"
    rc = refit_cli.main(
        [
            "--config",
            str(cfg_path),
            "--sessions-root",
            str(tmp_path / "sessions"),
            "--all",
            "--since-boundary",
            "--out",
            str(out),
            "--recipe",
            "coolant_warmup",
        ]
    )
    assert rc == 0
    assert out.exists()


def test_since_boundary_is_a_no_op_without_a_boundary_event(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _patch_pipeline(monkeypatch, tmp_path)
    cfg_path = _write_config_with_ledger(tmp_path, tmp_path / "vehicle_ledger.jsonl")
    out = tmp_path / "behavior_models.yaml"
    rc = refit_cli.main(
        [
            "--config",
            str(cfg_path),
            "--sessions-root",
            str(tmp_path / "sessions"),
            "--all",
            "--since-boundary",
            "--out",
            str(out),
            "--recipe",
            "coolant_warmup",
        ]
    )
    assert rc == 0
    assert out.exists()


def test_main_keeps_version_on_identical_second_run(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """4.5 — running the CLI twice against byte-identical training rows is a
    no-op refit; it must not churn the version."""
    _patch_pipeline(monkeypatch, tmp_path)
    out = tmp_path / "behavior_models.yaml"
    args = [
        "--config",
        "canary.example.yaml",
        "--sessions-root",
        str(tmp_path / "sessions"),
        "--all",
        "--out",
        str(out),
        "--recipe",
        "coolant_warmup",
    ]
    refit_cli.main(args)
    refit_cli.main(args)  # second run against the same synthetic fixture data

    from src.decode.behavior_models import load_behavior_models

    models = load_behavior_models(out)
    assert models.models[0].version == 1


def test_main_bumps_version_when_underlying_data_changes(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """4.5 — a second run against genuinely different training rows (here:
    a different RNG seed) still bumps the version."""
    _patch_pipeline(monkeypatch, tmp_path)
    out = tmp_path / "behavior_models.yaml"
    args = [
        "--config",
        "canary.example.yaml",
        "--sessions-root",
        str(tmp_path / "sessions"),
        "--all",
        "--out",
        str(out),
        "--recipe",
        "coolant_warmup",
    ]
    refit_cli.main(args)

    monkeypatch.setattr(
        refit_cli,
        "build_feature_frame",
        lambda *_a, **_k: pd.concat(
            [_frame(f"2026-05-1{day}T120000", seed=10 + day) for day in range(3)],
            ignore_index=True,
        ),
    )
    refit_cli.main(args)

    from src.decode.behavior_models import load_behavior_models

    models = load_behavior_models(out)
    assert models.models[0].version == 2
