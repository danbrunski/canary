"""Which vehicle is this? Active-profile resolution from the VIN probe.

The probe is a caller-supplied :data:`VehicleInfoProber` (the Mode 09 read
transmits, so it lives in :mod:`src.collection.vehicle_probe`). Resolution
registers an unknown VIN, restores a profile this machine deleted when the start
resolves to it, refreshes the stored calibration ID and CVN (a changed ID is an
ECU reflash and a corpus boundary), and finishes by scoping the config and
running the one-time legacy-calibration migration. It sits on top of the rest of
the package and nothing imports it.
"""

from __future__ import annotations

import logging
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path

from src.atomic_write import atomic_write_text
from src.config import CanaryConfig
from src.vehicle.profiles.edit import (
    ProfileResolution,
    _hidden_slugs,
    record_resolution,
    restore_profile,
)
from src.vehicle.profiles.legacy import migrate_legacy_calibration
from src.vehicle.profiles.model import (
    VehicleProfile,
    _dedupe_slug,
    _default_profile,
    load_registry,
    save_registry,
    slugify,
)
from src.vehicle.profiles.scope import _scope_file, profile_dir, scope_config

log = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# VIN probe result
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class VehicleInfoProbe:
    """Result of the startup Mode 09 probe (Task 3.4 extends the VIN-only read).

    The probe itself transmits, so it lives in
    :func:`src.collection.vehicle_probe.probe_vehicle_info`; resolution takes
    it from its caller as a :data:`VehicleInfoProber` (R09.5).
    """

    vin: str | None
    calibration_id: str | None
    cvn: str | None


#: How resolution reads the vehicle: the caller's Mode 09 probe
#: (:func:`src.collection.vehicle_probe.probe_vehicle_info` on the daemon).
VehicleInfoProber = Callable[[CanaryConfig], VehicleInfoProbe]


# ---------------------------------------------------------------------------
# Resolution
# ---------------------------------------------------------------------------


def _new_profile_for_vin(vin: str, *, taken: set[str]) -> VehicleProfile:
    """A freshly-seen vehicle: stable slug from the VIN, blank display name for
    the operator to fill in from the dashboard."""
    return VehicleProfile(slug=_dedupe_slug(slugify(vin), taken), vin=vin, name="")


def _maybe_persist(
    config: CanaryConfig,
    profiles: list[VehicleProfile],
    profile: VehicleProfile,
) -> None:
    """Add ``profile`` to the registry if its slug is new, so the active vehicle
    is always listable/nameable from the dashboard. No-op when
    ``profiles.auto_register`` is off or the slug already exists."""
    if not config.profiles.auto_register:
        return
    if any(p.slug == profile.slug for p in profiles):
        return
    profiles.append(profile)
    save_registry(config.profiles, profiles)
    log.warning(
        "registered vehicle profile %r (vin=%s) — set a display name in the "
        "dashboard Profiles panel",
        profile.slug,
        profile.vin or "unknown",
    )


def _apply_calibration_probe(
    config: CanaryConfig,
    profiles: list[VehicleProfile],
    profile: VehicleProfile,
    probe: VehicleInfoProbe,
) -> None:
    """Update ``profile``'s stored calibration ID/CVN from a fresh Mode 09
    probe (Task 3.4), persisting the registry when either changed.

    A changed calibration ID for an *already-registered* profile (i.e. one
    that had a non-blank stored value already) is an ECU reflash — logged as
    a ``corpus_boundary`` vehicle-ledger event, the same mechanism
    ``eval-longitudinal-and-dtc.md``'s Task 3.5 corpus-boundary integration
    already reads (``VehicleLedgerStore.latest_boundary`` / the
    ``--since-boundary`` corpus filter). A brand-new profile's first
    calibration read is just a baseline, not a boundary.
    """
    if not probe.calibration_id and not probe.cvn:
        return  # nothing read this probe; keep whatever was last stored
    previous_calibration_id = profile.calibration_id
    reflashed = bool(
        previous_calibration_id
        and probe.calibration_id
        and probe.calibration_id != previous_calibration_id
    )
    before = (profile.calibration_id, profile.cvn)
    if probe.calibration_id:
        profile.calibration_id = probe.calibration_id
    if probe.cvn:
        profile.cvn = probe.cvn
    if (profile.calibration_id, profile.cvn) != before:
        save_registry(config.profiles, profiles)
    if reflashed:
        _record_calibration_boundary(config, profile, previous_calibration_id)


