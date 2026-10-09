"""The mutually-exclusive Mode-22 window controller and its profile-start ledger.

Part of the ``obd_scheduler`` package (R02.10).
"""

from __future__ import annotations

import logging
import math
from collections.abc import Mapping, Sequence
from typing import TYPE_CHECKING, Any, Literal

from src.collection import tuning_profile
from src.collection.hybrid_cycle import (
    CycleAccounting,
    CyclePlan,
    ServiceKind,
)
from src.collection.isotp import nrc_name
from src.collection.obd_scheduler.requests import (
    PollRequest,
    PollRequestState,
)
from src.collection.obd_scheduler.spec import (
    DidSessionSpec,
    SessionControlResult,
)
from src.collection.request_eligibility import Eligibility, SessionPhase, evaluate

if TYPE_CHECKING:
    pass

log = logging.getLogger(__name__)


#: Consecutive *permanent* refusals before the Mode-22 fast exit is disabled.
#: Two, not one: the ECM that motivated this answered 139 of 140 exits
#: correctly, and one stray frame must never cost the rest of the drive.
FAST_EXIT_REFUSALS_BEFORE_DISABLE = 2


#: Disabling the fast exit is always provisional. Re-probe after this long,
#: doubling to the cap, so an ECU that refused while busy recovers on its own
#: instead of needing a daemon restart mid-drive.
FAST_EXIT_RETRY_MIN_SECONDS = 30.0


FAST_EXIT_RETRY_MAX_SECONDS = 300.0


#: Clear air handed to Mode 01 when a lapse ends with the next window already
#: overdue. Applied *only* in that case, so the fast-exit path — where the
#: window is still in the future when the exit grace ends — keeps its full
#: DID rate.
MODE01_MIN_SHARE_SECONDS = 0.5


#: Why a session-window member was not requested within one cycle of its
#: profile's start (D05.1). ``backoff``: a miss back-off still stands;
#: ``cadence``: its own due time had not come; ``no_window``: no Mode-22
#: window opened (entries failing, or the ECU silent); ``budget``: due and a
#: window opened, but the window or bus budget ran out before it.
WithheldReason = Literal["backoff", "cadence", "no_window", "budget"]


WITHHELD_REASONS: tuple[WithheldReason, ...] = ("backoff", "cadence", "no_window", "budget")


