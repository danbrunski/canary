# E02 — deterministic target-equivalence-ratio twin

Follow [E00](E00-second-brain-README.md) and the parent
[first-slice specification](../ecu-second-brain.md#first-ecm-twin-slice-target-equivalence-ratio).
Serves capability 3 and supplies command predictions to capability 5. The
required runtime is the Pi; development can use stored sessions elsewhere.
This predicts commanded φ from observed lookup inputs, not fuel delivery.

This is the first increment of the [application-code twin](E05-ecu-application-twin.md).
E05.1 records the decoded dependency graph and E05.2–E05.4 extend its upstream
calculations, state transitions and outputs. An observed intermediate is
labelled as an input; agreement downstream cannot validate its computation.

Scope: warm, steady, open-loop fuelling in independently validated rpm/load
regions, with stable map identity and no fuel cut. Qualify ordinary lookup
and the sustained pedal-enrichment override separately. Cold/warm-up,
closed-loop overrides, transients/DFCO recovery, map changes, protection
regimes, boost and other ECU programs remain outside the first evidence.

## Checklist

- [ ] **E02.1 — Declare applicability and typed inputs.**
  Profile data declares raw/decoded units, required windows, warm and
  steady-region bounds, dwell, coverage, mode source, freshness and acquisition
  skew. Inputs are `mem:ECM_FUEL_TARGET_MAP_X_TRACE`,
  `mem:ECM_ENGINE_SPEED_RAM`, `mem:ECM_PEDAL_ENRICH_FLAGS`,
  `mem:ECM_ACTIVE_MAP`, `mem:ECM_UPREV_MAGIC` and
  `mem:ECM_UPREV_MAP_SELECTOR`; reference output is
  `mem:ECM_TARGET_EQUIV_RATIO` plus its sentinel. Use
  `mem:ECM_AIR_CHARGE_SLOT_103A` for the independent X-formula check.
  Declare the coolant source (`ECM_COOLANT_TEMP` DID 0x1101 or
  `obd:COOLANT_TEMP`) and mode evidence such as `obd:FUEL_STATUS` explicitly.
  Derive gates from inputs/mode, never residual size. Preserve raw X counts;
  do not feed its display scale directly to the firmware's raw BFS axis.
  **Accept:** missing gates, wrong units/program, unsupported clocks,
  sentinel, stale input and uncovered region produce named unevaluable
  results. Negative controls expose an X-scale or rpm-decode mistake.

- [ ] **E02.2 — Resolve the exact ROM and active table.**
  Consume W08's library and W05's verified axis definitions. Every prediction
  names plain-image SHA-256, program/profile, active slot, table/axes,
  decoder and evaluator versions. The reference image is
  `a46baa248f16db8d5ed9854913e614dd7d77b7579bb6c36bddb792741531be50`
  (`ZS44A-20260920`). For this program, verify default slot 1 when active-map
  reads 0 with valid magic and cold-boot provenance; refuse unresolved state.
  Compare the active RAM Target AFR bytes with the selected library slot
  before qualifying live use. Keep ROM images/disassembly outside Git.
  **Accept:** a wrong hash, missing table, unverified mapping or ambiguous
  slot refuses by name. A tool-produced RAM/ROM comparison establishes byte
  identity. **Vehicle required** for this read if no recorded evidence exists.

- [ ] **E02.3 — Preserve the October 4 known-answer fixture.**
  Audit existing fixtures first and reuse any newly committed ROM-trace slice
  that satisfies this contract. Source:
  `_capture-ram-study-2026-10-04T204557`, 20:45:56–21:12:49Z. Commit raw
  telemetry, exact retained times, content hashes, channel/decode versions,
  image hash, gates and pairing policy under `tests/fixtures/ground_truth/`
  (or reference an equivalent existing fixture without copying it).
  Retain the two enrichment observations and neighbours, ordinary lookup
  coverage, and all 194 BFS/X pairs. Run the real evaluator to mint expected
  records. Keep private-ROM checks explicitly separate from committed
  telemetry/lookup-vector regression; a missing local ROM is not a pass.
  **Accept:** the two recorded commands are exactly φ **1.1640625**
  (`0x4A80 / 16384 = 0x95 / 128`); the image-scaled X relation
  `min(255, floor(BFS * 128 / 44019))` reproduces **168/194 exact** and
  **26/194 differing by 1–4 raw counts, both signs**. All retained samples
  have lineage. Absent eligible tuples produce a named capture requirement.

- [ ] **E02.4 — Implement and verify the firmware lookup arithmetic.**
  Reproduce raw axis search, clamping, fixed-point bilinear interpolation
  and rounding. Use the measured pedal-enrichment flag's bit 0 to force the
  last load column; do not reconstruct its threshold/hysteresis yet. Trace
  the remaining final-command writers to establish applicability before
  calling the lookup a final-command prediction. Share one pure kernel
  between streaming and stored-session adapters at the appropriate layer,
  and make it the same axis-search/interpolation function W05.1 uses for
  the ROM Maps ECM cell (`src/analysis/rom_trace.py`), never a second copy.
  The RAM table at `0xFFFFB260` is 64 bytes and this ECM caps a `0x23` read
  at 63 bytes (64 draws NRC `0x12`), so E02.2's comparison needs two reads.
  **Accept:** independent known lookup vectors cover both branches, interior
  cells, edges and rounding; E02.3 reproduces. Every eligible steady pair
  must meet the proposed **one Q14 count (1/16384 φ)** target, with nonempty
  ordinary and override coverage. This accuracy is a requirement to verify,
  not a result already measured. Insufficient coverage stays partial.

- [ ] **E02.5 — Pair predictions and account for every sample.**
  Emit sample/time/input references, skew, applicability/reason, lookup
  cells/weights, predicted/observed φ and `observed_phi - predicted_phi`.
  Use acquisition-time pairing through the shared comparison machinery;
  initial maximum skew is 250 ms, never a carried-forward five-second RAM
  value. Inspect differences and ratios; use exact-value checks for constant
  branches where regression is unresolvable. Keep same-read firmware update
  timing distinct from acquisition skew.
  **Accept:** all candidates partition into pending, unmatched/unevaluable,
  eligible and excluded states without duplicate target use. Steady
  passes/misses and transient differences/misses are reported separately.
  The 26 known X misses cannot disappear from accounting or justify a wider
  φ tolerance. Missing/skewed/sentinel samples have null qualified residuals.

- [ ] **E02.6 — Publish qualified evidence and command-change predictions.**
  Persist through E01; expose through E04. Explain positive φ residual as a
  richer ECM command than the reproduced logic, with omitted logic, timing
  and provenance among possible causes. Link command residuals to the
  investigation without relabelling them as delivery error or a fault.
  For a proposed ROM change, record an exact-image/cell command prediction
  before the intervention, conditional on stated inputs and region.
  **Accept:** cards resolve every value and image reference; unavailable
  coverage remains unevaluable; a zero residual makes no engine-health claim.
  A what-if record cannot be mistaken for measured output or confirmed
  outcome, and creates no ECU write request.

- [ ] **E02.7 — Qualify missing regions with targeted measurements.**
  First close E02.2's RAM-copy check and E02.4's arithmetic/applicability
  gaps. If October 4 lacks steady open-loop tuples, capture the declared
  channels at sufficient cadence across ordinary cells and sustained
  enrichment, preserving transition neighbours and an independent drive.
  Record coverage before and after; qualify only cells/regimes supported by
  evidence. **Vehicle required.**
  **Accept:** a tool report names each qualified branch/region, pair count,
  timing distribution and pass/miss count under frozen gates. Remaining gaps
  name the next measurement. E04.3 must also pass before live enablement.

## Follow-on logic, excluded from this slice

E05 owns implementation beyond the first slice. These measurements do not
broaden E02's applicability until their corresponding paths qualify:

| Unresolved logic | Next evidence |
|---|---|
| Cold/warm-up, closed-loop overrides and protection logic | Trace command writers/flags; capture cold-to-warm and mode changes with those inputs. |
| Transient enrichment and DFCO entry/recovery | Faster paired target/input/flag capture through tip-in, lift-off and recovery; preserve internal-update and acquisition uncertainty. |
| Pedal threshold and hysteresis | Trace the input of curve `0xCEDB`; repeat threshold crossings while observing pedal, flag and command. |
| Map switching | Observe an operator-selected switch with `ECM_MAP_SELECT_INPUT`, selector and active map; compare RAM table with library slot before/after. |
| Full BFS, Fuel Comp X and Load Point Scaler source | Trace callers and capture MAF/rpm/BFS, `ECM_FUEL_COMP_X_INPUT` and X; use same-instant ratios and neighbouring-byte controls. |
| Timing trims, slot `+0x260`, unnamed curves/maps | Identify each reader/output first; capture timing flags and independent timing/cam/throttle references appropriate to the candidate. |

Findings and confidence belong in profile data and
[firmware analysis](../../ecu-firmware-analysis.md), with evidence links.
Do not declare any of these decoded from correlation alone.
