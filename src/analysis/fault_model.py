"""Detectable and distinguishable fault hypotheses (E03.2).

A vehicle's ``fault_hypotheses.yaml`` (``canary-fault-hypotheses/1``) declares
operating regions, residuals and hypotheses. Each modelled hypothesis states,
for every residual and every region that residual is defined in, the set of
signs it allows (its *signature*): the fault signature matrix of
consistency-based diagnosis, indexed by region because the same fault shows
differently at idle and at full throttle (a fixed-quantity error is large at
idle and vanishes at WOT; a scaling error does not).

Two pure computations run on that artifact, cheap enough for the Pi:

* :func:`structural_analysis` — which hypotheses the *available, qualified*
  residuals can detect, which pairs they can tell apart, the pairs they
  cannot (reported explicitly), and for each such pair the residual that
  would separate it and why it is unavailable (an absent channel names its
  measurement, an unqualified residual names why, an uncovered region names
  the region). Nothing here needs data or probabilities.
* :func:`evaluate_observations` — which hypotheses are consistent with
  observed residual signs. A sign is an observation only when the producing
  tool qualified it (beyond a noise floor); an unevaluable residual is named,
  never read as zero.

What is made impossible rather than documented:

* **No invented likelihoods.** A hypothesis may carry a numerical likelihood
  only with the evidence ids it rests on; :func:`score` resolves them on the
  vehicle's ledger and refuses every hypothesis without one, or citing an id
  that does not resolve, and says so, instead of returning a number.
* **Language-generated hypotheses stay unscored.** ``origin: language`` may
  not carry a signature or a likelihood; it is listed as unscored until a
  person models it (states its signature and changes the origin).
* **Unconstrained is said, not implied.** A modelled signature covers every
  residual x region pair (``any`` where the fault may or may not show), so a
  forgotten entry cannot read as "this fault does not move that residual".
* **An unqualified path cannot separate anything.** A residual with
  ``qualification: none`` (B08's throttle-derived fill, uncalibrated trims) is
  listed as a separating candidate with its reason, never counted.

The Fault Diagnosis Toolbox (Frisk, Krysander, Jung; ``faultdiagnosistoolbox``)
does the equation-level version of this (structural analysis over a DAE,
MSO-based residual generation, isolability, sensor placement). Our artifact is
qualitative and region-indexed, which the toolbox's input form does not
express, so this module implements only the set-signature analysis; see E03.2
in ``docs/roadmap/tasks/E03-investigations-and-learning.md`` for the spike.

Pydantic + PyYAML + stdlib, no pandas: layer ``analysis`` by placement, but it
imports nothing above ``evidence`` (text rules, and the ledger :func:`score`
resolves likelihood evidence on).
"""

from __future__ import annotations

import hashlib
from collections.abc import Iterable
from dataclasses import dataclass, field
from itertools import combinations
from pathlib import Path
from typing import Any, Literal

import yaml
from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from src.evidence.ledger import MemoryLedger, MemoryReferenceError
from src.evidence.text import no_numerals

__all__ = [
    "FAULT_MODEL_FILE",
    "FAULT_MODEL_SCHEMA",
    "SIGN_SETS",
    "Conflict",
    "ConsistencyReport",
    "Exclusion",
    "FaultHypothesis",
    "FaultModel",
    "FaultModelError",
    "Feature",
    "HypothesisConsistency",
    "Likelihood",
    "Observation",
    "PairReport",
    "Region",
    "Residual",
    "ScoreRefusal",
    "Scores",
    "SeparatingResidual",
    "StructuralReport",
    "evaluate_observations",
    "load_fault_model",
    "observed_sign",
    "score",
    "structural_analysis",
]

FAULT_MODEL_SCHEMA = "canary-fault-hypotheses/1"
#: The artifact's name inside a vehicle profile's data directory.
FAULT_MODEL_FILE = "fault_hypotheses.yaml"

Sign = Literal["+", "-", "0", "nonzero", "nonnegative", "nonpositive", "any"]
ObservedSign = Literal["+", "-", "0"]

