"""The October 4 Fuel Comp restore's multiplier changes, reproduced (B08.6, desk).

The prediction of record (``data/analyses/2026-10-04-fuel-comp-restore-
prediction.md``, preserved verbatim in ``plant_interventions.yaml``,
:class:`~src.analysis.plant_validation.PredictedBand`) states, per band of the
Fuel Comp load byte ``0xFFFF118A`` and rpm, how much the restored image
changes the Fuel Comp multiplier. This module recomputes that change from the
two images' own bytes at input points of a recorded pre-flash drive, and says
band by band whether the written figures are reproduced. It predicts no
physical outcome and issues nothing to the ECU: whether trims or lambda
actually moved is the later-drive half of B08.6
(:func:`src.analysis.plant_validation.evaluate_intervention`).

What it enforces, because each was learned rather than assumed:

- **Input points, never aggregates.** One point is one read of the load byte
  (``observed``), or one N02 sample's reconstructed byte interval
  (``reconstructed:<route>``, :func:`points_from_samples`). The rpm word is
  never paired by nearest neighbour: on the 2026-10-04 ram-study capture the
  byte is read in its own memory window, 0.25-2.3 s from any PID 0x0C read
  (0 of 192 within 0.1 s). An observed point therefore carries the rpm *words
  of the reads bracketing it*, one at or before and one at or after, each
  within a caller-stated gap; anything else is unplaced under its reason. That
  the rpm lies between its bracketing reads is an assumption (no read sees an
  overshoot between them), stated here and in the report.
- **Both images, both axes.** Each image's multiplier is read by the
  firmware lookup (:func:`src.decode.ecm_lookup.lookup_2d`) on that image's
  own rpm and load axes. The restore changed the load axis (16..144 to
  16..255) and the cells, so reading new cells on the old axis is a different
  (wrong) answer, and the separate effects of axis and cells are not isolated.
- **An interval is bounded exactly.** Between neighbouring breakpoints each
  table is bilinear, so the ratio of the two is monotone along each axis; its
  extremes over a point's (rpm word x byte) rectangle lie on the corners of
  the sub-grid cut by both images' breakpoints, which are all evaluated. The
  truncating Q16 interpolation can move an interior value by one count.
- **The same input on both images.** The byte's own chain (the load-byte
  curves), K and the map slot must be equal on both images, or the observed
  byte is not the same operating point under the restored image and the
  multiplier ratio is not the fuel change (``load_input_chain_changed``,
  ``k_changed``, ``slot_mismatch``).
- **Only pre-flash points.** A point at or after the flash, or before the
  base revision was flashed, is unplaced (``after_flash``,
  ``before_base_revision``): the images must be the ones that drive ran.
- **The tolerance is the written precision, not a choice.** Each endpoint of a
  band's written change is allowed half a unit in its last written digit, read
  from the row as written ("about -1 %" -> +-0.5; "-0.2" -> +-0.05).
- **Each kind of empty is named.** A band with no point is ``no_points``
  (insufficient evidence), distinct from ``contradicted``; a placed point
  outside every predicted row is ``no_predicted_row``; one whose interval
  spans a band edge is ``straddles_band_edge``.
"""

from __future__ import annotations

import math
import re
from collections import Counter
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from datetime import UTC
from typing import Any, Literal

import numpy as np
import pandas as pd

from src.analysis.channels import TIMESTAMP_BASIS_COLUMN
from src.analysis.fuel_comp_inputs import FuelCompDeclaration, FuelCompInputError, rpm_word
from src.analysis.fuel_comp_rom import FuelCompCalibration
from src.analysis.plant_validation import Boundary, InterventionCase, PredictedBand
from src.decode.ecm_lookup import lookup_2d
from src.signals.pairing import to_seconds

#: Bumped whenever a point's placement, a reason or the report changes meaning.
REPRODUCTION_VERSION = 1

#: Fuel Comp cells are multipliers with 128 = 1.000.
UNITY = 128.0

ACQUIRED = "acquired"

Verdict = Literal["reproduced", "contradicted", "unresolved", "no_points"]


