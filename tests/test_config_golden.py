"""R21: the configuration schemas are pinned, field for field.

``src/config.py`` (1,740 lines) became the package ``src/config/`` behind the
same import path. A pure move must not change a field name, its order, a
default, an alias, a constraint, a validator or its message, the JSON schema, a
rooted path, which fields count as explicitly provided, or how a config scopes
to a vehicle profile. This captures all of that through the public path
(``src.config``) and compares it with ``tests/fixtures/config_golden.json``,
recorded from the unmodified single-file module before any code moved.

What is pinned:

* ``configs``: ``model_dump(mode="json")``, the explicitly-provided field set
  of each section (``model_fields_set``) and the ``exclude_unset`` dump of
  every example YAML and every test configuration ``tests.configs`` builds;
* ``classes``: for every model, its inheritance, ``model_config``, ordered
  fields (annotation, required, default, aliases, constraints), the validators
  in declaration order, private attributes and methods, and its defaults;
* ``schemas``: the root JSON schema (validation and serialization) and a hash
  of each class's own schema;
* ``strict``: a typo key inserted at every mapping of the example config is
  accepted or refused, with the error;
* ``cases``: retired keys and aliases, the alias conflict, the broadcast
  timeout floor, the root-only refusals (broadcast timeout against the
  session gap, the sequence cadence against the persistence window) and every
  other validator's accept/refuse and message, plus the logs they emit;
* ``paths``: ``state_dir`` rooting for implicit and explicit fields, the
  ``Path`` fields ``rebase_state_paths`` derives, its refusals and results;
* ``scope``: the unscoped refusal, a profile-scoped config, and what survives
  ``model_copy``, ``deepcopy`` and pickling (``scoped_profile``,
  ``_scope_source``);
* ``imports``: what a fresh interpreter loads for ``import src.config``.

Values are normalised only where they would differ between checkouts: the
repository root is written ``<repo>``.

Regenerate only on purpose, from the code whose behaviour is the reference::

    .venv/bin/python -m tests.test_config_golden --write
"""

from __future__ import annotations

import copy
import hashlib
import inspect
import json
import logging
import math
import os
import pickle
import subprocess
import sys
import types
from collections.abc import Callable
from pathlib import Path
from typing import Annotated, Any, Literal, Union, get_args, get_origin

import pytest
from pydantic import BaseModel, ValidationError
from pydantic_core import PydanticUndefined

import src.config as config_module
from src.config import (
    DAEMON_STATE_LAYOUT,
    DAEMON_TRACKED_PATHS,
    DEFAULT_BROADCAST_STALE_TIMEOUT_SECONDS,
    DEFAULT_PRIORITY_CHANNELS,
    MIN_BROADCAST_STALE_TIMEOUT_SECONDS,
    REWRITE_LEAD_SECONDS,
    CanaryConfig,
    DaemonConfig,
    DetectorRolesConfig,
    ImuConfig,
    UnscopedConfigError,
    VehicleParameters,
    _daemon_path_fields,
    rebase_state_paths,
    require_profile_scoped,
    validate_broadcast_stale_timeout,
)
from src.paths import REPO_ROOT
from src.yaml_load import load_yaml

GOLDEN = Path(__file__).parent / "fixtures" / "config_golden.json"
REPO = Path(__file__).resolve().parents[1]
SCRATCH_ROOT = Path("/golden-tmp")
EXAMPLES = {
    "example": REPO / "canary.example.yaml",
    "pi": REPO / "canary.pi.example.yaml",
    "dev": REPO / "canary.dev.example.yaml",
    "sim": REPO / "canary.sim.example.yaml",
}
TYPO = "zz_typo"


# ---------------------------------------------------------------------------
# Normalisation
# ---------------------------------------------------------------------------


def _norm(value: Any) -> Any:
    """``value`` as plain JSON: the repo root as ``<repo>``, sets sorted."""
    if isinstance(value, str):
        return value.replace(str(REPO_ROOT), "<repo>")
    if value is None or isinstance(value, (bool, int)):
        return value
    if isinstance(value, float):
        return repr(value) if not math.isfinite(value) else value
    if isinstance(value, Path):
        return _norm(str(value))
    if isinstance(value, BaseModel):
        return _norm(value.model_dump(mode="json"))
    if isinstance(value, dict):
        return {str(_norm(k)): _norm(v) for k, v in value.items()}
    if isinstance(value, (set, frozenset)):
        return sorted((_norm(v) for v in value), key=repr)
    if isinstance(value, (list, tuple)):
        return [_norm(v) for v in value]
    return _norm(repr(value))


def _errors(exc: ValidationError) -> list[dict[str, Any]]:
    return [
        {"loc": [str(part) for part in e["loc"]], "type": e["type"], "msg": _norm(e["msg"])}
        for e in exc.errors(include_url=False, include_input=False, include_context=False)
    ]


