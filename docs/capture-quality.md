# Capture quality: cadence, coverage, yield and attributable loss

Roadmap task C03 (`docs/roadmap/tasks/archive/03-capture-quality-accounting.md`).
This is the contract for what `capture_quality.json` claims about a capture,
how the claim is scored, and what a reader may and may not infer from a
sidecar of each version. It builds on `docs/capture-rounds.md` (C02: round
identity and acquisition clocks) and `docs/analysis-pairing.md` (C01).

## The three defects this replaces

The first evaluator (sidecar `schema_version` 1) quoted the wrong number in
three ways, each reproduced as a regression test before the fix:

| defect | reproduction | before | after |
|---|---|---|---|
| the verdict was the **trailing 60 s** (`CanObdLoopStats.capture_rates`) | five-minute capture, samples only in seconds 240–300 (`test_a_five_minute_capture_with_samples_only_in_the_last_minute_fails`, and end to end through the real writer in `test_the_four_minute_missing_data_case_fails_at_stop`) | 60 observed seconds, 0.25 s max gap, **pass** | `acquisition_gap`, `missing_initial_s: 240`, coverage 0.2, **fail** |
| **cumulative** storage drops described as loss in this capture | 2491 rows lost before capture 1 arms; capture 2 clean (`test_an_earlier_storage_loss_incident_is_not_attributed_to_a_later_clean_capture`) | capture 2 **fail** (`storage_dropped_records: 2491`) | capture 1 `persistence_loss` delta 2491; capture 2 **pass**, `storage.dropped_records` start 2491 / stop 2491 / delta 0 |
| `tuple_bound_is_exact` inferred from `round_timestamps` | A answers even rounds, B odd, both a healthy 2 Hz (`test_equal_per_channel_rates_with_disjoint_rounds_yield_zero_tuples`) | tuple "exact" at 2 Hz | `tuples.complete: 0` of 240 attempted, `tuple_incomplete`; the 2 Hz figure survives only as `tuple_rate_bound_hz` |

## Three questions, three numbers

Per capture member, from arm to stop (never a trailing window):

* **Cadence** — the median interarrival while observing (`cadence.hz` =
  1 / median; p95 beside it). From a bounded reservoir sample of
  interarrivals (`capture_lifetime.InterarrivalReservoir`, 1024 slots,
  Algorithm R) — O(1) memory however long the capture runs. **Never a count
  divided by wall time**: that conflates rate with duty cycle, which is how
  an on-target 14.93 Hz channel read 11.25 Hz on 2026-08-06.
* **Coverage** — observed versus eligible duration: `missing_initial_s`,
  `missing_final_s`, `max_gap_s`, the individually recorded `long_gaps`
  (≥ 2 s), `uncovered_s`, `eligible_s`, `fraction`. A gap longer than
  `max(5 requested periods, 2 s)` is an `acquisition_gap`.
* **Yield** — complete tuples actually persisted, counted per capture round
  from the rows on disk (`tuples.complete`). The slowest member's cadence is
  reported as `tuple_rate_bound_hz` and is *only ever* a bound.

### Pause accounting

`interval` carries `elapsed_s`, `intended_active_s`, `paused_s` and
`pause_windows` (`[[start_s, end_s], …]` on the capture clock, with the
reasons under `pause_reasons`). Windows come from
`tuning_profile.set_paused` (`operator`) and `tx_gate.set_passive`
(`passive`); overlapping reasons form one window. Coverage subtracts the
overlap of every gap with a pause window — that is the one exclusion the
contract makes, and it is made by name. An engine-off stretch with no pause
recorded is a gap like any other: a capture that promised continuous
acquisition does not get engine-off time excluded silently.

### Where the numbers live

* Live: `src/collection/capture_lifetime.py`, a process-wide ledger fed by
  `CanObdLoopStats.record_read` (the same events the trailing window sees),
  begun/ended by `tuning_profile.activate`, reset at every capture start
  (a generation boundary). `/status.capture_lifetime` exposes it beside —
  never inside — `obd_scheduler`'s trailing `achieved_hz_capture` /
  `capture_observed_seconds`, which remain what the dashboard shows.
