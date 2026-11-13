"""R29.9 -- ``/status`` memoizes its near-static blocks on file identity.

A memo that can serve stale data after ``canaryctl models upload`` is a bug, so
most of these tests are about *change*: a manifest added, rewritten or removed,
a flash registry edited, a verify record written, each in the racy window where
a ``(path, mtime_ns, size)`` key alone would still match.
"""

from __future__ import annotations

import json
import os
import time
from collections.abc import Sequence
from functools import partial
from pathlib import Path
from typing import Any

import pytest
import yaml
from fastapi.testclient import TestClient

from src.config import CanaryConfig
from src.control.calibration import memory_verify_summary, record_verify_results
from src.dashboard.api import create_app
from src.dashboard.status import model_manifest_summary
from src.decode.dids import MemoryChannel, is_memory_checkable, load_did_map
from src.file_memo import FileStamp, StampedCache, file_stamp
from src.paths import DIDS_YAML
from src.storage.capture_manifest import CaptureManifest
from src.storage.sidecar import read_json_object
from src.vehicle.tune_revisions import load_tune_revisions
from src.yaml_load import RACY_WINDOW_NS
from tests.configs import isolated_config

LONG_AGO_NS = time.time_ns() - 10 * RACY_WINDOW_NS


def _settle(path: Path) -> None:
    """Back-date ``path`` so a value computed from it now is outside the racy window."""
    os.utime(path, ns=(LONG_AGO_NS, LONG_AGO_NS))


# ---------------------------------------------------------------------------
# StampedCache
# ---------------------------------------------------------------------------


def test_a_settled_value_is_computed_once_while_the_stamps_hold(tmp_path: Path) -> None:
    path = tmp_path / "a.json"
    path.write_text("{}")
    _settle(path)
    calls: list[int] = []
    cache: StampedCache[int] = StampedCache()

    def compute() -> int:
        calls.append(1)
        return len(calls)

    stamps = [file_stamp(path)]
    assert [cache.get("scope", stamps, compute) for _ in range(5)] == [1, 1, 1, 1, 1]
    assert len(calls) == 1


def test_a_changed_stamp_recomputes_even_when_the_old_value_was_settled(tmp_path: Path) -> None:
    path = tmp_path / "a.json"
    path.write_text("one")
    _settle(path)
    cache: StampedCache[str] = StampedCache()
    assert cache.get("s", [file_stamp(path)], path.read_text) == "one"

    path.write_text("three")  # different size, newer mtime
    assert cache.get("s", [file_stamp(path)], path.read_text) == "three"


def test_a_file_appearing_or_vanishing_is_a_different_key(tmp_path: Path) -> None:
    path = tmp_path / "late.json"
    cache: StampedCache[str] = StampedCache()

    def read() -> str:
        return path.read_text() if path.exists() else "absent"

    assert cache.get("s", [file_stamp(path)], read) == "absent"
    path.write_text("here")
    _settle(path)
    assert cache.get("s", [file_stamp(path)], read) == "here"
    path.unlink()
    assert cache.get("s", [file_stamp(path)], read) == "absent"


def test_a_same_size_rewrite_in_the_racy_window_is_never_served_stale(tmp_path: Path) -> None:
    """The reason the key is not enough: the kernel stamps a write from a coarse
    clock, so two same-size writes in one tick carry one ``st_mtime_ns``. Forced
    here by restoring the first write's stamp, which is what a tick-fast second
    write looks like from outside."""
    path = tmp_path / "a.json"
    path.write_text("AAAA")
    stamp = path.stat().st_mtime_ns
    cache: StampedCache[str] = StampedCache()
    assert cache.get("s", [file_stamp(path)], path.read_text) == "AAAA"

    path.write_text("BBBB")
    os.utime(path, ns=(stamp, stamp))  # same path, mtime and size as before
    assert file_stamp(path) == FileStamp(str(path), stamp, 4)
    assert cache.get("s", [file_stamp(path)], path.read_text) == "BBBB"


def test_the_cache_is_bounded_and_clearable(tmp_path: Path) -> None:
    cache: StampedCache[int] = StampedCache(max_entries=3)
    for index in range(10):
        cache.get(index, [], partial(int, index))
    assert len(cache._entries) == 3
    cache.clear()
    assert not cache._entries


# ---------------------------------------------------------------------------
# Model manifests
# ---------------------------------------------------------------------------


