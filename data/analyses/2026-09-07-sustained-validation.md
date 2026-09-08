# Sustained collection validation on the synthetic bus — before/after C09 (2026-09-07)

Roadmap task C10 (`docs/roadmap/tasks/10-sustained-validation.md`); harness
and method in [`docs/sustained-validation.md`](../../docs/sustained-validation.md).
Per-scenario JSON: [`sustained-validation/before/`](sustained-validation/before/)
(commit `2257b53`, pre-C09, run from a read-only worktree) and
[`sustained-validation/after/`](sustained-validation/after/) (HEAD). Raw
sessions were written under `data/_validation/{before,after}/<scenario>/`
(`_`-prefixed; Parquet is gitignored) and are not committed.

**Read this first.** Every number below comes from a scripted ECU on a
workstation (WSL2, `hardware.adapter: synthetic`). The daemon's software path
— collector thread, `ThreadHandoff`, fanout, Parquet writer, capture
finalize, inference loop — is the shipped code; the adapter, the ECU's
latency (a `sleep`), the model artifacts (IF is a declared stub, the
sequence detector absent) and the kernel counters (`unavailable`) are not.
No profile capacity claim in `data/nissan-xterra-2008/capture_profiles.yaml`
is established here; the Pi procedure in the doc above is still pending.

## Host

| field | value |
|---|---|
| platform | Linux-6.6.87.2-microsoft-standard-WSL2 (WSL2), Python 3.12, hostname `CorsairDesktop` |
| dependencies | pyarrow 25.0.1, pandas 3.0.5, numpy 2.5.2, python-can 4.6.1, scikit-learn 1.9.0, torch 2.13.0 |
| vehicle | profile slug `nissan-xterra-2008` (config identity only; the ECU is `SyntheticEcu`) |
| clock | `time.time()` steps by ~0.9 s every 1–2 min on this host; runs that saw a step were rerun (`timing.wall_clock_steps.attempts_used` in each JSON) |
| power / thermal / kernel counters | unavailable |

## Headline per scenario (after = HEAD)

| scenario | limiting stage | sequence accounting | C03 verdict → with adapter ledger | tuples | batch occupancy | inference (1 Hz) |
|---|---|---|---|---|---|---|
| normal-monitoring | ECU-unsupported channels (33 direct probe timeouts on an 8 req/s budget) | reconciled, 0 missing | fail (`unanswered_input`: 30 auto-probed PIDs the ECU does not serve) → fail | — | 4.74 PIDs/batch | 18/18 ticks, lateness p95 1.4 ms, feature p95 55 ms |
| fuel-map | feature stage on the loop (heartbeat p99 27 ms, no loss) | reconciled | fail (`acquisition_gap` on `COMMANDED_EQUIV_RATIO`, finding 3) → fail | 32/40 complete, skew 0.0 s (one batched response) | 4.69 PIDs/batch | 16/16, p95 1.2 ms |
| tuning-did | feature stage on the loop | reconciled | unverified (`can0` unknown) → **pass** | — | 148 batched Mode 22 (2.2/batch) + 1093 single | 16/16 |
| fuel-did-research | feature stage on the loop | reconciled | fail (`tuple_skew`: 0/199, skew max 64 ms vs 25 ms bound; findings 4–5) → fail | 0/199 | 199 batched + 990 single | 16/16 |
| hybrid-mode01-mode22 | feature stage on the loop | reconciled | unverified → **pass** | — | 82 Mode 01 batches (4.3), 40 Mode 22 batches | 16/16 |
| passive-sniffer | feature stage on the loop | reconciled | unverified → unverified (nothing polled; `sent == []`) | — | — | 14/14 |
| fault-channels | ECU-unsupported channel | reconciled | fail (`unanswered_input: INTAKE_TEMP`, expected) → fail | 32/40 | 4.51 PIDs/batch | 16/16 |
| transitions | ECU-unsupported channel (expected: fuel-map channels in the tuning-did phase) | reconciled; 4 frames lost in the faulted adapter | unverified → unverified (`persistence_loss` from `adapter.rx_dropped: 4`) | 16/30 (fuel-map phase) | 61 Mode 01 + 58 Mode 22 batches | 19/19 |
| overload-storage-stall | **storage sink** | reconciled with counted loss: 3506 missing ≤ 3742 counted | fail (`persistence_loss`) → fail | 13/21 | 4.74 | 11 completed, **4 missed (overrun)** — finding 8 |
| overload-rate | **bus budget (request pacing)** | reconciled | fail (`rate_shortfall` + `unanswered_input`) → fail | 0/34 | batching off | 15/15 |
| replay-drive-slice | feature stage on the loop | not applicable | not a capture | — | — | 49/49 at 0.5 s, p95 1.2 ms |

