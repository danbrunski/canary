"""Training-run ledger — the source of truth for training history (Task 6.21).

The Isolation Forest, LSTM, and behavior-model trainers each already write a
manifest next to their artifact. Those manifests are *per-artifact*; what the
training dashboard (6.22), the false-positive feedback loop (6.24/6.26), and
the companion publish queue (6.37) need is a *cross-run* index: one structured
record per training job, carrying everything needed to explain or roll back a
model — input sessions, feature schema, hyperparameters, loss/threshold/
validation/false-positive metrics, the produced artifacts, the job log, and
the publish status.

Storage mirrors the anomaly + job ledgers: append-only JSONL under
``data/control/`` (gitignored). "Append-only" means a status change (e.g.
``unpublished`` → ``active``) appends a new line for the same ``run_id``;
:meth:`TrainingLedger.records` collapses to the latest line per run so the
provenance chain is preserved (Feedback-lineage cross-cutting concern) while
readers see current state.

:func:`build_run_record_from_manifest` adapts a trainer manifest (read through
the shared :func:`validate_manifest_common`) into a record, so the trainers
don't need to know about the ledger — a thin post-train hook ingests the
manifest they already wrote.
"""

from __future__ import annotations

import json
import logging
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Literal
from uuid import uuid4

from pydantic import BaseModel, ConfigDict, Field

from src.artifact_hash import sha256_file
from src.jsonl_store import JsonlStore, latest_by_key
from src.models.manifest import ManifestValidationError, validate_manifest_common
from src.paths import CONTROL_DIR

log = logging.getLogger(__name__)

__all__ = [
    "DEFAULT_TRAINING_LEDGER_PATH",
    "PublishStatus",
    "TrainingLedger",
    "TrainingRunRecord",
    "build_run_record_from_manifest",
]

#: Mirror of ``DaemonConfig.training_ledger`` (R01.2: ``src.paths.CONTROL_DIR``).
DEFAULT_TRAINING_LEDGER_PATH = CONTROL_DIR / "training_runs.jsonl"

PublishStatus = Literal["unpublished", "published", "active", "rolled_back"]
RunStatus = Literal["succeeded", "failed"]


class TrainingRunRecord(BaseModel):
    """One training run. JSON-safe; the unit the ledger stores."""

    # protected_namespaces=() so `model_type`/`model_version` don't collide
    # with Pydantic's reserved `model_` namespace.
    model_config = ConfigDict(extra="forbid", protected_namespaces=())

    run_id: str
    model_type: str
    model_version: str | None = None
    created_at: str
    status: RunStatus
    input_sessions: list[str] = Field(default_factory=list)
    training_start_timestamp: str | None = None
    training_end_timestamp: str | None = None
    input_rows: int | None = None
    training_data_hash: str | None = None
    feature_schema: list[str] = Field(default_factory=list)
    hyperparams: dict[str, Any] = Field(default_factory=dict)
    #: Free-form: ``loss_curve``, ``thresholds``, ``validation``,
    #: ``false_positive_benchmark``, ``modes``, and the raw manifest extras.
    metrics: dict[str, Any] = Field(default_factory=dict)
    artifacts: dict[str, str] = Field(default_factory=dict)
    #: sha256 of ``artifacts["model"]`` at train time (Task 6.6) — the
    #: deploy-skew CLI (``scripts/verify_artifacts.py``) recomputes this on
    #: both the local and Pi copies and compares against what shipped here.
    artifact_sha256: str | None = None
    job_id: str | None = None
    job_log_path: str | None = None
    #: `data/analyses/benchmark-<ts>.json`: retrain's post-train regression gate
    #: (Task 2.6) against this run's artifact, when the retrain flow ran one — an
    #: ``scripts/evaluate_detectors.py`` report since R06.5 (older rows may point
    #: at a retired-grid report, which the promotion gate refuses by name). The
    #: report itself carries the baseline diff. Name kept for ledger compatibility.
    detector_benchmark_report_path: str | None = None
    #: Task 1.4 (online-training-strategy.md) — the disposition screen's
    #: per-run summary (``training_disposition.DispositionSummary.to_dict()``:
    #: per-``(channel, operating_mode)`` Reject/Accept-recenter/Quarantine/
    #: Accept counts + corpus-wide totals), when this run's input was passed
    #: through the disposition pipeline. ``None`` for runs that predate the
    #: guard model or ran without it (e.g. a schema-only trainer test) —
    #: absence is not itself a red flag.
    dispositions: dict[str, Any] | None = None
    publish_status: PublishStatus = "unpublished"
    #: Task 3.4 (online-training-strategy.md) — the operator's frozen
    #: reference run for this ``model_type`` (within this profile's ledger
    #: file, which is already profile-scoped — see ``src/vehicle/profiles.py``'s
    #: ``training_ledger`` path scoping). Only ``TrainingLedger.set_golden``
    #: ever sets this; no retrain/scheduler/promotion code path may. At most
    #: one run per ``model_type`` is golden at a time (same demote-on-append
    #: pattern as ``publish_status``'s ``active``).
    golden: bool = False
    #: Task 6.36 — which machine actually ran the training job: ``"laptop"``
    #: (companion, against synced sessions) or ``"pi"`` (the live daemon's
    #: own `JobController`). ``None`` for runs ingested before this field
    #: existed. Threaded through from `scripts/retrain.py --compute-target`,
    #: itself set by whichever `JobController` built the command
    #: (`src/control/jobs.py`'s `compute_target` constructor arg).
    compute_target: str | None = None
    #: Task 6.36 — the operator's explicit "this run is good, queue it for
    #: publish" decision, distinct from `publish_status` (which tracks the
    #: *Pi-side* upload/activation lifecycle once a publish is attempted).
    #: Only `TrainingLedger.set_approved` ever sets these.
    approved: bool = False
    approved_at: str | None = None
    notes: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return self.model_dump(mode="json")

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> TrainingRunRecord:
        return cls.model_validate(data)