def _outcome(call: Callable[[], Any]) -> dict[str, Any]:
    """What ``call`` returns, or the refusal (type, message, validation errors)."""
    try:
        return {"ok": _norm(call())}
    except ValidationError as exc:
        return {"error": "ValidationError", "errors": _errors(exc)}
    except Exception as exc:
        return {"error": type(exc).__name__, "message": _norm(str(exc))}


class _Logs:
    """Every record emitted below ``WARNING`` while the block runs."""

    def __init__(self) -> None:
        self.records: list[logging.LogRecord] = []
        outer = self

        class _Handler(logging.Handler):
            def emit(self, record: logging.LogRecord) -> None:
                outer.records.append(record)

        self._handler = _Handler(level=logging.DEBUG)

    def __enter__(self) -> _Logs:
        self._root = logging.getLogger()
        self._level = self._root.level
        self._root.addHandler(self._handler)
        self._root.setLevel(logging.DEBUG)
        return self

    def __exit__(self, *exc: object) -> None:
        self._root.removeHandler(self._handler)
        self._root.setLevel(self._level)

    def rows(self) -> list[list[str]]:
        return [[r.name, r.levelname, _norm(r.getMessage())] for r in self.records]


def _with_logs(call: Callable[[], Any]) -> dict[str, Any]:
    with _Logs() as logs:
        result = _outcome(call)
    return {**result, "logs": logs.rows()}


# ---------------------------------------------------------------------------
# Classes
# ---------------------------------------------------------------------------


def _ann(annotation: Any) -> str:
    """An annotation rendered without module paths (those change with a move)."""
    if annotation is type(None):
        return "None"
    if annotation is Ellipsis:
        return "..."
    origin = get_origin(annotation)
    args = get_args(annotation)
    if origin is None:
        if isinstance(annotation, type):
            return annotation.__qualname__
        return repr(annotation)
    if origin is Literal:
        return "Literal[" + ", ".join(repr(a) for a in args) + "]"
    if origin is Annotated:
        return "Annotated[" + ", ".join(_ann(a) for a in args[:1]) + "]"
    if origin in (Union, types.UnionType):
        return " | ".join(_ann(a) for a in args)
    name = getattr(origin, "__qualname__", repr(origin))
    return f"{name}[" + ", ".join(_ann(a) for a in args) + "]"


def _default(field: Any) -> Any:
    if field.default is not PydanticUndefined:
        return {"default": _norm(field.default)}
    if field.default_factory is not None:
        return {
            "factory": getattr(field.default_factory, "__name__", "?"),
            "default": _norm(field.get_default(call_default_factory=True)),
        }
    return {"required": True}


def _field(name: str, field: Any) -> dict[str, Any]:
    return {
        "name": name,
        "annotation": _ann(field.annotation),
        **_default(field),
        "alias": field.alias,
        "validation_alias": _norm(field.validation_alias),
        "serialization_alias": field.serialization_alias,
        "constraints": [repr(m) for m in field.metadata],
        "frozen": field.frozen,
        "exclude": field.exclude,
        "title": field.title,
        "description": field.description,
        "json_schema_extra": _norm(field.json_schema_extra),
    }


def _decorators(cls: type[BaseModel]) -> dict[str, Any]:
    decorators = cls.__pydantic_decorators__
    return {
        "field_validators": [
            [name, list(d.info.fields), d.info.mode]
            for name, d in decorators.field_validators.items()
        ],
        "model_validators": [
            [name, d.info.mode] for name, d in decorators.model_validators.items()
        ],
        "computed_fields": list(decorators.computed_fields),
        "field_serializers": list(decorators.field_serializers),
        "model_serializers": list(decorators.model_serializers),
    }


def _methods(cls: type[BaseModel]) -> list[str]:
    kinds = (types.FunctionType, property, classmethod, staticmethod)
    return sorted(
        name
        for name, value in vars(cls).items()
        if not name.startswith("__") and name != "_abc_impl" and isinstance(value, kinds)
    )


def _model_classes() -> dict[str, type[BaseModel]]:
    return {
        name: value
        for name, value in sorted(vars(config_module).items())
        if isinstance(value, type)
        and issubclass(value, BaseModel)
        and value.__module__.startswith("src.config")
    }


def _class_record(cls: type[BaseModel]) -> dict[str, Any]:
    record: dict[str, Any] = {
        "mro": [c.__qualname__ for c in cls.__mro__ if c.__module__.startswith("src.config")],
        "model_config": _norm(dict(cls.model_config)),
        "fields": [_field(name, f) for name, f in cls.model_fields.items()],
        "decorators": _decorators(cls),
        "private": {
            name: _norm(attr.default) for name, attr in (cls.__private_attributes__ or {}).items()
        },
        "methods": _methods(cls),
        "doc": inspect.getdoc(cls),
    }
    record["defaults"] = _outcome(lambda: cls.model_validate({}).model_dump(mode="json"))
    return record


def _classes() -> dict[str, Any]:
    return {name: _class_record(cls) for name, cls in _model_classes().items()}


def _schema_digest(schema: Any) -> str:
    text = json.dumps(_norm(schema), sort_keys=True)
    return hashlib.sha256(text.encode()).hexdigest()


