"""What a cached score is keyed on (P02.5).

Every file a score-cache key is computed from is named by
:class:`KeySources` and hashed by :func:`key_inputs`: once, before anything is
loaded, then again by each scoring worker before it loads and by the parent
after scoring. The parent commits the run's staged entries only if nothing
moved, so a score computed from bytes another session wrote mid-run is never
kept under a key computed from the bytes before (or the reverse).

The code hash itself is :func:`src.models.evaluation_cache.code_version`: every
``src/**/*.py`` plus the library versions plus ``KeySources.harness_files``
(the CLI scripts, which stay outside ``src/``). Moving source between modules
therefore changes the code identity and invalidates cached scores once; that
is the expected, recorded effect of a refactor and never hidden here.
Extracted from ``scripts/evaluate_detectors.py`` (R15.5); that script
re-exports these names.
"""

from __future__ import annotations

import os
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from src.artifact_hash import sha256_file_or_none
from src.config import CanaryConfig
from src.models.alert_budget import CADENCE_KEY
from src.models.evaluation_cache import code_version, digest
from src.paths import DATA_DIR

#: R01.6: the one definition is ``src.artifact_hash.sha256_file_or_none``.
file_sha256 = sha256_file_or_none


def _thread_environment() -> dict[str, Any]:
    """What sets the thread counts a score was computed at: torch's pool in
    effect and the BLAS pool's defaults. Both move scores in the last bits,
    so a score computed at other counts is not served (P02.5)."""
    try:
        import torch

        torch_threads: int | None = torch.get_num_threads()
    except ImportError:
        torch_threads = None
    return {
        "torch_threads": torch_threads,
        "cpu_count": os.cpu_count(),
        **{
            name: os.environ.get(name)
            for name in ("OMP_NUM_THREADS", "OPENBLAS_NUM_THREADS", "MKL_NUM_THREADS")
        },
    }


def _data_inputs_digest(config_path: Path, profile_dir: Path, extra: Sequence[Path] = ()) -> str:
    """The data files a score can depend on besides the artifacts: the
    config, the repo catalogs, the DBCs, the profile's own files and
    ``extra`` (catalogs a config may point outside ``data/``). Hashed
    wholesale (a hand-kept list is a thing to forget to edit)."""
    files = [config_path, *sorted(DATA_DIR.glob("*.yaml")), *sorted((DATA_DIR / "dbc").glob("*"))]
    files += sorted(p for p in profile_dir.glob("*") if p.suffix in (".yaml", ".yml", ".json"))
    files += sorted(set(extra))
    return digest(*[(str(p), file_sha256(p)) for p in files if p.is_file()])


@dataclass(frozen=True)
class KeySources:
    """Every file a score-cache key is computed from (P02.5).

    :func:`key_inputs` hashes them once, before anything is loaded; scoring
    workers re-hash them before they load, and the parent re-hashes them
    after scoring and commits the run's entries only if nothing moved. So a
    score computed from bytes another session wrote mid-run can never be
    kept under a key computed from the bytes before (or the reverse).
    """

    config_path: Path
    profile_dir: Path
    extra_data: tuple[Path, ...]
    if_path: Path | None
    sequence_path: Path | None
    bindings_path: Path
    behavior_path: Path
    harness_files: tuple[Path, ...]


def sequence_identity(artifact_input: str | None, config: CanaryConfig) -> str:
    """The sequence component's score-cache identity: the artifact (with its
    manifest) and the forward cadence it is graded at (A06.6). A cache scored
    at one cadence is never read back at another."""
    return f"{artifact_input}|{CADENCE_KEY}={config.models.sequence_forward_every_n_ticks}"


def _artifact_input(path: Path | None) -> str | None:
    if path is None or not path.exists():
        return None
    return digest(file_sha256(path), file_sha256(path.with_name("model.manifest.json")))


def key_inputs(sources: KeySources) -> dict[str, str | None]:
    """Each key input by name: the code, each artifact (with its manifest),
    the bindings, the behavior models and the data catalogs."""
    return {
        "code": code_version(sources.harness_files),
        "config_file": (
            file_sha256(sources.config_path) if sources.config_path.is_file() else None
        ),
        "isolation_forest": _artifact_input(sources.if_path),
        "sequence": _artifact_input(sources.sequence_path),
        "bindings": file_sha256(sources.bindings_path) if sources.bindings_path.exists() else None,
        "behavior_models": (
            file_sha256(sources.behavior_path) if sources.behavior_path.exists() else None
        ),
        "data": _data_inputs_digest(sources.config_path, sources.profile_dir, sources.extra_data),
    }


def changed_key_inputs(sources: KeySources, expected: Mapping[str, str | None]) -> list[str]:
    """The names of the key inputs whose files no longer hash as ``expected``."""
    now = key_inputs(sources)
    return [name for name, value in now.items() if expected.get(name) != value]


def scoring_feature_key(code: str, config: CanaryConfig, inputs: Mapping[str, str | None]) -> str:
    """The part of every unit key that names how rows are featurized and scored:
    the code, the thread environment, the full config, the behavior models, the
    bindings and the data catalogs (P02.5)."""
    return digest(
        "features",
        code,
        _thread_environment(),
        config.model_dump(mode="json"),
        inputs["behavior_models"],
        inputs["bindings"],
        inputs["data"],
    )
