"""Measured run-to-run noise, stored as an artifact instead of remembered.

On 2026-08-04 two captures were taken with an identical tune, hours apart. The
deltas between them are, by construction, *nothing* — so their spread is the
floor below which no comparison can claim an effect:

    spark, matched rpm x MAF   mean +0.01 deg   sd 0.64   range -1.03 .. +0.52
    delivery error, per cell   mean -0.36 %     sd 1.49   range -2.38 .. +1.38

That measurement existed before three confident claims were published at or
below it ("spark fell 0.7-1.6 deg", "the +0.61 deg control cell", "K responds
1:1"), because it lived in a commit message rather than anywhere code could
reach. This module makes it loadable, so
:func:`src.analysis.tuning.compare_sessions` can annotate every delta with how
many sigma it actually is.

A missing floor degrades to ``unavailable`` — never to an invented one.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import numpy as np
import pandas as pd
import yaml
from pydantic import BaseModel, ConfigDict, Field

from src.atomic_write import atomic_write_text


class _Strict(BaseModel):
    model_config = ConfigDict(extra="forbid")


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
    #: than living in a prose note nobody reads.
    conditions: str = "unspecified"
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


def load_noise_floor(path: Path) -> NoiseFloor | None:
    """Load a floor, or ``None`` when none has been measured for this vehicle."""
    if not path.exists():
        return None
    return NoiseFloor.model_validate(yaml.safe_load(path.read_text()))


def save_noise_floor(path: Path, floor: NoiseFloor) -> None:
    atomic_write_text(
        path, yaml.safe_dump(floor.model_dump(mode="json"), sort_keys=False), mkdir=True
    )


def annotate(
    deltas: pd.Series,
    floor: NoiseFloor | None,
    channel: str,
    *,
    sigma_threshold: float = 2.0,
) -> pd.DataFrame:
    """Attach ``delta_sigma`` and ``distinguishable`` to a series of deltas.

    Below ``sigma_threshold`` the honest phrasing is "not distinguishable from
    noise", which is what was missing from the retracted claims — each of them
    quoted a per-cell delta smaller than the per-cell spread of a null run.
    """
    sd = floor.sd_for(channel) if floor is not None else None
    frame = pd.DataFrame({"delta": deltas})
    if sd is None or sd <= 0:
        frame["delta_sigma"] = np.nan
        frame["distinguishable"] = pd.NA
        frame.attrs["floor"] = "unavailable"
        return frame
    frame["delta_sigma"] = frame["delta"] / sd
    frame["distinguishable"] = frame["delta_sigma"].abs() >= sigma_threshold
    frame.attrs["floor"] = sd
    entry = floor.channels[channel] if floor is not None else None
    frame.attrs["floor_conditions"] = entry.conditions if entry is not None else "unspecified"
    return frame


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


__all__ = [
    "ChannelFloor",
    "NoiseFloor",
    "annotate",
    "load_noise_floor",
    "record_null_run",
    "save_noise_floor",
]
