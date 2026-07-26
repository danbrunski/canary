"""Tests for the hot-swap ModelManager (Task 5.16)."""

from __future__ import annotations

import json
import logging
import os
import time
from pathlib import Path

import pytest

from src.models.manager import (
    LoadedModel,
    ModelManager,
    SwapAttempt,
    default_manifest_sidecar,
    manager_for_artifact,
)
from src.models.manifest import CURRENT_MANIFEST_FORMAT_VERSION


def _feature_schema(
    feature_columns: list[str],
    *,
    all_nan_columns: list[str] | None = None,
) -> dict[str, object]:
    all_nan = all_nan_columns or []
    return {
        "feature_spec_version": 1,
        "feature_columns": feature_columns,
        "feature_dtypes": {column: "float64" for column in feature_columns},
        "source_prefixes": ["obd"],
        "behavior_model_columns": [],
        "feature_health": {
            "row_count": 1000,
            "candidate_feature_count": len(feature_columns),
            "selected_feature_count": len(feature_columns),
            "dropped_all_nan_columns": [],
            "all_nan_columns": all_nan,
            "constant_columns": [],
            "non_finite_columns": [],
            "columns": {
                column: {
                    "dtype": "float64",
                    "non_null_count": 0 if column in all_nan else 1000,
                    "finite_count": 0 if column in all_nan else 1000,
                    "nan_count": 1000 if column in all_nan else 0,
                    "non_finite_count": 0,
                    "unique_finite_count": 0 if column in all_nan else 10,
                    "constant": False,
                }
                for column in feature_columns
            },
        },
    }


def _write_pair(
    artifact_path: Path,
    *,
    model_type: str = "isolation_forest",
    version: str = "v1",
    feature_columns: list[str] | None = None,
    schema: dict[str, object] | None = None,
    mtime: float | None = None,
    calibration: str | None = None,
) -> Path:
    """Write a (artifact, manifest) pair on disk; return the manifest path."""
    manifest_path = default_manifest_sidecar(artifact_path)
    artifact_path.parent.mkdir(parents=True, exist_ok=True)
    artifact_path.write_text(f"artifact:{version}")
    payload = {
        "artifact_format_version": CURRENT_MANIFEST_FORMAT_VERSION,
        "model_type": model_type,
        "model_version": version,
        "created_at": "2026-05-13T12:00:00+00:00",
        "training_data_hash": "sha256:deadbeef",
        "input_rows": 1000,
        "hyperparams": {},
        "schema": schema or _feature_schema(feature_columns or ["obd:RPM", "obd:SPEED"]),
    }
    if calibration is not None:
        # Unknown top-level keys land in ModelManifestCommon.extras (3.1).
        payload["calibration"] = calibration
    manifest_path.write_text(json.dumps(payload))
    if mtime is not None:
        os.utime(manifest_path, (mtime, mtime))
        os.utime(artifact_path, (mtime, mtime))
    return manifest_path


def _loader(path: Path) -> dict:
    """Trivial loader — returns the artifact text so tests can spot which one is live."""
    return {"contents": path.read_text()}


# ---------------------------------------------------------------------------
# load_initial
# ---------------------------------------------------------------------------


def test_load_initial_loads_present_artifact(tmp_path: Path) -> None:
    artifact = tmp_path / "model.joblib"
    _write_pair(artifact, version="v1")
    mgr: ModelManager[dict] = ModelManager(artifact, loader=_loader)
    result = mgr.load_initial()
    assert result.kind == "swapped"
    assert result.new_version == "v1"
    assert mgr.current is not None
    assert mgr.current.manifest.model_version == "v1"


def test_load_initial_missing_files_returns_missing(tmp_path: Path) -> None:
    mgr: ModelManager[dict] = ModelManager(tmp_path / "absent.joblib", loader=_loader)
    result = mgr.load_initial()
    assert result.kind == "missing"
    assert mgr.current is None


class _EmbeddedManifest:
    def __init__(self, model_version: str) -> None:
        self.model_version = model_version


class _EmbeddedArtifact:
    """Artifact bundle carrying its own manifest, like both real trainers'."""

    def __init__(self, model_version: str) -> None:
        self.manifest = _EmbeddedManifest(model_version)


