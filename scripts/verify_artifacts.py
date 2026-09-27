#!/usr/bin/env python3
"""Deploy-skew check (Task 6.6).

Model artifacts are gitignored — they travel to the Pi by `scp`, not `git
pull` (AGENTS.md), and the scp step has no verification of its own today: a
partial copy or a stale artifact left over from a prior deploy looks fine
until the daemon logs something weird. This makes that path self-diagnosing:
compare each model's artifact sha256 + manifest `model_version`/
`training_data_hash` between two copies of a models directory and report any
mismatch.

Two comparison modes (exactly one required):

* `--compare-dir <path>` — compare this checkout's models directory against
  another *local* directory. No network involved; this is what the test
  suite exercises and it's also useful for comparing a local backup/checkout
  without touching the Pi.
* `--remote <user@host>` [`--remote-dir <path>`] — fetch the remote side's
  manifests + artifact sha256 over `ssh` (`find` the manifests, `cat` each
  one, `sha256sum` the paired artifact). The artifact itself is never
  copied — only its 64-hex-char digest crosses the network.

The real Pi is `canary@192.168.137.2` (AGENTS.md's documented deploy
address) — that's the intended value for `--remote`, not shown here as a
default so a bare invocation never talks to the network unprompted.

Usage:
    scripts/verify_artifacts.py --remote canary@192.168.137.2
    scripts/verify_artifacts.py --compare-dir /path/to/other/models
"""

from __future__ import annotations

import argparse
import json
import logging
import shlex
import subprocess
import sys
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from typing import Any

try:
    import _cli  # noqa: F401  (bootstraps repo root onto sys.path)
except ImportError:  # loaded as `scripts.verify_artifacts` — repo root on path, not scripts/
    import scripts._cli as _cli  # type: ignore[no-redef]  # noqa: F401

from src.artifact_hash import sha256_file  # noqa: E402

log = logging.getLogger(__name__)

DEFAULT_MODELS_DIR = Path("models")
DEFAULT_REMOTE_DIR = "/data/canary/models"
SSH_TIMEOUT_SECONDS = 20.0
#: Same two on-disk artifact filenames every trainer writes today (see
#: `src.models.manager.default_manifest_sidecar` for the manifest side).
_ARTIFACT_FILENAMES = ("model.pt", "model.joblib")

#: Manifest fields compared between the two sides — kept in sync with the
#: task's "artifact file sha256 + manifest model_version/training_data_hash".
_COMPARED_FIELDS = ("model_version", "training_data_hash", "artifact_sha256")


@dataclass(frozen=True)
class ArtifactInfo:
    """One model's identity as seen from one side (local or remote)."""

    name: str
    manifest_path: str
    artifact_path: str | None
    model_version: str | None
    training_data_hash: str | None
    artifact_sha256: str | None


@dataclass(frozen=True)
class Mismatch:
    """One field that differs (or is missing) between the two sides."""

    name: str
    field: str
    local: Any
    other: Any

    def __str__(self) -> str:
        return f"{self.name}.{self.field}: local={self.local!r} other={self.other!r}"


# ---------------------------------------------------------------------------
# Local side
# ---------------------------------------------------------------------------


def _manifest_name(manifest_path: Path, models_dir: Path) -> str:
    """Same naming convention as `dashboard/status.py::build_status`: the
    manifest's parent directory name, or its own stem when it sits directly
    in `models_dir` (the flat `<name>.manifest.json` layout)."""
    return (
        manifest_path.stem.removesuffix(".manifest")
        if manifest_path.parent == models_dir
        else manifest_path.parent.name
    )


def _local_artifact_path(manifest_path: Path) -> Path | None:
    for candidate in _ARTIFACT_FILENAMES:
        artifact = manifest_path.parent / candidate
        if artifact.exists():
            return artifact
    return None


def collect_local(models_dir: Path) -> dict[str, ArtifactInfo]:
    """Every model manifest found under `models_dir` (recursive)."""
    out: dict[str, ArtifactInfo] = {}
    if not models_dir.is_dir():
        return out
    for manifest_path in sorted(models_dir.rglob("*.manifest.json")):
        name = _manifest_name(manifest_path, models_dir)
        try:
            payload = json.loads(manifest_path.read_text())
        except (OSError, json.JSONDecodeError) as exc:
            log.warning("skipping unreadable manifest %s: %s", manifest_path, exc)
            continue
        artifact_path = _local_artifact_path(manifest_path)
        out[name] = ArtifactInfo(
            name=name,
            manifest_path=str(manifest_path),
            artifact_path=str(artifact_path) if artifact_path else None,
            model_version=payload.get("model_version"),
            training_data_hash=payload.get("training_data_hash"),
            artifact_sha256=sha256_file(artifact_path) if artifact_path else None,
        )
    return out


# ---------------------------------------------------------------------------
# Remote side — ssh only, never scp's the artifact itself
# ---------------------------------------------------------------------------

SshRunner = Callable[[Sequence[str]], "subprocess.CompletedProcess[str]"]


def _default_ssh_runner(cmd: Sequence[str]) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        cmd, capture_output=True, text=True, timeout=SSH_TIMEOUT_SECONDS, check=True
    )


