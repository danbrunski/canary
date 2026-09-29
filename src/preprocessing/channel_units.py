"""The unit a wide-frame column's values are in, from the repo's catalogs (P02.13).

One resolver for a question that ``src.signals`` cannot answer itself: the
catalogs sit above it (the Mode 01 registry is in ``collection``), so the
featurization layer resolves the unit here and hands
:func:`catalog_channel_unit` to :func:`src.signals.afr.append_afr_channels`.
The target-AFR lookup refuses a load-axis channel unless this unit is the
map's ``load_axis_unit``. A vehicle's declaration carries no unit of its own
to trust.

Covered namespaces, each the unit the frame's values are actually in:

* ``obd:`` -- the Mode 01 registry's unit (``MODE01_REGISTRY``);
* ``did:`` -- ``data/dids.yaml`` ``dids:`` (``unknown`` while unscaled: the
  frame then holds raw counts);
* ``mem:`` -- ``data/dids.yaml`` ``memory:``.

Everything else resolves to ``None``, which the lookup refuses. That includes
``decoded:``, whose frame rows are raw counts by design whatever unit the
binding names, and ``derived:``, which has no unit catalog.
"""

from __future__ import annotations

from functools import lru_cache
from pathlib import Path

from src.collection.obd_registry import MODE01_REGISTRY
from src.decode.dids import load_did_map
from src.paths import DIDS_YAML

_MODE01_UNITS: dict[str, str] = {
    f"obd:{definition.channel}": definition.unit for definition in MODE01_REGISTRY.values()
}


@lru_cache(maxsize=4)
def _did_catalog_units(path: Path, mtime_ns: int) -> dict[str, str]:
    del mtime_ns
    catalog = load_did_map(path)
    units = {f"did:{entry.name}": entry.unit for entry in catalog.dids}
    units.update({f"mem:{channel.name}": channel.unit for channel in catalog.memory})
    return units


def catalog_channel_unit(channel: str, *, dids_path: Path = DIDS_YAML) -> str | None:
    """``channel``'s unit per its catalog, or ``None`` when no catalog gives one.

    ``unknown`` is returned as the catalog spells it, and the caller treats it
    as no unit.
    """
    if channel.startswith("obd:"):
        return _MODE01_UNITS.get(channel)
    if channel.startswith(("did:", "mem:")):
        if not dids_path.is_file():
            return None
        return _did_catalog_units(dids_path, dids_path.stat().st_mtime_ns).get(channel)
    return None


__all__ = ["catalog_channel_unit"]
