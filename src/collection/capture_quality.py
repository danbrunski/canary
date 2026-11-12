"""Whether a capture actually delivered what its profile promised.

A capture profile makes claims — this rate, these channels, this tuple
co-observed. Nothing checked them. The 2026-08-10 fuel-map capture ran for 24
minutes describing itself as "High-rate fueling and timing capture" and
produced **two** usable delivery-error sample-instants; every individual number
along the way looked unremarkable, and the shortfall was only visible by
reconstructing the whole chain afterwards from the Parquet.

So the promise becomes a contract with a verdict, evaluated in one place and
consumed three times: **live** (arm-time, ~20 s in, while the operator can
still fix it), at **stop** (persisted beside the data as
``capture_quality.json``) and on **recompute** over a historic session's rows.

The first version of this evaluator (sidecar ``schema_version`` 1) had three
defects, each a way of quoting the wrong number (roadmap C03):

* it scored the *trailing 60 s* and called it the capture — a five-minute
  capture with samples only in its last minute passed;
* it described the daemon's *cumulative* storage-drop counter as loss during
  this capture — an old incident failed every later capture, and a capture
  that lost rows itself could hide behind a clean final minute;
* it marked the slowest member's rate as "exact" tuple coverage whenever the
  profile round-stamped — a shared stamp is not a complete tuple.

Version 2 keeps three questions apart, with three separate numbers:

* **cadence** — median interarrival while observing; never a count divided
  by wall time (that conflates rate with duty cycle: on 2026-08-06 an
  on-target 14.93 Hz channel read 11.25 Hz because the window opened late);
* **coverage** — observed versus eligible duration: missing start, missing
  tail, long gaps, with intentional pauses excluded *explicitly*;
* **yield** — complete tuples actually persisted, counted per capture round
  from the rows on disk with the profile's declared acquisition skew.

Loss is scoped by epoch (``capture_losses``): a counter is ``verified`` only
when both readings exist and it did not reset; ``unknown`` is reported as
unknown, never as zero. A failing verdict names the observed constraint
(``unanswered_input``, ``rate_shortfall``, ``acquisition_gap``,
``tuple_incomplete``, ``tuple_skew``, ``persistence_loss``) with the evidence
behind it, instead of advice about budgets and rates that an old drop
incident could trigger.
"""

from __future__ import annotations

from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from typing import Any

from src.collection.capture_lifetime import (
    LONG_GAP_SECONDS,
    MEMORY_READING_PREFIX,
    MEMORY_SENTINEL_SUFFIX,
    subtract_windows,
)
from src.collection.kline.names import capture_key
from src.collection.obd_scheduler import BATCHING_COUNTERS, MODE22_BATCHING_COUNTERS
from src.storage.capture_losses import (
    CONTROLLER,
    RECORDING,
    RESET,
    UNKNOWN,
    VERIFIED,
    unknown_losses,
)
from src.storage.capture_manifest import CAPTURE_QUALITY_SCHEMA_VERSION, SCOPE_LIVE

#: Fraction of the requested rate a channel may fall to before it counts as a
#: shortfall. Loose enough to absorb round scheduling jitter, tight enough that
#: the 0.28-vs-4 Hz case is never a pass.
RATE_TOLERANCE = 0.9

#: A gap longer than this many requested periods is a coverage failure, not a
#: slow rate. They were one number until 2026-08-06, when idle time inside a
#: capture made an on-target 14.93 Hz poll report 11.25 Hz and read as a fault.
STALL_PERIODS = 5.0

#: Floor under the stall threshold. Without it a 20 Hz channel was called
#: stalled after 0.25 s, which is ordinary scheduling jitter — the first live
#: run of this contract flagged every DID in the capture on that basis. A fast
#: channel silent for two seconds is a real coverage gap; a quarter-second is
#: not, at any rate.
MIN_STALL_SECONDS = 2.0

#: Poll tiers that are not part of a capture's channel selection. DTC reads are
#: always eligible (`request_eligible` returns True for them regardless of
#: profile) and are polled on their own slow schedule, so scoring them against
#: the capture's rate reported DTC_STORED/PENDING/PERMANENT as unanswered on
#: the contract's first live run. They are diagnostics, not capture channels.
EXCLUDED_KINDS = frozenset({"dtc"})
EXCLUDED_TIERS = frozenset({"dtc", "on_demand"})

#: Minimum verification interval. Below this the sample is too short to
#: separate a slow channel from an unlucky one, and a "pass" would be noise.
#: Returned as ``unverified`` rather than as a pass, because those are
#: different claims.
MIN_VERIFY_SECONDS = 5.0

#: Coverage a gapped channel must keep for its gaps to make the capture
#: ``degraded`` rather than ``fail``. Above it the channel delivered nearly the
#: whole drive and the gaps are named, not disqualifying: both 2026-09-21 tuning
#: drives "failed" on a 76 s start-up backoff or two 3 s stalls in 17-25 min of
#: otherwise complete data, which the operator rightly read as usable. Below it
#: the missing share is large enough to bias any analysis of that channel.
DEGRADED_MIN_COVERAGE = 0.9

#: Default acquisition skew a complete tuple may span when the profile does
#: not declare ``max_tuple_skew_s`` and has no target rate to derive it from.
DEFAULT_MAX_TUPLE_SKEW_S = 0.25

#: The constraint vocabulary (C03.10). Each failure names exactly one.
UNANSWERED_INPUT = "unanswered_input"
RATE_SHORTFALL = "rate_shortfall"
ACQUISITION_GAP = "acquisition_gap"
TUPLE_INCOMPLETE = "tuple_incomplete"
TUPLE_SKEW = "tuple_skew"
PERSISTENCE_LOSS = "persistence_loss"
UNKNOWN_LOSS_COUNTERS = "unknown_loss_counters"

#: Clauses that make the verdict ``fail`` (or ``degraded``, for gaps that
#: leave every channel above ``DEGRADED_MIN_COVERAGE``); the rest make it
#: ``unverified``.
HARD_CLAUSES = frozenset(
    {
        UNANSWERED_INPUT,
        RATE_SHORTFALL,
        ACQUISITION_GAP,
        TUPLE_INCOMPLETE,
        TUPLE_SKEW,
        PERSISTENCE_LOSS,
    }
)

TUPLE_CHANNEL = "__tuple__"

#: ``kind`` of a capture profile's ``kline:`` entry in the channel table
#: (H05.8). Scored on the capture ledger, which the K-line collector feeds per
#: entry; its cadence is the achieved K-line rate.
KLINE_KIND = "kline"
#: The pause reason CANary's own K-line session opens (``tx_gate``: the ECM's
#: diagnostic session moves to ``kline``, so CAN diagnostics pause). For a
#: K-line channel that window is its own acquisition time, never a pause.
KLINE_SESSION_PAUSE = "ecm-session-kline"
#: ``lifetime["source"]`` when the ledger was rebuilt from persisted rows
#: (``src.analysis.capture_coverage.SOURCE_ROWS``, pinned equal by a test).
#: That rebuild reads Mode 01/22 rows only, so K-line entries are not scored
#: from it rather than being reported as never answered.
ROWS_LIFETIME_SOURCE = "persisted_rows"

