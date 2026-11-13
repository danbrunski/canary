"""Timing evaluated with the actual lookup context (B08.4).

B04.2's timing surface grades ``obd:TIMING_ADVANCE`` on Mode 01 rpm x
calculated load. The ECM's own timing-trim lookup searches a different
coordinate: the rpm word ``0xFFFF11D8`` against ``0xCC93`` and the BFS-X byte
``0xFFFF103E`` against ``0xCC83``, in a table the HI DET latch
(``0xFFFF283F`` bit 6) selects. This module puts that coordinate, as the
B08.2 provider observes it (:mod:`src.analysis.operating_context`,
``timing_coordinate``), beside the existing surface **on identical samples**,
and says what the samples cover. It fits nothing: a candidate surface goes
through B04's recipe and fitter (:mod:`src.analysis.refit`) and the B02
evaluator, never a fitter of its own.

What it enforces, because each has been learned rather than assumed:

- **The target is final timing.** The target channel must be one the profile
  declares as observing final ignition timing
  (``operating_context.yaml`` ``timing_trim.final_timing_channels``). The trim
  lookup is an intermediate the timing sum consumes, so it is reported only as
  ``trim_*_intermediate`` and is never a target, final timing or knock retard
  (``not_final_timing:<spec>``).
- **Identical samples, never aggregates.** One sample is one read of the
  target. Each axis of the existing surface is paired to it on the
  acquisition clock under a caller-stated :class:`PairingPolicy` (no default),
  and the coordinate's own reads must sit within the timing lookup's declared
  skew of the target read (``skew:target_coordinate``). A sample that cannot
  be paired keeps the reason; nothing is forward-filled.
- **The existing surface is scored by the B02 evaluator**
  (:func:`~src.analysis.behavior_validation.evaluate_rows`) on a frame stamped
  with each session's corpus eligibility, so a capture-profile sample is
  refused by name (R03.12), exactly as it is at refit.
- **A branch is validated only by observing it.** :func:`require_branch_validation`
  refuses ``selection_unobserved`` when no latch was read,
  ``branch_unobserved:<set|clear>`` when one state never was, and
  ``branch_too_few_drives:<state>`` when fewer independent drives than asked
  observed it: a constant flag on one drive validates neither branch.
- **Empty results say which kind of empty** (:class:`IdenticalSampleSummary`):
  every unpaired sample is counted under its reason.
- **A memory-only target is its own path** (B08.4 option c, operator decision
  2026-10-08). ``timing_trim.memory_only_targets`` declares a final-timing
  channel read in the same service-0x23 read as a coordinate input
  (``mem:ECM_IGNITION_TIMING_RAM`` with the rpm word). :func:`memory_only_samples`
  pairs it with the coordinate from that very read (``not_same_read:<input>``
  otherwise) and the other coordinate reads within the lookup's skew. Grading
  it against a surface on Mode 01 axes is refused by name
  (``memory_only_target:<spec>``): that identical-sample comparison is what
  the interim gives up.
"""

from __future__ import annotations

import math
from collections import Counter
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Literal

import numpy as np
import pandas as pd

from src.analysis.behavior_validation_evaluation import evaluate_rows
from src.analysis.behavior_validation_inputs import stamp_profile_exclusions
from src.analysis.ecu_state_models import TRIM_VALUE_MASK, trim_lookup
from src.analysis.operating_context import (
    ACQUIRED,
    ContextProvider,
    MemoryOnlyTargetDecl,
    ResolvedContext,
)
from src.analysis.splits import Drive, group_drives
from src.behavior.behavior_models import BehaviorModel2D
from src.columns import SESSION_COLUMN
from src.refusal import ReasonedRefusal
from src.signals.pairing import PairingPolicy, match_ordered, to_seconds

#: Bumped when a sample's fields, reasons or the summary change.
#: 3 (B08.4 option c): memory-only samples and their refusals.
TIMING_LOOKUP_VERSION = 3

