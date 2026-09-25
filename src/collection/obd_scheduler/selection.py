"""Which due request goes next, and which companions share its batch.

Part of the ``obd_scheduler`` package (R02.10).
"""

from __future__ import annotations

import logging
from collections.abc import Container, Mapping
from typing import TYPE_CHECKING

from src.collection import tuning_profile
from src.collection.obd_scheduler.cadence import (
    _is_request_suppressed,
)
from src.collection.obd_scheduler.requests import (
    Mode01WidthLedger,
    PollRequest,
    PollRequestState,
    RequestFilter,
)

if TYPE_CHECKING:
    pass

log = logging.getLogger(__name__)


def _count_active_requests(
    requests: list[PollRequest],
    states: list[PollRequestState],
    now: float,
    *,
    eligible: RequestFilter | None = None,
) -> int:
    return sum(
        1
        for index, (request, state) in enumerate(zip(requests, states, strict=True))
        if not _is_request_suppressed(state, now) and (eligible is None or eligible(index, request))
    )


def _select_next_due_request(
    requests: list[PollRequest],
    states: list[PollRequestState],
    now: float,
    *,
    eligible: RequestFilter | None = None,
    among: Container[int] | None = None,
    preferred: Container[int] = frozenset(),
) -> tuple[int, PollRequest, PollRequestState] | None:
    """The due request to issue next.

    ``among`` restricts the choice to the open round's unattempted members
    (C02.3: one attempt per member per round). A member is due inside the
    round's window by construction, so it may be issued as soon as the round
    is open rather than at its own due instant — a one-time phase advance of
    under one period that lets a round's members converge onto one cadence
    (and one batch) instead of staying staggered forever; the configured
    rate is unchanged because the next due time is still ``issued + interval``.
    ``preferred`` — the profile's ``required_tuple`` — ranks just below the
    priority lane, so when the budget cannot serve a whole round the tuple
    completes first (C02.5); it never makes a request due, so it cannot
    starve the others.
    """
    candidates = [
        (state.next_due_at, index, request, state)
        for index, (request, state) in enumerate(zip(requests, states, strict=True))
        if not _is_request_suppressed(state, now)
        and (among is not None or state.next_due_at <= now)
        and (among is None or index in among)
        and (eligible is None or eligible(index, request))
    ]
    if not candidates:
        return None
    # Priority requests (the active profile's lane, or the collection default)
    # win over any other
    # due request, however overdue that one is. Without this the plan degrades
    # *uniformly* when demand exceeds what the bus can actually serve — every
    # request, mode-critical or not, ends up at the same fraction of its
    # configured rate. Starvation isn't a risk in the other direction: the
    # priority set is small and each member still only becomes due at its own
    # tier rate, so it can never claim more than that slice of the budget.
    _due_at, index, request, state = min(
        candidates,
        key=lambda item: (
            not tuning_profile.priority_channel(item[2].channel, item[2].priority),
            item[1] not in preferred,
            item[0],
            item[1],
        ),
    )
    return index, request, state


def is_batchable(request: PollRequest, widths: Mode01WidthLedger | None = None) -> bool:
    """Whether a request can ride along in a multi-PID Mode 01 batch.

    Only plain Mode 01 PIDs. Custom PIDs are excluded even when they sit on
    Mode 01: they decode through their own formula against a payload this
    module doesn't know the width of, so they can't be split back out of a
    batched response reliably. With ``widths``, a PID whose measured width
    contradicts the registry, or that is owed a single re-measure after a
    desync, is excluded too (``Mode01WidthLedger.batchable``).
    """
    if not (request.kind == "pid" and request.service == 0x01 and 0 <= request.identifier <= 0xFF):
        return False
    return widths is None or widths.batchable(request.identifier)


def select_batch_companions(  # noqa: PLR0913
    requests: list[PollRequest],
    states: list[PollRequestState],
    now: float,
    *,
    leader_index: int,
    max_pids: int,
    eligible: RequestFilter | None = None,
    ignore_tier: bool = False,
    members: Container[int] | None = None,
    preferred: Container[int] = frozenset(),
    widths: Mode01WidthLedger | None,
) -> list[int]:
    """Indices to issue alongside ``leader_index`` as one batched request.

    Returns the leader plus up to ``max_pids - 1`` companions, or just the
    leader when batching is off or nothing else qualifies.

    Companions must be due, unsuppressed, eligible, batchable, and **in the
    same poll tier** as the leader. The tier rule is what stops a slow-tier
    PID from being dragged to fast-tier cadence (wasting budget on data that
    changes on the order of minutes) or a fast-tier one being held back.
    ``ignore_tier`` lifts it for capture rounds, where the operator's channel
    selection *is* the sample and every member is expected on the same row.

    ``members`` confines companions to the open round's unattempted members
    (C02.5: same-round companions travel together; a member is never issued
    twice in one round). A member is due *inside this round's window* by
    construction, so it may ride the leader's batch a little before its own
    due instant: that is a one-time phase advance of under one period, after
    which the batch shares one cadence — not a rate increase. ``preferred``
    — the profile's ``required_tuple`` — fills the batch first, so the
    tuple's members share one response when the ceiling would otherwise
    split them across requests. ``widths`` (the loop's measured-width ledger)
    keeps a width-contradicted or re-measure-owed PID out of every batch, as
    leader or companion.
    """
    leader = requests[leader_index]
    if max_pids <= 1 or not is_batchable(leader, widths):
        return [leader_index]

    chosen = [leader_index]
    seen_pids = {leader.identifier}
    order = sorted(range(len(requests)), key=lambda index: (index not in preferred, index))
    for index in order:
        request, state = requests[index], states[index]
        if len(chosen) >= max_pids:
            break
        if index == leader_index or not is_batchable(request, widths):
            continue
        if not tuning_profile.mode01_batch_compatible(leader.channel, request.channel):
            continue
        if members is not None and index not in members:
            continue
        if not ignore_tier and request.poll_tier != leader.poll_tier:
            continue
        if request.identifier in seen_pids:
            continue
        if _is_request_suppressed(state, now):
            continue
        if members is None and state.next_due_at > now:
            continue
        if eligible is not None and not eligible(index, request):
            continue
        chosen.append(index)
        seen_pids.add(request.identifier)
    return chosen


def select_did_batch_companions(  # noqa: PLR0913
    requests: list[PollRequest],
    states: list[PollRequestState],
    now: float,
    *,
    leader_index: int,
    max_dids: int,
    response_widths: Mapping[int, int],
    eligible: RequestFilter | None = None,
    members: Container[int] | None = None,
    preferred: Container[int] = frozenset(),
) -> list[int]:
    """Same-round Mode-22 companions with known full payload boundaries."""
    leader = requests[leader_index]
    if max_dids <= 1 or leader.kind != "did" or leader.identifier not in response_widths:
        return [leader_index]
    chosen = [leader_index]
    seen = {leader.identifier}
    order = sorted(range(len(requests)), key=lambda index: (index not in preferred, index))
    for index in order:
        request, state = requests[index], states[index]
        if len(chosen) >= min(max_dids, 3):
            break
        if index == leader_index or request.kind != "did":
            continue
        if request.service != leader.service or request.request_id != leader.request_id:
            continue
        if request.identifier in seen or request.identifier not in response_widths:
            continue
        if members is not None and index not in members:
            continue
        if _is_request_suppressed(state, now):
            continue
        if members is None and state.next_due_at > now:
            continue
        if eligible is not None and not eligible(index, request):
            continue
        chosen.append(index)
        seen.add(request.identifier)
    return chosen
