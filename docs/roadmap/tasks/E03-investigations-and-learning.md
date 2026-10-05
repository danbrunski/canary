# E03 — deterministic investigations and learning on the Pi

Follow [E00](E00-second-brain-README.md). Implements capabilities 3–5 using
E01 evidence and existing analysis/model contracts. E02/E05 add predictions
of ECU computation as they qualify. All deployed decisions below are
computed on the Pi and render without a language model.

## Checklist

- [~] **E03.1 — Complete the fuel-delivery evidence/memory loop.**
  Use B06's existing command-versus-measured analysis to detect a qualified
  discrepancy, create an investigation, attach alternative explanations,
  select a measurement, record a prediction and evaluate/preserve its outcome.
  Retain the distinction between ECU-command disagreement (E02/E05) and
  engine response disagreement (B06). Reuse existing eligibility and lag
  contracts; do not rebuild delivery analysis to start this workflow.
  **Accept:** a known stored case completes observe → predict → investigate
  → verify → remember on the Pi, and a later occurrence retrieves it.
  Missing commanded AFR and insufficient regime coverage produce different
  explicit outcomes; neither becomes a zero error or a resolved diagnosis.

- [x] **E03.2 — Define detectable and distinguishable hypotheses.**
  Express air/fuel relations and fault signatures as versioned artifacts.
  Spike the proposed Fault Diagnosis Toolbox for structural analysis before
  writing an alternative. Research may use a laptop, but deploy compact
  residual/hypothesis artifacts that the Pi can evaluate independently.
  Start with MAF transfer error, fuel-delivery sag and a fixed-quantity error
  such as an idle air leak/injector dead time. Require evidence for numerical
  *Laptop loop done 2026-10-04 (revised after verification); the Pi run
  remains (E04).* **Real answer today: the WOT lean does not reproduce out
  of sample.** `_capture-tuning-2026-09-23T164007` (+3.30 %, 44 rows) is in
  B06's enrichment `informed_by`, so it is in-sample: the mint writes no
  sign (`sign_refused`, flag `informed_constants`) and `detect_discrepancy`
  returns `unqualified`/`in_sample` — qualified needs an explicit
  `session_informed_constants: false`, so an in-sample discrepancy cannot be
  represented. The five held-out drives are minted in the case, all within
  the 2.50 % detectable (sign `0`, `within_floor`, nothing opened):
  `_capture-tuning-2026-09-23T142926` −1.14 % (24 rows),
  `…09-22T170459` +0.17 % (22), `…09-22T235224` +0.74 % (21),
  `_capture-tuning-hybrid-research-2026-09-17T191712` −0.46 % (67) and
  `…T201000` −0.66 % (46) — the last two on calibration ZS44A-k106. Each
  committed slice (`tests/fixtures/delivery_investigation/`, 8 slices,
  ~1.8 MiB, cut with `tools/make_ground_truth_fixture.py`, which gained
  `--keep-sensor`/`--sensor-stride`) reproduces its full session's n and
  band-averaged residual exactly. **Producers**
  (`src/analysis/delivery_evidence.py`): `mint_delivery_error` calls
  `tuning.delivery_error` unchanged (Mode 01 route, WOT floors, B06
  eligibility/lag); kinds of empty are distinct reasons with no residual
  (`missing_input` / `stale_input` / `uncovered` / `unknown_context`).
  `mint_fuel_pressure_droop(region=…)`: `high_airflow` = closed-loop
  (Mode 01 fuel status 2, bracketed by agreeing status reads within 5 s) MAF
  ≥ 60 g/s against closed-loop MAF 2–10 g/s, signed only fell/rose beyond
  2 SE + low-airflow drift (never "steady"); `enrichment` = pressure at the
  rows B06's `gate_delivery_samples` admits, as injector flow loss
  (√ law), signed against the delivery floor's 2.50 % detectable with the
  same margin, no sign for an in-sample drive. Every record lists the fault
  model channels its session holds. **Loop**
  (`src/analysis/delivery_investigation.py`): `detect_discrepancy`;
  `investigate` (opens only on a qualified discrepancy, else retrieves and
  supersedes the open one; single-fault refutation; structural analysis per
  drive on that drive's own channels and coverage); `unresolved_pairs` /
  `select_measurement` (actionable measurements first, then pairs
  separated); `predict_recurrence` and `predict_pressure_flow_loss` (both
  under the verified WOT delivery floor; flow-loss magnitude = the observed
  lean); `evaluate_outcome` (refuses the prediction's own or an earlier
  drive with `NotNextDriveError`; in-sample or unqualified-sign records →
  `unresolvable`/`refused`; another region → `uncovered`); `retrieve`. CLI
  `tools/investigate_delivery.py`. **Labelled synthetic loop** (generated in
  the test, `_synthetic-…`): drive A, 4 % lean, no pressure → opens, MAF /
  sag / sensor bias open, first measurement "Log fuel pressure through a
  full-throttle pull"; predictions recorded; outcome on the in-sample
  164007 → `unresolvable` `refused`; on held-out 142926 → `no_effect`,
  `contradicted`; drive B, later, 4 % lean with pressure holding → recurrence
  `confirmed`, flow-loss prediction `contradicted` (loss sign `0`), sag
  refuted, MAF/sag pair resolved, MAF vs sensor bias still open (wideband
  named), investigation retrieved and superseded. `2026-10-04T194342`:
  delivery `uncovered`, closed-loop droop +1.99 % (sign `+`, refutes
  nothing), enrichment pressure `uncovered`;
  `_capture-rom-trace-2026-09-21T195647`: `missing_input` for delivery and
  pressure. Evidence: `tests/test_delivery_investigation.py` (12 passed;
  serial ~10–20 s, of which the real-drive module fixture is ~7–12 s and the
  synthetic one ~2–6 s, so the first test of each belongs in the measured
  slow tier — `tests/slow_tests.txt` is generated and was not edited):
  `test_the_in_sample_wot_lean_drive_opens_nothing`,
  `test_the_held_out_drives_show_no_qualified_discrepancy`,
  `test_missing_command_and_uncovered_regime_are_different_explicit_results`,
  `test_closed_loop_droop_is_minted_and_enrichment_pressure_is_uncovered`,
  `test_synthetic_a_qualified_discrepancy_opens_an_investigation`,
  `test_synthetic_predictions_carry_the_condition_matched_floor`,
  `test_outcomes_need_the_next_independent_drive`,
  `test_synthetic_loop_verifies_and_resolves_the_maf_sag_pair`,
  `test_ecu_computation_evidence_cannot_feed_the_delivery_loop`, plus B06
  equivalence, card and re-mint tests. **Remaining:** (1) the Pi run of the
  loop through E04 (job/route wiring, persistence across a restart); (2) a
  real qualified discrepancy does not exist yet — a further held-out
  full-throttle drive is welcome evidence, and one with fuel pressure would
  exercise the flow-loss residual on real data, but neither blocks the
  machinery; (3) B08.5's ECU-computation/historical residual evidence is
  not attached (B08's).
  likelihoods; do not invent probabilities for unsupported hypotheses.
  **Accept:** known-answer and labelled synthetic controls exercise each
  declared signature; the model explicitly reports indistinguishable pairs.
  With today's absent fuel-pressure measurement, MAF error versus delivery
  sag remains unresolved and the proposed separating measurement names fuel
  pressure. A language-generated hypothesis stays unscored until modelled.

- [ ] **E03.3 — Detect change against a frozen reference.**
  Evaluate CUSUM/GLR candidates and condition-specific conformal intervals
  with the existing held-out-drive machinery. Declare excitation, residual
  dependence, interval calibration, persistence and event deduplication.
  A per-sample exceedance is not automatically an event or a false positive.
  **Accept:** independent-drive reports give coverage, event counts and
  detection latency for declared shifts, distinguish synthetic fault labels
  from real observations, and preserve excluded/unevaluable counts. Candidate
  adaptation cannot move the reference or widen its alarm limits.

  *Done on the laptop 2026-10-04 (v2 after verification); toolbox install
  left to the operator.* Artifact `data/nissan-xterra-2008/fault_hypotheses.yaml`
  (`canary-fault-hypotheses/1`, `version: 2`): regions `low_airflow` /
  `high_airflow` (both closed loop by Mode 01 fuel status) / `enrichment`
  (B06 gates); residuals `delivery_error` (floor-qualified),
  `pressure_flow_loss@enrichment` (√-law flow loss at B06-eligible rows,
  qualified against the delivery floor), `fuel_pressure_droop@high_airflow`
  (within-drive), `closed_loop_trim` (unqualified: no trim floor, LTFT
  reset), `throttle_fill_vs_maf` (B08; unqualified — unresolved terms and
  scale, not an independent airflow sensor), `independent_lambda` (proposed
  instrument, no invented channel); hypotheses `no_fault`,
  `maf_transfer_error`, `fuel_delivery_sag`, `fixed_quantity_error`,
  `lambda_sensor_bias`. Because whether the calibration absorbs droop is
  unknown, no hypothesis but sag constrains pressure beyond `nonnegative`;
  sag alone requires a positive flow loss. Evaluator
  `src/analysis/fault_model.py`: `structural_analysis` (separators are
  features whose sign sets differ, `guaranteed` when disjoint; a pair is
  `distinguishable` only on a usable guaranteed separator, else `possibly` /
  `indistinguishable`; measurements listed actionable first),
  `evaluate_observations` (single fault; conflicting signs refused),
  `score(model, ledger)` (a likelihood counts only if every cited evidence id
  resolves current on the vehicle's ledger; none is declared). Results:
  without fuel pressure MAF error vs sag is `possibly` and its first
  measurement is "Log fuel pressure through a full-throttle pull"; with
  pressure it stays `possibly` — a flow loss too small to explain the lean
  refutes sag, a large one refutes neither (a MAF error with an absorbed
  droop); MAF error vs sensor bias stays `indistinguishable` (needs an
  independent wideband); closed-loop droop refutes nothing. Evidence:
  `tests/test_fault_model.py` (24 passed): known answer, labelled synthetic
  controls for all five signatures and
  `test_synthetic_control_pressure_at_a_pull_refutes_sag_only_when_flow_holds`,
  `test_no_hypothesis_but_sag_claims_anything_about_pressure`,
  `test_with_pressure_the_pair_is_only_possibly_separable`,
  `test_closed_loop_droop_refutes_nothing`,
  `test_no_likelihood_without_evidence_that_resolves` (invented and
  retracted ids refused), language hypothesis unscored, schema refusals and
  the negative control `test_negative_control_a_sag_that_claims_no_flow_loss_is_inseparable`.
  *Toolbox spike (no install; PyPI metadata and docs read 2026-10-04):*
  `faultdiagnosistoolbox` 0.12.5, MIT, cp312 manylinux aarch64 and x86_64
  wheels (~100 KB), depends on sympy, numpy, scipy, matplotlib and
  scikit-learn (sympy and matplotlib are new to the Pi). It analyses
  equation-level DAE models (structural/Dulmage–Mendelsohn analysis, MSO
  residual generation, isolability, sensor placement, code generation). Our
  first artifact is a qualitative, region-indexed sign table with no
  equations, which its model input does not express, so the minimal
  set-signature analysis above was written instead. The toolbox becomes the
  right tool once the air/fuel relations are written as equations (E03.4/E05
  scale); installing it is an operator decision (not taken here).
- [ ] **E03.4 — Track hypotheses and select the next measurement.**
  Update a finite hypothesis set when new evidence arrives, preserving
  supporting and refuting evidence ids. Rank measurements by expected
  information gain divided by declared cost where probabilities support it;
  otherwise report the missing likelihood/measurement instead of false
  precision. Respect input timing, complete tuples, profile bus capacity and
  operator capture ownership. Raise polling/open a capture only when the
  drive naturally enters a useful region; physical-action instructions are
  for review, not demands for the driver to read/respond mid-drive.
  **Accept:** replay selects the known separating measurement, leaves
  indistinguishable explanations unresolved, and cannot displace an
  operator-owned capture. Repeated evidence is not counted twice and
  conflicting evidence changes the recorded ranking reproducibly.

- [ ] **E03.5 — Learn a small candidate without absorbing a fault.**
  Trial recursive least squares or a Kalman candidate over a few physically
  meaningful parameters. Predict before updating; gate on excitation,
  conditions and accepted data disposition. Freeze candidate updates for
  drives after an unresolved change alarm. Preserve checkpoints, valid time,
  reference/candidate lineage and calibration boundaries.
  **Accept:** replay proves prediction precedes learning, idle-only data
  cannot identify unexcited parameters, and an injected developing fault is
  still visible against the frozen reference. Candidates cannot train across
  an invalidating hardware intervention. Learning cannot alter decoded ECU
  arithmetic or its exact ROM constants to conceal a twin residual.

- [ ] **E03.6 — Evaluate interventions and promote after collection stops.**
  Run bounded evaluation on the Pi through E04, using independent drives,
  known-answer cases, condition-matched comparisons and E01's pre-intervention
  prediction/noise-floor contract. Reuse B03/online-training promotion and
  retirement rules; retain failed candidates and earlier references.
  **Accept:** a better recent fit alone cannot promote; insufficient
  independent evidence stays pending. A qualified candidate can promote
  atomically on the Pi and the old reference remains recoverable. Replaying
  the same evidence does not create a second outcome or promotion.

- [ ] **E03.7 — Record the NA investigation and supercharger intervention.**
  Link to N01 and supercharger NA2. Resolve the WOT-lean investigation with
  separating evidence before teardown where still possible, or record it as
  unresolved and superseded. Record build predictions for MAF transfer,
  air/cylinder-event, IAT and fuel pressure before first start if still in
  the future; late records are post-hoc. Retire the NA references under the
  intervention. **Vehicle/operator facts required; do not invent history.**
  **Accept:** the ledger preserves both successful and failed predictions,
  names unresolved measurements and prevents the candidate from adapting
  across the build. Each recorded calibration references its exact image;
  no task here issues a flash command.
