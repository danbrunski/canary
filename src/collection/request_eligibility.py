"""Diagnostic request eligibility matrix (roadmap task C06.7).

One table answers, for every request kind × capture-profile class × session
phase, whether a request is *eligible* now, *deferred* (queued until a
compatible phase) or *ineligible* (never under this profile), and why.
Both gates — the process-wide profile gate in :mod:`tuning_profile` and the
Mode-22 session controller in :mod:`obd_scheduler` — are projections of
:func:`evaluate`: each passes what it knows and neutral values for the rest,
so the policy lives here and nowhere else. The collector loop calls it once
with full knowledge to report ``deferred`` in the scheduler metrics.

Physical facts the table encodes (vehicle-specific behaviour stays in
``data/dids.yaml`` / the profile; the table only says *which* phase a kind
needs):

* Mode 01-side services (Mode 01/02/03/06/07/0A, custom PIDs, support
  discovery) need the ECU in its default session — a session-gated ECM is
  silent on them while a Mode-22 window is held and during the lapse.
* Mode 22 DIDs on such an ECM need the window; bare (ungated) DIDs need no
  phase at all.
* Service 0x23 memory reads are gated by the same session as Mode 22 on
  this ECM, so ``memory`` shares the DID row rather than the Mode 01-side
  one. Putting it on the Mode 01-side row would have issued every read in
  the one phase where the ECM is silent about it.
* A deliberately continuous DID capture holds the session for the whole
  capture. Nothing here invents permission to exit it: Mode 01-side work is
  *ineligible* under it, never deferred-then-forced.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Literal, get_args

#: Every request kind, the one Literal (R02.10): ``obd_scheduler.PollRequest.kind``
#: and :func:`evaluate` share it, where two Literals used to disagree.
RequestKind = Literal[
    "pid",
    "custom",
    "did",
    "memory",
    "dtc",
    "service",
    "freeze_frame",
    "forced_poll",
    "discovery",
    "session",
    "identification",
]
ProfileClass = Literal["default", "focused_mode01", "mode22_owning", "continuous_did"]
SessionPhase = Literal["none", "mode01", "window", "lapse"]
Verdict = Literal["eligible", "deferred", "ineligible"]

#: Wire traffic the loop owns outright and the matrix never gates: session
#: control (the controller decides when) and identification reads (the
#: calibration-id schedule decides). ``evaluate`` calls them ``unknown_kind``.
UNGATED_KINDS: frozenset[RequestKind] = frozenset({"session", "identification"})
#: The kinds the matrix has rows for.
REQUEST_KINDS: tuple[RequestKind, ...] = tuple(
    kind for kind in get_args(RequestKind) if kind not in UNGATED_KINDS
)
PROFILE_CLASSES: tuple[ProfileClass, ...] = get_args(ProfileClass)
SESSION_PHASES: tuple[SessionPhase, ...] = get_args(SessionPhase)

#: Kinds answered by the ECU in its default session only.
MODE01_SIDE_KINDS: frozenset[str] = frozenset(
    {"pid", "custom", "dtc", "service", "freeze_frame", "forced_poll", "discovery"}
)
#: Kinds whose channel must be in the profile's selection; ``service`` only
#: outside ``default`` (a focused capture polls Mode 06 only when it names
#: ``MONITOR_RESULTS`` or ``AUTO``; the default profile mirrors config).
SELECTION_KINDS: frozenset[str] = frozenset({"pid", "custom", "did", "memory"})


@dataclass(frozen=True)
class Eligibility:
    verdict: Verdict
    reason: str

    @property
    def eligible(self) -> bool:
        return self.verdict == "eligible"

    def to_dict(self) -> dict[str, str]:
        return {"verdict": self.verdict, "reason": self.reason}


ELIGIBLE = Eligibility("eligible", "ok")
_CONTINUOUS = Eligibility("ineligible", "continuous_did_capture")
_NO_MODE22 = Eligibility("ineligible", "profile_without_mode22")


def _phase_row(**by_phase: Eligibility) -> dict[SessionPhase, Eligibility]:
    return {phase: by_phase[phase] for phase in SESSION_PHASES}


_MODE01_SIDE_ROW = _phase_row(
    none=ELIGIBLE,
    mode01=ELIGIBLE,
    window=Eligibility("deferred", "session_window"),
    lapse=Eligibility("deferred", "session_lapse"),
)
_DID_ROW = _phase_row(
    none=ELIGIBLE,
    mode01=Eligibility("deferred", "awaiting_window"),
    window=ELIGIBLE,
    lapse=Eligibility("deferred", "session_lapse"),
)
_ALL_CONTINUOUS = _phase_row(
    none=_CONTINUOUS, mode01=_CONTINUOUS, window=_CONTINUOUS, lapse=_CONTINUOUS
)
_ALL_NO_MODE22 = _phase_row(none=_NO_MODE22, mode01=_NO_MODE22, window=_NO_MODE22, lapse=_NO_MODE22)

#: Kinds the ECM answers only inside the Mode-22 diagnostic session.
#: ``memory`` (service 0x23) is here because it is gated by the same
#: session, verified on the vehicle 2026-09-20.
_SESSION_GATED_KINDS: frozenset[str] = frozenset({"did", "memory"})


def _build_matrix() -> dict[tuple[str, str], dict[SessionPhase, Eligibility]]:
    matrix: dict[tuple[str, str], dict[SessionPhase, Eligibility]] = {}
    for kind in REQUEST_KINDS:
        for profile_class in PROFILE_CLASSES:
            if kind in _SESSION_GATED_KINDS:
                row = _ALL_NO_MODE22 if profile_class == "focused_mode01" else _DID_ROW
            elif profile_class == "continuous_did":
                row = _ALL_CONTINUOUS
            else:
                row = _MODE01_SIDE_ROW
            matrix[(kind, profile_class)] = row
    return matrix


#: ``(kind, profile_class) -> {phase: Eligibility}`` — the policy, as data.
MATRIX: dict[tuple[str, str], dict[SessionPhase, Eligibility]] = _build_matrix()


def selection_applies(kind: str, profile_class: str) -> bool:
    """Whether the profile's channel selection gates this kind."""
    if kind in SELECTION_KINDS:
        return True
    return kind == "service" and profile_class != "default"


