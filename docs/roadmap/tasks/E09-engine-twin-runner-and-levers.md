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
| Timing trims A/B | CLI only | none | base timing table unmapped; chain untraced (E09.7); no torque plant *(2026-10-17: CLI + API; reaches `ignition_timing_deg` (Δ, ECU side, E09.7); page too (editable trim grids, HI DET / knock state inputs, per-cell Δ° grid); torque stage built, not validated (E07.5 stage 4: H07.10/H07.5/H07.12/H07.8/N01.2 and spark sweeps owed))* |
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
session takes E09.13. *Updated 2026-10-16 (simulator session, overnight,
operator's judgement call):* E09.2 and E09.14 landed; E09.12 stays `[~]`
because neither 10-04 prediction can reach a verdict (no co-sampled BFS,
no logged φ), so the next flash is scored instead, designed from a
twin-profile pre-change drive. That makes E09.1 the critical path, and the
simulator session takes its desk half (the derivation function in
`ecu_application.py`, the `twin` profile, the coverage test); the truck
acceptance stays with the operator. Unclaimed: E09.3–E09.6, E09.11
(truck or Pi for acceptance) and the listing items E09.7–E09.10.
Constraint from W09:
if E09.2a changes the simulate result shape, the W09 session is told the
shape before it lands, since the page and MCP tools render it.

Dispatch tiers (CLAUDE.md): E09.1 Standard; E09.2, E09.3, E09.5, E09.7,
E09.8, E09.9, E09.10, E09.11, E09.12 Deep; E09.4 Heavy; E09.6 Heavy on the
Pi; E09.13 Light. Items marked **listing** need the firmware session
(`~/ecu-re`, see memory `feedback_ghidra_via_owner_session`); items marked
**truck** need a drive; **Pi** needs the Pi.

## Checklist — use case 1: the twin against the running engine

- [~] **E09.1 — A `twin` capture profile, derived from the manifest.**
  *(Desk half 2026-10-16, revised the same day for the scheduler fix, the
  fill byte and the X trace; the drive remains, and three catalog
  identifications the acceptance does not need: see Remaining below.)*
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
  - **Desk, done 2026-10-16.**
    - *Derivation.* `ecu_application.twin_observation_channels(model, memory,
      *, kernel_inputs, kernel_context, downstream)` returns, per evaluable
      output (`evaluable_outputs`: a twin definition and an observation, so
      `target_equiv_ratio` and `base_fuel_schedule_word`; ignition timing had
      no twin and the selected trim no channel until 2026-10-17, see
      *Ignition timing in* below) and per runner stage the model
      does not carry (`injector_pulse_word`, after `final_command_word`), each
      channel with its role (`output` / `input` / `context`), who declared it
      (`output:` / `node:` from `observed_as`, or `kernel`) and its unit:
      `mode01_round`, `mode22_round`, `memory_window` (the catalog window) or
      `context`. The manifest gives the outputs' observations and the
      `observed_as` of every node on the predicted node's path; the kernels'
      pairings are passed in from what the runner reads
      (`twin_runner.BFS_CHANNEL_SETS`, the target-phi gates and X relation,
      every `fuel_plant*.yaml` `specs`, the E07.4 case's `load_input`), so no
      list is written by hand. An `input` beats `context`, so a caller cannot
      demote what the manifest pairs. Refuses `observation_not_catalogued`,
      `observation_not_pollable`, `kernel_output_undeclared`, `node_undeclared`.
    - *Derived groups.* Target phi: outputs `mem:ECM_TARGET_EQUIV_RATIO`
      (fuel_target) + alias `obd:COMMANDED_EQUIV_RATIO`; inputs in windows
      fuel_axis (X, and 0x103A for the X relation), spark (rpm word),
      flags_2841 (pedal flag), uprev_state (magic, active map, selector), which
      equal `target_phi_twin.yaml` `acquisition.windows` (tested); context
      `obd:COOLANT_TEMP`, `obd:FUEL_STATUS`, `obd:RPM`/`obd:MAF` steadiness.
      Steady BFS: outputs 0x103A (fuel_axis) + `did:ECM_AIR_CHARGE`; inputs
      spark, flags_2841 (0x283C), uprev_state, `mode22` {0x1208, 0x1209},
      `mode01` {RPM, MAF}, fuel_comp_x (0x118A). Pulse word: `mode22` {0x1206,
      0x1208}, `mode01` {RPM, MAF, O2_WR_LAMBDA_S1}, fuel_comp_x (E07.4's fill
      byte, 0.1 s); context trims, baro, IAT, coolant, battery, φ reference,
      PID 0x44, `sensor:FUEL_PRESSURE` (external).
    - *Profile `twin`* in `data/nissan-xterra-2008/capture_profiles.yaml`:
      `target_rate_hz` 4, `did_cycle_seconds` 0.5, `bus_budget_override` 90,
      `round_timestamps`, `requires_tune_revision`, `corpus_eligible: false`,
      `_capture-twin`, `experimental: true`. Mode 01 at 4 Hz: RPM, MAF,
      THROTTLE_POS, O2_WR_LAMBDA_S1, COMMANDED_EQUIV_RATIO; 2 Hz: FUEL_STATUS,
      STFT/LTFT both banks; 1 Hz: baro, IAT, coolant, PID 0x42, PID 0x2E.
      Mode 22 every window (2 Hz): 0x1209, 0x1208, 0x1206. Memory every window
      (2 Hz), six windows: fuel_target, spark, fuel_axis (the X trace),
      uprev_state (magic, active map), fuel_comp_x (the fill byte), flags_2841
      (pedal + HI DET).
    - *Capacity.* Demand from `preview_polling_plan`: 53 channel reads/s,
      57 req/s unbatched (with 4 req/s of entry/exit), 29 batched six PIDs to
      a request, against 90 (`expected_total_demand_hz` 57). Cycle from the
      scheduler's own `hybrid_cycle.plan_cycle`, the plan the loop reports at
      `/status.obd_scheduler.did_profile.cycle.plan` (the fake-clock run
      asserts the loop reports exactly it): 18 Mode 01-side requests (15 PIDs
      and the three DTC polls) in 3 batches, 9 in-window requests, 2 session
      requests at 90 req/s plus the 0.05 s exit grace: **`required_seconds`
      0.2056** of the 0.5 s cycle, 0.3722 with batching refused
      (`plan_cycle` with `batch_max_pids=1`). **`worst_case_seconds` 1.2056**
      (1.3722 unbatched) is that plus one request timeout (1.0 s): the cycle
      one unanswered request costs; the boundaries it overruns are skipped,
      not burst. On the fake clock the three cycles that held an unanswered
      DTC poll (the scripted ECM answers no Mode 03/07/0A) ran 1.18–1.21 s,
      4 cycles missed in 120 s, all in the first 5.1 s; the rest 0.500 s. (An earlier version of this text quoted a
      hand-computed 0.37 s "worst case" as `preview_polling_plan`'s; the
      preview reports no cycle.)
    - *Fill byte and X trace in (2026-10-16).* The live-read rule
      (`LIVE_READ_METHODS`, `tests/test_memory_catalog_knock_lambda.py`) now
      accepts `chain_reproduction`: a chain reproduction on a live capture is
      a read whose value is checked against an independent computation, here
      N02's chain reproducing `ECM_FUEL_COMP_X_INPUT` at 60 steady points on
      `_capture-ram-study-2026-10-04T204557` with the wrong variants missing.
      It is the only `chain_reproduction` entry in `data/dids.yaml`.
      `ECM_FUEL_TARGET_MAP_X_TRACE` gained a `live_read` evidence entry on
      the same capture: the X relation `min(255, floor(w103A × 128 / 44019))`
      on the same read is a known answer, 168/194 exact and 26 within 1–4
      counts both signs, over 66 distinct X values (9–100), and it can fail
      (wrong scaler under 100 exact, display-scaled X under 10:
      `test_the_x_relation_exposes_an_x_scale_mistake`). That pins the X
      trace's address; 0x103A's slot stays hypothesized. Both are in
      `twin.memory` and out of `AWAITING_LIVE_READ`.
    - *Fake-clock figures* (`tests/test_twin_profile.py`, 10 ms answers,
      90 req/s, measured from the first session entry, which now opens on
      the cycle 0.011 s in; every pairing kept inside its tolerance by
      `PAIRING_MARGIN_S` = one request slot, 1/90 s, so a phase shift of a
      read cannot flip a verdict): 236 windows in 120 s, each 0.11 s entry to
      exit; pulse↔BFS DID 0.011 s (0.1); BFS↔MAF DID 0.022 s (0.1); BFS DID↔
      Mode 01 rpm 0.160 s (0.25); pulse↔Mode 01 rpm and MAF 0.149 s (0.3);
      the lookup's windows within 0.050 s of the command (0.25; X trace
      0.020 s); **fill byte 0.069 s after the pulse DID on every read**
      (0.1; 0.083 s since 2026-10-17, see *Ignition timing in*); fill
      byte↔PID 0x0C 0.080 s on every read (0 of 192 within 0.1 s
      on the ram-study).
    - *Findings.* (1) **The fill byte trails the pulse DID by every read
      between them.** A window reads DIDs first and memory last, back to
      back (B08.4), in catalog window order (fuel_target, spark, fuel_axis,
      uprev_state, fuel_comp_x, flags_2841). Each DID added to the window
      moves the fill byte one slot later: 0.091 s with 0x120F and 0x1110
      (inside 0.1 s, but within a slot of it), 0.102 s on every read with
      0x1101 as well (was 0.125 s under the old scheduler). So the profile
      leaves those DIDs to `tuning` and reads throttle as PID 0x11, purge as
      PID 0x2E (`fuel_comp_fit.yaml`'s first purge source) and the phi
      coolant gate from PID 0x05. (2) **The steady-BFS RAM set's MAF pairing
      is undecided on the fake clock.** It pairs `obd:MAF` within 0.1 s of
      the 0x103A read (`SteadyGates.max_maf_skew_s`, frozen). What holds
      whatever the phase: no Mode 01 read lands inside a session window, so
      a memory read is at least its distance to the window's edges from the
      nearest MAF (asserted). With 0x103A added the read is 0.100 s from
      MAF, within a slot of the limit, so the fake clock decides nothing
      (an earlier version said "0.102 s, 1 of 280, not achievable": that
      was the clock's phase). On `_capture-tuning-2026-09-23T142926` DID
      0x1208 had `obd:MAF` within 0.1 s on 372 of 3,089 reads (p50
      0.125 s). The set cannot be in a profile anyway while 0x103A is
      hypothesized.
    - *Behaviour change, recorded.* The first `twin` widened `tuning`'s
      `flags_2841` read: memory reads were planned over the union of every
      profile's members, so `tuning` read three bytes instead of one and
      recorded `mem:ECM_PEDAL_ENRICH_FLAGS`. The scheduler fix (narrowed
      reads, `requests.narrow_memory_reads`; separate change, verified on
      its own) removes it. With it present and this `twin` in the tracked
      catalog, `tuning`'s sent frames (with their fake-clock times), decoded
      channels and resolved plan are identical with and without `twin`:
      `tests/test_capture_catalog_independence.py`
      (`test_no_other_profile_changes_what_the_active_one_records[tuning-None]`,
      `[tuning-10.0]`, `[default-None]`,
      `test_a_wider_window_elsewhere_is_not_read_or_decoded_under_tuning`),
      6 passed.
    - *Ignition timing in (2026-10-17, E05.4 (b)/(c) desk part).*
      `ignition_timing` gained `twin:` (the seven implemented chain nodes,
      predicting `timing_final_byte`) and `ignition_trim_selected` an
      observation (`mem:ECM_TIMING_TRIM_SELECTED`), so both are evaluable.
      The chain nodes declare `observed_as` for their results and for the
      unidentified terms they read: fourteen new catalog channels in eight
      windows, after `fuel_comp_x` in catalog order (E05.4 has the table).
      Derived for timing: PID 0x0E, `mem:ECM_IGNITION_TIMING_RAM`, the rpm
      word, X, uprev_state, flags_2841 (pedal, HI DET), DID 0x1101 (the
      coolant byte the trim lookup reads), knock_target (0xFFFF13FE, which
      the knock controller reads), plus the chain words. The live-read rule
      lets in PID 0x0E, DID 0x1101, `ECM_IGNITION_TIMING_RAM` and
      `ECM_KNOCK_RETARD_TARGET`; `twin` now polls them. The other sixteen
      (the fourteen, plus knock_chain's 0x232E and 0x2332) are pinned as
      `TIMING_AWAITING_LIVE_READ`.
      - *Capacity before → after* (`preview_polling_plan`; `plan_cycle` at
        0.5 s, 90 req/s, batch 6):
        - reads: 53 → 61 channel reads/s;
        - requests: 57 → 65 req/s unbatched, 29 → 33 batched;
        - cycle: Mode 01-side requests 18 in 3 batches → 19 in 4,
          in-window requests 9 → 11;
        - `required_seconds` 0.2056 → 0.2389, unbatched 0.3722 → 0.4056;
        - `worst_case_seconds` 1.2056 → 1.2389.

        On the fake clock (`test_one_session_window_carries_every_did_and_window_the_twin_pairs`):
        - every channel ran at its planned rate, and the window grew from
          0.110 to 0.133–0.135 s;
        - fill byte after the pulse DID 0.069 → 0.083 s (0.1 limit; DID
          0x1101 is one more read between them, and DIDs are read
          0x1208, 0x1206, 0x1209, 0x1101 whatever the profile's order);
        - BFS DID to PID 0x0C 0.160 → 0.184 s (0.25); pulse to PID 0x0C/MAF
          0.149 → 0.173 s (0.3);
        - fill byte to PID 0x0C 0.080 → 0.090 s, within a slot of 0.1 s, so
          it is reported, no longer asserted (no kernel pairs the two).

        65 req/s is under the 116.8 req/s the event loop sustained
        (2026-09-10); the old 25 req/s overload was the storage writer, so
        watch storage `queue_depth` on the drive.
      - *The chain does not fit in `twin`* (`test_the_awaited_timing_windows_do_not_fit_beside_the_fuel_pairings`,
        slow). With the sixteen awaited channels polled, there are 20
        in-window requests and the window takes 0.243–0.249 s.
        `plan_cycle` still says feasible (0.3389 s; 0.5056 s and infeasible
        unbatched), but on the fake clock:
        - the 4 Hz Mode 01 group falls to 1.96 Hz;
        - BFS DID to PID 0x0C reaches 0.298 s (limit 0.25);
        - pulse to PID 0x0C reaches 0.287 s (0.3; inside the margin by 2 ms);
        - the fill byte trails the pulse DID by 0.093 s (inside 0.1 by less
          than a slot).

        A timing-only candidate (the ignition outputs' derived channels:
        DID 0x1101 and 14 windows) runs 0.187–0.194 s windows, its 4 Hz
        group at 2.93 Hz. Same-window gaps:
        - selected trim to HI DET 0.063 s;
        - trim to timing byte 0.033 s;
        - trim to rpm word 0.095 s, to X 0.083 s;
        - trim term to timing byte 0.070 s.

        So a second profile with its Mode 01 group at 2 Hz fits (scratch
        simulation, not committed). Splitting is an operator decision.
      - *Tests* added: `test_ignition_timing_is_derived_from_the_implemented_chain`,
        `test_a_timing_node_gaining_an_observation_the_profile_lacks_fails`
        (DID 0x110A declared on `timing_final_byte` → one gap),
        `test_the_awaited_timing_windows_do_not_fit_beside_the_fuel_pairings`;
        the fuel awaited test now uses `FUEL_AWAITING_LIVE_READ`.
    - *Tests.* `tests/test_twin_profile.py` (11; two `slow`, the fake-clock
      runs): the accept test (every derived channel in its section, every
      paired DID and window due in every session window, each Mode 01 round
      group at one weight; the live-read-refused set equal to the pin
      `AWAITING_LIVE_READ`, now 0x103A, the selector and 0x283C); two
      scratch-model mutations loaded by the real loader (a node gains
      `did:ECM_RPM_COARSE`: two named gaps; an output gains hypothesized
      `mem:ECM_AIR_CHARGE_SLOT_1038`: the refused set no longer equals the
      pin); the four refusals; the derived groups against the runner's
      declarations; the planned demand and `plan_cycle`'s cycle; the
      fake-clock runs (every channel at its planned rate ±5 %, every pairing
      inside its tolerance by the margin, the loop's reported cycle plan;
      the awaited variant adds no window, keeps the fill byte inside 0.1 s
      by the margin, and bounds the RAM set's MAF gap by the window edges).
      `test_capture_profiles.py`'s catalog list includes `twin`.
  - **Remaining.**
    1. **Catalog identifications (desk, not needed for the acceptance).**
       Three channels the derivation names stay refused by the live-read
       rule, so no profile polls them: `ECM_AIR_CHARGE_SLOT_103A` (one of
       three air-charge slots, 0x1038/0x103A/0x103C, all within 1 % of DID
       0x1208; nobody has told them apart; read 194 times on the
       ram-study), `ECM_UPREV_MAP_SELECTOR` (hypothesized; constant 0 on all
       196 ram-study reads, so the reads cannot test the disassembly) and
       `ECM_FUEL_TRANSIENT_FLAGS` (0x283C, hypothesized, never read; needs
       one live read on the Pi). Without 0x103A the runner's X relation
       stage and the steady-BFS RAM channel set have no BFS word; target
       phi's lookup has its X input and E07.4 its fill byte, so a drive now
       can meet the acceptance. When one lands it adds no window (0x103A
       widens fuel_axis, the selector uprev_state, 0x283C flags_2841).
    2. **BFS already scores without this profile.** On
       `_capture-tuning-2026-09-23T142926`, `run_twin(...,
       plant_version="fuel-mv-1")` against the ZS44A-20260920 image gives
       `base_fuel_schedule_word` on `DID_1208` **98 eligible** samples
       (2,273 `maf_not_steady`, 442 `rpm_not_steady`, 157
       `maf_window_sparse`, 107 `maf_unpaired`, 10 `below_min_rpm`, 2
       `rpm_unpaired`; measured 2026-10-16), and `target_equiv_ratio` 0
       (1,475 `map_identity_unbracketed`, 194 `sentinel`). The drive's new
       evidence is target phi and E07.4's fill-byte pairing; BFS's > 0 is
       not in doubt.
    3. **The E05 RAM windows.** E05.2(b), E05.3(a)(b) and E05.4(b)(c) name
       about forty addresses; the catalog has channels for 0x1038–0x103E,
       0x118A, 0x283C–0x2842, 0x2332 and 0x232E only, the rest none.
       *(2026-10-17: E05.4 (b)/(c)'s addresses now have channels, all
       hypothesized; see *Ignition timing in*. They need the key-on/idle
       live read, then their own profile.)* Each
       needs a catalog channel and then a live read before a profile may
       poll it. A window read before fuel_comp_x moves the fill byte one
       memory read later (10 ms on the fake clock): from 0.083 s (since DID
       0x1101 joined the window, 2026-10-17) one more puts it at about
       0.093 s, inside 0.1 s by less than a slot (1/90 s), which the margin
       treats as no evidence; the awaited timing windows measured 0.093 s
       (`test_the_awaited_timing_windows_do_not_fit_beside_the_fuel_pairings`).
       So no window may be added before fuel_comp_x. The rest belong in a second,
       non-pulse profile or behind a scheduler change that reads the fill
       byte straight after the pulse DID. The RAM steady-BFS / DID 0x1209
       channel-set idea (pair 0x103A with 0x1209 rather than PID 0x10) is
       dropped for now: 0x103A is hypothesized, so it cannot be in any
       profile.
    4. **The drive (truck).** Profile `twin`; start it at `vehicle_state`
       `engine_running` with the flashed revision recorded, and keep it
       running until parked (memory `feedback_tuning_run_whole_drive`).
       Warm up past 80 °C coolant (the φ coolant gate). Then: steady
       part-throttle holds of ≥ 5 s at 1,500, 2,000, 2,500, 3,000 and
       3,500 rpm (BFS steady gates: MAF ±2 %, rpm ±1 % over ±1 s); at least
       three open-loop enrichment holds, high load in a high gear up a grade
       so rpm stays within 150 for 2 s (φ is eligible only in
       `open_loop_driving` with a 2 s steady dwell); and two or more pulls
       above 4,000 rpm (E07.4 wants WOT above 4,000 and part-throttle
       cruise). Pull the session as a tar stream. Check on the laptop:
       `kit = twin_runner.load_twin_kit(Path("data/nissan-xterra-2008"),
       plant_version="fuel-mv-1")`;
       `rom = rom_effects.load_rom(image, kit.rom_map, kit.model,
       kit.model_sha256, slot=1, slot_source="default_slot")` for the
       flashed image; `run = twin_runner.run_twin(session_dir, kit,
       twin_runner.resolve_ecu(kit, rom), plant_version="fuel-mv-1")`;
       `run.accounting()` must show `eligible` > 0 for
       `target_equiv_ratio` (E02) and for `base_fuel_schedule_word` on
       `DID_1208`, and `run.empty()` names any channel that came back
       empty. E07.4: `plant_validation.load_input_pairing(build_case_samples(
       session_dir, kit.plant, kit.delay, case), case)` with `case =
       load_interventions(...).case("fuel-comp-restore-2026-10-04")` must
       report paired > 0; and every `did:ECM_INJECTOR_PULSE_1` read must
       have a `mem:ECM_FUEL_COMP_X_INPUT` read within 0.1 s on the
       acquisition clock (`read_channels(..., acquired_time=True)`), the
       property `test_one_session_window_carries_every_did_and_window_the_twin_pairs`
       holds on the fake clock (0.083 s since DID 0x1101 joined the window,
       2026-10-17; 0.069 s before). Replace the profile's
       `capacity_evidence` with the drive's measured rates, cycle durations
       and storage `queue_depth`.
    5. **Operator decision** (reported, not taken here): whether a scheduler
       change should read the fill byte directly after the pulse DID, which
       would let 0x120F and 0x1110 back into the twin profile.

- [x] **E09.2 — One replay runner over the node graph.** (2026-10-16: 2a, 2b,
  2c below. Fixtures green through the runner, `-m "slow or not slow"`:
  `test_twin_runner.py` (36) with the other importers of the touched
  modules 468 passed; the 16 files that call `run_paired`/`run_monte_carlo`
  404 passed; `cmp_head`
  equality to 6ad4ca3 on 522 + 592 calls; shared-kernel mutations move
  both front ends; the clean-process test passes with `paired_steady`
  imported.)
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
  - [x] **2a — runner + ECU outputs (2026-10-16; verifier FAIL on empty-result
    accounting, fixed, re-verified PASS: 275 passed; seams byte-identical to
    HEAD on the 7 drives + RAM study; 10 ms-duplicate case now 106 scored in
    both front ends, the intended one-to-one unification).** Open for 2c:
    a public `target_phi.x_reads()` so the runner stops copying
    `x_formula_check`'s X pairing; a reason registry in `target_phi` so the
    reason-enumeration test cannot miss split-tuple or list-form reasons.
    `src/analysis/twin_runner.py`:
    `load_twin_kit`, `resolve_ecu(kit, LoadedRom)` (the evaluators' own
    constructors; a refusal makes that stage `calibration_unresolved:<cal>:<reason>`),
    `stage_order` (model `depends_on` topologically, `schedule` breaking ties:
    BFS → X → target φ → pulse word, the pulse stage after
    `final_command_word`), `run_twin(session | frame of reads, kit, ecu)` →
    `TwinRun`: one record per output per sample (observation, observed_spec,
    acquired_at, observed, predicted, residual, ratio, state, kind);
    `accounting()` and `empty()` per output **per observation channel**
    (BFS: DID 0x1208 round and the 0x103A RAM window, each declared, so one
    channel's eligible reads never hide another's empty). Kernels: φ
    `evaluate_session`; X `x_from_bfs` on the runner's own `match_ordered`
    pairs (every X read is a record with its time and value); BFS
    `score_bfs_read` on `air_charge_twin.pair_bfs_inputs`; pulse
    `build_samples` + `pulse_chain_ratios`. `native` holds each evaluator's
    own result. Refuses `stage_role_changed`, `kernel_mismatch`, `graph_cycle`,
    `reason_unclassified`; kinds `input_not_captured`, `not_co_sampled`,
    `gated`, `outside_model`, `calibration_unresolved`, `pending`, plus
    `output_not_captured`. `map_uninitialised`/`slot_ambiguous`/`map_switching`
    are `outside_model`; `map_identity_unbracketed` is `input_not_captured`
    when an identity channel had no read by the sample's decision time
    (`target_phi.captured_by`), else `not_co_sampled`; a test enumerates every
    reason `target_phi` emits from its source and classifies each. The
    docstring names the pairing exceptions (pulse slow context as-of, rail
    window median, reference-φ bracket; φ identity bracket/argmin and gates).
    **One BFS pairing (operator-delegated):** `evaluate_steady_bfs` now pairs
    with `match_ordered` (`EVALUATOR_VERSION` 2); every field of every record
    and every exclusion count is byte-identical to HEAD on the seven E05.2
    drives and the RAM study (both channel sets), JSON dumps compared with
    `cmp`; the 10 ms-duplicate case is a test. Seams: `air_charge_twin.bfs_series`,
    `score_bfs_read`, `pair_bfs_inputs`; `fuel_plant_eval.build_samples(..., name=)`
    (a named frame equals the directory, `assert_frame_equal`),
    `pulse_chain_states`. `tests/test_twin_runner.py` 22 tests,
    `-m "slow or not slow"` green; each pin through runner and evaluator,
    per-record observed/predicted compared for φ, X and pulse:
    E02 174 = 120 `input_stale:load` + 18 `input_stale:rpm` + 15
    `map_identity_unbracketed` + 21 `sentinel`, `record_lines` == fixture,
    both 0x4A80 commands at the recorded instants; X 194 pairs, 168 exact;
    E05.2 458 of 14,843, median 0.99912; Oct 4 RAM window `maf_unpaired: 194`;
    E07.3 slice n 218 median 1.0084; six captures n 2,361, median 1.0084,
    p10–p90 0.958–1.025, negative control 0.936. Kernel mutations move both
    front ends (four monkeypatch tests; a scratch plugin fails the old tests
    and the runner's alike). Clean-process test: no ECU-facing import.
    **Runner accounting, Oct 4 RAM study (full session):** BFS DID 0x1208
    193 `input_not_captured` (DID 0x1209 never polled), RAM window 194
    `not_co_sampled`; X 194 eligible; φ 174, 0 eligible: `not_co_sampled` 153
    (X/rpm/flag read 194/180/191 times, paired 19/9/0 within the skew),
    `outside_model` 21 — empty because of co-sampling; pulse 193
    `not_co_sampled` (`input_missing:bfs`). **2026-09-21T223734:** BFS DID 106
    eligible, RAM window `output_not_captured`; X `output_not_captured`; φ 1,078,
    0 eligible: `input_not_captured` 927 (X, flag and the three identity
    channels never polled), `outside_model` 151; pulse 655 eligible.
  - [x] **2b — plant stages (2026-10-16).** `run_twin` runs
    `delivered_lambda` (observed `obd:O2_WR_LAMBDA_S1` at B06's 0.10 s via
    `build_samples`), `fuel_per_event` and `manifold_pressure` after the four
    ECU stages, on the pulse stage's own `build_samples` frame (built once,
    `TwinRun.samples`; one sample source). Kernel `fuel_plant.predict` on the
    reads `apply_manifold_refusals` leaves `eligible`. **Plant version named,
    never defaulted (orchestrator decision):** `load_twin_kit` and `run_twin`
    take `plant_version` with no default; `None` refuses `plant_version_missing`,
    a kit with another plant `plant_version_mismatch`; the simulator's is
    `sim_kit.sim_plant_version()`. State → kind: `transient`, `post_fuel_cut`,
    `cold` → `gated`; `fuel_cut`, `lambda_rail`, `short_pulse`,
    `outside_ve_fit`, `manifold_above_ambient` → `outside_model`;
    `missing_input`, `not_co_observed` → `input_not_captured` when a missing
    input was never read in the run, else `not_co_sampled`; no frozen baseline
    → `calibration_unresolved:plant:<reason>`. Fuel and MAP predictions are
    the new kind `unobserved`, never `eligible`; a run with no pulse read says
    `input_not_captured:did:ECM_INJECTOR_PULSE_1` for each plant output.
    `RUNNER_VERSION` 2. `test_every_state_the_plant_names_classifies`
    enumerates `fuel_plant_eval.STATES` (and the check names in
    `build_samples`' source, and the manifold refusals in `fuel_plant`).
    Seam: `fuel_plant_eval.PLANT_SAMPLE_INPUTS` / `CO_OBSERVED_INPUTS` (the
    `missing_input` / `not_co_observed` column lists, now named); CLI
    `identify` mv1/mv2, `compare` mv1 mv2 and `chain`, and `evaluate` +
    `build_samples` on the seven drives (scratch profile), all `cmp`-identical
    to HEAD. `twin_runner.identify_plant(runs, kit, sessions_root=)` =
    `identify` on the runs' samples (VE(rpm) fitted first, as the CLI does).
    **Reproduced, through the runner and the evaluator in the same test**
    (`-m "slow or not slow"`, 32 runner tests green):
    `identify_plant` JSON == `tools.fuel_plant_check identify --plant-version
    fuel-mv-1` stdout, byte for byte: s 0.051063, interval 0.050591–0.051501,
    766 samples, 89 regions, in-sample |err| p90 12.17 %, region pass 0.584,
    null λ≡1 3.35 %; fuel-mv-2 likewise: s 0.050792, 0.050357–0.051198, 764,
    89, 2 `manifold_above_ambient`, in-sample p90 11.61 %, pass 0.618.
    Per record, the runner's eligible λ reads, observed and predicted, equal
    `predict` on the evaluator's eligible rows. **Held out (fuel-mv-1,
    nothing spent: `evaluate` only on a `tmp_path` copy of the profile, real
    `fuel_plant*.yaml` sha-checked unchanged):** the runner's per-record
    errors give `evaluate`'s `lambda` stats and MAF-band table exactly — 845
    reads, 137 regions, pooled median +1.69 %, |err| p90 11.7 %, pass 0.62,
    null 2.33 %, per drive +4.96/+0.69/+1.79 %, bands 614 +1.70 / 24 +1.04 /
    14 +22.2 / 25 +7.0 / 157 +0.85 / 11 −0.55 %; RAM study 0 eligible, by kind.
    **fuel-mv-2 against fuel-mv-1, per sample:** both runs' records on the
    same pulse reads reproduce `compare_versions` exactly (stats per version,
    change summaries and per-band changes): 1,490 common reads, mv2 refuses
    117 `outside_ve_fit` + 4 `manifold_above_ambient`; in-sample 764/89, p90
    12.01 → 11.61 %, pass 0.573 → 0.618; spent 726/101, p90 11.74 → 12.21 %,
    pass 0.564 → 0.554; per-record errors equal `predict` on compare's rows.
    Kernel mutation (`fuel_plant.flow_factor` ×1.1) moves runner and
    `_errors` identically. **Verifier FAIL, fixed:** a dead time is captured
    only if `build_samples` could derive one (PID 0x42 read, or the run has
    fuel-cut pulse reads), in the pulse stage's `_pulse_kind` and the plant
    alike (`_input_captured`). Before the fix, any `COMMANDED_EQUIV_RATIO`
    read made it `not_co_sampled`. Test
    `test_a_dead_time_nothing_could_derive_is_not_captured`: the slice minus
    its 196 `COMMANDED_EQUIV_RATIO == 0` reads gives `dead_time_source`
    none ×1596 and `missing_input`/`input_not_captured` ×1596. Corpus
    unaffected: the six tuning captures carry 58–361 fuel-cut reads each and
    the RAM study 548 PID 0x42 reads, so 0 reads change kind, and the slow
    pins re-ran green (89 passed). **Limits, noted:** `identify_plant` fits
    VE(rpm) on each whole recording (`ve_samples`), even for a bounded window;
    the enumerating test finds `MAP_*` refusals by the `reason[...] = MAP_*`
    pattern, so one written as `np.where(...)` would escape it (it would
    still refuse at run time as `reason_unclassified`); capture is not
    time-aware here (a spec read only before the first pulse read counts as
    captured, so the kind is `not_co_sampled`), unlike target phi's
    `captured_by`. Open for 2c: the paired comparison feeds stated
    (predicted) pulses, not observed ones, into the same plant stage.
  - [x] **2c — `run_paired` on the runner (2026-10-16).** One ECU path:
    `_side` is `twin_runner.resolve_image` (what `resolve_ecu` now calls;
    `SimKit` carries `twin_sha256`, so it is a `RunnerKit`), its refusals
    mapped to the paired names `<label>_<calibration>:<reason>`;
    `_ecu_chain` runs `stage_order(model)` through `twin_runner.stated_ecu`
    on a `StatedSample` (every stage's calibration first, then the X
    scaler; stage refusals mapped to `steady_bfs:` / `target_lookup:` /
    `load_point_scaler_unresolved`, not renamed); the pulse stage is
    `stated_pulse` (one point, or every Monte Carlo combination at once); the
    plant stage is `twin_runner.plant_predict` on `stated_plant_samples`,
    the predicted pulse and dead time in the replay's own sample columns
    (`PLANT_FRAME_COLUMNS`, `plant_inputs`), which `_run_plant` calls too.
    The Monte Carlo fast path keeps no copy: `_fuel_side`, `_pair_loop`
    (loop and closed-loop hold), `output_reasons` (per-output support),
    `rom_gate` and `compare_scenario_roms` are `paired_scenarios`' and both
    call them; `fast_path_diverged` kept (its test green). Plant: the kit's
    (`SimKit.plant`, the sim_kit plant under `load_sim_kit`), named by the
    scenario's `PlantIdentity` and checked (`plant_mismatch`), never defaulted.
    Folded in: `target_phi.x_reads()` (`XReads`; `x_formula_check` and the
    runner's X stage both read through it, the runner's `target_phi._series`
    / `_raw` copy gone); `target_phi.REASONS` (reason → state, `:`-templates)
    plus `excluded_mode_reasons(definition)`, every reason minted by
    `_reason` / `_excluded_mode`, which refuse an unregistered one
    (`reason_unregistered`), and `test_every_reason_target_phi_can_emit_classifies`
    reads the registry, not the source; `rank_as_improvement` refuses a
    non-`InterventionVerdict` by name (`intervention_not_a_verdict`, an
    `EcuOutputVerdict` in the test). **`cmp_head` equality, HEAD 6ad4ca3
    loaded from a `git archive` tree** (a pytest plugin wraps `run_paired`
    and `run_monte_carlo` wherever bound, pickles `asdict(result)` or the
    refusal per call, and prints each module's `__file__`; a leaf walker
    compares key order, types, float bits and array bytes): the 16 test
    files that call them (`-m "slow or not slow"`, private ROM files
    included), 522 calls (413 paired results, 12 paired refusals, 82 Monte
    Carlo results, 15 Monte Carlo refusals; 23,260 float leaves, 2,260
    arrays), 0 differing (4 calls a module-scoped private-ROM fixture makes
    land under another test id as xdist schedules it; compared across ids,
    identical); an explicit case file, 592 calls (580 paired, 12
    Monte Carlo), 0 differing: 14 edits (identical, Fuel Comp ±3, one cell,
    Target AFR, fill_curve_low, timing trims A/B, K, Load Point Scaler,
    fill_time_constant_rising, the Fuel Comp flow axis, the 0xE27E flip, the
    checksum flip) × 6 point sets (conditioned, unconditioned, cold, closed
    loop, model MAP, no battery read) × the contract plant, fuel-mv-1 and
    fuel-mv-2 × with and without `plant_overrides`; Monte Carlo on mv-1 and
    mv-2 over 5 edits; the operator's 20260920 → 20261004 on an 8-point grid
    and a 3-point Monte Carlo, both plants. The W09 form grid on fuel-mv-2
    (`test_rom_editor_private_rom`) is among the 522. The harness needs HEAD,
    so it stays in scratch; its committed reduced forms are
    `test_run_paired_and_the_fast_path_run_the_twin_runners_one_ecu_path`
    (one mutation in `twin_runner.steady_bfs` / `plant_inputs` moves
    `run_paired` and the fast path alike, self-check green) and
    `test_a_mutated_lookup_kernel_moves_the_replay_and_the_paired_comparison`
    (`target_command` +1: replay residual −1, paired phi word +1). Kernel
    patches in `test_sim_cells` / `test_sim_assumptions` now target
    `twin_runner` (where the kernels are bound). No TOOL_VERSION or
    RUNNER_VERSION bump: no result's meaning changed. **Verifier FAIL, fixed
    (refusals on the paired path):** `stage_order` on a model whose stage
    node moved (`node_undeclared` / `stage_role_changed` / `kernel_mismatch`)
    raised `TwinRunnerRefusalError` out of `run_paired` and the Monte Carlo
    `_build_cache`, which W09's `REFUSALS` does not hold and which did not
    pickle, so a workbench job raised (in the spawn child, a broken pool).
    Now `paired_scenarios.refusing_as_simulation` wraps `run_paired` and
    `run_monte_carlo`, re-raising any runner refusal as
    `SimulationRefusalError(reason, message)`; and `TwinRunnerRefusalError`,
    `SimulationRefusalError` and `TwinRefusalError` pickle (`__reduce__` from
    `(reason, message)`). `tests/test_paired_refusals.py`: a pickle
    round-trip per class; `run_paired` on a re-kerneled kit refuses
    `kernel_mismatch` as a `SimulationRefusalError`; the verifier's case
    through `rom_editor.run_job("rom_sim", …)` returns
    `refused: {kind: simulation, reason: kernel_mismatch}`. **Pre-existing,
    fixed (HEAD behaviour changes for this case only):** a Load Point Scaler
    of 0 raised `ZeroDivisionError` from `run_paired` (in `x_from_bfs`). The
    stated ECU stage now refuses it before any stage runs, and the replay's
    X stage too: `calibration_unresolved:load_point_scaler_invalid`, paired
    name `load_point_scaler_invalid` (beside `load_point_scaler_unresolved`);
    tests in `test_paired_refusals.py` (base side computed, candidate named)
    and `test_twin_runner.py` (194 X reads `calibration_unresolved`). After
    the fix the case file is still 592 calls identical to HEAD; the paired,
    Monte Carlo, rom_editor/levers/sim and runner files, `-m "slow or not
    slow"`, 534 passed, 1 xfailed. **Every `src` exception pickles (made
    impossible, not one class at a time):** 30 classes whose constructor
    takes required arguments (`(reason, message)` and others) and whose
    `super().__init__` gets one formatted string could not be unpickled, so
    each gained a `__reduce__` from its constructor's arguments (its
    subclasses inherit it). `tests/test_exceptions_pickle.py` imports every
    `src` module, builds every exception class defined there (strings for
    required arguments, a typed table for the 15 that need more) and asserts
    type, `args`, `str`, `.reason` and `vars` survive `pickle`: 165 passed;
    the same file run in the HEAD tree, 38 failed. Gate after it (the 21 files
    above, the new test, `test_layering.py`): 699 passed, 1 xfailed.

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

- [~] **E09.7 — ECU-only timing predictions.** **listing**, **truck**
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
  *Partial 2026-10-17 (desk; the capture, the page and part of the listing
  remain):*
  - **Base timing, from the exported listing (`~/ecu-re/out`, read only).**
    The base timing is a sum, not one table. Its terms:
    - `0xFFFF1A58 = 1D3E + 27C6 + 2793 − 279A − 134A − 174A − 0x80 + 1344`.
    - `0xFFFF27C6` (`0x04BC5A`) carries most of the advance. It is a time
      converted to crank angle at the current rpm: `(cal 0x880D × 120 ×
      0xFFFF17C6) / (0xFFFF15DE × 0xFFFF19CB)`, plus `cal 0x8B0C × 5 / 2`,
      times `rpm >> 10 × 3 / 5`, minus 14, capped at 127. The × 120 is the
      listing's (`0x04BC7C`–`0x04BC8A`: × 15, `shll2`, `shll`). Resolved
      2026-10-17 (see the next block).
    - `0xFFFF19CB` is X itself (curve `0xCD9B`). `0xFFFF15DE` carries the 4 × 4
      map `0x9AF9` (rpm/50 byte vs axis `0xC890`; X vs axis `0xC88C`; rows are
      rpm, from the `0x0268B8` → `0x026F40` stride). That map is now in
      `rom_map.yaml` as `base_timing_rate_map` with both axes, `inferred`.
      Its values (identical on ROM Store, k106, 20260920 and 20261004) rise
      with both axes.
    - `0xFFFF1A59`, the alternate selected by `0x04CF0C`, is curve `0xCD6B`
      by coolant: `base_timing_alternate_curve`.
    - `0xFFFF134A` = 0 on every image: curves `0xCC63` and `0xCC73` are all
      zero.
    - `0xFFFF174A` is an enrichment retard read off the Target AFR lookup
      `0xFFFF174B`.
    - `0xFFFF1D3E` is a valve-timing term, 0x40 when neutral.
    - `0xFFFF11CB` is `0xFFFF27C4` slewed ±5 per segment, ≤ cal `0x8B05` = 7.
    - `0xFFFF27C3` is idle timing (the `0xFFFF2833` bit 4 sum).

    **Resolved by the firmware session, 2026-10-17** (`~/ecu-re/NOTES.md`,
    "base timing term 0xFFFF27C6 fully resolved"): the divide helpers take
    the dividend in r1 and the divisor in r0 (`0x021C48` signed, truncating
    toward zero; `0x021D6C` unsigned, floor), and `0xFFFF27C6` is computable.
    It is a burn-time model: u = cal `0x880D` × 120 × `17C6` / (`15DE` ×
    `19CB`) + cal `0x8B0C` × 5 / 2 is a time in 1/128 ms, and the term is the
    crank angle swept in it at the current rpm [inferred]. `15DE` carries
    the rate map `0x9AF9` × coolant and mixture curves plus an rpm term;
    `17C6` the q7 factor map `0x9B89` (computed inside Fuel Comp, read only
    here) and the charge word `0xFFFF1B24`. X largely cancels between `17C6`
    and `19CB` and acts through `0x9AF9` and `0x9B89`. Manifest nodes
    `coolant_rescale`, `timing_charge_factor` (its copy to `10EB` is on
    `cycle_snapshot`),
    `timing_term_1512`, `timing_charge_word`, `base_timing_load_curve`,
    `base_timing_charge_term`, `base_timing_rate_word`, `timing_term_27c6`
    (code: `timing_term_27c6_chain` / `timing_term_27c6_steady`), all
    `twin: implemented` (`timing_charge_word` is `twin: observed` with the
    decode as its `reference_model` since part B: its inputs are the
    target-phi path's unmodelled corrections) and on `ignition_timing`'s
    path since part B (below). Hand vectors (hot, φ 1,
    `tests/test_timing_rate_term.py`): 24° at 700 rpm X 32, 33° at 2,500
    rpm X 64, 22° at 4,400 rpm X 112. The tables are in `rom_map.yaml`
    (`base_timing_charge_map`, `base_timing_load_curve` and four 8-entry
    curves). `0xFFFF279A`'s inputs (`timing_term_26d9`, `timing_term_26d0`,
    `timing_term_279a`): on every listed image map `0x9B09` is all zero and
    curve `0xCC33` is zero from entry 10, so `279A` = 0 on a warm engine.
    `0xFFFF11CB`'s source `0xFFFF2BD0` is the intake temperature + 50
    [inferred]; `11CB` itself is still unmodelled (the `0x04E5B0` slew,
    `27C4`, and `1766`'s gating flags). The absolute timing still needs the
    words the `timing_final_chain` known-unknown lists (`11CB`, `26E8` and
    the corrections in `26F6`, `1D3E`, `174A`, `1344`, `1215`, …).
  - **The seven nodes** are implemented integer-exact in
    `src.analysis.ecu_state_models` (`timing_trim_terms` …
    `timing_final_byte`, plus `timing_rate_limit_steady` and `timing_delta`).
    Every unidentified word is a required argument. The manifest nodes are
    `twin: implemented`, and the blocker `ignition_chain_unmodelled` now
    names only `knock_trim_difference`. Hand-worked vectors are in
    `tests/test_ecu_state_models.py`. One of them,
    `test_one_trim_count_is_one_degree_through_all_seven_nodes`, checks
    that the chain adds the trim term unscaled, which makes 1 count = 1°
    the code's prediction for N01.2 (recorded there).
  - **Simulator output `ignition_timing_deg`** (ECU side only). Every other
    term is held equal on both ROMs (`TIMING_DELTA_CONDITIONS`):
    - `paired_scenarios.ignition_timing_at` / `ignition_timing_cells` (per
      trim cell) and `sim_cells.ignition_timing_points` (per design point
      and draw).
    - Each ROM's own X comes from its own BFS and LPS. A K or Fuel Comp
      change therefore reaches the trims' column and `0xFFFF27C6`, which is
      computed per ROM since part B (below; it was
      `base_timing_load_term_unmodelled` until then). A Target AFR change
      that moves φ gets `enrichment_retard_unmodelled`, and since part B
      `compare_roms` traces it into spark through charge word 1B24.
    - Any changed byte outside the two trims that `compare_roms` says
      reaches `ignition_timing` (`sim_cells.timing_reach_reasons`) makes the
      trim-grid block `unevaluable` with no cells, naming the node
      (`timing_input_changed:<node>`), or `unknown_bytes_changed` for a byte
      no node reads; every design-point pair carries the same reasons. On
      ZS44A-20261004 with HI DET set: K +10 % gives
      `timing_input_changed:air_charge_tp`, a Fuel Comp cell
      `timing_input_changed:fuel_comp_lookup`; `base_timing_rate_map`
      `add_raw 5` gave `unknown_bytes_changed` until part B, which computes
      it on the grid. Tests:
      `tests/test_rom_sim_cli.py::test_an_edit_that_reaches_timing_outside_the_trims_is_not_zero_on_the_trim_grid`
      (k, fuel_comp), and k106 against ROM Store
      (K +6 %) through the block in the k106 test. `report.html` renders
      each design point's outcomes.
    - Calibration facts the twin rests on are pinned as manifest
      `calibration_conditions`, so an image that differs is refused
      `unmodelled_logic`: cal `0x8B05` = 7 on `timing_trim_terms` (the
      0..7 subtrahend bound behind `timing_delta`'s "exact when both trims
      ≥ 7") and cal `0x8294` = 0 on `timing_retard_combine` (it skips the
      held-step branch: `0x04DF72` `tst r5,r5; bt 0x4dfb6`). All five
      operator images pass. `0xFFFF1215` is a signed byte (`0x04DF4C`
      `mov.b`, then `neg` with no `extu.b`) and `0xFFFF46E6` an unsigned
      byte (`0x04E05E` `mov.b`, `extu.b`), with vectors in
      `test_the_retard_combine_takes_the_larger_retard_and_its_two_paths`.
    - The HI DET latch and knock control are stated inputs with no default
      (`--high-det`, `--knock-control`). A trim edit without them is refused
      (`timing_state_unstated`). Since part B, fallback with HI DET clear is
      refused (`high_det_forced_by_ks_fault`): the gate (`4782` bit 5 clear)
      is open in the engine state machine `0x027CC4`'s states 2–4 (`282A`
      bit 5, engine running [inferred meaning]), and inside it
      `0x04A928`–`0x04A990` sets HI DET on every pass while `479D` bit 7 is
      set (firmware notes 2026-10-17, "HI DET gate"). It was described as
      reachable before that finding.
    - With no logged timing (grid designs) a change is reported with
      `clamp_headroom_unknown`. A trim the ECM does not read in the stated
      state gives 0 with a named reason (`trim_a_not_read:high_det_latch_set`).
      The 56–60 °C blend stays `coolant_blend_band`.
    - Torque and knock carry E07.5's `claim_refusals`, headed by
      `spark_response_unmeasured`, plus `knock_unmodelled`.
    - Where it appears: `rom_sim inspect`/`run` (`result.json`
      `ignition_timing`, `report.html` section) and the ROM Editor's
      simulate result.
  - **Known answers** (`tests/test_ignition_timing.py`):
    - k106 against ROM Store ZS44A, with HI DET clear: Δ = the masked trim-A
      change in exactly the ten cells, rows 7–9 × cols 12–15, +1/+2. With
      HI DET set: 0 everywhere, the ten cells naming
      `trim_a_not_read:high_det_latch_set`.
    - −3 counts on all of trim B on ZS44A-20261004, through the edit spec
      and `rom_sim inspect`: −3° in all 256 cells with HI DET set; 0 with a
      named reason when clear. The same edit through `rom_sim run` on a 9-point
      grid gives −3 at every (point, draw).
    - A K +10 % `rom_sim run` (9-point grid, 2 draws), checked by hand,
      moved X 29→32 … 66→73 and the trim term by −2…+1. Every pair was
      `unevaluable` (`base_timing_load_term_unmodelled`,
      `timing_input_changed:air_charge_tp`) until part B, and so is the
      trim-grid block still (the grid holds X equal).
  - **Logged-timing sanity check** (not validation; the base terms are not
    observed). `_capture-rom-trace-2026-09-21T142926` is on ZS44A-20260920,
    with HI DET taken as set (UpRev logs read 0x40 throughout). It has
    1,416 BFS (0x1208) + timing (0x110A) reads with broadcast rpm within
    60 ms.
    - Over 1,415 consecutive pairs (< 1.5 s apart), the predicted trim term
      moved in 334. Logged timing moved the same way in 49 % of those, and
      by exactly the trim change in 12 %.
    - Where the trim term held (1,081 pairs), timing held in 67 % and moved
      at most 1° in 84 %.
    - So the trim term alone does not explain logged timing changes, as
      expected when `0xFFFF27C6` moves with rpm and X.
    - The 09-21..09-23 tuning drives co-sample BFS with Mode 01 timing too
      rarely to add anything (4–85 pairs).
  - **Editable:** `rom_editor.EDITABLE_TABLES` gains `timing_trim_a`/`_b`, so
    the ROM Editor previews a trim edit. `docs/headless-api.md` and
    `docs/mcp-server.md` list them.
  - **Remaining:**
    - ~~**Page:** `SimRequest` carries no HI DET / knock state yet~~
      **Done 2026-10-17 (operator-approved option (a): the state is two
      required request fields).**
      - **Request:** `SimRequest` gains `high_det` (`set` | `clear`) and
        `knock_control` (`active` | `fallback`), both required, no default
        (23 fields; `ValidationError` names a missing one). They build the
        `TimingState` (`source: "stated: ROM Editor"`) the simulate job hands
        the timing block, so a trim edit no longer refuses
        `timing_state_unstated`; a K or Fuel Comp edit still gives
        `timing_input_changed:<node>` and an unevaluable block.
      - **Typical or never:** both are in `rom_editor.NEVER_TYPICAL_FIELDS`
        (so "Fill typical values" cannot fill them and a YAML that names one
        is refused; `sim_typical_values.yaml` documents why). Evidence: the
        2026-10-08 RAM read found the latch set (`0xFFFF283F` = 0x40) and
        knock control in fallback (`0xFFFF13FE` = 0xFB00, KS1 / P0328); both
        clear when KS1 is repaired, a set latch makes every trim A edit read
        as no change, and they are the experiment's own condition. The page
        remembers neither.
      - **Page:** `EDITABLE_TABLES` lists `timing_trim_a` / `timing_trim_b`
        (cell grid, same affordances as the fuel tables). The cells carry a
        0x7F decode mask and a flag in bit 7, so the page does what the
        server's spec does: `add_raw` / `scale` act on the low 7 bits and keep
        the flag, a result outside 0..127 is `value_out_of_range`, `set_value`
        sets flag | value, and a `set_raw` that changes a flag bit is
        `masked_bits_changed`, the server's own name, for a raw outside the
        dtype too (the server checks the mask first) and for a selection of
        mixed flags (no one raw keeps both; the message says so). Page-only
        pre-flight refusals, which the server never sees because the page
        sends `set_raw`: `value_not_invertible` and, for `set_value`, a value
        whose value bits fall outside 0..127 (`value_out_of_range`). The
        Simulate form gains two required, unchosen
        choices (HI DET latch, Knock control) named in "What blocks Run". The
        result gets an "Ignition timing (ECU side)" section: status, the
        stated state, a 16 x 16 per-cell Δ° grid on the trims' own axes
        (a number the block gives is drawn as its signed degrees whatever
        else the cell names, so with both trims edited the trim the latch
        reads shows its Δ and the other is named in the hover text; `NR` only
        where the edited trim is unread and the cell's Δ is 0; `U` for
        unevaluable; `·` for no change), each reason with its count and a
        sentence (`timing_input_changed:*`, `trim_a_not_read:high_det_latch_set`
        and the rest), the torque and knock refusals as the block carries
        them, and the conditions held equal. The grid scrolls inside its own
        box at 412 px.
      - **Backend fix found by the verifier, 2026-10-17:**
        `timing_cell_summary.changed_trim_not_read` matched every reason
        starting `trim_`, so `trim_term_floor_may_bind` (a trim the ECM DOES
        read, set below the floor of 7) counted as an edit it does not read.
        It now matches `trim_[ab]_not_read:` only
        (`tests/test_ignition_timing.py`
        `test_the_unread_count_counts_only_a_trim_the_state_does_not_read`;
        the old rule gives 1 where the new one gives 0). The page drew `NR`
        over a real change when both trims were edited (Δ −2 on trim B, read,
        with `trim_a_not_read` beside it); fixed as above.
      - **Evidence (laptop, offline):** `tests/test_rom_editor.py` (76,
        `-m ""`): a trim simulate shows Δ −3 in exactly the edited cells (the
        flagged cell 0x80|47 -> 0x80|44 included), torque
        `spark_response_unmeasured`; the trim the latch does not read is named
        `trim_a_not_read:high_det_latch_set`, not shown as 0; a missing
        `high_det` / `knock_control` is refused by name; neither is a typical
        value. **On the real ZS44A-20261004 image**
        (`tests/test_rom_editor_private_rom.py`, slow, skips by name without
        the image; 8 new cases, 8.5 s): trim B `add_raw` −2 on row 8, columns
        4–11 (the flag, bit 7, is clear before column 6 and set from it, so
        the edit crosses the edge and each cell keeps its own flag) gives Δ −2
        in exactly those eight cells with the latch set, torque
        `unsupported: spark_response_unmeasured`, candidate trim B = base −2
        and trim A untouched; with the latch clear the same edit is Δ 0, each
        cell naming `trim_b_not_read:high_det_latch_clear` (8 unread); trims A
        and B both −2: Δ −2 in the eight cells under either latch (table read
        `b` / `a`) with the other trim named beside it (8 changed, 8 unread);
        trim B set to 3 counts (flag kept) with the latch set: one
        `unevaluable` cell, `trim_term_floor_may_bind`, 0 unread. With the API,
        MCP, snapshot, layering, paired-scenario, timing and sim tests:
        302 passed, 1 xfailed (`-m ""`). Vitest 258 passed over
        `RomEditorSimulate`, `RomEditorPage`, `SimResults`, `romSimTiming`,
        `romEditor`, `romSim`, `romSimTypical`; Playwright
        `e2e/romEditor.spec.ts` 9 passed (the whole e2e suite earlier: 40
        passed, 1 skipped), including a trim-B edit through preview and
        simulate to the Δ grid, and the phone-width test with the timing grid
        on screen (no horizontal page scroll).
      - **Not yet done:** a walk-through of the live page on the Pi/laptop
        daemon with the real image in the library (the operator's). The
        daemon's code on the real image is covered above; what remains is the
        page against a live daemon.
    - **Truck:** E05.4 (c)'s capture is needed to locate the earliest
      disagreement and to declare a final-byte tolerance.
    - **Part B (desk, 2026-10-17): 0xFFFF27C6 and 0xFFFF279A wired into the
      simulator.** `paired_scenarios.ignition_timing_at` computes the
      27C6 change from each ROM's own X with `timing_term_27c6_steady` on its
      own tables and cals (read by `rom_map` id: `base_timing_charge_map`,
      the rate map and four curves, `base_timing_load_curve`, and the 1×1
      cals `0x880D`, `0x880E`, `0x8440`, `0x8B0C`, `0x8538`, new in
      `rom_map.yaml`). Coolant is the rescaled byte. 1512 is 0 when cal
      `0x8538` is 0 on both images, else `term_1512_unmodelled`. The charge
      word 1B24 is never computed: `26F6 − 26E8` = 0x4000 + `175C` + `1736`,
      times the enrichment multiplier `26C0` (`0x04B98A`), where `1736` reads
      X (map `0x9724`) and `175C` reads BFS `0xFFFF1038` (curves
      `0xD34D`/`0xD345`), both stateful and not modelled, and `26C0` follows
      the Target AFR lookup. So 1B24 changes whenever an edit moves X, BFS or
      φ while `175C`/`1736` are nonzero (or the multiplier is not neutral).
      It is swept over 0x4000 × [0.7, 1.3] at every b step (928 counts) plus
      both ends (and every 64 counts if `0xCEAB`/`0xD60F` bend in the band):
      held equal on both ROMs only when nothing moves it (a base-timing
      table edit at fixed X, BFS and φ: the trim grid), else swept on each
      ROM independently, the change running over every (base step,
      candidate step) pair, with `charge_word_inputs_moved` and
      `charge_word_routes` naming the route (`0xFFFF1736 via X`,
      `0xFFFF175C via BFS`, `0xFFFF26C0 via the Target AFR lookup`;
      verifier finding and coordinator decision, 2026-10-18). An answer that
      does not collapse is a range (`status: range`, `delta_min`/`delta_max`,
      `charge_word_1b24_unresolved`), clamped at both ends. 279A is zero when
      `0x9B09` reads 0 at (min(255, rpm word), rescaled coolant) and `0xCC33`
      at the coolant reads 0 on both images (`11CE` = min(255, `w1210`),
      listing `0x03907A`, `0x038FEA`–`0x039034`); otherwise
      `term_279a_unmodelled` when BFS moved or the 279A tables differ, else
      held equal. The manifest wires `timing_term_27c6` and
      `timing_term_279a` into `base_timing_sum` with `rom:` refs;
      `timing_retard_map_3630` is implemented; `timing_charge_word` is
      `observed` (the correction sum and enrichment multiplier behind it are
      not modelled), so the twin profile does not grow: the seven
      `mem:ECM_TIMING_CHARGE_*`/`RATE_*`/`LOAD_CURVE` channels it reads are
      hypothesized and pinned as awaiting a live read. A consequence: a
      Target AFR edit now reaches `ignition_timing` through the enrichment
      multiplier (`observed_not_predicted:timing_charge_word`), so its
      controls are lambda and spark and it no longer ranks
      (`unsupported:ignition_timing:unevaluable`). The trim grid computes 27C6
      per cell, so an edit to the base timing tables is evaluated there.
      Fallback with HI DET clear is refused (`high_det_forced_by_ks_fault`;
      the gate is open in states 2–4 of `0x027CC4`, [inferred] engine
      running), and cal `0x8ADC` bit 7, which the latch logic needs
      (`0x04A672` clears HI DET and skips it when clear), is pinned as a
      `high_det_latch` calibration condition (0x86 on all five operator
      images).
      Hand-worked tests in `tests/test_ignition_timing.py`: an X move that
      collapses even swept independently (rpm word 40, X 91→85: 27C6 is 10
      at every 1B24 on the base and 11 on the candidate, Δ +1 exact), ones
      that are ranges (rpm word 200, X 104→98: [0, 2], +1..+3 with a trim
      change; X 64→61: [0, 3], base 47 reaches exactly 50, base 48/49 clamp
      to [0, 2]/[0, 1] with `timing_cap_binds`), the X move wider than the
      held-equal sweep at the same point (104→98 [1, 1] held vs [0, 2];
      64→61 [0, 2] vs [0, 3]; BFS-only [−2, 2]), the charge sweep, 279A hot zero and cold
      `term_279a_unmodelled`, cal `0x8538` = 3 → `term_1512_unmodelled`, a
      `0x9AF9` one-cell edit (+20 at [1][2]) changing 80 trim-grid cells, a
      range [−5, −3] at (56, 64) and −3 exact at (56, 56), and fallback+clear
      refused; through `rom_sim run` in
      `tests/test_rom_sim_cli.py::test_a_base_timing_table_edit_is_computed_on_the_trim_grid`.
      End to end on the real K −5 % spec (ZS44A-20261004, 8 draws, seed 1,
      HI DET set, fallback): before part B, all 936 pairs `unevaluable`
      (`base_timing_load_term_unmodelled`); with 1B24 held equal, 656
      ranges, 80 exact, 200 `enrichment_retard_unmodelled`; with 1B24 swept
      independently, 736 ranges (Δ ends from −3 to +4), no exact pair, 200
      `enrichment_retard_unmodelled`, `charge_word_inputs_moved` on all
      936 (720 via X and BFS, 16 via BFS alone, 200 via X, BFS and the
      Target AFR lookup); 279A zero at every pair. With the enrichment
      retard 0xFFFF174A also held where X moves at an enriched point (its cap
      0xFFFF174C reads X; enriched = either ROM's gated byte 0xFFFF174B at or
      above cal 0x8792 / 0x8793 by the latch, below-threshold gain cal
      0x8797 / 0x8798 not 0, or the byte unknown; all four cals new in
      `rom_map.yaml`, 0x82 / 0x82 / 0 / 0 on all five operator images;
      listing `0x04C9D8`–`0x04CB7E`; verifier finding 2026-10-18), final:
      576 ranges (most often [−1, 2] 128, [−2, 2] 88, [0, 2] 80, [0, 3] 72)
      and 360 `enrichment_retard_unmodelled`. On the WOT pedal-flag design
      (`61f25fab…`, 30 points, 3 draws), where the lookup reads its last
      column so K moves X and not φ, all 90 pairs are
      `enrichment_retard_unmodelled` (they were silently ranges before the
      fix). Test:
      `tests/test_ignition_timing.py::test_an_x_move_at_an_enriched_point_holds_the_enrichment_retard`.
      The trim grid holds X and φ equal, so 174A is equal there and nothing
      is held. The trim grid block for K
      stays `unevaluable` (`timing_input_changed:air_charge_tp`: the grid
      holds X equal). Not verified: the 0.7–1.3 band for 1B24 (a stated
      assumption until `mem:ECM_TIMING_CHARGE_WORD` is read live), and that
      `0x283D` bit 7 holds in the steady state.
    - **Listing:** done (2026-10-17, above). **Truck:** one window set
      reading `0xFFFF15DA`–`0xFFFF15DE`, `0xFFFF17C6`, `0xFFFF19CB`,
      `0xFFFF1B24`, `0xFFFF10EB` with `0xFFFF27C6` and `0xFFFF1A58` at hot
      idle and at steady cruise (the hypothesized `mem:ECM_TIMING_RATE_*`,
      `…_CHARGE_*`, `…_LOAD_CURVE` channels; in no profile).

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
  - **The second prediction of record: its own frozen block (2026-10-16,
    laptop).** `ecu_predictions[0]` in `plant_interventions.yaml`, id
    `fuel-comp-restore-2026-10-04-wot`, sha `bfc4e6ee…` (frozen
    2026-10-16T11:14:00Z).
    - **Why a block and not a second case.** It is the same intervention:
      the same flash, images, E07.4 matching, floor and acceptance (frozen
      once in `f522ff53…`) and the same post-flash recordings. A second case
      would copy all of them, and the copies could drift. It is not an
      amendment either: the first artifact stays frozen inside `15eaa48b…`.
      So `EcuPredictionBlock` carries its own artifact and hashes, outputs,
      quantities and the amendment-3 rules as required fields. It scores the
      case's declared post list (`ecu_outputs.post` + `ecu_post_appended`,
      whose chain is unchanged). Refusals: `rules_changed`,
      `null_control_in_design`, `prediction_artifact_duplicate` (an
      artifact already under a frozen block), `design_recordings_mismatch`
      (checked against `design.json` before scoring), `block_and_amendment`.
      The verdict seals `block` beside the rules version and sha.
    - **Artifact hashes, verified before the block was written.** All equal
      to E08.7's (b870e80f): `run-200draws/result.json` `4f5db38a…`,
      `report.html` `4cde07b0…`, `traces.parquet` `760bcea0…`;
      `result_digest` `6180dbcd…`; design `f4332b8d…` as named in
      `result.json` and `design.json`. `design.json` `20e516956daa5b2a…2cdf`
      and `design.parquet` `b648148c4857c018…000e` match E08.7's prefix and
      suffix, and the block holds their full values.
    - **Rules.** Amendment 3's: BFS air-normalised (ceiling 65,280), φ raw.
      Only pairs whose predicted |p50| exceeds the floor count, with a
      minimum of 20, and unknown regime is excluded. The reason, frozen in
      the sha, states what the pre side alone predicts:
      - **ΔBFS** `prediction_unresolvable`. The floor on this design is
        3.19 % on 330 pairs. Only 3 of 917 points predict beyond it (points
        365–367, +3.82 to +3.85 %), and all three are unknown regime.
      - **Δφ**: 26 points have a nonzero p50 under the scoring quantile.
        E08.7's 27 used the interpolated median. The floor is 0.0 % on 171
        pairs, but only **9** of those points have a φ read within 0.1 s in
        a named regime. So Δφ is capped at 9 pairs, below 20, however well a
        post-flash drive is co-sampled.
    - **Order evidence.** Frozen while 0 co-observed post-flash pairs exist
      (`co_observed_at_freeze`: 0 / 0). The five recordings hold 33 BFS
      reads, all `not_co_observed`, and no φ read. No null drive had been
      scored. The design is fuel-mv-2's, whose pairing tolerances (0.3 s /
      0.1 s) equal fuel-mv-1's.
    - **Null control** (`ecu_null_control`: base image, the real WOT
      prediction, the block's rules). It uses the pre-flash tuning drives
      outside the design: `_capture-tuning-2026-09-23T142926` and
      `-23T164007`. Its four design drives cannot serve. **Neither passes,
      and neither can fail: the control is unpowered.**
      - 142926: both outputs `insufficient_evidence`,
        `prediction_unresolvable`. BFS 0 of 354 pairs (534 eligible reads,
        floor 3.19 %). φ 0 of 179 pairs (261 eligible reads, floor 0.0 %);
        none of the 9 resolvable φ points is matched.
      - 164007: `filtered`, 0 eligible reads out of 1,353 BFS and 708 φ
        reads. The fill byte never pairs (`fill_byte:unpaired:pid_0x11` 322
        on BFS) and the rest is transient.
    - **Post-flash, the five by name: `insufficient_evidence` for both
      outputs, 0 pairs.** Sealed under block `fuel-comp-restore-2026-10-04-wot`,
      rules v1, `bfc4e6ee…`, scope `named`. BFS: 33 reads, all
      `not_co_observed`, floor 3.19 % on 330 pairs. φ: `never_captured`,
      floor 0.0 % on 171 pairs.
    - **Tests** (`tests/test_plant_validation_ecu_outputs.py`, now 30 fast
      and 7 slow). Fast:
      - the block's hashes, E08.7's values and the chain, with E07.4's
        `f522ff53…`, v1 `15eaa48b…`, v2 `c458d1a6…` and v3 `7a9f4279…`
        unchanged;
      - six edits refusing `rules_changed`, plus `null_control_in_design`,
        `prediction_artifact_duplicate` and the required fields;
      - the artifact refusals (`prediction_artifact_missing`,
        `prediction_artifact_changed`) before any recording is touched, and
        `block_and_amendment`;
      - `design_recordings_mismatch`, the seal naming the block, and a
        null-control read refusing `wrong_side`.

      Slow (each skips by name without the artifact or drives): the null
      control and the post-flash verdict. Mutations caught: dropping
      `_check_predictions`, and dropping the null-in-design check.
    - **Gate.** `pytest -q -n 4 tests/test_plant_validation.py
      tests/test_plant_validation_ecu_outputs.py tests/test_layering.py
      tests/test_exceptions_pickle.py`: 225 passed. `-m "slow or not slow"`
      on the ECU file: 37 passed. Whole-repo `ruff check`, `ruff format
      --check` and `mypy src` are clean.
  - **Remaining:**
    1. **Open dependency: E09.1's `twin` profile, which does not exist yet.**
       It must poll BFS with rpm, MAF and the fill byte, and log the RAM φ
       word. A post-flash drive on it is truck work.
    2. **Desk work once such a drive is pulled.** Append it to
       `ecu_post_appended` (chain value from `post_chain_sha256`), then run
       `score_ecu_outputs(..., amendment=3)` over every declared recording.
    3. **Operator decision: neither prediction can reach a BFS or φ verdict
       under the v3 rules.**
       - The first prediction has no resolvable design point in a named
         regime (the Consequence above).
       - The second, `fuel-comp-restore-2026-10-04-wot`, is now frozen and
         scored (block `bfc4e6ee…`, above). Its ΔBFS has 3 points beyond the
         3.19 % floor, all unknown regime. Its Δφ is capped at 9 resolvable
         design points, below 20, because 421 of its 917 points have no φ
         read within 0.1 s.
       - So a drive from item 1 gives `insufficient_evidence` against both
         designs, however well it is co-sampled.
       - Its null control (142926, 164007) does not pass, but it is
         unpowered: no resolvable pair on 142926, no eligible read on 164007.
       - Options, each a new prediction or block and never an edit of a
         frozen one:
         - (a) accept `insufficient_evidence` and retire E09.12's scoring of
           this flash;
         - (b) a new design whose points carry a co-sampled φ read (a
           `twin`-profile pre-change drive cannot exist: the base image is
           gone), or a declared φ pairing tolerance justified by φ's own
           update rate, frozen as a new block;
         - (c) score the next flash, with the pre-change drive on the
           `twin` profile recorded before it.
       - **Decided 2026-10-16 (orchestrator, operator-delegated overnight):
         (c).** (b) would loosen φ pairing after these numbers are known,
         which is too close to fitting; (a) is what (c) implies for this
         flash anyway. So the 10-04 flash's scoring stays
         `insufficient_evidence` and is not pursued further. The next
         intervention gets, in this order: E09.1's `twin` profile; a
         pre-change drive on it; a prediction whose design comes from that
         drive (so its points carry co-sampled BFS, rpm, MAF, fill byte and
         φ); a frozen block; then the flash and the post-change drive. The
         operator may revisit.
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

- [x] **E09.14 — `inspect` and the page name the lever and its blockers.**
  *Done 2026-10-16: 14a `0631530` (CLI + `rom_levers.classify_edit`), 14b
  `0ed1ea1e` (W09.10: `rom_editor.preview_edits` returns `levers` =
  `classify_edit(...).to_dict()`, the page lists `levers.messages` verbatim
  above Simulate). `tests/test_rom_levers_parity.py` calls CLI `inspect` and the
  preview for the same edit and requires identical strings (mutation-checked);
  real-image case in `tests/test_rom_editor_private_rom.py`. Evidence: W09.10
  in `docs/roadmap/tasks/W09-rom-editor.md`.*
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
  *(2026-10-17, E07.5 stage 4: the torque stage is now built and not
  validated, so a timing-trim edit says "torque production built, not
  validated (owed H07.10, H07.5, H07.12, H07.8, N01.2 and 6 more)" and
  "spark response unmeasured"; "no torque plant" is produced only while the
  stage is unbuilt. `rom_levers` `TOOL_VERSION` 2; the parity test and
  fixture assert the new string.)*
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