def _schemas() -> dict[str, Any]:
    return {
        "root_validation": _norm(CanaryConfig.model_json_schema()),
        "root_serialization": _norm(CanaryConfig.model_json_schema(mode="serialization")),
        "per_class": {
            name: _schema_digest(cls.model_json_schema()) for name, cls in _model_classes().items()
        },
    }


# ---------------------------------------------------------------------------
# Configurations
# ---------------------------------------------------------------------------


def _sections(config: CanaryConfig) -> dict[str, list[str]]:
    sections = {"": sorted(config.model_fields_set)}
    for name in CanaryConfig.model_fields:
        value = getattr(config, name)
        if isinstance(value, BaseModel):
            sections[name] = sorted(value.model_fields_set)
    return sections


def _config_record(config: CanaryConfig) -> dict[str, Any]:
    return {
        "dump": _norm(config.model_dump(mode="json")),
        "fields_set": _sections(config),
        "provided": _norm(config.model_dump(mode="json", exclude_unset=True)),
        "scoped_profile": config.scoped_profile,
    }


def _test_configs() -> dict[str, CanaryConfig]:
    from tests.configs import (
        DAEMON_DELTA,
        MINIMAL,
        _merge,
        daemon_test_config,
        example_config,
        isolated_config,
        scoped_test_config,
    )

    scratch = SCRATCH_ROOT
    return {
        "minimal": CanaryConfig.model_validate(copy.deepcopy(MINIMAL)),
        "daemon": CanaryConfig.model_validate(
            _merge(copy.deepcopy(MINIMAL), copy.deepcopy(DAEMON_DELTA))
        ),
        "isolated_minimal": isolated_config(scratch),
        "isolated_daemon": isolated_config(scratch, "daemon"),
        "isolated_example": isolated_config(scratch, "example"),
        "daemon_test_scoped": daemon_test_config(scratch),
        "daemon_test_unscoped": daemon_test_config(scratch, scoped=False),
        "example_single_vehicle": example_config(),
        "example_scoped": scoped_test_config(CanaryConfig.from_yaml(EXAMPLES["example"])),
    }


def _configs() -> dict[str, Any]:
    loaded = {name: CanaryConfig.from_yaml(path) for name, path in EXAMPLES.items()}
    return {name: _config_record(c) for name, c in {**loaded, **_test_configs()}.items()}


# ---------------------------------------------------------------------------
# Strict extra-key rejection
# ---------------------------------------------------------------------------


def _mapping_paths(node: Any, path: tuple[str, ...] = ()) -> list[tuple[str, ...]]:
    paths: list[tuple[str, ...]] = []
    if isinstance(node, dict):
        paths.append(path)
        for key, value in node.items():
            paths.extend(_mapping_paths(value, (*path, str(key))))
    return paths


def _with_typo(data: dict[str, Any], path: tuple[str, ...]) -> dict[str, Any]:
    out = copy.deepcopy(data)
    node = out
    for key in path:
        node = node[key]
    node[TYPO] = 1
    return out


def _strict() -> dict[str, Any]:
    data = load_yaml(EXAMPLES["example"].read_text())
    record: dict[str, Any] = {}
    for path in _mapping_paths(data):
        record["/".join(path) or "<root>"] = _outcome(
            lambda path=path: CanaryConfig.model_validate(_with_typo(data, path)).model_fields_set
        )
    # A typo directly on every class, whatever else it lacks: a class with
    # required fields reports those too, and the typo is among the errors.
    for name, cls in _model_classes().items():
        record[f"class:{name}"] = _outcome(lambda cls=cls: cls.model_validate({TYPO: 1}))
    return record


# ---------------------------------------------------------------------------
# Retired keys, validators and cross-domain refusals
# ---------------------------------------------------------------------------


def _example() -> dict[str, Any]:
    data = load_yaml(EXAMPLES["example"].read_text())
    assert isinstance(data, dict)
    return data


def _root_with(**sections: dict[str, Any]) -> CanaryConfig:
    data = _example()
    for name, update in sections.items():
        data.setdefault(name, {}).update(update)
    return CanaryConfig.model_validate(data)


def _vehicle_parameters(**kwargs: Any) -> Any:
    parameters = VehicleParameters.model_validate(kwargs)
    return {"dump": parameters.model_dump(mode="json"), "rpm_per_kph": parameters.rpm_per_kph()}


FULL_DRIVELINE = {
    "final_drive": 3.357,
    "gearbox_ratios": [3.784, 2.272, 1.521, 1.0, 0.8, 0.7],
    "tire_loaded_circumference_m": 2.2,
    "driveline_evidence": "measured",
}