class RestoreReproductionRefusalError(ValueError):
    """The reproduction cannot be computed; ``reason`` names why."""

    def __init__(self, reason: str, message: str) -> None:
        super().__init__(f"{reason}: {message}")
        self.reason = reason
        self.message = message


# -- input points ---------------------------------------------------------------------


@dataclass(frozen=True)
class InputPoint:
    """One operating point: a load-byte interval and an rpm-word interval.

    ``reason`` is ``None`` for a placed point; otherwise the intervals may be
    absent and the reason names what is missing."""

    session: str
    route: str
    t: float
    byte_lo: int | None
    byte_hi: int | None
    word_lo: int | None
    word_hi: int | None
    reason: str | None
    #: Acquisition times of the rpm reads the word interval came from.
    rpm_read_times: tuple[float, ...] = ()

    def as_dict(self) -> dict[str, Any]:
        return {
            "session": self.session,
            "route": self.route,
            "t": self.t,
            "byte": [self.byte_lo, self.byte_hi],
            "rpm_word": [self.word_lo, self.word_hi],
            "reason": self.reason,
            "rpm_read_times": list(self.rpm_read_times),
        }


def _acquired(reads: pd.DataFrame) -> pd.Series:
    if TIMESTAMP_BASIS_COLUMN not in reads.columns:
        return pd.Series(False, index=reads.index)
    return reads[TIMESTAMP_BASIS_COLUMN].astype(str) == ACQUIRED


def _bracket(
    times: np.ndarray, t: float, max_gap_s: float
) -> tuple[int | None, int | None, str | None]:
    """Indices of the rpm reads at or before and at or after ``t``."""
    before = int(np.searchsorted(times, t, side="right")) - 1
    after = int(np.searchsorted(times, t, side="left"))
    if before < 0 or t - times[before] > max_gap_s:
        return None, None, "unbracketed:before"
    if after >= len(times) or times[after] - t > max_gap_s:
        return None, None, "unbracketed:after"
    return before, after, None


def observed_points(
    long: pd.DataFrame,
    *,
    session: str,
    byte_spec: str,
    declaration: FuelCompDeclaration,
    max_gap_s: float,
) -> list[InputPoint]:
    """Every read of ``byte_spec`` with the rpm words of its bracketing reads.

    ``long`` is :func:`~src.analysis.channels.read_channels` output with
    ``acquired_time=True``; the rpm channel is the N02 declaration's (PID
    0x0C, whose value is the engine-speed word the Fuel Comp wrapper
    searches). ``max_gap_s`` is caller-stated: no default.
    """
    if not max_gap_s > 0:
        raise ValueError("max_gap_s must be positive")
    spec_of = long["spec"].astype(str)
    reads = long[spec_of == byte_spec]
    if reads.empty:
        raise RestoreReproductionRefusalError(
            f"not_captured:{byte_spec}",
            f"{session} never read {byte_spec}: there is no observed input point",
        )
    rpm_spec = declaration.inputs.rpm.channel
    rpm_all = long[spec_of == rpm_spec]
    rpm = rpm_all[_acquired(rpm_all)]
    rpm_missing = (
        None
        if not rpm.empty
        else (f"not_captured:{rpm_spec}" if rpm_all.empty else f"clock_unacquired:{rpm_spec}")
    )
    rpm_t = to_seconds(rpm["timestamp"]) if not rpm.empty else np.array([], dtype=float)
    order = np.argsort(rpm_t, kind="stable")
    rpm_t = rpm_t[order]
    rpm_v = pd.to_numeric(rpm["value"], errors="coerce").to_numpy(dtype=float)[order]
    byte_t = to_seconds(reads["timestamp"])
    byte_v = pd.to_numeric(reads["value"], errors="coerce").to_numpy(dtype=float)
    byte_ok = _acquired(reads).to_numpy()
    out: list[InputPoint] = []
    for t, value, acquired in sorted(zip(byte_t.tolist(), byte_v.tolist(), byte_ok, strict=True)):
        reason: str | None = None
        byte: int | None = None
        if not acquired:
            reason = f"clock_unacquired:{byte_spec}"
        elif not math.isfinite(value):
            reason = f"missing:{byte_spec}"
        elif value != int(value) or not 0 <= value <= 0xFF:
            reason = f"not_a_byte:{byte_spec}"
        else:
            byte = int(value)
        if reason is None and rpm_missing is not None:
            reason = rpm_missing
        words: tuple[int, int] | None = None
        read_times: tuple[float, ...] = ()
        if reason is None:
            i, j, reason = _bracket(rpm_t, t, max_gap_s)
            if i is not None and j is not None:
                read_times = (float(rpm_t[i]), float(rpm_t[j]))
                try:
                    pair = (rpm_word(rpm_v[i], declaration), rpm_word(rpm_v[j], declaration))
                    words = (min(pair), max(pair))
                except FuelCompInputError:
                    reason = f"rpm_not_a_word:{rpm_spec}"
        out.append(
            InputPoint(
                session=session,
                route="observed",
                t=float(t),
                byte_lo=byte,
                byte_hi=byte,
                word_lo=words[0] if words else None,
                word_hi=words[1] if words else None,
                reason=reason,
                rpm_read_times=read_times,
            )
        )
    return out


