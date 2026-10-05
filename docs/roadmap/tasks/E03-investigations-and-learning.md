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

- [x] **E03.2 — Define detectable and distinguishable hypotheses.**
  Express air/fuel relations and fault signatures as versioned artifacts.
  Spike the proposed Fault Diagnosis Toolbox for structural analysis before
  writing an alternative. Research may use a laptop, but deploy compact
  residual/hypothesis artifacts that the Pi can evaluate independently.
  Start with MAF transfer error, fuel-delivery sag and a fixed-quantity error
  such as an idle air leak/injector dead time. Require evidence for numerical
  likelihoods; do not invent probabilities for unsupported hypotheses.
  **Accept:** known-answer and labelled synthetic controls exercise each
  declared signature; the model explicitly reports indistinguishable pairs.
  With today's absent fuel-pressure measurement, MAF error versus delivery
  sag remains unresolved and the proposed separating measurement names fuel
  pressure. A language-generated hypothesis stays unscored until modelled.
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

- [~] **E03.3 — Detect change against a frozen reference.**
  Evaluate CUSUM/GLR candidates and condition-specific conformal intervals
  with the existing held-out-drive machinery. Declare excitation, residual
  dependence, interval calibration, persistence and event deduplication.
  A per-sample exceedance is not automatically an event or a false positive.
  **Accept:** independent-drive reports give coverage, event counts and
  detection latency for declared shifts, distinguish synthetic fault labels
  from real observations, and preserve excluded/unevaluable counts. Candidate
  adaptation cannot move the reference or widen its alarm limits.
  *Re-done twice on 2026-10-04 after verification (second pass: drive-level
  persistence, refreeze refusal, lag-1 bias correction). Laptop parts done;
  the real-drive evaluation **refuses**; the Pi run through E04 remains.*
  Residual `stft_bank_balance`: closed-loop STFT bank 1 minus bank 2, paired
  one-to-one on acquisition time (≤ 1 s). Gates: `FUEL_STATUS == 2`,
  coolant ≥ 70 °C, rpm ≥ 400, and `LONG_FUEL_TRIM_1/2 == 0` (`ltft_adapting`:
  the balance is STFT alone only while LTFT sits at zero). Trims at ±25 % are
  rejected as clamped. B06 delivery error was not used: ≤ 44 enrichment rows
  per drive, all in excluded tuning captures.
  Declarations: `data/nissan-xterra-2008/change_detection.yaml`
  (`canary-change-residuals/2`; policy `canary-change-detection/2`). Every
  section below is required.
  - **Excitation.** MAF cells, intervals per cell.
  - **Dependence.** Split in two. The within-drive lag-1 is measured on
    drive-and-cell-demeaned block residuals and refused past 0.5 (the AR(1)
    inflation is then √3). Demeaning biases it down by about (1 + 3ρ)/n
    (Marriott & Pope), so the first-order correction is inverted with n the
    mean blocks per calibration drive; below 8 blocks per drive the
    correction is unreliable (at 5 it turns 0.09 into 0.73), so the
    reference is refused. The raw and corrected values are both kept. A between-drive variance component per cell is
    estimated by method of moments, floored at 0.5 points, and used by the
    drive level only.
  - **Calibration.** Refused when the calibration-fold mean bias in any cell
    exceeds 0.5 × the shift of interest. A cell needs blocks from ≥ 2 drives
    and ≥ 8 *effective* blocks. Intervals are marked
    `coverage_guaranteed: false` when the conformal rank fails on the
    effective count. Test coverage is reported per drive, with a t interval
    over drives.
  - **Persistence.**
    - CUSUM inputs are clipped 3σ *beyond* k. (Clipping at 3σ absolute
      blinds CUSUM whenever k ≥ 3.)
    - GLR inputs are clipped 3σ about zero. One unit's GLR is therefore at
      most 4.5, which is under both GLR limits (5); the validator enforces
      this.
    - A per-block alarm needs 2 consecutive blocks each beyond k while the
      statistic is over h.
    - A drive-level alarm (CUSUM or ramp GLR) needs this drive and the
      previous one each beyond k toward the alarm. A drive ramp spans ≥ 2
      drives.
    - The verifier's probe showed offsets [0, 0, W, 0, 0] raising
      `drive_glr` on d3. That is now a test:
      `test_one_wild_drive_among_in_control_drives_raises_no_drive_level_alarm`
      for W = 4, 8 and 20.
  - **Deduplication.** As before.
  - **`drive_level` (new).** Each drive becomes one standardized unit. Cell
    means are combined with inverse within-variance weights; the between-drive
    component is treated as shared across cells. That unit feeds a drive
    CUSUM (h 4) and a ramp GLR (gradual drift of unknown start and slope,
    h 5, last 10 drives) in `ChangeMonitor.close_drive`.

  **Policy frozen before the test drives.**
  - `prepare` selects among the declared `tuning.candidates` on an inner
    split of train + calibration only, with synthetic shifts injected there
    only. The candidates are `merged_cells`, `four_cells` (the
    first-declared cells, GLR 8/30) and `long_blocks`.
  - `freeze_policy` mints a `policy_frozen` evidence record (policy sha,
    reference fingerprint, test-drive ids, tuning table).
  - `evaluate` raises `PolicyNotFrozenError` without that record, or when
    the record disagrees on policy, reference or test drives.
  - `freeze_policy` raises `PolicyRefreezeError` when the ledger already
    holds a current freeze naming any of the same test drives under a
    different policy or reference. The identical freeze returns the existing
    record.
  - The tool freezes only on the vehicle's persistent ledger (`--config`,
    which uses `memory_ledger_for`, or `--ledger`). With neither it refuses
    to monitor test drives (exit 2).

  **Honest provenance.** The previous version of this item chose four
  settings after seeing a run that included the test drives: the merged
  cells, the raised dependence limit, GLR 8 → 5 with a 60-block window, and
  the 3σ clip. They are now tuning candidates or replaced by principled
  limits. The test drives' earlier exposure cannot be undone.

  **Real result (fixture regenerated with the LTFT gate; 1,883 eligible
  samples, 36 sessions excluded with reasons):**
  - Drives are named by their first eligible piece.
  - Folds: train `2026-09-21T142126`, `2026-09-21T194247`; calibration
    `2026-09-21T223515`, `2026-09-22T163415`, `2026-09-22T233855`; test
    `2026-09-23T150658`, `2026-10-04T190056`, `2026-10-04T202737`.
  - `2026-10-04T215234` is unevaluable (`uncovered`).
  - **Every tuning candidate is refused on the inner split.** The
    calibration drives hold 5–6 blocks each, below the 8 the dependence
    estimate needs.
  - **On the outer folds every candidate is refused too.**
    - `merged_cells`: 7.4 effective idle and 5.8 part-load blocks, against
      8 required.
    - `four_cells`: lag-1 0.74, corrected from 0.39.
    - `long_blocks`: 6.5 / 6.5 effective blocks.
  - The bias gate is not reached. `calibration_fold_bias`, measured on its
    own, puts the merged-cell calibration fold at +1.565 points at part
    load, past the 1.5 limit, so it alone would refuse.
  - So no reference is frozen, no test drive is monitored, and no latency
    exists on real data.
  - Cause: the balance drifts *inside* train and calibration. Per-cell drive
    means of the balance itself, not residuals and not composition-mixed:

    | Drive | Idle | Part load |
    |---|---|---|
    | `2026-09-21T142126` | −0.62 (3) | −1.37 (11) |
    | `2026-09-21T194247` | −1.99 (11) | — |
    | `2026-09-21T223515` | −0.09 (6) | — |
    | `2026-09-22T163415` | −0.36 (2) | −1.29 (4) |
    | `2026-09-22T233855` | −0.73 (10) | +1.32 (10) |
    | `2026-09-23T150658` | +0.03 (14) | −0.05 (5) |
    | `2026-10-04T190056` | +0.81 (4) | +0.28 (16) |
    | `2026-10-04T202737` | +1.33 (7) | — |

    Against the previous (unfrozen) reference, the verifier measured idle
    −0.36 → +2.96 and part load +0.17 → +1.83.

  **Synthetic pipeline (nine generated drives, labelled synthetic; pinned in
  `SYNTHETIC_PINS`):**
  - `merged_cells` is selected (tuning: 3/3 detected, mean 337 s, 0 events;
    `long_blocks` 457 s).
  - The reference calibrates with within-drive lag-1 0.041 and coverage
    guaranteed in both cells.
  - Test coverage per drive is 1.0 / 0.889 / 0.911, mean 0.933, t interval
    0.787–1.0 (nominal 0.9).
  - 0 observed events.
  - ±3 steps detected on all 3 drives at 177 s; the +4/15-min ramp at 657,
    777 and 657 s.
  - The +0.4/drive drift is caught per block at drive 3. Over only three
    drives the drive level does not alarm there.
  - The ten-drive kernel case
    `test_a_drift_between_drives_is_seen_at_drive_level_and_missed_per_block`
    (+0.4/drive) alarms at drive level with no per-block event.

  **Evidence:**
  - `tests/test_change_detection.py` (29), including:
    - `test_a_reference_biased_on_its_own_calibration_fold_is_refused`
    - `test_a_per_drive_offset_is_between_drive_variance_not_within_drive_dependence`
    - `test_effective_count_and_drive_count_gate_a_cell`
    - `test_a_spike_on_an_accumulated_statistic_is_not_an_alarm`: the
      statistic stays over h on the next block, so it is discriminating.
    - `test_a_limit_one_block_or_drive_can_reach_is_refused`
    - `test_verdicts_carry_the_reference_and_every_open_alarm`
  - `tests/test_change_evaluation.py` (17):
    - `test_a_test_drive_cannot_be_refrozen_under_another_policy`
    - `test_the_tool_refuses_to_monitor_test_drives_without_a_persistent_ledger`
    - `test_real_drives_refuse_a_reference_under_the_declared_policy` (pins
      the real refusal)
    - `test_test_drives_are_not_monitored_without_a_policy_freeze`
    - `test_synthetic_pipeline_pins_detection_and_latency_per_drive`
    - `test_a_drive_is_named_by_its_first_eligible_piece`

  **Remaining:** a real reference needs all three conditions on one
  calibration (the ZS44A-20261004 epoch began with the 23:02Z flash).
  1. The inner calibration fold's drives must each hold ≥ 8 blocks (for the
     dependence estimate) and give ≥ 8 *effective* idle blocks.
  2. Part-load blocks must come from both of those inner calibration drives
     (≥ 2 drives per calibrated cell).
  3. The outer calibration fold must pass the bias gate: mean residual per
     cell ≤ 1.5 points.

  If the new epoch refuses on (3), that is itself the drift finding — the
  balance moved by more than half the shift of interest between the
  training and calibration drives. It is not a data shortage, and more
  drives will not clear it. Then re-run the tool with `--config`.
  - The Pi run through E04.