Branch = Literal["set", "clear"]
BRANCHES: tuple[Branch, ...] = ("set", "clear")
#: Independent drives each latch state needs before a branch counts as
#: validated: one drive is not independent evidence (B02's own floor).
MIN_BRANCH_DRIVES = 2


class TimingLookupRefusalError(ReasonedRefusal, ValueError):
    """The evaluation cannot be run or claimed; ``reason`` names why."""


def require_final_timing_target(provider: ContextProvider, spec: str) -> None:
    """Refuse a target that is not declared as observing final timing."""
    declared = provider.definition.timing_trim.final_timing_channels
    if spec not in declared:
        raise TimingLookupRefusalError(
            f"not_final_timing:{spec}",
            f"the target must observe final ignition timing ({', '.join(declared)}); "
            "the timing-trim lookup is an intermediate, never final timing or knock retard",
        )


def require_memory_only_target(provider: ContextProvider, spec: str) -> MemoryOnlyTargetDecl:
    """The declaration of a memory-only target, or ``not_memory_only_target``:
    a target without a same-read coordinate input has no memory-only tuple."""
    require_final_timing_target(provider, spec)
    for target in provider.definition.timing_trim.memory_only_targets:
        if target.channel == spec:
            return target
    raise TimingLookupRefusalError(
        f"not_memory_only_target:{spec}",
        "operating_context.yaml timing_trim.memory_only_targets does not declare it: its "
        "coordinate is not read in the same service-0x23 read",
    )


def refuse_mode01_surface_for_memory_target(
    provider: ContextProvider, model: BehaviorModel2D
) -> None:
    """Refuse ``memory_only_target:<spec>`` for a model whose output is a
    memory-only target and whose axes are not all memory reads.

    B08.4 option (c) narrows the tuple to memory and gives up the
    identical-sample comparison with the Mode 01 surface for as long as it is
    in use (operator decision 2026-10-08). Refused here rather than quietly
    re-joined, so the comparison returns only by a decision that lifts it."""
    declared = {t.channel for t in provider.definition.timing_trim.memory_only_targets}
    if model.output not in declared:
        return
    foreign = sorted(a.channel for a in model.axes if not str(a.channel).startswith("mem:"))
    if foreign:
        raise TimingLookupRefusalError(
            f"memory_only_target:{model.output}",
            f"{model.output} is graded on its memory-only tuple; the identical-sample "
            f"comparison against a surface on {foreign} is given up while B08.4 option (c) "
            "is in use (operator decision 2026-10-08)",
        )


def require_policy_within_skew(provider: ContextProvider, policy: PairingPolicy) -> None:
    """Refuse an axis pairing looser than the timing lookup's own skew limit:
    rpm/load read seconds from the timing read do not describe that read."""
    limit = provider.definition.timing_trim.max_acquisition_skew_s
    if policy.max_separation_s > limit:
        raise TimingLookupRefusalError(
            "pairing_exceeds_lookup_skew",
            f"axis pairing within {policy.max_separation_s:g} s is looser than the timing "
            f"lookup's max_acquisition_skew_s {limit:g} s",
        )


# -- coordinate samples ----------------------------------------------------------------


@dataclass(frozen=True)
class CoordinateSample:
    """The timing lookup coordinate at one instant, or why it is not observed."""

    session: str
    t: float
    refused: str | None
    rpm_word: int | None = None
    rpm: float | None = None
    load_raw: int | None = None
    high_det: bool | None = None
    high_det_refused: str | None = None
    #: Acquisition time of each read the coordinate used, by input name.
    read_times: Mapping[str, float] = field(default_factory=dict)

    @property
    def observed(self) -> bool:
        return self.refused is None


def _coordinate(session: str, t: float, record: Mapping[str, Any]) -> CoordinateSample:
    item = record["timing_coordinate"]
    if "refused" in item:
        return CoordinateSample(session=session, t=t, refused=str(item["refused"]))
    reads = item["acquisition"]["reads"]
    return CoordinateSample(
        session=session,
        t=t,
        refused=None,
        rpm_word=int(item["rpm_word"]),
        rpm=None if item["rpm"] is None else float(item["rpm"]),
        load_raw=int(item["load_raw"]),
        high_det=item["high_det"],
        high_det_refused=item["high_det_refused"],
        read_times={name: float(r["acquired_at"]) for name, r in reads.items()},
    )