* Persisted: `src/analysis/capture_coverage.py` rebuilds the same shape from
  the rows (`channel_lifetimes_from_rows`) with the same accumulator, and
  counts tuples (`count_persisted_tuples`).

## Tuples

A tuple is **complete** in a round `(generation, round_id)` when every
`required_tuple` member has at least one persisted observation tagged with
that round **and** the round's measured acquisition spread
(`channels.round_skew`: last `acquired_at` minus first) is within the
profile's `max_tuple_skew_s`. No observation is reused across rounds;
nothing is binned, averaged or interpolated. Reported:

| field | meaning |
|---|---|
| `scheduled` | rounds the scheduler opened with the tuple due — `null` from rows (only the live loop knows; not yet wired, see below) |
| `attempted` | rounds with at least one member persisted |
| `complete` / `persisted` | rounds with every member present within the skew (identical: they are counted from persisted rows) |
| `incomplete` | `attempted − complete` |
| `skew_rejected` | every member present, spread > `max_tuple_skew_s` |
| `member_misses` | per member, rounds it was absent from |
| `cadence` / `coverage` | of the *complete-tuple series*, same definitions as a channel |
| `skew` | p95 / max spread over rounds with all members |
| `source` | `persisted_rows`, or `no_round_evidence` (rows predate `ts_schema` 2 — not a zero yield), `unavailable`, `no_required_tuple` |

`max_tuple_skew_s` is a `CaptureProfile` field (validated to fit inside one
round period at `target_rate_hz`); `null` derives half a period, or 0.25 s
without a target rate.

## Loss epochs

Every daemon loss counter is cumulative since process start.
`HealthMonitor.loss_epoch()` snapshots them all at arm
(`capture_epoch.json`, plus in memory) and again at stop, and
`capture_losses.reconcile_losses` pairs them:

| status | meaning |
|---|---|
| `verified` | both readings, `stop ≥ start`; `delta` is this capture's loss (possibly 0) |
| `reset` | the counter went backwards; loss inside the capture is unknowable |
| `unknown` | one side missing (no `can0` on this host, a sink that never reported, a historic session without epochs); `delta` is `null`, never 0 |

Counters are scoped: `recording` (`storage.*`, `can_receive.*`,
`obd_collect.*`, `ingest.*`, `can0.rx_dropped` / `rx_over_errors`) versus
`dashboard` (`broadcast`, `live` — freshest state wins there by design) and
`derived` (`gps`, `dtc`, `monitor`). Only `recording` affects the verdict;
dashboard loss is a note. A recording counter that is not `verified` puts
`unknown_loss_counters` in `failures` and the verdict becomes `unverified`,
never `pass`.

`obd_collect` (C03.6) is the Canable collector's own handoff: the
`ThreadHandoff` buffer drops and the ingest-queue `QueueDropCounter`, which
previously only reached a log line. `derived_records` (a batched Mode 01
response producing N readings is N records) and `raw_responses` (ECU
responses) are kept apart.

## Batching counters (capture-scoped)

The scheduler's `batching` block in `/status.obd_scheduler` is cumulative
since its collector loop started. Until 2026-09-18 the capture report copied
it verbatim, so a capture reported 102 desyncs of which 99 had happened in an
earlier capture on the same daemon — the loss-epoch defect again, on a
different counter. The report's `batching` block is now a start/stop delta:

