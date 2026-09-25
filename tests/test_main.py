"""Tests for the daemon entry point."""

import asyncio
from pathlib import Path

import pytest

from src.collection.collectors import default_collectors as _default_collectors
from src.config import CanaryConfig
from src.main import parse_args, run
from src.telemetry import TelemetryRecord

REPO_ROOT = Path(__file__).resolve().parents[1]
EXAMPLE_YAML = REPO_ROOT / "canary.example.yaml"


def test_parse_args_defaults():
    args = parse_args([])
    assert args.config == Path("canary.yaml")
    assert args.log_level == "INFO"


def test_parse_args_overrides():
    args = parse_args(["--config", "/etc/canary.yaml", "--log-level", "DEBUG"])
    assert args.config == Path("/etc/canary.yaml")
    assert args.log_level == "DEBUG"


@pytest.mark.asyncio
async def test_run_exits_on_stop_event():
    config = CanaryConfig.from_yaml(EXAMPLE_YAML)
    stop = asyncio.Event()

    async def trigger_stop():
        await asyncio.sleep(0.01)
        stop.set()

    await asyncio.wait_for(
        asyncio.gather(run(config, stop_event=stop, start_collectors=False), trigger_stop()),
        timeout=1.0,
    )


def test_collection_only_is_retired() -> None:
    """R03.11, operator decision 2026-09-25: the ``--collection-only`` drain
    (no decode, no arming, no GPS sink) is gone; the flag is refused rather
    than silently ignored."""
    with pytest.raises(SystemExit):
        parse_args(["--collection-only"])
    import src.main as main_mod

    assert not hasattr(main_mod, "run_collection")
    assert not hasattr(main_mod, "_drain_queue")