def evaluate(
    kind: RequestKind,
    profile_class: ProfileClass,
    phase: SessionPhase,
    *,
    selection: str | None = None,
    paused: bool = False,
    passive: bool = False,
    forced: bool = False,
) -> Eligibility:
    """The verdict for one request.

    ``selection`` is the profile gate's exclusion reason for this channel
    (``"not_selected"``, ``"mode01_disabled"``, ``"mode22_disabled"``) or
    ``None`` when selected; it only matters for :func:`selection_applies`
    kinds. ``forced`` marks an operator-triggered one-shot (``trigger_poll``):
    the selection is lifted, but the physical phase requirement is not — a
    forced DID still waits for its window.
    """
    if passive:
        return Eligibility("deferred", "passive")
    if paused:
        return Eligibility("deferred", "paused")
    table_kind = kind
    if forced:
        selection = None
        table_kind = kind if kind in _SESSION_GATED_KINDS else "forced_poll"
    row = MATRIX.get((table_kind, profile_class))
    if row is None:
        return Eligibility("ineligible", "unknown_kind")
    verdict = row[phase]
    if verdict.eligible and selection is not None and selection_applies(kind, profile_class):
        return Eligibility("ineligible", selection)
    return verdict


def matrix_rows() -> list[dict[str, Any]]:
    """The whole table, one row per kind × class × phase (for docs and tests)."""
    return [
        {
            "kind": kind,
            "profile_class": profile_class,
            "phase": phase,
            **MATRIX[(kind, profile_class)][phase].to_dict(),
        }
        for kind in REQUEST_KINDS
        for profile_class in PROFILE_CLASSES
        for phase in SESSION_PHASES
    ]


__all__ = [
    "MATRIX",
    "PROFILE_CLASSES",
    "REQUEST_KINDS",
    "SESSION_PHASES",
    "Eligibility",
    "ProfileClass",
    "RequestKind",
    "SessionPhase",
    "UNGATED_KINDS",
    "Verdict",
    "evaluate",
    "matrix_rows",
    "selection_applies",
]
