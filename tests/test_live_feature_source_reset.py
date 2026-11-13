"""R27.6: ``LiveFeatureSource.reset()`` returns every state field to fresh.

``reset()`` is the replay-isolation boundary (Phase 7.1): a live drive's state
must not bleed into a replay run, nor a replay's tail back into live. It used
to re-assign each attribute by name, so an accumulator added later and
forgotten there leaked across that boundary and no test enumerated the
fields. This test does: it drives a source until every non-config field
differs from a freshly built one, resets it, and compares *every* attribute
(recursively, by value) against the fresh source, apart from the named
exclusions below. A new field is compared by default, so it fails here until
``reset()`` handles it — and the coverage assertion fails until this test
drives it.
"""

from __future__ import annotations

import dataclasses
import math
from collections import deque
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest

from src.behavior.behavior_models import (
    BehaviorModels,
    ColdStartRequirement,
    EngineStateRequirement,
    ServingEligibility,
    load_behavior_models,
)
from src.config import CanaryConfig
from src.decode.bindings import load_bindings
from src.decode.dids import load_repo_did_map
from src.features.live import LiveFeatureSource
from src.telemetry import REPLAY_SOURCE_AT_KEY, MetadataValue, TelemetryRecord
from tests.configs import example_config

T0 = datetime(2026, 11, 12, 12, 0, tzinfo=UTC)
BINDINGS = Path("data/bindings.yaml")
BEHAVIOR_MODELS = Path("data/nissan-xterra-2008/behavior_models.yaml")
#: 0x1F9 bytes 2-3 big-endian, 8 counts/rpm.
RPM_750 = bytes([0x00, 0x00, 0x17, 0x70, 0x00, 0x00, 0x00, 0x00])
RPM_2000 = bytes([0x00, 0x00, 0x3E, 0x80, 0x00, 0x00, 0x00, 0x00])

#: Fields ``reset()`` deliberately leaves alone, each with the reason. Every
#: other attribute of the instance is compared against a fresh source.
NOT_RESET: dict[str, str] = {
    # Constructor inputs: what the source *is*, not what it has seen.
    "config": "constructor input (hot-reloaded by the daemon, never by reset)",
    "bindings": "constructor input",
    "behavior_models": "constructor input (hot-reloaded by the daemon)",
    "accepted_baselines": "constructor input",
    "dbc_database": "constructor input",
    "did_map": "constructor input",
    "session": "constructor input: the label stamped on every row",
    "sample_observer": "constructor input: a consumer callback",
    "record_observers": "constructor input: consumer callbacks",
    "session_identity": "constructor input: the daemon's writer-directory getter",
    # Built once from the inputs in __post_init__; pure functions of them.
    "_frame_decoder": "built from bindings",
    "_dbc_message_by_id": "index of dbc_database",
    "_did_by_hex": "index of did_map",
    "_feature_config": "built from config",
    "_max_rows": "built from config",
    "_max_gap_seconds": "built from config",
    "_slow_max_gap_seconds": "built from config",
    "_resample_config": "built from config",
    "_max_gap_by_channel": "memo of the pure _resample_config.max_gap_seconds_for",
    # The one field reset must *move*: it tags in-flight worker results so a
    # result built before the reset cannot be applied after it.
    "_generation": "advanced by reset (asserted below)",
}

#: Sub-fields of compared objects that reset leaves alone, with the reason.
NOT_RESET_NESTED: dict[tuple[str, str], str] = {
    ("_can_filter", "kept"): (
        "CanChangeFilter.reset forgets ids, not its kept/suppressed tallies "
        "(the storage writer's tallies run across rotations); diagnostics only"
    ),
    ("_can_filter", "suppressed"): "as kept",
}