#: What explains one long gap in a channel (D03.70), in ``long_gap_causes``.
#: ``sent_and_missed``: the channel's request was sent inside the gap and
#: missed (a timeout, an NRC), so the miss back-off and the wait for the next
#: window are the candidates. ``not_sent``: the request's send timeline has a
#: gap covering this one, i.e. nothing was sent: scheduling, budget or a window
#: that did not open. ``sent_no_result``: sends continued through the gap with
#: no miss and no answer recorded: the send raised on the bus (the one send the
#: loop counts without deciding an outcome), the window answered but left a
#: member undecoded, or, in a rebuild from rows, the writer lost the rows.
#: ``unknown``: the request ledger cannot say (a sidecar from before it, a
#: K-line channel, or a miss list or send timeline that overflowed). A pause
#: window is no exception: the daemon sends nothing while paused, so a gap
#: overlapping one reads ``not_sent``.
GAP_SENT_AND_MISSED = "sent_and_missed"
GAP_NOT_SENT = "not_sent"
GAP_SENT_NO_RESULT = "sent_no_result"
GAP_UNKNOWN = "unknown"
#: How far before a gap's end the send that closes it may fall: the answer that
#: ends a gap arrives one response turnaround (about 10-30 ms) after its send,
#: so the send is just before the observation. Well above that, well below the
#: 2 s gap floor. Only the closing edge needs it: the send that produced the
#: gap's first observation is at or before that observation.
ATTEMPT_EDGE_SLACK_S = 0.25
#: Misses listed per gap (the count and kinds cover them all).
GAP_CAUSE_MISSES_LISTED = 8
#: A ledger rebuilt against rows (a recompute after the daemon's ledger is
#: gone) puts a gap's edges on the rows' wall clock and a miss on the
#: monotonic one; a miss this close outside a gap still belongs to it.
ROWS_CLOCK_SLACK_S = 0.25
#: ``request_ledger["source"]``.
LEDGER_LIVE = "live"
LEDGER_CARRIED = "carried"
LEDGER_UNAVAILABLE = "unavailable"

Windows = list[tuple[float, float]]


@dataclass(frozen=True)
class ChannelQuality:
    """One channel's side of the contract: cadence and coverage, separately."""

    channel: str
    transport_channel: str
    kind: str | None
    requested_hz: float | None
    #: ``ok`` | ``missing`` | ``shortfall`` | ``gap``. ``missing`` and
    #: ``shortfall`` demand opposite responses — a channel the ECU never
    #: answers wants removing from the profile, a throttled one wants budget —
    #: so they are never collapsed into a single "bad".
    status: str
    cadence: dict[str, Any]
    coverage: dict[str, Any]
    in_required_tuple: bool = False
    #: Trailing 60 s rate from the live scheduler, when available. Shown on
    #: the dashboard; never the basis of the verdict.
    trailing_hz: float | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "channel": self.channel,
            "transport_channel": self.transport_channel,
            "kind": self.kind,
            "requested_hz": self.requested_hz,
            "status": self.status,
            "in_required_tuple": self.in_required_tuple,
            "cadence": dict(self.cadence),
            "coverage": dict(self.coverage),
            "trailing_hz": self.trailing_hz,
        }


@dataclass(frozen=True)
class CaptureQuality:
    """The contract's verdict, and every number behind it."""

    verdict: str
    profile: str
    scope: str
    interval: dict[str, Any]
    channels: list[ChannelQuality]
    tuples: dict[str, Any]
    losses: dict[str, Any]
    persistence: dict[str, Any] | None
    trailing: dict[str, Any]
    #: Each entry names one constraint from the vocabulary and its evidence.
    failures: list[dict[str, Any]] = field(default_factory=list)
    required_tuple: list[str] = field(default_factory=list)
    #: Upper bound on how often the tuple could complete: its slowest member's
    #: cadence. Always a bound; the count is in ``tuples``.
    tuple_rate_bound_hz: float | None = None
    batching: dict[str, Any] | None = None
    notes: list[str] = field(default_factory=list)
    #: Per capture-selected request: sends, misses (by kind, with NRC) and the
    #: bounded miss list, from the capture's ledger (D03.70). ``source`` says
    #: whether it is the live ledger, one carried over from the stop-time
    #: sidecar into a rebuild, or unavailable. ``None`` on a report built
    #: before it existed.
    request_ledger: dict[str, Any] | None = None

    @property
    def passed(self) -> bool:
        return self.verdict == "pass"

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema_version": CAPTURE_QUALITY_SCHEMA_VERSION,
            "scope": self.scope,
            "verdict": self.verdict,
            "profile": self.profile,
            "interval": dict(self.interval),
            "required_tuple": list(self.required_tuple),
            "tuple_rate_bound_hz": self.tuple_rate_bound_hz,
            "tuples": dict(self.tuples),
            "failures": list(self.failures),
            "channels": [row.to_dict() for row in self.channels],
            "losses": dict(self.losses),
            "persistence": None if self.persistence is None else dict(self.persistence),
            "trailing": dict(self.trailing),
            "batching": self.batching,
            "notes": list(self.notes),
            "request_ledger": self.request_ledger,
        }


# ---------------------------------------------------------------------------
# Batching counters, scoped to the capture
# ---------------------------------------------------------------------------

#: What the capture report's ``batching`` block is about. The scheduler's own
#: ``batching`` counters are cumulative since its collector loop started; on
#: 2026-09-18 a capture reported 102 desyncs of which 99 had happened in an
#: earlier capture on the same daemon, because the block was copied verbatim.
BATCHING_SCOPE_CAPTURE = "capture"

#: Cumulative counters in the scheduler's ``batching`` block (Mode 01 level).
#: Identity of the counters' owner (the collector loop's start instant). A
#: different value at arm and now means the counters restarted in between,
#: even if they have since climbed past their arm-time values.
BATCHING_LOOP_KEY = "loop_started_monotonic"


def batching_baseline(scheduler: Mapping[str, Any] | None) -> dict[str, Any]:
    """The arm-time reading the capture's batching deltas are taken against.

    ``recorded`` is False when the scheduler had not reported yet (an empty
    payload): its batching counters were *unknown* at arm, not zero. A
    reported payload whose ``batching`` is ``None`` means batching was off
    with nothing sent, which is a genuine zero baseline.
    """
    if not scheduler:
        return {"recorded": False, "batching": None}
    block = scheduler.get("batching")
    return {"recorded": True, "batching": None if block is None else dict(block)}


def _counter_values(block: Mapping[str, Any] | None) -> tuple[dict[str, int | None], list[str]]:
    """The batching counters of one reading, and the ones it lacks.

    ``None`` (batching off, nothing sent) is a genuine zero for every
    counter. A reported block that lacks a counter is *not*: the key sets are
    the producer's (``obd_scheduler.BATCHING_COUNTERS``), so an absent key is
    a producer/consumer disagreement or an older payload, and reading it as 0
    passed as a verified "no batch failures in this capture" (R02.11). Such a
    counter comes back ``None`` and is named in the second element.
    """
    if block is None:
        zeros: dict[str, int | None] = {name: 0 for name in BATCHING_COUNTERS}
        return zeros | {f"mode22.{name}": 0 for name in MODE22_BATCHING_COUNTERS}, []
    raw_mode22 = block.get("mode22")
    mode22: Mapping[str, Any] = raw_mode22 if isinstance(raw_mode22, Mapping) else {}
    values: dict[str, int | None] = {}
    missing: list[str] = []
    for name in BATCHING_COUNTERS:
        if name in block:
            values[name] = int(block[name] or 0)
        else:
            values[name] = None
            missing.append(name)
    for name in MODE22_BATCHING_COUNTERS:
        key = f"mode22.{name}"
        if name in mode22:
            values[key] = int(mode22[name] or 0)
        else:
            values[key] = None
            missing.append(key)
    return values, missing


