"""The Fuel Comp load byte from logged channels, and its known answer (N02.2).

:mod:`src.decode.fuel_comp_load` is the firmware's arithmetic on raw words.
This module turns what a session *logged* into those words, with every
quantisation and unknown carried as an interval, and checks the result
against the one capture that logged the byte itself:

- **rpm**: PID 0x0C is the rpm word times 12.5, exactly (:func:`rpm_word`).
- **throttle, PID route**: PID 0x11's byte is a truncation of the corrected
  count, so each read is a 3-4 count interval (:func:`throttle_counts_pid`).
- **throttle, DID route**: DID 0x120F is the raw count before the learned
  closed position is subtracted; the closed position is never logged and is
  estimated per session from warm idle (:func:`estimate_closed_position`),
  as an interval. The route is not validated at acquisition time (no session
  checked has both reads within the pairing separation), and every result
  from it says so.
- **purge**: PID 0x2E or DID 0x1110 recover the purge byte exactly, but only
  a read inside the pairing separation is the value at the instant;
  otherwise purge is unknown (every value the code can produce).
- **idle air**: never logged; the profile declares an interval
  (``fuel_comp_fit.yaml`` ``inputs.idle_air``).

:func:`known_answer` reproduces the logged ``0xFFFF118A`` on the
``ram-study`` capture's steady points (the declared ``known_answer`` rule)
and is the test the negative controls in :class:`KnownAnswerVariant` must
fail.

Everything vehicle-specific is the profile's ``fuel_comp_fit.yaml``
(:class:`FuelCompDeclaration`).
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Literal

import numpy as np
import pandas as pd
import yaml
from pydantic import BaseModel, ConfigDict, Field, model_validator

from src.analysis.channels import parse_spec, read_channels
from src.analysis.fuel_comp_rom import FuelCompCalibration, FuelCompRomTables
from src.decode.dids import DidEntry, DidMap, load_did_map
from src.decode.fuel_comp_load import (
    Interval,
    LoadInterval,
    counts_from_truncated_byte,
    pid_byte_from_percent,
    purge_area_from_command_byte,
    purge_area_unknown,
    reconstruct,
    reconstruct_interval,
)
from src.paths import BINDINGS_YAML, DIDS_YAML
from src.signals.pairing import to_seconds

#: File name of the declaration in a vehicle profile directory.
FUEL_COMP_FILE = "fuel_comp_fit.yaml"


class FuelCompInputError(ValueError):
    """A logged input cannot be turned into the chain's raw word."""


class _Strict(BaseModel):
    model_config = ConfigDict(extra="forbid")


class Program(_Strict):
    calibration_id: str = Field(min_length=1)
    map_slot: int = Field(ge=1)
    map_slot_evidence: str = Field(min_length=1)


class RpmInput(_Strict):
    channel: str
    rpm_per_count: float = Field(gt=0)
    evidence: str = Field(min_length=1)


class ThrottlePidInput(_Strict):
    channel: str
    numerator: int = Field(ge=1)
    denominator: int = Field(ge=1)
    full_scale: int = Field(ge=1)
    counts_max: int = Field(ge=1)
    evidence: str = Field(min_length=1)


class ClosedPositionMethod(_Strict):
    """How a DID-only session's learned closed position is estimated."""

    method: Literal["warm_idle_mode_minus_idle_opening"]
    idle_rpm_min: float
    idle_rpm_max: float
    rpm_max_age_s: float = Field(gt=0)
    min_coolant_c: float
    coolant_max_age_s: float = Field(gt=0)
    min_samples: int = Field(ge=1)
    #: Counts above closed at warm idle, ``[lo, hi]``: a measured constraint.
    idle_opening_counts: tuple[int, int]
    status: Literal["proposed", "frozen"]
    evidence: str = Field(min_length=1)

    @model_validator(mode="after")
    def _ordered(self) -> ClosedPositionMethod:
        lo, hi = self.idle_opening_counts
        if not 0 <= lo <= hi:
            raise ValueError("idle_opening_counts must be 0 <= lo <= hi")
        if not self.idle_rpm_min < self.idle_rpm_max:
            raise ValueError("idle_rpm_min must be below idle_rpm_max")
        return self