def _cases() -> dict[str, Any]:
    from src.config import (
        AlertsConfig,
        CollectionConfig,
        HatAdcConfig,
        IsolationForestConfig,
        SupplyVoltageConfig,
    )

    cases: dict[str, Any] = {}

    def add(label: str, call: Callable[[], Any]) -> None:
        cases[label] = _with_logs(call)

    # Vehicle parameters: the retired and renamed keys, and the evidence rule.
    add("vp:defaults", lambda: _vehicle_parameters())
    add("vp:gear_ratios_set", lambda: _vehicle_parameters(gear_ratios=[3.7, 2.2]))
    add("vp:gear_ratios_empty", lambda: _vehicle_parameters(gear_ratios=[]))
    add("vp:gear_ratios_none", lambda: _vehicle_parameters(gear_ratios=None))
    add("vp:full_driveline", lambda: _vehicle_parameters(**FULL_DRIVELINE))
    add(
        "vp:british_tire_key_only",
        lambda: _vehicle_parameters(
            **{k: v for k, v in FULL_DRIVELINE.items() if k != "tire_loaded_circumference_m"},
            tyre_loaded_circumference_m=2.3,
        ),
    )
    add(
        "vp:both_tire_keys",
        lambda: _vehicle_parameters(**FULL_DRIVELINE, tyre_loaded_circumference_m=2.3),
    )
    add("vp:driveline_without_evidence", lambda: _vehicle_parameters(final_drive=3.3))
    add(
        "vp:nonpositive_gearbox_ratio",
        lambda: _vehicle_parameters(**{**FULL_DRIVELINE, "gearbox_ratios": [3.7, 0.0]}),
    )
    add(
        "vp:partial_table_rpm_per_kph",
        lambda: _vehicle_parameters(final_drive=3.3, driveline_evidence="x"),
    )
    add("vp:string_numbers", lambda: _vehicle_parameters(mass_kg="1800", displacement_l="3.3"))
    add("vp:bad_fuel", lambda: _vehicle_parameters(fuel_type="water"))

    # Collection: retired key, the broadcast floor, bounds.
    collection = _example()["collection"]
    for label, value in (
        ("confirmed_only", ["confirmed"]),
        ("confirmed_tuple_order", ["confirmed", "confirmed"]),
        ("other_level", ["confirmed", "probable"]),
        ("scalar", "yes"),
        ("empty", []),
    ):
        add(
            f"collection:persist_decoded_confidence:{label}",
            lambda value=value: CollectionConfig.model_validate(
                {**collection, "persist_decoded_confidence": value}
            ).model_dump(mode="json"),
        )
    for seconds in (
        MIN_BROADCAST_STALE_TIMEOUT_SECONDS,
        1.99,
        2.0,
        10,
        "3",
        -1.0,
        0.0,
        float("nan"),
        float("inf"),
        None,
        "x",
    ):
        add(
            f"broadcast_floor:direct:{seconds!r}",
            lambda seconds=seconds: validate_broadcast_stale_timeout(seconds),  # type: ignore[arg-type]
        )
        add(
            f"broadcast_floor:field:{seconds!r}",
            lambda seconds=seconds: (
                CollectionConfig.model_validate(
                    {**collection, "broadcast_stale_timeout_seconds": seconds}
                ).broadcast_stale_timeout_seconds
            ),
        )
    for knob, values in (
        ("obd_batch_max_pids", (0, 1, 6, 7)),
        ("obd_did_batch_max_dids", (0, 1, 3, 4)),
        ("obd_discovery_budget_requests_per_second", (0, -1, 0.5)),
        ("obd_did_session", ("auto", None, 3, "x")),
        ("obd_monitor_mids", ("auto", [1, 2], "x")),
        ("obd_source", ("canable", "elm327", "disabled", "bogus")),
    ):
        for value in values:
            add(
                f"collection:{knob}:{value!r}",
                lambda knob=knob, value=value: CollectionConfig.model_validate(
                    {**collection, knob: value}
                ).model_dump(mode="json")[knob],
            )
    cases["collection:required"] = _outcome(lambda: CollectionConfig.model_validate({}))

    # Hardware validators.
    imu = {"source": "lsm6dsox"}
    for label, update in (
        ("defaults", {}),
        ("odr_not_a_chip_rate", {"sample_rate_hz": 100.0}),
        ("odr_104", {"sample_rate_hz": 104.0}),
        ("odr_26", {"sample_rate_hz": 26.0}),
        ("bad_accel_range", {"sample_rate_hz": 104.0, "accel_range_g": 3}),
        ("bad_gyro_range", {"sample_rate_hz": 104.0, "gyro_range_dps": 7}),
        ("fifo_poll_zero", {"sample_rate_hz": 104.0, "fifo_poll_seconds": 0}),
    ):
        add(
            f"imu:lsm6dsox:{label}",
            lambda update=update: ImuConfig.model_validate({**imu, **update}).model_dump(
                mode="json"
            ),
        )
    add("imu:other_source_ignores_rate", lambda: ImuConfig(source="bno055", sample_rate_hz=3.0))
    add("imu:bad_source", lambda: ImuConfig.model_validate({"source": "gyro9000"}))
    add(
        "imu:enabled_property",
        lambda: [
            ImuConfig(source=s, sample_rate_hz=104.0).enabled
            for s in ("disabled", "bno055", "lsm6dsox")
        ],
    )
    for label, update in (
        ("ok", {}),
        ("record_above_burst", {"record_hz": 600.0}),
        ("slow_above_record", {"slow_record_hz": 200.0}),
        ("equal", {"slow_record_hz": 100.0, "record_hz": 100.0, "burst_hz": 100.0}),
        ("spi_speed_above_chip", {"spi_speed_hz": 20_000_000}),
    ):
        add(
            f"hat_adc:{label}",
            lambda update=update: HatAdcConfig.model_validate(update).model_dump(mode="json"),
        )
    for label, update in (
        ("channel_4", {"input_channel": 4}),
        ("divider_one", {"divider_ratio": 1.0}),
        ("rate_zero", {"rate_hz": 0}),
        ("rate_201", {"rate_hz": 201}),
    ):
        add(
            f"supply_voltage:{label}",
            lambda update=update: SupplyVoltageConfig.model_validate(update).model_dump(
                mode="json"
            ),
        )

    # Models and alerts.
    for label, update in (
        ("window_below_min", {"min_consecutive_ticks": 4, "persistence_window_ticks": 3}),
        ("window_equal_min", {"min_consecutive_ticks": 3, "persistence_window_ticks": 3}),
        ("window_none", {"persistence_window_ticks": None}),
        ("window_seconds_none", {"persistence_window_seconds": None}),
        ("min_ticks_zero", {"min_consecutive_ticks": 0}),
    ):
        add(
            f"alerts:{label}",
            lambda update=update: AlertsConfig.model_validate(update).model_dump(mode="json"),
        )
    forest = _example()["models"]["isolation_forest"]
    for n_jobs in (0, -1, 4):
        add(
            f"isolation_forest:n_jobs:{n_jobs}",
            lambda n_jobs=n_jobs: (
                IsolationForestConfig.model_validate({**forest, "n_jobs": n_jobs}).n_jobs
            ),
        )
    add("detector_roles:by_source", lambda: DetectorRolesConfig().by_source())
    add("detector_roles:deterministic", lambda: DetectorRolesConfig().deterministic_by_source())
    add("detector_roles:bad_role", lambda: DetectorRolesConfig.model_validate({"sequence": "loud"}))

    # Root-only refusals: they read two domains and live with the root.
    for timeout, gap in ((9.99, 10.0), (10.0, 10.0), (11.0, 10.0), (299.0, 300.0), (2.0, 3.0)):
        add(
            f"root:broadcast_vs_gap:{timeout}:{gap}",
            lambda timeout=timeout, gap=gap: (
                _root_with(
                    collection={"broadcast_stale_timeout_seconds": timeout},
                    preprocessing={"session_gap_seconds": gap},
                ).collection.broadcast_stale_timeout_seconds
            ),
        )
    for every_n, window, min_ticks, hz in (
        (1, 8.0, 3, 1.0),
        (2, 8.0, 3, 1.0),
        (4, 8.0, 3, 1.0),
        (5, 8.0, 3, 1.0),
        (5, None, 3, 1.0),
        (4, 8.0, 3, 0.5),
        (4, 8.0, 3, 2.0),
        (3, 8.0, 5, 1.0),
        (2, 3.9, 3, 1.0),
        (2, 4.0, 3, 1.0),
    ):
        add(
            f"root:sequence_cadence:{every_n}:{window}:{min_ticks}:{hz}",
            lambda every_n=every_n, window=window, min_ticks=min_ticks, hz=hz: (
                _root_with(
                    models={"sequence_forward_every_n_ticks": every_n},
                    alerts={
                        "persistence_window_seconds": window,
                        "min_consecutive_ticks": min_ticks,
                        "persistence_window_ticks": max(min_ticks, 5),
                    },
                    preprocessing={"resample_hz": hz},
                ).models.sequence_forward_every_n_ticks
            ),
        )
    data = _example()
    for section in ("vehicle", "hardware", "collection", "preprocessing", "models", "dashboard"):
        cases[f"root:required:{section}"] = _outcome(
            lambda section=section: CanaryConfig.model_validate(
                {k: v for k, v in data.items() if k != section}
            )
        )
    cases["root:empty"] = _outcome(lambda: CanaryConfig.model_validate({}))
    cases["root:not_a_mapping"] = _outcome(lambda: CanaryConfig.model_validate(["a"]))
    cases["root:from_yaml"] = _outcome(lambda: CanaryConfig.from_yaml(EXAMPLES["example"]).vehicle)
    cases["root:from_yaml_missing"] = _outcome(
        lambda: CanaryConfig.from_yaml(Path("/nonexistent.yaml"))
    )
    cases["constants"] = {
        "DEFAULT_BROADCAST_STALE_TIMEOUT_SECONDS": DEFAULT_BROADCAST_STALE_TIMEOUT_SECONDS,
        "MIN_BROADCAST_STALE_TIMEOUT_SECONDS": MIN_BROADCAST_STALE_TIMEOUT_SECONDS,
        "REWRITE_LEAD_SECONDS": REWRITE_LEAD_SECONDS,
        "DEFAULT_PRIORITY_CHANNELS": list(DEFAULT_PRIORITY_CHANNELS),
        "UnscopedConfigError.bases": [b.__name__ for b in UnscopedConfigError.__bases__],
    }
    return cases