def _ratio(num: int | None, den: int | None) -> float | None:
    if num is None or not den:
        return None
    return round(num / den, 2)


def scope_batching(
    current: Mapping[str, Any] | None,
    baseline: Mapping[str, Any] | None,
    *,
    elapsed_s: float | None = None,
) -> tuple[dict[str, Any] | None, list[str]]:
    """The capture's batching block: arm-to-now deltas, never lifetime totals.

    Returns ``(block, notes)``. Counters are top-level and capture-scoped;
    ``status`` says whether they are ``verified`` (both readings, no restart),
    ``reset`` (a counter went backwards or the collector loop restarted — the
    capture's share is unknowable, so every counter is ``None``, never
    negative) or ``unknown`` (no arm-time reading). Current settings live under
    ``config``; the scheduler's cumulative block is kept verbatim under
    ``lifetime`` and is never the answer to "what happened in this capture".
    ``None`` means batching was off throughout.
    """
    recorded = bool(baseline and baseline.get("recorded"))
    start_block = (baseline or {}).get("batching") if recorded else None
    if current is None and start_block is None:
        return None, []
    now_values, now_missing = _counter_values(current)
    start_values, start_missing = _counter_values(start_block)
    missing = sorted(set(now_missing) | set(start_missing))
    notes: list[str] = []
    reset_counters: list[str] = []
    if missing:
        notes.append(
            "batching reading lacks counter(s) "
            + ", ".join(missing)
            + "; they are reported as null, not read as zero (an older payload, or the "
            "scheduler's batching keys and capture_quality disagree)."
        )
    if not recorded:
        status = UNKNOWN
        notes.append(
            "batching counters have no arm-time reading, so how many batch failures and "
            "desyncs fell inside this capture is unknown; batching.lifetime is cumulative "
            "since the collector started and is not this capture's count."
        )
    else:
        reset_counters = [
            name
            for name, value in now_values.items()
            if value is not None and (start := start_values[name]) is not None and value < start
        ]
        loop_then = (start_block or {}).get(BATCHING_LOOP_KEY)
        loop_now = (current or {}).get(BATCHING_LOOP_KEY)
        restarted = loop_then is not None and loop_now is not None and loop_then != loop_now
        if restarted and BATCHING_LOOP_KEY not in reset_counters:
            reset_counters.append(BATCHING_LOOP_KEY)
        status = RESET if reset_counters else VERIFIED
        if status == RESET:
            notes.append(
                "batching counters restarted during this capture (a daemon or collector "
                f"restart: {', '.join(reset_counters)}); the capture's batch failures and "
                "desyncs are unknowable from the pair and are reported as null."
            )
    deltas: dict[str, int | None] = {}
    for name, value in now_values.items():
        start = start_values[name]
        deltas[name] = (
            value - start
            if status == VERIFIED and value is not None and start is not None
            else None
        )
    top = {name: deltas[name] for name in BATCHING_COUNTERS}
    mode22 = {name: deltas[f"mode22.{name}"] for name in MODE22_BATCHING_COUNTERS}
    pids = top["batched_pids_requested"]
    reads_hz = (
        round(pids / elapsed_s, 2)
        if pids is not None and elapsed_s is not None and elapsed_s > 0
        else None
    )
    current_mode22 = (current or {}).get("mode22") or {}
    block: dict[str, Any] = {
        "scope": BATCHING_SCOPE_CAPTURE,
        "status": status,
        "reset_counters": reset_counters,
        **top,
        "mean_pids_per_batch": _ratio(pids, top["batched_requests"]),
        # Batched channel reads over the capture interval (pauses included).
        "channel_reads_per_second": reads_hz,
        "mode22": {
            **mode22,
            "mean_dids_per_batch": _ratio(mode22["members_requested"], mode22["batched_requests"]),
        },
        # Current settings, not counters: what the scheduler is configured
        # with now. Nothing here is a count.
        "config": {
            "max_pids": (current or {}).get("max_pids"),
            "mode22_max_dids": current_mode22.get("max_dids"),
            "known_response_widths": dict(current_mode22.get("known_response_widths") or {}),
        },
        "baseline": None if start_block is None else dict(start_block),
        "lifetime": None if current is None else dict(current),
    }
    return block, notes


def tuple_rate_bound_hz(
    rates: Mapping[str, float | None],
    required_tuple: list[str] | tuple[str, ...],
) -> float | None:
    """Upper bound on how often the whole tuple can be observed together.

    The slowest member, because a tuple cannot complete more often than its
    scarcest channel is sampled. ``None`` when any member never answered — an
    absent member means the tuple's real rate is zero, but "zero because the
    channel is missing" is a different finding from "slow", and the caller
    reports it as a missing-channel failure rather than as a rate.
    """
    if not required_tuple:
        return None
    values: list[float] = []
    for name in required_tuple:
        rate = rates.get(name.upper())
        if rate is None:
            return None
        values.append(float(rate))
    return min(values) if values else None


def effective_max_tuple_skew_s(profile: Mapping[str, Any]) -> float:
    """The profile's declared skew, or half a target-rate period, or the default."""
    declared = profile.get("max_tuple_skew_s")
    if declared is not None:
        return float(declared)
    target = profile.get("target_rate_hz")
    if target:
        return 0.5 / float(target)
    return DEFAULT_MAX_TUPLE_SKEW_S


def profile_requests(
    profile: Mapping[str, Any],
    *,
    did_channel_for: Callable[[str], str | None] | None = None,
) -> list[dict[str, Any]]:
    """Synthesize the scheduler's ``requests`` rows from a profile snapshot.

    For the recompute path, where no live scheduler exists. ``AUTO`` cannot be
    enumerated from the profile; the caller supplies the observed channels.
    """
    rows: list[dict[str, Any]] = []
    if profile.get("mode01_enabled", True):
        for name in profile.get("mode01") or {}:
            upper = str(name).upper()
            if upper == "AUTO":
                continue
            rows.append(
                {
                    "channel": upper,
                    "profile_channel": upper,
                    "kind": "pid",
                    "capture_selected": True,
                }
            )
    if profile.get("mode22_enabled", True):
        for name in profile.get("mode22") or ():
            resolved = did_channel_for(str(name)) if did_channel_for is not None else None
            rows.append(
                {
                    "channel": resolved or str(name).upper(),
                    "profile_channel": str(name),
                    "kind": "did",
                    "capture_selected": True,
                }
            )
        # Memory reads share the Mode 22 session and its enable flag. One row
        # per selected channel, keyed as the rows store it (``mem:<NAME>``),
        # so a verdict rebuilt from rows counts what the reads decoded.
        for name in profile.get("memory") or ():
            channel = f"{MEMORY_READING_PREFIX}{name}"
            rows.append(
                {
                    "channel": channel,
                    "profile_channel": str(name),
                    "kind": "memory",
                    "decoded_channels": [channel],
                    "capture_selected": True,
                }
            )
    return rows


def kline_requests(profile: Mapping[str, Any]) -> list[dict[str, Any]]:
    """One request row per ``kline:`` entry of the profile, keyed as the
    collector observes it. No ``configured_rate_hz``: the profile names no
    K-line rate, so the achieved one is reported and never judged a
    shortfall."""
    rows: list[dict[str, Any]] = []
    for entry in profile.get("kline") or ():
        key = capture_key(str(entry))
        rows.append(
            {
                "channel": key,
                "profile_channel": key,
                "kind": KLINE_KIND,
                "capture_selected": True,
            }
        )
    return rows


