"""Which calibration, program and table the target-phi twin reads (E04.2).

One resolver for both of the twin's paths, so the stored evaluation of a
session (the brain job, :mod:`src.control.brain_jobs`) and the live stream of
the same reads (:mod:`src.daemon.twin_live`) cannot disagree about which bytes
the ECM was reading:

* **Calibration.** A capture manifest that names its tune revision wins for the
  whole session (``capture_manifest``; a disagreeing flash history is flagged
  ``conflict``). Otherwise the flash history decides by acquisition time
  (``registry``): the revision in force when the session's first sample was
  read (:func:`~src.vehicle.tune_revisions.session_dated_at`), and a new
  segment at every flash instant after it. A session that spans a flash is
  therefore two segments, each evaluated on its own calibration's table -- on
  the stored path as a windowed evaluation, on the live path as a stream rebuilt
  at the flash instant.
* **Program.** The image's program from the application model, hashing the
  library image only when the model does not list it -- once per image
  (:func:`~src.control.brain_state.image_program`). The table loader is the
  only refusal: an image of another or an unidentified program refuses there
  (``wrong_program`` / ``program_unverified``) on both paths alike.
* **Slots.** The profile's default slot and every committed slot the identity
  reads can name, each with its own table or its own named refusal.

Blocking (manifest, registry and profile reads, possibly one ROM hash): call it
off the event loop.
"""

from __future__ import annotations

import bisect
from collections.abc import Callable
from dataclasses import asdict, dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Literal

from src.analysis.ecu_application import ApplicationModel, load_application_model
from src.analysis.rom_trace import ViewerDefinition, load_viewer_definition
from src.analysis.target_phi import (
    ResolvedTable,
    TwinDefinition,
    TwinRefusalError,
    load_twin_definition,
    resolve_table,
    table_from_calibration,
)
from src.config import CanaryConfig
from src.control.brain_state import image_program
from src.control.rom_library import RomLibrary, RomNotFoundError
from src.signals.afr import load_afr_calibration
from src.storage.capture_manifest import read_capture_manifest
from src.storage.session_files import session_footer_span
from src.vehicle.profiles import active_registry, profile_dir
from src.vehicle.tune_revisions import TuneRevisionRegistry, session_dated_at

__all__ = [
    "IdentitySegment",
    "TwinIdentity",
    "TwinResolver",
    "candidate_slots",
    "session_last_read",
    "twin_tables",
]

CalibrationSource = Literal["capture_manifest", "registry", "unknown"]


@dataclass(frozen=True)
class TwinIdentity:
    """The calibration and program a stretch of data ran under."""

    calibration: str | None
    image_sha256: str | None
    source: CalibrationSource
    conflict: bool
    program_status: str
    program_id: str | None

    def key(self) -> tuple[str | None, str | None]:
        return (self.calibration, self.image_sha256)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(frozen=True)
class IdentitySegment:
    """``[start, end)`` acquisition seconds (``None``: open) under one identity."""

    start: float | None
    end: float | None
    identity: TwinIdentity

    def contains(self, t: float) -> bool:
        return (self.start is None or t >= self.start) and (self.end is None or t < self.end)

    def window(self) -> tuple[float | None, float | None] | None:
        return None if self.start is None and self.end is None else (self.start, self.end)

    def to_dict(self) -> dict[str, Any]:
        return {"start": self.start, "end": self.end, **self.identity.to_dict()}


def candidate_slots(definition: TwinDefinition) -> list[int]:
    """The default slot first, then every committed slot."""
    rules = definition.map_identity
    default = rules.default_slot.slot
    committed = range(rules.active_map.committed_min, rules.active_map.committed_max + 1)
    return [default, *(s for s in committed if s != default)]


def twin_tables(
    config: CanaryConfig,
    *,
    revision: str | None,
    image_sha256: str | None,
    definition: TwinDefinition | None = None,
    viewer: ViewerDefinition | None = None,
    model: tuple[ApplicationModel, str] | None = None,
) -> Callable[[int], ResolvedTable]:
    """``slot -> table`` for one calibration: the W08 library image when held,
    else the transcribed table; refusals by name (:class:`TwinRefusalError`)."""
    pdir = profile_dir(config)
    if definition is None:
        definition, _sha = load_twin_definition(pdir)
    viewer = viewer if viewer is not None else load_viewer_definition(pdir)
    model = model if model is not None else load_application_model(pdir)
    library = RomLibrary(
        config.daemon.rom_library_dir, profile_dir=pdir, registry=lambda: active_registry(config)
    )
    chosen = definition

    def table_for_slot(slot: int) -> ResolvedTable:
        if revision is None:
            raise TwinRefusalError(
                "revision_unknown", "neither the capture manifest nor the flash history names it"
            )
        if image_sha256 is None:
            raise TwinRefusalError(
                "image_unknown", f"the flash history records no plain-image hash for {revision!r}"
            )
        try:
            image = library.image(image_sha256)
        except RomNotFoundError:
            image = None
        if image is not None:
            return resolve_table(
                image,
                library.rom_map(),
                chosen,
                viewer=viewer,
                application_model=model,
                expected_sha256=image_sha256,
                slot=slot,
            )
        return table_from_calibration(
            load_afr_calibration(config.daemon.afr_calibration),
            chosen,
            viewer=viewer,
            application_model=model,
            revision=revision,
            image_sha256=image_sha256,
            slot=slot,
        )

    return table_for_slot