#: The observable signs each declared sign allows.
SIGN_SETS: dict[str, frozenset[str]] = {
    "+": frozenset({"+"}),
    "-": frozenset({"-"}),
    "0": frozenset({"0"}),
    "nonzero": frozenset({"+", "-"}),
    "nonnegative": frozenset({"0", "+"}),
    "nonpositive": frozenset({"0", "-"}),
    "any": frozenset({"+", "-", "0"}),
}


class FaultModelError(ValueError):
    """A fault model, observation or score request this module refuses."""


class _Strict(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class Region(_Strict):
    name: str = Field(min_length=1)
    description: str = Field(min_length=1)


class Residual(_Strict):
    """One residual: a relation that is zero when the engine behaves as modelled.

    ``channel_sets``: the residual is available on a session holding every
    channel of any one set. ``availability: proposed`` is an instrument not
    fitted (no channel set). ``qualification`` is how its producer decides a
    sign (``noise_floor``: against a measured condition-matched floor;
    ``within_drive``: against the drive's own standard error and drift;
    ``none``: it cannot, and ``unqualified_reason`` says why).
    """

    name: str = Field(min_length=1)
    description: str = Field(min_length=1)
    unit: str = Field(min_length=1)
    positive: str = Field(min_length=1)
    regions: tuple[str, ...] = Field(min_length=1)
    channel_sets: tuple[tuple[str, ...], ...]
    availability: Literal["installed", "proposed"]
    qualification: Literal["noise_floor", "within_drive", "none"]
    unqualified_reason: str | None = None
    producer: str = Field(min_length=1)
    measurement: str = Field(min_length=1)
    policy: dict[str, Any] = Field(default_factory=dict)

    @field_validator("measurement")
    @classmethod
    def _prose(cls, value: str) -> str:
        return no_numerals(value, what="residual measurement")

    @model_validator(mode="after")
    def _rules(self) -> Residual:
        if (self.qualification == "none") != (self.unqualified_reason is not None):
            raise ValueError(
                f"{self.name}: an unqualified residual says why (unqualified_reason), "
                "and only an unqualified one does"
            )
        if self.availability == "installed" and not self.channel_sets:
            raise ValueError(f"{self.name}: an installed residual names its channel sets")
        if self.availability == "proposed" and self.channel_sets:
            raise ValueError(
                f"{self.name}: a proposed instrument has no channel yet; it is named "
                "by its measurement, not by an invented channel"
            )
        if any(not channels for channels in self.channel_sets):
            raise ValueError(f"{self.name}: an empty channel set would always be available")
        if len(set(self.regions)) != len(self.regions):
            raise ValueError(f"{self.name}: a region is listed twice")
        return self

    @property
    def qualified(self) -> bool:
        return self.qualification != "none"

    def present(self, channels: frozenset[str]) -> bool:
        return any(set(group) <= channels for group in self.channel_sets)


class Likelihood(_Strict):
    """A numerical likelihood and the evidence it was computed from."""

    value: float = Field(gt=0.0, le=1.0)
    evidence: tuple[str, ...] = Field(min_length=1)
    method: str = Field(min_length=1)


class FaultHypothesis(_Strict):
    name: str = Field(min_length=1)
    label: str = Field(min_length=1)
    origin: Literal["modelled", "language"]
    #: residual -> region -> allowed sign set.
    signature: dict[str, dict[str, Sign]] = Field(default_factory=dict)
    likelihood: Likelihood | None = None

    @field_validator("label")
    @classmethod
    def _label(cls, value: str) -> str:
        return no_numerals(value, what="hypothesis label")

    @model_validator(mode="after")
    def _origin_rules(self) -> FaultHypothesis:
        if self.origin == "language" and (self.signature or self.likelihood is not None):
            raise ValueError(
                f"{self.name}: a language-generated hypothesis is unscored until it is "
                "modelled; it carries no signature or likelihood. Model it (state its "
                "signature, origin: modelled) to evaluate it."
            )
        if self.origin == "modelled" and not self.signature:
            raise ValueError(f"{self.name}: a modelled hypothesis states its signature")
        return self

    def allowed(self, residual: str, region: str) -> frozenset[str]:
        return SIGN_SETS[self.signature[residual][region]]


class FaultModel(_Strict):
    schema_: Literal["canary-fault-hypotheses/1"] = Field(alias="schema")
    version: int = Field(ge=1)
    assumptions: tuple[str, ...] = ()
    regions: tuple[Region, ...] = Field(min_length=1)
    residuals: tuple[Residual, ...] = Field(min_length=1)
    hypotheses: tuple[FaultHypothesis, ...] = Field(min_length=1)

    model_config = ConfigDict(extra="forbid", frozen=True, populate_by_name=True)

    @model_validator(mode="after")
    def _consistent(self) -> FaultModel:
        for kind, names in (
            ("region", [r.name for r in self.regions]),
            ("residual", [r.name for r in self.residuals]),
            ("hypothesis", [h.name for h in self.hypotheses]),
        ):
            if len(set(names)) != len(names):
                raise ValueError(f"duplicate {kind} name in {names}")
        regions = {r.name for r in self.regions}
        for residual in self.residuals:
            unknown = sorted(set(residual.regions) - regions)
            if unknown:
                raise ValueError(f"{residual.name} names undeclared region(s) {unknown}")
        expected = {(r.name, region) for r in self.residuals for region in r.regions}
        for hypothesis in self.hypotheses:
            if hypothesis.origin != "modelled":
                continue
            stated = {
                (residual, region)
                for residual, by_region in hypothesis.signature.items()
                for region in by_region
            }
            extra = sorted(stated - expected)
            if extra:
                raise ValueError(
                    f"{hypothesis.name} states signs for undeclared residual x region {extra}"
                )
            missing = sorted(expected - stated)
            if missing:
                raise ValueError(
                    f"{hypothesis.name} leaves {missing} unstated: say 'any' where the "
                    "fault may or may not show, so an omission cannot read as 'no effect'"
                )
        return self

    def residual(self, name: str) -> Residual:
        for residual in self.residuals:
            if residual.name == name:
                return residual
        raise FaultModelError(f"no residual {name!r} in this fault model")

    def hypothesis(self, name: str) -> FaultHypothesis:
        for hypothesis in self.hypotheses:
            if hypothesis.name == name:
                return hypothesis
        raise FaultModelError(f"no hypothesis {name!r} in this fault model")

    @property
    def modelled(self) -> tuple[FaultHypothesis, ...]:
        return tuple(h for h in self.hypotheses if h.origin == "modelled")

    @property
    def features(self) -> tuple[Feature, ...]:
        return tuple(Feature(r.name, region) for r in self.residuals for region in r.regions)

    def with_language_hypothesis(self, name: str, label: str) -> FaultModel:
        """This model plus an unmodelled hypothesis (stays unscored)."""
        added = FaultHypothesis(name=name, label=label, origin="language")
        return self.model_copy(update={"hypotheses": (*self.hypotheses, added)})


def load_fault_model(path: Path) -> tuple[FaultModel, str]:
    """The model at ``path`` and the sha256 of its bytes (evidence cites both)."""
    raw = Path(path).read_bytes()
    data = yaml.safe_load(raw)
    if not isinstance(data, dict) or data.get("schema") != FAULT_MODEL_SCHEMA:
        raise FaultModelError(f"{path} is not a {FAULT_MODEL_SCHEMA} file")
    return FaultModel.model_validate(data), hashlib.sha256(raw).hexdigest()


# ---------------------------------------------------------------------------
# Structural analysis


@dataclass(frozen=True, order=True)
class Feature:
    """One residual in one region: a column of the signature matrix."""

    residual: str
    region: str

    def __str__(self) -> str:
        return f"{self.residual}@{self.region}"


ExclusionKind = Literal["proposed_instrument", "channel_absent", "unqualified", "region_uncovered"]


@dataclass(frozen=True)
class Exclusion:
    """Why a feature cannot be used here, and what would make it usable."""

    feature: Feature
    kind: ExclusionKind
    detail: str
    measurement: str

    @property
    def actionable(self) -> bool:
        """Data alone would make it usable: a qualified evaluator exists."""
        return self.kind in ("channel_absent", "region_uncovered")


@dataclass(frozen=True)
class SeparatingResidual:
    """A feature on which a pair's sign sets differ.

    ``guaranteed`` when the sets are disjoint (every outcome separates the
    two); otherwise only some outcomes do (an outcome outside one set).
    """

    feature: Feature
    #: ``None`` when usable here; else why not.
    excluded: Exclusion | None
    guaranteed: bool


@dataclass(frozen=True)
class PairReport:
    a: str
    b: str
    #: ``distinguishable``: some usable feature has disjoint sets for the two;
    #: ``possibly``: usable features differ but always overlap, so some outcomes
    #: separate them and some do not; ``indistinguishable``: identical on every
    #: usable feature.
    verdict: Literal["distinguishable", "possibly", "indistinguishable"]
    usable_separators: tuple[Feature, ...]
    #: Every feature in the whole model that could separate them, usable or not.
    separators: tuple[SeparatingResidual, ...]

    @property
    def measurements(self) -> tuple[str, ...]:
        """What could separate a pair beyond the usable residuals (deduplicated).

        Actionable first (an existing evaluator lacks only the channel or the
        region), then residuals that still need qualifying or an instrument;
        the fault model's order within each.
        """
        excluded = [s.excluded for s in self.separators if s.excluded is not None]
        ordered = [e for e in excluded if e.actionable] + [e for e in excluded if not e.actionable]
        return tuple(dict.fromkeys(e.measurement for e in ordered))


Detectability = Literal["detectable", "possibly", "undetectable"]


@dataclass(frozen=True)
class StructuralReport:
    model_version: int
    #: Channels the analysis was asked about (sorted).
    channels: tuple[str, ...]
    usable: tuple[Feature, ...]
    excluded: tuple[Exclusion, ...]
    #: Per modelled hypothesis: guaranteed to move a usable residual away from
    #: zero, possibly, or not at all.
    detectability: dict[str, Detectability]
    pairs: tuple[PairReport, ...]
    #: Language-generated hypotheses: listed, never analysed or scored.
    unscored: tuple[str, ...]
    assumptions: tuple[str, ...]

    @property
    def indistinguishable(self) -> tuple[PairReport, ...]:
        """Every pair the usable residuals cannot guarantee to separate."""
        return tuple(p for p in self.pairs if p.verdict != "distinguishable")

    def pair(self, a: str, b: str) -> PairReport:
        for report in self.pairs:
            if {report.a, report.b} == {a, b}:
                return report
        raise FaultModelError(f"no pair {a!r}/{b!r} in this report")

    def as_dict(self) -> dict[str, Any]:
        return {
            "model_version": self.model_version,
            "channels": list(self.channels),
            "usable": [str(f) for f in self.usable],
            "excluded": [
                {
                    "feature": str(e.feature),
                    "kind": e.kind,
                    "detail": e.detail,
                    "measurement": e.measurement,
                }
                for e in self.excluded
            ],
            "detectability": dict(self.detectability),
            "pairs": [
                {
                    "a": p.a,
                    "b": p.b,
                    "verdict": p.verdict,
                    "usable_separators": [str(f) for f in p.usable_separators],
                    "measurements": list(p.measurements),
                }
                for p in self.pairs
            ],
            "indistinguishable": [[p.a, p.b] for p in self.indistinguishable],
            "unscored": list(self.unscored),
            "assumptions": list(self.assumptions),
        }


def _exclusion(
    residual: Residual, feature: Feature, channels: frozenset[str], covered: frozenset[str] | None
) -> Exclusion | None:
    if residual.availability == "proposed":
        return Exclusion(
            feature, "proposed_instrument", "the instrument is not fitted", residual.measurement
        )
    if not residual.qualified:
        assert residual.unqualified_reason is not None
        return Exclusion(
            feature, "unqualified", residual.unqualified_reason.strip(), residual.measurement
        )
    if not residual.present(channels):
        needed = " or ".join("+".join(group) for group in residual.channel_sets)
        return Exclusion(
            feature, "channel_absent", f"not in this channel set: {needed}", residual.measurement
        )
    if covered is not None and feature.region not in covered:
        return Exclusion(
            feature,
            "region_uncovered",
            f"the data does not cover region {feature.region}",
            f"{residual.measurement.rstrip('.')} (covering {feature.region}).",
        )
    return None


def structural_analysis(
    model: FaultModel,
    channels: Iterable[str],
    *,
    covered_regions: Iterable[str] | None = None,
) -> StructuralReport:
    """Detectability and distinguishability on the residuals ``channels`` support.

    ``channels`` are full specs (``obd:MAF``, ``sensor:FUEL_PRESSURE``) the
    data holds; ``covered_regions`` restricts usable features to regions the
    data reached (``None``: assume every region could be reached).
    """
    present = frozenset(channels)
    covered = None if covered_regions is None else frozenset(covered_regions)
    unknown = sorted((covered or frozenset()) - {r.name for r in model.regions})
    if unknown:
        raise FaultModelError(f"undeclared region(s) {unknown}")
    exclusions: dict[Feature, Exclusion] = {}
    usable: list[Feature] = []
    for feature in model.features:
        found = _exclusion(model.residual(feature.residual), feature, present, covered)
        if found is None:
            usable.append(feature)
        else:
            exclusions[feature] = found

    detectability: dict[str, Detectability] = {}
    for hypothesis in model.modelled:
        sets = [hypothesis.allowed(f.residual, f.region) for f in usable]
        if any("0" not in allowed for allowed in sets):
            detectability[hypothesis.name] = "detectable"
        elif any(allowed != SIGN_SETS["0"] for allowed in sets):
            detectability[hypothesis.name] = "possibly"
        else:
            detectability[hypothesis.name] = "undetectable"

    pairs: list[PairReport] = []
    for a, b in combinations(model.modelled, 2):
        separators = tuple(
            SeparatingResidual(
                f,
                exclusions.get(f),
                not (a.allowed(f.residual, f.region) & b.allowed(f.residual, f.region)),
            )
            for f in model.features
            if a.allowed(f.residual, f.region) != b.allowed(f.residual, f.region)
        )
        usable_separators = tuple(
            s.feature for s in separators if s.excluded is None and s.guaranteed
        )
        verdict: Literal["distinguishable", "possibly", "indistinguishable"]
        if usable_separators:
            verdict = "distinguishable"
        elif any(s.excluded is None for s in separators):
            verdict = "possibly"
        else:
            verdict = "indistinguishable"
        pairs.append(PairReport(a.name, b.name, verdict, usable_separators, separators))

    return StructuralReport(
        model_version=model.version,
        channels=tuple(sorted(present)),
        usable=tuple(usable),
        excluded=tuple(exclusions[f] for f in model.features if f in exclusions),
        detectability=detectability,
        pairs=tuple(pairs),
        unscored=tuple(h.name for h in model.hypotheses if h.origin != "modelled"),
        assumptions=model.assumptions,
    )


# ---------------------------------------------------------------------------
# Observations and consistency


@dataclass(frozen=True)
class Observation:
    """A qualified residual sign in one region, or why there is none.

    ``sign`` is set only when the producing tool qualified it; otherwise
    ``unevaluable`` names the reason (an E01 ``UnevaluableReason`` or a
    qualification refusal) and the feature is not read as zero.
    """

    feature: Feature
    sign: ObservedSign | None
    evidence: str | None = None
    unevaluable: str | None = None

    def __post_init__(self) -> None:
        if (self.sign is None) == (self.unevaluable is None):
            raise FaultModelError(
                f"{self.feature}: an observation is a qualified sign or a named "
                "unevaluable reason, exactly one"
            )


def observed_sign(value: float, threshold: float) -> ObservedSign:
    """A qualified sign: beyond ``threshold`` either way, else ``0``."""
    if threshold <= 0:
        raise FaultModelError("a sign needs a positive detection threshold")
    if value >= threshold:
        return "+"
    if value <= -threshold:
        return "-"
    return "0"


@dataclass(frozen=True)
class Conflict:
    feature: Feature
    observed: str
    allowed: tuple[str, ...]
    evidence: str | None


@dataclass(frozen=True)
class HypothesisConsistency:
    name: str
    consistent: bool
    conflicts: tuple[Conflict, ...]
    #: Observations whose sign this hypothesis allows and that are informative
    #: for it (its set there is narrower than ``any``).
    supporting: tuple[Observation, ...]


@dataclass(frozen=True)
class ConsistencyReport:
    model_version: int
    hypotheses: tuple[HypothesisConsistency, ...]
    unevaluable: tuple[Observation, ...]
    unscored: tuple[str, ...]
    assumptions: tuple[str, ...]

    @property
    def consistent(self) -> tuple[str, ...]:
        return tuple(h.name for h in self.hypotheses if h.consistent)

    def of(self, name: str) -> HypothesisConsistency:
        for item in self.hypotheses:
            if item.name == name:
                return item
        raise FaultModelError(f"{name!r} was not evaluated (unmodelled or unknown)")


def evaluate_observations(
    model: FaultModel, observations: Iterable[Observation]
) -> ConsistencyReport:
    """Which modelled hypotheses every qualified observation is consistent with.

    Two observations of one feature that disagree in sign are a conflict in
    the data itself and refused rather than silently resolved.
    """
    signed: dict[Feature, Observation] = {}
    unevaluable: list[Observation] = []
    known = set(model.features)
    for observation in observations:
        if observation.feature not in known:
            raise FaultModelError(f"{observation.feature} is not a feature of this fault model")
        if observation.sign is None:
            unevaluable.append(observation)
            continue
        earlier = signed.get(observation.feature)
        if earlier is not None and earlier.sign != observation.sign:
            raise FaultModelError(
                f"{observation.feature}: evidence {earlier.evidence} says {earlier.sign} and "
                f"{observation.evidence} says {observation.sign}; resolve the data conflict "
                "before reading hypotheses from it"
            )
        signed.setdefault(observation.feature, observation)
    results: list[HypothesisConsistency] = []
    for hypothesis in model.modelled:
        conflicts: list[Conflict] = []
        supporting: list[Observation] = []
        for feature, observation in signed.items():
            allowed = hypothesis.allowed(feature.residual, feature.region)
            assert observation.sign is not None
            if observation.sign not in allowed:
                conflicts.append(
                    Conflict(
                        feature, observation.sign, tuple(sorted(allowed)), observation.evidence
                    )
                )
            elif allowed != SIGN_SETS["any"]:
                supporting.append(observation)
        results.append(
            HypothesisConsistency(
                hypothesis.name, not conflicts, tuple(conflicts), tuple(supporting)
            )
        )
    return ConsistencyReport(
        model_version=model.version,
        hypotheses=tuple(results),
        unevaluable=tuple(unevaluable),
        unscored=tuple(h.name for h in model.hypotheses if h.origin != "modelled"),
        assumptions=model.assumptions,
    )


# ---------------------------------------------------------------------------
# Scores


@dataclass(frozen=True)
class ScoreRefusal:
    hypothesis: str
    reason: str


@dataclass(frozen=True)
class Scores:
    values: dict[str, float] = field(default_factory=dict)
    evidence: dict[str, tuple[str, ...]] = field(default_factory=dict)
    refused: tuple[ScoreRefusal, ...] = ()


def score(model: FaultModel, ledger: MemoryLedger, names: Iterable[str] | None = None) -> Scores:
    """Numerical likelihoods where evidence supports one; a named refusal otherwise.

    No hypothesis is given a number its artifact cannot cite evidence for, and
    the citation must resolve: every id is a current evidence record on this
    vehicle's ledger, or the likelihood is refused naming the ids that do not.
    """
    wanted = list(names) if names is not None else [h.name for h in model.hypotheses]
    values: dict[str, float] = {}
    evidence: dict[str, tuple[str, ...]] = {}
    refused: list[ScoreRefusal] = []
    for name in wanted:
        hypothesis = model.hypothesis(name)
        if hypothesis.origin != "modelled":
            refused.append(
                ScoreRefusal(name, "language-generated and not modelled: unscored until modelled")
            )
            continue
        if hypothesis.likelihood is None:
            refused.append(
                ScoreRefusal(
                    name,
                    "no likelihood with supporting evidence is declared; a probability "
                    "here would be invented",
                )
            )
            continue
        bad: list[str] = []
        for evidence_id in hypothesis.likelihood.evidence:
            try:
                entry = ledger.resolve_evidence(evidence_id)
            except MemoryReferenceError as error:
                bad.append(f"{evidence_id} ({error})")
                continue
            if not entry.current:
                bad.append(f"{evidence_id} (superseded or retracted)")
        if bad:
            refused.append(
                ScoreRefusal(
                    name,
                    "the likelihood cites evidence that does not resolve on this vehicle's "
                    "ledger: " + "; ".join(bad),
                )
            )
            continue
        values[name] = hypothesis.likelihood.value
        evidence[name] = hypothesis.likelihood.evidence
    return Scores(values=values, evidence=evidence, refused=tuple(refused))