def _record_calibration_boundary(
    config: CanaryConfig,
    profile: VehicleProfile,
    previous_calibration_id: str,
) -> None:
    """Append a ``corpus_boundary`` vehicle-ledger event for an ECU reflash
    (Task 3.4). Scoped straight to this profile's ledger path via
    ``_scope_file`` rather than a full ``scope_config`` call — this runs
    from inside ``resolve_active_profile``, before its caller has scoped
    ``config`` (the profile isn't resolved yet at that point).
    """
    from src.vehicle.vehicle_ledger import VehicleLedgerStore

    ledger = VehicleLedgerStore(_scope_file(config.daemon.vehicle_ledger, profile.slug))
    ledger.record(
        occurred_at=datetime.now(UTC).date(),
        kind="mod",
        title=f"ECU calibration changed: {previous_calibration_id} -> {profile.calibration_id}",
        details=f"cvn={profile.cvn or 'unknown'}",
        corpus_boundary=True,
    )
    log.warning(
        "ECU calibration ID changed (reflash detected) — corpus-boundary ledger entry appended",
        extra={
            "slug": profile.slug,
            "previous_calibration_id": previous_calibration_id,
            "calibration_id": profile.calibration_id,
        },
    )


def _restore_if_hidden(
    config: CanaryConfig,
    profiles: list[VehicleProfile],
    profile: VehicleProfile,
    *,
    how: str,
) -> None:
    """Un-delete ``profile`` when this machine hid it, and add it to ``profiles``.

    A start that resolves to a deleted profile (its VIN read, or the
    ``vehicle:`` block naming it, or ``--profile``) is the daemon running on it
    whatever the registry says, so the registry is made to say so. ``how`` is
    the log line's account of why.
    """
    if profile.slug not in _hidden_slugs(config.profiles):
        return
    restore_profile(config.profiles, profile.slug)
    profiles.append(profile)
    log.warning(
        "this start resolved to vehicle profile %r, which was deleted on this machine "
        "(%s), so it was restored",
        profile.slug,
        how,
    )


def _resolve_active_profile(
    config: CanaryConfig,
    *,
    probe: VehicleInfoProber,
    override_slug: str | None = None,
) -> VehicleProfile:
    """Determine which vehicle this is and return its profile.

    * ``override_slug`` (CLI ``--profile``) → that registry entry, or a
      synthetic default under that slug; the VIN probe is skipped.
    * else read the VIN with ``probe``; a registry hit returns that profile;
      an unknown VIN is registered with a blank display name for the operator
      to fill in.
    * no VIN (disabled / virtual / probe failed) → the default profile: the
      registry entry under the ``vehicle:`` block's slug, else one built from it.

    The resolved profile is persisted to the registry (when ``auto_register``)
    if not already present, so the dashboard can always show and rename it.
    A registered profile's calibration ID/CVN (Task 3.4) is refreshed from the
    same probe; a changed calibration ID is logged as a corpus-boundary event.

    Matching looks at every profile, including the tracked ones this machine
    deleted: resolution never runs on a hidden profile, so one that matches is
    restored (:func:`_restore_if_hidden`) rather than skipped, which would
    register a duplicate of the vehicle under a new slug.
    """
    registry = load_registry(config.profiles, include_deleted=True)
    hidden = _hidden_slugs(config.profiles)
    profiles = [p for p in registry if p.slug not in hidden]
    by_slug = {p.slug: p for p in registry}
    by_vin = {p.vin: p for p in registry if p.vin}
    fallback = _default_profile(config).slug

    if override_slug is not None:
        record_resolution(ProfileResolution(override_slug, "override", None, fallback))
        if override_slug in by_slug:
            profile = by_slug[override_slug]
            _restore_if_hidden(config, profiles, profile, how="the --profile override named it")
            return profile
        profile = _default_profile(config, slug=override_slug)
        _maybe_persist(config, profiles, profile)
        return profile

    if not config.profiles.enabled:
        profile = _default_profile(config)
        record_resolution(ProfileResolution(profile.slug, "profiles_disabled", None, fallback))
        return profile

    vin_probe = probe(config)
    vin = vin_probe.vin
    if vin is None:
        profile = _registered_default(config, by_slug)
        record_resolution(ProfileResolution(profile.slug, "no_vin_read", None, fallback))
        _restore_if_hidden(
            config, profiles, profile, how="no VIN was read and the vehicle: block names it"
        )
        _maybe_persist(config, profiles, profile)
        return profile

    if vin in by_vin:
        profile = by_vin[vin]
        record_resolution(ProfileResolution(profile.slug, "vin_matched", vin, fallback))
        _restore_if_hidden(config, profiles, profile, how=f"the vehicle reported its VIN {vin}")
    else:
        profile = _new_profile_for_vin(vin, taken=set(by_slug))
        record_resolution(ProfileResolution(profile.slug, "vin_registered", vin, fallback))
        _maybe_persist(config, profiles, profile)

    # Only touch calibration bookkeeping for a profile that's actually in the
    # persisted registry — `_maybe_persist` above is a no-op when
    # `auto_register` is off, and an unregistered profile has no registry row
    # to update (mirrors that gate rather than silently rewriting the file).
    if any(p.slug == profile.slug for p in profiles):
        _apply_calibration_probe(config, profiles, profile, vin_probe)
    return profile


