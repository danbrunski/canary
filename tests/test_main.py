"""Tests for the daemon entry point."""

import asyncio
from pathlib import Path

import pytest

from src.config import CanaryConfig
from src.main import parse_args, run

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
        asyncio.gather(run(config, stop_event=stop), trigger_stop()),
        timeout=1.0,
    )
