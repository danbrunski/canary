"""Behavior-validation documents and provenance identity (R20; B02.4/B02.6).

The leaf of the behavior-validation family: the per-vehicle
``behavior_validation.yaml`` schema and its loader, the evaluator version every
evidence record carries, the D03.22 mode-label cut-off, and the content
identities (``file_identity``, ``decoder_identity``, ``session_content_identity``)
a provenance record quotes. File hashes go through
:func:`src.artifact_hash.sha256_file_or_none`.

Imports nothing else of the family (``tests/test_behavior_validation_boundaries.py``).
"""

from __future__ import annotations

import hashlib
from collections.abc import Iterable
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Literal

import yaml
from pydantic import BaseModel, ConfigDict, Field, model_validator

from src.analysis.splits import DEFAULT_JOIN_GAP_SECONDS
from src.artifact_hash import sha256_file_or_none
from src.behavior.behavior_models import ValidationCriteria, governing_profile_file

#: Bump when the evaluator's definitions change (denominators, metrics,
#: event accounting). Persisted in every evidence record.
#: 2 (2026-09-22, B04.3): row times in true seconds for microsecond frames
#: (events, dedupe and synthetic windows were 1000x compressed), detection
#: delay, covered envelope and transient controls.
#: 3 (2026-09-23, B03.6): evaluated-hours exposure breaks at gaps longer than
#: the drive join gap. A directory that kept recording 11 h of a parked
#: truck (2026-09-23T032755) joined an evening and a morning drive into one
#: group, and its span counted the parked hours as watched time -- control
#: event rates came out ~10x too low.
#: 4 (2026-09-26, B05.3): a model carrying its own fitted
#: ``BandExcessPersistence`` is evented through it (``model_event_times``),
#: as the daemon does; every other model is evented exactly as in 3. Rows
#: stamped as not default-profile evidence (tuning captures, R03.12) are
#: refused as ``profile_excluded:*``, and evidence refuses an unstamped
#: capture piece.
#: 5 (2026-10-08, B04.9): a frame built by :func:`per_read_frame` is graded
#: one row per output read with its axes paired on the acquisition clock;
#: metrics record the bound (``FoldMetrics.axis_pairing_s``), and criteria
#: declaring ``axis_pairing`` fail grid-joined metrics. Grid frames are
#: graded exactly as in 4.
EVALUATOR_VERSION = 5


#: Name of the profile-data file holding the criteria (next to behavior_models.yaml).
VALIDATION_FILENAME = "behavior_validation.yaml"


#: D03.22 — classifier sidecars written before this commit derived mode
#: labels (and the events keyed on them) from raw broadcast counts whenever
#: Mode 01 was stale. Rows in a refit frame are re-segmented with current
#: code; the sidecar *events* that gate warmup recipes are not, so a sidecar
#: classified before the fix is not evidence until D03.22 relabels it.
MODE_LABEL_FIX_COMMIT = "f8b7d38"


MODE_LABEL_FIX_AT = datetime(2026, 9, 20, 4, 0, 7, tzinfo=UTC)


def mode_labels_predate_fix(classified_at: str) -> bool:
    """True when a sidecar was classified before :data:`MODE_LABEL_FIX_AT`
    (or its timestamp cannot be read — unknown is not current)."""
    try:
        stamp = datetime.fromisoformat(classified_at.replace("Z", "+00:00"))
    except ValueError:
        return True
    if stamp.tzinfo is None:
        stamp = stamp.replace(tzinfo=UTC)
    return stamp < MODE_LABEL_FIX_AT


# ---------------------------------------------------------------------------
# Profile data: criteria document
# ---------------------------------------------------------------------------


class _Strict(BaseModel):
    model_config = ConfigDict(extra="forbid", protected_namespaces=())


class ProvenanceResolution(_Strict):
    """A named decision for a session whose calibration identity is ambiguous.

    Sessions with an unknown tune revision, or whose recording crosses a
    flash, are refused until one of these names what to do and why.
    """

    session: str
    #: The calibration the session's rows belong to, or ``None`` with
    #: ``exclude: true`` to drop it from behavior-model evidence.
    revision: str | None = None
    exclude: bool = False
    reason: str = Field(min_length=1)

    @model_validator(mode="after")
    def _one_decision(self) -> ProvenanceResolution:
        if self.exclude == (self.revision is not None):
            raise ValueError("a resolution either names a revision or excludes the session")
        return self


class RegimeAxis(_Strict):
    """One reporting stratum: ``channel`` binned at ``edges`` (B04.1)."""

    channel: str = Field(min_length=1)
    unit: str = Field(min_length=1)
    edges: list[float] = Field(min_length=2)

    @model_validator(mode="after")
    def _increasing(self) -> RegimeAxis:
        if any(b <= a for a, b in zip(self.edges, self.edges[1:], strict=False)):
            raise ValueError(f"{self.channel}: edges must strictly increase")
        return self


