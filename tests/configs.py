"""One way to build a test ``CanaryConfig`` that cannot write into the repo (R07.2).

``DaemonConfig`` defaults 19 writable paths to the repo tree. The per-file
``_config_yaml`` / ``_CONFIG_YAML`` copies each redirected a different subset
(``tests/test_daemon.py``'s said "every dir" and set four), so ``trip_state``
and ``catalog_db`` kept landing in ``data/control``. :func:`isolated_config`
rebases every daemon path with :func:`src.config.rebase_state_paths`, which
reads the field list off the model, so a path added later is covered too.

Overrides are applied *after* the rebase, so a test that wants a specific file
names it explicitly::

    config = isolated_config(tmp_path, daemon={"custom_pids": custom_pids_path})
"""

from __future__ import annotations

import copy
from collections.abc import Iterable, Mapping
from pathlib import Path
from typing import Any, Literal

import yaml

from src.config import CanaryConfig, VehicleParameters, _daemon_path_fields, rebase_state_paths
from src.profiles import VehicleProfile, scope_config

REPO = Path(__file__).resolve().parents[1]
EXAMPLE_YAML = REPO / "canary.example.yaml"

#: The smallest config the daemon, the dashboard and the CLIs accept: the
#: shape 13 per-file ``_CONFIG_YAML`` copies shared -- IF enabled, LSTM
#: disabled, no operating modes, a virtual CAN bus. The IF artifact is never
#: present under ``tmp_path/models``, so "enabled" only means "declared".
MINIMAL: dict[str, Any] = {
    "vehicle": {"make": "Nissan", "model": "Xterra", "year": 2008, "vin": ""},
    "hardware": {
        "elm327_port": "/dev/ttyUSB0",
        "can": {"interface": "virtual", "channel": "vcan0", "bitrate": 500000},
    },
    "collection": {
        "obd_poll_rate_hz": 1.0,
        "storage_path": "storage",
        "session_retention_days": 30,
        "storage_warning_mb": 500,
    },
    "preprocessing": {
        "resample_hz": 1.0,
        "rolling_window_seconds": 5,
        "operating_modes": {},
    },
    "models": {
        "isolation_forest": {"enabled": True, "n_estimators": 100, "contamination": 0.01},
        "lstm_autoencoder": {
            "enabled": False,
            "sequence_length": 30,
            "latent_dim": 8,
            "threshold_k": 3.0,
        },
    },
    "dashboard": {"host": "127.0.0.1", "port": 8000},
}

#: What the daemon-level ``_config_yaml`` copies changed from :data:`MINIMAL`:
#: a 30 s rolling window, one ``idle`` mode, both detectors off, port 0.
DAEMON_DELTA: dict[str, Any] = {
    "preprocessing": {
        "rolling_window_seconds": 30,
        "operating_modes": {"idle": {"rpm_max": 900, "speed_max": 0}},
    },
    "models": {
        "isolation_forest": {"enabled": False},
        "lstm_autoencoder": {"enabled": False, "sequence_length": 60, "latent_dim": 16},
    },
    "dashboard": {"port": 0},
}

Base = Literal["minimal", "daemon", "example"]

#: Daemon paths that are per-vehicle *inputs* (read, never written by the code
#: under test): pass as ``keep=`` to read the repo's real ones.
READ_ONLY_INPUTS: tuple[str, ...] = (
    "models_dir",
    "behavior_models",
    "accepted_baselines",
    "custom_pids",
    "afr_calibration",
    "kline_profile",
)


def _merge(into: dict[str, Any], overrides: Mapping[str, Any]) -> dict[str, Any]:
    for key, value in overrides.items():
        if isinstance(value, Mapping) and isinstance(into.get(key), dict):
            _merge(into[key], value)
        else:
            into[key] = value
    return into


def _base_data(base: Base) -> dict[str, Any]:
    if base == "minimal":
        return copy.deepcopy(MINIMAL)
    if base == "daemon":
        return _merge(copy.deepcopy(MINIMAL), copy.deepcopy(DAEMON_DELTA))
    if base == "example":
        data = yaml.safe_load(EXAMPLE_YAML.read_text())
        assert isinstance(data, dict)
        return data
    raise ValueError(f"unknown base config {base!r}; use 'minimal', 'daemon' or 'example'")


