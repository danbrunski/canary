# B06 — commanded-versus-measured lambda delivery error

**Depends on:** B01/B02; B03 governs publication. Reuse `delivery_error`,
`delivery_error_from_semantic_frame`, AFR calibration and noise-floor schemas.
Follow [B00](B00-behavior-models-README.md).

This relationship should distinguish a commanded mixture change from a
delivery error. It supersedes the diagnostic purpose of an unconditional
AFR-voltage-vs-RPM/throttle surface; it does not infer a fuel-system cause from
a residual alone.

## Checklist

- [x] **B06.1 — Declare the measured and commanded quantities.** Resolve
  target RAM/PID channels and measured lambda through the existing profile
  providers and calibration. Convert phi with `lambda_target = 1 / phi` only
  when that convention is verified. Define signed residual units explicitly,
  for example `100 * (lambda_measured / lambda_target - 1)` with positive lean
  of target. Unit-test both lambda and phi conventions away from stoichiometry;
  agreement at 1.0 cannot verify the convention.
- [x] **B06.2 — Pin known answers and invalid states.** Reproduce target
  RAM/PID agreement using the sanctioned acquisition-time comparison and ratio
  inspection; use independent measured-mixture references where available.
  Propagate fuel-cut sentinels, zero/invalid targets, sensor rails, missing
  channels and unknown calibration as named ineligible states, never numbers
  that manufacture a delivery error. Include negative-control channels.
- [~] **B06.3 — Handle transport delay without test leakage.** Start with a
  justified steady-target/steady-operation eligibility rule. Estimate any
  later lag model only from training/calibration drives, conditioned on
  relevant flow/regime if supported; freeze it before test evaluation.
  Declare acquisition mismatch separately from exhaust/sensor response delay.
  Do not choose lag independently on each test pull to minimize residuals.
  *Machinery done 2026-09-22; no lag estimated.* The declared rule is a
  steady-target/steady-operation rule justified by the poll cadence (3 s dwell
  within 0.005 lambda, 2.5 s max gap, 15 s post-fuel-cut holdoff). Holdoff
  evidence: 112 cut episodes over the three drives settle within 3 % of target
  after a median 5.6-8.5 s, p90 12.2-17.6 s, max 18.4-22.9 s, 39 never
  settling. Acquisition mismatch is declared separately from response delay
  and measured (median 0.093-0.104 s, p95 0.130-0.142 s, 3386/3388 target
  reads within the declared 0.25 s). `estimate_transport_delay()` fits only on
  named training drives and freezes a `TransportDelay`; applying it to a drive
  in its own `fitted_on` raises `LeakedDriveError`, applying one without
  naming the drive is refused, and the drives that informed the eligibility
  constants are refused for `evaluation=True`. Evidence:
  `tests/test_lambda_delivery.py::test_transport_delay_is_estimated_on_training_drives_and_frozen`,
  `::test_a_test_drive_cannot_pick_its_own_lag`,
  `::test_drives_that_chose_the_eligibility_constants_are_not_evaluation_evidence`,
  `::test_transport_delay_refuses_insufficient_training_evidence`.
  *Lag estimated 2026-09-23 on the current calibration (corpus decision
  2026-09-23).* `estimate_transport_delay()` was run on the three informing
  drives only (candidates 0–3 s in 0.25 s steps, RAM target vs PID 0x24,
  2140 rows). It picks **0.25 s**: the first non-zero step, equal to the
  acquisition tolerance, with a 4 % score change (2.32 → 2.22). Frozen and
  applied to four later, independent tuning captures, it tightens
  closed-loop p05/p95 on one (−4.52/3.25 → −3.56/2.93 %) and widens p95 on
  the other three. At the ~1 Hz target cadence, lag is not resolved beyond
  acquisition skew ([report](../../../data/analyses/2026-09-23-behavior-refit-validation-report.md) §6). **Remaining:** not frozen into
  `lambda_delivery.yaml`. `TransportDelayDeclaration` has no numeric
  `seconds`/`fitted_on`, and a lag written into its `note` would be a
  constraint in a comment, so freezing one needs that schema field. A lag
  worth freezing needs a faster target read or sustained enrichment (still
  0–1 eligible enrichment rows per drive).