# ---------------------------------------------------------------------------
# Path rooting
# ---------------------------------------------------------------------------


def _state_config(daemon: dict[str, Any], **sections: dict[str, Any]) -> CanaryConfig:
    data = _example()
    data.setdefault("daemon", {}).update(daemon)
    for name, update in sections.items():
        data.setdefault(name, {}).update(update)
    return CanaryConfig.model_validate(data)


def _rooted(config: CanaryConfig) -> dict[str, Any]:
    return {
        "daemon": _norm(config.daemon.model_dump(mode="json")),
        "profiles_overlay": _norm(config.profiles.registry_overlay_path),
        "cards_path": _norm(config.llm_assist.cards_path),
        "fields_set": _sections(config),
    }


def _rebased(config: CanaryConfig, root: Path, kwargs: dict[str, Any]) -> dict[str, Any]:
    before = config.model_dump(mode="json")
    result = rebase_state_paths(config, root, **kwargs)
    rest = {"exclude": {"daemon"}, "mode": "json"}
    return {
        "daemon": result.daemon.model_dump(mode="json"),
        "rest_unchanged": result.model_dump(**rest) == config.model_dump(**rest),
        "same_type": type(result) is CanaryConfig,
        "input_unchanged": config.model_dump(mode="json") == before,
        "result_is_input": result is config,
    }


