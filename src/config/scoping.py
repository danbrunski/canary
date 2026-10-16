"""Path rebasing and the profile-scope refusal, both rootward of ``CanaryConfig`` (R21).

``rebase_state_paths`` reads the ``Path`` fields off ``DaemonConfig``;
``require_profile_scoped`` reads ``CanaryConfig.scoped_profile``. They sit
above the root schema so no schema imports them.
"""

from __future__ import annotations

from collections.abc import Iterable
from pathlib import Path
from typing import Any, get_args

from src.config.daemon import DaemonConfig
from src.config.root import CanaryConfig


class UnscopedConfigError(ValueError):
    """A vehicle-data reader was handed a config ``scope_config`` never scoped.

    With profiles enabled, every vehicle-specific path (``afr_calibration`` and
    the files beside it) exists only under ``<base>/<slug>/``; the unscoped path
    names a file that is absent, and every reader treats absent as "this
    vehicle has none" -- silently.
    """


def require_profile_scoped(config: CanaryConfig, *, reader: str) -> None:
    """Refuse ``config`` for ``reader`` unless it is profile-scoped (P02.11).

    Unscoped is correct only with ``profiles.enabled: false``, where the base
    paths are the vehicle's. The incident this names: the daemon scopes its
    config in ``main`` and so applied the vehicle's ``afr_calibration.yaml``
    while the trainers and the evaluation harness featurized through the
    unscoped config, where ``data/afr_calibration.yaml`` does not exist -- every
    offline feature frame was built without the calibration the served one had.
    """
    if config.profiles.enabled and config.scoped_profile is None:
        raise UnscopedConfigError(
            f"{reader} needs a profile-scoped config (src.vehicle.profiles.scope_config): with "
            "profiles enabled the vehicle's data lives under <base>/<slug>/, and "
            f"{config.daemon.afr_calibration} is the unscoped path. The trainers and "
            "the evaluation harness once featurized through it without the vehicle's "
            "afr_calibration.yaml while the daemon applied it (P02.11). Scope the "
            "config once at the entry point (resolve_profile_for_tooling + "
            "scope_config), or disable profiles."
        )


def _daemon_path_fields() -> tuple[str, ...]:
    """Every ``Path`` or ``Path | None`` :class:`DaemonConfig` field, read off
    the model.

    Any other annotation that contains ``Path`` (``list[Path]``, a dict of
    them, ...) is refused rather than skipped: :func:`rebase_state_paths`
    would otherwise leave those paths pointing wherever they were, silently.
    """
    fields: list[str] = []
    for name, field in DaemonConfig.model_fields.items():
        annotation = field.annotation
        if annotation is Path:
            fields.append(name)
            continue
        args = get_args(annotation)
        if args and set(args) == {Path, type(None)}:
            fields.append(name)
        elif _mentions_path(annotation):
            raise TypeError(
                f"DaemonConfig.{name}: {annotation!r} holds paths rebase_state_paths "
                "cannot rebase; use Path or Path | None, or teach it this shape"
            )
    return tuple(fields)


def _mentions_path(annotation: Any) -> bool:
    return annotation is Path or any(_mentions_path(arg) for arg in get_args(annotation))


def rebase_state_paths(
    config: CanaryConfig, root: Path, *, keep: Iterable[str] = ()
) -> CanaryConfig:
    """``config`` with every daemon file/directory path moved under ``root``.

    The field list is derived from :class:`DaemonConfig`'s ``Path``-typed
    fields, so a path added there later is rebased without anyone remembering
    to list it (R07.2: a hand-kept 12-entry copy in the validation harness had
    already missed ``capture_profiles``). A relative path keeps its shape under
    ``root`` (``data/control/audit.jsonl`` -> ``root/data/control/audit.jsonl``);
    an absolute one is re-rooted the same way, so two fields never collide.

    ``keep`` names fields left where they are -- read-only inputs such as
    ``behavior_models`` that the caller wants to read from the real tree. A
    name that is not a ``Path`` field of :class:`DaemonConfig` is refused, so
    a typo cannot silently leave a writable path pointing at the repo.
    """
    fields = _daemon_path_fields()
    kept = frozenset(keep)
    unknown = kept - set(fields)
    if unknown:
        raise ValueError(
            f"rebase_state_paths: {sorted(unknown)} are not Path fields of DaemonConfig "
            f"(known: {sorted(fields)})"
        )
    update: dict[str, Path] = {}
    for name in fields:
        if name in kept:
            continue
        path: Path | None = getattr(config.daemon, name)
        if path is None:
            continue
        update[name] = root / (path.relative_to(path.anchor) if path.is_absolute() else path)
    return config.model_copy(update={"daemon": config.daemon.model_copy(update=update)})
