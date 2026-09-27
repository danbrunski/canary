"""``poll_config``: the operating-mode hot reload keeps the daemon's profile scope (P02.11).

It used to swap in ``CanaryConfig.from_yaml(path)`` -- unscoped. With profiles
on (the Pi) every live tick after a YAML edit then refused to featurize, and
IF, sequence, behavior and the derived channels stayed dead until a restart.
"""

from __future__ import annotations

import asyncio
import logging
import os
from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
import yaml

from src.config import CanaryConfig
from src.daemon import polling
from src.daemon.polling import poll_config
from src.preprocessing.live import LiveFeatureSource
from src.telemetry import TelemetryRecord
from tests.configs import TEST_PROFILE, isolated_config, scoped_test_config

pytestmark = pytest.mark.asyncio

_MODES = {"idle": {"rpm_max": 900, "speed_max": 0}}
_EDITED = {**_MODES, "cruise": {"rpm_min": 1200, "speed_min": 40}}


def _write(path: Path, config: CanaryConfig, modes: dict[str, object]) -> None:
    data = config.model_dump(mode="json")
    data["preprocessing"]["operating_modes"] = modes
    path.write_text(yaml.safe_dump(data, sort_keys=False))


def _base(tmp_path: Path, *, profiles: bool) -> CanaryConfig:
    return isolated_config(
        tmp_path,
        "daemon",
        preprocessing={"operating_modes": _MODES},
        profiles={"enabled": profiles},
    )


async def _edit_and_poll(
    path: Path,
    source: LiveFeatureSource,
    yaml_base: CanaryConfig,
    done: Callable[[], bool] | None = None,
) -> None:
    """Start the poller, rewrite the YAML from ``yaml_base`` (unscoped, as the
    operator's file is) with an added mode, and poll until ``done`` (default:
    the live source's config was swapped) or ~2 s pass."""
    config = source.config

    def swapped() -> bool:
        return source.config is not config

    finished = done or swapped
    stop = asyncio.Event()
    task = asyncio.create_task(poll_config(path, source, stop, interval_seconds=0.01))
    await asyncio.sleep(0.05)
    _write(path, yaml_base, _EDITED)
    later = path.stat().st_mtime + 5
    os.utime(path, (later, later))
    for _ in range(200):
        if finished():
            break
        await asyncio.sleep(0.01)
    stop.set()
    await task


async def test_a_reload_is_scoped_exactly_as_main_scoped_the_startup_config(
    tmp_path: Path,
) -> None:
    path = tmp_path / "canary.yaml"
    base = _base(tmp_path, profiles=True)
    _write(path, base, _MODES)
    startup = scoped_test_config(CanaryConfig.from_yaml(path))
    source = LiveFeatureSource(config=startup)

    await _edit_and_poll(path, source, base)

    reloaded = source.config
    assert reloaded is not startup, "the edit was not picked up"
    assert sorted(reloaded.preprocessing.operating_modes) == ["cruise", "idle"]
    assert reloaded.scoped_profile == TEST_PROFILE.slug
    # The resolved profile's own parameters, not the YAML vehicle block's.
    assert reloaded.vehicle.parameters == TEST_PROFILE.parameters
    assert reloaded.vehicle.parameters != base.vehicle.parameters
    assert reloaded.daemon.afr_calibration == startup.daemon.afr_calibration
    assert reloaded.collection.storage_path == startup.collection.storage_path
    # A fed tick featurizes (it raised UnscopedConfigError before P02.11).
    now = datetime(2026, 9, 27, 12, 0, tzinfo=UTC)
    for i in range(5):
        at = (now + timedelta(seconds=i)).timestamp()
        source.observe(TelemetryRecord.obd(at, "RPM", 800.0 + i, "rpm"))
        source.observe(TelemetryRecord.obd(at, "SPEED", 0.0, "km/h"))
    row = source.tick(now + timedelta(seconds=4))
    assert row is not None and not row.empty


async def test_a_reload_that_cannot_be_scoped_keeps_the_previous_config(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    """Profiles switched on under a daemon that started without them: which
    vehicle is a startup decision (the VIN probe), so the reload is refused."""
    path = tmp_path / "canary.yaml"
    base = _base(tmp_path, profiles=False)
    _write(path, base, _MODES)
    startup = CanaryConfig.from_yaml(path)
    source = LiveFeatureSource(config=startup)
    edited = base.model_copy(
        update={"profiles": base.profiles.model_copy(update={"enabled": True})}
    )

    with caplog.at_level(logging.WARNING, logger="src.daemon.polling"):
        await _edit_and_poll(
            path, source, edited, done=lambda: "cannot scope the new config" in caplog.text
        )

    assert source.config is startup
    assert "cannot scope the new config; keeping previous" in caplog.text


async def test_a_failed_rescope_keeps_the_previous_config(
    tmp_path: Path, caplog: pytest.LogCaptureFixture, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = tmp_path / "canary.yaml"
    base = _base(tmp_path, profiles=True)
    _write(path, base, _MODES)
    startup = scoped_test_config(CanaryConfig.from_yaml(path))
    source = LiveFeatureSource(config=startup)

    def broken(config: CanaryConfig, reference: CanaryConfig) -> CanaryConfig:
        raise ValueError("re-scope failed")

    monkeypatch.setattr(polling, "rescope_like", broken)
    with caplog.at_level(logging.ERROR, logger="src.daemon.polling"):
        await _edit_and_poll(path, source, base, done=lambda: "hot-reload failed" in caplog.text)

    assert source.config is startup
    assert source.config.scoped_profile == TEST_PROFILE.slug
    assert "config hot-reload failed; keeping previous" in caplog.text
