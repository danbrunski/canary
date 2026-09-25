"""Plausibility evaluator: checks, confirmation and evidence (A05.2–A05.7).

Runs over **arrived** samples. A03.1's per-channel "sample arrived" flag does
not exist yet, so arrival is defined here directly: one call to
:meth:`PlausibilityEvaluator.observe` is one arrived sample — one decoded
collector reading or one decoded CAN frame, *before* the quality gate,
resampling or forward-fill. A value carried forward onto a later tick never
reaches this module. The live path calls it from
``LiveFeatureSource._set_value`` (the single choke point every observed value
passes through); the offline path from the session's long frame
(:mod:`src.plausibility.offline`). Both feed the same code.

Checks (each gated on the channel's enabled operating modes):

* **range** — the sample is outside the channel's physical limits.
* **slew** — ``|delta| > per_second * max(dt, update_interval) + allowance``
  against the previous arrived sample, ``dt`` being the real time between
  them (floored at the source's refresh interval, since two reads a few ms
  apart can straddle one refresh).
* **stuck** — ``samples`` consecutive identical arrived samples spanning at
  least ``min_seconds``, while in a mode where the channel is expected to
  move; re-detected each time the run is as long again.
* **freshness** — at a tick, a channel that has arrived at least
  :data:`FRESHNESS_MIN_ARRIVALS` times this session has had its group
  *answering* for
  longer than the channel's tier interval × ``freshness_k`` without it. The
  group's answering clock only advances across gaps between its arrivals no
  longer than the group's stated silence, so a Mode 22 window (which suspends
  every Mode 01 PID together) or a key-off adds nothing and is not a
  per-channel fault; a whole source going stale is ``vehicle_state``'s to
  report. This is the resampler's ``__gap`` age (``now - last arrival``) with
  the suspended time taken out and a threshold keyed to the channel's own
  poll tier rather than the 1 Hz model grid.

Confirmation (A05.6): the first detection of a (channel, check) opens a
*pending* episode; ``repeats`` detections at least ``min_separation_seconds``
after the first and inside ``window_seconds`` *confirm* it. Only confirmed
episodes alert. A pending episode of an ``across_drives`` check carried in
from the previous drive confirms on its first repeat in this one. Both states
are written as :class:`EvidenceRecord` lines (A05.7).

Stdlib only: this runs on the daemon's event loop.
"""

from __future__ import annotations

import logging
import time
from collections import deque
from collections.abc import Callable, Iterable, Mapping
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Literal

from src.jsonl_store import JsonlStore
from src.plausibility.limits import CHECKS, ChannelLimits, CheckName, LimitsCatalog

log = logging.getLogger(__name__)

EVIDENCE_SCHEMA = "plausibility-evidence/1"
EVIDENCE_FILENAME = "plausibility-evidence.jsonl"

State = Literal["pending", "confirmed"]

#: Records kept in memory for the status snapshot (the store keeps them all).
RECENT_RECORDS = 50

#: Arrivals a channel needs in this session before its absence is evidence.
#: One arrival shows no cadence: a capture that re-plans Mode 01 leaves the
#: previous plan's PIDs with exactly one sample at the session start.
FRESHNESS_MIN_ARRIVALS = 3

_INF = float("inf")
_NEG_INF = float("-inf")


def _iso(epoch: float) -> str:
    return datetime.fromtimestamp(epoch, UTC).isoformat().replace("+00:00", "Z")


