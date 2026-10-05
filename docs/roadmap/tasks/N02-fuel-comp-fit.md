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

- [ ] **N02.1 — ROM definitions and a hash-checked image resolver.**
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

- [ ] **N02.2 — Firmware-faithful reconstruction of `0xFFFF118A`.**
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

- [ ] **N02.3 — Per-sample fuelling error, with every sample accounted for.**
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

- [ ] **N02.4 — Separate fuel-supply droop from the air-model error.**
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

- [ ] **N02.5 — Per-cell proposal on the current (20261004) axes.**
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

- [ ] **N02.6 — Knock-on prediction and report.**
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

- [ ] **N02.7 — Run on the corpus; re-run after the 20261004 drive.**
  Orchestrator. Record the result in `data/analyses/` with session names, n per
  cell and the validation verdict. Re-run once the first 20261004 drive lands:
  - if the restore prediction passes, the proposal can inform an edit;
  - if it fails, record why and do not propose.

## Not now, and why

- **Target AFR or timing optimisation:** there is no power or knock evidence.
- **Boost:** the load byte cannot see boost. The supercharger tune belongs in
  K, the Load Point Scaler and MAF scaling.
- **Logging idle air, purge and the internal words in new captures:** a
  memory-window recipe exists (firmware doc). It would sharpen N02.2 but is
  not needed for history.
