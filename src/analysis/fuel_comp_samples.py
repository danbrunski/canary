"""Per-sample fuelling error on the Fuel Comp cells it was logged at (N02.3).

One row per *candidate*, two routes:

- ``closed_loop``: one candidate per STFT bank-1 read. The error is the
  total trim, STFT + LTFT per bank (``derived:TOTAL_TRIM_B1/B2``'s additive
  definition), the bank mean when both banks pair. Positive = the ECM added
  fuel = the base was lean.
- ``open_loop``: one candidate per commanded-target observation (and per
  fuel-cut sentinel), exactly B06's rows: :func:`src.analysis.lambda_delivery.
  delivery_samples` pairs each with a measured lambda read under the declared
  delay, names its state, and computes ``100 x (lambda_measured /
  lambda_target - 1)``, positive = lean. Bank 2's declared measured source
  is judged on the same rows; the error is the bank mean where both carry a
  residual. Only enrichment rows can be eligible: in closed loop the residual
  is near zero by construction.

Every candidate is paired on acquisition time with rpm and the throttle input
(:func:`src.signals.pairing.match_ordered`, the route's declared separation),
gets the context it is gated on as of its instant (coolant, baro, fuel status,
each within a declared age), and the Fuel Comp load byte reconstructed as an
interval (:mod:`src.analysis.fuel_comp_inputs`) on the revision in force *at
that sample* (per-sample, so a session straddling a flash splits). Where it
sits on the table is judged per route:

- closed loop: a steadiness gate over the dwell before the sample, running
  forward to its latest paired read, so every read it is built from lies in
  the span judged steady; rpm is one word;
- open loop (N02.5 redesign): no steadiness gate. The lambda read measures
  fuel set somewhere in the B06 enrichment transport-delay bracket before
  it, so rpm and throttle are the hull of the reads bounding that span (rpm
  words ``rpm_word_lo..rpm_word_hi``), and the cell is ambiguous when they
  read more than one pair of rows (``rpm_moves_cell``) or columns.

MAF and the BFS word ride along per sample (``maf_gs``, ``bfs_raw``) for the
droop, diagnostic and knock-on stages; they are never gated on. Each sample
ends in exactly one state:

- ``eligible``;
- ``gated`` with the gate's name (``reason``);
- ``unpaired``: the input was captured but no read is within the separation
  (or the declared age), named;
- ``input_missing``: the session never captured that input, or the image for
  the revision cannot be read (``rom_image:<refusal>``).

:class:`SessionSamples` counts them per route, state and reason, splits the
eligible ones by throttle route (:func:`eligible_summary`), and says which kind
of empty an empty route is (``never_captured``, ``unresolvable`` or
``filtered``).
Every constant is in the profile's ``fuel_comp_fit.yaml``; B06's states,
holdoffs and delays are ``lambda_delivery.yaml``'s, read, never restated.
"""

from __future__ import annotations

from collections import Counter
from collections.abc import Callable, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import numpy as np
import pandas as pd

from src.analysis.channels import (
    TIMESTAMP_BASIS_COLUMN,
    ChannelSpecError,
    raw_read_filters,
    read_channels,
)
from src.analysis.fuel_comp_inputs import (
    ClosedPositionEstimate,
    FuelCompDeclaration,
    FuelCompInputError,
    did_raw_counts,
    estimate_closed_position,
    load_interval,
    purge_area,
    purge_command_bytes,
    rpm_word,
    throttle_counts_did,
    throttle_counts_pid,
)
from src.analysis.fuel_comp_rom import (
    FuelCompCalibration,
    FuelCompRomRefusalError,
    RevisionImageResolver,
)
from src.analysis.lambda_delivery import (
    ELIGIBLE,
    LambdaDeliveryDeclaration,
    declared_delays_s,
    delivery_samples,
    resolve_quantity,
    target_side_from_arrays,
)
from src.decode.dids import DidMap, load_did_map
from src.decode.ecm_lookup import search_axis
from src.decode.fuel_comp_load import Interval, spans_one_cell
from src.paths import BINDINGS_YAML, DIDS_YAML
from src.preprocessing.loader import read_session
from src.signals.afr import AfrCalibration
from src.signals.pairing import (
    PairingPolicy,
    basis_floor_s,
    match_ordered,
    to_seconds,
    weakest_basis,
)
from src.vehicle.tune_revisions import TuneRevisionRegistry

#: Bumped whenever a state, gate, pairing or column changes meaning.
#: 3: open loop placed as an interval over the B06 delay bracket (no
#: steadiness gate); rpm word and row intervals; MAF and BFS carried.
SAMPLES_VERSION = 3

ROUTES = ("closed_loop", "open_loop")
STATES = ("eligible", "gated", "unpaired", "input_missing")
#: ``input_missing`` reasons that more recording of the same channels cannot
#: cure (:func:`_empty_kinds`' ``unresolvable``).
UNRESOLVABLE_REASONS = ("rom_image:", "closed_position")
#: The fuel status channel both routes gate on (SAE J1979 PID 0x03).
FUEL_STATUS_SPEC = "obd:FUEL_STATUS"

#: Every column of :attr:`SessionSamples.frame`, in order.
COLUMNS = (
    "session",
    "route",
    "timestamp",
    "revision",
    "image_sha256",
    "state",
    "reason",
    "rpm",
    "rpm_word",
    "rpm_word_lo",
    "rpm_word_hi",
    "throttle_route",
    "throttle_validated",
    "throttle_counts_lo",
    "throttle_counts_hi",
    "purge_basis",
    "purge_area_lo",
    "purge_area_hi",
    "idle_air_lo",
    "idle_air_hi",
    "byte_lo",
    "byte_hi",
    "x_lo",
    "x_hi",
    "rpm_index",
    "rpm_fraction_q16",
    "rpm_row_first",
    "rpm_row_last",
    "load_col_first",
    "load_col_last",
    "cell_ambiguous",
    "idle_air_moves_cell",
    "purge_moves_cell",
    "throttle_moves_cell",
    "rpm_moves_cell",
    "coolant_c",
    "baro_kpa",
    "maf_gs",
    "bfs_raw",
    "fuel_status_prior",
    "fuel_status_next",
    "commanded_source",
    "commanded_phi",
    "target_lambda",
    "lambda_b1",
    "lambda_b2",
    "stft_b1",
    "ltft_b1",
    "stft_b2",
    "ltft_b2",
    "total_trim_b1",
    "total_trim_b2",
    "error_b1_pct",
    "error_b2_pct",
    "error_pct",
    "error_banks",
    "delivery_state",
    "delivery_state_b2",
    "max_separation_s",
    "pairing_max_separation_s",
)


@dataclass(frozen=True)
class _Series:
    times: np.ndarray
    values: np.ndarray
    #: The weakest timestamp basis among the rows (:func:`weakest_basis`).
    basis: str = "acquired"

    @property
    def captured(self) -> bool:
        return bool(len(self.times))

    def clock_refusal(self, policy: PairingPolicy) -> str | None:
        """``None`` when this clock can carry the policy's separation, else the
        basis that cannot (a round stamp, or no provenance at all)."""
        floor = basis_floor_s(self.basis, self.times)  # type: ignore[arg-type]
        return None if floor <= policy.max_separation_s else self.basis

    def label(self, spec: str, policy: PairingPolicy) -> str:
        """``spec`` as an ``unpaired`` reason, naming a clock that cannot pair."""
        refusal = self.clock_refusal(policy)
        return spec if refusal is None else f"{spec} (clock: {refusal})"


