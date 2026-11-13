"""Strict, vehicle-scoped capture profile persistence.

Capture profiles describe *collection*, not vehicle identity.  The catalog is
stored beside the active vehicle's other scoped artifacts and is deliberately
small enough to reload on every API operation.

The module is a facade (R23.12). The schema is ``capture_profile_model``, the
catalog layering and files are ``capture_profile_files``, and the
serving-profile readers are ``capture_profile_serving``; every public name is
re-exported here, so ``from src.collection.capture_profiles import ...`` keeps
working. What stays is :class:`CaptureProfileStore`, which edits the layers.
"""

from __future__ import annotations

from pathlib import Path
from typing import TYPE_CHECKING, Any

from src.collection import capture_profile_files, capture_profile_model
from src.collection.capture_profile_files import (
    STARTER_CATALOG_PATH,
    CaptureProfileOverrides,
    CaptureProfileSources,
    _read_catalog,
    default_profile,
    layer_catalog,
    load_capture_profiles,
    load_overrides,
    save_capture_profiles,
    save_overrides,
    seed_catalog,
)
from src.collection.capture_profile_model import (
    CAPTURE_CONTEXT_MODE01,
    DEFAULT_PROFILE_NAME,
    FUEL_DELIVERY_TUPLE,
    MAX_TARGET_RATE_HZ,
    SCHEMA_VERSION,
    STORAGE_SAFE_REQUESTS_PER_SECOND,
    CaptureProfile,
    CaptureProfileCatalog,
    channel_satisfied,
    profile_digest,
)
from src.collection.capture_profile_serving import (
    MODE01_AUTO,
    serving_mode01_channels,
    serving_mode22_dids,
)
from src.config import CollectionConfig
from src.decode.dids import did_ids_for_tokens, refuse_side_effect_dids, select_memory_channels

if TYPE_CHECKING:
    from src.collection.kline.plan import KlineCatalog


def _reset_for_tests() -> None:
    """Forget that the ``broadcast_decode`` retirement and any shadowed
    override were already reported (``src.collection.process_state``)."""
    capture_profile_model._reset_for_tests()
    capture_profile_files._reset_for_tests()


