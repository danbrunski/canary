"""E06.3: the Pi benchmark's paired-run and timing harness (``tools/bench_ecu_twin.py``).

The harness parts that need no extension run everywhere: the percentile
arithmetic, the budget verdict, and the real tuples it reads from the
committed October 4 capture. The paired runs and the crossing probes need the
optional ``canary_ecu_twin`` extension and skip by name without it, as they do
in CI (no Rust toolchain there); see ``tests/test_ecu_twin_core_rust.py`` for
how to build it into a scratch directory.
"""

from __future__ import annotations

import importlib.util
from typing import Any

import pytest

from tools import bench_ecu_twin as bench

HAS_EXT = importlib.util.find_spec(bench.EXTENSION) is not None
needs_ext = pytest.mark.skipif(
    not HAS_EXT,
    reason="E06.3 Rust extension canary_ecu_twin is not built (optional; see module docstring)",
)


def test_percentiles_are_nearest_rank_in_microseconds() -> None:
    s = bench.stats_us([i * 1000 for i in range(1, 101)])  # 1..100 µs
    assert (s["n"], s["p50"], s["p95"], s["p99"], s["max"]) == (100, 50.0, 95.0, 99.0, 100.0)
    one = bench.stats_us([7_000])
    assert one["p50"] == one["p99"] == one["max"] == 7.0


def _stat(p99: float, worst: float) -> dict[str, float]:
    return {"n": 1, "p50": p99, "p95": p99, "p99": p99, "max": worst, "mean": p99}


def test_the_budget_names_each_limit_it_misses() -> None:
    ok, slow = _stat(10.0, 20.0), _stat(1_500.0, 6_000.0)
    lat: dict[str, Any] = {
        "single": {"rust_word": ok, "rust_command": ok, "python_predict": slow},
        "batch": {"4096": {"rust_word_batch": ok, "python_target_command_loop": slow}},
    }
    memory = {
        "probes": {
            "ext": {
                "footprint_bytes": 160,
                "rss_kib": {
                    "start": 1000,
                    "after_table": 1000 + 2048,  # 2 MiB: over the 1 MiB budget
                    "after_500_max_batches": 3048,
                    "after_1000_max_batches": 3048,
                },
            }
        }
    }
    verdict = bench.budget(lat, memory)
    assert verdict["rust_word (1 tuple)"]["p99_ok"] and verdict["rust_word (1 tuple)"]["max_ok"]
    today = verdict["python_predict (1 tuple, today's path)"]
    assert not today["p99_ok"] and not today["max_ok"]
    state = verdict["state_bytes"]
    assert state["table_ok"] and not state["rss_ok"] and state["rss_steady_growth_bytes"] == 0


def test_the_real_tuples_are_raw_words_from_the_committed_capture() -> None:
    tuples = bench.observed_tuples(bench.ground_truth_frame())
    assert len(tuples) > 500
    assert all(
        0 <= rpm <= 0xFFFF and 0 <= load <= 0xFF and 0 <= f <= 0xFF for rpm, load, f in tuples
    )
    # Raw counts, the catalog decode undone: the rpm word is 12.5 rpm/count,
    # so a warm idle (~700 rpm) sits near 56 counts, never at display rpm.
    assert min(rpm for rpm, _, _ in tuples) < 200
    # The stored twin decides none of them: the capture's reads are too far
    # apart for its skew gate (E02.3's records are all unevaluable).
    assert bench.kernel_calls(bench.ground_truth_frame()) == []


def test_random_tuples_are_seeded() -> None:
    assert bench.random_tuples(50) == bench.random_tuples(50)


@needs_ext
def test_the_probes_isolate_each_part_of_a_crossing() -> None:
    ext = bench.load_extension()
    _, _, _, t, k = bench.real_twin()
    table = bench._target_phi_table(ext, t, k)
    batch = bench.random_tuples(64)
    assert table.bench_convert(batch) == 64
    assert len(table.bench_compute_ns(batch, 5)) == 5
    words = ext.bench_return_words(16)
    assert len(words) == 16 and words[7] is None and words[0] == 0x4A80
    assert ext.bench_noop() is None and ext.bench_detach() is None
    for probe in (table.bench_convert, lambda b: table.bench_compute_ns(b, 1)):
        with pytest.raises(ValueError, match="exceeds"):
            probe([(0, 0, 0)] * (ext.MAX_BATCH + 1))
    with pytest.raises(ValueError, match="exceeds"):
        ext.bench_return_words(ext.MAX_BATCH + 1)


@needs_ext
def test_a_small_paired_run_reports_identical_outputs_and_every_section() -> None:
    result = bench.run(
        session=None,
        iterations=50,
        random_n=200,
        throughput_n=500,
        startup_runs=0,
        batch_sizes=[1, 64],
        build_info={"note": "test"},
    )
    parity = result["parity"]
    assert parity["identical"] is True
    assert parity["vectors"]["lookup"]["vectors"] == 4160
    assert parity["vectors"]["state"]["vectors"] == 1242
    real = parity["real_ground_truth_fixture"]
    assert real["mismatches"] == 0 and real["python_sha256"] == real["rust_sha256"]
    assert set(result["latency_us"]["batch"]) == {"1", "64"}
    assert result["throughput_tuples_per_s"]["tuples"] == 500
    assert "rust_word (1 tuple)" in result["budget_e04_3"]