- [x] **B06.4 — Share analysis and serving semantics.** Implement the
  relationship in the existing derived/model pipeline with B01's persisted
  eligibility. Reconcile analysis and live paths, including differing stored
  clocks and polling rates; no generic 1-second mean pairing may claim
  instantaneous target tracking. Separate closed-loop, enrichment and transient
  regimes. Reference condition-scoped noise floors with valid units/provenance.
  *Analysis side done 2026-09-22.* One residual shared with `delivery_error`
  (B06.1). `build_tuning_frame` now emits the target word's fuel-cut sentinel
  as a `fuel_cut` column, so the workbench Analysis page and the tuning digest
  count cut samples through the named gate instead of losing them before the
  gate report starts (a declared-but-unobserved sentinel still adds the
  column). Regimes are separated per row (`closed_loop` / `enrichment` /
  `lean_target`, plus the `target_transient` and `post_fuel_cut` states).
  `regime_noise_floor()` hands the WOT-measured `delivery_error_pct` floor to
  enrichment only and refuses it for closed loop, for a floor whose recorded
  `conditions` differ, and for a non-percent unit. The pooled workbench path
  now states `pairing: {method: 1s grid …, claims_instantaneous_target_tracking:
  false}` and points at the acquisition-time path. Evidence:
  `tests/test_lambda_delivery.py::test_a_wot_noise_floor_is_not_quoted_against_closed_loop`,
  `tests/test_lambda_delivery_known_answers.py::test_state_counts_account_for_every_target_observation`,
  `tests/test_workbench_analysis.py`, `tests/test_tuning_frame.py`.
  **Remaining:** the live/serving implementation (a persisted-eligibility
  behavior model or derived channel evaluated on the 1 Hz operating frame).
  The corpus choice is made (2026-09-23), but no delivery model can be
  fitted and validated yet (B06.5), and the serving path is new code, not a
  refit. Its 1 Hz pairing must not claim instantaneous tracking.
  *Serving side done 2026-09-25: a tuning readout, not a detector.* Decision
  (stated in `src/daemon/lambda_delivery_live.py` and the payload): delivery
  error is served as a readout that never alerts and is **not** gated by
  R03.12 or vehicle state — R03.12 keeps tuning captures out of the
  learned-normal detectors, this learns nothing, and the tuning captures are
  the only ones that carry enrichment. No delivery model is fitted or trusted
  (B06.5/B06.6 stay data-blocked; payload `validated_model: false`,
  `alerts: false`). One row judge serves both paths:
  `lambda_delivery.judge_target_rows` is what `delivery_samples` now calls
  (output bit-identical to before on 12 route × capture pairs: 223734 and
  22T235224, all six declared routes) and what the live
  `lambda_delivery_stream.DeliveryStream` calls per tick on a bounded window
  of reads. The persisted eligibility is the declaration itself
  (`lambda_delivery.yaml`: per-regime dwell/tolerance/holdoff, per-source
  delay). Clocks reconciled: reads are taken per record off
  `LiveFeatureSource.record_observers` on `acquired_at` (else the stored
  stamp, converted exactly as the offline reader does); rows are decided on
  the data's own clock 2 s after their pairing window closes, so replay
  re-stamping and round-order delivery cannot change a verdict; a read that
  arrives after a row depending on it was published is counted (`late_reads`);
  round-stamped reads refuse (`clock_unsupported`). The 1 Hz tick is only
  the publication cadence — a derived channel on the 1 Hz operating frame's
  last-value cells would be the 1 s pairing R05.16 deleted, so none is
  written (`claims_instantaneous_target_tracking: false`). Regimes separate
  per route (`closed_loop`/`enrichment` stats, per-source validity, delay,
  `regime_noise_floor`). Every empty route names its kind:
  `inputs_not_observed` (missing specs listed) / `pending` /
  `not_co_observed` / `clock_unsupported` / `no_eligible_rows` / `eligible`;
  no declaration → `not_declared`, malformed → `withheld`. Published as
  `/status` `detectors.inference.lambda_delivery`, cost as stage
  `lambda_delivery` (laptop, 6 routes, committed capture: tick median 9.9 ms,
  p95 14.2 ms under a 2-4 load average). Evidence, all in
  `tests/test_lambda_delivery_live.py`:
  `::test_live_readout_gives_the_offline_rows_on_a_committed_tuning_capture`
  (records through a real `LiveFeatureSource`, a tick per data second, all six
  routes: every row's pairing, state and residual `assert_frame_equal`
  exact against `delivery_samples`; published state counts and per-regime
  n/median/p95 equal; 0 late reads; eligible 560 RAM-route / 229 PID 0x44),
  `::test_live_readout_gives_the_offline_rows_on_a_held_out_capture`
  (`~/canary-data/.../_capture-tuning-2026-09-22T235224`, same assertions;
  skips where absent), `::test_the_stream_decides_the_same_rows_whatever_the_tick_cadence`
  (irregular 0.2-6 s ticks, 3 seeds), `::test_every_empty_result_says_which_kind`,
  `::test_round_stamped_reads_are_refused_not_paired`,
  `::test_the_acquisition_clock_decides_not_the_stored_stamp`,
  `::test_a_late_read_is_counted_not_silently_folded`,
  `::test_a_session_change_starts_a_new_drive`,
  `::test_it_runs_under_a_tuning_capture_and_never_alerts`,
  `::test_a_vehicle_without_a_declaration_says_so`,
  `::test_a_malformed_declaration_withholds_the_readout`. Not done here:
  a dashboard surface for the block (B06.6 names the fields; the payload
  carries them), and a live check on the truck.