def _reads(long: pd.DataFrame, spec: str) -> pd.DataFrame:
    return long[long["spec"].astype(str) == spec]


def coordinate_samples(
    provider: ContextProvider,
    long: pd.DataFrame,
    *,
    session: str,
    at_spec: str | None = None,
) -> list[CoordinateSample]:
    """The coordinate at every read of ``at_spec`` (default: the lookup's rpm
    input, whose window carries the rpm word). ``long`` is acquisition-timed
    :func:`~src.analysis.channels.read_channels` output; the provider refuses
    anything else."""
    spec = at_spec or provider.definition.inputs[provider.definition.timing_trim.rows.input].channel
    times = sorted(set(to_seconds(_reads(long, spec)["timestamp"]).tolist()))
    if not times:
        return []
    records = provider.evaluate_frame(long, times)
    return [_coordinate(session, t, r) for t, r in zip(times, records, strict=True)]


# -- identical samples with the existing surface ------------------------------------------


@dataclass(frozen=True)
class IdenticalSample:
    """One read of the target, with the existing surface's axes and the lookup
    coordinate paired to it, or the reason it cannot be."""

    session: str
    t: float
    target_spec: str
    target: float
    #: Existing-surface axis channel -> paired value (absent when unpaired).
    axes: Mapping[str, float]
    #: Axis channel -> |acquisition separation| from the target read, seconds.
    axis_separation_s: Mapping[str, float]
    coordinate: CoordinateSample
    #: Why this is not an identical sample (``None``: it is one).
    reason: str | None


def identical_samples(
    provider: ContextProvider,
    long: pd.DataFrame,
    model: BehaviorModel2D,
    *,
    session: str,
    policy: PairingPolicy,
) -> list[IdenticalSample]:
    """Pair each read of ``model``'s output with its axes and the coordinate.

    Every axis read is paired at most once (:func:`match_ordered`, target as
    the reference side) within ``policy``. The coordinate is the provider's
    ``timing_coordinate`` at the target read's time, and its reads must lie
    within the timing lookup's ``max_acquisition_skew_s`` of the target read.
    """
    target_spec = model.output
    require_final_timing_target(provider, target_spec)
    refuse_mode01_surface_for_memory_target(provider, model)
    require_policy_within_skew(provider, policy)
    target = _reads(long, target_spec)
    if target.empty:
        return []
    unacquired = target["timestamp_basis"].astype(str) != ACQUIRED
    t_target = to_seconds(target["timestamp"])
    order = np.argsort(t_target, kind="stable")
    t_target = t_target[order]
    values = pd.to_numeric(target["value"], errors="coerce").to_numpy(dtype=float)[order]
    unacquired_target = unacquired.to_numpy()[order]
    axis_specs = [axis.channel for axis in model.axes]
    paired: dict[str, dict[int, tuple[float, float]]] = {}
    axis_reason: dict[str, str] = {}
    for spec in axis_specs:
        captured = _reads(long, spec)
        side = captured[captured["timestamp_basis"].astype(str) == ACQUIRED]
        if side.empty:
            # Read on another clock is not the same as never read.
            axis_reason[spec] = (
                f"not_captured:{spec}" if captured.empty else f"clock_unacquired:{spec}"
            )
            paired[spec] = {}
            continue
        t_side = to_seconds(side["timestamp"])
        v_side = pd.to_numeric(side["value"], errors="coerce").to_numpy(dtype=float)
        matched = match_ordered(
            t_target,
            t_side,
            PairingPolicy(
                max_separation=policy.max_separation, reference="a", tie_break=policy.tie_break
            ),
        )
        paired[spec] = {
            int(i): (float(v_side[j]), float(s))
            for i, j, s in zip(matched.index_a, matched.index_b, matched.separation_s, strict=True)
        }
    records = provider.evaluate_frame(long, t_target.tolist())
    skew = provider.definition.timing_trim.max_acquisition_skew_s
    out: list[IdenticalSample] = []
    for i, t in enumerate(t_target.tolist()):
        coordinate = _coordinate(session, t, records[i])
        axes = {spec: paired[spec][i][0] for spec in axis_specs if i in paired[spec]}
        separation = {spec: paired[spec][i][1] for spec in axis_specs if i in paired[spec]}
        reason: str | None = None
        if unacquired_target[i]:
            reason = f"clock_unacquired:{target_spec}"
        elif not math.isfinite(values[i]):
            reason = f"missing:{target_spec}"
        else:
            for spec in axis_specs:
                if i not in paired[spec]:
                    reason = axis_reason.get(spec, f"unpaired:{spec}")
                    break
        if reason is None and coordinate.refused is not None:
            reason = coordinate.refused
        if reason is None and any(abs(r - t) > skew for r in coordinate.read_times.values()):
            reason = "skew:target_coordinate"
        out.append(
            IdenticalSample(
                session=session,
                t=t,
                target_spec=target_spec,
                target=float(values[i]),
                axes=axes,
                axis_separation_s=separation,
                coordinate=coordinate,
                reason=reason,
            )
        )
    return out


