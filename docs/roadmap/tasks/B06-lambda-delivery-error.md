# B06 — commanded-versus-measured lambda delivery error

**Depends on:** B01/B02; B03 governs publication. Reuse `delivery_error`,
`delivery_error_from_semantic_frame`, AFR calibration and noise-floor schemas.
Follow [B00](B00-behavior-models-README.md).

This relationship should distinguish a commanded mixture change from a
delivery error. It supersedes the diagnostic purpose of an unconditional
AFR-voltage-vs-RPM/throttle surface; it does not infer a fuel-system cause from
a residual alone.

## Checklist

- [ ] **B06.1 — Declare the measured and commanded quantities.** Resolve
  target RAM/PID channels and measured lambda through the existing profile
  providers and calibration. Convert phi with `lambda_target = 1 / phi` only
  when that convention is verified. Define signed residual units explicitly,
  for example `100 * (lambda_measured / lambda_target - 1)` with positive lean
  of target. Unit-test both lambda and phi conventions away from stoichiometry;
  agreement at 1.0 cannot verify the convention.
- [ ] **B06.2 — Pin known answers and invalid states.** Reproduce target
  RAM/PID agreement using the sanctioned acquisition-time comparison and ratio
  inspection; use independent measured-mixture references where available.
  Propagate fuel-cut sentinels, zero/invalid targets, sensor rails, missing
  channels and unknown calibration as named ineligible states, never numbers
  that manufacture a delivery error. Include negative-control channels.
- [ ] **B06.3 — Handle transport delay without test leakage.** Start with a
  justified steady-target/steady-operation eligibility rule. Estimate any
  later lag model only from training/calibration drives, conditioned on
  relevant flow/regime if supported; freeze it before test evaluation.
  Declare acquisition mismatch separately from exhaust/sensor response delay.
  Do not choose lag independently on each test pull to minimize residuals.
- [ ] **B06.4 — Share analysis and serving semantics.** Implement the
  relationship in the existing derived/model pipeline with B01's persisted
  eligibility. Reconcile analysis and live paths, including differing stored
  clocks and polling rates; no generic 1-second mean pairing may claim
  instantaneous target tracking. Separate closed-loop, enrichment and transient
  regimes. Reference condition-scoped noise floors with valid units/provenance.
- [ ] **B06.5 — Evaluate useful sensitivity.** Use independent steady pulls
  and closed-loop controls with known calibration, adequate target variation
  and declared dwell. Test normal target steps plus lag, sustained lean/rich
  deviations, fuel cut and input dropout. Report qualified sample/event counts,
  coverage, error, detection delay and uncertainty under B02. Ordinary tip-in
  lag must not become a persistent lean-delivery finding.
- [ ] **B06.6 — Publish with bounded claims.** Prefer this relationship over
  adding an unconditional raw AFR-voltage surface when it qualifies. Surface
  target, actual, residual, regime and eligibility reason together. A detected
  delivery mismatch does not identify pressure, injector calibration or sensor
  bias without additional evidence. Run B03's compatibility/replay/rollout
  checks and retain the calibration-specific report.

**Done when:** analysis and live evaluation agree on eligible observations,
normal target transitions are correctly handled, and held-out evidence shows
the declared delivery deviation is resolvable. Missing sustained enrichment
or an independent sensor reference stays an explicit limitation.