_EMPTY = _Series(np.array([], dtype=float), np.array([], dtype=float))


@dataclass
class SessionSamples:
    """One session's candidates, their states, and what an empty route means."""

    session: str
    frame: pd.DataFrame
    counts: dict[str, dict[str, int]]
    #: Per route: ``None`` when something is eligible, else
    #: ``never_captured: <inputs>`` or ``filtered: <top reasons>``.
    empty: dict[str, str | None]
    revisions: list[str]
    straddles_flash: bool
    refusals: dict[str, str]
    closed_position: dict[str, Any] | None
    throttle_route: str | None
    commanded_source: str | None
    delivery_constants_informed: bool
    notes: list[str] = field(default_factory=list)

    def as_dict(self) -> dict[str, Any]:
        return {
            "session": self.session,
            "samples_version": SAMPLES_VERSION,
            "counts": self.counts,
            "eligible": eligible_summary(self.frame),
            "empty": self.empty,
            "revisions": self.revisions,
            "straddles_flash": self.straddles_flash,
            "refusals": self.refusals,
            "closed_position": self.closed_position,
            "throttle_route": self.throttle_route,
            "commanded_source": self.commanded_source,
            "delivery_constants_informed": self.delivery_constants_informed,
            "notes": list(self.notes),
        }


# -- reading ----------------------------------------------------------------------


def _wanted_specs(
    declaration: FuelCompDeclaration, delivery: LambdaDeliveryDeclaration
) -> list[str]:
    inputs = declaration.inputs
    rules = declaration.samples
    specs = [
        inputs.rpm.channel,
        inputs.throttle_pid.channel,
        inputs.throttle_did.channel,
        *inputs.purge.sources,
        *rules.closed_loop.trims.values(),
        FUEL_STATUS_SPEC,
        *rules.gates.coolant.sources,
        rules.gates.baro.channel,
        rules.context.maf,
        rules.context.bfs,
        delivery.measured(rules.open_loop.measured_source).spec,
    ]
    if rules.open_loop.bank2_measured_source:
        specs.append(delivery.measured(rules.open_loop.bank2_measured_source).spec)
    for name in rules.open_loop.commanded_preference:
        source = delivery.commanded(name)
        specs.append(source.spec)
        if source.sentinel_spec:
            specs.append(source.sentinel_spec)
    return list(dict.fromkeys(specs))


def _read_long(
    session: Path | pd.DataFrame,
    specs: Sequence[str],
    *,
    calibration: AfrCalibration | None,
    afr_stoich: float,
    bindings_path: Path,
    dids_path: Path,
) -> dict[str, pd.DataFrame]:
    """Each spec as a long frame on the acquisition clock; an unresolvable or
    absent spec is an empty frame (never captured)."""
    rows: Path | pd.DataFrame = session
    if isinstance(session, Path):
        filters = raw_read_filters(list(specs), bindings_path=bindings_path, dids_path=dids_path)
        rows = read_session(session, filters=filters or None)
    empty = pd.DataFrame(columns=["timestamp", "spec", "value", "unit"])

    def read(wanted: list[str]) -> pd.DataFrame:
        return read_channels(
            rows,
            wanted,
            acquired_time=True,
            calibration=calibration,
            afr_stoich=afr_stoich,
            bindings_path=bindings_path,
            dids_path=dids_path,
        )

    # One read for every spec: the reader parses each row's metadata once per
    # call, so per-spec calls cost the whole session's parse each time. A spec
    # that does not resolve fails the shared read; then each is read alone and
    # the failing one is an empty frame (never captured).
    try:
        together = read(list(specs))
    except (ChannelSpecError, KeyError, ValueError):
        out: dict[str, pd.DataFrame] = {}
        for spec in specs:
            try:
                out[spec] = read([spec])
            except (ChannelSpecError, KeyError, ValueError):
                out[spec] = empty
        return out
    return {spec: together[together["spec"] == spec] for spec in specs}


def _series(frame: pd.DataFrame | None) -> _Series:
    if frame is None or frame.empty:
        return _EMPTY
    ordered = frame.sort_values("timestamp", kind="stable")
    times = np.asarray(to_seconds(pd.to_datetime(ordered["timestamp"], utc=True)), dtype=float)
    values = pd.to_numeric(ordered["value"], errors="coerce").to_numpy(dtype=float)
    keep = np.isfinite(times) & np.isfinite(values)
    basis = weakest_basis(
        ordered[TIMESTAMP_BASIS_COLUMN] if TIMESTAMP_BASIS_COLUMN in ordered else []
    )
    return _Series(times[keep], values[keep], basis)


# -- pairing helpers --------------------------------------------------------------


def _pair(anchor: np.ndarray, series: _Series, policy: PairingPolicy) -> np.ndarray:
    """Each anchor's paired read index into ``series`` (``-1`` for none);
    one-to-one, anchor-referenced (:func:`match_ordered`)."""
    out = np.full(len(anchor), -1, dtype=np.intp)
    if not len(anchor) or not series.captured or series.clock_refusal(policy) is not None:
        return out
    matched = match_ordered(anchor, series.times, policy)
    out[matched.index_a] = matched.index_b
    return out


def _separation(anchor: np.ndarray, series: _Series, index: np.ndarray) -> np.ndarray:
    out = np.full(len(anchor), np.nan)
    ok = index >= 0
    out[ok] = np.abs(series.times[index[ok]] - anchor[ok])
    return out


def _as_of(series: _Series, at: np.ndarray, max_age: float) -> np.ndarray:
    """The latest value at or before each instant, NaN when none is that recent."""
    out = np.full(len(at), np.nan)
    if not series.captured or not len(at):
        return out
    index = np.searchsorted(series.times, at, side="right") - 1
    ok = index >= 0
    age = np.full(len(at), np.inf)
    age[ok] = at[ok] - series.times[index[ok]]
    take = ok & (age <= max_age)
    out[take] = series.values[index[take]]
    return out


def _next_within(series: _Series, at: np.ndarray, max_age: float) -> np.ndarray:
    """The first value strictly after each instant, NaN when none is that soon."""
    out = np.full(len(at), np.nan)
    if not series.captured or not len(at):
        return out
    index = np.searchsorted(series.times, at, side="right")
    ok = index < len(series.times)
    gap = np.full(len(at), np.inf)
    gap[ok] = series.times[index[ok]] - at[ok]
    take = ok & (gap <= max_age)
    out[take] = series.values[index[take]]
    return out


def _window_span(
    series: _Series, start: np.ndarray, end: np.ndarray
) -> tuple[np.ndarray, np.ndarray]:
    """``(n, span)`` of the finite values over reads in ``[start_i, end_i]``."""
    n = np.zeros(len(start), dtype=int)
    span = np.full(len(start), np.nan)
    if not series.captured:
        return n, span
    lo = np.searchsorted(series.times, start, side="left")
    hi = np.searchsorted(series.times, end, side="right")
    for i, (a, b) in enumerate(zip(lo, hi, strict=True)):
        if b > a:
            window = series.values[a:b]
            window = window[np.isfinite(window)]
            n[i] = len(window)
            if len(window):
                span[i] = float(window.max() - window.min())
    return n, span