Accounting held in every scenario: `collector_pushed == persisted_raw_rows +
buffer_dropped + queue_dropped + storage_dropped` exactly, and (without a
reconnect) `collector_pushed − readings_emitted == adapter_delivered_frames`.
Decoded rows equalled `persisted broadcast frames × 2` in every synthetic
run. No `UNEXPLAINED_LOSS` anywhere.

## Known-answer checks (C10.7)

| pair | scenario | outcome | slope | `b − a` bias / spread | pairs | separation p95 |
|---|---|---|---|---|---|---|
| `can:SYNTH_RPM ~ obd:RPM` | fuel-map | agrees 1:1 | 1.00004 | 0.0 / 0.7 rpm | 40 | 4.9 ms |
| `can:SYNTH_RPM_HALF ~ obd:RPM` (wrong scale) | fuel-map | differs as expected | 2.00008 | 725 / 204 rpm | 40 | 4.9 ms |
| `did:ECM_RPM ~ can:SYNTH_RPM` | tuning-did | agrees 1:1 | 0.99998 | −0.15 / 1.2 rpm | 148 | — |
| `did:ECM_RPM ~ obd:RPM` (across the hybrid cycle) | hybrid | bounded | 0.994 | 4.3 / 43 rpm | 37 | 134 ms |
| `can:ECM_RPM ~ obd:RPM` (C01 ground truth, replayed) | replay | agrees 1:1 | 0.9915 | −5.5 / 13.4 rpm | 50 | 25 ms |
| `can:WHEEL_SPEED_6F7_A ~ obd:SPEED` (replayed) | replay | agrees 1:1 | 1.0029 | −0.36 / 0.73 km/h | 50 | 26 ms |

`can:SYNTH_SPEED ~ obd:SPEED` was refused in every run (10–11 pairs at the
1 Hz context rate over a 10 s capture, below `MIN_PAIRS`); a refusal is
reported as a refusal. The hybrid cross-mode pair is the honest bound on
pairing Mode 01 against Mode 22 inside one 0.25 s cycle: 153 ms median
separation, so a 470 rpm/s slew shows as ±43 rpm.

## What improved before → after (C10.8, C10.17)

Generated by `scripts/sustained_validation.py summarize` (field by field; a value shown once was identical on both sides):

### `fault-channels` — Fault and transition cases (silent/unsupported/delayed)

before `2257b53` → after `8b03717`; INTAKE_TEMP never answers, EVAPORATIVE_PURGE is refused (NRC 0x12), and every reply takes 60 ms.

