# B08 — behavior models informed by decoded ECU logic

Recorded 2026-10-04 from the [ECM application-code findings](../../ecu-firmware-analysis.md)
and the operator discussion about using them to improve behavior models and
the second brain. Follow [B00](B00-behavior-models-README.md) for validation
and publication, and [E00](E00-second-brain-README.md) for the Pi runtime and
evidence contracts. These are candidates and implementation tasks, not
measured model improvements or permission to refit/deploy unqualified models.

## Findings and implications

| Finding established by the code analysis | Proposed behavior-model use | Qualification still needed |
|---|---|---|
| `mem:ECM_FUEL_COMP_X_INPUT` (`0xFFFF118A`) is a throttle-derived, saturating fill estimate (alpha-N). BFS/Target AFR's X follow the MAF path. | Extend `air_charge_vs_rpm_throttle_did` with the actual fill coordinate and compare throttle-derived fill with MAF-derived charge under matched conditions. | The relative scale is established (0–224); absolute physical units remain open. Purge `w139E`, sensor-B fallback `w2CD0` and idle-air correction structure `w63FC` are now traced; full-chain live verification and branch coverage remain. This is a differently derived estimate, not an independent measured airflow sensor. |
| Fuel Comp uses rpm axis `0xCACD` and load axis `0xCB29`; UpRev labels the breakpoint lists the other way round. The grid is row-major, not transposed. | Compare rpm × fill-coordinate trim/delivery models with the existing rpm × OBD-load surfaces. Include active slot, table value and edge-clamp state as explanatory context. | Improved prediction or fault sensitivity must be demonstrated on the same eligible observations and independent drives. |
| Timing-trim axes are rpm `0xCC93` and BFS-X `0xCC83`, cells masked `& 0x7F`. The table is selected by the HI DET latch, `0xFFFF283F` bit 6: set → trim B (High Det), clear → trim A (Main) (FUN_0004e6d8, 0x04E6FE; corrected 2026-10-05 from an earlier `0xFFFF2842` claim, whose bit 2 only gates the knock controller's trim-difference branch). The latch is logged as `mem:ECM_HIGH_DET_FLAGS`. | Evaluate timing conditioned on the actual coordinate and selection state (283F bit 6); later use the decoded command as a reference. | Main/High Det role names are inferred from the latch's knock behaviour; final timing corrections and latch transitions are not yet qualified. A trim lookup is not final ignition timing. |
| Target φ lookup, pedal last-column override and active-map initialization are decoded. | Keep predicted-command versus observed-command error alongside B06's commanded-versus-measured mixture error. | Final-command applicability and interpolation accuracy remain E02 gates; transient exclusions and missing inputs cannot become normal results. |
| CAN and K-line DIDs can map to different RAM; CAN `0x1102` was mislabelled LTFT but is speed. | Audit artifact input lineage and supersede affected findings/models only where the wrong source was consumed. | Catalog correction is already recorded in D03.36; historical data/artifact impact still needs a source-specific audit. Current `TOTAL_TRIM_B1/B2` definitions consume Mode 01 STFT/LTFT, so contamination cannot be assumed for them. |
| `ZS44A-20261004` restored Fuel Comp's load axis and slot-1 cells; K, Load Point Scaler, Target AFR and timing-trim bytes were unchanged. | Use the recorded intervention to test whether decoded calibration changes predict condition-specific trim/delivery changes. | Outcome has not been established by these notes. Unchanged table bytes do not guarantee unchanged operating coordinates or final outputs. |

Evidence sources:

- [Fuel Comp writer and remaining unknowns](../../ecu-firmware-analysis.md#fuel-comp-load-input-0xffff118a-and-how-bfs-is-formed).
- [Corrected table axes](../../ecu-firmware-analysis.md#map-slot-tables-confirmed-from-the-code-that-reads-the-ram-copy)
  and [editor grid verification](../../ecu-firmware-analysis.md#uprevs-fuel-comp-display-checked-against-memory).
- [Existing timing/airflow limitations](B04-timing-and-airflow.md): timing
  remains exploratory; MAF-derived air mass and VE share measurement blind
  spots. The new coordinate is a candidate to evaluate against those gaps.
- [Pre-flash Fuel Comp prediction](../../../data/analyses/2026-10-04-fuel-comp-restore-prediction.md)
  and [revision ledger](../../../data/nissan-xterra-2008/tune_revisions.yaml).

## How this strengthens the second brain

Keep three distinct questions and evidence records:

1. **ECU computation:** does a decoded path reproduce the observed
   intermediate quantity or command? E02/E05 own that prediction.
2. **Engine response:** does the physical measurement agree with the
   observed command under validated timing and conditions? B06 and other
   command/response models own that residual.
3. **Historical relationship:** has either relationship changed from its
   condition-matched, validated reference? Behavior models and E03 own this.

For example, an agreeing φ prediction with lean measured mixture supports
investigating delivery, instrumentation or physical-model assumptions. A
disagreeing φ prediction first calls for checking model coverage, ECU state,
ROM identity and timing. Neither identifies a component failure by itself.
Throttle-fill/MAF disagreement may add discriminating evidence, but does not
yet establish that MAF bias and fuel-pressure sag are distinguishable.

Any learned offset or distribution around a decoded prediction remains a
separate, versioned behavior model. Preserve the original twin residual and
frozen reference; adaptation must not rewrite firmware constants, explain
away a developing fault or silently turn unknown logic into verified logic.
Shared-input channels and aliases are recorded as dependent evidence, never
extra independent votes.

## Checklist

- [ ] **B08.1 — Audit observation and artifact lineage.**
  Trace recipe inputs, derived dependencies, historical decoders and stored
  artifacts for the corrected CAN DIDs and shared-register aliases. Check
  payload/source identity, not just channel names; a current catalog cannot
  make old decoded values correct. Re-decode recoverable raw samples, retain
  original provenance and supersede affected findings/artifacts through B03.
  **Accept:** a report lists affected, unaffected and unverifiable artifacts
  with source evidence. Mode 01 total-trim inputs are checked explicitly;
  the road-speed negative control cannot qualify as trim. PID 0x44 and
  target RAM agreement is counted as decode verification, not independent
  algorithm validation. No blanket retrain or silent feature replacement.

- [ ] **B08.2 — Add firmware-derived operating context.**
  Share a profile-backed provider with E02/E05/W08 for exact program/image,
  active slot, Fuel Comp rpm/fill coordinates, lookup value, clamp state,
  timing coordinate/select flag, pedal-enrichment flag and fuel-cut state.
  Use observed RAM coordinates before claiming to reconstruct their writers.
  Retain purge, learned idle-air correction and throttle fallback state when
  captured; distinguish the 224-count fill ceiling and the reciprocal-rpm
  saturation near 5,185 rpm. These are relative model coordinates, not VE.
  Include acquisition skew, freshness, raw/engineering units and applicability
  in both fit and serve contracts. Do not assume all historical/default
  captures contain these channels.
  **Accept:** known vectors reproduce the corrected axes and row-major
  layout; wrong-axis/transpose controls fail. Missing/stale/unknown state
  refuses by name, with no five-second forward-fill presented as a fresh
  observation. Both calibration images resolve through W08; existing
  recipes retain their identity until a new candidate is qualified.

- [ ] **B08.3 — Evaluate throttle-fill and fuel-context candidates.**
  Extend existing B04 air-charge and B05 trim recipes rather than adding a
  parallel fitter. Compare the current rpm/throttle or rpm/OBD-load baseline
  with rpm/observed-fill and justified table-context alternatives. Keep
  pressure/IAT, fuel state, steady/transient regimes and calibration cohorts
  explicit. Fit no absolute-airflow or manifold-pressure interpretation to
  the raw fill byte without resolving its units. Saturation may reduce
  sensitivity at high load and must be reported.
  **Accept:** untouched-drive evaluation on the intersection of eligible
  samples reports physical-unit error, coverage, event rates and sensitivity
  to labelled MAF/throttle/common-input controls, plus exclusive coverage.
  Air mass/VE/common-MAF descendants do not acquire extra fusion votes.
  Improvements unsupported by excitation or pressure coverage stay
  exploratory. Name the targeted capture when RAM tuples are insufficient.

- [ ] **B08.4 — Evaluate timing with actual lookup context.**
  Extend B04.2 with the verified rpm/BFS coordinate mapping and observed
  timing-selection state; preserve thermal and mode context supported by
  data. Compare against the existing timing surface on identical samples.
  Introduce a deterministic timing reference only after E05.4 qualifies its
  final-command chain, with any learned correction reported separately.
  **Accept:** B04/B02's declared timing-resolution and transient-control
  tests pass on independent drives before publication. A constant flag on
  one drive cannot validate both branches, and an isolated trim table cannot
  be labelled final timing or knock retard. Missing coverage remains named.

- [ ] **B08.5 — Connect distinct residuals to investigation evidence.**
  Feed B08 context and qualified behavior results into E03 while keeping
  ECU-computation, command/response and historical residuals separately
  identifiable. Reuse B06's units, fuel-state and transport-delay contracts.
  Mint E01 evidence with input-dependency groups and exact ROM/model versions.
  **Accept:** known cases distinguish a wrong ROM/branch, an injected
  delivery deviation with correct command, and missing target data.
  Investigation cards name the earliest supported disagreement without
  declaring a unique cause. Learner updates cannot remove or renormalize the
  original twin discrepancy, and alias/common-input evidence is not doubled.

- [ ] **B08.6 — Evaluate the October 4 Fuel Comp intervention.**
  Preserve the original pre-flash prediction as written, including its
  historical uncertainty; add new interpretation and results as linked
  records. Old image:
  `a46baa248f16db8d5ed9854913e614dd7d77b7579bb6c36bddb792741531be50`;
  restored image:
  `5587f7e1e6dbd0fcb9ffe0c857cc0cafebbe11bf306636c0f3265b903254d253`.
  The ledger's flash boundary is `2026-10-04T23:02:33.183902Z`; the RAM-study
  fixture precedes it. At matched input points, reproduce the proposed table
  multiplier changes, then test physical outcomes on independent later drives.
  Match rpm × raw fill and relevant thermal/barometric/fuel-state context;
  retain commanded φ, injector pulse, mixture and bank trims. Account for
  LTFT reset/relearning rather than equating early total trim with the prior
  baseline. Both axis and cells changed, so their individual physical effects
  are not isolated by this intervention.
  **Accept:** tool evidence reports the recorded direction/magnitude tests
  and condition-matched noise floor, supporting and refuting observations,
  coverage, exclusions and remaining confounders. No effect and insufficient
  evidence are distinct outcomes. Unchanged target/timing bytes are not
  asserted to guarantee unchanged commands. This evaluation issues no flash.

- [ ] **B08.7 — Publish qualified models and verify their Pi integration.**
  Reuse B03 compatibility/publication gates and E04's shared runtime. Record
  calibration applicability and affected references without conflating a
  calibration edit with a moved RAM address. Preserve old references and
  feature contracts used by existing detectors; extend manifests explicitly
  for any new feature selection. Required evaluation/promotion can complete
  on the Pi after collection stops; live prediction stays bounded.
  **Accept:** held-out evidence justifies each promoted candidate, live and
  stored results agree, and the combined Pi workload meets E04's measured
  budget without blocking collection. An unqualified candidate remains
  diagnostic-only with its limitation visible in second-brain cards.

## Order and ownership

B08.5 supplies E03.1/E03.2 with distinct residual evidence and observation
dependencies; its separate learned corrections support E03.5. B08.6 supplies
the intervention case for E03.6 without rewriting the original prediction.

Start with B08.1/B08.2 and the Fuel Comp/throttle-fill candidates (B08.3),
then timing (B08.4). B08.6 is the concrete intervention evaluation and can
start once its input/provenance audit passes. B08.5/B08.7 connect only
qualified outputs to the Pi brain. E05 owns reconstructed ECU calculations;
B08 owns their use in behavior-model context and validation. Existing B04,
B05 and B06 checkboxes retain their own acceptance and historical evidence.
No queue order or existing model status changes merely because this plan exists.
