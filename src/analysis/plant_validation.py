"""Validate the fuel plant from telemetry and across an actual intervention (E07.4).

Three parts, each refusing what would make its answer meaningless:

* **Channel inventory** (:func:`inventory_session`). For every plant need --
  raw command, airflow/speed, lambda, thermal/barometric, voltage, fuel
  pressure, trims, and the intervention's load input -- each session's read
  count and its *acquisition clock* (median and p90 interval of the persisted
  per-read arrival times, never a count over wall-clock), and the calibration
  revision in force at its first read.
* **The intervention case** (:class:`InterventionCase`, the profile's
  ``plant_interventions.yaml``). Base and candidate revisions and images are
  checked against ``tune_revisions.yaml``; the flash boundary is read from it,
  never restated. The prediction of record is *preserved*: its document, the
  commit that first recorded it, that commit's time and the document's sha256
  at that commit, and its bands copied with their table rows verbatim
  (:func:`verify_prediction_of_record` re-reads the commit). It is ordered
  against the flash by that commit time; nothing here re-dates it.
* **Evaluation** (:func:`evaluate_intervention`). One frozen plant, one
  parameter set, both sides of the boundary:

  - the split is by sample time against the boundary (a recording that
    straddles it is refused) and every recording is declared in the case
    before it is scored;
  - **a separate plant per ROM cannot count**: a baseline identified on the
    candidate revision, on a post-change recording, or on a recording the case
    reserves as post-change is refused, and there is exactly one profile
    argument;
  - per-sample plant replay is reported for steady, transient and
    missing-input reads separately, with the observation latency's bracket as
    a separate sensitivity, before and after the change;
  - the intervention's effect is tested per *matched pair* (one post-change
    read, one pre-change read at the same regime, rpm, load input, baro and
    intake temperature; one-to-one), never by comparing aggregates, against a
    condition-matched noise floor from pre-vs-pre pairs (two recordings, or
    one recording at the case's declared minimum separation);
  - the scoring rules are frozen (hashed, with a time) before the first
    post-change recording is listed, and refuse to load if changed after;
  - learned trim is a declared state: the flash resets LTFT to its stated
    value and relearning is not modelled, so a post-change read whose LTFT has
    moved off that value is ``ltft_relearning`` and is reported, not tested.
    Recorded post-change trims never enter a prediction;
  - the verdict is ``pending`` -- with ``plant_not_qualified`` named -- until
    the plant has passed its own predeclared held-out test
    (:func:`src.analysis.fuel_plant.plant_qualification`), and
    ``no_post_change_data`` until post-change recordings exist. A baseline fit
    alone never qualifies a tuning effect.

A synthetic (labelled) plant may be evaluated only on ``_sim-*`` traces and its
result carries ``synthetic: true``: it demonstrates the path, never the truck.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import math
import secrets
import subprocess
from collections.abc import Callable, Iterable, Mapping, Sequence
from dataclasses import asdict, dataclass
from datetime import UTC, date, datetime
from pathlib import Path
from typing import Any, Literal

import numpy as np
import pandas as pd
from pydantic import BaseModel, ConfigDict, Field, model_validator

from src.analysis.channels import read_channels
from src.analysis.ecu_application import ApplicationModel, load_application_model
from src.analysis.fuel_comp_inputs import load_fuel_comp_declaration
from src.analysis.fuel_comp_rom import FuelCompCalibration, RevisionImageResolver
from src.analysis.fuel_comp_samples import session_samples
from src.analysis.fuel_plant import (
    Acceptance,
    B06Rules,
    FloatArray,
    FuelPlantProfile,
    FuelPlantRefusalError,
    PlantInputs,
    load_b06,
    load_fuel_plant,
    plant_qualification,
    predict,
    resolve_parameters,
)
from src.analysis.fuel_plant_eval import (
    _bracketed,
    _nearest_value,
    _pair,
    _series,
    _stats,
    _window,
    build_samples,
    check_drive_independence,
    check_origin,
    plant_inputs,
)
from src.analysis.lambda_delivery import load_lambda_delivery
from src.paths import REPO_ROOT
from src.session import SessionKind, is_synthetic_session, iter_sessions
from src.signals.afr import load_afr_calibration
from src.signals.pairing import PairingPolicy, match_ordered, to_seconds
from src.storage.sessions import session_started_at
from src.vehicle.tune_revisions import TuneRevision, TuneRevisionRegistry, session_dated_at
from src.yaml_load import load_yaml

INTERVENTIONS_FILE = "plant_interventions.yaml"

#: The plant's needs, by group, as ``Specs`` field names. The case adds its
#: own load input (:attr:`InterventionCase.load_input`).
PLANT_NEEDS: dict[str, tuple[str, ...]] = {
    "raw_command": ("pulse", "bfs"),
    "airflow_speed": ("maf", "rpm"),
    "lambda": ("lambda_observed",),
    "thermal_baro": ("coolant", "intake_temp", "baro"),
    "voltage": ("battery_voltage",),
    "fuel_pressure": ("rail_pressure",),
    "trims": ("stft", "ltft"),
}
#: ``Specs`` fields without which plant replay cannot run at all (BFS and the
#: trims are the ECU's, rail pressure has a working assumption).
REPLAY_NEEDS: tuple[str, ...] = (
    "pulse",
    "maf",
    "rpm",
    "lambda_observed",
    "coolant",
    "intake_temp",
    "baro",
)
#: Reads the plant has no partner for, reported apart from steady and transient.
MISSING_STATES: tuple[str, ...] = ("missing_input", "not_co_observed")

TestOutcome = Literal["pass", "fail", "insufficient_evidence", "not_run"]


class _Strict(BaseModel):
    model_config = ConfigDict(extra="forbid")


# -- inventory ---------------------------------------------------------------------


def _clock(times: FloatArray) -> dict[str, float | int | None]:
    """Acquisition clock of one channel: per-read intervals, never count/duration."""
    n = len(times)
    if n < 2:
        return {"n": n, "median_dt_s": None, "p90_dt_s": None}
    dt = np.diff(np.sort(times))
    return {
        "n": n,
        "median_dt_s": round(float(np.median(dt)), 4),
        "p90_dt_s": round(float(np.quantile(dt, 0.9)), 4),
    }


def inventory_session(
    session: Path,
    profile: FuelPlantProfile,
    *,
    extra: dict[str, str] | None = None,
    registry: TuneRevisionRegistry | None = None,
) -> dict[str, Any]:
    """What one session holds of each plant need, on its acquisition clock.

    ``extra`` maps further group names to a spec (the intervention's load input).
    Reads use the persisted per-read arrival time (``acquired_time=True``); the
    basis each channel's times rest on is reported with it."""
    s = profile.specs
    groups: dict[str, list[str]] = {
        g: [getattr(s, name) for name in names] for g, names in PLANT_NEEDS.items()
    }
    for group, spec in (extra or {}).items():
        groups.setdefault(group, []).append(spec)
    specs = list(dict.fromkeys(spec for group in groups.values() for spec in group))
    try:
        long = read_channels(session, specs, acquired_time=True)
    except FileNotFoundError:
        # A directory with no telemetry parquet: an empty kind, named, not a crash.
        long = pd.DataFrame(columns=["timestamp", "spec", "value"])
    channels: dict[str, dict[str, Any]] = {}
    first = math.inf
    last = -math.inf
    for spec in specs:
        rows = long[long["spec"] == spec]
        times = to_seconds(rows["timestamp"]) if len(rows) else np.array([], dtype=float)
        times = times[np.isfinite(times)]
        entry: dict[str, Any] = _clock(times)
        if "timestamp_basis" in rows and len(rows):
            entry["basis"] = sorted({str(b) for b in rows["timestamp_basis"].unique()})
        channels[spec] = entry
        if len(times):
            first, last = min(first, float(times.min())), max(last, float(times.max()))
    present = {g: [c for c in specs_ if channels[c]["n"] > 0] for g, specs_ in groups.items()}
    started = session_started_at(session.name)
    revision: TuneRevision | None = None
    if registry is not None and math.isfinite(first):
        revision = registry.at(datetime.fromtimestamp(first, tz=UTC))
    return {
        "session": session.name,
        "started_at": None if started is None else started.isoformat(),
        "first_read": None if not math.isfinite(first) else _iso(first),
        "last_read": None if not math.isfinite(last) else _iso(last),
        "revision": None if revision is None else revision.revision,
        "channels": channels,
        "groups": {g: {"present": present[g], "complete": present[g] == groups[g]} for g in groups},
        "plant_replay_ready": all(channels[getattr(s, f)]["n"] > 0 for f in REPLAY_NEEDS),
    }


def _iso(seconds: float) -> str:
    return datetime.fromtimestamp(seconds, tz=UTC).isoformat()


def recorded_sessions(root: Path) -> list[Path]:
    """Every recording under ``root``, oldest first by parsed time: live
    sessions, captures and tuning sessions. Replay output, ``_archive`` and
    simulator traces are not recordings."""
    return [
        p
        for p in iter_sessions(
            root, kinds=(SessionKind.LIVE, SessionKind.CAPTURE, SessionKind.TUNING)
        )
        if not is_synthetic_session(p.name)
    ]


# -- the intervention case -----------------------------------------------------------


class PredictedBand(_Strict):
    """One row of a preserved prediction: its operating band and change, as written."""

    #: Raw load-input counts, ``[lo, hi)``.
    load_input: tuple[int, int]
    #: rpm ``[lo, hi]``; ``None`` is the row's "any".
    rpm: tuple[float, float] | None = None
    #: Predicted change in delivered fuel, percent, ``[lo, hi]`` (richer > 0).
    fuel_change_pct: tuple[float, float]
    #: The table row exactly as the document has it at the recording commit.
    as_written: str = Field(min_length=1)

    @model_validator(mode="after")
    def _ordered(self) -> PredictedBand:
        if not self.load_input[0] < self.load_input[1]:
            raise ValueError(f"load band {self.load_input} is empty")
        if self.fuel_change_pct[0] > self.fuel_change_pct[1]:
            raise ValueError(f"change range {self.fuel_change_pct} is reversed")
        return self


class LedgerStatus(_Strict):
    status: Literal["recorded", "not_recorded"]
    record_id: str | None = None
    reason: str = Field(min_length=1)

    @model_validator(mode="after")
    def _id(self) -> LedgerStatus:
        if (self.status == "recorded") != (self.record_id is not None):
            raise ValueError("a record id goes with, and only with, status recorded")
        return self


class PredictionOfRecord(_Strict):
    """A prediction preserved as first recorded: never edited, never re-dated."""

    document: str = Field(min_length=1)
    commit: str = Field(pattern=r"^[0-9a-f]{40}$")
    #: The commit's own time: the instant the prediction is ordered by.
    committed_at: datetime
    #: sha256 of the document's bytes at ``commit``.
    text_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    bands: list[PredictedBand] = Field(min_length=1)
    ledger: LedgerStatus


class LearnedStatePolicy(_Strict):
    """How the flash leaves a learned trim, and when that declaration stops holding."""

    quantity: str = Field(min_length=1)
    #: The contract event that resets it.
    reset_event: str = Field(min_length=1)
    #: Its value after the reset, percent.
    reset_value_pct: float
    #: A post-change read further than this from ``reset_value_pct`` is
    #: ``ltft_relearning``: the declared state no longer holds there.
    relearn_tolerance_pct: float = Field(gt=0)
    evidence: str = Field(min_length=1)


class Matching(_Strict):
    """Which pre-change read stands for a post-change one (same conditions)."""

    rpm_rel: float = Field(gt=0)
    load_input_abs: float = Field(gt=0)
    baro_kpa_abs: float = Field(gt=0)
    iat_c_abs: float = Field(gt=0)
    evidence: str = Field(min_length=1)


class EffectAcceptance(_Strict):
    """Predeclared: when a direction/magnitude test has enough to say anything,
    and what it must meet."""

    declared_on: date
    min_pairs: int = Field(ge=1)
    min_floor_pairs: int = Field(ge=1)
    direction_agreement: float = Field(gt=0, le=1)
    within_uncertainty: float = Field(gt=0, le=1)
    #: Quantile of |pre-vs-pre residual| taken as the condition-matched floor.
    floor_quantile: float = Field(gt=0, lt=1)
    evidence: str = Field(min_length=1)


class CaseSplit(_Strict):
    declared_on: date
    #: Pre-change recordings scored with the frozen plant (never fitted on here).
    pre: list[str] = Field(default_factory=list)
    #: Post-change recordings, reserved: listed before they are scored, never
    #: identification for any plant.
    post: list[str] = Field(default_factory=list)
    evidence: str = Field(min_length=1)

    @model_validator(mode="after")
    def _disjoint(self) -> CaseSplit:
        both = sorted(set(self.pre) & set(self.post))
        if both:
            raise ValueError(f"recordings on both sides of the intervention: {both}")
        return self


_SEAL_KEY = secrets.token_bytes(32)


@dataclass(frozen=True)
class InterventionVerdict:
    """The outcome of one :func:`evaluate_intervention` run, sealed.

    Minted only there: ``seal`` is an HMAC (a process-private key) over every
    other field, so a verdict typed by a caller, or one edited with
    ``dataclasses.replace``, fails :meth:`verify`. Ranking a tuning effect
    (:func:`src.analysis.paired_scenarios.rank_as_improvement`) takes this,
    never a string."""

    case: str
    verdict: str
    synthetic: bool
    base_image_sha256: str
    candidate_image_sha256: str
    baseline_sha256: str
    plant_version: str
    qualified_regimes: tuple[str, ...]
    seal: str

    @staticmethod
    def _digest(fields: dict[str, Any]) -> str:
        body = json.dumps(fields, sort_keys=True, default=list).encode()
        return hmac.new(_SEAL_KEY, body, hashlib.sha256).hexdigest()

    @classmethod
    def _mint(cls, **fields: Any) -> InterventionVerdict:
        return cls(**fields, seal=cls._digest(fields))

    def verify(self) -> bool:
        fields = {k: v for k, v in asdict(self).items() if k != "seal"}
        return hmac.compare_digest(self.seal, self._digest(fields))


class FloorRule(_Strict):
    """Which pre-vs-pre pairs make the condition-matched noise floor.

    A pair may come from two recordings, or from one recording when its reads
    are at least ``min_separation_s`` apart: far enough that the two are not
    the same transient seen twice. The separation is chosen from physics
    (lambda transport delay, fuel-film and trim time constants), never tuned
    on data."""

    declared_on: date
    min_separation_s: float = Field(gt=0)
    evidence: str = Field(min_length=1)


class FrozenRules(_Strict):
    """The scoring rules as declared before the first post-change recording.

    ``sha256`` is :meth:`InterventionCase.rules_sha256` at ``frozen_at``. Once
    ``split.post`` lists a recording, the rules must still hash to it and every
    post-change recording must start after ``frozen_at``: a rule edited after a
    post-change drive exists cannot load (as ``spent_held_out`` does for the
    plant's held-out drives)."""

    sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    frozen_at: datetime


_SHA256 = r"^[0-9a-f]{64}$"


class EcuOutputSpec(_Strict):
    """One ECU-side output scored against the truck (E09.12): the decoded
    integer logic's value, which the plant does not touch."""

    id: str = Field(pattern=r"^[a-z][a-z0-9_]*$")
    #: The ``output`` name in the prediction's ``traces.parquet``.
    trace_output: str = Field(min_length=1)
    #: The channel the truck reports it on.
    observed: str = Field(min_length=1)
    #: The application-model output it is (``ecu_application_model.yaml``),
    #: whose dependency chain :func:`trim_independence` walks.
    model_output: str = Field(min_length=1)


class PredictionArtifact(_Strict):
    """A simulator prediction written before the drives it is scored on.

    ``files`` are the sha256 values recorded with the prediction (E08.7 item
    5), never recomputed here: :func:`verify_prediction_artifact` refuses any
    difference (``prediction_artifact_changed``) before a post-change read is
    taken. ``design_files`` are the design's own files, hashed before any
    scoring, since the prediction recorded only the design's canonical hash."""

    #: Directory name under the predictions root (outside the checkout).
    artifact: str = Field(pattern=r"^[A-Za-z0-9][A-Za-z0-9_.-]*$")
    recorded_in: str = Field(min_length=1)
    design_sha256: str = Field(pattern=_SHA256)
    result_digest: str = Field(pattern=_SHA256)
    result: str = Field(min_length=1)
    traces: str = Field(min_length=1)
    design: str = Field(min_length=1)
    design_record: str = Field(min_length=1)
    files: dict[str, str] = Field(min_length=1)
    design_files: dict[str, str] = Field(min_length=1)

    @model_validator(mode="after")
    def _named(self) -> PredictionArtifact:
        for label, digest in {**self.files, **self.design_files}.items():
            if len(digest) != 64 or any(c not in "0123456789abcdef" for c in digest):
                raise ValueError(f"{label}: {digest!r} is not a sha256")
        for name in (self.result, self.traces):
            if name not in self.files:
                raise ValueError(f"{name} is scored but its recorded hash is not in files")
        for name in (self.design, self.design_record):
            if name not in self.design_files:
                raise ValueError(f"{name} is read but its hash is not in design_files")
        return self


class EcuOutputRules(_Strict):
    """E09.12: effect tests on the ECU-side outputs, per matched pair, with the
    case's matching, floor and acceptance. The pre-change side is the
    prediction's own design points; the post-change side is each output's own
    reads on the recordings listed in ``post``. The plant is not used, so its
    qualification does not gate the verdict."""

    declared_on: date
    prediction: PredictionArtifact
    #: The predicted change at a point is the draws' quantiles of the relative
    #: change (inverted CDF, as the prediction's own cell table), ``[lo, hi]``,
    #: and their median as the central value.
    interval_quantiles: tuple[float, float]
    #: A test with no pair whose central prediction is signed has no direction
    #: to agree with: it is judged on within-uncertainty alone, named
    #: ``direction_not_applicable``.
    unsigned_prediction: Literal["within_only"]
    outputs: list[EcuOutputSpec] = Field(min_length=1)
    #: Post-change recordings, listed before they are scored. Every recording
    #: on the candidate revision must be here (``post_recording_undeclared``).
    post: list[str] = Field(default_factory=list)
    frozen_rules: FrozenRules
    evidence: str = Field(min_length=1)

    @model_validator(mode="after")
    def _ordered(self) -> EcuOutputRules:
        lo, hi = self.interval_quantiles
        if not 0.0 < lo < 0.5 < hi < 1.0:
            raise ValueError(f"interval quantiles {self.interval_quantiles} must bracket 0.5")
        ids = [o.id for o in self.outputs]
        if len(set(ids)) != len(ids):
            raise ValueError(f"duplicate output ids {ids}")
        if len(set(self.post)) != len(self.post):
            raise ValueError("a post-change recording is listed twice")
        return self


#: What an ECU-output test compares per pair. ``raw``: the output's own value.
#: ``air_normalised``: BFS x rpm / MAF, which the decoded steady chain makes
#: proportional to K x FC / 128 (:class:`EcuAmendment`).
EcuQuantity = Literal["raw", "air_normalised"]


class EcuAmendment(_Strict):
    """A versioned amendment to the ECU-output rules: never an edit of the
    block it amends, which stays frozen with its own verdict.

    ``amends`` is the frozen sha256 of the rules it replaces (the
    ``ecu_outputs`` block for version 2, the previous amendment after), and
    its own ``frozen_rules`` hash :meth:`rules_sha256`. ``reason`` states why
    and on what evidence it was made; it must say what post-change data
    existed then, so it cannot have been chosen to fit them."""

    version: int = Field(ge=2)
    declared_on: date
    amends: str = Field(pattern=_SHA256)
    #: Output id -> the quantity compared; an output not named keeps ``raw``.
    quantities: dict[str, EcuQuantity] = Field(min_length=1)
    #: BFS at or above this is the rpm ceiling's clamp (``0xD8B3 << 8``), not
    #: ``4 x 0xFFFF105C``: excluded as ``*_at_ceiling`` under ``air_normalised``.
    bfs_ceiling_counts: int = Field(gt=0, le=0xFFFF)
    #: ``predicted_beyond_floor`` (version 3): a pair counts toward the
    #: verdict only where the design point's predicted |change| exceeds the
    #: output's condition-matched floor, so the test has power there; fewer
    #: than ``acceptance.min_pairs`` such pairs is ``insufficient_evidence``
    #: named ``prediction_unresolvable``. ``None``: every predicted pair counts.
    resolvable: Literal["predicted_beyond_floor"] | None = None
    #: Version 3: reads whose regime is ``unknown`` are excluded by name
    #: (``*_regime_unknown``) rather than paired as a regime of their own.
    exclude_unknown_regime: bool | None = None
    reason: str = Field(min_length=1)
    frozen_rules: FrozenRules
    evidence: str = Field(min_length=1)

    def rules_sha256(self) -> str:
        """Over every rule field; an unset (``None``) field is left out, so a
        field added for a later version never moves an earlier one's hash."""
        rules = self.model_dump(
            mode="json", exclude={"frozen_rules", "evidence"}, exclude_none=True
        )
        return hashlib.sha256(json.dumps(rules, sort_keys=True).encode()).hexdigest()


class PostAppend(_Strict):
    """One post-change recording declared after the ECU-output rules froze.

    The post list is append-only and outside every frozen hash, as E07.4's
    ``split.post`` is outside its own: ``chain_sha256`` is
    :func:`post_chain_sha256` of the entry before (the ``ecu_outputs`` frozen
    sha for the first), so removing, reordering or editing an entry before the
    last refuses (``post_list_rewritten``). Dropping the last entry leaves the
    recording on disk undeclared, which :func:`score_ecu_outputs` refuses
    (``post_recording_undeclared``)."""

    recording: str = Field(min_length=1)
    appended_on: date
    chain_sha256: str = Field(pattern=_SHA256)


def post_chain_sha256(previous: str, recording: str, appended_on: date) -> str:
    """The next link of the append-only post list: the link before, the
    recording and the day it was declared. The chain carries no key, so it
    catches an edit that does not recompute the links after it; against a
    recomputed rewrite the record is the disk scan
    (``post_recording_undeclared``) and git history."""
    return hashlib.sha256(f"{previous}:{recording}:{appended_on.isoformat()}".encode()).hexdigest()


class EcuPredictionBlock(_Strict):
    """A further prediction of the same intervention scored on the ECU-side
    outputs (E09.12), frozen on its own: never an edit of ``ecu_outputs``,
    whose artifact stays frozen inside its own sha.

    It shares the case's flash, images, E07.4 matching, floor and acceptance,
    and its declared post-change recordings (``ecu_outputs.post`` plus
    ``ecu_post_appended``): those belong to the intervention, not to one
    prediction. It carries its own artifact and hashes, outputs and quantities,
    and the amendment-3 rules (``resolvable`` and ``exclude_unknown_regime``)
    as required fields rather than an amendment chain.

    ``design_recordings`` are the drives its design was built from, checked
    against the design's own record before scoring (``design_recordings_mismatch``).
    ``null_control`` are pre-flash drives on the base image scored against this
    prediction as if they were post-change: none may be a design recording
    (``null_control_in_design``), since a design point paired with its own
    drive is not an independent no-change comparison. ``co_observed_at_freeze``
    is, per output, how many co-observed post-change pairs existed when the
    block froze: the order evidence, stated as a number."""

    id: str = Field(pattern=r"^[a-z0-9][a-z0-9_.-]*$")
    declared_on: date
    prediction: PredictionArtifact
    design_recordings: list[str] = Field(min_length=1)
    null_control: list[str] = Field(min_length=1)
    interval_quantiles: tuple[float, float]
    unsigned_prediction: Literal["within_only"]
    outputs: list[EcuOutputSpec] = Field(min_length=1)
    #: Output id -> the quantity compared; an output not named keeps ``raw``.
    quantities: dict[str, EcuQuantity] = Field(min_length=1)
    bfs_ceiling_counts: int = Field(gt=0, le=0xFFFF)
    resolvable: Literal["predicted_beyond_floor"]
    exclude_unknown_regime: Literal[True]
    co_observed_at_freeze: dict[str, int]
    reason: str = Field(min_length=1)
    frozen_rules: FrozenRules
    evidence: str = Field(min_length=1)

    @model_validator(mode="after")
    def _ordered(self) -> EcuPredictionBlock:
        lo, hi = self.interval_quantiles
        if not 0.0 < lo < 0.5 < hi < 1.0:
            raise ValueError(f"interval quantiles {self.interval_quantiles} must bracket 0.5")
        ids = [o.id for o in self.outputs]
        if len(set(ids)) != len(ids):
            raise ValueError(f"duplicate output ids {ids}")
        unknown = sorted(set(self.quantities) - set(ids))
        if unknown:
            raise ValueError(f"block {self.id} names quantities for outputs {unknown} it lacks")
        if sorted(self.co_observed_at_freeze) != sorted(ids):
            raise ValueError(
                f"co_observed_at_freeze names {sorted(self.co_observed_at_freeze)}, the block's "
                f"outputs are {sorted(ids)}: state the order evidence for every output"
            )
        if any(n < 0 for n in self.co_observed_at_freeze.values()):
            raise ValueError("co_observed_at_freeze counts pairs: none is negative")
        shared = sorted(set(self.null_control) & set(self.design_recordings))
        if shared:
            raise ValueError(
                f"null_control_in_design: {shared} built block {self.id}'s design, so they "
                "cannot be its independent no-change control"
            )
        if len(set(self.null_control)) != len(self.null_control):
            raise ValueError("a null-control drive is listed twice")
        return self

    def rules_sha256(self) -> str:
        """Over every rule field: all but ``frozen_rules`` and ``evidence``."""
        rules = self.model_dump(mode="json", exclude={"frozen_rules", "evidence"})
        return hashlib.sha256(json.dumps(rules, sort_keys=True).encode()).hexdigest()


class InterventionCase(_Strict):
    id: str = Field(pattern=r"^[a-z0-9][a-z0-9_.-]*$")
    synthetic: bool = False
    base_revision: str
    candidate_revision: str
    base_image_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    candidate_image_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    #: The load input the prediction's bands are written in (a channel spec).
    load_input: str = Field(min_length=1)
    load_input_pair_tolerance_s: float = Field(gt=0)
    prediction_of_record: PredictionOfRecord
    learned_state: LearnedStatePolicy
    matching: Matching
    acceptance: EffectAcceptance
    floor: FloorRule
    split: CaseSplit
    #: Required once ``split.post`` is non-empty.
    frozen_rules: FrozenRules | None = None
    #: E09.12's tests on the plant-free outputs (BFS, target phi).
    ecu_outputs: EcuOutputRules | None = None
    #: Versioned amendments to ``ecu_outputs``, oldest first (E09.12).
    ecu_amendments: list[EcuAmendment] = Field(default_factory=list)
    #: Post-change recordings declared after ``ecu_outputs`` froze, append-only.
    ecu_post_appended: list[PostAppend] = Field(default_factory=list)
    #: Further predictions of this intervention scored on the ECU-side
    #: outputs, each frozen on its own (E09.12 follow-up).
    ecu_predictions: list[EcuPredictionBlock] = Field(default_factory=list)
    evidence: str = Field(min_length=1)

    def ecu_prediction(self, block_id: str) -> EcuPredictionBlock:
        found = next((b for b in self.ecu_predictions if b.id == block_id), None)
        if found is None:
            raise KeyError(f"case {self.id} has no ECU-output prediction block {block_id!r}")
        return found

    def ecu_post_recordings(self) -> list[str]:
        """Every declared post-change recording: the frozen block's, then the
        appended ones, in declaration order."""
        base = [] if self.ecu_outputs is None else list(self.ecu_outputs.post)
        return base + [a.recording for a in self.ecu_post_appended]

    def ecu_amendment(self, version: int) -> EcuAmendment:
        found = next((a for a in self.ecu_amendments if a.version == version), None)
        if found is None:
            raise KeyError(f"case {self.id} has no ECU-output amendment {version}")
        return found

    def ecu_rules_sha256(self) -> str:
        """sha256 over what decides an ECU-output score beyond E07.4's rules:
        the prediction and its recorded hashes, the interval, the outputs and
        their channels, and the post-change recordings. The matching,
        acceptance and floor it shares are frozen by :attr:`frozen_rules`,
        which :func:`evaluate_ecu_outputs` requires (``rules_not_frozen``)."""
        if self.ecu_outputs is None:
            raise ValueError(f"case {self.id} declares no ecu_outputs")
        rules = self.ecu_outputs.model_dump(mode="json", exclude={"frozen_rules", "evidence"})
        return hashlib.sha256(json.dumps(rules, sort_keys=True).encode()).hexdigest()

    def rules_sha256(self) -> str:
        """sha256 over everything that decides a score: the prediction's bands,
        the learned-state policy, matching, acceptance, the floor rule, the
        pre-change recordings and the load input with its pairing tolerance."""
        payload = {
            "pre": list(self.split.pre),
            "load_input": self.load_input,
            "load_input_pair_tolerance_s": self.load_input_pair_tolerance_s,
            "bands": [b.model_dump(mode="json") for b in self.prediction_of_record.bands],
            "learned_state": self.learned_state.model_dump(mode="json"),
            "matching": self.matching.model_dump(mode="json"),
            "acceptance": self.acceptance.model_dump(mode="json"),
            "floor": self.floor.model_dump(mode="json"),
        }
        return hashlib.sha256(json.dumps(payload, sort_keys=True).encode()).hexdigest()

    @model_validator(mode="after")
    def _rules_frozen_before_post(self) -> InterventionCase:
        frozen = self.frozen_rules
        if frozen is not None and frozen.sha256 != self.rules_sha256():
            raise ValueError(
                f"rules_changed: case {self.id}'s scoring rules no longer hash to the frozen "
                f"{frozen.sha256[:12]}...; rules are fixed before any post-change drive is scored"
            )
        if self.split.post:
            if frozen is None:
                raise ValueError(
                    f"rules_not_frozen: case {self.id} lists post-change recordings without "
                    "frozen_rules"
                )
            at = (
                frozen.frozen_at
                if frozen.frozen_at.tzinfo
                else frozen.frozen_at.replace(tzinfo=UTC)
            )
            unknown = sorted(
                n
                for n in self.split.post
                if session_started_at(n) is None and not is_synthetic_session(n)
            )
            if unknown:
                raise ValueError(
                    f"recording_time_unknown: post-change {unknown} have no parseable start "
                    "time, so they cannot be shown to follow the frozen rules"
                )
            early = sorted(
                n
                for n in self.split.post
                if (started := session_started_at(n)) is not None and started <= at
            )
            if early:
                raise ValueError(
                    f"rules_frozen_after_post: {early} started before the rules were frozen "
                    f"({at.isoformat()})"
                )
        ecu = self.ecu_outputs
        if ecu is not None and ecu.frozen_rules.sha256 != self.ecu_rules_sha256():
            raise ValueError(
                f"rules_changed: case {self.id}'s ECU-output rules no longer hash to the frozen "
                f"{ecu.frozen_rules.sha256[:12]}...; they are fixed before any post-change read "
                "is scored"
            )
        self._check_amendments()
        self._check_post_appends()
        self._check_predictions()
        return self

    def _check_predictions(self) -> None:
        if not self.ecu_predictions:
            return
        if self.ecu_outputs is None:
            raise ValueError(
                f"case {self.id}: a prediction block shares ecu_outputs' post list and needs it"
            )
        ids = [b.id for b in self.ecu_predictions]
        if len(set(ids)) != len(ids) or "ecu_outputs" in ids:
            raise ValueError(f"prediction block ids {ids} must be unique and not 'ecu_outputs'")
        artifacts = [self.ecu_outputs.prediction.artifact] + [
            b.prediction.artifact for b in self.ecu_predictions
        ]
        if len(set(artifacts)) != len(artifacts):
            raise ValueError(
                f"prediction_artifact_duplicate: {artifacts} -- an artifact already scored "
                "under a frozen block is re-scored only by an amendment of that block"
            )
        for block in self.ecu_predictions:
            if block.frozen_rules.sha256 != block.rules_sha256():
                raise ValueError(
                    f"rules_changed: case {self.id}'s prediction block {block.id} no longer "
                    f"hashes to the frozen {block.frozen_rules.sha256[:12]}...; its rules are "
                    "fixed before any co-observed post-change pair is scored"
                )

    def _check_post_appends(self) -> None:
        if not self.ecu_post_appended:
            return
        if self.ecu_outputs is None:
            raise ValueError(f"case {self.id}: an appended post recording needs ecu_outputs")
        previous = self.ecu_outputs.frozen_rules.sha256
        seen = set(self.ecu_outputs.post)
        for k, entry in enumerate(self.ecu_post_appended):
            if entry.recording in seen:
                raise ValueError(f"post_recording_duplicate: {entry.recording} is already declared")
            seen.add(entry.recording)
            if session_started_at(entry.recording) is None and not is_synthetic_session(
                entry.recording
            ):
                raise ValueError(
                    f"recording_time_unknown: appended {entry.recording} has no parseable "
                    "start time"
                )
            expected = post_chain_sha256(previous, entry.recording, entry.appended_on)
            if entry.chain_sha256 != expected:
                raise ValueError(
                    f"post_list_rewritten: appended entry {k} ({entry.recording}) does not "
                    f"chain from the one before it ({previous[:12]}...): the post list is "
                    "append-only"
                )
            previous = entry.chain_sha256

    def _check_amendments(self) -> None:
        if not self.ecu_amendments:
            return
        if self.ecu_outputs is None:
            raise ValueError(f"case {self.id}: an ECU-output amendment needs ecu_outputs")
        previous = self.ecu_outputs.frozen_rules
        outputs = {o.id for o in self.ecu_outputs.outputs}
        for number, amendment in enumerate(self.ecu_amendments, start=2):
            if amendment.version != number:
                raise ValueError(
                    f"amendment versions run 2, 3, ...: found {amendment.version} at {number}"
                )
            if amendment.amends != previous.sha256:
                raise ValueError(
                    f"amendment_chain_broken: version {number} amends "
                    f"{amendment.amends[:12]}..., the rules before it are {previous.sha256[:12]}..."
                )
            if amendment.frozen_rules.sha256 != amendment.rules_sha256():
                raise ValueError(
                    f"rules_changed: case {self.id}'s ECU-output amendment {number} no longer "
                    f"hashes to the frozen {amendment.frozen_rules.sha256[:12]}..."
                )
            unknown = sorted(set(amendment.quantities) - outputs)
            if unknown:
                raise ValueError(f"amendment {number} names outputs {unknown} the case lacks")
            if amendment.frozen_rules.frozen_at < previous.frozen_at:
                raise ValueError(f"amendment {number} is frozen before the rules it amends")
            previous = amendment.frozen_rules


class InterventionFile(_Strict):
    schema_version: Literal[1]
    cases: list[InterventionCase] = Field(min_length=1)

    def case(self, case_id: str) -> InterventionCase:
        found = next((c for c in self.cases if c.id == case_id), None)
        if found is None:
            raise KeyError(case_id)
        return found


def load_interventions(profile_dir: Path) -> InterventionFile:
    raw = load_yaml((profile_dir / INTERVENTIONS_FILE).read_text())
    return InterventionFile.model_validate(raw)


@dataclass(frozen=True)
class Boundary:
    """The flash, as the revision registry has it."""

    at: datetime
    precision: str | None
    base: TuneRevision
    candidate: TuneRevision


def resolve_boundary(case: InterventionCase, registry: TuneRevisionRegistry) -> Boundary:
    """The case's flash from ``tune_revisions.yaml``, refusing any disagreement.

    ``boundary_imprecise``: a flash time that is not ``exact`` cannot split
    samples or order a prediction against it."""
    by_name = {r.revision: r for r in registry.revisions}
    base, cand = by_name.get(case.base_revision), by_name.get(case.candidate_revision)
    if base is None or cand is None:
        raise FuelPlantRefusalError(
            "revision_unknown",
            f"{case.base_revision!r} / {case.candidate_revision!r} not both in the registry",
        )
    order = [r.revision for r in registry.revisions]
    if order.index(cand.revision) != order.index(base.revision) + 1:
        raise FuelPlantRefusalError(
            "not_consecutive",
            f"{cand.revision} does not directly follow {base.revision}: another flash between",
        )
    for label, rev, sha in (
        ("base", base, case.base_image_sha256),
        ("candidate", cand, case.candidate_image_sha256),
    ):
        if rev.image_sha256 != sha:
            raise FuelPlantRefusalError(
                "image_mismatch",
                f"{label} {rev.revision}: registry image {rev.image_sha256}, case {sha}",
            )
    if cand.flashed_at_precision != "exact":
        raise FuelPlantRefusalError(
            "boundary_imprecise",
            f"{cand.revision} flashed_at is {cand.flashed_at_precision!r}, not exact",
        )
    at = cand.flashed_at if cand.flashed_at.tzinfo else cand.flashed_at.replace(tzinfo=UTC)
    return Boundary(at, cand.flashed_at_precision, base, cand)


def prediction_order(case: InterventionCase, boundary: Boundary) -> str:
    """``prior`` when the prediction's recording commit precedes the flash."""
    when = case.prediction_of_record.committed_at
    when = when if when.tzinfo else when.replace(tzinfo=UTC)
    return "prior" if when < boundary.at else "post_hoc"


def _git(repo: Path, *args: str) -> bytes:
    return subprocess.run(
        ["git", "-C", str(repo), *args], check=True, capture_output=True, timeout=30
    ).stdout


def verify_prediction_of_record(record: PredictionOfRecord, repo: Path = REPO_ROOT) -> list[str]:
    """Where the preserved record and its commit disagree; empty when they agree.

    Re-reads the document at the recording commit: its sha256, the commit's
    time, and every band's ``as_written`` row. Raises ``FuelPlantRefusalError``
    (``commit_unavailable``) when the commit is not in this clone."""
    try:
        text = _git(repo, "show", f"{record.commit}:{record.document}")
        stamp = _git(repo, "show", "-s", "--format=%cI", record.commit).decode().strip()
    except (subprocess.CalledProcessError, OSError, subprocess.TimeoutExpired) as exc:
        raise FuelPlantRefusalError(
            "commit_unavailable", f"{record.commit[:12]} is not readable here: {exc}"
        ) from exc
    problems: list[str] = []
    digest = hashlib.sha256(text).hexdigest()
    if digest != record.text_sha256:
        problems.append(f"document sha256 at the commit is {digest}, record {record.text_sha256}")
    committed = datetime.fromisoformat(stamp)
    stated = (
        record.committed_at
        if record.committed_at.tzinfo
        else record.committed_at.replace(tzinfo=UTC)
    )
    if committed != stated:
        problems.append(f"commit time is {committed.isoformat()}, record {stated.isoformat()}")
    body = text.decode()
    for band in record.bands:
        if band.as_written not in body:
            problems.append(f"row not in the document as written: {band.as_written!r}")
    return problems


Predictor = Callable[[FloatArray, FloatArray], tuple[FloatArray, FloatArray]]


def band_predictor(record: PredictionOfRecord) -> Predictor:
    """``(load_input, rpm) -> (lo, hi)`` percent fuel change from the bands;
    NaN where no band speaks (never extrapolated)."""

    def predict_change(load: FloatArray, rpm: FloatArray) -> tuple[FloatArray, FloatArray]:
        lo = np.full(len(load), np.nan)
        hi = np.full(len(load), np.nan)
        for band in record.bands:
            mask = (load >= band.load_input[0]) & (load < band.load_input[1])
            if band.rpm is not None:
                mask &= (rpm >= band.rpm[0]) & (rpm <= band.rpm[1])
            mask &= np.isnan(lo)
            lo[mask], hi[mask] = band.fuel_change_pct
        return lo, hi

    return predict_change


# -- samples -----------------------------------------------------------------------------


def build_case_samples(
    session: Path, profile: FuelPlantProfile, delay: B06Rules, case: InterventionCase
) -> pd.DataFrame:
    """E07.3's per-pulse-read samples plus the case's load input, paired on the
    acquisition clock within the case's tolerance (NaN when unpaired)."""
    frame = build_samples(session, profile, delay)
    if not len(frame):
        return frame.assign(load_input=pd.Series(dtype=float))
    long = read_channels(session, [case.load_input], acquired_time=True)
    frame = frame.copy()
    frame["load_input"] = _pair(
        frame["t"].to_numpy(dtype=float),
        _series(long, case.load_input),
        case.load_input_pair_tolerance_s,
    )
    return frame


def load_input_pairing(samples: pd.DataFrame, case: InterventionCase) -> dict[str, Any]:
    """How many eligible reads carry the case's load input, paired within its
    tolerance. Every effect test matches pairs on the load input, so a side
    with eligible reads and none paired can form no pair at all -- an empty
    result that must say which kind of empty it is."""
    eligible = samples[samples["state"] == "eligible"] if len(samples) else samples
    if "load_input" in eligible:
        paired = int(np.isfinite(eligible["load_input"].to_numpy(dtype=float)).sum())
    else:
        paired = 0
    return {
        "channel": case.load_input,
        "tolerance_s": case.load_input_pair_tolerance_s,
        "eligible_reads": len(eligible),
        "paired": paired,
    }


def _unpaired_reason(side: str, pairing: dict[str, Any]) -> str | None:
    if not pairing["eligible_reads"] or pairing["paired"]:
        return None
    return (
        f"load_input_unpaired: {side}: 0 of {pairing['eligible_reads']} eligible reads have "
        f"{pairing['channel']} within {pairing['tolerance_s']} s, so no pair can be matched "
        "on the load input"
    )


def _span(frame: pd.DataFrame) -> tuple[float, float]:
    t = frame["t"].to_numpy(dtype=float)
    return float(np.nanmin(t)), float(np.nanmax(t))


def split_by_boundary(
    frames: Iterable[pd.DataFrame], boundary: Boundary
) -> tuple[list[pd.DataFrame], list[pd.DataFrame]]:
    """Pre- and post-change recordings by *sample time*, never by name.

    ``straddles_intervention``: a recording with reads on both sides."""
    cut = boundary.at.timestamp()
    pre: list[pd.DataFrame] = []
    post: list[pd.DataFrame] = []
    for frame in frames:
        if not len(frame):
            continue
        lo, hi = _span(frame)
        name = str(frame["session"].iloc[0])
        if lo < cut <= hi:
            raise FuelPlantRefusalError(
                "straddles_intervention",
                f"{name} has reads before and after {boundary.at.isoformat()}",
            )
        (post if lo >= cut else pre).append(frame)
    return pre, post


def check_one_plant(profile: FuelPlantProfile, case: InterventionCase, boundary: Boundary) -> None:
    """A separate plant per ROM cannot count: the one baseline must have been
    identified on the base revision only, before the flash, on no recording the
    case reserves as post-change."""
    baseline = profile.baseline
    if baseline is None or not baseline.frozen:
        raise FuelPlantRefusalError(
            "baseline_not_frozen", "freeze the plant before testing an intervention"
        )
    if case.candidate_revision in baseline.revisions:
        raise FuelPlantRefusalError(
            "plant_fitted_on_candidate_rom",
            f"baseline {baseline.id} was identified on {case.candidate_revision}: a plant fitted "
            "after the change cannot test the change",
        )
    if baseline.revisions != [case.base_revision]:
        raise FuelPlantRefusalError(
            "baseline_revision_mismatch",
            f"baseline {baseline.id} was identified on {baseline.revisions}, the case's base is "
            f"{case.base_revision}",
        )
    fitted = set(baseline.fitted_on) | set(profile.split.identification)
    reserved = sorted(fitted & set(case.split.post))
    unknown = sorted(
        n for n in fitted if session_started_at(n) is None and not is_synthetic_session(n)
    )
    if unknown:
        raise FuelPlantRefusalError(
            "recording_time_unknown",
            f"the plant was identified on {unknown}, whose start time does not parse: it "
            "cannot be shown to precede the flash",
        )
    late = sorted(
        n for n in fitted if (when := session_started_at(n)) is not None and when >= boundary.at
    )
    if reserved or late:
        raise FuelPlantRefusalError(
            "refit_across_intervention",
            f"the plant was identified on post-change recordings {sorted({*reserved, *late})}",
        )


# -- per-sample replay -------------------------------------------------------------------


def _lambda(frame: pd.DataFrame, profile: FuelPlantProfile, delay: B06Rules) -> FloatArray:
    params = resolve_parameters(profile, delay)
    out: FloatArray = predict(plant_inputs(frame), params, profile.geometry).lambda_delivered
    return out


def acceptance_checks(stats: dict[str, Any], acc: Acceptance) -> dict[str, dict[str, Any]]:
    """The plant's own predeclared bounds applied to one block of steady reads."""
    enough = stats["n_regions"] >= acc.min_regions
    return {
        "min_regions": {"value": stats["n_regions"], "bound": acc.min_regions, "pass": enough},
        "pooled_median_abs_pct": {
            "value": abs(stats["median_pct"]),
            "bound": acc.pooled_median_abs_pct,
            "pass": abs(stats["median_pct"]) <= acc.pooled_median_abs_pct,
        },
        "sample_abs_p90_pct": {
            "value": stats["abs_p90_pct"],
            "bound": acc.sample_abs_p90_pct,
            "pass": stats["abs_p90_pct"] <= acc.sample_abs_p90_pct,
        },
        "region_pass_fraction": {
            "value": stats["region_pass_fraction"],
            "bound": acc.region_pass_fraction,
            "pass": stats["region_pass_fraction"] >= acc.region_pass_fraction,
        },
    }


def replay_report(
    frames: Sequence[pd.DataFrame], profile: FuelPlantProfile, delay: B06Rules
) -> tuple[dict[str, Any], pd.DataFrame]:
    """Frozen-plant replay of recorded actuator commands: steady, transient and
    missing reads reported apart, and the lambda delay's bracket as a separate
    observation-latency sensitivity. Returns the summary and the per-sample frame."""
    data = pd.concat([f for f in frames if len(f)], ignore_index=True) if frames else None
    if data is None or not len(data):
        return {"n_reads": 0, "verdict": "not_run", "reason": "no reads"}, pd.DataFrame()
    inputs = ["pulse_ms", "ecu_dead_time_ms", "rpm", "maf_gps", "baro_kpa", "iat_c"]
    finite = np.isfinite(data[[*inputs, "lambda_obs"]].to_numpy(dtype=float)).all(axis=1)
    data = data.assign(lambda_pred=np.nan, err_pct=np.nan)
    if finite.any():
        lam = _lambda(data[finite], profile, delay)
        data.loc[finite, "lambda_pred"] = lam
        data.loc[finite, "err_pct"] = 100.0 * (
            lam / data.loc[finite, "lambda_obs"].to_numpy(dtype=float) - 1.0
        )
    steady = data[data["state"] == "eligible"]
    transient = data[(data["state"] == "transient") & np.isfinite(data["err_pct"])]
    steady_stats = _stats(
        steady["err_pct"].to_numpy(dtype=float), steady["region_id"].to_numpy(), profile
    )
    transient_err = transient["err_pct"].to_numpy(dtype=float)
    latency: dict[str, float] = {}
    if len(steady):
        params_lam = steady["lambda_pred"].to_numpy(dtype=float)
        for end in ("lo", "hi"):
            obs = steady[f"lambda_obs_{end}"].to_numpy(dtype=float)
            ok = np.isfinite(obs)
            shifted = 100.0 * (params_lam[ok] / obs[ok] - 1.0)
            latency[f"lambda_observation_delay={end}"] = (
                float(np.median(shifted)) - steady_stats["median_pct"] if ok.any() else math.nan
            )
    checks = acceptance_checks(steady_stats, profile.acceptance) if len(steady) else {}
    summary = {
        "n_reads": len(data),
        "states": {k: int(v) for k, v in data["state"].value_counts().items()},
        "steady": steady_stats,
        "transient": {
            "n_samples": len(transient_err),
            "median_pct": float(np.median(transient_err)) if len(transient_err) else math.nan,
            "abs_p90_pct": (
                float(np.quantile(np.abs(transient_err), 0.9)) if len(transient_err) else math.nan
            ),
        },
        "missing": {s: int((data["state"] == s).sum()) for s in MISSING_STATES},
        "observation_latency_median_shift_pct": latency,
        "checks": checks,
        "verdict": (
            "not_run"
            if not checks
            else "pass"
            if all(c["pass"] for c in checks.values())
            else "fail"
        ),
        "sessions": sorted(set(data["session"])),
    }
    return summary, data


# -- the effect test ---------------------------------------------------------------------


def match_pairs(
    a: pd.DataFrame,
    b: pd.DataFrame,
    m: Matching,
    *,
    floor: FloorRule | None = None,
    load: Sequence[str] = ("load_input",),
) -> tuple[np.ndarray, np.ndarray]:
    """One-to-one pairs ``(i in a, j in b)`` at the same regime and conditions,
    nearest first. With ``floor`` (the pre-vs-pre noise floor), a pair inside
    one recording must be at least ``floor.min_separation_s`` apart.

    ``load`` names the load-input columns, each held within
    ``m.load_input_abs``: one logged byte, or both ends of a reconstructed
    byte interval (``load_lo``, ``load_hi``), so two intervals match only when
    every end is within the tolerance."""
    cols = ["rpm", *load, "baro_kpa", "iat_c"]
    av, bv = a[cols].to_numpy(dtype=float), b[cols].to_numpy(dtype=float)
    ar, br = a["regime"].to_numpy(), b["regime"].to_numpy()
    asess, bsess = a["session"].to_numpy(), b["session"].to_numpy()
    at, bt = a["t"].to_numpy(dtype=float), b["t"].to_numpy(dtype=float)
    scale = np.array([m.rpm_rel, *([m.load_input_abs] * len(load)), m.baro_kpa_abs, m.iat_c_abs])
    found: list[tuple[float, int, int]] = []
    for i in range(len(a)):
        if not np.isfinite(av[i]).all():
            continue
        diff = np.abs(bv - av[i])
        diff[:, 0] = diff[:, 0] / av[i, 0]
        norm = diff / scale
        ok = (norm <= 1.0).all(axis=1) & (br == ar[i])
        if floor is not None:
            ok &= (bsess != asess[i]) | (np.abs(bt - at[i]) >= floor.min_separation_s)
        for j in np.flatnonzero(ok):
            found.append((float(norm[j].sum()), i, int(j)))
    found.sort()
    used_a: set[int] = set()
    # Pairing a frame with itself: a read is used once in either role, so the
    # mirror pair (j, i) never counts as a second, independent pair.
    used_b: set[int] = used_a if a is b else set()
    ia: list[int] = []
    jb: list[int] = []
    for _, pa, pb in found:
        if pa in used_a or pb in used_b:
            continue
        used_a.add(pa)
        used_b.add(pb)
        ia.append(pa)
        jb.append(pb)
    return np.asarray(ia, dtype=int), np.asarray(jb, dtype=int)


def _plant_lambda_at(
    post: pd.DataFrame,
    effective_ms: FloatArray,
    profile: FuelPlantProfile,
    delay: B06Rules,
) -> FloatArray:
    """The frozen plant at the post-change reads' conditions, with an effective
    pulse the *prediction* supplies (never the post-change read's own)."""
    inputs = plant_inputs(post)
    pulse = inputs.ecu_dead_time_ms + effective_ms
    params = resolve_parameters(profile, delay)
    stated = PlantInputs(
        pulse_ms=pulse,
        ecu_dead_time_ms=inputs.ecu_dead_time_ms,
        rpm=inputs.rpm,
        maf_gps=inputs.maf_gps,
        baro_kpa=inputs.baro_kpa,
        iat_c=inputs.iat_c,
        rail_kpa=inputs.rail_kpa,
    )
    out: FloatArray = predict(stated, params, profile.geometry).lambda_delivered
    return out


def _ltft_state(ltft: FloatArray, policy: LearnedStatePolicy) -> np.ndarray:
    state = np.full(len(ltft), "reset_holds", dtype=object)
    state[np.abs(ltft - policy.reset_value_pct) > policy.relearn_tolerance_pct] = "ltft_relearning"
    state[~np.isfinite(ltft)] = "ltft_unobserved"
    return state


def _floor(residual: FloatArray, acc: EffectAcceptance) -> float | None:
    if len(residual) < acc.min_floor_pairs:
        return None
    return float(np.quantile(np.abs(residual), acc.floor_quantile))


def _verdict(
    n_pairs: int, floor: float | None, direction: float, within: float, acc: EffectAcceptance
) -> tuple[TestOutcome, str | None]:
    if n_pairs < acc.min_pairs:
        return "insufficient_evidence", f"{n_pairs} pairs < {acc.min_pairs}"
    if floor is None:
        return "insufficient_evidence", "condition-matched noise floor unavailable"
    if direction >= acc.direction_agreement and within >= acc.within_uncertainty:
        return "pass", None
    return "fail", (
        f"direction agreement {direction:.2f} (>= {acc.direction_agreement}), within "
        f"uncertainty {within:.2f} (>= {acc.within_uncertainty})"
    )


def _summ(values: FloatArray) -> dict[str, float]:
    if not len(values):
        return {"median": math.nan, "p10": math.nan, "p90": math.nan}
    return {
        "median": float(np.median(values)),
        "p10": float(np.quantile(values, 0.1)),
        "p90": float(np.quantile(values, 0.9)),
    }


def lambda_effect_test(
    pre: pd.DataFrame,
    post: pd.DataFrame,
    case: InterventionCase,
    profile: FuelPlantProfile,
    delay: B06Rules,
    predictor: Predictor,
) -> dict[str, Any]:
    """Open-loop (enrichment) reads: does delivered fuel move as predicted?

    Per pair, the pre-change read's effective pulse is scaled by the predicted
    change and by the declared trim state: LTFT at its reset value after the
    flash, acting in open loop as the decoded pulse formula adds it with no
    open-loop gate (docs/ecu-firmware-analysis.md:629); STFT neutral in open
    loop is an assumption, not decoded, and the frozen plant evaluates it at the
    post-change read's conditions. The observed change is the post read's
    lambda against the no-change prediction (``fuel_change = lambda_null /
    lambda_obs - 1``); the floor is the same quantity over pre-vs-pre pairs."""
    acc, policy = case.acceptance, case.learned_state
    pre_o = pre[pre["regime"] == "enrichment"].reset_index(drop=True)
    post_o = post[post["regime"] == "enrichment"].reset_index(drop=True)

    def change(a: pd.DataFrame, b: pd.DataFrame, ltft_after: FloatArray) -> FloatArray:
        te = (b["pulse_ms"] - b["ecu_dead_time_ms"]).to_numpy(dtype=float)
        trim = (100.0 + ltft_after) / (100.0 + b["ltft_pct"].to_numpy(dtype=float))
        lam_null = _plant_lambda_at(a, te * trim, profile, delay)
        out: FloatArray = 100.0 * (lam_null / a["lambda_obs"].to_numpy(dtype=float) - 1.0)
        return out

    fi, fj = match_pairs(pre_o, pre_o, case.matching, floor=case.floor)
    floor_res = (
        change(
            pre_o.iloc[fi].reset_index(drop=True),
            pre_o.iloc[fj].reset_index(drop=True),
            pre_o["ltft_pct"].to_numpy(dtype=float)[fi],
        )
        if len(fi)
        else np.array([], dtype=float)
    )
    floor_res = floor_res[np.isfinite(floor_res)]
    floor = _floor(floor_res, acc)

    ltft_state = _ltft_state(post_o["ltft_pct"].to_numpy(dtype=float), policy)
    counts = {str(k): int(v) for k, v in pd.Series(ltft_state).value_counts().items()}
    usable = post_o[ltft_state == "reset_holds"].reset_index(drop=True)
    load = usable["load_input"].to_numpy(dtype=float)
    counts["load_input_unpaired"] = int((~np.isfinite(load)).sum())
    lo, hi = predictor(load, usable["rpm"].to_numpy(dtype=float))
    predicted = np.isfinite(lo)
    counts["not_predicted"] = int((~predicted & np.isfinite(load)).sum())
    usable = usable[predicted].reset_index(drop=True)
    lo, hi = lo[predicted], hi[predicted]
    i, j = match_pairs(usable, pre_o, case.matching)
    a, b = usable.iloc[i].reset_index(drop=True), pre_o.iloc[j].reset_index(drop=True)
    lo, hi = lo[i], hi[i]
    observed = change(a, b, np.full(len(a), policy.reset_value_pct)) if len(a) else np.array([])
    ok = np.isfinite(observed)
    observed, lo, hi = observed[ok], lo[ok], hi[ok]
    mid = (lo + hi) / 2.0
    signed = mid != 0
    direction = (
        float(np.mean(np.sign(observed[signed]) == np.sign(mid[signed]))) if signed.any() else 0.0
    )
    u = floor if floor is not None else math.nan
    within = float(np.mean((observed >= lo - u) & (observed <= hi + u))) if len(observed) else 0.0
    outcome, why = _verdict(len(observed), floor, direction, within, acc)
    return {
        "regime": "enrichment",
        "n_pairs": len(observed),
        "post_reads": len(post_o),
        "exclusions": counts,
        "observed_fuel_change_pct": _summ(observed),
        "predicted_fuel_change_pct": _summ(mid),
        "residual_pct": _summ(observed - mid),
        "noise_floor_pct": floor,
        "floor_pairs": len(floor_res),
        "direction_agreement": direction,
        "within_uncertainty": within,
        "outcome": outcome,
        "reason": why,
    }


def trim_effect_test(
    pre: pd.DataFrame,
    post: pd.DataFrame,
    case: InterventionCase,
    predictor: Predictor,
) -> dict[str, Any]:
    """Closed-loop reads: does the short-term trim move as predicted?

    The controller holds lambda at target, so a fuel change shows in the trims:
    total trim needed after = (1 + total before) / (1 + change) - 1. The flash
    puts LTFT at its declared reset value, so the short-term trim carries the
    rest; a read whose LTFT has moved off that value is excluded as
    ``ltft_relearning``. Recorded post-change trims are only the observation."""
    acc, policy = case.acceptance, case.learned_state
    pre_c = pre[pre["regime"] == "closed_loop"].reset_index(drop=True)
    post_c = post[post["regime"] == "closed_loop"].reset_index(drop=True)

    def predicted_stft(
        b: pd.DataFrame, change_pct: FloatArray, ltft_after: FloatArray
    ) -> FloatArray:
        total = b["stft_pct"].to_numpy(dtype=float) + b["ltft_pct"].to_numpy(dtype=float)
        needed = 100.0 * ((1.0 + total / 100.0) / (1.0 + change_pct / 100.0) - 1.0)
        out: FloatArray = needed - ltft_after
        return out

    fi, fj = match_pairs(pre_c, pre_c, case.matching, floor=case.floor)
    if len(fi):
        a0, b0 = pre_c.iloc[fi], pre_c.iloc[fj]
        floor_res = a0["stft_pct"].to_numpy(dtype=float) - predicted_stft(
            b0, np.zeros(len(fi)), a0["ltft_pct"].to_numpy(dtype=float)
        )
        floor_res = floor_res[np.isfinite(floor_res)]
    else:
        floor_res = np.array([], dtype=float)
    floor = _floor(floor_res, acc)

    ltft_state = _ltft_state(post_c["ltft_pct"].to_numpy(dtype=float), policy)
    counts = {str(k): int(v) for k, v in pd.Series(ltft_state).value_counts().items()}
    usable = post_c[ltft_state == "reset_holds"].reset_index(drop=True)
    load = usable["load_input"].to_numpy(dtype=float)
    counts["load_input_unpaired"] = int((~np.isfinite(load)).sum())
    lo, hi = predictor(load, usable["rpm"].to_numpy(dtype=float))
    predicted = np.isfinite(lo)
    counts["not_predicted"] = int((~predicted & np.isfinite(load)).sum())
    usable = usable[predicted].reset_index(drop=True)
    lo, hi = lo[predicted], hi[predicted]
    i, j = match_pairs(usable, pre_c, case.matching)
    a, b = usable.iloc[i].reset_index(drop=True), pre_c.iloc[j].reset_index(drop=True)
    lo, hi = lo[i], hi[i]
    reset = np.full(len(a), policy.reset_value_pct)
    observed = a["stft_pct"].to_numpy(dtype=float) - b["stft_pct"].to_numpy(dtype=float)
    base = b["stft_pct"].to_numpy(dtype=float)
    # A richer change (hi) needs less trim: the predicted range is reversed.
    d_hi = predicted_stft(b, lo, reset) - base
    d_lo = predicted_stft(b, hi, reset) - base
    d_mid = predicted_stft(b, (lo + hi) / 2.0, reset) - base
    # The part of the move the ROM change alone predicts (LTFT reset removed).
    rom_only = predicted_stft(b, (lo + hi) / 2.0, b["ltft_pct"].to_numpy(dtype=float)) - base
    ok = np.isfinite(observed) & np.isfinite(d_mid)
    observed, d_lo, d_hi, d_mid, rom_only = (
        observed[ok],
        d_lo[ok],
        d_hi[ok],
        d_mid[ok],
        rom_only[ok],
    )
    signed = d_mid != 0
    direction = (
        float(np.mean(np.sign(observed[signed]) == np.sign(d_mid[signed]))) if signed.any() else 0.0
    )
    u = floor if floor is not None else math.nan
    within = (
        float(np.mean((observed >= d_lo - u) & (observed <= d_hi + u))) if len(observed) else 0.0
    )
    outcome, why = _verdict(len(observed), floor, direction, within, acc)
    return {
        "regime": "closed_loop",
        "n_pairs": len(observed),
        "post_reads": len(post_c),
        "exclusions": counts,
        "observed_stft_change_pct": _summ(observed),
        "predicted_stft_change_pct": _summ(d_mid),
        "predicted_from_rom_change_only_pct": _summ(rom_only),
        "noise_floor_pct": floor,
        "floor_pairs": len(floor_res),
        "direction_agreement": direction,
        "within_uncertainty": within,
        "outcome": outcome,
        "reason": why,
    }


# -- evaluation -------------------------------------------------------------------------


def evaluate_intervention(
    case: InterventionCase,
    boundary: Boundary,
    profile: FuelPlantProfile,
    delay: B06Rules,
    frames: Sequence[pd.DataFrame],
    *,
    sessions_root: Path,
    predictor: Predictor | None = None,
) -> tuple[dict[str, Any], pd.DataFrame]:
    """The intervention test with one frozen plant across the boundary.

    Returns the report and the per-sample frame (``side``, ``lambda_pred``,
    ``err_pct``, ``ltft_state``). ``verdict`` is ``pending`` (with ``pending``
    naming each reason) until the plant is qualified and post-change data
    exist; otherwise ``pass``/``fail``/``insufficient_evidence``."""
    if case.synthetic != profile.synthetic:
        raise FuelPlantRefusalError(
            "synthetic_mismatch",
            "a synthetic case runs a synthetic plant and a recorded case a recorded plant",
        )
    sessions = sorted({str(f["session"].iloc[0]) for f in frames if len(f)})
    check_origin(sessions, profile)
    undeclared = sorted(set(sessions) - set(case.split.pre) - set(case.split.post))
    if undeclared:
        raise FuelPlantRefusalError(
            "undeclared_recording",
            f"{undeclared} are not in the case's split: declare recordings before scoring them",
        )
    check_one_plant(profile, case, boundary)
    if not profile.synthetic:
        check_drive_independence(profile, sessions_root)
    pre, post = split_by_boundary(frames, boundary)
    wrong = sorted(
        {str(f["session"].iloc[0]) for f in pre} & set(case.split.post)
        | {str(f["session"].iloc[0]) for f in post} & set(case.split.pre)
    )
    if wrong:
        raise FuelPlantRefusalError(
            "wrong_side", f"{wrong} are declared on the other side of the boundary"
        )
    qualification = plant_qualification(profile)
    order = prediction_order(case, boundary)
    pending: list[str] = []
    if not qualification.qualified:
        assert qualification.reason is not None
        pending.append(qualification.reason)
    if order != "prior":
        pending.append("prediction_not_prior: recorded at or after the flash")
    pre_report, pre_samples = replay_report(pre, profile, delay)
    post_report, post_samples = replay_report(post, profile, delay)
    if not post or not post_report.get("n_reads"):
        pending.append("no_post_change_data: no recording after the flash is in this split")
    pairing = {
        side: load_input_pairing(frame, case)
        for side, frame in (("pre", pre_samples), ("post", post_samples))
    }
    for side, summary in pairing.items():
        why = _unpaired_reason(side, summary)
        if why is not None:
            pending.append(why)
    if len(post_samples):
        post_samples = post_samples.assign(
            ltft_state=_ltft_state(
                post_samples["ltft_pct"].to_numpy(dtype=float), case.learned_state
            )
        )
    samples = pd.concat(
        [pre_samples.assign(side="pre"), post_samples.assign(side="post")]
        if len(post_samples)
        else [pre_samples.assign(side="pre")],
        ignore_index=True,
    )
    predictor = predictor or band_predictor(case.prediction_of_record)
    tests: dict[str, dict[str, Any]] = {}
    if len(pre_samples) and len(post_samples):
        pre_ok = pre_samples[pre_samples["state"] == "eligible"]
        post_ok = post_samples[post_samples["state"] == "eligible"]
        tests["lambda_open_loop"] = lambda_effect_test(
            pre_ok, post_ok, case, profile, delay, predictor
        )
        tests["trim_closed_loop"] = trim_effect_test(pre_ok, post_ok, case, predictor)
        if "enrichment" not in qualification.regimes and qualification.qualified:
            tests["lambda_open_loop"]["outcome"] = "not_run"
            tests["lambda_open_loop"]["reason"] = "plant qualified for closed loop only"
    verdict: str
    if pending:
        verdict = "pending"
    else:
        outcomes = [t["outcome"] for t in tests.values()]
        if post_report["verdict"] == "fail" or "fail" in outcomes:
            verdict = "fail"
        elif "pass" in outcomes and post_report["verdict"] == "pass":
            verdict = "pass"
        else:
            verdict = "insufficient_evidence"
    report = {
        "case": case.id,
        "synthetic": profile.synthetic,
        "claim": (
            "synthetic demonstration of the path; not evidence about the vehicle"
            if profile.synthetic
            else "frozen-plant intervention test"
        ),
        "verdict": verdict,
        "pending": pending,
        "boundary": boundary.at.isoformat(),
        "base_revision": case.base_revision,
        "candidate_revision": case.candidate_revision,
        "prediction_order": order,
        "prediction_ledger": case.prediction_of_record.ledger.model_dump(),
        "plant": {
            "plant_version": profile.plant_version,
            "baseline_sha256": qualification.baseline_sha256,
            "qualified": qualification.qualified,
            "qualified_regimes": list(qualification.regimes),
        },
        "baseline_reproduction": pre_report,
        "post_change_replay": post_report,
        "load_input_pairing": pairing,
        "effect_tests": tests,
        "learned_state": case.learned_state.model_dump(),
        "base_image_sha256": case.base_image_sha256,
        "candidate_image_sha256": case.candidate_image_sha256,
    }
    report["verdict_record"] = InterventionVerdict._mint(
        case=case.id,
        verdict=verdict,
        synthetic=profile.synthetic,
        base_image_sha256=case.base_image_sha256,
        candidate_image_sha256=case.candidate_image_sha256,
        baseline_sha256=qualification.baseline_sha256,
        plant_version=profile.plant_version,
        qualified_regimes=tuple(qualification.regimes),
    )
    return report, samples


# -- E09.12: the plant-free (ECU-side) outputs ----------------------------------------

#: The fuel-trim scaling the application model declares its trim words in
#: (``0x6400`` = 1.0); :func:`trim_independence` reads them from the model.
TRIM_UNITS = "q_0x6400"


@dataclass(frozen=True)
class EcuOutputVerdict:
    """The outcome of one :func:`evaluate_ecu_outputs` run, sealed.

    Sealed with :class:`InterventionVerdict`'s process-private HMAC key over a
    body that names its kind, so a verdict typed by a caller, edited with
    ``dataclasses.replace``, or copied field by field into the other class
    fails :meth:`verify`. It is a verdict on decoded ECU logic, never on the
    plant: it carries no plant baseline, so it cannot stand in for
    :func:`evaluate_intervention`'s verdict when a tuning effect is ranked."""

    case: str
    #: ``(output id, outcome, n_pairs, floor_pairs)`` per output.
    outcomes: tuple[tuple[str, str, int, int], ...]
    synthetic: bool
    base_image_sha256: str
    candidate_image_sha256: str
    traces_sha256: str
    #: Which frozen block: ``ecu_outputs`` (its amendments by version), or an
    #: :class:`EcuPredictionBlock`'s id.
    block: str
    #: The rules it was scored under: 1 is the block itself, 2.. an
    #: :class:`EcuAmendment` of ``ecu_outputs``; with that version's frozen sha256.
    rules_version: int
    rules_sha256: str
    #: The post-change recordings this verdict scored, by name.
    recordings: tuple[str, ...]
    #: ``all``: every declared recording on the candidate revision; ``named``:
    #: a subset chosen by name, with the candidates it left out. A sealed
    #: subset verdict never reads as a full one.
    scope: Literal["all", "named"]
    candidates_not_scored: tuple[str, ...]
    seal: str

    KIND = "ecu_output_verdict"

    @classmethod
    def _digest(cls, fields: dict[str, Any]) -> str:
        return InterventionVerdict._digest({"kind": cls.KIND, **fields})

    @classmethod
    def _mint(cls, **fields: Any) -> EcuOutputVerdict:
        return cls(**fields, seal=cls._digest(fields))

    def verify(self) -> bool:
        fields = {k: v for k, v in asdict(self).items() if k != "seal"}
        return hmac.compare_digest(self.seal, self._digest(fields))

    def outcome(self, output: str) -> str:
        for name, outcome, _, _ in self.outcomes:
            if name == output:
                return outcome
        raise KeyError(output)


def _file_sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as fh:
        for block in iter(lambda: fh.read(1 << 20), b""):
            digest.update(block)
    return digest.hexdigest()


def verify_prediction_artifact(directory: Path, prediction: PredictionArtifact) -> dict[str, str]:
    """Check the prediction's files against their recorded hashes, before
    anything is scored against it; return the hashes checked.

    ``prediction_artifact_missing``: a recorded file is absent.
    ``prediction_artifact_changed``: a file's bytes, the result's digest or
    the design it names differ from the record. A changed prediction is not
    the prediction of record: nothing re-runs or re-dates it here."""
    checked: dict[str, str] = {}
    for rel, recorded in {**prediction.files, **prediction.design_files}.items():
        path = directory / rel
        if not path.is_file():
            raise FuelPlantRefusalError(
                "prediction_artifact_missing", f"{path} is recorded but absent"
            )
        found = _file_sha256(path)
        if found != recorded:
            raise FuelPlantRefusalError(
                "prediction_artifact_changed",
                f"{rel}: sha256 {found}, recorded {recorded} ({prediction.recorded_in})",
            )
        checked[rel] = found
    result = json.loads((directory / prediction.result).read_text())
    design = json.loads((directory / prediction.design_record).read_text())
    named = {
        "result_digest": (result.get("result_digest"), prediction.result_digest),
        "result design": (
            (result.get("cell_table") or {}).get("design_sha256"),
            prediction.design_sha256,
        ),
        "design": (design.get("sha256"), prediction.design_sha256),
    }
    for label, (found_value, recorded) in named.items():
        if found_value != recorded:
            raise FuelPlantRefusalError(
                "prediction_artifact_changed", f"{label} is {found_value}, recorded {recorded}"
            )
    return checked


def trim_independence(model: ApplicationModel, output_ids: Sequence[str]) -> dict[str, Any]:
    """Whether an output's decoded chain reads a fuel trim: the flash reset
    LTFT, so an output that depends on a trim is not comparable across it.

    Walks every node the output's writer depends on (through ``observed``
    nodes too: this is a dependency audit, not an evaluation) and refuses
    (``output_reads_trim``) when one reads or writes a word the model declares
    in the trim scaling (:data:`TRIM_UNITS`). Long-term trim has no RAM word
    in the model; the report says so rather than claiming it was checked."""
    by_id = {n.id: n for n in model.nodes}
    outputs = {o.id: o for o in model.outputs}
    trims = {v.address: v.name for v in model.ram if v.units == TRIM_UNITS}
    named_ltft = sorted(v.name for v in model.ram if "long_term" in v.name)
    out: dict[str, Any] = {
        "trim_words": {f"0x{a:08X}": n for a, n in sorted(trims.items())},
        "long_term_trim_in_model": named_ltft,
        "outputs": {},
    }
    for output_id in output_ids:
        if output_id not in outputs:
            raise FuelPlantRefusalError(
                "model_output_unknown", f"{output_id} is not an application-model output"
            )
        start = outputs[output_id].written_by
        seen: set[str] = set()
        stack = [start]
        while stack:
            current = stack.pop()
            if current in seen:
                continue
            seen.add(current)
            stack.extend(by_id[current].depends_on)
        touched = sorted(
            f"{node}:0x{a:08X}"
            for node in seen
            for a in (*by_id[node].reads, *by_id[node].writes)
            if a in trims
        )
        if touched:
            raise FuelPlantRefusalError(
                "output_reads_trim",
                f"{output_id}'s chain touches trim words {touched}: the flash's LTFT reset would "
                "move it apart from the ROM change",
            )
        out["outputs"][output_id] = {"nodes": sorted(seen), "trim_words_read": []}
    return out


def prediction_points(
    directory: Path,
    rules: EcuOutputRules | EcuPredictionBlock,
    *,
    rpm_per_count: float,
    ceilings: Mapping[str, float] | None = None,
) -> pd.DataFrame:
    """The prediction's design points with each output's predicted relative
    change, percent, from its draws: ``pred_lo/mid/hi:<id>`` (the declared
    quantiles and the median, inverted CDF), ``draws:<id>`` the supported
    draws (a point with none is ``not_predicted``), and ``cell``: the point's
    rpm word and fill-interval midpoint on the prediction's own cell ranges.

    ``ceilings`` (output id -> counts, an amendment's ``bfs_ceiling_counts``
    for an ``air_normalised`` output): a draw whose base or candidate is at or
    above it is clamped, not the multiplier's change; it is left out of the
    interval and counted in ``draws_at_ceiling:<id>``.

    Call :func:`verify_prediction_artifact` first; this reads the files as
    they are."""
    prediction = rules.prediction
    design = pd.read_parquet(directory / prediction.design)
    traces = pd.read_parquet(directory / prediction.traces)
    result = json.loads((directory / prediction.result).read_text())
    table = result["cell_table"]
    frame = pd.DataFrame(
        {
            "point": np.arange(len(design)),
            "session": design["session"].astype(str).to_numpy(),
            "t": design["t"].to_numpy(dtype=float),
            "regime": design["regime"].astype(str).to_numpy(),
            "rpm": design["rpm"].to_numpy(dtype=float),
            "load_lo": design["fill_byte_lo"].to_numpy(dtype=float),
            "load_hi": design["fill_byte_hi"].to_numpy(dtype=float),
            "baro_kpa": design["baro_kpa"].to_numpy(dtype=float),
            "iat_c": design["iat_c"].to_numpy(dtype=float),
            "maf_gps": design["maf_gps"].to_numpy(dtype=float),
        }
    )
    lo_q, hi_q = rules.interval_quantiles
    for spec in rules.outputs:
        rows = traces[(traces["output"] == spec.trace_output) & (traces["status"] == "supported")]
        rows = rows[np.isfinite(rows["relative_delta"].to_numpy(dtype=float))]
        ceiling = (ceilings or {}).get(spec.id)
        clamped = np.zeros(len(rows), dtype=bool)
        if ceiling is not None:
            clamped = (rows["base"].to_numpy(dtype=float) >= ceiling) | (
                rows["candidate"].to_numpy(dtype=float) >= ceiling
            )
            at_ceiling = rows[clamped].groupby("point").size()
            rows = rows[~clamped]
        if not len(rows):
            raise FuelPlantRefusalError(
                "output_not_in_traces", f"{spec.trace_output} has no supported trace rows"
            )
        grouped = rows.groupby("point")["relative_delta"]
        stats = pd.DataFrame(
            {
                f"pred_lo:{spec.id}": grouped.quantile(lo_q, interpolation="lower"),
                f"pred_mid:{spec.id}": grouped.quantile(0.5, interpolation="lower"),
                f"pred_hi:{spec.id}": grouped.quantile(hi_q, interpolation="higher"),
                f"draws:{spec.id}": grouped.size(),
            }
        )
        for col in stats.columns:
            if col.startswith("pred_"):
                stats[col] = 100.0 * stats[col]
        frame = frame.merge(stats, left_on="point", right_index=True, how="left")
        frame[f"draws:{spec.id}"] = frame[f"draws:{spec.id}"].fillna(0).astype(int)
        if ceiling is not None:
            frame[f"draws_at_ceiling:{spec.id}"] = (
                frame["point"].map(at_ceiling).fillna(0).astype(int).to_numpy()
            )
    words = np.rint(frame["rpm"].to_numpy(dtype=float) / rpm_per_count)
    mids = np.floor((frame["load_lo"].to_numpy() + frame["load_hi"].to_numpy()) / 2.0)
    frame["cell"] = [
        f"{_cell_label(table['rows'], w, rpm_per_count)}x{_cell_label(table['cols'], m, 1.0)}"
        for w, m in zip(words, mids, strict=True)
    ]
    return frame


def _cell_label(axis: dict[str, Any], value: float, unit: float) -> str:
    """The breakpoint whose cell range (inclusive, open-ended at the ends)
    holds ``value``, in display units (rpm for the speed word)."""
    for bp, (lo, hi) in zip(axis["breakpoints"], axis["cell_ranges"], strict=True):
        if (lo is None or value >= lo) and (hi is None or value <= hi):
            return f"{bp * unit:g}"
    raise FuelPlantRefusalError("cell_unplaced", f"{value} is in no cell range of {axis}")


def pre_observed(
    points: pd.DataFrame,
    rules: EcuOutputRules | EcuPredictionBlock,
    profile: FuelPlantProfile,
    sessions_root: Path,
) -> pd.DataFrame:
    """Each design point's observed outputs on its own pre-change recording:
    the output's read paired one-to-one within the plant's ECU pairing
    tolerance of the point's read (NaN where none is), as E07.3 pairs BFS."""
    out = points.copy()
    tol = profile.steady.ecu_pair_tolerance_s
    for spec in rules.outputs:
        out[f"observed:{spec.id}"] = np.nan
    for session, idx in out.groupby("session").groups.items():
        path = sessions_root / str(session)
        if not path.is_dir():
            raise FuelPlantRefusalError(
                "pre_recording_missing", f"the design's recording {path} is not here"
            )
        long = read_channels(path, [s.observed for s in rules.outputs], acquired_time=True)
        t = out.loc[idx, "t"].to_numpy(dtype=float)
        for spec in rules.outputs:
            out.loc[idx, f"observed:{spec.id}"] = _pair(t, _series(long, spec.observed), tol)
    return out


def _regime_labels(phi: FloatArray, band: float) -> np.ndarray:
    """E07.3's regime from the bracketed commanded phi
    (:func:`src.analysis.fuel_plant_eval.build_samples`, the same band)."""
    regime = np.full(len(phi), "unknown", dtype=object)
    regime[np.abs(phi - 1.0) <= band] = "closed_loop"
    regime[phi > 1.0 + band] = "enrichment"
    regime[phi < 1.0 - band] = "lean_target"
    return regime


ECU_READ_COLUMNS = (
    "session",
    "t",
    "observed",
    "rpm",
    "maf_gps",
    "baro_kpa",
    "iat_c",
    "coolant_c",
    "regime",
    "load_lo",
    "load_hi",
    "state",
)


def ecu_output_reads(
    session: Path,
    output: EcuOutputSpec,
    profile: FuelPlantProfile,
    delay: B06Rules,
    fill: pd.DataFrame,
    *,
    image_sha256: str,
    boundary: Boundary,
    side: Literal["post", "pre"] = "post",
) -> pd.DataFrame:
    """One row per read of the output's own channel on a post-change recording,
    with the conditions the case matches on, each on the acquisition clock.

    The design built its points this way, so a post read is what would have
    been a point on this drive: rpm and MAF paired one-to-one within the plant's
    ``pair_tolerance_s``; baro, IAT and coolant the nearest read within
    ``slow_max_age_s``; the regime from the bracketed commanded phi; steady when
    the rpm and MAF windows over ``window_s`` before the read hold the plant's
    spans (the pulse-read clause is dropped: the output's read is the anchor);
    warm at ``min_coolant_c``; and the fill byte N02's closed-loop-route
    interval (``fill``, :func:`fill_samples`) paired one-to-one within
    ``pair_tolerance_s``, on ``image_sha256`` only (``fill_image_mismatch``).

    ``state`` is ``eligible`` or the first reason, in this precedence:
    ``not_co_observed`` (no rpm and no MAF read), ``missing_input``, ``cold``,
    ``transient``, ``fill_byte_unpaired``, ``fill_byte:<N02 state:reason>``.
    Reads before the flash refuse (``wrong_side``). ``side="pre"`` reads a
    pre-flash recording the same way (a null control, :func:`ecu_null_control`):
    there a read at or after the flash refuses ``wrong_side``."""
    s, rule = profile.specs, profile.steady
    specs = list(
        dict.fromkeys(
            [output.observed, s.rpm, s.maf, s.baro, s.intake_temp, s.coolant, s.reference_phi]
        )
    )
    try:
        long = read_channels(session, specs, acquired_time=True)
    except FileNotFoundError:
        long = pd.DataFrame(columns=["timestamp", "spec", "value"])
    obs = _series(long, output.observed)
    t = obs.times
    if not len(t):
        return pd.DataFrame({c: pd.Series([], dtype=object) for c in ECU_READ_COLUMNS})
    if side == "post" and float(t.min()) < boundary.at.timestamp():
        raise FuelPlantRefusalError(
            "wrong_side",
            f"{session.name} has {output.observed} reads before {boundary.at.isoformat()}",
        )
    if side == "pre" and float(t.max()) >= boundary.at.timestamp():
        raise FuelPlantRefusalError(
            "wrong_side",
            f"{session.name} has {output.observed} reads at or after {boundary.at.isoformat()}",
        )
    frame = pd.DataFrame({"session": session.name, "t": t, "observed": obs.values})
    rpm_s, maf_s = _series(long, s.rpm), _series(long, s.maf)
    frame["rpm"] = _pair(t, rpm_s, rule.pair_tolerance_s)
    frame["maf_gps"] = _pair(t, maf_s, rule.pair_tolerance_s)
    frame["baro_kpa"] = _nearest_value(_series(long, s.baro), t, rule.slow_max_age_s)
    frame["iat_c"] = _nearest_value(_series(long, s.intake_temp), t, rule.slow_max_age_s)
    frame["coolant_c"] = _nearest_value(_series(long, s.coolant), t, rule.slow_max_age_s)
    phi = _bracketed(_series(long, s.reference_phi), t, rule.reference_phi_max_age_s)
    frame["regime"] = _regime_labels(phi, delay.closed_loop_band)
    rmin, rmax, rn = _window(rpm_s, t, rule.window_s, 0.0)
    mmin, mmax, mn = _window(maf_s, t, rule.window_s, 0.0)
    with np.errstate(divide="ignore", invalid="ignore"):
        steady = (
            ((rmax - rmin) / frame["rpm"].to_numpy() <= rule.rpm_span)
            & ((mmax - mmin) / frame["maf_gps"].to_numpy() <= rule.maf_span)
            & (rn >= rule.min_rpm_reads)
            & (mn >= rule.min_maf_reads)
        )
    fill_reason, load_lo, load_hi = _pair_fill(t, fill, rule.pair_tolerance_s, image_sha256)
    frame["load_lo"], frame["load_hi"] = load_lo, load_hi
    needed = frame[["rpm", "baro_kpa", "iat_c", "coolant_c"]].to_numpy(dtype=float)
    state = np.full(len(t), "eligible", dtype=object)
    checks: list[tuple[np.ndarray, object]] = [
        (fill_reason != "", fill_reason),
        (~steady, "transient"),
        (frame["coolant_c"].to_numpy() < rule.min_coolant_c, "cold"),
        (~np.isfinite(needed).all(axis=1), "missing_input"),
        (
            ~np.isfinite(frame[["rpm", "maf_gps"]].to_numpy(dtype=float)).any(axis=1),
            "not_co_observed",
        ),
    ]
    for mask, name in checks:  # last assignment wins: the list is reverse precedence
        mask = np.asarray(mask, dtype=bool)
        state[mask] = name[mask] if isinstance(name, np.ndarray) else name
    frame["state"] = state
    return frame[list(ECU_READ_COLUMNS)]


def _pair_fill(
    t: FloatArray, fill: pd.DataFrame, tolerance_s: float, image_sha256: str
) -> tuple[np.ndarray, FloatArray, FloatArray]:
    """N02's closed-loop fill interval per anchor: ``(reason, lo, hi)`` with
    reason ``""`` where an interval is paired, as the design pairs it."""
    reason = np.full(len(t), "fill_byte_unpaired", dtype=object)
    lo = np.full(len(t), np.nan)
    hi = np.full(len(t), np.nan)
    rows = fill[fill["route"] == "closed_loop"].reset_index(drop=True) if len(fill) else fill
    if not len(rows) or not len(t):
        return reason, lo, hi
    times = np.asarray(to_seconds(rows["timestamp"]), dtype=float)
    order = np.argsort(times, kind="stable")
    policy = PairingPolicy.within(pd.Timedelta(seconds=tolerance_s), reference="a")
    matched = match_ordered(t, times[order], policy)
    for a, b in zip(matched.index_a.tolist(), order[matched.index_b].tolist(), strict=True):
        row = rows.iloc[int(b)]
        values = [row[c] for c in ("byte_lo", "byte_hi", "rpm_word_lo", "rpm_word_hi")]
        if not all(v is not None and pd.notna(v) for v in values):
            why = row.get("reason")
            reason[a] = (
                f"fill_byte:{row['state']}:{why if isinstance(why, str) and why else 'no_interval'}"
            )
            continue
        if row["image_sha256"] != image_sha256:
            raise FuelPlantRefusalError(
                "fill_image_mismatch",
                f"fill byte at t={times[b]:.3f} was reconstructed on {row['revision']} "
                f"({str(row['image_sha256'])[:12]}...), not {image_sha256[:12]}...",
            )
        reason[a], lo[a], hi[a] = "", float(values[0]), float(values[1])
    return reason, lo, hi


def fill_samples(
    session: Path,
    profile_dir: Path,
    registry: TuneRevisionRegistry,
    resolver: RevisionImageResolver | Callable[[str], FuelCompCalibration],
) -> pd.DataFrame:
    """N02's samples (:func:`src.analysis.fuel_comp_samples.session_samples`)
    of one recording, the fill byte reconstructed on the revision in force at
    each sample, as the design reconstructed its own."""
    providers = profile_dir / "tuning_providers.yaml"
    samples = session_samples(
        session,
        declaration=load_fuel_comp_declaration(profile_dir),
        delivery=load_lambda_delivery(
            profile_dir / "lambda_delivery.yaml",
            providers_path=providers if providers.exists() else None,
        ),
        afr_calibration=load_afr_calibration(profile_dir / "afr_calibration.yaml"),
        registry=registry,
        resolver=resolver,
    )
    return samples.frame


def _ecu_verdict(
    n_pairs: int,
    floor: float | None,
    n_signed: int,
    direction: float | None,
    within: float,
    acc: EffectAcceptance,
) -> tuple[TestOutcome, str | None]:
    if n_pairs < acc.min_pairs:
        return "insufficient_evidence", f"{n_pairs} pairs < {acc.min_pairs}"
    if floor is None:
        return "insufficient_evidence", "condition-matched noise floor unavailable"
    if direction is None:
        if within >= acc.within_uncertainty:
            return "pass", f"direction_not_applicable: no pair of {n_pairs} predicts a change"
        return "fail", (
            f"direction_not_applicable; within uncertainty {within:.2f} "
            f"(>= {acc.within_uncertainty})"
        )
    if direction >= acc.direction_agreement and within >= acc.within_uncertainty:
        return "pass", None
    return "fail", (
        f"direction agreement {direction:.2f} over {n_signed} signed pairs "
        f"(>= {acc.direction_agreement}), within uncertainty {within:.2f} "
        f"(>= {acc.within_uncertainty})"
    )


def _relative_pct(a: FloatArray, b: FloatArray) -> FloatArray:
    out: FloatArray = 100.0 * (a / b - 1.0)
    return out


_INTERVAL = ("load_lo", "load_hi")


def _compared(
    frame: pd.DataFrame,
    side: str,
    quantity: EcuQuantity,
    ceiling: float | None,
    exclusions: dict[str, int],
) -> FloatArray:
    """The value one side compares per row, NaN where it is excluded, each
    exclusion counted by name.

    ``raw``: the output's read (``<side>_observed_unpaired`` when missing or
    not positive). ``air_normalised``: BFS x rpm / MAF at the read's own
    paired rpm and MAF; ``<side>_at_ceiling`` (a read at or above the rpm
    ceiling's clamp is not 4 x (Tp x FC >> 7)) and ``<side>_maf_unpaired``
    are named apart."""
    if not len(frame):
        exclusions[f"{side}_observed_unpaired"] = 0
        return np.array([], dtype=float)
    value = frame["observed"].to_numpy(dtype=float).copy()
    missing = ~(np.isfinite(value) & (value > 0))
    exclusions[f"{side}_observed_unpaired"] = int(missing.sum())
    value[missing] = np.nan
    if quantity == "raw":
        return value
    if ceiling is None:
        raise FuelPlantRefusalError(
            "ceiling_unstated", "air_normalised BFS needs the ceiling its clamp is excluded at"
        )
    clamped = ~missing & (value >= ceiling)
    exclusions[f"{side}_at_ceiling"] = int(clamped.sum())
    value[clamped] = np.nan
    rpm = frame["rpm"].to_numpy(dtype=float)
    maf = (
        frame["maf_gps"].to_numpy(dtype=float)
        if "maf_gps" in frame
        else np.full(len(value), np.nan)
    )
    no_air = np.isfinite(value) & ~(np.isfinite(maf) & (maf > 0) & np.isfinite(rpm))
    exclusions[f"{side}_maf_unpaired"] = int(no_air.sum())
    with np.errstate(divide="ignore", invalid="ignore"):
        out: FloatArray = np.where(no_air, np.nan, value * rpm / maf)
    return out


def ecu_effect_test(
    pre: pd.DataFrame,
    post: pd.DataFrame,
    case: InterventionCase,
    *,
    quantity: EcuQuantity = "raw",
    ceiling: float | None = None,
    resolvable: bool = False,
    exclude_unknown_regime: bool = False,
) -> dict[str, Any]:
    """Does one ECU-side output move, per matched pair, as the prediction says?

    ``resolvable`` (amendment 3): only design points whose predicted |change|
    (the median) exceeds the floor are paired, so every counted pair is one
    the test has power at; with fewer than ``min_pairs`` such pairs where all
    predicted points would have given enough, the verdict is
    ``insufficient_evidence`` named ``prediction_unresolvable``.
    ``exclude_unknown_regime`` drops ``unknown``-regime reads on both sides
    (``*_regime_unknown``): E07.4's effect tests score named regimes only.

    ``pre``: the prediction's design points (``session``, ``t``, ``regime``,
    ``rpm``, ``load_lo``/``load_hi``, ``baro_kpa``, ``iat_c``), each with the
    pre-change recording's ``observed`` value there and the predicted relative
    change ``pred_lo``/``pred_mid``/``pred_hi`` (percent); optional ``cell``.
    ``post``: the output's own post-change reads (:func:`ecu_output_reads`).

    A post read is paired one-to-one, nearest first, with a design point in the
    same regime under the case's matching, both ends of the two fill intervals
    within the byte tolerance. Observed change = post / pre - 1 at the pair;
    predicted = that point's interval. The floor is the same quantity over
    pre-vs-pre pairs of design points (no change between them), within one
    recording only at ``floor.min_separation_s``; its declared quantile of
    |change| widens the predicted interval for within-uncertainty. Nothing
    here reads a trim: the outputs do not depend on one
    (:func:`trim_independence`).

    ``quantity`` (an :class:`EcuAmendment`'s): ``air_normalised`` compares
    BFS x rpm / MAF on both sides instead of BFS, each side at its own paired
    rpm and MAF, and excludes reads at ``ceiling`` by name (:func:`_compared`).
    The predicted interval is unchanged: at one design point base and
    candidate share its rpm and airflow, so the normalised change is the
    traces' candidate / base - 1."""
    acc = case.acceptance
    exclusions: dict[str, int] = {}
    pre_value = _compared(pre, "pre", quantity, ceiling, exclusions)
    if exclude_unknown_regime:
        unknown = np.isfinite(pre_value) & (pre["regime"].astype(str).to_numpy() == "unknown")
        exclusions["pre_regime_unknown"] = int(unknown.sum())
        pre_value = np.where(unknown, np.nan, pre_value)
    has_obs = np.isfinite(pre_value)
    pre_obs = pre[has_obs].reset_index(drop=True).assign(value=pre_value[has_obs])
    fi, fj = match_pairs(pre_obs, pre_obs, case.matching, floor=case.floor, load=_INTERVAL)
    obs = pre_obs["value"].to_numpy(dtype=float)
    floor_res = _relative_pct(obs[fi], obs[fj]) if len(fi) else np.array([], dtype=float)
    floor_res = floor_res[np.isfinite(floor_res)]
    floor = _floor(floor_res, acc)

    predicted = np.isfinite(pre_obs["pred_mid"].to_numpy(dtype=float)) if len(pre_obs) else []
    exclusions["pre_not_predicted"] = int((~np.asarray(predicted, dtype=bool)).sum())
    scored = pre_obs[np.asarray(predicted, dtype=bool)].reset_index(drop=True)
    all_predicted = scored
    if resolvable:
        mids = np.abs(scored["pred_mid"].to_numpy(dtype=float))
        beyond = mids > floor if floor is not None else np.zeros(len(scored), dtype=bool)
        exclusions["pre_unresolvable"] = int((~beyond).sum())
        scored = scored[beyond].reset_index(drop=True)

    post_states = (
        {str(k): int(v) for k, v in post["state"].value_counts().items()} if len(post) else {}
    )
    in_state = (
        post[(post["state"] == "eligible").to_numpy()].reset_index(drop=True) if len(post) else post
    )
    post_value = _compared(in_state, "post", quantity, ceiling, exclusions)
    if exclude_unknown_regime:
        unknown = np.isfinite(post_value) & (
            in_state["regime"].astype(str).to_numpy() == "unknown"
            if len(in_state)
            else np.array([], dtype=bool)
        )
        exclusions["post_regime_unknown"] = int(unknown.sum())
        post_value = np.where(unknown, np.nan, post_value)
    keep = np.isfinite(post_value)
    usable = in_state[keep].reset_index(drop=True).assign(value=post_value[keep])
    i, j = match_pairs(usable, scored, case.matching, load=_INTERVAL)
    a, b = usable.iloc[i].reset_index(drop=True), scored.iloc[j].reset_index(drop=True)
    observed = (
        _relative_pct(a["value"].to_numpy(dtype=float), b["value"].to_numpy(dtype=float))
        if len(a)
        else np.array([], dtype=float)
    )
    lo = b["pred_lo"].to_numpy(dtype=float) if len(b) else np.array([])
    mid = b["pred_mid"].to_numpy(dtype=float) if len(b) else np.array([])
    hi = b["pred_hi"].to_numpy(dtype=float) if len(b) else np.array([])
    signed = mid != 0
    n_signed = int(signed.sum())
    agree = np.sign(observed) == np.sign(mid)
    direction = float(np.mean(agree[signed])) if n_signed else None
    u = floor if floor is not None else math.nan
    inside = (observed >= lo - u) & (observed <= hi + u)
    within = float(np.mean(inside)) if len(observed) else 0.0
    outcome, why = _ecu_verdict(len(observed), floor, n_signed, direction, within, acc)
    pairs_all = len(observed)
    if resolvable:
        pairs_all = len(match_pairs(usable, all_predicted, case.matching, load=_INTERVAL)[0])
        if floor is None:
            outcome, why = "insufficient_evidence", "condition-matched noise floor unavailable"
        elif len(observed) < acc.min_pairs and pairs_all >= acc.min_pairs:
            why = (
                f"prediction_unresolvable: {len(observed)} pairs predict |change| beyond the "
                f"{floor:.2f} % floor, < {acc.min_pairs} (all predicted points: {pairs_all} pairs)"
            )
    if (
        resolvable
        and outcome == "insufficient_evidence"
        and str(why).startswith("prediction_unresolvable")
    ):
        empty: str | None = str(why)
    elif not len(post):
        empty = "never_captured: no read of the output on any post-change recording"
    elif not len(usable):
        empty = f"filtered: no post-change read is eligible ({post_states})"
    elif not len(observed):
        empty = "unmatched: no eligible post-change read matches a design point"
    else:
        empty = None
    cells: dict[str, dict[str, int]] = {}
    if len(b) and "cell" in b:
        for k, cell in enumerate(b["cell"].astype(str).tolist()):
            entry = cells.setdefault(cell, {"n_pairs": 0, "n_signed": 0, "agree": 0, "within": 0})
            entry["n_pairs"] += 1
            entry["n_signed"] += int(signed[k])
            entry["agree"] += int(signed[k] and agree[k])
            entry["within"] += int(inside[k])
    return {
        "quantity": quantity,
        "resolvable_only": resolvable,
        "n_pairs": len(observed),
        "pairs_before_resolvable": pairs_all,
        "n_signed": n_signed,
        "post_reads": len(post),
        "post_states": post_states,
        "post_eligible": len(usable),
        "post_sessions": sorted(set(post["session"])) if len(post) else [],
        "design_points": len(pre),
        "exclusions": exclusions,
        "empty": empty,
        "observed_change_pct": _summ(observed),
        "predicted_change_pct": _summ(mid),
        "residual_pct": _summ(observed - mid),
        "noise_floor_pct": floor,
        "floor_pairs": len(floor_res),
        "direction_agreement": direction,
        "within_uncertainty": within,
        "cells": dict(sorted(cells.items())),
        "outcome": outcome,
        "reason": why,
    }


def _output_frame(points: pd.DataFrame, output_id: str) -> pd.DataFrame:
    """The design points with one output's columns under the generic names
    :func:`ecu_effect_test` reads."""
    rename = {
        f"observed:{output_id}": "observed",
        f"pred_lo:{output_id}": "pred_lo",
        f"pred_mid:{output_id}": "pred_mid",
        f"pred_hi:{output_id}": "pred_hi",
    }
    keep = [
        c
        for c in (
            "session",
            "t",
            "regime",
            "rpm",
            "maf_gps",
            "load_lo",
            "load_hi",
            "baro_kpa",
            "iat_c",
        )
        if c in points
    ]
    if "cell" in points:
        keep.append("cell")
    return points[keep + list(rename)].rename(columns=rename)


@dataclass(frozen=True)
class _EcuScoring:
    """The frozen rules one ECU-output score runs under, resolved once from
    ``ecu_outputs`` (with an amendment) or a prediction block."""

    block: str
    rules: EcuOutputRules | EcuPredictionBlock
    version: int
    sha256: str
    reason: str | None
    quantities: dict[str, EcuQuantity]
    ceiling: float | None
    resolvable: bool
    exclude_unknown_regime: bool

    @property
    def prediction(self) -> PredictionArtifact:
        return self.rules.prediction

    def ceilings(self) -> dict[str, float]:
        """Output id -> the clamp a draw is left out at (``air_normalised`` only)."""
        if self.ceiling is None:
            return {}
        return {k: self.ceiling for k, q in self.quantities.items() if q == "air_normalised"}


def _ecu_scoring(
    case: InterventionCase, *, amendment: int | None, block: str | None
) -> _EcuScoring:
    """``ecu_outputs_undeclared`` without the original block (every prediction
    block shares its post list); ``block_and_amendment``: an amendment belongs
    to ``ecu_outputs``, a prediction block is frozen with its own rules."""
    rules = case.ecu_outputs
    if rules is None:
        raise FuelPlantRefusalError("ecu_outputs_undeclared", f"case {case.id} has no ecu_outputs")
    if block is not None:
        if amendment is not None:
            raise FuelPlantRefusalError(
                "block_and_amendment",
                f"amendment {amendment} amends ecu_outputs; block {block!r} carries its own rules",
            )
        b = case.ecu_prediction(block)
        return _EcuScoring(
            block=b.id,
            rules=b,
            version=1,
            sha256=b.frozen_rules.sha256,
            reason=b.reason,
            quantities=dict(b.quantities),
            ceiling=float(b.bfs_ceiling_counts),
            resolvable=b.resolvable == "predicted_beyond_floor",
            exclude_unknown_regime=bool(b.exclude_unknown_regime),
        )
    amended = None if amendment is None else case.ecu_amendment(amendment)
    if amended is None:
        return _EcuScoring(
            block="ecu_outputs",
            rules=rules,
            version=1,
            sha256=rules.frozen_rules.sha256,
            reason=None,
            quantities={},
            ceiling=None,
            resolvable=False,
            exclude_unknown_regime=False,
        )
    return _EcuScoring(
        block="ecu_outputs",
        rules=rules,
        version=amended.version,
        sha256=amended.frozen_rules.sha256,
        reason=amended.reason,
        quantities=dict(amended.quantities),
        ceiling=float(amended.bfs_ceiling_counts),
        resolvable=amended.resolvable == "predicted_beyond_floor",
        exclude_unknown_regime=bool(amended.exclude_unknown_regime),
    )


def _ecu_test(
    points: pd.DataFrame,
    reads: pd.DataFrame,
    case: InterventionCase,
    scoring: _EcuScoring,
    spec: EcuOutputSpec,
) -> dict[str, Any]:
    """:func:`ecu_effect_test` for one output under the resolved rules."""
    quantity = scoring.quantities.get(spec.id, "raw")
    return ecu_effect_test(
        _output_frame(points, spec.id),
        reads,
        case,
        quantity=quantity,
        ceiling=scoring.ceiling if quantity == "air_normalised" else None,
        resolvable=scoring.resolvable,
        exclude_unknown_regime=scoring.exclude_unknown_regime,
    )


def evaluate_ecu_outputs(
    case: InterventionCase,
    points: pd.DataFrame,
    post: Mapping[str, pd.DataFrame],
    *,
    artifact: Mapping[str, str],
    trims: Mapping[str, Any] | None = None,
    amendment: int | None = None,
    block: str | None = None,
    recordings: Sequence[str] | None = None,
    scope_kind: Literal["all", "named"] | None = None,
    candidates_not_scored: Sequence[str] = (),
) -> dict[str, Any]:
    """E09.12's verdict per ECU-side output, sealed.

    ``amendment``: score under that :class:`EcuAmendment` version (its
    quantities and ceiling) instead of the original block; ``block``: under
    that :class:`EcuPredictionBlock` (its own artifact, outputs and rules)
    instead. Either way the verdict names the block, rules version and sha it
    was scored under.
    ``recordings``: the declared post-change recordings scored (default all
    of :meth:`InterventionCase.ecu_post_recordings`); the verdict names them.
    ``scope_kind`` (default ``all`` without ``recordings``, else ``named``) and
    ``candidates_not_scored`` are sealed with it.

    ``points``: :func:`prediction_points` with :func:`pre_observed`'s columns.
    ``post``: per output id, its post-change reads (:func:`ecu_output_reads`
    over the case's ``post`` recordings). ``artifact``: the hashes
    :func:`verify_prediction_artifact` checked (the traces' among them).

    Refusals: ``ecu_outputs_undeclared``, ``outputs_mismatch``,
    ``rules_not_frozen`` (the shared matching, floor and acceptance),
    ``undeclared_recording`` (a post read from a recording not in
    ``ecu_outputs.post``), ``synthetic_as_observed`` / ``synthetic_mismatch``
    (a ``_sim-*`` recording on a recorded case, or the reverse) and
    ``prediction_not_verified``. The plant's qualification is not read:
    BFS and target phi are the ECU's decoded integer logic."""
    scoring = _ecu_scoring(case, amendment=amendment, block=block)
    rules = scoring.rules
    if case.frozen_rules is None:
        raise FuelPlantRefusalError(
            "rules_not_frozen",
            f"case {case.id}'s matching, floor and acceptance are not frozen: an ECU-output "
            "score uses them",
        )
    rules_version, rules_sha = scoring.version, scoring.sha256
    ids = [o.id for o in rules.outputs]
    if sorted(post) != sorted(ids):
        raise FuelPlantRefusalError(
            "outputs_mismatch", f"post reads for {sorted(post)}, the case declares {sorted(ids)}"
        )
    traces_sha = artifact.get(rules.prediction.traces)
    if traces_sha != rules.prediction.files[rules.prediction.traces]:
        raise FuelPlantRefusalError(
            "prediction_not_verified",
            "pass the hashes verify_prediction_artifact checked: the traces are not among them",
        )
    sessions = {str(s) for f in post.values() if len(f) for s in f["session"]}
    declared = case.ecu_post_recordings()
    scope = list(declared if recordings is None else recordings)
    outside = sorted(set(scope) - set(declared))
    if outside:
        raise FuelPlantRefusalError(
            "undeclared_recording", f"{outside} are not declared post-change recordings"
        )
    undeclared = sorted(sessions - set(scope))
    if undeclared:
        raise FuelPlantRefusalError(
            "undeclared_recording",
            f"{undeclared} are not in ecu_outputs.post: declare recordings before scoring them",
        )
    observed_sessions = sessions | {str(s) for s in points["session"]}
    synthetic = {is_synthetic_session(s) for s in observed_sessions}
    if case.synthetic and synthetic != {True}:
        raise FuelPlantRefusalError(
            "synthetic_mismatch", "a synthetic case scores _sim-* traces only"
        )
    if not case.synthetic and True in synthetic:
        raise FuelPlantRefusalError(
            "synthetic_as_observed", "a _sim-* trace is never an observation of the vehicle"
        )
    tests = {
        spec.id: {
            "observed_channel": spec.observed,
            "trace_output": spec.trace_output,
            **_ecu_test(points, post[spec.id], case, scoring, spec),
        }
        for spec in rules.outputs
    }
    report: dict[str, Any] = {
        "case": case.id,
        "synthetic": case.synthetic,
        "claim": (
            "synthetic demonstration of the path; not evidence about the vehicle"
            if case.synthetic
            else "ECU-side outputs of the prediction of record against the truck"
        ),
        "plant": "not used: BFS and target phi are decoded ECU logic; plant_not_qualified "
        "does not gate this verdict",
        "rules": {
            "block": scoring.block,
            "version": rules_version,
            "sha256": rules_sha,
            "reason": scoring.reason,
        },
        "verdicts": {k: t["outcome"] for k, t in tests.items()},
        "effect_tests": tests,
        "prediction": {
            "artifact": rules.prediction.artifact,
            "result_digest": rules.prediction.result_digest,
            "design_sha256": rules.prediction.design_sha256,
            "hashes_checked": dict(artifact),
        },
        "post_recordings": scope,
        "scope": scope_kind or ("all" if recordings is None else "named"),
        "candidates_not_scored": list(candidates_not_scored),
        "learned_state": trims,
        "base_image_sha256": case.base_image_sha256,
        "candidate_image_sha256": case.candidate_image_sha256,
    }
    report["verdict_record"] = EcuOutputVerdict._mint(
        case=case.id,
        outcomes=tuple(
            (k, str(t["outcome"]), int(t["n_pairs"]), int(t["floor_pairs"]))
            for k, t in tests.items()
        ),
        synthetic=case.synthetic,
        base_image_sha256=case.base_image_sha256,
        candidate_image_sha256=case.candidate_image_sha256,
        traces_sha256=traces_sha,
        block=scoring.block,
        rules_version=rules_version,
        rules_sha256=rules_sha,
        recordings=tuple(scope),
        scope=report["scope"],
        candidates_not_scored=tuple(candidates_not_scored),
    )
    return report


def candidate_recordings(
    root: Path, registry: TuneRevisionRegistry, case: InterventionCase
) -> list[str]:
    """Every recording under ``root`` that ran on the case's candidate
    revision, dated by its first sample (``for_session``), oldest first."""
    found: list[tuple[datetime, str]] = []
    for path in recorded_sessions(root):
        rev = registry.for_session(path)
        if rev is not None and rev.revision == case.candidate_revision:
            dated = session_dated_at(path)
            found.append((dated or datetime.max.replace(tzinfo=UTC), path.name))
    return [name for _, name in sorted(found)]


def post_scope(
    case: InterventionCase,
    boundary: Boundary,
    on_candidate: Sequence[str],
    sessions_root: Path,
    *,
    recordings: Sequence[str] | None = None,
) -> tuple[list[str], list[str]]:
    """Which declared post-change recordings to score, and which candidate
    recordings are left unscored.

    Without ``recordings`` every recording on the candidate revision is
    scored and must be declared (``post_recording_undeclared``): none is
    chosen. With ``recordings``, those (declared, by name) only; the others on
    disk are returned so the report names them. Every scored recording must be
    here on the candidate revision (``post_recording_missing``) with its first
    sample after the flash (``post_recording_before_flash``)."""
    declared = case.ecu_post_recordings()
    if recordings is None:
        missing = sorted(set(on_candidate) - set(declared))
        if missing:
            raise FuelPlantRefusalError(
                "post_recording_undeclared",
                f"{missing} ran on {case.candidate_revision} and are not declared (append them "
                "to ecu_post_appended): every post-change recording is scored, none is chosen",
            )
        scope = list(declared)
    else:
        scope = list(recordings)
        outside = sorted(set(scope) - set(declared))
        if outside:
            raise FuelPlantRefusalError(
                "undeclared_recording", f"{outside} are not declared post-change recordings"
            )
    absent = sorted(set(scope) - set(on_candidate))
    if absent:
        raise FuelPlantRefusalError(
            "post_recording_missing",
            f"{absent} are declared but not here on {case.candidate_revision}",
        )
    for name in scope:
        first = session_dated_at(sessions_root / name)
        if first is None or first < boundary.at:
            raise FuelPlantRefusalError(
                "post_recording_before_flash",
                f"{name}'s first sample ({first}) is not after the flash {boundary.at.isoformat()}",
            )
    return scope, sorted(set(on_candidate) - set(scope))


@dataclass(frozen=True)
class _EcuInputs:
    """The pre side of one ECU-output score, built after the artifact check."""

    scoring: _EcuScoring
    artifact: dict[str, str]
    design: dict[str, Any]
    points: pd.DataFrame
    profile: FuelPlantProfile
    delay: B06Rules
    trims: dict[str, Any]


def _check_design_recordings(block: EcuPredictionBlock, design: Mapping[str, Any]) -> None:
    """``design_recordings_mismatch``: the block names other drives than the
    design was built from, so its null control's independence is unproven."""
    built = sorted(str(s["session"]) for s in design.get("sessions") or [])
    if built != sorted(block.design_recordings):
        raise FuelPlantRefusalError(
            "design_recordings_mismatch",
            f"block {block.id} declares design recordings {sorted(block.design_recordings)}, "
            f"the design was built from {built}",
        )


def _ecu_inputs(
    case: InterventionCase,
    *,
    profile_dir: Path,
    sessions_root: Path,
    predictions_root: Path,
    amendment: int | None,
    block: str | None,
) -> _EcuInputs:
    """The artifact checked against its recorded hashes first, then (for a
    prediction block) its design recordings, then the design points with their
    pre-change values. No post-change recording is read here."""
    scoring = _ecu_scoring(case, amendment=amendment, block=block)
    rules = scoring.rules
    directory = predictions_root / rules.prediction.artifact
    artifact = verify_prediction_artifact(directory, rules.prediction)
    design = json.loads((directory / rules.prediction.design_record).read_text())
    if isinstance(rules, EcuPredictionBlock):
        _check_design_recordings(rules, design)
    profile, _ = load_fuel_plant(profile_dir, str(design["plant_version"]))
    delay = load_b06(profile_dir, profile.delay)
    model, _ = load_application_model(profile_dir)
    trims = trim_independence(model, [o.model_output for o in rules.outputs])
    declaration = load_fuel_comp_declaration(profile_dir)
    points = prediction_points(
        directory,
        rules,
        rpm_per_count=declaration.inputs.rpm.rpm_per_count,
        ceilings=scoring.ceilings(),
    )
    if isinstance(rules, EcuPredictionBlock):
        in_points = sorted(set(points["session"].astype(str)) - set(rules.design_recordings))
        if in_points:
            raise FuelPlantRefusalError(
                "design_recordings_mismatch",
                f"design points come from {in_points}, not declared in block {rules.id}",
            )
    points = pre_observed(points, rules, profile, sessions_root)
    return _EcuInputs(scoring, artifact, design, points, profile, delay, trims)


def _empty_reads() -> pd.DataFrame:
    return pd.DataFrame({c: pd.Series([], dtype=object) for c in ECU_READ_COLUMNS})


def score_ecu_outputs(
    case: InterventionCase,
    boundary: Boundary,
    registry: TuneRevisionRegistry,
    *,
    profile_dir: Path,
    sessions_root: Path,
    predictions_root: Path,
    resolver: RevisionImageResolver | Callable[[str], FuelCompCalibration],
    amendment: int | None = None,
    block: str | None = None,
    recordings: Sequence[str] | None = None,
) -> tuple[dict[str, Any], pd.DataFrame, dict[str, pd.DataFrame]]:
    """E09.12 end to end, in the order of evidence: the prediction's files are
    checked against their recorded hashes before any post-change read is
    taken; every recording on the candidate revision must be declared
    (``post_recording_undeclared``); then the design points, their pre-change
    values, and each output's post-change reads. Returns the sealed report,
    the points and the post reads per output. ``amendment`` scores under that
    :class:`EcuAmendment` version, ``block`` under that
    :class:`EcuPredictionBlock` (:func:`evaluate_ecu_outputs`).
    ``recordings`` names declared recordings to score instead of all of them
    (:func:`post_scope`): the report lists the candidates it did not score."""
    scoring = _ecu_scoring(case, amendment=amendment, block=block)
    directory = predictions_root / scoring.prediction.artifact
    verify_prediction_artifact(directory, scoring.prediction)
    on_candidate = candidate_recordings(sessions_root, registry, case)
    scope, not_scored = post_scope(
        case, boundary, on_candidate, sessions_root, recordings=recordings
    )
    inputs = _ecu_inputs(
        case,
        profile_dir=profile_dir,
        sessions_root=sessions_root,
        predictions_root=predictions_root,
        amendment=amendment,
        block=block,
    )
    rules = inputs.scoring.rules
    post: dict[str, list[pd.DataFrame]] = {o.id: [] for o in rules.outputs}
    for name in scope:
        path = sessions_root / name
        fill = fill_samples(path, profile_dir, registry, resolver)
        for spec in rules.outputs:
            post[spec.id].append(
                ecu_output_reads(
                    path,
                    spec,
                    inputs.profile,
                    inputs.delay,
                    fill,
                    image_sha256=case.candidate_image_sha256,
                    boundary=boundary,
                )
            )
    reads = {
        k: (
            pd.concat([f for f in v if len(f)], ignore_index=True)
            if any(len(f) for f in v)
            else _empty_reads()
        )
        for k, v in post.items()
    }
    report = evaluate_ecu_outputs(
        case,
        inputs.points,
        reads,
        artifact=inputs.artifact,
        trims=inputs.trims,
        amendment=amendment,
        block=block,
        recordings=scope,
        scope_kind="all" if recordings is None else "named",
        candidates_not_scored=not_scored,
    )
    report["plant_version_of_design"] = inputs.design["plant_version"]
    return report, inputs.points, reads


def ecu_null_control(
    case: InterventionCase,
    boundary: Boundary,
    registry: TuneRevisionRegistry,
    *,
    block: str,
    profile_dir: Path,
    sessions_root: Path,
    predictions_root: Path,
    resolver: RevisionImageResolver | Callable[[str], FuelCompCalibration],
) -> dict[str, Any]:
    """A prediction block's null control: each of its ``null_control`` drives,
    pre-flash on the base image (no ROM change), scored as if post-change
    against the block's REAL prediction under the block's rules. A test with
    power does not pass there.

    In the order of evidence, as :func:`score_ecu_outputs`: the artifact and
    the design recordings first. A drive must be here
    (``null_drive_missing``), on the base revision by its first sample
    (``null_drive_not_on_base``) and wholly before the flash (reads after it
    refuse ``wrong_side``); the fill byte is reconstructed and checked on the
    base image. Not part of any verdict: this is the control's evidence, and
    ``passed`` names any drive and output that passed."""
    inputs = _ecu_inputs(
        case,
        profile_dir=profile_dir,
        sessions_root=sessions_root,
        predictions_root=predictions_root,
        amendment=None,
        block=block,
    )
    rules = inputs.scoring.rules
    assert isinstance(rules, EcuPredictionBlock)
    drives: dict[str, dict[str, Any]] = {}
    for name in rules.null_control:
        path = sessions_root / name
        if not path.is_dir():
            raise FuelPlantRefusalError("null_drive_missing", f"{path} is not here")
        rev = registry.for_session(path)
        if rev is None or rev.revision != case.base_revision:
            raise FuelPlantRefusalError(
                "null_drive_not_on_base",
                f"{name} ran on {None if rev is None else rev.revision}, not {case.base_revision}",
            )
        fill = fill_samples(path, profile_dir, registry, resolver)
        drives[name] = {
            spec.id: _ecu_test(
                inputs.points,
                ecu_output_reads(
                    path,
                    spec,
                    inputs.profile,
                    inputs.delay,
                    fill,
                    image_sha256=case.base_image_sha256,
                    boundary=boundary,
                    side="pre",
                ),
                case,
                inputs.scoring,
                spec,
            )
            for spec in rules.outputs
        }
    return {
        "case": case.id,
        "block": rules.id,
        "rules_sha256": inputs.scoring.sha256,
        "claim": "null control: the base image on both sides, scored against the real "
        "prediction; a pass here is a test without power, not evidence",
        "hashes_checked": inputs.artifact,
        "design_points": len(inputs.points),
        "drives": drives,
        "passed": sorted(
            f"{name}:{output}"
            for name, tests in drives.items()
            for output, test in tests.items()
            if test["outcome"] == "pass"
        ),
    }


__all__ = [
    "INTERVENTIONS_FILE",
    "PLANT_NEEDS",
    "Boundary",
    "EcuAmendment",
    "EcuOutputRules",
    "EcuOutputSpec",
    "EcuOutputVerdict",
    "EcuPredictionBlock",
    "InterventionCase",
    "InterventionFile",
    "InterventionVerdict",
    "PostAppend",
    "PredictionArtifact",
    "PredictionOfRecord",
    "acceptance_checks",
    "band_predictor",
    "load_input_pairing",
    "build_case_samples",
    "candidate_recordings",
    "ecu_effect_test",
    "ecu_null_control",
    "ecu_output_reads",
    "evaluate_ecu_outputs",
    "fill_samples",
    "pre_observed",
    "prediction_points",
    "post_chain_sha256",
    "post_scope",
    "score_ecu_outputs",
    "trim_independence",
    "verify_prediction_artifact",
    "check_one_plant",
    "evaluate_intervention",
    "inventory_session",
    "lambda_effect_test",
    "load_interventions",
    "match_pairs",
    "prediction_order",
    "recorded_sessions",
    "replay_report",
    "resolve_boundary",
    "split_by_boundary",
    "trim_effect_test",
    "verify_prediction_of_record",
]
