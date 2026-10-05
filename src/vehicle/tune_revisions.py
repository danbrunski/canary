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

See ``docs/roadmap/archive/tuning-analysis-toolkit.md`` Phase 4.3.
"""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path
from typing import Literal

import numpy as np
import pandas as pd
import yaml
from pydantic import BaseModel, ConfigDict, Field, model_validator


class _Strict(BaseModel):
    model_config = ConfigDict(extra="forbid")


class TuneRevision(_Strict):
    """One flashed calibration."""

    revision: str
    #: Raw identifier reported by the ECU.  Revision names are operator-facing
    #: labels and are never guessed from this part-number-style value.
    calibration_id: str | None = None
    #: When it was flashed. Everything recorded from here until the next
    #: revision belongs to this one.
    flashed_at: datetime
    #: How well ``flashed_at`` is known (E01.4): ``exact`` (pinned by a flash
    #: capture), ``approximate`` (a recollection or a file mtime), ``floor``
    #: (a placeholder earlier than anything it must cover, not a time). Unset
    #: is unknown: never read as exact, so a prediction cannot be proved to
    #: precede a flash whose time nobody pinned.
    flashed_at_precision: Literal["exact", "approximate", "floor"] | None = None
    #: Injector scaling constant in force, when known — the knob the fuel
    #: campaign moves, so worth carrying alongside the results it explains.
    k: float | None = None
    rom_file: str | None = None
    #: sha256 of the ROM *file* as saved (for an Osiris flash, the container).
    rom_sha256: str | None = Field(default=None, pattern=r"^[0-9a-f]{64}$")
    #: sha256 of the plain 1:1 flash image (the decoded ``ROMData``, or what an
    #: ECU read returns). W08.2: an image matches this revision when its plain
    #: sha256 equals this; ``rom_sha256`` names a container, which an ECU read
    #: never produces.
    image_sha256: str | None = Field(default=None, pattern=r"^[0-9a-f]{64}$")
    notes: str = ""


class ReferenceImage(_Strict):
    """A known image that was never flashed as-is, e.g. a tuner's base ROM.

    Kept out of :attr:`TuneRevisionRegistry.revisions` on purpose: a revision
    is a flash boundary that sessions are assigned to by time, and an image
    nobody flashed has no such boundary. Listing it here lets the ROM library
    name it (W08.2) without inventing a ``flashed_at``.
    """

    name: str = Field(min_length=1)
    image_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    rom_file: str | None = None
    rom_sha256: str | None = Field(default=None, pattern=r"^[0-9a-f]{64}$")
    notes: str = ""


class TuneRevisionRegistry(_Strict):
    schema_version: int = 1
    revisions: list[TuneRevision] = Field(default_factory=list)
    reference_images: list[ReferenceImage] = Field(default_factory=list)

    @model_validator(mode="after")
    def _ordered(self) -> TuneRevisionRegistry:
        stamps = [r.flashed_at for r in self.revisions]
        if any(a > b for a, b in zip(stamps, stamps[1:], strict=False)):
            raise ValueError("revisions must be listed oldest-first by flashed_at")
        return self

    @model_validator(mode="after")
    def _image_hashes_unique(self) -> TuneRevisionRegistry:
        """One plain image names one thing: a revision or a reference image."""
        seen: dict[str, str] = {}
        claims = [(r.revision, r.image_sha256) for r in self.revisions if r.image_sha256] + [
            (ref.name, ref.image_sha256) for ref in self.reference_images
        ]
        for name, digest in claims:
            if digest in seen:
                raise ValueError(
                    f"image_sha256 {digest[:12]}... is claimed by both {seen[digest]!r} and "
                    f"{name!r}; one image cannot be two calibrations"
                )
            seen[digest] = name
        if len({ref.name for ref in self.reference_images}) != len(self.reference_images):
            raise ValueError("reference_images names must be unique")
        return self

    def match_image(self, image_sha256: str) -> tuple[str | None, str | None]:
        """``(revision, reference)`` whose plain-image hash equals ``image_sha256``.

        At most one of the two is set; both ``None`` means unrecognised.
        """
        for revision in self.revisions:
            if revision.image_sha256 == image_sha256:
                return revision.revision, None
        for reference in self.reference_images:
            if reference.image_sha256 == image_sha256:
                return None, reference.name
        return None, None

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

    def revisions_at(self, timestamps: pd.Series) -> pd.Series:
        """:meth:`at` for every timestamp: the revision name in force at each.

        ``None`` before the first flash and for a missing timestamp. Naive
        timestamps are UTC, as in :meth:`at`. The same rule as :meth:`at`
        (flashed at or before the instant), vectorised: this is how
        ``derived:TARGET_AFR`` picks each row's map, on the live path (rows
        stamped "now", so the revision currently flashed) and offline alike
        (P02.16), satisfying :class:`src.signals.afr.FlashHistory`.
        """
        stamps = pd.to_datetime(timestamps, utc=True, errors="coerce")
        values = np.full(len(stamps), None, dtype=object)
        if not self.revisions or stamps.empty:
            return pd.Series(values, index=timestamps.index, dtype=object)
        flashed = pd.DatetimeIndex([_aware(r.flashed_at) for r in self.revisions]).as_unit("ns")
        present = stamps.notna().to_numpy()
        instants = pd.DatetimeIndex(stamps[present]).as_unit("ns")
        position = flashed.searchsorted(instants, side="right") - 1
        names = np.array([None, *(r.revision for r in self.revisions)], dtype=object)
        values[present] = names[position + 1]
        return pd.Series(values, index=timestamps.index, dtype=object)

    def for_session(self, session: str | Path) -> TuneRevision | None:
        """The revision in force when a session started (:func:`session_dated_at`).

        Pass the session *directory*: its first stored sample dates it. A bare
        name (a ``str``, or a path with no telemetry) can only be dated by the
        name, which is wrong whenever the clock stepped after the directory was
        created.
        """
        started = session_dated_at(session)
        return None if started is None else self.at(started)


def resolve_session_revision(
    session: Path, registry: TuneRevisionRegistry | None
) -> tuple[str | None, Literal["capture_manifest", "registry", "unknown"], bool]:
    """Resolve identity with explicit capture metadata taking precedence."""
    from src.storage.capture_manifest import read_capture_manifest

    manifest = read_capture_manifest(session)
    explicit = None if manifest is None else manifest.tune_revision
    inferred = None
    if registry is not None:
        found = registry.for_session(session)
        inferred = None if found is None else found.revision
    if explicit is not None:
        return explicit, "capture_manifest", inferred is not None and inferred != explicit
    if inferred is not None:
        return inferred, "registry", False
    return None, "unknown", False


def session_dated_at(session: str | Path) -> datetime | None:
    """When a session started, for deciding which calibration it ran on.

    The first stored sample when ``session`` is a directory holding telemetry
    (``src.storage.session_files.session_first_sample``), else the time its
    directory name states. The name is when the directory was created by a
    clock that may not have been set yet: ``2026-08-03T163440`` is named
    16:34:40Z and holds data from 23:11:54Z, after the k106 flash at
    18:45:21Z, and resolved by name to the previous revision (E01.6). Dating
    by data is the shared rule, so no caller has to remember it.
    """
    if isinstance(session, Path):
        from src.storage.session_files import session_first_sample

        first = session_first_sample(session) if session.is_dir() else None
        if first is not None:
            return first.to_pydatetime()
    return session_started_at(session)


def session_started_at(session: str | Path) -> datetime | None:
    """Parse a session directory name's UTC start time.

    Re-exported from :mod:`src.storage.sessions`, which owns the session
    naming rules. Kept as a name here because this module's callers think in
    terms of revisions rather than session plumbing — but there is one parser,
    not two.
    """
    from src.storage.sessions import session_started_at as _parse

    return _parse(session)


def calibration_matches(
    ecu_calibration_id: str | None, registry_calibration_id: str | None
) -> bool | None:
    """Whether the ECU-reported id agrees with the registry's, by exact equality.

    ``None`` when either side is unknown: an unread ECU, or a revision recorded
    without a ``calibration_id``. Never derived from the revision *name* —
    ``ZS44A-20260920`` and ``1ZS44A00000`` are different namespaces, and
    string-matching one against the other would call any ZS44A flash a match.
    """
    if not ecu_calibration_id or not registry_calibration_id:
        return None
    return ecu_calibration_id == registry_calibration_id


def load_tune_revisions(path: Path) -> TuneRevisionRegistry | None:
    """Load the registry, or ``None`` when the vehicle has none recorded."""
    if not path.exists():
        return None
    return TuneRevisionRegistry.model_validate(yaml.safe_load(path.read_text()))


#: What makes two entries with one ``revision`` name the same flash. ``notes``
#: is free text a hand edit may reword, so it is not identity.
_IDENTITY_FIELDS: tuple[str, ...] = ("calibration_id", "flashed_at", "k", "rom_file", "rom_sha256")


class RegistryConflictError(ValueError):
    """One revision name recorded as two different flashes, in two registries.

    Carries both paths and the fields that differ, so a caller can name the
    two files without parsing the message.
    """

    def __init__(self, revision: str, first: Path, second: Path, fields: tuple[str, ...]) -> None:
        super().__init__(
            f"revision {revision!r} is recorded differently in {first} and {second} "
            f"({', '.join(fields)} differ). One name cannot be two flashes: correct one "
            "of the two files."
        )
        self.revision = revision
        self.paths = (first, second)
        self.fields = fields


def _aware(moment: datetime) -> datetime:
    return moment if moment.tzinfo is not None else moment.replace(tzinfo=UTC)


def merge_tune_registries(
    operational: TuneRevisionRegistry | None,
    operational_path: Path,
    tracked: TuneRevisionRegistry | None,
    tracked_path: Path,
) -> TuneRevisionRegistry | None:
    """The operational registry plus every tracked revision it lacks (R05.17).

    The operational copy holds dashboard-recorded flashes; the tracked file is
    hand-edited and committed. Either may know a flash the other does not, so
    neither may simply win. Revisions are merged by name and ordered by
    ``flashed_at``. A name present in both must be the same flash (every
    field in :data:`_IDENTITY_FIELDS` equal); otherwise this raises
    :class:`RegistryConflictError` naming both files. For a name present in
    both, the tracked ``notes`` are kept: the tracked file is where notes are
    hand-edited.
    """
    if operational is None:
        return tracked
    if tracked is None:
        return operational
    by_name = {item.revision: item for item in operational.revisions}
    merged = list(operational.revisions)
    for item in tracked.revisions:
        own = by_name.get(item.revision)
        if own is None:
            merged.append(item)
            continue
        differ = tuple(
            name
            for name in _IDENTITY_FIELDS
            if (
                _aware(getattr(own, name)) != _aware(getattr(item, name))
                if name == "flashed_at"
                else getattr(own, name) != getattr(item, name)
            )
        )
        # ``image_sha256`` (W08.2) is filled in the tracked file after the
        # fact, so an operational copy written before it existed lacks it:
        # absent on one side is not a conflict, two different hashes are.
        if own.image_sha256 and item.image_sha256 and own.image_sha256 != item.image_sha256:
            differ = (*differ, "image_sha256")
        if differ:
            raise RegistryConflictError(item.revision, operational_path, tracked_path, differ)
        update: dict[str, object] = {}
        if own.notes != item.notes:
            update["notes"] = item.notes
        # Precision is a hand-recorded judgement about the time, kept in the
        # tracked file like the notes it comes from.
        if item.flashed_at_precision not in (None, own.flashed_at_precision):
            update["flashed_at_precision"] = item.flashed_at_precision
        if own.image_sha256 is None and item.image_sha256 is not None:
            update["image_sha256"] = item.image_sha256
        if update:
            merged[merged.index(own)] = own.model_copy(update=update)
    merged.sort(key=lambda revision: _aware(revision.flashed_at))
    references = {ref.name: ref for ref in operational.reference_images}
    for ref in tracked.reference_images:
        own_ref = references.get(ref.name)
        if own_ref is not None and own_ref.image_sha256 != ref.image_sha256:
            raise RegistryConflictError(ref.name, operational_path, tracked_path, ("image_sha256",))
        references[ref.name] = ref
    return TuneRevisionRegistry(
        schema_version=operational.schema_version,
        revisions=merged,
        reference_images=list(references.values()),
    )


__all__ = [
    "calibration_matches",
    "ReferenceImage",
    "RegistryConflictError",
    "TuneRevision",
    "TuneRevisionRegistry",
    "load_tune_revisions",
    "merge_tune_registries",
    "resolve_session_revision",
    "session_dated_at",
    "session_started_at",
]