def test_load_detects_artifact_manifest_version_mismatch(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    """A fresh manifest sidecar next to a stale binary (partial deploy —
    binaries move by scp, not git) must load, report the *binary's* version
    as ``serving_version``, and log an error naming both."""
    artifact = tmp_path / "model.joblib"
    _write_pair(artifact, version="v2-manifest-only")
    mgr: ModelManager[_EmbeddedArtifact] = ModelManager(
        artifact, loader=lambda _p: _EmbeddedArtifact("v1-in-binary")
    )
    with caplog.at_level(logging.ERROR, logger="src.models.manager"):
        result = mgr.load_initial()
    assert result.kind == "swapped"  # still serves — old model beats no model
    assert mgr.current is not None
    assert mgr.current.embedded_version == "v1-in-binary"
    assert mgr.current.serving_version == "v1-in-binary"
    assert mgr.current.manifest.model_version == "v2-manifest-only"
    assert any("version mismatch" in record.message for record in caplog.records)


def test_load_matching_embedded_version_is_quiet(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    artifact = tmp_path / "model.joblib"
    _write_pair(artifact, version="v1")
    mgr: ModelManager[_EmbeddedArtifact] = ModelManager(
        artifact, loader=lambda _p: _EmbeddedArtifact("v1")
    )
    with caplog.at_level(logging.ERROR, logger="src.models.manager"):
        mgr.load_initial()
    assert mgr.current is not None
    assert mgr.current.serving_version == "v1"
    assert not [r for r in caplog.records if "version mismatch" in r.message]


def test_serving_version_falls_back_to_manifest_without_embedded(tmp_path: Path) -> None:
    """Loaders that don't expose an embedded manifest (tests, future model
    kinds) keep the old manifest-derived behavior."""
    artifact = tmp_path / "model.joblib"
    _write_pair(artifact, version="v1")
    mgr: ModelManager[dict] = ModelManager(artifact, loader=_loader)
    mgr.load_initial()
    assert mgr.current is not None
    assert mgr.current.embedded_version is None
    assert mgr.current.serving_version == "v1"


# ---------------------------------------------------------------------------
# poll: swap, no-change, rejection
# ---------------------------------------------------------------------------


def test_poll_no_change_when_mtime_unchanged(tmp_path: Path) -> None:
    artifact = tmp_path / "model.joblib"
    _write_pair(artifact, version="v1")
    mgr: ModelManager[dict] = ModelManager(artifact, loader=_loader)
    mgr.load_initial()
    result = mgr.poll()
    assert result.kind == "no_change"
    assert mgr.current is not None
    assert mgr.current.manifest.model_version == "v1"


def test_poll_swaps_when_manifest_is_newer(tmp_path: Path) -> None:
    artifact = tmp_path / "model.joblib"
    _write_pair(artifact, version="v1", mtime=1_000_000.0)
    mgr: ModelManager[dict] = ModelManager(artifact, loader=_loader)
    mgr.load_initial()

    # Replace with a newer manifest
    _write_pair(artifact, version="v2", mtime=2_000_000.0)
    result = mgr.poll()
    assert result.kind == "swapped"
    assert result.new_version == "v2"
    assert mgr.current is not None
    assert mgr.current.manifest.model_version == "v2"


def test_poll_accepts_feature_schema_expansion(tmp_path: Path) -> None:
    artifact = tmp_path / "model.joblib"
    _write_pair(artifact, version="v1", feature_columns=["a", "b"], mtime=1_000_000.0)
    mgr: ModelManager[dict] = ModelManager(artifact, loader=_loader)
    mgr.load_initial()

    _write_pair(
        artifact,
        version="v2",
        feature_columns=["a", "b", "c"],  # different schema
        mtime=2_000_000.0,
    )
    result = mgr.poll()
    assert result.kind == "swapped"
    assert result.new_version == "v2"
    assert mgr.current is not None
    assert mgr.current.manifest.model_version == "v2"


def test_poll_retries_when_only_artifact_mtime_changes(tmp_path: Path) -> None:
    artifact = tmp_path / "model.joblib"
    _write_pair(artifact, version="v1", mtime=1_000_000.0)
    mgr: ModelManager[dict] = ModelManager(artifact, loader=_loader)
    mgr.load_initial()

    artifact.write_text("artifact:v2")
    os.utime(artifact, (2_000_000.0, 2_000_000.0))
    result = mgr.poll()
    assert result.kind == "swapped"
    assert mgr.current is not None
    assert mgr.current.artifact["contents"] == "artifact:v2"


def test_poll_rejects_partial_pair_then_loads_when_binary_lands(tmp_path: Path) -> None:
    artifact = tmp_path / "model.joblib"
    manifest = _write_pair(artifact, version="v1", mtime=1_000_000.0)
    embedded_version = "v1"
    mgr: ModelManager[_EmbeddedArtifact] = ModelManager(
        artifact, loader=lambda _p: _EmbeddedArtifact(embedded_version)
    )
    mgr.load_initial()

    payload = json.loads(manifest.read_text())
    payload["model_version"] = "v2"
    manifest.write_text(json.dumps(payload))
    os.utime(manifest, (2_000_000.0, 2_000_000.0))
    rejected = mgr.poll()
    assert rejected.kind == "rejected"
    assert mgr.current is not None
    assert mgr.current.manifest.model_version == "v1"

    embedded_version = "v2"
    artifact.write_text("artifact:v2")
    os.utime(artifact, (3_000_000.0, 3_000_000.0))
    swapped = mgr.poll()
    assert swapped.kind == "swapped"
    assert mgr.current is not None
    assert mgr.current.serving_version == "v2"


def test_poll_rejects_unhealthy_feature_spec(tmp_path: Path) -> None:
    artifact = tmp_path / "model.joblib"
    _write_pair(artifact, version="v1", feature_columns=["obd:RPM"], mtime=1_000_000.0)
    mgr: ModelManager[dict] = ModelManager(artifact, loader=_loader)
    mgr.load_initial()

    _write_pair(
        artifact,
        version="v2",
        schema=_feature_schema(["obd:RPM"], all_nan_columns=["obd:RPM"]),
        mtime=2_000_000.0,
    )
    result = mgr.poll()
    assert result.kind == "rejected"
    assert "all-NaN" in result.detail
    assert mgr.current is not None
    assert mgr.current.manifest.model_version == "v1"


def test_poll_rejects_unknown_model_type_when_allowed_list_set(tmp_path: Path) -> None:
    artifact = tmp_path / "model.joblib"
    _write_pair(artifact, version="v1", model_type="isolation_forest", mtime=1_000_000.0)
    mgr: ModelManager[dict] = ModelManager(
        artifact, loader=_loader, allowed_model_types=("isolation_forest",)
    )
    mgr.load_initial()
    # Same schema, but the new manifest is a different detector kind.
    _write_pair(artifact, version="v2", model_type="lstm_autoencoder", mtime=2_000_000.0)
    result = mgr.poll()
    assert result.kind == "rejected"
    assert "model_type" in result.detail
    assert mgr.current is not None
    assert mgr.current.manifest.model_version == "v1"


def test_poll_rejects_malformed_manifest(tmp_path: Path) -> None:
    artifact = tmp_path / "model.joblib"
    _write_pair(artifact, version="v1", mtime=1_000_000.0)
    mgr: ModelManager[dict] = ModelManager(artifact, loader=_loader)
    mgr.load_initial()

    # Corrupt the manifest with newer mtime
    manifest_path = default_manifest_sidecar(artifact)
    manifest_path.write_text("{nope")
    os.utime(manifest_path, (2_000_000.0, 2_000_000.0))
    result = mgr.poll()
    assert result.kind == "rejected"
    assert mgr.current is not None
    assert mgr.current.manifest.model_version == "v1"


def test_poll_rejects_when_loader_raises(tmp_path: Path) -> None:
    artifact = tmp_path / "model.joblib"
    _write_pair(artifact, version="v1", mtime=1_000_000.0)
    mgr: ModelManager[dict] = ModelManager(artifact, loader=_loader)
    mgr.load_initial()

    def _broken(path: Path) -> dict:
        raise RuntimeError("artifact corrupt")

    _write_pair(artifact, version="v2", mtime=2_000_000.0)
    # Swap the loader to a broken one for the next poll.
    mgr._loader = _broken  # type: ignore[assignment]
    result = mgr.poll()
    assert result.kind == "rejected"
    assert "loader failed" in result.detail
    assert mgr.current is not None
    assert mgr.current.manifest.model_version == "v1"


def test_poll_uses_lock_so_two_pollers_dont_double_load(tmp_path: Path) -> None:
    """The lock guards load-validate-assign; two simultaneous poll() calls must
    each see a consistent ``current`` even if one swaps in between."""
    artifact = tmp_path / "model.joblib"
    _write_pair(artifact, version="v1", mtime=1_000_000.0)
    mgr: ModelManager[dict] = ModelManager(artifact, loader=_loader)
    mgr.load_initial()

    load_calls: list[Path] = []

    def _tracking_loader(path: Path) -> dict:
        load_calls.append(path)
        # Yield to let other thread's poll try to slip in
        time.sleep(0.05)
        return {"contents": path.read_text()}

    mgr._loader = _tracking_loader  # type: ignore[assignment]
    _write_pair(artifact, version="v2", mtime=2_000_000.0)

    import threading

    results: list[SwapAttempt] = []
    threads = [
        threading.Thread(target=lambda: results.append(mgr.poll())),
        threading.Thread(target=lambda: results.append(mgr.poll())),
    ]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    # Exactly one swap should have happened — the second poll sees the same mtime
    # we already loaded and reports no_change.
    swapped = [r for r in results if r.kind == "swapped"]
    no_change = [r for r in results if r.kind == "no_change"]
    assert len(swapped) == 1
    assert len(no_change) == 1
    assert len(load_calls) == 1  # loader called once, not twice


# ---------------------------------------------------------------------------
# manager_for_artifact convenience
# ---------------------------------------------------------------------------


def test_manager_for_artifact_loads_on_construction(tmp_path: Path) -> None:
    artifact = tmp_path / "model.joblib"
    _write_pair(artifact, version="v1", model_type="lstm_autoencoder")
    mgr = manager_for_artifact(artifact, _loader, model_type="lstm_autoencoder")
    assert mgr.current is not None
    assert mgr.current.manifest.model_version == "v1"


def test_manager_for_artifact_with_wrong_type_returns_unloaded(tmp_path: Path) -> None:
    artifact = tmp_path / "model.joblib"
    _write_pair(artifact, version="v1", model_type="lstm_autoencoder")
    mgr = manager_for_artifact(artifact, _loader, model_type="isolation_forest")
    # initial load is rejected; current stays None
    assert mgr.current is None


# ---------------------------------------------------------------------------
# Snapshot semantics
# ---------------------------------------------------------------------------


def test_current_is_a_dataclass_snapshot(tmp_path: Path) -> None:
    artifact = tmp_path / "model.joblib"
    _write_pair(artifact, version="v1")
    mgr: ModelManager[dict] = ModelManager(artifact, loader=_loader)
    mgr.load_initial()
    snap1 = mgr.current
    assert isinstance(snap1, LoadedModel)
    # Swapping to a newer model: the prior snapshot must not mutate
    _write_pair(artifact, version="v2", mtime=time.time() + 10)
    mgr.poll()
    snap2 = mgr.current
    assert snap1 is not snap2
    assert snap1.manifest.model_version == "v1"
    assert snap2 is not None and snap2.manifest.model_version == "v2"


# ---------------------------------------------------------------------------
# Task 3.1 — in-sample calibration load-time warning
# ---------------------------------------------------------------------------


def test_load_initial_warns_when_manifest_is_in_sample_calibrated(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    artifact = tmp_path / "model.joblib"
    _write_pair(artifact, version="v1", model_type="lstm_autoencoder", calibration="in_sample")
    mgr: ModelManager[dict] = ModelManager(artifact, loader=_loader)
    with caplog.at_level(logging.WARNING, logger="src.models.manager"):
        result = mgr.load_initial()

    assert result.kind == "swapped"
    assert any("in-sample" in record.message for record in caplog.records)


def test_load_initial_does_not_warn_for_held_out_calibration(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    artifact = tmp_path / "model.joblib"
    _write_pair(artifact, version="v1", model_type="lstm_autoencoder", calibration="held_out")
    mgr: ModelManager[dict] = ModelManager(artifact, loader=_loader)
    with caplog.at_level(logging.WARNING, logger="src.models.manager"):
        result = mgr.load_initial()

    assert result.kind == "swapped"
    assert not any("in-sample" in record.message for record in caplog.records)


# ---------------------------------------------------------------------------
# Task 6.7 — load-time log line carries version + training window + data-hash
# ---------------------------------------------------------------------------


def test_load_initial_logs_version_window_and_data_hash(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    artifact = tmp_path / "model.joblib"
    _write_pair(artifact, version="v1")
    mgr: ModelManager[dict] = ModelManager(artifact, loader=_loader)
    with caplog.at_level(logging.INFO, logger="src.models.manager"):
        result = mgr.load_initial()

    assert result.kind == "swapped"
    swapped = [r for r in caplog.records if r.message == "model swapped"]
    assert len(swapped) == 1
    record = swapped[0]
    assert record.new_version == "v1"
    assert record.prior_version is None
    assert record.training_data_hash == "sha256:deadbeef"
    # _write_pair doesn't set these — the field just needs to be logged when present.
    assert record.training_start_timestamp is None
    assert record.training_end_timestamp is None


def test_poll_swap_logs_the_new_manifests_provenance(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    artifact = tmp_path / "model.joblib"
    _write_pair(artifact, version="v1", mtime=1_000_000.0)
    mgr: ModelManager[dict] = ModelManager(artifact, loader=_loader)
    mgr.load_initial()

    _write_pair(artifact, version="v2", mtime=2_000_000.0)
    with caplog.at_level(logging.INFO, logger="src.models.manager"):
        result = mgr.poll()

    assert result.kind == "swapped"
    swapped = [r for r in caplog.records if r.message == "model swapped"]
    assert len(swapped) == 1
    record = swapped[0]
    assert record.prior_version == "v1"
    assert record.new_version == "v2"
    assert record.training_data_hash == "sha256:deadbeef"
