# E09 — one engine twin: the running engine and the ROM editor

Written 2026-10-15 from a review of the simulator code (E07, E08, W09, E05)
against the operator's statement of what the engine twin is for, the same
day:

1. **Run an engine digital twin.** Load the ROM that is flashed to the ECM,
   run it, and output data that is compared against the real running engine.
   The twin's internal machinery interacts with the ROM the way the
   disassembled program does. This validates the model and lets the system
   trend variations in the engine against what is expected. It is part of the
   second brain, which also processes sensors the ECM does not have (the
   supercharger TMAP, fuel pressure).
2. **Basically the same code, different use case.** A user edits the ROM
   (a fuel map, fuel targets, a timing table, the MAF curve, …) and runs a
   simulation to get an idea of the effect.

Follow [E00](E00-second-brain-README.md). [E05](E05-ecu-application-twin.md)
owns decoded ECU logic, [E07](E07-engine-plant-simulator.md) the plant and
the paired-scenario rules, [E08](E08-rom-edit-simulation.md) the edit
workbench and [W09](W09-rom-editor.md) its page. This document adds the items
those plans do not contain. It implements nothing by itself.

## What the review found

The contract (E07.1) already declares the two use cases as modes: ECU replay
and plant replay are use case 1, the conditional ROM comparison is use case
2. The code serves them unevenly.

| Capability | State on 2026-10-15 |
|---|---|
| Load the flashed ROM | Done. `.OsirisROM` and plain images decode, round-trip and are checksum- and program-checked (W08, E07.2). The factory ROM is a different program inside an encrypted backup and cannot load. |
| Decoded ECU chain | Steady fuel chain only: K, Fuel Comp, BFS, Load Point Scaler, Target AFR, injector pulse; integer-exact and verified on drives (E05.2, E07.3). Ignition stops at the trim lookup. Airflow pipeline, enrichment timing, closed-loop trim, fuel-cut triggers, idle, cold: traced or absent. About 1,350 of 57,000 calibration bytes are consumed. |
| Twin against the engine | Four separate offline evaluators (`target_phi`, `air_charge_twin`, `injector_pulse`, `fuel_plant_eval`), each with its own pairing. The target-φ twin has **0 of 174** eligible samples on its fixture because inputs and outputs were never polled together. Nothing runs all outputs over one session, and nothing runs on the Pi but the target-φ stream (E04.1). E03.3's change detector watches one residual, STFT bank balance. |
| Plant | λ = air / (s · √ΔP · Te), one fitted scalar. Fails its own bounds in-sample and held out (|err| p90 11.6 % against 8 %; λ ≡ 1 does 2.3 %); residual ≈ −0.92 × STFT. Three versions differ only in how MAP is guessed. Manifold ODE converged, unvalidatable on the NA engine (operator decision 2026-10-08). Torque, spark, knock, throttle, rpm dynamics, thermal, rail: declared, unbuilt. |
| ROM editor | Complete for Fuel Comp, Fuel Targets and K: edit spec, paired Monte Carlo, per-cell report, CLI, page, MCP. Known answers are code identities (BFS against the Fuel Comp multiplier), not engine truth. Five post-flash drives exist; none is scored against the prediction of record. |

**Lever coverage for use case 2.** An edit is only useful when it reaches a
computed output.

| Lever | Expressible as an edit | Reaches an output | Blocked by |
|---|---|---|---|
| Fuel Comp, Fuel Targets, K | yes | BFS, φ, pulse, λ | plant unqualified (E07.3); closed loop unsupported (E09.10) |
| Timing trims A/B | CLI only | none | base timing table unmapped; chain untraced (E09.7); no torque plant |
| MAF curve `0xE27E` | no | none | not in `rom_map.yaml`; airflow pipeline unmodelled (E09.8) |
| Injector scaler `0x9254`, dead time, short pulse | no | none | constants not mapped as tables (E09.9) |
| Load Point Scaler | yes | X | not editable on the page |
| Cam target map `0xA579`, rev limiter words, cold tables | no | none | unmapped; no consumer |

