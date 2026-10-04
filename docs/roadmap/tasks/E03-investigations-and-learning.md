# E03 — deterministic investigations and learning on the Pi

Follow [E00](E00-second-brain-README.md). Implements capabilities 3–5 using
E01 evidence and existing analysis/model contracts. E02/E05 add predictions
of ECU computation as they qualify. All deployed decisions below are
computed on the Pi and render without a language model.

## Checklist

- [ ] **E03.1 — Complete the fuel-delivery evidence/memory loop.**
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

- [ ] **E03.2 — Define detectable and distinguishable hypotheses.**
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

- [ ] **E03.3 — Detect change against a frozen reference.**
  Evaluate CUSUM/GLR candidates and condition-specific conformal intervals
  with the existing held-out-drive machinery. Declare excitation, residual
  dependence, interval calibration, persistence and event deduplication.
  A per-sample exceedance is not automatically an event or a false positive.
  **Accept:** independent-drive reports give coverage, event counts and
  detection latency for declared shifts, distinguish synthetic fault labels
  from real observations, and preserve excluded/unevaluable counts. Candidate
  adaptation cannot move the reference or widen its alarm limits.

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