class ThrottleDidInput(_Strict):
    channel: str
    counts_max: int = Field(ge=1)
    raw_counts_evidence: str = Field(min_length=1)
    closed_position: ClosedPositionMethod
    #: Whether the route has been shown to agree with the PID route at
    #: acquisition time. Required: an unvalidated route says so everywhere.
    validated: bool
    #: The capture that would validate it, when it is not.
    closing_capture: str | None = None

    @model_validator(mode="after")
    def _closing(self) -> ThrottleDidInput:
        if not self.validated and not self.closing_capture:
            raise ValueError("an unvalidated DID route must name the capture that would close it")
        return self


class PurgeInput(_Strict):
    sources: list[str] = Field(min_length=1)
    pid_numerator: int = Field(ge=1)
    pid_denominator: int = Field(ge=1)
    gain_shift: int = Field(ge=0)
    evidence: str = Field(min_length=1)


class IdleAir(_Strict):
    """The declared idle-air interval: a measured constraint, with its conditions."""

    low: int
    high: int
    nominal: int
    conditions: str = Field(min_length=1)
    informed_by: list[str]
    status: Literal["proposed", "frozen"]
    evidence: str = Field(min_length=1)

    @model_validator(mode="after")
    def _inside(self) -> IdleAir:
        if not self.low <= self.nominal <= self.high:
            raise ValueError("idle_air needs low <= nominal <= high")
        return self

    @property
    def interval(self) -> Interval:
        return Interval(self.low, self.high)


class Inputs(_Strict):
    rpm: RpmInput
    throttle_pid: ThrottlePidInput
    throttle_did: ThrottleDidInput
    purge: PurgeInput
    idle_air: IdleAir


class RoutePairing(_Strict):
    """How far apart two reads may be and still be one sample, on one route."""

    max_separation_s: float = Field(gt=0)
    evidence: str = Field(min_length=1)


class Pairing(_Strict):
    """Per route: the separation is a property of how that route's inputs are
    polled, so each states its own with its evidence."""

    closed_loop: RoutePairing
    open_loop: RoutePairing


class KnownAnswerRule(_Strict):
    session: str
    byte_channel: str
    max_gap_s: float = Field(gt=0)
    max_throttle_step_bytes: int = Field(ge=0)
    max_rpm_step: float = Field(ge=0)
    idle_max_pid_byte: int = Field(ge=0)
    nominal_purge: Literal["prior_read"]
    median_ratio_tolerance: float = Field(gt=0)
    min_points_idle: int = Field(ge=1)
    min_points_off_idle: int = Field(ge=1)
    evidence: str = Field(min_length=1)


class SteadyGate(_Strict):
    """rpm and throttle reads in ``[t - dwell_s, t]``: at least ``min_samples``
    of each, spans within the limits (throttle in corrected counts)."""

    dwell_s: float = Field(gt=0)
    min_samples: int = Field(ge=2)
    rpm_max_span: float = Field(ge=0)
    throttle_max_span_counts: int = Field(ge=0)


class ClosedLoopRoute(_Strict):
    trims: dict[Literal["stft_b1", "ltft_b1", "stft_b2", "ltft_b2"], str]
    closed_loop_codes: list[int] = Field(min_length=1)
    fuel_status_max_age_s: float = Field(gt=0)
    open_loop_lookback_s: float = Field(ge=0)
    steady: SteadyGate
    status: Literal["proposed", "frozen"]
    evidence: str = Field(min_length=1)

    @model_validator(mode="after")
    def _all_trims(self) -> ClosedLoopRoute:
        missing = {"stft_b1", "ltft_b1", "stft_b2", "ltft_b2"} - set(self.trims)
        if missing:
            raise ValueError(f"closed_loop.trims must name every trim: missing {sorted(missing)}")
        return self


class OpenLoopRoute(_Strict):
    commanded_preference: list[str] = Field(min_length=1)
    measured_source: str
    #: Bank 2's declared measured source, judged by the same B06 rows.
    bank2_measured_source: str | None = None
    open_loop_codes: list[int] = Field(min_length=1)
    fuel_status_max_age_s: float = Field(gt=0)
    steady: SteadyGate
    status: Literal["proposed", "frozen"]
    evidence: str = Field(min_length=1)


class CoolantGate(_Strict):
    sources: list[str] = Field(min_length=1)
    min_c: float
    max_age_s: float = Field(gt=0)
    status: Literal["proposed", "frozen"]


