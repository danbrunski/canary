"""Lambda delivery error decided incrementally, read by read (B06.4).

The offline engine (:func:`src.analysis.lambda_delivery.delivery_samples`)
sees a whole drive at once. A live readout sees reads as they arrive, and has
to give the **same rows**: the same pairing, the same named state, the same
residual. :class:`DeliveryStream` does it by calling the same function,
:func:`~src.analysis.lambda_delivery.judge_target_rows`, on a bounded window
of recent reads, deciding only the rows whose answer can no longer change:

* **Pairing.** A target read at ``t`` pairs (after its regime's transport
  delay ``d``) with a measured read inside ``t + d ± tolerance``. It is
  decided once the stream has seen reads well past ``t + d + tolerance``
  (``horizon``). Rows are decided in shifted-time order, and the greedy
  matcher walks rows in that order, so pairing a later block of rows against
  the measured reads earlier blocks left unused reproduces a whole-drive
  pass exactly.
* **States.** Dwell, target gaps and the post-cut holdoff look only
  backwards. The window keeps ``max(dwell + max gap, holdoff) + margin``
  seconds of already-decided rows before the first undecided one, so every
  backward look sees what a whole-drive pass would.

The clock is the data's own acquisition clock, never the wall clock: a
replay re-stamps its reads to "now" and a round-stamped capture delivers
reads out of acquisition order, and neither may change a verdict. A read
that arrives after the horizon already passed it cannot be folded in
without changing a published row; it is counted in :attr:`late_reads`, so a
readout that disagrees with the offline engine says why.

There is no 1-second grid anywhere here. The live daemon evaluates on its
1 Hz inference tick, but the tick is only when rows are *published*; each row
is one target read on the acquisition clock.
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field

import numpy as np

from src.analysis.lambda_delivery import (
    JudgedRows,
    LambdaDeliveryDeclaration,
    declared_delays_s,
    judge_target_rows,
    measured_side_from_arrays,
    resolve_quantity,
    target_side_from_arrays,
)
from src.signals.afr import AfrCalibration

#: How far past a row's pairing window the data clock must be before the row
#: is decided: reads reach the live sinks close to, but not exactly in,
#: acquisition order (a replay emits in stored-stamp order; a batched Mode 01
#: response stamps its PIDs at one instant). Two seconds is ~8 Mode 01 cycles.
DEFAULT_SETTLE_S = 2.0
#: Extra history kept beyond the longest backward look, so a window boundary
#: never lands exactly on a gap or holdoff edge.
HISTORY_MARGIN_S = 1.0


def _empty() -> np.ndarray:
    return np.array([], dtype=float)


def _empty_flags() -> np.ndarray:
    return np.array([], dtype=bool)


@dataclass
class _Reads:
    """One input stream's retained reads, time-ordered, with a per-read flag.

    ``flag`` is "decided" for target and sentinel rows, "consumed" for
    measured reads.
    """

    times: np.ndarray = field(default_factory=_empty)
    values: np.ndarray = field(default_factory=_empty)
    flag: np.ndarray = field(default_factory=_empty_flags)

    def extend(self, times: np.ndarray, values: np.ndarray) -> None:
        if not len(times):
            return
        merged_times = np.concatenate([self.times, times])
        # Stable, retained reads first: at an equal instant arrival order wins,
        # which is the stored order the offline read sorts by.
        order = np.argsort(merged_times, kind="stable")
        self.times = merged_times[order]
        self.values = np.concatenate([self.values, values])[order]
        self.flag = np.concatenate([self.flag, np.zeros(len(times), dtype=bool)])[order]

    def keep(self, mask: np.ndarray) -> None:
        self.times, self.values, self.flag = self.times[mask], self.values[mask], self.flag[mask]


class DeliveryStream:
    """One (commanded source, measured source) route, decided as reads arrive.

    Feed reads with :meth:`extend` (float seconds on the acquisition clock,
    values in each source's own quantity, exactly as the offline read gives
    them), then call :meth:`advance` with a horizon; it returns the rows that
    horizon decided (:class:`JudgedRows`; ``to_frame()`` gives
    :attr:`DeliverySamples.frame`'s columns).
    :meth:`flush` decides whatever is left, as the end of a drive does.
    """

    def __init__(
        self,
        declaration: LambdaDeliveryDeclaration,
        *,
        target_source: str,
        measured_source: str,
        calibration: AfrCalibration | None,
        afr_stoich: float,
        settle_s: float = DEFAULT_SETTLE_S,
    ) -> None:
        if settle_s < 0:
            raise ValueError("settle_s must not be negative")
        self.declaration = declaration
        self.commanded = declaration.commanded(target_source)
        self.instrument = declaration.measured(measured_source)
        self.calibration = calibration
        self.afr_stoich = afr_stoich
        self.settle_s = settle_s
        self.quantity = resolve_quantity(self.commanded, calibration)
        self.delays_s = declared_delays_s(declaration, self.commanded.name)
        self.tolerance_s = declaration.acquisition.max_separation_s
        elig = declaration.eligibility
        rules = (elig.regimes.closed_loop, elig.regimes.enrichment)
        #: Longest backward look any state takes, plus margin.
        self.history_s = (
            max(
                max(
                    rule.steady_target_dwell_s + elig.max_target_gap_s, rule.post_fuel_cut_holdoff_s
                )
                for rule in rules
            )
            + HISTORY_MARGIN_S
        )
        #: B06.7 — a ``voltage_afr`` route converts each measured read at the
        #: ECM's barometric gain, from the newest baro read at or before it.
        self.needs_baro = (
            self.instrument.transform == "voltage_afr"
            and calibration is not None
            and calibration.baro_gain is not None
        )
        self.baro_max_age_s = (
            calibration.baro_gain.max_age_s
            if calibration is not None and calibration.baro_gain is not None
            else 0.0
        )
        self._max_shift = max(self.delays_s.values())
        self._min_shift = min(self.delays_s.values())
        self.reset()

    def reset(self) -> None:
        """Forget every read (a new drive)."""
        self._target = _Reads()
        self._sentinel = _Reads()
        self._measured = _Reads()
        self._baro = _Reads()
        self.horizon_s = -math.inf
        self.clock_s = -math.inf
        kinds = ("target", "sentinel", "measured", *(("baro",) if self.needs_baro else ()))
        self.late_reads: dict[str, int] = dict.fromkeys(kinds, 0)
        self.reads_seen: dict[str, int] = dict.fromkeys(kinds, 0)

    # -- input -----------------------------------------------------------

    def _note(self, kind: str, times: np.ndarray, late_before: float) -> None:
        self.reads_seen[kind] += len(times)
        if len(times):
            self.late_reads[kind] += int((times <= late_before).sum())
            self.clock_s = max(self.clock_s, float(np.nanmax(times)))

    def extend(
        self,
        *,
        target: tuple[np.ndarray, np.ndarray] | None = None,
        sentinel: np.ndarray | None = None,
        measured: tuple[np.ndarray, np.ndarray] | None = None,
        baro: tuple[np.ndarray, np.ndarray] | None = None,
    ) -> None:
        """Add reads. A read at or before a time an already-decided row
        depended on is *late*: counted, and folded in on a best-effort basis.
        ``baro`` (B06.7) is kept only by a route that converts with it."""
        # A decided row's own time is at most horizon - tolerance - min shift,
        # and it looked back from there; its measured partner lay at most at
        # the horizon.
        decided_before = self.horizon_s - self.tolerance_s - self._min_shift
        if target is not None:
            times, values = (np.asarray(a, dtype=float) for a in target)
            self._note("target", times, decided_before)
            self._target.extend(times, values)
        if sentinel is not None:
            times = np.asarray(sentinel, dtype=float)
            self._note("sentinel", times, decided_before)
            self._sentinel.extend(times, np.full(len(times), np.nan))
        if measured is not None:
            times, values = (np.asarray(a, dtype=float) for a in measured)
            self._note("measured", times, self.horizon_s)
            self._measured.extend(times, values)
        if baro is not None and self.needs_baro:
            times, values = (np.asarray(a, dtype=float) for a in baro)
            self._note("baro", times, self.horizon_s)
            self._baro.extend(times, values)

    # -- decisions ---------------------------------------------------------

    def default_horizon(self) -> float:
        """The data clock less the settle margin."""
        return self.clock_s - self.settle_s

    @property
    def undecided(self) -> int:
        return int((~self._target.flag).sum() + (~self._sentinel.flag).sum())

    def advance(self, horizon_s: float | None = None) -> JudgedRows:
        """Decide every row whose pairing window closes by ``horizon_s``.

        ``None`` uses :meth:`default_horizon`. The horizon never moves back.
        """
        wanted = self.default_horizon() if horizon_s is None else horizon_s
        self.horizon_s = max(self.horizon_s, wanted)
        target, sentinel, measured = self._target, self._sentinel, self._measured
        side = target_side_from_arrays(
            target.times,
            target.values,
            sentinel.times,
            commanded=self.commanded,
            quantity=self.quantity,
            afr_stoich=self.afr_stoich,
        )
        decided = np.concatenate([target.flag, sentinel.flag])[side.origin]
        measured_side = measured_side_from_arrays(
            measured.times,
            measured.values,
            instrument=self.instrument,
            calibration=self.calibration,
            afr_stoich=self.afr_stoich,
            baro_times=self._baro.times if self.needs_baro else None,
            baro_kpa=self._baro.values if self.needs_baro else None,
        )
        judged = judge_target_rows(
            side,
            measured_side,
            commanded=self.commanded,
            declaration=self.declaration,
            quantity=self.quantity,
            delays_s=self.delays_s,
            undecided=~decided,
            measured_free=~measured.flag,
            horizon_s=self.horizon_s,
        )
        n_target = len(target.times)
        origin = side.origin[judged.rows]
        target.flag[origin[origin < n_target]] = True
        sentinel.flag[origin[origin >= n_target] - n_target] = True
        measured.flag[judged.paired[judged.paired >= 0]] = True
        self._prune()
        return judged

    def flush(self) -> JudgedRows:
        """Decide every remaining row with the reads there are (end of drive)."""
        return self.advance(math.inf)

    def _prune(self) -> None:
        if not math.isfinite(self.horizon_s):
            if self.horizon_s == math.inf:
                # Flushed: nothing can be decided again from these reads.
                self._target.keep(~self._target.flag)
                self._sentinel.keep(~self._sentinel.flag)
                self._measured.keep(~self._measured.flag)
                self._baro.keep(self._baro.times >= self._baro.times.max(initial=-math.inf))
            return
        # Undecided rows start after horizon - tolerance - max shift; keep the
        # full backward look before that, and every undecided row regardless.
        rows_from = self.horizon_s - self.tolerance_s - self._max_shift - self.history_s
        for reads in (self._target, self._sentinel):
            reads.keep((reads.times >= rows_from) | ~reads.flag)
        # An undecided row's partner lies after horizon - 2 x tolerance.
        measured_from = self.horizon_s - 2.0 * self.tolerance_s - HISTORY_MARGIN_S
        self._measured.keep(self._measured.times >= measured_from)
        # A retained measured read looks back at most the baro max age; keep
        # those baro reads and the newest one before them.
        if len(self._baro.times):
            older = self._baro.times < measured_from - self.baro_max_age_s
            if older.any():
                older[np.flatnonzero(older)[-1]] = False
            self._baro.keep(~older)


__all__ = ["DEFAULT_SETTLE_S", "HISTORY_MARGIN_S", "DeliveryStream"]
