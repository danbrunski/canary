# Live-pipeline feature-stage microbenchmark — workstation baseline (2026-09-06)

Roadmap task C05 (`docs/roadmap/tasks/archive/05-pipeline-benchmark.md`), items
C05.10 / C05.12. Raw reports:
[smoke JSON](2026-09-06-benchmark-live-pipeline-smoke.json),
[steady JSON](2026-09-06-benchmark-live-pipeline-steady.json).

## What this measures — and what it does not

**This is the feature-only microbenchmark, run on the workstation.** Per
tick it times (a) the `LiveFeatureSource.observe()` calls that apply one
tick's synthetic OBD + CAN records (`observe` stage), (b) the
`LiveFeatureSource.tick()` call that emits a wide row and runs the shared
wide→features builder over the ring buffer (`tick` stage), and (c) both
together (`total`). **Detector scoring, the collectors, the network and
disk paths are untimed** — the full-pipeline harness is task C10.

**The Pi result is pending** (C05.11). Nothing here is extrapolated to the
Pi; the old "multiply by ~3×" projection has been removed from the script.
`1/p95(total)` is a microbenchmark estimate for this stage alone, not a
sustainable daemon collection rate.

## Why the previous number was wrong

The pre-repair script observed records at Unix seconds 0, 1, 2, … and
ticked at a 2026 datetime, so the freshness rule in
`src/preprocessing/live.py` rejected every observation. A 30-tick run
reported ~27,600 Hz with `buffer_size == 0` and zero feature rows; warmup
and measurement also restarted the timestamp sequence at 0. Reproduced
before the repair on this workstation (`buffer rows at end: 0`,
`sustained @ p95: 27669.4 Hz`). The repaired script marks such a run
`valid: false` with named reasons and exits with code 2;
`tests/test_benchmark_live_pipeline.py::test_all_stale_run_is_rejected_with_named_reasons`
and `::test_cli_exits_nonzero_and_marks_report_invalid_for_a_stale_run`
pin that behaviour.

## Evidence that the feature transformation actually executed

Elapsed duration alone is not sufficient evidence. Both runs below report
`completed_ticks == measured_ticks`, `empty_ticks == 0`, feature rows equal
to the number of measured ticks (60 and
600, each row 312 feature columns wide),
a warm ring buffer after warmup, operating modes classified by the real
segmenter (no fallback ticks), and zero quality-gate rejects. Median tick
latency is ~40 ms, consistent with the earlier independent
consistent-clock smoke check (~11 ms with a smaller workload) and
inconsistent with the 0.03 ms of the empty-buffer defect.

## Host identity

| field | value |
|---|---|
| hostname | CorsairDesktop |
| platform | Linux-6.6.87.2-microsoft-standard-WSL2-x86_64-with-glibc2.39 |
| python | 3.12.3 |
| git revision | `2726beb` (working tree with the C05 changes applied, pre-commit) |
| config | `canary.example.yaml` (sha256 `cb841942ef5e0986…`) |
| bindings | `data/bindings.yaml` (sha256 `1707dce3bc95cb06…`) |
| models dir | `models` (informational only — no model is scored here) |
| behavior models | none loaded |

Model manifests present under the models dir (not exercised by this
benchmark, recorded for comparability):

| manifest | type | version |
|---|---|---|
| `models/nissan-xterra-2008/isolation_forest/model.manifest.json` | isolation_forest | `isolation_forest_2026-07-27T045036.161167Z` |
| `models/nissan-xterra-2008/lstm_autoencoder/model.manifest.json` | lstm_autoencoder | `lstm_autoencoder_2026-07-27T045351.620955Z` |

## Workload

Synthetic only (a recorded-session replay through `observe()` is not part
of this script; that belongs to the C10 harness). Deterministic drive
cycle seeded with `--seed 0`: idle → acceleration → highway cruise →
deceleration → idle on a 240 s cycle, coolant warming from 20 °C toward
95 °C with a 120 s time constant. All values sit inside
`quality_gate.is_physically_valid` bounds. CAN frames are exactly
`--can-frames-per-tick` per tick, round-robin over the 17 bound IDs in
`data/bindings.yaml`, spread through each tick period (per-ID cadence
carried across ticks). One scenario clock drives both observations and
ticks; each tick advances by `1 / preprocessing.resample_hz`.

## Results

### `smoke` profile

Command:

