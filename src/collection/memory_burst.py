"""A session window's memory burst and the budget clock it charges (R18).

The coordinate a timing lookup searches is spread over several service-0x23
reads (the rpm word, BFS-X and the HI DET latch are hundreds of bytes apart,
past the ECM's 63-byte read). Paced one budget slot apart, three reads span
0.25 s at the 8 req/s serving budget, which is the lookup's whole skew limit,
before any Mode 01 read joins them. So a window's memory reads go **last,
back to back, flush against the session exit** (B08.4, operator 2026-10-08):

- while any other session-window member is still owed, memory reads wait
  (``BurstDecision.outside`` excludes them);
- then the window waits (``hold_until``) until the reads, back to back at the
  turnaround the last burst measured, will finish where pacing's last read
  would have ended. A paced read costs the longer of a slot and its
  turnaround, so at high budgets the plan runs on turnaround;
- then every read is charged at once from the plan, the reads go with no slot
  between them, and the exit goes when pacing sent it. The lapse, Mode 01 and
  the next window keep pacing's times;
- a slot anything else spends meanwhile (a keep-alive, an identification
  read) moves the start and the charge out by exactly that slot, never by the
  wait (``BurstPlan.others``). Keep-alives follow
  :meth:`MemoryBurstPlanner.keepalive_policy`; a burst read's cadence grid
  steps from the window's opening (the loop's ``served_at``).

Over the window the request count and its span are what pacing gave, so the
window's average rate is unchanged; only the order and spacing inside it
change. The ECM needs no gap between 0x23 reads (measured: the 2026-09-21
rom-trace captures issued 6,081 memory reads straight after a memory read, the
closest 1.79 ms acquisition to acquisition, against a 2.05-2.11 ms median
request-to-response).

:class:`MemoryBurstPlanner` is the one owner of that plan, of the budget clock
every request charges (``next_budget_at``, ``slots_spent``) and of the
turnaround estimates the plan is sized from. It never sends, sleeps, reads a
clock or reads the active profile: the collector loop
(``can_obd_loop._CanObdLoop``) supplies the time, the budget slot and the
window's facts, applies the decisions, and keeps the bus.
"""

from __future__ import annotations

import dataclasses
from dataclasses import dataclass

from src.collection.obd_scheduler.requests import PollRequest, RequestFilter


@dataclass(frozen=True)
class BurstPlan:
    """The open window's memory burst: when it starts and what it is charged."""

    #: The first slot a paced memory read could have used.
    first_slot: float
    #: Memory reads the burst was planned for.
    reads: int
    #: What one paced read costs: a budget slot, or its own turnaround when
    #: that is longer (a paced read cannot go before the last one answered).
    per_read: float
    #: When the burst starts if nothing else spends a slot in the meantime:
    #: late enough that the reads, the exit and its grace end when Mode 01
    #: would have resumed after a paced exit.
    start_at: float
    #: ``MemoryBurstPlanner.slots_spent`` when the plan was made. A slot spent
    #: since by anything but a burst read (a keep-alive, an identification
    #: read) moves the start and the charged clock out by one slot.
    spent_at_plan: int
    #: False: the window's deadline falls before the paced reads would end,
    #: so the reads go at once and each is charged its slot as it is sent.
    prepaid: bool
    #: Burst reads issued so far.
    reads_done: int = 0
    started: bool = False

    def others(self, spent: int) -> int:
        """Slots spent since the plan by anything but a burst read."""
        return spent - self.spent_at_plan - self.reads_done

    def start(self, spent: int, slot: float) -> float:
        return self.start_at + self.others(spent) * slot

    def release(self, spent: int, slot: float) -> float:
        """The budget clock with every read charged as pacing would have
        charged it, plus every other slot spent since the plan."""
        return self.first_slot + self.reads * self.per_read + self.others(spent) * slot


@dataclass(frozen=True)
class BurstWindow:
    """One tick's facts about an open, closing session window.

    The loop builds this only while the controller holds a window that will
    close (not a continuous one) and the plan has memory reads in it."""

    #: Memory reads still owed in this window (pending, unsuppressed, eligible).
    members: frozenset[int]
    #: Whether any other session-window member is still owed. Computed only
    #: when ``members`` is non-empty.
    others_owed: bool
    #: When the window closes however much is still owed.
    deadline: float


@dataclass(frozen=True)
class BurstDecision:
    """One tick's answer from :meth:`MemoryBurstPlanner.decide`.

    Invariants: ``hold_until`` is set only while a planned burst waits to
    start (``issue_now`` false); ``issue_now`` means the burst has started and
    ``members`` go before anything else; ``paced_close_at`` is set on the one
    tick a prepaid burst starts, never otherwise.
    """

    members: frozenset[int]
    #: The ordinary filter with every memory read taken out: memory goes last.
    outside: RequestFilter
    hold_until: float | None
    issue_now: bool
    #: When the window would have closed under pacing; the loop hands it to
    #: ``DidWindowController.close_as_paced`` so the next window keeps
    #: pacing's schedule.
    paced_close_at: float | None = None

    def member(self, index: int, _request: PollRequest) -> bool:
        return index in self.members