@dataclass(frozen=True)
class ScoredSample:
    """An identical sample graded by the existing surface (B02 evaluator)."""

    sample: IdenticalSample
    expected: float | None
    residual: float | None
    #: The evaluator's refusal (``profile_excluded:...``, ``off_map``, ...).
    reason: str | None


def score_identical_samples(
    provider: ContextProvider,
    model: BehaviorModel2D,
    samples: Sequence[IdenticalSample],
    *,
    sessions: Sequence[Path],
) -> list[ScoredSample]:
    """Grade the identical samples with :func:`evaluate_rows`.

    One row per sample, built from its own paired reads (no grid). The frame
    is stamped from ``sessions`` (:func:`stamp_profile_exclusions`), so a
    capture-profile sample is refused by name, as at refit (R03.12)."""
    require_final_timing_target(provider, model.output)
    foreign = sorted({s.target_spec for s in samples if s.target_spec != model.output})
    if foreign:
        raise TimingLookupRefusalError(
            "target_mismatch",
            f"samples read {foreign}, the model grades {model.output}",
        )
    refuse_mode01_surface_for_memory_target(provider, model)
    usable = [s for s in samples if s.reason is None]
    if not usable:
        return []
    frame = pd.DataFrame(
        {
            "timestamp": [datetime.fromtimestamp(s.t, tz=UTC) for s in usable],
            SESSION_COLUMN: [s.session for s in usable],
            **{axis.channel: [float(s.axes[axis.channel]) for s in usable] for axis in model.axes},
            model.output: [s.target for s in usable],
        }
    )
    frame = stamp_profile_exclusions(frame, sessions)
    ev = evaluate_rows(model, frame)
    out: list[ScoredSample] = []
    for i, sample in enumerate(usable):
        reason = ev.reasons[i]
        graded = bool(ev.evaluated[i])
        out.append(
            ScoredSample(
                sample=sample,
                expected=float(ev.expected[i]) if graded else None,
                residual=float(ev.actual[i] - ev.expected[i]) if graded else None,
                reason=None if graded else str(reason if reason is not None else "not_evaluated"),
            )
        )
    return out


# -- memory-only samples (B08.4 option c) ----------------------------------------------------


#: Two channels decoded from one read carry one acquisition time; this only
#: absorbs float round-off in a stored timestamp.
SAME_READ_TOLERANCE_S = 1e-6


@dataclass(frozen=True)
class MemoryOnlySample:
    """One read of a memory-only target with the coordinate from the same
    read, or the reason it is not one. No Mode 01 axis is paired."""

    session: str
    t: float
    target_spec: str
    target: float
    coordinate: CoordinateSample
    #: Why this is not a memory-only sample (``None``: it is one).
    reason: str | None