def _paths() -> dict[str, Any]:
    state = SCRATCH_ROOT / "state"
    record: dict[str, Any] = {
        "path_fields": list(_daemon_path_fields()),
        "layout": {k: _norm(v) for k, v in DAEMON_STATE_LAYOUT.items()},
        "tracked": sorted(DAEMON_TRACKED_PATHS),
    }
    cases: dict[str, Callable[[], Any]] = {
        "none": lambda: _rooted(_state_config({})),
        "state_only": lambda: _rooted(_state_config({"state_dir": str(state)})),
        "relative_state": lambda: _rooted(_state_config({"state_dir": "rel/state"})),
        "state_null": lambda: _rooted(_state_config({"state_dir": None})),
        "explicit_audit": lambda: _rooted(
            _state_config({"state_dir": str(state), "audit_log": "/elsewhere/audit.jsonl"})
        ),
        "explicit_equal_to_default": lambda: _rooted(
            _state_config({"state_dir": str(state), "models_dir": "models"})
        ),
        "explicit_tracked": lambda: _rooted(
            _state_config({"state_dir": str(state), "afr_calibration": "/x/afr.yaml"})
        ),
        "explicit_overlay_and_cards": lambda: _rooted(
            _state_config(
                {"state_dir": str(state)},
                profiles={"registry_overlay_path": "/o/profiles.yaml"},
                llm_assist={"cards_path": "/o/cards.jsonl"},
            )
        ),
        "explicit_overlay_only": lambda: _rooted(
            _state_config(
                {"state_dir": str(state)}, profiles={"registry_overlay_path": "/o/profiles.yaml"}
            )
        ),
        "revalidated_dump": lambda: _rooted(
            CanaryConfig.model_validate(
                _state_config({"state_dir": str(state)}).model_dump(mode="python")
            )
        ),
        "model_copy_state_dir": lambda: _rooted(
            _state_config({}).model_copy(
                update={"daemon": _state_config({}).daemon.model_copy(update={"state_dir": state})}
            )
        ),
        "daemon_alone": lambda: _norm(
            DaemonConfig.model_validate({"state_dir": str(state)}).model_dump(mode="json")
        ),
        "daemon_alone_fields_set": lambda: sorted(
            DaemonConfig.model_validate({"state_dir": str(state)}).model_fields_set
        ),
    }
    record["rooting"] = {label: _outcome(call) for label, call in cases.items()}

    base = CanaryConfig.from_yaml(EXAMPLES["example"])
    absolute = base.model_copy(
        update={
            "daemon": base.daemon.model_copy(
                update={"trip_state": Path("/var/lib/canary/trip.json"), "state_dir": None}
            )
        }
    )
    root = SCRATCH_ROOT / "rebased"
    pinned = _state_config({"state_dir": str(SCRATCH_ROOT / "state")})
    names = ["behavior_models", "capture_profiles", "kline_profile"]
    rebase_cases: dict[str, tuple[CanaryConfig, dict[str, Any]]] = {
        "plain": (base, {}),
        "absolute_reroot": (absolute, {}),
        "keep_behavior_models": (base, {"keep": ("behavior_models",)}),
        "keep_several": (base, {"keep": names}),
        "keep_unknown": (base, {"keep": ("not_a_field",)}),
        "keep_a_non_path_field": (base, {"keep": ("model_poll_seconds",)}),
        "state_dir_rebased": (pinned, {}),
        "keep_state_dir": (pinned, {"keep": ("state_dir",)}),
    }
    record["rebase"] = {
        label: _outcome(lambda config=config, kw=kw: _rebased(config, root, kw))
        for label, (config, kw) in rebase_cases.items()
    }
    return record


