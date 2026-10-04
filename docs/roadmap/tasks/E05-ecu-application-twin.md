# E05 — expand the twin from decoded ECU application code

Follow [E00](E00-second-brain-README.md). Operator clarification, 2026-10-04:
the aim is a **digital twin of the ECU running on the Pi**, using the recent
application-code findings to predict and verify observations. E02's target-φ
calculation is the first executable slice. This checklist extends it into
connected, stateful ECU computations, each qualified against the real ECU.

Start from [ECM firmware analysis](../../ecu-firmware-analysis.md), its
live-verification section and W08's exact image. Reproduce decoded arithmetic,
control flow and state transitions as versioned executable models. The exact
MCU/peripheral behavior and substantial application logic remain unknown;
full instruction/peripheral emulation is not a prerequisite for the first
slices. A claimed output must name the code path it reproduces and what
still comes from observed RAM. Never present partial coverage as a complete
ECU replica.

## Findings the implementation must carry forward

| Application-code finding | Twin consequence |
|---|---|
| Target function `0x055DB2` calls `0x0269AC`; helpers implement raw rpm/load search, bilinear interpolation and clamping | Reproduce actual integer semantics and call dependencies, not a generic interpolation chosen for convenience. |
| Nissan's Target AFR pointer is redirected to RAM `0xFFFFB260`, slot `+0x220` | Use the UpRev active RAM copy's matching ROM slot; the old calibration table at `0x00A169` does not drive this patched path. |
| UpRev init/copy/selector routines `0x0DE1A4`, `0x0DE3E4`, `0x0DE358` | Model default slot 1, magic, selector debounce and committed slot separately; active-map 0 after cold boot is meaningful state. |
| Pedal flag at `0xFFFF2841` bit 0 forces X=`0xFF`; curve `0xCEDB` and +8-count hysteresis govern it | First consume the observed flag; later predict its transitions after identifying its input and update order. |
| X derives from BFS using Load Point Scaler; Fuel Comp is slot `+0x660`, K is `+0x762` | Extend upstream from observed X into computed BFS/air charge only after tracing the full arithmetic and resolving Fuel Comp X and scaler source. |
| Timing-trim tables at `+0x020`/`+0x120`, cells masked `& 0x7F`, selected by a flag at `0xFFFF2842` | Table locations are verified; Main/High Det meaning and the final timing chain still need evidence. |
| Slot `+0x260`, many 1-D curves and flash-resident maps remain unidentified | Track unknown functions and the measurement needed to identify them; do not guess cam/throttle/torque semantics from shape. |
| CAN and K-line Mode 22 dispatch to different RAM pointer tables | Observation mappings must include transport. Apply catalog corrections at their owner; stale trim labels cannot validate the twin. |
| PID 0x44 and target RAM read the same `0xFFFF26BE` word | Their agreement verifies observation mapping, not two independent confirmations of the command algorithm. |
| Same-read BFS/X differed by 1–4 counts on 26/194 transient pairs | Acquisition simultaneity does not imply atomic firmware updates; state/update timing is part of fidelity and error accounting. |

## Checklist

- [ ] **E05.1 — Build a versioned application model and coverage manifest.**
  Trace each proposed output backwards through writers, callers, RAM state,
  flags and ROM tables. Record function/address references, integer widths,
  signedness, rounding/saturation, branch conditions, state/update order,
  observed versus predicted dependencies, confidence and evidence ids.
  Identify compatibility using verified program/patch identity plus full
  image hash for calibration; never use calibration ID alone. Keep Nissan
  and UpRev disassembly/decompiled output local; commit behavioral contracts,
  profile metadata, our implementation and evidence references.
  **Accept:** the target-φ dependency graph resolves to E02 inputs and W08
  tables; every undecoded dependency marks affected outputs/regions as
  conditional or unevaluable. Wrong-program artifacts refuse by name.
  The dashboard can report qualified outputs and uncovered logic from this
  manifest. Backfill E02 with it without changing established predictions.

- [ ] **E05.2 — Extend upstream through air charge and fuelling.**
  Trace `0x03C4D0`, K's read at `0x03C6E0` and Fuel Comp readers including
  `0x02D5F4`. Resolve the exact Volumetric Flow Ratio input (code reading, 2026-10-04: probably
  axis `0xCB29` against RAM `0xFFFF118A`, with `0xCACD` the rpm axis, the reverse of
  UpRev's labels; inferred until a live check), accumulator units,
  all corrections and which Load Point Scaler copy is consumed. Build the
  dependency chain toward BFS → X → target φ from independently observed
  sensor inputs, retaining intermediate RAM observations for verification.
  **Accept:** each new intermediate reproduces known vectors and eligible
  independent-drive observations under predeclared tolerances and pairing;
  controls using wrong neighbouring bytes/scales fail. Feeding observed BFS
  may validate downstream logic but cannot count as predicting BFS. Unknown
  upstream dependencies remain visibly conditional. **Capture likely needed.**

- [ ] **E05.3 — Reproduce state transitions and additional fuel regimes.**
  Decode and implement pedal enrichment hysteresis, map selection/init/copy,
  and then individually identified warm-up, closed-loop, transient and
  fuel-cut command overrides. Model time/state initialization explicitly:
  mid-drive attachment can use an observed state checkpoint with provenance;
  missing history must not silently assume cold boot or steady state.
  Measure transition/update timing rather than aligning each test trace to
  minimize its error. Keep the first slice's exclusions until each addition
  qualifies separately.
  **Accept:** held-out transition sequences reproduce predicted flags,
  branch/slot and command with frozen value/time tolerances. Reset/gap and
  map-switch cases become unevaluable until required state is known. Both
  transition misses and excluded samples remain counted. **Vehicle required**
  for unobserved map switching and new regimes.

- [ ] **E05.4 — Add ignition and later identified outputs incrementally.**
  Start by tracing both timing-trim readers through the final ignition
  command, including table selection, masking, corrections and clamps. Keep
  the Main/High Det labels inferred until observed. Investigate slot `+0x260`
  and unnamed curves/maps only through identified readers and independently
  observed quantities; record a capture recipe once a candidate is testable.
  **Accept:** each added output has its own known-answer fixture, dependency
  manifest, input/output units, eligible regions, tolerances and independent
  validation. An isolated trim-table prediction is labelled an intermediate,
  not final ignition timing. Unidentified maps cannot emit qualified
  physical predictions. **New capture required where evidence is absent.**

- [ ] **E05.5 — Verify connected computations on the Pi.**
  Run each qualified increment through E02's per-sample accounting, E01
  evidence, E03 investigation and E04 parity/budget gates. Freeze predicted
  outputs before examining their corresponding target observations; prohibit
  using an output or its alias as an input to the path being validated.
  Compare intermediate and final residuals to locate the earliest supported
  disagreement without claiming a unique cause from it. Preserve program,
  image, model version, observed state and sampling uncertainty per result.
  **Accept:** a Pi trace shows input → predicted intermediate → predicted
  command → observed command, with evidence at each boundary. An intentional
  wrong-table/branch control is detected, shared-register aliases do not
  inflate evidence, and integrated load still passes E04.3. Broader coverage
  never silently changes previously qualified behavior.
