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
| E09.14a | `src/analysis/rom_levers.py`, `tests/test_rom_levers.py` | `tools/rom_sim.py` (inspect) |
| E09.14b | — | `rom_editor.py` (`preview_edits`), the ROM Editor page and its tests |

**Session split (2026-10-15).** The W09 session (ROM Editor page,
`rom_editor.py`, MCP ROM tools) takes E09.14b only, after E08.11 and its
W09.6 real-image update are committed. The simulator session (`sim_*`,
`paired_scenarios.py`, `tools/rom_sim.py`) takes E09.12 (started
2026-10-15, Deep), E09.2a/b/c after E08.11 lands, and E09.14a. The review
session takes E09.13. Unclaimed: E09.1, E09.3–E09.6, E09.11 (truck or Pi
for acceptance) and the listing items E09.7–E09.10. Constraint from W09:
if E09.2a changes the simulate result shape, the W09 session is told the
shape before it lands, since the page and MCP tools render it.

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

- [~] **E09.12 — Score the prediction of record on the plant-free outputs.**
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
  *Partial 2026-10-16 (laptop). The machinery is done; the engine-truth check
  could not happen. The verdict is `insufficient_evidence` for both outputs,
  0 pairs in every cell, under the original rules and under amendment 2. The
  post-flash drives were recorded on the live profile, which polls DID 0x1208
  once a minute, never next to an rpm read, and never polls the RAM φ word.
  The null control showed raw BFS cannot resolve this change between drives.
  Amendment 2 (BFS·rpm/MAF) passed that null, but it also passed a no-change
  drive against the real prediction. Amendment 3 counts only pairs where the
  prediction is larger than the floor, and under it no design point of this
  prediction can be resolved at all (below). Verdict under amendment 3:
  `insufficient_evidence`.*
  - **Code.**
    - `src/analysis/plant_validation.py` gains `score_ecu_outputs`, which
      runs in the order of evidence:
      1. `verify_prediction_artifact` checks the artifact's files;
      2. `candidate_recordings` lists every recording on the candidate
         revision, and an undeclared one is refused;
      3. `prediction_points` and `pre_observed` build the pre side;
      4. `fill_samples` and `ecu_output_reads` build the post side;
      5. `evaluate_ecu_outputs` and `ecu_effect_test` score it.
    - `match_pairs` takes `load=` columns, so two N02 fill intervals match
      only when **both** ends are within `load_input_abs`. The default stays
      E07.4's single `load_input`.
    - `plant_interventions.yaml` holds the rules in `ecu_outputs`. They are
      frozen by `ecu_rules_sha256` (`15eaa48b…`, 2026-10-16T06:09:20Z), and
      an edit refuses with `rules_changed`. They were declared before any
      post-flash BFS or φ value was read. E07.4's `rules_sha256` is unchanged
      (`f522ff53…`), and its matching, floor and acceptance are reused as
      written.
  - **Artifact first.** `verify_prediction_artifact` runs before any
    post-change read. It refuses `prediction_artifact_changed` or
    `prediction_artifact_missing` by name. Hashes checked, all equal to
    E08.7's:
    - `run/result.json` `0e129471…`, `run/report.html` `cd99ebcb…`,
      `run/traces.parquet` `92d35caa…`;
    - `run-200draws/result.json` `71267e16…`, `report.html` `5173dadd…`,
      `traces.parquet` `f05b5c2d…`;
    - `result_digest` `956d7bbe…`, and the design's canonical `322e4cab…` as
      named in `result.json` and in `design.json`.

    E08.7 did not record the design's own files, so they were hashed before
    scoring: `design.json` `bccf7f73…`, `design.parquet` `119bf48a…`. Nothing
    under `~/canary-data/sim/predictions/` was written.
  - **Rules.**
    - **Pre side.** The 298 design points (`fuel-mv-1`, committed code) on
      `_capture-tuning-2026-09-23T142926`. At each point, the output's read
      is paired within the plant's `ecu_pair_tolerance_s` (0.1 s).
    - **Predicted change.** Per point, the 200 draws' relative Δ, as p05 /
      p50 / p95 (inverted CDF, the cell table's method).
    - **Post side.** A read is built the way a design point is:
      - anchored on the output's own read;
      - rpm and MAF within `pair_tolerance_s` (0.3 s);
      - baro, IAT and coolant within `slow_max_age_s`;
      - regime from the bracketed commanded φ;
      - steady by the plant's rpm and MAF windows;
      - warm;
      - N02's closed-loop fill interval on the candidate image within 0.3 s
        (`fill_image_mismatch` refuses).
    - **Pairing.** One to one, nearest first, in the same regime, using E07.4's
      matching (rpm 5 %, both fill ends within 4 counts, baro 2 kPa, IAT
      10 °C).
    - **Floor.** Pre-vs-pre design points within the one recording, at least
      30 s apart.
    - **Acceptance.** E07.4's: 20 pairs, 20 floor pairs, direction 0.7,
      within 0.8. Where no pair predicts a signed change (`unsigned_prediction:
      within_only`), within-uncertainty decides alone, named
      `direction_not_applicable`.
    - **Seal.** The verdict is an `EcuOutputVerdict`, sealed with E07.4's
      HMAC key over a body that names its kind. It carries no plant baseline,
      so it cannot stand in for a plant verdict in `rank_as_improvement`.
  - **Learned state.** `trim_independence` walks the application model from
    each output's writer, through every node it depends on: 16 nodes for
    `base_fuel_schedule_word` and 36 for `target_equiv_ratio`. Neither touches
    a trim word (`q_0x6400`: `0xFFFF26B4`/`26B6`), so the flash's LTFT reset
    does not move BFS or φ. LTFT has no RAM word in the model, so the check
    covers it only through the override text: trims enter the bank pulse
    factor, not φ.
  - **Drives.** All five post-flash recordings, and only those:
    `candidate_recordings` finds no other on ZS44A-20261004.
    - **`base_fuel_schedule` (DID 0x1208): `insufficient_evidence`, 0 pairs,
      0 cells.**
      - There are 33 post reads: 2026-10-08T172943 21, 2026-10-08T182550 9,
        2026-10-10T153117 2, 2026-10-10T165941 1, and
        `_capture-fuel-pulsation-2026-10-08T210012` 0.
      - All 33 are `not_co_observed`. The nearest rpm/MAF read is 0.417 s
        away at best (medians 0.43–1.4 s), against 0.3 s. The live profile
        polls Mode 22 and Mode 01 apart.
      - Only 2 of the 33 have a fill interval within 0.3 s, and every post
        read's regime is `unknown`, because φ is not logged. So looser
        pairing could not reach 20 pairs either.
      - Floor: 4.11 % (p90 |Δ|) on 103 pairs. Pre exclusions: 4 points with
        no BFS within 0.1 s, and 1 point not predicted.
    - **`target_phi` (`mem:ECM_TARGET_EQUIV_RATIO`): `insufficient_evidence`,
      0 pairs, `never_captured`.** No post-flash recording polls the word.
      Floor: 0.0 % on 57 pairs. 133 design points have no φ read within
      0.1 s.
  - **Known answer: a null control across drives.** Two pre-flash drives on
    the base image are scored as "post" against the design with a predicted
    Δ of exactly 0.
    - **φ passes** on both (closed loop, Δ 0.0 %).
    - **BFS fails** on both:
      - `_capture-tuning-2026-09-22T235224`: 114 pairs, median +8.56 %
        (p10 +4.5, p90 +18.8), within 0.035;
      - `_capture-tuning-2026-09-22T170459`: 93 pairs, median −0.21 % (p10
        −6.4, p90 +2.8), within 0.73.

    At these matching tolerances the same ROM moves BFS by more than the
    within-recording floor between drives. The fill intervals are about 46
    counts wide, and BFS ∝ Qa/rpm is not pinned by rpm and the fill byte.
    The prediction's ΔBFS is at most −2.4 to +4.6 %. So a BFS `fail` on
    co-sampled post-flash drives would not be evidence against the
    prediction. E07.4 already named this limit of a within-recording floor;
    this is its measurement.
  - **Amendment 2: BFS·rpm/MAF (decided 2026-10-16, operator-delegated,
    option a).** A versioned amendment, `ecu_amendments[0]` in
    `plant_interventions.yaml`. It is not an edit: the original block, its
    sha `15eaa48b…` and its verdict above stand as recorded.
    - **Schema.** `EcuAmendment` names the sha it `amends` and has its own
      `frozen_rules`: `c458d1a6…`, frozen 2026-10-16T06:33:31Z. The case
      refuses `amendment_chain_broken`, and `rules_changed` on any edit.
      Verdicts carry `rules_version` and `rules_sha256` in the seal.
    - **Reason (in the file).** The null control above: raw BFS on
      `_capture-tuning-2026-09-22T235224` sits +8.56 % at matched conditions
      on the same ROM. No post-flash pair existed under either rule when the
      amendment was made: 0 of 33 BFS reads co-observed, and no φ read. So it
      cannot have been chosen to fit post-flash data.
    - **Checked on the decoded chain before freezing.** In the running
      branch, Tp = ⌊⌊K·Qa·m/256⌋ / (d·N)⌋ × 0x23CB >> 16, with Qa =
      0xFFFF2100 and N = max(0x40, 0xFFFF1204). 0xFFFF1204 is 1.5625 rpm per
      count, so the floor binds only below 100 rpm. Then 0xFFFF105C =
      min(0xFFFF, (Tp·FC) >> 7), and steady BFS = min(0xFFFF, 4·105C,
      ceiling << 8). So BFS·rpm/MAF ∝ K·FC/128, except where a named state
      excludes the read:
      - `*_at_ceiling` (BFS ≥ 65,280; both images' ceiling is flat 255), and
        `draws_at_ceiling` for a draw clamped on base or candidate;
      - `transient` (the fill filter, the transient term 0xFFFF105A, the
        airflow low-pass and the 0xFFFF233C speed branch need the steady
        windows);
      - `cold` (the after-start hot-wire term);
      - `*_maf_unpaired`.

      Carried by the floor rather than excluded:
      - N from PID 0x0C is the 12.5 rpm word, which bounds 0xFFFF1204 to 8
        counts (up to 1.7 % at 750 rpm);
      - PID 0x10 MAF stands in for Qa (curve 0xE27E, low-pass, purge hold).

      The MAF-fault and cranking branches are assumed absent, as in E05.2.
      The predicted change is unchanged: base and candidate share a point's
      rpm and airflow. φ keeps `raw`.
    - **Null control, re-run after freezing: passes on both drives.**
      - `_capture-tuning-2026-09-22T235224`: 114 pairs, median −0.33 % (p10
        −1.97, p90 +1.58), within 0.851.
      - `_capture-tuning-2026-09-22T170459`: 93 pairs, median −0.23 % (p10
        −1.93, p90 +1.74), within 0.860.

      The floor is 2.44 % on 103 pairs, against raw's 4.11 %. Excluded reads
      by state:
      - `at_ceiling` 0, `maf_unpaired` 0, `draws_at_ceiling` 0 (pre and post);
      - on the null drives' post side, before the amendment's own
        exclusions: 235224 has eligible 603, transient 1,513, fill unpaired
        72, not_co_observed 2; 170459 has eligible 247, transient 936, fill
        unpaired 8.

      The margin over 0.8 is thin (0.85 / 0.86). A correct idle prediction
      (−1.6 %) also sits inside this floor, so direction carries most of the
      information there.
    - **Post-flash re-score under amendment 2: `insufficient_evidence` for
      both outputs**, 0 pairs. BFS: 33 reads, all `not_co_observed`, floor
      2.44 % on 103 pairs. φ: `never_captured`, floor 0.0 % on 57 pairs.
      Pairing, not the quantity, is the limit.
    - **The v2 evidence is idle-only, and it was overstated.** 96 of the 103
      floor pairs are idle (800-rpm cells), and so are 109/114 and 62/93 of
      the null pairs. Off idle, within-uncertainty is 0.40 (n = 5) and 0.74
      (n = 31). These counts were re-measured here. The verifier's
      pairing-robustness spread, not re-run here: within-uncertainty under a
      random one-to-one pairing is at least 0.791 / 0.768; nearest design
      point gives 0.880 and 0.717 (a fail); all pairs give 0.904 / 0.928. So
      the floor and the v2 null pass say nothing about BFS off idle.
    - **v2 passes a no-change drive against the real prediction**
      (re-measured). `_capture-tuning-2026-09-22T235224` scores direction
      0.762 on 21 signed pairs and within 0.904: `pass`. Cell 800×48 alone
      supplies 73 of its 114 pairs, at points the prediction calls ≈ 0.
      `_capture-tuning-2026-09-22T170459` fails (direction 0.60 on 30 signed
      pairs, within 0.87). Version 2's block and verdict stand as recorded.
  - **Amendment 3: only resolvable pairs count.** `ecu_amendments[1]`, sha
    `7a9f4279…` (frozen 2026-10-16T06:56:08Z), amends `c458d1a6…`.
    - **Rule.** A design point is paired only when its predicted |Δ| (p50)
      is above the output's condition-matched floor; the rest are
      `pre_unresolvable`. Direction and within-uncertainty are computed over
      those pairs only. The minimum is E07.4's `min_pairs`, 20: E07.4
      declares no per-cell or per-regime minimum. Fewer such pairs, where
      all predicted points would have given 20, is `insufficient_evidence`
      named `prediction_unresolvable`. That is distinct from `never_captured`,
      `filtered` / `not_co_observed`, and `unmatched`.
    - **unknown regime.** E07.4's effect tests score named regimes only
      (`enrichment`, `closed_loop`); `match_pairs` itself compares labels, so
      under v1/v2 `unknown` paired with `unknown`. Under v3 such reads are
      `pre_regime_unknown` / `post_regime_unknown`.
    - **Reason (in the file).** The v2 pass above. It was declared before
      any co-observed post-flash pair existed: 0 pairs for both outputs under
      v1 and under v2.
    - **Control (a), the real prediction against the two pre-flash no-change
      drives: `insufficient_evidence`, `prediction_unresolvable` on both.**
      0 resolvable pairs, against 106 and 90 pairs that every predicted point
      would have given. The floor is 2.44 % on 100 pairs. All 274 predicted
      design points that are in a named regime and have a BFS read sit inside
      the floor (`pre_unresolvable` 274, `pre_regime_unknown` 20). Only 3
      points have |p50| > 2.44 %: indices 91 (+3.82 %), 92 (+4.62 %) and 93
      (+3.85 %), at 5,162.5–5,362.5 rpm in cell 5200×144. All three are
      excluded by regime (`pre_regime_unknown`), not by the floor.
    - **Controls (b) and (c), synthetic.** A known +2.31 % change with a
      floor of about 0.6 % passes (60 resolvable pairs), and the reversed
      prediction fails.
    - **Post-flash re-score under v3: `insufficient_evidence` for both
      outputs**, 0 pairs. BFS: 33 reads, all `not_co_observed`; floor 2.44 %
      on 100 pairs. φ: `never_captured`; floor 0.0 % on 54 pairs. Five
      points predict a φ change: 19 (−2.90 %) and 92 have no φ read, and 90,
      91 and 93 are unknown regime.
    - **Consequence.** Against this prediction and this design, ΔBFS and Δφ
      cannot reach a verdict under v3, however well a post-flash drive is
      co-sampled. In a named regime no design point is resolvable: the
      predicted changes (idle −1.6 %, light load up to +2.3 %) are inside the
      2.44 % idle floor. The only three beyond it (91–93) are unknown regime.
      Even if they counted, they would cap the pairs at 3, below the minimum
      of 20.
  - **Post list: append-only, outside the frozen hashes.** The five in
    `ecu_outputs.post` stay inside `15eaa48b…`, so removing or reordering
    them refuses with `rules_changed`. Later recordings go in the case's
    `ecu_post_appended`: each entry is `{recording, appended_on,
    chain_sha256}`. The chain value is `post_chain_sha256(previous,
    recording, appended_on)`, starting from `15eaa48b…`. Nothing has been
    appended yet, so the root and every frozen sha are unchanged.
    - **What the chain catches.** It has no key. It catches a reordered,
      edited (recording or `appended_on`), inserted or removed entry that does
      not recompute the links after it: `post_list_rewritten`. Also refused:
      a duplicate (`post_recording_duplicate`) and a name with no start time
      (`recording_time_unknown`).
    - **Against a rewrite that recomputes the tail,** the record is the disk
      scan, which refuses any candidate recording left undeclared
      (`post_recording_undeclared`, which also catches a dropped last entry),
      plus git history.
    - **End to end, a pre-flash recording appended to the list refuses as
      `post_recording_missing`:** it is never on the candidate revision, so
      `candidate_recordings` does not list it. `post_recording_before_flash`
      is a backstop for a candidate list built by hand.
    - **Named scope.** `score_ecu_outputs(..., recordings=[...])` scores only
      the named recordings. The seal carries `recordings`, `scope` (`all` or
      `named`) and `candidates_not_scored`, so a subset verdict never reads
      as a full one: editing the scope fails `verify()`. The slow tests score
      the five by name, so a newly pulled session does not change them.
  - **Tests.** `tests/test_plant_validation_ecu_outputs.py` has 18 fast tests
    and 5 slow private ones, which skip by name (`prediction_artifact_not_here`,
    `drives_not_here`, `image_not_found`).
    - Fast: a known +2.31 % ratio passes (60 pairs, direction 1.0); the
      reversed prediction fails; 19 pairs, unmatched intervals and no floor
      each give `insufficient_evidence`, each kind of empty named.
    - Fast: unsigned prediction; one-to-one pairing with n per cell; the
      seal; the refusals `undeclared_recording`, `synthetic_as_observed`,
      `synthetic_mismatch` and `prediction_not_verified`.
    - Fast: the artifact refusals; the frozen rules; trim independence,
      including a mutated model that refuses `output_reads_trim`; post-read
      states on the committed slice (`wrong_side`).
    - Fast (amendment): an 8 % airflow difference fails raw and passes
      air-normalised; the ceiling and MAF exclusions are counted by name;
      `ceiling_unstated`; the chain, `rules_changed` and
      `amendment_chain_broken` refusals; each verdict names its rules version
      and sha.
    - Fast (amendment 3 and post list):
      - controls (b) and (c); `prediction_unresolvable`, with
        `pairs_before_resolvable`; mixed points, where only the resolvable
        ones are paired; `*_regime_unknown`;
      - appending a recording leaves every frozen sha unchanged (no
        `rules_changed`), and an appended recording is scored and named in
        the seal, with its scope sealed (a relabelled scope fails
        `verify()`); `post_list_rewritten` (including an edited
        `appended_on`), `post_recording_duplicate` and
        `recording_time_unknown`; the frozen five cannot be removed or
        reordered (`rules_changed`);
      - `post_scope`: full scope, named scope with the unscored ones listed,
        `post_recording_undeclared` and `post_recording_before_flash`;
      - in `score_ecu_outputs` the artifact is verified before any recording
        is touched (monkeypatched readers raise if called).
    - Slow: the real verdict under v1, v2 and v3; the null control (raw
      fails, air-normalised passes); and control (a) (v2 passes 235224, v3
      `prediction_unresolvable` on both).
    - Mutation checks, each caught: the observed ratio inverted, matching on
      the low fill end only, and direction 0 for an unsigned prediction.
  - **Gate.** `pytest -n 4 tests/test_plant_validation.py
    tests/test_plant_validation_ecu_outputs.py tests/test_layering.py
    tests/test_fuel_comp_restore.py tests/test_paired_scenarios.py`: 78
    passed. With `-m "slow or not slow"` on the new file and
    `test_fuel_comp_restore_private_rom.py`: 24 passed. `ruff check .`,
    `ruff format --check .` and `mypy src` are clean. Each slow test spends
    about 32 s building the real score.
  - **Remaining:**
    1. **Open dependency: E09.1's `twin` profile, which does not exist yet.**
       It must poll BFS with rpm, MAF and the fill byte, and log the RAM φ
       word. A post-flash drive on it is truck work.
    2. **Desk work once such a drive is pulled.** Append it to
       `ecu_post_appended` (chain value from `post_chain_sha256`), then run
       `score_ecu_outputs(..., amendment=3)` over every declared recording.
    3. **Operator decision.** Under v3 this prediction has no resolvable
       design point in a named regime (the Consequence above). The only
       three beyond the 2.44 % floor are points 91–93 (5,162.5–5,362.5 rpm,
       cell 5200×144, +3.82 to +4.62 %), and all three are unknown regime.
       So a drive from item 1 cannot give a BFS or φ verdict against this
       design.
       - **Owed (not built here): score the second prediction.** Commit
         `b870e80f` records a second 10-04 prediction in E08.7,
         `fuel-comp-restore-2026-10-04-wot`: 917 points, 3 ΔBFS-resolvable,
         27 with nonzero Δφ. Scoring it needs a new frozen block or case,
         because the first artifact is frozen inside `15eaa48b…`.
       - **Its null control needs other drives.** Its design is built on both
         of this item's null drives (`_capture-tuning-2026-09-22T235224` and
         `_capture-tuning-2026-09-22T170459`), so they cannot be an
         independent null control for it.
       - A narrower floor would need co-sampled pre-change data on the base
         image, which can no longer be recorded. ~~Comparing BFS across
         drives~~ was decided 2026-10-16 (amendments 2 and 3).

- [x] **E09.13 — Declare the minimum torque model and its order.** Light
  Doc work: in E07.5 stage 4, state the minimum model (friction and
  pumping from H07.5 coast-downs; a declared MBT-relative spark-efficiency
  shape and a λ-efficiency curve, both identified on H07 pulls, never
  fitted to the fuel-derived torque estimate); and the order: H07.10
  (mass), H07.5 (road load), H07.12 (NA baseline) before any torque claim.
  Add the three H07 items to the queue ahead of E07.5 stage 4.
  **Accept:** E07.5 stage 4 names the model and cites this order; the
  queue lists the H07 items.
  *Done 2026-10-15:* E07.5 remaining item 4 now states the minimum model
  (indicated torque from air per event × η(λ) × η(spark), friction and
  pumping from coast-downs, an MBT-relative quadratic η(spark) identified
  on H07 pulls and never on `derived:ESTIMATED_TORQUE_NM`) and the order
  H07.10 → H07.5 → H07.12 → identification → spark sweeps.
  `OVERNIGHT-QUEUE.md` Q27 records the order with the truck items marked
  blocked overnight. No code.

- [~] **E09.14 — `inspect` and the page name the lever and its blockers.**
  One function, `src/analysis/rom_levers.py` (layer 5), classifies an edit
  by lever (the table above) and lists, per output, what blocks a number:
  unmapped, unmodelled node, plant unqualified, closed loop. It reads the
  manifest and `compare_roms`, never a hand table. Two callers: 14a
  `tools/rom_sim.py inspect` (simulator session), 14b
  `rom_editor.preview_edits` and the ROM Editor page beside the preview
  (W09 session). Neither caller classifies on its own.
  **Accept:** a timing-trim edit says "no torque plant"; a MAF-curve edit
  before E09.8 says "not mapped"; a Fuel Comp cruise edit before E09.10
  says "closed loop: trim authority undecoded"; the CLI and the page show
  the same strings for the same edit (one test calls both).
  *14a done 2026-10-16 (simulator session); 14b open (W09 session).*
  `src/analysis/rom_levers.py` holds `classify_edit(report, *, rom_map,
  model, contract, qualification, regime=None, cells=None) ->
  LeverClassification` (pydantic, `extra="forbid"`, stable `to_dict()`,
  plain-string `messages`). Levers are the rom map's table `kind` (an axis
  follows the table that names it); blockers come from `compare_roms`'s
  per-output reasons, the contract (`no_torque_plant`: no quantity carries the
  output and the torque subsystem is `unbuilt`), `plant_qualification`, and
  the manifest's closed-loop overrides not yet `vehicle`-qualified
  (`TRIM_AUTHORITY_UNDECODED`). `tools/rom_sim.py inspect` adds a `levers` key
  (additive) and `--regime`. Evidence: `tests/test_rom_levers.py` (18 tests:
  the three accept strings; scratch-profile mutations that lift each blocker
  (node made `implemented`, override qualified, torque subsystem built,
  contract quantity joined, plant qualified); runner names pinned to
  `paired_scenarios`; clean-process no-ECU-import) and
  `tests/test_rom_sim_cli.py` (3 added: CLI messages equal
  `classify_edit(...).messages` for the same Fuel Comp, timing-trim and
  0xE27E edits).
  *Verifier fix 2026-10-16:* the two ECU outputs the run computes (BFS, the
  Target AFR word) no longer show reasons the run absorbs. One rule:
  `paired_steady.ecu_output_gate` (called by `paired_scenarios.twin_gate`, the
  Monte Carlo fast path through it, and `rom_levers` through `twin_gate` and
  the run's `ReasonScope`), so a Fuel Comp edit says "a number can be given"
  for both, while an edit to a filter node itself keeps its uncovered
  blocker; ignition outputs keep every reason. The closed-loop blocker is
  `paired_steady.closed_loop_trim_pending`, called by the run, the fast path and
  the classifier: it holds until **all three** E05.3 closed-loop overrides are
  `vehicle`-qualified (E09.10). `classify_edit` refuses a report not made under
  the run's contract mode. `tests/test_rom_levers.py` cross-checks each ECU
  output's blockers against `run_paired` on the same edit (Fuel Comp, an axis,
  a fill curve, a filter's own table) and drives the run and the classifier from
  one qualified-override model. `run_paired` is byte-identical over 13 edits x
  3 points.
  *Re-verification 2026-10-16 (verifier, recorded here):* `run_paired`
  identical to HEAD on 46 results; the mv-2 Monte Carlo identical (the fast
  path's `fast_path_diverged` self-check green); a real Fuel Comp edit agrees
  with the run; the 51-flip cross-check agrees; the closed-loop blocker
  behaves with 0/1/2/3 of the three overrides qualified (lifts only at 3).
  *Wording fixes the same day:* "a number can be given" now carries "(for
  points that state only the fill byte)", with structured
  `assumes_conditioned` (`paired_steady.FILL_BYTE`), `refused_if_conditioned`
  (per output, from the contract mode's conditionable quantities through the
  run's `ReasonScope`: `paired_steady.conditionable_node`/`conditionable_quantities`,
  which `run_paired` now also uses for `conditioning_not_allowed`) and
  `design_dependent` on each such output in `to_dict()`. A mapped table no node
  reads says "unknown bytes changed: a mapped table no node reads (<table>)"
  and keeps its lever label, instead of "not mapped". Tests: a hand-built
  point conditioned on `target_phi_word` (the run refuses the Target AFR word,
  not BFS, as the caveat says) and an unconditioned point
  (`conditioning_missing`); `load_point_scaler_slot_copy`.
  **Remaining (14b):** `rom_editor.preview_edits` and the page
  call `classify_edit` and show `messages`; one test calls both front ends.

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