| field | before → after |
|---|---|
| limiting stage | ecu_unsupported_channel |
| sequence accounting | reconciled |
| sequences missing | 0 |
| push identity | yes |
| storage drops | 0 |
| C03 verdict | fail |
| verdict w/ adapter ledger | fail |
| failures | unanswered_input, acquisition_gap, acquisition_gap, unknown_loss_counters |
| expected failure met | yes |
| complete tuples | 32 |
| tuple skew max s | 0 |
| batch occupancy | batched_requests=84; mean_pids_per_batch=4.92 → batched_requests=91; mean_pids_per_batch=4.51 |
| ECU batches seen | mode01_batched_requests_seen=84; mode22_batched_requests_seen=0; mode22_single_requests_seen=0 → mode01_batched_requests_seen=91; mode22_batched_requests_seen=0; mode22_single_requests_seen=0 |
| inference | feature_p95_ms=55.5; lateness_max_ms=1.12; lateness_p95_ms=1.09; missed_ticks=0; ticks_completed=16; ticks_scheduled=16 → feature_p95_ms=56.1; lateness_max_ms=0.967; lateness_p95_ms=0.931; missed_ticks=0; ticks_completed=16; ticks_scheduled=16 |
| loop heartbeat p99 ms | 31.9 → 31.2 |
| known answer | outcome=agrees_1to1; pair=can:SYNTH_RPM ~ obd:RPM; slope=0.998, outcome=differs_as_expected; pair=can:SYNTH_RPM_HALF ~ obd:RPM; slope=2, outcome=refused; pair=can:SYNTH_SPEED ~ obd:SPEED; slope=— |

### `fuel-did-research` — Fuel DID research

before `2257b53` → after `8b03717`; 

| field | before → after |
|---|---|
| limiting stage | inference_feature_stage_on_loop |
| sequence accounting | reconciled |
| sequences missing | 0 |
| push identity | yes |
| storage drops | 0 |
| C03 verdict | fail |
| verdict w/ adapter ledger | fail |
| failures | tuple_skew, acquisition_gap, unknown_loss_counters → tuple_skew, unknown_loss_counters |
| expected failure met | yes |
| complete tuples | 95 → 0 |
| tuple skew max s | 0.06 → 0.0641 |
| batch occupancy | — → batched_requests=0; mean_pids_per_batch=— |
| ECU batches seen | mode01_batched_requests_seen=0; mode22_batched_requests_seen=0; mode22_single_requests_seen=1381 → mode01_batched_requests_seen=0; mode22_batched_requests_seen=199; mode22_single_requests_seen=990 |
| inference | feature_p95_ms=63.1; lateness_max_ms=1.15; lateness_p95_ms=1.1; missed_ticks=0; ticks_completed=16; ticks_scheduled=16 → feature_p95_ms=67.8; lateness_max_ms=1.17; lateness_p95_ms=1.15; missed_ticks=0; ticks_completed=16; ticks_scheduled=16 |
| loop heartbeat p99 ms | 31.7 → 36.4 |
| known answer | outcome=refused; pair=can:SYNTH_RPM ~ obd:RPM; slope=—, outcome=refused; pair=can:SYNTH_RPM_HALF ~ obd:RPM; slope=—, outcome=refused; pair=can:SYNTH_SPEED ~ obd:SPEED; slope=—, outcome=agrees_1to1; pair=did:ECM_RPM ~ can:SYNTH_RPM; slope=1 |

### `fuel-map` — Fuel-map

before `2257b53` → after `8b03717`; 

| field | before → after |
|---|---|
| limiting stage | inference_feature_stage_on_loop |
| sequence accounting | reconciled |
| sequences missing | 0 |
| push identity | yes |
| storage drops | 0 |
| C03 verdict | fail |
| verdict w/ adapter ledger | fail |
| failures | acquisition_gap, acquisition_gap, unknown_loss_counters |
| expected failure met | yes |
| complete tuples | 33 → 32 |
| tuple skew max s | 0 |
| batch occupancy | batched_requests=92; mean_pids_per_batch=4.73 → batched_requests=90; mean_pids_per_batch=4.69 |
| ECU batches seen | mode01_batched_requests_seen=92; mode22_batched_requests_seen=0; mode22_single_requests_seen=0 → mode01_batched_requests_seen=90; mode22_batched_requests_seen=0; mode22_single_requests_seen=0 |
| inference | feature_p95_ms=53.2; lateness_max_ms=1.15; lateness_p95_ms=0.917; missed_ticks=0; ticks_completed=16; ticks_scheduled=16 → feature_p95_ms=54.7; lateness_max_ms=1.25; lateness_p95_ms=1.23; missed_ticks=0; ticks_completed=16; ticks_scheduled=16 |
| loop heartbeat p99 ms | 29.3 → 27.1 |
| known answer | outcome=agrees_1to1; pair=can:SYNTH_RPM ~ obd:RPM; slope=1, outcome=differs_as_expected; pair=can:SYNTH_RPM_HALF ~ obd:RPM; slope=2, outcome=refused; pair=can:SYNTH_SPEED ~ obd:SPEED; slope=— |

