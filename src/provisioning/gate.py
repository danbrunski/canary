"""Who may provision right now (roadmap N1.2).

A service that accepts WiFi credentials over the air and writes network
profiles is reachable by anyone in radio range, so it is open only when it has
to be:

- **No WiFi client profile exists.** A fresh install has no way onto any
  network, so nobody can hold the control token yet; the first provisioning
  is open because nothing else is possible.
- **Inside a window** opened by someone who already has access: the control
  token (``POST /control/network/provisioning/window``) or a shell on the Pi
  (``python -m src.provisioning open-window``).

Otherwise the Improv state is "authorization required" and a WiFi write is
refused. The status characteristic (N1.3) stays readable either way.

The window is a file holding one float, the epoch at which it closes -- the
same two-line contract as the UPS keep-alive hold (``src/daemon/ups.py``),
written by the unprivileged daemon and read by the root service through the
shared ``/run/canary`` tmpfs.
"""

from __future__ import annotations

import math
import time
from dataclasses import dataclass
from pathlib import Path

from src.atomic_write import atomic_write_text

DEFAULT_WINDOW_PATH = Path("/run/canary/provision_window")
DEFAULT_WINDOW_MINUTES = 5.0
#: A window is for one provisioning, done standing next to the vehicle. A long
#: one is an open write left behind, so the writer refuses it.
MAX_WINDOW_MINUTES = 30.0


def read_window_remaining(path: Path = DEFAULT_WINDOW_PATH, *, now: float | None = None) -> float:
    """Seconds left in the provisioning window; 0 when closed or unreadable."""
    try:
        text = path.read_text()
    except OSError:
        return 0.0
    try:
        expiry = float(text.strip())
    except ValueError:
        return 0.0
    if not math.isfinite(expiry):
        return 0.0
    now = time.time() if now is None else now
    # The cap holds on the read side too: the file sits in a directory the
    # unprivileged daemon writes, so the root service must not trust its value.
    return min(max(0.0, expiry - now), MAX_WINDOW_MINUTES * 60.0)


def write_window(
    minutes: float, path: Path = DEFAULT_WINDOW_PATH, *, now: float | None = None
) -> float:
    """Open (``minutes`` > 0) or close (``minutes`` <= 0) the window.

    Returns the expiry epoch written, ``0.0`` when closed.
    """
    if minutes > MAX_WINDOW_MINUTES:
        raise ValueError(
            f"a provisioning window is at most {MAX_WINDOW_MINUTES:g} minutes, not {minutes:g}"
        )
    now = time.time() if now is None else now
    expiry = 0.0 if minutes <= 0 else now + minutes * 60.0
    atomic_write_text(path, str(expiry))
    return expiry


@dataclass(frozen=True)
class GateVerdict:
    authorized: bool
    #: ``unprovisioned``, ``window``, or ``locked``.
    reason: str
    window_remaining_s: float


def decide(*, client_profiles: int, window_remaining_s: float) -> GateVerdict:
    if client_profiles == 0:
        return GateVerdict(True, "unprovisioned", window_remaining_s)
    if window_remaining_s > 0:
        return GateVerdict(True, "window", window_remaining_s)
    return GateVerdict(False, "locked", 0.0)