def kline_pause_windows(lifetime: Mapping[str, Any] | None, elapsed: float) -> Windows | None:
    """The pause windows that are pauses for a K-line channel.

    Every window opened by CANary's own K-line session is K-line acquisition
    time, so it is kept out; a window that *also* names another reason is
    kept out too, because the ledger merges overlapping reasons into one
    window and its K-line part cannot be told apart. **Known cost:** passive
    armed mid-session joins the open K-line window, which then stays open
    until passive disarms, so the whole passive tail after the session counts
    as eligible K-line time. The silence there is an ``acquisition_gap`` — a
    hard clause — and the capture's verdict turns ``fail`` (or ``degraded``
    if coverage stays above ``DEGRADED_MIN_COVERAGE``). It errs toward
    reporting a gap, never toward hiding one; the fix is per-reason window
    bounds in ``capture_lifetime``.
    """
    described = [
        window
        for window in (lifetime or {}).get("pause_reasons") or []
        if isinstance(window, Mapping)
    ]
    if not described and (lifetime or {}).get("pause_windows"):
        # Windows without their reasons: which is K-line's own cannot be
        # told, so every window stays a pause, as for any other channel.
        return None
    out: Windows = []
    for window in described:
        if KLINE_SESSION_PAUSE in (window.get("reasons") or ()):
            continue
        start = float(window.get("start_s") or 0.0)
        end = window.get("end_s")
        out.append((start, float(end) if end is not None else elapsed))
    return out


def gap_threshold_s(requested_hz: float | None, median_interarrival_s: float | None) -> float:
    """Silence longer than this is a coverage gap, not a slow rate."""
    if requested_hz:
        return max(STALL_PERIODS / float(requested_hz), MIN_STALL_SECONDS)
    if median_interarrival_s:
        return max(STALL_PERIODS * float(median_interarrival_s), MIN_STALL_SECONDS)
    return MIN_STALL_SECONDS


def coverage_from_lifetime(
    row: Mapping[str, Any],
    *,
    elapsed_s: float,
    eligible_s: float,
    pause_windows: Windows,
    threshold_s: float,
) -> dict[str, Any]:
    """Pause-aware coverage for one channel's lifetime row.

    Intentional pauses are subtracted from every gap — that is the one
    exclusion the contract makes, and it is made here, explicitly. An
    engine-off stretch with no pause recorded is a gap like any other.
    """
    observations = int(row.get("observations") or 0)
    first = row.get("first_observed_at")
    last = row.get("last_observed_at")
    gaps: list[tuple[float, float]] = []
    if first is None:
        gaps.append((0.0, elapsed_s))
    else:
        gaps.append((0.0, float(first)))
        if last is not None:
            gaps.append((float(last), elapsed_s))
    long_gaps = [(float(a), float(b)) for a, b in (row.get("long_gaps") or [])]
    adjusted_long = [(a, b, subtract_windows(a, b, pause_windows)) for a, b in long_gaps]
    adjusted_initial = subtract_windows(gaps[0][0], gaps[0][1], pause_windows)
    adjusted_final = (
        subtract_windows(gaps[1][0], gaps[1][1], pause_windows) if len(gaps) > 1 else 0.0
    )
    over = [(a, b, s) for a, b, s in adjusted_long if s > threshold_s]
    uncovered = (
        (adjusted_initial if adjusted_initial > threshold_s else 0.0)
        + (adjusted_final if adjusted_final > threshold_s else 0.0)
        + sum(s for _a, _b, s in over)
    )
    # Interarrivals under LONG_GAP_SECONDS are not individually recorded (the
    # ledger keeps only their maximum); anything longer is in ``long_gaps``
    # and has been pause-adjusted above, so the raw maximum must not be
    # re-introduced unadjusted.
    max_interarrival = row.get("max_interarrival_s")
    sub_threshold = (
        [float(max_interarrival)]
        if max_interarrival is not None and float(max_interarrival) < LONG_GAP_SECONDS
        else []
    )
    max_gap = max(
        [adjusted_initial, adjusted_final, *(s for _a, _b, s in adjusted_long), *sub_threshold]
    )
    return {
        "observations": observations,
        "first_observed_at": first,
        "last_observed_at": last,
        "missing_initial_s": round(adjusted_initial, 3),
        "missing_final_s": round(adjusted_final, 3),
        "max_gap_s": round(max_gap, 3),
        "long_gaps": [[round(a, 3), round(b, 3), round(s, 3)] for a, b, s in over],
        "gap_threshold_s": round(threshold_s, 3),
        "uncovered_s": round(uncovered, 3),
        "eligible_s": round(eligible_s, 3),
        "fraction": None if eligible_s <= 0 else round(max(0.0, 1.0 - uncovered / eligible_s), 4),
        "clock_regressions": int(row.get("clock_regressions") or 0),
    }


def cadence_from_lifetime(row: Mapping[str, Any]) -> dict[str, Any]:
    median = row.get("median_interarrival_s")
    hz = None if not median else round(1.0 / float(median), 3)
    return {
        "median_interarrival_s": median,
        "p95_interarrival_s": row.get("p95_interarrival_s"),
        "hz": hz,
        "samples": int(row.get("interarrival_samples") or 0),
    }


def _channel_status(
    requested: float | None, cadence: Mapping[str, Any], coverage: Mapping[str, Any]
) -> str:
    if int(coverage.get("observations") or 0) == 0:
        return "missing"
    hz = cadence.get("hz")
    if hz is not None and requested is not None and float(hz) < float(requested) * RATE_TOLERANCE:
        return "shortfall"
    if float(coverage.get("max_gap_s") or 0.0) > float(coverage.get("gap_threshold_s") or 0.0):
        return "gap"
    return "ok"


def _requested_hz(
    request: Mapping[str, Any], upper: str, weights: Mapping[str, float], target: float | None
) -> float | None:
    # The weight is part of the promise: under a target rate a 0.25-weight
    # context channel is *asked* for a quarter of it, so holding it to the
    # full target would report a shortfall the profile never claimed.
    if target is not None and upper in weights:
        return float(target) * weights[upper]
    if target is not None and request.get("kind") == "did":
        return float(target)
    rate = request.get("configured_rate_hz")
    return None if rate is None else float(rate)


def _interval(lifetime: Mapping[str, Any] | None) -> tuple[dict[str, Any], float, float, Windows]:
    if lifetime is None:
        return (
            {
                "source": "unavailable",
                "elapsed_s": None,
                "intended_active_s": None,
                "paused_s": None,
                "pause_windows": [],
            },
            0.0,
            0.0,
            [],
        )
    elapsed = float(lifetime.get("elapsed_s") or 0.0)
    windows: Windows = [
        (float(a), float(b if b is not None else elapsed))
        for a, b in (lifetime.get("pause_windows") or [])
    ]
    paused = sum(b - a for a, b in windows)
    active = float(lifetime.get("intended_active_s") or max(0.0, elapsed - paused))
    started_wall = lifetime.get("started_wall")
    ended_wall = lifetime.get("ended_wall")
    if ended_wall is None and started_wall is not None:
        ended_wall = float(started_wall) + elapsed
    return (
        {
            "source": lifetime.get("source", "live"),
            # The declared interval on the wall clock, persisted so a later
            # recompute scores the same interval instead of guessing the end
            # from the last row (which cannot see a missing tail).
            "started_wall": None if started_wall is None else float(started_wall),
            "ended_wall": None if ended_wall is None else float(ended_wall),
            "elapsed_s": round(elapsed, 3),
            "intended_active_s": round(active, 3),
            "paused_s": round(paused, 3),
            "pause_windows": [[round(a, 3), round(b, 3)] for a, b in windows],
            "pause_reasons": list(lifetime.get("pause_reasons") or []),
            "generation_at_start": lifetime.get("generation_at_start"),
        },
        elapsed,
        active,
        windows,
    )


