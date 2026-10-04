"""R01.2: one repo-rooted home for the default data-catalog paths."""

from __future__ import annotations

import inspect
from pathlib import Path

import pytest

from src import paths
from tests.configs import example_config

REPO_ROOT = Path(__file__).resolve().parents[1]


def test_catalog_paths_are_repo_rooted() -> None:
    assert paths.REPO_ROOT == REPO_ROOT
    for path in (paths.DIDS_YAML, paths.BINDINGS_YAML, paths.DBC_DIR, paths.TEST_PROFILES_DIR):
        assert path.is_absolute()
        assert path.exists(), path


def test_every_default_home_re_exports_the_same_file() -> None:
    """The copies that used to disagree (repo-rooted vs working-directory)."""
    from src.dashboard import paths as dashboard_paths
    from src.decode import dbc, engineering
    from src.features import feature_cache, feature_pipeline
    from src.models import spec_producibility
    from src.preprocessing import decode

    assert dashboard_paths.DEFAULT_BINDINGS_PATH == paths.BINDINGS_YAML
    assert dashboard_paths.DEFAULT_DIDS_PATH == paths.DIDS_YAML
    assert engineering.DEFAULT_BINDINGS_PATH == paths.BINDINGS_YAML
    assert decode.DEFAULT_BINDINGS_PATH == paths.BINDINGS_YAML
    assert feature_cache.DEFAULT_BINDINGS_PATH == paths.BINDINGS_YAML
    assert feature_cache.DEFAULT_DIDS_PATH == paths.DIDS_YAML
    assert feature_pipeline.DEFAULT_BINDINGS_PATH == paths.BINDINGS_YAML
    assert feature_pipeline.DEFAULT_DIDS_PATH == paths.DIDS_YAML
    assert spec_producibility.DEFAULT_DIDS_PATH == paths.DIDS_YAML
    assert spec_producibility.DEFAULT_DBC_DIR == paths.DBC_DIR
    assert dbc.DEFAULT_DBC_DIR == paths.DBC_DIR


def test_feature_cache_key_hashes_the_dids_file_it_is_given(tmp_path: Path) -> None:
    """The cache key used to hash a fixed constant, whatever DID map the build
    loaded, so a build against another catalog could be served a stale frame."""
    from src.features.feature_cache import build_feature_frame_cache_key

    config = example_config(REPO_ROOT / "canary.example.yaml")
    other = tmp_path / "dids.yaml"
    other.write_text(paths.DIDS_YAML.read_text() + "\n# edited\n")

    default_key = build_feature_frame_cache_key([], config)
    assert build_feature_frame_cache_key([], config, dids_path=paths.DIDS_YAML) == default_key
    assert build_feature_frame_cache_key([], config, dids_path=other) != default_key


def _did_session(root: Path) -> Path:
    from src.storage.parquet_writer import write_records_to_parquet
    from src.telemetry import TelemetryRecord

    session = root / "2026-09-20T100000"
    records = []
    for i in range(60):
        t = 1_780_000_000.0 + i
        rpm = 780 + (i % 4)
        records.extend(
            [
                TelemetryRecord.obd(t, "RPM", float(rpm), "rpm"),
                # ECM_RPM (0x1219) is a 2-byte, scale-1 DID in data/dids.yaml.
                TelemetryRecord.obd(t, "DID_1219", f"{rpm:04X}", ""),
            ]
        )
    write_records_to_parquet(records, session, max_rows=10_000)
    return session


