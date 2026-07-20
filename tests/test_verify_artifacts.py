"""Tests for the deploy-skew check CLI (Task 6.6).

Local-vs-local comparison (`--compare-dir`) is fully exercised here — no
network involved. The `--remote` ssh path is tested with a mocked/faked
subprocess runner (`collect_remote(..., runner=fake)`); the real
`canary@192.168.137.2` round trip is untested tonight (laptop-only, per
OVERNIGHT-QUEUE.md's Q12 "code yes, remote test blocked" framing).
"""

from __future__ import annotations

import json
import subprocess
from pathlib import Path

import pytest

from scripts.verify_artifacts import (
    ArtifactInfo,
    collect_local,
    collect_remote,
    compare,
    main,
)
from src.models.artifact_hash import sha256_file


def _write_model(
    models_dir: Path,
    name: str,
    *,
    artifact_name: str = "model.joblib",
    model_version: str = "v1",
    training_data_hash: str = "sha256:abc",
    artifact_bytes: bytes = b"fake-artifact-bytes",
) -> Path:
    model_dir = models_dir / name
    model_dir.mkdir(parents=True, exist_ok=True)
    (model_dir / artifact_name).write_bytes(artifact_bytes)
    manifest_path = model_dir / "model.manifest.json"
    manifest_path.write_text(
        json.dumps(
            {
                "artifact_format_version": 1,
                "model_type": name,
                "model_version": model_version,
                "created_at": "2026-07-19T00:00:00Z",
                "training_data_hash": training_data_hash,
                "input_rows": 100,
                "hyperparams": {},
                "schema": {},
            }
        )
    )
    return manifest_path


# ---------------------------------------------------------------------------
# collect_local
# ---------------------------------------------------------------------------


def test_collect_local_reads_manifest_and_hashes_artifact(tmp_path: Path) -> None:
    models_dir = tmp_path / "models"
    _write_model(models_dir, "isolation_forest", artifact_name="model.joblib")
    info = collect_local(models_dir)
    assert set(info) == {"isolation_forest"}
    entry = info["isolation_forest"]
    assert entry.model_version == "v1"
    assert entry.training_data_hash == "sha256:abc"
    assert entry.artifact_sha256 == sha256_file(models_dir / "isolation_forest" / "model.joblib")


def test_collect_local_empty_dir(tmp_path: Path) -> None:
    assert collect_local(tmp_path / "nope") == {}


def test_collect_local_skips_unreadable_manifest(tmp_path: Path) -> None:
    models_dir = tmp_path / "models"
    (models_dir / "broken").mkdir(parents=True)
    (models_dir / "broken" / "model.manifest.json").write_text("{not json")
    assert collect_local(models_dir) == {}


# ---------------------------------------------------------------------------
# compare — the LOCAL-only path (matching case, mismatching case)
# ---------------------------------------------------------------------------


def test_compare_matching_dirs_has_no_mismatches(tmp_path: Path) -> None:
    a = tmp_path / "a"
    b = tmp_path / "b"
    _write_model(a, "isolation_forest", artifact_bytes=b"same-bytes")
    _write_model(b, "isolation_forest", artifact_bytes=b"same-bytes")
    assert compare(collect_local(a), collect_local(b)) == []


def test_compare_flags_artifact_sha_mismatch(tmp_path: Path) -> None:
    a = tmp_path / "a"
    b = tmp_path / "b"
    _write_model(a, "isolation_forest", artifact_bytes=b"local-bytes")
    _write_model(b, "isolation_forest", artifact_bytes=b"different-remote-bytes")
    mismatches = compare(collect_local(a), collect_local(b))
    assert len(mismatches) == 1
    assert mismatches[0].field == "artifact_sha256"


def test_compare_flags_model_version_and_training_data_hash_mismatch(tmp_path: Path) -> None:
    a = tmp_path / "a"
    b = tmp_path / "b"
    _write_model(a, "lstm_autoencoder", artifact_name="model.pt", model_version="v1")
    _write_model(
        b,
        "lstm_autoencoder",
        artifact_name="model.pt",
        model_version="v2",
        training_data_hash="sha256:different",
    )
    mismatches = compare(collect_local(a), collect_local(b))
    fields = {m.field for m in mismatches}
    # artifact bytes differ too (default is identical bytes, so only the two
    # manifest fields should differ here since artifact_bytes wasn't varied).
    assert "model_version" in fields
    assert "training_data_hash" in fields


def test_compare_flags_model_missing_on_one_side(tmp_path: Path) -> None:
    a = tmp_path / "a"
    b = tmp_path / "b"
    _write_model(a, "isolation_forest")
    _write_model(a, "lstm_autoencoder", artifact_name="model.pt")
    _write_model(b, "isolation_forest")
    mismatches = compare(collect_local(a), collect_local(b))
    assert len(mismatches) == 1
    assert mismatches[0].name == "lstm_autoencoder"
    assert mismatches[0].field == "presence"
    assert mismatches[0].other is None


# ---------------------------------------------------------------------------
# CLI — --compare-dir (local only)
# ---------------------------------------------------------------------------