### `hybrid-mode01-mode22` — Hybrid Mode 01/22

before `2257b53` → after `8b03717`; 

| field | before → after |
|---|---|
| limiting stage | inference_feature_stage_on_loop |
| sequence accounting | reconciled |
| sequences missing | 0 |
| push identity | yes |
| storage drops | 0 |
| C03 verdict | fail → unverified |
| verdict w/ adapter ledger | fail → pass |
| failures | acquisition_gap, unknown_loss_counters → unknown_loss_counters |
| expected failure met | yes |
| complete tuples | — |
| tuple skew max s | — |
| batch occupancy | batched_requests=82; mean_pids_per_batch=4.28 |
| ECU batches seen | mode01_batched_requests_seen=82; mode22_batched_requests_seen=0; mode22_single_requests_seen=360 → mode01_batched_requests_seen=82; mode22_batched_requests_seen=40; mode22_single_requests_seen=285 |
| inference | feature_p95_ms=53; lateness_max_ms=1.17; lateness_p95_ms=1.1; missed_ticks=0; ticks_completed=16; ticks_scheduled=16 → feature_p95_ms=61.8; lateness_max_ms=1.34; lateness_p95_ms=1; missed_ticks=0; ticks_completed=16; ticks_scheduled=16 |
| loop heartbeat p99 ms | 27.9 → 32.6 |
| known answer | outcome=agrees_1to1; pair=can:SYNTH_RPM ~ obd:RPM; slope=1, outcome=differs_as_expected; pair=can:SYNTH_RPM_HALF ~ obd:RPM; slope=2, outcome=refused; pair=can:SYNTH_SPEED ~ obd:SPEED; slope=—, outcome=agrees_1to1; pair=did:ECM_RPM ~ can:SYNTH_RPM; slope=1, outcome=bounded; pair=did:ECM_RPM ~ obd:RPM; slope=0.993 → outcome=agrees_1to1; pair=can:SYNTH_RPM ~ obd:RPM; slope=1, outcome=differs_as_expected; pair=can:SYNTH_RPM_HALF ~ obd:RPM; slope=2, outcome=refused; pair=can:SYNTH_SPEED ~ obd:SPEED; slope=—, outcome=agrees_1to1; pair=did:ECM_RPM ~ can:SYNTH_RPM; slope=1, outcome=bounded; pair=did:ECM_RPM ~ obd:RPM; slope=0.994 |

### `normal-monitoring` — Normal monitoring with discovery

before `2257b53` → after `8b03717`; Support discovery walks the Mode 01 bitmap groups; the DTC poll is unanswered by the synthetic ECU (a timeout, counted).

| field | before → after |
|---|---|
| limiting stage | ecu_unsupported_channel |
| sequence accounting | reconciled |
| sequences missing | 0 |
| push identity | yes |
| storage drops | 0 |
| C03 verdict | fail |
| verdict w/ adapter ledger | fail |
| failures | unanswered_input, unknown_loss_counters |
| expected failure met | yes |
| complete tuples | — |
| tuple skew max s | — |
| batch occupancy | batched_requests=38; mean_pids_per_batch=4.74 |
| ECU batches seen | mode01_batched_requests_seen=38; mode22_batched_requests_seen=0; mode22_single_requests_seen=2 → mode01_batched_requests_seen=38; mode22_batched_requests_seen=0; mode22_single_requests_seen=0 |
| inference | feature_p95_ms=156; lateness_max_ms=4.73; lateness_p95_ms=1.32; missed_ticks=0; ticks_completed=18; ticks_scheduled=18 → feature_p95_ms=55.3; lateness_max_ms=1.9; lateness_p95_ms=1.38; missed_ticks=0; ticks_completed=18; ticks_scheduled=18 |
| loop heartbeat p99 ms | 61 → 36.6 |
| known answer | outcome=refused; pair=can:SYNTH_RPM ~ obd:RPM; slope=—, outcome=refused; pair=can:SYNTH_RPM_HALF ~ obd:RPM; slope=—, outcome=refused; pair=can:SYNTH_SPEED ~ obd:SPEED; slope=— |

