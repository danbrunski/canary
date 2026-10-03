"""The unit a wide-frame column's values are in, from the catalogs that decoded it (P02.13).

One resolver for a question that ``src.signals`` cannot answer itself: the
catalogs sit above it (the Mode 01 registry is in ``collection``), so the
featurization layer resolves the unit here and hands a resolver
(:func:`channel_units_for`) to :func:`src.signals.afr.append_afr_channels`.
The target-AFR lookup refuses a load-axis channel unless this unit is the
map's ``load_axis_unit``. A vehicle's declaration carries no unit of its own
to trust.

**The DID catalog is the one that decoded the frame, passed in, never re-read
from the repo** (P02.14 verification). A frame decoded with a caller's
``dids_path`` (``build_feature_frame``, ``analyze_session``, the ``--dids``
CLIs) holds values in *that* catalog's units. Checking them against the repo
file let a catalog that scales 0x1208 to ms produce a base fuel schedule
scaled twice, silently. So every function here takes ``did_map`` as a
required keyword, and ``None`` (no DID catalog decoded this frame) gives no
``did:`` / ``mem:`` unit and refuses every DID view.

Covered namespaces, each the unit the frame's values are actually in:

* ``obd:`` -- the Mode 01 registry's unit (``MODE01_REGISTRY``);
* ``did:`` -- the DID catalog's ``dids:`` (``unknown`` while unscaled: the
  frame then holds raw counts);
* ``mem:`` -- the DID catalog's ``memory:``;
* ``derived:`` -- only a vehicle's DID views (P02.14,
  ``src.signals.vehicle_signals.DID_VIEWS``): the unit of the source DID's
  evidence record that supplies the view's scale (:func:`did_view_scale`).
  That record is also what :func:`append_did_views` multiplies by, so the
  unit and the values come from one place.

Everything else resolves to ``None``, which the lookup refuses. That includes
``decoded:``, whose frame rows are raw counts by design whatever unit the
binding names, and every other ``derived:`` channel, which has no unit catalog.
"""

from __future__ import annotations

import re
from collections.abc import Callable
from dataclasses import dataclass
from functools import partial

import pandas as pd

from src.collection.obd_registry import MODE01_REGISTRY
from src.columns import arrived_column, gap_column
from src.decode.dids import DidEntry, DidMap
from src.signals.vehicle_signals import DidView, did_views

_MODE01_UNITS: dict[str, str] = {
    f"obd:{definition.channel}": definition.unit for definition in MODE01_REGISTRY.values()
}

#: A view's scale is a per-count constant: ``"<unit> per count"``.
_PER_COUNT = re.compile(r"^(?P<unit>\S+) per count$")

#: Catalog placeholders that are not a unit.
_NO_UNIT: frozenset[str] = frozenset({"", "unknown"})


@dataclass(frozen=True)
class DidViewScale:
    """How a :class:`~src.signals.vehicle_signals.DidView` is computed: the
    source's raw counts times ``scale`` are in ``unit``."""

    view: DidView
    scale: float
    unit: str


def _entry(did_map: DidMap, source: str) -> DidEntry | None:
    name = source.removeprefix("did:")
    return next((e for e in did_map.dids if e.name == name), None)


def did_view_refusal(view: DidView, *, did_map: DidMap | None) -> str | None:
    """Why ``view`` cannot be computed from ``did_map``, or ``None``.

    ``did_map`` is the catalog that decoded the frame. Refused when there is
    none; when the source DID is not in it; when its entry has a ``scale`` or
    unit of its own (the frame then holds scaled values, and the view would
    scale them twice); and unless exactly one of its evidence records has
    ``method == view.scale_method``, a ``constant``, and a ``constant_unit``
    of the form ``"<unit> per count"``.
    """
    if did_map is None:
        return f"{view.channel}: no DID catalog decoded this frame, so {view.source} has no unit"
    entry = _entry(did_map, view.source)
    if entry is None:
        return f"{view.channel}: {view.source} is not in the DID catalog that decoded the frame"
    if entry.scale is not None or entry.unit.strip().lower() not in _NO_UNIT:
        return (
            f"{view.channel}: {view.source} is already scaled by the DID catalog that decoded "
            f"the frame (scale {entry.scale}, unit {entry.unit!r}), so its column is not raw "
            "counts and the view would scale it twice (P02.14)"
        )
    records = [e for e in entry.evidence if e.method == view.scale_method]
    if len(records) != 1:
        return (
            f"{view.channel}: {view.source} has {len(records)} evidence records with method "
            f"{view.scale_method!r}; the scale must come from exactly one"
        )
    record = records[0]
    match = _PER_COUNT.match((record.constant_unit or "").strip())
    if record.constant is None or match is None:
        return (
            f"{view.channel}: the {view.scale_method!r} evidence of {view.source} records no "
            f"per-count constant (constant {record.constant}, constant_unit "
            f"{record.constant_unit!r}; expected '<unit> per count')"
        )
    return None