def _state(value: Any, path: str = "") -> Any:
    """A by-value, recursively comparable view of ``value``.

    Dataclasses and plain objects are expanded into their attributes so a
    mismatch names the leaf; floats compare NaN-equal; a deque keeps its
    ``maxlen`` (a buffer rebuilt with another bound is not a fresh one).
    """
    top = path.split(".", 1)[0]
    if isinstance(value, float):
        return "nan" if math.isnan(value) else value
    if isinstance(value, deque):
        return ("deque", value.maxlen, [_state(item, path) for item in value])
    if isinstance(value, dict):
        return {key: _state(item, f"{path}[{key!r}]") for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return (type(value).__name__, [_state(item, path) for item in value])
    expandable = dataclasses.is_dataclass(value) or (
        # An object with its own __eq__ is compared whole; one without it is
        # expanded field by field. mypy types the two unbound __eq__ methods
        # with different signatures and calls the identity check impossible;
        # at runtime it is exactly "does this class override __eq__".
        hasattr(value, "__dict__") and type(value).__eq__ is object.__eq__  # type: ignore[comparison-overlap]
    )
    if expandable and not isinstance(value, type):
        return {
            name: _state(item, f"{path}.{name}")
            for name, item in vars(value).items()
            if (top, name) not in NOT_RESET_NESTED
        }
    return value


def _attributes(source: LiveFeatureSource) -> set[str]:
    """Every dataclass field and instance attribute. ``vars`` alone misses an
    ``init=False`` field with a plain default until something assigns it."""
    return {f.name for f in dataclasses.fields(source)} | set(vars(source))


def _compared(source: LiveFeatureSource) -> dict[str, Any]:
    return {
        name: _state(getattr(source, name), name)
        for name in _attributes(source)
        if name not in NOT_RESET
    }


def _leaves(a: Any, b: Any, path: str) -> list[str]:
    """Paths at which ``a`` and ``b`` differ."""
    if isinstance(a, dict) and isinstance(b, dict):
        out = [f"{path}[{key!r}] (only one side)" for key in set(a) ^ set(b)]
        for key in set(a) & set(b):
            out += _leaves(a[key], b[key], f"{path}.{key}" if path else str(key))
        return out
    return [] if a == b else [path]


@pytest.fixture
def config() -> CanaryConfig:
    return example_config(Path("canary.example.yaml"))


def _models_with_a_cold_start_contract() -> BehaviorModels:
    """The shipped models, the first given a cold-start contract so the source
    keeps an engine-start tracker (B01.5)."""
    shipped = load_behavior_models(BEHAVIOR_MODELS)
    first = shipped.models[0]
    eligibility = ServingEligibility(
        engine_state=EngineStateRequirement(state="engine_running"),
        cold_start=ColdStartRequirement(max_initial_coolant_c=40.0),
    )
    return shipped.model_copy(
        update={
            "models": [
                first.model_copy(update={"eligibility": eligibility}),
                *shipped.models[1:],
            ]
        }
    )


def _build(config: CanaryConfig, recording: list[str]) -> LiveFeatureSource:
    return LiveFeatureSource(
        config=config,
        bindings=load_bindings(BINDINGS),
        behavior_models=_models_with_a_cold_start_contract(),
        did_map=load_repo_did_map(),
        session_identity=lambda: recording[0],
    )


def _drive(source: LiveFeatureSource) -> None:
    """Touch every state group: ingest/decode, the tick and its buffer, trip
    integrals, cold-start trackers, bus freshness, the IMU frame, the quality
    gate, the replay clock."""
    source.seed_bus_last_heard(T0 - timedelta(hours=1))
    for second in range(12):
        at = T0 + timedelta(seconds=second)
        stamp = at.timestamp()
        replayed: dict[str, MetadataValue] = {REPLAY_SOURCE_AT_KEY: stamp - 86_400.0}
        rpm = 0.0 if second < 2 else 1800.0
        speed = 0.0 if second < 4 else 40.0 + second
        for channel, value, unit in (
            ("RPM", rpm, "rpm"),
            ("SPEED", speed, "km/h"),
            ("MAF", 3.0 + second, "g/s"),
            ("COOLANT_TEMP", 20.0 + second, "C"),
            ("RUN_TIME", float(max(second - 1, 0)), "s"),
        ):
            source.observe(TelemetryRecord.obd(stamp, channel, value, unit, metadata=replayed))
        # The same payload twice inside the re-write interval: one kept, one
        # suppressed by the change filter.
        payload = RPM_750 if second < 6 else RPM_2000
        source.observe(TelemetryRecord.can(stamp, 0x1F9, payload))
        source.observe(TelemetryRecord.can(stamp + 0.01, 0x1F9, payload))
        source.observe(
            TelemetryRecord(timestamp=at, source="obd", channel="DID_1208", value=f"{20000:04x}")
        )
        source.observe(TelemetryRecord.gps(stamp, "speed", speed, "km/h"))
        source.observe(TelemetryRecord.gps(stamp, "heading", 90.0, "deg"))
        for step in range(10):
            imu_at = stamp + step / 10.0
            for axis, value in (("ACCEL_X", 0.02), ("ACCEL_Y", -0.01), ("ACCEL_Z", 1.0)):
                source.observe(TelemetryRecord.imu(imu_at, axis, value, "g"))
        source.tick(at)
    # A PID 0x0C rollover the quality gate rejects.
    source.observe(TelemetryRecord.obd(T0.timestamp() + 12, "RPM", 16383.0, "rpm"))


def test_reset_returns_every_state_field_to_fresh(config: CanaryConfig) -> None:
    recording = ["2026-11-12T120000"]
    fresh = _compared(_build(config, recording))

    source = _build(config, recording)
    _drive(source)
    driven = _compared(source)
    # Coverage: the drive must move every compared field, or a field reset
    # forgets would compare equal here by never having left its default.
    undriven = sorted(name for name in fresh if driven.get(name) == fresh[name])
    assert not undriven, f"drive these in _drive() so reset is tested on them: {undriven}"
    generation = source._generation

    source.reset()
    after = _compared(source)
    assert set(after) == set(fresh), "reset() added or removed attributes"
    leaked = sorted(_leaves(after, fresh, ""))
    assert not leaked, f"reset() left these fields as driven, not fresh: {leaked}"
    assert source._generation == generation + 1


def test_reset_test_names_only_real_fields(config: CanaryConfig) -> None:
    """A stale exclusion would hide nothing and rot: every name exists."""
    names = _attributes(_build(config, ["x"]))
    assert set(NOT_RESET) <= names, sorted(set(NOT_RESET) - names)
    assert {top for top, _ in NOT_RESET_NESTED} <= names