### `overload-rate` — Overload (rate contract)

before `2257b53` → after `8b03717`; Sixteen channels at 4 Hz on a 4 req/s budget with batching off.

| field | before → after |
|---|---|
| limiting stage | bus_budget_request_pacing |
| sequence accounting | reconciled |
| sequences missing | 0 |
| push identity | yes |
| storage drops | 0 |
| C03 verdict | fail |
| verdict w/ adapter ledger | fail |
| failures | unanswered_input, rate_shortfall, unknown_loss_counters |
| expected failure met | yes |
| complete tuples | 0 |
| tuple skew max s | — |
| batch occupancy | — |
| ECU batches seen | mode01_batched_requests_seen=0; mode22_batched_requests_seen=0; mode22_single_requests_seen=0 |
| inference | feature_p95_ms=64; lateness_max_ms=1.18; lateness_p95_ms=1.16; missed_ticks=0; ticks_completed=15; ticks_scheduled=15 → feature_p95_ms=46.5; lateness_max_ms=0.878; lateness_p95_ms=0.867; missed_ticks=0; ticks_completed=15; ticks_scheduled=15 |
| loop heartbeat p99 ms | 30 → 26.4 |
| known answer | outcome=agrees_1to1; pair=can:SYNTH_RPM ~ obd:RPM; slope=1, outcome=differs_as_expected; pair=can:SYNTH_RPM_HALF ~ obd:RPM; slope=2, outcome=refused; pair=can:SYNTH_SPEED ~ obd:SPEED; slope=— |

### `overload-storage-stall` — Overload (accounting proof)

before `2257b53` → after `8b03717`; A wedged writer under a flooded bus with a small queue: loss must be counted at the stage it happens, never hidden.

| field | before → after |
|---|---|
| limiting stage | storage_sink |
| sequence accounting | reconciled_with_counted_loss |
| sequences missing | 3855 → 3506 |
| push identity | yes |
| storage drops | 4126 → 3742 |
| C03 verdict | fail |
| verdict w/ adapter ledger | fail |
| failures | acquisition_gap, acquisition_gap, persistence_loss, unknown_loss_counters |
| expected failure met | yes |
| complete tuples | 11 → 13 |
| tuple skew max s | 0 |
| batch occupancy | batched_requests=84; mean_pids_per_batch=4.74 |
| ECU batches seen | mode01_batched_requests_seen=84; mode22_batched_requests_seen=0; mode22_single_requests_seen=0 |
| inference | feature_p95_ms=69.8; lateness_max_ms=37.5; lateness_p95_ms=37.5; missed_ticks=4; ticks_completed=11; ticks_scheduled=11 → feature_p95_ms=66.5; lateness_max_ms=0.748; lateness_p95_ms=0.748; missed_ticks=4; ticks_completed=11; ticks_scheduled=11 |
| loop heartbeat p99 ms | 4.4 → 4.32 |
| known answer | outcome=refused; pair=can:SYNTH_RPM ~ obd:RPM; slope=—, outcome=refused; pair=can:SYNTH_RPM_HALF ~ obd:RPM; slope=—, outcome=refused; pair=can:SYNTH_SPEED ~ obd:SPEED; slope=— |

### `passive-sniffer` — Passive / legacy ELM327 companion