* at arm, `record_capture_start` stores `capture_quality.batching_baseline`
  (the scheduler's batching block as it read then) in the `StartEpoch` and
  in `capture_epoch.json` (`batching_baseline`), so a restarted daemon can
  still reconcile against it;
* the live and stop judges pass it as `batching_start`, and
  `capture_quality.scope_batching` returns this capture's share.

```json
"batching": {
  "scope": "capture", "status": "verified", "reset_counters": [],
  "batched_requests": 400, "batched_pids_requested": 2000, "failures": 1, "desyncs": 3,
  "size_narrowings": 0, "failures_while_ecu_silent": 0,
  "mean_pids_per_batch": 5.0, "channel_reads_per_second": 20.0,
  "mode22": {"batched_requests": 400, "members_requested": 1200, "failures": 1, "desyncs": 3,
             "fallbacks": 0, "failures_while_ecu_silent": 0, "mean_dids_per_batch": 3.0},
  "config": {"max_pids": 6, "mode22_max_dids": 4, "known_response_widths": {"0x1208": {"bytes": 2, "source": "measured"}}},
  "baseline": {"...": "the arm-time scheduler block"},
  "lifetime": {"...": "the scheduler block now, cumulative — never this capture's count"}
}
```

| `status` | meaning |
|---|---|
| `verified` | arm-time reading present, no counter went backwards, same collector loop (`loop_started_monotonic`); the top-level and `mode22` counters are this capture's |
| `reset` | a counter went backwards, or the loop restarted (a restart that has since regrown past the baseline is still a reset); every counter is `null`, never negative, and `reset_counters` names the evidence |
| `unknown` | no arm-time reading (an epoch from before this change, or the scheduler had not reported yet at arm); every counter is `null` |

`channel_reads_per_second` is batched reads over the capture interval (pauses
included). `config` holds current settings, not counts. The block is `null`
when batching was off throughout, and a recompute from rows (no scheduler)
carries none. The arm-time reading is the last published scheduler snapshot,
so it may lag the arm by up to one metrics interval. None of this affects the
verdict; a `reset` or `unknown` status adds a note.

## Finalization order (stop)

`src/control/capture_finalize.py`: **boundary** (bump the capture
generation; later readings cannot complete a tuple in this capture) →
**drain** (`HealthMonitor.wait_for_queues_idle` on the ingest and storage
queues, bounded by `DRAIN_TIMEOUT_SECONDS`) → **flush** the writer →
**judge** from the rows on disk → write the sidecar → only then rotate the
writer and reset the profile. A flush failure, a drain timeout or
`rows_on_disk < rows_written` is `persistence_loss`; a passing sidecar is
never written before the final records' persistence outcome is known
(`test_a_late_writer_failure_at_stop_is_recorded_not_hidden`).

## Sidecar v2 schema

```json
{
  "schema_version": 2,
  "scope": "stop",
  "verdict": "pass",
  "profile": "fuel-map",
  "interval": {"source": "live", "elapsed_s": 300.0, "intended_active_s": 240.0,
               "paused_s": 60.0, "pause_windows": [[100.0, 160.0]],
               "pause_reasons": [{"start_s": 100.0, "end_s": 160.0, "reasons": ["operator"]}],
               "generation_at_start": 3},
  "required_tuple": ["RPM", "ENGINE_LOAD", "FUEL_STATUS", "COMMANDED_EQUIV_RATIO", "O2_WR_LAMBDA_S1"],
  "tuple_rate_bound_hz": 3.98,
  "tuples": {"source": "persisted_rows", "max_tuple_skew_s": 0.125, "rate_bound_hz": 3.98,
             "scheduled": null, "attempted": 962, "complete": 951, "persisted": 951,
             "incomplete": 11, "skew_rejected": 2, "member_misses": {"FUEL_STATUS": 9},
             "cadence": {"median_interarrival_s": 0.25, "p95_interarrival_s": 0.26, "hz": 4.0},
             "coverage": {"max_gap_s": 0.5, "fraction": 1.0}, "skew": {"p95_s": 0.011, "max_s": 0.19}},
  "channels": [{"channel": "RPM", "transport_channel": "RPM", "kind": "pid", "requested_hz": 4.0,
                "status": "ok", "in_required_tuple": true,
                "cadence": {"median_interarrival_s": 0.25, "p95_interarrival_s": 0.26, "hz": 4.0, "samples": 959},
                "coverage": {"observations": 960, "first_observed_at": 0.1, "last_observed_at": 299.9,
                             "missing_initial_s": 0.1, "missing_final_s": 0.1, "max_gap_s": 0.5,
                             "long_gaps": [], "gap_threshold_s": 2.0, "uncovered_s": 0.0,
                             "eligible_s": 240.0, "fraction": 1.0},
                "trailing_hz": 4.0}],
  "losses": {"recording": {"status": "verified", "lost": 0, "unverified": [],
                           "counters": {"storage.dropped_records": {"scope": "recording", "start": 2491,
                                        "stop": 2491, "delta": 0, "status": "verified"}}},
             "dashboard": {"status": "verified", "lost": 12, "unverified": [], "counters": {"...": "..."}}},
  "persistence": {"scope": "stop", "generation_boundary": 4, "drained": ["ingest", "storage"],
                  "drain_timed_out": [], "flushed": true, "flush_error": null,
                  "rows_written": 17280, "rows_on_disk": 17280},
  "trailing": {"window_s": 60.0, "observed_seconds": 60.0, "hz": {"RPM": 4.0}, "max_gap_seconds": {}},
  "failures": [],
  "notes": ["60.0s of intentional pause excluded from coverage; engine-off time without a pause is not excluded."]
}
```

`scope` is one of `live` (arm to now, on what has been persisted so far —
`GET /control/capture/quality`, the MCP arm-time verification), `stop` (the
whole capture, after drain and flush), `recompute` (from the rows:
`GET /control/capture/quality/{session_id}`, `get_capture_quality` MCP tool,
`capture_coverage.recompute_capture_quality`) or `trailing_snapshot` — what a
`schema_version` 1 sidecar is read back as by `read_capture_quality`: its
verdict is preserved with its limited scope, its losses are marked unknown,
and the digest says so. Historic sessions are never rewritten; a recompute
reuses a v2 sidecar's recorded end and epochs, and otherwise takes the
interval end from the last persisted row (then a missing tail cannot be
detected, and the notes say so) and reports losses unknown.

**The scoring runs in a child process.** Reading a capture's rows back is
pandas work that holds the GIL: run inline on the daemon's loop it froze HTTP
and collection for ~60 s per call (2026-09-17 — one live check plus the stop
dropped 38,600 records at the OBD handoff and made the stop-time verdict fail
`acquisition_gap` on its own ~49 s hole). Every async caller awaits
`capture_finalize.recompute_quality_off_loop`, which runs
`recompute_capture_quality` in a spawned single-use process; the function
itself refuses to run on a thread with a running event loop. A child that
cannot start, dies or exceeds `RECOMPUTE_TIMEOUT_SECONDS` (600 s) is reported
— `503` on the live and recompute routes, "no verdict" (`None`, logged) at
stop — never waited on.

**It reads only the rows it scores, and caps itself (D03.64, 2026-11-10).**
The score is made of `obd` rows and their round identity, so
`persisted_quality_inputs` reads exactly those (`read_scored_rows`: a
`source == "obd"` filter and four columns at the parquet read, nothing decoded
on read). Before, it read every row with the CAN decoded: on a 108-minute
`tuning` capture (80 % raw CAN, 5 % `obd`) the child reached 6.19 GB on the
Pi, the memory cgroup's OOM killer took it, and systemd failed the whole
daemon unit; on the laptop that session scored in 9.5 GB / 41 s, and now scores
in 0.84 GB / 12 s with the same verdict. Memory follows the capture's `obd`
rows (~1.5 KB each), not its CAN volume. Two figures are whole-session counts
and are taken without that read: `rows_on_disk` is the chunk footers' row count
(every persisted row, the number the writer's `rows_written` is compared with
— it used to be a count of the decoded frame, which included rows synthesised
from the CAN frames), and the interval's end, when none was recorded, still
comes from the last row of *any* source (`last_row_seconds_outside_scored`).
The child also puts `RLIMIT_AS` on itself before reading anything — the digest
child's mechanism, with its RAM-derived ceiling
(`digest.derived_address_space_bytes`, 1–4 GiB) — so a capture that still does
not fit ends as a `MemoryError` in the child and a `CaptureQualityUnavailableError`
naming the cap, never the kernel's OOM kill. The boot-time finalize of an
interrupted capture (`finalize_interrupted_capture`) logs that refusal and the
daemon starts; the capture is left without a `capture_quality.json`, which
every reader takes as unknown, and `GET /control/capture/quality/{session_id}`
scores it later.

### Verdicts

* `pass` — interval ≥ 5 s, every capture-selected channel `ok`, the tuple
  completes at the profile's floor without gaps, every recording counter
  verified at zero delta, a clean flush.
* `degraded` — the only hard constraint is `acquisition_gap`, and every
  gapped channel still covered ≥ 90 % (`DEGRADED_MIN_COVERAGE`) of the
  interval. Usable data with named gaps: both 2026-09-21 tuning drives were
  called `fail` for a 76 s start-up backoff or two 3 s stalls in 17–25 minutes.
* `fail` — any other hard constraint below.
* `unverified` — too short, nothing selected, tuple yield unverifiable
  (`no_round_evidence`), or unknown / reset loss counters. Never a pass.

### Constraint vocabulary (`failures[].clause`)

Each failure names one observed constraint with the `evidence` that
triggered it; there is no advice about budgets or rates, because an old drop
incident is not proof of the current limiting resource.

| clause | fires when | evidence |
|---|---|---|
| `unanswered_input` | a selected channel has zero observations over the interval | `{channel: {observations: 0}}`; a missing tuple member is reported here once, not again as a tuple failure |
| `rate_shortfall` | cadence below 0.9 × requested, or below `min_achieved_rate_hz` for a channel asked for at least that; or the complete-tuple cadence below the floor (`channels: ["__tuple__"]`) | requested / floor, `cadence_hz`, median and p95 interarrival |
| `acquisition_gap` | a pause-adjusted gap (initial, internal or final) exceeds the threshold; or the complete-tuple series has one. Engine-off time is not excluded unless it was an explicit pause: an all-channel silence cannot be told apart from CANary not polling (the C03 case), so a capture stopped long after key-off reads as a gap — usually `degraded`, not `fail` | `max_gap_s`, threshold, `missing_initial_s`, `missing_final_s`, `long_gaps` as `[start, end, uncovered]`, `coverage_fraction` |
| `tuple_incomplete` | rounds were attempted but none completed (members answered in different rounds) | attempted / complete / `member_misses` |
| `tuple_skew` | rounds with every member present but spread beyond `max_tuple_skew_s` dominate | `skew_rejected`, `skew.p95_s` / `max_s`, the declared bound |
| `persistence_loss` | a recording counter's verified delta > 0 (`can0.rx_over_errors`, the controller's hardware overflow, is scope `controller`: reported in `losses` and the notes, never a failure — its cost to any channel is already in that channel's cadence and coverage); a flush error; a drain timeout; `rows_on_disk < rows_written` | the counters' start / stop / delta, or the flush error |
| `unknown_loss_counters` | any recording counter `unknown` or `reset` (soft: verdict `unverified`) | the counters and their status |

Per-channel `status` is `ok` / `missing` / `shortfall` / `gap`.

## Examples

**Pass** — the sidecar above: 300 s armed, a 60 s operator pause excluded,
951 complete tuples, every recording counter verified at zero delta (the
storage counter sat at 2491 from an earlier incident throughout).

**Fail** — the four-minute case: `interval.elapsed_s: 300`, every channel
`gap` with `missing_initial_s: 240` and `coverage.fraction: 0.2`, while the
`trailing` block still reads 60 observed seconds at 4 Hz — reported, and
labelled as what it is.

**Unverified** — a 2026-08 session recomputed on the laptop: tuples counted
from its rows, but `losses.recording.status: unknown` (no epochs were
recorded and there is no `can0` here), so the verdict cannot claim no loss.

## Consumers swept

`POST /control/capture/stop` returns the v2 payload and writes it;
`GET /control/capture/quality` (live) and `GET /control/capture/quality/{id}`
(recompute, with the stored sidecar beside it) are new;
`arm_tuning_capture` verifies through the live route and hoists `scope` and
`failing_constraints`; `get_capture_quality` is the MCP read tool;
`tuning_digest` carries the sidecar verbatim and notes a v1 sidecar's
trailing scope; `types/control.ts` `CaptureQuality` is the v2 shape and the
capture panel shows the scope and the complete-tuple count.

## Not done here

* Live round-level tuple accounting (`tuples.scheduled`, and a tuple count
  before any row is flushed) needs a hook in `RoundScheduler.close`, which
  is in the collector-loop region owned concurrently by C04; the live
  verdict counts tuples from the rows flushed so far instead.
* `ObdSchedulerMetrics.achieved_hz_capture` / `capture_observed_seconds`
  keep their names (same region); they are documented as trailing and the
  lifetime block lives under `/status.capture_lifetime`.
* Nothing here was measured on the truck: the drain timeout, the round
  skew the ECM actually produces and the Pi's flush latency at stop are
  C10's.