class ProfileStartLedger:
    """First request of every session-window member after a profile starts (D05.1).

    On 2026-09-23 six of eight `tuning` DIDs recorded nothing for 37.5 s and
    nothing said so: the journal was silent, and ``/status.deferred`` lists
    only requests that are *due*, so a DID held back by a stale due time was
    not in it. An empty channel must say which kind of empty.

    A start is any new capture generation within a running loop (a capture
    starting or stopping, pause/resume, the passive gate lifting, a replay
    transition). A collector reconnect is not one: it builds a new loop whose
    requests all start due, and its ledger starts resolved. The
    ledger is resolved one cycle (``window_max_seconds`` for an interval
    profile) after the first window *may* open — an ECU session lapse in
    progress is a protocol wait, not a withholding. Every member not requested
    by then is logged once, with its reason, and counted by reason.
    """

    def __init__(self) -> None:
        #: Members withheld at a start, by reason, since the loop started.
        self.withheld_total: dict[str, int] = dict.fromkeys(WITHHELD_REASONS, 0)
        self.starts = 0
        self._profile: str | None = None
        self._started_at = 0.0
        self._ready_at: float | None = None
        self._grace = 0.0
        self._windows_at_start = 0
        self._members: dict[int, str] = {}
        self._first: dict[int, float] = {}
        self._withheld: list[dict[str, Any]] = []
        self._resolved = True

    def begin(
        self,
        *,
        profile: str,
        members: Mapping[int, str],
        now: float,
        in_lapse: bool,
        grace: float,
        windows_opened: int,
    ) -> None:
        self.starts += 1
        self._profile = profile
        self._started_at = now
        self._ready_at = None if in_lapse else now
        self._grace = grace
        self._windows_at_start = windows_opened
        self._members = dict(members)
        self._first = {}
        self._withheld = []
        self._resolved = not self._members

    @property
    def profile(self) -> str | None:
        return self._profile

    @property
    def member_count(self) -> int:
        return len(self._members)

    def observe(
        self,
        states: Sequence[PollRequestState],
        now: float,
        *,
        in_lapse: bool,
        windows_opened: int,
    ) -> list[dict[str, Any]]:
        """Record first requests; return the withheld rows once, when resolved."""
        for index in self._members:
            if index in self._first:
                continue
            sent = states[index].last_requested_at
            if sent is not None and sent >= self._started_at:
                self._first[index] = sent
        if self._resolved:
            return []
        if self._ready_at is None:
            if in_lapse:
                return []
            self._ready_at = now
        if now < self._ready_at + self._grace:
            return []
        self._resolved = True
        opened = windows_opened > self._windows_at_start
        for index, channel in sorted(self._members.items(), key=lambda item: item[1]):
            if index in self._first:
                continue
            state = states[index]
            row: dict[str, Any] = {"channel": channel}
            if state.suppressed_until > now:
                row["reason"] = "backoff"
                row["for_s"] = (
                    None
                    if state.suppressed_until == float("inf")
                    else round(state.suppressed_until - now, 3)
                )
                row["missed_responses"] = state.missed_responses
            elif state.next_due_at > now:
                row["reason"] = "cadence"
                row["due_in_s"] = round(state.next_due_at - now, 3)
            elif not opened:
                row["reason"] = "no_window"
            else:
                row["reason"] = "budget"
            self._withheld.append(row)
            self.withheld_total[row["reason"]] += 1
        return list(self._withheld)

    def snapshot(self, now: float) -> dict[str, Any]:
        """``/status.health.obd_scheduler.did_profile.profile_start``."""
        return {
            "profile": self._profile,
            "starts": self.starts,
            "age_s": round(max(0.0, now - self._started_at), 3) if self.starts else None,
            "resolved": self._resolved,
            #: Seconds from the start to each member's first request; ``None``
            #: until it has gone out, then filled in however late it is.
            "first_request_s": {
                channel: (
                    round(self._first[index] - self._started_at, 3)
                    if index in self._first
                    else None
                )
                for index, channel in sorted(self._members.items(), key=lambda item: item[1])
            },
            "withheld": list(self._withheld),
            "withheld_total": dict(self.withheld_total),
        }