before `2257b53` → after `8b03717`; The ELM327 companion is not fakeable offline without the ELM327-emulator serial integration tier; declared not exercised.

| field | before → after |
|---|---|
| limiting stage | inference_feature_stage_on_loop |
| sequence accounting | reconciled |
| sequences missing | 0 |
| push identity | yes |
| storage drops | 0 |
| C03 verdict | unverified |
| verdict w/ adapter ledger | unverified |
| failures | unknown_loss_counters |
| expected failure met | yes |
| complete tuples | — |
| tuple skew max s | — |
| batch occupancy | — |
| ECU batches seen | mode01_batched_requests_seen=0; mode22_batched_requests_seen=0; mode22_single_requests_seen=0 |
| inference | feature_p95_ms=17.2; lateness_max_ms=1.09; lateness_p95_ms=1.03; missed_ticks=0; ticks_completed=14; ticks_scheduled=14 → feature_p95_ms=17.1; lateness_max_ms=0.933; lateness_p95_ms=0.907; missed_ticks=0; ticks_completed=14; ticks_scheduled=14 |
| loop heartbeat p99 ms | 5.6 → 6.26 |
| known answer | none |

### `replay-drive-slice` — Replay (C10.9)

before `2257b53` → after `8b03717`; tests/fixtures/ground_truth/drive-slice at 2x through the isolated replay path. Labelled replay: no transport or ECU capacity claim.

| field | before → after |
|---|---|
| limiting stage | inference_feature_stage_on_loop |
| sequence accounting | not_applicable |
| sequences missing | — |
| push identity | — |
| storage drops | 0 |
| C03 verdict | — |
| verdict w/ adapter ledger | — |
| failures | none |
| expected failure met | yes |
| complete tuples | — |
| tuple skew max s | — |
| batch occupancy | — |
| ECU batches seen | mode01_batched_requests_seen=0; mode22_batched_requests_seen=0; mode22_single_requests_seen=0 |
| inference | feature_p95_ms=42.7; lateness_max_ms=1.18; lateness_p95_ms=1.14; missed_ticks=0; ticks_completed=49; ticks_scheduled=49 → feature_p95_ms=40.9; lateness_max_ms=2.03; lateness_p95_ms=1.16; missed_ticks=0; ticks_completed=49; ticks_scheduled=49 |
| loop heartbeat p99 ms | 33.3 → 32.6 |
| known answer | outcome=agrees_1to1; pair=can:ECM_RPM ~ obd:RPM; slope=0.992, outcome=agrees_1to1; pair=can:WHEEL_SPEED_6F7_A ~ obd:SPEED; slope=1 |

### `transitions` — Fault and transition cases (pause/profile change/reconnect/flush)

before `2257b53` → after `8b03717`; Quality is judged against the fuel-map manifest the capture was armed with, but tuning_profile.activate restarts the capture ledger, so the stop-time interval is the tuning-did phase and every fuel-map channel is an unanswered input there — the named failure is expected. The pause window and the fuel-map phase are kept in generation_events.

| field | before → after |
|---|---|
| limiting stage | ecu_unsupported_channel |
| sequence accounting | reconciled |
| sequences missing | 0 |
| push identity | yes |
| storage drops | 0 |
| C03 verdict | unverified |
| verdict w/ adapter ledger | unverified |
| failures | unanswered_input, unknown_loss_counters |
| expected failure met | yes |
| complete tuples | 16 |
| tuple skew max s | 0 |
| batch occupancy | batched_requests=34; mean_pids_per_batch=4.38 → batched_requests=34; mean_pids_per_batch=4.44 |
| ECU batches seen | mode01_batched_requests_seen=61; mode22_batched_requests_seen=0; mode22_single_requests_seen=622 → mode01_batched_requests_seen=61; mode22_batched_requests_seen=58; mode22_single_requests_seen=429 |
| inference | feature_p95_ms=64.5; lateness_max_ms=0.909; lateness_p95_ms=0.893; missed_ticks=0; ticks_completed=19; ticks_scheduled=19 → feature_p95_ms=67.7; lateness_max_ms=0.916; lateness_p95_ms=0.842; missed_ticks=0; ticks_completed=19; ticks_scheduled=19 |
| loop heartbeat p99 ms | 29.6 → 26.6 |
| known answer | outcome=refused; pair=can:SYNTH_RPM ~ obd:RPM; slope=—, outcome=refused; pair=can:SYNTH_RPM_HALF ~ obd:RPM; slope=—, outcome=refused; pair=can:SYNTH_SPEED ~ obd:SPEED; slope=— |