def did_view_scale(view: DidView, *, did_map: DidMap | None) -> DidViewScale | None:
    """``view``'s scale and unit from its source's evidence in ``did_map``, or
    ``None`` when :func:`did_view_refusal` refuses it."""
    if did_map is None or did_view_refusal(view, did_map=did_map) is not None:
        return None
    entry = _entry(did_map, view.source)
    assert entry is not None
    record = next(e for e in entry.evidence if e.method == view.scale_method)
    match = _PER_COUNT.match((record.constant_unit or "").strip())
    assert record.constant is not None and match is not None
    return DidViewScale(view=view, scale=float(record.constant), unit=match["unit"])


def catalog_channel_unit(channel: str, *, did_map: DidMap | None) -> str | None:
    """``channel``'s unit per its catalog, or ``None`` when no catalog gives one.

    ``did_map`` is the DID catalog that decoded the frame (``None``: none did).
    ``unknown`` is returned as the catalog spells it, and the caller treats it
    as no unit.
    """
    if channel.startswith("obd:"):
        return _MODE01_UNITS.get(channel)
    if channel.startswith("did:"):
        entry = None if did_map is None else _entry(did_map, channel)
        return None if entry is None else entry.unit
    if channel.startswith("mem:"):
        if did_map is None:
            return None
        name = channel.removeprefix("mem:")
        return next((m.unit for m in did_map.memory if m.name == name), None)
    if channel.startswith("derived:"):
        view = next((v for v in did_views() if v.channel == channel), None)
        if view is None:
            return None
        scaled = did_view_scale(view, did_map=did_map)
        return None if scaled is None else scaled.unit
    return None


def channel_units_for(did_map: DidMap | None) -> Callable[[str], str | None]:
    """:func:`catalog_channel_unit` bound to the DID catalog that decoded the
    frame: the ``channel_units`` resolver ``src.signals.afr`` takes."""
    return partial(catalog_channel_unit, did_map=did_map)


def append_did_views(frame: pd.DataFrame, *, did_map: DidMap | None) -> pd.DataFrame:
    """Append each vehicle DID view whose source column is in ``frame`` (P02.14).

    ``did_map`` is the catalog that decoded ``frame``. The view is the source
    times its scale there (:func:`did_view_scale`). Its ``__gap`` and
    ``__arrived`` flags are the source's, since it is the same reading. A view
    :func:`did_view_refusal` refuses is not emitted. Returns ``frame`` itself
    when there is nothing to add.
    """
    additions: dict[str, pd.Series] = {}
    for view in did_views():
        if view.source not in frame.columns:
            continue
        scaled = did_view_scale(view, did_map=did_map)
        if scaled is None:
            continue
        values = pd.to_numeric(frame[view.source], errors="coerce").astype(float)
        additions[view.channel] = values * scaled.scale
        source_gap = gap_column(view.source)
        additions[gap_column(view.channel)] = (
            frame[source_gap].fillna(True).astype(bool) | values.isna()
            if source_gap in frame.columns
            else values.isna()
        )
        source_arrived = arrived_column(view.source)
        if source_arrived in frame.columns:
            additions[arrived_column(view.channel)] = (
                frame[source_arrived].fillna(False).astype(bool)
                & ~additions[gap_column(view.channel)]
            )
    if not additions:
        return frame
    out = frame.drop(columns=[c for c in additions if c in frame.columns])
    return pd.concat([out, pd.DataFrame(additions, index=frame.index)], axis=1)


__all__ = [
    "DidViewScale",
    "append_did_views",
    "catalog_channel_unit",
    "channel_units_for",
    "did_view_refusal",
    "did_view_scale",
]
