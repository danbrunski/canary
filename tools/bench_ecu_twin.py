#!/usr/bin/env python
"""E06.3: paired Python/Rust runs and the boundary cost of the ECU twin core.

Runs the optional ``canary_ecu_twin`` extension (``rust/ecu_twin_py``) beside
the Python reference (``src.decode.target_phi_command``) and writes one JSON
document:

- **parity** -- every language-neutral vector (``tests/fixtures/ecu_twin``),
  every lookup tuple the stored twin evaluates in the committed October 4
  capture (and in ``--session``, a whole stored capture), and seeded random
  tuples over the same real table, through both. Each side's outputs are
  hashed (canonical JSON, sha256); identical outputs give identical digests
  and zero mismatches.
- **latency** -- per call, in microseconds (p50/p95/p99/max): tiny calls
  (one tuple), batches up to the binding's bound (``MAX_BATCH``, the worst
  supported batch), and the four parts of a crossing timed on their own
  (argument conversion, interpreter detach/attach, Rust-only computation,
  return handling).
- **throughput** -- tuples per second over the real tuple stream, repeated.
- **memory/startup** -- fresh interpreters: incremental RSS of importing the
  extension, building the table and a steady batch loop; import time.
- **budget** -- the E04.3 first-slice limits (<= 1 ms p99, <= 5 ms max per
  tick, <= 1 MiB table/state) against the numbers above.

A laptop run is a dry run: E06.3 is closed only by a run on the Pi
(``scripts/e06_pi_bench.sh``), and never by these numbers alone -- missed
ticks and collector loss under collection load are a separate measurement
(the E06.3 protocol in ``docs/roadmap/tasks/E06-rust-twin-core-trial.md``).
The tool does no bus, network or daemon I/O.

    PYTHONPATH=<unpacked wheel> .venv/bin/python -m tools.bench_ecu_twin --out r.json
"""

from __future__ import annotations

import argparse
import gc
import hashlib
import importlib
import json
import os
import platform
import random
import statistics
import subprocess
import sys
import time
from collections.abc import Callable, Iterable, Sequence
from functools import partial
from pathlib import Path
from types import ModuleType
from typing import Any

REPO = Path(__file__).resolve().parents[1]
EXTENSION = "canary_ecu_twin"
#: The committed real capture the stored twin's tuples come from (E02.3).
GROUND_TRUTH = REPO / "tests" / "fixtures" / "ground_truth" / "target_phi.yaml"
PROFILE = REPO / "data" / "nissan-xterra-2008"

#: E04.3's proposed first-slice budget for the twin, per 1 Hz inference tick.
BUDGET_P99_US = 1_000.0
BUDGET_MAX_US = 5_000.0
BUDGET_STATE_BYTES = 1 << 20

Json = dict[str, Any]
Tuple3 = tuple[int, int, int]


# -- the extension, run like the vectors' reference -------------------------------------


def load_extension() -> ModuleType:
    """The built extension, or ``SystemExit`` naming how to build it."""
    try:
        return importlib.import_module(EXTENSION)
    except ImportError as exc:
        raise SystemExit(
            f"{EXTENSION} is not importable ({exc}); build it with "
            "`maturin build --release -m rust/ecu_twin_py/Cargo.toml -o <dir>` and put the "
            "unpacked wheel on PYTHONPATH (never into the daemon's venv)"
        ) from exc


def _refusal(error: Exception) -> str:
    if isinstance(error, TypeError):
        return "not_whole_count"  # a float where the binding takes an int
    if isinstance(error, OverflowError):
        return "out_of_domain"  # wider than i64
    return str(error).split(":", 1)[0]


def _target_phi_table(ext: ModuleType, t: Json, k: Json) -> Any:
    return ext.TargetPhiTable(
        t["cells"],
        t["row_breakpoints"],
        t["column_breakpoints"],
        k["flag_bit"],
        k["load_bits"],
        k["floor_cell"],
        k["threshold_cell"],
        k["hysteresis_cells"],
        k["cell_to_word_shift"],
    )


