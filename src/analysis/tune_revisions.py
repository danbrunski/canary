"""Which calibration was in the ECU when a session was recorded.

"Delivery error by tune revision" should be a groupby, not archaeology. It was
archaeology: establishing which flash a July session belonged to meant reading
capture metadata for ROM ids and cross-checking a prose timeline, and the flash
boundary had to be pinned by hand ("stock ZS04C through 16:21 MDT on
2026-07-26; Osiris ZS44A from the 16:31 session").

This makes that a lookup. Revisions are a time-ordered list; a session belongs
to the newest revision flashed at or before it started. Sessions before the
first recorded flash resolve to ``None`` rather than being forced into a
revision that did not exist yet.

See ``docs/roadmap/tuning-analysis-toolkit.md`` Phase 4.3.
"""

from __future__ import annotations

import re
from datetime import UTC, datetime
from pathlib import Path

import yaml
from pydantic import BaseModel, ConfigDict, Field, model_validator

#: Session directory names are ``YYYY-MM-DDTHHMMSS`` in UTC, optionally behind
#: a ``_``-prefixed capture/quarantine marker (``_tuning-2026-08-05T142720``).
_SESSION_STAMP = re.compile(r"(\d{4}-\d{2}-\d{2})T(\d{2})(\d{2})(\d{2})")


class _Strict(BaseModel):
    model_config = ConfigDict(extra="forbid")


class TuneRevision(_Strict):
    """One flashed calibration."""

    revision: str
    #: When it was flashed. Everything recorded from here until the next
    #: revision belongs to this one.
    flashed_at: datetime
    #: Injector scaling constant in force, when known — the knob the fuel
    #: campaign moves, so worth carrying alongside the results it explains.
    k: float | None = None
    rom_file: str | None = None
    notes: str = ""


class TuneRevisionRegistry(_Strict):
    schema_version: int = 1
    revisions: list[TuneRevision] = Field(default_factory=list)

    @model_validator(mode="after")
    def _ordered(self) -> TuneRevisionRegistry:
        stamps = [r.flashed_at for r in self.revisions]
        if any(a > b for a, b in zip(stamps, stamps[1:], strict=False)):
            raise ValueError("revisions must be listed oldest-first by flashed_at")
        return self

    def at(self, when: datetime) -> TuneRevision | None:
        """The revision in force at ``when``, or ``None`` before the first flash."""
        moment = when if when.tzinfo is not None else when.replace(tzinfo=UTC)
        found: TuneRevision | None = None
        for revision in self.revisions:
            flashed = revision.flashed_at
            if flashed.tzinfo is None:
                flashed = flashed.replace(tzinfo=UTC)
            if flashed <= moment:
                found = revision
            else:
                break
        return found

    def for_session(self, session: str | Path) -> TuneRevision | None:
        """The revision in force when a session (by directory name) started."""
        started = session_started_at(session)
        return None if started is None else self.at(started)


def session_started_at(session: str | Path) -> datetime | None:
    """Parse a session directory name's UTC start time.

    Re-exported from :mod:`src.control.sessions`, which owns the session
    naming rules. Kept as a name here because this module's callers think in
    terms of revisions rather than session plumbing — but there is one parser,
    not two.
    """
    from src.control.sessions import session_started_at as _parse

    return _parse(session)


def load_tune_revisions(path: Path) -> TuneRevisionRegistry | None:
    """Load the registry, or ``None`` when the vehicle has none recorded."""
    if not path.exists():
        return None
    return TuneRevisionRegistry.model_validate(yaml.safe_load(path.read_text()))


__all__ = [
    "TuneRevision",
    "TuneRevisionRegistry",
    "load_tune_revisions",
    "session_started_at",
]
