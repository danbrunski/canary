# B04 — timing resolution and airflow consolidation

**Depends on:** B01/B02; B03 governs artifact status and publication.
Addresses the behavior-model part of D03.20. Follow
[B00](B00-behavior-models-README.md).

## Timing

- [ ] **B04.1 — Reproduce the timing baseline.** Record held-out errors,
  coverage and physical warning widths for RPM/load and RPM/IAT, including
  the cooler September drives. Preserve the roughly 24° median warning width
  as an observed limitation, not a desired threshold. Separate calibration,
  temperature, load and steady/transient regimes before comparing candidates.
- [ ] **B04.2 — Build a load-aware context candidate.** Retain RPM and load
  as essential explanatory inputs; assess IAT/coolant and relevant air-density
  context on independent drives. Prefer the smallest interpretable conditional
  model justified by coverage. If additional axes cannot be supported, keep
  the model exploratory with named missing coverage rather than substituting
  an RPM/IAT-only fit or broadening bands until it passes.
- [ ] **B04.3 — Define and test useful timing resolution.** Put the intended
  sustained timing change and its required duration into the evaluation spec,
  justified by measured noise and the diagnostic question. Validate simulated
  shifts, transient retard controls and unmodified held-out data through actual
  eligibility and persistence. Report detection delay and covered envelope;
  an unexpected timing change must not be labelled proven knock retard.
- [ ] **B04.4 — Publish only demonstrated improvement.** Use B02's untouched
  test drives, same eligible points and physical-unit errors to compare against
  the existing surface and simple baseline. Publish via B03 if qualified;
  otherwise leave the candidate diagnostic-only. RPM/IAT does not regain an
  independent alert solely because it supplies useful context.

## Air charge and VE

- [ ] **B04.5 — Preserve physical definitions in the contract.** Trace
  `compute_anomaly_signals` and `_theoretical_air_mass_per_cycle_g`: distinguish
  measured MAP, measured baro and missing/default IAT in model inputs and
  provenance. Do not silently train/serve one VE definition as another.
  Retain raw air-mass/air-charge quantities for inspection and restrict
  diagnostic claims to relationships the measurements actually identify.
- [ ] **B04.6 — Validate normalization across conditions.** Keep the air-charge
  recipe, quantify its missing grid coverage, and compare density-aware
  alternatives on multiple independent low/high-pressure and temperature
  sessions. Record units and calibration dependence of raw DID counts. Missing
  pressure/IAT is an unavailable input, not evidence of stability.
- [ ] **B04.7 — Run the airflow ablation on identical observations.** Compare
  air-mass, VE and their combination using the intersection of eligible rows;
  separately report each model's exclusive coverage. Include no-fault controls
  and explicitly synthetic single-sensor/common-input and airflow-change
  cases. Count unique detections, misses and delays, not just output correlation
  or aggregate alert reduction. Label common-input blind spots honestly.
- [ ] **B04.8 — Make the conditional retirement decision.** Demote the
  air-mass alert only if validated VE/context coverage preserves useful unique
  sensitivity under the predeclared B02 criteria. Otherwise retain it with
  corrected context or diagnostic-only status and name the unresolved evidence.
  Check fusion grouping so transforms of the same MAF/RPM evidence are not
  treated as independent confirmation. Publish through B03 and link the
  behavior-side evidence back to D03.20.

**Done when:** timing has either demonstrated useful resolution or remains
explicitly exploratory, and airflow retention/removal is supported by an
independent-drive, same-observation ablation. Fewer alerts alone is not success.
