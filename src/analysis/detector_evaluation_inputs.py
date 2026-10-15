"""Phase one of a detector evaluation: the request, the artifacts, the key inputs (A02.4).

``scripts/evaluate_detectors.py`` owns the command line; everything below it is
a library that is handed an :class:`EvaluationRequest` -- the parsed
arguments, resolved to paths and values, with no ``argparse.Namespace`` in
sight -- and returns one frozen phase result per step:

1. :func:`prepare_inputs` -> :class:`PreparedInputs`: the persistence gate,
   every file a score-cache key is computed from, hashed once before anything
   loads, the artifacts and what each one has seen.
2. :func:`prepare_behavior` -> :class:`BehaviorInputs`: the behavior models the
   rows are featurized with, refusing an IF whose columns they cannot produce.
3. :func:`~src.analysis.detector_evaluation_plan.plan_drives`, then loading,
   scoring and aggregation (:mod:`src.analysis.detector_evaluation_run`), and
   :func:`~src.analysis.detector_evaluation_report.build_report`.

A refusal is an :class:`EvaluationRefusedError` carrying the stderr lines and the
exit status; the library never prints or exits. Extracted from
``scripts/evaluate_detectors.py`` (R15.4).
"""

from __future__ import annotations

import json
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any

from src.behavior.behavior_models import BehaviorModels, load_behavior_models
from src.config import CanaryConfig
from src.decode.bindings import Bindings, load_bindings
from src.models.benchmark_gate import load_sequence_artifact
from src.models.evaluation import PersistenceSpec
from src.models.evaluation_heldout import record_seen
from src.models.evaluation_identity import KeySources, file_sha256, key_inputs
from src.models.isolation_forest import TrainedIsolationForest, load_isolation_forest_model
from src.models.sequence_protocol import TrainedSequenceModel
from src.models.spec_producibility import (
    BEHAVIOR_PROVENANCE_ABSENT,
    behavior_fingerprint_provenance,
    manifest_behavior_fingerprints,
)
from src.plausibility.limits import plausibility_path


class EvaluationRefusedError(Exception):
    """The evaluation will not run, and why.

    ``lines`` are the stderr lines the caller prints, in order; ``exit_code``
    is the status it exits with. Raised instead of printing so a library never
    writes to the terminal or decides an exit status.
    """

    def __init__(self, lines: Sequence[str], *, exit_code: int = 2) -> None:
        super().__init__("; ".join(lines))
        self.lines = tuple(lines)
        self.exit_code = exit_code


@dataclass(frozen=True)
class EvaluationRequest:
    """Everything one evaluation is told, resolved: no defaults are decided below.

    ``config`` is already profile-scoped (P02.11): featurization, the
    plausibility limits and the tune registry read the vehicle's data
    directory, so the request carries the scoped config and never the raw one.
    """

    config: CanaryConfig
    config_path: Path
    #: ``data/<slug>/``: the vehicle's labels, tune registry and fault roles.
    data_dir: Path
    if_path: Path
    sequence_path: Path
    use_sequence: bool
    behavior_path: Path
    bindings_path: Path
    #: Explicit session directories; ``sessions_roots`` are searched when empty.
    sessions: tuple[Path, ...]
    sessions_roots: tuple[Path, ...]
    labels_path: Path
    #: An explicit ``--tune-revisions`` file; ``None`` reads the profile's.
    tune_revisions_path: Path | None
    fault_roles_path: Path
    curated_channels_path: Path
    baselines: tuple[str, ...]
    baseline_fit_sessions: int
    faults: tuple[str, ...]
    injections_per_drive: int
    window_seconds: float
    lead_seconds: float
    grace_seconds: float
    cache_dir: Path
    use_score_cache: bool
    derived_dir: Path | None
    workers: int
    scoring_threads: int
    seed: int
    parity_rows: int
    budget: float
    since: datetime | None
    until: datetime | None
    cache_only: bool
    shift_test: bool
    shift_rows: int
    #: Source files outside ``src/`` the code identity also covers (the CLI).
    harness_files: tuple[Path, ...]


@dataclass(frozen=True)
class PreparedInputs:
    """The artifacts and key inputs, hashed before anything else loads."""

    persistence: PersistenceSpec
    key_sources: KeySources
    #: Each key input by name; the earliest hashes win (see :func:`prepare_inputs`).
    inputs: dict[str, str | None]
    bindings: Bindings | None
    trained_if: TrainedIsolationForest | None
    trained_seq: TrainedSequenceModel | None
    artifacts: dict[str, Any]
    #: ``session -> {artifact label: role}``: what each artifact trained,
    #: calibrated or chose its alert level on (A02.3).
    seen: dict[str, dict[str, str]]

    @property
    def reference(self) -> TrainedSequenceModel | TrainedIsolationForest | None:
        """The artifact the baselines follow: the sequence model, else the IF."""
        return self.trained_seq if self.trained_seq is not None else self.trained_if