def points_from_samples(frame: pd.DataFrame) -> list[InputPoint]:
    """N02 samples (:func:`src.analysis.fuel_comp_samples.session_samples`) as
    reconstructed points: the byte interval its chain gives on the sample's
    revision and the rpm-word interval it was placed with. A sample without
    both intervals is unplaced under its own state and reason."""
    out: list[InputPoint] = []
    times = to_seconds(frame["timestamp"]) if len(frame) else np.array([], dtype=float)
    for t, row in zip(times.tolist(), frame.to_dict("records"), strict=True):
        values = [row[c] for c in ("byte_lo", "byte_hi", "rpm_word_lo", "rpm_word_hi")]
        finite = all(v is not None and pd.notna(v) for v in values)
        why = row.get("reason")
        reason = (
            None
            if finite
            else f"{row['state']}:{why if isinstance(why, str) and why else 'no_interval'}"
        )
        ints: list[int | None] = [int(v) for v in values] if finite else [None] * 4
        out.append(
            InputPoint(
                session=str(row["session"]),
                route=f"reconstructed:{row['route']}",
                t=float(t),
                byte_lo=ints[0],
                byte_hi=ints[1],
                word_lo=ints[2],
                word_hi=ints[3],
                reason=reason,
            )
        )
    return out


# -- the multiplier change at a point --------------------------------------------------


def check_comparable(old: FuelCompCalibration, new: FuelCompCalibration) -> None:
    """Refuse two images on which the same logged input is not the same point."""
    if old.slot != new.slot:
        raise RestoreReproductionRefusalError(
            "slot_mismatch", f"{old.name} slot {old.slot}, {new.name} slot {new.slot}"
        )
    if old.curves != new.curves:
        raise RestoreReproductionRefusalError(
            "load_input_chain_changed",
            f"the load byte's chain differs between {old.name} and {new.name}: a byte "
            "logged on one is not the same operating point on the other",
        )
    if old.k != new.k:
        raise RestoreReproductionRefusalError(
            "k_changed",
            f"K {old.k} on {old.name}, {new.k} on {new.name}: the Fuel Comp ratio alone "
            "is not the fuel change",
        )


@dataclass(frozen=True)
class PointChange:
    """The multiplier on each image over one point's rectangle (raw, 128 = 1)."""

    point: InputPoint
    fc_old: tuple[int, int]
    fc_new: tuple[int, int]
    #: ``100 x (new / old - 1)`` over the rectangle, ``[min, max]``.
    change_pct: tuple[float, float]
    #: The byte reaches past the image's last load breakpoint (the column clamps).
    old_load_clamped: bool
    new_load_clamped: bool
    #: Corners evaluated.
    evaluations: int

    def as_dict(self) -> dict[str, Any]:
        return {
            **self.point.as_dict(),
            "fc_old": list(self.fc_old),
            "fc_new": list(self.fc_new),
            "multiplier_old": [v / UNITY for v in self.fc_old],
            "multiplier_new": [v / UNITY for v in self.fc_new],
            "change_pct": list(self.change_pct),
            "old_load_clamped": self.old_load_clamped,
            "new_load_clamped": self.new_load_clamped,
            "evaluations": self.evaluations,
        }