def memory_only_samples(
    provider: ContextProvider,
    long: pd.DataFrame,
    *,
    session: str,
    target: str,
) -> list[MemoryOnlySample]:
    """The coordinate at every read of a declared memory-only ``target``.

    The ``same_read_as`` input must be the read the target came in (its
    acquisition time equal to the target's), else ``not_same_read:<input>``;
    every other coordinate read must lie within the lookup's
    ``max_acquisition_skew_s`` of it (``skew:target_coordinate``).

    The rest of the tuple is taken from the target's own **read group**: the
    reads between the previous and the next read of the target's window. A
    window's memory burst reads the target's window and the others
    milliseconds apart, in either order, so each other coordinate input uses
    its first read at or after the target inside the group, else its last
    one before it. A read from another group is never used: evaluated just
    before the next spark read, the next burst's BFS-X (read first in that
    burst) used to replace this burst's own. An input with no read in the
    group is refused ``no_read_in_group:<input>``, one whose read in the
    group lies beyond the skew ``skew:target_coordinate``, and one the
    provider still takes from an earlier group ``other_read_group:<input>``. The provider's
    own freshness and tuple-skew refusals apply unchanged."""
    declared = require_memory_only_target(provider, target)
    reads = _reads(long, target)
    if reads.empty:
        return []
    t_target = to_seconds(reads["timestamp"])
    order = np.argsort(t_target, kind="stable")
    t_target = t_target[order]
    values = pd.to_numeric(reads["value"], errors="coerce").to_numpy(dtype=float)[order]
    unacquired = (reads["timestamp_basis"].astype(str) != ACQUIRED).to_numpy()[order]
    skew = provider.definition.timing_trim.max_acquisition_skew_s
    trim = provider.definition.timing_trim
    others = [
        name
        for name in (trim.rows.input, trim.columns.input, trim.select_input)
        if name != declared.same_read_as
    ]
    inputs = provider.definition.inputs
    other_times = {
        name: np.sort(to_seconds(_reads(long, inputs[name].channel)["timestamp"]))
        for name in others
    }
    n = len(t_target)
    group_lo = np.concatenate([[-np.inf], t_target[:-1]])
    group_hi = np.concatenate([t_target[1:], [np.inf]])
    t_eval = t_target.copy()
    no_read: list[str | None] = [None] * n
    beyond = [False] * n
    for name in others:
        times = other_times[name]
        at_or_after = np.searchsorted(times, t_target - SAME_READ_TOLERANCE_S, side="left")
        for i in range(n):
            j = int(at_or_after[i])
            if j < len(times) and times[j] < group_hi[i]:
                chosen = float(times[j])
            elif j > 0 and times[j - 1] > group_lo[i]:
                chosen = float(times[j - 1])
            else:
                if no_read[i] is None and name != trim.select_input:
                    no_read[i] = f"no_read_in_group:{name}"
                continue
            if abs(chosen - t_target[i]) > skew:
                beyond[i] = True
            t_eval[i] = max(t_eval[i], chosen)
    records = provider.evaluate_frame(long, t_eval.tolist())
    out: list[MemoryOnlySample] = []
    for i, t in enumerate(t_target.tolist()):
        coordinate = _coordinate(session, t, records[i])
        reason: str | None = None
        if unacquired[i]:
            reason = f"clock_unacquired:{target}"
        elif not math.isfinite(values[i]):
            reason = f"missing:{target}"
        elif no_read[i] is not None:
            reason = no_read[i]
        elif beyond[i]:
            reason = "skew:target_coordinate"
        elif coordinate.refused is not None:
            reason = coordinate.refused
        elif abs(coordinate.read_times[declared.same_read_as] - t) > SAME_READ_TOLERANCE_S:
            reason = f"not_same_read:{declared.same_read_as}"
        elif foreign := sorted(
            name
            for name, at in coordinate.read_times.items()
            if name != declared.same_read_as and at <= group_lo[i]
        ):
            reason = f"other_read_group:{foreign[0]}"
        elif any(abs(r - t) > skew for r in coordinate.read_times.values()):
            reason = "skew:target_coordinate"
        out.append(
            MemoryOnlySample(
                session=session,
                t=t,
                target_spec=target,
                target=float(values[i]),
                coordinate=coordinate,
                reason=reason,
            )
        )
    return out