- [~] **E03.4 — Track hypotheses and select the next measurement.**
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
  *Laptop parts done 2026-10-04 on labelled synthetic drives; the live hook
  (E04.1 streaming tick) and the Pi run remain.*
  - **Tracking** (`src/analysis/hypothesis_tracking.py`,
    `track_hypotheses`): the fault model's modelled hypotheses, each with
    its supporting/refuting evidence ids and drive counts. Counted once: a
    repeated id, a superseded id (counts as its current replacement), the
    same content, and a second record of one feature on one drive (a session
    in common); every skipped id is listed with why. Records of one feature
    on one drive that disagree in sign are a named conflict, excluded.
    Ranking: refuting drives, then supporting drives, then model order —
    independent of input order.
  - **Ranking measurements** (`rank_measurements`, extends
    `delivery_investigation.select_measurement`; `UnresolvedPair` and
    `MeasurementChoice` gained the `features` each measurement observes):
    expected information gain ÷ declared cost only when every open
    hypothesis has a likelihood whose cited evidence resolves
    (`fault_model.score`) and every open hypothesis's outcome on the
    measured features is a single sign; otherwise method `structural` with
    each missing likelihood and `P(feature | hypothesis)` named. On the real
    model today it is structural: no likelihood is declared, and MAF error's
    pressure signature is `nonnegative`, so pressure's information gain is
    undefined, not zero.
  - **Recording** (`update_investigation`): supersedes the investigation
    with hypotheses in ranked order and `next_measurements` in ranked order;
    re-running with already-counted evidence returns the same record.
  - **Plan artifact** `data/nissan-xterra-2008/measurement_plan.yaml`
    (`canary-measurement-plan/1`, `src/analysis/measurement_plan.py`): per
    residual a declared cost (operator minutes, with its basis) and
    acquisition (`capture: tuning`, `default`, `analysis`, `instrument`;
    `sensor:` channels are instruments); live region entry predicates.
    `load_measurement_plan` refuses another fault-model version, a missing
    residual/region, and thresholds that differ from their sources (the
    pressure policy's 2/10/60 g/s; `tuning_providers.yaml` `mode01_wot`
    85 % load / 2500 rpm).
  - **Proposals** (`propose`, `replay_proposals`): a capture is proposed
    only while every predicate of the measurement's region holds on values
    younger than 2 s (missing/stale input → named `defer`), only with
    `vehicle_state == engine_running`, never while any capture is active
    (`Proposal` refuses `open_capture` with `during_capture` set; the
    operator's own `tuning` capture reads `collecting`), and only from a
    profile that polls the residual's whole channel set (one batch group if
    declared) with declared demand within its bus budget
    (`profile_shortfall`: `incomplete_tuple` / `capacity_undeclared` /
    `over_capacity`; tuning 59 of 90 req/s passes). Instruments, analysis
    and fitting are `review` delivered `after_drive`; no delivery reaches the
    driver.
  - **Evidence:** `tests/test_hypothesis_tracking.py` (15 passed after the
    second pass, which added the session-overlap test; first pass 14 in 17.5 s
    serial): `test_drive_a_ranks_open_hypotheses_and_names_fuel_pressure_first`,
    `test_update_records_the_ranking_and_leaves_indistinguishable_pairs_open`
    (first next measurement "Log fuel pressure through a full-throttle pull
    (supercharger-build NA2)."; MAF error vs sensor bias unresolved, the
    wideband non-actionable), `test_repeated_evidence_is_counted_once`,
    `test_updating_with_counted_evidence_returns_the_same_record`,
    `test_conflicting_evidence_changes_the_ranking_reproducibly` (drive C on
    the command; five shuffles, one ranking; no hypothesis left open),
    `test_within_one_drive_a_sign_conflict_is_excluded_and_named`,
    `test_information_gain_per_cost_ranks_only_with_evidence_backed_likelihoods`
    (labelled variant model, weights 0.5/0.3/0.2: pressure 0.8813 bits,
    wideband 0.7219 bits; pricing pressure at 10,000 flips the order; an
    unresolvable citation falls back to structural),
    `test_replay_proposes_the_tuning_capture_only_inside_enrichment`,
    `test_replay_cannot_displace_an_operator_capture`,
    `test_replay_without_the_sensor_asks_for_it_after_the_drive`,
    `test_no_proposal_unless_the_engine_runs_and_canary_transmits`,
    `test_missing_or_stale_region_inputs_defer_with_a_named_reason`,
    `test_the_plan_refuses_thresholds_that_differ_from_their_sources`,
    `test_profile_capacity_and_complete_tuples`.
  - **Remaining:** (1) call `propose` from the live tick (E04.1's streaming
    evaluator, `src/daemon/twin_live.py`/`inference.py`, held by the E04
    agent) with `/status` capture state and fresh readings, and surface
    proposals on the Brain page (`BrainPage.tsx`, also E04's); (2) the Pi
    run; (3) no evidence-backed likelihood exists, so real rankings stay
    structural until one is earned.

- [~] **E03.5 — Learn a small candidate without absorbing a fault.**
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
  *Re-done 2026-10-04; laptop parts done on synthetic drives (no real
  reference can be frozen, see E03.3); the Pi run through E04 remains.*
  `src/models/adaptive_candidate.py`: a random-walk Kalman filter.
  - **Model:** `balance = proportional_imbalance + unmetered_air_imbalance /
    MAF`.
  - **Only from memory.** `candidate_from_ledger(ledger, spec, reference,
    calibration, channels, now)` is the only constructor; a direct
    `KalmanCandidate(...)` raises. It reads `unresolved_alarms` and
    `invalidating_boundaries` (moved into the kernel) from the E01 ledger,
    and gives the candidate its own `ChangeMonitor` holding those alarms.
    The tool and `replay_candidate` use it.
  - **Enforced freeze.**
    - `learn(pending, verdict, …)` needs the one-shot prediction token *and*
      the `BlockVerdict` the candidate's own monitor issued last, for the
      same block under the same reference fingerprint. A stale, reused,
      foreign or other-block verdict raises `PredictionOrderError`.
    - Any unresolved alarm in the verdict (ledger, per-block, drive-level)
      or held by the candidate makes the block `frozen`.
    - A block the monitor could not evaluate is `unmonitored`, never
      learned.
  - **Divergence guard.** An update that would put the candidate's
    prediction more than k (half the shift of interest, 1.5 points) away
    from the reference at any declared probe (4, 30, 100 g/s) is not applied: the
    outcome is `diverged`, a `divergence:` alarm is raised and the candidate
    freezes.
  - **Unchanged from the first version:** excitation (window reset per
    drive), disposition, calibration/intervention boundaries, self-hashed
    checkpoints with lineage, and twin refusals. The AST test now allows
    `src.evidence.*` and `src.models.change_detection` only.

  **Replays (synthetic):**
  - `test_replay_a_fault_developing_between_drives_alarms_freezes_and_bounds_the_candidate`
    (+0.4 points per drive from drive 3, 16 drives):
    - (a) the drive level alarms;
    - (b) every later drive starts frozen with 0 updates;
    - (c) the maximum probe divergence stays ≤ 1.5 points (k);
    - the reference is byte-identical afterwards, and the last drive's
      residual against it is > 3 points.
  - `test_replay_an_intra_drive_ramp_bounds_absorption_before_the_alarm`:
    probe divergence at the alarm is < 2 points against a 4-point ramp.
  - `test_the_candidate_cannot_drift_past_the_shift_of_interest`
  - `test_learning_needs_a_fresh_prediction_and_the_monitors_verdict_for_that_block`
  - `test_the_factory_reads_unresolved_alarms_from_memory`: an alarm on the
    ledger freezes the candidate; a resolved investigation releases it.
  - `test_the_candidate_cannot_adapt_across_a_recorded_intervention`

  **Real data:** the three idle-only drives' least-squares airflow-term
  standard error is > 5× the mixed drives'
  (`test_real_idle_only_drives_fix_one_combination_not_each_parameter`).
  No real replay is possible without a frozen reference.

  **Evidence:** `tests/test_adaptive_candidate.py` (21),
  `tests/test_change_evaluation.py`.

  **Remaining:**
  - A real-drive replay once E03.3 can freeze a reference.
  - The Pi run through E04: per-tick cost, and checkpoint persistence under
    `state_dir`.

- [~] **E03.6 — Evaluate interventions and promote after collection stops.**
  Run bounded evaluation on the Pi through E04, using independent drives,
  known-answer cases, condition-matched comparisons and E01's pre-intervention
  prediction/noise-floor contract. Reuse B03/online-training promotion and
  retirement rules; retain failed candidates and earlier references.
  **Accept:** a better recent fit alone cannot promote; insufficient
  independent evidence stays pending. A qualified candidate can promote
  atomically on the Pi and the old reference remains recoverable. Replaying
  the same evidence does not create a second outcome or promotion.
  *Laptop parts done 2026-10-04 on labelled synthetic drives; no real
  reference exists (E03.3), so the real state is `pending:
  no_frozen_reference`; the Pi run remains.*
  - **Store** (`src/models/candidate_store.py`, `CandidateStore` at
    `control/<slug>/brain/candidates/` under `state_dir`): references and
    checkpoints written once under their self-checked fingerprints (an
    edited file is refused), never deleted; `active.json` swapped by
    `atomic_write_text` with compare-and-swap (`StaleActivationError`);
    `history` and `restore`; content-keyed `decisions.jsonl`.
  - **Promoted reference** (`change_detection.calibrate_promoted`;
    `fit_reference` now shares `_calibrate_and_freeze`): the checkpoint's
    parameters, calibrated on independent drives under the predecessor's
    policy with every calibration gate; `FrozenReference.lineage`
    `promoted_candidate` must name `promoted_from` and `previous_reference`.
  - **Evaluation** (`src/analysis/candidate_promotion.py`,
    `evaluate_candidate`; B03's gates, each fail-closed): (1) only
    independent drives — not in the reference's folds, starting after the
    checkpoint's learned data, before no invalidating intervention, on the
    reference's calibration, and not before the prediction was recorded
    (`post_hoc`); (2) contamination — lineage, calibration, `frozen_by`,
    nothing learned, unidentified parameters and a retiring intervention
    reject; an unresolved change alarm keeps it pending; (3) E01 prediction
    (`register_prediction`: direction `increase` of the gain with the
    floor `<residual>_drive_mean` from `noise_floor.yaml`) judged per test
    drive on a paired comparison (same blocks, same calibrated cells, mean
    absolute residual) minted as evidence + outcome; `confirmed` needed on 3
    drives, one `contradicted` rejects, no floor leaves all `unresolvable`
    and pending; (4) known answers — each declared non-ramp shift injected
    into each test drive; the promoted reference must detect at least as
    many and raise no more alarms on the recorded drives. Drives after the
    3 test drives calibrate the promoted reference; too few → pending.
  - **Promotion is separate and explicit** (`promote`; B03: never
    automatic): only a `qualified` decision, against the reference still
    active; supersedes the old reference's memory record; the old file and
    record stay. Synthetic-origin decisions promote only on an isolated
    (replay) ledger.
  - **E04 jobs** (`src/control/brain_jobs.py`): kinds
    `candidate_evaluation` (residual + checkpoint; reads independent
    sessions in the child via `load_independent_drives`, E03.3's
    `read_session_samples`/`assemble_drives`) and `candidate_promotion`
    (residual + decision; refused 409 unless qualified). A pending decision
    leaves the job `pending` (not `completed`); the precheck re-runs it only
    when the count of sessions after the checkpoint changes.
  - **Synthetic result** (reference fitted on the true relation; the engine
    then sits +0.6 points off it; candidate learned 84 blocks over 4
    drives; floor sd 0.05 → detectable 0.1): test drives e0–e2 gain 0.316,
    0.417, 0.373 points (reference |residual| 0.57–0.64 → candidate
    0.19–0.27), all `confirmed`; known answers 3/3 both; 0 observed events
    both; promoted reference calibrated on e3–e4.
  - **Evidence:** `tests/test_candidate_promotion.py` (16 passed after the
    second pass; first pass 12 in 7.4 s serial):
    `test_a_qualified_candidate_promotes_atomically_and_the_old_reference_stays`,
    `test_replaying_the_same_evidence_creates_no_second_outcome_or_promotion`
    (same decision key and outcome ids, ledger size unchanged, second
    `promote` `changed=False`, one history entry, 3 outcomes),
    `test_a_stale_decision_cannot_overwrite_a_newer_reference`,
    `test_a_synthetic_decision_cannot_promote_in_live_memory`,
    `test_a_better_recent_fit_alone_cannot_promote` (its own drives are all
    `in_sample` → pending; independent drives without the drift →
    `contradicted` → rejected, checkpoint kept),
    `test_insufficient_independent_evidence_stays_pending`,
    `test_without_a_noise_floor_every_outcome_is_unresolvable_and_pending`,
    `test_a_promoted_reference_that_would_miss_known_faults_is_rejected`
    (the detector stubbed to miss under the promoted reference),
    `test_an_alarm_freezes_the_lineage_and_an_open_one_keeps_promotion_pending`,
    `test_the_store_refuses_edited_files_and_a_second_installation`,
    `test_a_promoted_reference_names_its_lineage`,
    `test_the_vehicle_job_stays_pending_until_independent_evidence_exists`
    (real profile: `pending: no_frozen_reference`, then `no_prediction` with
    0 sessions seen, not re-run; promotion 409).
  - **Verification fixes (2026-10-04, second pass):**
    - The brain child no longer imports torch: `default_profile_exclusion`
      moved from `src.models.evaluation` (which reaches the LSTM through
      `false_positive_benchmark`) to the torch-free
      `src/storage/corpus_eligibility.py`; `change_evaluation`,
      `behavior_validation`, `scripts/evaluate_detectors.py` and its test
      import it there. `test_the_brain_jobs_module_does_not_import_torch`
      (fresh interpreter: `'torch' not in sys.modules`).
    - `promote` re-checks the gates that can change after a decision:
      an unresolved change alarm and an intervention retiring the replaced
      reference raise `PromotionError` naming the gate
      (`test_promotion_re_checks_an_alarm_raised_after_the_decision`,
      `test_promotion_re_checks_an_intervention_recorded_after_the_decision`).
    - Unset `promoted_from`/`previous_reference` are left out of the hash, so
      a held-out fit's fingerprint is HEAD's (`3684479673d992f5` for the
      test reference, computed by running HEAD's `change_detection.py`;
      `test_a_held_out_fit_hashes_as_it_did_before_promotion_existed`).
    - E03.4 occurrence grouping is by session overlap (union-find over
      sessions), not the exact session set
      (`test_a_record_pooling_the_drive_with_another_session_is_the_same_drive`).
  - **Behaviour changes:** `POST /control/brain/jobs` accepts
    `candidate_evaluation` and `candidate_promotion` (the route is generic
    over `canonical_params`); a brain job can now end `pending` (not
    `completed`) when its run finds too little independent evidence, and
    re-runs only when the count of sessions after the checkpoint changes.
  - **Remaining:** (1) a real reference (E03.3) and a real candidate;
    nothing yet installs a frozen reference into the store or registers a
    prediction for a live checkpoint (the E03.3 tool and E04.1 streaming
    path); (2) `stft_bank_balance_drive_mean` has no measured floor, so a
    real evaluation stays `unresolvable` until one is measured under a
    declared regime; (3) the Pi run (job in a no_data window, a promotion
    surviving `scripts/restart_canary.sh`); (4) the dashboard job-kind type
    (`src/dashboard/static/src/types/brain.ts`, E04's) lists only the two
    older kinds.

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