def _any_in_window(times: np.ndarray, flags: np.ndarray, at: np.ndarray, back: float) -> np.ndarray:
    """Whether any flagged read lies in ``[t - back, t]``."""
    if not len(times) or not len(at):
        return np.zeros(len(at), dtype=bool)
    cumulative = np.concatenate([[0], np.cumsum(flags.astype(int))])
    lo = np.searchsorted(times, at - back, side="left")
    hi = np.searchsorted(times, at, side="right")
    return np.asarray(cumulative[hi] - cumulative[lo] > 0)


def _seconds_since(cut_times: np.ndarray, at: np.ndarray) -> np.ndarray:
    """Seconds since the latest cut observation at or before each instant."""
    out = np.full(len(at), np.inf)
    if not len(cut_times):
        return out
    index = np.searchsorted(cut_times, at, side="right") - 1
    ok = index >= 0
    out[ok] = at[ok] - cut_times[index[ok]]
    return out


# -- the throttle input -----------------------------------------------------------


@dataclass(frozen=True)
class _Throttle:
    route: str | None
    series: _Series
    #: Per read: the w2CC8 interval, or ``None`` when it cannot be formed.
    counts: list[Interval | None]
    validated: bool
    closed_position: ClosedPositionEstimate | None
    missing_reason: str | None


def _throttle_input(
    long: dict[str, pd.DataFrame],
    declaration: FuelCompDeclaration,
    catalog: DidMap,
    coolant: _Series,
) -> _Throttle:
    inputs = declaration.inputs
    pid = _series(long.get(inputs.throttle_pid.channel))
    if pid.captured:
        counts: list[Interval | None] = []
        for value in pid.values:
            try:
                counts.append(throttle_counts_pid(value, declaration))
            except FuelCompInputError:
                counts.append(None)
        return _Throttle("pid_0x11", pid, counts, True, None, None)
    did_frame = long.get(inputs.throttle_did.channel)
    did = _series(did_frame)
    if not did.captured:
        return _Throttle(
            None,
            _EMPTY,
            [],
            False,
            None,
            f"throttle ({inputs.throttle_pid.channel} or {inputs.throttle_did.channel})",
        )
    raw = did_raw_counts(did.values, inputs.throttle_did.channel, catalog)
    rpm = _series(long.get(inputs.rpm.channel))
    estimate = estimate_closed_position(
        did.times, raw, rpm.times, rpm.values, coolant.times, coolant.values, declaration
    )
    if estimate.interval is None:
        return _Throttle(
            "did_0x120F",
            _Series(did.times, raw.astype(float), did.basis),
            [None] * len(raw),
            False,
            estimate,
            "closed_position",
        )
    closed = estimate.interval
    counts = [throttle_counts_did(int(r), closed, inputs.throttle_did.counts_max) for r in raw]
    return _Throttle(
        "did_0x120F",
        _Series(did.times, raw.astype(float), did.basis),
        counts,
        inputs.throttle_did.validated,
        estimate,
        None,
    )


# -- the reconstruction per candidate ---------------------------------------------


def _rows_read(breakpoints: Sequence[int], words: Interval) -> tuple[int, int]:
    """``(first, last)`` Fuel Comp row the rpm search reads across a word
    interval: monotone in the word, so the low end's row to the high end's
    upper row (one row on a breakpoint or past an end, two between)."""
    first = search_axis(breakpoints, words.lo)
    last = search_axis(breakpoints, words.hi)
    assert first is not None and last is not None
    return first.index, last.index + (1 if last.fraction_q16 else 0)


def _reconstruct(
    calibration: FuelCompCalibration,
    *,
    throttle: Interval,
    word: int,
    words: Interval,
    idle_air: Interval,
    idle_nominal: int,
    purge: Interval,
) -> dict[str, Any]:
    """The byte interval over every input, the Fuel Comp rows and columns it
    reads, and which input moves it across cells.

    ``words`` is the rpm word interval (one word in closed loop; the delay
    bracket's hull in open loop), ``word`` the nominal one the row columns
    describe. ``cell_ambiguous``: the whole interval reads more than one
    adjacent pair of load columns, or the words more than one adjacent pair
    of rows, so no single cell can be named. The ``*_moves_cell`` flags name
    the culprit: the same test with only that input spanning its interval,
    the others held at a representative value (throttle and purge at their
    interval's lower middle, idle air at its declared nominal, rpm at the
    nominal word).
    """
    full = load_interval(
        throttle_counts=throttle,
        rpm_words=[words.lo, words.hi],
        idle_air=idle_air,
        purge=purge,
        calibration=calibration,
    )
    axis = calibration.load_axis_raw

    def moves(throttle_: Interval, idle_: Interval, purge_: Interval, words_: Interval) -> bool:
        part = load_interval(
            throttle_counts=throttle_,
            rpm_words=[words_.lo, words_.hi],
            idle_air=idle_,
            purge=purge_,
            calibration=calibration,
        )
        return not spans_one_cell(axis, part.byte)

    t_mid, p_mid = Interval.exactly(throttle.mid), Interval.exactly(purge.mid)
    i_nom, w_nom = Interval.exactly(idle_nominal), Interval.exactly(word)
    rpm_hit = search_axis(calibration.rpm_breakpoints, word)
    assert rpm_hit is not None
    row_first, row_last = _rows_read(calibration.rpm_breakpoints, words)
    rows_ambiguous = row_last - row_first > 1
    first = search_axis(axis, full.low.byte)
    last = search_axis(axis, full.high.byte)
    assert first is not None and last is not None
    return {
        "byte_lo": full.low.byte,
        "byte_hi": full.high.byte,
        "x_lo": full.low.x,
        "x_hi": full.high.x,
        "rpm_index": rpm_hit.index,
        "rpm_fraction_q16": rpm_hit.fraction_q16,
        "rpm_row_first": row_first,
        "rpm_row_last": row_last,
        "load_col_first": first.index,
        "load_col_last": last.index + (1 if last.fraction_q16 else 0),
        "cell_ambiguous": rows_ambiguous or not spans_one_cell(axis, full.byte),
        "idle_air_moves_cell": moves(t_mid, idle_air, p_mid, w_nom),
        "purge_moves_cell": moves(t_mid, i_nom, purge, w_nom),
        "throttle_moves_cell": moves(throttle, i_nom, p_mid, w_nom),
        "rpm_moves_cell": rows_ambiguous or moves(t_mid, i_nom, p_mid, words),
    }


# -- the session ------------------------------------------------------------------


def _first_captured(
    long: dict[str, pd.DataFrame], specs: Sequence[str]
) -> tuple[str | None, _Series]:
    for spec in specs:
        series = _series(long.get(spec))
        if series.captured:
            return spec, series
    return None, _EMPTY


