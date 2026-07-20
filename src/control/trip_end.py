"""Trip-end orchestration (Task 5.4/5.5, eval-longitudinal-and-dtc.md).

The pure analysis/IO lives in :mod:`src.analysis.digest`; this is the
control-layer side-effecting half — the same split :mod:`src.control.trends`
draws against :mod:`src.analysis.trends`. Two triggers call
:func:`handle_trip_end`, both best-effort from the daemon's point of view:

* :class:`~src.analysis.gps_tracker.GpsTracker`'s sustained moving ->
  stationary edge (the normal case — the daemon's GPS sink calls this the
  tick the edge fires).
* Daemon shutdown (the fallback — the drive ends because the Pi lost power
  or was shut down mid-trip, with no clean GPS-detected stop first).

Idempotent either way: a session that already has a ``digest.json`` is
treated as already handled, so the fallback firing after the GPS edge
already ran (or vice versa) never double-computes or double-notifies.
"""

from __future__ import annotations

import logging
from datetime import datetime
from pathlib import Path
from typing import TYPE_CHECKING, Any

from src.alerts.notifier import format_digest_message
from src.analysis.digest import get_or_compute_digest, read_digest
from src.config import CanaryConfig

if TYPE_CHECKING:
    from src.alerts.emitter import AnomalyEventEmitter
    from src.alerts.notifier import Notifier

log = logging.getLogger(__name__)


def handle_trip_end(
    session_dir: Path,
    config: CanaryConfig,
    *,
    emitter: AnomalyEventEmitter | None = None,
    notifier: Notifier | None = None,
    now: datetime | None = None,
) -> dict[str, Any] | None:
    """Assemble + persist the digest for one completed trip, and push it.

    Returns the digest dict on a freshly-computed trip end, or ``None``
    when the trip has no telemetry, no session directory, or was already
    handled (idempotent re-fire — see module docstring).
    """
    if emitter is not None:
        # The next trip gets its own push budget regardless of which path
        # (fresh or idempotent re-fire) got us here.
        emitter.reset_notification_count()
    if read_digest(session_dir) is not None:
        return None
    digest = get_or_compute_digest(session_dir, config, now=now)
    if digest is None:
        return None
    if notifier is not None and config.notifications.digest_enabled:
        title, body = format_digest_message(digest)
        notifier.notify(title, body)
    return digest


__all__ = ["handle_trip_end"]