def isolated_config(
    tmp_path: Path,
    base: Base = "minimal",
    *,
    storage: Path | None = None,
    keep: Iterable[str] = (),
    **overrides: Mapping[str, Any],
) -> CanaryConfig:
    """``base`` with every writable path under ``tmp_path``, then ``overrides``.

    - every ``Path`` field of ``DaemonConfig`` -> ``tmp_path/<its default>``;
    - ``collection.storage_path`` -> ``storage`` (default ``tmp_path/storage``);
    - ``profiles.registry_path`` and ``llm_assist.cards_path``, the two other
      repo-relative files the daemon writes, -> under ``tmp_path``.

    Each keyword names a top-level section and is deep-merged into it after
    the rebase, e.g. ``models={"isolation_forest": {"enabled": False}}``.
    ``keep`` leaves the named daemon paths where ``base`` put them -- for a
    test that reads the real catalogue inputs (:data:`READ_ONLY_INPUTS`); the
    write guard in ``tests/repo_guard.py`` still refuses any write to them.
    """
    data = _base_data(base)
    data.setdefault("collection", {})["storage_path"] = str(
        storage if storage is not None else tmp_path / "storage"
    )
    config = rebase_state_paths(CanaryConfig.model_validate(data), tmp_path, keep=keep)
    isolated = config.model_dump(mode="python", exclude_unset=False)
    _merge(
        isolated,
        {
            "profiles": {"registry_path": tmp_path / "profiles.yaml"},
            "llm_assist": {"cards_path": tmp_path / "data" / "analyses" / "llm_assist_cards.jsonl"},
        },
    )
    _merge(isolated, overrides)
    return CanaryConfig.model_validate(isolated)


#: What ``run_daemon`` tests add to the ``daemon`` base: in-process feature
#: build and fast polling (``tests/test_daemon.py``'s original fixture).
DAEMON_RUN_OVERRIDES: dict[str, Any] = {
    "models": {"out_of_process_feature_build": False},
    "daemon": {
        "model_poll_seconds": 0.05,
        "retention_sweep_seconds": 0.05,
        "broadcaster_queue_size": 16,
    },
}


def profiles_disabled(config: CanaryConfig) -> CanaryConfig:
    """``config`` with ``profiles.enabled: false``: its base paths *are* the
    vehicle's, so featurization may read them unscoped (P02.11).

    Production never featurizes unscoped with profiles on -- the daemon scopes
    in ``main`` and every offline entry point scopes once -- and
    :func:`src.config.require_profile_scoped` refuses the combination. A test
    whose config is a single-vehicle harness (tmp paths, the example YAML's
    top-level data files) says so here instead.
    """
    return config.model_copy(
        update={"profiles": config.profiles.model_copy(update={"enabled": False})}
    )


def example_config(path: Path = EXAMPLE_YAML) -> CanaryConfig:
    """``canary.example.yaml`` (or ``path``) as a single-vehicle config, for
    tests that featurize with it (:func:`profiles_disabled`)."""
    return profiles_disabled(CanaryConfig.from_yaml(path))


#: The vehicle profile :func:`scoped_test_config` scopes to. Its parameters
#: differ from the ``vehicle:`` block's defaults on purpose, so a reader that
#: re-derives the profile instead of reusing the resolved one shows up.
TEST_PROFILE = VehicleProfile(
    slug="test-vehicle",
    vin="",
    name="Test vehicle",
    make="Nissan",
    model="Xterra",
    year=2008,
    parameters=VehicleParameters(displacement_l=3.3),
)


def scoped_test_config(
    config: CanaryConfig, profile: VehicleProfile = TEST_PROFILE
) -> CanaryConfig:
    """``config`` with profiles on, scoped as ``src.main`` scopes the daemon's:
    every vehicle path under ``<base>/<slug>/`` (P02.11). The Pi's shape."""
    enabled = config.model_copy(
        update={"profiles": config.profiles.model_copy(update={"enabled": True})}
    )
    return scope_config(enabled, profile)


