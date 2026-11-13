"""Known-answer distribution shift for the detector evaluation (A04.1).

Shift one quantity by k standard deviations of its operating mode and count
what each detector flags (:func:`single_quantity_shift`), with a verdict that
says whether the case was valid (:class:`ShiftReport`). Split out of
:mod:`src.models.evaluation` (R27.7), which re-exports every name here.
"""

from __future__ import annotations

from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from typing import Any, Literal

import numpy as np
import pandas as pd

from src.models.evaluation_alerts import _verdict

# ---------------------------------------------------------------------------
# Known answer: single-quantity shift (A04.1)
# ---------------------------------------------------------------------------

#: Shift sizes, in standard deviations of the quantity within the row's
#: operating mode. The 2026-09-22 review's sweep.
SHIFT_SIGMAS: tuple[float, ...] = (3.0, 10.0, 30.0, 100.0)
#: A detector passes when a shift of this size in *any single* quantity flags
#: at least :data:`SHIFT_PASS_FLAG_RATE` of the rows it scored as quiet.
SHIFT_PASS_SIGMA = 10.0
SHIFT_PASS_FLAG_RATE = 0.5
#: The control: every numeric column moved together by the largest shift
#: size must flag at least :data:`SHIFT_CONTROL_MIN_FLAG_RATE`, or the case is
#: invalid for that detector (the harness, not the detector, is then in
#: question). The review's 3 sigma all-feature control suits the IF, whose
#: score sums over every feature; a max-|z| detector thresholded at 6 sigma
#: legitimately ignores 3 sigma everywhere, so the control uses the largest size.
SHIFT_CONTROL_MIN_FLAG_RATE = 0.9
SHIFT_CONTROL = "_control_all_features"
_SHIFTED_SUFFIXES = ("", "__mean")


def shift_columns(frame: pd.DataFrame, channels: Sequence[str]) -> list[str]:
    """The columns a sustained offset in ``channels`` moves: each raw channel
    and its rolling mean. ``__std``/``__roc`` do not move under a constant
    offset, so they are left alone."""
    return [
        f"{channel}{suffix}"
        for channel in channels
        for suffix in _SHIFTED_SUFFIXES
        if f"{channel}{suffix}" in frame.columns
        and pd.api.types.is_numeric_dtype(frame[f"{channel}{suffix}"])
        and not pd.api.types.is_bool_dtype(frame[f"{channel}{suffix}"])
    ]


def mode_spreads(frame: pd.DataFrame, columns: Sequence[str], mode_column: str) -> pd.DataFrame:
    """Per row, each column's standard deviation within the row's operating
    mode on ``frame`` (the pooled one where a mode has no spread)."""
    values = frame[list(columns)].apply(pd.to_numeric, errors="coerce")
    pooled = values.std()
    if mode_column in frame.columns:
        per_mode = values.groupby(frame[mode_column].astype(str)).transform("std")
        spreads = per_mode.where(per_mode > 0).fillna(pooled)
    else:
        spreads = pd.DataFrame(
            np.broadcast_to(pooled.to_numpy(), values.shape),
            index=frame.index,
            columns=list(columns),
        )
    out: pd.DataFrame = spreads.where(spreads > 0)
    return out


@dataclass(frozen=True)
class ShiftOutcome:
    """One (quantity, size) cell for one detector.

    ``rows`` are the rows the detector scored *quiet* on the clean slice
    where the quantity was freshly observed (every row for the control);
    ``scored`` of them still had a verdict after the shift, and ``flagged``
    of those were flagged. ``flag_rate`` is ``None`` when nothing was scored
    (withheld, never a zero).
    """

    quantity: str
    sigma: float
    columns: tuple[str, ...]
    rows: int
    scored: int
    flagged: int

    @property
    def flag_rate(self) -> float | None:
        return None if self.scored == 0 else self.flagged / self.scored

    def to_dict(self) -> dict[str, Any]:
        return {
            "quantity": self.quantity,
            "sigma": self.sigma,
            "columns": list(self.columns),
            "rows": self.rows,
            "scored": self.scored,
            "flagged": self.flagged,
            "flag_rate": self.flag_rate,
        }


@dataclass(frozen=True)
class ShiftReport:
    """A detector's single-quantity-shift known answer on one slice."""

    outcomes: tuple[ShiftOutcome, ...]
    control: ShiftOutcome | None
    verdict: Literal["pass", "fail", "invalid", "withheld"]
    note: str

    def to_dict(self) -> dict[str, Any]:
        return {
            "verdict": self.verdict,
            "note": self.note,
            "control": None if self.control is None else self.control.to_dict(),
            "outcomes": [o.to_dict() for o in self.outcomes],
        }


VerdictScorer = Callable[[pd.DataFrame], Mapping[str, Sequence[Any]]]


