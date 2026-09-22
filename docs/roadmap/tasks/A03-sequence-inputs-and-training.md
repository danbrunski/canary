# A03 — sequence detector inputs and training

**Depends on:** A02 for evaluation. Items that change the trained model take
effect only at the next retrain, which the operator deferred on 2026-09-22
(D03.25). A06 decides whether that retrain happens for the naturally
aspirated engine at all. Code and tests can land before then. Follow
[A00](A00-anomaly-detectors-README.md).

The 2026-09-20 artifact reconstructs 361 columns: 98 channel families × raw,
`__mean`, `__std`, `__roc`, with duplicate sources for the same quantity.
Many are driving conditions (baro, IAT, speed, TPMS, steering, HVAC) rather
than engine responses. So its score measures "unusual trip", not "engine
responding abnormally to its inputs". Its "observed" mask also does not mean
what the code says it means.

## Checklist

- [ ] **A03.1 — Make the mask mean "a sample arrived".** The mask is
  `¬<channel>__gap`, and `__gap` means "no sample within `max_gap`": 2 s for
  fast channels, 120 s for slow ones (`resampler.py`, `live.py`). So
  forward-filled cells read as observed, contrary to the docstrings in
  `sequence_dataset.py` and `lstm_autoencoder.py`. Measured on the
  calibration session:
  - Barometric pressure had 52 real samples in 1,085 s, yet mask = 1 on
    90.8 % of rows. About 95 % of its "observed" cells are forward-fills, and
    Δt is nonzero on only 10 % of them.
  - Coolant and IAT show the same pattern.
  - Fast-class PIDs polled every 4–6 s count each sample on about 2 rows.

  Consequences: GRU-D's decay engages only for dead channels, and slow
  regime channels are scored ~18 times per real sample.
  - Emit a separate per-cell "sample arrived this tick" flag from both the
    offline resampler and the live source. Use it for the mask and Δt.
  - Keep `__gap` for dead-sensor detection.
  - **Parity test:** the offline and live paths produce the same flag for
    the same input stream.
- [ ] **A03.2 — Condition on driving conditions; reconstruct engine
  responses.** Split the columns into:
  - *inputs*: operator commands and environment, such as rpm, throttle or
    pedal, speed or gear, baro, IAT, coolant, AC load and supply voltage;
  - *targets*: what the engine does in response, such as air mass, trims,
    lambda, timing, idle air and load.

  Score only target residuals, by operating point. This is the shape A06.4
  asks for; A06 decides whether to build it into the sequence model or leave
  it to the behavior models. Design and ablation here, not a rewrite ahead of
  that decision.
- [ ] **A03.3 — De-duplicate sources and justify every derived column.**
  - Four coolant channels contribute 19.2 % of the global-score numerator
    from 7.1 % of the observed cells.
  - `BAROMETRIC_PRESSURE__std` is a step detector: its scaler σ is 0.21 and
    it is zero except when a 1 kPa step arrives. It leads the top-feature
    ranking even on in-distribution calibration windows.

  Keep one source per physical quantity, chosen by the ground-truth suite.
  Drop `__std`/`__roc` targets unless an ablation on A02's protocol shows
  they carry detection value. Record the choice in the feature spec, not in
  a comment.
- [ ] **A03.4 — Fit the scaler on training sessions only.**
  `train_sequence.py` fits the `ZScoreScaler` over all sessions before the
  session-level validation split, so validation statistics leak into the
  scaling. Fit after the split, and test that validation rows do not move
  the scaler.
- [ ] **A03.5 — Choose validation sessions by a stated rule.** The seeded
  shuffle in `sequence_training.py` is immediately re-sorted by window
  count; the sort is stable, so the shuffle only breaks ties. Validation is
  therefore always the smallest sessions. Pick validation sessions so they
  span the operating modes the model will score, grouped by physical drive.
  Record the chosen sessions in the manifest.
- [ ] **A03.6 — Refuse ambiguous holdouts at the CLI.** `_resolve_training_sessions`
  (`scripts/train_sequence.py`) has two holes:
  - It returns explicit positional sessions without removing
    `--calibration-sessions` or `--evaluate`.
  - When every discovered session is held out, it silently trains on all of
    them.

  Remove held-out sessions from explicit lists too. Refuse when nothing is
  left to train on, naming the cause.
- [ ] **A03.7 — Make "top feature" mean something or stop showing it.**
  Attribution today is each channel's MAE divided by that channel's own
  0.999 quantile, over every column including driving conditions and rolling
  statistics. On the altitude trip it named `BAROMETRIC_PRESSURE` on every
  event (D03.20).
  - After A03.2/A03.3, attribute over engine-response targets only, grouped
    by channel family, and label driving-condition columns as context.
  - Until then, label the dashboard field as "largest deviation from the
    training envelope", not as a cause.
- [ ] **A03.8 — Record training-envelope coverage in the manifest.** For
  each operating mode, record the baro, IAT and speed ranges and the hours
  of data the model trained on. A02.2 uses this to refuse calibration that
  does not cover a mode, and A06.2's novelty-screen role reports "outside
  the envelope on X" against it.

## Acceptance

A03.4–A03.6 are code fixes with tests and no model decision. A03.1 needs a
parity test plus a retrain before it affects serving. A03.2, A03.3 and A03.7
are judged on A02's protocol against the current artifact, using the same
held-out drives and rows. Report per-drive results, not pooled totals.