def test_build_feature_frame_did_columns_do_not_depend_on_the_working_directory(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from src.features.feature_pipeline import build_feature_frame

    config = example_config(REPO_ROOT / "canary.example.yaml")
    session = _did_session(tmp_path / "sessions")

    def did_columns(frame: object) -> list[str]:
        return sorted(c for c in frame.columns if "did:" in str(c))

    monkeypatch.chdir(REPO_ROOT)
    from_repo = did_columns(build_feature_frame([session], config, max_workers=1))
    elsewhere = tmp_path / "elsewhere"
    elsewhere.mkdir()
    monkeypatch.chdir(elsewhere)
    from_elsewhere = did_columns(build_feature_frame([session], config, max_workers=1))

    assert from_repo, "the fixture must produce did: columns from the repo root"
    assert from_elsewhere == from_repo
    assert "dids_path" in inspect.signature(build_feature_frame).parameters


def test_build_feature_frame_cache_key_hashes_the_dids_file_it_loads(tmp_path: Path) -> None:
    """R01.2: ``dids_path`` is threaded into the cache key, so a build against
    another DID catalog cannot be served the repo catalog's cached frame."""
    import json

    from src.features.feature_pipeline import build_feature_frame

    config = example_config(REPO_ROOT / "canary.example.yaml")
    session = _did_session(tmp_path / "sessions")
    other = tmp_path / "dids.yaml"
    other.write_text(paths.DIDS_YAML.read_text())

    cache = tmp_path / "cache"
    build_feature_frame([session], config, cache_dir=cache, max_workers=1)
    build_feature_frame([session], config, cache_dir=cache, max_workers=1, dids_path=other)

    sidecars = [json.loads(p.read_text()) for p in sorted(cache.glob("*.json"))]
    assert sorted(entry["dids"]["path"] for entry in sidecars) == sorted(
        [str(paths.DIDS_YAML), str(other.resolve())]
    )


def test_runtime_state_defaults_do_not_depend_on_the_working_directory() -> None:
    """R01.2 remainder: the daemon's runtime-state defaults are repo-rooted, and
    the module constants that mirror them are the same paths, not copies that
    only agree when the process starts at the repo root."""
    from src.analysis.llm_assist.store import DEFAULT_LLM_ASSIST_CARDS_PATH
    from src.analysis.reference_epoch import DEFAULT_REFERENCE_EPOCH_PATH
    from src.config import DaemonConfig, LLMAssistConfig
    from src.vehicle.vehicle_ledger import DEFAULT_VEHICLE_LEDGER_PATH

    daemon = DaemonConfig()
    runtime_state = {
        name: getattr(daemon, name)
        for name, field in DaemonConfig.model_fields.items()
        if isinstance(field.default, Path) and field.default.is_relative_to(paths.DATA_DIR)
    }
    assert {"jobs_dir", "anomalies_dir", "trip_state", "calibration_dir"} <= set(runtime_state)
    for name, value in runtime_state.items():
        assert value.is_absolute(), name
    assert daemon.anomalies_dir == paths.ANOMALIES_DIR
    assert DEFAULT_REFERENCE_EPOCH_PATH == daemon.reference_epoch
    assert DEFAULT_VEHICLE_LEDGER_PATH == daemon.vehicle_ledger
    assert DEFAULT_LLM_ASSIST_CARDS_PATH == LLMAssistConfig().cards_path


def test_mirror_constants_and_per_vehicle_catalogs_are_repo_rooted() -> None:
    """R01.2 remainder: every module constant that mirrors a ``DaemonConfig``
    default is that default, and the per-vehicle catalog defaults
    (``behavior_models`` and its siblings) are rooted at the checkout, so a
    tool started from another directory reads the daemon's own files."""
    from src.alerts.tuning import DEFAULT_TUNING_AUDIT_PATH, DEFAULT_TUNING_PATH
    from src.behavior.accepted_baselines import DEFAULT_ACCEPTED_BASELINES_PATH
    from src.collection.custom_pids import DEFAULT_CUSTOM_PIDS_PATH
    from src.config import DaemonConfig, SensorCanConfig
    from src.features.feature_pipeline import DEFAULT_FEATURE_CACHE_DIR
    from src.models.feedback import DEFAULT_FEEDBACK_PATH
    from src.models.training_ledger import DEFAULT_TRAINING_LEDGER_PATH

    daemon = DaemonConfig()
    assert DEFAULT_FEEDBACK_PATH == daemon.feedback_log
    assert DEFAULT_TUNING_PATH == daemon.tuning_config
    assert DEFAULT_TUNING_AUDIT_PATH == daemon.tuning_audit
    assert DEFAULT_TRAINING_LEDGER_PATH == daemon.training_ledger
    assert DEFAULT_ACCEPTED_BASELINES_PATH == daemon.accepted_baselines
    assert DEFAULT_CUSTOM_PIDS_PATH == daemon.custom_pids
    assert DEFAULT_FEATURE_CACHE_DIR == paths.DATA_DIR / "cache" / "feature_frames"
    for name in (
        "behavior_models",
        "accepted_baselines",
        "custom_pids",
        "capture_profiles",
        "afr_calibration",
        "kline_profile",
    ):
        value = getattr(daemon, name)
        assert value.is_absolute() and value.is_relative_to(paths.DATA_DIR), name
    assert SensorCanConfig().signals == paths.DATA_DIR / "sensor_can_signals.yaml"
    assert SensorCanConfig().signals.exists()


def test_mcp_fallbacks_are_the_daemon_defaults(tmp_path: Path) -> None:
    """The MCP server's no-YAML-key fallbacks are the daemon's own defaults."""
    from src.config import DaemonConfig
    from src.mcp_server.config import (
        _anomalies_dir_from_config_yaml,
        _catalog_db_from_config_yaml,
    )

    config = tmp_path / "canary.yaml"
    config.write_text("collection: {}\n")
    assert _anomalies_dir_from_config_yaml(str(config)) == DaemonConfig().anomalies_dir
    assert _catalog_db_from_config_yaml(str(config)) == DaemonConfig().catalog_db


def test_no_working_directory_relative_data_path_in_src() -> None:
    """The R01.2 acceptance, kept: ``grep -rn 'Path("data/' src`` is empty."""
    offenders = [
        f"{path.relative_to(REPO_ROOT)}:{number}"
        for path in sorted((REPO_ROOT / "src").rglob("*.py"))
        for number, line in enumerate(path.read_text().splitlines(), start=1)
        if 'Path("data/' in line or "Path('data/" in line
    ]
    assert offenders == []