def _grid(lo: int, hi: int, *axes: Sequence[int]) -> list[int]:
    inside = {int(b) for axis in axes for b in axis if lo < b < hi}
    return sorted({lo, hi} | inside)


def multiplier_change(
    old: FuelCompCalibration, new: FuelCompCalibration, point: InputPoint
) -> PointChange:
    """Both images' multipliers over ``point``'s rectangle, each on its own axes."""
    if point.reason is not None or None in (
        point.byte_lo,
        point.byte_hi,
        point.word_lo,
        point.word_hi,
    ):
        raise ValueError(f"point at {point.t} is unplaced ({point.reason})")
    assert point.byte_lo is not None and point.byte_hi is not None
    assert point.word_lo is not None and point.word_hi is not None
    bytes_ = _grid(point.byte_lo, point.byte_hi, old.load_axis_raw, new.load_axis_raw)
    words = _grid(point.word_lo, point.word_hi, old.rpm_breakpoints, new.rpm_breakpoints)
    olds: list[int] = []
    news: list[int] = []
    ratios: list[float] = []
    for word in words:
        for byte in bytes_:
            fo = lookup_2d(old.fuel_comp, old.rpm_breakpoints, old.load_axis_raw, word, byte).value
            fn = lookup_2d(new.fuel_comp, new.rpm_breakpoints, new.load_axis_raw, word, byte).value
            if fo <= 0:
                raise RestoreReproductionRefusalError(
                    "zero_multiplier", f"{old.name} reads {fo} at word {word}, byte {byte}"
                )
            olds.append(fo)
            news.append(fn)
            ratios.append(100.0 * (fn / fo - 1.0))
    return PointChange(
        point=point,
        fc_old=(min(olds), max(olds)),
        fc_new=(min(news), max(news)),
        change_pct=(min(ratios), max(ratios)),
        old_load_clamped=point.byte_hi > old.load_axis_raw[-1],
        new_load_clamped=point.byte_hi > new.load_axis_raw[-1],
        evaluations=len(ratios),
    )


# -- bands as written ------------------------------------------------------------------

_NUMBER = re.compile(r"\d+(?:\.\d+)?")


def written_tolerance(band: PredictedBand) -> tuple[float, float]:
    """Half a unit in the last digit each endpoint of the change is written with.

    Read from ``as_written``: the first number there whose magnitude equals
    the endpoint gives its precision. An endpoint the row does not write is
    refused (``precision_unstated``): the tolerance is never chosen."""
    tokens = _NUMBER.findall(band.as_written)
    out: list[float] = []
    for value in band.fuel_change_pct:
        token = next((t for t in tokens if math.isclose(float(t), abs(value))), None)
        if token is None:
            raise RestoreReproductionRefusalError(
                "precision_unstated",
                f"{value} is not written in {band.as_written!r}: its precision is unknown",
            )
        decimals = len(token.split(".")[1]) if "." in token else 0
        out.append(0.5 * 10.0**-decimals)
    return out[0], out[1]


def classify(
    point: InputPoint, bands: Sequence[PredictedBand], rpm_per_count: float
) -> tuple[int | None, str | None]:
    """``(band index, None)``, or ``(None, reason)`` for a placed point no
    predicted row holds whole."""
    assert point.byte_lo is not None and point.byte_hi is not None
    assert point.word_lo is not None and point.word_hi is not None
    rpm_lo, rpm_hi = point.word_lo * rpm_per_count, point.word_hi * rpm_per_count
    straddles = False
    for index, band in enumerate(bands):
        lo, hi = band.load_input
        if point.byte_hi < lo or point.byte_lo >= hi:
            continue
        if not (lo <= point.byte_lo and point.byte_hi < hi):
            straddles = True
            continue
        if band.rpm is None:
            return index, None
        r0, r1 = band.rpm
        if r0 <= rpm_lo and rpm_hi <= r1:
            return index, None
        if rpm_hi >= r0 and rpm_lo <= r1:
            straddles = True
    return None, ("straddles_band_edge" if straddles else "no_predicted_row")