class TrainingLedger(JsonlStore[TrainingRunRecord]):
    """Append-only JSONL training-run ledger.

    Not thread-safe on its own; the daemon serializes ledger writes the same
    way it does the anomaly/job ledgers. Reads tolerate a torn final line
    (Task 3.4: both behaviors now live on the shared
    :class:`~src.jsonl_store.JsonlStore` base).
    """

    def __init__(self, path: Path = DEFAULT_TRAINING_LEDGER_PATH) -> None:
        super().__init__(path, from_dict=TrainingRunRecord.from_dict)

    def append(self, record: TrainingRunRecord) -> TrainingRunRecord:
        """Append one record (flushed; crash loses at most the last line)."""
        return self._append_record(record, to_dict=TrainingRunRecord.to_dict)

    def records(self) -> list[TrainingRunRecord]:
        """Latest record per ``run_id``, oldest-created first.

        Collapsing the append-only log: a later line for the same run wins
        (that's how a publish-status change is recorded), but ``created_at``
        ordering is preserved so the dashboard's run history is stable.
        """
        by_id = latest_by_key(self._iter_raw(), key=lambda r: r.run_id)
        out = list(by_id.values())
        out.sort(key=lambda r: r.created_at)
        return out

    def get(self, run_id: str) -> TrainingRunRecord | None:
        return next((r for r in reversed(self.raw_records()) if r.run_id == run_id), None)

    def set_publish_status(
        self, run_id: str, status: PublishStatus, *, notes: str | None = None
    ) -> TrainingRunRecord | None:
        """Append a status-change line for ``run_id``; return the new record.

        Returns ``None`` for an unknown run. When ``status`` is ``active``,
        any other run of the same ``model_type`` currently ``active`` is
        demoted to ``published`` (only one model of a type runs live), each
        demotion recorded as its own append so the chain stays auditable.
        """
        current = self.get(run_id)
        if current is None:
            return None
        if status == "active":
            for other in self.records():
                if (
                    other.model_type == current.model_type
                    and other.run_id != run_id
                    and other.publish_status == "active"
                ):
                    self.append(
                        other.model_copy(
                            update={
                                "publish_status": "published",
                                "notes": f"demoted: {run_id} became active",
                            }
                        )
                    )
        updated = current.model_copy(
            update={"publish_status": status, "notes": notes or current.notes}
        )
        return self.append(updated)

    def set_golden(self, run_id: str, *, notes: str | None = None) -> TrainingRunRecord | None:
        """Declare ``run_id`` the frozen golden baseline for its ``model_type``
        (Task 3.4) — an explicit operator assertion, recorded as one (never
        called from a retrain/scheduler/promotion code path). Demotes any
        other run of the same ``model_type`` currently golden, appended as
        its own line (same provenance-preserving pattern as
        ``set_publish_status``'s ``active`` demotion).

        Returns ``None`` for an unknown run without appending anything.
        """
        current = self.get(run_id)
        if current is None:
            return None
        for other in self.records():
            if other.model_type == current.model_type and other.run_id != run_id and other.golden:
                self.append(
                    other.model_copy(
                        update={
                            "golden": False,
                            "notes": f"demoted: {run_id} declared golden",
                        }
                    )
                )
        updated = current.model_copy(update={"golden": True, "notes": notes or current.notes})
        return self.append(updated)

    def set_approved(
        self, run_id: str, approved: bool, *, notes: str | None = None
    ) -> TrainingRunRecord | None:
        """Record the operator's approve/unapprove decision for ``run_id`` (6.36).

        Appended like every other status change here (`set_publish_status`,
        `set_golden`) — no demotion needed since approval isn't exclusive per
        ``model_type`` (multiple runs can be approved; only one gets
        published `active` at a time, which `set_publish_status` already
        guards). Returns ``None`` for an unknown run.
        """
        current = self.get(run_id)
        if current is None:
            return None
        updated = current.model_copy(
            update={
                "approved": approved,
                "approved_at": datetime.now(UTC).isoformat() if approved else None,
                "notes": notes or current.notes,
            }
        )
        return self.append(updated)

    def golden_run(self, model_type: str) -> TrainingRunRecord | None:
        """The current golden run for ``model_type``, or ``None`` if the
        operator hasn't declared one yet (the default posture — Task 3.4 is
        explicit that no code path may auto-declare one)."""
        return next(
            (r for r in self.records() if r.model_type == model_type and r.golden),
            None,
        )


