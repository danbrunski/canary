# Doing this work inside CANary

Everything in the rest of this skill is tool-agnostic — it works on an UpRev
K-line log, an HP Tuners CSV, or a notebook. This file is the CANary-specific
mapping: which tool answers which question, and which gates the repo already
enforces so you don't re-derive them. Vehicle facts (which PID is λ and which is
φ, which DID is what) live in the vehicle skill and the profile's data files, not
here.

CANary logs, requests, decodes and analyzes, and its ad-hoc request path will
send any diagnostic service. It does **not** flash a calibration: the operator
does that with the tuning software (UpRev on the dev vehicle). CANary can view a
ROM and prepare a hash-bound *offline* Fuel Targets edit
(`docs/offline-rom-targets.md`), but writing it to the ECU is the operator's act.
Any non-volatile ECU write needs the two conditions in `AGENTS.md`: explicit
per-operation permission, and `vehicle_state == ignition_only`. So "tuning" here
means *measure → decide the change → the operator flashes it → measure again*.

## Naming trap: two unrelated things are called "tuning"

| Tool | What it tunes |
|---|---|
| `get_tuning` / `set_tuning` / `estimate_tuning` / `set_tuning_mode` / `revert_tuning` | **Anomaly-detector thresholds.** Nothing to do with the engine. `estimate_tuning` = "how many false positives would this detector change cost me." |
| `arm_tuning_capture` / `get_delivery_error` / `compare_tuning_sessions` / `get_current_capture` | **Engine calibration work**: capturing and analyzing fuelling and timing across a flash. |

Reading `set_tuning` as an engine change is the mistake to avoid. If a request
is about AFR, spark, or a flash, it is the second row.

Tuning captures are kept out of the anomaly detectors on purpose: they never
train, evaluate or score IF/sequence models. Don't "fix" a detector by feeding it
one.

## Getting data

| Question | Tool |
|---|---|
| What is the engine doing right now, and is it even running? | `get_status`, then read `vehicle_state` first (`engine_running` / `ignition_only` / `engine_off` / `replay` / `no_data`). A conclusion drawn without this is unsafe. |
| Which PIDs does this ECU actually answer? | `get_obd_coverage` |
| Aligned channels over a past drive | `get_session_timeseries` (use `source:CHANNEL`, e.g. `obd:RPM`). 1 Hz for a whole drive; higher only to resolve a transient. |
| Arbitrary SQL over the Parquet ring buffer | `query_telemetry` (DuckDB) |
| A quick look at trims/AFR/timing binned by rpm × airflow | `get_binned_analysis`: value channel, x channel, optional y channel, agg ∈ mean/median/p95/count. It forward-fills onto a display grid, so it is fine for exploring and wrong for a claim (see below). |
| A dense, fast capture for a specific tuning question | `list_capture_profiles`, then `arm_tuning_capture` (profile + rate/budget); `tag_current_capture` / `record_vehicle_event` for the physical actions. `get_capture_quality` says whether it ran at the rate it was armed for. |
| "The drive's over, what did we get?" | `get_session_digest`. Its tuning section counts usable WOT, achieved rate, delivery error and cam tracking. |
| Trip-level scalars (distance, economy, 0–60, ¼ mile) | `get_trip_summary` |
| Stored DTCs and MIL state | `get_dtc` |
| Another tool (UpRev, a scan tool) needs the ECU to itself | `set_passive_mode` with a TTL, which gates every CANary transmit |
| Test an analysis without the truck | `start_replay` / `replay_window` over a stored session |

Capture sessions are written under `_`-prefixed directories so they never
contaminate the training corpus. That also means **"which session is live"
(`src.storage.sessions.latest_session`) and "which sessions may train"
(`preprocessing.loader.discover_sessions`) are different questions.** Don't use
one to answer the other, and never order sessions by directory name: prefixes
sort before dates, so use `session_started_at`.

## The analysis functions, and why not to hand-roll around them

These already refuse the failure modes described in `log-analysis-method.md`.
Reaching past them into a bespoke pandas script is how this project produced its
retracted findings.