@dataclass(frozen=True)
class EvidenceRecord:
    """One detection's evidence: what failed, against what, when, where.

    ``value``/``limit`` are in the channel's arriving units (raw counts for a
    ``decoded:`` channel); ``engineering_value``/``engineering_limit`` repeat
    them in engineering units when the channel has a pinned scale.
    ``sample_times`` are the arrived samples the detection rests on (ISO
    UTC): the offending one, and for slew/stuck/confirmation the earlier one
    it was compared with. ``text`` is rendered from the other fields by
    :func:`render_evidence` and never edited by hand.
    """

    state: State
    channel: str
    check: CheckName
    value: float | None
    limit: float
    limit_kind: str
    limit_source: str
    unit: str
    operating_mode: str | None
    sample_times: tuple[str, ...]
    detected_at: str
    session: str
    detections: int
    detail: Mapping[str, Any] = field(default_factory=dict)
    engineering_value: float | None = None
    engineering_limit: float | None = None
    schema: str = EVIDENCE_SCHEMA

    @property
    def alerts(self) -> bool:
        """Only a confirmed detection alerts (A05.6)."""
        return self.state == "confirmed"

    @property
    def text(self) -> str:
        return render_evidence(self)

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema": self.schema,
            "state": self.state,
            "alerts": self.alerts,
            "channel": self.channel,
            "check": self.check,
            "value": self.value,
            "limit": self.limit,
            "limit_kind": self.limit_kind,
            "limit_source": self.limit_source,
            "unit": self.unit,
            "engineering_value": self.engineering_value,
            "engineering_limit": self.engineering_limit,
            "operating_mode": self.operating_mode,
            "sample_times": list(self.sample_times),
            "detected_at": self.detected_at,
            "session": self.session,
            "detections": self.detections,
            "detail": dict(self.detail),
            "text": self.text,
        }

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> EvidenceRecord:
        if data.get("schema") != EVIDENCE_SCHEMA:
            raise ValueError(f"not a {EVIDENCE_SCHEMA} record")
        return cls(
            state=data["state"],
            channel=str(data["channel"]),
            check=data["check"],
            value=data.get("value"),
            limit=float(data["limit"]),
            limit_kind=str(data["limit_kind"]),
            limit_source=str(data["limit_source"]),
            unit=str(data.get("unit") or ""),
            operating_mode=data.get("operating_mode"),
            sample_times=tuple(data.get("sample_times") or ()),
            detected_at=str(data["detected_at"]),
            session=str(data["session"]),
            detections=int(data["detections"]),
            detail=dict(data.get("detail") or {}),
            engineering_value=data.get("engineering_value"),
            engineering_limit=data.get("engineering_limit"),
        )


def _fmt(value: float | None) -> str:
    if value is None:
        return "?"
    return f"{value:.6g}"


def render_evidence(record: EvidenceRecord) -> str:
    """The dashboard sentence for a record — derived, never stored alone."""
    unit = f" {record.unit}" if record.unit else ""
    value = record.value
    limit = record.limit
    if record.engineering_value is not None and record.engineering_limit is not None:
        value, limit = record.engineering_value, record.engineering_limit
    mode = record.operating_mode or "unknown mode"
    detail = record.detail
    if record.check == "range":
        side = "above" if record.limit_kind == "max" else "below"
        body = (
            f"{record.channel} read {_fmt(value)}{unit}, {side} its {_fmt(limit)}{unit} "
            f"limit ({record.limit_source})"
        )
    elif record.check == "slew":
        rate = detail.get("engineering_rate_per_second", detail.get("rate_per_second"))
        body = (
            f"{record.channel} moved {_fmt(detail.get('engineering_delta', detail.get('delta')))}"
            f"{unit} in {float(detail.get('dt_seconds', 0.0)):.3g} s ({_fmt(rate)}{unit}/s), "
            f"over its {_fmt(limit)}{unit}/s slew limit ({record.limit_source})"
        )
    elif record.check == "stuck":
        body = (
            f"{record.channel} held {_fmt(value)}{unit} for {detail.get('run_samples')} "
            f"consecutive samples ({float(detail.get('run_seconds', 0.0)):.0f} s) where it is "
            f"expected to move (limit {_fmt(limit)} samples, {record.limit_source})"
        )
    else:
        body = (
            f"{record.channel} has not arrived for {float(detail.get('age_seconds', 0.0)):.0f} s, "
            f"{float(detail.get('answering_age_seconds', 0.0)):.0f} s of it while its "
            f"{detail.get('group', '')} group kept answering (expected every "
            f"{_fmt(detail.get('expected_interval_seconds'))} s, limit {_fmt(limit)} s)"
        )
    suffix = f" in {mode}"
    if record.state == "confirmed":
        suffix += f"; confirmed after {record.detections} detections"
    return f"[{record.state}] {body}{suffix}"


