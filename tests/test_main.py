"""Tests for the daemon entry point."""

import asyncio
from datetime import UTC, datetime
from pathlib import Path

import pyarrow.parquet as pq
import pytest

from src.config import CanaryConfig
from src.main import _default_collectors, parse_args, run, run_collection
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


@pytest.mark.asyncio
async def test_run_collection_writes_collector_records(tmp_path: Path):
    config = CanaryConfig.from_yaml(EXAMPLE_YAML)
    config = config.model_copy(
        update={
            "collection": config.collection.model_copy(
                update={
                    "storage_path": tmp_path,
                    "session_retention_days": 30,
                }
            )
        }
    )
    stop = asyncio.Event()

    async def collector(queue: asyncio.Queue[TelemetryRecord], stop_event: asyncio.Event) -> None:
        await queue.put(
            TelemetryRecord(
                timestamp=datetime(2026, 5, 11, tzinfo=UTC),
                source="obd",
                channel="RPM",
                value=700.0,
                unit="rpm",
            )
        )
        stop_event.set()

    await run_collection(config, stop, collectors=[collector])

    files = sorted(tmp_path.glob("*/telemetry-*.parquet"))
    assert len(files) == 1
    rows = pq.read_table(files[0]).to_pylist()
    assert rows[0]["channel"] == "RPM"
    assert rows[0]["value_number"] == 700.0


@pytest.mark.asyncio
async def test_run_collection_smoke_writes_obd_and_can_records(tmp_path: Path):
    config = CanaryConfig.from_yaml(EXAMPLE_YAML)
    config = config.model_copy(
        update={
            "collection": config.collection.model_copy(
                update={
                    "storage_path": tmp_path,
                    "session_retention_days": 30,
                }
            )
        }
    )
    stop = asyncio.Event()

    async def collector(queue: asyncio.Queue[TelemetryRecord], stop_event: asyncio.Event) -> None:
        await queue.put(TelemetryRecord.obd(1_778_000_000.0, "RPM", 700.0, "rpm"))
        await queue.put(TelemetryRecord.can(1_778_000_000.1, 0x123, b"\x01\x02"))
        stop_event.set()

    await run_collection(config, stop, collectors=[collector])

    files = sorted(tmp_path.glob("*/telemetry-*.parquet"))
    assert len(files) == 1
    rows = pq.read_table(files[0]).to_pylist()
    assert {row["source"] for row in rows} == {"obd", "can"}
    assert {row["channel"] for row in rows} == {"RPM", "0x123"}


@pytest.mark.asyncio
async def test_default_collectors_use_canable_obd_by_default(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    config = CanaryConfig.from_yaml(EXAMPLE_YAML)
    captured = {}

    async def fake_collect_can_obd(
        spec, queue, stop_event, *, reconnect_policy, on_scheduler_metrics=None
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
    # behavior recipes (injector_pw_vs_rpm_load, afr_bank_balance_vs_rpm,
    # etc.) feed at the next refit. The test pins the list shape, not exact
    # contents — order is preserved from yaml.
    assert "ECM_INJECTOR_PW_B1" in spec.dids
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
        spec, queue, stop_event, *, reconnect_policy, on_scheduler_metrics=None
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