- [~] **B06.5 — Evaluate useful sensitivity.** Use independent steady pulls
  and closed-loop controls with known calibration, adequate target variation
  and declared dwell. Test normal target steps plus lag, sustained lean/rich
  deviations, fuel cut and input dropout. Report qualified sample/event counts,
  coverage, error, detection delay and uncertainty under B02. Ordinary tip-in
  lag must not become a persistent lean-delivery finding.
  *Closed-loop controls measured 2026-09-23; enrichment still absent.*
  `lambda_delivery_evidence.py` covered the five ZS44A-20260920 tuning
  captures after the three informing drives (all independent of the
  declared constants). RAM target vs PID 0x24 states per capture:
  - eligible closed loop: 448 / 556 / 11 / 989 / 473
  - fuel cut: 35 / 143 / 0 / 194 / 51
  - post-cut holdoff: 153 / 415 / 0 / 435 / 182
  - target transient: 19 / 28 / 3 / 34 / 40
  - **eligible enrichment: 0 / 1 / 0 / 1 / 0**

  Closed-loop residual median −0.38 … +0.78 %; on the four captures with
  ≥ 448 rows, p05 −4.5 … −9.3 % and p95 +3.2 … +3.8 %. **Remaining:** independent steady enrichment dwell (a
  held WOT or high-load pull ≥ the 3 s dwell, repeated over ≥ 2 drives).
  Until then, lean/rich sensitivity and tip-in behaviour cannot be scored
  under B02. There is no closed-loop noise floor either (refused by
  `regime_noise_floor`, correctly).
- [ ] **B06.6 — Publish with bounded claims.** *Not publishable (2026-09-23):
  corpus chosen, but there is no validated delivery model (B06.5). The serving
  implementation landed 2026-09-25 as a non-alerting tuning readout (B06.4).* Prefer this relationship over
  adding an unconditional raw AFR-voltage surface when it qualifies. Surface
  target, actual, residual, regime and eligibility reason together. A detected
  delivery mismatch does not identify pressure, injector calibration or sensor
  bias without additional evidence. Run B03's compatibility/replay/rollout
  checks and retain the calibration-specific report.

**Done when:** analysis and live evaluation agree on eligible observations,
normal target transitions are correctly handled, and held-out evidence shows
the declared delivery deviation is resolvable. Missing sustained enrichment
or an independent sensor reference stays an explicit limitation.
