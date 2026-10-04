"""Tests for ``poll_behavior_models`` — daemon hot-reload of behavior models.

Validates the deployment loop: drop a new ``behavior_models.yaml`` on
disk, the live source picks it up without a daemon restart. Closes the
"refit + scp + ssh + sudo restart" cycle that blocked Phase D deploys
during the May 2026 storm-quieting work.
"""

from __future__ import annotations

import asyncio
from pathlib import Path

import pytest

from src.behavior.behavior_models import (
    Axis1D,
    BehaviorModel1D,
    BehaviorModels,
    PolynomialFit,
    dump_behavior_models,
)
from src.daemon import poll_behavior_models
from src.features.live import LiveFeatureSource
from tests.configs import example_config


def _models(name: str, operating_mode: str | None) -> BehaviorModels:
    return BehaviorModels(
        schema_version=1,
        models=[
            BehaviorModel1D(
                name=name,
                kind="surface_1d",
                output="obd:COOLANT_TEMP",
                operating_mode=operating_mode,
                axis=Axis1D(name="t", channel="obd:RUN_TIME", domain=(0.0, 600.0)),
                fit=PolynomialFit(
                    kind="poly",
                    coefficients=[25.0, 0.15],
                    sample_count=100,
                    r_squared=0.95,
                    fitted_at="2026-05-20T00:00:00Z",
                ),
            )
        ],
    )


@pytest.fixture
def yaml_path(tmp_path: Path) -> Path:
    path = tmp_path / "behavior_models.yaml"
    dump_behavior_models(_models("coolant_warmup", operating_mode=None), path)
    return path


async def test_poll_picks_up_new_yaml_without_restart(tmp_path: Path, yaml_path: Path) -> None:
    """A fresh YAML on disk replaces the in-memory ``live_source.behavior_models``
    reference within one poll interval.
    """

    config = example_config(Path("canary.example.yaml"))
    live_source = LiveFeatureSource(
        config=config,
        behavior_models=_models("coolant_warmup", operating_mode=None),
    )
    stop = asyncio.Event()
    task = asyncio.create_task(
        poll_behavior_models(yaml_path, live_source, stop, interval_seconds=0.05)
    )
    try:
        # First poll picks up the file we already wrote — confirm baseline.
        await asyncio.sleep(0.15)
        baseline = live_source.behavior_models
        assert baseline is not None
        assert baseline.models[0].operating_mode is None

        # Drop a refitted YAML on disk with a different operating_mode.
        dump_behavior_models(_models("coolant_warmup", operating_mode="warmup"), yaml_path)
        # Bump mtime so the poll detects the change even on fast filesystems.
        import os

        future = yaml_path.stat().st_mtime + 1
        os.utime(yaml_path, (future, future))

        async def _wait_for_swap() -> None:
            while True:
                current = live_source.behavior_models
                if current is not None and current.models[0].operating_mode == "warmup":
                    return
                await asyncio.sleep(0.05)

        await asyncio.wait_for(_wait_for_swap(), timeout=2.0)
        assert live_source.behavior_models.models[0].operating_mode == "warmup"
    finally:
        stop.set()
        await asyncio.wait_for(task, timeout=2.0)


async def test_poll_keeps_previous_models_on_load_error(tmp_path: Path) -> None:
    """A corrupted YAML doesn't blank the in-memory models — log + retry."""

    config = example_config(Path("canary.example.yaml"))
    original = _models("coolant_warmup", operating_mode="warmup")
    live_source = LiveFeatureSource(config=config, behavior_models=original)
    yaml_path = tmp_path / "behavior_models.yaml"
    yaml_path.write_text("schema_version: 1\nmodels: [not a real list]\n")
    stop = asyncio.Event()
    task = asyncio.create_task(
        poll_behavior_models(yaml_path, live_source, stop, interval_seconds=0.05)
    )
    try:
        await asyncio.sleep(0.2)
        # In-memory models stay put — the partial garbage YAML didn't blow
        # them away.
        assert live_source.behavior_models is original
    finally:
        stop.set()
        await asyncio.wait_for(task, timeout=2.0)


async def test_hot_reloaded_cold_start_contract_cannot_qualify_mid_drive(tmp_path: Path) -> None:
    """B01.5/B01.6 — a schema-2 artifact hot-reloaded mid-drive keeps its
    serving contract, and its engine-start tracker begins with no evidence:
    a warmup model cannot be qualified by a reload any more than by a
    daemon restart."""
    from datetime import UTC, datetime, timedelta

    from src.behavior.behavior_decoder import deviation
    from src.behavior.behavior_models import (
        CURRENT_SCHEMA_VERSION,
        ColdStartRequirement,
        EngineStateRequirement,
        ServingEligibility,
        serving_status,
    )
    from src.telemetry import TelemetryRecord

    config = example_config(Path("canary.example.yaml"))
    live_source = LiveFeatureSource(
        config=config, behavior_models=_models("coolant_warmup", operating_mode=None)
    )
    now = datetime(2026, 9, 21, 12, 0, tzinfo=UTC)
    # The engine has been running for ten minutes before the reload lands.
    for second in range(5):
        tick = now + timedelta(seconds=second)
        for channel, value in (
            ("RPM", 800.0),
            ("RUN_TIME", 600.0 + second),
            ("COOLANT_TEMP", 20.0),
        ):
            live_source.observe(TelemetryRecord.obd(tick.timestamp(), channel, value, ""))
        live_source.tick(tick)

    contract = _models("coolant_warmup", operating_mode=None)
    contract = BehaviorModels(
        schema_version=CURRENT_SCHEMA_VERSION,
        models=[
            contract.models[0].model_copy(
                update={
                    "eligibility": ServingEligibility(
                        engine_state=EngineStateRequirement(state="engine_running"),
                        cold_start=ColdStartRequirement(max_initial_coolant_c=40.0),
                    )
                }
            )
        ],
    )
    yaml_path = tmp_path / "behavior_models.yaml"
    stop = asyncio.Event()
    task = asyncio.create_task(
        poll_behavior_models(yaml_path, live_source, stop, interval_seconds=0.05)
    )
    try:
        await asyncio.sleep(0.1)
        # The refit lands on disk while the daemon is already polling.
        dump_behavior_models(contract, yaml_path)

        async def _wait_for_swap() -> None:
            while live_source.behavior_models is None or (
                live_source.behavior_models.models[0].eligibility is None
            ):
                await asyncio.sleep(0.05)

        await asyncio.wait_for(_wait_for_swap(), timeout=2.0)
    finally:
        stop.set()
        await asyncio.wait_for(task, timeout=2.0)

    reloaded = live_source.behavior_models
    assert reloaded is not None
    assert reloaded.models[0].eligibility == contract.models[0].eligibility
    # Contract present but no held-out evidence: still diagnostic-only.
    assert serving_status(reloaded.models[0]).reasons == ("unvalidated:no_held_out_evidence",)
    tick = now + timedelta(seconds=6)
    for channel, value in (("RPM", 800.0), ("RUN_TIME", 606.0), ("COOLANT_TEMP", 20.0)):
        live_source.observe(TelemetryRecord.obd(tick.timestamp(), channel, value, ""))
    live_source.tick(tick)
    point = live_source.latest_operating_point
    assert point is not None
    result = deviation(reloaded.models[0], point["obd:COOLANT_TEMP"], point)
    assert result.expected is None
    assert result.reason == "cold_start_no_start_evidence"