## Decisions made here (reviewer, 2026-10-15; the operator may revisit)

- **One executor, two front ends.** The operator's "basically the same code"
  is not true today: the editor runs through `paired_scenarios`, the twin
  through four evaluators. E09.2 builds one runner over the application
  model's node graph; replay (recorded inputs) and comparison (stated
  inputs) are callers of it. Existing evaluators become its kernels, and
  their fixtures pin it. Nothing else here starts before E09.2's runner
  exists, except the capture profile (E09.1) and the listing work (E09.7,
  E09.8, E09.9, E09.10), which feed it.
- **Co-sampling is the twin's first blocker, not model coverage.** Every
  evaluator loses most of its samples to `not_co_observed` or a pairing
  tolerance. E09.1 derives the capture from the manifest so this cannot
  recur per output.
- **ECU-side predictions are a product before the plant qualifies.** ΔBFS,
  Δφ, Δpulse and Δtiming depend on decoded integer logic, not on the plant.
  They are scored against the truck (E09.12) and shown for timing edits
  (E09.7) without a torque claim. The plant's claims stay gated as E07
  gates them.
- **The STFT-shaped plant residual is a signal in use case 1.** The ECM's
  trim is correcting for what the plant does not carry. "Expected STFT"
  against observed STFT is published as a trend channel (E09.4), and the
  plant's structural fix (E09.11) is pursued separately.
- **Hardware changes are new plants, never refits.** The supercharged engine
  gets its own hardware configuration, baseline and uncertainty file
  (E09.5). TMAP and rail are twin inputs there; on the NA engine rail is an
  input where logged and MAP is never measured (operator, 2026-10-08).

## File ownership (for parallel implementation)

| Item | New files | May edit |
|---|---|---|
| E09.1 | — | `data/capture_profiles.yaml`, `tests/test_capture_profiles*.py`, `src/analysis/ecu_application.py` (one derivation function) |
| E09.2 | `src/analysis/twin_runner.py`, `tests/test_twin_runner.py` | `paired_scenarios.py` (to call the runner), the four evaluators (kernel seams only) |
| E09.3 | `src/decode/airflow_pipeline.py`, tests | `ecu_application_model.yaml`, `src/decode/air_charge.py` |
| E09.4 | `src/analysis/twin_residuals.py`, tests | `data/<vehicle>/change_detection.yaml`, `noise_floor.yaml`, one dashboard panel |
| E09.5 | `data/<vehicle>/fuel_plant_sc1.yaml`, `sim_uncertainty_sc1.yaml` | `fuel_plant.py` (measured-MAP input), `engine_sim_contract.yaml` (hardware configuration) |
| E09.7 | — | `rom_map.yaml`, `ecu_application_model.yaml`, `ecu_state_models.py`, `paired_scenarios.py` (one output), `rom_editor.py` (editable tables) |
| E09.8 | — | `rom_map.yaml`, `src/decode/air_charge.py`, `paired_scenarios.py`, `sim_design.py` |
| E09.9 | — | `rom_map.yaml`, `injector_pulse.py`, `rom_editor.py` |
| E09.10 | — | `ecu_application_model.yaml`, `paired_steady.py`, `paired_scenarios.py` |
| E09.11 | `data/<vehicle>/fuel_plant_mv4.yaml`, `tests/test_fuel_plant_mv4.py` | `fuel_plant.py`, `fuel_plant_eval.py` |
| E09.12 | `tests/test_plant_validation_ecu_outputs.py` | `plant_validation.py`, `plant_interventions.yaml` |