# ---------------------------------------------------------------------------
# Profile scope
# ---------------------------------------------------------------------------


def _scope() -> dict[str, Any]:
    from src.vehicle.profiles import rescope_like, scope_config
    from tests.configs import TEST_PROFILE, scoped_test_config

    unscoped = CanaryConfig.from_yaml(EXAMPLES["example"])
    scoped = scoped_test_config(unscoped)
    record: dict[str, Any] = {}

    def describe(config: CanaryConfig) -> dict[str, Any]:
        source = config._scope_source
        return {
            "scoped_profile": config.scoped_profile,
            "private_slug": config._scoped_profile,
            "scope_source": None if source is None else [type(source).__name__, source.slug],
            "afr": _norm(config.daemon.afr_calibration),
            "vehicle": _norm(config.vehicle),
            "equal_to_scoped": config == scoped,
        }

    record["private_attributes"] = sorted(CanaryConfig.__private_attributes__)
    record["unscoped"] = describe(unscoped)
    record["scoped"] = describe(scoped)
    record["model_copy"] = describe(scoped.model_copy())
    record["model_copy_update"] = describe(
        scoped.model_copy(update={"dashboard": scoped.dashboard.model_copy(update={"port": 9})})
    )
    record["deepcopy"] = describe(copy.deepcopy(scoped))
    for protocol in (4, pickle.HIGHEST_PROTOCOL):
        record[f"pickle_{protocol}"] = describe(
            pickle.loads(pickle.dumps(scoped, protocol=protocol))
        )
    record["dump_has_no_private"] = sorted(k for k in scoped.model_dump() if k.startswith("_"))
    record["yaml_cannot_claim_scope"] = _outcome(
        lambda: CanaryConfig.model_validate(
            {**unscoped.model_dump(mode="json"), "_scoped_profile": "x"}
        )
    )
    record["scoped_unequal_unscoped"] = scoped != unscoped
    record["revalidated_scoped_dump_is_unscoped"] = describe(
        CanaryConfig.model_validate(scoped.model_dump(mode="json"))
    )

    record["require_unscoped_profiles_on"] = _outcome(
        lambda: require_profile_scoped(unscoped, reader="the golden")
    )
    record["require_scoped"] = _outcome(lambda: require_profile_scoped(scoped, reader="the golden"))
    off = unscoped.model_copy(
        update={"profiles": unscoped.profiles.model_copy(update={"enabled": False})}
    )
    record["require_unscoped_profiles_off"] = _outcome(
        lambda: require_profile_scoped(off, reader="the golden")
    )
    record["scope_idempotent"] = scope_config(scoped, TEST_PROFILE) is scoped
    record["scope_other_profile"] = _outcome(
        lambda: (
            scope_config(scoped, TEST_PROFILE.model_copy(update={"slug": "other"}))
            if hasattr(TEST_PROFILE, "model_copy")
            else scope_config(
                scoped, type(TEST_PROFILE)(**{**TEST_PROFILE.__dict__, "slug": "other"})
            )
        )
    )
    record["rescope_like"] = describe(rescope_like(unscoped, scoped))
    record["rescope_like_unscoped_reference"] = describe(rescope_like(unscoped, unscoped))
    return record


# ---------------------------------------------------------------------------
# Cold import
# ---------------------------------------------------------------------------

# A namespace package (no file of its own, e.g. ``site-packages/cuda`` from
# ``cuda-bindings``, installed beside torch) is the venv's, not a dependency of
# ``src.config``: the editable-install finder probes it whatever the import is.
_IMPORT_PROBE = (
    "import json, sys\n"
    "import src.config\n"
    "namespace = [n for n, m in sys.modules.items()\n"
    "             if getattr(getattr(m, '__spec__', None), 'origin', 0) in (None, 'namespace')]\n"
    "print(json.dumps({'modules': sorted(sys.modules), 'namespace': sorted(namespace)}))\n"
)


def _fresh_python(code: str, *, cwd: Path) -> str:
    """Stdout of ``code`` run in a new interpreter that can import ``src``."""
    env = {**os.environ, "PYTHONPATH": str(REPO)}
    return subprocess.run(
        [sys.executable, "-c", code], capture_output=True, text=True, check=True, cwd=cwd, env=env
    ).stdout