class BaroGate(_Strict):
    channel: str
    min_kpa: float
    max_kpa: float
    max_age_s: float = Field(gt=0)
    evidence: str = Field(min_length=1)

    @model_validator(mode="after")
    def _band(self) -> BaroGate:
        if not self.min_kpa < self.max_kpa:
            raise ValueError("baro min_kpa must be below max_kpa")
        return self


class Gates(_Strict):
    coolant: CoolantGate
    baro: BaroGate


class SampleRules(_Strict):
    closed_loop: ClosedLoopRoute
    open_loop: OpenLoopRoute
    gates: Gates


class FuelCompDeclaration(_Strict):
    """``fuel_comp_fit.yaml``: every N02 constant, with its evidence."""

    schema_version: Literal[1]
    program: Program
    rom: FuelCompRomTables
    inputs: Inputs
    pairing: Pairing
    known_answer: KnownAnswerRule
    samples: SampleRules

    @model_validator(mode="after")
    def _specs_parse(self) -> FuelCompDeclaration:
        specs = [
            self.inputs.rpm.channel,
            self.inputs.throttle_pid.channel,
            self.inputs.throttle_did.channel,
            *self.inputs.purge.sources,
            self.known_answer.byte_channel,
            *self.samples.closed_loop.trims.values(),
            *self.samples.gates.coolant.sources,
            self.samples.gates.baro.channel,
        ]
        for spec in specs:
            parse_spec(spec)
        return self


def load_fuel_comp_declaration(profile_dir: Path) -> FuelCompDeclaration:
    """``<profile_dir>/fuel_comp_fit.yaml``, validated (``FileNotFoundError``
    when the vehicle has none)."""
    path = profile_dir / FUEL_COMP_FILE
    return FuelCompDeclaration.model_validate(yaml.safe_load(path.read_text()))


# -- logged values to raw words -----------------------------------------------------


def rpm_word(rpm: float, declaration: FuelCompDeclaration) -> int:
    """The engine-speed word behind a PID 0x0C value (refused unless exact)."""
    raw = float(rpm) / declaration.inputs.rpm.rpm_per_count
    word = round(raw)
    if abs(raw - word) > 1e-6 or word < 0:
        raise FuelCompInputError(f"rpm {rpm!r} is not a whole engine-speed word")
    return int(word)


def throttle_counts_pid(percent: float, declaration: FuelCompDeclaration) -> Interval:
    """The corrected throttle counts a PID 0x11 percent can have come from."""
    spec = declaration.inputs.throttle_pid
    try:
        byte = pid_byte_from_percent(percent, full_scale=spec.full_scale)
    except ValueError as exc:
        raise FuelCompInputError(str(exc)) from exc
    return counts_from_truncated_byte(
        byte,
        numerator=spec.numerator,
        denominator=spec.denominator,
        full_scale=spec.full_scale,
        counts_max=spec.counts_max,
    )


def throttle_counts_did(raw: int, closed_position: Interval, counts_max: int) -> Interval:
    """``w2CC8`` from a raw DID 0x120F count and the closed-position interval.

    ``w2CCE = raw - w4C72``, clamped at 0 the way the chain's input is.
    """
    lo = max(0, int(raw) - closed_position.hi)
    hi = max(0, int(raw) - closed_position.lo)
    return Interval(min(lo, counts_max), min(hi, counts_max))


def _catalog_did(catalog: DidMap, name: str) -> DidEntry | None:
    """A DID by name or hex id, resolved as :func:`read_channels` resolves it."""
    wanted = name.upper().removeprefix("DID_")
    for entry in catalog.dids:
        if entry.name.upper() == wanted:
            return entry
    try:
        did_id = int(wanted, 16)
    except ValueError:
        return None
    return next((entry for entry in catalog.dids if entry.did == did_id), None)


def did_raw_counts(values: np.ndarray, spec: str, catalog: DidMap) -> np.ndarray:
    """A DID's stored values back to the payload integers.

    The reader decoded them with the catalog's scale and offset; undoing the
    same decode returns the integer exactly, which is checked (a value that
    does not land on a whole count was stored under another decode).
    """
    source = parse_spec(spec)
    if source.source != "did":
        raise FuelCompInputError(f"{spec} is not a DID spec")
    entry = _catalog_did(catalog, source.name)
    if entry is None:
        raise FuelCompInputError(f"{spec} is not in the DID catalog")
    scale = 1.0 if entry.scale is None else float(entry.scale)
    offset = 0.0 if entry.offset is None else float(entry.offset)
    raw = (np.asarray(values, dtype=float) - offset) / scale
    whole = np.rint(raw)
    if len(raw) and float(np.max(np.abs(raw - whole))) > 1e-6:
        raise FuelCompInputError(
            f"{spec} values do not return to whole counts under the catalog decode "
            f"(scale {scale}, offset {offset})"
        )
    return whole.astype(int)


