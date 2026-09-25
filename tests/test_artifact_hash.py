"""R01.6: one streaming file sha256, with a hash-if-present form."""

from __future__ import annotations

import hashlib
from pathlib import Path

from src.models.artifact_hash import sha256_file, sha256_file_or_none


def test_streaming_hash_matches_hashlib(tmp_path: Path) -> None:
    path = tmp_path / "artifact.bin"
    data = bytes(range(256)) * 9000  # spans several 1 MiB chunks' worth of reads
    path.write_bytes(data)

    assert sha256_file(path, chunk_size=4096) == hashlib.sha256(data).hexdigest()
    assert sha256_file_or_none(path) == hashlib.sha256(data).hexdigest()


def test_or_none_for_absent_directory_and_none(tmp_path: Path) -> None:
    assert sha256_file_or_none(None) is None
    assert sha256_file_or_none(tmp_path / "missing") is None
    assert sha256_file_or_none(tmp_path) is None


def test_every_old_name_is_the_one_function() -> None:
    import importlib

    from src.models import alert_budget

    assert alert_budget.file_sha256 is sha256_file
    for module, name in (
        ("scripts.retrain", "_file_sha256"),
        ("scripts.evaluate_detectors", "file_sha256"),
        ("scripts.benchmark_live_pipeline", "_sha256"),
    ):
        assert getattr(importlib.import_module(module), name) is sha256_file_or_none