class DidWindowController:
    """Mutually-exclusive Mode-22 window scheduler (risk R8 / Task 6.15).

    Drives three phases inside ``run_can_obd_loop``:

    * ``mode01`` — normal polling; DID requests are ineligible. When the
      next window is due (and at least one DID isn't backed off) the loop
      sends ``10 <session>`` to open a window.
    * ``window`` — only DIDs are eligible; each active DID is read once.
      ``10 <session>`` is re-sent every ``keepalive_seconds``. The window
      closes when every pending DID was attempted, on keep-alive failure,
      or at ``window_max_seconds``.
    * ``lapse`` — nothing diagnostic is sent so the ECM's session expires
      (S3 timeout) and Mode 01 answers again — or, with a fast exit, the
      short post-exit grace. Broadcast capture continues throughout — only
      the request side pauses.

    Two timing policies (C04.3): with a ``cycle_seconds`` (spec or profile)
    windows open **start-to-start** at ``anchor + k * cycle`` and the Mode 01
    phase is reserved a share of each cycle; without one the historic
    **end-to-start** ``window_interval_seconds`` applies unchanged.

    Mode 01 misses are *prevented* (requests deferred, not timed out), so
    the 30 s auto-miss backoff never punishes channels for a DID window.
    """

    def __init__(
        self,
        spec: DidSessionSpec,
        did_indices: Sequence[int],
        *,
        now: float,
    ) -> None:
        self._spec = spec
        self._did_indices = frozenset(did_indices)
        self.phase: Literal["mode01", "window", "lapse"] = "mode01"
        self._next_window_at = now + self._interval()
        self._window_opened_at = 0.0
        #: When pacing would have closed the open window (``close_as_paced``).
        self._paced_close_at: float | None = None
        self._window_deadline = 0.0
        self._lapse_until = 0.0
        self._last_session_sent = 0.0
        self._pending: set[int] = set()
        self.windows_opened = 0
        self.windows_completed = 0
        self.session_failures = 0
        self.keepalives_sent = 0
        self.fast_exits = 0
        self.fast_exit_failures = 0
        #: A (re)connect cannot know whether the ECU is still holding the
        #: session a previous loop opened — the Mode 01 blackout outlives an
        #: S3 lapse by >30 s on the Xterra — so a vehicle with a verified fast
        #: exit is returned to the default session first (C04.7 reconnect).
        self._exit_due = spec.exit_session is not None
        #: Consecutive *permanent* refusals (serviceNotSupported and friends).
        #: Any success resets it — a busy ECU must not accumulate toward being
        #: declared incapable.
        self._exit_refusals = 0
        #: Monotonic time the fast exit may next be attempted. 0.0 = now.
        #: This replaces the old boolean latch, which never cleared and so
        #: turned one refused exit into a Mode-01 blackout for the whole drive.
        self._exit_retry_at = 0.0
        self._exit_retry_backoff = FAST_EXIT_RETRY_MIN_SECONDS
        #: NRC of the most recent failed exit, for `/status` and the log.
        self.last_exit_nrc: int | None = None
        #: One immediate re-send of a transiently failed exit per window
        #: (D03.18): a lost ``50 81`` otherwise costs the full ``lapse_seconds``
        #: of Mode-01 blackout. Armed by `_close_window` only.
        self._exit_retry_armed = False
        #: Whether the ECU has answered anything since the last exit sent for a
        #: silent entry. A key-off ECU answers nothing, so this bounds the
        #: extra exit to one per silence rather than one per window attempt —
        #: the same "ECU answered since" gate batching uses (D03.19).
        self._answered_since_unknown_exit = True
        #: The owed exit was scheduled by a silent entry, not a window close.
        self._unknown_exit_pending = False
        self.unknown_session_exits = 0
        self.fast_exit_retries = 0
        #: Cycle schedule (C04.3/C04.5): windows open at ``anchor + k*cycle``.
        self._cycle_anchor: float | None = None
        self._cycle_k = 0
        self._next_cycle_k = 0
        self._plan: CyclePlan | None = None
        self._infeasible_logged: CyclePlan | None = None
        self.cycle = CycleAccounting()
        #: First requests after each profile start, and what withheld any (D05.1).
        self.start_ledger = ProfileStartLedger()

    def advance(self, states: Sequence[PollRequestState], now: float) -> None:
        """Apply phase transitions that don't require bus I/O."""
        self._advance_phase(states, now)
        self._observe_start(states, now)

    def _observe_start(self, states: Sequence[PollRequestState], now: float) -> None:
        withheld = self.start_ledger.observe(
            states, now, in_lapse=self.phase == "lapse", windows_opened=self.windows_opened
        )
        if not withheld:
            return
        log.warning(
            "profile %s: %d of %d Mode-22 window members withheld at profile start: %s",
            self.start_ledger.profile,
            len(withheld),
            self.start_ledger.member_count,
            ", ".join(f"{row['channel']} ({row['reason']})" for row in withheld),
            extra={"withheld": withheld},
        )

    def _advance_phase(self, states: Sequence[PollRequestState], now: float) -> None:
        if self.phase == "window":
            if self._continuous():
                # Nothing to hand the bus back to, so the window never closes:
                # re-arm every DID that has come due instead. Closing would
                # cost a session re-entry and, worse, a lapse during which the
                # capture records nothing.
                self._pending = {i for i in self._did_indices if states[i].suppressed_until <= now}
                return
            live = any(states[i].suppressed_until <= now for i in self._pending)
            if now >= self._window_deadline or not live:
                self._close_window(now)
        elif self.phase == "lapse" and now >= self._lapse_until:
            self.phase = "mode01"
            if self._cycle() is not None:
                # Mode 01 gets its planned share of the cycle before the next
                # window may open, however overdue that window is (C04.5).
                share = self._plan.mode01_share_seconds if self._plan is not None else 0.0
                self._schedule_next_window(now, earliest=now + share)
            elif self._next_window_at <= now:
                # The lapse ate the whole inter-window gap, so the next window
                # is already overdue and would reopen before a single Mode 01
                # request could be issued — starving Mode 01 to *exactly* zero
                # rather than merely slowing it. Any `window_interval` below
                # `lapse_seconds` guarantees this, which is how the 0.25 s
                # hybrid profile lost Mode 01 entirely on 2026-08-07. Hand
                # Mode 01 a slice before reopening.
                self._next_window_at = now + MODE01_MIN_SHARE_SECONDS

    @staticmethod
    def _continuous() -> bool:
        return tuning_profile.did_continuous_enabled()

    def _interval(self) -> float:
        """Seconds between windows — the active capture profile wins.

        DID cadence is a sampling choice, not an ECU constraint (this ECM
        answers a DID in ~1.5 ms), so a capture that wants DIDs quickly says
        so and gets them, without a restart or a config edit. The precedence
        is ``CaptureProfile.did_window_interval``'s.
        """
        return tuning_profile.view().did_window_interval(self._spec)

    def _cycle(self) -> float | None:
        """Start-to-start cycle period, profile first; ``None`` = legacy interval."""
        return tuning_profile.view().did_cycle_seconds(self._spec)

    def cycle_mode(self) -> bool:
        return self._cycle() is not None

    def set_plan(self, plan: CyclePlan | None) -> None:
        """Record what the next cycle must fit; log an infeasible plan once."""
        self._plan = plan
        if plan is None or plan.feasible or plan == self._infeasible_logged:
            return
        self._infeasible_logged = plan
        log.warning(
            "hybrid cycle of %.3fs is infeasible: %d Mode 01 + %d DID + %d session requests "
            "at %.1f req/s plus %.3fs grace need %.3fs (short by %.3fs); reporting "
            "cycle_feasible=false rather than promising the target",
            plan.cycle_seconds,
            plan.mode01_requests,
            plan.did_requests,
            plan.session_requests,
            plan.budget_rps,
            plan.grace_seconds,
            plan.required_seconds,
            plan.shortfall_seconds,
            extra={"cycle_plan": plan.to_dict()},
        )

    def _schedule_next_window(self, now: float, *, earliest: float) -> None:
        """Next window at the first cycle boundary at or after ``earliest``.

        Boundaries that cannot be met are skipped and counted as missed
        cycles — never issued late as a catch-up burst (C04.5).
        """
        cycle = self._cycle()
        if cycle is None:
            self._next_window_at = max(now + self._interval(), earliest)
            return
        if self._cycle_anchor is None:
            # No grid yet: the first entry anchors one (``on_entry_sent``), so
            # the first window is due as soon as it may open. Falling through
            # to the interval policy here charged a cycle-mode profile the
            # config's ``window_interval_seconds`` — 60 s on the Xterra — for
            # its first window whenever a switch landed inside a `default`
            # window or its exit grace (D05.1).
            self._next_window_at = max(now, earliest)
            return
        k_next = max(self._next_cycle_k, self._cycle_k + 1)
        boundary = self._cycle_anchor + k_next * cycle
        if boundary < earliest:
            k_needed = math.ceil((earliest - self._cycle_anchor) / cycle - 1e-9)
            k_needed = max(k_needed, k_next)
            self.cycle.on_missed(k_needed - k_next)
            k_next = k_needed
            boundary = self._cycle_anchor + k_next * cycle
        self._next_cycle_k = k_next
        self._next_window_at = boundary

    def session_control_due(
        self, states: Sequence[PollRequestState], now: float, *, keepalive_lead: float = 0.0
    ) -> bool:
        """Whether the loop should send ``10 <session>`` now.

        ``keepalive_lead`` sends a window's keep-alive that much early, so it
        does not fall due inside a memory burst about to start (B08.4)."""
        if self.phase == "mode01":
            if now < self._next_window_at:
                return False
            due = self._due_dids(states, now)
            if not due:
                # Every DID is backed off or on a slower cadence; don't burn a
                # diagnostic-session transition on an empty window. Slower
                # weighted members remain aligned to cycle boundaries.
                next_due = min(
                    (
                        max(states[i].next_due_at, states[i].suppressed_until)
                        for i in self._did_indices
                    ),
                    default=now + (self._cycle() or self._interval()),
                )
                if all(states[i].suppressed_until > now for i in self._did_indices):
                    # Nothing schedulable: retain the historical idle re-anchor.
                    self._cycle_anchor = None
                    self._next_window_at = now + (self._cycle() or self._interval())
                elif self._cycle() is not None and self._cycle_anchor is not None:
                    self._schedule_next_window(now, earliest=next_due)
                else:
                    self._next_window_at = max(now + self._interval(), next_due)
                return False
            return True
        if self.phase == "window":
            return now + keepalive_lead - self._last_session_sent >= self._spec.keepalive_seconds
        return False

    def session_work_pending(self, states: Sequence[PollRequestState], now: float) -> bool:
        """Side-effect-free: is an exit, entry or keep-alive waiting on the budget?"""
        if self._exit_due:
            return True
        if self.phase == "mode01":
            return now >= self._next_window_at and bool(self._due_dids(states, now))
        if self.phase == "window":
            return now - self._last_session_sent >= self._spec.keepalive_seconds
        return False

    def _due_dids(self, states: Sequence[PollRequestState], now: float) -> frozenset[int]:
        """DIDs whose own cadence is due at this window boundary."""
        return frozenset(
            i
            for i in self._did_indices
            if states[i].suppressed_until <= now and states[i].next_due_at <= now
        )

    def on_entry_sent(self, now: float) -> None:
        """The loop is about to send ``10 <session>``: a cycle begins (or a keep-alive)."""
        if self.phase != "mode01":
            return
        cycle = self._cycle()
        missed = 0
        if cycle is None or self._cycle_anchor is None:
            self._cycle_anchor, self._cycle_k, self._next_cycle_k = now, 0, 0
        else:
            k_actual = max(self._next_cycle_k, int((now - self._cycle_anchor) / cycle + 1e-9))
            missed = k_actual - self._next_cycle_k
            self._cycle_k = k_actual
        self.cycle.begin_cycle(now, self._plan, missed=missed)
        self.cycle.on_entry_sent(now)

    def on_exit_sent(self, now: float) -> None:
        self.cycle.on_exit_sent(now)

    def on_session_response(
        self,
        *,
        result: SessionControlResult,
        states: Sequence[PollRequestState],
        now: float,
    ) -> None:
        self._last_session_sent = now
        ok = result.ok
        if result.negative_response_code is not None or ok:
            self._answered_since_unknown_exit = True
        if self.phase == "mode01":
            if ok:
                self.phase = "window"
                self.windows_opened += 1
                self._window_opened_at = now
                self._paced_close_at = None
                self._window_deadline = now + self._spec.window_max_seconds
                self._pending = set(self._due_dids(states, now))
            else:
                self.session_failures += 1
                self._schedule_next_window(now, earliest=now)
                if (
                    result.silent
                    and self.fast_exit_available(now)
                    and self._answered_since_unknown_exit
                ):
                    # A silent entry leaves the session state unknown: the ECM
                    # may have entered and lost its reply, and Mode 01 sent
                    # into a held session is silent — a second full timeout
                    # (the 2.4 s D03.18 stall). Hand the bus back first. Only
                    # an NRC proves the ECM stayed in the default session.
                    self._answered_since_unknown_exit = False
                    self.unknown_session_exits += 1
                    self.phase = "lapse"
                    self._lapse_until = now + self._spec.lapse_seconds
                    self._exit_due = True
                    self._exit_retry_armed = False
                    self._unknown_exit_pending = True
        elif self.phase == "window":
            if ok:
                self.keepalives_sent += 1
            else:
                # Session lost mid-window: let it lapse, resume Mode 01.
                self.session_failures += 1
                self._close_window(now)

    def session_exit_due(self) -> bool:
        """Whether the loop should send ``10 <exit_session>`` now."""
        return self._exit_due

    def fast_exit_available(self, now: float) -> bool:
        """Whether a fast exit should be attempted when the next window closes."""
        return self._spec.exit_session is not None and now >= self._exit_retry_at

    def on_exit_response(self, *, result: SessionControlResult, now: float) -> None:
        """Result of the return-to-default request.

        Only an NRC that means "never" disables the fast exit, and only after
        `FAST_EXIT_REFUSALS_BEFORE_DISABLE` of them in a row. Everything else —
        busy, conditions-not-correct, or silence — is re-sent once at the next
        budget slot (D03.18: a lost ``50 81`` cost the full 5 s lapse); a
        second failure in the same window costs that window's lapse and the
        exit is tried again on the next one. Even a disable is provisional: it
        expires, so the capture heals mid-drive without a restart.

        The exit sent after a silent entry is not retried: if it is silent too
        the ECU is most likely off, and Mode 01 resumes exactly as it did
        before that exit existed.
        """
        self._exit_due = False
        after_silent_entry = self._unknown_exit_pending
        retry_armed = self._exit_retry_armed
        self._unknown_exit_pending = self._exit_retry_armed = False
        if result.ok or result.negative_response_code is not None:
            self._answered_since_unknown_exit = True
        if result.ok:
            self.fast_exits += 1
            if self._exit_retry_at:
                log.info(
                    "Mode-22 fast exit recovered; Mode 01 back at full rate",
                    extra={"fast_exits": self.fast_exits, "failures": self.fast_exit_failures},
                )
            self._exit_refusals = 0
            self._exit_retry_at = 0.0
            self._exit_retry_backoff = FAST_EXIT_RETRY_MIN_SECONDS
            self.last_exit_nrc = None
            self._lapse_until = min(self._lapse_until, now + self._spec.exit_grace_seconds)
            return

        self.fast_exit_failures += 1
        self.last_exit_nrc = result.negative_response_code
        if not result.permanently_refused:
            if retry_armed:
                # Transient: re-send once now rather than wait the lapse out.
                self._exit_due = True
                self.fast_exit_retries += 1
            elif after_silent_entry:
                # The exit after a silent entry failed as well: nothing says a
                # session is held, so resume Mode 01 as before that exit existed.
                self._lapse_until = min(self._lapse_until, now)
            return
        self._exit_refusals += 1
        if self._exit_refusals < FAST_EXIT_REFUSALS_BEFORE_DISABLE:
            return
        self._exit_retry_at = now + self._exit_retry_backoff
        log.warning(
            "Mode-22 fast exit refused %d times in a row (%s); falling back to the "
            "%.1fs session lapse and re-probing in %.0fs. Mode 01 sample rate drops "
            "until then.",
            self._exit_refusals,
            nrc_name(result.negative_response_code)
            if result.negative_response_code is not None
            else "no response",
            self._spec.lapse_seconds,
            self._exit_retry_backoff,
            extra={
                "nrc": result.negative_response_code,
                "retry_in_seconds": self._exit_retry_backoff,
            },
        )
        self._exit_retry_backoff = min(self._exit_retry_backoff * 2, FAST_EXIT_RETRY_MAX_SECONDS)

    def request_verdict(
        self,
        index: int,
        request: PollRequest,
        *,
        selection: str | None = None,
        paused: bool = False,
        forced: bool = False,
    ) -> Eligibility:
        """The session gate: the C06.7 matrix row for this phase.

        ``selection``/``paused``/``forced`` are the profile gates the loop
        knows (the active profile's exclusion, a paused capture, the
        ``trigger_poll`` one-shot lift); passing the neutral defaults asks the
        session question alone. This is the verdict production runs: the loop
        composes it with the profile gates rather than keeping a copy (R02.2).

        Mode 03/07/0A (Task 1.3), Mode 06 (Task 3.1), Mode 02 freeze frames
        (Task 3.3) and custom PIDs (Task 2.6) are Mode 01-side services the
        ECM answers in the default session, so they share Mode 01's phase
        rather than being starved for the controller's whole lifetime; a
        custom PID declared on Mode 22 for a session-gated ECU would need the
        ``did`` treatment (not implemented — no such custom PID exists yet).
        Inside a window a member already attempted this window is deferred to
        the next one.
        """
        verdict = evaluate(
            request.kind,
            tuning_profile.profile_class(),
            self.phase,
            selection=selection,
            paused=paused,
            forced=forced,
        )
        if verdict.eligible and request.in_session_window and index not in self._pending:
            return Eligibility("deferred", "attempted_this_window")
        return verdict

    @property
    def matrix_phase(self) -> SessionPhase:
        return self.phase

    def did_pending(self, index: int) -> bool:
        """Whether this DID is still owed in the open window."""
        return index in self._pending

    def service_kind(self, index: int) -> ServiceKind:
        return "mode22" if index in self._did_indices else "mode01"

    def on_request_attempted(self, index: int) -> None:
        if index in self._did_indices:
            if index in self._pending:
                self.cycle.on_attempt("mode22")
            self._pending.discard(index)
        else:
            self.cycle.on_attempt("mode01")

    def on_service_time(self, kind: ServiceKind, seconds: float) -> None:
        self.cycle.on_service_time(kind, seconds)

    def on_response(self, index: int, now: float, acquired_at: float | None) -> None:
        """A request answered with readings — the only recovery evidence there is."""
        self._answered_since_unknown_exit = True
        kind = self.service_kind(index)
        self.cycle.on_acquired(kind, acquired_at)
        if kind == "mode22":
            self.cycle.on_did_answered(now)
        else:
            self.cycle.on_mode01_answered(now)

    def next_transition_at(self) -> float:
        if self.phase == "window":
            if self._continuous():
                # The window deadline is meaningless here and, once passed,
                # would pin this to a time in the past and spin the loop.
                # Keep-alive is the only scheduled event left.
                return self._last_session_sent + self._spec.keepalive_seconds
            return min(
                self._window_deadline,
                self._last_session_sent + self._spec.keepalive_seconds,
            )
        if self.phase == "lapse":
            return self._lapse_until
        return self._next_window_at

    @property
    def keepalive_seconds(self) -> float:
        return self._spec.keepalive_seconds

    def close_as_paced(self, at: float) -> None:
        """The open window's last members were moved to its end (a memory
        burst): schedule the next window as if it had closed at ``at``, when its
        paced last read would have been answered."""
        if self.phase == "window":
            self._paced_close_at = at

    def window_opened_at(self) -> float:
        """When the open window's session entry was answered: the instant its
        pending set was fixed."""
        return self._window_opened_at

    def window_deadline(self) -> float:
        """When the open window closes however much is still owed
        (``window_max_seconds`` after it opened); infinite outside a window."""
        return self._window_deadline if self.phase == "window" else math.inf

    def next_keepalive_at(self) -> float | None:
        """When the held session next needs ``10 <session>`` re-sent."""
        if self.phase != "window":
            return None
        return self._last_session_sent + self._spec.keepalive_seconds

    def release(self, now: float) -> None:
        """Close an open window so the session transition completes (C04.7).

        Used when the profile drops its DIDs, on a generation change, and
        when passive TX gating lifts: a window left in ``window`` phase would
        keep the ECU in the diagnostic session (Mode 01 dead) while the loop
        polls as if it were not.
        """
        if self.phase == "window":
            self._close_window(now)

    def on_generation_change(self, now: float, *, profile: str, members: Mapping[int, str]) -> None:
        """New capture generation: finish any transition and re-plan promptly.

        ``members`` (request index -> channel) are the session-window requests
        the profile now in force selects; the start ledger reports any of them
        not requested within one cycle, with why (D05.1). Required, so a
        generation change cannot be applied without being accounted.

        The next window is due now (not ``interval`` after the previous
        close), so a DID profile armed mid-drive starts within a budget slot
        instead of up to a config interval later — the 26.56 s first-DID
        delay recorded on 2026-08-07.

        That held only when the change arrived in the ``mode01`` phase. A
        capture armed *during* a window or its lapse closed the window and
        then waited out the OLD profile's ``window_interval_seconds``: on
        2026-09-22 a `tuning` capture asking for a window every 0.5 s took
        27.6 s to record its first DID, because `default`'s ~50 s interval
        was still running. The lapse is a protocol constraint (the ECU's
        session has to expire), so it is respected; the stale interval is
        not — the next window is due the moment the lapse ends.
        """
        self.release(now)
        self.cycle.abandon(now)
        self.cycle.reset()
        self._plan = None
        self._infeasible_logged = None
        self._cycle_anchor = None
        self._next_window_at = max(now, self._lapse_until) if self.phase == "lapse" else now
        unknown = set(members) - self._did_indices
        if unknown:
            raise ValueError(f"not Mode-22 window requests: {sorted(unknown)}")
        self.start_ledger.begin(
            profile=profile,
            members=members,
            now=now,
            in_lapse=self.phase == "lapse",
            grace=self._cycle() or self._spec.window_max_seconds,
            windows_opened=self.windows_opened,
        )

    def _close_window(self, now: float) -> None:
        self.cycle.on_window_closed(unattempted=len(self._pending))
        self.phase = "lapse"
        self._lapse_until = now + self._spec.lapse_seconds
        # Prefer handing the bus back explicitly. The lapse exists only
        # because the session has to expire on its own; when the ECU accepts a
        # return-to-default it is seconds of Mode-01 blackout for nothing.
        self._exit_due = self.fast_exit_available(now)
        self._exit_retry_armed = self._exit_due
        self._unknown_exit_pending = False
        if not self._exit_due:
            self.cycle.on_lapse_started()
        # A window whose memory reads were held back to its end is scheduled
        # from when pacing would have closed it, so where its reads sit never
        # moves the next window (B08.4): the end-to-start interval used to
        # restart 0.18 s later per window on the fake clock.
        paced = now if self._paced_close_at is None else min(now, self._paced_close_at)
        self._paced_close_at = None
        self._schedule_next_window(paced, earliest=paced)
        self.windows_completed += 1
        self._pending.clear()

    def snapshot(self, now: float) -> dict[str, Any]:
        """DID profile state for ``/status.obd_scheduler.did_profile``."""
        cycle = self._cycle()
        return {
            "phase": self.phase,
            "session": f"0x{self._spec.session:02X}",
            "did_request_count": len(self._did_indices),
            "next_window_in_seconds": (
                round(max(0.0, self._next_window_at - now), 3) if self.phase == "mode01" else 0.0
            ),
            "window_interval_seconds": self._interval(),
            "fast_exits": self.fast_exits,
            "fast_exit_failures": self.fast_exit_failures,
            #: Immediate re-sends of a transiently failed exit (D03.18).
            "fast_exit_retries": self.fast_exit_retries,
            #: Exits sent because a silent entry left the session unknown.
            "unknown_session_exits": self.unknown_session_exits,
            "fast_exit_supported": self.fast_exit_available(now),
            #: Seconds until the fast exit is re-probed; 0.0 when it is live.
            #: Non-zero means Mode 01 is running at the reduced lapse-path
            #: rate *and will recover on its own* — the state the old boolean
            #: could not express.
            "fast_exit_retry_in_seconds": (
                round(max(0.0, self._exit_retry_at - now), 1) if self._exit_retry_at else 0.0
            ),
            "last_exit_nrc": self.last_exit_nrc,
            "last_exit_nrc_name": (
                nrc_name(self.last_exit_nrc) if self.last_exit_nrc is not None else None
            ),
            "window_max_seconds": self._spec.window_max_seconds,
            "windows_opened": self.windows_opened,
            "windows_completed": self.windows_completed,
            "session_failures": self.session_failures,
            "keepalives_sent": self.keepalives_sent,
            #: D05.1: when each member of the profile now in force was first
            #: requested, and which were withheld at its start and why.
            "profile_start": self.start_ledger.snapshot(now),
            # Hybrid cycle (C04.8): the target and the achieved values share
            # one definition — start-to-start between session entries.
            "cycle": {
                "timing_policy": "cycle" if cycle is not None else "interval",
                "cycle_seconds": cycle,
                "cycle_feasible": self._plan.feasible if self._plan is not None else None,
                "plan": self._plan.to_dict() if self._plan is not None else None,
                **self.cycle.snapshot(now),
            },
        }