class EvidenceStore(JsonlStore[EvidenceRecord]):
    """Append-only JSONL of every pending and confirmed record."""

    def __init__(self, path: Path) -> None:
        super().__init__(path, from_dict=EvidenceRecord.from_dict)

    def append(self, record: EvidenceRecord) -> EvidenceRecord:
        return self._append_record(record, to_dict=EvidenceRecord.to_dict)

    def carried_pending(self, current_session: str) -> list[EvidenceRecord]:
        """Pending records of the drive before ``current_session``, not
        confirmed there — the candidates an ``across_drives`` check may
        confirm on its first repeat now.

        "Before" is write order (the file is appended chronologically), never
        session-name order: capture prefixes sort by prefix first.
        """
        previous: str | None = None
        by_session: dict[str, list[EvidenceRecord]] = {}
        for record in self.raw_records():
            if record.session == current_session:
                continue
            by_session.setdefault(record.session, []).append(record)
            previous = record.session
        if previous is None:
            return []
        records = by_session[previous]
        confirmed = {(r.channel, r.check) for r in records if r.state == "confirmed"}
        return [
            r for r in records if r.state == "pending" and (r.channel, r.check) not in confirmed
        ]


@dataclass
class _Episode:
    state: State
    first_at: float
    last_at: float
    detections: int
    record: EvidenceRecord


@dataclass
class _GroupState:
    """One answer group's clock.

    ``answering`` is the group's *answering time*: seconds accumulated only
    across gaps between its arrivals no longer than ``silence_seconds``. A
    longer gap is the group being suspended (a Mode 22 window) or stopped
    (key-off) and adds nothing, so a channel's age on this clock is how long
    its group was answering without it.
    """

    silence_seconds: float
    last_at: float | None = None
    answering: float = 0.0


class _ChannelState:
    """Per-channel history, with the checks' parameters unpacked once so the
    per-sample path is attribute reads and float compares."""

    __slots__ = (
        "limits",
        "last_value",
        "last_at",
        "run_samples",
        "run_started_at",
        "arrivals",
        "stale_multiples",
        "answering_at",
        "group",
        "low",
        "high",
        "range_modes",
        "slew_rate",
        "slew_allowance",
        "slew_min_dt",
        "slew_modes",
        "stuck_samples",
        "stuck_seconds",
        "stuck_fired",
        "stuck_modes",
    )

    def __init__(self, limits: ChannelLimits, group: _GroupState | None) -> None:
        self.limits = limits
        self.last_value: float | None = None
        self.last_at: float | None = None
        self.run_samples = 0
        self.run_started_at: float | None = None
        self.arrivals = 0
        #: Freshness detections raised during the current silence.
        self.stale_multiples = 0
        #: The group's answering clock at this channel's last arrival.
        self.answering_at = 0.0
        self.group = group
        self.low = limits.minimum.value if limits.minimum is not None else None
        self.high = limits.maximum.value if limits.maximum is not None else None
        self.range_modes = limits.modes.get("range")
        self.slew_rate = limits.slew.per_second if limits.slew is not None else None
        self.slew_allowance = limits.slew.allowance if limits.slew is not None else 0.0
        self.slew_min_dt = limits.slew.update_interval_seconds if limits.slew is not None else 0.0
        self.slew_modes = limits.modes.get("slew")
        self.stuck_samples = limits.stuck.samples if limits.stuck is not None else None
        self.stuck_seconds = limits.stuck.min_seconds if limits.stuck is not None else 0.0
        #: Stuck detections raised in the current run.
        self.stuck_fired = 0
        self.stuck_modes = limits.modes.get("stuck")


