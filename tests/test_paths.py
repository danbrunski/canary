"""R01.2: one repo-rooted home for the default data-catalog paths."""

from __future__ import annotations

import inspect
from pathlib import Path

import pytest

from src import paths
from src.config import CanaryConfig

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
    from src.models import spec_producibility
    from src.preprocessing import decode, feature_cache, feature_pipeline

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
    from src.preprocessing.feature_cache import build_feature_frame_cache_key

    config = CanaryConfig.from_yaml(REPO_ROOT / "canary.example.yaml")
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
    from src.preprocessing.feature_pipeline import build_feature_frame

    config = CanaryConfig.from_yaml(REPO_ROOT / "canary.example.yaml")
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

    from src.preprocessing.feature_pipeline import build_feature_frame

    config = CanaryConfig.from_yaml(REPO_ROOT / "canary.example.yaml")
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
    from src.analysis.vehicle_ledger import DEFAULT_VEHICLE_LEDGER_PATH
    from src.config import DaemonConfig, LLMAssistConfig

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
