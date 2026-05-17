"""Test-session profile catalog — a scanned directory of YAML files.

A *test profile* is what the in-vehicle Test panel offers: pick one, start a
capture, then tap big event buttons as they happen (each tap writes a tag).
This deliberately replaces the profiles that used to be hardcoded in Python
(``scripts/guided_session.py``'s ``PROTOCOL_*``) and in the frontend — drop a
``.yaml`` file in the profiles directory and it appears, no code change.

Schema is a strict pydantic model so a typo in a hand-edited profile fails
loudly. Unlike the single-file ``dids.py`` / ``bindings.py`` loaders, this
scans a *user-editable directory* that is read on the road, so one malformed
file must not blank the whole picker: a bad/duplicate file is skipped and the
reason collected in ``catalog.errors`` for the UI to surface, while every
good file still loads.
"""

from __future__ import annotations

from pathlib import Path
from typing import Literal

import yaml
from pydantic import BaseModel, ConfigDict, Field, ValidationError


class _Strict(BaseModel):
    model_config = ConfigDict(extra="forbid")


class TestEvent(_Strict):
    """One tappable event in a profile.

    ``tag`` is what lands in ``tags.jsonl`` (reuse the established drive
    vocabulary so window/segmentation tooling keeps working). ``kind``
    ``"state"`` marks a phase boundary vs an instantaneous ``"event"``.
    ``advisory_duration_s`` is only meaningful to an ordered walk
    (``guided_session.py``); the tap UI ignores it.
    """

    label: str
    tag: str
    kind: Literal["event", "state"] = "event"
    hint: str | None = None
    advisory_duration_s: int | None = Field(default=None, ge=0)


class TestProfile(_Strict):
    """One profile = capture defaults + an unordered event palette."""

    id: str
    label: str
    description: str = ""
    #: Echoed onto the capture as ``protocol`` (free-form, not validated).
    protocol: str = "drive"
    #: Capture ``vehicle_state``: cold_start | engine_running_idle | driving.
    vehicle_state: str = "driving"
    #: Default capture ``purpose`` (operator can override before starting).
    purpose: str = ""
    #: Optional manual sort key for the picker (lower = earlier).
    order: int | None = None
    events: list[TestEvent] = Field(min_length=1)


class TestProfileError(_Strict):
    """A profile file that failed to load — surfaced, not swallowed."""

    file: str
    error: str


class TestProfileCatalog(_Strict):
    """Everything ``GET /test-profiles`` returns."""

    profiles: list[TestProfile] = Field(default_factory=list)
    errors: list[TestProfileError] = Field(default_factory=list)


def _sort_key(p: TestProfile) -> tuple[int, int, str]:
    # Explicit `order` first (nulls last), then label — deterministic picker.
    return (0, p.order, p.label) if p.order is not None else (1, 0, p.label)


def load_test_profiles(directory: Path) -> TestProfileCatalog:
    """Scan ``directory`` for ``*.yaml`` / ``*.yml`` profile files.

    Missing/empty directory → empty catalog (the panel shows a hint, not an
    error). Each file is validated independently; a failing or duplicate-``id``
    file is skipped with its reason recorded in ``errors`` so a single bad
    hand-edit never takes the whole picker down mid-drive.
    """
    catalog = TestProfileCatalog()
    if not directory.exists() or not directory.is_dir():
        return catalog

    seen: dict[str, str] = {}
    for path in sorted([*directory.glob("*.yaml"), *directory.glob("*.yml")]):
        try:
            raw = yaml.safe_load(path.read_text())
            if raw is None:
                raise ValueError("empty file")
            profile = TestProfile.model_validate(raw)
        except (yaml.YAMLError, ValidationError, ValueError, OSError) as exc:
            catalog.errors.append(TestProfileError(file=path.name, error=str(exc)))
            continue
        if profile.id in seen:
            catalog.errors.append(
                TestProfileError(
                    file=path.name,
                    error=f"duplicate id {profile.id!r} (already from {seen[profile.id]})",
                )
            )
            continue
        seen[profile.id] = path.name
        catalog.profiles.append(profile)

    catalog.profiles.sort(key=_sort_key)
    return catalog