Dispatch tiers (CLAUDE.md): E09.1 Standard; E09.2, E09.3, E09.5, E09.7,
E09.8, E09.9, E09.10, E09.11, E09.12 Deep; E09.4 Heavy; E09.6 Heavy on the
Pi; E09.13 Light. Items marked **listing** need the firmware session
(`~/ecu-re`, see memory `feedback_ghidra_via_owner_session`); items marked
**truck** need a drive; **Pi** needs the Pi.

## Checklist — use case 1: the twin against the running engine

- [ ] **E09.1 — A `twin` capture profile, derived from the manifest.**
  One capture profile that polls every observed input and every observable
  output of each evaluable application-model output in the same poll round
  or the same `0x23` window, so the twin can be scored at all. Derive the
  channel list from `ecu_application_model.yaml` (each node's `reads`,
  `writes`, `observed_as`) with one function in `ecu_application.py`; never
  a hand list. Include: the E05.2(b), E05.3(a)(b) and E05.4(b)(c) RAM
  windows; DIDs 0x1206/0x1208/0x1209/0x1110/0x120F; PIDs 0x0C/0x10/0x11/
  0x24/0x42, STFT/LTFT; the fill byte in the pulse DID's round (operator,
  2026-10-13: do not widen the 0.1 s tolerance). Rate-tier by `did_rate_tiering`
  evidence; the bus budget is measured (memory `project_bus_budget_overload`:
  watch storage `queue_depth`).
  **Accept:** a test asserts every observation of every evaluable output is
  in the profile, and fails when a node gains an observation the profile
  lacks. One drive on the profile (**truck**) gives > 0 eligible samples in
  E02's accounting, > 0 `load_input` pairs in E07.4's case, and a fill-byte
  read within 0.1 s of every pulse read.

- [ ] **E09.2 — One replay runner over the node graph.**
  `src/analysis/twin_runner.py` (layer 5): given a resolved ROM and kit and
  a session (or a bounded window of reads), walk the application model in
  `schedule` order, execute each `implemented` node's kernel, take each
  `observed` node from its channel, and emit per output per sample:
  predicted, observed, residual, and a named state (`eligible`, or the
  reason it is not). One pairing discipline for every output
  (`match_ordered` on the acquisition clock), one count of unevaluable
  reads per reason. Plant stages run after the ECU outputs on the same
  samples. The paired comparison (`run_paired`) becomes a caller that
  feeds stated inputs instead of channels.
  **Accept:** the existing fixtures pin it, byte for byte: E02's 174-sample
  October 4 accounting and its two enrichment commands; E05.2's 458 steady
  BFS reads (median 0.9991); E07.3's pulse-chain ratios and the
  `na-stock-2026-10-05.1` identification numbers; E07.6/E08's `cmp_head`
  equality for every paired case. A mutation in a shared kernel fails both
  front ends. Clean-process test: no ECU-facing import. Split for dispatch
  as 2a (runner + ECU outputs), 2b (plant stages), 2c (`run_paired` on it).

- [ ] **E09.3 — Stateful ECU replay at the tick.**
  The airflow pipeline (`w20A8` low-pass, `L20AC`, `w1056`, the `w273E`
  purge hold), the fill filter, transient and blend as time-stepped nodes,
  and the measured tick. E05.2(b) names the capture (**truck**; E09.1's
  profile carries its windows) and E05.3(b) measures the tick. Until the
  tick is measured, transient outputs are in ticks and `unevaluable` by
  name; a steady-state result from E09.2 must not change.
  **Accept:** on the E05.2(b) capture, BFS per sample through throttle
  steps and holds within a tolerance declared before scoring; `airflow_qa`
  and the fill nodes move from `not_modelled` to `implemented` with
  `reference_validation: vehicle`; E05.2's steady result is unchanged.