def ext_run(ext: ModuleType, vector: Json, named: Json) -> Json:
    """``{"out": ...}`` or ``{"refuse": kind}`` from the extension, in the
    vectors' shape (the counterpart of ``tests.ecu_twin_vectors.run``)."""
    op, i = vector["op"], vector["in"]
    try:
        if op == "search_axis":
            axis = named["axes"][i["axis"]] if "axis" in i else i["breakpoints"]
            out: Json = {"hit": ext.search_axis(axis, i["value"])}
        elif op == "interpolate_q16":
            out = {"value": ext.interpolate_q16(i["a"], i["b"], i["fraction"])}
        elif op == "lookup_2d":
            t = named["tables"][i["table"]]
            out = ext.lookup_2d(
                t["cells"],
                t["row_breakpoints"],
                t["column_breakpoints"],
                i["row_value"],
                i["column_value"],
            )
        elif op == "lookup_curve_q16":
            c = named["curves"][i["curve"]]
            out = {"value": ext.lookup_curve_q16(c["values"], c["breakpoints"], i["value"])}
        elif op == "lookup_shift12_q16":
            out = {"value": ext.lookup_shift12_q16(named["tables16"][i["table16"]], i["value"])}
        elif op == "target_command":
            table = _target_phi_table(
                ext, named["tables"][i["table"]], named["constants"][i["constants"]]
            )
            out = table.command(i["rpm_raw"], i["load_raw"], i["flag_raw"])
        elif op == "x_from_bfs":
            out = {"value": ext.x_from_bfs(i["bfs"], i["scaler"], i["shift"], i["cap"])}
        elif op in ("load_input_curves", "fuel_comp_load"):
            c = named["load_curves"][i["curves"]]
            curves = ext.LoadCurves(
                c["area_breakpoints"],
                c["area_values"],
                c["fill_low"],
                c["fill_high"],
                c["rpm_multiplier"],
                c["reciprocal_numerator"],
                c["idle_air_cap"],
                c["limp_home_area"],
            )
            if op == "load_input_curves":
                out = {"valid": True}
            else:
                out = curves.reconstruct(
                    i["throttle_counts"],
                    i["rpm_word"],
                    i["idle_air"],
                    i["purge_area"],
                    i["limp_home"],
                )
        elif op == "uprev_power_on":
            out = ext.uprev_power_on(tuple(i["retained"]))
        elif op == "uprev_step":
            out = ext.uprev_step(tuple(i["state"]), i["inputs_2ee0"])
        elif op == "uprev_sequence":
            out = {"steps": ext.uprev_sequence(tuple(i["state"]), i["inputs"])}
        elif op == "loaded_slot":
            out = ext.loaded_slot(i["magic"], i["active_map"])
        else:  # pragma: no cover - a new op must be wired here
            raise AssertionError(f"unwired op {op}")
    except (ValueError, TypeError, OverflowError) as error:
        return {"refuse": _refusal(error)}
    return {"out": out}


# -- the real table and real tuples -------------------------------------------------------


def real_twin() -> tuple[Any, Any, str, Json, Json]:
    """The shipped twin definition, the Xterra table the October 4 capture
    ran on (transcribed, no private ROM), the definition's sha256, and the
    table/constants in the vectors' ``named`` shape."""
    from src.analysis.ecu_application import load_application_model
    from src.analysis.rom_trace import load_viewer_definition
    from src.analysis.target_phi import load_twin_definition, table_from_calibration
    from src.signals.afr import load_afr_calibration
    from src.yaml_load import load_yaml

    expect = load_yaml(GROUND_TRUTH.read_text())
    definition, sha = load_twin_definition(PROFILE)
    table = table_from_calibration(
        load_afr_calibration(PROFILE / "afr_calibration.yaml"),
        definition,
        viewer=load_viewer_definition(PROFILE),
        application_model=load_application_model(PROFILE),
        revision=expect["calibration"]["revision"],
        image_sha256=expect["calibration"]["image_sha256"],
        slot=expect["calibration"]["slot"],
    )
    gate = definition.final_command.enrichment_gate
    named_table = {
        "cells": [list(row) for row in table.cells],
        "row_breakpoints": list(table.row_breakpoints),
        "column_breakpoints": list(table.column_breakpoints),
    }
    constants = {
        "flag_bit": definition.inputs.forced_column_flag.bit,
        "load_bits": definition.inputs.load.bits,
        "floor_cell": gate.floor_cell,
        "threshold_cell": gate.threshold_cell,
        "hysteresis_cells": gate.hysteresis_cells,
        "cell_to_word_shift": definition.table.cell_to_word_shift,
    }
    return definition, table, sha, named_table, constants


