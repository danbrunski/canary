#!/usr/bin/env python3
"""Micro-benchmark for ``LiveFeatureSource.tick()`` (informs Task 3.5).

NFR-01 says the daemon must stay under 50% Pi 5 CPU steady-state. The
live-pipeline tick is the recompute-each-second cost (segment +
deviation-columns + ``compute_features`` over the buffer). This script
runs the tick in a tight loop with synthetic OBD+CAN records to get a
lower-bound latency distribution on whatever host it runs on. It does
*not* exercise the network / disk paths — those have their own
benchmarks — so the headroom on the laptop here translates roughly into
"if Pi 5 is ~3x slower, do I still have budget?".

Usage:

    .venv/bin/python scripts/benchmark_live_pipeline.py
    .venv/bin/python scripts/benchmark_live_pipeline.py --ticks 1000 --channels 20

Output: count, mean, median, p95, p99, max — all in milliseconds. Plus
"effective max sustained Hz" = 1 / p95, the conservative cadence at which
the pipeline keeps up without exceeding the p95 latency.
"""

from __future__ import annotations

import argparse
import statistics
import sys
import time
from datetime import UTC, datetime, timedelta
from pathlib import Path

from _cli import load_config

from src.decode.bindings import load_bindings
from src.preprocessing.live import LiveFeatureSource
from src.telemetry import TelemetryRecord


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        prog="benchmark_live_pipeline",
        description="Measure LiveFeatureSource.tick() latency over N ticks.",
    )
    parser.add_argument(
        "--config",
        type=Path,
        default=Path("canary.example.yaml"),
        help="Path to canary.yaml.",
    )
    parser.add_argument(
        "--bindings",
        type=Path,
        default=Path("data/bindings.yaml"),
        help="Path to bindings.yaml (optional). Pass /dev/null to skip CAN decode.",
    )
    parser.add_argument(
        "--ticks",
        type=int,
        default=500,
        help="Number of ticks to benchmark after warmup. Default 500.",
    )
    parser.add_argument(
        "--warmup-ticks",
        type=int,
        default=60,
        help="Warmup ticks (fill the rolling buffer before measuring). Default 60.",
    )
    parser.add_argument(
        "--channels",
        type=int,
        default=16,
        help="Number of OBD channels to simulate. Default 16 (matches the daemon).",
    )
    parser.add_argument(
        "--can-frames-per-tick",
        type=int,
        default=20,
        help=(
            "CAN frames observed between ticks. ~20 at 1Hz tick / 500 fps bus "
            "is realistic for the Xterra."
        ),
    )
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)

    config = load_config(args)
    bindings = load_bindings(args.bindings) if args.bindings.exists() else None
    source = LiveFeatureSource(config=config, bindings=bindings)

    # Seed channel set: a mix of OBD PIDs the daemon's --obd-pids default
    # exposes. The exact names don't matter — what matters is the column count.
    obd_pids = [
        "RPM",
        "SPEED",
        "COOLANT_TEMP",
        "INTAKE_TEMP",
        "INTAKE_PRESSURE",
        "MAF",
        "THROTTLE_POS",
        "ENGINE_LOAD",
        "TIMING_ADVANCE",
        "SHORT_FUEL_TRIM_1",
        "LONG_FUEL_TRIM_1",
        "SHORT_FUEL_TRIM_2",
        "LONG_FUEL_TRIM_2",
        "O2_B1S1",
        "O2_B2S1",
        "BAROMETRIC_PRESSURE",
        "CONTROL_MODULE_VOLTAGE",
        "RUN_TIME",
    ][: args.channels]

    can_ids = list(bindings._bindings_by_id.keys()) if False else []  # noqa: SIM108
    # Use the bindings the source knows about, or fall back to a few hex IDs
    # that won't decode (which exercises the "unknown ID → skip" path).
    if bindings is not None:
        can_ids = sorted({sig.can_id for sig in bindings.signals})[: args.can_frames_per_tick]
    if not can_ids:
        can_ids = [0x1F9, 0x551, 0x23D]

    base = datetime(2026, 5, 13, 12, 0, tzinfo=UTC)
    print(f"warming up ({args.warmup_ticks} ticks)…", file=sys.stderr)
    _run_loop(source, obd_pids, can_ids, base, args.warmup_ticks, measure=False)

    print(f"measuring ({args.ticks} ticks)…", file=sys.stderr)
    latencies = _run_loop(source, obd_pids, can_ids, base, args.ticks, measure=True)

    _print_report(latencies, source, args)
    return 0


def _run_loop(
    source: LiveFeatureSource,
    obd_pids: list[str],
    can_ids: list[int],
    base: datetime,
    n: int,
    *,
    measure: bool,
) -> list[float]:
    """Run ``n`` ticks; return per-tick latency in ms when ``measure`` is True."""
    latencies: list[float] = []
    for i in range(n):
        for j, pid in enumerate(obd_pids):
            # A simple oscillating value per channel keeps rolling-std meaningful
            value = 700.0 + (i + j) % 40
            source.observe(TelemetryRecord.obd(float(i), pid, value, ""))
        for k, can_id in enumerate(can_ids):
            payload = bytes([(i + k) % 256, 0, ((i + k) * 31) % 256, 0, 0, 0, 0, 0])
            source.observe(TelemetryRecord.can(float(i), can_id, payload))

        if measure:
            started = time.perf_counter()
            source.tick(base + timedelta(seconds=i))
            latencies.append((time.perf_counter() - started) * 1000.0)
        else:
            source.tick(base + timedelta(seconds=i))
    return latencies


def _print_report(
    latencies: list[float], source: LiveFeatureSource, args: argparse.Namespace
) -> None:
    if not latencies:
        print("no latencies measured", file=sys.stderr)
        return
    latencies_sorted = sorted(latencies)
    p = lambda q: latencies_sorted[max(0, int(q * len(latencies_sorted)) - 1)]  # noqa: E731

    print("=" * 60)
    print(f"LiveFeatureSource.tick()  ({len(latencies)} ticks)")
    print("=" * 60)
    print(f"  channels observed:        {len(source.known_channels)}")
    print(f"  buffer rows at end:       {source.buffer_size}")
    print(f"  rolling window (s):       {source.config.preprocessing.rolling_window_seconds}")
    print(f"  resample_hz:              {source.config.preprocessing.resample_hz}")
    print()
    print(f"  mean      {statistics.mean(latencies):7.3f} ms")
    print(f"  median    {statistics.median(latencies):7.3f} ms")
    print(f"  p95       {p(0.95):7.3f} ms")
    print(f"  p99       {p(0.99):7.3f} ms")
    print(f"  max       {max(latencies):7.3f} ms")
    print()
    # Conservative cadence: at p95 latency, how many ticks/sec could we sustain
    # without queuing?
    sustained_hz = 1000.0 / p(0.95) if p(0.95) > 0 else float("inf")
    print(f"  sustained @ p95:        {sustained_hz:6.1f} Hz")
    print()
    print(
        f"  (NFR-01 budget is <50% Pi 5 CPU steady-state at "
        f"{args.channels} channels × ~{args.can_frames_per_tick} CAN frames per "
        f"tick at {source.config.preprocessing.resample_hz} Hz; multiply this "
        f"laptop result by ~3× as a rough Pi 5 single-core projection.)"
    )


if __name__ == "__main__":
    sys.exit(main())