class SteadyStateRegime(_Strict):
    """Steady vs transient split: every listed rate within its limit (per s).

    Rates come from the feature pipeline's one rate-of-change implementation
    at its rolling horizon — the same derivative a persisted rate gate reads.
    """

    max_abs_rate_per_second: dict[str, float] = Field(min_length=1)
    basis: str = Field(min_length=1)


class ReportRegimes(_Strict):
    """How evaluation reports stratify rows before any comparison (B04.1).

    Reporting only: a regime never selects rows for fitting or serving. The
    calibration stratum is always the session's tune revision.
    """

    axes: dict[str, RegimeAxis] = Field(default_factory=dict)
    steady_state: SteadyStateRegime | None = None


class BehaviorValidationDocument(_Strict):
    """``behavior_validation.yaml`` — per-vehicle validation policy (B02.4/B02.6)."""

    schema_version: Literal[1] = 1
    #: The operator-approved segmenter vocabulary. A config whose modes
    #: differ can reproduce history but not produce corrected evidence.
    approved_operating_modes: list[str] = Field(min_length=1)
    #: Modes that existed historically and what replaced them.
    retired_operating_modes: dict[str, str] = Field(default_factory=dict)
    drive_join_gap_seconds: float = Field(default=DEFAULT_JOIN_GAP_SECONDS, ge=0)
    provenance_resolutions: list[ProvenanceResolution] = Field(default_factory=list)
    report_regimes: ReportRegimes | None = None
    recipes: dict[str, ValidationCriteria] = Field(default_factory=dict)

    def resolution_for(self, session: str) -> ProvenanceResolution | None:
        for resolution in self.provenance_resolutions:
            if resolution.session == session:
                return resolution
        return None

    def taxonomy_problems(self, configured_modes: Iterable[str]) -> list[str]:
        """Named differences between a config's modes and the approved set."""
        configured = set(configured_modes)
        approved = set(self.approved_operating_modes)
        problems = [
            f"retired_mode_present:{mode}->{self.retired_operating_modes[mode]}"
            for mode in sorted(configured & set(self.retired_operating_modes))
        ]
        problems += [
            f"unapproved_mode:{mode}"
            for mode in sorted(configured - approved - set(self.retired_operating_modes))
        ]
        problems += [f"approved_mode_missing:{mode}" for mode in sorted(approved - configured)]
        return problems


def validation_document_path(behavior_models_path: Path) -> Path:
    """The tracked validation document for the artifact (S03.3: from the profile
    directory when the artifact is a machine's own copy outside the checkout)."""
    return governing_profile_file(behavior_models_path, VALIDATION_FILENAME)


def load_validation_document(path: Path) -> BehaviorValidationDocument | None:
    if not path.exists():
        return None
    return BehaviorValidationDocument.model_validate(yaml.safe_load(path.read_text()))


# ---------------------------------------------------------------------------
# Provenance identity
# ---------------------------------------------------------------------------


def file_identity(path: Path) -> str:
    """``<path>@sha256:<hex>`` or ``<path>@missing`` — for provenance records."""
    digest = sha256_file_or_none(path)
    return f"{path}@missing" if digest is None else f"{path}@sha256:{digest}"


def file_sha256(path: Path) -> str | None:
    """``sha256:<hex>`` of ``path``, or ``None`` when it is not a file."""
    digest = sha256_file_or_none(path)
    return None if digest is None else "sha256:" + digest


def decoder_identity(
    *, bindings_path: Path, dids_path: Path, dbc_dir: Path | None = None
) -> dict[str, str]:
    """Content identity of every catalog that turns bytes into model inputs."""
    identity = {
        "bindings": file_sha256(bindings_path) or "missing",
        "dids": file_sha256(dids_path) or "missing",
    }
    if dbc_dir is not None:
        digest = hashlib.sha256()
        files = sorted(dbc_dir.glob("*.dbc")) if dbc_dir.exists() else []
        for dbc in files:
            digest.update(dbc.name.encode())
            digest.update(dbc.read_bytes())
        identity["dbc"] = "sha256:" + digest.hexdigest() if files else "none"
    return identity


def session_content_identity(session: Path) -> dict[str, Any]:
    """Content hash of a session's telemetry chunks, for evidence manifests."""
    digest = hashlib.sha256()
    files = sorted(session.glob("telemetry-*.parquet"))
    total = 0
    for chunk in files:
        data = chunk.read_bytes()
        total += len(data)
        digest.update(chunk.name.encode())
        digest.update(len(data).to_bytes(8, "big"))
        digest.update(data)
    return {
        "name": session.name,
        "chunks": len(files),
        "bytes": total,
        "sha256": "sha256:" + digest.hexdigest(),
    }