@dataclass
class EvaluatorCounters:
    samples: int = 0
    non_finite: int = 0
    unchecked_samples: int = 0
    ticks: int = 0
    detections: dict[str, int] = field(default_factory=lambda: dict.fromkeys(CHECKS, 0))
    pending: int = 0
    confirmed: int = 0

    def to_dict(self) -> dict[str, Any]:
        return {
            "samples": self.samples,
            "non_finite": self.non_finite,
            "unchecked_samples": self.unchecked_samples,
            "ticks": self.ticks,
            "detections": dict(self.detections),
            "pending_records": self.pending,
            "confirmed_records": self.confirmed,
        }


class PlausibilityEvaluator:
    """Per-channel checks over arrived samples; see the module docstring.

    ``sink`` receives every record written (pending and confirmed).
    ``alert_sink`` receives confirmed records only — the alert path (the
    daemon hands it ``AnomalyEventEmitter.emit_plausibility``). The
    pending/confirmed split is decided here, so a caller cannot route a
    pending detection into the anomaly stream by forgetting to filter.
    ``carried`` is the previous drive's still-pending records; only those of
    ``across_drives`` checks are honoured.
    """

    def __init__(
        self,
        catalog: LimitsCatalog,
        *,
        session: str,
        sink: Callable[[EvidenceRecord], object] | None = None,
        alert_sink: Callable[[EvidenceRecord], object] | None = None,
        carried: Iterable[EvidenceRecord] = (),
    ) -> None:
        if catalog.profile is None:
            raise ValueError(
                "a plausibility evaluator needs the vehicle's plausibility.yaml "
                "(tier intervals and confirmation policy)"
            )
        self._catalog = catalog
        self.sink = sink
        self.alert_sink = alert_sink
        self.mode: str | None = None
        self.counters = EvaluatorCounters()
        self._recent: deque[EvidenceRecord] = deque(maxlen=RECENT_RECORDS)
        self._observed_unchecked: set[str] = set()
        self._reset_state(session, carried)

    # ------------------------------------------------------------------
    # lifecycle
    # ------------------------------------------------------------------

    @property
    def session(self) -> str:
        return self._session

    @property
    def catalog(self) -> LimitsCatalog:
        return self._catalog

    def reset(self, session: str, carried: Iterable[EvidenceRecord] = ()) -> None:
        """Start a new drive/session: every channel's history is dropped."""
        self._reset_state(session, carried)

    def _reset_state(self, session: str, carried: Iterable[EvidenceRecord]) -> None:
        self._session = session
        self._channels: dict[str, _ChannelState | None] = {}
        self._groups: dict[str, _GroupState] = {}
        self._episodes: dict[tuple[str, CheckName], _Episode] = {}
        self._carried: dict[tuple[str, CheckName], EvidenceRecord] = {}
        for record in carried:
            if record.state != "pending":
                continue
            policy = self._catalog.confirm_policy(record.channel, record.check)
            if policy.across_drives:
                self._carried[(record.channel, record.check)] = record
        group_silence: dict[str, float] = {}
        for limits in self._catalog.channels.values():
            threshold = limits.freshness_threshold_seconds
            if threshold is None:
                continue
            current = group_silence.get(limits.group)
            group_silence[limits.group] = threshold if current is None else min(current, threshold)
        for group, silence in group_silence.items():
            stated = self._catalog.group_silence(group)
            self._groups[group] = _GroupState(
                silence_seconds=stated if stated is not None else silence
            )

    # ------------------------------------------------------------------
    # samples
    # ------------------------------------------------------------------

    def _state_for(self, channel: str) -> _ChannelState | None:
        limits = self._catalog.channels.get(channel)
        if limits is None or not limits.is_checked():
            self._channels[channel] = None
            self._observed_unchecked.add(channel)
            return None
        state = _ChannelState(limits, self._groups.get(limits.group))
        self._channels[channel] = state
        return state

    def observe(self, channel: str, value: float, at: float, mode: str | None = None) -> None:
        """One arrived sample of ``channel`` at epoch seconds ``at``.

        ``mode`` defaults to :attr:`mode` (the live tick's latest operating
        mode). This is the per-sample hot path: every branch is a dict read,
        an attribute read or a float compare until something fails.
        """
        counters = self.counters
        counters.samples += 1
        try:
            state = self._channels[channel]
        except KeyError:
            state = self._state_for(channel)
        if state is None:
            counters.unchecked_samples += 1
            return
        if value != value or value in (_INF, _NEG_INF):
            counters.non_finite += 1
            return
        if mode is None:
            mode = self.mode
        group = state.group
        if group is not None:
            last = group.last_at
            if last is not None and 0.0 < at - last <= group.silence_seconds:
                group.answering += at - last
            if last is None or at > last:
                group.last_at = at
            state.answering_at = group.answering
        state.arrivals += 1
        state.stale_multiples = 0

        low = state.low
        high = state.high
        if (high is not None and value > high) or (low is not None and value < low):
            modes = state.range_modes
            if modes is None or mode in modes:
                limits = state.limits
                above = high is not None and value > high
                bound = limits.maximum if above else limits.minimum
                assert bound is not None
                self._detect(
                    state,
                    "range",
                    at,
                    mode,
                    value,
                    bound.value,
                    "max" if above else "min",
                    bound.source,
                    (at,),
                    {},
                )

        previous_value = state.last_value
        rate_limit = state.slew_rate
        if rate_limit is not None and previous_value is not None:
            previous_at = state.last_at
            assert previous_at is not None
            dt = at - previous_at
            delta = value - previous_value
            span = dt if dt > state.slew_min_dt else state.slew_min_dt
            if dt > 0 and abs(delta) > rate_limit * span + state.slew_allowance:
                modes = state.slew_modes
                if modes is None or mode in modes:
                    self._slew(state, at, mode, value, previous_value, previous_at, delta, dt)

        needed = state.stuck_samples
        if needed is not None:
            modes = state.stuck_modes
            if modes is None or mode in modes:
                if state.run_samples and value == previous_value:
                    state.run_samples += 1
                else:
                    state.run_samples = 1
                    state.run_started_at = at
                    state.stuck_fired = 0
                started = state.run_started_at if state.run_started_at is not None else at
                # The k-th detection of one run needs k x samples AND
                # k x min_seconds: a repeat is "still stuck as long again".
                due = state.stuck_fired + 1
                if state.run_samples >= due * needed and at - started >= due * state.stuck_seconds:
                    state.stuck_fired = due
                    limits = state.limits
                    assert limits.stuck is not None
                    self._detect(
                        state,
                        "stuck",
                        at,
                        mode,
                        value,
                        float(needed),
                        "stuck_samples",
                        limits.stuck.source,
                        (started, at),
                        {
                            "run_samples": state.run_samples,
                            "run_seconds": at - started,
                            "min_seconds": state.stuck_seconds,
                        },
                    )
            else:
                # A sample outside the modes where the channel must move
                # breaks the run: "unchanged" only counts while it should move.
                state.run_samples = 0
                state.run_started_at = None
                state.stuck_fired = 0

        state.last_value = value
        state.last_at = at

    def _slew(
        self,
        state: _ChannelState,
        at: float,
        mode: str | None,
        value: float,
        previous_value: float,
        previous_at: float,
        delta: float,
        dt: float,
    ) -> None:
        limits = state.limits
        assert limits.slew is not None
        rate = abs(delta) / dt
        detail: dict[str, Any] = {
            "previous_value": previous_value,
            "delta": delta,
            "dt_seconds": dt,
            "rate_per_second": rate,
            "allowance": limits.slew.allowance,
            "update_interval_seconds": limits.slew.update_interval_seconds,
        }
        if limits.engineering is not None:
            detail["engineering_delta"] = delta * limits.engineering[0]
            detail["engineering_rate_per_second"] = rate * abs(limits.engineering[0])
        self._detect(
            state,
            "slew",
            at,
            mode,
            value,
            limits.slew.per_second,
            "slew_per_second",
            limits.slew.source,
            (previous_at, at),
            detail,
        )

    # ------------------------------------------------------------------
    # ticks
    # ------------------------------------------------------------------

    def check_freshness(self, now: float, mode: str | None = None) -> None:
        """Run the freshness check at ``now`` (the live tick / offline grid)."""
        self.counters.ticks += 1
        if mode is None:
            mode = self.mode
        groups = self._groups
        for state in self._channels.values():
            if state is None or state.arrivals < FRESHNESS_MIN_ARRIVALS:
                continue
            limits = state.limits
            threshold = limits.freshness_threshold_seconds
            if threshold is None or not limits.enabled_in("freshness", mode):
                continue
            group = groups.get(limits.group)
            if group is None or group.last_at is None:
                continue
            # Age on the group's answering clock: time its peers kept
            # answering without this channel. A Mode 22 window or a key-off
            # adds nothing, so neither can make one channel look stale.
            age = group.answering - state.answering_at
            if age <= threshold:
                continue
            assert state.last_at is not None
            # Detected when the channel becomes overdue, then again for every
            # further expected interval it stays missing: "still absent one
            # interval later" is the repeat that confirms.
            interval = limits.expected_interval_seconds or threshold
            missed = 1 + int((age - threshold) // interval)
            if missed <= state.stale_multiples:
                continue
            state.stale_multiples = missed
            self._detect(
                state,
                "freshness",
                now,
                mode,
                state.last_value,
                threshold,
                "max_age_seconds",
                f"tier {limits.tier!r} interval x freshness_k",
                (state.last_at,),
                {
                    "age_seconds": now - state.last_at,
                    "answering_age_seconds": age,
                    "expected_interval_seconds": limits.expected_interval_seconds,
                    "tier": limits.tier,
                    "group": limits.group,
                    "checked_at": _iso(now),
                },
            )

    # ------------------------------------------------------------------
    # confirmation
    # ------------------------------------------------------------------

    def _detect(
        self,
        state: _ChannelState,
        check: CheckName,
        at: float,
        mode: str | None,
        value: float | None,
        limit: float,
        limit_kind: str,
        limit_source: str,
        sample_times: tuple[float, ...],
        detail: Mapping[str, Any],
    ) -> None:
        self.counters.detections[check] += 1
        limits = state.limits
        channel = limits.channel
        key = (channel, check)
        policy = self._catalog.confirm_policy(channel, check)
        episode = self._episodes.get(key)
        if episode is not None and at - episode.first_at > policy.window_seconds:
            if episode.state == "pending" or at - episode.last_at > policy.window_seconds:
                episode = None  # expired: a new detection opens a new episode
        engineering_value = (
            limits.to_engineering(value)
            if value is not None and limits.engineering is not None and check != "slew"
            else None
        )
        engineering_limit = None
        if limits.engineering is not None:
            if check == "range":
                engineering_limit = limits.to_engineering(limit)
            elif check == "slew":
                engineering_limit = limit * abs(limits.engineering[0])

        def build(
            state_name: State, detections: int, times: tuple[float, ...], extra: Mapping[str, Any]
        ) -> EvidenceRecord:
            return EvidenceRecord(
                state=state_name,
                channel=channel,
                check=check,
                value=value,
                limit=limit,
                limit_kind=limit_kind,
                limit_source=limit_source,
                unit=limits.unit if limits.engineering is not None else _arriving_unit(limits),
                operating_mode=mode,
                sample_times=tuple(_iso(t) for t in times),
                detected_at=_iso(at),
                session=self._session,
                detections=detections,
                detail={**detail, **extra},
                engineering_value=engineering_value,
                engineering_limit=engineering_limit,
            )

        if episode is None:
            carried = self._carried.pop(key, None)
            if carried is not None:
                record = build(
                    "confirmed",
                    carried.detections + 1,
                    sample_times,
                    {
                        "confirmed_by": "repeat on the next drive",
                        "pending_session": carried.session,
                        "pending_sample_times": list(carried.sample_times),
                    },
                )
                self._episodes[key] = _Episode("confirmed", at, at, record.detections, record)
                self._write(record)
                return
            record = build("pending", 1, sample_times, {})
            self._episodes[key] = _Episode("pending", at, at, 1, record)
            self._write(record)
            return

        if episode.state == "pending" and at - episode.first_at < policy.min_separation_seconds:
            episode.last_at = at
            return  # too close to the first to be an independent repeat
        episode.detections += 1
        episode.last_at = at
        if episode.state == "pending" and episode.detections >= policy.repeats:
            first_times = episode.record.sample_times
            record = build(
                "confirmed",
                episode.detections,
                sample_times,
                {
                    "first_detected_at": episode.record.detected_at,
                    "first_sample_times": list(first_times),
                },
            )
            episode.state = "confirmed"
            episode.record = record
            self._write(record)

    def _write(self, record: EvidenceRecord) -> None:
        if record.state == "confirmed":
            self.counters.confirmed += 1
            log.warning("plausibility: %s", record.text)
        else:
            self.counters.pending += 1
        self._recent.append(record)
        if self.sink is not None:
            try:
                self.sink(record)
            except Exception:  # noqa: BLE001 — evidence persistence is best-effort
                log.exception("plausibility evidence sink failed")
        if record.alerts and self.alert_sink is not None:
            try:
                self.alert_sink(record)
            except Exception:  # noqa: BLE001 — an alert failure must never cost a sample
                log.exception("plausibility alert sink failed")

    # ------------------------------------------------------------------
    # reporting
    # ------------------------------------------------------------------

    def active(self) -> list[EvidenceRecord]:
        """The latest record of every open episode."""
        return [episode.record for episode in self._episodes.values()]

    def confirmed(self) -> list[EvidenceRecord]:
        return [r for r in self.active() if r.state == "confirmed"]

    def recent(self) -> list[EvidenceRecord]:
        return list(self._recent)

    def coverage_summary(self) -> dict[str, Any]:
        """Which kind of empty: observed channels checked vs unchecked."""
        checked: dict[str, int] = dict.fromkeys(CHECKS, 0)
        observed_checked = 0
        for state in self._channels.values():
            if state is None:
                continue
            observed_checked += 1
            for check, verdict in state.limits.coverage().items():
                if verdict == "checked":
                    checked[check] += 1
        return {
            "observed_channels_checked": observed_checked,
            "observed_channels_unchecked": len(self._observed_unchecked),
            "channels_per_check": checked,
        }

    def snapshot(self) -> dict[str, Any]:
        confirmed = self.confirmed()
        return {
            "session": self._session,
            "mode": self.mode,
            "counters": self.counters.to_dict(),
            "coverage": self.coverage_summary(),
            "open_pending": sum(1 for r in self.active() if r.state == "pending"),
            "open_confirmed": len(confirmed),
            "confirmed": [r.text for r in confirmed[-10:]],
            "carried_pending": len(self._carried),
        }


def _arriving_unit(limits: ChannelLimits) -> str:
    if limits.group == "broadcast":
        return "counts"
    return limits.unit


class ObserveTimer:
    """Accumulates the per-sample observe cost between ticks (A05.9)."""

    def __init__(self) -> None:
        self.seconds = 0.0
        self.samples = 0

    def take(self) -> tuple[float, int]:
        seconds, samples = self.seconds, self.samples
        self.seconds = 0.0
        self.samples = 0
        return seconds, samples

    def timed(
        self, observe: Callable[[str, float, float], None]
    ) -> Callable[[str, float, float], None]:
        def run(channel: str, value: float, at: float) -> None:
            started = time.perf_counter()
            try:
                observe(channel, value, at)
            finally:
                self.seconds += time.perf_counter() - started
                self.samples += 1

        return run


__all__ = [
    "EVIDENCE_FILENAME",
    "EVIDENCE_SCHEMA",
    "EvidenceRecord",
    "EvidenceStore",
    "ObserveTimer",
    "PlausibilityEvaluator",
    "render_evidence",
]
