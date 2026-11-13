"""Arm or disarm the legacy tuning-mode poll profile, with its storage (R29.7).

``POST /control/tuning-mode`` changes two things that must not disagree: the
poll profile (what the collectors ask for) and the on-disk layout (a
quarantined ``_tuning-*`` session while armed, a plain one after). The pair was
a ``try`` inside the route handler, whose failure branch is the whole point of
it and could be exercised only by building an app around a writer that raises.
It is :func:`set_tuning_mode` here: the poll profile goes back to what it was
when the writer cannot follow, and the failure is a
:class:`TuningStorageSwitchError` the app answers as a 500.
"""

from __future__ import annotations

import logging
from typing import Any

from src.collection import tuning_profile
from src.control.status_error import StatusError
from src.session import issue_isolated_id
from src.storage.async_writer import AsyncParquetTelemetryWriter

log = logging.getLogger(__name__)


class TuningStorageSwitchError(StatusError[str]):
    """The writer could not switch session directories; the poll profile was put back."""


async def set_tuning_mode(
    storage_writer: AsyncParquetTelemetryWriter | None,
    *,
    enabled: bool,
    channels: list[str] | None,
    rate_hz: float | None,
) -> dict[str, Any]:
    """Arm (``enabled``) or disarm tuning mode and move storage to match.

    Returns the response payload. Storage moves only on a real transition, so
    re-arming an already-active tuning mode does not split the session. When
    the writer raises, the poll profile is restored to ``previous`` before the
    error is raised: the mode state and the on-disk layout never disagree.
    """
    names = frozenset(channels) if channels else None
    previous = tuning_profile.set_active(
        enabled,
        channel_names=names,
        rate_hz=rate_hz,
        source="control",
    )
    payload: dict[str, Any] = {
        "tuning_mode": tuning_profile.snapshot(),
        "previous_active": previous,
    }
    # engine-tuning-support.md 1.2 — quarantine the capture at the moment
    # the mode is armed, not afterwards. Only on a real transition, so
    # re-arming an already-active tuning mode doesn't split the session.
    if storage_writer is not None and previous != enabled:
        try:
            if enabled:
                tuning_id = issue_isolated_id()
                await storage_writer.enter_tuning_mode(tuning_id)
                payload["output_session_dir"] = str(storage_writer.paths.root)
            else:
                await storage_writer.exit_tuning_mode()
                payload["output_session_dir"] = str(storage_writer.paths.root)
        except Exception as exc:
            # Storage isolation failed, so the mode state and the on-disk
            # layout now disagree. Put the poll profile back rather than
            # silently logging a quarantined drive into the corpus.
            log.exception("tuning-mode storage isolation failed; reverting poll profile")
            tuning_profile.set_active(previous, source="control")
            raise TuningStorageSwitchError(
                500, "failed to switch tuning session storage; tuning mode unchanged"
            ) from exc
    payload["tuning_mode"] = tuning_profile.snapshot()
    return payload


__all__ = ["TuningStorageSwitchError", "set_tuning_mode"]