def collect_remote(
    remote: str,
    remote_dir: str,
    *,
    runner: SshRunner = _default_ssh_runner,
) -> dict[str, ArtifactInfo]:
    """Fetch manifests + artifact sha256 from `remote` over ssh.

    Three kinds of ssh round trip: list manifest paths (`find`), read each
    manifest (`cat`), hash the paired artifact remotely (`sha256sum`) — the
    digest is 64 hex chars, so this never transfers the (possibly
    multi-hundred-MB) artifact itself. `runner` is injectable so tests can
    fake the ssh round trip without touching a real host.
    """
    out: dict[str, ArtifactInfo] = {}
    find_cmd = ["ssh", remote, f"find {shlex.quote(remote_dir)} -name model.manifest.json"]
    try:
        find_result = runner(find_cmd)
    except (OSError, subprocess.SubprocessError) as exc:
        log.error("could not list remote manifests on %s: %s", remote, exc)
        return out

    remote_dir_posix = PurePosixPath(remote_dir.rstrip("/") or "/")
    manifest_paths = [line.strip() for line in find_result.stdout.splitlines() if line.strip()]
    for manifest_path in sorted(manifest_paths):
        try:
            cat_result = runner(["ssh", remote, f"cat {shlex.quote(manifest_path)}"])
            payload = json.loads(cat_result.stdout)
        except (OSError, subprocess.SubprocessError, json.JSONDecodeError) as exc:
            log.warning("skipping unreadable remote manifest %s: %s", manifest_path, exc)
            continue

        manifest_posix = PurePosixPath(manifest_path)
        manifest_dir = str(manifest_posix.parent)
        # Same "flat vs nested" naming convention as the local side.
        name = (
            manifest_posix.stem.removesuffix(".manifest")
            if manifest_posix.parent == remote_dir_posix
            else manifest_posix.parent.name
        )
        artifact_path: str | None = None
        artifact_sha256: str | None = None
        for candidate in _ARTIFACT_FILENAMES:
            remote_artifact = f"{manifest_dir}/{candidate}"
            try:
                sha_result = runner(["ssh", remote, f"sha256sum {shlex.quote(remote_artifact)}"])
            except (OSError, subprocess.SubprocessError):
                continue
            fields = sha_result.stdout.split()
            if fields:
                artifact_path = remote_artifact
                artifact_sha256 = fields[0]
                break

        out[name] = ArtifactInfo(
            name=name,
            manifest_path=manifest_path,
            artifact_path=artifact_path,
            model_version=payload.get("model_version"),
            training_data_hash=payload.get("training_data_hash"),
            artifact_sha256=artifact_sha256,
        )
    return out


# ---------------------------------------------------------------------------
# Comparison
# ---------------------------------------------------------------------------


def compare(local: dict[str, ArtifactInfo], other: dict[str, ArtifactInfo]) -> list[Mismatch]:
    """Every mismatch between `local` and `other` — missing models on either
    side count as one mismatch each; present-on-both models are diffed field
    by field over `_COMPARED_FIELDS`."""
    mismatches: list[Mismatch] = []
    for name in sorted(set(local) | set(other)):
        local_info = local.get(name)
        other_info = other.get(name)
        if local_info is None:
            mismatches.append(Mismatch(name, "presence", None, "present"))
            continue
        if other_info is None:
            mismatches.append(Mismatch(name, "presence", "present", None))
            continue
        for field in _COMPARED_FIELDS:
            local_value = getattr(local_info, field)
            other_value = getattr(other_info, field)
            if local_value != other_value:
                mismatches.append(Mismatch(name, field, local_value, other_value))
    return mismatches


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------


def main(argv: Sequence[str] | None = None) -> int:
    logging.basicConfig(level=logging.INFO, format="%(message)s")
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    parser.add_argument(
        "--models-dir",
        type=Path,
        default=DEFAULT_MODELS_DIR,
        help="Local models directory (default: models/).",
    )
    parser.add_argument(
        "--compare-dir",
        type=Path,
        default=None,
        help="Compare against this local directory instead of a remote host.",
    )
    parser.add_argument(
        "--remote",
        default=None,
        help="ssh host to compare against, e.g. canary@192.168.137.2.",
    )
    parser.add_argument(
        "--remote-dir",
        default=DEFAULT_REMOTE_DIR,
        help=f"Remote models directory (default: {DEFAULT_REMOTE_DIR}).",
    )
    args = parser.parse_args(argv)

    if (args.compare_dir is None) == (args.remote is None):
        parser.error("exactly one of --compare-dir or --remote is required")

    local = collect_local(args.models_dir)
    if args.compare_dir is not None:
        other = collect_local(args.compare_dir)
        other_label = str(args.compare_dir)
    else:
        other = collect_remote(args.remote, args.remote_dir)
        other_label = f"{args.remote}:{args.remote_dir}"

    mismatches = compare(local, other)
    if not mismatches:
        print(f"ok: {len(local)} model(s) match against {other_label}")
        return 0

    print(f"{len(mismatches)} mismatch(es) vs {other_label}:")
    for mismatch in mismatches:
        print(f"  MISMATCH {mismatch}")
    return 1


if __name__ == "__main__":
    sys.exit(main())
