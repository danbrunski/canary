"""``accepted_baselines.yaml`` schema & loader (Task 1.3, online-training-strategy.md).

A known-accepted fault is not a mute — it's a *recentering with a one-sided
band* (see the doc's "Directional overrides — the schema gap" section). This
is the declarative, forward-looking cousin of ``src.models.feedback``:
feedback records "that past alert was a false positive"; an override
declares "I know this engine state and accept it going forward," with
provenance (who/when/why) and a ``review_by`` date so an accepted fault
can't quietly age into masking a *second*, newly-failing one.

Loaded the same strict-Pydantic way as ``behavior_models.yaml``
(``src.behavior.behavior_models``), but **profile-scoped** — every override is
a this-vehicle fact (a second vehicle profile must never inherit the
Xterra's weak-injector override), so the path is resolved through
``src.vehicle.profiles.scope_config`` under the active vehicle's data tree, never a
flat ``data/`` path. See ``DaemonConfig.accepted_baselines``.

Phase 1 shipped the schema + loader only, with the shipped
``data/nissan-xterra-2008/accepted_baselines.yaml`` carrying **zero active
overrides** — a commented worked example, nothing more. Authoring a real
override is an explicit operator (morning) task, never an unattended one:
declaring "this fault is accepted" is exactly the kind of judgment call this
repo's standing rules reserve for the operator. This module answers "is
there an active override for this (channel, mode)?" for the Phase 1
disposition classifier (``src.analysis.training_disposition``) and, as of
Phase 2 (:meth:`AcceptedBaselineOverride.to_deviation_config`), converts an
override into the runtime directional ``DeviationConfig`` variant
(``src.behavior.behavior_models``) that ``src.behavior.behavior_decoder``
actually grades live deviations against — one severity-grading
implementation shared by statically-authored directional models and
dynamically-applied overrides alike.
"""

from __future__ import annotations

from datetime import date
from pathlib import Path
from typing import Literal

import yaml
from pydantic import BaseModel, ConfigDict, Field, model_validator

from src.behavior.behavior_models import DeviationConfig
from src.paths import DATA_DIR

#: Default (unscoped) location — always resolve the real path through
#: ``src.vehicle.profiles.scope_config`` (``DaemonConfig.accepted_baselines``); this
#: constant exists only as the field default / for tooling that runs with
#: profiles disabled.
DEFAULT_ACCEPTED_BASELINES_PATH = DATA_DIR / "accepted_baselines.yaml"

CURRENT_SCHEMA_VERSION = 1


class _Strict(BaseModel):
    model_config = ConfigDict(extra="forbid")


class DirectionalBand(_Strict):
    """One-sided deviation band around ``accepted_center``.

    ``tolerance`` is the wander allowed either side of center with no alert
    at all. Beyond that, only ``worse_direction`` matters: past
    ``worse_warning`` / ``worse_critical`` (in output units, past center)
    trips a warning/critical; drifting the *other* way (``better_direction:
    ignore``, the only value today) never alerts — that's what stops
    improvement/measurement noise from generating false alarms. Parsed and
    validated here; :meth:`AcceptedBaselineOverride.to_deviation_config`
    (Phase 2) is what actually wires it into runtime grading.
    """

    kind: Literal["directional"] = "directional"
    tolerance: float = Field(ge=0)
    worse_direction: Literal["positive", "negative"]
    worse_warning: float = Field(gt=0)
    worse_critical: float = Field(gt=0)
    better_direction: Literal["ignore"] = "ignore"

    @model_validator(mode="after")
    def _check_thresholds(self) -> DirectionalBand:
        if self.worse_warning >= self.worse_critical:
            raise ValueError("worse_warning must be < worse_critical")
        return self


class Provenance(_Strict):
    """Who declared this override, when, why, and when it needs re-confirming.

    ``review_by`` is non-negotiable (see the doc's "Durability, provenance,
    and not rotting" section) — a two-year-old "accept the weak injector"
    must not silently mask a second, newly-failing injector.
    """

    declared_by: str
    declared_at: date
    review_by: date
    reason: str

    @model_validator(mode="after")
    def _check_dates(self) -> Provenance:
        if self.review_by <= self.declared_at:
            raise ValueError("review_by must be after declared_at")
        return self


