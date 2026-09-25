"""Test-side helpers for driving ``run_can_obd_loop`` on a fake clock (C02).

The scripted ECU and the clock live in :mod:`src.validation.scripted_ecu`
(R07.1: ``src`` never imports ``tests``); they are re-exported here so the
collector tests keep one import. What stays is pytest-dependent:
:func:`install_clock` binds the fake clock as the ``time`` module of every
collection module the loop reads, so request timeouts, budget pacing, round
deadlines and presentation stamps all run on simulated time.
"""

from __future__ import annotations

from collections.abc import Iterable

import pytest

from src.collection import (
    can_obd_io,
    can_obd_loop,
    obd_response_decode,
    tuning_profile,
)
from src.collection.obd_scheduler import AUTO_MISS_BACKOFF_SECONDS
from src.collection.obd_scheduler import cadence as scheduler_cadence
from src.validation.scripted_ecu import (
    BROADCAST_ID,
    DEFAULT_SESSION_SUBFUNCTIONS,
    DEFAULT_VALUES,
    ENGINE_ECU,
    NRC_SERVICE_NOT_SUPPORTED_IN_SESSION,
    SUPPORT_GROUPS,
    FakeClock,
    ScriptedEcu,
    SessionReply,
    broadcast_sequence,
    isotp_frames,
    single_frame_reply,
    support_bits,
)

__all__ = [
    "BATCH_REFUSAL_WINDOW_SECONDS",
    "BROADCAST_ID",
    "DEFAULT_SESSION_SUBFUNCTIONS",
    "DEFAULT_VALUES",
    "ENGINE_ECU",
    "NRC_SERVICE_NOT_SUPPORTED_IN_SESSION",
    "SUPPORT_GROUPS",
    "FakeClock",
    "ScriptedEcu",
    "SessionReply",
    "broadcast_sequence",
    "group_by_stamp",
    "install_clock",
    "isotp_frames",
    "single_frame_reply",
    "stamp_span_seconds",
    "support_bits",
]

#: Simulated seconds the batch-refusal runs (``test_multi_pid_batching``,
#: ``test_d02_accounting``) last. Giving up on batching costs ten whole-batch
#: timeouts (3 at each of sizes 6/4/2, 0.05 s each) plus the RPM reads between
#: them: the first single SPEED read lands at 0.755 s of simulated time. The
#: tests used to run 0.8 s of *wall* clock, so 45 ms of scheduling delay
#: anywhere failed them (T01.4, 2026-09-23). On the fake clock that delay does
#: not exist, and a channel wrongly backed off would stay silent for
#: ``AUTO_MISS_BACKOFF_SECONDS``, far past this window.
BATCH_REFUSAL_WINDOW_SECONDS = 2.0
assert BATCH_REFUSAL_WINDOW_SECONDS < AUTO_MISS_BACKOFF_SECONDS


def install_clock(monkeypatch: pytest.MonkeyPatch, clock: FakeClock) -> None:
    """Bind ``clock`` as the ``time`` module of every module the loop reads."""
    for module in (
        can_obd_loop,
        can_obd_io,
        obd_response_decode,
        scheduler_cadence,
        tuning_profile,
    ):
        monkeypatch.setattr(module, "time", clock)


def group_by_stamp(readings: Iterable[object]) -> dict[float, list[object]]:
    """Readings keyed by presentation timestamp."""
    grouped: dict[float, list[object]] = {}
    for reading in readings:
        grouped.setdefault(float(reading.timestamp), []).append(reading)  # type: ignore[attr-defined]
    return grouped


def stamp_span_seconds(readings: Iterable[object]) -> float:
    """Wall-clock spread of the acquisition instants behind one presentation stamp."""
    acquired = [
        float(reading.acquired_at)  # type: ignore[attr-defined]
        for reading in readings
        if getattr(reading, "acquired_at", None) is not None
    ]
    return max(acquired) - min(acquired) if acquired else 0.0