def _scored_channels(
    request: Mapping[str, Any], memory_selection: tuple[str, ...]
) -> list[tuple[str, str]]:
    """``(lifetime channel, profile name)`` pairs one plan member is scored as.

    One for most kinds. A memory member is a *read* named after its window
    (``mem:fuel_target``), while the ledger counts what the read decoded,
    keyed ``mem:<NAME>``; a window name matches no row, so scoring the read
    under it reported every window ``missing`` although every channel in it
    was answered (D03.38). It is scored once per decoded channel instead: the
    member's ``decoded_channels`` (plans written since D03.38), else the
    profile's whole ``memory:`` selection. Old plans carried one memory member
    at identifier 0 standing for every window, so the selection is the right
    expansion for them too.
    """
    channel = str(request.get("channel") or "")
    if request.get("kind") != "memory":
        return [(channel, str(request.get("profile_channel") or channel))]
    decoded = [str(name) for name in request.get("decoded_channels") or ()]
    if not decoded:
        decoded = [f"{MEMORY_READING_PREFIX}{name}" for name in memory_selection]
    return [(name, name.removeprefix(MEMORY_READING_PREFIX)) for name in decoded]


def _lifetime_row(
    lifetime_channels: Mapping[str, Mapping[str, Any]], channel: str, kind: Any
) -> Mapping[str, Any]:
    row = lifetime_channels.get(channel) or lifetime_channels.get(channel.upper())
    if not row and kind == "memory":
        # A sentinel is an answer ("the ECU commands nothing"), stored on its
        # own state channel; a read that only ever returned it was answered.
        # The ledger now also counts a sentinel against its value channel
        # (``capture_lifetime.observed_channels``, D03.69), so this serves
        # lifetimes persisted before that.
        row = lifetime_channels.get(f"{channel}{MEMORY_SENTINEL_SUFFIX}")
    return row or {}


def _request_ledger_block(
    lifetime: Mapping[str, Any] | None,
) -> tuple[Mapping[str, Mapping[str, Any]] | None, dict[str, Any]]:
    """The lifetime's per-request ledger and the block the report persists.

    ``(None, unavailable)`` when the lifetime carries none: an older sidecar's
    rebuild, or a scorer handed no ledger. Every gap is then ``unknown``,
    never ``not_sent``.
    """
    raw = (lifetime or {}).get("requests")
    if not isinstance(raw, Mapping):
        return None, {
            "source": LEDGER_UNAVAILABLE,
            "event_capacity": None,
            "requests": {},
            "reason": "no request ledger was recorded for this capture",
        }
    return raw, {
        "source": str((lifetime or {}).get("requests_source") or LEDGER_LIVE),
        "event_capacity": (lifetime or {}).get("request_event_capacity"),
        "requests": {str(name): dict(row) for name, row in sorted(raw.items())},
    }


def _request_row(
    ledger: Mapping[str, Mapping[str, Any]], request: Mapping[str, Any], channel: str
) -> Mapping[str, Any] | None:
    """The request ledger's row for ``request`` (found through the plan), else
    the row whose decoded ``members`` name ``channel``."""
    row = ledger.get(str(request.get("channel") or ""))
    if row is not None:
        return row
    wanted = channel.upper()
    for candidate in ledger.values():
        if wanted in {str(name).upper() for name in candidate.get("members") or ()}:
            return candidate
    return None


def attribute_gap(
    start: float,
    end: float,
    *,
    ledger: Mapping[str, Mapping[str, Any]] | None,
    row: Mapping[str, Any] | None,
    kind: Any = None,
    slack_s: float = 0.0,
) -> dict[str, Any]:
    """Which kind of silence one long gap ``[start, end]`` was (D03.70).

    ``ledger`` is the capture's per-request ledger (``None``: none was
    recorded) and ``row`` this channel's request in it (``None``: the request
    was never sent). Never a guess: a miss inside the gap is
    ``sent_and_missed`` (listed by kind and NRC); no miss and a send timeline
    with a gap covering it is ``not_sent``; no miss but sends continuing
    through it is ``sent_no_result``; anything the ledger cannot cover is
    ``unknown`` with the reason.
    """
    out: dict[str, Any] = {"start": round(start, 3), "end": round(end, 3)}
    if kind == KLINE_KIND:
        return {
            **out,
            "cause": GAP_UNKNOWN,
            "reason": "K-line channels are not requests of the OBD scheduler's ledger",
        }
    if ledger is None:
        return {
            **out,
            "cause": GAP_UNKNOWN,
            "reason": "no request ledger was recorded for this capture "
            "(a sidecar from before D03.70, or a verdict rebuilt from rows)",
        }
    if row is None:
        return {
            **out,
            "cause": GAP_NOT_SENT,
            "reason": "no send of this channel's request was recorded in the whole capture",
        }
    inside = [
        event
        for event in row.get("events") or ()
        if start - slack_s <= float(event["at_s"]) <= end + slack_s
    ]
    if inside:
        by_kind: dict[str, int] = {}
        for event in inside:
            by_kind[str(event["kind"])] = by_kind.get(str(event["kind"]), 0) + 1
        return {
            **out,
            "cause": GAP_SENT_AND_MISSED,
            "request": row.get("channel"),
            "misses_total": len(inside),
            "misses_by_kind": dict(sorted(by_kind.items())),
            "misses": [dict(event) for event in inside[:GAP_CAUSE_MISSES_LISTED]],
        }
    complete_until = row.get("events_complete_until_s")
    if row.get("events_dropped") and (complete_until is None or end > float(complete_until)):
        return {
            **out,
            "cause": GAP_UNKNOWN,
            "request": row.get("channel"),
            "reason": f"the request's miss list overflowed ({row.get('events_dropped')} "
            f"dropped after {complete_until}s) before this gap ended",
        }
    if "attempt_long_gaps" not in row:
        return {
            **out,
            "cause": GAP_UNKNOWN,
            "request": row.get("channel"),
            "reason": "the request ledger recorded no send timeline (an older ledger)",
        }
    # No miss in the gap. Whether the request was sent is the send timeline's
    # answer, not an inference from the missing miss: a send that left no miss
    # and no observation is still a send. A send precedes the observation it
    # produces, so the stretch with no sends opens at or before the gap's start
    # (the clock slack only: 0 live) and ends within one response turnaround
    # before its end (``ATTEMPT_EDGE_SLACK_S``, or the clock slack if larger).
    closing = max(slack_s, ATTEMPT_EDGE_SLACK_S)
    covering = [
        (float(a), float(b))
        for a, b in row.get("attempt_long_gaps") or ()
        if float(a) <= start + slack_s and float(b) >= end - closing
    ]
    if covering:
        (sent_from, sent_again) = covering[0]
        return {
            **out,
            "cause": GAP_NOT_SENT,
            "request": row.get("channel"),
            "reason": f"no send of the request between {sent_from:.3f}s and {sent_again:.3f}s",
        }
    dropped = int(row.get("attempt_gaps_dropped") or 0)
    if dropped:
        return {
            **out,
            "cause": GAP_UNKNOWN,
            "request": row.get("channel"),
            "reason": f"the request's send timeline overflowed ({dropped} long gaps dropped); "
            "none kept covers this gap",
        }
    return {
        **out,
        "cause": GAP_SENT_NO_RESULT,
        "request": row.get("channel"),
        "reason": "the request was sent inside the gap but no miss and no answer was recorded "
        "(a send that raised on the bus, a window left undecoded, or rows the writer lost)",
    }


