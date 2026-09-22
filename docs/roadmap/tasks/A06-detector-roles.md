# A06 — what the Isolation Forest and sequence detector are for

**Depends on:** the operator for every decision here. A02.11's baseline table
supplies the evidence. Items marked *decision* stay `[~]` with the options
named until the operator chooses; an implementer does not choose. Follow
[A00](A00-anomaly-detectors-README.md).

The two detectors were built to say "something is unusual" across hundreds
of features. The record so far, detailed in the
[review](../../../data/analyses/2026-09-22-anomaly-detector-review.md):

- **No confirmed fault catch.** No written record credits either detector
  with a real vehicle fault. The behavior validation report states "there is
  no real fault recall to claim".
- **Nuisance rate.** The live and replayed 2026-09-10 drive produced about
  117 sequence events per hour and 0 from the Isolation Forest, against a
  budget of 1 per engine-hour.
- **Weak on gross faults.** On 2026-09-20 the Isolation Forest caught 26 of
  250 injected faults, at ~1.9 alerts/h on clean data.
- **What they flagged was change, not faults.** Altitude, a PID that came
  alive after training, counter channels, and corrected bindings, which
  blinded the IF twice.
- **Cost.** Sequence scoring took a 251.5 ms median on the 2026-09-10
  drive. D01.7 measured the whole tick at a 439 ms median at idle.
- **Shelf life.** The supercharger build, weeks away, invalidates every
  learned normal.

On one vehicle with a moving corpus, "unusual" mostly means "outside the
training envelope". That is useful to know. It is not a diagnosis.

## Checklist

- [ ] **A06.1 — Decision: does the Isolation Forest alert?**
  - Options:
    - (a) keep alerting after A04's fixes;
    - (b) score and store as low-weight evidence, but never alert;
    - (c) retire it.
  - *Recommendation:* (b) now, since A05 covers its useful job (gross
    single-channel faults) by construction. Revisit (a) only if A02.4 shows
    it beating A02.5's baselines.
- [ ] **A06.2 — Decision: what the sequence detector is for.**
  - Options:
    - (a) keep alerting after A01–A03 and A02's recalibration;
    - (b) a non-alerting *novelty screen* reporting "outside the learned
      envelope; largest deviations on X, Y" against A03.8's envelope, stored
      as low-weight evidence and used as a corpus-coverage meter (after the
      supercharger it shows where the new normal lacks data);
    - (c) retire it.
  - *Recommendation:* (b). The "D01.7 kept it because it produces 60 % of
    events" argument measures volume, not usefulness.
  - Whatever is chosen, A01.1–A01.4 are correctness fixes and land anyway.
- [ ] **A06.3 — Decision: scope of the owed retrain (D03.25).** It is owed
  for three reasons: the binding retraction, the mode labels, and the new
  default-profile DIDs. The supercharger build will make the result obsolete
  within weeks.
  - Options:
    - (a) full naturally aspirated retrain now;
    - (b) retrain only what A06.1/A06.2's roles need, e.g. the envelope
      record for a novelty screen;
    - (c) skip it and start the boosted corpus at the build.
  - *Recommendation:* (b) or (c). Record the build as a hardware
    intervention and corpus boundary either way.
- [ ] **A06.4 — Decision: where the primary detector lives.** The literature
  (A02.5) and production OBD practice both point at per-quantity residuals:
  engine responses predicted from operating inputs, with per-regime bands,
  change detection and confirmation. That is what the behavior models
  (B-series) and the second brain's deterministic layer already are.
  - Options:
    - (a) grow the behavior models and the second brain into the primary
      detector, with A05 for gross faults;
    - (b) rebuild the sequence model as a conditioned forecaster
      (A03.2) and make it primary.
  - *Recommendation:* (a). It does not build a parallel framework, and each
    alarm names a component. (b) stays open as an A02-judged experiment.
- [ ] **A06.5 — Fusion and the alert budget follow the roles.** Once
  A06.1/A06.2 are decided, change fusion so non-alerting detectors
  contribute evidence but cannot raise an alert on their own. Enforce ≤ 1
  alert per engine-hour on A02's evaluation drives at publish time, as a
  refusal, not a report.
- [ ] **A06.6 — Give back the tick budget if the sequence detector stops
  alerting.** A novelty screen does not need 1 Hz. Measure it at a lower
  cadence, or post-drive, on the Pi, and return the time to the collector and
  the live second-brain loop (Pi item).
- [ ] **A06.7 — Update the docs that describe the detectors' role.**
  Update, citing A02's numbers:
  - [`CANary-overview.md`](../../CANary-overview.md);
  - [`per-mode-detection-and-mode-taxonomy.md`](../../per-mode-detection-and-mode-taxonomy.md);
  - [`online-training-strategy.md`](../online-training-strategy.md);
  - the dashboard's anomaly panel copy.

## Acceptance

Each decision item is closed by the operator's choice, recorded here with
its date, plus the A02.11 table it was based on. A06.5 and A06.6 follow the
decisions. None of this removes code before A02 has measured the
replacement.
