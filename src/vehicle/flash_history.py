"""The vehicle's flash history: which ``tune_revisions.yaml`` files are live, and what they say.

``active_registry`` merges the operational registry with the tracked one;
``active_flash_history`` is what ``derived:TARGET_AFR`` resolves each row's
revision from, on the live tick and in every offline frame. They moved here from
``profiles.py`` (R26.4): they read :mod:`src.vehicle.tune_revisions` and
:mod:`src.signals.afr`, and the profile package imports neither at module level.
Importing this module imports pandas; ``src.vehicle.profiles`` serves these names
lazily (:func:`src.vehicle.profiles.__getattr__`) so importing the package does not.
"""

from __future__ import annotations

from hashlib import sha256
from pathlib import Path
from typing import Any, cast

import yaml

from src.config import CanaryConfig
from src.signals.afr import FlashHistory, NoFlashHistory
from src.vehicle.profiles.legacy import operational_file
from src.vehicle.profiles.scope import source_registry_path
from src.vehicle.tune_revisions import (
    RegistryConflictError,
    TuneRevisionRegistry,
    load_tune_revisions,
    merge_tune_registries,
)


def active_registry_path(config: CanaryConfig) -> Path:
    """The operational registry file once one exists, else the tracked one.

    A *file identity*, not the flash history: the operational copy lacks any
    revision hand-added to the tracked file after it was seeded (R05.17).
    Read the history with :func:`active_registry`, which merges both; cite
    the files it read with :func:`active_registry_sources`.
    """
    override = operational_file(config, "tune_revisions.yaml")
    return override if override is not None else source_registry_path(config)


def active_registry_sources(config: CanaryConfig) -> tuple[Path, ...]:
    """The existing registry files :func:`active_registry` reads, operational first."""
    tracked = source_registry_path(config)
    operational = operational_file(config, "tune_revisions.yaml")
    paths = [p for p in (operational, tracked) if p is not None and p.exists()]
    return tuple(dict.fromkeys(paths))


def active_registry(config: CanaryConfig) -> TuneRevisionRegistry | None:
    """The vehicle's flash history: the operational registry merged with the tracked one.

    R05.17: once ``append_flash`` created the operational copy it used to win
    outright, so a revision committed to the tracked
    ``data/<profile>/tune_revisions.yaml`` afterwards (the hand-edit workflow)
    was invisible to every reader, while making the tracked file the only
    source would have dropped the dashboard-recorded flashes. Both are read;
    tracked revisions the operational copy lacks are merged in by
    ``flashed_at``, and one name recorded as two different flashes raises
    :class:`src.vehicle.tune_revisions.RegistryConflictError` naming both
    files. ``None`` when neither file exists.
    """
    tracked_path = source_registry_path(config)
    operational_path = operational_file(config, "tune_revisions.yaml")
    tracked = load_tune_revisions(tracked_path)
    if operational_path is None or operational_path == tracked_path:
        return tracked
    return merge_tune_registries(
        load_tune_revisions(operational_path), operational_path, tracked, tracked_path
    )


#: ``active_flash_history`` by the content of the files it read. The live path
#: featurizes every tick, and parsing the registry costs ~2.5 ms on the laptop.
#: Keyed by bytes, not ``st_mtime_ns``: the kernel stamps a write with its
#: coarse clock, so an in-place rewrite within one tick (~4 ms here) kept the
#: old stamp and the old history (``test_an_edited_flash_history_is_reread``,
#: 2026-10-23, once parsing got fast). The files are a few kB.
#: Since R29.9 :func:`~src.vehicle.tune_revisions.load_tune_revisions` memoizes the
#: parse by file identity, so a miss here costs the merge and a hit costs one
#: ``read_bytes`` and sha256 per file: 0.034 ms per call with the Xterra's 10 kB
#: tracked registry, 0.036 ms with an operational copy beside it (median of 300,
#: laptop; R26.4). That is why no second, stat-keyed memo sits in front of this one.
_flash_history_cache: dict[tuple[tuple[str, str], ...], Any] = {}


def active_flash_history(config: CanaryConfig) -> FlashHistory:
    """The flash history ``derived:TARGET_AFR`` resolves each row's revision
    from (P02.16): :func:`active_registry`, or a :class:`NoFlashHistory`
    naming why there is none.

    One function for the live tick and every offline frame, since both run
    ``feature_transform.append_derived_channels``. The live rows are stamped
    "now", so they resolve to the revision currently flashed, by the same rule
    (``TuneRevisionRegistry.at``) ``/control/calibration`` reports as
    ``running_revision``. Re-read when either registry file changes.
    """
    sources = active_registry_sources(config)
    try:
        key = tuple((str(path), sha256(path.read_bytes()).hexdigest()) for path in sources)
    except OSError:
        key = ()
    cached = _flash_history_cache.get(key) if key else None
    if cached is not None:
        return cast("FlashHistory", cached)
    history: FlashHistory
    try:
        registry = active_registry(config)
    except (RegistryConflictError, OSError, ValueError, yaml.YAMLError) as exc:
        history = NoFlashHistory(f"the vehicle's flash history could not be read: {exc}")
    else:
        missing = source_registry_path(config)
        history = (
            NoFlashHistory(f"no tune_revisions.yaml is recorded for this vehicle ({missing})")
            if registry is None
            else registry
        )
    if key:
        _flash_history_cache.clear()
        _flash_history_cache[key] = history
    return history