def _build_channels(  # noqa: PLR0913
    requests: list[Mapping[str, Any]],
    *,
    lifetime_channels: Mapping[str, Mapping[str, Any]],
    weights: Mapping[str, float],
    target: float | None,
    required: list[str],
    elapsed: float,
    active: float,
    windows: Windows,
    trailing_hz: Mapping[str, Any],
    kline_windows: Windows | None = None,
    memory_selection: tuple[str, ...] = (),
    request_ledger: Mapping[str, Mapping[str, Any]] | None = None,
    gap_slack_s: float = 0.0,
) -> list[ChannelQuality]:
    channels: list[ChannelQuality] = []
    seen: set[str] = set()
    kline_paused = kline_windows if kline_windows is not None else windows
    kline_active = max(0.0, elapsed - sum(b - a for a, b in kline_paused))
    for request in requests:
        if not request.get("capture_selected", False):
            continue
        if request.get("kind") in EXCLUDED_KINDS or request.get("poll_tier") in EXCLUDED_TIERS:
            continue
        for channel, profile_channel in _scored_channels(request, memory_selection):
            # Two requests can resolve to one DID (a catalogue alias and the
            # profile's own name both map to 0x1209), which listed it twice.
            if channel.upper() in seen:
                continue
            seen.add(channel.upper())
            upper = profile_channel.upper()
            requested = _requested_hz(request, upper, weights, target)
            row = _lifetime_row(lifetime_channels, channel, request.get("kind"))
            cadence = cadence_from_lifetime(row)
            is_kline = request.get("kind") == KLINE_KIND
            coverage = coverage_from_lifetime(
                row,
                elapsed_s=elapsed,
                eligible_s=kline_active if is_kline else active,
                pause_windows=kline_paused if is_kline else windows,
                threshold_s=gap_threshold_s(requested, cadence.get("median_interarrival_s")),
            )
            ledger_row = (
                None if request_ledger is None else _request_row(request_ledger, request, channel)
            )
            coverage["long_gap_causes"] = [
                attribute_gap(
                    float(a),
                    float(b),
                    ledger=request_ledger,
                    row=ledger_row,
                    kind=request.get("kind"),
                    slack_s=gap_slack_s,
                )
                for a, b, _s in coverage["long_gaps"]
            ]
            channels.append(
                ChannelQuality(
                    # Reported under the profile's name, not the transport's:
                    # "ECM_AIR_CHARGE" is what the operator selected,
                    # "DID_1208" is what they then have to look up.
                    channel=profile_channel,
                    transport_channel=channel,
                    kind=request.get("kind"),
                    requested_hz=requested,
                    status=_channel_status(requested, cadence, coverage),
                    cadence=cadence,
                    coverage=coverage,
                    in_required_tuple=upper in required,
                    trailing_hz=trailing_hz.get(channel),
                )
            )
    return channels


def _channel_failures(channels: list[ChannelQuality], floor: float | None) -> list[dict[str, Any]]:
    failures: list[dict[str, Any]] = []
    missing = [row for row in channels if row.status == "missing"]
    if missing:
        failures.append(
            {
                "clause": UNANSWERED_INPUT,
                "channels": [row.channel for row in missing],
                "evidence": {row.channel: {"observations": 0} for row in missing},
                "detail": "requested for the whole interval, never answered",
            }
        )
    short = [row for row in channels if row.status == "shortfall"]
    if floor is not None:
        short += [
            row
            for row in channels
            if row.status == "ok"
            and row.requested_hz is not None
            and row.requested_hz >= float(floor)
            and row.cadence.get("hz") is not None
            and float(row.cadence["hz"]) < float(floor)
        ]
    if short:
        failures.append(
            {
                "clause": RATE_SHORTFALL,
                "channels": [row.channel for row in short],
                "evidence": {
                    row.channel: {
                        "requested_hz": row.requested_hz,
                        "floor_hz": floor,
                        "cadence_hz": row.cadence.get("hz"),
                        "median_interarrival_s": row.cadence.get("median_interarrival_s"),
                        "p95_interarrival_s": row.cadence.get("p95_interarrival_s"),
                    }
                    for row in short
                },
                "detail": "median interarrival below the requested cadence while observing",
            }
        )
    gapped = [row for row in channels if row.status == "gap"]
    if gapped:
        failures.append(
            {
                "clause": ACQUISITION_GAP,
                "channels": [row.channel for row in gapped],
                "evidence": {
                    row.channel: {
                        "max_gap_s": row.coverage.get("max_gap_s"),
                        "gap_threshold_s": row.coverage.get("gap_threshold_s"),
                        "missing_initial_s": row.coverage.get("missing_initial_s"),
                        "missing_final_s": row.coverage.get("missing_final_s"),
                        "long_gaps": row.coverage.get("long_gaps"),
                        "long_gap_causes": row.coverage.get("long_gap_causes"),
                        "coverage_fraction": row.coverage.get("fraction"),
                    }
                    for row in gapped
                },
                "detail": "on-cadence while observing, but silent for longer than the threshold",
            }
        )
    return failures