| Job | Call |
|---|---|
| Does channel A track channel B? | `src.analysis.compare.compare_channels`. It pairs real same-instant observations itself (`align_pairs`: nothing forward-filled or interpolated), refuses too-few-pairs and barely-moving signals, and reports `scale_verdict` (stable / unstable / unresolvable), not just r². It takes **specs and a session**, not a frame, so a pre-aggregated input is unrepresentable. Then look at the per-sample ratio anyway: the verdict can mislead in either direction. |
| Is a measured scale factor stable or drifting? | `src.analysis.compare.scale_stability` |
| Is this difference bigger than run-to-run noise? | `src.analysis.noise_floor.significance`. The floor was measured from two same-tune captures, and it **requires a `conditions` argument**: a floor measured at WOT is 16× tighter than one across all conditions, so quoting it against idle turns jitter into a "finding". It refuses when no floor exists. |
| Commanded vs measured fuelling, banded by rpm | `src.analysis.tuning.delivery_error`. It enforces the closed-loop, DFCO, band-composition and forbidden-axis gates below, and reports how many samples each gate dropped. |
| Commanded vs measured λ as a declared relationship | `src.analysis.lambda_delivery`: which commanded source is valid in which regime, the λ/φ convention of each source, and the transport delay, declared per vehicle in `<profile>/lambda_delivery.yaml`. |
| Same analysis across a flash boundary | `src.analysis.tuning.compare_sessions` |
| Which calibration was in the ECU for this session? | `src.analysis.tune_revisions.resolve_session_revision`, over `<profile>/tune_revisions.yaml`. Don't reconstruct it from prose. |
| Which ROM cell is the ECM actually reading? | `src.analysis.rom_trace`: the ECM's own table coordinates (from RAM trace channels) beside the inferred rpm × load lookup. A disagreement between the two means a wrong axis decode or a wrong load scale. |
| Bin anything by anything | `src.analysis.binned_analysis.compute_binned_analysis`. It uses the training pipeline's forward-fill resample, so it describes; it does not pair. |
| Fit an expected-behavior surface over rpm × load | `src.analysis.surface_2d.fit_grid_binned_mean`: per-cell mean/std/**count**. Empty cells stay empty instead of getting a made-up value. |
| Derived physics channels (fuel rate, air mass/cycle, bank balances) | `src.analysis.derived`: `compute_derived_channels`, `compute_anomaly_signals` |
| Measured/target AFR reconstruction and the error surface | `src.analysis.afr`: `append_afr_channels`, `afr_error_surface` |

Before trusting a new analysis chain, run it where the answer is already known.
`tests/test_analysis_ground_truth.py` runs the real chain over a committed slice
of a real drive, with expectations in `tests/fixtures/ground_truth/expectations.yaml`.
Recording a newly established fact is an edit to that YAML.

## Gates these functions enforce (each one cost a retraction)

- **Forbidden grid axes.** `ENGINE_LOAD` / `CALCULATED_LOAD` / `ABSOLUTE_LOAD`
  are rejected as comparison axes when fuelling moved: calculated load comes out
  of the fuel/airflow math, so conditioning on it while fuelling changes partly
  conditions on the thing that changed. Grid on **rpm × MAF** (real airflow) or
  on rpm × the actual table axis instead.
- **Closed-loop dilution.** In closed loop the ECU corrects back to target
  whatever you changed, so pooling closed-loop samples drives any measured
  effect toward zero *by construction*.
- **`FUEL_STATUS == 4` is "open loop", not "power enrichment".** It also covers
  decel fuel cut, which dominates a normal drive. Filtering on it alone gives
  you mostly overrun.
- **Band-composition bias.** Two datasets pooled across rpm bands with
  different coverage compare coverage, not fuelling.
- **Target moves vs delivery moves.** An error-versus-target metric silently
  credits a target edit to delivery. When a flash changed both the target table
  and a fuel/airflow table, decompose them: measure delivery against the ECU's
  own commanded value, and read the target change from the map diff, never from
  binned data.
- **Empty must say which empty.** `delivery_error` refuses with the cause when
  the inputs were never captured (e.g. a Mode 22 profile disables Mode 01, so
  commanded AFR is absent by construction). It does not report a zero result
  that reads as "the gates rejected everything."

## Rate and staleness in CANary data

The general rules are in `log-analysis-method.md`. What is specific here:

- Mode 01 PIDs, Mode 22 DIDs, RAM windows and broadcast frames arrive on
  different cadences (≈1 Hz, ≈15 Hz, per window, ≈100 Hz), and a DID read has
  been measured 0.1–0.35 s away from the Mode 01 batch it was compared with. On a fast
  transient that skew alone reads as a scale error: one "MAF is 5 % low at high
  flow" finding was exactly this, and it vanished once gated to |dMAF/dt| ≤ 5 %/s.
- Two channels in the same Mode 01 batch share a request time, so a lag scan
  between them resolves only to the batch period. Split them into separate
  batches when lag is the question.
- A single-frame source (the UpRev K-line log, a RAM window read) needs no join.
  That is why results from one survive when join-based ones don't.

## Handoffs

- **Which byte / PID / DID carries this quantity, or is this binding real?** →
  `ecu-reverse-engineering` skill. This skill assumes channels are already
  correctly identified and in engineering units.
- **Planning a multi-factor test (several changes, few runs, confounding)?** →
  `designed-experiments` skill. Come back here to interpret the results.
- **Facts about the specific truck** (which PID is λ, what a DID means, what a
  flash resets) → its vehicle skill, e.g. `vehicle-nissan-xterra-2008`.