@dataclass(frozen=True)
class IdenticalSampleSummary:
    """Counts by reason, so an empty comparison names its kind of empty."""

    target_reads: int
    identical: int
    graded: int
    not_identical: dict[str, int]
    not_graded: dict[str, int]


def summarize(
    samples: Sequence[IdenticalSample | MemoryOnlySample], scored: Sequence[ScoredSample]
) -> IdenticalSampleSummary:
    return IdenticalSampleSummary(
        target_reads=len(samples),
        identical=sum(1 for s in samples if s.reason is None),
        graded=sum(1 for s in scored if s.reason is None),
        not_identical=dict(Counter(str(s.reason) for s in samples if s.reason is not None)),
        not_graded=dict(Counter(str(s.reason) for s in scored if s.reason is not None)),
    )


# -- branch evidence -------------------------------------------------------------------


@dataclass(frozen=True)
class BranchEvidence:
    """Which latch states were observed, on how many samples and drives."""

    samples: dict[Branch, int]
    drives: dict[Branch, tuple[str, ...]]
    #: Coordinate samples without an observed latch, by reason.
    unobserved: dict[str, int]


def drives_of(samples: Iterable[CoordinateSample]) -> list[Drive]:
    """Independent physical drives over the samples' sessions
    (:func:`~src.analysis.splits.group_drives`, parsed time, never names)."""
    ranges: dict[str, tuple[datetime | None, datetime | None]] = {}
    for s in samples:
        at = datetime.fromtimestamp(s.t, tz=UTC)
        first, last = ranges.get(s.session, (at, at))
        assert first is not None and last is not None
        ranges[s.session] = (min(first, at), max(last, at))
    return group_drives(ranges)


def branch_evidence(samples: Sequence[CoordinateSample]) -> BranchEvidence:
    """Branch counts per independent physical drive. The drives are built
    here (:func:`drives_of`), so two sessions of one drive can never count
    as two drives."""
    drives = drives_of(samples)
    drive_of = {piece: d.drive_id for d in drives for piece in d.pieces}
    counts: dict[Branch, int] = {"set": 0, "clear": 0}
    seen: dict[Branch, set[str]] = {"set": set(), "clear": set()}
    unobserved: Counter[str] = Counter()
    for s in samples:
        if s.refused is not None:
            unobserved[s.refused] += 1
            continue
        if s.high_det is None:
            unobserved[str(s.high_det_refused)] += 1
            continue
        branch: Branch = "set" if s.high_det else "clear"
        counts[branch] += 1
        seen[branch].add(drive_of[s.session])
    return BranchEvidence(
        samples=counts,
        drives={b: tuple(sorted(v)) for b, v in seen.items()},
        unobserved=dict(unobserved),
    )


def require_branch_validation(evidence: BranchEvidence, *, min_drives: int) -> None:
    """Refuse a claim about the selected timing table unless both latch
    states were observed on at least ``min_drives`` independent drives each.

    A latch read constant on one drive validates neither branch: the other
    was never seen, and the seen one rests on one drive."""
    if min_drives < MIN_BRANCH_DRIVES:
        raise ValueError(
            f"min_drives must be at least {MIN_BRANCH_DRIVES}: one drive validates no branch"
        )
    if not any(evidence.samples.values()):
        raise TimingLookupRefusalError(
            "selection_unobserved",
            f"no HI DET latch read was usable ({evidence.unobserved or 'no coordinate samples'})",
        )
    for branch in BRANCHES:
        if not evidence.samples[branch]:
            raise TimingLookupRefusalError(
                f"branch_unobserved:{branch}",
                f"the latch was never read {branch}; the {branch} branch is unvalidated",
            )
    for branch in BRANCHES:
        if len(evidence.drives[branch]) < min_drives:
            raise TimingLookupRefusalError(
                f"branch_too_few_drives:{branch}",
                f"{branch} was observed on {len(evidence.drives[branch])} independent "
                f"drive(s) < {min_drives}",
            )