```
.venv/bin/python scripts/benchmark_live_pipeline.py --profile smoke --seed 0 \
    --report data/analyses/2026-09-06-benchmark-live-pipeline-smoke.json
```

| field | value |
|---|---|
| valid | `True` (reasons: `[]`) |
| resample rate / tick period | 1.0 Hz / 1.0 s |
| warmup / measured ticks | 30 / 60 |
| scenario span | 2026-05-13T12:00:00+00:00 → 2026-05-13T12:01:29+00:00 (89 s, monotonic across both phases) |
| OBD channels | 16 (RPM, SPEED, THROTTLE_POS, COOLANT_TEMP, ENGINE_LOAD, INTAKE_TEMP, INTAKE_PRESSURE, MAF, TIMING_ADVANCE, SHORT_FUEL_TRIM_1, LONG_FUEL_TRIM_1, SHORT_FUEL_TRIM_2, LONG_FUEL_TRIM_2, O2_B1S1, O2_B2S1, BAROMETRIC_PRESSURE) |
| CAN frames per tick / bound IDs | 20 / 17 |
| observations per tick / measured total | 36 / 2160 |
| buffer size after warmup / at end | 30 / 90 |
| completed / empty ticks | 60 / 0 |
| **feature rows produced** / feature columns | **60** / 312 |
| operating modes (measured ticks) | city: 57, warmup: 3 |
| segmenter fallback ticks / quality-gate rejects | 0 / {} |
| 1/p95(total) | 23.3 Hz — microbenchmark estimate, see caveat below |

| stage | n | median ms | p95 ms | p99 ms | max ms |
|---|---:|---:|---:|---:|---:|
| observe | 60 | 0.108 | 0.146 | 0.252 | 0.395 |
| tick | 60 | 40.089 | 42.766 | 54.317 | 54.743 |
| total | 60 | 40.203 | 42.880 | 54.425 | 54.858 |

### `steady` profile

Command:

```
.venv/bin/python scripts/benchmark_live_pipeline.py --profile steady --seed 0 \
    --report data/analyses/2026-09-06-benchmark-live-pipeline-steady.json
```

| field | value |
|---|---|
| valid | `True` (reasons: `[]`) |
| resample rate / tick period | 1.0 Hz / 1.0 s |
| warmup / measured ticks | 120 / 600 |
| scenario span | 2026-05-13T12:00:00+00:00 → 2026-05-13T12:11:59+00:00 (719 s, monotonic across both phases) |
| OBD channels | 16 (RPM, SPEED, THROTTLE_POS, COOLANT_TEMP, ENGINE_LOAD, INTAKE_TEMP, INTAKE_PRESSURE, MAF, TIMING_ADVANCE, SHORT_FUEL_TRIM_1, LONG_FUEL_TRIM_1, SHORT_FUEL_TRIM_2, LONG_FUEL_TRIM_2, O2_B1S1, O2_B2S1, BAROMETRIC_PRESSURE) |
| CAN frames per tick / bound IDs | 20 / 17 |
| observations per tick / measured total | 36 / 21600 |
| buffer size after warmup / at end | 120 / 181 |
| completed / empty ticks | 600 / 0 |
| **feature rows produced** / feature columns | **600** / 312 |
| operating modes (measured ticks) | city: 236, highway: 184, idle_transient: 180 |
| segmenter fallback ticks / quality-gate rejects | 0 / {} |
| 1/p95(total) | 23.1 Hz — microbenchmark estimate, see caveat below |

| stage | n | median ms | p95 ms | p99 ms | max ms |
|---|---:|---:|---:|---:|---:|
| observe | 600 | 0.100 | 0.132 | 0.206 | 0.407 |
| tick | 600 | 39.247 | 43.224 | 54.474 | 61.153 |
| total | 600 | 39.353 | 43.339 | 54.563 | 61.302 |

## Reading the numbers

- `observe` is negligible (~0.1 ms for 36 records); the cost is the
  per-tick `featurize_wide_frame` over the ring buffer (`tick`).
- The steady run's buffer reaches its cap (181 rows =
  `idle_transient_lookback_seconds × hz + hz`) during measurement, so its
  p95/p99 reflect the full-buffer steady state; the smoke run never fills
  the buffer (90 rows at end) and is a warm-up-shaped lower bound.
- At 1 Hz the feature stage consumes ~4 % of the tick period on this
  workstation. What that means on the Pi is a C05.11 measurement, not an
  inference from this table.