### `tuning-did` — Continuous tuning DID

before `2257b53` → after `8b03717`; 

| field | before → after |
|---|---|
| limiting stage | inference_feature_stage_on_loop |
| sequence accounting | reconciled |
| sequences missing | 0 |
| push identity | yes |
| storage drops | 0 |
| C03 verdict | unverified |
| verdict w/ adapter ledger | pass |
| failures | unknown_loss_counters |
| expected failure met | yes |
| complete tuples | — |
| tuple skew max s | — |
| batch occupancy | — → batched_requests=0; mean_pids_per_batch=— |
| ECU batches seen | mode01_batched_requests_seen=0; mode22_batched_requests_seen=0; mode22_single_requests_seen=1613 → mode01_batched_requests_seen=0; mode22_batched_requests_seen=148; mode22_single_requests_seen=1093 |
| inference | feature_p95_ms=60.7; lateness_max_ms=1.16; lateness_p95_ms=1.1; missed_ticks=0; ticks_completed=16; ticks_scheduled=16 → feature_p95_ms=73.6; lateness_max_ms=1.27; lateness_p95_ms=1.26; missed_ticks=0; ticks_completed=16; ticks_scheduled=16 |
| loop heartbeat p99 ms | 29.8 → 47.1 |
| known answer | outcome=refused; pair=can:SYNTH_RPM ~ obd:RPM; slope=—, outcome=refused; pair=can:SYNTH_RPM_HALF ~ obd:RPM; slope=—, outcome=refused; pair=can:SYNTH_SPEED ~ obd:SPEED; slope=—, outcome=agrees_1to1; pair=did:ECM_RPM ~ can:SYNTH_RPM; slope=1 |


Reading the diff:

- **Per-DID cadence and weights (C09.1)** — after: `tuning-did`'s weighted
  members run at 3.0 and 7.5 Hz (`mode22_rate_weights` 0.2 / 0.5) beside
  the nine at 15.0 Hz. Before: the field does not exist (dropped and
  reported in `provenance.profile_fields_unsupported_by_this_tree`) and
  all eleven run at 15.0 Hz — 1613 single Mode 22 requests in ~10 s against
  1241 after (148 batched × 2.2 + 1093 single) for the same channel reads.
- **Mode 22 batching (C09.4)** — after: 148 batched requests in
  `tuning-did`, 199 in `fuel-did-research`, 40 in the hybrid profile, 58 in
  `transitions`, and the ECU-side count agrees with the scheduler's every
  time. Before: `did_batch_max_dids` does not exist; 1613 / 1381 / 360 / 622
  single requests respectively. Cadence, session keep-alives (4, no lapses)
  and the hybrid cycle (0.250 s median and p95, 0 missed of 20, cross-mode
  separation 163 → 153 ms) are the same on both sides: on this bus the
  budget was never the constraint, so batching bought request count, not
  rate.
- **Resolved polling plan in the manifest (C09.6)** — after: the recompute
  path judges against the plan's per-member `configured_rate_hz` and
  `resolved_plan_demand` is reported (e.g. fuel-DID 120 req/s best-case
  batched vs 140 single). Before: no plan, `resolved_plan_demand:
  unavailable`; the requests are synthesised from the profile without
  rates.