def purge_command_from_percent(percent: float, declaration: FuelCompDeclaration) -> Interval:
    """The purge bytes a PID 0x2E percent can come from (``A = min(255, b x
    1275/1000)``, handler 0xD461C).

    Below the clamp each ``A`` comes from at most one byte (the factor exceeds
    1), so the byte is exact; an ``A`` no byte produces is refused. ``A = 255``
    is every byte from the first that reaches the clamp up to 255.
    """
    spec = declaration.inputs.purge
    a = pid_byte_from_percent(percent)
    command = -(-a * spec.pid_denominator // spec.pid_numerator)
    if min(255, command * spec.pid_numerator // spec.pid_denominator) != a:
        raise FuelCompInputError(f"PID 0x2E byte {a} is not produced by any purge byte")
    if a == 255:
        return Interval(int(command), 255)
    return Interval.exactly(int(command))


def purge_command_bytes(
    values: np.ndarray, spec: str | None, declaration: FuelCompDeclaration, catalog: DidMap | None
) -> list[Interval]:
    """The purge byte (as an interval) of each stored purge value, from
    either declared source."""
    if spec is None or not len(values):
        return []
    if parse_spec(spec).source == "did":
        if catalog is None:
            raise FuelCompInputError(f"{spec} needs the DID catalog to undo its decode")
        return [Interval.exactly(int(v)) for v in did_raw_counts(values, spec, catalog)]
    return [purge_command_from_percent(v, declaration) for v in values]


def purge_area(command: Interval | None, calibration: FuelCompCalibration, shift: int) -> Interval:
    """``w139E`` over a purge-byte interval, or every value the code can
    produce when purge is unknown.

    The area rises with the byte except at 255, which is ambiguous (the low
    byte may have wrapped) and covers everything from 0.
    """
    gain = calibration.purge_gain
    if command is None:
        return purge_area_unknown(gain=gain, gain_shift=shift)
    low = purge_area_from_command_byte(command.lo, gain=gain, gain_shift=shift)
    high = purge_area_from_command_byte(command.hi, gain=gain, gain_shift=shift)
    return Interval(min(low.lo, high.lo), high.hi)


# -- the learned closed position (DID route) ---------------------------------------


@dataclass(frozen=True)
class ClosedPositionEstimate:
    """One session's ``w4C72`` estimate, or why there is none."""

    interval: Interval | None
    method: str
    n_samples: int
    mode_raw: int | None
    reason: str | None
    validated: bool

    def as_dict(self) -> dict[str, Any]:
        return {
            "interval": None if self.interval is None else [self.interval.lo, self.interval.hi],
            "method": self.method,
            "n_samples": self.n_samples,
            "mode_raw": self.mode_raw,
            "reason": self.reason,
            "validated": self.validated,
        }


def _as_of(times: np.ndarray, values: np.ndarray, at: np.ndarray, max_age: float) -> np.ndarray:
    """The latest value at or before each instant, NaN when older than ``max_age``."""
    out = np.full(len(at), np.nan)
    if not len(times):
        return out
    index = np.searchsorted(times, at, side="right") - 1
    ok = index >= 0
    age = np.full(len(at), np.inf)
    age[ok] = at[ok] - times[index[ok]]
    take = ok & (age <= max_age)
    out[take] = values[index[take]]
    return out


def estimate_closed_position(
    did_times: np.ndarray,
    did_raw: np.ndarray,
    rpm_times: np.ndarray,
    rpm_values: np.ndarray,
    coolant_times: np.ndarray,
    coolant_values: np.ndarray,
    declaration: FuelCompDeclaration,
) -> ClosedPositionEstimate:
    """``w4C72`` by the declared method: the mode of the raw count at warm idle
    minus the declared warm-idle opening, as an interval.

    Inputs are float seconds (sorted) and values. Warm idle is the rpm as of
    each DID read inside the declared band and the coolant as of it at or
    above the declared floor, each within its declared age.
    """
    method = declaration.inputs.throttle_did.closed_position
    validated = declaration.inputs.throttle_did.validated
    rpm = _as_of(rpm_times, rpm_values, did_times, method.rpm_max_age_s)
    coolant = _as_of(coolant_times, coolant_values, did_times, method.coolant_max_age_s)
    idle = (
        np.isfinite(rpm)
        & (rpm >= method.idle_rpm_min)
        & (rpm <= method.idle_rpm_max)
        & np.isfinite(coolant)
        & (coolant >= method.min_coolant_c)
    )
    n = int(idle.sum())
    if n < method.min_samples:
        return ClosedPositionEstimate(
            None,
            method.method,
            n,
            None,
            f"closed_position_unestimated: {n} warm-idle DID reads, the method needs "
            f"{method.min_samples}",
            validated,
        )
    counts, frequency = np.unique(np.asarray(did_raw)[idle].astype(int), return_counts=True)
    mode = int(counts[int(np.argmax(frequency))])
    lo, hi = method.idle_opening_counts
    return ClosedPositionEstimate(
        Interval(mode - hi, mode - lo), method.method, n, mode, None, validated
    )


# -- the known answer ---------------------------------------------------------------


@dataclass(frozen=True)
class KnownAnswerVariant:
    """A deliberately wrong chain, for the negative controls (N02.2).

    The decoded chain is ``KnownAnswerVariant()``. ``rpm_power`` replaces the
    1/rpm dependence with 1/rpm^p (the word fed to the chain is
    ``R x (word / R)^p``, ``R`` the 800 rpm word, so idle is unchanged);
    ``throttle_scaling`` replaces PID 0x11's 321/100 counts per byte;
    ``add_closed_position`` feeds the raw DID count (corrected count plus the
    learned closed position), i.e. DID 0x120F without the correction.
    """

    name: str = "decoded"
    rpm_power: float = 1.0
    throttle_scaling: tuple[int, int] | None = None
    add_closed_position: int | None = None


#: The rpm word ``rpm_power`` pivots about (800 rpm at 12.5 rpm per count).
_VARIANT_PIVOT_WORD = 64


@dataclass
class KnownAnswerResult:
    session: str
    variant: str
    image_sha256: str
    points: pd.DataFrame
    subsets: dict[str, dict[str, Any]] = field(default_factory=dict)
    passed: bool = False
    reasons: list[str] = field(default_factory=list)

    def as_dict(self) -> dict[str, Any]:
        return {
            "session": self.session,
            "variant": self.variant,
            "image_sha256": self.image_sha256,
            "subsets": self.subsets,
            "passed": self.passed,
            "reasons": list(self.reasons),
        }


def _sorted_series(frame: pd.DataFrame, spec: str) -> tuple[np.ndarray, np.ndarray]:
    rows = frame[frame["spec"] == spec].sort_values("timestamp", kind="stable")
    times = to_seconds(pd.to_datetime(rows["timestamp"], utc=True))
    return np.asarray(times, dtype=float), pd.to_numeric(rows["value"]).to_numpy(dtype=float)


def _bracket(times: np.ndarray, t: float, max_gap: float) -> tuple[int, int] | None:
    """The read at or before ``t`` and the one after it, each within ``max_gap``."""
    before = int(np.searchsorted(times, t, side="right")) - 1
    after = before + 1
    if before < 0 or after >= len(times):
        return None
    if t - times[before] > max_gap or times[after] - t > max_gap:
        return None
    return before, after


def known_answer(
    session: Path | pd.DataFrame,
    calibration: FuelCompCalibration,
    declaration: FuelCompDeclaration,
    *,
    variant: KnownAnswerVariant | None = None,
    bindings_path: Path = BINDINGS_YAML,
    dids_path: Path = DIDS_YAML,
) -> KnownAnswerResult:
    """The reconstructed byte against the logged one, on the declared steady points.

    Each logged byte whose throttle and rpm reads on both sides agree within
    the declared steps is a point. Its throttle interval is the union of the
    two reads' count intervals and its rpm the two words (the value at the
    byte's instant is assumed to lie between two agreeing reads: the steady
    rule is that assumption). Per point: the **interval** over throttle, rpm,
    the declared idle-air interval and unknown purge, whether the logged byte
    is inside it, and a **point ratio** ``logged / reconstructed`` at the
    midpoints, the nominal idle air and the prior purge read. Per subset
    (idle / off idle): n, median ratio, p10-p90, containment. Passes when
    both subsets have their declared minimum points and a median ratio within
    the declared tolerance.
    """
    rule = declaration.known_answer
    variant = variant or KnownAnswerVariant()
    specs = [
        rule.byte_channel,
        declaration.inputs.throttle_pid.channel,
        declaration.inputs.rpm.channel,
        *declaration.inputs.purge.sources,
    ]
    frame = read_channels(
        session, specs, acquired_time=True, bindings_path=bindings_path, dids_path=dids_path
    )
    tb, vb = _sorted_series(frame, rule.byte_channel)
    tt, vt = _sorted_series(frame, declaration.inputs.throttle_pid.channel)
    tr, vr = _sorted_series(frame, declaration.inputs.rpm.channel)
    purge_spec = next(
        (s for s in declaration.inputs.purge.sources if (frame["spec"] == s).any()), None
    )
    tp, vp = (
        _sorted_series(frame, purge_spec)
        if purge_spec
        else (np.array([], dtype=float), np.array([], dtype=float))
    )
    purge_commands = purge_command_bytes(
        vp, purge_spec, declaration, load_did_map(dids_path) if purge_spec else None
    )
    pid = declaration.inputs.throttle_pid
    numerator, denominator = variant.throttle_scaling or (pid.numerator, pid.denominator)
    shift = declaration.inputs.purge.gain_shift
    idle_air = declaration.inputs.idle_air
    unknown_purge = purge_area(None, calibration, shift)

    def word_of(rpm: float) -> int:
        word = rpm_word(rpm, declaration)
        if variant.rpm_power == 1.0:
            return word
        scaled = float(_VARIANT_PIVOT_WORD * (word / _VARIANT_PIVOT_WORD) ** variant.rpm_power)
        return min(0xFFFF, max(0, int(round(scaled))))

    def counts_of(byte: int) -> Interval:
        counts = counts_from_truncated_byte(
            byte,
            numerator=numerator,
            denominator=denominator,
            full_scale=pid.full_scale,
            counts_max=pid.counts_max,
        )
        if variant.add_closed_position is not None:
            counts = Interval(
                counts.lo + variant.add_closed_position, counts.hi + variant.add_closed_position
            )
        return counts

    rows: list[dict[str, Any]] = []
    for t, logged in zip(tb, vb, strict=True):
        throttle = _bracket(tt, t, rule.max_gap_s)
        rpm = _bracket(tr, t, rule.max_gap_s)
        if throttle is None or rpm is None or not logged > 0:
            continue
        bytes_ = [pid_byte_from_percent(vt[i]) for i in throttle]
        rpms = [float(vr[i]) for i in rpm]
        if abs(bytes_[0] - bytes_[1]) > rule.max_throttle_step_bytes:
            continue
        if abs(rpms[0] - rpms[1]) > rule.max_rpm_step:
            continue
        intervals = [counts_of(b) for b in bytes_]
        counts = Interval(min(i.lo for i in intervals), max(i.hi for i in intervals))
        words = sorted(word_of(r) for r in rpms)
        prior = int(np.searchsorted(tp, t, side="right")) - 1
        if prior >= 0 and purge_spec is not None:
            command = purge_commands[prior]
            nominal_purge = purge_area(command, calibration, shift).mid
        else:
            nominal_purge = unknown_purge.mid
        point = reconstruct(
            throttle_counts=counts.mid,
            rpm_word=(words[0] + words[1]) // 2,
            idle_air=idle_air.nominal,
            purge_area=nominal_purge,
            curves=calibration.curves,
        ).byte
        low = reconstruct_interval(
            throttle_counts=counts,
            rpm_word=words[1],
            idle_air=idle_air.interval,
            purge_area=unknown_purge,
            curves=calibration.curves,
        ).low.byte
        high = reconstruct_interval(
            throttle_counts=counts,
            rpm_word=words[0],
            idle_air=idle_air.interval,
            purge_area=unknown_purge,
            curves=calibration.curves,
        ).high.byte
        rows.append(
            {
                "timestamp": pd.Timestamp(t, unit="s", tz="UTC"),
                "logged": int(logged),
                "subset": "idle" if max(bytes_) <= rule.idle_max_pid_byte else "off_idle",
                "pid_bytes": tuple(bytes_),
                "counts_lo": counts.lo,
                "counts_hi": counts.hi,
                "rpm_words": tuple(words),
                "point": point,
                "ratio": logged / point if point else np.nan,
                "byte_lo": low,
                "byte_hi": high,
                "inside": low <= logged <= high,
            }
        )
    points = pd.DataFrame(rows)
    result = KnownAnswerResult(
        session=session.name if isinstance(session, Path) else rule.session,
        variant=variant.name,
        image_sha256=calibration.image_sha256,
        points=points,
    )
    minimum = {"idle": rule.min_points_idle, "off_idle": rule.min_points_off_idle}
    for subset in ("idle", "off_idle"):
        part = points[points["subset"] == subset] if len(points) else points
        ratios = part["ratio"].to_numpy(dtype=float) if len(part) else np.array([])
        ratios = ratios[np.isfinite(ratios)]
        stats: dict[str, Any] = {"n": int(len(ratios))}
        if len(ratios):
            stats.update(
                median_ratio=float(np.median(ratios)),
                p10_ratio=float(np.quantile(ratios, 0.1)),
                p90_ratio=float(np.quantile(ratios, 0.9)),
                inside=int(part["inside"].sum()),
                median_interval_width=float(np.median(part["byte_hi"] - part["byte_lo"])),
            )
        result.subsets[subset] = stats
        if stats["n"] < minimum[subset]:
            result.reasons.append(f"{subset}: {stats['n']} points, need {minimum[subset]}")
        elif abs(stats["median_ratio"] - 1.0) > rule.median_ratio_tolerance:
            result.reasons.append(
                f"{subset}: median ratio {stats['median_ratio']:.3f} is outside "
                f"1 +- {rule.median_ratio_tolerance}"
            )
    result.passed = not result.reasons
    return result


def negative_controls(closed_position: int) -> list[KnownAnswerVariant]:
    """The wrong chains the known answer must refuse (N02.2), as listed in the
    checklist: wrong rpm power, wrong throttle scaling, and DID 0x120F used
    without the closed-position correction (``closed_position`` counts)."""
    return [
        KnownAnswerVariant(name="rpm^0.5", rpm_power=0.5),
        KnownAnswerVariant(name="rpm^1.5", rpm_power=1.5),
        # 2.55 and 4.0 counts per PID byte instead of 3.21.
        KnownAnswerVariant(name="2.55 counts per byte", throttle_scaling=(100, 255)),
        KnownAnswerVariant(name="4.0 counts per byte", throttle_scaling=(100, 400)),
        KnownAnswerVariant(name="0x120F uncorrected", add_closed_position=closed_position),
    ]


def load_interval(
    *,
    throttle_counts: Interval,
    rpm_words: Sequence[int],
    idle_air: Interval,
    purge: Interval,
    calibration: FuelCompCalibration,
) -> LoadInterval:
    """The byte interval over every input, rpm included as its word range."""
    words = sorted(int(w) for w in rpm_words)
    low = reconstruct_interval(
        throttle_counts=throttle_counts,
        rpm_word=words[-1],
        idle_air=idle_air,
        purge_area=purge,
        curves=calibration.curves,
    ).low
    high = reconstruct_interval(
        throttle_counts=throttle_counts,
        rpm_word=words[0],
        idle_air=idle_air,
        purge_area=purge,
        curves=calibration.curves,
    ).high
    return LoadInterval(low=low, high=high)


def load_catalog(dids_path: Path = DIDS_YAML) -> DidMap:
    return load_did_map(dids_path)


__all__ = [
    "FUEL_COMP_FILE",
    "ClosedPositionEstimate",
    "FuelCompDeclaration",
    "FuelCompInputError",
    "KnownAnswerResult",
    "KnownAnswerVariant",
    "Pairing",
    "RoutePairing",
    "did_raw_counts",
    "estimate_closed_position",
    "known_answer",
    "load_catalog",
    "load_fuel_comp_declaration",
    "load_interval",
    "negative_controls",
    "purge_area",
    "purge_command_bytes",
    "purge_command_from_percent",
    "rpm_word",
    "throttle_counts_did",
    "throttle_counts_pid",
]