def _tuple_block(  # noqa: PLR0913
    tuples: Mapping[str, Any] | None,
    *,
    required: list[str],
    max_skew_s: float,
    bound: float | None,
    elapsed: float,
    active: float,
    windows: Windows,
    floor: float | None,
) -> tuple[dict[str, Any], list[dict[str, Any]], list[str]]:
    """The yield block and its failures. Counts, never a bound in disguise."""
    notes: list[str] = []
    block: dict[str, Any] = {
        "required": list(required),
        "max_tuple_skew_s": max_skew_s,
        "rate_bound_hz": bound,
        "source": "unavailable",
        "scheduled": None,
        "attempted": None,
        "complete": None,
        "persisted": None,
        "incomplete": None,
        "skew_rejected": None,
        "member_misses": {},
        "cadence": {},
        "coverage": {},
        "skew": {},
    }
    if not required:
        block["source"] = "no_required_tuple"
        return block, [], notes
    if tuples is None or tuples.get("source") in (None, "unavailable", "no_round_evidence"):
        reason = (tuples or {}).get("reason") or "no persisted round evidence was available"
        block["source"] = (tuples or {}).get("source") or "unavailable"
        block["reason"] = reason
        notes.append(f"tuple yield unverified: {reason}")
        return block, [], notes

    for key in (
        "scheduled",
        "attempted",
        "complete",
        "persisted",
        "incomplete",
        "skew_rejected",
        "member_misses",
        "skew",
        "reason",
    ):
        if key in tuples:
            block[key] = tuples[key]
    block["source"] = tuples.get("source")
    lifetime_row = tuples.get("lifetime") or {}
    cadence = cadence_from_lifetime(lifetime_row)
    threshold = gap_threshold_s(floor, cadence.get("median_interarrival_s"))
    coverage = coverage_from_lifetime(
        lifetime_row,
        elapsed_s=elapsed,
        eligible_s=active,
        pause_windows=windows,
        threshold_s=threshold,
    )
    block["cadence"] = cadence
    block["coverage"] = coverage
    failures: list[dict[str, Any]] = []
    attempted = int(tuples.get("attempted") or 0)
    complete = int(tuples.get("complete") or 0)
    skew_rejected = int(tuples.get("skew_rejected") or 0)
    by_members = max(0, attempted - complete - skew_rejected)
    evidence = {
        "attempted": attempted,
        "complete": complete,
        "persisted": tuples.get("persisted"),
        "incomplete": tuples.get("incomplete"),
        "skew_rejected": skew_rejected,
        "member_misses": tuples.get("member_misses"),
        "max_tuple_skew_s": max_skew_s,
        "skew": tuples.get("skew"),
    }
    if attempted > 0 and skew_rejected > 0 and skew_rejected >= max(complete, by_members):
        failures.append(
            {
                "clause": TUPLE_SKEW,
                "channels": list(required),
                "evidence": evidence,
                "detail": "members answered in the same round but further apart than the "
                "declared acquisition skew",
            }
        )
    elif attempted > 0 and complete == 0:
        failures.append(
            {
                "clause": TUPLE_INCOMPLETE,
                "channels": list(required),
                "evidence": evidence,
                "detail": "every round missed at least one member; no complete tuple persisted",
            }
        )
    elif attempted == 0:
        failures.append(
            {
                "clause": TUPLE_INCOMPLETE,
                "channels": list(required),
                "evidence": evidence,
                "detail": "no round contained any required member",
            }
        )
    if complete > 0:
        if floor is not None and cadence.get("hz") is not None and float(cadence["hz"]) < floor:
            failures.append(
                {
                    "clause": RATE_SHORTFALL,
                    "channels": [TUPLE_CHANNEL],
                    "evidence": {"floor_hz": floor, **cadence, **evidence},
                    "detail": "complete tuples recur below the profile's floor",
                }
            )
        if float(coverage["max_gap_s"]) > threshold:
            failures.append(
                {
                    "clause": ACQUISITION_GAP,
                    "channels": [TUPLE_CHANNEL],
                    "evidence": {**coverage, **evidence},
                    "detail": "complete tuples absent for longer than the threshold",
                }
            )
    return block, failures, notes


def _loss_failures(losses: Mapping[str, Any]) -> list[dict[str, Any]]:
    recording = losses.get(RECORDING) or {}
    failures: list[dict[str, Any]] = []
    counters = recording.get("counters") or {}
    lost = {
        name: row
        for name, row in counters.items()
        if row.get("status") == VERIFIED and row["delta"]
    }
    if lost:
        failures.append(
            {
                "clause": PERSISTENCE_LOSS,
                "channels": sorted(lost),
                "evidence": lost,
                "detail": "recording-path counters advanced during this capture",
            }
        )
    if recording.get("status") != VERIFIED:
        failures.append(
            {
                "clause": UNKNOWN_LOSS_COUNTERS,
                "channels": list(recording.get("unverified") or []),
                "evidence": {
                    name: row for name, row in counters.items() if row.get("status") != VERIFIED
                },
                "detail": "no verified no-loss claim: these counters are unknown or reset",
            }
        )
    return failures


def _persistence_failures(persistence: Mapping[str, Any] | None) -> list[dict[str, Any]]:
    if persistence is None:
        return []
    evidence: dict[str, Any] = {}
    if persistence.get("flush_error"):
        evidence["flush_error"] = persistence["flush_error"]
    timed_out = list(persistence.get("drain_timed_out") or [])
    if timed_out:
        evidence["drain_timed_out"] = timed_out
    written = persistence.get("rows_written")
    on_disk = persistence.get("rows_on_disk")
    if written is not None and on_disk is not None and int(on_disk) < int(written):
        evidence["rows_written"] = written
        evidence["rows_on_disk"] = on_disk
    failures: list[dict[str, Any]] = []
    if evidence:
        failures.append(
            {
                "clause": PERSISTENCE_LOSS,
                "channels": [],
                "evidence": evidence,
                "detail": "the final records' persistence outcome is not a clean flush",
            }
        )
    unobserved = list(persistence.get("drain_unobserved") or [])
    if unobserved:
        # A queue the finalizer could not watch is not a queue known to be
        # empty: the outcome is unverified, never a clean pass.
        failures.append(
            {
                "clause": UNKNOWN_LOSS_COUNTERS,
                "channels": unobserved,
                "evidence": {"drain_unobserved": unobserved},
                "detail": "these queues were never registered with health, so their "
                "drain at stop could not be observed",
            }
        )
    return failures


def _verdict(
    *,
    elapsed: float,
    channels: list[ChannelQuality],
    failures: list[dict[str, Any]],
    notes: list[str],
) -> str:
    if elapsed < MIN_VERIFY_SECONDS:
        notes.append(
            f"observed for {elapsed:.1f}s, under the {MIN_VERIFY_SECONDS:g}s minimum — too "
            "short to tell a slow channel from an unlucky one."
        )
        return "unverified"
    if not channels:
        notes.append("no capture-selected requests were reported, so nothing could be checked.")
        return "unverified"
    hard = [f for f in failures if f["clause"] in HARD_CLAUSES]
    if hard:
        gaps_only = all(f["clause"] == ACQUISITION_GAP for f in hard)
        by_name = {row.channel: row for row in channels}
        covered = all(
            (by_name[c].coverage.get("fraction") or 0.0) >= DEGRADED_MIN_COVERAGE
            for f in hard
            for c in f["channels"]
            if c in by_name
        )
        if gaps_only and covered:
            notes.append(
                "degraded, not failed: every gapped channel still covered at least "
                f"{DEGRADED_MIN_COVERAGE:.0%} of the interval; the gaps are named "
                "in the acquisition_gap evidence."
            )
            return "degraded"
        return "fail"
    if failures or any(note.startswith("tuple yield unverified") for note in notes):
        return "unverified"
    return "pass"


