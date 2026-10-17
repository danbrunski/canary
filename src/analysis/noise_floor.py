"""Measured run-to-run noise, stored as an artifact instead of remembered.

On 2026-08-04 two captures were taken with an identical tune, hours apart. The
deltas between them are, by construction, *nothing* — so their spread is the
floor below which no comparison can claim an effect:

    spark, matched rpm x MAF   mean +0.01 deg   sd 0.64   range -1.03 .. +0.52
    delivery error, per cell   mean -0.36 %     sd 1.49   range -2.38 .. +1.38

That measurement existed before three confident claims were published at or
below it ("spark fell 0.7-1.6 deg", "the +0.61 deg control cell", "K responds
1:1"), because it lived in a commit message rather than anywhere code could
reach. This module makes it loadable, and :meth:`NoiseFloor.significance` is
the one place a delta becomes a sigma — refusing, and naming both regimes,
when the comparison was not made under the conditions the floor was measured
in (R05.2).

A missing floor degrades to ``unavailable`` — never to an invented one.
"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Literal

import pandas as pd
import yaml
from pydantic import BaseModel, ConfigDict, Field

from src.atomic_write import atomic_write_text
from src.evidence.floors import floor_source_ref
from src.evidence.records import DETECTABLE_SIGMA, NoiseFloorRef, Quantity


class _Strict(BaseModel):
    model_config = ConfigDict(extra="forbid")


#: Below this many sigma a delta is "not distinguishable from changing
#: nothing" — the phrasing the retracted 2026-08 claims were missing.
SIGMA_THRESHOLD = DETECTABLE_SIGMA  # 2.0; investigation memory uses the same bar


@dataclass(frozen=True)
class Significance:
    """Per-delta sigma against a floor measured under the same conditions."""

    channel: str
    sd: float
    conditions: str
    sigma_threshold: float
    delta_sigma: pd.Series = field(repr=False)
    distinguishable: pd.Series = field(repr=False)

    def as_dict(self) -> dict[str, Any]:
        return {
            "available": True,
            "channel": self.channel,
            "sd": self.sd,
            "conditions": self.conditions,
            "sigma_threshold": self.sigma_threshold,
        }


@dataclass(frozen=True)
class FloorRefusal:
    """No significance verdict, and which kind of "no" it is."""

    channel: str
    kind: Literal["no_floor", "conditions_mismatch"]
    reason: str
    requested_conditions: str
    floor_conditions: str | None = None

    def as_dict(self) -> dict[str, Any]:
        return {
            "available": False,
            "channel": self.channel,
            "kind": self.kind,
            "reason": self.reason,
            "floor_conditions": self.floor_conditions,
            "requested_conditions": self.requested_conditions,
        }


class ChannelFloor(_Strict):
    """Per-cell spread for one quantity, measured from a null-run pair."""

    #: What the sd is in — ``percent`` for delivery error, else the channel's unit.
    unit: str
    #: The operating regime this was measured in. **A floor is only valid for
    #: the conditions it was measured under** — measured across all conditions
    #: instead of WOT, spark spread is 7.7 deg rather than 0.48, because idle
    #: timing swings 17-45 deg. Quoting the WOT floor against an idle
    #: comparison would understate noise ~16x and make ordinary jitter look
    #: like a finding. Machine-readable so the mismatch can be surfaced rather
    #: than living in a prose note nobody reads. Required (R05.2): a floor
    #: without its regime cannot be matched against anything, so it is not
    #: representable.
    conditions: str = Field(min_length=1)
    #: Per-cell standard deviation of the A-vs-B delta.
    sd: float = Field(gt=0)
    #: Mean delta; a real null run should sit near zero, and a large value is
    #: itself a finding (the runs were not equivalent after all).
    mean: float = 0.0
    n_cells: int = Field(ge=1)
    #: Smallest and largest per-cell delta of the null run. Optional because
    #: floors recorded before 2026-09-21 did not keep them; a page drawing
    #: the per-cell range must say "not recorded" rather than invent one.
    min: float | None = None
    max: float | None = None


class NoiseFloor(_Strict):
    schema_version: int = 1
    #: Which sessions produced this, so it can be recomputed or challenged.
    provenance: dict[str, Any] = Field(default_factory=dict)
    channels: dict[str, ChannelFloor] = Field(default_factory=dict)

    def sd_for(self, channel: str) -> float | None:
        entry = self.channels.get(channel)
        return None if entry is None else entry.sd

    def significance(
        self,
        channel: str,
        deltas: pd.Series,
        *,
        conditions: str,
        sigma_threshold: float = SIGMA_THRESHOLD,
    ) -> Significance | FloorRefusal:
        """How many sigma each delta is — or a refusal naming both regimes.

        ``conditions`` is the regime the *comparison* was made under. It must
        equal the regime the floor was measured under, character for
        character: the WOT spark floor quoted against an idle comparison
        understates noise ~16x, and a fuzzy match is how that would slip back
        in. Declare the matching regime in profile data (as
        ``lambda_delivery.yaml``'s ``noise_floors`` does) rather than loosening
        this.
        """
        entry = self.channels.get(channel)
        if entry is None:
            return FloorRefusal(
                channel=channel,
                kind="no_floor",
                reason=f"no noise floor measured for {channel}",
                requested_conditions=conditions,
            )
        if entry.conditions != conditions:
            return FloorRefusal(
                channel=channel,
                kind="conditions_mismatch",
                reason=(
                    f"{channel} was measured under {entry.conditions!r}; this comparison "
                    f"is under {conditions!r}. A floor is only valid for its own regime."
                ),
                floor_conditions=entry.conditions,
                requested_conditions=conditions,
            )
        values = pd.to_numeric(pd.Series(deltas), errors="coerce").astype(float)
        sigma = values / entry.sd
        return Significance(
            channel=channel,
            sd=entry.sd,
            conditions=entry.conditions,
            sigma_threshold=sigma_threshold,
            delta_sigma=sigma,
            distinguishable=sigma.abs() >= sigma_threshold,
        )


def significance(
    floor: NoiseFloor | None,
    channel: str,
    deltas: pd.Series,
    *,
    conditions: str,
    sigma_threshold: float = SIGMA_THRESHOLD,
) -> Significance | FloorRefusal:
    """:meth:`NoiseFloor.significance`, refusing when the vehicle has no floor at all."""
    if floor is None:
        return FloorRefusal(
            channel=channel,
            kind="no_floor",
            reason="no noise floor has been measured for this vehicle",
            requested_conditions=conditions,
        )
    return floor.significance(
        channel, deltas, conditions=conditions, sigma_threshold=sigma_threshold
    )


def load_noise_floor(path: Path) -> NoiseFloor | None:
    """Load a floor, or ``None`` when none has been measured for this vehicle."""
    if not path.exists():
        return None
    return NoiseFloor.model_validate(yaml.safe_load(path.read_text()))


def save_noise_floor(path: Path, floor: NoiseFloor) -> None:
    atomic_write_text(
        path, yaml.safe_dump(floor.model_dump(mode="json"), sort_keys=False), mkdir=True
    )


def record_null_run(
    per_cell_deltas: dict[str, pd.Series],
    *,
    sessions: tuple[str, str],
    units: dict[str, str],
    conditions: str,
    note: str = "",
) -> NoiseFloor:
    """Build a floor from paired same-tune captures.

    ``per_cell_deltas`` maps channel -> the A-vs-B delta of each matched cell.
    The caller does the matching (it is :func:`~src.analysis.tuning.compare_sessions`'s
    job); this only measures the spread, so the floor is always in the same
    units and grid as the comparisons it will gate.

    ``conditions`` is required, not optional: the regime is part of the
    measurement. The same null pair yields 0.48 deg of spark spread at WOT and
    7.7 deg across all conditions, so a floor without its regime recorded is
    not interpretable.
    """
    channels: dict[str, ChannelFloor] = {}
    for channel, deltas in per_cell_deltas.items():
        values = pd.to_numeric(deltas, errors="coerce").dropna()
        if len(values) < 2:
            raise ValueError(f"channel {channel!r} needs >=2 cells to have a spread")
        channels[channel] = ChannelFloor(
            unit=units.get(channel, "unknown"),
            conditions=conditions,
            sd=float(values.std(ddof=1)),
            mean=float(values.mean()),
            n_cells=int(len(values)),
            min=float(values.min()),
            max=float(values.max()),
        )
    return NoiseFloor(
        provenance={"sessions": list(sessions), "note": note},
        channels=channels,
    )


class FloorRefusedError(ValueError):
    """No floor can bound this prediction; carries the :class:`FloorRefusal`."""

    def __init__(self, refusal: FloorRefusal) -> None:
        super().__init__(refusal.reason)
        self.refusal = refusal

    def __reduce__(self) -> tuple[object, ...]:
        # Rebuilt from its constructor's arguments, so it survives a process
        # boundary (pickle); tests/test_exceptions_pickle.py holds every class to it.
        return (type(self), (self.refusal,), self.__dict__)


def floor_reference(
    source_path: Path,
    channel: str,
    *,
    conditions: str,
    sigma_threshold: float = SIGMA_THRESHOLD,
) -> NoiseFloorRef:
    """The floor a prediction under ``conditions`` states (E01.4), or a refusal.

    Reads the floor file once: its bytes' sha256 and its entry for
    ``channel`` go into the reference together, so the reference is exactly
    what the file measured. The same matching rule as
    :meth:`NoiseFloor.significance`: the floor's regime must equal
    ``conditions`` character for character. Raises :class:`FloorRefusedError`
    naming both regimes, or the absent file or channel.

    This is a convenience, not the guard. The guard is the memory ledger,
    which re-reads ``source`` when a prediction is appended
    (``src.evidence.floors.verify_floor``) and refuses a reference whose hash,
    sd, unit or regime differs from the file — so a reference relabelled to
    another regime or built by hand is refused there whatever built it.
    """
    if not source_path.is_file():
        raise FloorRefusedError(
            FloorRefusal(
                channel=channel,
                kind="no_floor",
                reason=f"no noise floor has been measured for this vehicle ({source_path})",
                requested_conditions=conditions,
            )
        )
    raw = source_path.read_bytes()
    floor = NoiseFloor.model_validate(yaml.safe_load(raw))
    entry = floor.channels.get(channel)
    if entry is None or entry.conditions != conditions:
        refusal = floor.significance(channel, pd.Series(dtype=float), conditions=conditions)
        assert isinstance(refusal, FloorRefusal)
        raise FloorRefusedError(refusal)
    return NoiseFloorRef(
        quantity=channel,
        sd=Quantity(value=entry.sd, unit=entry.unit),
        conditions=entry.conditions,
        sigma_threshold=sigma_threshold,
        source=floor_source_ref(source_path),
        source_sha256=hashlib.sha256(raw).hexdigest(),
    )


__all__ = [
    "SIGMA_THRESHOLD",
    "ChannelFloor",
    "FloorRefusal",
    "FloorRefusedError",
    "NoiseFloor",
    "Significance",
    "load_noise_floor",
    "record_null_run",
    "save_noise_floor",
    "floor_reference",
    "significance",
]
