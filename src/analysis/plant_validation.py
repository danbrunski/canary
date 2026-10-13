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
from collections.abc import Callable, Iterable, Sequence
from dataclasses import asdict, dataclass
from datetime import UTC, date, datetime
from pathlib import Path
from typing import Any, Literal

import numpy as np
import pandas as pd
import yaml
from pydantic import BaseModel, ConfigDict, Field, model_validator

from src.analysis.channels import read_channels
from src.analysis.fuel_plant import (
    Acceptance,
    B06Rules,
    FloatArray,
    FuelPlantProfile,
    FuelPlantRefusalError,
    PlantInputs,
    plant_qualification,
    predict,
    resolve_parameters,
)
from src.analysis.fuel_plant_eval import (
    _pair,
    _series,
    _stats,
    build_samples,
    check_drive_independence,
    check_origin,
    plant_inputs,
)
from src.paths import REPO_ROOT
from src.session import SessionKind, is_synthetic_session, iter_sessions
from src.signals.pairing import to_seconds
from src.storage.sessions import session_started_at
from src.vehicle.tune_revisions import TuneRevision, TuneRevisionRegistry

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
    evidence: str = Field(min_length=1)

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
        return self


class InterventionFile(_Strict):
    schema_version: Literal[1]
    cases: list[InterventionCase] = Field(min_length=1)

    def case(self, case_id: str) -> InterventionCase:
        found = next((c for c in self.cases if c.id == case_id), None)
        if found is None:
            raise KeyError(case_id)
        return found


def load_interventions(profile_dir: Path) -> InterventionFile:
    raw = yaml.safe_load((profile_dir / INTERVENTIONS_FILE).read_text())
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
    a: pd.DataFrame, b: pd.DataFrame, m: Matching, *, floor: FloorRule | None = None
) -> tuple[np.ndarray, np.ndarray]:
    """One-to-one pairs ``(i in a, j in b)`` at the same regime and conditions,
    nearest first. With ``floor`` (the pre-vs-pre noise floor), a pair inside
    one recording must be at least ``floor.min_separation_s`` apart."""
    cols = ["rpm", "load_input", "baro_kpa", "iat_c"]
    av, bv = a[cols].to_numpy(dtype=float), b[cols].to_numpy(dtype=float)
    ar, br = a["regime"].to_numpy(), b["regime"].to_numpy()
    asess, bsess = a["session"].to_numpy(), b["session"].to_numpy()
    at, bt = a["t"].to_numpy(dtype=float), b["t"].to_numpy(dtype=float)
    scale = np.array([m.rpm_rel, m.load_input_abs, m.baro_kpa_abs, m.iat_c_abs])
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


__all__ = [
    "INTERVENTIONS_FILE",
    "PLANT_NEEDS",
    "Boundary",
    "InterventionCase",
    "InterventionFile",
    "InterventionVerdict",
    "PredictionOfRecord",
    "acceptance_checks",
    "band_predictor",
    "load_input_pairing",
    "build_case_samples",
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