class _States:
    """Each candidate's state and reason; the first assignment wins."""

    def __init__(self, n: int) -> None:
        self.state = np.full(n, "", dtype=object)
        self.reason = np.full(n, None, dtype=object)

    def assign(self, mask: np.ndarray, kind: str, why: str) -> None:
        take = np.asarray(mask, dtype=bool) & (self.state == "")
        self.state[take] = kind
        self.reason[take] = why

    def finish(self) -> None:
        self.state[self.state == ""] = ELIGIBLE


@dataclass
class _Session:
    """One session's inputs, read once and shared by both routes."""

    name: str
    declaration: FuelCompDeclaration
    delivery: LambdaDeliveryDeclaration
    long: dict[str, pd.DataFrame]
    #: Per route: the declared pairing separation.
    policies: dict[str, PairingPolicy]
    rpm: _Series
    coolant_spec: str | None
    coolant: _Series
    baro: _Series
    fuel_status: _Series
    purge: _Series
    purge_bytes: list[Interval]
    throttle: _Throttle
    maf: _Series
    bfs: _Series
    #: Open loop: the B06 enrichment transport-delay bracket ``(lo, hi)`` s,
    #: and the commanded source's own pairing shift, the measured instant.
    delay_bracket_s: tuple[float, float]
    cuts: np.ndarray
    cut_source_captured: bool
    registry: TuneRevisionRegistry
    resolve: Callable[[str], FuelCompCalibration]
    calibrations: dict[str, FuelCompCalibration] = field(default_factory=dict)
    refusals: dict[str, str] = field(default_factory=dict)
    revisions_seen: set[str] = field(default_factory=set)

    def calibration(self, revision: str) -> FuelCompCalibration | None:
        self.revisions_seen.add(revision)
        if revision not in self.calibrations and revision not in self.refusals:
            try:
                self.calibrations[revision] = self.resolve(revision)
            except FuelCompRomRefusalError as exc:
                self.refusals[revision] = exc.reason
        return self.calibrations.get(revision)

    def series(self, spec: str | None) -> _Series:
        return _series(self.long.get(spec)) if spec else _EMPTY

    def session_missing(self) -> list[str]:
        """Inputs both routes need that this session never captured."""
        gates = self.declaration.samples.gates
        missing: list[str] = []
        if not self.rpm.captured:
            missing.append(self.declaration.inputs.rpm.channel)
        if self.throttle.route is None:
            missing.append(self.throttle.missing_reason or "throttle")
        if not self.fuel_status.captured:
            missing.append(FUEL_STATUS_SPEC)
        if self.coolant_spec is None:
            missing.append("coolant (" + " or ".join(gates.coolant.sources) + ")")
        if not self.baro.captured:
            missing.append(gates.baro.channel)
        return missing


def _fuel_cuts(
    long: dict[str, pd.DataFrame],
    declaration: FuelCompDeclaration,
    delivery: LambdaDeliveryDeclaration,
    afr_calibration: AfrCalibration | None,
    afr_stoich: float,
) -> tuple[np.ndarray, bool]:
    """Every fuel-cut observation on any declared commanded source (B06's
    signatures: the RAM word's sentinel, PID 0x44's cut value), sorted."""
    found: list[np.ndarray] = []
    for source_name in declaration.samples.open_loop.commanded_preference:
        source = delivery.commanded(source_name)
        target = _series(long.get(source.spec))
        sentinel = _series(long.get(source.sentinel_spec)) if source.sentinel_spec else _EMPTY
        if not target.captured and not sentinel.captured:
            continue
        side = target_side_from_arrays(
            target.times,
            target.values,
            sentinel.times,
            commanded=source,
            quantity=resolve_quantity(source, afr_calibration),
            afr_stoich=afr_stoich,
        )
        found.append(side.times[side.cut])
    if not found:
        return np.array([], dtype=float), False
    return np.sort(np.concatenate(found)), True


@dataclass(frozen=True)
class _OpenLoop:
    """B06's delivery rows for one commanded source, per bank."""

    commanded: str | None
    #: Bank 1's rows (the declared ``measured_source``): the candidates.
    rows: pd.DataFrame
    #: Bank 2's rows for the same target observations, when declared and
    #: captured; row ``i`` judges the same target read as ``rows`` row ``i``.
    bank2: pd.DataFrame | None


def _open_loop_rows(
    ctx: _Session, afr_calibration: AfrCalibration | None, afr_stoich: float
) -> _OpenLoop:
    """B06's delivery rows for the first declared commanded source captured,
    against bank 1's measured source and, where captured, bank 2's."""
    rules = ctx.declaration.samples.open_loop
    delivery = ctx.delivery
    name = next(
        (n for n in rules.commanded_preference if ctx.series(delivery.commanded(n).spec).captured),
        None,
    )
    if name is None:
        return _OpenLoop(None, pd.DataFrame(), None)
    source = delivery.commanded(name)
    sentinel = ctx.long.get(source.sentinel_spec) if source.sentinel_spec else None
    empty = pd.DataFrame(columns=["timestamp", "spec", "value", "unit"])

    def judged(measured_source: str) -> pd.DataFrame:
        measured = ctx.long.get(delivery.measured(measured_source).spec)
        return delivery_samples(
            target=ctx.long[source.spec],
            measured=measured if measured is not None else empty,
            sentinel=sentinel if sentinel is not None and len(sentinel) else None,
            declaration=delivery,
            target_source=name,
            measured_source=measured_source,
            calibration=afr_calibration,
            afr_stoich=afr_stoich,
            # The declared measured sources are PID-only and carry no baro
            # gain (B06.7), so none is passed.
            baro=None,
        ).frame

    bank2 = None
    if (
        rules.bank2_measured_source is not None
        and ctx.series(delivery.measured(rules.bank2_measured_source).spec).captured
    ):
        bank2 = judged(rules.bank2_measured_source)
    return _OpenLoop(name, judged(rules.measured_source), bank2)


def _paired_values(
    times: np.ndarray, series: _Series, policy: PairingPolicy
) -> tuple[np.ndarray, np.ndarray]:
    """``(index, values)`` of each anchor's one-to-one paired read."""
    index = _pair(times, series, policy)
    values = np.full(len(times), np.nan)
    values[index >= 0] = series.values[index[index >= 0]]
    return index, values


def _paired_times(series: _Series, index: np.ndarray) -> np.ndarray:
    """Each anchor's paired read's acquisition time (NaN where unpaired)."""
    out = np.full(len(index), np.nan)
    ok = index >= 0
    out[ok] = series.times[index[ok]]
    return out