def test_main_compare_dir_ok(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    a = tmp_path / "a"
    b = tmp_path / "b"
    _write_model(a, "isolation_forest", artifact_bytes=b"same")
    _write_model(b, "isolation_forest", artifact_bytes=b"same")
    rc = main(["--models-dir", str(a), "--compare-dir", str(b)])
    assert rc == 0
    assert "ok:" in capsys.readouterr().out


def test_main_compare_dir_reports_mismatch(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    a = tmp_path / "a"
    b = tmp_path / "b"
    _write_model(a, "isolation_forest", artifact_bytes=b"one")
    _write_model(b, "isolation_forest", artifact_bytes=b"two")
    rc = main(["--models-dir", str(a), "--compare-dir", str(b)])
    assert rc == 1
    out = capsys.readouterr().out
    assert "MISMATCH" in out
    assert "artifact_sha256" in out


def test_main_requires_exactly_one_of_compare_dir_or_remote(tmp_path: Path) -> None:
    with pytest.raises(SystemExit):
        main(["--models-dir", str(tmp_path)])
    with pytest.raises(SystemExit):
        main(["--models-dir", str(tmp_path), "--compare-dir", str(tmp_path), "--remote", "x@y"])


# ---------------------------------------------------------------------------
# collect_remote — mocked ssh subprocess, no real host contact (Task 6.6's
# "remote test blocked" half; this is the offline mock substitute).
# ---------------------------------------------------------------------------


def test_collect_remote_parses_find_cat_sha256sum_over_mocked_ssh() -> None:
    manifest_payload = json.dumps(
        {
            "model_version": "remote_v1",
            "training_data_hash": "sha256:remote-hash",
        }
    )

    calls: list[list[str]] = []

    def fake_runner(cmd: list[str]) -> subprocess.CompletedProcess[str]:
        calls.append(list(cmd))
        assert cmd[0] == "ssh"
        assert cmd[1] == "canary@192.168.137.2"
        remote_cmd = cmd[2]
        if remote_cmd.startswith("find"):
            stdout = "/data/canary/models/isolation_forest/model.manifest.json\n"
        elif remote_cmd.startswith("cat"):
            stdout = manifest_payload
        elif remote_cmd.startswith("sha256sum"):
            # Only the .joblib artifact exists remotely (an IF model) — the
            # .pt attempt (tried first) must come back empty so the code
            # falls through to it.
            path = remote_cmd.split()[-1]
            stdout = f"deadbeefcafe  {path}\n" if path.endswith("model.joblib") else ""
        else:
            raise AssertionError(f"unexpected remote command: {remote_cmd}")
        return subprocess.CompletedProcess(cmd, 0, stdout=stdout, stderr="")

    result = collect_remote("canary@192.168.137.2", "/data/canary/models", runner=fake_runner)
    assert set(result) == {"isolation_forest"}
    entry = result["isolation_forest"]
    assert entry.model_version == "remote_v1"
    assert entry.training_data_hash == "sha256:remote-hash"
    assert entry.artifact_sha256 == "deadbeefcafe"
    assert entry.artifact_path == "/data/canary/models/isolation_forest/model.joblib"
    # ssh was invoked, never scp — and the artifact bytes never crossed the wire.
    assert all(c[0] == "ssh" for c in calls)


def test_collect_remote_returns_empty_when_ssh_listing_fails() -> None:
    def failing_runner(cmd: list[str]) -> subprocess.CompletedProcess[str]:
        raise subprocess.TimeoutExpired(cmd=list(cmd), timeout=20.0)

    assert (
        collect_remote("canary@192.168.137.2", "/data/canary/models", runner=failing_runner) == {}
    )


def test_collect_remote_skips_manifest_with_bad_json() -> None:
    def fake_runner(cmd: list[str]) -> subprocess.CompletedProcess[str]:
        remote_cmd = cmd[2]
        if remote_cmd.startswith("find"):
            return subprocess.CompletedProcess(
                cmd,
                0,
                stdout="/data/canary/models/isolation_forest/model.manifest.json\n",
                stderr="",
            )
        if remote_cmd.startswith("cat"):
            return subprocess.CompletedProcess(cmd, 0, stdout="{not json", stderr="")
        raise AssertionError("sha256sum should not be reached for an unreadable manifest")

    assert collect_remote("canary@192.168.137.2", "/data/canary/models", runner=fake_runner) == {}


def test_end_to_end_local_vs_mocked_remote_via_compare() -> None:
    """Exercises `compare()` against a `collect_remote()` result — the shape
    both sides feed it is identical, so the same diff logic applies whether
    `other` came from a local dir or a (mocked) ssh round trip."""
    local = {
        "isolation_forest": ArtifactInfo(
            name="isolation_forest",
            manifest_path="models/isolation_forest/model.manifest.json",
            artifact_path="models/isolation_forest/model.joblib",
            model_version="v1",
            training_data_hash="sha256:same",
            artifact_sha256="aaaa",
        )
    }

    def fake_runner(cmd: list[str]) -> subprocess.CompletedProcess[str]:
        remote_cmd = cmd[2]
        if remote_cmd.startswith("find"):
            stdout = "/data/canary/models/isolation_forest/model.manifest.json\n"
        elif remote_cmd.startswith("cat"):
            stdout = json.dumps({"model_version": "v1", "training_data_hash": "sha256:same"})
        else:
            stdout = "bbbb  /data/canary/models/isolation_forest/model.joblib\n"
        return subprocess.CompletedProcess(cmd, 0, stdout=stdout, stderr="")

    remote = collect_remote("canary@192.168.137.2", "/data/canary/models", runner=fake_runner)
    mismatches = compare(local, remote)
    assert len(mismatches) == 1
    assert mismatches[0].field == "artifact_sha256"
    assert mismatches[0].local == "aaaa"
    assert mismatches[0].other == "bbbb"