class AcceptedBaselineOverride(_Strict):
    """One directional, provenanced override.

    Targets either a raw ``channel`` (e.g. ``obd:SHORT_FUEL_TRIM_1``) or a
    ``behavior_model`` by name — exactly one, never both/neither. A raw-
    channel override needs ``accepted_center`` (there's no fitted surface to
    read a center from); a behavior-model override's center comes from the
    surface's own refit (Phase 2), so ``accepted_center`` is omitted there.
    ``operating_mode`` optionally scopes the override to one segmenter mode
    (e.g. ``idle``) — omitted means it applies across all modes.
    """

    channel: str | None = None
    behavior_model: str | None = None
    operating_mode: str | None = None
    accepted_center: float | None = None
    band: DirectionalBand
    provenance: Provenance

    @model_validator(mode="after")
    def _check_target(self) -> AcceptedBaselineOverride:
        if (self.channel is None) == (self.behavior_model is None):
            raise ValueError("exactly one of `channel` / `behavior_model` must be set")
        if self.channel is not None and self.accepted_center is None:
            raise ValueError(
                f"channel override {self.channel!r} needs accepted_center "
                "(a raw channel has no fitted surface to read a center from)"
            )
        return self

    @property
    def target(self) -> str:
        """The channel or behavior-model name this override applies to."""
        return self.channel if self.channel is not None else str(self.behavior_model)

    def is_expired(self, *, today: date | None = None) -> bool:
        """True once ``today`` is past ``provenance.review_by`` — an expired
        override is inert (Phase 2 stops recentering/suppressing on it and
        surfaces it as needs-re-confirmation instead)."""
        return (today or date.today()) > self.provenance.review_by

    def applies_to(self, target: str, operating_mode: str | None) -> bool:
        """True when this override targets ``target`` and, if scoped, matches
        ``operating_mode``."""
        if self.target != target:
            return False
        return self.operating_mode is None or self.operating_mode == operating_mode

    def to_deviation_config(self) -> DeviationConfig:
        """Convert this override's directional band into a runtime
        ``DeviationConfig`` (2.2, online-training-strategy.md Phase 2).

        For a ``behavior_model`` target, ``accepted_center`` is omitted
        (``None``) even if a caller somehow set it — the doc is explicit
        that the center for a behavior-model override comes from the
        surface's own refit, not a fixed number; the override contributes
        only the one-sided band. A ``channel`` target always carries
        ``accepted_center`` (schema-enforced at construction) and it is
        included here — there's no fitted surface to read a center from,
        so the caller (``src.behavior.behavior_decoder``) grades directly
        against this fixed number.
        """
        return DeviationConfig(
            kind="directional",
            warning_band=self.band.worse_warning,
            critical_band=self.band.worse_critical,
            accepted_center=self.accepted_center if self.channel is not None else None,
            tolerance=self.band.tolerance,
            worse_direction=self.band.worse_direction,
            better_direction=self.band.better_direction,
        )


class AcceptedBaselines(_Strict):
    """Top-level ``accepted_baselines.yaml`` document."""

    schema_version: int
    overrides: list[AcceptedBaselineOverride] = Field(default_factory=list)

    def active_override_for(
        self, target: str, operating_mode: str | None = None, *, today: date | None = None
    ) -> AcceptedBaselineOverride | None:
        """First non-expired override matching ``target`` (mode-scoped or
        global). ``None`` when no override applies (or the only match has
        expired) — the Phase 1 disposition classifier treats that as "no
        cover", i.e. Quarantine rather than Accept-recenter."""
        for override in self.overrides:
            if override.applies_to(target, operating_mode) and not override.is_expired(today=today):
                return override
        return None


def load_accepted_baselines(path: Path) -> AcceptedBaselines:
    """Load and validate an ``accepted_baselines.yaml`` file.

    A missing file is a valid, empty document (zero overrides) — this file
    is optional; most profiles will never need one. Mirrors
    ``load_behavior_models``'s strict-Pydantic posture: a malformed file
    raises rather than silently ignoring bad overrides.
    """
    if not path.exists():
        return AcceptedBaselines(schema_version=CURRENT_SCHEMA_VERSION, overrides=[])
    raw = yaml.safe_load(path.read_text()) or {}
    return AcceptedBaselines.model_validate(raw)


def dump_accepted_baselines(baselines: AcceptedBaselines, path: Path) -> None:
    """Serialize an ``AcceptedBaselines`` object back to YAML (round-trip
    counterpart to :func:`load_accepted_baselines`, same posture as
    ``dump_behavior_models``)."""
    payload = baselines.model_dump(mode="json", exclude_none=True)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(yaml.safe_dump(payload, sort_keys=False, default_flow_style=False))


__all__ = [
    "CURRENT_SCHEMA_VERSION",
    "DEFAULT_ACCEPTED_BASELINES_PATH",
    "AcceptedBaselineOverride",
    "AcceptedBaselines",
    "DirectionalBand",
    "Provenance",
    "dump_accepted_baselines",
    "load_accepted_baselines",
]