def _error_columns(
    ctx: _Session,
    route: str,
    times: np.ndarray,
    columns: dict[str, Any],
    states: _States,
    open_loop: _OpenLoop,
    policy: PairingPolicy,
) -> list[np.ndarray]:
    """Fill the route's error inputs; assign their pairing states. Returns the
    acquisition times of the reads paired here that describe the operating
    point (the trims), NaN where unpaired."""
    rules = ctx.declaration.samples
    n = len(times)
    paired_times: list[np.ndarray] = []
    if route == "closed_loop":
        trims = rules.closed_loop.trims
        missing = [spec for spec in trims.values() if not ctx.series(spec).captured]
        if not ctx.cut_source_captured:
            missing.append("fuel-cut source (a declared commanded target)")
        for spec in missing:
            states.assign(np.ones(n, dtype=bool), "input_missing", spec)
        unpaired: list[tuple[np.ndarray, str]] = []
        for role, spec in trims.items():
            series = ctx.series(spec)
            if role == "stft_b1":
                columns[role] = series.values
                continue
            index, values = _paired_values(times, series, policy)
            columns[role] = values
            paired_times.append(_paired_times(series, index))
            unpaired.append((index < 0, series.label(spec, policy)))
        total_b1 = columns["stft_b1"].astype(float) + columns["ltft_b1"].astype(float)
        total_b2 = columns["stft_b2"].astype(float) + columns["ltft_b2"].astype(float)
        columns["total_trim_b1"] = total_b1
        columns["total_trim_b2"] = total_b2
        columns["error_b1_pct"] = total_b1
        columns["error_b2_pct"] = total_b2
        columns["error_pct"] = (total_b1 + total_b2) / 2.0
        columns["error_banks"] = np.full(n, "mean", dtype=object)
        # The commanded word in closed loop: the first preferred source that
        # is valid in that regime (PID 0x44 is not, lambda_delivery.yaml).
        ram = next(
            source
            for source in (ctx.delivery.commanded(n) for n in rules.open_loop.commanded_preference)
            if "closed_loop" in source.valid_regimes
        )
        ram_series = ctx.series(ram.spec)
        _, columns["commanded_phi"] = _paired_values(times, ram_series, policy)
        columns["commanded_source"] = np.full(
            n, ram.name if ram_series.captured else None, dtype=object
        )
        for mask, spec in unpaired:
            states.assign(mask, "unpaired", spec)
        return paired_times
    rows = open_loop.rows
    measured_spec = ctx.delivery.measured(rules.open_loop.measured_source).spec
    columns["commanded_source"] = np.full(n, open_loop.commanded, dtype=object)
    columns["commanded_phi"] = rows["target_raw"].to_numpy(dtype=float)
    columns["target_lambda"] = rows["target_lambda"].to_numpy(dtype=float)
    columns["lambda_b1"] = rows["measured_lambda"].to_numpy(dtype=float)
    error_b1 = rows["residual_pct"].to_numpy(dtype=float)
    columns["error_b1_pct"] = error_b1
    delivery_state = rows["state"].to_numpy(dtype=object)
    columns["delivery_state"] = delivery_state
    error = error_b1.copy()
    banks = np.full(n, "b1", dtype=object)
    if open_loop.bank2 is not None:
        bank2 = open_loop.bank2
        columns["lambda_b2"] = bank2["measured_lambda"].to_numpy(dtype=float)
        error_b2 = bank2["residual_pct"].to_numpy(dtype=float)
        columns["error_b2_pct"] = error_b2
        columns["delivery_state_b2"] = bank2["state"].to_numpy(dtype=object)
        both = np.isfinite(error_b1) & np.isfinite(error_b2)
        error[both] = (error_b1[both] + error_b2[both]) / 2.0
        banks[both] = "mean"
    columns["error_pct"] = error
    columns["error_banks"] = banks
    measured = ctx.series(measured_spec)
    if not measured.captured:
        states.assign(np.ones(n, dtype=bool), "input_missing", measured_spec)
    b06_policy = PairingPolicy.within(
        pd.Timedelta(seconds=ctx.delivery.acquisition.max_separation_s), reference="a"
    )
    if measured.clock_refusal(b06_policy) is not None:
        states.assign(np.ones(n, dtype=bool), "unpaired", measured.label(measured_spec, b06_policy))
    states.assign(delivery_state == "measured_missing", "unpaired", measured_spec)
    for b06 in sorted(set(delivery_state) - {ELIGIBLE, "measured_missing"}):
        states.assign(delivery_state == b06, "gated", f"b06:{b06}")
    regime = rows["regime"].to_numpy(dtype=object)
    states.assign(regime != "enrichment", "gated", "closed_loop_regime")
    return paired_times


@dataclass(frozen=True)
class _Bracket:
    """Open loop, per candidate: rpm words and throttle counts over the span
    the measured fuel was set in (:class:`src.analysis.fuel_comp_inputs.
    DelayBracket`), or ``False`` where the span cannot be bounded."""

    rpm_ok: np.ndarray
    words: list[Interval | None]
    throttle_ok: np.ndarray
    counts: list[Interval | None]
    #: A zero rpm read inside the span: the engine was not turning for part
    #: of it, and the chain's divide guard (``w116A = 0`` at word 0) breaks
    #: the byte's monotonicity in rpm, so no interval can be formed.
    rpm_zero: np.ndarray


def _hull(
    times: np.ndarray,
    lows: np.ndarray,
    highs: np.ndarray,
    start: np.ndarray,
    end: np.ndarray,
    max_gap: float,
) -> tuple[np.ndarray, np.ndarray, np.ndarray]:
    """``(ok, lo, hi)`` over every read from the last at or before ``start``
    to the first at or after ``end``; not ok when either end has no read, two
    consecutive reads in the hull are more than ``max_gap`` apart, or a read
    in it has no value."""
    n = len(start)
    ok = np.zeros(n, dtype=bool)
    lo = np.full(n, np.nan)
    hi = np.full(n, np.nan)
    if not len(times) or not n:
        return ok, lo, hi
    first = np.searchsorted(times, start, side="right") - 1
    last = np.searchsorted(times, end, side="left")
    for i in range(n):
        a, b = int(first[i]), int(last[i])
        if a < 0 or b >= len(times):
            continue
        if b > a and float(np.max(np.diff(times[a : b + 1]))) > max_gap:
            continue
        seg_lo, seg_hi = lows[a : b + 1], highs[a : b + 1]
        if not (np.isfinite(seg_lo).all() and np.isfinite(seg_hi).all()):
            continue
        ok[i] = True
        lo[i] = float(seg_lo.min())
        hi[i] = float(seg_hi.max())
    return ok, lo, hi


def _delay_bracket(
    ctx: _Session, times: np.ndarray, shift_s: float, policy: PairingPolicy
) -> _Bracket:
    """rpm and throttle over ``[t + shift - hi, t + shift - lo]`` for each
    open-loop candidate: the hull of the reads bounding that span, no two
    consecutive reads in it further apart than the declared gap (the clock
    must still carry the route's pairing separation)."""
    lo_s, hi_s = ctx.delay_bracket_s
    measured_at = times + shift_s
    start, end = measured_at - hi_s, measured_at - lo_s
    gap = ctx.declaration.samples.open_loop.delay_bracket.max_read_gap_s
    n = len(times)
    rpm, throttle = ctx.rpm, ctx.throttle
    words: list[Interval | None] = [None] * n
    rpm_ok = np.zeros(n, dtype=bool)
    rpm_zero = np.zeros(n, dtype=bool)
    if rpm.captured and rpm.clock_refusal(policy) is None:
        ok, low, high = _hull(rpm.times, rpm.values, rpm.values, start, end, gap)
        rpm_zero = ok & (low <= 0)
        for i in np.flatnonzero(ok & ~rpm_zero):
            try:
                words[i] = Interval(
                    rpm_word(low[i], ctx.declaration), rpm_word(high[i], ctx.declaration)
                )
            except FuelCompInputError:
                continue
            rpm_ok[i] = True
    counts: list[Interval | None] = [None] * n
    throttle_ok = np.zeros(n, dtype=bool)
    series = throttle.series
    if series.captured and series.clock_refusal(policy) is None:
        lows = np.array([np.nan if c is None else c.lo for c in throttle.counts], dtype=float)
        highs = np.array([np.nan if c is None else c.hi for c in throttle.counts], dtype=float)
        ok, low, high = _hull(series.times, lows, highs, start, end, gap)
        for i in np.flatnonzero(ok):
            counts[i] = Interval(int(low[i]), int(high[i]))
        throttle_ok = ok
    return _Bracket(rpm_ok, words, throttle_ok, counts, rpm_zero)


