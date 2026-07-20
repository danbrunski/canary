"""Tests for ``scripts/refit_behavior_models.py`` (Task 4.E.8 CLI shell)."""

from __future__ import annotations

import sys
from datetime import UTC, datetime
from pathlib import Path

import numpy as np
import pandas as pd
import pytest

# Importable as a module so we can call main() and patch its dependencies.
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
import refit_behavior_models as refit_cli  # noqa: E402


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
# _filter_sessions_by_age
# ---------------------------------------------------------------------------


def test_filter_keeps_recent_sessions_drops_old_ones(tmp_path: Path) -> None:
    now = datetime(2026, 5, 13, tzinfo=UTC)
    recent = tmp_path / "2026-05-10T120000"
    old = tmp_path / "2026-03-01T000000"
    odd = tmp_path / "manual-import"  # non-conforming name — kept
    for p in (recent, old, odd):
        p.mkdir()
    kept = refit_cli._filter_sessions_by_age([recent, old, odd], days=14, now=now)
    assert recent in kept
    assert old not in kept
    assert odd in kept


def test_filter_disabled_by_days_zero(tmp_path: Path) -> None:
    sessions = [tmp_path / "2020-01-01T000000", tmp_path / "2026-05-13T000000"]
    for s in sessions:
        s.mkdir()
    kept = refit_cli._filter_sessions_by_age(sessions, days=0, now=datetime.now(UTC))
    assert kept == sessions


# ---------------------------------------------------------------------------
# _select_recipes
# ---------------------------------------------------------------------------


def test_select_recipes_default_returns_full_catalog() -> None:
    catalog = refit_cli.default_recipes()
    assert refit_cli._select_recipes(catalog, None) == catalog


def test_select_recipes_filters_by_name() -> None:
    catalog = refit_cli.default_recipes()
    selected = refit_cli._select_recipes(catalog, ["coolant_warmup", "idle_rpm_vs_coolant"])
    assert [r.name for r in selected] == ["coolant_warmup", "idle_rpm_vs_coolant"]


def test_select_recipes_rejects_unknown_name() -> None:
    catalog = refit_cli.default_recipes()
    with pytest.raises(SystemExit, match="unknown recipe"):
        refit_cli._select_recipes(catalog, ["nope"])


# ---------------------------------------------------------------------------
# main() — dry run, smoke, write-out
# ---------------------------------------------------------------------------


def _patch_pipeline(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> Path:
    """Stub out session discovery + the feature pipeline so we don't need real Parquet.

    Also writes a synthetic ``classification.json`` carrying a
    ``cold_start`` event so the post-Phase-C event-gated ``coolant_warmup``
    recipe finds the session eligible.
    """
    session = tmp_path / "sessions" / "2026-05-13T120000"
    session.mkdir(parents=True)
    # Mirror what ``label_sessions.py`` would write — minimal but valid.
    import json

    (session / "classification.json").write_text(
        json.dumps(
            {
                "schema_version": 1,
                "classifier_version": "0.1.0",
                "classified_at": "2026-05-13T12:00:00Z",
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
    monkeypatch.setattr(refit_cli, "discover_sessions", lambda _root: [session])
    monkeypatch.setattr(refit_cli, "build_feature_frame", lambda *_a, **_k: _frame(session.name))
    return session


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
    # YAML loads cleanly and contains the model
    from src.decode.behavior_models import load_behavior_models

    models = load_behavior_models(out)
    assert [m.name for m in models.models] == ["coolant_warmup"]
    # Report mentions the recipe in fitted block
    captured = capsys.readouterr().out
    assert "coolant_warmup" in captured
    assert "fitted (1)" in captured


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
    monkeypatch.setattr(refit_cli, "discover_sessions", lambda _root: [])
    rc = refit_cli.main(
        [
            "--config",
            "canary.example.yaml",
            "--sessions-root",
            str(tmp_path / "sessions"),
        ]
    )
    assert rc == 1


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
    session = _patch_pipeline(monkeypatch, tmp_path)
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
        refit_cli, "build_feature_frame", lambda *_a, **_k: _frame(session.name, seed=1)
    )
    refit_cli.main(args)

    from src.decode.behavior_models import load_behavior_models

    models = load_behavior_models(out)
    assert models.models[0].version == 2
