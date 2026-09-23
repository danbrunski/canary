"""Real events on the vehicle, labelled as intervals with provenance (A02.7).

A detector's recall on *real* events needs the events written down as data a
harness can score against, not as prose in a drive log. Each entry is an
interval with the evidence that places it, and a ``kind`` that says what a
detector firing inside it means:

* ``fault`` — something was wrong with the vehicle. A detection here is a
  catch; the only kind that counts toward fault recall.
* ``novelty`` — the vehicle was fine but outside what the models learned
  (an altitude trip, a cleared fuel-trim table relearning). Firing here is
  expected and is reported separately; it is never counted as a fault catch.
* ``intervention`` — the vehicle itself changed (a flash, a hardware mod).
  ``corpus_boundary`` intervals split evaluation folds so no fold mixes sides.

Flashes are not duplicated here: :func:`load_labelled_events` derives one
``intervention`` per revision in the vehicle's ``tune_revisions.yaml`` (the
first entry is the factory floor, not a flash), so the flash registry stays
the single source of truth for flash times.
"""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path
from typing import Literal

import yaml
from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from src.analysis.tune_revisions import TuneRevisionRegistry, load_tune_revisions

LabelKind = Literal["fault", "novelty", "intervention"]

#: How the interval's end was placed. ``measured``: the data shows the state
#: ending there. ``last_observation``: the state was still present in the last
#: session available; the true end is later. ``approximate``: dated from a
#: record without a time of day.
EndBasis = Literal["measured", "last_observation", "approximate"]

#: Label file name inside a vehicle's data directory (``data/<profile>/``).
LABELLED_EVENTS_FILENAME = "labelled_events.yaml"


class _Strict(BaseModel):
    model_config = ConfigDict(extra="forbid")


class LabelledEvent(_Strict):
    """One real event as a closed interval ``[start, end]`` in UTC."""

    id: str = Field(min_length=1)
    kind: LabelKind
    title: str = Field(min_length=1)
    start: datetime
    end: datetime
    end_basis: EndBasis
    #: Where the interval comes from — a file, a measurement, a ledger entry.
    #: Required: an unsourced label is a claim nobody can re-check.
    provenance: str = Field(min_length=1)
    #: Does "normal" change here? Folds never mix drives across a boundary.
    corpus_boundary: bool = False
    affected_channels: list[str] = Field(default_factory=list)
    notes: str = ""

    @field_validator("start", "end")
    @classmethod
    def _utc(cls, value: datetime) -> datetime:
        return value.replace(tzinfo=UTC) if value.tzinfo is None else value.astimezone(UTC)

    @model_validator(mode="after")
    def _ordered(self) -> LabelledEvent:
        if self.end < self.start:
            raise ValueError(f"event {self.id!r}: end {self.end} precedes start {self.start}")
        return self


class LabelledEventRegistry(_Strict):
    schema_version: Literal[1] = 1
    events: list[LabelledEvent] = Field(default_factory=list)

    @model_validator(mode="after")
    def _unique_ids(self) -> LabelledEventRegistry:
        ids = [event.id for event in self.events]
        duplicates = sorted({i for i in ids if ids.count(i) > 1})
        if duplicates:
            raise ValueError(f"duplicate labelled event ids: {duplicates}")
        return self

    def of_kind(self, kind: LabelKind) -> list[LabelledEvent]:
        return [event for event in self.events if event.kind == kind]

    def boundaries(self) -> list[datetime]:
        """Start times of every corpus boundary, oldest first."""
        return sorted(event.start for event in self.events if event.corpus_boundary)


def flash_events(registry: TuneRevisionRegistry, *, source: Path) -> list[LabelledEvent]:
    """One ``intervention`` per flashed revision (all but the factory floor)."""
    events: list[LabelledEvent] = []
    for revision in registry.revisions[1:]:
        events.append(
            LabelledEvent(
                id=f"flash-{revision.revision}",
                kind="intervention",
                title=f"ECU flash: {revision.revision}",
                start=revision.flashed_at,
                end=revision.flashed_at,
                end_basis="measured",
                provenance=f"{source} (revision {revision.revision})",
                corpus_boundary=True,
                notes=revision.notes.strip(),
            )
        )
    return events


def load_labelled_events(
    path: Path, *, tune_revisions_path: Path | None = None
) -> LabelledEventRegistry:
    """Labels from ``path`` plus a flash per tune revision, oldest first.

    A missing label file yields only the derived flashes (an empty registry
    when there is no tune registry either) — "no labels recorded" is a fact
    about the vehicle, not an error.
    """
    events: list[LabelledEvent] = []
    if path.exists():
        events.extend(LabelledEventRegistry.model_validate(yaml.safe_load(path.read_text())).events)
    if tune_revisions_path is not None:
        tune_registry = load_tune_revisions(tune_revisions_path)
        if tune_registry is not None:
            events.extend(flash_events(tune_registry, source=tune_revisions_path))
    return LabelledEventRegistry(events=sorted(events, key=lambda event: event.start))


__all__ = [
    "LABELLED_EVENTS_FILENAME",
    "EndBasis",
    "LabelKind",
    "LabelledEvent",
    "LabelledEventRegistry",
    "flash_events",
    "load_labelled_events",
]