def _route_frame(
    ctx: _Session, route: str, times: np.ndarray, open_loop: _OpenLoop
) -> pd.DataFrame:
    """One route's candidates as :data:`COLUMNS`, each in exactly one state."""
    declaration = ctx.declaration
    rules = declaration.samples
    gates = rules.gates
    inputs = declaration.inputs
    policy = ctx.policies[route]
    n = len(times)
    stamps = pd.Series(pd.to_datetime(times, unit="s", utc=True))
    revisions = ctx.registry.revisions_at(stamps).to_numpy(dtype=object)
    for revision in {r for r in revisions if r is not None}:
        ctx.calibration(revision)
    throttle = ctx.throttle
    rpm_index = _pair(times, ctx.rpm, policy)
    throttle_index = _pair(times, throttle.series, policy)
    purge_index = _pair(times, ctx.purge, policy)
    bracket = None
    if route == "open_loop":
        source = open_loop.commanded or rules.open_loop.commanded_preference[0]
        shift = declared_delays_s(ctx.delivery, source)["enrichment"]
        bracket = _delay_bracket(ctx, times, shift, policy)
    coolant_now = _as_of(ctx.coolant, times, gates.coolant.max_age_s)
    baro_now = _as_of(ctx.baro, times, gates.baro.max_age_s)
    route_rules = rules.closed_loop if route == "closed_loop" else rules.open_loop
    fs_prior = _as_of(ctx.fuel_status, times, route_rules.fuel_status_max_age_s)
    fs_next = _next_within(ctx.fuel_status, times, route_rules.fuel_status_max_age_s)

    columns: dict[str, Any] = {name: np.full(n, np.nan, dtype=object) for name in COLUMNS}
    columns.update(
        session=np.full(n, ctx.name, dtype=object),
        route=np.full(n, route, dtype=object),
        timestamp=stamps.to_numpy(),
        revision=revisions,
        throttle_route=np.full(n, throttle.route, dtype=object),
        throttle_validated=np.full(n, throttle.validated, dtype=object),
        coolant_c=coolant_now,
        baro_kpa=baro_now,
        fuel_status_prior=fs_prior,
        fuel_status_next=fs_next,
        idle_air_lo=np.full(n, inputs.idle_air.low, dtype=object),
        idle_air_hi=np.full(n, inputs.idle_air.high, dtype=object),
        maf_gs=_paired_values(times, ctx.maf, policy)[1],
        bfs_raw=_paired_values(times, ctx.bfs, policy)[1],
        pairing_max_separation_s=np.full(n, policy.max_separation_s, dtype=object),
    )
    states = _States(n)
    for missing in ctx.session_missing():
        states.assign(np.ones(n, dtype=bool), "input_missing", missing)
    if throttle.route == "did_0x120F" and throttle.missing_reason == "closed_position":
        detail = throttle.closed_position.reason if throttle.closed_position else ""
        states.assign(np.ones(n, dtype=bool), "input_missing", f"closed_position: {detail}")
    states.assign(
        np.array([r is None for r in revisions], dtype=bool),
        "input_missing",
        "rom_image:before_first_recorded_flash",
    )
    for revision, why in ctx.refusals.items():
        states.assign(revisions == revision, "input_missing", f"rom_image:{why}")
    anchor_spec = (
        rules.closed_loop.trims["stft_b1"]
        if route == "closed_loop"
        else ctx.delivery.commanded(
            open_loop.commanded or rules.open_loop.commanded_preference[0]
        ).spec
    )
    anchor = ctx.series(anchor_spec)
    if anchor.clock_refusal(policy) is not None:
        states.assign(np.ones(n, dtype=bool), "unpaired", anchor.label(anchor_spec, policy))

    paired = _error_columns(ctx, route, times, columns, states, open_loop, policy)
    paired += [_paired_times(ctx.rpm, rpm_index), _paired_times(throttle.series, throttle_index)]
    stacked = np.vstack(paired)
    separation = np.where(np.isfinite(stacked), np.abs(stacked - times), -np.inf).max(axis=0)
    columns["max_separation_s"] = np.where(np.isfinite(separation), separation, np.nan)
    # The steadiness window runs forward to the latest paired read, so every
    # read the sample is built from lies inside the span judged steady.
    window_end = np.fmax(times, np.where(np.isfinite(stacked), stacked, -np.inf).max(axis=0))

    states.assign(rpm_index < 0, "unpaired", ctx.rpm.label(inputs.rpm.channel, policy))
    states.assign(
        throttle_index < 0,
        "unpaired",
        throttle.series.label(throttle.route or "throttle", policy),
    )
    counts_ok = np.array(
        [i >= 0 and throttle.counts[i] is not None for i in throttle_index], dtype=bool
    )
    states.assign(~counts_ok, "unpaired", f"{throttle.route}: no count interval")
    states.assign(~np.isfinite(coolant_now), "unpaired", f"{ctx.coolant_spec} (stale)")
    states.assign(~np.isfinite(baro_now), "unpaired", f"{gates.baro.channel} (stale)")
    states.assign(
        ~np.isfinite(fs_prior) | ~np.isfinite(fs_next), "unpaired", f"{FUEL_STATUS_SPEC} (stale)"
    )
    _assign_gates(ctx, route, times, window_end, states, coolant_now, baro_now, fs_prior, fs_next)
    if bracket is not None:
        rpm_label = ctx.rpm.label(inputs.rpm.channel, policy)
        states.assign(bracket.rpm_zero, "gated", "engine_stopped_in_delay_bracket")
        states.assign(~bracket.rpm_ok, "unpaired", f"{rpm_label} (delay bracket)")
        states.assign(
            ~bracket.throttle_ok,
            "unpaired",
            f"{throttle.series.label(throttle.route or 'throttle', policy)} (delay bracket)",
        )
    states.finish()
    _fill_reconstruction(ctx, columns, revisions, rpm_index, throttle_index, purge_index, bracket)
    columns["state"] = states.state
    columns["reason"] = states.reason
    return pd.DataFrame({name: columns[name] for name in COLUMNS})


