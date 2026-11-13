"""A small adaptive candidate that cannot absorb a fault (E03.5).

A Kalman random-walk filter (recursive least squares with per-parameter drift)
over a few physically meaningful parameters of the same linear relation a
:class:`~src.models.change_detection.FrozenReference` fixes. It learns
alongside the reference, never in its place. What it cannot do is structural:

* **Built from memory, only.** :func:`candidate_from_ledger` is the only way
  to construct one (the constructor refuses any other caller). It reads the
  unresolved change alarms (:func:`unresolved_alarms`) and the invalidating
  interventions (:func:`invalidating_boundaries`) from the vehicle's memory
  ledger and gives the candidate its own :class:`ChangeMonitor` holding them.
* **Monitored, then predicted, then learned.** :meth:`KalmanCandidate.learn`
  takes the one-shot :class:`Pending` token :meth:`predict` returned *and*
  the :class:`BlockVerdict` the candidate's monitor issued for the same block
  under the same reference; a stale, reused or foreign token or verdict is
  refused (:class:`PredictionOrderError`).
* **Frozen while any alarm stands.** If the verdict lists an unresolved alarm
  (per-block, drive-level, or carried from the ledger), or the candidate
  holds one of its own, the block is ``frozen``: predicted, never learned.
* **Divergence guard.** An update that would move the candidate's prediction
  at any declared probe point (``divergence_probes``) further than
  half the reference's shift of interest (CUSUM's ``k``) is not applied: the
  candidate raises its own ``divergence`` alarm and freezes. A candidate
  cannot drift past what the change detector is built to see, whether or not
  the detector saw it.
* **Excitation.** An update is taken only when this drive's window of
  regressors has its information matrix's smallest eigenvalue above the
  declared floor; otherwise the block is ``unexcited``. An idle-only drive
  does not separate the intercept from the airflow term.
* **Unmonitored is not learned.** A block the monitor could not evaluate
  (an uncalibrated cell) is ``unmonitored``: what the detector cannot see,
  the candidate does not learn.
* **Conditions and disposition.** Blocks marked with a disposition outside
  ``accepted_dispositions`` (quarantine, reject) are never learned.
* **Intervention boundaries.** A candidate belongs to its reference's
  calibration and to the span before the first invalidating intervention
  after its reference began; learning past either raises.
* **Engine response only.** :class:`CandidateSpec` admits only an
  ``engine_response`` residual and refuses ``twin:``/``rom:``/``ecu:`` names;
  this module reaches nothing of the twin (E02/E05), so a learned correction
  cannot conceal a twin residual or alter decoded ECU arithmetic.

Checkpoints (:class:`CandidateCheckpoint`) carry parameters, covariance, the
valid time of the data learned, the reference fingerprint, the parent
checkpoint, the calibration and the alarms holding it, self-hashed.

Per block: one small covariance update and one window eigenvalue.
"""

from __future__ import annotations

import math
from collections import deque
from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any, Literal

import numpy as np
from pydantic import BaseModel, ConfigDict, Field, ValidationInfo, model_validator

from src.artifact_hash import sha256_json
from src.evidence.ledger import MemoryLedger, MemoryQuery
from src.evidence.records import Evidence, Intervention, Investigation
from src.models.change_detection import Block, BlockVerdict, ChangeMonitor, FrozenReference

__all__ = [
    "CANDIDATE_SCHEMA",
    "CalibrationBoundaryError",
    "CandidateCheckpoint",
    "CandidateSpec",
    "InterventionBoundaryError",
    "KalmanCandidate",
    "LearnOutcome",
    "Pending",
    "PredictionOrderError",
    "StepRecord",
    "batch_fit",
    "candidate_from_ledger",
    "invalidating_boundaries",
    "unresolved_alarms",
]

CANDIDATE_SCHEMA: Literal["canary-adaptive-candidate/2"] = "canary-adaptive-candidate/2"