def ground_truth_frame() -> Any:
    """The committed October 4 slices as one session frame, hash-checked."""
    import pandas as pd

    from src.yaml_load import load_yaml

    expect = load_yaml(GROUND_TRUTH.read_text())
    frames = []
    for part in expect["telemetry"]:
        path = REPO / part["path"]
        digest = hashlib.sha256(path.read_bytes()).hexdigest()
        if digest != part["sha256"]:
            raise SystemExit(f"{part['path']} does not match its pinned sha256")
        frame = pd.read_parquet(path)
        frame["timestamp"] = pd.to_datetime(frame["timestamp"], utc=True)
        frames.append(frame)
    frame = pd.concat(frames, ignore_index=True)
    return frame.sort_values("timestamp", kind="stable").reset_index(drop=True)


def kernel_calls(session: Any) -> list[Tuple3]:
    """Every ``(rpm_raw, load_raw, flag_raw)`` the stored twin hands its
    kernel for ``session`` (a frame or a session directory), in order: the
    stored evaluator runs unchanged and each kernel call is recorded. Only
    tuples that pass the twin's applicability gates reach the kernel."""
    from src.analysis import target_phi

    definition, table, sha, _, _ = real_twin()
    seen: list[Tuple3] = []
    kernel = getattr(target_phi, "target_command")  # noqa: B009 - the name the twin calls

    def record(*args: Any, **kwargs: Any) -> Any:
        seen.append((int(kwargs["rpm_raw"]), int(kwargs["load_raw"]), int(kwargs["flag_raw"])))
        return kernel(*args, **kwargs)

    setattr(target_phi, "target_command", record)  # noqa: B010
    try:
        target_phi.evaluate_session(session, definition, table, definition_sha256=sha)
    finally:
        setattr(target_phi, "target_command", kernel)  # noqa: B010
    return seen


def observed_tuples(session: Any) -> list[Tuple3]:
    """The real lookup inputs of ``session`` as raw counts (the catalog
    decode undone, as the twin does), one tuple per input read once all three
    have been read, each carrying the latest read of the other two.

    These are real values, not the twin's applicability-gated tuples (that is
    :func:`kernel_calls`): kernel parity and cost do not depend on whether a
    tuple would be eligible, and a capture whose reads are too far apart for
    the twin's skew gate still holds real rpm, load and flag words."""
    from src.analysis.channels import read_channels
    from src.analysis.rom_trace import raw_counts, stored_decode
    from src.paths import DIDS_YAML

    definition = real_twin()[0]
    inputs = definition.inputs.channels()
    specs = [inputs["rpm"], inputs["load"], inputs["flag"]]
    long = read_channels(session, specs, dids_path=DIDS_YAML, acquired_time=True)
    reads: list[tuple[Any, int, int]] = []
    for position, spec in enumerate(specs):
        rows = long[long["spec"] == spec].dropna(subset=["value"])
        decode = stored_decode(spec, DIDS_YAML)
        if decode is None:
            raise SystemExit(f"{spec} is not a catalog memory channel")
        raw = raw_counts(rows["value"].to_numpy(dtype=float), decode)
        if raw is None:
            raise SystemExit(f"{spec} is stored under another decode")
        reads += [(t, position, int(v)) for t, v in zip(rows["timestamp"], raw, strict=True)]
    reads.sort(key=lambda r: (r[0], r[1]))
    latest: list[int | None] = [None, None, None]
    out: list[Tuple3] = []
    for _, position, value in reads:
        latest[position] = value
        rpm, load, flag = latest
        if rpm is not None and load is not None and flag is not None:
            out.append((rpm, load, flag))
    return out


# -- parity ----------------------------------------------------------------------------------


def _digest(outputs: Iterable[Any]) -> str:
    h = hashlib.sha256()
    for out in outputs:
        h.update(json.dumps(out, sort_keys=True, separators=(",", ":")).encode())
        h.update(b"\n")
    return h.hexdigest()