def _as_str_list(value: Any) -> list[str]:
    if isinstance(value, list):
        return [str(v) for v in value]
    return []


def build_run_record_from_manifest(
    manifest_path: Path,
    *,
    status: RunStatus = "succeeded",
    job_id: str | None = None,
    job_log_path: str | None = None,
    false_positive_benchmark_path: Path | None = None,
    detector_benchmark_report_path: Path | None = None,
    dispositions: dict[str, Any] | None = None,
    compute_target: str | None = None,
    notes: str | None = None,
) -> TrainingRunRecord:
    """Build a :class:`TrainingRunRecord` from a trainer's manifest JSON.

    Reads through :func:`validate_manifest_common` (``allowed_model_types=
    None`` so a future trainer's type doesn't block ingestion). Pulls the
    feature schema, hyperparams, and the detector-specific metrics that live
    in ``extras`` (LSTM ``thresholds`` / ``losses`` / ``architecture``; IF
    ``modes``). The sibling artifact (``model.pt`` / ``model.joblib``) is
    recorded if present. An optional false-positive benchmark JSON
    (``scripts/fp_benchmark.py`` output) is merged into ``metrics``.
    ``detector_benchmark_report_path`` (retrain's benchmark gate report, an
    ``scripts/evaluate_detectors.py`` report since R06.5) is recorded as-is —
    unlike the FP benchmark it isn't
    read/merged here, since callers (the retrain flow) already have the
    parsed report in hand and just need the path linked for provenance.

    Raises :class:`ManifestValidationError` if the manifest is unreadable or
    fails the common-schema check — the caller decides whether that's fatal
    (a bad manifest is a failed run worth recording with ``status="failed"``
    and ``notes``).
    """
    try:
        payload = json.loads(Path(manifest_path).read_text())
    except (OSError, json.JSONDecodeError) as exc:
        raise ManifestValidationError(f"cannot read manifest {manifest_path}: {exc}") from exc
    common = validate_manifest_common(payload, allowed_model_types=None)

    extras = common.extras
    schema = common.schema_
    feature_schema = _as_str_list(schema.get("feature_columns"))

    metrics: dict[str, Any] = {}
    if "thresholds" in extras:
        metrics["thresholds"] = extras["thresholds"]
    loss_curve = extras.get("losses") or extras.get("per_epoch_losses")
    if loss_curve is not None:
        metrics["loss_curve"] = loss_curve
    if "architecture" in extras:
        metrics["architecture"] = extras["architecture"]
    if "modes" in extras:
        metrics["modes"] = extras["modes"]
    if "validation" in extras:
        metrics["validation"] = extras["validation"]
    metrics["manifest_extras"] = extras

    if false_positive_benchmark_path is not None and Path(false_positive_benchmark_path).exists():
        try:
            metrics["false_positive_benchmark"] = json.loads(
                Path(false_positive_benchmark_path).read_text()
            )
        except (OSError, json.JSONDecodeError):
            log.warning("could not read FP benchmark %s", false_positive_benchmark_path)

    artifacts: dict[str, str] = {"manifest": str(manifest_path)}
    manifest_dir = Path(manifest_path).parent
    artifact_sha256: str | None = None
    for candidate in ("model.pt", "model.joblib"):
        artifact = manifest_dir / candidate
        if artifact.exists():
            artifacts["model"] = str(artifact)
            try:
                artifact_sha256 = sha256_file(artifact)
            except OSError as exc:
                log.warning("could not hash artifact %s: %s", artifact, exc)
            break

    return TrainingRunRecord(
        run_id=common.model_version or uuid4().hex,
        model_type=common.model_type,
        model_version=common.model_version,
        created_at=common.created_at or datetime.now(UTC).isoformat(),
        status=status,
        input_sessions=list(common.training_sessions),
        training_start_timestamp=common.training_start_timestamp,
        training_end_timestamp=common.training_end_timestamp,
        input_rows=common.input_rows,
        training_data_hash=common.training_data_hash,
        feature_schema=feature_schema,
        hyperparams=dict(common.hyperparams),
        metrics=metrics,
        artifacts=artifacts,
        artifact_sha256=artifact_sha256,
        job_id=job_id,
        job_log_path=job_log_path,
        detector_benchmark_report_path=(
            str(detector_benchmark_report_path)
            if detector_benchmark_report_path is not None
            else None
        ),
        dispositions=dispositions,
        compute_target=compute_target,
        notes=notes,
    )
