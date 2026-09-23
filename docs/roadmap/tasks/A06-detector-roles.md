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

- [~] **A06.1 — Decision: does the Isolation Forest alert?** *Open:
  operator decision. Option (b) is now one config line,
  `models.roles.isolation_forest: evidence` (A06.5); the default is still
  `alert`.*
  - Options:
    - (a) keep alerting after A04's fixes;
    - (b) score and store as low-weight evidence, but never alert;
    - (c) retire it.
  - *Recommendation:* (b) now, since A05 covers its useful job (gross
    single-channel faults) by construction. Revisit (a) only if A02.4 shows
    it beating A02.5's baselines.
  - *Evidence now available (2026-09-23, awaiting the operator's choice):*
    A02.11's held-out table,
    [`2026-09-23-a02-detector-baseline.md`](../../../data/analyses/2026-09-23-a02-detector-baseline.md).
    It is **provisional**: it is being re-run after a harness fix, so re-read
    it before deciding. First-run overall false alarms per engine-hour,
    against a budget of 1: Isolation Forest 1.45 [0.00, 4.29] with 78 % of
    rows withheld; sequence (per-channel) 42.78 [22.58, 62.13]; sequence
    global 3.38; fused 0.00. A05's deterministic plausibility checks are
    committed (`06a3188`, [A05](A05-plausibility-checks.md)). The role
    mechanism the decision would switch is in A06.5.
- [~] **A06.2 — Decision: what the sequence detector is for.** *Open:
  operator decision. The non-alerting half of option (b) is
  `models.roles.sequence: evidence` (A06.5). The novelty-screen report
  against A03.8's envelope is not built.*
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
  - *Evidence now available (2026-09-23, awaiting the operator's choice):*
    A02.11's held-out table,
    [`2026-09-23-a02-detector-baseline.md`](../../../data/analyses/2026-09-23-a02-detector-baseline.md).
    It is **provisional**: it is being re-run after a harness fix, so re-read
    it before deciding. First-run overall false alarms per engine-hour,
    against a budget of 1: Isolation Forest 1.45 [0.00, 4.29] with 78 % of
    rows withheld; sequence (per-channel) 42.78 [22.58, 62.13]; sequence
    global 3.38; fused 0.00. A05's deterministic plausibility checks are
    committed (`06a3188`, [A05](A05-plausibility-checks.md)). The role
    mechanism the decision would switch is in A06.5.
- [~] **A06.3 — Decision: scope of the owed retrain (D03.25).** *Open:
  operator decision.* It is owed
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
  - *Evidence now available (2026-09-23, awaiting the operator's choice):*
    A02.11's held-out table,
    [`2026-09-23-a02-detector-baseline.md`](../../../data/analyses/2026-09-23-a02-detector-baseline.md).
    It is **provisional**: it is being re-run after a harness fix, so re-read
    it before deciding. First-run overall false alarms per engine-hour,
    against a budget of 1: Isolation Forest 1.45 [0.00, 4.29] with 78 % of
    rows withheld; sequence (per-channel) 42.78 [22.58, 62.13]; sequence
    global 3.38; fused 0.00. A05's deterministic plausibility checks are
    committed (`06a3188`, [A05](A05-plausibility-checks.md)). The role
    mechanism the decision would switch is in A06.5.
- [~] **A06.4 — Decision: where the primary detector lives.** *Open:
  operator decision.* The literature
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
  - *Evidence now available (2026-09-23, awaiting the operator's choice):*
    A02.11's held-out table,
    [`2026-09-23-a02-detector-baseline.md`](../../../data/analyses/2026-09-23-a02-detector-baseline.md).
    It is **provisional**: it is being re-run after a harness fix, so re-read
    it before deciding. First-run overall false alarms per engine-hour,
    against a budget of 1: Isolation Forest 1.45 [0.00, 4.29] with 78 % of
    rows withheld; sequence (per-channel) 42.78 [22.58, 62.13]; sequence
    global 3.38; fused 0.00. A05's deterministic plausibility checks are
    committed (`06a3188`, [A05](A05-plausibility-checks.md)). The role
    mechanism the decision would switch is in A06.5.
- [~] **A06.5 — Fusion and the alert budget follow the roles.** Once
  A06.1/A06.2 are decided, change fusion so non-alerting detectors
  contribute evidence but cannot raise an alert on their own. Enforce ≤ 1
  alert per engine-hour on A02's evaluation drives at publish time, as a
  refusal, not a report.
  - *Done, decision-neutral (2026-09-23):* the role mechanism.
    `models.roles.{isolation_forest,sequence}: alert | evidence`
    (`DetectorRolesConfig`, `src/config.py`) defaults both to `alert`, so an
    unchanged config behaves as before. It is enforced in the two shared
    paths, not per detector:
    - `AnomalyEventEmitter._emit` stamps `role: evidence` on the stored
      event, counts it in `emitted_evidence` rather than `emitted`, and never
      pushes it to ntfy;
    - `FusionScorer.fuse` counts an evidence source toward `agreement` and
      the boost, but only an alert-role source sets the base severity, and
      `is_anomaly` needs one. The `agreement >= 2` gate is unchanged.

    The daemon wires both from config (`build_components`,
    `LiveInferencePipeline`). The dashboard status strip never shows an
    evidence event as an active anomaly, and the Anomalies panel tags it
    `evidence`. The trip digest (`src/analysis/digest.py`, schema 2) counts
    only alert-role events in `anomaly_counts_by_source`, the "gated
    anomalies" figure the ntfy digest push and the Trip panel show. Evidence
    events go in a separate `evidence_counts_by_source`, shown on its own
    "N evidence event(s) (not alerts)" line, which is left out when it is
    zero. Schema-1 digests are read as they are: they predate roles, so
    every event they counted was an alert. They are not recomputed, because
    a late recompute's window ends at "now".

    Additive payload changes, all visible to callers:
    - `role` on every stored event (`"alert"` by default);
    - `emitted_evidence` in `/status` `emitter_counters`;
    - a per-source `role` in the fused event's `detail.sources`;
    - `evidence_counts_by_source` and `schema_version: 2` in `digest.json`. Tests: `tests/test_detector_roles.py`, 19 passed
    (config defaults and refusals, example configs, emitter storage and
    no-push, per-detector roles, fusion agreement, evidence-only agreement
    never alerts, daemon wiring end to end), and
    `src/dashboard/static/src/panels/StatusStrip.test.ts` (4 role cases);
    digest tests: `tests/test_digest.py::test_compute_digest_separates_evidence_role_events_from_alerts`
    (one alert and one evidence event give 1 and 1, and the push body does
    not count the evidence event as gated), 3 `format_digest_message` cases
    in `tests/test_notifier.py`, and 2 `TripPanel.test.ts` cases. The
    existing fusion, emitter, notifier, digest and trip-end suites pass
    unchanged.
  - *Remaining:*
    - the operator's role choice (A06.1/A06.2);
    - the publish-time alert-budget refusal (≤ 1 alert per engine-hour on
      A02's evaluation drives). It depends on A02's harness
      (`src/models/evaluation*.py`, `scripts/evaluate_detectors.py`), which
      another implementer is fixing, so it is not built. It has to count
      `role: alert` events only.
    - A02's harness builds `FusionScorer(config.models.fusion)` without
      `roles=` (`evaluation_detectors.py`, `synthetic_fault_eval.py`), so
      its fused numbers assume both detectors alert until it passes
      `roles=config.models.roles.by_source()`.
- [~] **A06.6 — Give back the tick budget if the sequence detector stops
  alerting.** A novelty screen does not need 1 Hz. Measure it at a lower
  cadence, or post-drive, on the Pi, and return the time to the collector and
  the live second-brain loop (Pi item).
  - *Remaining:* the whole item. It depends on A06.2 and needs a measurement
    on the Pi. No lower-cadence option was added. The window is state
    advanced by every row (in-process and in the scoring child), so "score
    every n ticks" means "append every tick, forward-pass every n". That
    change touches `src/models/inference.py` and the worker, not just config.
- [~] **A06.7 — Update the docs that describe the detectors' role.**
  *Partial (2026-09-23): `CANary-overview.md` now has a "current state,
  decision pending" note on the role mechanism (FR-07) and a `roles:` block
  in its config sample. The Anomalies panel tags evidence events. Remaining:
  the role text itself for all four docs, and A02's final numbers, once
  A06.1/A06.2 are decided.*
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