def evaluate_capture_quality(  # noqa: PLR0913
    *,
    profile: Mapping[str, Any],
    scheduler: Mapping[str, Any] | None = None,
    lifetime: Mapping[str, Any] | None = None,
    tuples: Mapping[str, Any] | None = None,
    losses: Mapping[str, Any] | None = None,
    persistence: Mapping[str, Any] | None = None,
    requests: list[Mapping[str, Any]] | None = None,
    scope: str = SCOPE_LIVE,
    requested_rate_hz: float | None = None,
    batching_start: Mapping[str, Any] | None = None,
) -> CaptureQuality:
    """Score a capture against its profile's promises.

    * ``profile`` — a ``tuning_profile.snapshot()`` / manifest snapshot.
    * ``scheduler`` — the live ``/status`` ``health.obd_scheduler`` payload:
      the request table and the *trailing* rates. Trailing numbers are
      reported, never scored.
    * ``lifetime`` — ``capture_lifetime.CaptureLifetime.to_dict()`` (live)
      or the same shape rebuilt from persisted rows (recompute). This is what
      the verdict is scored on.
    * ``tuples`` — persisted round-level tuple counts
      (``src.analysis.capture_coverage.count_persisted_tuples``).
    * ``losses`` — ``capture_losses.summarize_losses`` output; ``None`` means
      no epochs, which is reported as unknown.
    * ``persistence`` — the finalization outcome (flush, drain, rows).
    * ``requests`` — overrides ``scheduler["requests"]`` for the recompute
      path, where no scheduler exists.
    * ``batching_start`` — :func:`batching_baseline` taken at arm. The
      scheduler's ``batching`` counters are cumulative; the report carries
      their arm-to-now delta (:func:`scope_batching`), and without a baseline
      says the capture's share is unknown rather than quoting the total.
    """
    name = str(profile.get("name") or "unknown")
    weights = {
        str(k).upper(): float(v)
        for source in (
            profile.get("mode01") or {},
            profile.get("mode22_rate_weights") or {},
        )
        for k, v in source.items()
    }
    target = requested_rate_hz if requested_rate_hz is not None else profile.get("target_rate_hz")
    target_f = None if target is None else float(target)
    required = [str(item).upper() for item in (profile.get("required_tuple") or [])]
    floor = (
        None
        if profile.get("min_achieved_rate_hz") is None
        else float(profile["min_achieved_rate_hz"])
    )
    scheduler = scheduler or {}
    interval, elapsed, active, windows = _interval(lifetime)
    lifetime_channels = (lifetime or {}).get("channels") or {}
    trailing_hz = scheduler.get("achieved_hz_capture") or {}

    plan_members = (scheduler.get("resolved_plan") or {}).get("members") or []
    base_requests = list(
        requests if requests is not None else plan_members or scheduler.get("requests") or []
    )
    # K-line entries are added here, once, for every caller (live, stop,
    # recompute): no OBD scheduler plan or request table lists them (H05.8).
    kline_rows = kline_requests(profile)
    notes: list[str] = []
    if kline_rows and (lifetime or {}).get("source") == ROWS_LIFETIME_SOURCE:
        notes.append(
            f"{len(kline_rows)} K-line channel(s) not scored: this verdict was rebuilt from "
            "persisted rows, which cover Mode 01/22 only; the live and stop-time verdicts "
            "score K-line from the capture ledger."
        )
        kline_rows = []
    elif kline_rows:
        notes.append(
            "K-line channels are scored on the capture ledger: their cadence is the achieved "
            "K-line rate. The profile names no K-line rate, so none is judged a shortfall; "
            "CANary's own K-line session (ecm-session-kline) is acquisition time for them, "
            "not a pause."
        )
    listed = {str(row.get("channel") or "").upper() for row in base_requests}
    request_ledger_rows, request_ledger = _request_ledger_block(lifetime)
    # A ledger carried into a rebuild from rows is on the monotonic clock while
    # the rows' gaps are on the wall clock.
    rows_clock = (lifetime or {}).get("source") == ROWS_LIFETIME_SOURCE
    channels = _build_channels(
        base_requests + [row for row in kline_rows if row["channel"].upper() not in listed],
        lifetime_channels=lifetime_channels,
        weights=weights,
        target=target_f,
        required=required,
        elapsed=elapsed,
        active=active,
        windows=windows,
        trailing_hz=trailing_hz,
        kline_windows=kline_pause_windows(lifetime, elapsed),
        memory_selection=tuple(str(name) for name in profile.get("memory") or ()),
        request_ledger=request_ledger_rows,
        gap_slack_s=ROWS_CLOCK_SLACK_S if rows_clock else 0.0,
    )
    cadence_by_profile = {
        row.channel.upper(): (None if row.status == "missing" else row.cadence.get("hz"))
        for row in channels
    }
    bound = tuple_rate_bound_hz(cadence_by_profile, required)
    max_skew = effective_max_tuple_skew_s(profile)

    failures = _channel_failures(channels, floor)
    tuple_block, tuple_failures, tuple_notes = _tuple_block(
        tuples,
        required=required,
        max_skew_s=max_skew,
        bound=bound,
        elapsed=elapsed,
        active=active,
        windows=windows,
        floor=floor,
    )
    notes += tuple_notes
    # A member the ECU never answered is already an unanswered_input; the
    # tuple could never complete and saying so twice adds nothing.
    missing_members = {row.channel.upper() for row in channels if row.status == "missing"}
    if not (set(required) & missing_members):
        failures += tuple_failures
    loss_summary = dict(losses) if losses is not None else unknown_losses()
    failures += _loss_failures(loss_summary)
    failures += _persistence_failures(persistence)

    raw_batching = scheduler.get("batching")
    batching, batching_notes = scope_batching(raw_batching, batching_start, elapsed_s=elapsed)
    notes += batching_notes
    if scheduler and raw_batching is None:
        notes.append(
            "request batching was off or unreported. Unbatched, one request carries one PID, "
            "so the bus budget binds far sooner than the ECM does."
        )
    if not required:
        notes.append(
            "this profile declares no required_tuple, so only per-channel cadence and coverage "
            "were checked — which cannot tell whether any joint analysis has usable samples."
        )
    dashboard = loss_summary.get("dashboard") or {}
    if dashboard.get("lost"):
        notes.append(
            f"dashboard-only sinks dropped {dashboard['lost']} records during the capture; "
            "the freshest state wins there by design and nothing on disk is affected."
        )
    controller = loss_summary.get(CONTROLLER) or {}
    if controller.get("lost"):
        notes.append(
            f"the CAN controller overflowed {controller['lost']} frames during the capture "
            "(can0.rx_over_errors). Reported, not failed: any capture channel it cost shows up "
            "in that channel's cadence and coverage above."
        )
    if interval.get("paused_s"):
        notes.append(
            f"{interval['paused_s']:.1f}s of intentional pause excluded from coverage; "
            "engine-off time without a pause is not excluded."
        )

    verdict = _verdict(elapsed=elapsed, channels=channels, failures=failures, notes=notes)
    return CaptureQuality(
        verdict=verdict,
        profile=name,
        scope=scope,
        interval=interval,
        channels=channels,
        tuples=tuple_block,
        losses=loss_summary,
        persistence=None if persistence is None else dict(persistence),
        trailing={
            "window_s": scheduler.get("trailing_window_s", 60.0),
            "observed_seconds": scheduler.get("capture_observed_seconds"),
            "hz": dict(trailing_hz),
            "max_gap_seconds": dict(scheduler.get("capture_max_gap_seconds") or {}),
        },
        failures=failures,
        required_tuple=required,
        tuple_rate_bound_hz=bound,
        batching=batching,
        notes=notes,
        request_ledger=request_ledger,
    )


__all__ = [
    "ACQUISITION_GAP",
    "BATCHING_COUNTERS",
    "BATCHING_SCOPE_CAPTURE",
    "DEFAULT_MAX_TUPLE_SKEW_S",
    "GAP_NOT_SENT",
    "GAP_SENT_AND_MISSED",
    "GAP_SENT_NO_RESULT",
    "GAP_UNKNOWN",
    "HARD_CLAUSES",
    "LEDGER_CARRIED",
    "LEDGER_LIVE",
    "LEDGER_UNAVAILABLE",
    "MIN_STALL_SECONDS",
    "MIN_VERIFY_SECONDS",
    "MODE22_BATCHING_COUNTERS",
    "PERSISTENCE_LOSS",
    "RATE_SHORTFALL",
    "RATE_TOLERANCE",
    "STALL_PERIODS",
    "TUPLE_CHANNEL",
    "TUPLE_INCOMPLETE",
    "TUPLE_SKEW",
    "UNANSWERED_INPUT",
    "UNKNOWN_LOSS_COUNTERS",
    "CaptureQuality",
    "ChannelQuality",
    "attribute_gap",
    "batching_baseline",
    "cadence_from_lifetime",
    "coverage_from_lifetime",
    "effective_max_tuple_skew_s",
    "evaluate_capture_quality",
    "kline_pause_windows",
    "kline_requests",
    "gap_threshold_s",
    "profile_requests",
    "scope_batching",
    "tuple_rate_bound_hz",
]