- **Fuel-DID tuple yield did not improve** — 95/199 complete before,
  0/199 after (39/199 in an earlier after run): with the tuple preference
  not resolving DID names (finding 4) the members' order inside a 50 ms
  round is whatever the due-order phase makes it, and batching two of the
  four members moved the other two. `tuple_skew` is named on both sides.
- **Budget override, Mode 01 batching, fuel-map tuples** — identical on
  both sides (they predate C09): 4.7 PIDs per batch, 32–33 of 40–41
  tuples complete with 0.0 s skew, the same `acquisition_gap` on
  `COMMANDED_EQUIV_RATIO` (finding 3).
- **Overloads** — the same named failures on both sides
  (`persistence_loss` with 3855 / 3506 missing sequences all covered by
  counted drops; `rate_shortfall` + priority-lane starvation), which is the
  point: C09 did not change what happens when the contract cannot be met.

## Which resource limited each mode on the synthetic bus

- **Mode 01 captures at 20 req/s (fuel-map)**: nothing in the collector or
  storage path; the first stage to show is the inference feature stage
  holding the loop 40–60 ms per tick (heartbeat p99 ≈ 30 ms, max ≤ 137 ms).
  Budget utilisation ≈ 50 %.
- **Continuous DID at 160–180 req/s**: the same; 1200–1300 requests in
  ~10 s, zero timeouts, session held (4 keep-alives, 0 lapses). The
  fuel-DID tuple is limited by **which widths are known** (findings 4–5),
  not by the bus.
- **Hybrid at 90 req/s**: the cycle plan is feasible (0.194 s of 0.25 s) and
  achieved — cycle duration 0.250 s median and p95, 0 missed of 20,
  entry→DID 12 ms, exit→Mode 01 63 ms median.
- **Storage under a wedged writer**: the storage sink, by design of the
  scenario — and through it the inference loop (finding 8).
- **Rate overload**: request pacing, with the priority lane (`RPM`,
  `THROTTLE_POS`) starving every other member at 4 req/s.
- CPU (process-wide, harness included): 9–23 % of one core; RSS 0.7–0.8 GB
  (torch import). Writer flush p95 12–76 ms; storage growth 5–17 kB/s.

## Findings (defects surfaced; only 9 fixed here)

See `docs/sustained-validation.md` for reproductions. In short:

1. WSL2 wall-clock steps break acquisition-time pairing (environment;
   detected and rerun).
2. A continuous-DID capture armed before its DIDs' first staggered due
   time waits the default 60 s window interval; the same after a reconnect.
3. Channels keep the previous profile's due time across activation
   (`acquisition_gap` at capture start).
4. `tuning_profile.required_tuple()` does not resolve DID names, so DID
   tuples get no round-order preference (`tuple_skew`).
5. Only three Mode 22 widths carry provenance; the fuel-DID tuple cannot
   batch into two requests.
6. The feature stage's loop occupancy is the first thing to show at every
   non-overloaded workload.
7. `default` is not a capture; its recompute uses a 2 s gap threshold for
   slow-tier channels.
8. **Derived-channel persistence couples the inference loop to the writer
   lock**: under the storage stall `publish_derived` took 2.53 s and the
   loop missed four deadlines (`overrun`), with heartbeat max 97 ms.
9. **A saturated sink queue reported depth 0** — the storage sink dropped
   3506 records behind a 256-deep queue whose `queue_depth` read 0 by the
   time anything looked. Fixed: `telemetry_sinks.<sink>.queue_high_water`
   now keeps the monotonic peak, taken by the fanout while the consumer is
   still blocked. **The tables above predate the fix**; their JSON has no
   `queue_high_water` key and a re-run at HEAD carries it.

## Unverified (all hardware)

Real SocketCAN stamp skew, the ECM's response times and batched widths,
kernel receive drops, Pi CPU/thermal/power, chrony behaviour, the 20–30
minute captures per profile (C10.10), vehicle-state-phased interpretation
(C10.11), incremental budget comparison on the truck (C10.12), and every
rate a profile names.