#: Names a candidate may not learn or regress on: the twin's decoded
#: computation and ROM constants are exact, not parameters to fit.
REFUSED_PREFIXES: tuple[str, ...] = ("twin:", "rom:", "ecu:")

Disposition = Literal["accept", "accept_recenter", "quarantine", "reject"]
LearnOutcome = Literal[
    "learned", "unexcited", "frozen", "rejected_disposition", "diverged", "unmonitored"
]


class PredictionOrderError(RuntimeError):
    """A learn call without a fresh prediction and verdict for the same block."""


class InterventionBoundaryError(ValueError):
    """Learning was asked to cross an invalidating intervention."""


class CalibrationBoundaryError(InterventionBoundaryError):
    """Learning was asked to use data recorded on another calibration."""


class _Strict(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class CandidateSpec(_Strict):
    """The candidate's parameters, priors, drift, gates — declared."""

    schema_: Literal["canary-adaptive-candidate/2"] = Field(alias="schema")
    name: str = Field(min_length=1)
    residual: str = Field(min_length=1)
    #: What the learned relation is a model *of*. Only engine response.
    evidence_class: Literal["engine_response"]
    features: tuple[str, ...] = Field(min_length=1, max_length=10)
    parameters: tuple[str, ...] = Field(min_length=1, max_length=10)
    units: tuple[str, ...] = Field(min_length=1, max_length=10)
    prior_sd: tuple[float, ...] = Field(min_length=1)
    random_walk_sd_per_hour: tuple[float, ...] = Field(min_length=1)
    observation_sd: float = Field(gt=0.0)
    excitation_window_blocks: int = Field(ge=2)
    min_information_eigenvalue: float = Field(gt=0.0)
    #: Posterior sd below which a parameter is reported identified.
    identified_sd: tuple[float, ...] = Field(min_length=1)
    accepted_dispositions: tuple[Literal["accept", "accept_recenter"], ...] = Field(min_length=1)
    #: Regressor vectors at which candidate and reference predictions are
    #: compared; the gap may not exceed half the reference's shift of
    #: interest (CUSUM's reference value k).
    divergence_probes: tuple[tuple[float, ...], ...] = Field(min_length=1)

    model_config = ConfigDict(extra="forbid", frozen=True, populate_by_name=True)

    @model_validator(mode="after")
    def _shape(self) -> CandidateSpec:
        n = len(self.features)
        for name in ("parameters", "units", "prior_sd", "random_walk_sd_per_hour", "identified_sd"):
            if len(getattr(self, name)) != n:
                raise ValueError(f"{name} needs one entry per feature ({n})")
        if any(len(p) != n for p in self.divergence_probes):
            raise ValueError(f"each divergence probe has one value per feature ({n})")
        if any(v <= 0 for v in self.prior_sd) or any(v <= 0 for v in self.identified_sd):
            raise ValueError("prior_sd and identified_sd are positive")
        if any(v < 0 for v in self.random_walk_sd_per_hour):
            raise ValueError("random_walk_sd_per_hour is non-negative")
        for name in (*self.features, *self.parameters, self.residual):
            if name.lower().startswith(REFUSED_PREFIXES):
                raise ValueError(
                    f"{name!r} names decoded ECU computation; a candidate learns engine "
                    "response around it and never fits the twin's arithmetic or ROM constants"
                )
        return self

    def sha256(self) -> str:
        return sha256_json(self.model_dump(mode="json", by_alias=True))


#: Identity-compared validation context for minting a fingerprint.
_SEALING: dict[str, bool] = {"sealing": True}


def _hash(payload: dict[str, Any]) -> str:
    body = {k: v for k, v in payload.items() if k != "fingerprint"}
    return sha256_json(body, default=str)


class CandidateCheckpoint(_Strict):
    """The candidate's state at one instant, with its lineage. Self-hashed."""

    kind: Literal["candidate_checkpoint"] = "candidate_checkpoint"
    spec_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    reference_fingerprint: str = Field(pattern=r"^[0-9a-f]{64}$")
    parent: str | None
    calibration: str | None
    parameters: tuple[str, ...]
    theta: tuple[float, ...]
    covariance: tuple[tuple[float, ...], ...]
    n_predicted: int = Field(ge=0)
    n_learned: int = Field(ge=0)
    #: Valid time of the data learned: first and last learned block.
    valid_from: datetime | None
    valid_to: datetime | None
    #: Unresolved alarms the candidate was frozen by at checkpoint time.
    frozen_by: tuple[str, ...]
    fingerprint: str = Field(pattern=r"^[0-9a-f]{64}$")

    @model_validator(mode="after")
    def _self_hashed(self, info: ValidationInfo) -> CandidateCheckpoint:
        if info.context is _SEALING:
            return self
        if self.fingerprint != _hash(self.model_dump(mode="json")):
            raise ValueError("checkpoint fingerprint does not hash its content")
        return self


@dataclass(frozen=True)
class Pending:
    """A prediction made before learning; with the block's verdict, the only
    key to :meth:`KalmanCandidate.learn`."""

    owner: int
    version: int
    step: int
    block: Block
    prediction: float
    theta_before: tuple[float, ...]


@dataclass(frozen=True)
class StepRecord:
    step: int
    drive: str
    start: float
    end: float
    prediction: float
    observed: float
    outcome: LearnOutcome | None
    theta_after: tuple[float, ...]


def _aware(t: float) -> datetime:
    return datetime.fromtimestamp(t, UTC)


# ---------------------------------------------------------------------------
# What memory says about learning now


def unresolved_alarms(ledger: MemoryLedger, residual: str, *, before: datetime) -> tuple[str, ...]:
    """Ids of observed change alarms on ``residual`` valid before ``before``
    that no resolved investigation has taken as an observation."""
    alarms = [
        entry.record
        for entry in ledger.query(MemoryQuery(kinds=("evidence",), incident=f"change:{residual}"))
        if isinstance(entry.record, Evidence)
        and entry.record.labels.get("kind") == "change_alarm"
        and entry.record.labels.get("data_origin") == "observed"
        and entry.record.valid_from < before
    ]
    resolved: set[str] = set()
    for entry in ledger.query(MemoryQuery(kinds=("investigation",))):
        record = entry.record
        if isinstance(record, Investigation) and record.status == "resolved":
            resolved.update(record.observations)
    return tuple(sorted(a.id for a in alarms if a.id not in resolved))


def invalidating_boundaries(ledger: MemoryLedger, channels: Iterable[str]) -> tuple[datetime, ...]:
    """``valid_from`` of every standing intervention that retires ``channels``."""
    wanted = tuple(channels)
    return tuple(
        sorted(
            entry.record.valid_from
            for entry in ledger.query(MemoryQuery(kinds=("intervention",)))
            if isinstance(entry.record, Intervention) and entry.record.retires(wanted)
        )
    )


#: Only :func:`candidate_from_ledger` holds this.
_FACTORY = object()


def candidate_from_ledger(
    ledger: MemoryLedger,
    spec: CandidateSpec,
    reference: FrozenReference,
    *,
    calibration: str | None,
    channels: Iterable[str],
    now: datetime | None = None,
    parent: CandidateCheckpoint | None = None,
) -> KalmanCandidate:
    """A candidate (with its monitor) that knows every unresolved alarm and
    invalidating intervention the vehicle's memory holds at ``now``."""
    moment = now or datetime.now(UTC)
    alarms = unresolved_alarms(ledger, spec.residual, before=moment)
    monitor = ChangeMonitor(reference, unresolved=alarms)
    return KalmanCandidate(
        _FACTORY,
        spec,
        reference,
        monitor=monitor,
        calibration=calibration,
        boundaries=invalidating_boundaries(ledger, channels),
        parent=parent,
    )


class KalmanCandidate:
    """Random-walk Kalman filter over the reference relation's parameters."""

    def __init__(
        self,
        key: object,
        spec: CandidateSpec,
        reference: FrozenReference,
        *,
        monitor: ChangeMonitor,
        calibration: str | None,
        boundaries: Iterable[datetime] = (),
        parent: CandidateCheckpoint | None = None,
        log_limit: int = 10_000,
    ) -> None:
        if key is not _FACTORY:
            raise TypeError(
                "build a candidate with candidate_from_ledger(): it reads the unresolved "
                "alarms and invalidating interventions the candidate must respect"
            )
        if not isinstance(reference, FrozenReference):
            raise TypeError("a candidate starts from a frozen reference")
        if monitor.reference is not reference:
            raise ValueError("the candidate's monitor watches another reference")
        if spec.residual != reference.residual or spec.features != reference.features:
            raise ValueError(
                f"candidate {spec.name!r} models {spec.residual}{list(spec.features)}; "
                f"the reference is {reference.residual}{list(reference.features)}"
            )
        if calibration != reference.calibration:
            raise CalibrationBoundaryError(
                f"the reference was fitted on calibration {reference.calibration!r}; a "
                f"candidate on {calibration!r} would adapt across a flash"
            )
        self.spec = spec
        self.monitor = monitor
        self._reference_fp = reference.fingerprint
        self._reference_theta = np.asarray(reference.theta, dtype=np.float64)
        # Half the shift of interest: CUSUM's reference value k. A candidate
        # further than k from the reference has absorbed a change the
        # detector is built to accumulate.
        self._limit = reference.alarm.shift_of_interest / 2.0
        self._probes = np.asarray(spec.divergence_probes, dtype=np.float64)
        self.calibration = calibration
        # Only boundaries after the reference's data began can invalidate it.
        self._boundary = min((b for b in boundaries if b > reference.valid_from), default=None)
        self._parent: str | None = None
        self._theta = self._reference_theta.copy()
        self._cov = np.diag(np.asarray(spec.prior_sd, dtype=np.float64) ** 2)
        self._n_predicted = 0
        self._n_learned = 0
        self._valid_from: float | None = None
        self._valid_to: float | None = None
        self._last_learn_t: float | None = None
        self._frozen_by: set[str] = set()
        if parent is not None:
            if parent.spec_sha256 != spec.sha256():
                raise ValueError("checkpoint was written under another candidate spec")
            if parent.reference_fingerprint != reference.fingerprint:
                raise ValueError("checkpoint belongs to another reference's lineage")
            if parent.calibration != calibration:
                raise CalibrationBoundaryError("checkpoint was learned on another calibration")
            self._parent = parent.fingerprint
            self._theta = np.asarray(parent.theta, dtype=np.float64)
            self._cov = np.asarray(parent.covariance, dtype=np.float64)
            self._n_predicted = parent.n_predicted
            self._n_learned = parent.n_learned
            self._frozen_by.update(parent.frozen_by)
            if parent.valid_from is not None:
                self._valid_from = parent.valid_from.timestamp()
            if parent.valid_to is not None:
                self._valid_to = parent.valid_to.timestamp()
                self._last_learn_t = self._valid_to
            if self._boundary is not None and self._valid_to is not None:
                if self._valid_to >= self._boundary.timestamp():
                    raise InterventionBoundaryError("checkpoint already spans a boundary")
        self._version = 0
        self._step = 0
        self._consumed: set[int] = set()
        self._window: deque[tuple[float, ...]] = deque(maxlen=spec.excitation_window_blocks)
        self.divergence_alarms: list[str] = []
        self.log: deque[StepRecord] = deque(maxlen=log_limit)

    # -- state ------------------------------------------------------------

    @property
    def theta(self) -> tuple[float, ...]:
        return tuple(float(v) for v in self._theta)

    @property
    def frozen_by(self) -> tuple[str, ...]:
        return tuple(sorted(self._frozen_by | set(self.monitor.open_alarms)))

    def divergence(self, theta: np.ndarray | None = None) -> float:
        """Largest |candidate - reference| prediction gap over the probes."""
        delta = (self._theta if theta is None else theta) - self._reference_theta
        return float(np.max(np.abs(self._probes @ delta)))

    def start_drive(self) -> None:
        """A new drive: excitation is judged on this drive's blocks alone."""
        self._window.clear()

    def freeze(self, alarms: Iterable[str]) -> None:
        self._frozen_by.update(alarms)

    def resolve(self, alarms: Iterable[str]) -> None:
        """Alarms a resolved investigation closed; learning resumes only when
        neither the candidate nor its monitor holds any."""
        ids = list(alarms)
        self._frozen_by.difference_update(ids)
        self.monitor.resolve(ids)

    def information_min_eigenvalue(self) -> float | None:
        """Smallest eigenvalue of the window's mean regressor outer product."""
        if len(self._window) < self.spec.excitation_window_blocks:
            return None
        design = np.asarray(self._window, dtype=np.float64)
        info = design.T @ design / len(design)
        return float(np.linalg.eigvalsh(info)[0])

    def identifiability(self) -> dict[str, dict[str, float | bool]]:
        """Posterior sd per parameter and whether it is below ``identified_sd``."""
        out: dict[str, dict[str, float | bool]] = {}
        for i, name in enumerate(self.spec.parameters):
            sd = float(math.sqrt(max(self._cov[i, i], 0.0)))
            out[name] = {"sd": sd, "identified": sd <= self.spec.identified_sd[i]}
        return out

    # -- predict, then learn ---------------------------------------------

    def predict(self, block: Block) -> Pending:
        x = np.asarray(block.x, dtype=np.float64)
        self._step += 1
        self._n_predicted += 1
        return Pending(
            owner=id(self),
            version=self._version,
            step=self._step,
            block=block,
            prediction=float(self._theta @ x),
            theta_before=self.theta,
        )

    def learn(
        self,
        pending: Pending,
        verdict: BlockVerdict,
        *,
        disposition: Disposition,
        calibration: str | None,
    ) -> LearnOutcome:
        """Use the block behind ``pending`` if its verdict and every gate allow."""
        if pending.owner != id(self) or pending.version != self._version:
            raise PredictionOrderError(
                "learn() takes the token of a prediction made from the current state; "
                "this one is stale or belongs to another candidate"
            )
        if pending.step in self._consumed:
            raise PredictionOrderError("this prediction was already learned from")
        if (
            not isinstance(verdict, BlockVerdict)
            or verdict.issuer is not self.monitor
            or not self.monitor.issued(verdict)
            or verdict.block != pending.block
            or verdict.reference_fingerprint != self._reference_fp
        ):
            raise PredictionOrderError(
                "learn() needs the verdict this candidate's monitor issued for the same "
                "block under the same reference: the change detector sees a block first"
            )
        self._consumed.add(pending.step)
        block = pending.block
        if calibration != self.calibration:
            raise CalibrationBoundaryError(
                f"block from drive {block.drive} ran on calibration {calibration!r}; this "
                f"candidate is {self.calibration!r}"
            )
        if self._boundary is not None and block.end >= self._boundary.timestamp():
            raise InterventionBoundaryError(
                f"block from drive {block.drive} ends at {_aware(block.end).isoformat()}, after "
                f"an invalidating intervention at {self._boundary.isoformat()}; retire this "
                "candidate with its reference and start a new lineage"
            )
        outcome: LearnOutcome
        self._frozen_by.update(verdict.open_alarms)
        if self._frozen_by:
            outcome = "frozen"
        elif verdict.status != "evaluated":
            # The change detector could not judge this block (an uncalibrated
            # cell): nothing it cannot see is learned.
            outcome = "unmonitored"
        elif disposition not in self.spec.accepted_dispositions:
            outcome = "rejected_disposition"
        else:
            self._window.append(tuple(block.x))
            eigen = self.information_min_eigenvalue()
            if eigen is None or eigen < self.spec.min_information_eigenvalue:
                outcome = "unexcited"
            else:
                outcome = self._update(block, pending.step)
        self.log.append(
            StepRecord(
                step=pending.step,
                drive=block.drive,
                start=block.start,
                end=block.end,
                prediction=pending.prediction,
                observed=block.y,
                outcome=outcome,
                theta_after=self.theta,
            )
        )
        return outcome

    def _update(self, block: Block, step: int) -> LearnOutcome:
        x = np.asarray(block.x, dtype=np.float64)
        cov = self._cov
        if self._last_learn_t is not None:
            hours = max(block.end - self._last_learn_t, 0.0) / 3600.0
            drift = np.asarray(self.spec.random_walk_sd_per_hour, dtype=np.float64) ** 2 * hours
            cov = cov + np.diag(drift)
        r_var = self.spec.observation_sd**2
        s = float(x @ cov @ x) + r_var
        gain = (cov @ x) / s
        theta = self._theta + gain * (block.y - float(self._theta @ x))
        if self.divergence(theta) > self._limit:
            alarm = f"divergence:{self._reference_fp[:12]}:{block.drive}:{block.start:.0f}:{step}"
            self.divergence_alarms.append(alarm)
            self._frozen_by.add(alarm)
            return "diverged"
        # Joseph form keeps the covariance symmetric positive semi-definite.
        a = np.eye(len(x)) - np.outer(gain, x)
        self._cov = a @ cov @ a.T + r_var * np.outer(gain, gain)
        self._theta = theta
        self._version += 1
        self._n_learned += 1
        self._last_learn_t = block.end
        self._valid_from = block.start if self._valid_from is None else self._valid_from
        self._valid_to = block.end
        return "learned"

    # -- checkpoints ------------------------------------------------------

    def checkpoint(self) -> CandidateCheckpoint:
        payload: dict[str, Any] = {
            "kind": "candidate_checkpoint",
            "spec_sha256": self.spec.sha256(),
            "reference_fingerprint": self._reference_fp,
            "parent": self._parent,
            "calibration": self.calibration,
            "parameters": list(self.spec.parameters),
            "theta": [float(v) for v in self._theta],
            "covariance": [[float(v) for v in row] for row in self._cov],
            "n_predicted": self._n_predicted,
            "n_learned": self._n_learned,
            "valid_from": None if self._valid_from is None else _aware(self._valid_from),
            "valid_to": None if self._valid_to is None else _aware(self._valid_to),
            "frozen_by": list(self.frozen_by),
        }
        probe = CandidateCheckpoint.model_validate(
            {**payload, "fingerprint": "0" * 64}, context=_SEALING
        )
        payload["fingerprint"] = _hash(probe.model_dump(mode="json"))
        checkpoint = CandidateCheckpoint.model_validate(payload)
        self._parent = checkpoint.fingerprint
        return checkpoint


def batch_fit(blocks: Sequence[Block]) -> tuple[np.ndarray, np.ndarray]:
    """Least-squares parameters of ``blocks`` and their covariance.

    For the excitation demonstration: on idle-only blocks the airflow term's
    standard error is large while the prediction *at idle* is still tight —
    the data fixes one combination of the parameters, not each of them.
    """
    design = np.asarray([b.x for b in blocks], dtype=np.float64)
    y = np.asarray([b.y for b in blocks], dtype=np.float64)
    theta, *_ = np.linalg.lstsq(design, y, rcond=None)
    dof = max(len(y) - design.shape[1], 1)
    sigma2 = float(np.sum((y - design @ theta) ** 2)) / dof
    return theta, sigma2 * np.linalg.pinv(design.T @ design)
