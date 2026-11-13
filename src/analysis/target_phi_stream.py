"""The target-phi twin decided incrementally, read by read (E04.1/E04.2).

:func:`~src.analysis.target_phi.evaluate_session` sees a whole session at
once. A live twin sees reads as they arrive and must give the **same
records**: the same tuples, gates, states, predictions and differences, and
the same counts. :class:`TwinStream` does it the way
:class:`~src.analysis.lambda_delivery_stream.DeliveryStream` does for the
delivery readout -- by calling the stored evaluator's own per-candidate
judgement (``_assess``, which calls :func:`~src.analysis.target_phi.predict`
and the shared lookup kernel) over a bounded window of retained reads, and
deciding only the command samples whose answer can no longer change:

* **Decision horizon.** A sample at ``t`` depends on reads up to
  :func:`~src.analysis.target_phi.decision_horizon_s` after it (the
  after-side identity, mode and coolant reads, the inputs' skew). It is
  decided once every stream has been read that far past it, plus a settle
  margin for reads that reach the sink slightly out of acquisition order.
* **Pairing.** Inputs pair one-to-one with samples by
  :func:`~src.signals.pairing.match_ordered`, greedily in sample order.
  Samples are decided in time order and each batch is paired against the
  reads earlier batches left unused, which is exactly where a whole-session
  pass would be after the same samples.
* **Lookback.** Gates and identity look back at most
  :func:`~src.analysis.target_phi.lookback_s`; the window keeps that much
  (plus a margin) before the first undecided sample, so every backward look
  sees what a whole-session pass would.

The clock is the data's acquisition clock, never the wall clock and never a
1-second tick: a record is one command sample. A read stamped at or before a
time a decided record already depended on is **late**: it is counted and
dropped, so the stream's records are exactly the stored evaluator's records
over the reads it accepted -- never silently different. Nothing is carried
forward across a gap; a gap is a named reason on the samples inside it.

Every retained structure is bounded by the data clock (reads older than the
lookback before the decided frontier are evicted, and a hard per-channel cap
counts what it drops). The records themselves are not retained: they fold
into :class:`~src.analysis.target_phi.Accounting` and a short recent ring.
"""

from __future__ import annotations

import math
from collections import deque
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import numpy as np
import pandas as pd

from src.analysis.lambda_delivery_stream import DEFAULT_SETTLE_S, HISTORY_MARGIN_S
from src.analysis.rom_trace import raw_counts, stored_decode
from src.analysis.target_phi import (
    Accounting,
    Qualification,
    ResolvedTable,
    TwinDefinition,
    TwinRefusalError,
    _assess,
    _Series,
    capture_requirement,
    check_windows,
    decision_horizon_s,
    evaluated_specs,
    lookback_s,
    new_candidate,
    pairing_block,
    raw_specs,
)
from src.decode.dids import DidMap
from src.paths import DIDS_YAML
from src.signals.pairing import PairingPolicy, match_ordered
from src.timefmt import format_epoch

#: Recent decided records kept for publication (the live trace).
RECENT_RECORDS = 120
#: Hard cap on retained reads per channel. The data-clock window keeps far
#: fewer at any real poll rate; reaching the cap is counted, never silent.
MAX_READS_PER_SPEC = 20_000


def _empty() -> np.ndarray:
    return np.array([], dtype=float)


@dataclass
class _Reads:
    """One channel's retained reads, in acquisition order.

    ``flag`` is "decided" for command samples, "consumed" for lookup inputs.
    ``evicted`` counts reads dropped from the front, so ``evicted + i`` is a
    retained read's row in the channel's whole accepted series -- the row a
    stored evaluation would cite."""

    times: np.ndarray = field(default_factory=_empty)
    values: np.ndarray = field(default_factory=_empty)
    raw: np.ndarray = field(default_factory=_empty)
    basis: np.ndarray = field(default_factory=lambda: np.array([], dtype=object))
    flag: np.ndarray = field(default_factory=lambda: np.array([], dtype=bool))
    evicted: int = 0

    def extend(
        self, times: np.ndarray, values: np.ndarray, raw: np.ndarray, basis: list[str]
    ) -> None:
        if not len(times):
            return
        merged = np.concatenate([self.times, times])
        # Stable, retained reads first: at an equal instant arrival order wins,
        # which is the stored order a session read sorts by.
        order = np.argsort(merged, kind="stable")
        self.times = merged[order]
        self.values = np.concatenate([self.values, values])[order]
        self.raw = np.concatenate([self.raw, raw])[order]
        self.basis = np.concatenate([self.basis, np.asarray(basis, dtype=object)])[order]
        self.flag = np.concatenate([self.flag, np.zeros(len(times), dtype=bool)])[order]

    def drop_front(self, n: int) -> None:
        if n <= 0:
            return
        self.times, self.values, self.raw = self.times[n:], self.values[n:], self.raw[n:]
        self.basis, self.flag = self.basis[n:], self.flag[n:]
        self.evicted += n

    def series(self) -> _Series:
        return _Series(self.times, self.values, list(self.basis))