- [ ] **E09.4 — Twin residuals as trend channels.**
  Publish the runner's residuals as derived channels with the conditions
  required by `noise_floor.yaml`: BFS, pulse, φ, final timing (after
  E09.7), λ, and expected-versus-observed STFT (the trim the plant says
  the ECM should need). Add them to `change_detection.yaml`'s residuals so
  E03.3's detector watches them beside STFT bank balance; one dashboard
  panel shows each residual with its noise floor and unevaluable share.
  **Accept:** each channel's floor is measured on two same-tune drives
  with `conditions` stated; E03.3's independent-drive report lists the new
  residuals with coverage and event counts; a residual with no floor
  cannot be published (schema).

- [ ] **E09.5 — External sensors as twin inputs; the boosted plant.**
  The contract already declares `sensor:MANIFOLD_PRESSURE` and the plant
  takes measured rail where logged. Add: measured MAP as a plant input
  that replaces the manifold model when present (ΔP = rail − (MAP − baro));
  a MAF-versus-speed-density committee (`committees.yaml`) that runs only
  with a measured MAP; and the supercharged engine as a new hardware
  configuration (`supercharged-<date>`) with its own plant file, baseline,
  uncertainty file and identification split. The boosted correction the
  ECM cannot make (supercharger doc, "Boosted fuelling without a pressure
  input") is a plant effect here, named until measured.
  **Accept:** the NA plant is byte-identical; a profile without the
  hardware configuration refuses it by name; identification on the boosted
  engine follows E07.3's rules (declared split, frozen baseline, bounds
  before scoring). Needs the build (**hardware**).

- [ ] **E09.6 — Pi residency.**
  Run E09.2 as a bounded job after collection stops (E04.4) over the latest
  session, persisting residual records under `/data/canary/twin/`
  (declared in `tests/test_state_dir.py`); and a bounded live observer for
  the steady ECU outputs on E04.1's `TwinStream` pattern, with its budget
  measured per E04.3 before enablement. Shares E07.7's integration.
  **Accept:** E04.3's budget evidence for the observer; a post-drive job
  report visible on the phone; collection never waits (E04.1). **Pi.**

## Checklist — use case 2: edit a ROM and see the effect

- [ ] **E09.7 — ECU-only timing predictions.** **listing**, **truck**
  Map the base ignition timing table(s) the chain reads (`base_timing_sum`
  `0x04CB9E`, `base_timing_select` `0x04CF0C`) with their axes into
  `rom_map.yaml`; implement E05.4's seven nodes through
  `timing_final_byte`; add a simulator output `ignition_timing_deg`
  (ECU side: the final byte, no torque or knock claim; E07.5's
  `require_claim` refusals stay). Make timing trims A/B editable on the
  page (W09.7 names it later; this is the item). The HI DET latch is a
  stated input until E05.4(b) observes it.
  **Accept:** E05.4(c)'s capture locates the earliest disagreement and the
  chain reproduces the final byte within a declared tolerance; a trim edit
  of −3 counts shows Δtiming per cell on the page and in the CLI, with
  torque `unsupported: spark_response_unmeasured`; k106's ten trim edits
  are the known answer.

