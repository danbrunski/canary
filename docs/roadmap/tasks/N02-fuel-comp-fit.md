# N02 — Fuel Comp corrections from logged fuelling error

Written 2026-10-04, after the Fuel Comp load input was decoded
(`docs/ecu-firmware-analysis.md`, "Fuel Comp load input `0xFFFF118A`, and how
BFS is formed").

**Why this exists.** Fuel Comp (slot `+0x660`, 16 × 16, 128 = 1.0) multiplies
the MAF-derived charge at each rpm × throttle-fill point, inside BFS. Its
job is to make the ECU deliver the AFR it commands. The logged data
measures how far off delivery is:
- in open loop, measured λ against commanded φ;
- in closed loop, the trims.

Now that the load byte is decoded, every past sample can be placed on the
Fuel Comp cells that were active when it was logged. This checklist builds
that analysis and proposes per-cell factors. **It never writes a ROM.** Any
edit is the operator's decision, flashed in UpRev under the AGENTS.md rules.

**What it cannot do.** It cannot find the best target AFR or timing: that
needs power or knock evidence. It cannot correct boost either. The load byte
saturates near full throttle (about 207–224 above 1,660 rpm), so Fuel Comp
sees no boost (see the firmware doc).

**Who does what.** Implementer dispatches build N02.1–N02.6. The
orchestrator runs N02.7 on the corpus and records the outcome. Task-level
truth is the checkboxes; `[x]` only with evidence in hand.

**Depends on:** W05.1 lookup kernel (`src/decode/ecm_lookup.py`), W08 image
loading (`src/analysis/rom_image.py`), B06 delivery error
(`src/signals/lambda_delivery.py`, `tuning.delivery_error`), the tune
registry. **Order:** read the results after the first drive on ZS44A-20261004
has tested the restore prediction
(`data/analyses/2026-10-04-fuel-comp-restore-prediction.md`). If that fails,
the model of how Fuel Comp acts is wrong and N02.5's factors must not be used.

## Facts the design rests on (2026-10-04)

- Sessions: `~/canary-data/sessions/nissan-xterra-2008/` (126 dated ≥ 2026-08-03).
  - k106: 2026-08-03 → 09-21; 20260920: 09-21 → 10-04 23:02Z.
  - k105 and osiris-initial have no surviving ROM file.
- Throttle:
  - plain sessions and `fuel-map` captures log Mode 01 PID `0x11`;
  - `tuning` captures (the WOT pulls) do not, but log DID `0x120F` instead.
    That is throttle sensor A's A/D count before the learned closed position
    `w4C72` is subtracted.
  - PID `0x11` = `w2CCE × 100/321`, so 8.19 counts per %, quantised at
    about 3.2 counts.
- Purge (PID `0x2E`) is in plain sessions, not in `tuning` captures. Its term
  (≤ about 72 area units) only matters near idle.
- Idle air `w63FC` (≤ 1185) is never logged in history. canary-60's fit put it
  at about 115 at steady points.
- Fuel pressure (`sensor:FUEL_PRESSURE`, kPa gauge) exists only from 2026-10-04.
- The logged load byte `mem:ECM_FUEL_COMP_X_INPUT` exists only in
  `_capture-ram-study-2026-10-04T204557` (192 rows). It is the known answer.
- The codebase combines trims additively (`derived:TOTAL_TRIM_B1/B2`); keep that.

## Checklist

- [x] **N02.1 — ROM definitions and a hash-checked image resolver.**
  Add new entries to `data/nissan-xterra-2008/rom_map.yaml` (new entries only;
  the file's other entries belong to another session):
  - throttle-area curve `0xDD94`: 32 u16, with its 32 u16 axis at `0xEE6A`;
  - fill curves `0xEAEC` and `0xEACC`: 16 u16 each;
  - cal byte `0x8D25` (158);
  - idle-air cap `0x8ED2` (1185);
  - limp-home area `0x90BE` (782);
  - K at slot `+0x762`;
  - the code constant `0xDC1D00` as a program constant, with evidence.
  Extend the schema with tests if it cannot express 1-D u16 curves or scalars.
  Add a resolver: revision → plain image from operator-supplied directories
  (CLI argument or environment, no hard-coded paths). It decodes OsirisROM and
  checks the registry's `image_sha256`, then yields:
  - that revision's Fuel Comp table, rpm axis `0xCACD` and load axis `0xCB29`;
  - the curves and K.
  **Accept:** k106, 20260920 and 20261004 resolve from the UpRev files.
  `0xCB29` reads 16…144 on k106/20260920 and 16…255 on 20261004. A wrong
  hash, a missing file, or k105/osiris-initial refuses by name. Tests that
  need the private ROM skip with a stated reason; a missing ROM is not a pass.
  **Done 2026-10-04.** `rom_map.yaml` gains (new entries only)
  `throttle_area_axis` (0xEE6A) / `throttle_area_curve` (0xDD94, 1×32 be16),
  `fill_curve_low` / `fill_curve_high` (0xEAEC / 0xEACC, 1×16 be16),
  `fill_rpm_multiplier` (0x8D25), `idle_air_cap` (0x8ED2), `etc_limp_home_area`
  (0x90BE), `evap_purge_area_gain` (0x82FE, the 144 in `w139E`; not in the list
  above but read by the same chain), each with its code addresses; K is the
  existing `k_constant`. The schema already expressed 1-D be16 curves and
  scalars; it gains `program_constants` (`ProgramConstant`: offset, `be32`,
  the decoded `value`, `loaded_at`, evidence) for the literal `0x00DC1D00` at
  0x03C5E8 (loaded at 0x03C3F6, the only occurrence in all four images), and
  `check_program_constants` refuses an image whose literal differs.
  Resolver: `src/analysis/fuel_comp_rom.py` `RevisionImageResolver`
  (directories from the caller or `CANARY_ROM_DIRS`; explicit image files
  matched by hash), refusing by name: `revision_unknown`,
  `no_surviving_image`, `image_not_found`, `image_hash_mismatch`,
  `image_unreadable`, `wrong_program`, `table_missing`, `table_unverified`,
  `curves_not_monotone` (an image whose curves would make the byte
  non-monotone; a session then marks its samples
  `input_missing:rom_image:curves_not_monotone` and carries on).
  Real files: k106 / 20260920 / 20261004 resolve (plain sha256 e01cc0cb… /
  a46baa24… / 5587f7e1…), `0xCB29` 16…144 / 16…144 / 16…255, K 28,715 in all
  three; ZS44A-uprev-base resolves too (K 27,089); k105, osiris-initial and
  ZS04C-stock refuse `no_surviving_image`. Tests: `tests/test_fuel_comp_rom.py`
  (22: a synthetic image or map for each refusal -- revision_unknown,
  no_surviving_image, image_not_found, image_hash_mismatch, image_unreadable,
  wrong_program by literal and by calibration ID, table_missing for a table
  id and for the Fuel Comp kind, table_unverified for a curve and for the load
  axis, curves_not_monotone; the three revisions and three refusals on the
  private files, skipping by file name when absent;
  `tests/test_fuel_comp_samples.py` runs the real resolver over a falling
  curve inside a session),
  `tests/test_rom_map.py` (+4: placement, constant schema, overlap/id clash,
  program-literal mismatch).

- [x] **N02.2 — Firmware-faithful reconstruction of `0xFFFF118A`.**
  Pure functions at the decode layer, beside `ecm_lookup`, reproducing:
  - `0x26D58`/`0x27154`: the 32-point search, with interpolation through
    `interpolate_q16`'s semantics;
  - `0x26C28`: the 16-entry `x >> 12` lookup;
  - `w116A = 0xDC1D00 / clamp16(158 × rpm word)`, rpm word at 12.5 rpm per count;
  - `X = (w116A·A) >> 9 + (w116A·w139E) >> 11`, clamped (skip the second term
    if the first saturates);
  - the `0x3C00` split.
  Inputs:
  - `w2CC8` from PID `0x11` (`% × 321/100 × 255/100`, rounding declared), or
    from DID `0x120F` raw counts minus a per-session closed-position estimate,
    taken from closed-throttle idle samples (method declared);
  - purge from PID `0x2E` or DID `0x1110` (`× 1.125` per the firmware doc),
    else unknown;
  - idle air as a declared interval, so each byte is reported as an interval.
  **Accept:**
  - Known answer: on the ram-study capture's steady points, the reconstructed
    byte matches the logged one. Report median ratio, spread and n; canary-60's
    listing pass got 1.000 at idle (n = 38) and 1.027 off idle (n = 22).
  - Negative controls must fail that test: wrong rpm power, wrong throttle
    scaling, 0x120F instead of 0x11 without the closed-position correction.
  - Where a session has both PID `0x11` and DID `0x120F`, the two input routes
    agree within PID quantisation; otherwise the DID route stays unvalidated
    and the capture that would close it is named.
  - Every sample whose cell assignment depends on unknown purge or idle air
    is flagged, not guessed.
  - canary-60's `chain.py` (copied to the orchestrator's scratchpad) is a
    reference only. Nothing depends on it.
  **Done 2026-10-04.** `src/decode/ecm_lookup.py` gains `lookup_curve_q16`
  (0x26D58/0x27154) and `lookup_shift12_q16` (0x26C28; `KERNEL_VERSION`
  unchanged, no existing arithmetic moved); `src/decode/fuel_comp_load.py`
  holds the chain (`rpm_reciprocal`, `throttle_area`, `effective_area`,
  `load_x`, `load_byte`, `reconstruct`) and `reconstruct_interval`, exact
  because `LoadInputCurves` refuses curves that would make the byte
  non-monotone. Bit-identical to the reference chain on 200,000 random inputs
  (checked once, offline; nothing imports it). Inputs
  (`src/analysis/fuel_comp_inputs.py`, constants in `fuel_comp_fit.yaml`):
  PID 0x11 → the count interval `ceil(321A/100)…ceil(321(A+1)/100)−1`
  (truncating handler 0xD41E0; 3–4 counts, exhaustively tested); DID 0x120F
  raw counts (catalog decode undone, whole-count checked) minus a per-session
  closed position = mode of the warm-idle raw count minus the declared
  warm-idle opening 23–32 counts (PID bytes 7–9); purge from PID 0x2E (exact
  below its A = 255 clamp, bytes 200–255 at the clamp) or DID 0x1110, used only
  when a read pairs within the route's separation (commands change by tens of %
  between 15 s polls), else every value the code can produce (0–286); idle air
  the declared
  interval 70–250 (nominal 115). Known answer (`known_answer`, on the committed
  slice `tests/fixtures/fuel_comp/_capture-ram-study-2026-10-04T204557`,
  ZS44A-20260920 read from the private image): idle n = 38, median ratio
  1.000, p10–p90 0.960–1.025, 38/38 inside the interval (median width 45);
  off idle n = 22, median 1.026, p10–p90 0.870–1.148, 11/22 inside (median
  width 14; the inputs are read up to 2.5 s from the byte: no PID 0x11 read is
  within 250 ms of any of the 192 bytes, one rpm read is). Negative controls
  all fail it (idle / off-idle median): rpm^0.5 1.019/0.664, rpm^1.5
  0.980/1.576, 2.55 counts/byte 1.293/1.310, 4.0 counts/byte 0.794/0.784,
  0x120F uncorrected (+96 counts) 0.267/0.559. **The known answer is a coarse
  screen, not proof of exact arithmetic.** Its pass rule, |median − 1| ≤ 0.05
  on at least 20 idle and 15 off-idle points, is what this capture can
  resolve: 0.05 is the smallest whole percent holding the decoded chain's 95 %
  bootstrap interval for both medians (idle 0.980–1.009, off idle
  0.974–1.049), chosen after seeing the decoded result; an idle byte is about
  50, so the idle median moves in 2 % steps. The 0xDC1D00 numerator scaled by
  0.98–1.04 still passes (×0.97 and ×1.05 fail), and only 11 of 22 off-idle
  bytes fall inside their interval. The exact arithmetic is established by the
  disassembly line-check (each routine and constant cited by address in
  `fuel_comp_load.py` and `rom_map.yaml`) and the bit-identical reference
  comparison, not by this data. Both limits are pinned in
  `test_the_known_answer_is_a_coarse_screen_not_proof_of_exact_arithmetic`,
  and the tolerance's provenance is in `fuel_comp_fit.yaml`.
  DID route: no session checked has PID 0x11 and DID 0x120F within 250 ms (0
  pairs on six plain sessions with both, 2026-08-03…10-04), so it stays
  unvalidated
  (`validated: false`, carried on every sample and session); steady brackets
  put w4C72 at 98–104 (2026-09-20T152758), 98–100 (2026-09-23T003917) and
  93–100 (ram-study), the range the estimator itself gives on the tuning
  captures (93–102 on `_capture-tuning-2026-09-23T142926`); no session has
  enough warm-idle DID reads and PID brackets to compare the two on one key
  cycle. Closing capture (declared): PID 0x11 and DID 0x120F back to back in
  one cycle at warm idle and a slow sweep, or one memory window over
  0xFFFF2B0C/2CCE/4C72. Ambiguity: each sample carries `cell_ambiguous` and
  `idle_air_moves_cell` / `purge_moves_cell` / `throttle_moves_cell`. Tests:
  `tests/test_fuel_comp_load.py` (24, synthetic tables: edges, clamps,
  truncation, the 0x3C00 split, saturation, monotone-interval proof by
  enumeration), `tests/test_fuel_comp_inputs.py` (22: declaration refusals,
  input routes, the known answer, its coarseness, and 5 controls at their
  re-run medians, private-ROM skip by name).