def prepare_inputs(request: EvaluationRequest, early: Mapping[str, str | None]) -> PreparedInputs:
    """Hash every score-cache key input, then load the artifacts.

    ``early`` holds the code and config-file hashes the CLI took before the
    config was parsed or any module did work, so the key they give is what the
    run executes; ``changed_key_inputs`` re-checks both later.
    """
    config = request.config
    persistence = PersistenceSpec.from_config(config)
    # P02.5: every score-cache key input is hashed here, before anything is
    # loaded; the run's entries are committed only if they still hash so
    # after scoring (``changed_key_inputs``).
    key_sources = KeySources(
        config_path=request.config_path.resolve(),
        profile_dir=request.data_dir,
        # The AFR calibration featurization reads and the limits the
        # plausibility detector reads, both from the scoped config: the files
        # the run actually loads.
        extra_data=(
            config.daemon.afr_calibration.resolve(),
            plausibility_path(config).resolve(),
        ),
        if_path=request.if_path,
        sequence_path=None if not request.use_sequence else request.sequence_path,
        bindings_path=request.bindings_path,
        behavior_path=request.behavior_path,
        harness_files=request.harness_files,
    )
    # The earliest hashes win: a change after them is caught by the re-checks.
    inputs = {**key_inputs(key_sources), **early}
    bindings = load_bindings(request.bindings_path) if request.bindings_path.exists() else None

    trained_if: TrainedIsolationForest | None = (
        load_isolation_forest_model(request.if_path) if request.if_path.exists() else None
    )
    trained_seq: TrainedSequenceModel | None = None
    if request.use_sequence and request.sequence_path.exists():
        # The backend the artifact's manifest names (LSTM or TCN), not a
        # hard-coded LSTM loader (R06.5: retrain's gate scores either).
        trained_seq = load_sequence_artifact(request.sequence_path)
    artifacts: dict[str, Any] = {}
    seen: dict[str, dict[str, str]] = {}
    for label, path, manifest in (
        ("isolation_forest", request.if_path, trained_if.manifest if trained_if else None),
        ("sequence", request.sequence_path, trained_seq.manifest if trained_seq else None),
    ):
        if manifest is None:
            continue
        artifacts[label] = {
            "path": str(path),
            "sha256": file_sha256(path),
            "manifest_sha256": file_sha256(path.with_name("model.manifest.json")),
            "model_version": manifest.model_version,
        }
        record_seen(seen, label, manifest)
    return PreparedInputs(
        persistence, key_sources, inputs, bindings, trained_if, trained_seq, artifacts, seen
    )


@dataclass(frozen=True)
class BehaviorInputs:
    """The behavior models the rows are featurized with, and the report's record of them."""

    behavior: BehaviorModels | None
    record: dict[str, Any]


def behavior_record(
    path: Path,
    behavior: BehaviorModels | None,
    trained_if: TrainedIsolationForest | None,
    if_path: Path,
) -> dict[str, Any]:
    """Which behavior models the rows were featurized with, and whether each
    one the IF consumes is the artifact it was trained against (B03.4)."""
    provenance: dict[str, dict[str, Any]] = {}
    if trained_if is not None:
        try:
            manifest = json.loads(if_path.with_name("model.manifest.json").read_text())
        except (OSError, json.JSONDecodeError):
            manifest = {}
        provenance = behavior_fingerprint_provenance(
            trained_if.feature_columns, manifest_behavior_fingerprints(manifest), behavior
        )
    return {
        "path": str(path),
        "loaded": behavior is not None,
        "sha256": file_sha256(path) if behavior is not None else None,
        "provenance": provenance,
    }


def prepare_behavior(request: EvaluationRequest, prepared: PreparedInputs) -> BehaviorInputs:
    """Load the behavior models; refuse an IF whose ``deviation:*`` columns they cannot produce."""
    path = request.behavior_path
    behavior = load_behavior_models(path) if path.exists() else None
    record = behavior_record(path, behavior, prepared.trained_if, request.if_path)
    absent = {
        name: entry["reason"]
        for name, entry in record["provenance"].items()
        if entry["status"] == BEHAVIOR_PROVENANCE_ABSENT
    }
    if absent:
        # The IF would be scored with those columns empty: not the detector
        # the daemon serves. Refuse rather than report its numbers.
        raise EvaluationRefusedError([f"error: {reason} ({path})" for reason in absent.values()])
    return BehaviorInputs(behavior, record)