def single_quantity_shift(
    score: VerdictScorer,
    frame: pd.DataFrame,
    quantities: Mapping[str, Sequence[str]],
    *,
    mode_column: str,
    sigmas: Sequence[float] = SHIFT_SIGMAS,
) -> dict[str, ShiftReport]:
    """The A04.1 known answer, for every detector ``score`` returns.

    Shift one quantity (every column that carries it) by k standard deviations
    of its operating mode, on rows each detector scored quiet, and count what
    it flags. The deployed IF flagged 0 % at every size up to 100 sigma — a
    value past the training range follows the same branch as the training
    maximum, and one of ~250 features lies on ~3 % of a path — while shifting
    every feature by 3 sigma flagged 100 %. A detector that cannot see a
    single-quantity fault of any size is recorded as failing, not averaged
    away. Row-level verdicts (no persistence gate): this measures whether the
    score moves at all.
    """
    clean = {name: list(v) for name, v in score(frame).items()}
    every = sorted({c for channels in quantities.values() for c in shift_columns(frame, channels)})
    all_numeric = [
        str(c)
        for c in frame.columns
        if pd.api.types.is_numeric_dtype(frame[c])
        and not pd.api.types.is_bool_dtype(frame[c])
        and not str(c).endswith(("__gap", "__arrived"))
        and str(c) not in (mode_column,)
    ]
    spreads = mode_spreads(frame, all_numeric, mode_column)

    def shifted(columns: Sequence[str], sigma: float) -> pd.DataFrame:
        out = frame.copy()
        for column in columns:
            delta = (sigma * spreads[column]).fillna(0.0)
            out[column] = pd.to_numeric(out[column], errors="coerce") + delta
        return out

    def observed_rows(channels: Sequence[str]) -> np.ndarray[Any, np.dtype[np.bool_]]:
        """Rows where some channel of the quantity carries a fresh sample
        (its ``__gap`` flag clear; no flag = always fresh). A fault in a
        quantity polled every 5 s can only show where it was observed: a
        detector reading arrived samples must not be graded on the rows
        between them."""
        seen = np.zeros(len(frame), dtype=bool)
        for channel in channels:
            gap = f"{channel}__gap"
            if channel not in frame.columns:
                continue
            if gap not in frame.columns:
                return np.ones(len(frame), dtype=bool)
            seen |= ~frame[gap].fillna(True).astype(bool).to_numpy()
        return seen

    def outcome(
        name: str,
        quantity: str,
        sigma: float,
        columns: Sequence[str],
        verdicts: Any,
        rows: np.ndarray[Any, np.dtype[np.bool_]] | None = None,
    ) -> ShiftOutcome:
        quiet = [
            i
            for i, v in enumerate(clean[name])
            if _verdict(v) is False and (rows is None or bool(rows[i]))
        ]
        after = [_verdict(verdicts[i]) for i in quiet]
        scored = [v for v in after if v is not None]
        return ShiftOutcome(
            quantity, float(sigma), tuple(columns), len(quiet), len(scored), sum(scored)
        )

    cells: dict[str, list[ShiftOutcome]] = {name: [] for name in clean}
    for quantity, channels in quantities.items():
        columns = shift_columns(frame, channels)
        if not columns:
            continue
        seen = observed_rows(channels)
        for sigma in sigmas:
            result = score(shifted(columns, sigma))
            for name in clean:
                if name in result:
                    cells[name].append(
                        outcome(name, quantity, sigma, columns, list(result[name]), seen)
                    )
    control_columns = all_numeric if all_numeric else every
    control_sigma = float(max(sigmas))
    control_result = score(shifted(control_columns, control_sigma))
    reports: dict[str, ShiftReport] = {}
    for name in clean:
        control = (
            outcome(
                name,
                SHIFT_CONTROL,
                control_sigma,
                control_columns,
                list(control_result[name]),
            )
            if name in control_result
            else None
        )
        reports[name] = _shift_verdict(tuple(cells[name]), control)
    return reports


def _shift_verdict(outcomes: tuple[ShiftOutcome, ...], control: ShiftOutcome | None) -> ShiftReport:
    if control is None or control.rows == 0 or control.flag_rate is None:
        return ShiftReport(
            outcomes, control, "withheld", "withheld: no row the detector scored quiet"
        )
    if control.flag_rate < SHIFT_CONTROL_MIN_FLAG_RATE:
        return ShiftReport(
            outcomes,
            control,
            "invalid",
            f"control flagged {control.flag_rate:.0%} < {SHIFT_CONTROL_MIN_FLAG_RATE:.0%}: "
            "the shift did not reach this detector's inputs",
        )
    at_pass = [o for o in outcomes if o.sigma == SHIFT_PASS_SIGMA]
    blind = sorted(
        o.quantity
        for o in at_pass
        if o.flag_rate is not None and o.flag_rate < SHIFT_PASS_FLAG_RATE
    )
    if not at_pass or all(o.flag_rate is None for o in at_pass):
        return ShiftReport(outcomes, control, "withheld", "withheld: no shifted quantity scored")
    if blind:
        return ShiftReport(
            outcomes,
            control,
            "fail",
            f"a +{SHIFT_PASS_SIGMA:g} sigma shift flagged under {SHIFT_PASS_FLAG_RATE:.0%} "
            f"of rows for: {', '.join(blind)}",
        )
    return ShiftReport(outcomes, control, "pass", "every quantity visible at +10 sigma")