def _assign_gates(
    ctx: _Session,
    route: str,
    times: np.ndarray,
    window_end: np.ndarray,
    states: _States,
    coolant_now: np.ndarray,
    baro_now: np.ndarray,
    fs_prior: np.ndarray,
    fs_next: np.ndarray,
) -> None:
    """The declared gates, in order. Closed loop only: steadiness is judged
    over ``[t - dwell_s, window_end]``, the dwell before the sample and
    forward to its latest paired read. Open loop has no steadiness gate; its
    rpm and throttle are intervals over the delay bracket (:func:`_delay_bracket`)."""
    rules = ctx.declaration.samples
    gates = rules.gates
    if route == "closed_loop":
        since = _seconds_since(ctx.cuts, times)
        holdoff = ctx.delivery.eligibility.regimes.closed_loop.post_fuel_cut_holdoff_s
        states.assign(since < holdoff, "gated", "post_fuel_cut")
        codes = np.asarray(rules.closed_loop.closed_loop_codes, dtype=float)
        closed = np.isin(fs_prior, codes) & np.isin(fs_next, codes)
        states.assign(~closed, "gated", "not_closed_loop")
        recent = _any_in_window(
            ctx.fuel_status.times,
            ~np.isin(ctx.fuel_status.values, codes),
            times,
            rules.closed_loop.open_loop_lookback_s,
        )
        states.assign(recent, "gated", "open_loop_recently")
    else:
        codes = np.asarray(rules.open_loop.open_loop_codes, dtype=float)
        open_loop = np.isin(fs_prior, codes) & np.isin(fs_next, codes)
        states.assign(~open_loop, "gated", "not_open_loop")
    states.assign(coolant_now < gates.coolant.min_c, "gated", "cold")
    states.assign(
        (baro_now < gates.baro.min_kpa) | (baro_now > gates.baro.max_kpa),
        "gated",
        "baro_outside_band",
    )
    if route != "closed_loop":
        # Open loop is placed as an interval over the delay bracket instead.
        return
    steady = rules.closed_loop.steady
    start = times - steady.dwell_s
    rpm_n, rpm_span = _window_span(ctx.rpm, start, window_end)
    states.assign(rpm_n < steady.min_samples, "gated", "rpm_steadiness_unobserved")
    states.assign(~(rpm_span <= steady.rpm_max_span), "gated", "rpm_transient")
    mids = np.array(
        [np.nan if c is None else float(c.mid) for c in ctx.throttle.counts], dtype=float
    )
    throttle_n, throttle_span = _window_span(
        _Series(ctx.throttle.series.times, mids), start, window_end
    )
    states.assign(throttle_n < steady.min_samples, "gated", "throttle_steadiness_unobserved")
    states.assign(
        ~(throttle_span <= steady.throttle_max_span_counts), "gated", "throttle_transient"
    )


def _fill_reconstruction(
    ctx: _Session,
    columns: dict[str, Any],
    revisions: np.ndarray,
    rpm_index: np.ndarray,
    throttle_index: np.ndarray,
    purge_index: np.ndarray,
    bracket: _Bracket | None = None,
) -> None:
    """Inputs and the load byte interval wherever rpm, throttle and an image
    exist. Open loop (``bracket``): rpm words and throttle counts are the
    delay bracket's intervals where it could be bounded."""
    declaration = ctx.declaration
    idle_air = declaration.inputs.idle_air
    shift = declaration.inputs.purge.gain_shift
    for i in range(len(revisions)):
        revision = revisions[i]
        calibration = ctx.calibrations.get(revision) if revision is not None else None
        if calibration is not None:
            columns["image_sha256"][i] = calibration.image_sha256
        if rpm_index[i] < 0:
            continue
        rpm_value = float(ctx.rpm.values[rpm_index[i]])
        columns["rpm"][i] = rpm_value
        try:
            word = rpm_word(rpm_value, declaration)
        except FuelCompInputError:
            continue
        columns["rpm_word"][i] = word
        words = Interval.exactly(word)
        if bracket is not None and bracket.rpm_ok[i]:
            hull = bracket.words[i]
            assert hull is not None
            # The span the fuel was set in, alone: the read paired at the
            # target instant may lie after it.
            words = hull
        columns["rpm_word_lo"][i] = words.lo
        columns["rpm_word_hi"][i] = words.hi
        if throttle_index[i] < 0:
            continue
        counts = ctx.throttle.counts[throttle_index[i]]
        if bracket is not None and bracket.throttle_ok[i]:
            counts = bracket.counts[i]
        if counts is None:
            continue
        columns["throttle_counts_lo"][i] = counts.lo
        columns["throttle_counts_hi"][i] = counts.hi
        if calibration is None:
            continue
        if purge_index[i] >= 0:
            command: Interval | None = ctx.purge_bytes[purge_index[i]]
            basis = "paired"
        else:
            command = None
            basis = "unknown" if ctx.purge.captured else "not_captured"
        area = purge_area(command, calibration, shift)
        columns["purge_basis"][i] = basis
        columns["purge_area_lo"][i] = area.lo
        columns["purge_area_hi"][i] = area.hi
        built = _reconstruct(
            calibration,
            throttle=counts,
            word=word if words.exact else (words.lo + words.hi) // 2,
            words=words,
            idle_air=idle_air.interval,
            idle_nominal=idle_air.nominal,
            purge=area,
        )
        for key, value in built.items():
            columns[key][i] = value


def _bracket_seconds(
    declaration: FuelCompDeclaration, delivery: LambdaDeliveryDeclaration
) -> tuple[float, float]:
    """The declared delay source's frozen enrichment bracket, read from
    ``lambda_delivery.yaml``; refused when that source has none."""
    source = declaration.samples.open_loop.delay_bracket.delay_source
    delays = delivery.eligibility.regimes.enrichment.transport_delays
    if source not in delays or delays[source].bracket_s is None:
        raise FuelCompInputError(
            f"samples.open_loop.delay_bracket names {source!r}, which has no frozen "
            "enrichment transport-delay bracket in lambda_delivery.yaml"
        )
    lo, hi = delays[source].bracket_s or (0.0, 0.0)
    return float(lo), float(hi)


