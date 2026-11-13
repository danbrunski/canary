"""What the serving (``default``) capture profile polls (R23.12).

The readers a trainer asks before accepting a ``did:`` or Mode 01 feature.
They were two copies of the same fifteen lines, with a cache dict each, in the
module the poll loop imports; :func:`_stamp_cached` is the one cache-by-mtime
they share. Re-exported from ``capture_profiles``.
"""

from __future__ import annotations

from collections.abc import Callable

from src.collection.capture_profile_files import (
    CaptureProfileSources,
    _read_catalog,
    layer_catalog,
    load_overrides,
    seed_catalog,
)
from src.collection.capture_profile_model import DEFAULT_PROFILE_NAME, CaptureProfile
from src.config import CollectionConfig

#: ``(question, sources)`` -> ``(both files' mtimes, answer)``. Keyed by the
#: mtimes, so an entry is never stale; ``question`` keeps the Mode 22 and Mode 01
#: answers for one ``sources`` apart.
_SERVING_CACHE: dict[
    tuple[str, CaptureProfileSources], tuple[tuple[int, int | None], frozenset[str] | None]
] = {}

#: In a :func:`serving_mode01_channels` answer: every supported PID is polled.
MODE01_AUTO = "AUTO"


def _serving_default(
    sources: CaptureProfileSources, collection: CollectionConfig | None
) -> tuple[bool, CaptureProfile | None]:
    """``(resolved, default profile)`` for the serving-set readers, read-only."""
    base = _read_catalog(sources.catalog)
    if base is None:
        if collection is None:
            override = load_overrides(sources.overrides)
            default = next((p for p in override.profiles if p.name == DEFAULT_PROFILE_NAME), None)
            return default is not None, default
        base = seed_catalog(collection)
    return True, layer_catalog(base, load_overrides(sources.overrides)).by_name(
        DEFAULT_PROFILE_NAME
    )


def _sources_stamp(sources: CaptureProfileSources) -> tuple[int, int | None] | None:
    """Both files' mtimes, or ``None`` when the tracked catalog is absent (then
    the answer depends on the collection config, so it is not cached)."""
    try:
        catalog = sources.catalog.stat().st_mtime_ns
    except FileNotFoundError:
        return None
    overrides = None
    if sources.overrides is not None:
        try:
            overrides = sources.overrides.stat().st_mtime_ns
        except FileNotFoundError:
            overrides = None
    return catalog, overrides


def _stamp_cached(
    question: str,
    sources: CaptureProfileSources,
    collection: CollectionConfig | None,
    answer: Callable[[CaptureProfile | None], frozenset[str]],
) -> frozenset[str] | None:
    """``answer(serving default)`` once per change of either source file.

    ``None`` is *unresolvable* (no catalog on disk and no collection config to
    seed one from); ``answer`` runs only when the default could be resolved, and
    gets ``None`` when the resolved catalog has no ``default``. With no tracked
    catalog the answer depends on the collection config, so it is not cached.
    """
    stamp = _sources_stamp(sources)
    key = (question, sources)
    cached = _SERVING_CACHE.get(key)
    if stamp is not None and cached is not None and cached[0] == stamp:
        return cached[1]
    resolved, default = _serving_default(sources, collection)
    polled = answer(default) if resolved else None
    if stamp is not None:
        _SERVING_CACHE[key] = (stamp, polled)
    return polled


def _mode22_selection(default: CaptureProfile | None) -> frozenset[str]:
    if default is None or not default.mode22_enabled:
        return frozenset()
    return frozenset(item.strip().upper() for item in default.mode22 if item.strip())


def _mode01_selection(profile: CaptureProfile | None) -> frozenset[str]:
    if profile is None or not profile.mode01_enabled:
        return frozenset()
    return frozenset(name.upper() for name in profile.mode01)


def serving_mode22_dids(
    sources: CaptureProfileSources, collection: CollectionConfig | None = None
) -> frozenset[str] | None:
    """Upper-cased Mode-22 DID names the *serving* (``default``) profile polls.

    The question a trainer has to ask before accepting a ``did:`` feature: will
    the daemon ever observe this channel while it is scoring? Only the
    ``default`` profile runs during ordinary (model-serving) collection; a DID
    that exists in the corpus solely because a ``_capture-*`` research profile
    polled it is NaN forever at serve.

    Read-only: feature selection must not rewrite operator data. The answer is
    the effective ``default`` (tracked catalog under this machine's overrides,
    S03.4); with no catalog the ``default`` profile *is* ``collection.obd_dids``
    (see :func:`seed_catalog`). A laptop does not see the Pi's overrides, so a
    ``default`` edited in the Pi's dashboard is invisible to a laptop retrain.

    ``None`` means *unresolvable* — no catalog on disk and no collection config
    to seed one from — which is a different answer from an empty set ("the
    serving profile polls no DIDs"). Callers must not conflate them.
    """
    return _stamp_cached("mode22", sources, collection, _mode22_selection)


def serving_mode01_channels(
    sources: CaptureProfileSources, collection: CollectionConfig | None = None
) -> frozenset[str] | None:
    """Upper-cased Mode 01 channel names the serving (``default``) profile polls.

    :func:`serving_mode22_dids`'s question for Mode 01: a PID that the serving
    profile no longer polls is NaN for a model's whole serving life, however
    much of the old corpus carries it. The Xterra ``default`` dropped ten PIDs
    on 2026-09-26 (D03.34), and without this answer the next retrain would have
    learned them. Contains :data:`MODE01_AUTO` when the profile polls every
    supported PID. Empty when Mode 01 is disabled. ``None`` is unresolvable,
    as for DIDs. Same layering and precedence.
    """
    return _stamp_cached("mode01", sources, collection, _mode01_selection)


__all__ = ["MODE01_AUTO", "serving_mode01_channels", "serving_mode22_dids"]
