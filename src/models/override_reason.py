"""An operator override's stated reason: the one rule every override shares (R27.1).

An ``--accept-*`` override (thin calibration, over-budget alerts, an
unbudgeted publish) publishes something a gate refused, and the reason is
stamped into the artifact beside it. Blank or whitespace-only text is not a
reason. The rule used to be written five times in three forms; the IF
trainer's ``if not calibration_override:`` let ``"  "`` through to a manifest
whenever the caller was not the CLI (R06.2 named it). Every trainer, gate and
the CLI's argparse ``type`` now call :func:`require_override_reason`, and
``tests/test_override_reason.py`` refuses a sixth hand-written copy.

Imports nothing, so the daemon's job queue and ``scripts/_cli.py`` can use it
without loading a model module.
"""

from __future__ import annotations

BLANK_OVERRIDE_REASON = "an override needs a stated reason, not blank text"


def require_override_reason(value: str, *, refusal: str = BLANK_OVERRIDE_REASON) -> str:
    """Return ``value`` stripped; raise ``ValueError(refusal)`` when that is empty.

    ``refusal`` lets each gate keep the wording its callers and tests already
    match on.
    """
    reason = value.strip()
    if not reason:
        raise ValueError(refusal)
    return reason


def stated_override_reason(value: str | None) -> str | None:
    """``value`` stripped, or ``None`` when no reason was stated (absent or blank).

    For a gate whose refusal is its own (the IF trainer's A04.4 drive-count
    skip, the sequence trainer's :class:`CalibrationCoverageError`): blank text
    gets exactly the refusal a missing override gets.
    """
    if value is None:
        return None
    try:
        return require_override_reason(value)
    except ValueError:
        return None


__all__ = ["BLANK_OVERRIDE_REASON", "require_override_reason", "stated_override_reason"]