def _manifest(models: Path, name: str, version: str) -> Path:
    directory = models / name
    directory.mkdir(parents=True, exist_ok=True)
    path = directory / "model.manifest.json"
    path.write_text(json.dumps({"model_version": version, "training_sessions": ["s1"]}))
    return path


def test_manifests_are_parsed_once_while_unchanged(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    models = tmp_path / "models"
    for name in ("isolation_forest", "lstm_autoencoder"):
        _settle(_manifest(models, name, f"{name}-1"))
    reads: list[Path] = []
    real = read_json_object

    def counting(path: Path) -> dict[str, Any] | None:
        reads.append(path)
        return real(path)

    monkeypatch.setattr("src.dashboard.status.read_json_object", counting)

    first = model_manifest_summary(models)
    for _ in range(4):
        assert model_manifest_summary(models) == first
    # Two manifests, parsed once each -- and once, not twice per manifest.
    assert len(reads) == 2
    assert first[0] == {
        "isolation_forest": "isolation_forest-1",
        "lstm_autoencoder": "lstm_autoencoder-1",
    }


def test_an_uploaded_manifest_shows_on_the_next_call(tmp_path: Path) -> None:
    models = tmp_path / "models"
    _settle(_manifest(models, "isolation_forest", "if-1"))
    assert model_manifest_summary(models)[0] == {"isolation_forest": "if-1"}
    assert model_manifest_summary(models)[0] == {"isolation_forest": "if-1"}  # settled and cached

    _manifest(models, "lstm_autoencoder", "lstm-1")  # `canaryctl models upload`
    versions, provenance = model_manifest_summary(models)
    assert versions == {"isolation_forest": "if-1", "lstm_autoencoder": "lstm-1"}
    assert provenance["lstm_autoencoder"] is not None


def test_a_replaced_manifest_shows_on_the_next_call(tmp_path: Path) -> None:
    models = tmp_path / "models"
    path = _manifest(models, "isolation_forest", "if-1")
    _settle(path)
    assert model_manifest_summary(models)[0] == {"isolation_forest": "if-1"}
    assert model_manifest_summary(models)[0] == {"isolation_forest": "if-1"}

    path.write_text(json.dumps({"model_version": "if-22", "training_sessions": []}))  # new size
    assert model_manifest_summary(models)[0] == {"isolation_forest": "if-22"}


def test_a_same_size_replacement_in_the_racy_window_shows_on_the_next_call(
    tmp_path: Path,
) -> None:
    """Version ``if-1`` to ``if-2``: same size, written within one clock tick."""
    models = tmp_path / "models"
    path = _manifest(models, "isolation_forest", "if-1")
    stamp = path.stat().st_mtime_ns
    assert model_manifest_summary(models)[0] == {"isolation_forest": "if-1"}

    path.write_text(json.dumps({"model_version": "if-2", "training_sessions": ["s1"]}))
    os.utime(path, ns=(stamp, stamp))
    assert model_manifest_summary(models)[0] == {"isolation_forest": "if-2"}


def test_a_removed_manifest_disappears(tmp_path: Path) -> None:
    models = tmp_path / "models"
    gone = _manifest(models, "isolation_forest", "if-1")
    _settle(_manifest(models, "lstm_autoencoder", "lstm-1"))
    _settle(gone)
    assert set(model_manifest_summary(models)[0]) == {"isolation_forest", "lstm_autoencoder"}
    gone.unlink()
    assert set(model_manifest_summary(models)[0]) == {"lstm_autoencoder"}


def test_a_caller_cannot_corrupt_the_cached_summary(tmp_path: Path) -> None:
    models = tmp_path / "models"
    _settle(_manifest(models, "isolation_forest", "if-1"))
    versions, provenance = model_manifest_summary(models)
    versions["isolation_forest"] = "tampered"
    provenance["isolation_forest"]["training_sessions"].append("tampered")  # type: ignore[index]
    versions2, provenance2 = model_manifest_summary(models)
    assert versions2 == {"isolation_forest": "if-1"}
    assert provenance2["isolation_forest"]["training_sessions"] == ["s1"]  # type: ignore[index]


def test_two_models_directories_do_not_share_a_cache_entry(tmp_path: Path) -> None:
    for vehicle, version in (("a", "a-1"), ("b", "b-1")):
        _settle(_manifest(tmp_path / vehicle, "isolation_forest", version))
    assert model_manifest_summary(tmp_path / "a")[0] == {"isolation_forest": "a-1"}
    assert model_manifest_summary(tmp_path / "b")[0] == {"isolation_forest": "b-1"}


def test_status_serves_a_model_uploaded_between_two_polls(tmp_path: Path) -> None:
    """End to end through the route: a poll, an upload, the next poll."""
    config = isolated_config(tmp_path, "daemon")
    models = tmp_path / "models"
    _settle(_manifest(models, "isolation_forest", "if-1"))
    client = TestClient(create_app(config, anomalies_dir=tmp_path / "anomalies", models_dir=models))

    assert client.get("/status").json()["model_versions"] == {"isolation_forest": "if-1"}
    _manifest(models, "lstm_autoencoder", "lstm-1")
    body = client.get("/status").json()
    assert body["model_versions"] == {"isolation_forest": "if-1", "lstm_autoencoder": "lstm-1"}
    assert body["model_provenance"]["lstm_autoencoder"]["model_version"] == "lstm-1"


# ---------------------------------------------------------------------------
# The flash registry
# ---------------------------------------------------------------------------


def _registry(path: Path, *revisions: str) -> None:
    path.write_text(
        yaml.safe_dump(
            {
                "revisions": [
                    {"revision": name, "flashed_at": f"2026-01-0{index + 1}T00:00:00+00:00"}
                    for index, name in enumerate(revisions)
                ]
            }
        )
    )


def test_the_flash_registry_is_parsed_once_while_the_file_is_unchanged(tmp_path: Path) -> None:
    path = tmp_path / "tune_revisions.yaml"
    _registry(path, "R1")
    _settle(path)
    first = load_tune_revisions(path)
    assert first is not None
    assert load_tune_revisions(path) is first


def test_an_edited_flash_registry_is_reread(tmp_path: Path) -> None:
    path = tmp_path / "tune_revisions.yaml"
    _registry(path, "R1")
    registry = load_tune_revisions(path)
    assert registry is not None and [r.revision for r in registry.revisions] == ["R1"]

    _registry(path, "R1", "R2")  # `append_flash`, or a hand edit
    registry = load_tune_revisions(path)
    assert registry is not None and [r.revision for r in registry.revisions] == ["R1", "R2"]


def test_a_missing_flash_registry_is_none_and_a_bad_one_names_the_file(tmp_path: Path) -> None:
    path = tmp_path / "tune_revisions.yaml"
    assert load_tune_revisions(path) is None
    path.write_text("revisions: [not, a, revision]")
    with pytest.raises(ValueError, match="tune_revisions.yaml"):
        load_tune_revisions(path)


# ---------------------------------------------------------------------------
# The DID catalog's verify summary
# ---------------------------------------------------------------------------


def _verify_config(tmp_path: Path) -> CanaryConfig:
    return isolated_config(tmp_path, "daemon")


def test_the_verify_summary_follows_the_catalog_and_the_verify_record(tmp_path: Path) -> None:
    config = _verify_config(tmp_path)
    catalog = load_did_map(DIDS_YAML).memory
    checkable = [c.name for c in catalog if is_memory_checkable(c, catalog)]
    assert len(checkable) >= 3

    before = memory_verify_summary(config, DIDS_YAML)
    assert before["passed"] == 0 and before["total"] == len(catalog)
    assert memory_verify_summary(config, DIDS_YAML) == before

    record_verify_results(config, {checkable[0]: "pass"})
    assert memory_verify_summary(config, DIDS_YAML)["passed"] == 1
    record_verify_results(config, {checkable[1]: "fail", checkable[2]: "pass"})
    after = memory_verify_summary(config, DIDS_YAML)
    assert after["passed"] == 2 and after["failed"] == 1


def test_the_verify_summary_is_computed_once_while_nothing_changes(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import src.control.calibration as calibration

    config = _verify_config(tmp_path)
    catalog = tmp_path / "dids.yaml"
    catalog.write_text(DIDS_YAML.read_text())
    _settle(catalog)
    asked: list[str] = []
    real = is_memory_checkable

    def counting(channel: MemoryChannel, memory: Sequence[MemoryChannel]) -> bool:
        asked.append(channel.name)
        return real(channel, memory)

    monkeypatch.setattr(calibration, "is_memory_checkable", counting)

    first = memory_verify_summary(config, catalog)
    after_first = len(asked)
    assert after_first > 0
    assert memory_verify_summary(config, catalog) == first
    assert len(asked) == after_first


def test_a_caller_cannot_corrupt_the_cached_verify_summary(tmp_path: Path) -> None:
    config = _verify_config(tmp_path)
    summary = memory_verify_summary(config, DIDS_YAML)
    summary["passed"] = 999
    assert memory_verify_summary(config, DIDS_YAML)["passed"] == 0


def test_a_missing_catalog_still_raises(tmp_path: Path) -> None:
    config = _verify_config(tmp_path)
    with pytest.raises(FileNotFoundError):
        memory_verify_summary(config, tmp_path / "absent.yaml")


# ---------------------------------------------------------------------------
# The newest capture
# ---------------------------------------------------------------------------


def _capture(root: Path, name: str, profile: str, *, settled: bool = True) -> Path:
    session = root / name
    session.mkdir(parents=True)
    manifest = session / "capture_profile.json"
    manifest.write_text(
        json.dumps(
            {
                "schema_version": 2,
                "profile": profile,
                "started_at": "2026-09-21T18:00:00+00:00",
                "profile_snapshot": {},
            }
        )
    )
    if settled:
        _settle(manifest)
    return session


def test_the_newest_capture_is_read_once_while_nothing_changes(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import src.storage.capture_manifest as capture_manifest

    _capture(tmp_path, "_capture-a-2026-09-20T180000", "older")
    _capture(tmp_path, "_capture-a-2026-09-21T180000", "newest")
    (tmp_path / "2026-09-22T090000").mkdir()  # a plain session: not a capture
    reads: list[Path] = []
    real = capture_manifest.read_capture_manifest

    def counting(session: Path) -> CaptureManifest | None:
        reads.append(session)
        return real(session)

    monkeypatch.setattr(capture_manifest, "read_capture_manifest", counting)

    first = capture_manifest.latest_capture_summary(tmp_path)
    assert first is not None and first["name"] == "newest"
    after_first = len(reads)
    assert after_first == 3  # every directory once, the first time
    for _ in range(5):
        assert capture_manifest.latest_capture_summary(tmp_path) == first
    assert len(reads) == after_first


def test_a_capture_stopping_shows_on_the_next_call(tmp_path: Path) -> None:
    from src.storage.capture_manifest import latest_capture_summary, stamp_capture_end

    session = _capture(tmp_path, "_capture-a-2026-09-21T180000", "tuning")
    running = latest_capture_summary(tmp_path)
    assert running is not None and running["end_reason"] is None
    assert latest_capture_summary(tmp_path) == running  # settled and cached

    stamp_capture_end(session, "operator_stop")  # rewrites the manifest in place
    stopped = latest_capture_summary(tmp_path)
    assert stopped is not None and stopped["end_reason"] == "operator_stop"


def test_a_quality_verdict_written_after_the_stop_shows_on_the_next_call(tmp_path: Path) -> None:
    from src.storage.capture_manifest import latest_capture_summary, write_capture_quality

    session = _capture(tmp_path, "_capture-a-2026-09-21T180000", "tuning")
    assert latest_capture_summary(tmp_path)["quality_verdict"] is None  # type: ignore[index]
    write_capture_quality(session, {"schema_version": 2, "verdict": "usable"})
    assert latest_capture_summary(tmp_path)["quality_verdict"] == "usable"  # type: ignore[index]


def test_a_newer_capture_replaces_the_cached_one(tmp_path: Path) -> None:
    from src.storage.capture_manifest import latest_capture_summary

    _capture(tmp_path, "_capture-a-2026-09-20T180000", "first")
    first = latest_capture_summary(tmp_path)
    assert first is not None and first["name"] == "first"
    _capture(tmp_path, "_capture-a-2026-09-21T180000", "second")
    second = latest_capture_summary(tmp_path)
    assert second is not None and second["name"] == "second"


def test_no_captures_and_no_root_are_none_and_not_cached_wrongly(tmp_path: Path) -> None:
    from src.storage.capture_manifest import latest_capture_summary

    assert latest_capture_summary(tmp_path / "absent") is None
    assert latest_capture_summary(tmp_path) is None
    _capture(tmp_path, "_capture-a-2026-09-21T180000", "arrived")
    summary = latest_capture_summary(tmp_path)
    assert summary is not None and summary["name"] == "arrived"


def test_a_caller_cannot_corrupt_the_cached_capture_summary(tmp_path: Path) -> None:
    from src.storage.capture_manifest import latest_capture_summary

    _capture(tmp_path, "_capture-a-2026-09-21T180000", "tuning")
    summary = latest_capture_summary(tmp_path)
    assert summary is not None
    summary["name"] = "tampered"
    again = latest_capture_summary(tmp_path)
    assert again is not None and again["name"] == "tuning"