@dataclass(frozen=True)
class BandResult:
    """One written row against the changes reproduced inside it."""

    index: int
    band: PredictedBand
    tolerance_pct: tuple[float, float]
    changes: tuple[PointChange, ...]
    within: int
    outside: int
    overlapping: int
    verdict: Verdict

    def window(self) -> tuple[float, float]:
        lo, hi = self.band.fuel_change_pct
        return lo - self.tolerance_pct[0], hi + self.tolerance_pct[1]

    def as_dict(self) -> dict[str, Any]:
        changes = self.changes
        return {
            "index": self.index,
            "as_written": self.band.as_written,
            "load_input": list(self.band.load_input),
            "rpm": None if self.band.rpm is None else list(self.band.rpm),
            "predicted_change_pct": list(self.band.fuel_change_pct),
            "tolerance_pct": list(self.tolerance_pct),
            "window_pct": list(self.window()),
            "n_points": len(changes),
            "change_pct_range": (
                [min(c.change_pct[0] for c in changes), max(c.change_pct[1] for c in changes)]
                if changes
                else None
            ),
            "multiplier_old_range": (
                [
                    min(c.fc_old[0] for c in changes) / UNITY,
                    max(c.fc_old[1] for c in changes) / UNITY,
                ]
                if changes
                else None
            ),
            "multiplier_new_range": (
                [
                    min(c.fc_new[0] for c in changes) / UNITY,
                    max(c.fc_new[1] for c in changes) / UNITY,
                ]
                if changes
                else None
            ),
            "within": self.within,
            "outside": self.outside,
            "overlapping": self.overlapping,
            "verdict": self.verdict,
        }


def _band_result(index: int, band: PredictedBand, changes: Sequence[PointChange]) -> BandResult:
    tolerance = written_tolerance(band)
    lo, hi = band.fuel_change_pct[0] - tolerance[0], band.fuel_change_pct[1] + tolerance[1]
    within = sum(1 for c in changes if lo <= c.change_pct[0] and c.change_pct[1] <= hi)
    outside = sum(1 for c in changes if c.change_pct[1] < lo or c.change_pct[0] > hi)
    overlapping = len(changes) - within - outside
    verdict: Verdict
    if not changes:
        verdict = "no_points"
    elif outside:
        verdict = "contradicted"
    elif within == len(changes):
        verdict = "reproduced"
    else:
        verdict = "unresolved"
    return BandResult(index, band, tolerance, tuple(changes), within, outside, overlapping, verdict)


# -- the reproduction ------------------------------------------------------------------


@dataclass(frozen=True)
class Reproduction:
    """Every point accounted for, and each written row's verdict."""

    case: str
    old: Mapping[str, Any]
    new: Mapping[str, Any]
    bands: tuple[BandResult, ...]
    #: Placed points no row holds whole, with their changes.
    no_claim: tuple[tuple[str, PointChange], ...]
    #: Reason -> count, for points that could not be placed.
    unplaced: Mapping[str, int]
    #: Route -> point count, placed or not.
    routes: Mapping[str, int]
    #: Per band: observed byte reads inside its load range, whatever the rpm,
    #: over every observed byte read (the row's "% of samples").
    load_share: tuple[tuple[int, int], ...]
    assumptions: tuple[str, ...] = field(default=())

    def as_dict(self) -> dict[str, Any]:
        no_claim = Counter(reason for reason, _ in self.no_claim)
        nonzero = sum(1 for _, c in self.no_claim if c.change_pct != (0.0, 0.0))
        return {
            "version": REPRODUCTION_VERSION,
            "case": self.case,
            "old": dict(self.old),
            "new": dict(self.new),
            "routes": dict(self.routes),
            "unplaced": dict(self.unplaced),
            "bands": [b.as_dict() for b in self.bands],
            "load_share": [
                {"band": i, "reads": n, "of": total, "share": (n / total if total else None)}
                for i, (n, total) in enumerate(self.load_share)
            ],
            "no_claim": {
                "by_reason": dict(no_claim),
                "changed": nonzero,
                "points": [{"reason": r, **c.as_dict()} for r, c in self.no_claim],
            },
            "points": [{"band": b.index, **c.as_dict()} for b in self.bands for c in b.changes],
            "assumptions": list(self.assumptions),
        }