def loaded_by_importing_config() -> dict[str, list[str]]:
    """Top-level third-party modules and ``src`` modules a fresh interpreter has
    after ``import src.config`` (and nothing else)."""
    out = _fresh_python(_IMPORT_PROBE, cwd=REPO)
    probed = json.loads(out)
    namespace = {n.split(".")[0] for n in probed["namespace"]}
    modules = [m for m in probed["modules"] if m.split(".")[0] not in namespace]
    stdlib = set(sys.stdlib_module_names)
    # Only distributions: the interpreter's own finders and site hooks differ by machine.
    third = sorted(
        {m.split(".")[0] for m in modules if m.split(".")[0] not in stdlib | {"src"}}
        - {"__main__", "sitecustomize", "cython_runtime"}
        - {m for m in modules if m.startswith("_")}
    )
    ours = sorted(m for m in modules if m == "src" or m.startswith("src."))
    return {"third_party": third, "src": ours}


# ---------------------------------------------------------------------------
# Capture and comparison
# ---------------------------------------------------------------------------


def capture() -> dict[str, Any]:
    return {
        "configs": _configs(),
        "classes": _classes(),
        "schemas": _schemas(),
        "strict": _strict(),
        "cases": _cases(),
        "paths": _paths(),
        "scope": _scope(),
        "imports": loaded_by_importing_config(),
    }


def _golden() -> dict[str, Any]:
    return json.loads(GOLDEN.read_text())


@pytest.fixture(scope="module")
def captured() -> dict[str, Any]:
    return json.loads(json.dumps(capture()))


SECTIONS = ("configs", "classes", "schemas", "strict", "cases", "paths", "scope")


def _diff(expected: Any, actual: Any, path: str = "") -> list[str]:
    if type(expected) is not type(actual):
        return [f"{path or '<root>'}: {expected!r:.120} != {actual!r:.120}"]
    if isinstance(expected, dict):
        out: list[str] = []
        for key in sorted(set(expected) | set(actual)):
            if key not in expected:
                out.append(f"{path}/{key}: unexpected")
            elif key not in actual:
                out.append(f"{path}/{key}: missing")
            else:
                out.extend(_diff(expected[key], actual[key], f"{path}/{key}"))
        return out
    if isinstance(expected, list):
        if len(expected) != len(actual):
            return [f"{path}: length {len(expected)} != {len(actual)}"]
        return [
            d
            for i, (e, a) in enumerate(zip(expected, actual, strict=True))
            for d in _diff(e, a, f"{path}[{i}]")
        ]
    return [] if expected == actual else [f"{path}: {expected!r:.120} != {actual!r:.120}"]


@pytest.mark.parametrize("section", SECTIONS)
def test_section_matches_the_golden(section: str, captured: dict[str, Any]) -> None:
    differences = _diff(_golden()[section], captured[section], section)
    assert not differences, "\n".join(differences[:40]) + (
        f"\n... {len(differences)} differences" if len(differences) > 40 else ""
    )


def test_the_golden_covers_every_model_the_package_exports(captured: dict[str, Any]) -> None:
    assert set(captured["classes"]) == set(_golden()["classes"])
    assert len(captured["classes"]) == 40  # +CompanionConfig (2026-10-16)


def test_a_fresh_interpreter_loads_nothing_new_for_import_src_config(
    captured: dict[str, Any],
) -> None:
    """R21: the split adds files, not dependencies. Third-party modules are
    those the single file loaded; ``src`` gains only ``src.config.*``."""
    head = _golden()["imports"]
    loaded = captured["imports"]
    assert set(loaded["third_party"]) <= set(head["third_party"])
    heavy = {"torch", "numpy", "pandas", "can", "serial", "fastapi", "uvicorn", "duckdb", "sklearn"}
    assert not heavy & set(loaded["third_party"]), sorted(heavy & set(loaded["third_party"]))
    outside = [m for m in loaded["src"] if m != "src.config" and not m.startswith("src.config.")]
    assert outside == [m for m in head["src"] if m != "src.config"]


def test_a_spawned_interpreter_unpickles_a_scoped_config(tmp_path: Path) -> None:
    """The scoring and feature children receive the parent's live config object
    pickled over a pipe; a fresh interpreter must resolve every class it names."""
    from tests.configs import TEST_PROFILE, scoped_test_config

    config = scoped_test_config(CanaryConfig.from_yaml(EXAMPLES["example"]))
    blob = tmp_path / "config.pickle"
    blob.write_bytes(pickle.dumps(config))
    code = (
        "import json, pickle\n"
        f"config = pickle.loads(open({str(blob)!r}, 'rb').read())\n"
        "print(json.dumps([config.scoped_profile, config._scope_source.slug,"
        " config.daemon.afr_calibration.parent.name, config.vehicle.make]))\n"
    )
    out = _fresh_python(code, cwd=tmp_path)
    assert json.loads(out) == [
        TEST_PROFILE.slug,
        TEST_PROFILE.slug,
        TEST_PROFILE.slug,
        TEST_PROFILE.make,
    ]


def main() -> None:
    if "--write" not in sys.argv:
        raise SystemExit("pass --write to regenerate tests/fixtures/config_golden.json")
    GOLDEN.write_text(json.dumps(capture(), indent=1, sort_keys=True) + "\n")
    print(f"wrote {GOLDEN} ({GOLDEN.stat().st_size} bytes)")


if __name__ == "__main__":
    main()