def _registered_default(config: CanaryConfig, by_slug: dict[str, VehicleProfile]) -> VehicleProfile:
    """The fallback profile: the registry's entry under the ``vehicle:`` block's
    slug when there is one, else one built from that block.

    Until 2026-10-10 the fallback was always built from the block, so a start
    that read no VIN (key off, parked, bench) ran on ``canary.yaml``'s vehicle
    parameters, often the code defaults, while the dashboard showed the
    registry's: a dashboard edit had no effect, and the dyno added payload to a
    mass other than the one it displayed.
    """
    default = _default_profile(config)
    return by_slug.get(default.slug, default)


def resolve_active_profile(
    config: CanaryConfig,
    *,
    probe: VehicleInfoProber,
    override_slug: str | None = None,
) -> VehicleProfile:
    """Determine which vehicle this is and return its profile (the daemon's path).

    See :func:`_resolve_active_profile`. Also migrates, once, the pre-R05.15
    shared calibration directory into the resolved profile's own when the
    profile owns it (:func:`migrate_legacy_calibration`). A migration failure
    is logged, never fatal: the read-only fallback still finds the files.
    """
    profile = _resolve_active_profile(config, probe=probe, override_slug=override_slug)
    scoped = scope_config(config, profile)
    try:
        migrate_legacy_calibration(scoped)
    except (OSError, ValueError) as exc:
        log.warning("legacy calibration migration failed: %s", exc)
    try:
        seed_machine_owned_files(scoped)
    except OSError as exc:
        log.warning("seeding machine-owned files failed: %s", exc)
    return profile


#: Files a machine owns once it has them, seeded from the tracked profile
#: directory the first time (S03.3). ``behavior_models.yaml``: the Pi's refit
#: job rewrites it, so it is a trained artifact, Pi-owned (operator decision
#: 2026-10-04); the tracked copy is only where a fresh install starts.
SEEDED_FROM_PROFILE: tuple[str, ...] = ("behavior_models",)


def seed_machine_owned_files(config: CanaryConfig) -> list[Path]:
    """Copy each :data:`SEEDED_FROM_PROFILE` file from the tracked profile
    directory to its configured location when that is somewhere else and empty.

    Never overwrites: after the first copy the machine's file is authoritative,
    and a laptop-trained replacement arrives the way models do (upload or
    ``scp``), not through git. Returns the files written.
    """
    written: list[Path] = []
    for name in SEEDED_FROM_PROFILE:
        target: Path = getattr(config.daemon, name)
        source = profile_dir(config) / target.name
        if target.exists() or not source.exists() or target.resolve() == source.resolve():
            continue
        atomic_write_text(target, source.read_text(), mkdir=True)
        log.info(
            "seeded %s from the tracked profile",
            name,
            extra={"source": str(source), "target": str(target)},
        )
        written.append(target)
    return written


def resolve_profile_for_tooling(config: CanaryConfig, slug: str | None) -> VehicleProfile:
    """Resolve a profile for offline tools (refit, migration) WITHOUT a VIN probe
    and WITHOUT mutating the registry (so a dry-run stays read-only).

    ``--profile <slug>`` selects a registry entry (or a synthetic default under
    that slug); otherwise the ``vehicle:`` block's default profile, as the
    registry holds it when it does (:func:`_registered_default`, the daemon's
    own fallback). A
    config already scoped (P02.11) resolves to the profile it was scoped to, so
    re-resolving downstream of an entry point's ``scope_config`` is a no-op.
    """
    # Hidden profiles are read too, never restored (a dry run stays read-only):
    # their data is still on disk, and the daemon restores one at the start
    # that resolves to it, so this is the profile that start would use.
    registry = load_registry(config.profiles, include_deleted=True)
    slug = slug if slug is not None else config.scoped_profile
    if slug is not None:
        for profile in registry:
            if profile.slug == slug:
                return profile
        return _default_profile(config, slug=slug)
    if not config.profiles.enabled:
        return _default_profile(config)
    return _registered_default(config, {p.slug: p for p in registry})
