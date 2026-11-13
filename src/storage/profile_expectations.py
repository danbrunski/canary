"""What a capture profile snapshot promises, parsed once (R23.11).

A capture's manifest carries the profile as a loose ``Mapping[str, Any]``
(``tuning_profile.snapshot()``, written to ``capture_profile.json``). Three
readers each pulled the same fields out of it by hand -- the quality scorer
(``collection.capture_quality``), the persisted-round coverage count
(``analysis.capture_coverage``) and the manifest's own ``required_tuple``
property -- and each normalised them a little differently (upper-casing,
dropping blanks, tolerating a non-list). :class:`ProfileExpectations` is the one
parse; it lives in ``storage`` because the manifest is the lowest of the three.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field
from typing import Any


@dataclass(frozen=True)
class ProfileExpectations:
    """The scoring-relevant promises of one profile snapshot, normalised.

    * ``weights`` -- per-channel rate weight, upper-cased: the Mode 01 weights
      and the Mode 22 ``mode22_rate_weights`` together.
    * ``target_rate_hz`` -- the rate the capture was asked to hold: the caller's
      ``requested_rate_hz`` when it names one, else the snapshot's.
    * ``required_tuple`` -- upper-cased channel names that must land in one
      round; blank entries and a non-list value read as no tuple.
    * ``min_achieved_rate_hz`` -- the floor a channel may not fall below.
    * ``mode01_names`` -- upper-cased Mode 01 channel names the profile polls.
    """

    weights: Mapping[str, float] = field(default_factory=dict)
    target_rate_hz: float | None = None
    required_tuple: tuple[str, ...] = ()
    min_achieved_rate_hz: float | None = None
    mode01_names: frozenset[str] = frozenset()

    @classmethod
    def from_snapshot(
        cls, profile: Mapping[str, Any], requested_rate_hz: float | None = None
    ) -> ProfileExpectations:
        mode01 = profile.get("mode01") or {}
        weights = {
            str(name).upper(): float(rate)
            for source in (mode01, profile.get("mode22_rate_weights") or {})
            for name, rate in source.items()
        }
        target = (
            requested_rate_hz if requested_rate_hz is not None else profile.get("target_rate_hz")
        )
        floor = profile.get("min_achieved_rate_hz")
        raw_tuple = profile.get("required_tuple") or ()
        required = (
            tuple(str(item).upper() for item in raw_tuple if str(item).strip())
            if isinstance(raw_tuple, list | tuple)
            else ()
        )
        return cls(
            weights=weights,
            target_rate_hz=None if target is None else float(target),
            required_tuple=required,
            min_achieved_rate_hz=None if floor is None else float(floor),
            mode01_names=frozenset(str(name).upper() for name in mode01),
        )


__all__ = ["ProfileExpectations"]
