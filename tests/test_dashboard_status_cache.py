"""Concurrency contracts for the relatively expensive status derivation."""

from __future__ import annotations

import threading
import time
from pathlib import Path
from typing import Any

import src.dashboard.status as status
from src.config import CanaryConfig
from src.dashboard.status import VehicleStateCache


def test_vehicle_state_cache_single_flights_an_expired_refresh() -> None:
    cache = VehicleStateCache(ttl_seconds=60.0)
    calls = 0
    calls_lock = threading.Lock()
    start = threading.Barrier(5)
    results: list[Any] = []

    def derive() -> Any:
        nonlocal calls
        with calls_lock:
            calls += 1
        time.sleep(0.05)
        return object()

    def worker() -> None:
        start.wait()
        results.append(cache.get(derive))

    threads = [threading.Thread(target=worker) for _ in range(4)]
    for thread in threads:
        thread.start()
    start.wait()
    for thread in threads:
        thread.join(timeout=2)

    assert all(not thread.is_alive() for thread in threads)
    assert calls == 1
    assert len(results) == 4
    assert all(result is results[0] for result in results)


def test_status_uses_live_state_without_reading_parquet(monkeypatch: Any) -> None:
    class _Live:
        def vehicle_state_inputs(self, _now: Any) -> dict[str, float | None]:
            return {
                "rpm": 750.0,
                "decoded_rpm": None,
                "coolant_temp": 88.0,
                "run_time": 120.0,
                "obd_freshness_seconds": 0.1,
                "can_freshness_seconds": 0.01,
            }

    def disk_read(*_args: Any, **_kwargs: Any) -> Any:
        raise AssertionError("live status fell back to a Parquet scan")

    monkeypatch.setattr(status, "_derive_vehicle_state", disk_read)
    config = CanaryConfig.from_yaml(Path("canary.example.yaml"))
    sources = status.gather_status_sources(config, live_source=_Live())

    assert sources.vehicle_state.state == "engine_running"
    assert sources.vehicle_state.rpm == 750.0