def daemon_test_config(
    tmp_path: Path, *, scoped: bool = True, **overrides: Mapping[str, Any]
) -> CanaryConfig:
    """The config ``run_daemon`` tests start on (``conftest.daemon_config``).

    Detectors off, one ``idle`` mode, in-process feature build, fast model
    polling, every writable path under ``tmp_path``; ``overrides`` deep-merge
    on top, e.g. ``models={"isolation_forest": {"enabled": True}}``.

    Scoped by default, as ``src.main`` builds it on the Pi: profiles on, every
    vehicle path under ``<path>/test-vehicle/`` (:func:`scoped_test_config`),
    overrides included. ``scoped=False`` is the profile-less daemon (profiles
    off, paths as given) for a test that names exact paths (P02.11).
    """
    merged = _merge(copy.deepcopy(DAEMON_RUN_OVERRIDES), overrides)
    if not scoped:
        _merge(merged, {"profiles": {"enabled": False}})
        return isolated_config(tmp_path, "daemon", **merged)
    return scoped_test_config(isolated_config(tmp_path, "daemon", **merged))


def isolate(config: CanaryConfig, tmp_path: Path) -> CanaryConfig:
    """``config`` with every writable path not already under ``tmp_path`` moved
    there -- for a test that builds its config from its own YAML.

    Paths the YAML set under ``tmp_path`` stay put (the test may read them
    back by name); every other daemon path, the profile registry and the
    LLM-assist cards file are rebased as in :func:`isolated_config`. A YAML that
    names four ``tmp_path`` directories and leaves ``trip_state`` and
    ``data/control`` at their repo defaults is exactly the leak R07.2 closed.
    """
    under = [
        name
        for name in _daemon_path_fields()
        if (path := getattr(config.daemon, name)) is not None
        and Path(path).is_absolute()
        and Path(path).is_relative_to(tmp_path)
    ]
    config = rebase_state_paths(config, tmp_path, keep=under)
    update: dict[str, Any] = {}
    if not config.profiles.registry_path.is_relative_to(tmp_path):
        update["profiles"] = config.profiles.model_copy(
            update={"registry_path": tmp_path / "profiles.yaml"}
        )
    if not config.llm_assist.cards_path.is_relative_to(tmp_path):
        update["llm_assist"] = config.llm_assist.model_copy(
            update={"cards_path": tmp_path / "data" / "analyses" / "llm_assist_cards.jsonl"}
        )
    return config.model_copy(update=update) if update else config


def isolated_from_yaml(tmp_path: Path, text: str) -> CanaryConfig:
    """Parse a test's own YAML, then :func:`isolate` it."""
    return isolate(CanaryConfig.model_validate(yaml.safe_load(text)), tmp_path)


def write_isolated_config(
    tmp_path: Path,
    base: Base = "minimal",
    *,
    name: str = "canary.yaml",
    storage: Path | None = None,
    keep: Iterable[str] = (),
    **overrides: Mapping[str, Any],
) -> Path:
    """:func:`isolated_config`, written to ``tmp_path/name`` for a CLI or ``from_yaml``."""
    config = isolated_config(tmp_path, base, storage=storage, keep=keep, **overrides)
    path = tmp_path / name
    path.write_text(yaml.safe_dump(config.model_dump(mode="json"), sort_keys=False))
    return path


def retrain_argv(
    tmp_path: Path, *extra: str, feedback: Path | None = None, config: Path | None = None
) -> list[str]:
    """``scripts/retrain.py`` argv whose every output lands under ``tmp_path``.

    ``--if-out``, ``--sequence-out``, ``--ledger``, ``--analyses-dir`` and the
    feature cache default to the repo's ``models/`` and ``data/``; 14 of the 28
    calls in ``test_retrain_script.py`` once omitted ``--if-out`` and all 37
    omitted ``--analyses-dir``. The feedback policy is off unless ``feedback``
    names a file (the default reads the operator's real
    ``data/control/anomaly_feedback.jsonl``). ``extra`` comes last, so a test
    that names one of these flags itself wins (argparse keeps the last value).
    """
    out = tmp_path / "_retrain"
    argv = [
        "--config",
        str(config if config is not None else EXAMPLE_YAML),
        "--if-out",
        str(out / "isolation_forest"),
        "--sequence-out",
        str(out / "sequence"),
        "--ledger",
        str(out / "training_runs.jsonl"),
        "--analyses-dir",
        str(out / "analyses"),
        "--feature-cache",
        str(out / "feature_frames"),
    ]
    argv += ["--feedback", str(feedback)] if feedback is not None else ["--no-feedback-policy"]
    return argv + list(extra)