# -- where the coordinate falls on the image's trim tables ---------------------------------


@dataclass(frozen=True)
class TrimCellCoverage:
    """The coordinate samples placed on one image's trim axes.

    ``trim_*_intermediate`` are trim lookups, never final timing. The pedal
    column override and the cold rule are not applied (``assumptions``)."""

    provenance: dict[str, Any]
    assumptions: tuple[str, ...]
    samples: int
    #: (row, column) of the lower-left cell read -> samples.
    cells: dict[tuple[int, int], int]
    #: Cells whose masked values differ between trim A and trim B.
    distinguishable_cells: tuple[tuple[int, int], ...]
    #: Samples whose interpolated trim A and trim B differ: where the
    #: selected table could be told apart from the trim at all.
    samples_reading_distinguishable: int
    clamped: dict[str, int]
    trim_a_intermediate: tuple[int, ...]
    trim_b_intermediate: tuple[int, ...]


def trim_cell_coverage(
    samples: Sequence[CoordinateSample],
    resolved: ResolvedContext,
    *,
    assumptions: Sequence[str] = (),
) -> TrimCellCoverage:
    a, b = resolved.trim_a, resolved.trim_b
    if (a.row_axis_raw, a.column_axis_raw) != (b.row_axis_raw, b.column_axis_raw):
        raise TimingLookupRefusalError(
            "axis_mismatch", "trim A and trim B resolved on different axes"
        )
    observed = [s for s in samples if s.observed]
    cells: Counter[tuple[int, int]] = Counter()
    clamped: Counter[str] = Counter()
    trims_a: list[int] = []
    trims_b: list[int] = []
    for s in observed:
        assert s.rpm_word is not None and s.load_raw is not None
        la = trim_lookup(a.cells, a.row_axis_raw, a.column_axis_raw, s.rpm_word, s.load_raw)
        lb = trim_lookup(b.cells, b.row_axis_raw, b.column_axis_raw, s.rpm_word, s.load_raw)
        cells[(la.lookup.row.index, la.lookup.column.index)] += 1
        if la.lookup.row.edge is not None:
            clamped[f"rows:{la.lookup.row.edge}"] += 1
        if la.lookup.column.edge is not None:
            clamped[f"columns:{la.lookup.column.edge}"] += 1
        trims_a.append(la.trim)
        trims_b.append(lb.trim)
    distinguishable = tuple(
        (r, c)
        for r, row in enumerate(a.cells)
        for c, value in enumerate(row)
        if value & TRIM_VALUE_MASK != b.cells[r][c] & TRIM_VALUE_MASK
    )
    return TrimCellCoverage(
        provenance=resolved.provenance(),
        assumptions=tuple(assumptions),
        samples=len(observed),
        cells=dict(sorted(cells.items())),
        distinguishable_cells=distinguishable,
        samples_reading_distinguishable=sum(
            1 for x, y in zip(trims_a, trims_b, strict=True) if x != y
        ),
        clamped=dict(clamped),
        trim_a_intermediate=tuple(trims_a),
        trim_b_intermediate=tuple(trims_b),
    )


__all__ = [
    "MIN_BRANCH_DRIVES",
    "TIMING_LOOKUP_VERSION",
    "BranchEvidence",
    "CoordinateSample",
    "IdenticalSample",
    "IdenticalSampleSummary",
    "MemoryOnlySample",
    "SAME_READ_TOLERANCE_S",
    "ScoredSample",
    "TimingLookupRefusalError",
    "TrimCellCoverage",
    "branch_evidence",
    "coordinate_samples",
    "drives_of",
    "identical_samples",
    "memory_only_samples",
    "refuse_mode01_surface_for_memory_target",
    "require_branch_validation",
    "require_memory_only_target",
    "require_policy_within_skew",
    "require_final_timing_target",
    "score_identical_samples",
    "summarize",
    "trim_cell_coverage",
]
