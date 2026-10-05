# B03.7: runtime replay, replaced set vs promoted set

Recorded 2026-10-05. This compares the replaced behavior set with the
promoted one on the same known recordings. Both run through the daemon's own
live pipeline. It is a **model comparison, not a vehicle-health finding**.
The [JSON](2026-10-05-b03-7-behavior-replay-comparison.json) holds the full
per-model numbers, without the per-tick records.

## What ran

- **Tool:** `tools/replay_behavior_compare.py` with `--shadow-alerting`.
- **Sides:** one `tools/replay_event_diff.drive_session` pass per side. The
  stored rows go into `LiveFeatureSource` on the session clock and tick
  through `LiveInferencePipeline`. `BehaviorDetector` then grades,
  persistence-gates, dedupes, emits and fuses exactly as the daemon does.
  Detectors were behavior-only; IF and sequence were off.
- **Replay state:** the pipeline sees a running replay (vehicle state
  `replay`). The capture-profile gate reads the session's own manifest. All
  three sessions are plain default-profile collection, so the gate allows
  scoring.
- **Served (baseline):** `9cd174ce`, the set replaced on 2026-09-24
  (`data/nissan-xterra-2008/_prior/2026-09-24T024700`, and git `2971d79^`).
- **Candidate:** `816c150b`, the promoted set (B03.9).
- **Isolation:** each side wrote only under `<scratch>/_replay-<run>-<side>/`.
  The artifact was staged at `models/nissan-xterra-2008/`, so the tracked
  `data/nissan-xterra-2008/behavior_policy.yaml` governed both sides.
- **Sessions** (all `ready`, all on calibration ZS44A-20260920 by the registry):

| Session | Ticks per side | Same tick timestamps |
|---|---|---|
| `2026-09-22T233855` | 475 | yes |
| `2026-09-21T194247` | 564 | yes |
| `2026-09-22T234813` | 249 | yes |

## As served: nothing alerts

Every model on both sides is diagnostic-only (`alerting_as_served: false`).
A run without shadow mode on `2026-09-22T234813` wrote **0 behavior events
and 0 fused events** on either side. That is the expected result: neither set
has a validated model.

## Shadow mode: residuals on paired ticks

Under `--shadow-alerting`, every residual is handed to the daemon's
persistence gate as if it were alerting.

Residual metrics are computed **only on ticks both sides scored**
(`n_pairs`). The two maps refuse different regions off-map. Pooling each
side's own scored ticks would let the MAE move with *which* ticks were
scored, not with the model. Ticks scored by one side only are listed apart,
with the other side's refusal reason.

### Fuel trim (STFT %)

| Session | n_pairs | MAE (served → candidate) | Bias | Candidate-only ticks (served refusal) |
|---|---|---|---|---|
| 233855 | 197 | 11.52 → 4.59 | −11.52 → −4.57 | 12 (`off_map`) |
| 194247 | 374 | 9.94 → 2.68 | −9.94 → −2.54 | 0 |
| 234813 | 155 | 10.08 → 2.38 | −10.08 → −2.38 | 0 |

Shadow persisted events were 8 → 0, 16 → 0 and 7 → 0. Fusion votes were
51 → 0, 98 → 0 and 63 → 0.

### VE (%)

| Session | n_pairs | MAE (served → candidate) | Candidate-only ticks |
|---|---|---|---|
| 233855 | 198 | 2.12 → 1.15 | 116 (served `off_map`), MAE 0.82 |
| 194247 | 362 | 1.65 → 0.55 | 176 (served `off_map`), MAE 0.25 |
| 234813 | 233 | 1.28 → 0.53 | 8 (served `off_map`), MAE 0.99 |

Coverage rises because the candidate's map is wider; those ticks are the
`off_map` refusals listed above. **Air mass per cycle** follows the same
pattern: paired MAE 0.059 → 0.050, 0.034 → 0.028 and 0.028 → 0.025 g, with
119, 176 and 8 candidate-only ticks.

### Timing vs rpm/load (°, exploratory)

| Session | n_pairs | MAE (served → candidate) | Only-served ticks | Only-candidate ticks |
|---|---|---|---|---|
| 233855 | 311 | 1.39 → 1.09 | 2 | 20 |
| 194247 | 556 | 0.38 → 0.46 | 4 | 0 |
| 234813 | 244 | 0.44 → 0.44 | 0 | 0 |

The candidate is worse on 194247, so the improvement is not uniform.

### Warm idle (rpm)

| Session | n_pairs | MAE (served → candidate) | Bias | Shadow events |
|---|---|---|---|---|
| 233855 | 172 | 69.8 → 70.8 | +52.6 → +65.1 | 3 → 3 |
| 194247 | 260 | 15.0 → 11.3 | −8.9 → +3.6 | 0 → 0 |
| 234813 | 246 | 16.6 → 13.7 | −5.9 → +6.6 | 0 → 0 |

### The rest

- **Unchanged models:** the demoted `timing_advance_vs_rpm_iat` and the
  quarantined `cat_warmup` are the same artifact on both sides, and they
  replay identically. On 233855 the iat model gave 3 shadow events, 21
  votes and 1 fused contribution on both sides.
- **`air_charge_vs_rpm_throttle_did`:** 5–11 paired ticks. Nearly every tick
  refuses `input_stale:did:ECM_RPM`, because the default profile does not
  poll that DID. The residuals over so few ticks resolve nothing.
- **New models:** `total_trim_*`, `stft_bank_balance_*` are candidate-only.

## Refusals, no-data and context

- **No-data refusals carry different labels.** The same stale-RPM ticks are
  labelled `input_stale:obd:RPM` by the replaced set (no serving contract) and
  `engine_state_stale:obd:RPM` by the refits: 110 ticks on 233855, 1 on
  194247. The underlying data is identical.
- **Calibration context:** the replaced set's models are
  `model_revision_unknown`, and `air_charge` is a `mismatch` (fitted on k106).
  The refits `match` ZS44A-20260920, apart from the carried-over `cat_warmup`
  and `timing_advance_vs_rpm_iat`.
- **IF/sequence:** neither scoped manifest consumes a `deviation:*` column.
  There is no conflict on either side.

## Limits

- Three short drives, 21 minutes in total. This is not a held-out evaluation;
  B02 evidence stays the gate, and every candidate failed it (B03.6).
- With an empty qualified set, this gates no promotion. It is the comparison
  B03.9's next promotion runs first.