class MemoryBurstPlanner:
    """The memory burst's plan, the budget clock and the turnarounds behind them.

    Every request the loop sends spends one slot through :meth:`spend_slot`;
    a prepaid burst moves the clock to its planned charge when it starts and
    after each of its reads, so its reads go without waiting for a slot. All
    times are ``time.monotonic()`` seconds supplied by the caller; ``slot`` is
    one budget slot (``1 / effective req/s``), read by the caller per call so
    an override armed mid-drive applies to the next request.
    """

    def __init__(self, memory_indices: frozenset[int], *, now: float) -> None:
        #: Service-0x23 reads in the request plan, which a window issues last
        #: and back to back.
        self.memory_indices = memory_indices
        self._plan: BurstPlan | None = None
        self._next_budget_at = now
        self._slots_spent = 0
        #: Mean turnaround of the latest memory burst's reads (0.0 until one
        #: has run): how long the next burst takes back to back, per read.
        self._memory_read_seconds = 0.0
        #: The same reads as pacing would have spaced them: each the longer of
        #: a budget slot and its turnaround.
        self._paced_read_seconds = 0.0
        self._burst_reads_s: list[float] = []
        #: Turnaround of the latest session-control request (entry, exit or
        #: keep-alive): the exit's cost when a burst is placed before it.
        self._session_seconds = 0.0

    # -- read-only state -------------------------------------------------

    @property
    def plan(self) -> BurstPlan | None:
        return self._plan

    @property
    def next_budget_at(self) -> float:
        """The budget clock: when the next request's slot opens."""
        return self._next_budget_at

    @property
    def slots_spent(self) -> int:
        """Budget slots spent since the loop started (:meth:`spend_slot`)."""
        return self._slots_spent

    @property
    def memory_read_seconds(self) -> float:
        return self._memory_read_seconds

    @property
    def paced_read_seconds(self) -> float:
        return self._paced_read_seconds

    @property
    def session_seconds(self) -> float:
        return self._session_seconds

    # -- observations ----------------------------------------------------

    def spend_slot(self, now: float, slot: float) -> None:
        """Advance the budget clock one slot, never into the past.

        Every request spends its slot here. The one exception to *when* the
        clock is read is a prepaid memory burst: it sets the clock once to its
        planned charge (``BurstPlan.release``) and restores it after each of
        its reads, so its reads go without waiting for a slot.
        """
        self._next_budget_at = max(self._next_budget_at + slot, now)
        self._slots_spent += 1

    def on_session_control(self, seconds: float) -> None:
        """A session-control request (entry, exit, keep-alive) took ``seconds``."""
        self._session_seconds = seconds

    def after_read(self, seconds: float, slot: float) -> None:
        """A burst read completed in ``seconds`` (answered, refused or timed
        out). A prepaid read was charged when the burst started: the next read
        goes without a slot. The mean over a burst sizes the next burst's
        start."""
        self._burst_reads_s.append(seconds)
        self._memory_read_seconds = sum(self._burst_reads_s) / len(self._burst_reads_s)
        self._paced_read_seconds = sum(max(slot, r) for r in self._burst_reads_s) / len(
            self._burst_reads_s
        )
        plan = self._plan
        if plan is None:
            return
        plan = dataclasses.replace(plan, reads_done=plan.reads_done + 1)
        self._plan = plan
        if plan.prepaid:
            self._next_budget_at = plan.release(self._slots_spent, slot)

    # -- decisions -------------------------------------------------------

    def decide(
        self,
        now: float,
        slot: float,
        window: BurstWindow | None,
        eligible: RequestFilter | None,
    ) -> BurstDecision | None:
        """Where the open window's memory reads stand this tick.

        ``None`` (pacing as before) when ``window`` is ``None`` (no window is
        open, the window is continuous, or the plan has no memory reads) or no
        memory read is owed; the plan is dropped. A window whose deadline would
        fall inside the wait reads its memory at once instead and is charged
        per read as before (``prepaid`` false).
        """
        if window is None or not window.members:
            self._plan = None
            return None
        members = window.members
        memory = self.memory_indices

        def outside(index: int, request: PollRequest) -> bool:
            return index not in memory and (eligible is None or eligible(index, request))

        if window.others_owed:
            self._plan = None
            return BurstDecision(members, outside, hold_until=None, issue_now=False)
        plan = self._plan
        spent = self._slots_spent
        if plan is None:
            first = max(self._next_budget_at, now)
            # Back to back, each read takes its turnaround: the last burst's
            # mean, or the session control's before any burst has run.
            read_s = self._memory_read_seconds or self._session_seconds
            # Paced, a read waits for its slot or for the last one's answer,
            # whichever is later (measured per read on the last burst).
            per_read = max(slot, self._paced_read_seconds or read_s)
            # Paced, the last read ends here and the exit goes. The burst ends
            # there too, so the exit, the lapse after it and the next window
            # keep pacing's times: a later exit moved the next cycle-mode
            # window past a boundary (``tuning`` at 8 req/s, 294 -> 280
            # windows in 900 s).
            paced_end = first + len(members) * per_read
            prepaid = paced_end < window.deadline
            plan = BurstPlan(
                first_slot=first,
                reads=len(members),
                per_read=per_read,
                start_at=max(first, paced_end - len(members) * read_s) if prepaid else now,
                spent_at_plan=spent,
                prepaid=prepaid,
            )
            self._burst_reads_s = []
        if not plan.started and now < plan.start(spent, slot):
            self._plan = plan
            return BurstDecision(
                members,
                outside,
                hold_until=plan.start(spent, slot),
                issue_now=False,
            )
        paced_close_at = None
        if not plan.started:
            plan = dataclasses.replace(plan, started=True)
            if plan.prepaid:
                # Every read is charged now, as pacing would have charged it;
                # the reads then go without waiting, and the exit waits for the
                # clock, so the window's request count and span are pacing's.
                release = plan.release(spent, slot)
                self._next_budget_at = release
                # Paced, the last read went one read before the release and
                # the window closed once it was answered.
                paced_close_at = (
                    release - plan.per_read + (self._memory_read_seconds or self._session_seconds)
                )
        self._plan = plan
        return BurstDecision(
            members, outside, hold_until=None, issue_now=True, paced_close_at=paced_close_at
        )

    def session_budget_at(self, now: float, *, window_open: bool, exit_due: bool) -> float:
        """The budget slot a session-control request waits for.

        A running prepaid burst charged every read's slot up front, so the
        clock sits at its paced end. A keep-alive due mid-burst (an unanswered
        read takes its 1 s timeout) may not wait for that: it goes now and is
        charged one slot on top (``BurstPlan.others``). Nothing else is due
        mid-burst; the exit follows the burst and waits for the clock."""
        plan = self._plan
        if plan is not None and plan.started and plan.prepaid and window_open and not exit_due:
            return min(self._next_budget_at, now)
        return self._next_budget_at

    def keepalive_policy(
        self,
        now: float,
        slot: float,
        *,
        keepalive_due_at: float | None,
        keepalive_seconds: float,
    ) -> tuple[bool, float]:
        """Whether a due keep-alive may go now, and how far ahead of its due
        time it goes (B08.4).

        Pacing sent a keep-alive only if it fell due by the window's last
        read slot; one due after that met the exit first and was never sent.
        The burst replaces those read slots, so while it waits:

        - a keep-alive due by the last read slot goes, and if the burst is
          about to start it goes now, so it does not land inside the burst and
          split the tuple;
        - one due only after the last read slot is not sent, as pacing did
          not send it (the exit follows the burst).

        Once the burst has started, a keep-alive pacing would have sent goes
        when due, and once the burst runs past its paced end (a read slower
        than paced, an unanswered read's 1 s timeout) every due keep-alive goes:
        a keep-alive is held at most one paced read, as pacing held it. Held
        for the whole burst, unanswered reads kept it back for seconds, past
        the ECM's 3-5 s S3 timeout.
        """
        plan = self._plan
        if plan is None or not plan.prepaid:
            return True, 0.0
        release = plan.release(self._slots_spent, slot)
        # Pacing's last read slot.
        last_read_slot = release - plan.per_read
        due_at = keepalive_due_at
        if plan.started:
            # Mid-burst: pacing's keep-alive goes when due, and so does any
            # once the burst runs past its paced end (a read slower than
            # paced, an unanswered read's timeout). Only one pacing would not
            # have sent goes after the burst, at most one read late.
            return (due_at is None or due_at <= last_read_slot or now >= release), 0.0
        if due_at is None or due_at > last_read_slot:
            return False, 0.0
        if now < plan.start(self._slots_spent, slot):
            return True, 0.0
        # About to start: one due within the burst's estimated length goes
        # now. Never more than once: a burst estimated at a whole keep-alive
        # interval or longer (unanswered reads) pre-sends nothing, or every
        # fresh keep-alive would fall inside it again and repeat each slot.
        burst_s = plan.reads * (self._memory_read_seconds or self._session_seconds)
        if burst_s >= keepalive_seconds:
            return True, 0.0
        return True, burst_s + self._session_seconds