OBSERVED_ASSUMPTION = (
    "observed points: the rpm word at the byte read lies between the words of the "
    "bracketing PID 0x0C reads (no read sees an overshoot between them)"
)


def reproduce(
    case: InterventionCase,
    boundary: Boundary,
    old: FuelCompCalibration,
    new: FuelCompCalibration,
    points: Sequence[InputPoint],
    *,
    rpm_per_count: float,
) -> Reproduction:
    """The case's written bands against both images' multipliers at ``points``.

    ``boundary`` is :func:`~src.analysis.plant_validation.resolve_boundary`'s
    for ``case`` (the registry's flash and images); ``old`` and ``new`` must
    be the case's base and candidate images by plain sha256."""
    for label, calibration, expected in (
        ("base", old, case.base_image_sha256),
        ("candidate", new, case.candidate_image_sha256),
    ):
        if calibration.image_sha256 != expected:
            raise RestoreReproductionRefusalError(
                f"image_mismatch:{label}",
                f"{calibration.name} is {calibration.image_sha256[:12]}..., the case's "
                f"{label} image is {expected[:12]}...",
            )
    if (boundary.base.revision, boundary.candidate.revision) != (
        case.base_revision,
        case.candidate_revision,
    ):
        raise RestoreReproductionRefusalError(
            "boundary_mismatch", "the boundary was resolved for another case"
        )
    check_comparable(old, new)
    flash = boundary.at.timestamp()
    base_flashed = boundary.base.flashed_at
    base_from = (
        base_flashed if base_flashed.tzinfo else base_flashed.replace(tzinfo=UTC)
    ).timestamp()
    bands = case.prediction_of_record.bands
    in_band: list[list[PointChange]] = [[] for _ in bands]
    no_claim: list[tuple[str, PointChange]] = []
    unplaced: Counter[str] = Counter()
    routes: Counter[str] = Counter()
    observed_bytes: list[int] = []
    for point in points:
        routes[point.route] += 1
        if point.route == "observed" and point.byte_lo is not None:
            observed_bytes.append(point.byte_lo)
        reason = point.reason
        if reason is None and point.t >= flash:
            reason = "after_flash"
        if reason is None and point.t < base_from:
            reason = "before_base_revision"
        if reason is not None:
            unplaced[reason] += 1
            continue
        change = multiplier_change(old, new, point)
        index, why = classify(point, bands, rpm_per_count)
        if index is None:
            assert why is not None
            no_claim.append((why, change))
        else:
            in_band[index].append(change)
    load_share = tuple(
        (
            sum(1 for b in observed_bytes if band.load_input[0] <= b < band.load_input[1]),
            len(observed_bytes),
        )
        for band in bands
    )
    return Reproduction(
        case=case.id,
        old=old.provenance(),
        new=new.provenance(),
        bands=tuple(_band_result(i, b, in_band[i]) for i, b in enumerate(bands)),
        no_claim=tuple(no_claim),
        unplaced=dict(unplaced),
        routes=dict(routes),
        load_share=load_share,
        assumptions=(OBSERVED_ASSUMPTION,) if routes.get("observed") else (),
    )


__all__ = [
    "REPRODUCTION_VERSION",
    "BandResult",
    "InputPoint",
    "PointChange",
    "Reproduction",
    "RestoreReproductionRefusalError",
    "check_comparable",
    "classify",
    "multiplier_change",
    "observed_points",
    "points_from_samples",
    "reproduce",
    "written_tolerance",
]
