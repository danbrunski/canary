# ml-hardening 1.6 — laptop retrain + replay diff

Overnight run, 2026-07-19. Validates that Phase 1's stale-data changes
(1.1–1.5: bounded ffill + gap flags, segmenter coolant-staleness guard,
live-tick parity, vehicle-state gating) don't break the training/serving
pipeline end-to-end. Laptop-only per `OVERNIGHT-QUEUE.md` Q2 — the Pi
deploy half is `blocked-overnight` (needs the operator's truck + `scp` +
daemon restart).

## Retrain

`scripts/retrain.py --all --sessions-root data/sessions --config
<scratch smoke config: canary.yaml + lstm max_epochs=8,
early_stopping_patience=3, ~30 min budget> --if-out
models/_overnight-1.6-if --sequence-out models/_overnight-1.6-lstm`.

- 12 training sessions (2 auto-held-out for calibration:
  `2026-05-24T193045`, `2026-05-20T033820` — the default
  most-recent-session holdout).
- **45,184 / 49,570 rows (91%) dropped as fully-stale** by
  `_drop_fully_stale_rows` before hitting the trainers. This is the
  intended effect of 1.1–1.4, not a regression: these are long
  power-off-gap rows that used to forward-fill indefinitely and
  contaminate training (a 12h gap → thousands of identical fake "warmup"
  rows); now they're correctly recognized as synthesized, not telemetry,
  and excluded. Worth a sanity gut-check on the next real retrain (does a
  91% drop rate hold up outside this artificially-small 12-session
  smoke corpus, or is it inflated by a couple of pathological sessions?)
  but it isn't a red flag on its own.
- LSTM: 224 feature columns, 8,529 windows, converged smoothly and
  early-stopped at epoch 8 (train loss 0.59→0.34, val loss bottomed at
  epoch 5 = 0.633). IF: trained clean, no errors.
- Both artifacts wrote successfully; `retrain.py`'s summary reported
  both `ok`.

## Replay diff

Replayed session `2026-05-24T193045` (one of the calibration holdouts,
~29 min, 2026-05-24T19:30:46–19:59:35Z) through
`src.analysis.replay_window.replay_window` against the new LSTM
artifact, diffed against that day's real historical anomaly log
(`~/canary-data/anomalies/`).

- **LSTM: 44 anomalies / 1,700 windows scored (~2.6%)**, all one
  contiguous 44-second episode (19:44:16–19:44:59Z), not scattered
  across the drive. But it's an *escalating, broadening* event, not a
  static signature: the first 4 windows flag a narrow 3-channel set
  (`obd:EVAP_VAPOR_PRESSURE`, `obd:EVAP_VAPOR_PRESSURE__roc`,
  `obd:TIMING_ADVANCE__std`); by the last windows it's broadened to
  18–21 channels including `obd:RPM`, `obd:SPEED`, `obd:MAF`,
  `obd:ENGINE_LOAD`, `obd:THROTTLE_POS`, `obd:CATALYST_TEMP_B1S1/B2S1`,
  `gps:speed`. Worth a human look before Pi deploy — could be a genuine
  transient (e.g. a real EVAP/purge event during a load change that
  cascades into engine-load channels) or a smoke-config scaling
  artifact; the reduced-epoch config wasn't tuned for threshold
  quality, so don't over-read the exact count or channel list.
  Directionally sane in that it's one bounded 44s episode, not
  runaway/every-tick firing across the whole 29-minute drive — but the
  broadening pattern is exactly the kind of thing that needs a look,
  not a shrug.
- **Historical baseline: 690 events in the same window** (real
  pre-fix flood-era daemon output — before the STFT-smoothing,
  persistence-gate, and quantile-threshold fixes already on `main`;
  see `project_pending_pi_deploy_2026-05.md`). 0 matched (different
  model version/thresholds/config entirely, so no timestamp overlap
  is expected) — the comparison that matters here is 44 vs 690, not
  the match count.
- **IF side of the diff: blocked**, not attempted. `scripts/replay.py`
  (`replay_sessions`) never decodes CAN — no `bindings.yaml`, no DBC —
  while the trained IF's `feature_columns` require `decoded:*`/`dbc:*`
  columns (training's `build_feature_frame` always loads both). Scoring
  the retrained IF via `replay_window` fails with a `KeyError: missing
  required columns`. This is a **pre-existing parity gap**, not
  something 1.4/1.5 introduced — `OVERNIGHT-QUEUE.md` Q3 already scopes
  the fix ("2.1/2.2 parity pre-flight fixes to `scripts/replay.py` and
  `build_feature_frame`"), so it wasn't fixed here to avoid stepping on
  that work.
- **Also found, not fixed** (same reasoning — out of scope, flagging for
  whoever picks up Q3/eval-realism): `tools/replay_window.py:107` reads
  `config.alerts.anomalies_dir`, but `AlertsConfig` has no such field
  (it lives on `DaemonConfig.anomalies_dir`) — the CLI's `--anomalies-dir`
  default silently `AttributeError`s unless the flag is passed
  explicitly. Worked around here by always passing `--anomalies-dir`.
  Separately, `_load_historical_events`'s session filter
  (`if record.get("session") and record["session"] != session_id:
  continue`) is broken in two different, opposite directions depending
  on source: `behavior`/`fused`/`isolation_forest` events never
  populate `session` (`None`, falsy) so the filter is a no-op for them
  — an unscoped replay pulls in *every* such event ever logged across
  the whole directory, not just the target session (confirmed: 41,625
  events with no `--start`/`--end`, all non-`session`-tagged). But
  `lstm_autoencoder` events *do* set `session` — to the literal string
  `"live"` (`LiveFeatureSource.session` default in
  `src/preprocessing/live.py`, threaded through to
  `AnomalyEvent.from_lstm_result`), which is truthy and never equals a
  real session id — so for LSTM specifically the filter is not a no-op,
  it's a silent **100% exclusion**: zero of the 402 real
  `lstm_autoencoder` events in the corpus ever pass it, no matter which
  session you ask for. Both bugs were worked around here by passing
  explicit `--start`/`--end` (this session's actual window) instead of
  relying on the session filter at all, so the reported 690/44 counts
  aren't affected by either — but whoever picks this up for Q3 should
  know the LSTM-side bug is an exclusion, not an over-broad match.

## Verdict

Phase 1's stale-data handling retrains and serves cleanly end-to-end —
no crashes, sane convergence, a bounded (not runaway) firing pattern
over the whole drive. That much is solid. The one 44-second LSTM
episode is a genuine open question, not a settled "looks fine": it
escalates from a narrow EVAP/timing signature to 18–21 channels
including engine-load signals, which could be a real cascading
transient or a smoke-config artifact — this needs a human look, not
just this report's read of it, before treating Phase 1 as fully clear
for Pi deploy. The IF replay-diff gap and the two `replay_window`
bugs found along the way are tooling limitations independent of Phase
1's actual changes, not evidence against them. Recommend: real
(non-smoke, full epoch budget) retrain, human review of the
19:44:16–19:44:59Z episode against that drive's actual conditions,
and picking up Q3's replay/build_feature_frame parity fix (plus the
`anomalies_dir` and LSTM-session-filter bugs noted above) before
relying on `replay_window` for IF verification or session-scoped
LSTM history again.