def test_main_restores_passive_before_the_vin_probe(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """D03.30: the startup VIN probe transmits Mode 09, so a passive arm the
    previous process persisted must already hold when it runs — and ``run``
    gets the same file to keep writing."""
    import json
    import time

    import src.main as main_mod
    from src.collection import tx_gate
    from tests.configs import isolated_config

    config = isolated_config(tmp_path, profiles={"enabled": True})
    state = tx_gate.passive_state_path(config)
    state.parent.mkdir(parents=True, exist_ok=True)
    state.write_text(
        json.dumps(
            {
                "version": tx_gate.PASSIVE_STATE_VERSION,
                "enabled": True,
                "armed_at": time.time() - 60,
                "ttl_seconds": 1800,
                "source": "control",
            }
        )
    )
    seen: dict[str, object] = {}

    def _probe(_config: CanaryConfig, override_slug: str | None = None) -> object:
        seen["passive_at_probe"] = tx_gate.is_passive()
        raise SystemExit(0)  # stop before scope_config/run; the probe is the point

    monkeypatch.setattr(main_mod, "configure_logging", lambda _level: None)
    monkeypatch.setattr(main_mod.CanaryConfig, "from_yaml", classmethod(lambda cls, _p: config))
    monkeypatch.setattr(main_mod, "resolve_active_profile", _probe)

    with pytest.raises(SystemExit):
        main_mod.main(["--config", str(tmp_path / "canary.yaml")])

    assert seen["passive_at_probe"] is True
    assert tx_gate.snapshot()["source"] == "restored:control"
    assert tx_gate.state_path() == state


@pytest.mark.asyncio
async def test_default_collectors_use_canable_obd_by_default(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    config = CanaryConfig.from_yaml(EXAMPLE_YAML)
    captured = {}

    async def fake_collect_can_obd(
        spec,
        queue,
        stop_event,
        *,
        reconnect_policy,
        on_scheduler_metrics=None,
        on_handoff_stats=None,
    ):
        captured["spec"] = spec
        captured["queue"] = queue
        captured["reconnect_policy"] = reconnect_policy
        stop_event.set()

    monkeypatch.setattr(
        "src.collection.collectors.collect_can_obd_from_config", fake_collect_can_obd
    )

    collectors = _default_collectors(config)
    queue: asyncio.Queue[TelemetryRecord] = asyncio.Queue()
    stop = asyncio.Event()

    assert len(collectors) == 1
    await collectors[0](queue, stop)

    spec = captured["spec"]
    assert spec.can == config.hardware.can
    assert spec.pids == ("auto",)
    # canary.example.yaml ships with the high-value Nissan DIDs enabled so
    # behavior recipes (air_charge_vs_rpm_throttle_did, afr_bank_balance_vs_rpm,
    # etc.) feed at the next refit. The test pins the list shape, not exact
    # contents — order is preserved from yaml.
    assert "ECM_AIR_CHARGE" in spec.dids
    assert "ECM_AF_SENSOR_V_B1" in spec.dids
    assert spec.did_request_id == 0x7E0
    assert captured["queue"] is queue
    assert captured["reconnect_policy"].initial_delay_seconds == 10
    # Task 2.6: no data/custom_pids.yaml on disk for this test config ⇒ zero
    # custom PIDs, no crash, no warning — the common (no-file) case.
    assert spec.custom_pids == ()


@pytest.mark.asyncio
async def test_default_collectors_loads_custom_pids_when_present(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """Task 2.6: a present data/custom_pids.yaml is loaded once at startup
    and threaded into the canable CanObdSpec."""
    custom_pids_path = tmp_path / "custom_pids.yaml"
    custom_pids_path.write_text(
        "schema_version: 1\n"
        "custom_pids:\n"
        "  - name: OIL_PRESS\n"
        "    display_name: Oil Pressure\n"
        "    mode: 34\n"  # 0x22
        "    pid: 4416\n"  # 0x1140
        "    formula: (A*256+B)/100\n"
        "    unit: psi\n"
    )
    config = CanaryConfig.from_yaml(EXAMPLE_YAML)
    config = config.model_copy(
        update={"daemon": config.daemon.model_copy(update={"custom_pids": custom_pids_path})}
    )
    captured = {}

    async def fake_collect_can_obd(
        spec,
        queue,
        stop_event,
        *,
        reconnect_policy,
        on_scheduler_metrics=None,
        on_handoff_stats=None,
    ):
        captured["spec"] = spec
        stop_event.set()

    monkeypatch.setattr(
        "src.collection.collectors.collect_can_obd_from_config", fake_collect_can_obd
    )

    collectors = _default_collectors(config)
    queue: asyncio.Queue[TelemetryRecord] = asyncio.Queue()
    stop = asyncio.Event()
    await collectors[0](queue, stop)

    spec = captured["spec"]
    assert len(spec.custom_pids) == 1
    assert spec.custom_pids[0].name == "OIL_PRESS"


@pytest.mark.asyncio
async def test_default_collectors_legacy_elm327_starts_passive_can_and_obd(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """C07.9 — the legacy ELM327-plus-passive configuration still starts
    both collectors, and the passive one receives the receive-stats hook."""
    config = CanaryConfig.from_yaml(EXAMPLE_YAML)
    config = config.model_copy(
        update={"collection": config.collection.model_copy(update={"obd_source": "elm327"})}
    )
    started: dict[str, object] = {}

    async def fake_collect_can_from_config(
        spec, queue, stop_event, *, reconnect_policy, on_receive_stats=None
    ):
        started["can"] = (spec, queue, on_receive_stats)

    async def fake_collect_obd(spec, queue, stop_event, *, reconnect_policy):
        started["obd"] = (spec, queue)

    monkeypatch.setattr(
        "src.collection.collectors.collect_can_from_config", fake_collect_can_from_config
    )
    monkeypatch.setattr("src.collection.collectors.collect_obd", fake_collect_obd)

    def stats_hook(_stats: object) -> None:
        pass

    collectors = _default_collectors(config, can_receive_stats_callback=stats_hook)
    queue: asyncio.Queue[TelemetryRecord] = asyncio.Queue()
    stop = asyncio.Event()

    assert len(collectors) == 2
    for collector in collectors:
        await collector(queue, stop)

    assert set(started) == {"can", "obd"}
    can_spec, can_queue, hook = started["can"]  # type: ignore[misc]
    assert can_spec == config.hardware.can
    assert can_queue is queue
    assert hook is stats_hook
    obd_spec, obd_queue = started["obd"]  # type: ignore[misc]
    assert obd_spec.port == config.hardware.elm327_port
    assert obd_queue is queue