def _epoch(moment: datetime) -> float:
    aware = moment if moment.tzinfo is not None else moment.replace(tzinfo=UTC)
    return aware.timestamp()


class TwinResolver:
    """Identity segments and slot tables for one vehicle, cached per image
    (a ROM is hashed at most once per image) and per (identity, slot)."""

    def __init__(self, config: CanaryConfig) -> None:
        self.config = config
        pdir = profile_dir(config)
        self.definition, self.definition_sha256 = load_twin_definition(pdir)
        self._model = load_application_model(pdir)
        self._viewer = load_viewer_definition(pdir)
        self._programs: dict[str, tuple[str, str | None]] = {}
        self._dated_cache: dict[Path, datetime] = {}
        self._tables: dict[
            tuple[str | None, str | None, int], ResolvedTable | TwinRefusalError
        ] = {}

    # -- identity ------------------------------------------------------------

    def _identity(
        self,
        registry: TuneRevisionRegistry | None,
        revision: str | None,
        source: CalibrationSource,
        conflict: bool,
    ) -> TwinIdentity:
        entry = (
            next((r for r in registry.revisions if r.revision == revision), None)
            if registry is not None and revision is not None
            else None
        )
        image = entry.image_sha256 if entry is not None else None
        if image is None:
            status, program_id = ("no_image" if revision else "no_revision"), None
        else:
            if image not in self._programs:
                program_status, _actual = image_program(self.config, self._model[0], image)
                self._programs[image] = (
                    program_status,
                    self._model[0].program.id if program_status == "known" else None,
                )
            status, program_id = self._programs[image]
        return TwinIdentity(revision, image, source, conflict, status, program_id)

    def segments(
        self,
        session: Path | None,
        *,
        first_read: float | None,
        until: float | None = None,
    ) -> list[IdentitySegment]:
        """The identity segments of ``session`` (a directory, or ``None`` for a
        stream with no session on disk), dated by its first stored sample, else
        by ``first_read`` (acquisition seconds). ``until`` drops segments that
        would start after it: a finished session's last sample (the stored
        path), so a flash after the session is no segment of it. A live
        stream leaves it open: a later flash instant is a bound it will cross."""
        registry = active_registry(self.config)
        manifest = read_capture_manifest(session) if session is not None else None
        dated = self._dated(session)
        if dated is None and first_read is not None:
            dated = datetime.fromtimestamp(first_read, UTC)
        found = registry.at(dated) if registry is not None and dated is not None else None
        inferred = None if found is None else found.revision
        explicit = None if manifest is None else manifest.tune_revision
        if explicit is not None:
            conflict = inferred is not None and inferred != explicit
            return [
                IdentitySegment(
                    None, None, self._identity(registry, explicit, "capture_manifest", conflict)
                )
            ]
        if registry is None or dated is None:
            return [IdentitySegment(None, None, self._identity(registry, None, "unknown", False))]
        instants = [
            _epoch(r.flashed_at)
            for r in registry.revisions
            if _epoch(r.flashed_at) > _epoch(dated)
            and (until is None or _epoch(r.flashed_at) <= until)
        ]
        bounds: list[float | None] = [None, *instants]
        out = []
        for i, start in enumerate(bounds):
            end = bounds[i + 1] if i + 1 < len(bounds) else None
            at = dated if start is None else datetime.fromtimestamp(start, UTC)
            revision = registry.at(at)
            name = None if revision is None else revision.revision
            out.append(
                IdentitySegment(
                    start,
                    end,
                    self._identity(registry, name, "registry" if name else "unknown", False),
                )
            )
        return out

    def _dated(self, session: Path | None) -> datetime | None:
        """The session's first stored sample, read once per session: it never
        changes, and a live recheck must not re-read Parquet every minute."""
        if session is None:
            return None
        if session not in self._dated_cache:
            found = session_dated_at(session) if session.is_dir() else None
            if found is None:
                return None  # a recording with no chunk yet: ask again next time
            self._dated_cache[session] = found
        return self._dated_cache[session]

    # -- tables ----------------------------------------------------------------

    def table(self, identity: TwinIdentity, slot: int) -> ResolvedTable:
        """The table the ECM reads in ``slot`` under ``identity``, or the named
        refusal (cached, so a refused slot is not re-resolved per segment)."""
        key = (identity.calibration, identity.image_sha256, slot)
        if key not in self._tables:
            tables = twin_tables(
                self.config,
                revision=identity.calibration,
                image_sha256=identity.image_sha256,
                definition=self.definition,
                viewer=self._viewer,
                model=self._model,
            )
            try:
                self._tables[key] = tables(slot)
            except TwinRefusalError as exc:
                self._tables[key] = exc
        found = self._tables[key]
        if isinstance(found, TwinRefusalError):
            raise found
        return found


def session_last_read(session: Path) -> float | None:
    """A finished session's last stored sample (its parquet footers), seconds."""
    span = session_footer_span(session)
    if span.last is None:
        return None
    stamp = span.last if isinstance(span.last, datetime) else datetime.fromisoformat(str(span.last))
    return _epoch(stamp)


def segment_index(segments: list[IdentitySegment], t: float) -> int:
    """Which segment acquisition time ``t`` falls in (segments are ordered)."""
    starts = [s.start for s in segments[1:]]
    return bisect.bisect_right([float(s) for s in starts if s is not None], t)