class CaptureProfileStore:
    def __init__(
        self,
        sources: CaptureProfileSources,
        collection: CollectionConfig,
        behavior_models_path: Path,
        *,
        custom_pids_path: Path | None = None,
        kline_catalog: KlineCatalog | None = None,
    ):
        self.sources = sources
        self.collection = collection
        self.behavior_models_path = behavior_models_path
        #: The daemon's custom-PID file, so the preview's request universe
        #: matches the live one. ``None`` previews without custom PIDs.
        self.custom_pids_path = custom_pids_path
        #: What a profile's ``kline:`` list is resolved against at save
        #: (H05.8). ``None`` refuses to save any profile that lists K-line
        #: channels: an unchecked list is what left the collector idle.
        self.kline_catalog = kline_catalog

    def load(self) -> CaptureProfileCatalog:
        return load_capture_profiles(self.sources, self.collection)

    def _base(self) -> CaptureProfileCatalog:
        return _read_catalog(self.sources.catalog) or seed_catalog(self.collection)

    def _overrides_path(self) -> Path:
        if self.sources.overrides is None:
            raise ValueError("this capture profile store is read-only (no overrides file)")
        return self.sources.overrides

    def resolved_plan(self, profile: CaptureProfile) -> dict[str, Any]:
        """The identifier-level plan ``profile`` would run: the live plan, asked early.

        One function with the live metrics (``obd_scheduler.preview_polling_plan``
        runs the scheduler's own spec, request and plan builders with the
        candidate in place of the active profile). The copy this replaced
        disagreed with live on DID-window precedence, capped DID rates live
        does not, and ignored ``round_timestamps`` (R02.3). The import is local
        because the scheduler already depends on this profile module.
        """
        from src.collection.obd_scheduler import preview_polling_plan

        return preview_polling_plan(
            self.collection, profile, custom_pids_path=self.custom_pids_path
        )

    def save(
        self,
        profile: CaptureProfile,
        *,
        previous_name: str | None = None,
    ) -> None:
        """Save ``profile``. Which behavior models it would starve is
        :func:`src.features.capture_inputs.model_input_warnings`'s to say: the
        behavior models sit above collection (R09.6)."""
        catalog = self.load()
        target = (previous_name or profile.name).strip().lower()
        profiles = [item for item in catalog.profiles if item.name != target]
        if (
            previous_name
            and target == DEFAULT_PROFILE_NAME
            and profile.name != DEFAULT_PROFILE_NAME
        ):
            raise ValueError("the default profile cannot be renamed")
        if any(item.name == profile.name for item in profiles):
            raise ValueError(f"capture profile already exists: {profile.name}")
        self._check_memory(profile)
        self._check_kline(profile)
        refuse_side_effect_dids(
            did_ids_for_tokens(profile.mode22), where=f"capture profile {profile.name!r}"
        )
        # Only this machine's layer is written (S03.4); the tracked catalog never is.
        path = self._overrides_path()
        base = {item.name: item for item in self._base().profiles}
        overrides = load_overrides(path)
        kept = [item for item in overrides.profiles if item.name not in {target, profile.name}]
        deleted = [name for name in overrides.deleted if name != profile.name]
        digests = {
            name: digest
            for name, digest in overrides.base_digests.items()
            if name not in {target, profile.name}
        }
        if target != profile.name and target in base:
            deleted.append(target)  # a rename hides the tracked original
        if profile.name in base:
            digests[profile.name] = profile_digest(base[profile.name])
        save_overrides(
            path,
            CaptureProfileOverrides(
                profiles=sorted([*kept, profile], key=lambda item: item.name),
                deleted=sorted(set(deleted)),
                base_digests=digests,
            ),
        )

    def _check_memory(self, profile: CaptureProfile) -> None:
        """Refuse a ``memory:`` entry the catalog does not hold.

        The daemon start refuses the same name (``profile_memory_union``), so
        an override saved with it would keep the next start from collecting
        at all; refusing here keeps it out of the overrides file.
        """
        try:
            select_memory_channels(profile.memory)
        except ValueError as exc:
            raise ValueError(f"memory: {exc}") from exc

    def _check_kline(self, profile: CaptureProfile) -> None:
        """Resolve ``profile.kline`` exactly as the K-line collector will
        (:meth:`~src.collection.kline.plan.KlineCatalog.resolve`), or refuse.

        The collector refuses an unreadable list at run time, logged once, and
        the capture then records no K-line at all; refusing here puts the same
        reason in front of the operator while the profile is still open.
        """
        if not profile.kline:
            return
        if self.kline_catalog is None:
            raise ValueError(
                f"capture profile {profile.name} lists K-line channels, but this store "
                "has no K-line catalog to check them against"
            )
        try:
            self.kline_catalog.resolve(profile.kline)
        except (LookupError, ValueError) as exc:
            # KlinePlanError is a ValueError; a vehicle with no K-line profile
            # is a LookupError. Both are a refusal of this list.
            raise ValueError(f"kline: {exc}") from exc

    def delete(self, name: str) -> None:
        normalized = name.strip().lower()
        if normalized == DEFAULT_PROFILE_NAME:
            raise ValueError("the default capture profile cannot be deleted")
        if self.load().by_name(normalized) is None:
            raise KeyError(normalized)
        path = self._overrides_path()
        overrides = load_overrides(path)
        deleted = set(overrides.deleted)
        if self._base().by_name(normalized) is not None:
            deleted.add(normalized)
        save_overrides(
            path,
            CaptureProfileOverrides(
                profiles=[item for item in overrides.profiles if item.name != normalized],
                deleted=sorted(deleted),
                base_digests={
                    name: digest
                    for name, digest in overrides.base_digests.items()
                    if name != normalized
                },
            ),
        )

    def revert(self, name: str) -> CaptureProfile | None:
        """Drop this machine's override or deletion of ``name``; return the
        profile now in effect (the repo's), or ``None`` if the repo has none.

        Raises ``KeyError`` when this machine has nothing to revert for it.
        """
        normalized = name.strip().lower()
        path = self._overrides_path()
        overrides = load_overrides(path)
        overridden = any(item.name == normalized for item in overrides.profiles)
        if not overridden and normalized not in overrides.deleted:
            raise KeyError(normalized)
        save_overrides(
            path,
            CaptureProfileOverrides(
                profiles=[item for item in overrides.profiles if item.name != normalized],
                deleted=[item for item in overrides.deleted if item != normalized],
                base_digests={
                    key: digest
                    for key, digest in overrides.base_digests.items()
                    if key != normalized
                },
            ),
        )
        return self._base().by_name(normalized)

    def overridden_names(self) -> dict[str, str]:
        """``{name: "overridden" | "added" | "deleted"}`` for this machine's layer."""
        base = {item.name for item in self._base().profiles}
        overrides = load_overrides(self.sources.overrides)
        out = {
            item.name: ("overridden" if item.name in base else "added")
            for item in overrides.profiles
        }
        out.update({name: "deleted" for name in overrides.deleted})
        return out


__all__ = [
    "CaptureProfileOverrides",
    "CaptureProfileSources",
    "channel_satisfied",
    "CAPTURE_CONTEXT_MODE01",
    "DEFAULT_PROFILE_NAME",
    "MAX_TARGET_RATE_HZ",
    "MODE01_AUTO",
    "SCHEMA_VERSION",
    "STARTER_CATALOG_PATH",
    "STORAGE_SAFE_REQUESTS_PER_SECOND",
    "CaptureProfile",
    "CaptureProfileCatalog",
    "CaptureProfileStore",
    "FUEL_DELIVERY_TUPLE",
    "default_profile",
    "layer_catalog",
    "load_capture_profiles",
    "load_overrides",
    "profile_digest",
    "save_capture_profiles",
    "save_overrides",
    "seed_catalog",
    "serving_mode01_channels",
    "serving_mode22_dids",
]