def compact(record: dict[str, Any]) -> dict[str, Any]:
    """One decided record as the live block publishes it."""
    sample = record["sample"]
    lookup = record.get("lookup") or {}
    return {
        "acquired_at": sample["acquired_at"],
        "observed_phi": sample["observed_phi"],
        "sentinel": sample["observed_word"] is None,
        "predicted_phi": record["predicted_phi"],
        "difference_phi": record["difference_phi"],
        "qualified_residual_phi": record["qualified_residual_phi"],
        "within_tolerance": record["within_tolerance"],
        "state": record["state"],
        "reason": record["reason"],
        "branch": record["branch"],
        "cell": None if not lookup else [lookup["row"], lookup["column"]],
        "tuple_skew_s": record.get("tuple_skew_s"),
    }


class TwinStream:
    """The stored evaluator's records, decided as reads arrive.

    Feed reads with :meth:`extend` (float seconds on the acquisition clock,
    values exactly as a session read gives them, and each read's timestamp
    basis), then :meth:`advance`; it returns the records decided since the
    last call. :meth:`flush` decides the rest, as the end of a session does.
    A channel stored under another decode refuses the stream as a whole
    (:attr:`refusal`), as it refuses a stored session.
    """

    def __init__(
        self,
        definition: TwinDefinition,
        table: ResolvedTable,
        *,
        definition_sha256: str,
        catalog: DidMap,
        dids_path: Path = DIDS_YAML,
        tolerance_words: int = 1,
        settle_s: float = DEFAULT_SETTLE_S,
        max_reads_per_spec: int = MAX_READS_PER_SPEC,
        on_records: Callable[[list[dict[str, Any]]], None] | None = None,
    ) -> None:
        if settle_s < 0:
            raise ValueError("settle_s must not be negative")
        check_windows(definition, catalog)
        self.definition = definition
        self.table = table
        self.definition_sha256 = definition_sha256
        self.tolerance_words = tolerance_words
        self.settle_s = settle_s
        self.max_reads_per_spec = max_reads_per_spec
        self.on_records = on_records
        self.specs = evaluated_specs(definition)
        self._raw_specs = set(raw_specs(definition))
        self._decodes = {
            spec: stored_decode(spec, dids_path, catalog=catalog) for spec in self._raw_specs
        }
        self.horizon_s = decision_horizon_s(definition)
        self.lookback_s = lookback_s(definition)
        self.qualification = Qualification.of(table, definition, definition_sha256)
        self._policy = PairingPolicy.within(
            pd.Timedelta(seconds=definition.gates.max_acquisition_skew_s), reference="a"
        )
        self.reset()

    # -- state ------------------------------------------------------------

    def reset(self) -> None:
        """Forget every read, decision and count (a new session, a restart,
        or an identity change): nothing transient survives."""
        self._reads: dict[str, _Reads] = {spec: _Reads() for spec in self.specs}
        self.accounting = Accounting()
        self.recent: deque[dict[str, Any]] = deque(maxlen=RECENT_RECORDS)
        self.latest_prediction: dict[str, Any] | None = None
        self.clock_s = -math.inf
        #: Reads at or before this were (or could have been) cited by a
        #: decided record; a later arrival there is late.
        self.sealed_s = -math.inf
        self.decided_through_s = -math.inf
        self.reads_seen: dict[str, int] = dict.fromkeys(self.specs, 0)
        self.late_reads: dict[str, int] = dict.fromkeys(self.specs, 0)
        self.evicted_over_cap: dict[str, int] = dict.fromkeys(self.specs, 0)
        self.paired: dict[str, int] = dict.fromkeys(self.definition.inputs.channels(), 0)
        self.refusal: TwinRefusalError | None = None
        #: Earliest accepted read per channel: the causal "captured" and
        #: "which coolant source" questions are asked of it per sample.
        self._first: dict[str, float] = dict.fromkeys(self.specs, math.inf)

    @property
    def undecided(self) -> int:
        reference = self.definition.reference
        return int(
            (~self._reads[reference.channel].flag).sum()
            + (~self._reads[reference.sentinel_channel].flag).sum()
        )

    @property
    def retained(self) -> int:
        return sum(len(reads.times) for reads in self._reads.values())

    # -- input -------------------------------------------------------------

    def extend(
        self, spec: str, times: np.ndarray, values: np.ndarray, basis: list[str] | None = None
    ) -> None:
        """Add reads of ``spec``. Late reads are counted and dropped."""
        if spec not in self._reads or self.refusal is not None:
            return
        times = np.asarray(times, dtype=float)
        values = np.asarray(values, dtype=float)
        bases = list(basis) if basis is not None else ["acquired"] * len(times)
        self.reads_seen[spec] += len(times)
        if not len(times):
            return
        keep = times > self.sealed_s
        late = int((~keep).sum())
        if late:
            self.late_reads[spec] += late
            times, values = times[keep], values[keep]
            bases = [b for b, k in zip(bases, keep.tolist(), strict=True) if k]
        if not len(times):
            return
        raw = np.full(len(times), np.nan)
        if spec in self._raw_specs:
            decode = self._decodes[spec]
            if decode is None:
                self.refusal = TwinRefusalError(
                    "wrong_units",
                    f"{spec} is not a catalog memory channel; its raw counts are unknown",
                )
                return
            converted = raw_counts(values, decode)
            if converted is None:
                self.refusal = TwinRefusalError(
                    "wrong_units",
                    f"{spec} values do not return to whole raw counts under the catalog "
                    f"decode (scale {decode[0]}, offset {decode[1]}): stored under another decode",
                )
                return
            raw = converted
        self._reads[spec].extend(times, values, raw, bases)
        self._first[spec] = min(self._first[spec], float(np.min(times)))
        self.clock_s = max(self.clock_s, float(np.max(times)))

    # -- decisions ---------------------------------------------------------

    def default_cutoff(self) -> float:
        """The latest sample time every stream has been read far enough past."""
        return self.clock_s - self.settle_s - self.horizon_s

    def advance(self, cutoff_s: float | None = None) -> list[dict[str, Any]]:
        """Decide every command sample at or before ``cutoff_s`` (default:
        :meth:`default_cutoff`). The cutoff never moves back."""
        if self.refusal is not None:
            return []
        cutoff = self.default_cutoff() if cutoff_s is None else cutoff_s
        cutoff = max(cutoff, self.decided_through_s)
        records = self._decide(cutoff)
        self.decided_through_s = cutoff
        self.sealed_s = max(self.sealed_s, cutoff + self.horizon_s)
        self._prune(cutoff)
        if records and self.on_records is not None:
            self.on_records(records)
        return records

    def flush(self) -> list[dict[str, Any]]:
        """Decide every remaining sample with the reads there are (session end)."""
        return self.advance(math.inf)

    def _decide(self, cutoff: float) -> list[dict[str, Any]]:
        definition = self.definition
        reference = definition.reference
        target = self._reads[reference.channel]
        sentinel = self._reads[reference.sentinel_channel]
        stamps: list[tuple[float, bool, int]] = []
        for is_sentinel, reads in ((False, target), (True, sentinel)):
            chosen = np.flatnonzero(~reads.flag & (reads.times <= cutoff))
            stamps += [(float(reads.times[j]), is_sentinel, int(j)) for j in chosen]
        if not stamps:
            return []
        stamps.sort(key=lambda s: (s[0], s[1], s[2]))
        series = {spec: reads.series() for spec, reads in self._reads.items()}
        raw = {spec: self._reads[spec].raw for spec in self._raw_specs}
        candidates = []
        for position, (t, is_sentinel, j) in enumerate(stamps):
            reads = sentinel if is_sentinel else target
            candidates.append(
                new_candidate(
                    definition,
                    index=position,
                    time=t,
                    sentinel=is_sentinel,
                    row=reads.evicted + j,
                    word=None if is_sentinel else int(reads.raw[j]),
                    basis=str(reads.basis[j]),
                )
            )
            reads.flag[j] = True

        # Pair against the reads earlier batches left unused.
        times = np.array([c.time for c in candidates], dtype=float)
        pairs: dict[str, dict[int, int]] = {}
        for name, spec in definition.inputs.channels().items():
            reads = self._reads[spec]
            free = np.flatnonzero(~reads.flag)
            matched = match_ordered(times, reads.times[free], self._policy)
            chosen = free[matched.index_b]
            reads.flag[chosen] = True
            pairs[name] = dict(zip(matched.index_a.tolist(), chosen.tolist(), strict=True))

        records = []
        for candidate in candidates:
            _assess(
                candidate,
                definition=definition,
                table=self.table,
                series=series,
                raw=raw,
                pairs=pairs,
                first_read=self._first.__getitem__,
                session_end=self.clock_s,
                session_complete=True,
                tolerance_words=self.tolerance_words,
            )
            record = candidate.record
            for ref in (record.get("inputs") or {}).values():
                if ref is not None:
                    ref["row"] += self._reads[ref["spec"]].evicted
            self.qualification.apply(record)
            self.accounting.add(record)
            for name, ref in record["inputs"].items():
                if ref is not None:
                    self.paired[name] += 1
            self.recent.append(compact(record))
            if record["predicted_word"] is not None:
                self.latest_prediction = compact(record)
            records.append(record)
        return records

    def _prune(self, cutoff: float) -> None:
        if math.isinf(cutoff) and cutoff > 0:
            return  # flushed: the stream is closed and every read sealed
        keep_from = cutoff - self.lookback_s - HISTORY_MARGIN_S
        for spec, reads in self._reads.items():
            n = int(np.searchsorted(reads.times, keep_from, side="left"))
            if spec in (
                self.definition.reference.channel,
                self.definition.reference.sentinel_channel,
            ):
                # Never evict an undecided sample.
                undecided = np.flatnonzero(~reads.flag)
                if len(undecided):
                    n = min(n, int(undecided[0]))
            over = len(reads.times) - n - self.max_reads_per_spec
            if over > 0:
                self.evicted_over_cap[spec] += over
                n += over
            reads.drop_front(n)
        self.accounting.forget_before({spec: r.evicted for spec, r in self._reads.items()})

    # -- publication ---------------------------------------------------------

    def summary(self) -> dict[str, Any]:
        """Counts and applicability so far, in the stored evaluation's terms."""
        accounting = self.accounting
        return {
            "refusal": None
            if self.refusal is None
            else {"reason": self.refusal.reason, "detail": self.refusal.message},
            "provenance": self.qualification.provenance,
            "pairing": pairing_block(self.definition),
            "tolerance_words": self.tolerance_words,
            "qualification_blockers": list(self.qualification.blockers),
            "coolant_sources": accounting.coolant_sources(self.definition),
            "accounting": accounting.counts(),
            "coverage": accounting.coverage(),
            "capture_requirement": capture_requirement(accounting.eligible, self.definition),
            "stream": {
                "pending": self.undecided,
                "decision_horizon_s": self.horizon_s,
                "settle_s": self.settle_s,
                "lookback_s": self.lookback_s,
                "clock": None
                if not math.isfinite(self.clock_s)
                else format_epoch(self.clock_s, nanoseconds=True),
                "decided_through": None
                if not math.isfinite(self.decided_through_s)
                else format_epoch(self.decided_through_s, nanoseconds=True),
                "reads_seen": dict(self.reads_seen),
                "late_reads": dict(self.late_reads),
                "evicted_over_cap": dict(self.evicted_over_cap),
                "retained_reads": self.retained,
                "paired": dict(self.paired),
            },
            "latest_prediction": self.latest_prediction,
            "recent": list(self.recent),
        }


__all__ = ["MAX_READS_PER_SPEC", "RECENT_RECORDS", "TwinStream", "compact"]