- [x] **N02.3 — Per-sample fuelling error, with every sample accounted for.**
  For each session on a resolvable revision, assemble acquisition-time pairs
  (`match_ordered`, declared separation, 250 ms to start) of rpm, throttle
  input, purge, coolant, baro, fuel status, commanded φ, λ (S1, plus S5 where
  logged) and STFT/LTFT B1/B2.
  - **Error:** fuel needed relative to fuel delivered, positive = lean.
    - Closed loop: `derived:TOTAL_TRIM` (bank mean, banks also reported).
    - Open loop: λ_meas/λ_target − 1 from `delivery_samples`, reusing B06's
      states and delay.
  - **Gates, declared in profile data, never derived from residual size:**
    - warm coolant;
    - home baro band (default 76–84 kPa; altitude matters because the load
      byte has no baro input);
    - steady rpm and throttle over a dwell;
    - no fuel cut or its holdoff;
    - no pedal-enrichment transitions.
  - **States:** every candidate sample ends in exactly one named state:
    eligible, ungated-out (with the gate's name), unpaired, or input missing.
  **Accept:** counts by state and gate per session. An empty result says which
  kind of empty it is ("never captured" vs "filtered"). Known answers: B06's
  delivery fixture still gives its committed numbers through this path.
  **Done 2026-10-04.** `src/analysis/fuel_comp_samples.py` `session_samples`
  (CLI `tools/fuel_comp_samples.py`): one row per candidate (closed loop: each
  STFT B1 read; open loop: each `delivery_samples` row), columns per
  `COLUMNS` (`SAMPLES_VERSION` 2: session, revision and image sha per sample,
  rpm word, throttle route and its validation, throttle and purge intervals,
  byte interval, Fuel Comp row and columns, ambiguity flags, coolant, baro,
  fuel status, commanded φ, λ B1/B2, trims, total trims, error per bank and
  bank mean, B06 state per bank, the pairing separation used), states
  `eligible` / `gated:<gate>` / `unpaired:<input>` / `input_missing:<input>`,
  revision resolved per sample (a refusal is `input_missing:rom_image:<reason>`).
  Empty routes say which kind: `never_captured` (an input the session never
  recorded), `unresolvable` (only the image or the closed position is
  missing; more of the same recording will not help) or `filtered`.
  Pairing per route, declared in `fuel_comp_fit.yaml`: closed loop 500 ms
  (operator decision 2026-10-04, option b; default-profile trims sit a median
  375 ms from rpm and throttle), open loop 250 ms (B06's batch; its redesign is
  the next dispatch). rpm, throttle, trims, purge and λ pair by `match_ordered`;
  the steadiness window runs from the dwell before the sample forward to its
  latest paired read, so every read a sample is built from lies inside the
  span judged steady. Coolant, baro and fuel status are state gates read as of
  the sample within declared ages (10 s, 60 s, a 5 s bracket), as
  `target_phi_twin.yaml` does. A clock that cannot carry the separation is
  named in the reason. Gates (all in `fuel_comp_fit.yaml`): cold < 80 °C, baro
  outside 76–84 kPa, post-fuel-cut (B06's closed-loop 15 s), not closed loop /
  open loop within 10 s (closed route), not open loop (open route), steadiness
  per route (2 s, 150 rpm, 8 counts; open loop 0.5 s, 100 rpm). Bank 2: PID
  0x28 is now a declared measured source (`ecm_lambda_pid_0x28`), judged on the
  same B06 rows; the open-loop error is the bank mean where both banks carry a
  residual, else bank 1's. B06 fixture through this path: RAM route 1,078
  rows, fuel_cut 151, measured_rail 19, measured_missing 1, post_fuel_cut 316,
  target_transient 31, eligible 560 and the 8 enrichment residuals; PID 0x44
  route eligible 229 (`tests/test_fuel_comp_samples.py`, 17 tests: each gate,
  unpaired vs clock, the forward window, never-captured / unresolvable /
  filtered, a flash splitting a session, a refused image mid-session, the
  throttle-route split, the bank-2 mean, ambiguity flags). `summary.json`
  (`eligible_summary`) splits every revision and route's eligible and
  unambiguous counts by throttle route with its validation, so the DID-route
  share cannot be averaged away. Corpus (112 sessions ≥ 2026-08-03, declared
  pairing; no image refused, no session straddles a flash per sample,
  2026-08-03T163440 is k106 throughout):

  | Revision / route | Candidates | Eligible | Unambiguous cell | PID 0x11 (validated) | DID 0x120F (unvalidated) |
  |---|---|---|---|---|---|
  | 20260920 closed loop | 17,870 | 1,588 | 94 | 680 (54) | 908 (40) |
  | 20260920 open loop | 35,334 | 86 | 80 | 0 | 86 (80) |
  | k106 closed loop | 10,566 | 1,719 | 381 | 1,719 (381) | 0 |
  | k106 open loop | 21,812 | 0 | 0 | 0 | 0 |

  **SAMPLES_VERSION 3 (N02.5, 2026-10-07; open loop as an interval over the
  delay bracket, RAM phi target preferred over PID 0x44):** closed loop
  unchanged (20260920 17,870 / 1,588 / 94, PID 680 (54), DID 908 (40); k106
  10,566 / 1,719 / 381, all PID); open loop 20260920 13,479 candidates / 42
  eligible / 28 unambiguous, all DID route, all RAM target; k106 open loop
  18,762 / 0 / 0. The v2 table above is kept as the record of that version.

  (At 250 ms with a backward-only window the same corpus gave closed loop
  1,070 / 137 eligible, 939 / 131 DID / PID on 20260920, and open loop 0.)
  Every eligible open-loop sample is from a September tuning capture's
  unvalidated DID route. Top closed-loop removals: 20260920 post_fuel_cut
  5,515, throttle_transient 2,597, not_closed_loop 1,486, unpaired PID 0x11
  1,442, unpaired DID 1,141; k106 not_closed_loop 2,592, baro never captured
  1,828, unpaired LTFT1 1,159, unpaired rpm 916. Of 2,180 B06-eligible
  enrichment rows: DID unpaired 788, not open loop 650, coolant never captured
  331, PID 0x11 unpaired 158, throttle never captured 101, baro never captured
  33. Eligible closed-loop samples are mostly warm idle (median 762 rpm),
  which is why so few sit on an unambiguous cell. Per session: closed loop 44
  with eligible samples, 29 filtered, 38 never captured, 1 unresolvable
  (`_capture-tuning-2026-09-23T022831`, no warm idle for the closed position);
  open loop 5 / 68 / 38 / 1.

- [x] **N02.4 — Separate fuel-supply droop from the air-model error.**
  Model injector flow ∝ √(rail gauge pressure), relative to the drive's own
  low-flow reference.
  - **Measured:** on drives that logged `FUEL_PRESSURE`, apply the measured
    droop per sample.
  - **Modelled:** elsewhere, use the droop-versus-MAF relation fitted on those
    drives, labelled as modelled with its fit and n.
  - Ignore manifold pressure (the vehicle has no MAP sensor). State that as a
    limitation: rail-to-manifold ΔP is not modelled.
  **Accept:**
  - Each eligible sample carries total error, droop component (measured,
    modelled or none) and remainder.
  - The 10-04 drive reproduces the recorded −1.9 psi at about 107 g/s.
  - The fuel pump is being replaced (supercharger build), so the remainder is
    the part a Fuel Comp edit should own.
  **Done 2026-10-05.** `src/analysis/fuel_comp_droop.py` (declaration
  `droop:` in `fuel_comp_fit.yaml`, schema `DroopRules`): pressure is the mean
  of `sensor:FUEL_PRESSURE` reads within +-0.25 s of the sample (>= 30 reads,
  no `FUEL_PRESSURE_FAULT` row, which is read as a state from the stored rows),
  against each drive's own reference (median window pressure over
  engine-running MAF reads < 5 g/s, >= 30 reads, pooled over the drive as
  `fit.drive_unit` defines it). Flow factor
  `sqrt(P/P_ref)`; droop error `sqrt(P_ref/P) - 1`; remainder
  `(1+error)/(1+droop) - 1`. Elsewhere `droop_psi = a maf + b maf^2` fitted on
  every engine-running MAF read of the drives with pressure and a reference
  (>= 200 pairs, >= 2 drives), applied from 0 to the top of its fitted MAF
  range (zero at zero flow by construction; the report shows fitted and
  applied ranges), labelled
  `modelled (<why not measured>)`; else `none` with the reason. Each sample
  carries `droop_kind`, `droop_basis`, pressure, reference, droop psi, flow
  factor, droop error and remainder. Rail-to-manifold dP, the +1.8 psi
  analog offset and the Oct-4-only pump history are required `limitations`
  printed in the report. Known answer (real session, through this path):
  2026-10-04T194342, 100-118 g/s, **median -1.925 psi at median 106.4 g/s,
  n = 7** (p10-p90 -1.96..-1.71; flow -1.91 %), recorded -1.9 (8 pairs):
  passes the declared +-0.25 psi. With the drive unit (2026-10-07 fix-up)
  190056 and 194342 are one drive (1.2 min apart) and share its reference
  (50.79 psi). Corpus model (run5): a = -0.00716 psi/(g/s), b = -8.08e-5,
  n = 3,162 pairs from two drives (190056..194342 and 202737..ram-study),
  fitted on MAF 3.3-142.9 g/s, applied 0-142.9, pooled reference 50.62 psi,
  residual RMS 0.22 psi. 215234 is KOEO (rpm 0) and forms no reference.
  Eligible samples: 143 measured, 3,201 modelled, 5 none. The model is
  fitted once, not per held-out fold; disclosed in the report: 0 modelled
  samples sit on a pressure drive, so nothing the model saw is predicted by
  it in the validation. Tests:
  `tests/test_fuel_comp_droop.py` (9: reference, measured decomposition
  multiplicative to 1e-12, modelled fit and labels, fault fallback, one drive
  refused, no-reference kinds, known-answer empties, fault rows as states,
  and the real-session reproduction, `slow`, skipping by name when absent;
  +1 for sessions of one drive sharing a pooled reference).

- [x] **N02.5 — Per-cell proposal on the current (20261004) axes.**
  For each eligible sample:
  - FC_applied is the bilinear value from the table and axes in force when it
    was logged.
  - The required factor is `FC_applied × (1 + remainder) × K_logged / K_now`.
  - Fit the 20261004 table's cells to the required factors by weighted least
    squares, with the bilinear weights on the 20261004 axes as the design
    matrix (sparse, exact for a linear interpolator).
  - Uncertainty: drive-level bootstrap.
  **Refusals (declared):**
  - fewer than a minimum of weighted pairs;
  - fewer than two drives;
  - samples whose cell is ambiguous because of idle-air or purge intervals.
  **Validation:**
  - Held-out drives: the fitted table must reduce held-out error, else
    report "no improvement".
  - Diagnostic: compare held-out error explained by Fuel Comp cells with error
    explained by MAF g/s bins alone. If MAF bins explain it as well or better,
    the error is a MAF-scaling shape and Fuel Comp is the wrong table; say so.
  **Accept:** each of the 256 cells is proposed (with factor and CI),
  insufficient (with n and drives), or not covered. Synthetic tests recover a
  known injected cell pattern and refuse sparse cells.
  **Done 2026-10-05.** `src/analysis/fuel_comp_fit.py`. FC_applied is
  `ecm_lookup.lookup_2d` on the logged revision's table and axes at the
  nominal word and byte (throttle/purge interval lower-middle, idle air
  nominal, byte rebuilt on that revision's curves; the corner range is
  carried); required = `FC_applied x (1 + remainder) x K_logged / K_now`; the
  design is `(sample, cell, weight)` bilinear triplets on the 20261004 axes
  (byte rebuilt on the target's curves). Least squares on cells passing the
  declared refusals (>= 10 weighted pairs, >= 2 drives each with >= 1 weighted
  pair), others held at current, ridge of 1 pair toward current; 500-resample
  drive bootstrap (seed declared). Sample refusals by name: droop none,
  `cell_ambiguous: <idle_air|purge|throttle|rpm>` on the logged axes,
  `target_cell_ambiguous`, inputs missing. DID route excluded by the declared
  switch; `fit_both_ways` reports both, with each cell's DID-route weight
  share. Validation: leave-one-drive-out, current table vs fitted cells vs
  MAF-bin multipliers on the same held-out samples; verdict
  `rms_and_drive_majority`; diagnostic says "the error is MAF-shaped; Fuel
  Comp is the wrong table" when bins remove as much MSE. **Open-loop redesign
  (orchestrator-approved):** no steadiness gate; rpm and throttle are the
  hull of reads bounding the B06 enrichment delay bracket
  (`ram_final_target`, 0.0-0.5 s, read from lambda_delivery.yaml), consecutive
  reads <= 0.6 s apart (declared from the 0.25 s / 0.5 s cadence), `rpm_moves_cell`
  when rows span more than a pair; a stopped engine inside the span is
  `gated:engine_stopped_in_delay_bracket` (SAMPLES_VERSION 3, also `maf_gs`,
  `bfs_raw`, rpm word/row intervals). RAM phi target now preferred over PID
  0x44, whose caveat (46B2 feedback target) is a required `source_caveats`
  entry. Tests: `tests/test_fuel_comp_fit.py` (injected 5-cell pattern
  recovered within 0.5 raw with resolved CIs; sparse cells refused naming
  `min_weighted_pairs` / `min_drives`; DID route flagged; cell-shaped error ->
  improvement; MAF-shaped error -> MAF-shaped; no pattern -> no improvement;
  required factor and every refusal through `prepare_fit_samples`),
  `tests/test_fuel_comp_samples.py` (+6: bracket interval, pull placed not
  gated, unbounded bracket named, stopped engine, MAF/BFS ride along, RAM
  preference and caveat).

  **Fix-up 2026-10-07 (verifier).** The first record called the error
  "MAF-shaped; Fuel Comp is the wrong table". **That is retracted**: it
  compared bare RMS with `>=`, never asked whether a single global factor did
  as well, and counted back-to-back sessions of one outing as independent
  drives. Now:
  - `fit.drive_unit`: sessions closer than 5 min are one drive (the corpus
    gaps are <= 2.7 min or >= 11.6 min, nothing between; evidence in the
    YAML). Used by the cell refusals, the bootstrap, the folds, and droop's
    references. `tests/test_fuel_comp_fit.py`
    `test_sessions_of_one_drive_count_once`,
    `test_drives_merge_sessions_closer_than_the_declared_gap`.
  - `validate` scores four models on the same samples and folds (current
    table, one global factor, MAF bins, fitted cells), each with held-out
    RMS, mean signed residual, per-drive better/worse/same, and parameters
    per fold (active bins, free cells). Models are told apart by a paired
    drive bootstrap of pooled MSE (`fit.model_comparison`, 2,000 resamples,
    95 %); a tie is never a win. MAF-shaped only when the bins beat the global
    factor; cell-shaped only when the cells do; "uniform offset (K / MAF
    scale)" when the global factor beats the current table and neither does.
    `test_a_uniform_offset_is_not_called_maf_shaped` (synthetic -1.7 %
    everywhere: uniform offset, not MAF-shaped).
  - Disclosed: the fit and validation solve on real-valued bilinear weights,
    which differ from the firmware's truncating lookup by -1.42..+1.81 raw
    (median +0.42) on the current table at the 414 usable samples; the
    knock-on uses the firmware lookup. Every profile file read is recorded
    with its sha256 and whether it differs from HEAD; `lambda_delivery.yaml`
    (27a86461...) and `afr_calibration.yaml` (f9632bcb...) were pinned copies
    of another session's uncommitted B06.7 working files, `dids.yaml`
    (a597b079...) equal to HEAD.

  **Corpus (run5, scratchpad n02fit/run5; 112 sessions >= 2026-08-03 form
  24 drives under the drive unit (18 of them merged from 106 sessions)).** DID route excluded (declared): **8
  proposed, 33 insufficient, 215 not covered** (366 samples, 7 drives, all
  closed loop). Included: 11 / 43 / 202 (414 samples, 9 drives; 24 of the 42
  eligible open-loop samples are usable, all RAM target and DID route; the
  other 18 are refused: cell_ambiguous rpm 9, throttle+rpm 3, throttle 2,
  target_cell_ambiguous 3, droop none (MAF 146.1 g/s above the model's range)
  1; PID 0x44 contributed 0). The earlier "28 placeable" was the count
  unambiguous on the logged axes: of those 28, 24 are usable, 3 are ambiguous
  on the 20261004 axes and 1 has no droop separation.
  - Held-out, excluded (7 folds, 364 samples): RMS current 2.755 %, global
    factor 2.744 %, MAF bins 2.289 %, cells 2.613 %; mean signed residual
    -1.70 / +1.19 / +0.19 / -1.55 %; drives better/worse vs current: global
    5/2, bins 6/1, cells 4/2 (1 same); bins 2-3 active per fold, cells 0-8
    free. Bootstrap: cells < current 0.975, global < current 0.552, bins <
    global 0.906, cells < global 0.650. **Verdict: improvement** (cells beat
    the current table, 4 vs 2 drives). **Diagnostic: neither a global factor,
    MAF bins nor Fuel Comp cells explain held-out error** beyond the current
    table at 95 % (the bins come closest, 0.906 against the global factor).
  - Included (9 folds, 412 samples): current 2.693 %, global 2.313 %, bins
    2.330 %, cells 2.510 %; global < current 1.000, bins < global 0.302,
    cells < global 0.000. **Verdict: no improvement** (cells < current only
    0.869). **Diagnostic: uniform offset (K / MAF scale)**.
  - Largest proposed (excluded), factor and drive-bootstrap CI: (2800, 144)
    -6.4 % [0.921-1.020], 4 drives; (2400, 128) -3.7 % [0.930-0.989], 2
    drives; (2400, 160) -3.1 % [0.979-1.018], 2 drives; (2400, 176) -2.3 %
    [0.996-1.015], 2 drives. With 2 drives a drive bootstrap has 3 distinct
    resamples, so those intervals understate the uncertainty (said in the
    report).
  The honest reading: the evidence does not separate a Fuel Comp shape from a
  uniform offset or from MAF shape; no cell change is supported by more than
  four independent drives.

- [x] **N02.6 — Knock-on prediction and report.**
  For each proposed change, predict at the logged samples:
  - the BFS change (the Fuel Comp ratio);
  - the Target AFR X-byte shift, and whether it crosses a Target AFR
    breakpoint, giving the commanded-φ change (E02 kernel; clamped at
    X ≥ the last breakpoint);
  - the timing-trim cell shift.
  Output: a CLI tool writes JSON plus a markdown report (coverage grid, proposed
  grid, CIs, knock-on, validation, every refusal) to a chosen directory.
  **Accept:** the report names every input's revision and image hash, the gates
  and pairing, and the reconstruction's known-answer result. No code path
  writes or exports a ROM. A proposal cannot be mistaken for a measured
  outcome.
  **Done 2026-10-05.** `src/analysis/fuel_comp_knock_on.py`: at every usable
  sample a changed (rounded) cell touches, FC now/proposed by `lookup_2d` on
  the 20261004 axes, BFS ratio, X before/after via `target_phi.x_from_bfs`
  (Load Point Scaler 44,019 from the image), Target AFR breakpoint crossing,
  phi before/after via `target_phi.predict` (Target AFR from the 20261004 image
  through `resolve_table`; the twin's blockers final_command_path_unverified /
  ram_copy_unverified are printed), and the timing column on
  `timing_trim_load_axis` (0xCC83, existing rom_map entry). BFS is DID 0x1208
  where paired, else `c x K x FC/128 x MAF/rpm` with c the median per-sample
  ratio (corpus c = 43.93, n = 948, p10-p90 43.26-44.53), labelled; pedal flag
  assumed clear; D8B3 ceiling, filter/transient, HI DET trim selection listed
  as limitations. `src/analysis/fuel_comp_report.py` + `tools/fuel_comp_fit.py`
  write `fit.json`, `report.md` (proposal header, inputs with every revision's
  image sha256, gates and pairing, all state/refusal counts, open-loop counts
  per target route with caveat, both known answers, droop, 16x16 coverage and
  proposal grids both ways, CIs, validation, knock-on table) and Parquet only;
  `write_outputs` refuses any other suffix before writing. Tests: knock-on
  (X 98 -> 102 crosses Target AFR 100, phi equals `predict`, unpaired BFS says
  why), report is a proposal and outputs are JSON/MD/Parquet, other names
  refused, AST check that no N02 module imports `rom_targets`/`osiris` or calls
  write_bytes/serialize/transform/open(..., "wb"), tool writes only via
  `write_outputs`. Corpus (run5, declared configuration): 302 samples, BFS
  modelled 300 / not predicted 2, 21 cross a Target AFR breakpoint, 52 a
  timing column. `c` = 43.93 (n 948) comes mostly from idle DID-route samples
  (idle <= 1,000 rpm 43.93, n 833; DID route 942 of 948); off-idle check 44.01
  (n 115), stated in the report. Load-byte known answer in the report: idle 1.000
  (n 38), off idle 1.026 (n 22), passed.

- [ ] **N02.7 — Run on the corpus; re-run after the 20261004 drive.**
  Orchestrator. Record the result in `data/analyses/` with session names, n per
  cell and the validation verdict. Re-run once the first 20261004 drive lands:
  - if the restore prediction passes, the proposal can inform an edit;
  - if it fails, record why and do not propose.

- [ ] **N02.8 — Choose the throttle route per read, not per session.**
  Found 2026-10-15 by E08.2's verifier. `fuel_comp_samples._throttle_input`
  returns the PID 0x11 route for a whole session as soon as one PID 0x11 read
  exists, and reaches the DID 0x120F route only when PID 0x11 was never
  captured. `_capture-tuning-2026-09-23T164007` has a DID closed-position
  estimate ([94, 103]) yet loses 170 eligible reads (115.5 s) to
  `fill_byte:unpaired:pid_0x11`, so it gives E08 designs no points and N02
  no samples. Fix at source: per read, use PID 0x11 where it is co-observed
  within the pairing tolerance, else the DID, and name the route on each
  sample. `tests/test_sim_design.py::test_pid_0x11_is_used_where_co_observed_and_the_did_elsewhere`
  currently pins the per-session behaviour and changes with it.
  **Accept:** the 09-23T164007 drive yields DID-route samples where PID 0x11
  is absent. Sample counts on the other N02 sessions are reported before and
  after, so any change is visible.

## Not now, and why

- **Target AFR or timing optimisation:** there is no power or knock evidence.
- **Boost:** the load byte cannot see boost. The supercharger tune belongs in
  K, the Load Point Scaler and MAF scaling.
- **Logging idle air, purge and the internal words in new captures:** a
  memory-window recipe exists (firmware doc). It would sharpen N02.2 but is
  not needed for history.
