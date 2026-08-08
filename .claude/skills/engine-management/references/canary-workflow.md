# Doing this work inside CANary

Everything in the rest of this skill is tool-agnostic — it works on an UpRev
K-line log, an HP Tuners CSV, or a notebook. This file is the CANary-specific
mapping: which tool answers which question, and which gates the repo already
enforces so you don't re-derive them.

CANary is **read-only on the bus**. It can log, request, decode, and analyze.
It cannot flash a calibration — the tuning software (UpRev on the dev vehicle)
is the only writer. So "tuning" here means *measure → decide the change →
someone else applies it → measure again*.

## Naming trap: two unrelated things are called "tuning"

| Tool | What it tunes |
|---|---|
| `get_tuning` / `set_tuning` / `estimate_tuning` / `set_tuning_mode` / `revert_tuning` | **Anomaly-detector thresholds.** Nothing to do with the engine. `estimate_tuning` = "how many false positives would this detector change cost me." |
| `arm_tuning_capture` / `get_delivery_error` / `compare_tuning_sessions` / `get_current_capture` | **Engine calibration work** — capturing and analyzing fuelling/timing across a flash. |

Reading `set_tuning` as an engine change is the mistake to avoid. If a request
is about AFR, spark, or a flash, it is the second row.

## Getting data

| Question | Tool |
|---|---|
| What is the engine doing right now, and is it even running? | `get_status` → read `vehicle_state` first (`engine_running` / `ignition_only` / `engine_off` / `replay` / `no_data`). A conclusion drawn without this is unsafe. |
| Which PIDs does this ECU actually answer? | `get_obd_coverage` |
| Aligned channels over a past drive | `get_session_timeseries` (use `source:CHANNEL`, e.g. `obd:RPM`, `obd:ENGINE_LOAD`). 1 Hz for a whole drive; higher only to resolve a transient. |
| Arbitrary SQL over the Parquet ring buffer | `query_telemetry` (DuckDB) |
| Trims/AFR/timing binned by rpm × load without writing SQL | `get_binned_analysis` — value channel, x channel, optional y channel, agg ∈ mean/median/p95/count |
| A dense, fast, verified capture for a specific tuning question | `arm_tuning_capture` (profile + rate/budget), then `tag_current_capture` / `record_vehicle_event` for the physical actions |
| Trip-level scalars (distance, economy, 0–60, ¼ mile) | `get_trip_summary` |
| Stored DTCs and MIL state | `get_dtc` |
| Test an analysis without the truck | `start_replay` / `replay_window` over a stored session |

Capture sessions are written under `_`-prefixed directories so they never
contaminate the training corpus. That also means: **"which session is live"
(`src.control.sessions.latest_session`) and "which sessions may train"
(`preprocessing.loader.discover_sessions`) are different questions** — don't
use one to answer the other, and never order sessions by directory name
(prefixes sort before dates; use `session_started_at`).

## The analysis functions — and why you should not hand-roll around them

These already refuse the failure modes described in `log-analysis.md`. Reaching
past them into a bespoke pandas script is how this project produced its
retracted findings.

| Job | Call |
|---|---|
| Does channel A track channel B? | `src.analysis.compare.compare_channels` — pairs same-instant samples itself, refuses too-few-pairs and barely-moving signals, reports `scale_verdict` (stable / unstable / unresolvable) rather than r² alone. It takes **specs and a session**, not a frame, so a pre-aggregated input is unrepresentable. |
| Commanded vs measured fuelling, banded by rpm | `src.analysis.tuning.delivery_error` — enforces the closed-loop, DFCO, band-composition and forbidden-axis gates below, and reports how many samples each gate dropped |
| Same analysis across a flash boundary | `src.analysis.tuning.compare_sessions` |
| Is a measured scale factor stable, or drifting? | `src.analysis.tuning.scale_stability` |
| Bin anything by anything | `src.analysis.binned_analysis.compute_binned_analysis` (resamples onto a common grid via the same forward-fill the training pipeline uses) |
| Fit an expected-behavior surface over rpm × load | `src.analysis.surface_2d.fit_grid_binned_mean` — per-cell mean/std/**count**; empty cells stay empty rather than fabricating a value |
| Derived physics channels (fuel rate, air mass/cycle, boost, bank balances) | `src.analysis.derived` — `compute_derived_channels`, `compute_anomaly_signals` |
| Measured/target AFR reconstruction and the error surface | `src.analysis.afr` — `append_afr_channels`, `afr_error_surface` |

## Gates these functions enforce (each one cost a retraction)

- **Forbidden grid axes.** `ENGINE_LOAD` / `CALCULATED_LOAD` / `ABSOLUTE_LOAD`
  are rejected as comparison axes when fuelling moved: calculated load is
  derived from the fuel/airflow math, so conditioning on it while fuelling
  changes partly conditions on the thing that changed. Grid on **rpm × MAF**
  (real airflow) or rpm × the actual table axis instead.
- **Closed-loop dilution.** In closed loop the ECU corrects back to target
  whatever you changed, so pooling closed-loop samples drives any measured
  effect toward zero *by construction*.
- **`FUEL_STATUS == 4` is "open loop", not "power enrichment".** It also covers
  decel fuel cut, which dominates a normal drive. Filtering on it alone gives
  you mostly overrun.
- **Band-composition bias.** Two datasets pooled across rpm bands with
  different coverage compare coverage, not fuelling.
- **Empty must say which empty.** `delivery_error` refuses with the cause when
  the inputs were never captured (e.g. a Mode 22 profile disables Mode 01, so
  commanded AFR is absent by construction) instead of reporting a zero result
  that reads as "the gates rejected everything."

## Rate and staleness

Channels are polled in rotation, so two channels essentially never share a
timestamp and every binned analysis is an ASOF join underneath. That join is
where a clean-looking analysis dies:

- Bound joins by **sample age**, explicitly, per channel — 20 s for
  temperatures, a few seconds for load/loop-status/purge. A single uniform
  bound is either too loose for the fast channels or too tight for the slow
  ones (one analysis kept 8 of 594 samples uniformly bounded, 110 with
  per-channel bounds).
- **Carry and report the joined sample's age.** A stale value looks exactly
  like a fresh one, and one stale reading re-paired four times looks like four
  measurements.
- **Report the drop count.** If freshness bounding discards two-thirds of the
  data, that is the headline, not a footnote.
- Prefer a synchronously-sampled source when one exists — a single-frame log
  (UpRev K-line) needs no join at all, which is why such results survive when
  join-based ones don't.

## Handoffs

- **Which byte / PID / DID carries this quantity, or is this binding real?** →
  `ecu-reverse-engineering` skill. This skill assumes channels are already
  correctly identified and in engineering units.
- **Planning a multi-factor test (several changes, few runs, confounding)?** →
  `designed-experiments` skill. Come back here to interpret the results.
