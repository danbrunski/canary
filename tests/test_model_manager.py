"""Tests for the hot-swap ModelManager (Task 5.16)."""

from __future__ import annotations

import json
import os
import time
from pathlib import Path

from src.models.manager import (
    LoadedModel,
    ModelManager,
    SwapAttempt,
    default_manifest_sidecar,
    manager_for_artifact,
)
from src.models.manifest import CURRENT_MANIFEST_FORMAT_VERSION


def _write_pair(
    artifact_path: Path,
    *,
    model_type: str = "isolation_forest",
    version: str = "v1",
    feature_columns: list[str] | None = None,
    mtime: float | None = None,
) -> Path:
    """Write a (artifact, manifest) pair on disk; return the manifest path."""
    manifest_path = default_manifest_sidecar(artifact_path)
    artifact_path.parent.mkdir(parents=True, exist_ok=True)
    artifact_path.write_text(f"artifact:{version}")
    manifest_path.write_text(
        json.dumps(
            {
                "artifact_format_version": CURRENT_MANIFEST_FORMAT_VERSION,
                "model_type": model_type,
                "model_version": version,
                "created_at": "2026-05-13T12:00:00+00:00",
                "training_data_hash": "sha256:deadbeef",
                "input_rows": 1000,
                "hyperparams": {},
                "schema": {"feature_columns": feature_columns or ["obd:RPM", "obd:SPEED"]},
            }
        )
    )
    if mtime is not None:
        os.utime(manifest_path, (mtime, mtime))
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


def test_poll_rejects_when_feature_columns_differ(tmp_path: Path) -> None:
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
    assert result.kind == "rejected"
    assert "feature_columns" in result.detail
    assert result.new_version == "v2"
    # Running model is unchanged
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