def vector_parity(ext: ModuleType) -> Json:
    """Every committed vector through both: outputs, refusal kinds, traces."""
    from tests import ecu_twin_vectors as vectors

    refusals = vectors.contract()["refusals"]
    report: Json = {}
    for name, path in (("lookup", vectors.LOOKUP_PATH), ("state", vectors.STATE_PATH)):
        document = json.loads(path.read_text())
        py_out, rs_out, mismatches, unexpected = [], [], [], 0
        for vector in document["vectors"]:
            py = vectors.run(vector, document["named"], refusals)
            rs = ext_run(ext, vector, document["named"])
            py_out.append(py)
            rs_out.append(rs)
            if py != rs:
                mismatches.append(vector["id"])
            expected = {k: vector[k] for k in ("out", "refuse") if k in vector}
            unexpected += py != expected
        report[name] = {
            "vectors": len(document["vectors"]),
            "mismatches": len(mismatches),
            "first_mismatches": mismatches[:5],
            "python_differs_from_committed": unexpected,
            "python_sha256": _digest(py_out),
            "rust_sha256": _digest(rs_out),
        }
    return report


def tuple_parity(ext: ModuleType, tuples: Sequence[Tuple3], named_table: Json, k: Json) -> Json:
    """Each tuple through the Python reference and through all three of the
    extension's entry points (full trace, word, bounded batch)."""
    from tests import ecu_twin_vectors as vectors

    refusals = vectors.contract()["refusals"]
    named = {"tables": {"real": named_table}, "constants": {"real": k}}
    table = _target_phi_table(ext, named_table, k)
    py_out, rs_out, mismatches = [], [], 0
    batched: list[int | None] = []
    for start in range(0, len(tuples), ext.MAX_BATCH):
        batched += table.word_batch(list(tuples[start : start + ext.MAX_BATCH]))
    for (rpm, load, flag), batch_word in zip(tuples, batched, strict=True):
        vector = {
            "op": "target_command",
            "in": {
                "table": "real",
                "constants": "real",
                "rpm_raw": rpm,
                "load_raw": load,
                "flag_raw": flag,
            },
        }
        py = vectors.run(vector, named, refusals)
        rs = ext_run(ext, vector, named)
        word = table.word(rpm, load, flag)
        py_out.append(py)
        rs_out.append(rs)
        py_word = py.get("out", {}).get("word")
        mismatches += py != rs or word != py_word or batch_word != py_word
    words = [o.get("out", {}).get("word") for o in py_out]
    return {
        "tuples": len(tuples),
        "distinct": len(set(tuples)),
        "mismatches": mismatches,
        "branches": dict(
            sorted(
                {
                    b: sum(1 for o in py_out if o.get("out", {}).get("branch") == b)
                    for b in ("ordinary", "override")
                }.items()
            )
        ),
        "gate_band": sum(1 for w in words if w is None),
        "python_sha256": _digest(py_out),
        "rust_sha256": _digest(rs_out),
    }


def random_tuples(n: int, seed: int = 0xE0603) -> list[Tuple3]:
    rng = random.Random(seed)
    return [
        (rng.randint(0, 0xFFFF), rng.randint(0, 0xFF), rng.choice((0, 1, rng.randint(0, 0xFF))))
        for _ in range(n)
    ]


# -- timing ------------------------------------------------------------------------------