- [ ] **E09.8 — The MAF curve, mapped and modelled.** **listing**
  Add `0xE27E` (the MAF transfer curve the airflow pipeline reads) to
  `rom_map.yaml` with its axis and decode, from the listing. Route it
  through the airflow word: a design point's airflow is inverted through
  the base curve to the sensor reading and re-applied through the
  candidate curve, so a curve edit moves Qa, BFS, X, φ, every load axis and
  the plant's air. Record that the fitted plant scale inherits the base
  curve's transfer error (contract `parameter_fits`).
  **Accept:** a flat +5 % curve edit moves BFS +5 % at every point (as
  E08.7's K check); a one-cell edit moves only the points whose reading
  falls in that cell; negative control exact; `inspect` names the curve.

- [ ] **E09.9 — Injector constants as editable tables.** **listing**
  Map the injector-flow scaler `0x9254`, dead time `0x8C41`/`0x84F2`, the
  short-pulse constants `0x9120`–`0x9126` and the rail reference `0x8A89`
  as `rom_map.yaml` tables; `injector_pulse` reads them from the image
  (today's constants in `fuel_plant.yaml` become the map's). Make them
  editable on the page. State the distinction in the result: an edit to
  `0x9254` changes the ECU's pulse at the same injectors; new injectors are
  a hardware configuration (E09.5), never an edit.
  **Accept:** a scaler edit of ×1.1 moves every pulse ×1.1 and no load
  axis; the dead-time formula is pinned by the 46 fuel-cut reads
  (E07.3); the short-pulse decode lifts `short_pulse_undecoded`.

- [ ] **E09.10 — Closed-loop trim authority, decoded.** **listing**
  Decode the STFT enable (`uncovered: closed_loop_enable`), the clamp,
  step and integrator (`0x05E368`), and the A/F feedback target's relation
  to φ (`0xFFFF46B2`). Then closed-loop outputs in `paired_scenarios` are
  supported when the trim the candidate needs is within authority, and
  `closed_loop_setpoint_moved` resolves. Most of the Fuel Comp map and most
  of a drive are closed loop; this item unblocks both use cases.
  **Accept:** `closed_loop_trim_authority_undecoded` is lifted with a
  vehicle-qualified override (E05.3's schema); a Fuel Comp edit in a cruise
  cell reports Δtrim as `changed`; the clamp is reproduced on the ±25 %
  reads E03.3 already rejects.

- [ ] **E09.11 — fuel-mv-4: the plant's structure, not its VE.** **truck**
  E07.3's residual tracks STFT; purge and a fast MAF transfer error are
  the named candidates. Declare a new `plant_version` with: purge flow as
  an input (DID 0x1110, polled since 2026-10-15), MAF transfer error as a
  named parameter whose bracket is the unassigned 9 % between the fitted
  scale and the datasheet, PID 0x42 logged with the pulse so Ts is not
  taken from fuel-cut reads, the short-pulse correction (E09.9), and the
  steady rule fixed so a pulse read's partners come from before the next
  change (E07.3's pairing leak). Bounds and split declared before scoring;
  identification on never-scored drives logged on the `twin` profile
  (E09.1).
  **Accept:** E07.3's bounds on held-out drives, and the residual's
  correlation with STFT below a threshold declared with the bounds. If it
  fails, the next version is not VE either; the evidence says where.

- [ ] **E09.12 — Score the prediction of record on the plant-free outputs.**
  Five drives on ZS44A-20261004 exist (E08.7 item 5) and none is scored.
  Extend `plant_validation` with effect tests for ΔBFS and Δφ per matched
  pair (E07.4's bands and floor), read from `run-200draws/traces.parquet`,
  against DID 0x1208 and `mem:ECM_TARGET_EQUIV_RATIO` on the post-flash
  drives; sealed verdict as E07.4. Reuse B08.6's pairing. The plant is not
  needed, so `plant_not_qualified` does not gate this verdict. Desk: the
  data is on the laptop.
  **Accept:** a `pass`/`fail`/`insufficient_evidence` verdict per output
  with n per cell, recorded here with the drives named; the first
  engine-truth check of the simulator. Only the ram-study logs the fill
  byte pre-flash, so the floor comes from within-recording pairs.

- [ ] **E09.13 — Declare the minimum torque model and its order.** Light
  Doc work: in E07.5 stage 4, state the minimum model (friction and
  pumping from H07.5 coast-downs; a declared MBT-relative spark-efficiency
  shape and a λ-efficiency curve, both identified on H07 pulls, never
  fitted to the fuel-derived torque estimate); and the order: H07.10
  (mass), H07.5 (road load), H07.12 (NA baseline) before any torque claim.
  Add the three H07 items to the queue ahead of E07.5 stage 4.
  **Accept:** E07.5 stage 4 names the model and cites this order; the
  queue lists the H07 items.

- [ ] **E09.14 — `inspect` names the lever and its blockers.**
  `tools/rom_sim.py inspect` and the page's preview classify an edit by
  lever (the table above) and list, per output, what blocks a number:
  unmapped, unmodelled node, plant unqualified, closed loop. Read from the
  manifest and `compare_roms`, not a hand table.
  **Accept:** a timing-trim edit says "no torque plant"; a MAF-curve edit
  before E09.8 says "not mapped"; a Fuel Comp cruise edit before E09.10
  says "closed loop: trim authority undecoded".

## Where Rust would pay, and where it would not

Asked by the operator 2026-10-15. Measured facts: E06.2's `ecu_twin_core`
already holds the lookup family, `target_command`, `x_from_bfs`, the fill
chain and the UpRev state path, bit-exact on 5,402 vectors; E06.3 measured
the binding at p99 0.61 µs per tuple against Python's 13 µs, and 0.58 ms
against 83 ms for a 4,096-tuple batch, with every Rust case inside E04.3's
Pi budget and the Python batch outside it. E08.3 measured the Monte Carlo at
5–21 s for 1,000 draws × 2,000 points on the laptop, the cost in the
ECU-side cache, already vectorised in numpy; W09.6 measured the 32 × 32
grid at 2,000 draws at 51 s and 1.2 GiB RSS on the laptop (Pi estimate
150–310 s). The manifold stage runs fixed-step rk4 at 1.28 ms.

| Part | Rust benefit | Why |
|---|---|---|
| Integer ECU kernels on a per-tick path (E09.3 stateful replay, E09.6 live observer) | **Yes, and it is the only place it is needed soon.** | A 30-minute drive is ~1.8 M ticks; at ~13 µs per node call in Python, ten nodes is ~4 min per pass per drive and the live observer misses E04.3's budget on any batch. Rust is ~20× per call and ~140× batched, measured. Explicit wrapping/saturating arithmetic is also a correctness property here: the kernels mirror firmware integer semantics. |
| Continuous plant stages under a coupled rollout (E07.5/E07.6, later) | **Yes, when the rollout exists.** | rk4 at 1.28 ms over a drive is ~1.4 M steps per stage; with eight stages and Monte Carlo draws, scalar Python is hours. Parity there is tolerance-based (contract), so an f64 port needs no bit-exactness. Not before a dynamic plant is built and validated. |
| The steady Monte Carlo (E08.3) | **No, by E08.3's own rule.** | Under 60 s on the laptop, numpy-vectorised. The Pi risk is memory (per point × draw arrays, 1.2 GiB at the cap), which Rust does not fix; streaming aggregation into the cell table does. Revisit only when a measured Pi run misses its budget. |
| Steady `fuel_plant.predict` | No. | 2 s of an 18.7 s run before the cache was vectorised; now vectorised over points. |
| Sample building and pairing (`build_samples`, `match_ordered`, drive designs at ~11 s) | Not yet. | pandas-bound; vectorise or move to DuckDB first. |
| ROM codec, `compare_roms`, cells, report, provenance and refusal machinery | No. | 31 ms fixed per run; the rest is I/O and schema work. |

**Rule for E09.2.** Keep every kernel behind E06.1's computation contract
so a node's backend can be swapped and the Python reference stays the
verifier. Port a node to Rust only after it is `implemented` and vehicle-
validated and sits on a per-tick path; the decode is still moving (E09.3,
E09.7, E09.8, E09.10 all add nodes), and a port before that doubles the
churn. Adoption itself stays E06.4's decision after E06.3's under-load arms
on the Pi.

## Not in scope (named so they are not implied)

- A full SH7058 instruction emulator (E07 says the same).
- Knock prediction: `knock_unmodelled` stays until a knock model is
  identified on the RAM knock state (`docs/ecu-firmware-analysis.md`,
  "Knock monitoring").
- Optimising a ROM against the simulator (E07.6, refused).
- Writing anything to the ECU (AGENTS.md).
