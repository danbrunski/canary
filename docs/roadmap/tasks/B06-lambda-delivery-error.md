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
  **Remaining:** choose and freeze an actual lag — deferred: needs the
  operator's corpus choice (2026-09-22) and drives holding enrichment long
  enough to resolve one (the three 2026-09-21 drives yield 2 eligible
  enrichment rows in total).
- [~] **B06.4 — Share analysis and serving semantics.** Implement the
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
  behavior model or derived channel evaluated on the 1 Hz operating frame)
  — deferred: it needs a published model, i.e. the operator's corpus choice
  (2026-09-22), and its 1 Hz pairing must not claim instantaneous tracking.
- [ ] **B06.5 — Evaluate useful sensitivity.** *Deferred: needs the
  operator's corpus choice (2026-09-22), plus drives with sustained
  enrichment and declared dwell — the current corpus yields 2 eligible
  enrichment rows.* Use independent steady pulls
  and closed-loop controls with known calibration, adequate target variation
  and declared dwell. Test normal target steps plus lag, sustained lean/rich
  deviations, fuel cut and input dropout. Report qualified sample/event counts,
  coverage, error, detection delay and uncertainty under B02. Ordinary tip-in
  lag must not become a persistent lean-delivery finding.
- [ ] **B06.6 — Publish with bounded claims.** *Deferred: publication is
  fitting + rollout (operator's corpus choice, 2026-09-22).* Prefer this relationship over
  adding an unconditional raw AFR-voltage surface when it qualifies. Surface
  target, actual, residual, regime and eligibility reason together. A detected
  delivery mismatch does not identify pressure, injector calibration or sensor
  bias without additional evidence. Run B03's compatibility/replay/rollout
  checks and retain the calibration-specific report.

**Done when:** analysis and live evaluation agree on eligible observations,
normal target transitions are correctly handled, and held-out evidence shows
the declared delivery deviation is resolvable. Missing sustained enrichment
or an independent sensor reference stays an explicit limitation.