def session_samples(
    session: Path | pd.DataFrame,
    *,
    declaration: FuelCompDeclaration,
    delivery: LambdaDeliveryDeclaration,
    afr_calibration: AfrCalibration | None,
    afr_stoich: float,
    registry: TuneRevisionRegistry,
    resolver: RevisionImageResolver | Callable[[str], FuelCompCalibration],
    session_name: str | None = None,
    bindings_path: Path = BINDINGS_YAML,
    dids_path: Path = DIDS_YAML,
) -> SessionSamples:
    """Every fuelling-error candidate of one session, each in exactly one state.

    ``resolver`` maps a revision name to its :class:`FuelCompCalibration`
    (raising :class:`FuelCompRomRefusalError`): a
    :class:`RevisionImageResolver`, or any callable, so tests can supply
    synthetic tables without a private ROM.
    """
    name = session_name or (session.name if isinstance(session, Path) else "frame")
    resolve = resolver.resolve if isinstance(resolver, RevisionImageResolver) else resolver
    rules = declaration.samples
    inputs = declaration.inputs
    catalog = load_did_map(dids_path)
    long = _read_long(
        session,
        _wanted_specs(declaration, delivery),
        calibration=afr_calibration,
        afr_stoich=afr_stoich,
        bindings_path=bindings_path,
        dids_path=dids_path,
    )
    coolant_spec, coolant = _first_captured(long, rules.gates.coolant.sources)
    purge_spec, purge = _first_captured(long, inputs.purge.sources)
    cuts, cut_captured = _fuel_cuts(long, declaration, delivery, afr_calibration, afr_stoich)
    ctx = _Session(
        name=name,
        declaration=declaration,
        delivery=delivery,
        long=long,
        policies={
            route: PairingPolicy.within(
                pd.Timedelta(seconds=getattr(declaration.pairing, route).max_separation_s),
                reference="a",
            )
            for route in ROUTES
        },
        rpm=_series(long.get(inputs.rpm.channel)),
        coolant_spec=coolant_spec,
        coolant=coolant,
        baro=_series(long.get(rules.gates.baro.channel)),
        fuel_status=_series(long.get(FUEL_STATUS_SPEC)),
        purge=purge,
        purge_bytes=purge_command_bytes(purge.values, purge_spec, declaration, catalog),
        throttle=_throttle_input(long, declaration, catalog, coolant),
        maf=_series(long.get(rules.context.maf)),
        bfs=_series(long.get(rules.context.bfs)),
        delay_bracket_s=_bracket_seconds(declaration, delivery),
        cuts=cuts,
        cut_source_captured=cut_captured,
        registry=registry,
        resolve=resolve,
    )
    notes: list[str] = []
    if ctx.throttle.route == "did_0x120F" and not ctx.throttle.validated:
        notes.append(
            "throttle from DID 0x120F minus an estimated closed position: not validated "
            "at acquisition time against PID 0x11; closing capture: "
            + (inputs.throttle_did.closing_capture or "none named")
        )
    open_loop = _open_loop_rows(ctx, afr_calibration, afr_stoich)
    anchors = {
        "closed_loop": ctx.series(rules.closed_loop.trims["stft_b1"]).times,
        "open_loop": (
            np.asarray(to_seconds(open_loop.rows["timestamp"]), dtype=float)
            if len(open_loop.rows)
            else np.array([], dtype=float)
        ),
    }
    frames = [
        _route_frame(ctx, route, times, open_loop) for route, times in anchors.items() if len(times)
    ]
    frame = (
        pd.concat(frames, ignore_index=True)
        if frames
        else pd.DataFrame({column: pd.Series([], dtype=object) for column in COLUMNS})
    )
    return SessionSamples(
        session=name,
        frame=frame,
        counts=route_counts(frame),
        empty=_empty_kinds(frame, declaration, delivery),
        revisions=sorted(ctx.revisions_seen),
        straddles_flash=len(ctx.revisions_seen) > 1,
        refusals=dict(ctx.refusals),
        closed_position=(
            None if ctx.throttle.closed_position is None else ctx.throttle.closed_position.as_dict()
        ),
        throttle_route=ctx.throttle.route,
        commanded_source=open_loop.commanded,
        delivery_constants_informed=name in delivery.eligibility.informed_by,
        notes=notes,
    )


def _empty_kinds(
    frame: pd.DataFrame, declaration: FuelCompDeclaration, delivery: LambdaDeliveryDeclaration
) -> dict[str, str | None]:
    """Per route: ``None`` when something is eligible; otherwise which kind of
    empty, with the top reasons:

    - ``never_captured``: no candidates, or every candidate lacks an input the
      session never recorded -- capture it;
    - ``unresolvable``: every candidate is missing only what the session's
      data cannot supply -- the revision's image (``rom_image:*``) or the
      learned closed position (``closed_position*``) -- so recording more of
      the same will not help, an image or a capture of the missing quantity
      will;
    - ``filtered``: gates and pairing removed them, an answer about the drive.
    """
    rules = declaration.samples
    out: dict[str, str | None] = {}
    for route in ROUTES:
        rows = frame[frame["route"] == route]
        if rows.empty:
            anchor = (
                rules.closed_loop.trims["stft_b1"]
                if route == "closed_loop"
                else " or ".join(
                    delivery.commanded(n).spec for n in rules.open_loop.commanded_preference
                )
            )
            out[route] = f"never_captured: no {anchor} reads"
            continue
        if (rows["state"] == ELIGIBLE).any():
            out[route] = None
            continue
        top = Counter(
            f"{s}:{r}" for s, r in zip(rows["state"], rows["reason"], strict=True)
        ).most_common(3)
        if not (rows["state"] == "input_missing").all():
            kind = "filtered"
        elif rows["reason"].astype(str).str.startswith(UNRESOLVABLE_REASONS).all():
            kind = "unresolvable"
        else:
            kind = "never_captured"
        out[route] = f"{kind}: " + ", ".join(f"{k} ({v})" for k, v in top)
    return out


def eligible_summary(frame: pd.DataFrame) -> dict[str, dict[str, Any]]:
    """Per revision, per route: candidates, eligible, eligible on an
    unambiguous cell, and both split by throttle route with its validation.

    The split is what a fit must not average over: eligible samples from the
    unvalidated DID route (raw counts minus an *estimated* closed position)
    are a different kind of evidence from PID 0x11 samples, and a per-revision
    total hides how much of it rests on the estimate.
    """
    out: dict[str, dict[str, Any]] = {}
    if frame.empty:
        return out
    revision = frame["revision"].astype(object).where(frame["revision"].notna(), "none")
    for (rev, route), rows in frame.groupby([revision, frame["route"]], sort=True):
        eligible = rows[rows["state"] == ELIGIBLE]
        unambiguous = eligible["cell_ambiguous"].astype(object).eq(False)
        by_route: dict[str, dict[str, Any]] = {}
        throttle = (
            rows["throttle_route"].astype(object).where(rows["throttle_route"].notna(), "none")
        )
        for name in sorted(set(throttle)):
            mine = throttle == name
            mine_eligible = mine[eligible.index]
            by_route[str(name)] = {
                "validated": bool(
                    rows.loc[mine, "throttle_validated"].astype(object).eq(True).all()
                ),
                "candidates": int(mine.sum()),
                "eligible": int(mine_eligible.sum()),
                "eligible_cell_unambiguous": int((mine_eligible & unambiguous).sum()),
            }
        out.setdefault(str(rev), {})[str(route)] = {
            "candidates": int(len(rows)),
            "eligible": int(len(eligible)),
            "eligible_cell_unambiguous": int(unambiguous.sum()),
            "by_throttle_route": by_route,
        }
    return out


def route_counts(frame: pd.DataFrame) -> dict[str, dict[str, int]]:
    """Candidates per route by ``state`` and ``state:reason`` (and the total)."""
    out: dict[str, dict[str, int]] = {}
    for route in ROUTES:
        rows = frame[frame["route"] == route]
        counts: dict[str, int] = {"total": int(len(rows))}
        for state in STATES:
            counts[state] = int((rows["state"] == state).sum())
        for (state, why), n in Counter(zip(rows["state"], rows["reason"], strict=True)).items():
            if state != ELIGIBLE:
                counts[f"{state}:{why}"] = int(n)
        out[route] = counts
    return out


__all__ = [
    "COLUMNS",
    "ROUTES",
    "SAMPLES_VERSION",
    "STATES",
    "UNRESOLVABLE_REASONS",
    "SessionSamples",
    "eligible_summary",
    "route_counts",
    "session_samples",
]
