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
from collections.abc import Iterator
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Literal
from uuid import uuid4

from pydantic import BaseModel, ConfigDict, Field

from src.models.manifest import ManifestValidationError, validate_manifest_common

log = logging.getLogger(__name__)

__all__ = [
    "DEFAULT_TRAINING_LEDGER_PATH",
    "PublishStatus",
    "TrainingLedger",
    "TrainingRunRecord",
    "build_run_record_from_manifest",
]

DEFAULT_TRAINING_LEDGER_PATH = Path("data/control/training_runs.jsonl")

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
    job_id: str | None = None
    job_log_path: str | None = None
    publish_status: PublishStatus = "unpublished"
    notes: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return self.model_dump(mode="json")

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> TrainingRunRecord:
        return cls.model_validate(data)


class TrainingLedger:
    """Append-only JSONL training-run ledger.

    Not thread-safe on its own; the daemon serializes ledger writes the same
    way it does the anomaly/job ledgers. Reads tolerate a torn final line.
    """

    def __init__(self, path: Path = DEFAULT_TRAINING_LEDGER_PATH) -> None:
        self.path = Path(path)

    def append(self, record: TrainingRunRecord) -> TrainingRunRecord:
        """Append one record (flushed; crash loses at most the last line)."""
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self.path.open("a") as handle:
            handle.write(json.dumps(record.to_dict(), sort_keys=True) + "\n")
            handle.flush()
        return record

    def _iter_raw(self) -> Iterator[TrainingRunRecord]:
        if not self.path.exists():
            return
        with self.path.open() as handle:
            for line in handle:
                line = line.strip()
                if not line:
                    continue
                try:
                    yield TrainingRunRecord.from_dict(json.loads(line))
                except (json.JSONDecodeError, ValueError):
                    continue

    def raw_records(self) -> list[TrainingRunRecord]:
        """Every appended line in write order (full provenance chain)."""
        return list(self._iter_raw())

    def records(self) -> list[TrainingRunRecord]:
        """Latest record per ``run_id``, oldest-created first.

        Collapsing the append-only log: a later line for the same run wins
        (that's how a publish-status change is recorded), but ``created_at``
        ordering is preserved so the dashboard's run history is stable.
        """
        latest: dict[str, TrainingRunRecord] = {}
        order: list[str] = []
        for rec in self._iter_raw():
            if rec.run_id not in latest:
                order.append(rec.run_id)
            latest[rec.run_id] = rec
        out = [latest[rid] for rid in order]
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
    for candidate in ("model.pt", "model.joblib"):
        artifact = manifest_dir / candidate
        if artifact.exists():
            artifacts["model"] = str(artifact)
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
        job_id=job_id,
        job_log_path=job_log_path,
        notes=notes,
    )