def stats_us(samples_ns: Sequence[int]) -> Json:
    """p50/p95/p99/max (nearest-rank) and mean of nanosecond samples, in µs."""
    ordered = sorted(samples_ns)
    n = len(ordered)

    def rank(q: float) -> float:
        return ordered[min(n - 1, max(0, -(-int(q * n * 1000) // 1000) - 1))] / 1e3

    return {
        "n": n,
        "p50": rank(0.50),
        "p95": rank(0.95),
        "p99": rank(0.99),
        "max": ordered[-1] / 1e3,
        "mean": statistics.fmean(ordered) / 1e3,
    }


def time_calls(call: Callable[[], object], iterations: int, warmup: int = 200) -> Json:
    """Wall time of each call, with the collector paused (a GC pause is the
    interpreter's, not the call's; the daemon's own GC is part of E04.3's
    under-load run)."""
    for _ in range(min(warmup, iterations)):
        call()
    samples = []
    clock = time.perf_counter_ns
    gc.disable()
    try:
        for _ in range(iterations):
            start = clock()
            call()
            samples.append(clock() - start)
    finally:
        gc.enable()
    return stats_us(samples)


def _each(call: Callable[[Tuple3], object], batch: Sequence[Tuple3]) -> None:
    for t in batch:
        call(t)


def _cycle(tuples: Sequence[Tuple3]) -> Callable[[], Tuple3]:
    position = [0]

    def nxt() -> Tuple3:
        position[0] = (position[0] + 1) % len(tuples)
        return tuples[position[0]]

    return nxt


def latency(
    ext: ModuleType,
    tuples: Sequence[Tuple3],
    named_table: Json,
    k: Json,
    *,
    iterations: int,
    batch_sizes: Sequence[int],
) -> Json:
    from src.analysis import ecu_state_models as sm
    from src.analysis.target_phi import predict
    from src.decode.target_phi_command import GateConstants, target_command

    definition, table_py, _, _, _ = real_twin()
    table = _target_phi_table(ext, named_table, k)
    gate = GateConstants(k["floor_cell"], k["threshold_cell"], k["hysteresis_cells"])
    cells, rows, columns = table_py.cells, table_py.row_breakpoints, table_py.column_breakpoints

    def py_kernel(t: Tuple3) -> object:
        return target_command(
            cells,
            rows,
            columns,
            rpm_raw=t[0],
            load_raw=t[1],
            flag_raw=t[2],
            flag_bit=k["flag_bit"],
            load_bits=k["load_bits"],
            gate=gate,
            cell_to_word_shift=k["cell_to_word_shift"],
        )

    nxt = _cycle(tuples)
    out: Json = {"single": {}, "batch": {}, "parts": {}}
    single = out["single"]
    single["python_target_command"] = time_calls(lambda: py_kernel(nxt()), iterations)

    def py_predict(t: Tuple3) -> object:
        return predict(table_py, definition, rpm_raw=t[0], load_raw=t[1], flag_raw=t[2])

    single["python_predict"] = time_calls(lambda: py_predict(nxt()), iterations)
    single["rust_word"] = time_calls(lambda: table.word(*nxt()), iterations)
    single["rust_command"] = time_calls(lambda: table.command(*nxt()), iterations)
    single["rust_word_batch_1"] = time_calls(lambda: table.word_batch([nxt()]), iterations)

    # The stateful path: one UpRev cycle from a known committed state.
    state = (0, 0, 1, 0xAA5555AA, 1, 0x01010101)
    py_state = sm.UpRevState(*state)
    single["python_uprev_step"] = time_calls(lambda: sm.uprev_step(py_state, 0), iterations)
    single["rust_uprev_step"] = time_calls(lambda: ext.uprev_step(state, 0), iterations)

    parts = out["parts"]
    parts["noop_call"] = time_calls(ext.bench_noop, iterations)
    parts["detach_attach_call"] = time_calls(lambda: ext.bench_detach(), iterations)

    rng = random.Random(0xE063)
    for size in batch_sizes:
        batch = [tuples[rng.randrange(len(tuples))] for _ in range(size)]
        # Every size gets enough calls for a p99; the big ones are capped by time.
        reps = iterations if size <= 16 else max(200, iterations * 16 // size)
        row: Json = {
            "rust_word_batch": time_calls(partial(table.word_batch, batch), reps, warmup=20),
            "rust_convert": time_calls(partial(table.bench_convert, batch), reps, warmup=20),
            "rust_return": time_calls(partial(ext.bench_return_words, size), reps, warmup=20),
        }
        compute = table.bench_compute_ns(batch, reps)
        row["rust_compute"] = stats_us(compute)
        py_reps = iterations if size <= 16 else max(50, iterations * 4 // size)
        row["python_target_command_loop"] = time_calls(
            partial(_each, py_kernel, batch), py_reps, warmup=5
        )
        parts_p50 = (
            row["rust_convert"]["p50"]
            + parts["detach_attach_call"]["p50"]
            - parts["noop_call"]["p50"]
            + row["rust_compute"]["p50"]
            + row["rust_return"]["p50"]
        )
        row["sum_of_parts_p50"] = parts_p50
        row["per_tuple_p50"] = {
            "rust_word_batch": row["rust_word_batch"]["p50"] / size,
            "python_target_command_loop": row["python_target_command_loop"]["p50"] / size,
        }
        out["batch"][str(size)] = row
    return out


def throughput(
    ext: ModuleType, tuples: Sequence[Tuple3], named_table: Json, k: Json, total: int
) -> Json:
    """Tuples per second over the real stream repeated to ``total``."""
    from src.decode.target_phi_command import GateConstants, target_command

    stream = [tuples[i % len(tuples)] for i in range(total)]
    table = _target_phi_table(ext, named_table, k)
    gate = GateConstants(k["floor_cell"], k["threshold_cell"], k["hysteresis_cells"])
    cells, rows, cols = (
        named_table["cells"],
        named_table["row_breakpoints"],
        named_table["column_breakpoints"],
    )

    def python() -> None:
        for r, ld, f in stream:
            target_command(
                cells,
                rows,
                cols,
                rpm_raw=r,
                load_raw=ld,
                flag_raw=f,
                flag_bit=k["flag_bit"],
                load_bits=k["load_bits"],
                gate=gate,
                cell_to_word_shift=k["cell_to_word_shift"],
            )

    def word() -> None:
        for t in stream:
            table.word(*t)

    def command() -> None:
        for t in stream:
            table.command(*t)

    def batched() -> None:
        for s in range(0, len(stream), ext.MAX_BATCH):
            table.word_batch(stream[s : s + ext.MAX_BATCH])

    out: Json = {"tuples": total}
    for name, run in (
        ("python_target_command", python),
        ("rust_word", word),
        ("rust_command", command),
        ("rust_word_batch_max", batched),
    ):
        start = time.perf_counter()
        run()
        out[name] = round(total / (time.perf_counter() - start))
    return out


# -- fresh-interpreter probes (RSS, import) -------------------------------------------------


def _rss_kib() -> int | None:
    try:
        for line in Path("/proc/self/status").read_text().splitlines():
            if line.startswith("VmRSS:"):
                return int(line.split()[1])
    except OSError:
        pass
    return None


def probe(kind: str) -> Json:
    """Run inside a fresh interpreter (``--probe``): import cost and the RSS
    each step adds."""
    rss: Json = {"start": _rss_kib()}
    start = time.perf_counter()
    if kind == "ext":
        ext = importlib.import_module(EXTENSION)
        imported = time.perf_counter() - start
        rss["after_import"] = _rss_kib()
        # The real table's shape, without importing the stored-path modules.
        cells = [[0x80 + ((r * 8 + c) % 32) for c in range(8)] for r in range(8)]
        rows = [b * 4 for b in (0x18, 0x20, 0x28, 0x38, 0x48, 0x50, 0x58, 0x68)]
        columns = [0x34, 0x44, 0x5D, 0x64, 0x6C, 0x6E, 0x7F, 0x80]
        table = ext.TargetPhiTable(cells, rows, columns, 0, 8, 128, 130, 1, 7)
        rss["after_table"] = _rss_kib()
        batch = random_tuples(ext.MAX_BATCH)
        for _ in range(500):
            table.word_batch(batch)
        rss["after_500_max_batches"] = _rss_kib()
        for _ in range(500):
            table.word_batch(batch)
        rss["after_1000_max_batches"] = _rss_kib()
        footprint = table.footprint_bytes
    elif kind == "python":
        mod = importlib.import_module("src.decode.target_phi_command")
        imported = time.perf_counter() - start
        rss["after_import"] = _rss_kib()
        cells = [[0x80 + ((r * 8 + c) % 32) for c in range(8)] for r in range(8)]
        rows = [b * 4 for b in (0x18, 0x20, 0x28, 0x38, 0x48, 0x50, 0x58, 0x68)]
        columns = [0x34, 0x44, 0x5D, 0x64, 0x6C, 0x6E, 0x7F, 0x80]
        gate = mod.GateConstants(128, 130, 1)
        rss["after_table"] = _rss_kib()
        batch = random_tuples(4096)
        for i in range(2):
            for r, ld, f in batch:
                mod.target_command(
                    cells,
                    rows,
                    columns,
                    rpm_raw=r,
                    load_raw=ld,
                    flag_raw=f,
                    flag_bit=0,
                    load_bits=8,
                    gate=gate,
                    cell_to_word_shift=7,
                )
            rss[f"after_{(i + 1) * 4096}_calls"] = _rss_kib()
        footprint = None
    elif kind == "none":
        imported = 0.0
        footprint = None
    else:
        raise SystemExit(f"unknown probe {kind!r}")
    return {"kind": kind, "import_s": imported, "rss_kib": rss, "footprint_bytes": footprint}


def _child(args: list[str], env: dict[str, str]) -> tuple[float, str]:
    start = time.perf_counter()
    done = subprocess.run(
        [sys.executable, *args], capture_output=True, text=True, env=env, cwd=REPO, check=True
    )
    return time.perf_counter() - start, done.stdout


def startup_and_memory(runs: int) -> Json:
    env = dict(os.environ)
    env["PYTHONDONTWRITEBYTECODE"] = "1"
    walls: dict[str, list[float]] = {"bare": [], "ext": [], "python_reference": []}
    for _ in range(runs):
        walls["bare"].append(_child(["-c", "pass"], env)[0])
        walls["ext"].append(_child(["-c", f"import {EXTENSION}"], env)[0])
        walls["python_reference"].append(
            _child(["-c", "import src.decode.target_phi_command"], env)[0]
        )
    probes = {}
    for kind in ("none", "ext", "python"):
        _, text = _child(["-m", "tools.bench_ecu_twin", "--probe", kind], env)
        probes[kind] = json.loads(text)
    return {
        "interpreter_wall_ms": {
            name: {
                "median": statistics.median(v) * 1e3,
                "max": max(v) * 1e3,
                "runs": len(v),
            }
            for name, v in walls.items()
        },
        "import_added_ms_median": {
            "ext": (statistics.median(walls["ext"]) - statistics.median(walls["bare"])) * 1e3,
            "python_reference": (
                statistics.median(walls["python_reference"]) - statistics.median(walls["bare"])
            )
            * 1e3,
        },
        "probes": probes,
    }


# -- budget ------------------------------------------------------------------------------


def budget(lat: Json, memory: Json | None) -> Json:
    """E04.3's limits against the per-call numbers: a tick runs the twin on at
    most a handful of tuples, so the one-tuple call is the tick's unit and the
    full bounded batch is its worst supported case."""
    biggest = max(lat["batch"], key=int)
    rows = {
        "rust_word (1 tuple)": lat["single"]["rust_word"],
        "rust_command (1 tuple, full trace)": lat["single"]["rust_command"],
        "python_predict (1 tuple, today's path)": lat["single"]["python_predict"],
        f"rust_word_batch ({biggest}, worst supported)": lat["batch"][biggest]["rust_word_batch"],
        f"python loop ({biggest})": lat["batch"][biggest]["python_target_command_loop"],
    }
    verdict: Json = {
        name: {
            "p99_us": s["p99"],
            "max_us": s["max"],
            "p99_ok": s["p99"] <= BUDGET_P99_US,
            "max_ok": s["max"] <= BUDGET_MAX_US,
        }
        for name, s in rows.items()
    }
    if memory is not None:
        ext_probe = memory["probes"]["ext"]
        rss = ext_probe["rss_kib"]
        if rss.get("start") is not None:
            added = (rss["after_table"] - rss["start"]) * 1024
            verdict["state_bytes"] = {
                "table_footprint_bytes": ext_probe["footprint_bytes"],
                "rss_import_plus_table_bytes": added,
                "rss_steady_growth_bytes": (
                    rss["after_1000_max_batches"] - rss["after_500_max_batches"]
                )
                * 1024,
                "table_ok": ext_probe["footprint_bytes"] <= BUDGET_STATE_BYTES,
                "rss_ok": added <= BUDGET_STATE_BYTES,
            }
    return verdict


# -- the run ---------------------------------------------------------------------------


def environment(ext: ModuleType) -> Json:
    model = None
    try:
        for line in Path("/proc/cpuinfo").read_text().splitlines():
            if line.startswith("Model"):
                model = line.split(":", 1)[1].strip()
    except OSError:
        pass
    return {
        "host": platform.node(),
        "machine": platform.machine(),
        "platform": platform.platform(),
        "board": model,
        "cpus": os.cpu_count(),
        "python": sys.version.split()[0],
        "python_implementation": platform.python_implementation(),
        "extension": {
            name: getattr(ext, name)
            for name in (
                "BACKEND",
                "CRATE_VERSION",
                "BUILD_PROFILE",
                "CONTRACT_VERSION",
                "KERNEL_VERSION",
                "STATE_MODEL_VERSION",
                "MAX_BATCH",
            )
        },
        "extension_path": getattr(ext, "__file__", None),
    }


def run(
    *,
    session: Path | None,
    iterations: int,
    random_n: int,
    throughput_n: int,
    startup_runs: int,
    batch_sizes: Sequence[int],
    build_info: Json | None,
) -> Json:
    ext = load_extension()
    _, _, _, named_table, constants = real_twin()
    frame = ground_truth_frame()
    fixture = observed_tuples(frame)
    result: Json = {"environment": environment(ext), "build": build_info}
    parity: Json = {
        "vectors": vector_parity(ext),
        "real_ground_truth_fixture": {
            "twin_kernel_calls": len(kernel_calls(frame)),
            **tuple_parity(ext, fixture, named_table, constants),
        },
        "random_over_real_table": tuple_parity(
            ext, random_tuples(random_n), named_table, constants
        ),
    }
    stream = fixture
    if session is not None:
        stored = observed_tuples(session)
        parity["real_session"] = {
            "session": session.name,
            "twin_kernel_calls": len(kernel_calls(session)),
            **tuple_parity(ext, stored, named_table, constants),
        }
        stream = stored or fixture
    parity["identical"] = all(
        part["mismatches"] == 0 and part["python_sha256"] == part["rust_sha256"]
        for part in [*parity["vectors"].values(), *(v for k, v in parity.items() if k != "vectors")]
        if isinstance(part, dict)
    )
    result["parity"] = parity
    result["latency_us"] = latency(
        ext, stream, named_table, constants, iterations=iterations, batch_sizes=batch_sizes
    )
    result["throughput_tuples_per_s"] = throughput(
        ext, stream, named_table, constants, throughput_n
    )
    memory = startup_and_memory(startup_runs) if startup_runs else None
    result["startup_and_memory"] = memory
    result["budget_e04_3"] = budget(result["latency_us"], memory)
    return result


def _render(result: Json) -> str:
    lines = [f"environment: {json.dumps(result['environment'])}"]
    parity = result["parity"]
    lines.append(f"parity identical: {parity['identical']}")
    for name, part in [*parity["vectors"].items(), *parity.items()]:
        if isinstance(part, dict) and "mismatches" in part:
            n = part.get("vectors", part.get("tuples"))
            same = part["python_sha256"] == part["rust_sha256"]
            lines.append(
                f"  {name}: n={n} mismatches={part['mismatches']} digests_equal={same} "
                f"sha256={part['rust_sha256'][:16]}"
            )
    lines.append("latency (µs)       n      p50      p95      p99      max")
    lat = result["latency_us"]
    for group in ("single", "parts"):
        for name, s in lat[group].items():
            lines.append(_stat_line(name, s))
    for size, row in lat["batch"].items():
        for name in (
            "rust_word_batch",
            "rust_convert",
            "rust_compute",
            "rust_return",
            "python_target_command_loop",
        ):
            lines.append(_stat_line(f"batch{size}.{name}", row[name]))
        lines.append(f"  batch{size}.sum_of_parts_p50 {row['sum_of_parts_p50']:.2f}")
    lines.append(f"throughput: {json.dumps(result['throughput_tuples_per_s'])}")
    if result["startup_and_memory"]:
        lines.append(f"startup/memory: {json.dumps(result['startup_and_memory'])}")
    lines.append(f"budget: {json.dumps(result['budget_e04_3'])}")
    return "\n".join(lines)


def _stat_line(name: str, s: Json) -> str:
    return (
        f"  {name:<34} {s['n']:>7} {s['p50']:>8.2f} {s['p95']:>8.2f} "
        f"{s['p99']:>8.2f} {s['max']:>8.2f}"
    )


def parse_args(argv: Sequence[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--out", type=Path, help="write the JSON result here")
    parser.add_argument(
        "--session", type=Path, help="also run every tuple the stored twin evaluates here"
    )
    parser.add_argument("--iterations", type=int, default=20_000)
    parser.add_argument("--random", type=int, default=20_000, dest="random_n")
    parser.add_argument("--throughput", type=int, default=100_000, dest="throughput_n")
    parser.add_argument("--startup-runs", type=int, default=15)
    parser.add_argument("--batch-sizes", default="1,16,256,4096")
    parser.add_argument("--build-info", type=Path, help="JSON from the build (versions, flags)")
    parser.add_argument("--probe", choices=("none", "ext", "python"), help=argparse.SUPPRESS)
    return parser.parse_args(argv)


def main(argv: Sequence[str] | None = None) -> int:
    args = parse_args(argv)
    if args.probe:
        print(json.dumps(probe(args.probe)))
        return 0
    result = run(
        session=args.session,
        iterations=args.iterations,
        random_n=args.random_n,
        throughput_n=args.throughput_n,
        startup_runs=args.startup_runs,
        batch_sizes=[int(s) for s in args.batch_sizes.split(",")],
        build_info=json.loads(args.build_info.read_text()) if args.build_info else None,
    )
    if args.out:
        args.out.write_text(json.dumps(result, indent=2, sort_keys=True))
    print(_render(result))
    return 0 if result["parity"]["identical"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
